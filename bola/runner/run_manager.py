"""Drive a whole BOLA run: orchestrate the phases and own all the persistence.

The strategy, the snapshotter, and the analyzer were each built as pure components that deliberately
lack a lifecycle — they decide, capture, or judge, but they do not loop, persist, or resume. This
module is the loop and the bookkeeping they leave out. It runs the phases in order:

    setup → explore → snap_before → attack → snap_hacker → snap_after → analyze

`explore` and `attack` drive a `Strategy`: `plan()` → execute each action through the `ApiExecutor`
→ `harvest` the response into the `FieldRepo` → `observe(results)`. Every executed action is
appended to the log (`ExecutionRecord`) the moment it is sent, and that log — not the repo — is the
source of truth: the `FieldRepo` is a pure in-memory projection rebuilt on resume by replaying
`harvest` over the logged responses. The attacker inherits the regular user's ids by `fork()`ing
that repo, then replays them under its own auth (the BOLA manoeuvre). The AI strategy's
`AgentMemory` is the one piece of state that is *not* replayable, so it is snapshotted per turn and
fed back into a restored strategy on resume.

Resume is by reduced budget: on re-entry the runner counts the actions/turns already logged for a
phase and constructs the strategy with that much less budget, so completed work is never re-sent —
which matters because both phases mutate the target. Snapshot capture and analysis are each
independently resumable (they skip what is already on disk), so simply re-invoking `execute_run`
with the same `run_id` continues an interrupted run.

HTTP goes through the executor's `Transport` seam, so tests inject a fake transport and a fake
strategy and drive the entire loop with no network and no LLM.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from tqdm import tqdm

from bola.aggregate.runner import aggregate_run, write_report_json
from bola.analysis.runner import AnalysisResult, analyze_run
from bola.db.models import RunRecord
from bola.db.store import Store
from bola.execution.api_executor import (
    ApiExecutor,
    AuthSession,
    Transport,
    build_auth_session,
    requests_transport,
)
from bola.execution.field_repo import FieldRepo, harvest
from bola.execution.snapshotter import (
    SNAP_AFTER,
    SNAP_BEFORE,
    SNAP_HACKER,
    capture_snapshots,
)
from bola.runner import metrics
from bola.settings import Settings
from bola.spec import views
from bola.spec.model import Spec
from bola.strategies.ai_strategy import AIStrategy
from bola.strategies.base import ATTACKER, REGULAR, ActionResult, Strategy, StrategyContext
from bola.strategies.models import AgentMemory
from bola.strategies.random_strategy import RandomStrategy
from bola.target.manifest import TargetManifest

logger = logging.getLogger("bola.runner.run_manager")

# The combined build+attack loop logs under one phase; identity (regular|attacker) distinguishes
# the credentials each action used.
RUN_PHASE = "run"

Relations = list[dict]


@dataclass
class _Resume:
    """What a resumed run reconstructs from the log before continuing."""

    next_seq: int = 0
    turns_done: int = 0
    regular_done: int = 0
    attacker_done: int = 0
    snap_before_done: bool = False
    memory: Optional[AgentMemory] = None


# Build the single run-spanning strategy, given the resume state (budget already spent + memory).
StrategyFactory = Callable[[StrategyContext, _Resume], Strategy]


@dataclass
class RunResult:
    """The outcome of a run: id, completion, call counts, and the analysis + metrics."""

    run_id: str
    completed: bool = False
    regular_calls: int = 0
    hacker_calls: int = 0
    analysis: Optional[AnalysisResult] = None
    metrics: dict = field(default_factory=dict)
    report: Optional[Any] = None  # the aggregated AggregatedReport, when the report phase ran


def execute_run(
    spec: Spec,
    manifest: TargetManifest,
    relations: Relations,
    settings: Settings,
    store: Store,
    *,
    run_id: Optional[str] = None,
    transport: Transport = requests_transport,
    strategy_factory: Optional[StrategyFactory] = None,
    ground_truth: Optional[list[str]] = None,
    analyze: bool = True,
    analyzer: Any = None,
    aggregator: Any = None,
    callbacks: Optional[list] = None,
    show_progress: bool = True,
) -> RunResult:
    """Run all execution phases for one target, then (by default) one analysis pass."""
    run_id = run_id or str(uuid.uuid4())
    operations = _without_auth_login(views.operations(spec), manifest)
    factory = strategy_factory or _default_factory(callbacks)

    _ensure_run(store, run_id, manifest, settings, len(operations))

    executor = ApiExecutor(
        manifest.base_url,
        transport=transport,
        timeout=settings.test.request_timeout,
        max_retries=settings.test.api_max_retries,
    )
    auths = {
        REGULAR: _auth(manifest, manifest.users.regular.vars, transport, settings),
        ATTACKER: _auth(manifest, manifest.users.attacker.vars, transport, settings),
    }

    try:
        # One continuous strategy spans build (regular) → attack (attacker) with one memory; the
        # FieldRepo is a single shared pool rebuilt from the whole log. The build→attack boundary is
        # detected from the first attacker-tagged action, where the victim baseline (snap_before) is
        # captured — see `_run_combined`.
        repo = FieldRepo()
        _replay(repo, store, run_id, relations)
        _run_combined(
            spec, operations, relations, repo, executor, auths,
            settings, store, run_id, factory, show_progress, access=manifest.access,
        )

        capture_snapshots(
            SNAP_HACKER, run_id, spec, operations, relations, repo, executor, store,
            auth=auths[ATTACKER], on_error="continue", show_progress=show_progress,
        )
        capture_snapshots(
            SNAP_AFTER, run_id, spec, operations, relations, repo, executor, store,
            auth=auths[REGULAR], on_error="continue", show_progress=show_progress,
        )

        run_metrics = metrics.compute_run_metrics(
            run_id, store, total_operations=len(operations)
        )
        store.update_run(
            run_id,
            metrics_json=json.dumps(run_metrics),
            status="completed",
            completed_at=datetime.now(timezone.utc),
        )
        _write_run_json(settings, run_id, manifest, run_metrics, store)

        result = RunResult(
            run_id=run_id,
            completed=True,
            regular_calls=run_metrics["execution"]["regular_calls"],
            hacker_calls=run_metrics["execution"]["hacker_calls"],
            metrics=run_metrics,
        )

        if analyze:
            analysis, full_metrics = analyze_and_report(
                run_id, settings, store,
                access_description=manifest.access,
                ground_truth=ground_truth,
                total_operations=len(operations),
                analyzer=analyzer,
                analysis_id=f"{run_id}-auto",  # stable id: interrupted auto-pass resumes, not forks
                callbacks=callbacks,
                show_progress=show_progress,
            )
            result.analysis = analysis
            result.metrics = full_metrics
            if analysis.completed:
                store.update_run(run_id, findings_json=json.dumps(analysis.findings))

            # Final phase: fuse the analysis findings + the explorer's memory into one report.
            # Telemetry-grade, never fatal — the run and analysis are already saved, so a failure
            # here just means regenerating later with `bola report aggregate`.
            try:
                agg = aggregate_run(
                    run_id, settings, store,
                    spec=spec,
                    manifest=manifest,
                    access_description=manifest.access,
                    analysis_id=f"{run_id}-auto",
                    aggregator=aggregator,
                    callbacks=callbacks,
                )
                write_report_json(settings, run_id, agg.payload)
                result.report = agg.report
            except Exception:
                logger.exception(
                    "report aggregation failed — run + analysis saved; regenerate with "
                    "`bola report aggregate %s`", run_id,
                )
        return result
    except KeyboardInterrupt:
        # Ctrl-C: each phase already checkpointed and re-raised. Propagate to halt the run cleanly;
        # state is on disk, a rerun with the same run_id resumes. (Status stays "running".)
        raise
    except Exception:
        # An LLM-call timeout (e.g. in `strategy.plan()`) or any other unexpected error: log it with
        # a traceback, mark the run failed, and stop. API-call errors never reach here — the executor
        # catches them and records a failed call so the loop continues. Everything is append-only, so
        # a rerun resumes from the log.
        logger.exception("run %s failed — progress saved, rerun the same run_id to resume", run_id)
        store.update_run(run_id, status="failed")
        raise


def analyze_and_report(
    run_id: str,
    settings: Settings,
    store: Store,
    *,
    access_description: str = "",
    ground_truth: Optional[list[str]] = None,
    total_operations: Optional[int] = None,
    analysis_id: Optional[str] = None,
    analyzer: Any = None,
    callbacks: Optional[list] = None,
    show_progress: bool = True,
) -> tuple[AnalysisResult, dict]:
    """Run one analysis pass over a run's snapshots and persist its result + metrics.

    Re-runnable: each call is a fresh pass (a new `analysis_id`) accumulating against the same
    execution `run_id`, and writes its own metrics — to the `AnalysisRecord` row and to a per-pass
    JSON file — so passes under different access assumptions or models stay comparable.
    """
    analysis = analyze_run(
        run_id, settings, store,
        analyzer=analyzer,
        access_description=access_description,
        analysis_id=analysis_id,
        callbacks=callbacks,
        show_progress=show_progress,
    )
    total_ops = total_operations if total_operations is not None else _total_ops(store, run_id)
    full_metrics = metrics.compute_metrics(
        run_id, store,
        total_operations=total_ops,
        findings=analysis.findings,
        ground_truth=ground_truth,
        analysis_elapsed_s=analysis.elapsed_s,
        analysis_tokens=analysis.tokens,
    )
    store.save_analysis_metrics(analysis.analysis_id, full_metrics)
    _write_analysis_json(settings, run_id, analysis, full_metrics, access_description, store)
    return analysis, full_metrics


# --------------------------------------------------------------------------------------
# Combined build → attack loop
# --------------------------------------------------------------------------------------


def _run_combined(
    spec: Spec,
    operations: list,
    relations: Relations,
    repo: FieldRepo,
    executor: ApiExecutor,
    auths: dict[str, AuthSession],
    settings: Settings,
    store: Store,
    run_id: str,
    factory: StrategyFactory,
    show_progress: bool,
    access: str = "",
) -> None:
    """Drive one continuous strategy: plan → execute (under the per-action identity's auth) →
    harvest → observe → snapshot memory. The victim baseline (`snap_before`) is captured the moment
    the first `attacker`-tagged action appears (or at the end if the run never attacks)."""
    resume = _resume_state(store, run_id)
    ctx = StrategyContext(
        spec=spec, relations=relations, field_repo=repo,
        settings=settings, operations=operations, identities=list(auths),
        access=access,
    )
    strategy = factory(ctx, resume)

    seq = resume.next_seq
    turn_index = resume.turns_done
    # The baseline is *settled* once an attacker has acted; before that the victim state is still
    # pristine, so an interrupted baseline can be safely completed on resume. Gating on
    # `attacker_done` (not on the presence of snap_before rows) is what makes that completion happen.
    snap_before_taken = resume.attacker_done > 0
    interrupted = False
    bar = tqdm(desc="Run", unit="call", disable=not show_progress)
    try:
        while not strategy.is_done():
            actions = strategy.plan()
            if not actions:
                # An empty plan is ambiguous: the strategy may be finished, or it may have just
                # crossed build→attack on a turn it proposed no calls. Defer to is_done() rather
                # than treating "nothing this turn" as "done" — otherwise a run can end before it
                # ever attacks. (Both strategies advance toward is_done() each turn, so it is bounded.)
                if strategy.is_done():
                    break
                continue
            if not snap_before_taken and any(a.identity == ATTACKER for a in actions):
                # Capture the victim baseline before any attacker call. A Ctrl-C here re-raises (so
                # the attack never starts and the run halts); the loop's handler below catches it.
                _snap_before(spec, operations, relations, repo, executor, store, run_id,
                             auths[REGULAR], show_progress)
                snap_before_taken = True
            results: list[ActionResult] = []
            revise_max = settings.test.revise_max
            for action in actions:
                auth = auths.get(action.identity, auths[REGULAR])
                res = executor.execute(action.request, auth)
                # A 4xx *input* error (400/422 — not 401/403 auth refusals, not 404 not-found) means the
                # call reached the right operation but a field was malformed. Let the strategy repair it
                # and resend, a bounded number of times, so a fixable request isn't wasted (and the
                # sweep isn't stuck re-issuing the same bad value).
                revises = 0
                while (res.status_code in (400, 422) and revises < revise_max
                       and hasattr(strategy, "revise")):
                    revised = strategy.revise(action, res)
                    if revised is None:
                        break
                    action = revised
                    res = executor.execute(action.request, auth)
                    revises += 1
                store.append_execution(
                    run_id, seq, RUN_PHASE, action.op_key,
                    identity=action.identity, turn_index=turn_index, rationale=action.rationale,
                    request=asdict(action.request), media_type=res.media_type,
                    status_code=res.status_code, ok=res.ok,
                    response=res.response, error=res.error,
                )
                seq += 1
                if res.response is not None and res.status_code:
                    harvest(
                        repo, action.op_key, res.status_code,
                        res.media_type or "application/json", res.response, relations,
                        owner=action.identity,
                        created=action.request.method.upper() == "POST",
                    )
                results.append(ActionResult(action=action, result=res))
            strategy.observe(results)
            mem = getattr(strategy, "memory", None)
            if isinstance(mem, AgentMemory):
                store.save_strategy_memory(
                    run_id, RUN_PHASE, turn_index, mem.model_dump(),
                    tokens=getattr(strategy, "last_turn_tokens", None),
                )
            turn_index += 1
            bar.update(len(actions))
    except KeyboardInterrupt:
        interrupted = True
    finally:
        bar.close()  # close the bar on a normal finish, a Ctrl-C, or a propagating exception

    if interrupted:
        # Stop the whole run — every action/turn/snapshot is already committed, so re-invoking
        # `execute_run` with the same run_id resumes from the log. Propagated (not swallowed) so the
        # later snapshot/analysis phases do not run on a half-finished attack.
        logger.warning("interrupted — progress saved, rerun the same run_id to resume")
        raise KeyboardInterrupt
    # A final reflection so the last turn's results (typically the attack) reach memory — plan()
    # only ever reasons over the previous turn, so otherwise the attack's findings would be lost.
    if strategy.finalize():
        mem = getattr(strategy, "memory", None)
        if isinstance(mem, AgentMemory):
            store.save_strategy_memory(
                run_id, RUN_PHASE, turn_index, mem.model_dump(),
                tokens=getattr(strategy, "last_turn_tokens", None),
            )
            turn_index += 1
    if not snap_before_taken:  # a run that never attacked still needs a victim baseline
        _snap_before(spec, operations, relations, repo, executor, store, run_id,
                     auths[REGULAR], show_progress)


def _without_auth_login(operations, manifest):
    """Drop the manifest's auth-login operation from the strategy's surface.

    Authentication is applied automatically by the runner (the manifest's `auth`), so exposing the
    login endpoint as a callable operation only invites the agent to waste turns "logging in" with
    guessed credentials — which on a weak model can consume the whole build phase and leave an empty
    id pool. Removing it is target-agnostic: it is identified purely from the manifest's auth config.
    """
    login = getattr(getattr(manifest, "auth", None), "login", None)
    if not login:
        return operations
    method = str(getattr(login.method, "value", login.method)).upper()
    return [op for op in operations if not (op.path == login.path and op.method.upper() == method)]


def _snap_before(spec, operations, relations, repo, executor, store, run_id, auth, show_progress):
    """Capture the victim baseline (`snap_before`) over the tracked objects with regular auth."""
    return capture_snapshots(
        SNAP_BEFORE, run_id, spec, operations, relations, repo, executor, store,
        auth=auth, on_error="continue", show_progress=show_progress,
    )


def _resume_state(store: Store, run_id: str) -> _Resume:
    """Reconstruct budget-spent + memory + boundary state from the log, for resume."""
    execs = store.get_executions(run_id)
    memories = store.get_strategy_memories(run_id, RUN_PHASE)
    snaps = store.get_snapshots(run_id)
    return _Resume(
        next_seq=(execs[-1]["seq"] + 1) if execs else 0,
        turns_done=len(memories),
        regular_done=sum(1 for e in execs if e["identity"] == REGULAR),
        attacker_done=sum(1 for e in execs if e["identity"] == ATTACKER),
        snap_before_done=any(
            s["phase"] == SNAP_BEFORE and s["status"] == "done" for s in snaps
        ),
        memory=AgentMemory.model_validate(memories[-1]["memory"]) if memories else None,
    )


def _replay(repo: FieldRepo, store: Store, run_id: str, relations: Relations) -> None:
    """Rebuild the shared FieldRepo by replaying `harvest` over every logged response."""
    for e in store.get_executions(run_id):
        if e["response"] is None or not e["status_code"]:
            continue
        method = str((e.get("request") or {}).get("method", "")).upper()
        harvest(
            repo, e["op_key"], e["status_code"],
            e.get("media_type") or "application/json", e["response"], relations,
            owner=e.get("identity", "regular"),
            created=method == "POST",
        )


# --------------------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------------------


def _default_factory(callbacks: Optional[list]) -> StrategyFactory:
    """A strategy factory honoring `settings.test.strategy`, with resume budgets/memory applied."""

    def factory(ctx: StrategyContext, resume: _Resume) -> Strategy:
        """Construct the run's strategy with budget reduced by what the log already spent."""
        s = ctx.settings
        if s.test.strategy == "ai":
            start_in_attack = resume.snap_before_done or resume.attacker_done > 0
            return AIStrategy(
                ctx,
                memory=resume.memory,
                max_turns=max(0, s.test.ai_max_turns - resume.turns_done),
                build_cap=max(0, s.test.ai_build_max_turns - resume.turns_done),
                start_in_attack=start_in_attack,
                callbacks=callbacks,
            )
        return RandomStrategy(
            ctx,
            max_actions=max(0, s.test.regular_ops - resume.regular_done),
            attacker_actions=max(0, s.test.hacker_ops - resume.attacker_done),
        )

    return factory


def _auth(
    manifest: TargetManifest, user_vars: dict, transport: Transport, settings: Settings
) -> AuthSession:
    """Build an `AuthSession` for one identity from the manifest auth + that user's vars."""
    return build_auth_session(
        manifest.auth, user_vars, manifest.base_url,
        transport=transport, timeout=settings.test.request_timeout,
    )


def _ensure_run(
    store: Store, run_id: str, manifest: TargetManifest, settings: Settings, total_ops: int
) -> None:
    """Create the run record on first entry; a no-op when resuming an existing run."""
    if store.get_run(run_id) is not None:
        return
    store.create_run(RunRecord(
        run_id=run_id,
        target_name=manifest.base_url,
        strategy=settings.test.strategy,
        status="running",
        config_json=json.dumps({
            "base_url": manifest.base_url,
            "strategy": settings.test.strategy,
            "total_operations": total_ops,
        }),
    ))


def _total_ops(store: Store, run_id: str) -> int:
    """The operation count recorded in the run's config (0 if unknown/unparseable)."""
    run = store.get_run(run_id)
    if run is None:
        return 0
    try:
        return int(json.loads(run.config_json).get("total_operations", 0))
    except (json.JSONDecodeError, TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------------------
# Result files (alongside the SQLite records)
# --------------------------------------------------------------------------------------


def _write_run_json(
    settings: Settings, run_id: str, manifest: TargetManifest, run_metrics: dict, store: Store
) -> None:
    """Write `runs/<id>/run.json` — run metrics plus the strategy memory trail."""
    _write_json(settings, run_id, "run.json", {
        "run_id": run_id,
        "target": manifest.base_url,
        "strategy": settings.test.strategy,
        "metrics": run_metrics,
        "strategy_memory": store.get_strategy_memories(run_id),
    })


def _write_analysis_json(
    settings: Settings,
    run_id: str,
    analysis: AnalysisResult,
    full_metrics: dict,
    access_description: str,
    store: Store,
) -> None:
    """Write `runs/<id>/analysis_<aid>.json` — one analysis pass's findings, metrics, and memory."""
    _write_json(settings, run_id, f"analysis_{analysis.analysis_id}.json", {
        "run_id": run_id,
        "analysis_id": analysis.analysis_id,
        "access_description": access_description,
        "completed": analysis.completed,
        "metrics": full_metrics,
        "findings": analysis.findings,
        "strategy_memory": store.get_strategy_memories(run_id),
    })


def _write_json(settings: Settings, run_id: str, name: str, payload: Any) -> None:
    """Write `payload` as pretty JSON to `runs/<run_id>/<name>`, creating the directory."""
    run_dir = Path(settings.storage.runs_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / name).write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
"""Orchestrate one report-aggregation pass over a finished run, and persist it.

Unlike relation detection and analysis, aggregation is a single LLM call over already-persisted
evidence, so there is no per-unit checkpointing: `aggregate_run` gathers the inputs (the analysis
pass's findings + metrics, the AI memory, a spec summary, the target's access model), calls the
`ReportAggregator`, and upserts the structured report into the store (one per run; regenerating
overwrites). `write_report_json` emits the same report — wrapped with the deterministic metrics and
run metadata — as `runs/<run_id>/report.json`, the artifact a later HTML view renders. It never
re-contacts the target.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from langchain_core.callbacks import UsageMetadataCallbackHandler

from bola.aggregate.aggregator import ReportAggregator
from bola.aggregate.context import build_report_context, spec_overview, summarize_spec
from bola.aggregate.models import AggregatedReport
from bola.db.store import Store
from bola.llm import usage_totals
from bola.settings import Settings
from bola.spec.model import Spec
from bola.spec.views import spec_hash
from bola.target.manifest import TargetManifest

logger = logging.getLogger("bola.aggregate.runner")


@dataclass
class AggregateResult:
    """Outcome of one aggregation pass: the report, the persisted payload, and this call's cost."""

    run_id: str
    report: Optional[AggregatedReport] = None
    payload: dict = field(default_factory=dict)
    elapsed_s: float = 0.0
    tokens: dict[str, int] = field(
        default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    )


def aggregate_run(
    run_id: str,
    settings: Settings,
    store: Store,
    *,
    spec: Optional[Spec] = None,
    manifest: Optional[TargetManifest] = None,
    access_description: str = "",
    analysis_id: Optional[str] = None,
    aggregator: Any = None,
    callbacks: Optional[list] = None,
) -> AggregateResult:
    """Aggregate one run's evidence into a structured report and persist it.

    `analysis_id` selects which analysis pass's findings to fold in; when omitted (or unknown) the
    newest pass for the run is used. `spec`/`access_description` enrich the prompt (operation summary
    + intended authorization rules); both are optional so the standalone command can run from a bare
    run id. The aggregator is injectable for tests.

    The persisted payload is **self-contained**: alongside the LLM report it embeds the full
    evidence the report is grounded in — the target manifest, the raw spec, every API call (request
    + response bodies), every snapshot, the AI memory trail, and the analysis pass — so the JSON
    file alone is a portable, DB-free record of the run. `manifest`/`spec` are embedded only when
    passed (the standalone command may run from a bare run id).
    """
    started = time.perf_counter()
    usage = UsageMetadataCallbackHandler()

    analysis = store.get_analysis(analysis_id) if analysis_id else None
    if analysis is None:
        passes = store.list_analyses(run_id)
        if passes:
            analysis = store.get_analysis(passes[0]["analysis_id"])

    findings = (analysis or {}).get("findings") or []
    metrics = (analysis or {}).get("metrics") or {}
    run = store.get_run(run_id)
    if not metrics and run and run.metrics_json:
        metrics = json.loads(run.metrics_json)
    target = run.target_name if run else ""
    memories = store.get_strategy_memories(run_id)
    executions = store.get_executions(run_id)
    snapshots = store.get_snapshots(run_id)

    context = build_report_context(
        target=target,
        access_description=access_description,
        spec_summary=summarize_spec(spec) if spec is not None else [],
        metrics=metrics,
        analysis_findings=findings,
        ai_memory=memories,
        executions=_compact_executions(executions),
    )

    aggregator = aggregator or ReportAggregator(settings, callbacks=[*(callbacks or []), usage])
    report = aggregator.aggregate(context)
    model_name = getattr(aggregator, "model_name", settings.llm.analysis_model or settings.llm.model)
    aid = (analysis or {}).get("analysis_id")
    tokens = usage_totals(usage)  # this step's own slice (zero when an aggregator is injected)
    elapsed = round(time.perf_counter() - started, 3)

    steps = _build_steps(
        store, spec, settings, analysis, metrics, executions,
        aggregation_tokens=tokens, aggregation_duration_s=elapsed,
    )
    payload = {
        "run_id": run_id,
        "target": target,
        "analysis_id": aid,
        "model": model_name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "metrics": metrics,
        "steps": steps,
        # At-a-glance digest of the spec surface and the calls made against it.
        "summary": {
            "spec": spec_overview(spec) if spec is not None else None,
            "api_calls": _api_call_summary(executions),
        },
        "report": report.model_dump(),
        # Full self-contained evidence: the JSON file alone reconstructs the run without the DB.
        "manifest": manifest.model_dump(mode="json") if manifest is not None else None,
        "spec": spec.raw if spec is not None else None,
        "api_calls": {
            "summary": _compact_executions(executions),
            "full": executions,
        },
        "snapshots": snapshots,
        "ai_memory": memories,
        "analysis": analysis,
    }
    store.save_aggregated_report(run_id, aid, model_name, payload, tokens=tokens, duration_s=elapsed)

    result = AggregateResult(run_id=run_id, report=report, payload=payload, tokens=tokens)
    result.elapsed_s = elapsed
    logger.info(
        "aggregated report for run %s: %d BOLA, %d non-BOLA, %d open question(s)",
        run_id, len(report.bola_findings), len(report.non_bola_findings), len(report.open_questions),
    )
    return result


def _compact_executions(executions: list[dict]) -> list[dict]:
    """The execution log trimmed to the authoritative HTTP facts the report grounds findings on."""
    out: list[dict] = []
    for e in executions or []:
        req = e.get("request") or {}
        out.append({
            "identity": e.get("identity"),
            "op_key": e.get("op_key"),
            "method": req.get("method"),
            "path": req.get("path"),
            "path_params": req.get("path_params") or {},
            "status_code": e.get("status_code"),
            "ok": e.get("ok"),
        })
    return out


def _api_call_summary(executions: list[dict]) -> dict:
    """At-a-glance call digest: total, per-identity (regular vs attacker) counts, and a
    status-code histogram. Derived from the execution log so it stands on its own in the payload."""
    by_identity = {"regular": 0, "attacker": 0}
    by_status: dict[str, int] = {}
    for e in executions or []:
        identity = e.get("identity", "regular")
        by_identity[identity] = by_identity.get(identity, 0) + 1
        code = e.get("status_code")
        if code:
            by_status[str(code)] = by_status.get(str(code), 0) + 1
    return {
        "total": len(executions or []),
        "by_identity": by_identity,
        "by_status_code": dict(sorted(by_status.items())),
    }


def _zero_tokens() -> dict:
    return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


def _exec_span_seconds(executions: list[dict]) -> Optional[float]:
    """Wall-clock of the exploration+attack phase, from the execution log's created_at span.

    The strategy step is logged per action rather than as one timed call, so its duration is taken
    as the span between the first and last logged action (includes API latency, not just LLM time).
    """
    stamps = sorted(e["created_at"] for e in executions or [] if e.get("created_at"))
    if len(stamps) < 2:
        return 0.0 if stamps else None
    try:
        return round(
            (datetime.fromisoformat(stamps[-1]) - datetime.fromisoformat(stamps[0])).total_seconds(),
            3,
        )
    except (ValueError, TypeError):
        return None


def _build_steps(
    store: Store,
    spec: Optional[Spec],
    settings: Settings,
    analysis: Optional[dict],
    metrics: dict,
    executions: list[dict],
    *,
    aggregation_tokens: dict,
    aggregation_duration_s: float,
) -> dict:
    """Per-step `{tokens, duration_s}` breakdown, sourced from each step's own persisted record."""
    rel = (
        store.get_relation_usage(spec_hash(spec), settings.llm.model)
        if spec is not None else None
    ) or {}
    strategy_tokens = (metrics.get("llm") or {}).get("strategy_tokens") or _zero_tokens()
    return {
        "relations": {
            "tokens": rel.get("tokens") or _zero_tokens(),
            "duration_s": rel.get("duration_s"),
        },
        "strategy": {
            "tokens": strategy_tokens,
            "duration_s": _exec_span_seconds(executions),
        },
        "analysis": {
            "tokens": (analysis or {}).get("tokens") or _zero_tokens(),
            "duration_s": (analysis or {}).get("duration_s"),
        },
        "aggregation": {
            "tokens": aggregation_tokens,
            "duration_s": aggregation_duration_s,
        },
    }


def write_report_json(settings: Settings, run_id: str, payload: dict) -> Path:
    """Write `runs/<run_id>/report.json` (the HTML view's source + the portable run bundle), returning its path.

    The payload is self-contained — report, manifest, raw spec, API calls, snapshots, AI memory,
    and analysis — so the file can be shared and read back without the database.
    """
    run_dir = Path(settings.storage.runs_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "report.json"
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path
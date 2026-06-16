"""Orchestrate one analysis pass over a run's snapshots, resumably and re-runnably.

A pass reads the frozen snapshots for a `run_id`, groups them into objects, and judges each object
for BOLA. It never re-contacts the target — it is a pure function of persisted evidence — so the
same execution can be analysed many times: a **new `analysis_id`** (a different `access`
description, or a different model on the `Analyzer`) is a fresh pass that accumulates alongside the
others.

Each object is the checkpoint unit (`AnalysisItemResult`): a done object is skipped on resume, a
failed/missing one is retried, and survivors in a failing batch stay checkpointed — the relation
runner's pattern. `objects_per_call` packs objects into one prompt; `batch_size` runs that many
prompts concurrently. When every object is done, the per-object verdicts are consolidated into the
`AnalysisRecord` projection.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Iterator, Optional

from langchain_core.callbacks import UsageMetadataCallbackHandler
from tqdm import tqdm

from bola.analysis.analyzer import Analyzer
from bola.analysis.context import group_objects, response_objects
from bola.db.store import Store
from bola.llm import usage_totals
from bola.settings import Settings

logger = logging.getLogger("bola.analysis.runner")


@dataclass
class AnalysisResult:
    """Outcome of one analysis pass: per-object counts and the consolidated findings."""

    analysis_id: str = ""
    objects_total: int = 0
    objects_done: int = 0
    objects_skipped: int = 0
    objects_failed: int = 0
    findings: list[dict] = field(default_factory=list)
    completed: bool = False
    elapsed_s: float = 0.0  # wall-clock of this analyze_run call (resumed passes measure only their own slice)
    tokens: dict[str, int] = field(
        default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    )  # LLM tokens this call spent (resumed passes count only their own slice; zero when an analyzer is injected)


def analyze_run(
    run_id: str,
    settings: Settings,
    store: Store,
    *,
    analyzer: Optional[Analyzer] = None,
    access_description: str = "",
    analysis_id: Optional[str] = None,
    show_progress: bool = True,
    callbacks: Optional[list] = None,
    reraise_interrupt: bool = True,
) -> AnalysisResult:
    """Run a resumable BOLA analysis pass over `run_id`'s snapshots, returning its verdicts.

    On Ctrl-C each finished verdict is already checkpointed; by default the interrupt is **re-raised**
    after that (so a run manager / CLI halts cleanly and a rerun resumes the pass). Pass
    `reraise_interrupt=False` to swallow it and return a partial `AnalysisResult` instead.
    """
    started = time.perf_counter()
    usage = UsageMetadataCallbackHandler()
    analyzer = analyzer or Analyzer(
        settings, access_description=access_description,
        callbacks=[*(callbacks or []), usage],
    )
    analysis_id = analysis_id or _new_analysis_id()
    access_hash = hashlib.sha256(analyzer.access_description.encode("utf-8")).hexdigest()
    store.start_analysis(
        analysis_id,
        run_id,
        analyzer.model_name,
        access_hash,
        {"objects_per_call": settings.analysis.objects_per_call,
         "batch_size": settings.analysis.batch_size},
    )

    objects = group_objects(store.get_snapshots(run_id))
    # Reply-only crossings (reveals/reports/searches/deep reads) leave no snapshot, so they are
    # reconstructed from the execution log and judged by the response-judge in a second pass. Guarded
    # so an injected analyzer without the method (or a run with no executions) is a clean no-op.
    response_objs = (
        response_objects(store.get_executions(run_id))
        if hasattr(analyzer, "analyze_response_batches") else []
    )
    all_objects = objects + response_objs
    by_key = {o["object_key"]: o for o in all_objects}
    checkpoints = store.get_analysis_items(analysis_id)

    verdicts: dict[str, dict] = {
        key: cp["verdict"] or {}
        for key, cp in checkpoints.items()
        if cp["status"] == "done" and key in by_key
    }
    result = AnalysisResult(
        analysis_id=analysis_id,
        objects_total=len(all_objects),
        objects_skipped=sum(1 for o in all_objects if o["object_key"] in verdicts),
    )

    per_call = max(1, settings.analysis.objects_per_call)
    batch_size = max(1, settings.analysis.batch_size)

    bar = tqdm(
        total=len(all_objects),
        initial=result.objects_skipped,
        desc="Analyzing objects",
        unit="obj",
        disable=not show_progress,
    )
    stopped = False
    interrupted = False

    def _apply(group: list[dict], outcome) -> None:
        """Record one judged group's outcome into verdicts/result/bar; set `stopped` on a stop error."""
        nonlocal stopped
        if isinstance(outcome, BaseException):
            for obj in group:
                store.save_analysis_item(analysis_id, obj["object_key"], "failed", error=str(outcome))
                result.objects_failed += 1
                bar.update(1)
            logger.error("analysis batch failed: %s", outcome)
            if settings.analysis.on_error == "stop":
                stopped = True
            return
        by_verdict = {v.object_key: v for v in outcome.verdicts}
        for obj in group:
            verdict = by_verdict.get(obj["object_key"])
            if verdict is None:
                store.save_analysis_item(
                    analysis_id, obj["object_key"], "failed",
                    error="model returned no verdict for this object",
                )
                result.objects_failed += 1
                if settings.analysis.on_error == "stop":
                    stopped = True
            else:
                data = verdict.model_dump()
                store.save_analysis_item(analysis_id, obj["object_key"], "done", verdict=data)
                verdicts[obj["object_key"]] = data
                result.objects_done += 1
            bar.update(1)

    def _run_pass(pending: list[dict], judge) -> None:
        """Judge `pending` in concurrent batches via `judge`, checkpointing each group's outcome."""
        nonlocal stopped, interrupted
        for batch in _chunks(list(_chunks(pending, per_call)), batch_size):
            try:
                outcomes = judge(batch)
            except KeyboardInterrupt:
                logger.warning("interrupted — analysis progress saved, rerun to resume")
                stopped = interrupted = True
                return
            for group, outcome in zip(batch, outcomes):
                _apply(group, outcome)
            if stopped:
                return

    _run_pass([o for o in objects if o["object_key"] not in verdicts], analyzer.analyze_batches)
    if not stopped and response_objs:
        _run_pass(
            [o for o in response_objs if o["object_key"] not in verdicts],
            analyzer.analyze_response_batches,
        )

    bar.close()

    result.elapsed_s = round(time.perf_counter() - started, 3)
    result.tokens = usage_totals(usage)
    if interrupted and reraise_interrupt:
        raise KeyboardInterrupt
    # "Completed" means the pass ran to the end without a *stop*; individual objects that failed
    # under `on_error: continue` must not sink the whole pass — consolidate every verdict we have, so
    # one bad object can no longer hide the rest of the findings from the report.
    result.completed = not stopped and len(verdicts) + result.objects_failed == len(all_objects)
    if result.completed:
        result.findings = [verdicts[o["object_key"]] for o in all_objects if o["object_key"] in verdicts]
        store.finish_analysis(
            analysis_id, result.findings, tokens=result.tokens, duration_s=result.elapsed_s
        )
        positives = sum(1 for f in result.findings if f.get("is_bola"))
        logger.info(
            "analysis %s complete: %d object(s) judged (%d failed), %d BOLA finding(s)",
            analysis_id, len(result.findings), result.objects_failed, positives,
        )
    return result


def _new_analysis_id() -> str:
    """A fresh random analysis-pass id."""
    return str(uuid.uuid4())


def _chunks(items: list, size: int) -> Iterator[list]:
    """Yield successive `size`-length slices of `items`."""
    for i in range(0, len(items), size):
        yield items[i : i + size]
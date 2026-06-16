"""Orchestrate relation detection across a whole spec, resumably.

Enumerates operations, chunks each source's targets into groups of `group_size`, and runs the
detector once per `source × group` unit. Each unit is checkpointed (`RelationGroupResult`), so
a crash, an error (`on_error: stop`), or a Ctrl-C leaves progress on disk and a rerun skips the
units already done. When every unit is done, the consolidated edges are written to
`RelationCache` for downstream steps.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Iterator, Optional

from langchain_core.callbacks import UsageMetadataCallbackHandler
from tqdm import tqdm

from bola.db.store import Store
from bola.llm import usage_totals
from bola.relations.detector import RelationDetector
from bola.relations.models import GroupRelations, Relation
from bola.settings import Settings
from bola.spec import views
from bola.spec.model import Spec
from bola.spec.views import OperationRef, spec_hash

logger = logging.getLogger("bola.relations.runner")


@dataclass
class DetectionResult:
    """Outcome of a detection run: the consolidated relations and per-unit counts."""

    relations: list[dict] = field(default_factory=list)
    units_total: int = 0
    units_done: int = 0
    units_skipped: int = 0
    units_failed: int = 0
    completed: bool = False
    elapsed_s: float = 0.0  # wall-clock of this detect_relations call (resumed runs measure only their own slice)
    tokens: dict[str, int] = field(
        default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    )  # LLM tokens this call spent (resumed runs count only their own slice; zero when a detector is injected)


@dataclass
class _Unit:
    """One detection work unit: a source op, a group index, and that group's target ops."""

    source: OperationRef
    group_index: int
    targets: list[OperationRef]


def detect_relations(
    spec: Spec,
    settings: Settings,
    store: Store,
    *,
    detector: Optional[RelationDetector] = None,
    show_progress: bool = True,
    callbacks: Optional[list] = None,
    reraise_interrupt: bool = True,
) -> DetectionResult:
    """Run resumable relation detection over `spec`, returning the consolidated result.

    On Ctrl-C each finished `(source, group)` unit is already checkpointed; by default the interrupt
    is **re-raised** after that so the caller halts cleanly and a rerun resumes. Pass
    `reraise_interrupt=False` to swallow it and return a partial `DetectionResult`.
    """
    started = time.perf_counter()
    usage = UsageMetadataCallbackHandler()
    sh = spec_hash(spec)
    model = settings.llm.model
    detector = detector or RelationDetector(
        spec, settings, callbacks=[*(callbacks or []), usage]
    )

    ops = views.operations(spec)
    units = list(_iter_units(ops, settings.relations.group_size))
    checkpoints = store.get_group_results(sh, model)

    # Seed assembled results with any already-done units (resume).
    results: dict[tuple[str, int], list[dict]] = {
        key: cp["result"] or []
        for key, cp in checkpoints.items()
        if cp["status"] == "done"
    }

    pending = [u for u in units if (u.source.key, u.group_index) not in results]
    result = DetectionResult(units_total=len(units), units_skipped=len(units) - len(pending))

    batch_size = max(1, settings.relations.batch_size)
    bar = tqdm(
        total=len(units),
        initial=result.units_skipped,
        desc="Detecting relations",
        unit="group",
        disable=not show_progress,
    )
    stopped = False
    interrupted = False

    for chunk in _chunks(pending, batch_size):
        try:
            outcomes = detector.detect_groups([(u.source, u.targets) for u in chunk])
        except KeyboardInterrupt:
            logger.warning("interrupted — progress saved, rerun to resume")
            stopped = True
            interrupted = True
            break

        for unit, outcome in zip(chunk, outcomes):
            if isinstance(outcome, BaseException):
                store.save_group_result(
                    sh, model, unit.source.key, unit.group_index, "failed", error=str(outcome)
                )
                result.units_failed += 1
                logger.error(
                    "unit %s[group %d] failed: %s", unit.source.key, unit.group_index, outcome
                )
                if settings.relations.on_error == "stop":
                    stopped = True
            else:
                rels = _flatten(unit.source.key, outcome)
                store.save_group_result(
                    sh, model, unit.source.key, unit.group_index, "done", result=rels
                )
                results[(unit.source.key, unit.group_index)] = rels
                result.units_done += 1
                logger.info(
                    "detected %s[group %d]: %d edge(s)",
                    unit.source.key, unit.group_index, len(rels),
                )
            bar.update(1)

        if stopped:
            break

    bar.close()

    if interrupted and reraise_interrupt:
        raise KeyboardInterrupt
    result.completed = not stopped and len(results) == len(units)
    result.elapsed_s = round(time.perf_counter() - started, 3)
    result.tokens = usage_totals(usage)
    if result.completed:
        result.relations = [r for key in results for r in results[key]]
        store.save_relations(
            sh, model, result.relations, tokens=result.tokens, duration_s=result.elapsed_s
        )
        logger.info("relation detection complete: %d edge(s) cached", len(result.relations))
    return result


def _chunks(items: list, size: int) -> Iterator[list]:
    """Yield successive `size`-length slices of `items`."""
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _iter_units(ops: list[OperationRef], group_size: int) -> Iterator[_Unit]:
    """Enumerate the detection units: each source op paired with its other ops chunked by group size."""
    size = max(1, group_size)
    for source in ops:
        targets = [o for o in ops if o.key != source.key]
        for gi in range(0, len(targets), size):
            yield _Unit(source=source, group_index=gi // size, targets=targets[gi : gi + size])


def _flatten(source_key: str, group: GroupRelations) -> list[dict]:
    """Flatten a group result into self-contained per-edge `Relation` dicts."""
    return [
        Relation(source_key=source_key, target_key=tr.target_key, edge=edge).model_dump()
        for tr in group.targets
        for edge in tr.edges
    ]
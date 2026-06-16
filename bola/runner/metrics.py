"""Thesis metrics, computed as a pure function of the persisted run records.

Deliberately independent of the run manager: it reads the execution log, the snapshots, and (for a
given analysis pass) the verdicts straight from the `Store`, and derives every number from them.
That decoupling is what makes it testable in isolation — seed a store with records and assert the
numbers, no run loop, no network — and what lets metrics be recomputed for any past run or any
re-analysis pass without re-executing anything.

Four groups (mirroring `docs/metrics.md`, which Step 7 replaces):
  - `coverage`   — how much of the API surface was exercised.
  - `execution`  — call counts (incl. a per-HTTP-status-code breakdown) and wall-clock spans
                   (derived from record timestamps).
  - `llm`        — strategy turn count plus token usage: `strategy_tokens` (summed from the
                   per-turn cost persisted on each memory snapshot) and, when an analysis pass is
                   included, `analysis_tokens` (that pass's judge calls). Relation-detection tokens
                   are *not* here — relations are spec-level and cached across runs, so their cost
                   rides on the `DetectionResult`, not a run's metrics. (Currency cost stays out of
                   scope — it lives in Langfuse traces.)
  - `quality`    — precision/recall against a ground truth. **Omitted entirely when no ground
                   truth is supplied** — most targets have none, and zeros would read as real.

`quality` is computed at operation granularity: a finding's object (`op_key|id=…`) counts as a
true positive when the model flagged it *and* its operation is listed vulnerable in the ground
truth. Object-level ids are unstable across runs (they depend on harvested values), so the stable
unit of truth is the endpoint.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Optional

from bola.db.store import Store


def compute_metrics(
    run_id: str,
    store: Store,
    *,
    total_operations: int,
    findings: Optional[list[dict]] = None,
    ground_truth: Optional[Iterable[str]] = None,
    analysis_elapsed_s: Optional[float] = None,
    analysis_tokens: Optional[dict] = None,
) -> dict:
    """The full metrics document for a run (+ one analysis pass, when findings are given)."""
    metrics = compute_run_metrics(run_id, store, total_operations=total_operations)
    if analysis_elapsed_s is not None:
        metrics["execution"]["phase_times_s"]["analysis"] = round(analysis_elapsed_s, 3)
    if analysis_tokens is not None:
        metrics["llm"]["analysis_tokens"] = analysis_tokens
    quality = compute_quality(findings or [], ground_truth)
    if quality is not None:
        metrics["quality"] = quality
    return metrics


def compute_run_metrics(run_id: str, store: Store, *, total_operations: int) -> dict:
    """Coverage / execution / llm metrics — everything derivable from the run itself."""
    execs = store.get_executions(run_id)
    snaps = [s for s in store.get_snapshots(run_id) if s["status"] == "done"]
    turns = store.get_strategy_memories(run_id)

    regular = [e for e in execs if e.get("identity", "regular") == "regular"]
    attacker = [e for e in execs if e.get("identity") == "attacker"]
    called_ops = {e["op_key"] for e in execs}

    coverage = {
        "total_operations": total_operations,
        "operations_called": len(called_ops),
        "endpoint_coverage_pct": (
            round(100.0 * len(called_ops) / total_operations, 1) if total_operations else 0.0
        ),
        "objects_snapshotted": len({s["object_key"] for s in snaps}),
    }

    execution = {
        "regular_calls": len(regular),
        "hacker_calls": len(attacker),
        "snapshot_calls": len(snaps),
        "status_codes": _status_code_counts(execs),
        "phase_times_s": {
            "regular": _span([e["created_at"] for e in regular]),
            "attacker": _span([e["created_at"] for e in attacker]),
            "snapshot": _span([s.get("created_at") for s in snaps]),
        },
        "total_time_s": _span(
            [e["created_at"] for e in execs] + [s.get("created_at") for s in snaps]
        ),
    }

    llm = {"strategy_turns": len(turns), "strategy_tokens": _sum_tokens(turns)}

    return {"coverage": coverage, "execution": execution, "llm": llm}


def compute_quality(
    findings: list[dict], ground_truth: Optional[Iterable[str]]
) -> Optional[dict]:
    """Precision/recall of the findings vs. a ground truth of vulnerable operation keys.

    Returns `None` when `ground_truth` is `None` — there is nothing to score against, and a block
    of zeros would misrepresent that absence as a perfect-or-empty result.
    """
    if ground_truth is None:
        return None
    vulnerable = set(ground_truth)
    tp = fp = fn = tn = 0
    for f in findings:
        op_key = _op_of(f.get("object_key", ""))
        predicted = bool(f.get("is_bola"))
        actual = op_key in vulnerable
        if predicted and actual:
            tp += 1
        elif predicted and not actual:
            fp += 1
        elif not predicted and actual:
            fn += 1
        else:
            tn += 1

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    accuracy = (tp + tn) / (tp + fp + fn + tn) if (tp + fp + fn + tn) else 0.0
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
        "accuracy": round(accuracy, 3),
    }


def _sum_tokens(turns: Iterable[dict]) -> dict[str, int]:
    """Total input/output/total LLM tokens across every persisted strategy turn.

    Turns predating token capture (or non-AI runs) carry no `tokens`; they contribute zero, so the
    totals stay meaningful for any run rather than misreporting absence as usage.
    """
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for t in turns:
        tok = t.get("tokens")
        if not tok:
            continue
        for key in totals:
            totals[key] += tok.get(key, 0) or 0
    return totals


def _status_code_counts(execs: Iterable[dict]) -> dict[str, int]:
    """How many execution calls returned each HTTP status code.

    Keys are stringified codes so the document survives a JSON round-trip unchanged (JSON object
    keys are always strings); calls with no recorded status (transport errors) are skipped.
    """
    counts: dict[str, int] = {}
    for e in execs:
        code = e.get("status_code")
        if not code:
            continue
        key = str(code)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _op_of(object_key: str) -> str:
    """The operation key embedded in an `op_key|name=value|…` object key."""
    return object_key.split("|", 1)[0]


def _span(timestamps: list[Any]) -> float:
    """Seconds between the earliest and latest ISO timestamp (0.0 for fewer than two)."""
    times = [datetime.fromisoformat(t) for t in timestamps if t]
    if len(times) < 2:
        return 0.0
    return round((max(times) - min(times)).total_seconds(), 3)
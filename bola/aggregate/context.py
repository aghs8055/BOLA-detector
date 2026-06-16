"""Assemble the LLM input context for report aggregation from a run's persisted evidence.

Pure assembly: it contacts nothing — the caller passes the already-fetched findings, metrics, AI
memory and target description, and this packs them into the JSON the prompt consumes. `summarize_spec`
keeps only what helps the model describe and remediate (the operation, a one-line purpose, parameter
names) — never full schemas, which would bloat the prompt without adding signal. The AI memory is
flattened to one entry per turn (notes/conclusions/plan), dropping per-turn token bookkeeping.
"""

from __future__ import annotations

from bola.spec import views
from bola.spec.model import Spec


def summarize_spec(spec: Spec) -> list[dict]:
    """A compact per-operation summary: `METHOD /path`, its purpose, and parameter names."""
    out: list[dict] = []
    for op in views.operations(spec):
        params = views.parameters(spec, op)
        out.append(
            {
                "api": f"{op.method} {op.path}",
                "key": op.key,
                "summary": getattr(op.operation, "summary", None) or "",
                "description": getattr(op.operation, "description", None) or "",
                "params": [f"{p.name} ({p.location})" for p in params],
            }
        )
    return out


def spec_overview(spec: Spec) -> dict:
    """Operation counts for the spec: the total and a per-HTTP-method breakdown."""
    by_method: dict[str, int] = {}
    operations = views.operations(spec)
    for op in operations:
        by_method[op.method] = by_method.get(op.method, 0) + 1
    return {
        "total_operations": len(operations),
        "operations_by_method": dict(sorted(by_method.items())),
    }


def _flatten_memory(ai_memory: list[dict]) -> list[dict]:
    """One entry per turn — phase, turn, and the reasoning fields — without token bookkeeping."""
    flat: list[dict] = []
    for m in ai_memory or []:
        entry = {"phase": m.get("phase"), "turn": m.get("turn_index")}
        entry.update(m.get("memory") or {})
        flat.append(entry)
    return flat


def build_report_context(
    *,
    target: str,
    access_description: str,
    spec_summary: list[dict],
    metrics: dict,
    analysis_findings: list[dict],
    ai_memory: list[dict],
    executions: list[dict] | None = None,
) -> dict:
    """Assemble the JSON context block the aggregation prompt consumes.

    `executions` is the authoritative HTTP record (who called what, with which id, and the real
    status) — stronger than the AI memory's self-report, so the prompt uses it to ground findings.
    """
    return {
        "target": target,
        "access_model": access_description or "(none provided)",
        "metrics": metrics or {},
        "operations": spec_summary or [],
        "analysis_findings": analysis_findings or [],
        "ai_memory": _flatten_memory(ai_memory),
        "executions": executions or [],
    }
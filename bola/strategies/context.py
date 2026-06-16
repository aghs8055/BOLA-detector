"""Shape the strategy's world into the JSON context handed to the LLM each turn.

Pure arrangement (like `relations/context.py`): it lays out the operations, the known relations,
the current id pool, the generator catalogue, the model's memory, and the most recent results as
one document the prompt renders. It derives nothing about the spec itself — that comes from
`bola.spec.views`.
"""

from __future__ import annotations

from typing import Any

from bola import generators
from bola.spec import views
from bola.strategies.base import REGULAR, ActionResult, StrategyContext
from bola.strategies.models import AgentMemory

_MAX_RESPONSE_CHARS = 2000


def build_turn_context(
    ctx: StrategyContext,
    memory: AgentMemory,
    recent: list[ActionResult],
    *,
    stage: str = "build",
    build_turns_left: int = 0,
    identities: list[str] | None = None,
    unexercised_operations: list[str] | None = None,
    unbuilt_operations: list[str] | None = None,
    creation_plan: list[dict] | None = None,
    creative: bool = False,
) -> dict[str, Any]:
    """The per-turn document: operations, relations, id pool, generators, memory, last results.

    `stage` / `build_turns_left` / `identities` tell the model where it is in the build→attack
    lifecycle: which credentials exist, whether it is still building the id pool or already
    attacking, and how many build turns remain before it is forced to attack. `operator_notes`
    (only when the manifest set `access`) carries the operator's scope constraints to honour.
    `unexercised_operations` is the attack-stage coverage worklist — relation-consuming operations
    the attacker has not yet called — so the agent spends its turns widening coverage, not repeating.
    `creation_plan` (build stage) tells the agent how many instances of each created entity to build
    so every mutating operation can later be attacked against its own object. `unbuilt_operations`
    (build stage) is the producer worklist — ops that have harvested no id yet — so the victim fills
    the pool for every object type before attacking.
    """
    doc: dict[str, Any] = {
        "stage": stage,
        "build_turns_left": build_turns_left,
        **({"operator_notes": ctx.access} if ctx.access else {}),
        "identities": identities if identities is not None else ctx.identities,
        "operations": [_operation_view(ctx, op) for op in ctx.operations],
        "relations": _compact_relations(ctx.relations),
        "field_repo": _repo_view(ctx),
        "generators": generators.describe_all(),
        "memory": memory.model_dump(),
        "recent_results": [_result_view(r) for r in recent],
    }
    if unexercised_operations is not None:
        doc["unexercised_operations"] = unexercised_operations
    if unbuilt_operations is not None:
        doc["unbuilt_operations"] = unbuilt_operations
    if creation_plan:
        doc["creation_plan"] = creation_plan
    if stage == "attack":
        # The victim's own ids, frozen by harvesting identity — the ids worth replaying. Surfaced so
        # the attacker targets the owner's objects, not its own (which never produce a crossing).
        victim_ids = sorted(str(v) for v in ctx.field_repo.values_owned_by(REGULAR))
        if victim_ids:
            doc["victim_ids"] = victim_ids
    if creative:
        # The bounded creative tail probes what single-step replay cannot: multi-step sequences,
        # array bodies, and mass-assignment — injecting owner/scope fields the schema does not declare.
        doc["creative_mode"] = True
        doc["mass_assignment_field_names"] = _MASS_ASSIGNMENT_NAMES
    return doc


# Generic owner/scope field names to try injecting on write ops (mass-assignment probe). Target-
# agnostic: the model also adds its own domain guesses. A field the server ignores produces no change
# and therefore no finding, so spraying these is false-positive-safe.
_MASS_ASSIGNMENT_NAMES = [
    "org_id", "organization_id", "owner_id", "user_id", "account_id",
    "tenant_id", "tenant", "as_org", "as_user", "role", "is_admin",
]


def build_attack_fill_context(ctx: StrategyContext, batch: list[dict]) -> dict[str, Any]:
    """The focused per-batch document for the deterministic attack sweep.

    Small by design: only the ops in `batch` (each with its full schema) plus the **victim ids** to
    replay into each — not the whole surface. The model's only job is to build one attacker request per
    (op, victim id), placing the id in the right input and filling the other fields with valid values.
    Keeping the prompt this narrow is what lets a weak model stay reliable across the whole sweep.
    """
    by_key = ctx.ops_by_key()
    items = []
    for entry in batch:
        op = by_key.get(entry["op_key"])
        if op is None:
            continue
        items.append({
            "operation": _operation_view(ctx, op),
            "victim_ids": [str(v) for v in entry["victim_ids"]],
        })
    return {
        **({"operator_notes": ctx.access} if ctx.access else {}),
        "instructions_pairs": items,
        "generators": generators.describe_all(),
    }


def build_revise_context(ctx: StrategyContext, op, request, result) -> dict[str, Any]:
    """The focused document for repairing a rejected request: the op schema, the request that was
    sent, and the server's error — so the model can fix the bad field(s) and resend the same call."""
    return {
        **({"operator_notes": ctx.access} if ctx.access else {}),
        "operation": _operation_view(ctx, op),
        "sent_request": {
            "method": getattr(request, "method", None),
            "path_params": getattr(request, "path_params", {}) or {},
            "query": getattr(request, "query", {}) or {},
            "body": getattr(request, "body", None),
        },
        "server_status": getattr(result, "status_code", None),
        "server_response": getattr(result, "response", None),
        "generators": generators.describe_all(),
    }


def _compact_relations(relations: list[dict]) -> list[dict]:
    """A token-light projection of the relation edges for the turn prompt.

    The raw consolidated edges carry verbose source/target coordinates; on a large spec that JSON can
    run to hundreds of KB and overflow the model's context window. The agent only needs to know *which
    operation feeds which* (and into what input) to plan — so each edge is reduced to its source op,
    target op, and the target input's name/location. De-duplicated.
    """
    seen: set = set()
    out: list[dict] = []
    for r in relations:
        tgt = r.get("edge", {}).get("target", {})
        handle = tgt.get("name") or tgt.get("json_pointer")
        key = (r.get("source_key"), r.get("target_key"), handle, tgt.get("location"))
        if key in seen:
            continue
        seen.add(key)
        out.append({"from": r.get("source_key"), "to": r.get("target_key"),
                    "into": handle, "in": tgt.get("location") or tgt.get("type")})
    return out


def _operation_view(ctx: StrategyContext, op) -> dict[str, Any]:
    """One operation as the AI strategy sees it: identity plus parameters and body fields + constraints."""
    return {
        "key": op.key,
        "method": op.method,
        "path": op.path,
        "summary": op.operation.summary,
        "parameters": [
            {"name": p.name, "in": p.location, "type": p.type, "format": p.format,
             "required": p.required, **({"constraints": p.constraints} if p.constraints else {})}
            for p in views.parameters(ctx.spec, op)
        ],
        "request_body": [
            {"media_type": b.media_type,
             "fields": [{"json_pointer": f.json_pointer, "type": f.type, "format": f.format,
                         **({"constraints": f.constraints} if f.constraints else {})}
                        for f in b.fields]}
            for b in views.request_bodies(ctx.spec, op)
            if b.fields
        ],
    }


def _repo_view(ctx: StrategyContext) -> list[dict[str, Any]]:
    """The harvested id pool flattened to (source_key, json_pointer, values) rows for the prompt."""
    return [
        {"source_key": key, "json_pointer": pointer, "values": values}
        for (key, pointer), values in ctx.field_repo.items().items()
    ]


def _result_view(r: ActionResult) -> dict[str, Any]:
    """One executed action's outcome, response truncated, as the next turn's feedback."""
    response = r.result.response
    if isinstance(response, str) and len(response) > _MAX_RESPONSE_CHARS:
        response = response[:_MAX_RESPONSE_CHARS] + "…(truncated)"
    return {
        "op_key": r.action.op_key,
        "method": r.result.method,
        "url": r.result.url,
        "status_code": r.result.status_code,
        "ok": r.result.ok,
        "response": response,
        "error": r.result.error,
    }
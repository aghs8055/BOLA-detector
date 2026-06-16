"""The strategy seam: decide *what* to call, build the requests, react to the results.

A strategy is the brain the actuator (Step 4) lacks. The `ApiExecutor` sends a fully-specified
`ApiRequest` and the `FieldRepo` remembers ids; neither chooses an operation or fills an input.
A `Strategy` does exactly that, and nothing else: it is a **pure planner** with no HTTP, no
store, no harvesting. The run manager (Step 7) drives the loop — `plan()` → execute each action
→ `harvest` into the repo → `observe(results)` — and owns all persistence (the execution log and,
for the AI strategy, its memory snapshot). Keeping the strategy pure is the same discipline the
`FieldRepo` follows: lifecycle policy lives in the runner, not pushed into the primitive.

Both strategies share the work of turning an abstract choice ("fill this input from a harvested
id / a generator / a literal") into a concrete value, and of assembling body-field values addressed
by schema pointers into a nested JSON body. That shared resolution lives here so the random and AI
strategies only differ in *how they decide*, not in *how they build*.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from bola import generators
from bola.execution.api_executor import ApiRequest, ExecutionResult
from bola.execution.field_repo import FieldRepo
from bola.settings import Settings
from bola.spec.model import Spec
from bola.spec.views import OperationRef

# Relations arrive in the consolidated cache shape (`Relation.model_dump()` dicts), the same list
# `Store.get_relations` returns and `field_repo.candidates_*` consume.
Relations = list[dict]

# The two identities a run drives: the resource owner (regular) and the attacker. A strategy tags
# each action with the credentials it should be sent under; the run manager picks the matching
# `AuthSession` and takes the victim baseline snapshot right before the first `attacker` action.
REGULAR = "regular"
ATTACKER = "attacker"


@dataclass
class PlannedAction:
    """One operation the strategy has decided to call, fully resolved into a request.

    `op_key` is carried alongside the request so the runner knows which operation ran (for
    harvesting at that source key and for the execution log); `rationale` is the strategy's own
    note on why it chose this call (free text — the AI strategy fills it, the random one labels it).
    `identity` is which user's credentials to send it under (`regular` | `attacker`).
    """

    op_key: str
    request: ApiRequest
    rationale: str = ""
    identity: str = REGULAR


@dataclass
class ActionResult:
    """A planned action paired with what the executor returned for it."""

    action: PlannedAction
    result: ExecutionResult


@dataclass
class StrategyContext:
    """Everything a strategy reads to decide: the spec, known relations, the live id pool.

    The `field_repo` is a *read* reference — the strategy pulls candidate ids from it but never
    writes; the runner harvests into it between turns. `settings` carries the budgets
    (`test.regular_ops`, `test.ai_max_turns`).
    """

    spec: Spec
    relations: Relations
    field_repo: FieldRepo
    settings: Settings
    operations: list[OperationRef] = field(default_factory=list)
    identities: list[str] = field(default_factory=lambda: [REGULAR, ATTACKER])
    access: str = ""  # operator's neutral API/scope note (e.g. accounts that are out of scope)

    def ops_by_key(self) -> dict[str, OperationRef]:
        """The operations indexed by their stable key, for lookup by op_key."""
        return {o.key: o for o in self.operations}


class Strategy(ABC):
    """Decide the next batch of calls, then react to their results. Pure: no HTTP, no store."""

    @abstractmethod
    def plan(self) -> list[PlannedAction]:
        """The next batch of actions to execute (empty when there is nothing left to try)."""

    @abstractmethod
    def observe(self, results: list[ActionResult]) -> None:
        """Update internal state from the results of the actions `plan` last returned."""

    @abstractmethod
    def is_done(self) -> bool:
        """True when the strategy has nothing more worth trying (budget or its own verdict)."""

    def finalize(self) -> bool:
        """End-of-run hook: fold the final batch's results into internal state (e.g. memory).

        `plan` reasons over the *previous* turn's results, so the last executed turn's outcomes —
        typically the attack — would otherwise never reach memory. Return True if this updated state
        worth persisting. Default: a no-op (stateless strategies have nothing to fold)."""
        return False


# --------------------------------------------------------------------------------------
# Shared value resolution + body assembly
# --------------------------------------------------------------------------------------


def resolve_repo(field_repo: FieldRepo, source_key: str, json_pointer: str, index: int = 0) -> Any:
    """A harvested id at `(source_key, json_pointer)`, or None if the pool has none there."""
    values = field_repo.get(source_key, json_pointer)
    if not values:
        return None
    return values[index] if 0 <= index < len(values) else values[0]


def resolve_generator(name: str, args: dict[str, Any] | None = None) -> Any:
    """Run a named generator, passing `args` only if its function declares parameters."""
    gen = generators.get(name)
    if gen is None:
        return None
    if not args:
        return gen.fn()
    try:
        sig = inspect.signature(gen.fn)
    except (TypeError, ValueError):
        return gen.fn()
    if sig.parameters:
        return gen.fn(**args)
    return gen.fn()


def generate_for_field(type_: str | None, format_: str | None) -> Any:
    """The no-LLM default fill for an input a relation does not supply."""
    return generators.generate_for_schema(type_, format_)


def build_body(values_by_pointer: list[tuple[str, Any]]) -> Any:
    """Assemble a nested JSON body from `(schema_pointer, value)` pairs.

    Each pointer uses the schema alphabet (`/properties/<name>`, `/items`,
    `/additionalProperties`); this is the inverse of `views.data_values` — it *builds* the
    structure the pointer describes and deep-merges each fragment so sibling fields share one
    object. `/items` produces a single-element list, `/additionalProperties` a single-key map
    (enough to carry one value into a list/map field).
    """
    body: Any = {}
    for pointer, value in values_by_pointer:
        tokens = [t for t in pointer.split("/") if t]
        fragment = _fragment(tokens, 0, value)
        body = _merge(body, fragment)
    return body


def _fragment(tokens: list[str], i: int, value: Any) -> Any:
    """Build the nested JSON fragment a schema pointer implies, with `value` at the leaf."""
    if i >= len(tokens):
        return value
    tok = tokens[i]
    if tok == "properties" and i + 1 < len(tokens):
        return {tokens[i + 1]: _fragment(tokens, i + 2, value)}
    if tok == "items":
        return [_fragment(tokens, i + 1, value)]
    if tok == "additionalProperties":
        return {"key": _fragment(tokens, i + 1, value)}
    return value


def _merge(dst: Any, src: Any) -> Any:
    """Deep-merge two body fragments so sibling fields share one object/list instead of clobbering."""
    if isinstance(dst, dict) and isinstance(src, dict):
        for k, v in src.items():
            dst[k] = _merge(dst.get(k), v) if k in dst else v
        return dst
    if isinstance(dst, list) and isinstance(src, list) and dst and src:
        dst[0] = _merge(dst[0], src[0])
        return dst
    return src
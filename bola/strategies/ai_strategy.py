"""The LLM-driven strategy: reason about the API, choose calls, learn from the responses.

Where the random strategy explores by luck, this one explores by reasoning. Each turn it is
shown the operations, the detected relations, the live id pool, the value generators, its own
memory, and the most recent results; it returns an `AgentTurn` — a rewritten memory, a batch of
calls (each input chosen as a harvested id / a generator / a literal), and an `is_done` verdict.
The strategy resolves those abstract choices into concrete `ApiRequest`s exactly the way the
random strategy does, via the shared `bola.strategies.base` helpers, so the two differ only in
*how they decide*.

The chat chain is injectable so unit tests run the full turn loop with a canned `AgentTurn` and
no network — identical to how the relation detector is tested. Memory is plain in/out state the
strategy reads and rewrites; persisting and restoring it across runs is the run manager's job
(Step 7), keeping this class pure.
"""

from __future__ import annotations

import json
import logging
from itertools import product
from pathlib import Path
from typing import Any, Optional

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.prompts import PromptTemplate

from bola.execution.api_executor import ApiRequest, FilePart
from bola.execution.field_repo import candidates_for_parameter
from bola.llm import build_chat_model, usage_totals
from bola.spec import views
from bola.spec.views import OperationRef
from bola.strategies.base import (
    ATTACKER,
    REGULAR,
    ActionResult,
    PlannedAction,
    Strategy,
    StrategyContext,
    build_body,
    resolve_generator,
    resolve_repo,
)
from bola.strategies.context import (
    build_attack_fill_context,
    build_revise_context,
    build_turn_context,
)
from bola.strategies.models import (
    AgentMemory,
    AgentTurn,
    BodyInput,
    ConcreteChoice,
    FileChoice,
    GeneratorChoice,
    ParamInput,
    PlannedCall,
    RepoChoice,
)

logger = logging.getLogger("bola.strategies.ai_strategy")

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "strategies"

_LEAK_FEEDBACK_CAP = 40  # max leaking-read results kept at the front of the attack-stage feedback


def _nonempty(response: Any) -> bool:
    """True if a response body actually carries data (not None / empty string / empty container)."""
    if response is None:
        return False
    if isinstance(response, (str, list, dict)):
        return len(response) > 0
    return True


class AIStrategy(Strategy):
    """Plan calls with an LLM, resolve them to requests, and carry memory across turns."""

    def __init__(
        self,
        context: StrategyContext,
        *,
        chain: Any = None,
        memory: Optional[AgentMemory] = None,
        max_turns: Optional[int] = None,
        build_cap: Optional[int] = None,
        start_in_attack: bool = False,
        callbacks: Optional[list] = None,
    ):
        self.ctx = context
        self.memory = memory or AgentMemory()
        self.max_turns = (
            max_turns if max_turns is not None else context.settings.test.ai_max_turns
        )
        self.build_cap = (
            build_cap if build_cap is not None else context.settings.test.ai_build_max_turns
        )
        self._callbacks = callbacks or []
        self.chain = chain or self._build_chain()
        # The sweep's focused per-batch prompt. Reuses the injected `chain` under test (so fakes drive
        # both flows); otherwise built from its own template. Lazy so freeform runs never construct it.
        self._injected_chain = chain
        self._attack_chain = None
        self._revise_chain = None
        self._ops_by_key = context.ops_by_key()
        # Coverage worklist: operations some relation feeds an id into — the BOLA attack surface.
        # When `ai_require_full_coverage` is set, the run may not finish until each has been
        # attacked, so a weak model spends its budget widening coverage instead of stopping early.
        self._consumer_ops = {r["target_key"] for r in context.relations if r.get("target_key")}
        # Producer ops feed ids into the pool; the build phase must call them so every object type's
        # ids are harvested as the victim — otherwise an unread object (e.g. a document) never enters
        # `victim_ids` and the attacker can never reach it, however well it explores.
        self._producer_ops = {r["source_key"] for r in context.relations if r.get("source_key")}
        self._attacked: set[str] = set()
        self._op_attempts: dict[str, int] = {}  # attacker attempts per op (credited or not) — loop guard
        self._require_full_coverage = context.settings.test.ai_require_full_coverage
        # Build-stage creation plan: an entity mutated by N operations needs N fresh instances so each
        # attacker write lands on its own object — otherwise their before/after diffs collide and
        # attribution is ambiguous. Derived from the relations, target-agnostic.
        self._creation_plan = _creation_plan(context.relations, self._ops_by_key)
        self._recent: list[ActionResult] = []
        # Every attacker result so far, kept whole. The sweep orders calls reads→writes→deletes, so the
        # *last* batch a reasoning turn sees is the (usually refused) deletes; reasoning over `_recent`
        # alone would make the agent conclude "all refused" and miss the earlier leaking reads. The
        # creative tail and `finalize` reflect over this instead so memory records the real crossings.
        self._attacker_results: list[ActionResult] = []
        self._turns = 0
        self._build_turns = 0
        self._creative_turns_done = 0
        self.creative_turns = context.settings.test.creative_turns
        # Substage is the single source of truth. `freeform` mode keeps the original two-stage flow
        # (`build` → `attack`); `sweep` mode runs the four-substage deterministic flow
        # (`enumerate` → `build` → `sweep` → `creative`).
        self._mode = context.settings.test.attack_mode
        if self._mode == "sweep":
            self._substage = "sweep" if start_in_attack else "enumerate"
        else:
            self._substage = "attack" if start_in_attack else "build"
        self._enumerated: set[str] = set()  # GET ops already emitted in the enumerate substage
        self._done = False
        self.last_turn_tokens: dict[str, int] = {
            "input_tokens": 0, "output_tokens": 0, "total_tokens": 0
        }

    @property
    def _stage(self) -> str:
        """The build/attack identity stage derived from the substage (controls which creds are used)."""
        return "build" if self._substage in ("enumerate", "build") else "attack"

    @property
    def stage(self) -> str:
        """`build` while harvesting ids as the regular user; `attack` once crossing as the attacker."""
        return self._stage

    def plan(self) -> list[PlannedAction]:
        """One turn. Dispatches to the freeform two-stage flow or the sweep four-substage flow."""
        if self.is_done():
            return []
        if self._mode != "sweep":
            return self._plan_freeform()
        if self._substage == "enumerate":
            return self._plan_enumerate()
        if self._substage == "build":
            return self._plan_build(next_substage="sweep")
        if self._substage == "sweep":
            return self._plan_sweep()
        return self._plan_creative()

    # ----- freeform (legacy) flow -------------------------------------------------------------
    def _plan_freeform(self) -> list[PlannedAction]:
        """The original single-pass flow: LLM-driven build then attack, kept for A/B comparison."""
        context = build_turn_context(
            self.ctx, self.memory, self._recent,
            stage=self._stage,
            build_turns_left=max(0, self.build_cap - self._build_turns),
            identities=self.ctx.identities,
            unexercised_operations=self._unexercised(),
            unbuilt_operations=self._unbuilt() if self._stage == "build" else None,
            creation_plan=self._creation_plan if self._stage == "build" else None,
        )
        turn = self._invoke(context)
        self.memory = turn.memory
        self._turns += 1
        if self._stage == "build":
            self._build_turns += 1
            if self._build_turns >= self.build_cap:
                self._substage = "attack"
            elif turn.ready_to_attack or turn.is_done:
                if not (self._require_full_coverage and self._unbuilt()):
                    self._substage = "attack"
        else:
            self._done = turn.is_done and not (self._require_full_coverage and self._unexercised())
        actions = [self._resolve_call(c) for c in turn.calls]
        return [a for a in actions if a is not None]

    # ----- sweep flow: enumerate → build → sweep → creative ------------------------------------
    def _plan_enumerate(self) -> list[PlannedAction]:
        """Deterministic BFS: emit the currently-callable GET frontier as the victim, no LLM call.

        The runner harvests between turns, so each call's responses unlock the next BFS level; when no
        new GET is callable the pool covers every reachable object type and we move on to `build`.
        """
        frontier = self._get_frontier()
        if not frontier:
            self._substage = "build"
            return []
        cap = max(1, self.ctx.settings.test.attack_ids_per_op)
        actions: list[PlannedAction] = []
        for op in frontier:
            self._enumerated.add(op.key)
            path_params = [p for p in views.parameters(self.ctx.spec, op) if p.location == "path"]
            candidate_lists = [
                candidates_for_parameter(self.ctx.field_repo, self.ctx.relations, op.key, p.name, "path")[:cap]
                for p in path_params
            ]
            combos = [{}] if not path_params else [
                dict(zip((p.name for p in path_params), combo)) for combo in product(*candidate_lists)
            ]
            for bound in combos:
                req = ApiRequest(method="GET", path=op.path, path_params=dict(bound))
                actions.append(PlannedAction(op_key=op.key, request=req, rationale="enumerate",
                                             identity=REGULAR))
        # deterministic, no LLM call — does not consume the `max_turns` reasoning budget
        return actions

    def _plan_build(self, *, next_substage: str) -> list[PlannedAction]:
        """LLM build turn: create victim objects until the pool is complete, then advance."""
        context = build_turn_context(
            self.ctx, self.memory, self._recent,
            stage="build", build_turns_left=max(0, self.build_cap - self._build_turns),
            identities=self.ctx.identities,
            unexercised_operations=self._unexercised(),
            unbuilt_operations=self._unbuilt(),
            creation_plan=self._creation_plan,
        )
        turn = self._invoke(context)
        self.memory = turn.memory
        self._turns += 1
        self._build_turns += 1
        if self._build_turns >= self.build_cap:
            self._substage = next_substage
        elif turn.ready_to_attack or turn.is_done:
            if not (self._require_full_coverage and self._unbuilt()):
                self._substage = next_substage
        return [a for a in (self._resolve_call(c) for c in turn.calls) if a is not None]

    def _plan_sweep(self) -> list[PlannedAction]:
        """Deterministic pair-driven attack: the runner enumerates (op × victim id); the LLM fills the
        other fields. A focused batch of ops (each with its victim ids) is judged per call."""
        worklist = self._sweep_worklist()
        if not worklist:
            self._substage = "creative"
            return []
        batch = worklist[: max(1, self.ctx.settings.test.attack_ops_per_call)]
        context = build_attack_fill_context(self.ctx, batch)
        turn = self._invoke(context, chain=self.attack_chain)
        # The attack-fill turn is a mechanical request builder, not a reasoner — its memory is blind to
        # the responses, so it must not overwrite the accumulated reasoning memory. The creative tail and
        # `finalize` are what fold the sweep's results back in.
        self._turns += 1
        return [a for a in (self._resolve_call(c) for c in turn.calls) if a is not None]

    def _plan_creative(self) -> list[PlannedAction]:
        """Bounded free-form tail: multi-step + mass-assignment, for what the sweep can't express."""
        context = build_turn_context(
            self.ctx, self.memory, self._attack_feedback(),
            stage="attack", build_turns_left=0, identities=self.ctx.identities,
            unexercised_operations=self._unexercised(),
            creative=True,
        )
        turn = self._invoke(context)
        self.memory = turn.memory
        self._turns += 1
        self._creative_turns_done += 1
        if turn.is_done or self._creative_turns_done >= self.creative_turns:
            self._done = True
        return [a for a in (self._resolve_call(c) for c in turn.calls) if a is not None]

    def observe(self, results: list[ActionResult]) -> None:
        """Record results; credit coverage only for attacker calls that used a *victim* id.

        Counting any attacker call would let the agent tick an operation off by hitting its *own*
        object — no authorization crossing. An operation is covered only once it has been attacked
        with one of the victim's harvested ids, so the worklist drives the real cross-user test."""
        self._recent = results
        victim = self.ctx.field_repo.values_owned_by(REGULAR)
        for r in results:
            if r.action.identity == ATTACKER:
                self._attacker_results.append(r)
                self._op_attempts[r.action.op_key] = self._op_attempts.get(r.action.op_key, 0) + 1
                if _request_values(r.action.request) & victim:
                    self._attacked.add(r.action.op_key)

    def _attack_feedback(self) -> list[ActionResult]:
        """The attack-stage reasoning feedback: every attacker call that returned data (a 2xx with a
        body — the potential read crossings) followed by the most recent batch.

        Built so the leaking reads reach the reasoning turns regardless of the sweep's
        reads→writes→deletes ordering: tail-only feedback would show just the refused deletes and the
        agent would wrongly conclude nothing leaked. Leaks are kept at the front (capped) so a long run
        cannot crowd them out."""
        leaks = [r for r in self._attacker_results if r.result.ok and _nonempty(r.result.response)]
        feedback = leaks[:_LEAK_FEEDBACK_CAP]
        seen = {id(r) for r in feedback}
        feedback.extend(r for r in self._recent if id(r) not in seen)
        return feedback or self._recent

    def _unexercised(self) -> list[str]:
        """Consumer ops not yet attacked with a victim id — and not yet retried into the ground.

        An op is also dropped once it has been attempted `attack_max_attempts_per_op` times (whether or
        not a victim id was ever used), so a stuck or persistently-failing op cannot starve the
        worklist of the operations still waiting to be tested."""
        cap = self.ctx.settings.test.attack_max_attempts_per_op
        stuck = {op for op, n in self._op_attempts.items() if n >= cap}
        return sorted(self._consumer_ops - self._attacked - stuck)

    def _unbuilt(self) -> list[str]:
        """Producer operations that have not yet harvested any id — the build-phase worklist."""
        built = {src for (src, _), values in self.ctx.field_repo.items().items() if values}
        return sorted(self._producer_ops - built)

    def _get_frontier(self) -> list[OperationRef]:
        """GET ops, not yet enumerated, whose every required path param can be filled from the pool."""
        out: list[OperationRef] = []
        for op in self.ctx.operations:
            if op.method != "GET" or op.key in self._enumerated:
                continue
            path_params = [p for p in views.parameters(self.ctx.spec, op) if p.location == "path"]
            if any(
                not candidates_for_parameter(self.ctx.field_repo, self.ctx.relations, op.key, p.name, "path")
                for p in path_params
            ):
                continue  # a path param the pool cannot supply yet — wait for a later BFS level
            out.append(op)
        return out

    def _victim_candidates(self, op_key: str) -> list[Any]:
        """Victim ids worth replaying into `op_key`, ranked created → exclusive → visible, capped.

        Considers every id a relation can feed into this op (path/query/body), keeps only victim-owned
        ones, and orders by ownership strength so the capped attack budget lands on the best handle."""
        repo = self.ctx.field_repo
        pool: list[Any] = []
        for rel in self.ctx.relations:
            if rel.get("target_key") != op_key:
                continue
            src = rel["edge"]["source"]
            for v in repo.get(rel["source_key"], src["json_pointer"]):
                if v not in pool:
                    pool.append(v)
        created = repo.values_created_by(REGULAR)
        owned = repo.values_owned_by(REGULAR)
        attacker = repo.values_owned_by(ATTACKER)
        ranked = sorted(
            (v for v in pool if v in owned),
            key=lambda v: (0 if v in created else (1 if v not in attacker else 2)),
        )
        return ranked[: max(1, self.ctx.settings.test.attack_ids_per_op)]

    def _sweep_worklist(self) -> list[dict]:
        """Consumer ops not yet attacked with a victim id, each paired with its ranked victim ids.

        Ops for which no victim id exists are skipped (a crossing cannot be tested without a victim
        handle). The list shrinks as `observe` credits ops, so the sweep drains deterministically.

        Ordered **reads → non-destructive writes → deletes** so a destructive op never wipes an object
        that a later read or write of the same object still needs (exhaustive coverage would otherwise
        cannibalise itself — e.g. deleting a card before revealing it)."""
        out: list[dict] = []
        for op_key in self._unexercised():
            ids = self._victim_candidates(op_key)
            if ids:
                out.append({"op_key": op_key, "victim_ids": ids})
        return sorted(out, key=lambda e: self._method_rank(e["op_key"]))

    def _method_rank(self, op_key: str) -> int:
        """Sweep ordering key: GET (read) first, other writes next, DELETE last."""
        op = self._ops_by_key.get(op_key)
        method = (op.method if op else "GET").upper()
        return {"GET": 0, "DELETE": 2}.get(method, 1)

    def is_done(self) -> bool:
        """True once the agent declared done in the attack stage or the turn budget is spent."""
        return self._done or self._turns >= self.max_turns

    def finalize(self) -> bool:
        """Reflect once over the final batch's results so the attack turn's findings are not lost.

        Memory is written in `plan()` over the *previous* turn's results, so when the agent attacks
        and finishes on the same turn, those outcomes never reach memory. This runs one extra
        reasoning pass purely to fold them in; any calls it proposes are discarded."""
        if not self._recent:
            return False
        context = build_turn_context(
            self.ctx, self.memory, self._attack_feedback(),
            stage=self._stage, build_turns_left=0, identities=self.ctx.identities,
            unexercised_operations=self._unexercised(),
        )
        turn = self._invoke(context)
        self.memory = turn.memory
        return True

    def _resolve_call(self, call: PlannedCall) -> Optional[PlannedAction]:
        """Turn one planned call into a concrete `PlannedAction`, or None for an unknown op."""
        op = self._ops_by_key.get(call.op_key)
        if op is None:
            logger.warning("AI strategy proposed unknown op_key %r — skipped", call.op_key)
            return None
        request = ApiRequest(method=op.method, path=op.path)
        self._apply_parameters(op, call, request)
        self._apply_body(call, request)
        identity = REGULAR if self._stage == "build" else call.identity
        return PlannedAction(
            op_key=op.key, request=request, rationale=call.rationale, identity=identity
        )

    def _apply_parameters(self, op: OperationRef, call: PlannedCall, request: ApiRequest) -> None:
        """Resolve each parameter input and place it in the request by location (path/query/header)."""
        for pi in call.parameters:
            value = self._resolve(pi)
            if pi.location == "path":
                request.path_params[pi.name] = value
            elif pi.location == "query":
                request.query[pi.name] = value
            elif pi.location == "header":
                request.headers[pi.name] = str(value)
            # cookie params have no ApiRequest slot; skipped

    def _apply_body(self, call: PlannedCall, request: ApiRequest) -> None:
        """Assemble the request body from the call's body inputs (pointer→value pairs).

        File inputs are split off into multipart `files` (keyed by the field name their pointer
        names); the rest become the ordinary pointer-addressed body. Any `extra_body` fields are
        merged at the body root afterwards — these are UNDOCUMENTED keys (mass-assignment probes) not
        present in the schema, so they bypass the pointer-addressed path entirely.
        """
        if not call.body and not call.extra_body:
            return
        if call.body:
            request.media_type = call.body[0].media_type
        elif not request.media_type:
            request.media_type = "application/json"
        file_inputs = [bi for bi in call.body if isinstance(bi.choice, FileChoice)]
        value_inputs = [bi for bi in call.body if not isinstance(bi.choice, FileChoice)]
        if value_inputs:
            request.body = build_body([(bi.json_pointer, self._resolve(bi)) for bi in value_inputs])
        if call.extra_body:
            base = request.body if isinstance(request.body, dict) else {}
            for ef in call.extra_body:
                base[ef.name] = self._resolve(ef)
            request.body = base
        if file_inputs:
            request.files = {
                _field_name(bi.json_pointer): FilePart(
                    content=bi.choice.content,
                    filename=bi.choice.filename,
                    content_type=bi.choice.content_type,
                )
                for bi in file_inputs
            }

    def _resolve(self, inp: Any) -> Any:
        """Resolve one input's value from its choice: a literal, a harvested id, or a generator."""
        choice = inp.choice
        if isinstance(choice, ConcreteChoice):
            return choice.value
        if isinstance(choice, RepoChoice):
            return resolve_repo(
                self.ctx.field_repo, choice.source_key, choice.json_pointer, choice.index
            )
        if isinstance(choice, GeneratorChoice):
            return resolve_generator(choice.name, choice.args_dict())
        return None

    def _invoke(self, context: Any, *, chain: Any = None) -> AgentTurn:
        """Run one turn through a chain, recording its token cost in `last_turn_tokens`.

        A fresh `UsageMetadataCallbackHandler` per call scopes the tally to this turn alone; the run
        manager reads `last_turn_tokens` right after, so the cost lands on the right memory snapshot.
        `chain` overrides the default (used for the focused attack-fill prompt in the sweep).
        """
        usage = UsageMetadataCallbackHandler()
        config = {"callbacks": [*self._callbacks, usage]}
        turn: AgentTurn = (chain or self.chain).invoke({"context": _json(context)}, config=config)
        self.last_turn_tokens = usage_totals(usage)
        return turn

    @property
    def attack_chain(self):
        """The sweep's attack-fill chain — the injected test chain, or its own template, built lazily."""
        if self._injected_chain is not None:
            return self._injected_chain
        if self._attack_chain is None:
            self._attack_chain = self._build_chain("attack_fill.md")
        return self._attack_chain

    @property
    def revise_chain(self):
        """The chain that repairs a request the server rejected for a bad field (4xx input error)."""
        if self._injected_chain is not None:
            return self._injected_chain
        if self._revise_chain is None:
            self._revise_chain = self._build_chain("revise_request.md")
        return self._revise_chain

    def revise(self, action: PlannedAction, result) -> Optional[PlannedAction]:
        """Given a request the server rejected (4xx input error), ask the LLM to fix the offending
        fields and return a corrected action that keeps the same operation and the same (victim) id."""
        op = self._ops_by_key.get(action.op_key)
        if op is None:
            return None
        context = build_revise_context(self.ctx, op, action.request, result)
        turn = self._invoke(context, chain=self.revise_chain)
        if not turn.calls:
            return None
        revised = self._resolve_call(turn.calls[0])
        if revised is None:
            return None
        return PlannedAction(op_key=revised.op_key, request=revised.request,
                             rationale=revised.rationale or action.rationale, identity=action.identity)

    def _build_chain(self, template_name: str = "ai_turn.md"):
        """A named per-turn prompt piped into a chat model with `AgentTurn` structured output."""
        text = (_TEMPLATE_DIR / template_name).read_text(encoding="utf-8")
        prompt = PromptTemplate.from_template(text)
        llm = build_chat_model(self.ctx.settings)
        return prompt | llm.with_structured_output(AgentTurn)


def _request_values(request) -> set:
    """All scalar values a request carries (path/query/body), for matching against the victim ids."""
    out: set = set()

    def walk(v):
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, (str, int, float)) and not isinstance(v, bool):
            out.add(v)

    walk(getattr(request, "path_params", {}) or {})
    walk(getattr(request, "query", {}) or {})
    walk(getattr(request, "body", None))
    return out


def _creation_plan(relations: list[dict], ops_by_key: dict) -> list[dict]:
    """How many instances of each created entity to build, so each mutating op gets its own.

    An entity's creator is a `POST` operation that produces an id; the operations that *mutate* that
    entity are the `POST`/`PUT`/`PATCH`/`DELETE` operations a relation feeds that id into. When two or
    more mutating ops share one creator, that many instances are worth creating (one per op) so their
    write effects do not collide on a single object. Returned only for creators with ≥2 such ops.
    """
    mutating = {"POST", "PUT", "PATCH", "DELETE"}
    consumers: dict[str, set[str]] = {}
    for rel in relations:
        src, tgt = rel.get("source_key"), rel.get("target_key")
        src_op, tgt_op = ops_by_key.get(src), ops_by_key.get(tgt)
        if not src_op or not tgt_op or src_op.method != "POST" or tgt == src:
            continue
        if tgt_op.method in mutating:
            consumers.setdefault(src, set()).add(tgt)
    plan = [
        {"create_op": c, "instances": len(ops), "for_operations": sorted(ops)}
        for c, ops in consumers.items()
        if len(ops) >= 2
    ]
    return sorted(plan, key=lambda p: -p["instances"])


def _field_name(json_pointer: str) -> str:
    """The form-field name a body pointer addresses — its last `/properties/<name>` segment.

    A whole-body file (root pointer `/`) has no named field, so it falls back to `file`.
    """
    structural = {"properties", "items", "additionalProperties"}
    names = [t for t in json_pointer.split("/") if t and t not in structural]
    return names[-1] if names else "file"


def _json(obj: Any) -> str:
    """Pretty-printed JSON for embedding in the prompt."""
    return json.dumps(obj, indent=2, default=str)
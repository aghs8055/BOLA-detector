"""The baseline strategy: call operations at random, filling inputs by the obvious rule.

This is the thesis *baseline* the AI strategy is measured against. It needs no LLM. Each turn it
samples operations and, for every input, applies one fixed rule: if a detected relation supplies
a harvested id for that input, use it (the BOLA chaining substrate — replay an id obtained from
one call as another call's object handle); otherwise generate a plausible value from the field's
type/format. It explores by volume and luck, not reasoning — exactly the contrast the experiment
wants. An injectable `rng` makes its choices reproducible in tests.
"""

from __future__ import annotations

import random

from bola.execution.api_executor import ApiRequest
from bola.execution.field_repo import candidates_for_body, candidates_for_parameter
from bola.spec import views
from bola.spec.views import OperationRef
from bola.strategies.base import (
    ATTACKER,
    REGULAR,
    PlannedAction,
    Strategy,
    StrategyContext,
    build_body,
    generate_for_field,
)


class RandomStrategy(Strategy):
    """Sample operations at random; fill each input from a harvested id or a generator.

    Spans the same two stages as the AI strategy, but the boundary is a fixed budget split, not a
    reasoned decision: it emits `max_actions` calls as the regular user (BUILD), then
    `attacker_actions` calls as the attacker (ATTACK), tagging each action with its identity. The
    run manager takes the victim baseline snapshot at the first attacker-tagged action.
    """

    def __init__(
        self,
        context: StrategyContext,
        *,
        max_actions: int | None = None,
        attacker_actions: int = 0,
        batch_size: int = 1,
        rng: random.Random | None = None,
    ):
        self.ctx = context
        self.max_actions = (
            max_actions if max_actions is not None else context.settings.test.regular_ops
        )
        self.attacker_actions = max(0, attacker_actions)
        self.batch_size = max(1, batch_size)
        self.rng = rng or random.Random()
        self._regular_emitted = 0
        self._attacker_emitted = 0

    @property
    def stage(self) -> str:
        """`build` while the regular-op budget remains, then `attack`."""
        return "build" if self._regular_emitted < self.max_actions else "attack"

    def plan(self) -> list[PlannedAction]:
        """Emit the next batch of random actions for the current identity, respecting the budgets."""
        ops = self.ctx.operations
        if not ops:
            return []
        if self._regular_emitted < self.max_actions:
            identity, remaining = REGULAR, self.max_actions - self._regular_emitted
        elif self._attacker_emitted < self.attacker_actions:
            identity, remaining = ATTACKER, self.attacker_actions - self._attacker_emitted
        else:
            return []
        n = min(self.batch_size, remaining)
        actions = [self._action_for(self.rng.choice(ops), identity) for _ in range(n)]
        if identity == REGULAR:
            self._regular_emitted += len(actions)
        else:
            self._attacker_emitted += len(actions)
        return actions

    def observe(self, results) -> None:  # noqa: D401 - baseline keeps no state
        """The baseline ignores results; harvesting happens in the runner."""

    def is_done(self) -> bool:
        """True when there are no operations, or both the regular and attacker budgets are spent."""
        return not self.ctx.operations or (
            self._regular_emitted >= self.max_actions
            and self._attacker_emitted >= self.attacker_actions
        )

    def _action_for(self, op: OperationRef, identity: str = REGULAR) -> PlannedAction:
        """Build one action for `op` under `identity`, filling its parameters and body."""
        request = ApiRequest(method=op.method, path=op.path)
        self._fill_parameters(op, request)
        self._fill_body(op, request)
        return PlannedAction(op_key=op.key, request=request, rationale="random", identity=identity)

    def _fill_parameters(self, op: OperationRef, request: ApiRequest) -> None:
        """Fill required parameters from a harvested candidate when a relation supplies one, else generate."""
        for p in views.parameters(self.ctx.spec, op):
            if not p.required and p.location in ("query", "header", "cookie"):
                continue
            candidates = candidates_for_parameter(
                self.ctx.field_repo, self.ctx.relations, op.key, p.name, p.location
            )
            value = self.rng.choice(candidates) if candidates else generate_for_field(p.type, p.format)
            if p.location == "path":
                request.path_params[p.name] = value
            elif p.location == "query":
                request.query[p.name] = value
            elif p.location == "header":
                request.headers[p.name] = str(value)
            # cookie params have no ApiRequest slot; skipped (rare, and not load-bearing here)

    def _fill_body(self, op: OperationRef, request: ApiRequest) -> None:
        """Fill the first non-empty request body, candidate-or-generated per field."""
        bodies = views.request_bodies(self.ctx.spec, op)
        body = next((b for b in bodies if b.fields), None)
        if body is None:
            return
        pairs = []
        for f in body.fields:
            candidates = candidates_for_body(
                self.ctx.field_repo, self.ctx.relations, op.key, body.media_type, f.json_pointer
            )
            value = self.rng.choice(candidates) if candidates else generate_for_field(f.type, f.format)
            pairs.append((f.json_pointer, value))
        request.body = build_body(pairs)
        request.media_type = body.media_type
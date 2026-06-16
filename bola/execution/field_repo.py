"""In-memory pool of identifier values harvested from real API responses.

The detector reasons on paper; the executor acts. When an operation is actually called and
returns, `harvest` walks the real response for every relation edge whose *source* is that
operation and stashes the concrete id values it finds. A later call can then pull a real id
from the pool (`candidates_for_parameter` / `candidates_for_body`) to drive the target input
the edge points at — the core BOLA manoeuvre.

The pool is a plain dict (architecture decision: SQLite + in-memory field repo, no Redis). It
is keyed by `(source operation key, source schema pointer)`, exactly the coordinates a relation
edge's source carries, so harvest and lookup share one address. `fork` deep-copies it: the
attacker phase inherits the *regular* user's harvested ids and replays them against its own auth.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from bola.spec import views

# Relations arrive in the consolidated cache shape: `Relation.model_dump()` dicts
# (`source_key`, `target_key`, `edge`), the same list `Store.get_relations` returns.
Relations = list[dict]


@dataclass
class FieldRepo:
    """Harvested id values, keyed by (source op key, source schema pointer)."""

    _values: dict[tuple[str, str], list[Any]] = field(default_factory=dict)
    # Which identity harvested each value: `owner -> {values}`. An id first seen in the regular
    # user's responses is the victim's; one the attacker harvested (e.g. an object it created) is its
    # own. The attack phase uses this to replay *victim* ids — not its own — and to score coverage.
    _owner_values: dict[str, set] = field(default_factory=dict)
    # Values that came from a *create* (POST) response, per owner. A victim-created id is *provably*
    # the victim's, a stronger signal than merely appearing in a victim response (which a leaky list
    # can pollute). The attack sweep ranks created ids first when picking which victim id to replay.
    _created_values: dict[str, set] = field(default_factory=dict)

    def add(
        self, source_key: str, json_pointer: str, value: Any,
        owner: str = "regular", created: bool = False,
    ) -> None:
        """Store one harvested value (de-duplicated, order-preserving), recording who harvested it and
        whether it came from a create (POST) response — the provably-owned tier."""
        bucket = self._values.setdefault((source_key, json_pointer), [])
        if value not in bucket:
            bucket.append(value)
        self._owner_values.setdefault(owner, set()).add(value)
        if created:
            self._created_values.setdefault(owner, set()).add(value)

    def get(self, source_key: str, json_pointer: str) -> list[Any]:
        """All values harvested at one source coordinate (empty list if none)."""
        return list(self._values.get((source_key, json_pointer), []))

    def values_owned_by(self, owner: str) -> set:
        """The set of id values first harvested under `owner` (e.g. the victim's ids for `regular`)."""
        return set(self._owner_values.get(owner, set()))

    def values_created_by(self, owner: str) -> set:
        """The set of id values `owner` obtained from a create (POST) response — provably owned."""
        return set(self._created_values.get(owner, set()))

    def items(self) -> dict[tuple[str, str], list[Any]]:
        """A copy of the whole pool — for snapshots / strategy context."""
        return {k: list(v) for k, v in self._values.items()}

    def fork(self) -> "FieldRepo":
        """A deep copy — the attacker inherits the regular user's harvested ids."""
        return FieldRepo(
            _values=deepcopy(self._values),
            _owner_values=deepcopy(self._owner_values),
            _created_values=deepcopy(self._created_values),
        )


def _is_scalar(value: Any) -> bool:
    """True for an id-shaped scalar (str/int/float). Excludes bool: an id is never boolean, and
    bool is an int subclass."""
    return isinstance(value, (str, int, float)) and not isinstance(value, bool)


def harvest(
    repo: FieldRepo,
    source_key: str,
    status_code: int | str,
    media_type: str,
    response_data: Any,
    relations: Relations,
    owner: str = "regular",
    created: bool = False,
) -> int:
    """Harvest id values from one real response into `repo`. Returns the count stored.

    Walks `response_data` at the source pointer of every relation edge whose source is
    `source_key` and whose declared `(status_code, media_type)` matches this response, storing
    each scalar value found (full-depth, arrays/maps fanned out by `views.data_values`). `owner`
    is the identity whose call produced the response, recorded so victim vs attacker ids stay distinct.
    `created` marks values from a create (POST) response as provably owned by `owner`.
    """
    stored = 0
    status = str(status_code)
    for rel in relations:
        if rel["source_key"] != source_key:
            continue
        src = rel["edge"]["source"]
        if src["status_code"] != status or src["media_type"] != media_type:
            continue
        for value in views.data_values(response_data, src["json_pointer"]):
            if _is_scalar(value):
                before = len(repo.get(source_key, src["json_pointer"]))
                repo.add(source_key, src["json_pointer"], value, owner=owner, created=created)
                stored += len(repo.get(source_key, src["json_pointer"])) - before
    return stored


def candidates_for_parameter(
    repo: FieldRepo, relations: Relations, target_key: str, name: str, location: str
) -> list[Any]:
    """Harvested values that can fill parameter `(name, location)` on `target_key`."""
    out: list[Any] = []
    for rel in relations:
        if rel["target_key"] != target_key:
            continue
        tgt = rel["edge"]["target"]
        if tgt.get("type") != "parameter" or tgt.get("name") != name or tgt.get("location") != location:
            continue
        _extend_unique(out, repo.get(rel["source_key"], rel["edge"]["source"]["json_pointer"]))
    return out


def candidates_for_body(
    repo: FieldRepo, relations: Relations, target_key: str, media_type: str, json_pointer: str
) -> list[Any]:
    """Harvested values that can fill request-body field `json_pointer` on `target_key`."""
    out: list[Any] = []
    for rel in relations:
        if rel["target_key"] != target_key:
            continue
        tgt = rel["edge"]["target"]
        if (
            tgt.get("type") != "request_body"
            or tgt.get("media_type") != media_type
            or tgt.get("json_pointer") != json_pointer
        ):
            continue
        _extend_unique(out, repo.get(rel["source_key"], rel["edge"]["source"]["json_pointer"]))
    return out


def _extend_unique(out: list[Any], values: list[Any]) -> None:
    """Append values to `out`, skipping any already present (order-preserving dedupe)."""
    for v in values:
        if v not in out:
            out.append(v)
"""Shape persisted snapshots into the per-object evidence the analyzer judges.

Pure arrangement (like `relations/context.py` and `strategies/context.py`): it groups a run's
snapshot rows by `object_key` into the three phases, then reduces each object to a **compact,
pre-computed** evidence record — so the LLM judges a small, meaningful input instead of three full
response bodies. Two reductions happen here, in code, deterministically:

- **Ownership filter.** Only an object the legitimate owner could actually read (`snap_before` is a
  `2xx`) can be the owner's object. If the baseline read was refused/absent, the object is not the
  owner's (it is the attacker's own, or never existed at baseline), so it cannot be a crossing and is
  dropped — this is what stops the attacker's *own* objects being reported as findings.
- **Deterministic diff + gating.** The owner-before vs owner-after change is computed here as a
  structural path-level diff (git-style), not delegated to the model; and an object with **no**
  signal — the attacker was refused *and* nothing changed — is dropped without an LLM call. The model
  only ever sees objects that actually leaked or actually changed.

Analysis reads **only** from persisted snapshots — never the live `FieldRepo` — which is what makes
it a re-runnable function of frozen evidence.
"""

from __future__ import annotations

from typing import Any

_MAX_RESPONSE_CHARS = 4000


def group_objects(snapshots: list[dict]) -> list[dict[str, Any]]:
    """Group snapshot rows into compact, owner-filtered, diff-carrying evidence records.

    Each surviving record carries the object identity, the attacker's view (status + response, for
    the read judgement) and `victim_change` — the deterministic owner-before→after diff (for the
    write judgement). Dropped, without reaching the model: objects with no attacker view, objects the
    owner could not read at baseline (not the owner's object), and objects with neither a read nor a
    write signal.
    """
    objects: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for snap in snapshots:
        if snap["status"] != "done":
            continue
        key = snap["object_key"]
        if key not in objects:
            objects[key] = {
                "object_key": key,
                "op_key": snap["op_key"],
                "object_id": snap.get("request") or {},
            }
            order.append(key)
        objects[key][snap["phase"]] = {
            "status_code": snap.get("status_code"),
            "response": snap.get("response"),
        }

    out: list[dict[str, Any]] = []
    for key in order:
        record = _reduce(objects[key])
        if record is not None:
            out.append(record)
    return out


def _reduce(obj: dict[str, Any]) -> dict[str, Any] | None:
    """Filter + compact one grouped object into an evidence record, or None to drop it."""
    hacker = obj.get("snap_hacker")
    if hacker is None:
        return None  # no authorization crossing was attempted — nothing to judge
    before = obj.get("snap_before")
    if not (before and _is_2xx(before.get("status_code"))):
        return None  # the owner could not read it at baseline → not the owner's object

    after = obj.get("snap_after")
    victim_change = _diff(before.get("response"), after.get("response")) if after else []
    attacker_status = hacker.get("status_code")
    attacker_got_data = _is_2xx(attacker_status) and _nonempty(hacker.get("response"))

    if not attacker_got_data and not victim_change:
        return None  # attacker refused and owner's object unchanged → no signal, skip (no LLM call)

    return {
        "object_key": obj["object_key"],
        "op_key": obj["op_key"],
        "object_id": obj["object_id"],
        "owner_baseline_status": before.get("status_code"),
        "attacker": {
            "status_code": attacker_status,
            "response": _truncate(hacker.get("response")),
        },
        "victim_change": victim_change,
    }


def build_object_context(objects: list[dict], access_description: str) -> dict[str, Any]:
    """The JSON document for one analysis call: the access hint + a batch of objects."""
    return {
        "access_model": access_description or "(none provided)",
        "objects": objects,
    }


def response_objects(executions: list[dict]) -> list[dict[str, Any]]:
    """Attacker calls whose *own response* leaked owner data — evidence the snapshots cannot hold.

    Snapshots only capture re-readable GET-by-id objects, so reply-only crossings (a reveal, a
    report, a search, a deep-chain read) never reach the snapshot judge. This reconstructs them from
    the execution log: an attacker call that (1) returned a `2xx` with a body and (2) addressed an
    identifier the **owner's** own responses exposed — i.e. an id belonging to the owner, not the
    attacker. Ownership is derived generically from the log itself (no per-id tagging): a value the
    regular user's responses revealed is the regular user's; the attacker's own ids never appear
    there, so the attacker reaching its own objects is excluded by construction.
    """
    owner_ids = _owner_id_values(executions)
    out: list[dict[str, Any]] = []
    for rec in executions:
        if rec.get("identity") != "attacker" or not _is_2xx(rec.get("status_code")):
            continue
        if not _nonempty(rec.get("response")):
            continue
        used = sorted(_id_scalars(rec.get("request") or {}) & owner_ids)
        if not used:
            continue
        out.append({
            "object_key": f"response:{rec.get('seq')}:{rec.get('op_key')}",
            "op_key": rec.get("op_key"),
            "request": rec.get("request"),
            "owner_id_used": used,
            "attacker_response": _truncate(rec.get("response")),
        })
    return out


def build_response_context(objects: list[dict], access_description: str) -> dict[str, Any]:
    """The JSON document for one response-judge call: the access hint + a batch of attacker replies."""
    return {
        "access_model": access_description or "(none provided)",
        "responses": objects,
    }


def _owner_id_values(executions: list[dict]) -> set:
    """Id-shaped values the regular (owner) user's responses exposed — the owner's identifiers."""
    owned: set = set()
    for rec in executions:
        if rec.get("identity") == "regular":
            owned |= _id_scalars(rec.get("response"))
    return owned


def _id_scalars(value: Any, key: str | None = None) -> set:
    """All id-shaped scalar leaves in a value, normalised to strings.

    Two kinds count as ids: a long string (>= 8 chars — a UUID/token), or **any scalar (incl. small
    integers) sitting under an id-shaped key** (`id`, `*_id`, `*Id`). The latter is what catches APIs
    whose object ids are small integers (e.g. `1`, `7`), which the length floor alone would miss and
    so leave reply-only read crossings undetected. Other short strings/numbers are ignored to avoid
    spurious request↔response matches.
    """
    found: set = set()
    if isinstance(value, dict):
        for k, v in value.items():
            found |= _id_scalars(v, key=k)
    elif isinstance(value, list):
        for v in value:
            found |= _id_scalars(v, key=key)
    elif isinstance(value, str) and len(value) >= 8:
        found.add(value)
    elif key is not None and _is_id_key(key) and isinstance(value, (str, int)) and not isinstance(value, bool):
        found.add(str(value))
    return found


def _is_id_key(key: str) -> bool:
    """True if a field name denotes an object id (`id`, `user_id`, `accountId`, …)."""
    k = key.lower()
    return k == "id" or k.endswith("_id") or k.endswith("id")


def _diff(before: Any, after: Any, path: str = "") -> list[dict[str, Any]]:
    """A structural, path-level diff of two JSON values (git-style): the list of changed leaves.

    Each entry is `{path, before, after}` at the JSON pointer `path`. Dict keys are compared by name
    (order-independent); lists by index; everything else by equality. An empty list means the two
    values are identical — the signal the gate uses to skip an unchanged object.
    """
    if isinstance(before, dict) and isinstance(after, dict):
        changes: list[dict[str, Any]] = []
        for k in sorted(set(before) | set(after), key=str):
            changes.extend(_diff(before.get(k, _MISSING), after.get(k, _MISSING), f"{path}/{k}"))
        return changes
    if isinstance(before, list) and isinstance(after, list):
        changes = []
        for i in range(max(len(before), len(after))):
            b = before[i] if i < len(before) else _MISSING
            a = after[i] if i < len(after) else _MISSING
            changes.extend(_diff(b, a, f"{path}/{i}"))
        return changes
    if before == after:
        return []
    return [{"path": path or "/", "before": _show(before), "after": _show(after)}]


class _Missing:
    """Sentinel for a key/index present on one side of a diff but absent on the other."""

    def __repr__(self) -> str:
        return "(absent)"


_MISSING = _Missing()


def _show(value: Any) -> Any:
    """Render a diff side, mapping the absence sentinel to None for JSON-serialisability."""
    return None if isinstance(value, _Missing) else value


def _is_2xx(status: Any) -> bool:
    """True for a 2xx HTTP status code."""
    return isinstance(status, int) and 200 <= status < 300


def _nonempty(response: Any) -> bool:
    """True if a response body actually carries data (not None / empty string / empty container)."""
    if response is None:
        return False
    if isinstance(response, (str, list, dict)):
        return len(response) > 0
    return True


def _truncate(response: Any) -> Any:
    """Cap a long string response so the prompt stays bounded; pass other values through."""
    if isinstance(response, str) and len(response) > _MAX_RESPONSE_CHARS:
        return response[:_MAX_RESPONSE_CHARS] + "…(truncated)"
    return response

"""Capture object state by replaying read-only GET-by-id requests, resumably.

The analyzer (Step 6) judges whether one user reached another user's object; to do that it needs
*evidence* — the object's state seen from three vantage points: the victim before the attack
(`snap_before`), the attacker (`snap_hacker`), and the victim again after (`snap_after`). This
module captures one such vantage point. The run manager (Step 7) calls it three times with the
right `AuthSession` and `FieldRepo` for each phase.

A "snapshot" is the set of GET responses over the tracked objects at one point, taken as one user.
An *object* is a GET-by-id endpoint bound to harvested ids: for every `GET` operation whose path
parameters can all be filled from the id pool (via the detected relations), each combination of
candidate ids is one object, addressed by a stable `object_key` so the three phases line up.

Capture is **append-only and resumable** (the relation-runner pattern): each object is checkpointed
as a `SnapshotRecord` the moment it is taken, so a crash, an error (`on_error: stop`), or a Ctrl-C
leaves progress on disk and a rerun skips the objects already captured. Read-only GETs make
re-capture harmless. HTTP goes through the `ApiExecutor`, so unit tests inject a fake transport and
never touch the network.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from itertools import product
from typing import Optional

from tqdm import tqdm

from bola.db.store import Store
from bola.execution.api_executor import ApiExecutor, ApiRequest, AuthSession
from bola.execution.field_repo import FieldRepo, candidates_for_parameter
from bola.spec import views
from bola.spec.model import Spec
from bola.spec.views import OperationRef

logger = logging.getLogger("bola.execution.snapshotter")

# The three vantage points; the run manager picks the AuthSession/FieldRepo for each.
SNAP_BEFORE = "snap_before"
SNAP_HACKER = "snap_hacker"
SNAP_AFTER = "snap_after"

Relations = list[dict]


@dataclass
class SnapshotTarget:
    """One object to capture: a GET operation bound to a concrete set of harvested ids."""

    op_key: str
    object_key: str
    request: ApiRequest


@dataclass
class SnapshotResult:
    """Outcome counts of one snapshot-capture phase (targets total / captured / skipped / failed)."""

    targets_total: int = 0
    captured: int = 0
    skipped: int = 0
    failed: int = 0
    completed: bool = False


def object_key(op_key: str, path_params: dict, query: dict | None = None) -> str:
    """A stable identity for an object across phases: its op, its bound ids, and any probe query."""
    bound = "|".join(f"{name}={path_params[name]}" for name in sorted(path_params))
    if query:
        bound += "|?" + "&".join(f"{k}={query[k]}" for k in sorted(query))
    return f"{op_key}|{bound}"


def _query_probe(params: list) -> dict:
    """A generic probe value for each optional query param of a GET op (empty if none).

    Some endpoints widen what they return — and skip an ownership check — when an optional query
    parameter (e.g. an `expand`/`include`/filter) is present. Snapshotting a variant with those set
    surfaces that bypass. Values are generic: a declared `enum`'s first value, else a neutral
    truthy token — never anything target-specific.
    """
    probe: dict = {}
    for p in params:
        if p.location != "query" or p.required:
            continue
        enum = (p.constraints or {}).get("enum")
        if enum:
            probe[p.name] = enum[0]
        elif p.type == "boolean":
            probe[p.name] = True
        elif p.type in ("integer", "number"):
            probe[p.name] = 1
        else:
            probe[p.name] = "1"
    return probe


def snapshot_targets(
    spec: Spec,
    operations: list[OperationRef],
    relations: Relations,
    repo: FieldRepo,
) -> list[SnapshotTarget]:
    """Every GET-by-id object whose path params can all be filled from the id pool.

    For each `GET` operation, each path parameter is bound from the harvested ids a relation says
    can fill it; an operation with no path params, or any path param the pool cannot supply, yields
    no object (we only snapshot objects we actually discovered ids for). When several ids fit, every
    combination is its own object.
    """
    out: list[SnapshotTarget] = []
    seen: set[str] = set()
    for op in operations:
        if op.method != "GET":
            continue
        path_params = [p for p in views.parameters(spec, op) if p.location == "path"]
        if not path_params:
            continue
        candidate_lists = [
            candidates_for_parameter(repo, relations, op.key, p.name, "path")
            for p in path_params
        ]
        if any(not c for c in candidate_lists):
            continue  # an unfilled path param means we cannot address a real object
        probe = _query_probe(views.parameters(spec, op))
        for combo in product(*candidate_lists):
            bound = {p.name: value for p, value in zip(path_params, combo)}
            # the plain object, then (if the op has optional query params) a probed variant that may
            # take a different, looser authorization path
            for query in ([{}, probe] if probe else [{}]):
                key = object_key(op.key, bound, query or None)
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    SnapshotTarget(
                        op_key=op.key,
                        object_key=key,
                        request=ApiRequest(method="GET", path=op.path, path_params=bound,
                                           query=dict(query)),
                    )
                )
    return out


def capture_snapshots(
    phase: str,
    run_id: str,
    spec: Spec,
    operations: list[OperationRef],
    relations: Relations,
    repo: FieldRepo,
    executor: ApiExecutor,
    store: Store,
    *,
    auth: Optional[AuthSession] = None,
    on_error: str = "stop",
    show_progress: bool = True,
    reraise_interrupt: bool = True,
) -> SnapshotResult:
    """Capture one phase's snapshots over `repo`'s objects, resumably, into `store`.

    On Ctrl-C the progress is checkpointed first, then by default the interrupt is **re-raised** so
    the caller (the run manager, a CLI) halts cleanly — every captured object is already on disk, so
    a rerun resumes. Pass `reraise_interrupt=False` to instead swallow it and return a `SnapshotResult`
    with `completed=False` (for a caller that prefers to inspect the result and continue).
    """
    targets = snapshot_targets(spec, operations, relations, repo)
    done = {
        snap["object_key"]
        for snap in store.get_snapshots(run_id)
        if snap["phase"] == phase and snap["status"] == "done"
    }
    pending = [t for t in targets if t.object_key not in done]
    result = SnapshotResult(targets_total=len(targets), skipped=len(targets) - len(pending))

    bar = tqdm(
        total=len(targets),
        initial=result.skipped,
        desc=f"Snapshot {phase}",
        unit="obj",
        disable=not show_progress,
    )
    stopped = False
    interrupted = False
    try:
        for target in pending:
            exec_result = executor.execute(target.request, auth)
            if exec_result.error is not None:
                store.save_snapshot(
                    run_id, phase, target.op_key, target.object_key, "failed",
                    request=target.request.path_params, error=exec_result.error,
                )
                result.failed += 1
                logger.error("snapshot %s %s failed: %s", phase, target.object_key, exec_result.error)
                if on_error == "stop":
                    stopped = True
            else:
                store.save_snapshot(
                    run_id, phase, target.op_key, target.object_key, "done",
                    request=target.request.path_params,
                    status_code=exec_result.status_code,
                    response=exec_result.response,
                )
                result.captured += 1
            bar.update(1)
            if stopped:
                break
    except KeyboardInterrupt:
        logger.warning("interrupted — snapshot progress saved, rerun to resume")
        stopped = True
        interrupted = True
    bar.close()

    if interrupted and reraise_interrupt:
        raise KeyboardInterrupt
    result.completed = not stopped and len(done) + result.captured == len(targets)
    return result
"""Execution layer: actually call the target and remember what comes back.

`api_executor` sends one request (applying the manifest's auth); `field_repo` harvests id values
from real responses and serves them back as inputs for later calls. The snapshotter (Step 6)
joins this package. This is the mechanical primitive the strategies (Step 5) and the run manager
(Step 7) drive — it sends requests and stores values; it does not decide what to call.
"""

from bola.execution.api_executor import (
    ApiExecutor,
    ApiRequest,
    AuthSession,
    ExecutionResult,
    HttpRequest,
    HttpResponse,
    Transport,
    build_auth_session,
    requests_transport,
)
from bola.execution.field_repo import (
    FieldRepo,
    candidates_for_body,
    candidates_for_parameter,
    harvest,
)
from bola.execution.snapshotter import (
    SNAP_AFTER,
    SNAP_BEFORE,
    SNAP_HACKER,
    SnapshotResult,
    SnapshotTarget,
    capture_snapshots,
    object_key,
    snapshot_targets,
)

__all__ = [
    "ApiExecutor",
    "ApiRequest",
    "AuthSession",
    "ExecutionResult",
    "HttpRequest",
    "HttpResponse",
    "Transport",
    "build_auth_session",
    "requests_transport",
    "FieldRepo",
    "harvest",
    "candidates_for_parameter",
    "candidates_for_body",
    "capture_snapshots",
    "snapshot_targets",
    "object_key",
    "SnapshotTarget",
    "SnapshotResult",
    "SNAP_BEFORE",
    "SNAP_HACKER",
    "SNAP_AFTER",
]
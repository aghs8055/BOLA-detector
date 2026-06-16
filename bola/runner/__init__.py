"""Run orchestration: drive the phases, own the persistence, compute the metrics.

`execute_run` runs a whole target end to end — setup → explore → snap_before → attack → snap_hacker
→ snap_after → analyze — appending every action to a resumable log and rebuilding the `FieldRepo`
from it. `analyze_and_report` is the re-runnable analysis-plus-metrics pass over a finished run's
snapshots. `metrics` derives the thesis numbers as a pure function of the persisted records. See
`README.md`.
"""

from bola.runner.metrics import compute_metrics, compute_quality, compute_run_metrics
from bola.runner.run_manager import RunResult, analyze_and_report, execute_run

__all__ = [
    "execute_run",
    "analyze_and_report",
    "RunResult",
    "compute_metrics",
    "compute_run_metrics",
    "compute_quality",
]
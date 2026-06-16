"""Report aggregation: fuse a finished run's analysis findings and AI-explorer memory into one
uniform, human-facing report.

`aggregate_run` is a single structured LLM call over already-persisted evidence (it never
re-contacts the target): it reads the analyzer's per-object verdicts, the run metrics, the AI
strategy's per-turn memory, and a compact spec summary, and emits an `AggregatedReport` split into
BOLA vs non-BOLA findings plus open questions for human review. `write_report_json` emits the same
report as `runs/<run_id>/report.json` — the artifact a later HTML view renders. See `README.md`.
"""

from bola.aggregate.aggregator import ReportAggregator
from bola.aggregate.html import render_report_html
from bola.aggregate.models import AggregatedReport, OpenQuestion, ReportFinding
from bola.aggregate.runner import AggregateResult, aggregate_run, write_report_json

__all__ = [
    "ReportAggregator",
    "AggregatedReport",
    "ReportFinding",
    "OpenQuestion",
    "AggregateResult",
    "aggregate_run",
    "write_report_json",
    "render_report_html",
]

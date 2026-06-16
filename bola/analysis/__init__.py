"""Analysis: judge whether the captured snapshots show a BOLA.

Decoupled from execution and **re-runnable**: execution freezes the evidence (snapshots), then this
package judges it any number of times — a fresh `analysis_id` per pass, under a different `access`
description or model, all accumulating against one execution `run_id`. The `Analyzer` is the
LLM-as-judge (stateless per-object verdicts); `analyze_run` is the resumable orchestrator. See
`README.md`.
"""

from bola.analysis.analyzer import Analyzer
from bola.analysis.context import build_object_context, group_objects
from bola.analysis.models import AnalysisBatch, ObjectVerdict
from bola.analysis.runner import AnalysisResult, analyze_run

__all__ = [
    "Analyzer",
    "AnalysisBatch",
    "ObjectVerdict",
    "AnalysisResult",
    "analyze_run",
    "group_objects",
    "build_object_context",
]
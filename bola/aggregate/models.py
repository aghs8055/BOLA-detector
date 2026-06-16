"""Pydantic models for the report-aggregation LLM I/O.

The aggregator correlates two independent evidence streams for one run — the analyzer's per-object
BOLA verdicts (judged from snapshots) and the AI explorer's own per-turn memory (what it claimed to
have done while testing) — into one uniform, human-facing report. Findings are split into
`bola_findings` vs `non_bola_findings`, but both use the same `ReportFinding` schema so the two
categories render identically.

`evidence_source` makes provenance explicit: a finding the analyzer confirmed from snapshots is
stronger than one seen only in the explorer's narration, and the report must surface that gap rather
than hide it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ReportFinding(BaseModel):
    """One finding in the aggregated report — the uniform shape for both BOLA and non-BOLA."""

    title: str = Field(description="Short human-readable name for the finding")
    apis: list[str] = Field(
        default_factory=list,
        description="Affected operations as 'METHOD /path' (use the spec summary's `api` values)",
    )
    severity: Literal["info", "low", "medium", "high", "critical"] = Field(
        default="info", description="Rough impact rating"
    )
    description: str = Field(description="What the issue is and why it matters, in plain language")
    evidence_source: Literal["analysis", "ai_memory", "both"] = Field(
        description="Where the evidence came from: snapshot analysis, the AI explorer's memory, or both"
    )
    how_it_was_found: str = Field(
        description="The signal that revealed it (e.g. attacker snapshot matched the owner's, or a "
        "cross-user write the explorer recorded)"
    )
    how_to_regenerate: list[str] = Field(
        default_factory=list,
        description="Concrete reproduction steps grounded in the evidence: authenticate as which "
        "user, call which endpoint with which object id, expected vs. observed result",
    )
    fix_suggestion: str = Field(description="How to remediate the issue")


class OpenQuestion(BaseModel):
    """Something the test surfaced that the aggregation could not resolve — flagged for a human."""

    question: str = Field(description="The unresolved question")
    why_unresolved: str = Field(description="Why it cannot be settled from the available evidence")
    suggested_next_step: str = Field(
        default="", description="What a human should do to resolve it"
    )


class AggregatedReport(BaseModel):
    """The structured aggregation output: target summary, categorized findings, and open questions."""

    model_config = ConfigDict(extra="ignore")

    target_summary: str = Field(
        description="A few sentences describing the target and its intended authorization model"
    )
    bola_findings: list[ReportFinding] = Field(
        default_factory=list,
        description="Broken object-level authorization findings (confirmed or strongly evidenced)",
    )
    non_bola_findings: list[ReportFinding] = Field(
        default_factory=list,
        description="Other notable issues that are not BOLA (server errors, validation gaps, "
        "observations worth reporting)",
    )
    open_questions: list[OpenQuestion] = Field(
        default_factory=list,
        description="Things needing human review that the aggregation could not resolve",
    )
"""Unit tests for `bola.aggregate.models` — the structured report schema."""

from bola.aggregate.models import AggregatedReport, OpenQuestion, ReportFinding


def test_report_finding_defaults():
    f = ReportFinding(
        title="t", description="d", evidence_source="analysis",
        how_it_was_found="h", fix_suggestion="fix",
    )
    assert f.apis == []
    assert f.how_to_regenerate == []
    assert f.severity == "info"


def test_aggregated_report_defaults_to_empty_lists():
    r = AggregatedReport(target_summary="s")
    assert r.bola_findings == []
    assert r.non_bola_findings == []
    assert r.open_questions == []


def test_aggregated_report_ignores_extra_fields():
    r = AggregatedReport.model_validate({"target_summary": "s", "surprise": 1})
    assert r.target_summary == "s"


def test_open_question_round_trips():
    q = OpenQuestion(question="q?", why_unresolved="no evidence")
    assert q.suggested_next_step == ""
    assert q.model_dump()["question"] == "q?"
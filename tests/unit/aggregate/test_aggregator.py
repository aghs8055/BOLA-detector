"""Unit tests for `bola.aggregate.aggregator` — the single structured call, with an injected chain."""

from bola.aggregate.aggregator import ReportAggregator
from bola.aggregate.models import AggregatedReport, ReportFinding


class _FakeChain:
    """Records the input it was invoked with and returns a canned report."""

    def __init__(self, report):
        self.report = report
        self.last_input = None

    def invoke(self, payload, config=None):
        self.last_input = payload
        return self.report


def test_aggregate_passes_context_json_and_returns_report(settings):
    report = AggregatedReport(
        target_summary="s",
        bola_findings=[ReportFinding(
            title="t", description="d", evidence_source="analysis",
            how_it_was_found="h", fix_suggestion="f",
        )],
    )
    chain = _FakeChain(report)
    agg = ReportAggregator(settings, chain=chain)

    out = agg.aggregate({"target": "http://t", "metrics": {"x": 1}})

    assert out is report
    assert "http://t" in chain.last_input["context"]  # context serialized into the prompt var


def test_model_name_falls_back_to_main_model(settings):
    settings.llm.analysis_model = ""
    settings.llm.model = "main-model"
    agg = ReportAggregator(settings, chain=_FakeChain(AggregatedReport(target_summary="s")))
    assert agg.model_name == "main-model"


def test_model_name_prefers_analysis_model(settings):
    settings.llm.analysis_model = "judge-model"
    agg = ReportAggregator(settings, chain=_FakeChain(AggregatedReport(target_summary="s")))
    assert agg.model_name == "judge-model"

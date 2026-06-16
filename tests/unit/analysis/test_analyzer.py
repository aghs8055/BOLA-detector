"""Unit tests for `bola.analysis.analyzer`."""

import json
from pathlib import Path

from langchain_core.prompts import PromptTemplate

from bola.analysis.analyzer import Analyzer
from bola.analysis.models import AnalysisBatch, ObjectVerdict


def test_analyze_template_has_only_the_context_variable():
    # guards against a stray `{...}` in the prose being read as a template variable, which would
    # make every analysis call fail at runtime (it has no value to fill it with).
    text = (Path("bola/templates/bola/analyze.md")).read_text(encoding="utf-8")
    assert PromptTemplate.from_template(text).input_variables == ["context"]


class FakeChain:
    """Records inputs; returns a canned AnalysisBatch from invoke and batch."""

    def __init__(self, batch):
        self.batch_out = batch
        self.invoked = []
        self.batched = None

    def invoke(self, inputs, config=None):
        self.invoked.append(inputs)
        return self.batch_out

    def batch(self, inputs, config=None, return_exceptions=False):
        self.batched = inputs
        return [self.batch_out for _ in inputs]


def _objects():
    return [{
        "object_key": "getPet|petId=7",
        "op_key": "getPet",
        "object_id": {"petId": 7},
        "snap_before": {"status_code": 200, "response": {"id": 7, "owner": "alice"}},
        "snap_hacker": {"status_code": 200, "response": {"id": 7, "owner": "alice"}},
        "snap_after": {"status_code": 200, "response": {"id": 7, "owner": "alice"}},
    }]


def test_analyze_batch_passes_access_and_objects_into_context(settings):
    chain = FakeChain(AnalysisBatch(verdicts=[ObjectVerdict(object_key="getPet|petId=7",
                                                            rationale="match", is_bola=True)]))
    az = Analyzer(settings, access_description="only owners read pets", chain=chain)
    out = az.analyze_batch(_objects())
    assert out.verdicts[0].is_bola
    ctx = json.loads(chain.invoked[0]["context"])
    assert ctx["access_model"] == "only owners read pets"
    assert ctx["objects"][0]["object_key"] == "getPet|petId=7"


def test_analyze_batches_uses_chain_batch(settings):
    chain = FakeChain(AnalysisBatch(verdicts=[]))
    az = Analyzer(settings, chain=chain)
    az.analyze_batches([_objects(), _objects()])
    assert len(chain.batched) == 2  # one prompt per object-group


def test_model_name_falls_back_to_main_model(settings):
    az = Analyzer(settings, chain=FakeChain(AnalysisBatch()))
    assert az.model_name == settings.llm.model


def test_model_name_uses_analysis_model_when_set(settings):
    settings.llm.analysis_model = "anthropic/claude-opus-4-8"
    az = Analyzer(settings, chain=FakeChain(AnalysisBatch()))
    assert az.model_name == "anthropic/claude-opus-4-8"
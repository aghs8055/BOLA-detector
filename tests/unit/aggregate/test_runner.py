"""Unit tests for `bola.aggregate.runner` — gather → aggregate → persist, with an injected aggregator."""

import json

import pytest

from bola.aggregate.models import AggregatedReport, ReportFinding
from bola.aggregate.runner import aggregate_run, write_report_json
from bola.db.models import RunRecord
from bola.db.store import Store
from bola.spec.loader import load_spec_from_dict
from bola.target.manifest import load_manifest_from_dict


class FakeAggregator:
    """Canned aggregator — echoes back the findings it was given so we can assert wiring."""

    def __init__(self, *, model_name="fake-judge"):
        self.model_name = model_name
        self.seen_context = None

    def aggregate(self, context):
        self.seen_context = context
        bolas = [
            ReportFinding(
                title=f["object_key"], apis=[f.get("op_key", "")],
                description="d", evidence_source="analysis",
                how_it_was_found="snapshot", fix_suggestion="owner check",
            )
            for f in context["analysis_findings"] if f.get("is_bola")
        ]
        return AggregatedReport(target_summary="summary of " + context["target"], bola_findings=bolas)


@pytest.fixture
def seeded_store(tmp_path):
    """A store with one completed run + analysis pass (one BOLA finding) + a memory turn."""
    store = Store(db_path=str(tmp_path / "agg.db"))
    store.create_run(RunRecord(run_id="r1", target_name="http://t", strategy="ai", status="completed"))
    store.update_run("r1", metrics_json=json.dumps({"execution": {"regular_calls": 1}}))
    store.start_analysis("r1-auto", "r1", "judge", "h", {})
    store.finish_analysis("r1-auto", [
        {"object_key": "getPet|petId=7", "op_key": "getPetById", "is_bola": True, "rationale": "x"},
        {"object_key": "getPet|petId=8", "op_key": "getPetById", "is_bola": False, "rationale": "y"},
    ], tokens={"input_tokens": 40, "output_tokens": 8, "total_tokens": 48}, duration_s=1.5)
    store.save_analysis_metrics("r1-auto", {"quality": {"tp": 1}, "llm": {
        "strategy_tokens": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}}})
    store.save_strategy_memory("r1", "run", 0, {"notes": "explored", "conclusions": ["found one"]})
    store.append_execution("r1", 0, "run", "getPetById", identity="attacker",
                           request={"method": "GET", "path": "/pet/{petId}", "path_params": {"petId": 7}},
                           status_code=200, ok=True)
    return store


def test_aggregate_run_persists_report_and_payload(settings, seeded_store):
    agg = FakeAggregator()
    result = aggregate_run("r1", settings, seeded_store, access_description="owner only", aggregator=agg)

    # the aggregator saw the findings + memory + metrics + the authoritative execution log
    assert agg.seen_context["access_model"] == "owner only"
    assert agg.seen_context["metrics"]["quality"]["tp"] == 1
    assert agg.seen_context["ai_memory"][0]["notes"] == "explored"
    assert agg.seen_context["executions"][0] == {
        "identity": "attacker", "op_key": "getPetById", "method": "GET",
        "path": "/pet/{petId}", "path_params": {"petId": 7}, "status_code": 200, "ok": True,
    }

    # payload wraps the report with deterministic metrics + metadata
    assert result.payload["run_id"] == "r1"
    assert result.payload["analysis_id"] == "r1-auto"
    assert result.payload["model"] == "fake-judge"
    assert result.payload["metrics"]["quality"]["tp"] == 1
    assert len(result.payload["report"]["bola_findings"]) == 1

    # per-step token + duration breakdown, separated by step
    steps = result.payload["steps"]
    assert set(steps) == {"relations", "strategy", "analysis", "aggregation"}
    for s in steps.values():
        assert set(s) == {"tokens", "duration_s"}
        assert set(s["tokens"]) == {"input_tokens", "output_tokens", "total_tokens"}
    # strategy tokens come from the run metrics; analysis duration from its record; strategy
    # duration from the execution-log span (one action → 0.0)
    assert steps["strategy"]["tokens"]["total_tokens"] == 120
    assert steps["analysis"]["tokens"]["total_tokens"] == 48
    assert steps["analysis"]["duration_s"] == 1.5
    assert steps["strategy"]["duration_s"] == 0.0
    assert steps["aggregation"]["tokens"] == result.tokens

    # persisted and re-readable
    stored = seeded_store.get_aggregated_report("r1")
    assert stored["report"]["bola_findings"][0]["title"] == "getPet|petId=7"
    assert stored["steps"]["analysis"]["tokens"]["total_tokens"] == 48


def test_aggregate_run_embeds_full_self_contained_evidence(settings, seeded_store):
    """The payload carries everything needed to read the run back without the DB."""
    seeded_store.save_snapshot(
        "r1", "snap_hacker", "getPetById", "getPet|petId=7", "done",
        request={"method": "GET", "path": "/pet/7"},
        status_code=200, response={"id": 7, "name": "rex"},
    )
    manifest = load_manifest_from_dict({
        "version": 1,
        "base_url": "http://t",
        "spec": {"kind": "file", "value": "/tmp/spec.yaml"},
        "auth": {},
        "users": {
            "regular": {"vars": {"email": "a@b.c"}},
            "attacker": {"vars": {"email": "x@y.z"}},
        },
        "access": "owner only",
    })
    spec = load_spec_from_dict(
        {"openapi": "3.0.0", "info": {"title": "T", "version": "1"}, "paths": {}}
    )

    p = aggregate_run(
        "r1", settings, seeded_store, spec=spec, manifest=manifest, aggregator=FakeAggregator()
    ).payload

    # manifest embedded verbatim (credentials included), raw spec embedded
    assert p["manifest"]["users"]["regular"]["vars"]["email"] == "a@b.c"
    assert p["spec"]["openapi"] == "3.0.0"
    # API calls: a compact summary and the full request/response log
    assert p["api_calls"]["summary"][0]["path"] == "/pet/{petId}"
    assert p["api_calls"]["full"][0]["request"]["path_params"] == {"petId": 7}
    # snapshots, memory, and the analysis pass are all carried in full
    assert p["snapshots"][0]["response"] == {"id": 7, "name": "rex"}
    assert p["ai_memory"][0]["memory"]["notes"] == "explored"
    assert p["analysis"]["findings"][0]["object_key"] == "getPet|petId=7"
    # digest: spec operation counts + per-identity and per-status-code call counts
    assert p["summary"]["spec"]["total_operations"] == 0  # the embedded spec has empty paths
    assert p["summary"]["spec"]["operations_by_method"] == {}
    assert p["summary"]["api_calls"]["total"] == 1
    assert p["summary"]["api_calls"]["by_identity"] == {"regular": 0, "attacker": 1}
    assert p["summary"]["api_calls"]["by_status_code"] == {"200": 1}


def test_aggregate_run_summary_spec_is_none_without_spec(settings, seeded_store):
    p = aggregate_run("r1", settings, seeded_store, aggregator=FakeAggregator()).payload
    assert p["summary"]["spec"] is None
    assert p["summary"]["api_calls"]["by_identity"] == {"regular": 0, "attacker": 1}


def test_aggregate_run_embeds_none_without_manifest_or_spec(settings, seeded_store):
    p = aggregate_run("r1", settings, seeded_store, aggregator=FakeAggregator()).payload
    assert p["manifest"] is None
    assert p["spec"] is None


def test_aggregate_run_defaults_to_newest_analysis(settings, seeded_store):
    result = aggregate_run("r1", settings, seeded_store, aggregator=FakeAggregator())
    assert result.payload["analysis_id"] == "r1-auto"  # found without passing analysis_id


def test_write_report_json_writes_file(settings, seeded_store, tmp_path):
    settings.storage.runs_dir = str(tmp_path / "runs")
    result = aggregate_run("r1", settings, seeded_store, aggregator=FakeAggregator())
    path = write_report_json(settings, "r1", result.payload)
    assert path.exists()
    on_disk = json.loads(path.read_text())
    assert on_disk["report"]["target_summary"].startswith("summary of")


def test_save_aggregated_report_overwrites(settings, seeded_store):
    aggregate_run("r1", settings, seeded_store, aggregator=FakeAggregator())
    seeded_store.save_aggregated_report("r1", "r1-auto", "m", {"report": {"target_summary": "v2"}})
    assert seeded_store.get_aggregated_report("r1")["report"]["target_summary"] == "v2"
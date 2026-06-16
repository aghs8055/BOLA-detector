"""Unit tests for `bola.runner.metrics`."""

import pytest

from bola.db.store import Store
from bola.runner import metrics


@pytest.fixture
def store(tmp_path):
    return Store(db_path=str(tmp_path / "t.db"))


def _seed_run(store, run_id="run1"):
    store.append_execution(run_id, 0, "run", "createPet", identity="regular", status_code=201,
                           ok=True, response={"id": 7}, media_type="application/json")
    store.append_execution(run_id, 1, "run", "createPet", identity="regular", status_code=201,
                           ok=True, response={"id": 8})
    store.append_execution(run_id, 2, "run", "getPet", identity="attacker", status_code=200,
                           ok=True, response={"id": 7})
    store.save_strategy_memory(run_id, "run", 0, {"notes": "n"},
                               tokens={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120})
    for phase in ("snap_before", "snap_hacker", "snap_after"):
        store.save_snapshot(run_id, phase, "getPet", "getPet|petId=7", "done",
                            status_code=200, response={"id": 7})


def test_run_metrics_counts(store):
    _seed_run(store)
    m = metrics.compute_run_metrics("run1", store, total_operations=4)
    assert m["coverage"] == {
        "total_operations": 4,
        "operations_called": 2,           # createPet + getPet
        "endpoint_coverage_pct": 50.0,
        "objects_snapshotted": 1,
    }
    assert m["execution"]["regular_calls"] == 2
    assert m["execution"]["hacker_calls"] == 1
    assert m["execution"]["snapshot_calls"] == 3
    assert m["execution"]["status_codes"] == {"200": 1, "201": 2}
    assert m["llm"]["strategy_turns"] == 1
    assert m["llm"]["strategy_tokens"] == {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
    assert "quality" not in m  # run-only metrics never include quality


def test_strategy_tokens_sum_across_turns_and_treat_missing_as_zero(store):
    store.save_strategy_memory("r", "run", 0, {},
                               tokens={"input_tokens": 5, "output_tokens": 3, "total_tokens": 8})
    store.save_strategy_memory("r", "run", 1, {})  # a turn with no captured cost contributes zero
    store.save_strategy_memory("r", "run", 2, {},
                               tokens={"input_tokens": 7, "output_tokens": 1, "total_tokens": 8})
    m = metrics.compute_run_metrics("r", store, total_operations=1)
    assert m["llm"]["strategy_tokens"] == {"input_tokens": 12, "output_tokens": 4, "total_tokens": 16}


def test_analysis_time_and_tokens_added_only_when_supplied(store):
    _seed_run(store)
    findings = [{"object_key": "getPet|petId=7", "is_bola": True}]
    analysis_tokens = {"input_tokens": 40, "output_tokens": 9, "total_tokens": 49}
    full = metrics.compute_metrics("run1", store, total_operations=4, findings=findings,
                                   analysis_elapsed_s=4.27, analysis_tokens=analysis_tokens)
    assert full["execution"]["phase_times_s"]["analysis"] == 4.27
    assert full["llm"]["analysis_tokens"] == analysis_tokens
    without = metrics.compute_run_metrics("run1", store, total_operations=4)
    assert "analysis" not in without["execution"]["phase_times_s"]
    assert "analysis_tokens" not in without["llm"]


def test_status_codes_tally_each_code_and_skip_missing(store):
    store.append_execution("r", 0, "run", "getPet", identity="regular", status_code=200, ok=True)
    store.append_execution("r", 1, "run", "getPet", identity="attacker", status_code=403, ok=False)
    store.append_execution("r", 2, "run", "getPet", identity="attacker", status_code=403, ok=False)
    store.append_execution("r", 3, "run", "getPet", identity="regular", status_code=None, ok=False)
    m = metrics.compute_run_metrics("r", store, total_operations=1)
    assert m["execution"]["status_codes"] == {"200": 1, "403": 2}


def test_coverage_pct_zero_when_no_operations(store):
    m = metrics.compute_run_metrics("empty", store, total_operations=0)
    assert m["coverage"]["endpoint_coverage_pct"] == 0.0


def test_quality_omitted_without_ground_truth():
    findings = [{"object_key": "getPet|petId=7", "is_bola": True}]
    assert metrics.compute_quality(findings, None) is None


def test_quality_scored_at_operation_granularity():
    findings = [
        {"object_key": "getPet|petId=7", "is_bola": True},    # TP (getPet vulnerable)
        {"object_key": "getUser|id=1", "is_bola": True},      # FP (getUser not vulnerable)
        {"object_key": "getPet|petId=8", "is_bola": False},   # FN (getPet vulnerable, missed)
        {"object_key": "getOrder|id=2", "is_bola": False},    # TN
    ]
    q = metrics.compute_quality(findings, ground_truth=["getPet"])
    assert (q["tp"], q["fp"], q["fn"], q["tn"]) == (1, 1, 1, 1)
    assert q["precision"] == 0.5
    assert q["recall"] == 0.5
    assert q["f1"] == 0.5
    assert q["accuracy"] == 0.5


def test_compute_metrics_includes_quality_when_ground_truth(store):
    _seed_run(store)
    findings = [{"object_key": "getPet|petId=7", "is_bola": True}]
    m = metrics.compute_metrics("run1", store, total_operations=4,
                                findings=findings, ground_truth=["getPet"])
    assert m["quality"]["tp"] == 1
    assert "coverage" in m and "execution" in m
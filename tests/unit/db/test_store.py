"""Unit tests for `bola.db.store`."""

import pytest
from datetime import datetime, timezone
from bola.db import RunRecord, Store


@pytest.fixture
def store(tmp_path):
    return Store(db_path=str(tmp_path / "test.db"))


# ------------------------------------------------------------------
# Relation cache
# ------------------------------------------------------------------

def test_cache_miss(store):
    assert store.get_relations("abc", "gpt-4") is None


def test_save_and_load_relations(store):
    rels = [{"source": "listUsers", "target": "getUser", "field": "id"}]
    store.save_relations("abc", "gpt-4", rels)
    assert store.get_relations("abc", "gpt-4") == rels


def test_cache_is_model_specific(store):
    store.save_relations("abc", "gpt-4", [{"x": 1}])
    assert store.get_relations("abc", "claude-3") is None


def test_cache_is_spec_specific(store):
    store.save_relations("hash-a", "gpt-4", [{"x": 1}])
    assert store.get_relations("hash-b", "gpt-4") is None


def test_overwrite_cache(store):
    store.save_relations("abc", "gpt-4", [{"x": 1}])
    store.save_relations("abc", "gpt-4", [{"x": 2}])
    assert store.get_relations("abc", "gpt-4") == [{"x": 2}]


def test_relation_tokens_stored_separately(store):
    tokens = {"input_tokens": 30, "output_tokens": 7, "total_tokens": 37}
    store.save_relations("abc", "gpt-4", [{"x": 1}], tokens=tokens)
    assert store.get_relation_tokens("abc", "gpt-4") == tokens
    assert store.get_relations("abc", "gpt-4") == [{"x": 1}]  # the list is unaffected


def test_relation_tokens_absent_is_none(store):
    store.save_relations("abc", "gpt-4", [{"x": 1}])
    assert store.get_relation_tokens("abc", "gpt-4") is None


# ------------------------------------------------------------------
# Relation group checkpoints
# ------------------------------------------------------------------

def test_group_results_empty(store):
    assert store.get_group_results("h", "m") == {}


def test_save_and_get_group_result(store):
    store.save_group_result("h", "m", "createPet", 0, "done", result=[{"e": 1}])
    got = store.get_group_results("h", "m")
    assert got[("createPet", 0)]["status"] == "done"
    assert got[("createPet", 0)]["result"] == [{"e": 1}]


def test_save_group_failed_carries_error(store):
    store.save_group_result("h", "m", "createPet", 1, "failed", error="boom")
    got = store.get_group_results("h", "m")[("createPet", 1)]
    assert got["status"] == "failed"
    assert got["error"] == "boom"
    assert got["result"] is None


def test_group_result_upsert(store):
    store.save_group_result("h", "m", "createPet", 0, "failed", error="boom")
    store.save_group_result("h", "m", "createPet", 0, "done", result=[{"e": 2}])
    got = store.get_group_results("h", "m")[("createPet", 0)]
    assert got["status"] == "done"
    assert got["result"] == [{"e": 2}]


def test_group_results_scoped_by_spec_and_model(store):
    store.save_group_result("h", "m", "op", 0, "done", result=[])
    assert store.get_group_results("other", "m") == {}
    assert store.get_group_results("h", "other") == {}


# ------------------------------------------------------------------
# Run records
# ------------------------------------------------------------------

def _run(run_id: str = "run-001", **kwargs) -> RunRecord:
    defaults = dict(
        run_id=run_id,
        target_name="juice-shop",
        strategy="random",
        status="running",
        config_json="{}",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kwargs)
    return RunRecord(**defaults)


def test_create_and_get_run(store):
    store.create_run(_run("r1"))
    r = store.get_run("r1")
    assert r is not None
    assert r.target_name == "juice-shop"
    assert r.status == "running"


def test_get_missing_run(store):
    assert store.get_run("nope") is None


def test_update_run_status(store):
    store.create_run(_run("r2"))
    store.update_run("r2", status="completed", findings_json='{"count":3}')
    r = store.get_run("r2")
    assert r.status == "completed"
    assert r.findings_json == '{"count":3}'


def test_update_nonexistent_run_is_noop(store):
    store.update_run("ghost", status="completed")  # must not raise


def test_list_runs_respects_limit(store):
    for i in range(5):
        store.create_run(_run(f"r{i}"))
    assert len(store.list_runs(limit=3)) == 3


def test_list_runs_empty(store):
    assert store.list_runs() == []


def test_list_runs_returns_detached_objects(store):
    store.create_run(_run("r1"))
    runs = store.list_runs()
    # Access attributes outside session — must not raise DetachedInstanceError
    assert runs[0].target_name == "juice-shop"


# ------------------------------------------------------------------
# Snapshots (resumable capture)
# ------------------------------------------------------------------

def test_snapshot_save_and_load(store):
    store.save_snapshot(
        "run1", "snap_before", "getPet", "getPet|petId=7", "done",
        request={"petId": 7}, status_code=200, response={"id": 7, "owner": "alice"},
    )
    snaps = store.get_snapshots("run1")
    assert len(snaps) == 1
    s = snaps[0]
    assert s["phase"] == "snap_before"
    assert s["object_key"] == "getPet|petId=7"
    assert s["request"] == {"petId": 7}
    assert s["response"] == {"id": 7, "owner": "alice"}
    assert s["status"] == "done"


def test_snapshot_is_run_scoped(store):
    store.save_snapshot("run1", "snap_before", "getPet", "getPet|petId=7", "done")
    assert store.get_snapshots("run2") == []


def test_snapshot_upsert_on_same_unit(store):
    store.save_snapshot("run1", "snap_hacker", "getPet", "getPet|petId=7", "failed", error="boom")
    store.save_snapshot(
        "run1", "snap_hacker", "getPet", "getPet|petId=7", "done", status_code=403, response=None,
    )
    snaps = [s for s in store.get_snapshots("run1") if s["phase"] == "snap_hacker"]
    assert len(snaps) == 1  # same (run, phase, object_key) updates in place
    assert snaps[0]["status"] == "done"
    assert snaps[0]["status_code"] == 403


def test_snapshot_phases_coexist(store):
    for phase in ("snap_before", "snap_hacker", "snap_after"):
        store.save_snapshot("run1", phase, "getPet", "getPet|petId=7", "done", status_code=200)
    phases = {s["phase"] for s in store.get_snapshots("run1")}
    assert phases == {"snap_before", "snap_hacker", "snap_after"}


# ------------------------------------------------------------------
# Analysis passes (resumable, re-runnable)
# ------------------------------------------------------------------

def test_analysis_item_checkpoint_roundtrip(store):
    store.start_analysis("an1", "run1", "claude", "deadbeef", {"objects_per_call": 5})
    store.save_analysis_item("an1", "getPet|petId=7", "done", verdict={"is_bola": True})
    items = store.get_analysis_items("an1")
    assert items["getPet|petId=7"]["status"] == "done"
    assert items["getPet|petId=7"]["verdict"] == {"is_bola": True}


def test_analysis_item_upsert(store):
    store.save_analysis_item("an1", "obj", "failed", error="boom")
    store.save_analysis_item("an1", "obj", "done", verdict={"is_bola": False})
    items = store.get_analysis_items("an1")
    assert len(items) == 1
    assert items["obj"]["status"] == "done"


def test_finish_analysis_writes_findings(store):
    store.start_analysis("an1", "run1", "claude", "h", {})
    store.finish_analysis("an1", [{"object_key": "x", "is_bola": True}])
    rec = store.get_analysis("an1")
    assert rec["status"] == "completed"
    assert rec["findings"] == [{"object_key": "x", "is_bola": True}]


def test_finish_analysis_stores_tokens_separately(store):
    store.start_analysis("an1", "run1", "claude", "h", {})
    tokens = {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15}
    store.finish_analysis("an1", [{"object_key": "x", "is_bola": False}], tokens=tokens)
    assert store.get_analysis("an1")["tokens"] == tokens


def test_aggregated_report_tokens_round_trip(store):
    tokens = {"input_tokens": 80, "output_tokens": 20, "total_tokens": 100}
    store.save_aggregated_report("run1", "an1", "judge", {"report": {"target_summary": "s"}}, tokens=tokens)
    payload = store.get_aggregated_report("run1")
    assert payload["report"]["target_summary"] == "s"


def test_multiple_analyses_per_run(store):
    store.start_analysis("an1", "run1", "claude", "h1", {})
    store.start_analysis("an2", "run1", "gpt", "h2", {})
    listed = store.list_analyses("run1")
    assert {a["analysis_id"] for a in listed} == {"an1", "an2"}
    assert {a["model_name"] for a in listed} == {"claude", "gpt"}


def test_get_missing_analysis_is_none(store):
    assert store.get_analysis("ghost") is None


# ------------------------------------------------------------------
# Strategy memory (per-turn reasoning state + token cost)
# ------------------------------------------------------------------


def test_strategy_memory_persists_tokens(store):
    store.save_strategy_memory("run1", "run", 0, {"notes": "n"},
                               tokens={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14})
    [row] = store.get_strategy_memories("run1")
    assert row["memory"] == {"notes": "n"}
    assert row["tokens"] == {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}


def test_strategy_memory_tokens_default_none(store):
    store.save_strategy_memory("run1", "run", 0, {"notes": "n"})
    [row] = store.get_strategy_memories("run1")
    assert row["tokens"] is None


def test_strategy_memory_upsert_overwrites_tokens(store):
    store.save_strategy_memory("run1", "run", 0, {}, tokens={"total_tokens": 5})
    store.save_strategy_memory("run1", "run", 0, {"notes": "redone"},
                               tokens={"total_tokens": 9})
    [row] = store.get_strategy_memories("run1")
    assert row["memory"] == {"notes": "redone"}
    assert row["tokens"] == {"total_tokens": 9}

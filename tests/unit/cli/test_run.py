"""`bola run` — ground-truth loading, relation gating, fresh-restart, and the happy path.

The heavy layers (`execute_run`, `detect_relations`, spec/manifest loading) are patched, so these
tests touch no network and no LLM — they exercise the CLI's orchestration and error handling.
"""

import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import cli.run as run_mod
from bola.db.store import Store
from bola.runner.run_manager import RunResult
from cli.app import app
from cli.common import CliError

runner = CliRunner()


# ----------------------------- pure helpers ---------------------------------


def test_load_ground_truth_list(tmp_path):
    p = tmp_path / "gt.yaml"
    p.write_text("- getThing\n- getOther\n")
    assert run_mod._load_ground_truth(str(p)) == ["getThing", "getOther"]


def test_load_ground_truth_dict_key(tmp_path):
    p = tmp_path / "gt.yaml"
    p.write_text("vulnerable:\n  - opA\n  - opB\n")
    assert run_mod._load_ground_truth(str(p)) == ["opA", "opB"]


def test_load_ground_truth_none():
    assert run_mod._load_ground_truth(None) is None


def test_load_ground_truth_bad_shape(tmp_path):
    p = tmp_path / "gt.yaml"
    p.write_text("just: a mapping\n")
    with pytest.raises(CliError):
        run_mod._load_ground_truth(str(p))


def test_ensure_relations_uses_cache(monkeypatch):
    monkeypatch.setattr(run_mod, "spec_hash", lambda s: "h")
    store = SimpleNamespace(get_relations=lambda h, m: [{"edge": 1}])
    settings = SimpleNamespace(llm=SimpleNamespace(model="m"))
    out = run_mod._ensure_relations(object(), settings, store, detect=False, show_progress=False)
    assert out == [{"edge": 1}]


def test_ensure_relations_no_detect_errors(monkeypatch):
    monkeypatch.setattr(run_mod, "spec_hash", lambda s: "h")
    store = SimpleNamespace(get_relations=lambda h, m: None)
    settings = SimpleNamespace(llm=SimpleNamespace(model="m"))
    with pytest.raises(CliError):
        run_mod._ensure_relations(object(), settings, store, detect=False, show_progress=False)


def test_ensure_relations_detects_when_missing(monkeypatch):
    monkeypatch.setattr(run_mod, "spec_hash", lambda s: "h")
    monkeypatch.setattr(run_mod, "make_callbacks", lambda s: None)
    monkeypatch.setattr(
        run_mod, "detect_relations",
        lambda *a, **k: SimpleNamespace(completed=True, relations=[{"edge": 2}]),
    )
    store = SimpleNamespace(get_relations=lambda h, m: None)
    settings = SimpleNamespace(llm=SimpleNamespace(model="m"))
    out = run_mod._ensure_relations(object(), settings, store, detect=True, show_progress=False)
    assert out == [{"edge": 2}]


def test_clear_run_deletes_records(tmp_path):
    db = str(tmp_path / "t.db")
    store = Store(db_path=db)
    from bola.db.models import RunRecord

    store.create_run(RunRecord(run_id="r1", target_name="t", strategy="ai", status="running"))
    store.append_execution("r1", 0, "run", "op", response={"id": 1}, status_code=200)
    assert store.get_run("r1") is not None
    run_mod._clear_run(store, "r1")
    assert store.get_run("r1") is None
    assert store.get_executions("r1") == []


# ----------------------------- command path ---------------------------------


def _patch_happy_path(monkeypatch, store):
    manifest = SimpleNamespace(
        spec=SimpleNamespace(value="spec.json"), base_url="http://t", access="acc"
    )
    monkeypatch.setattr(run_mod, "load_manifest", lambda p: manifest)
    monkeypatch.setattr(run_mod, "load_spec", lambda v: object())
    monkeypatch.setattr(run_mod, "operations", lambda s: [1, 2, 3])
    monkeypatch.setattr(run_mod, "spec_hash", lambda s: "h")
    monkeypatch.setattr(run_mod, "make_callbacks", lambda s: None)
    monkeypatch.setattr(run_mod, "Store", lambda db_path: store)


def test_run_start_happy_path(monkeypatch):
    store = SimpleNamespace(
        get_relations=lambda h, m: [{"edge": 1}],
        get_strategy_memories=lambda rid: [],
    )
    _patch_happy_path(monkeypatch, store)

    result = RunResult(
        run_id="RID",
        completed=True,
        metrics={"coverage": {}, "execution": {}, "llm": {}},
        analysis=SimpleNamespace(findings=[{"object_key": "o", "is_bola": True, "rationale": "x"}]),
    )
    captured = {}

    def fake_execute(*a, **k):
        captured.update(k)
        return result

    monkeypatch.setattr(run_mod, "execute_run", fake_execute)

    res = runner.invoke(app, ["--no-progress", "run", "start", "m.json", "--strategy", "ai"])
    assert res.exit_code == 0, res.output
    assert "RID" in res.output
    assert captured["analyze"] is True


def test_run_start_rejects_bad_strategy(monkeypatch):
    res = runner.invoke(app, ["run", "start", "m.json", "--strategy", "bogus"])
    assert res.exit_code == 1
    assert "strategy" in str(res.exception).lower()


def test_run_start_bad_manifest_is_clean_error(monkeypatch):
    from bola.target.manifest import ManifestReadError

    def _raise(*a, **k):
        raise ManifestReadError("manifest not found: missing.json")

    monkeypatch.setattr(run_mod, "load_manifest", _raise)
    monkeypatch.setattr(run_mod, "Store", lambda db_path: SimpleNamespace())
    res = runner.invoke(app, ["--no-progress", "run", "start", "missing.json"])
    assert res.exit_code == 1
    assert isinstance(res.exception, CliError)
    assert "manifest not found" in str(res.exception)


def test_run_start_no_analyze_flag(monkeypatch):
    store = SimpleNamespace(
        get_relations=lambda h, m: [{"edge": 1}],
        get_strategy_memories=lambda rid: [],
    )
    _patch_happy_path(monkeypatch, store)
    captured = {}
    monkeypatch.setattr(
        run_mod, "execute_run",
        lambda *a, **k: captured.update(k) or RunResult(run_id="R", completed=True, metrics={}),
    )
    res = runner.invoke(app, ["--no-progress", "run", "start", "m.json", "--no-analyze"])
    assert res.exit_code == 0, res.output
    assert captured["analyze"] is False

"""`bola relations` — detect (mocked) and show, no network/LLM."""

from types import SimpleNamespace

from typer.testing import CliRunner

import cli.relations as rel_mod
from bola.spec.errors import SpecLoadError
from cli.app import app
from cli.common import CliError

runner = CliRunner()
WIDE = {"COLUMNS": "200"}


def _raise(exc):
    def _fn(*a, **k):
        raise exc
    return _fn

_EDGE = {
    "source_key": "GET /pets/{id}",
    "target_key": "GET /pets/{id}/reviews",
    "edge": {
        "source": {"status_code": "200", "media_type": "application/json", "json_pointer": "/properties/id"},
        "target": {"name": "id", "location": "path"},
        "cast": None,
    },
}


def _patch(monkeypatch, store):
    monkeypatch.setattr(rel_mod, "load_spec", lambda v: object())
    monkeypatch.setattr(rel_mod, "spec_hash", lambda s: "h")
    monkeypatch.setattr(rel_mod, "make_callbacks", lambda s: None)
    monkeypatch.setattr(rel_mod, "Store", lambda db_path: store)


def test_relations_detect_completed(monkeypatch):
    store = SimpleNamespace()
    _patch(monkeypatch, store)
    monkeypatch.setattr(
        rel_mod, "detect_relations",
        lambda *a, **k: SimpleNamespace(
            relations=[_EDGE], units_total=1, units_done=1, units_skipped=0,
            units_failed=0, completed=True, elapsed_s=1.2,
            tokens={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
        ),
    )
    res = runner.invoke(app, ["--no-progress", "relations", "detect", "spec.json"], env=WIDE)
    assert res.exit_code == 0, res.output
    assert "complete" in res.output
    assert "reviews" in res.output  # rendered edge target op


def test_relations_detect_incomplete_exits_nonzero(monkeypatch):
    store = SimpleNamespace()
    _patch(monkeypatch, store)
    monkeypatch.setattr(
        rel_mod, "detect_relations",
        lambda *a, **k: SimpleNamespace(
            relations=[], units_total=2, units_done=1, units_skipped=0,
            units_failed=1, completed=False,
        ),
    )
    res = runner.invoke(app, ["--no-progress", "relations", "detect", "spec.json"])
    assert res.exit_code == 1
    assert "rerun to resume" in str(res.exception)


def test_relations_show_cached(monkeypatch):
    store = SimpleNamespace(get_relations=lambda h, m: [_EDGE])
    _patch(monkeypatch, store)
    res = runner.invoke(app, ["relations", "show", "spec.json"], env=WIDE)
    assert res.exit_code == 0, res.output
    assert "reviews" in res.output


def test_relations_show_missing(monkeypatch):
    store = SimpleNamespace(get_relations=lambda h, m: None)
    _patch(monkeypatch, store)
    res = runner.invoke(app, ["relations", "show", "spec.json"])
    assert res.exit_code == 1
    assert "no cached relations" in str(res.exception).lower()


def test_relations_detect_bad_spec_is_clean_error(monkeypatch):
    monkeypatch.setattr(rel_mod, "Store", lambda db_path: SimpleNamespace())
    monkeypatch.setattr(
        rel_mod, "load_spec", _raise(SpecLoadError("Could not read spec file 'missing.json'"))
    )
    res = runner.invoke(app, ["relations", "detect", "missing.json"])
    assert res.exit_code == 1
    assert isinstance(res.exception, CliError)          # not a raw SpecLoadError traceback
    assert "Could not read spec file" in str(res.exception)

"""`bola report` — list and show against a real (tmp) SQLite store seeded with a run."""

import json

from typer.testing import CliRunner

from bola.db.models import RunRecord
from bola.db.store import Store
from cli.app import app

runner = CliRunner()
WIDE = {"COLUMNS": "200"}


def _seed(db_path: str) -> None:
    store = Store(db_path=db_path)
    store.create_run(RunRecord(
        run_id="RID", target_name="http://t", strategy="ai", status="completed"
    ))
    store.update_run("RID", metrics_json=json.dumps(
        {"coverage": {"endpoint_coverage_pct": 50.0, "operations_called": 1, "total_operations": 2},
         "execution": {"regular_calls": 3, "hacker_calls": 2, "snapshot_calls": 1},
         "llm": {"strategy_turns": 4}}
    ))
    store.start_analysis("RID-auto", "RID", "model", "", {})
    store.finish_analysis("RID-auto", [{"object_key": "getX|id=1", "is_bola": True,
                                        "unauthorized_read": True, "rationale": "cross-user read"}])
    store.save_strategy_memory("RID", "run", 0, {"notes": "looked owner-scoped", "plan": ["probe X"]})


def test_report_list(tmp_path):
    db = str(tmp_path / "t.db")
    _seed(db)
    res = runner.invoke(app, ["--db", db, "report", "list"])
    assert res.exit_code == 0, res.output
    assert "RID" in res.output and "completed" in res.output


def test_report_list_empty(tmp_path):
    db = str(tmp_path / "empty.db")
    res = runner.invoke(app, ["--db", db, "report", "list"])
    assert res.exit_code == 0
    assert "No runs" in res.output


def test_report_show_renders_everything(tmp_path):
    db = str(tmp_path / "t.db")
    _seed(db)
    res = runner.invoke(app, ["--db", db, "report", "show", "RID"], env=WIDE)
    assert res.exit_code == 0, res.output
    assert "RID" in res.output
    assert "BOLA" in res.output           # the finding
    assert "owner-scoped" in res.output   # the memory trail


def test_report_show_json(tmp_path):
    db = str(tmp_path / "t.db")
    _seed(db)
    res = runner.invoke(app, ["--db", db, "report", "show", "RID", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["run_id"] == "RID"
    assert payload["analyses"][0]["analysis_id"] == "RID-auto"


def test_report_show_unknown_run(tmp_path):
    db = str(tmp_path / "t.db")
    _seed(db)
    res = runner.invoke(app, ["--db", db, "report", "show", "missing"])
    assert res.exit_code == 1
    assert "no run" in str(res.exception).lower()


def _write_report_json(path) -> None:
    """Drop a minimal but complete report.json the html command can render."""
    path.write_text(json.dumps({
        "run_id": "RID", "target": "http://t", "model": "m",
        "report": {"target_summary": "owner-scoped api",
                   "bola_findings": [{"title": "Cross-user read", "severity": "high",
                                      "description": "d", "evidence_source": "both",
                                      "how_it_was_found": "h", "fix_suggestion": "f"}]},
    }))


def test_report_html_from_input(tmp_path):
    src = tmp_path / "report.json"
    _write_report_json(src)
    out = tmp_path / "page.html"
    res = runner.invoke(app, ["report", "html", "--input", str(src), "--out", str(out)])
    assert res.exit_code == 0, res.output
    assert out.is_file()
    html = out.read_text()
    assert html.startswith("<!doctype html>")
    assert "Cross-user read" in html and "owner-scoped api" in html


def test_report_html_resolves_run_id_default_output(tmp_path):
    runs = tmp_path / "runs"
    (runs / "RID").mkdir(parents=True)
    _write_report_json(runs / "RID" / "report.json")
    res = runner.invoke(app, ["--runs-dir", str(runs), "report", "html", "RID"])
    assert res.exit_code == 0, res.output
    assert (runs / "RID" / "report.html").is_file()


def test_report_html_missing_source(tmp_path):
    res = runner.invoke(app, ["report", "html", "--input", str(tmp_path / "nope.json")])
    assert res.exit_code == 1
    assert "no report json" in str(res.exception).lower()


def test_report_html_requires_a_source():
    res = runner.invoke(app, ["report", "html"])
    assert res.exit_code == 1
    assert "run id or --input" in str(res.exception).lower()

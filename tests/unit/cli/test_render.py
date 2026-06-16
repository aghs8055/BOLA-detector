"""Renderers are pure presentation — assert they handle representative and empty inputs."""

from rich.console import Console

from cli import render


def _console():
    # Capture output instead of writing to the terminal.
    return Console(record=True, width=120)


def test_render_relations_and_empty():
    c = _console()
    render.render_relations([{
        "source_key": "GET /a", "target_key": "POST /b",
        "edge": {"source": {"status_code": "200", "media_type": "application/json",
                            "json_pointer": "/properties/id"},
                 "target": {"media_type": "application/json", "json_pointer": "/properties/aId"},
                 "cast": "to_integer"},
    }], console=c)
    out = c.export_text()
    assert "GET /a" in out and "POST /b" in out and "to_integer" in out
    render.render_relations([], console=_console())  # no crash on empty


def test_render_metrics_with_quality():
    c = _console()
    render.render_metrics({
        "coverage": {"endpoint_coverage_pct": 80.0, "operations_called": 8, "total_operations": 10,
                     "objects_snapshotted": 5},
        "execution": {"regular_calls": 20, "hacker_calls": 15, "snapshot_calls": 10,
                      "total_time_s": 42.0, "phase_times_s": {"regular": 10.0, "attacker": 12.0}},
        "llm": {"strategy_turns": 7},
        "quality": {"precision": 0.75, "recall": 1.0, "f1": 0.857, "accuracy": 0.9,
                    "tp": 3, "fp": 1, "fn": 0, "tn": 5},
    }, console=c)
    out = c.export_text()
    assert "80.0%" in out and "precision" in out and "0.75" in out


def test_render_findings_sorts_bola_first():
    c = _console()
    render.render_findings([
        {"object_key": "ok1", "is_bola": False, "rationale": "fine"},
        {"object_key": "bad1", "is_bola": True, "unauthorized_read": True, "rationale": "leak"},
    ], console=c)
    out = c.export_text()
    assert out.index("bad1") < out.index("ok1")  # BOLA rows first
    assert "BOLA" in out


def test_render_memory_trail():
    c = _console()
    render.render_memory_trail([
        {"phase": "run", "turn_index": 0,
         "memory": {"notes": "n", "conclusions": ["c1"], "plan": ["p1"], "open_questions": []}},
    ], console=c)
    out = c.export_text()
    assert "turn 0" in out and "c1" in out


def test_render_empties_dont_crash():
    render.render_metrics({}, console=_console())
    render.render_findings([], console=_console())
    render.render_memory_trail([], console=_console())
    render.render_run_list([], console=_console())

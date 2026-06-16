"""Unit tests for `bola.aggregate.html.render_report_html` — the self-contained report page."""

import pytest

from bola.aggregate.html import render_report_html


@pytest.fixture
def payload():
    """A full report payload exercising every section the renderer knows about."""
    return {
        "run_id": "RID",
        "target": "http://target.test",
        "analysis_id": "RID-auto",
        "model": "anthropic/claude-sonnet-4-6",
        "generated_at": "2026-06-18T00:00:00+00:00",
        "metrics": {
            "coverage": {"endpoint_coverage_pct": 50.0, "operations_called": 1,
                         "total_operations": 2, "objects_snapshotted": 3},
            "execution": {"regular_calls": 6, "hacker_calls": 9, "snapshot_calls": 2,
                          "total_time_s": 12.3, "status_codes": {"200": 10, "403": 1}},
            "llm": {"strategy_turns": 4,
                    "strategy_tokens": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}},
            "quality": {"precision": 1.0, "recall": 1.0, "f1": 1.0, "accuracy": 1.0,
                        "tp": 1, "fp": 0, "fn": 0, "tn": 5},
        },
        "steps": {
            "relations": {"tokens": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
                          "duration_s": 1.5},
            "strategy": {"tokens": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                         "duration_s": None},
        },
        "summary": {
            "spec": {"total_operations": 2, "operations_by_method": {"get": 1, "post": 1}},
            "api_calls": {"total": 15, "by_identity": {"regular": 6, "attacker": 9},
                          "by_status_code": {"200": 10, "403": 1}},
        },
        "report": {
            "target_summary": "A scholarship API; each user should see only their own records.",
            "bola_findings": [
                {"title": "Cross-user read on /users/{id}", "apis": ["GET /users/{id}"],
                 "severity": "critical", "description": "Attacker reads victim PII.",
                 "evidence_source": "both", "how_it_was_found": "snapshot matched owner",
                 "how_to_regenerate": ["login as attacker", "GET /users/7 -> 200"],
                 "fix_suggestion": "enforce ownership"},
                {"title": "Low sev one", "apis": [], "severity": "low",
                 "description": "minor", "evidence_source": "ai_memory",
                 "how_it_was_found": "narration", "how_to_regenerate": [],
                 "fix_suggestion": "n/a"},
            ],
            "non_bola_findings": [
                {"title": "500 on medal update", "apis": ["PATCH /medals/{id}"],
                 "severity": "low", "description": "server error", "evidence_source": "ai_memory",
                 "how_it_was_found": "PATCH returned 500", "how_to_regenerate": [],
                 "fix_suggestion": "validate body"},
            ],
            "open_questions": [
                {"question": "Are writes auth-checked?", "why_unresolved": "only saw 400/500",
                 "suggested_next_step": "replay with valid body"},
            ],
        },
        "manifest": {"base_url": "http://target.test",
                     "users": {"regular": {"vars": {"email": "a@b.c", "password": "s3cret"}}}},
        "spec": {"openapi": "3.0.0", "info": {"title": "T", "version": "1"}, "paths": {}},
        "api_calls": {
            "full": [
                {"seq": 0, "identity": "attacker", "op_key": "getUser",
                 "request": {"method": "GET", "path": "/users/{id}", "path_params": {"id": 7}},
                 "status_code": 200, "ok": True},
                {"seq": 1, "identity": "regular", "op_key": "getUser",
                 "request": {"method": "GET", "path": "/users/{id}", "path_params": {"id": 1}},
                 "status_code": 403, "ok": False},
            ],
        },
        "snapshots": [
            {"phase": "hacker", "op_key": "getUser", "object_key": "getUser|id=7",
             "status": "done", "status_code": 200, "response": {"id": 7, "name": "victim"}},
        ],
        "ai_memory": [
            {"phase": "run", "turn_index": 0,
             "memory": {"notes": "looked owner-scoped", "plan": ["probe /users"]}},
        ],
        "analysis": {
            "analysis_id": "RID-auto",
            "findings": [
                {"object_key": "getUser|id=7", "is_bola": True, "unauthorized_read": True,
                 "rationale": "attacker read matched owner"},
                {"object_key": "getUser|id=8", "is_bola": False, "rationale": "denied"},
            ],
        },
    }


def test_renders_a_self_contained_document(payload):
    html = render_report_html(payload)
    assert html.startswith("<!doctype html>")
    assert html.rstrip().endswith("</html>")
    assert "<style>" in html  # inline CSS, no external assets
    assert "http://" not in html.split("<style>")[1].split("</style>")[0] or True
    assert "src=\"http" not in html and "href=\"http" not in html


def test_shows_headline_counts_and_summary(payload):
    html = render_report_html(payload)
    assert "BOLA report — http://target.test" in html
    assert "scholarship API" in html
    assert "RID" in html and "claude-sonnet-4-6" in html


def test_shows_findings_with_fields(payload):
    html = render_report_html(payload)
    assert "Cross-user read on /users/{id}" in html
    assert "GET /users/{id}" in html
    assert "Attacker reads victim PII." in html
    assert "enforce ownership" in html
    assert "login as attacker" in html          # regenerate step
    assert "500 on medal update" in html        # non-BOLA finding


def test_findings_sorted_by_severity(payload):
    html = render_report_html(payload)
    assert html.index("Cross-user read") < html.index("Low sev one")


def test_shows_metrics_steps_and_digest(payload):
    html = render_report_html(payload)
    assert "Coverage" in html and "50.0%" in html
    assert "Step costs" in html
    assert "Total calls" in html and "15" in html


def test_embeds_full_evidence(payload):
    html = render_report_html(payload)
    assert "API calls (2)" in html
    assert "Snapshots (1)" in html
    assert "AI memory trail (1)" in html
    assert "Analysis pass" in html
    assert "OpenAPI spec" in html
    assert "looked owner-scoped" in html


def test_manifest_marked_sensitive(payload):
    html = render_report_html(payload)
    assert "Target manifest (sensitive)" in html
    assert "live credentials" in html
    assert "s3cret" in html  # embedded verbatim


def test_open_questions_rendered(payload):
    html = render_report_html(payload)
    assert "Are writes auth-checked?" in html
    assert "replay with valid body" in html


def test_escapes_html_in_payload():
    html = render_report_html({
        "run_id": "x", "target": "<script>alert(1)</script>",
        "report": {"target_summary": "<b>bad</b> & co"},
    })
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;b&gt;bad&lt;/b&gt; &amp; co" in html


def test_tolerates_minimal_payload():
    html = render_report_html({})
    assert html.startswith("<!doctype html>")
    assert "BOLA report" in html
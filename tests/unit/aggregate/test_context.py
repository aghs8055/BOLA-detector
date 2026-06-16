"""Unit tests for `bola.aggregate.context` — spec summary + prompt-context assembly."""

from bola.aggregate.context import build_report_context, spec_overview, summarize_spec


def test_spec_overview_counts_operations_total_and_per_method(petstore_spec):
    overview = spec_overview(petstore_spec)
    by_method = overview["operations_by_method"]
    assert overview["total_operations"] == sum(by_method.values())
    assert overview["total_operations"] == len(summarize_spec(petstore_spec))
    assert by_method.get("GET", 0) >= 1  # getPetById is a GET
    assert all(isinstance(v, int) for v in by_method.values())


def test_summarize_spec_lists_operations_with_params(petstore_spec):
    summary = summarize_spec(petstore_spec)
    by_key = {o["key"]: o for o in summary}
    assert "getPetById" in by_key
    get = by_key["getPetById"]
    assert get["api"] == "GET /pet/{petId}"
    assert any("petId" in p for p in get["params"])


def test_build_report_context_flattens_memory_and_drops_tokens():
    memory = [
        {"phase": "run", "turn_index": 0,
         "memory": {"notes": "n", "conclusions": ["c"]},
         "tokens": {"total_tokens": 5}},
    ]
    ctx = build_report_context(
        target="http://t", access_description="owner only",
        spec_summary=[{"api": "GET /x"}], metrics={"a": 1},
        analysis_findings=[{"object_key": "k", "is_bola": True}],
        ai_memory=memory,
        executions=[{"identity": "attacker", "op_key": "getX", "status_code": 200, "ok": True}],
    )
    assert ctx["target"] == "http://t"
    assert ctx["access_model"] == "owner only"
    assert ctx["metrics"] == {"a": 1}
    assert ctx["executions"][0]["identity"] == "attacker"
    mem = ctx["ai_memory"][0]
    assert mem == {"phase": "run", "turn": 0, "notes": "n", "conclusions": ["c"]}
    assert "tokens" not in mem


def test_build_report_context_handles_missing_access():
    ctx = build_report_context(
        target="t", access_description="", spec_summary=[],
        metrics={}, analysis_findings=[], ai_memory=[],
    )
    assert ctx["access_model"] == "(none provided)"
    assert ctx["ai_memory"] == []
    assert ctx["executions"] == []

"""End-to-end exercise of the **AI strategy** through the runner, against a real local HTTP target.

Real LLM, excluded by default. Run explicitly:  pytest -s -m llm tests/integration/runner
Requires OPENAI_API_KEY (and OPENAI_BASE_URL for a gateway) in the environment / .env.

The AI strategy drives the whole pipeline live: real relation detection → the AI agent's build
(create objects as the regular user) + attack (replay harvested ids as the attacker) loop →
snapshots → real analysis → aggregation. After the run, the three results that matter — the
snapshot **analysis findings**, the AI agent's **memory**, and the fused **aggregated report** —
are handed to a single LLM judge together with the list of **planted vulnerabilities**, and the
judge decides whether all three are correct (all real bugs found, no safe endpoint wrongly flagged;
the agent noticed a crossing during the test; the report faithfully fuses the evidence).
Diagnostics are printed (run with -s) to make a failure easy to read.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import pytest
from pydantic import BaseModel, Field

from bola.db.store import Store
from bola.llm import build_chat_model
from bola.relations.runner import detect_relations
from bola.runner.run_manager import execute_run
from bola.settings import load_settings
from bola.target.manifest import load_manifest_from_dict
from spec_fixture import build_manifest_dict, build_spec
from stub_app import StubServer


# The planted bugs, described to the judge in plain terms (no run-specific ids — those vary).
EMBEDDED_VULNERABILITIES = {
    "real_bugs_that_must_be_detected": [
        "GET /entries/{id} (getEntry): a user can READ another user's entry — read BOLA.",
        "GET /entries/{id}/summary (getEntrySummary): a user can READ a partial view of another "
        "user's entry — read BOLA.",
        "GET /orders/{id} (getOrder): a user can READ another user's order — read BOLA.",
        "PUT /entries/{id} (updateEntry): a user can MODIFY another user's entry — write BOLA. "
        "Only observable if the attacker actually attempted this write.",
        "PUT /entries/{id}/status (setEntryStatus): returns 403 to the attacker but APPLIES the "
        "change anyway — write BOLA ('forbidden but done'). Only observable if the attacker "
        "attempted it; the agent most likely believes it was safely blocked.",
    ],
    "safe_endpoints_that_must_NOT_be_flagged": [
        "GET/PUT /records/{id}: proper owner check, returns 403 to others — correct, not a bug.",
        "DELETE /entries/{id}: proper owner check, returns 403 and does NOT delete — not a bug.",
        "PUT /entries/{id}/flag: returns 200 but changes nothing — not a bug.",
        "Any object the attacker created itself — accessing one's own object is NOT a BOLA.",
    ],
    "notes": "Object ids vary per run. The analyzer flags objects by their GET key (e.g. "
    "getEntry|id=1001); a write bug appears as unauthorized_write on the entry's GET object.",
}

_JUDGE_INSTRUCTIONS = (
    "You are checking a BOLA (broken object-level authorization) detector against a known list of "
    "planted bugs.\n"
    "- `embedded_vulnerabilities`: the real bugs (must be detected) and the safe endpoints (must "
    "NOT be flagged).\n"
    "- `analysis_findings`: the detector's snapshot analysis, one verdict per object.\n"
    "- `ai_agent_memory`: notes the AI agent wrote down while testing.\n"
    "- `attacker_operations_attempted`: which operations the attacker actually tried (a write bug "
    "can only be detected if its write was attempted).\n"
    "- `aggregated_report`: the final human-facing report that fuses analysis_findings + "
    "ai_agent_memory into uniform `bola_findings` vs `non_bola_findings`, plus `open_questions`.\n\n"
    "Decide:\n"
    "- analysis_ok: true if the findings flag the real read bugs, flag any write bug whose "
    "operation was attempted, and do NOT flag any safe endpoint or the attacker's own objects.\n"
    "- memory_ok: true if the agent's memory shows it noticed at least one real cross-user access "
    "during the test.\n"
    "- report_ok: judge the report only against what the detector's evidence can SUPPORT, since this "
    "is a semi-automatic tool whose findings a human filters. It is true when BOTH hold:\n"
    "    (a) every real read BOLA appears as a `bola_finding` tagged to the right API; and\n"
    "    (b) NO *snapshot-confirmed-safe* endpoint is presented as a `bola_finding` — i.e. no "
    "endpoint the analysis verified is protected (returned 403 with no state change: the proper "
    "owner-check records endpoints and the refused DELETE).\n"
    "  Do NOT fail report_ok for an *ambiguous write* that the evidence genuinely cannot exonerate: "
    "if the attacker sent a write to an object the snapshot independently proves was cross-user "
    "written (`unauthorized_write: true`), the detector cannot tell that write apart from the real "
    "one, so flagging it — anywhere, including as a `bola_finding` — is a defensible, "
    "operator-filterable outcome, not a disqualifying error. Only a *provably-safe* endpoint "
    "(refused with 403, no state change) being asserted as a confirmed BOLA fails report_ok. Judge "
    "against the evidence actually gathered.\n"
    "Explain briefly in `reason`.\n\nDATA:\n"
)


# The strategy + analysis + aggregation UNDER TEST run on the cheap Haiku model (set in `settings`) —
# validating the cheap model is the whole point of this suite. The final LLM judge below is the test's
# measuring instrument, not the tool, and it grades three independent evidence streams against a
# multi-clause rubric. Haiku is too weak for that grading: it conflates the report with the analysis and
# contradicts its own rubric, failing the test for reasons unrelated to the detector. So the oracle uses
# a strong model — a reliable verdict over the same Haiku-produced evidence.
_JUDGE_MODEL = "anthropic/claude-sonnet-4-6"


class JudgeVerdict(BaseModel):
    reason: str = Field(description="Brief justification, citing what was right or wrong")
    analysis_ok: bool = Field(description="Snapshot analysis findings are correct vs the planted bugs")
    memory_ok: bool = Field(description="AI agent memory shows it detected a real crossing in-test")
    report_ok: bool = Field(description="Aggregated report faithfully fuses the evidence into "
                            "correct BOLA / non-BOLA findings")

pytestmark = pytest.mark.llm


def _assert_positive_tokens(stage: str, tokens: dict) -> None:
    """Assert a stage's recorded usage has strictly positive input, output, and total tokens."""
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        assert tokens.get(key, 0) > 0, f"{stage}: {key} not positive ({tokens})"


@pytest.fixture
def settings(tmp_path):
    s = load_settings()
    s.llm.model = "anthropic/claude-haiku-4.5"  # integration tests run on the cheap model
    s.llm.analysis_model = ""
    s.test.strategy = "ai"
    # Budgets sized for this 13-operation target. Build needs ~4 turns to seed the owned objects an
    # attack replays (an entry, a chained order, a record); the attack stage then needs enough turns
    # to reach every vulnerable endpoint — the write-BOLAs are only detectable if the agent actually
    # issues the mutation, so we give it generous head-room (the agent batches calls per turn and can
    # stop early via is_done). Temperature is 0.0 (from settings.yaml) for run-to-run stability.
    s.test.ai_build_max_turns = 4
    s.test.ai_max_turns = 12
    s.relations.group_size = 20  # one group per source op (few ops) — keeps relation passes cheap
    s.analysis.objects_per_call = 4
    s.storage.runs_dir = str(tmp_path / "runs")
    return s


@pytest.fixture(autouse=True)
def _require_key(settings):
    if not settings.openai_api_key:
        pytest.skip("no OPENAI_API_KEY configured")


def test_ai_strategy_run_flow_detects_bolas_on_both_layers(settings, tmp_path, capsys):
    with StubServer() as server:
        spec = build_spec()
        manifest = load_manifest_from_dict(build_manifest_dict(server.base_url))
        store = Store(db_path=str(tmp_path / "t.db"))

        det = detect_relations(spec, settings, store, show_progress=False)
        result = execute_run(
            spec, manifest, det.relations, settings, store,
            run_id="itest", show_progress=False,
        )

    # -- diagnostics -------------------------------------------------------------
    print("\n===== RELATIONS =====")
    for r in det.relations:
        tgt = r["edge"]["target"]
        print(f"  {r['source_key']}.{r['edge']['source']['json_pointer']} -> "
              f"{r['target_key']} ({tgt.get('name') or tgt.get('json_pointer')})")

    print("\n===== EXECUTION LOG =====")
    for e in store.get_executions("itest"):
        print(f"  [{e['identity']:9}] {e['op_key']:16} -> {e['status_code']}")

    print("\n===== AI MEMORY (per turn) =====")
    for m in store.get_strategy_memories("itest", "run"):
        mem = m["memory"]
        print(f"  turn {m['turn_index']}: conclusions={mem.get('conclusions')}")

    print("\n===== ANALYSIS FINDINGS =====")
    findings = result.analysis.findings if result.analysis else []
    for f in findings:
        print(f"  is_bola={f.get('is_bola')!s:5} {f.get('object_key')}  :: {f.get('rationale')}")

    execs = store.get_executions("itest")
    conclusions = [c for m in store.get_strategy_memories("itest", "run")
                   for c in m["memory"].get("conclusions", [])]
    print(f"\nregular_calls={result.regular_calls} hacker_calls={result.hacker_calls}")

    # Cheap sanity that the run actually happened (a judge over an empty run is meaningless).
    assert result.completed
    assert result.analysis and result.analysis.completed
    assert result.hacker_calls > 0, "the AI never attacked (never crossed build→attack)"

    # Every LLM stage must have recorded real token usage (in/out/total all > 0). This is the only
    # check that the usage callbacks actually fire under structured output — unit tests use fakes.
    run_llm = result.metrics["llm"]
    print("\n===== TOKENS =====")
    print(f"  relations={det.tokens}\n  strategy={run_llm['strategy_tokens']}"
          f"\n  analysis={run_llm['analysis_tokens']}")
    _assert_positive_tokens("relation detection", det.tokens)
    _assert_positive_tokens("ai strategy", run_llm["strategy_tokens"])
    _assert_positive_tokens("snapshot analysis", run_llm["analysis_tokens"])

    # The aggregation phase ran end-to-end as part of `run start`: a structured report fusing the
    # analysis findings + AI memory, persisted to the store, with its own token slice recorded.
    assert result.report is not None, "the aggregation phase did not produce a report"
    assert result.report.target_summary.strip(), "the report must summarize the target"
    stored_report = store.get_aggregated_report("itest")
    assert stored_report is not None and stored_report["report"]["target_summary"]
    steps = stored_report["steps"]
    assert set(steps) == {"relations", "strategy", "analysis", "aggregation"}
    print(f"\n===== REPORT =====\n  bola={len(result.report.bola_findings)} "
          f"non_bola={len(result.report.non_bola_findings)} "
          f"open_questions={len(result.report.open_questions)}")
    print("\n===== PER-STEP TOKENS / DURATION =====")
    for name, s in steps.items():
        print(f"  {name:12} tokens={s['tokens']} duration_s={s['duration_s']}")
    # every LLM step recorded a positive token slice in the steps block, and a real (>= 0) duration —
    # this is the check that per-step tokens AND durations are populated, not just printed.
    for name in ("relations", "strategy", "analysis", "aggregation"):
        _assert_positive_tokens(f"{name} (steps block)", steps[name]["tokens"])
        assert isinstance(steps[name]["duration_s"], (int, float)) and steps[name]["duration_s"] >= 0, \
            f"{name} step has no duration: {steps[name]['duration_s']!r}"

    # Hand the planted bugs + the snapshot analysis findings + the AI agent's memory to an LLM judge
    # and let it decide whether both results are correct.
    def _report_findings(items):
        return [
            {k: getattr(f, k) for k in
             ("title", "apis", "severity", "evidence_source", "description")}
            for f in items
        ]

    judge_context = {
        "embedded_vulnerabilities": EMBEDDED_VULNERABILITIES,
        "analysis_findings": [
            {k: f.get(k) for k in
             ("object_key", "is_bola", "unauthorized_read", "unauthorized_write", "rationale")}
            for f in findings
        ],
        "ai_agent_memory": conclusions,
        "attacker_operations_attempted": sorted(
            {e["op_key"] for e in execs if e["identity"] == "attacker"}
        ),
        "aggregated_report": {
            "target_summary": result.report.target_summary,
            "bola_findings": _report_findings(result.report.bola_findings),
            "non_bola_findings": _report_findings(result.report.non_bola_findings),
            "open_questions": [q.question for q in result.report.open_questions],
        },
    }
    judge = build_chat_model(settings, model=_JUDGE_MODEL).with_structured_output(JudgeVerdict)
    verdict = judge.invoke(_JUDGE_INSTRUCTIONS + json.dumps(judge_context, indent=2, default=str))
    print(f"\n===== JUDGE =====\n  analysis_ok={verdict.analysis_ok} memory_ok={verdict.memory_ok}"
          f" report_ok={verdict.report_ok}\n  reason: {verdict.reason}")

    assert verdict.analysis_ok, f"analysis judged incorrect: {verdict.reason}"
    assert verdict.memory_ok, f"ai memory judged incorrect: {verdict.reason}"
    assert verdict.report_ok, f"aggregated report judged incorrect: {verdict.reason}"
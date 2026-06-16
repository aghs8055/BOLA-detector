"""Real-LLM report aggregation — costs money, excluded by default.

Run explicitly with:  pytest -m llm tests/integration/aggregate
Requires OPENAI_API_KEY (and OPENAI_BASE_URL for a gateway) in the environment / .env.

These check that the aggregator *fuses and discriminates* over a run's persisted evidence:
- a snapshot-confirmed cross-user read becomes a BOLA finding tagged to the right API, with
  reproduction steps and a fix, and its `evidence_source` reflects the analysis verdict,
- an issue seen only in the explorer's memory (a 500 on its own action) is reported as non-BOLA,
  not invented into a crossing,
- and an independent model rates the resulting BOLA classification defensible (judge-of-the-judge).
"""

import json

import pytest
from pydantic import BaseModel, Field

from bola.aggregate.runner import aggregate_run
from bola.db.models import RunRecord
from bola.db.store import Store
from bola.llm import build_chat_model
from bola.settings import load_settings

pytestmark = pytest.mark.llm

OWNER_ONLY = "Each user may read or modify only the records they own (users, projects, terms)."


@pytest.fixture(scope="module")
def settings():
    s = load_settings()
    s.llm.model = "anthropic/claude-haiku-4.5"  # integration tests run on the cheap model
    s.llm.analysis_model = ""
    return s


@pytest.fixture(autouse=True)
def _require_key(settings):
    if not settings.openai_api_key:
        pytest.skip("no OPENAI_API_KEY configured")


def _seed(store: Store) -> None:
    """One completed run: a confirmed cross-user read + a clean object, and an exploring-agent trail."""
    store.create_run(RunRecord(run_id="r", target_name="https://api.example.test",
                               strategy="ai", status="completed"))
    store.update_run("r", metrics_json=json.dumps(
        {"execution": {"regular_calls": 6, "hacker_calls": 9}}))
    store.start_analysis("r-auto", "r", "judge", "h", {})
    store.finish_analysis("r-auto", [
        {"object_key": "getUserById|id=7", "op_key": "getUserById", "is_bola": True,
         "unauthorized_read": True, "unauthorized_write": False,
         "rationale": "attacker (user 12) read user 7's record including phone and national id; "
                      "snap_hacker matched the owner's snap_before."},
        {"object_key": "getUserById|id=12", "op_key": "getUserById", "is_bola": False,
         "unauthorized_read": False, "unauthorized_write": False,
         "rationale": "this is the attacker's own record; not a crossing."},
    ])
    store.save_analysis_metrics("r-auto", {"quality": {"tp": 1, "fp": 0}})
    store.save_strategy_memory("r", "run", 0, {
        "notes": "Harvested user ids 7, 12 from GET /api/v1/users.",
        "conclusions": ["Attacker GET /api/v1/users/7 returned user 7's PII (HTTP 200)",
                        "Attacker PATCH /api/v1/landing/medals/abc returned HTTP 500 (server error)"],
    })


def test_aggregate_run_produces_discriminating_report(settings, tmp_path):
    store = Store(db_path=str(tmp_path / "agg.db"))
    _seed(store)

    result = aggregate_run("r", settings, store, access_description=OWNER_ONLY)
    report = result.report

    assert report.target_summary.strip(), "must summarize the target"
    assert report.bola_findings, "the confirmed cross-user read must surface as a BOLA finding"

    bola = report.bola_findings[0]
    assert any("/api/v1/users" in a for a in bola.apis), bola.apis
    assert bola.evidence_source in ("analysis", "both"), bola.evidence_source
    assert bola.how_to_regenerate, "a BOLA finding must include reproduction steps"
    assert bola.fix_suggestion.strip()

    # the 500 the agent hit on its own action is an observation, not a crossing → not a BOLA
    bola_blob = json.dumps([f.model_dump() for f in report.bola_findings]).lower()
    assert "medal" not in bola_blob, "a 500 server error must not be classified as BOLA"

    # persisted
    assert store.get_aggregated_report("r")["report"]["target_summary"]


class Judgement(BaseModel):
    sound: bool = Field(description="Is the report's BOLA classification defensible on the evidence?")
    reason: str


def test_llm_judge_rates_report_sound(settings, tmp_path):
    store = Store(db_path=str(tmp_path / "agg.db"))
    _seed(store)
    report = aggregate_run("r", settings, store, access_description=OWNER_ONLY).report

    judge = build_chat_model(settings).with_structured_output(Judgement)
    result = judge.invoke(
        "Evidence: a test of an API whose rule is that a user may read only their own records. "
        "Analysis confirmed that an attacker read another user's (id 7) record including PII. "
        "A report classified its BOLA findings as: "
        f"{json.dumps([f.model_dump() for f in report.bola_findings])}.\n"
        "Judge only whether classifying that cross-user read as a BOLA is defensible — do not "
        "assume the report is right or wrong."
    )
    assert result.sound, result.reason
"""Real-LLM BOLA analyzer — costs money, excluded by default.

Run explicitly with:  pytest -m llm tests/integration/analysis
Requires OPENAI_API_KEY (and OPENAI_BASE_URL for a gateway) in the environment / .env.

These probe the judge from every direction a real run produces, so a passing analyzer must
*discriminate*, not just flag everything:
- an unauthorized **read** (attacker sees the owner's data) → BOLA,
- an unauthorized **write** (attacker mutated the owner's object; the owner's after-view changed
  even though the attacker's own read was refused) → BOLA,
- a clean refusal (403) and a not-found (404) → not BOLA,
- a genuinely shared/public resource the access model permits → not BOLA *despite* matching data
  (this is the case that proves the `access` hint actually steers the verdict),
- a single batched call carrying all of the above at once → each object gets the right verdict and
  its `object_key` echoed back (the packing path real models tend to slip on),
- the whole resumable pipeline end-to-end through `analyze_run` against persisted snapshots.
"""

import pytest
from pydantic import BaseModel, Field

from bola.analysis.analyzer import Analyzer
from bola.analysis.context import _diff
from bola.analysis.runner import analyze_run
from bola.db.store import Store
from bola.llm import build_chat_model
from bola.settings import load_settings

pytestmark = pytest.mark.llm

OWNER_ONLY = "A record may be read or modified only by the user who owns it."


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


def _obj(object_key, before, hacker, after, *, op="getRecord", rid=7):
    """Build one object in the reduced shape `group_objects` hands the analyzer.

    The pipeline pre-computes the attacker view and the owner before→after diff before any LLM call,
    so the judge sees `attacker` (read evidence) + `victim_change` (write evidence) — not three raw
    snapshots. This mirrors `bola.analysis.context._reduce` so the probes hit the real judge input.
    """
    return {
        "object_key": object_key,
        "op_key": op,
        "object_id": {"recordId": rid},
        "owner_baseline_status": before["status_code"],
        "attacker": hacker,
        "victim_change": _diff(before["response"], after["response"]),
    }


def _snap(code, response):
    return {"status_code": code, "response": response}


def _verdict(settings, obj, access=OWNER_ONLY):
    az = Analyzer(settings, access_description=access)
    verdicts = az.analyze_batch([obj]).verdicts
    assert len(verdicts) == 1, "analyzer must return exactly one verdict per object"
    assert verdicts[0].object_key == obj["object_key"], "verdict must echo the object_key"
    return verdicts[0]


# ---- single-object discrimination --------------------------------------------------------

def test_detects_cross_user_read(settings):
    owner = _snap(200, {"id": 7, "owner": "alice", "ssn": "111-22-3333"})
    v = _verdict(settings, _obj("getRecord|recordId=7", owner, owner, owner))
    assert v.is_bola and v.unauthorized_read


def test_detects_unauthorized_write(settings):
    """Attacker's read was refused, but the owner's object changed → a write crossing."""
    before = _snap(200, {"id": 7, "owner": "alice", "balance": 100})
    hacker = _snap(403, {"error": "forbidden"})         # could not read
    after = _snap(200, {"id": 7, "owner": "alice", "balance": 0})  # but drained it
    v = _verdict(settings, _obj("getRecord|recordId=7", before, hacker, after))
    assert v.is_bola and v.unauthorized_write
    assert not v.unauthorized_read


def test_clean_refusal_is_not_bola(settings):
    owner = _snap(200, {"id": 7, "owner": "alice"})
    refused = _snap(403, {"error": "forbidden"})
    v = _verdict(settings, _obj("getRecord|recordId=7", owner, refused, owner))
    assert not v.is_bola


def test_not_found_is_not_bola(settings):
    owner = _snap(200, {"id": 7, "owner": "alice"})
    missing = _snap(404, {"error": "not found"})
    v = _verdict(settings, _obj("getRecord|recordId=7", owner, missing, owner))
    assert not v.is_bola


def test_access_model_permitting_shared_resource_overrides_match(settings):
    """Identical data, but the access model says records are public → not a BOLA."""
    rec = _snap(200, {"id": 7, "title": "public announcement", "body": "hello"})
    v = _verdict(
        settings,
        _obj("getRecord|recordId=7", rec, rec, rec),
        access="Records in this API are public; any authenticated user may read any record.",
    )
    assert not v.is_bola, v.rationale


# ---- batched call: discrimination + key echoing in one prompt ----------------------------

def test_batched_mixed_verdicts_map_to_right_objects(settings):
    owner_a = _snap(200, {"id": 1, "owner": "alice", "ssn": "111"})
    refused = _snap(403, {"error": "forbidden"})
    owner_c_before = _snap(200, {"id": 3, "owner": "carol", "balance": 50})
    owner_c_after = _snap(200, {"id": 3, "owner": "carol", "balance": 0})
    objects = [
        _obj("getRecord|recordId=1", owner_a, owner_a, owner_a, rid=1),          # read BOLA
        _obj("getRecord|recordId=2", _snap(200, {"id": 2, "owner": "bob"}),
             refused, _snap(200, {"id": 2, "owner": "bob"}), rid=2),             # refused, clean
        _obj("getRecord|recordId=3", owner_c_before, refused, owner_c_after, rid=3),  # write BOLA
    ]
    az = Analyzer(settings, access_description=OWNER_ONLY)
    verdicts = {v.object_key: v for v in az.analyze_batch(objects).verdicts}

    assert set(verdicts) == {o["object_key"] for o in objects}, "every object must get a verdict"
    assert verdicts["getRecord|recordId=1"].is_bola
    assert not verdicts["getRecord|recordId=2"].is_bola
    assert verdicts["getRecord|recordId=3"].is_bola


# ---- full pipeline: resumable runner over persisted snapshots ----------------------------

def test_analyze_run_end_to_end(settings, tmp_path):
    store = Store(db_path=str(tmp_path / "run.db"))
    run_id = "run-llm"

    def seed(key, op, before, hacker, after):
        store.save_snapshot(run_id, "snap_before", op, key, "done", status_code=before[0], response=before[1])
        store.save_snapshot(run_id, "snap_hacker", op, key, "done", status_code=hacker[0], response=hacker[1])
        store.save_snapshot(run_id, "snap_after", op, key, "done", status_code=after[0], response=after[1])

    seed("getRecord|recordId=1", "getRecord",
         (200, {"id": 1, "owner": "alice", "ssn": "111"}),
         (200, {"id": 1, "owner": "alice", "ssn": "111"}),   # attacker saw it → BOLA
         (200, {"id": 1, "owner": "alice", "ssn": "111"}))
    seed("getRecord|recordId=2", "getRecord",
         (200, {"id": 2, "owner": "bob"}),
         (403, {"error": "forbidden"}),                       # refused → clean
         (200, {"id": 2, "owner": "bob"}))

    settings.analysis.objects_per_call = 1  # force two separate calls, exercise the loop
    settings.analysis.batch_size = 2
    res = analyze_run(run_id, settings, store, access_description=OWNER_ONLY, show_progress=False)

    # Object 2 (attacker refused with 403, owner unchanged) carries no read or write signal, so the
    # context layer correctly drops it before any LLM call — only the leaking object reaches the judge.
    assert res.completed
    assert res.objects_total == 1
    by_key = {f["object_key"]: f for f in res.findings}
    assert by_key["getRecord|recordId=1"]["is_bola"]
    assert "getRecord|recordId=2" not in by_key  # clean object filtered out, never judged

    # the pass was persisted and is re-readable
    rec = store.get_analysis(res.analysis_id)
    assert rec["status"] == "completed"
    assert len(rec["findings"]) == 1


# ---- judge-of-the-judge: an independent model rates the analyzer's reasoning -------------

class Judgement(BaseModel):
    sound: bool = Field(description="Is the analyst's verdict and rationale defensible on the evidence?")
    reason: str


def test_llm_judge_rates_verdict_sound(settings):
    owner = _snap(200, {"id": 7, "owner": "alice", "ssn": "111-22-3333"})
    v = _verdict(settings, _obj("getRecord|recordId=7", owner, owner, owner))

    judge = build_chat_model(settings).with_structured_output(Judgement)
    result = judge.invoke(
        "Evidence: record 7 is owned by user alice, who read it and saw fields including an SSN. A "
        "second, different account — which is NOT the owner of record 7 — addressed the same record "
        "id and received the identical record including the SSN. The documented rule: a record may "
        "be read only by its owner.\n"
        f"An analyst concluded is_bola={v.is_bola}, unauthorized_read={v.unauthorized_read}, "
        f"with rationale: {v.rationale!r}.\n"
        "Judge only whether that conclusion follows from the evidence and the rule — do not "
        "assume the analyst is right or wrong."
    )
    assert result.sound, result.reason

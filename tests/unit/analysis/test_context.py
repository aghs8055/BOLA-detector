"""Unit tests for `bola.analysis.context` (owner filter + deterministic diff + signal gating)."""

from bola.analysis.context import (
    _diff,
    build_object_context,
    group_objects,
    response_objects,
)


def _exec(seq, identity, op, *, code=200, request=None, response=None):
    return {"seq": seq, "identity": identity, "op_key": op, "status_code": code,
            "request": request or {}, "response": response}


def test_response_objects_flags_attacker_reply_using_owner_id():
    # the owner's id 'acct-victim-0001' appears in a regular response; the attacker then uses it and
    # gets data back -> a reply-only read crossing the snapshots cannot capture.
    execs = [
        _exec(0, "regular", "listAccounts", response={"items": [{"id": "acct-victim-0001"}]}),
        _exec(1, "attacker", "revealCard",
              request={"path_params": {"id": "acct-victim-0001"}},
              response={"pan": "secret"}),
    ]
    [obj] = response_objects(execs)
    assert obj["op_key"] == "revealCard"
    assert obj["owner_id_used"] == ["acct-victim-0001"]
    assert obj["attacker_response"] == {"pan": "secret"}


def test_response_objects_excludes_attacker_own_id():
    # the id the attacker used never appeared in an owner response -> it is the attacker's own -> skip
    execs = [
        _exec(0, "regular", "listAccounts", response={"items": [{"id": "acct-victim-0001"}]}),
        _exec(1, "attacker", "revealCard",
              request={"path_params": {"id": "acct-attacker-9999"}},
              response={"pan": "mine"}),
    ]
    assert response_objects(execs) == []


def test_response_objects_excludes_refused_and_regular_calls():
    execs = [
        _exec(0, "regular", "listAccounts", response={"id": "acct-victim-0001"}),
        _exec(1, "attacker", "getThing", code=403,
              request={"path_params": {"id": "acct-victim-0001"}}, response={"detail": "no"}),
        _exec(2, "regular", "getThing",
              request={"path_params": {"id": "acct-victim-0001"}}, response={"ok": 1}),
    ]
    assert response_objects(execs) == []


def _snap(phase, object_key, *, op="getPet", status="done", code=200, response=None):
    return {
        "phase": phase,
        "op_key": op,
        "object_key": object_key,
        "status": status,
        "request": {"petId": 7},
        "status_code": code,
        "response": response if response is not None else {"id": 7},
    }


def test_read_leak_object_is_kept_with_attacker_view():
    # owner could read it (before 200) and the attacker also got data (hacker 200) -> a read crossing
    snaps = [
        _snap("snap_before", "getPet|petId=7", response={"id": 7, "secret": "s"}),
        _snap("snap_hacker", "getPet|petId=7", response={"id": 7, "secret": "s"}),
        _snap("snap_after", "getPet|petId=7", response={"id": 7, "secret": "s"}),
    ]
    [obj] = group_objects(snaps)
    assert obj["object_key"] == "getPet|petId=7"
    assert obj["attacker"]["status_code"] == 200
    assert obj["attacker"]["response"] == {"id": 7, "secret": "s"}
    assert obj["victim_change"] == []  # nothing changed


def test_write_change_is_diffed_even_when_attacker_was_refused():
    # attacker's request 403 (refused), but the owner's object changed -> kept, diff carries the change
    snaps = [
        _snap("snap_before", "getPet|petId=7", response={"id": 7, "status": "active"}),
        _snap("snap_hacker", "getPet|petId=7", code=403, response={"detail": "Forbidden"}),
        _snap("snap_after", "getPet|petId=7", response={"id": 7, "status": "frozen"}),
    ]
    [obj] = group_objects(snaps)
    assert obj["victim_change"] == [{"path": "/status", "before": "active", "after": "frozen"}]


def test_attacker_own_object_is_filtered_out():
    # owner could NOT read it at baseline (before 403) -> not the owner's object -> dropped
    snaps = [
        _snap("snap_before", "getPet|petId=7", code=403, response={"detail": "Forbidden"}),
        _snap("snap_hacker", "getPet|petId=7", code=200, response={"id": 7}),
    ]
    assert group_objects(snaps) == []


def test_no_signal_object_is_dropped_without_llm():
    # owner owns it, attacker refused, nothing changed -> no read, no write -> skip
    snaps = [
        _snap("snap_before", "getPet|petId=7", response={"id": 7}),
        _snap("snap_hacker", "getPet|petId=7", code=403, response={"detail": "Forbidden"}),
        _snap("snap_after", "getPet|petId=7", response={"id": 7}),
    ]
    assert group_objects(snaps) == []


def test_object_without_hacker_view_is_dropped():
    assert group_objects([_snap("snap_before", "getPet|petId=9")]) == []


def test_object_without_before_view_is_dropped():
    assert group_objects([_snap("snap_hacker", "getPet|petId=7")]) == []


def test_failed_snapshots_are_skipped():
    assert group_objects([_snap("snap_hacker", "getPet|petId=7", status="failed",
                                code=None, response=None)]) == []


def test_grouping_preserves_first_seen_order():
    snaps = [
        _snap("snap_before", "getPet|petId=7", response={"v": "a"}),
        _snap("snap_hacker", "getPet|petId=7", code=200, response={"v": "a"}),
        _snap("snap_before", "getPet|petId=8", op="getPet", response={"v": "b"}),
        _snap("snap_hacker", "getPet|petId=8", op="getPet", code=200, response={"v": "b"}),
    ]
    assert [o["object_key"] for o in group_objects(snaps)] == ["getPet|petId=7", "getPet|petId=8"]


def test_large_attacker_response_is_truncated():
    snaps = [
        _snap("snap_before", "getPet|petId=7", response="ok"),
        _snap("snap_hacker", "getPet|petId=7", response="x" * 5000),
    ]
    text = group_objects(snaps)[0]["attacker"]["response"]
    assert text.endswith("…(truncated)") and len(text) < 5000


def test_build_object_context_carries_access_and_objects():
    snaps = [
        _snap("snap_before", "getPet|petId=7"),
        _snap("snap_hacker", "getPet|petId=7"),
    ]
    ctx = build_object_context(group_objects(snaps), "owners only")
    assert ctx["access_model"] == "owners only"
    assert ctx["objects"][0]["object_key"] == "getPet|petId=7"


def test_build_object_context_handles_empty_access():
    ctx = build_object_context([], "")
    assert ctx["access_model"] == "(none provided)"
    assert ctx["objects"] == []


def test_diff_finds_nested_change_only():
    before = {"a": 1, "nested": {"x": 1, "y": 2}, "list": [1, 2, 3]}
    after = {"a": 1, "nested": {"x": 1, "y": 99}, "list": [1, 2, 3]}
    assert _diff(before, after) == [{"path": "/nested/y", "before": 2, "after": 99}]


def test_diff_reports_deletion_as_absent():
    assert _diff({"id": 7}, None) == [{"path": "/", "before": {"id": 7}, "after": None}]


def test_diff_identical_is_empty():
    assert _diff({"a": [1, {"b": 2}]}, {"a": [1, {"b": 2}]}) == []


def test_id_scalars_collects_integer_ids_under_id_keys():
    from bola.analysis.context import _id_scalars
    # small integer ids (e.g. Juice Shop) under id-shaped keys are collected; non-id numbers are not
    got = _id_scalars({"data": {"id": 7, "user_id": 3, "amount": 200, "name": "x"}})
    assert got == {"7", "3"}
    # long strings still count regardless of key
    assert _id_scalars({"token": "abcdefgh12"}) == {"abcdefgh12"}


def test_response_objects_matches_small_integer_owner_id():
    # owner (regular) exposed integer id 7; attacker replays it and gets data -> a reply-only crossing
    execs = [
        _exec(0, "regular", "listFeedbacks", response={"data": [{"id": 7}, {"id": 9}]}),
        _exec(1, "attacker", "getFeedback", code=200,
              request={"path_params": {"id": 7}}, response={"data": {"id": 7, "comment": "secret"}}),
    ]
    out = response_objects(execs)
    assert any(o["owner_id_used"] == ["7"] for o in out)

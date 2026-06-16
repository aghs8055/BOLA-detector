"""Unit tests for `bola.execution.snapshotter`."""

import pytest

from bola.db.store import Store
from bola.execution.api_executor import ApiExecutor, HttpResponse
from bola.execution.field_repo import FieldRepo
from bola.execution.snapshotter import (
    SNAP_BEFORE,
    SNAP_HACKER,
    _query_probe,
    capture_snapshots,
    object_key,
    snapshot_targets,
)
from bola.spec import views
from bola.spec.views import ParamRef


def test_query_probe_picks_generic_values_for_optional_query_params():
    params = [
        ParamRef("id", "path", "string", None, True),                     # path: ignored
        ParamRef("page", "query", "integer", None, True),                 # required: ignored
        ParamRef("expand", "query", "string", None, False),               # -> generic "1"
        ParamRef("verbose", "query", "boolean", None, False),             # -> True
        ParamRef("mode", "query", "string", None, False, {"enum": ["full", "lite"]}),  # -> enum[0]
    ]
    assert _query_probe(params) == {"expand": "1", "verbose": True, "mode": "full"}


def test_object_key_distinguishes_probed_variant():
    plain = object_key("getX", {"id": 7})
    probed = object_key("getX", {"id": 7}, {"expand": "1"})
    assert plain != probed and "expand=1" in probed


def _relations():
    """createCategory's response id flows into getCategory's path param."""
    return [{
        "source_key": "createCategory",
        "target_key": "getCategory",
        "edge": {
            "source": {"status_code": "201", "media_type": "application/json",
                       "json_pointer": "/properties/id"},
            "target": {"type": "parameter", "name": "categoryId", "location": "path"},
        },
    }]


def _repo(*ids):
    repo = FieldRepo()
    for i in ids:
        repo.add("createCategory", "/properties/id", i)
    return repo


@pytest.fixture
def store(tmp_path):
    return Store(db_path=str(tmp_path / "t.db"))


class FakeTransport:
    """Records requests and returns a canned response (or raises) per call."""

    def __init__(self, response=None, raise_exc=None):
        self.response = response
        self.raise_exc = raise_exc
        self.requests = []

    def __call__(self, req, timeout):
        self.requests.append(req)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response


def _executor(transport):
    return ApiExecutor("http://t", transport=transport, max_retries=1)


# ---- enumeration --------------------------------------------------------------

def test_snapshot_targets_enumerates_objects_from_pool(recursive_spec):
    ops = views.operations(recursive_spec)
    targets = snapshot_targets(recursive_spec, ops, _relations(), _repo(7))
    assert len(targets) == 1
    t = targets[0]
    assert t.op_key == "getCategory"
    assert t.object_key == object_key("getCategory", {"categoryId": 7})
    assert t.request.method == "GET"
    assert t.request.path_params == {"categoryId": 7}


def test_no_candidates_means_no_target(recursive_spec):
    ops = views.operations(recursive_spec)
    assert snapshot_targets(recursive_spec, ops, _relations(), FieldRepo()) == []


def test_multiple_ids_become_multiple_objects(recursive_spec):
    ops = views.operations(recursive_spec)
    targets = snapshot_targets(recursive_spec, ops, _relations(), _repo(7, 8))
    assert {t.object_key for t in targets} == {
        object_key("getCategory", {"categoryId": 7}),
        object_key("getCategory", {"categoryId": 8}),
    }


# ---- capture ------------------------------------------------------------------

def test_capture_persists_snapshots(recursive_spec, store):
    ops = views.operations(recursive_spec)
    transport = FakeTransport(HttpResponse(status_code=200, body={"id": 7, "owner": "alice"}))
    res = capture_snapshots(
        SNAP_HACKER, "run1", recursive_spec, ops, _relations(), _repo(7),
        _executor(transport), store, show_progress=False,
    )
    assert res.completed
    assert res.captured == 1
    snaps = store.get_snapshots("run1")
    assert len(snaps) == 1
    assert snaps[0]["phase"] == "snap_hacker"
    assert snaps[0]["response"] == {"id": 7, "owner": "alice"}
    assert snaps[0]["status_code"] == 200
    # the GET actually went to the id-bound URL
    assert transport.requests[0].url == "http://t/categories/7"


class _InterruptingTransport:
    """Returns a canned response, but raises KeyboardInterrupt on the n-th call."""

    def __init__(self, response, raise_at):
        self.response = response
        self.raise_at = raise_at
        self.i = 0

    def __call__(self, req, timeout):
        self.i += 1
        if self.i == self.raise_at:
            raise KeyboardInterrupt
        return self.response


def test_capture_reraises_interrupt_by_default_after_checkpointing(recursive_spec, store):
    ops = views.operations(recursive_spec)
    transport = _InterruptingTransport(HttpResponse(status_code=200, body={"id": 1}), raise_at=2)
    with pytest.raises(KeyboardInterrupt):
        capture_snapshots(SNAP_BEFORE, "run1", recursive_spec, ops, _relations(), _repo(7, 8),
                          _executor(transport), store, show_progress=False)
    # the first object was checkpointed before the interrupt; a rerun would resume from there
    done = [s for s in store.get_snapshots("run1") if s["status"] == "done"]
    assert len(done) == 1


def test_capture_swallows_interrupt_when_reraise_disabled(recursive_spec, store):
    ops = views.operations(recursive_spec)
    transport = _InterruptingTransport(HttpResponse(status_code=200, body={"id": 1}), raise_at=2)
    res = capture_snapshots(SNAP_BEFORE, "run1", recursive_spec, ops, _relations(), _repo(7, 8),
                            _executor(transport), store, show_progress=False,
                            reraise_interrupt=False)
    assert not res.completed
    assert res.captured == 1


def test_capture_resumes_skips_done(recursive_spec, store):
    ops = views.operations(recursive_spec)
    t1 = FakeTransport(HttpResponse(status_code=200, body={"id": 7}))
    capture_snapshots(SNAP_BEFORE, "run1", recursive_spec, ops, _relations(), _repo(7),
                      _executor(t1), store, show_progress=False)
    t2 = FakeTransport(HttpResponse(status_code=200, body={"id": 7}))
    res = capture_snapshots(SNAP_BEFORE, "run1", recursive_spec, ops, _relations(), _repo(7),
                            _executor(t2), store, show_progress=False)
    assert res.skipped == 1
    assert res.captured == 0
    assert res.completed
    assert t2.requests == []  # nothing re-fetched


def test_phases_are_independent(recursive_spec, store):
    ops = views.operations(recursive_spec)
    t = FakeTransport(HttpResponse(status_code=200, body={"id": 7}))
    capture_snapshots(SNAP_BEFORE, "run1", recursive_spec, ops, _relations(), _repo(7),
                      _executor(t), store, show_progress=False)
    # a different phase over the same object is still pending
    res = capture_snapshots(SNAP_HACKER, "run1", recursive_spec, ops, _relations(), _repo(7),
                            _executor(t), store, show_progress=False)
    assert res.captured == 1


def test_capture_records_failure_and_stops(recursive_spec, store):
    ops = views.operations(recursive_spec)
    transport = FakeTransport(raise_exc=ConnectionError("down"))
    res = capture_snapshots(
        SNAP_BEFORE, "run1", recursive_spec, ops, _relations(), _repo(7, 8),
        _executor(transport), store, on_error="stop", show_progress=False,
    )
    assert not res.completed
    assert res.failed == 1
    failed = [s for s in store.get_snapshots("run1") if s["status"] == "failed"]
    assert len(failed) == 1
    assert "down" in failed[0]["error"]


def test_capture_continue_processes_rest_after_failure(recursive_spec, store):
    ops = views.operations(recursive_spec)
    transport = FakeTransport(raise_exc=ConnectionError("down"))
    res = capture_snapshots(
        SNAP_BEFORE, "run1", recursive_spec, ops, _relations(), _repo(7, 8),
        _executor(transport), store, on_error="continue", show_progress=False,
    )
    assert res.failed == 2  # both attempted despite the first failing
    assert not res.completed
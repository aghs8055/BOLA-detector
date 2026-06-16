"""Drive the merged build→attack loop with a fake transport + fake strategy/analyzer.

One continuous strategy emits identity-tagged actions; the runner picks the matching auth and takes
the victim baseline at the first attacker-tagged action. No network, no LLM.
"""

import json
from pathlib import Path

import pytest

from bola.analysis.models import AnalysisBatch, ObjectVerdict
from bola.db.store import Store
from bola.execution.api_executor import ApiRequest, HttpResponse
from bola.runner.run_manager import execute_run
from bola.strategies.base import ATTACKER, REGULAR, PlannedAction, Strategy
from bola.strategies.models import AgentMemory
from bola.target.manifest import load_manifest_from_dict

# A relation: createCategory's response id (201/json at /properties/id) fills getCategory's path id.
RELATIONS = [{
    "source_key": "createCategory",
    "target_key": "getCategory",
    "edge": {
        "source": {"status_code": "201", "media_type": "application/json",
                   "json_pointer": "/properties/id"},
        "target": {"type": "parameter", "name": "categoryId", "location": "path"},
    },
}]


def _manifest():
    return load_manifest_from_dict({
        "version": 1,
        "base_url": "http://t",
        "spec": {"kind": "file", "value": "x.json"},
        "auth": {},
        "users": {"regular": {"vars": {}}, "attacker": {"vars": {}}},
        "access": "an owner may read only their own category",
    })


def _transport():
    """POST /categories → 201 {id:7}; GET /categories/7 → 200 {id:7}; else 404. Records calls."""
    calls = []

    def transport(req, timeout):
        calls.append((req.method, req.url))
        if req.method == "POST" and req.url == "http://t/categories":
            return HttpResponse(status_code=201, headers={"Content-Type": "application/json"},
                                body={"id": 7, "name": "n"})
        if req.method == "GET" and req.url == "http://t/categories/7":
            return HttpResponse(status_code=200, headers={"Content-Type": "application/json"},
                                body={"id": 7, "name": "n"})
        return HttpResponse(status_code=404, body=None)

    transport.calls = calls
    return transport


class FakeStrategy(Strategy):
    """Emits a fixed list of per-turn action batches (each action already identity-tagged)."""

    def __init__(self, batches, memory=None):
        self._batches = list(batches)
        self.memory = memory
        self._i = 0

    def plan(self):
        if self._i >= len(self._batches):
            return []
        batch = self._batches[self._i]
        self._i += 1
        return batch

    def observe(self, results):
        pass

    def is_done(self):
        return self._i >= len(self._batches)


class FakeAnalyzer:
    def __init__(self, *, bola_keys=(), access_description="", model_name="fake"):
        self.bola_keys = set(bola_keys)
        self.access_description = access_description
        self.model_name = model_name

    def analyze_batches(self, batches):
        return [
            AnalysisBatch(verdicts=[
                ObjectVerdict(object_key=o["object_key"], rationale="r",
                              is_bola=o["object_key"] in self.bola_keys)
                for o in group
            ])
            for group in batches
        ]


class FakeAggregator:
    """A canned report aggregator — no LLM. Echoes a marker so tests can assert it ran."""

    def __init__(self, *, model_name="fake"):
        self.model_name = model_name

    def aggregate(self, context):
        from bola.aggregate.models import AggregatedReport, ReportFinding

        bolas = [
            ReportFinding(
                title="cross-user read", apis=[f["op_key"]],
                description="d", evidence_source="analysis",
                how_it_was_found="snapshot diff", fix_suggestion="add owner check",
            )
            for f in (context.get("analysis_findings") or []) if f.get("is_bola")
        ]
        return AggregatedReport(target_summary="summary", bola_findings=bolas)


def _create():
    return PlannedAction(op_key="createCategory",
                         request=ApiRequest(method="POST", path="/categories"),
                         rationale="build a category", identity=REGULAR)


def _attacker_get(cat_id=7):
    return PlannedAction(op_key="getCategory",
                         request=ApiRequest(method="GET", path="/categories/{categoryId}",
                                            path_params={"categoryId": cat_id}),
                         rationale="cross-read", identity=ATTACKER)


def _factory(batches, memory=None):
    return lambda ctx, resume: FakeStrategy(batches, memory=memory)


@pytest.fixture
def store(tmp_path):
    return Store(db_path=str(tmp_path / "t.db"))


@pytest.fixture
def runs_settings(settings, tmp_path):
    settings.storage.runs_dir = str(tmp_path / "runs")
    return settings


def test_full_run_logs_harvests_snapshots_and_analyzes(store, runs_settings, recursive_spec):
    transport = _transport()
    result = execute_run(
        recursive_spec, _manifest(), RELATIONS, runs_settings, store,
        run_id="r1", transport=transport,
        strategy_factory=_factory([[_create()], [_attacker_get()]],
                                  memory=AgentMemory(notes="explored")),
        analyzer=FakeAnalyzer(bola_keys={"getCategory|categoryId=7"}),
        aggregator=FakeAggregator(),
        ground_truth=["getCategory"], show_progress=False,
    )

    # one continuous log: a regular build call then an attacker crossing
    execs = store.get_executions("r1")
    assert [(e["op_key"], e["identity"]) for e in execs] == [
        ("createCategory", "regular"), ("getCategory", "attacker"),
    ]

    # all three snapshot phases captured the shared id-7 object
    snaps = {(s["phase"], s["object_key"]) for s in store.get_snapshots("r1") if s["status"] == "done"}
    assert ("snap_before", "getCategory|categoryId=7") in snaps
    assert ("snap_hacker", "getCategory|categoryId=7") in snaps
    assert ("snap_after", "getCategory|categoryId=7") in snaps

    assert result.completed and result.analysis.completed
    assert result.metrics["quality"]["tp"] == 1
    assert result.metrics["execution"]["regular_calls"] == 1
    assert result.metrics["execution"]["hacker_calls"] == 1
    run = store.get_run("r1")
    assert json.loads(run.findings_json)[0]["is_bola"] is True


def test_snap_before_taken_before_first_attacker_call(store, runs_settings, recursive_spec):
    """The victim baseline must exist before any attacker call is logged (created_at ordering)."""
    execute_run(
        recursive_spec, _manifest(), RELATIONS, runs_settings, store,
        run_id="rb", transport=_transport(),
        strategy_factory=_factory([[_create()], [_attacker_get()]]),
        analyze=False, show_progress=False,
    )
    snap_before = next(s for s in store.get_snapshots("rb")
                       if s["phase"] == "snap_before" and s["status"] == "done")
    attacker_exec = next(e for e in store.get_executions("rb") if e["identity"] == "attacker")
    assert snap_before["created_at"] <= attacker_exec["created_at"]


def test_per_turn_memory_persisted(store, runs_settings, recursive_spec):
    execute_run(
        recursive_spec, _manifest(), RELATIONS, runs_settings, store,
        run_id="r2", transport=_transport(),
        strategy_factory=_factory([[_create()]], memory=AgentMemory(notes="explored")),
        analyze=False, show_progress=False,
    )
    mems = store.get_strategy_memories("r2", "run")
    assert len(mems) == 1
    assert mems[0]["turn_index"] == 0
    assert mems[0]["memory"]["notes"] == "explored"


def test_writes_run_and_analysis_json(store, runs_settings, recursive_spec):
    result = execute_run(
        recursive_spec, _manifest(), RELATIONS, runs_settings, store,
        run_id="r3", transport=_transport(),
        strategy_factory=_factory([[_create()], [_attacker_get()]],
                                  memory=AgentMemory(notes="explored")),
        analyzer=FakeAnalyzer(bola_keys={"getCategory|categoryId=7"}),
        aggregator=FakeAggregator(),
        show_progress=False,
    )
    run_dir = Path(runs_settings.storage.runs_dir) / "r3"
    run_json = json.loads((run_dir / "run.json").read_text())
    assert run_json["metrics"]["execution"]["regular_calls"] == 1
    assert run_json["strategy_memory"][0]["memory"]["notes"] == "explored"

    analysis_json = json.loads((run_dir / f"analysis_{result.analysis.analysis_id}.json").read_text())
    assert analysis_json["findings"][0]["object_key"] == "getCategory|categoryId=7"
    assert analysis_json["access_description"].startswith("an owner")


def test_resume_does_not_re_execute_completed_actions(store, runs_settings, recursive_spec):
    execute_run(recursive_spec, _manifest(), RELATIONS, runs_settings, store, run_id="r4",
                transport=_transport(), strategy_factory=_factory([[_create()]]),
                analyze=False, show_progress=False)
    first = len(store.get_executions("r4"))
    assert first == 1

    # rerun: the log replays to rebuild the repo; an empty strategy emits nothing new
    execute_run(recursive_spec, _manifest(), RELATIONS, runs_settings, store, run_id="r4",
                transport=_transport(), strategy_factory=lambda ctx, resume: FakeStrategy([]),
                analyze=False, show_progress=False)
    assert len(store.get_executions("r4")) == first  # no new actions on resume
    snaps = {s["object_key"] for s in store.get_snapshots("r4") if s["status"] == "done"}
    assert "getCategory|categoryId=7" in snaps  # repo replayed → object still resolvable


def _transport_raising(n):
    """Like `_transport` but raises KeyboardInterrupt on the n-th call (simulating Ctrl-C)."""
    base = _transport()
    state = {"i": 0}

    def transport(req, timeout):
        state["i"] += 1
        if state["i"] == n:
            raise KeyboardInterrupt
        return base(req, timeout)

    return transport


def _resumable_factory(batches):
    """A fake factory that honours resume: skips the turns already in the log."""
    return lambda ctx, resume: FakeStrategy(batches[resume.turns_done:], memory=AgentMemory())


def test_ctrl_c_during_build_persists_and_resumes(store, runs_settings, recursive_spec):
    batches = [[_create()], [_create()], [_attacker_get()]]
    # 2nd transport call is the 2nd create (a build POST) — interrupt there
    with pytest.raises(KeyboardInterrupt):
        execute_run(recursive_spec, _manifest(), RELATIONS, runs_settings, store, run_id="rc",
                    transport=_transport_raising(2), strategy_factory=_resumable_factory(batches),
                    analyze=False, show_progress=False)
    assert len(store.get_executions("rc")) == 1  # only the first create was committed

    # resume with a clean transport — no duplicate of the first create, run finishes
    execute_run(recursive_spec, _manifest(), RELATIONS, runs_settings, store, run_id="rc",
                transport=_transport(), strategy_factory=_resumable_factory(batches),
                analyze=False, show_progress=False)
    ids = [(e["op_key"], e["identity"]) for e in store.get_executions("rc")]
    assert ids == [("createCategory", "regular"), ("createCategory", "regular"),
                   ("getCategory", "attacker")]
    snaps = {(s["phase"], s["object_key"]) for s in store.get_snapshots("rc") if s["status"] == "done"}
    assert ("snap_before", "getCategory|categoryId=7") in snaps
    assert ("snap_hacker", "getCategory|categoryId=7") in snaps


def test_ctrl_c_during_snap_before_keeps_baseline_pristine(store, runs_settings, recursive_spec):
    batches = [[_create()], [_attacker_get()]]
    # call 1 = create (POST); call 2 = the snap_before GET — interrupt the baseline capture
    with pytest.raises(KeyboardInterrupt):
        execute_run(recursive_spec, _manifest(), RELATIONS, runs_settings, store, run_id="rs",
                    transport=_transport_raising(2), strategy_factory=_resumable_factory(batches),
                    analyze=False, show_progress=False)
    execs = store.get_executions("rs")
    assert sum(1 for e in execs if e["identity"] == "attacker") == 0  # attack never started
    assert not any(s["phase"] == "snap_before" and s["status"] == "done"
                   for s in store.get_snapshots("rs"))  # baseline not yet recorded

    # resume: the baseline is completed (victim still pristine) *before* the attacker acts
    execute_run(recursive_spec, _manifest(), RELATIONS, runs_settings, store, run_id="rs",
                transport=_transport(), strategy_factory=_resumable_factory(batches),
                analyze=False, show_progress=False)
    snaps = {(s["phase"], s["object_key"]) for s in store.get_snapshots("rs") if s["status"] == "done"}
    assert ("snap_before", "getCategory|categoryId=7") in snaps
    assert sum(1 for e in store.get_executions("rs") if e["identity"] == "attacker") == 1


class FlakyAnalyzer(FakeAnalyzer):
    """Raises KeyboardInterrupt on its first batch, then behaves like FakeAnalyzer."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self._raise = True

    def analyze_batches(self, batches):
        if self._raise:
            self._raise = False
            raise KeyboardInterrupt
        return super().analyze_batches(batches)


# call order for [[create],[attacker_get]]: 1 create, 2 snap_before, 3 attacker_get,
# 4 snap_hacker, 5 snap_after — used to target an interrupt at a specific phase.
@pytest.mark.parametrize("phase,raise_at", [("snap_hacker", 4), ("snap_after", 5)])
def test_ctrl_c_during_post_loop_snapshots_propagates_and_resumes(
    store, runs_settings, recursive_spec, phase, raise_at
):
    batches = [[_create()], [_attacker_get()]]
    with pytest.raises(KeyboardInterrupt):
        execute_run(recursive_spec, _manifest(), RELATIONS, runs_settings, store, run_id="rp",
                    transport=_transport_raising(raise_at),
                    strategy_factory=_resumable_factory(batches),
                    analyze=False, show_progress=False)
    assert not any(s["phase"] == phase and s["status"] == "done"
                   for s in store.get_snapshots("rp"))  # the interrupted phase isn't recorded

    execute_run(recursive_spec, _manifest(), RELATIONS, runs_settings, store, run_id="rp",
                transport=_transport(), strategy_factory=_resumable_factory(batches),
                analyze=False, show_progress=False)
    snaps = {(s["phase"], s["object_key"]) for s in store.get_snapshots("rp") if s["status"] == "done"}
    assert ("snap_hacker", "getCategory|categoryId=7") in snaps
    assert ("snap_after", "getCategory|categoryId=7") in snaps


def test_ctrl_c_during_analysis_propagates_and_resumes(store, runs_settings, recursive_spec):
    batches = [[_create()], [_attacker_get()]]
    with pytest.raises(KeyboardInterrupt):
        execute_run(recursive_spec, _manifest(), RELATIONS, runs_settings, store, run_id="ra",
                    transport=_transport(), strategy_factory=_resumable_factory(batches),
                    analyzer=FlakyAnalyzer(bola_keys={"getCategory|categoryId=7"}),
                    aggregator=FakeAggregator(),
                    show_progress=False)
    # execution + snapshots completed; the auto analysis pass exists but is unfinished
    assert store.get_analysis("ra-auto")["status"] != "completed"

    # resume the same auto pass (stable id) with a working analyzer → it finishes, no fork
    execute_run(recursive_spec, _manifest(), RELATIONS, runs_settings, store, run_id="ra",
                transport=_transport(), strategy_factory=_resumable_factory(batches),
                analyzer=FakeAnalyzer(bola_keys={"getCategory|categoryId=7"}),
                aggregator=FakeAggregator(),
                show_progress=False)
    rec = store.get_analysis("ra-auto")
    assert rec["status"] == "completed"
    assert rec["findings"][0]["is_bola"] is True
    assert len(store.list_analyses("ra")) == 1  # resumed, did not create a second pass


class ExplodingStrategy(Strategy):
    """Plans one build call, then raises a given exception on the next plan() (e.g. an LLM timeout)."""

    def __init__(self, exc):
        self.memory = None
        self._exc = exc
        self._i = 0

    def plan(self):
        self._i += 1
        if self._i == 1:
            return [_create()]
        raise self._exc

    def observe(self, results):
        pass

    def is_done(self):
        return False  # never finishes on its own; it raises instead


def _transport_erroring(n, exc):
    """Like `_transport`, but raises `exc` on the n-th call (e.g. an API request timeout)."""
    base = _transport()
    state = {"i": 0}

    def transport(req, timeout):
        state["i"] += 1
        if state["i"] == n:
            raise exc
        return base(req, timeout)

    return transport


@pytest.mark.parametrize("exc", [TimeoutError("llm timed out"), ValueError("unexpected")])
def test_llm_timeout_or_unexpected_exception_stops_gracefully(store, runs_settings, recursive_spec, exc):
    with pytest.raises(type(exc)):
        execute_run(recursive_spec, _manifest(), RELATIONS, runs_settings, store, run_id="rx",
                    transport=_transport(),
                    strategy_factory=lambda ctx, resume: ExplodingStrategy(exc),
                    analyze=False, show_progress=False)
    # the build call before the failure is persisted, and the run is marked failed (not "running")
    assert len(store.get_executions("rx")) == 1
    assert store.get_run("rx").status == "failed"


def test_api_error_during_execution_is_recorded_and_run_continues(store, runs_settings, recursive_spec):
    # the 2nd call (a build POST) times out at the transport; the executor records it and the loop goes on
    transport = _transport_erroring(2, RuntimeError("api request timed out"))
    result = execute_run(
        recursive_spec, _manifest(), RELATIONS, runs_settings, store, run_id="rq",
        transport=transport,
        strategy_factory=_factory([[_create()], [_create()]]),
        analyze=False, show_progress=False,
    )
    assert result.completed
    assert store.get_run("rq").status == "completed"
    execs = store.get_executions("rq")
    assert execs[0]["ok"] is True and execs[0]["error"] is None
    assert execs[1]["ok"] is False and execs[1]["error"] is not None  # timeout recorded, not fatal


def test_run_resumes_after_a_failed_run(store, runs_settings, recursive_spec):
    with pytest.raises(ValueError):
        execute_run(recursive_spec, _manifest(), RELATIONS, runs_settings, store, run_id="rr",
                    transport=_transport(),
                    strategy_factory=lambda ctx, resume: ExplodingStrategy(ValueError("boom")),
                    analyze=False, show_progress=False)
    # rerun with a healthy strategy that skips the already-logged build call → run completes
    execute_run(recursive_spec, _manifest(), RELATIONS, runs_settings, store, run_id="rr",
                transport=_transport(),
                strategy_factory=lambda ctx, resume: FakeStrategy(
                    [[_attacker_get()]] if resume.regular_done else [[_create()], [_attacker_get()]]),
                analyze=False, show_progress=False)
    assert store.get_run("rr").status == "completed"


def test_default_factory_random_budget_split_and_resume(store, runs_settings, recursive_spec):
    runs_settings.test.regular_ops = 3
    runs_settings.test.hacker_ops = 2
    execute_run(recursive_spec, _manifest(), RELATIONS, runs_settings, store, run_id="r5",
                transport=_transport(), analyze=False, show_progress=False)
    execs = store.get_executions("r5")
    assert sum(1 for e in execs if e["identity"] == "regular") == 3
    assert sum(1 for e in execs if e["identity"] == "attacker") == 2

    execute_run(recursive_spec, _manifest(), RELATIONS, runs_settings, store, run_id="r5",
                transport=_transport(), analyze=False, show_progress=False)
    execs2 = store.get_executions("r5")
    assert sum(1 for e in execs2 if e["identity"] == "regular") == 3  # budgets already spent
    assert sum(1 for e in execs2 if e["identity"] == "attacker") == 2

def test_without_auth_login_excludes_the_manifest_login_op():
    """The manifest's auth-login endpoint is removed from the strategy's callable operations."""
    from bola.runner.run_manager import _without_auth_login

    manifest = load_manifest_from_dict({
        "version": 1, "base_url": "http://t",
        "spec": {"kind": "file", "value": "/x"},
        "auth": {"login": {"method": "POST", "path": "/api/login",
                           "headers": {}, "body": "{}"},
                 "extract": {"from": "body", "path": "token"},
                 "inject": {"into": "header", "name": "Authorization", "value": "Bearer {{credential}}"}},
        "users": {"regular": {"vars": {}}, "attacker": {"vars": {}}},
    })

    class _Op:
        def __init__(self, method, path):
            self.method, self.path = method, path

    ops = [_Op("POST", "/api/login"), _Op("GET", "/api/accounts"), _Op("POST", "/api/accounts")]
    kept = _without_auth_login(ops, manifest)
    assert [(o.method, o.path) for o in kept] == [("GET", "/api/accounts"), ("POST", "/api/accounts")]

"""Unit tests for `bola.strategies.ai_strategy`."""

import json

from bola.execution.api_executor import ApiRequest, ExecutionResult, FilePart
from bola.execution.field_repo import FieldRepo
from bola.spec import views
from bola.strategies.ai_strategy import AIStrategy
from bola.strategies.base import ATTACKER, REGULAR, ActionResult, PlannedAction, StrategyContext
from bola.strategies.models import (
    AgentMemory,
    AgentTurn,
    BodyInput,
    ConcreteChoice,
    FileChoice,
    GeneratorChoice,
    ParamInput,
    PlannedCall,
    RepoChoice,
)


class FakeChain:
    """Returns successive AgentTurns (last repeats) and records the inputs it was given."""

    def __init__(self, *turns):
        self.turns = list(turns)
        self.calls = 0
        self.last_inputs = None

    def invoke(self, inputs, config=None):
        self.last_inputs = inputs
        turn = self.turns[min(self.calls, len(self.turns) - 1)]
        self.calls += 1
        return turn


def _ctx(spec, settings, repo=None, relations=None):
    # These tests exercise the legacy freeform build→attack flow; the sweep four-substage flow has
    # its own dedicated tests below.
    settings.test.attack_mode = "freeform"
    return StrategyContext(
        spec=spec,
        relations=relations or [],
        field_repo=repo or FieldRepo(),
        settings=settings,
        operations=views.operations(spec),
    )


def _turn(*calls, memory=None, is_done=False):
    return AgentTurn(memory=memory or AgentMemory(), calls=list(calls), is_done=is_done)


def test_repo_choice_resolves_to_harvested_id(recursive_spec, settings):
    repo = FieldRepo()
    repo.add("createCategory", "/properties/id", 42)
    call = PlannedCall(
        op_key="getCategory",
        parameters=[ParamInput(
            name="categoryId", location="path",
            choice=RepoChoice(source_key="createCategory", json_pointer="/properties/id"),
        )],
        rationale="replay id",
    )
    strat = AIStrategy(_ctx(recursive_spec, settings, repo), chain=FakeChain(_turn(call)))

    [action] = strat.plan()
    assert action.op_key == "getCategory"
    assert action.request.path_params["categoryId"] == 42
    assert action.rationale == "replay id"


def test_extra_body_injects_undocumented_field(recursive_spec, settings):
    from bola.strategies.models import ExtraBodyField

    call = PlannedCall(
        op_key="createCategory",
        identity="attacker",
        extra_body=[ExtraBodyField(name="as_org", choice=ConcreteChoice(value="org-victim"))],
        rationale="mass-assignment probe",
    )
    strat = AIStrategy(_ctx(recursive_spec, settings), chain=FakeChain(_turn(call)),
                       start_in_attack=True)
    [action] = strat.plan()
    assert action.request.body == {"as_org": "org-victim"}  # undocumented key reaches the wire
    assert action.request.media_type == "application/json"


def test_concrete_and_generator_choices(recursive_spec, settings):
    call = PlannedCall(
        op_key="createCategory",
        body=[
            BodyInput(media_type="application/json", json_pointer="/properties/name",
                      choice=ConcreteChoice(value="literal")),
        ],
        parameters=[],
        rationale="seed",
    )
    strat = AIStrategy(_ctx(recursive_spec, settings), chain=FakeChain(_turn(call)))
    [action] = strat.plan()
    assert action.request.body == {"name": "literal"}
    assert action.request.media_type == "application/json"

    gen_call = PlannedCall(
        op_key="getCategory",
        parameters=[ParamInput(name="categoryId", location="path",
                               choice=GeneratorChoice(name="integer"))],
        rationale="generate",
    )
    strat2 = AIStrategy(_ctx(recursive_spec, settings), chain=FakeChain(_turn(gen_call)))
    [a2] = strat2.plan()
    assert isinstance(a2.request.path_params["categoryId"], int)


def test_file_choice_becomes_multipart_file_part(recursive_spec, settings):
    # the model authors the file's contents; they ride out as an ApiRequest.files part, not the body
    call = PlannedCall(
        op_key="createCategory",
        body=[BodyInput(
            media_type="multipart/form-data", json_pointer="/properties/numbers",
            choice=FileChoice(content="+14155550101\n+14155550102",
                              filename="numbers.txt", content_type="text/plain"),
        )],
        rationale="upload phone list",
    )
    strat = AIStrategy(_ctx(recursive_spec, settings), chain=FakeChain(_turn(call)))
    [action] = strat.plan()
    assert action.request.files == {
        "numbers": FilePart(content="+14155550101\n+14155550102",
                            filename="numbers.txt", content_type="text/plain")
    }
    assert action.request.body is None
    assert action.request.media_type == "multipart/form-data"


def test_file_and_value_inputs_split_into_files_and_body(recursive_spec, settings):
    # a whole-body file (root pointer) keys as "file"; non-file fields still form the body
    call = PlannedCall(
        op_key="createCategory",
        body=[
            BodyInput(media_type="multipart/form-data", json_pointer="/",
                      choice=FileChoice(content="payload")),
            BodyInput(media_type="multipart/form-data", json_pointer="/properties/name",
                      choice=ConcreteChoice(value="vip")),
        ],
        rationale="file plus a form field",
    )
    strat = AIStrategy(_ctx(recursive_spec, settings), chain=FakeChain(_turn(call)))
    [action] = strat.plan()
    assert action.request.files == {"file": FilePart(content="payload", filename="upload.txt")}
    assert action.request.body == {"name": "vip"}


def test_memory_is_updated_and_is_done_from_turn(recursive_spec, settings):
    mem = AgentMemory(conclusions=["created a category"], plan=["read it back"])
    # is_done is honoured only in the attack stage, so resume directly into it
    strat = AIStrategy(_ctx(recursive_spec, settings),
                       chain=FakeChain(_turn(memory=mem, is_done=True)), start_in_attack=True)
    strat.plan()
    assert strat.memory.conclusions == ["created a category"]
    assert strat.is_done()


def test_finalize_folds_last_results_into_memory(recursive_spec, settings):
    # the attack's results arrive after the last plan(); finalize reflects them into memory
    found = AgentMemory(conclusions=["BOLA: attacker read the owner's object"])
    strat = AIStrategy(_ctx(recursive_spec, settings), chain=FakeChain(_turn(memory=found)))
    res = ExecutionResult(method="GET", url="u", status_code=200, ok=True, response={"id": 1})
    action = PlannedAction(op_key="getCategory", request=ApiRequest(method="GET", path="/x"),
                           rationale="cross", identity=ATTACKER)
    strat.observe([ActionResult(action=action, result=res)])
    assert strat.finalize() is True
    assert strat.memory.conclusions == ["BOLA: attacker read the owner's object"]


def test_finalize_is_noop_without_results(recursive_spec, settings):
    strat = AIStrategy(_ctx(recursive_spec, settings), chain=FakeChain(_turn()))
    assert strat.finalize() is False


def test_is_done_during_build_crosses_to_attack_not_terminates(recursive_spec, settings):
    # A build-stage `is_done` means "done building" — it must cross into attack, never end the run
    # (a run that only built would never test authorization).
    call = PlannedCall(op_key="getCategory", identity="attacker", rationale="x")
    strat = AIStrategy(_ctx(recursive_spec, settings), chain=FakeChain(_turn(call, is_done=True)))
    strat.plan()
    assert strat.stage == "attack"
    assert not strat.is_done()


def test_full_coverage_gate_defers_done_until_all_consumers_attacked(recursive_spec, settings):
    # With the coverage gate on, a model `is_done` in the attack stage is ignored while any
    # relation-consuming operation remains un-attacked; it is honoured once the worklist is empty.
    settings.test.ai_require_full_coverage = True
    settings.test.attack_mode = "freeform"
    repo = FieldRepo()
    repo.add("createCategory", "/properties/id", 7, owner="regular")  # a victim id
    ctx = StrategyContext(
        spec=recursive_spec,
        relations=[{"source_key": "createCategory", "target_key": "getCategory", "edge": {}}],
        field_repo=repo, settings=settings, operations=views.operations(recursive_spec),
    )
    strat = AIStrategy(ctx, chain=FakeChain(_turn(is_done=True)), start_in_attack=True)
    strat.plan()
    assert "getCategory" in json.loads(strat.chain.last_inputs["context"])["unexercised_operations"]
    assert not strat.is_done()  # gated: the consumer op has not been attacked with a victim id yet

    # attacking with the attacker's OWN id does not count
    res = ExecutionResult(method="GET", url="u", status_code=200, ok=True, response={})
    own = PlannedAction(op_key="getCategory",
                        request=ApiRequest(method="GET", path="/x", path_params={"categoryId": 999}),
                        identity=ATTACKER)
    strat.observe([ActionResult(action=own, result=res)])
    assert not strat.is_done()  # own-id attack does not clear coverage

    # attacking with the victim id 7 does count -> worklist empties -> done is honoured
    hit = PlannedAction(op_key="getCategory",
                        request=ApiRequest(method="GET", path="/x", path_params={"categoryId": 7}),
                        identity=ATTACKER)
    strat.observe([ActionResult(action=hit, result=res)])
    strat.plan()
    assert strat.is_done()


def test_creation_plan_counts_mutating_consumers_per_creator(recursive_spec, settings):
    # a POST creator feeding two mutating ops (patch + delete) and one read -> plan asks for 2 instances
    from bola.strategies.ai_strategy import _creation_plan

    class _Op:
        def __init__(self, method): self.method = method
    ops = {"createCategory": _Op("POST"), "patchCategory": _Op("PATCH"),
           "deleteCategory": _Op("DELETE"), "getCategory": _Op("GET")}
    rels = [
        {"source_key": "createCategory", "target_key": "patchCategory", "edge": {}},
        {"source_key": "createCategory", "target_key": "deleteCategory", "edge": {}},
        {"source_key": "createCategory", "target_key": "getCategory", "edge": {}},  # read, not counted
    ]
    [entry] = _creation_plan(rels, ops)
    assert entry["create_op"] == "createCategory"
    assert entry["instances"] == 2
    assert entry["for_operations"] == ["deleteCategory", "patchCategory"]


def test_build_coverage_gate_defers_attack_until_producers_built(recursive_spec, settings):
    # with the coverage gate on, ready_to_attack is ignored while a producer op has harvested no id
    settings.test.ai_require_full_coverage = True
    settings.test.attack_mode = "freeform"
    repo = FieldRepo()
    ctx = StrategyContext(
        spec=recursive_spec,
        relations=[{"source_key": "createCategory", "target_key": "getCategory", "edge": {}}],
        field_repo=repo, settings=settings, operations=views.operations(recursive_spec),
    )
    turn = AgentTurn(calls=[], ready_to_attack=True)
    strat = AIStrategy(ctx, chain=FakeChain(turn))
    strat.plan()
    # createCategory produced nothing yet -> stay in build despite ready_to_attack
    assert strat.stage == "build"
    assert "createCategory" in json.loads(strat.chain.last_inputs["context"])["unbuilt_operations"]

    repo.add("createCategory", "/properties/id", 7, owner="regular")  # now a producer has output
    strat.plan()
    assert strat.stage == "attack"  # pool covers the producer -> crossing allowed


def test_creation_plan_skips_creators_with_one_mutating_op(recursive_spec, settings):
    from bola.strategies.ai_strategy import _creation_plan

    class _Op:
        def __init__(self, method): self.method = method
    ops = {"createCategory": _Op("POST"), "patchCategory": _Op("PATCH")}
    rels = [{"source_key": "createCategory", "target_key": "patchCategory", "edge": {}}]
    assert _creation_plan(rels, ops) == []  # one mutating op needs no extra instances


def test_unknown_op_key_is_skipped(recursive_spec, settings):
    call = PlannedCall(op_key="ghostOp", rationale="bad")
    strat = AIStrategy(_ctx(recursive_spec, settings), chain=FakeChain(_turn(call)))
    assert strat.plan() == []


def test_max_turns_bounds_planning(recursive_spec, settings):
    call = PlannedCall(op_key="getCategory", rationale="x")
    chain = FakeChain(_turn(call))
    strat = AIStrategy(_ctx(recursive_spec, settings), chain=chain, max_turns=1)
    assert len(strat.plan()) == 1
    assert strat.is_done()
    assert strat.plan() == []  # past the turn budget, no further LLM call
    assert chain.calls == 1


def test_build_stage_forces_regular_identity(recursive_spec, settings):
    # even if the model asks for attacker creds, build-stage calls run as the regular user
    call = PlannedCall(op_key="getCategory", identity="attacker", rationale="x")
    strat = AIStrategy(_ctx(recursive_spec, settings), chain=FakeChain(_turn(call)))
    [action] = strat.plan()
    assert strat.stage == "build"
    assert action.identity == REGULAR


def test_ready_to_attack_switches_to_attacker_identity(recursive_spec, settings):
    call = PlannedCall(op_key="getCategory", identity="attacker", rationale="cross")
    turn = AgentTurn(calls=[call], ready_to_attack=True)
    strat = AIStrategy(_ctx(recursive_spec, settings), chain=FakeChain(turn))
    [action] = strat.plan()
    assert strat.stage == "attack"
    assert action.identity == ATTACKER


def test_build_cap_forces_attack_after_n_turns(recursive_spec, settings):
    call = PlannedCall(op_key="getCategory", identity="attacker", rationale="x")
    chain = FakeChain(_turn(call))  # never sets ready_to_attack
    strat = AIStrategy(_ctx(recursive_spec, settings), chain=chain, build_cap=2)
    [a1] = strat.plan()
    assert strat.stage == "build" and a1.identity == REGULAR
    [a2] = strat.plan()
    assert strat.stage == "attack" and a2.identity == ATTACKER  # cap reached → forced to attack


def test_start_in_attack_resumes_in_attack_stage(recursive_spec, settings):
    call = PlannedCall(op_key="getCategory", identity="attacker", rationale="x")
    strat = AIStrategy(_ctx(recursive_spec, settings),
                       chain=FakeChain(_turn(call)), start_in_attack=True)
    [action] = strat.plan()
    assert strat.stage == "attack"
    assert action.identity == ATTACKER


def test_observe_feeds_recent_results_into_next_context(recursive_spec, settings):
    chain = FakeChain(_turn())
    strat = AIStrategy(_ctx(recursive_spec, settings), chain=chain)
    result = ActionResult(
        action=PlannedAction(op_key="createCategory", request=None, rationale="x"),
        result=ExecutionResult(method="POST", url="/categories", status_code=201, ok=True,
                               response={"id": 7}),
    )
    strat.observe([result])
    strat.plan()
    ctx = json.loads(chain.last_inputs["context"])
    assert ctx["recent_results"][0]["op_key"] == "createCategory"
    assert ctx["recent_results"][0]["response"] == {"id": 7}

# ----- sweep four-substage flow -------------------------------------------------------------------

def _rel():
    """createCategory's 201 response id flows into getCategory's path param categoryId."""
    return {
        "source_key": "createCategory", "target_key": "getCategory",
        "edge": {
            "source": {"status_code": "201", "media_type": "application/json",
                       "json_pointer": "/properties/id"},
            "target": {"type": "parameter", "name": "categoryId", "location": "path"},
        },
    }


def _sweep_ctx(spec, settings, repo=None):
    settings.test.attack_mode = "sweep"
    settings.test.creative_turns = 1
    return StrategyContext(
        spec=spec, relations=[_rel()], field_repo=repo or FieldRepo(),
        settings=settings, operations=views.operations(spec),
    )


def test_enumerate_emits_callable_get_frontier_as_regular(recursive_spec, settings):
    repo = FieldRepo()
    repo.add("createCategory", "/properties/id", 7, owner="regular")
    strat = AIStrategy(_sweep_ctx(recursive_spec, settings, repo), chain=FakeChain(_turn()))
    actions = strat.plan()  # enumerate substage: GET getCategory is fillable from the pool
    assert strat.stage == "build"  # enumerate runs under the regular (build) identity
    assert any(a.op_key == "getCategory" and a.identity == REGULAR
               and a.request.method == "GET" and a.request.path_params == {"categoryId": 7}
               for a in actions)


def test_enumerate_finishes_to_build_at_fixpoint(recursive_spec, settings):
    repo = FieldRepo()
    repo.add("createCategory", "/properties/id", 7, owner="regular")
    strat = AIStrategy(_sweep_ctx(recursive_spec, settings, repo), chain=FakeChain(_turn()))
    strat.plan()                 # emits the frontier
    actions = strat.plan()       # nothing new callable -> cross to build
    assert actions == [] and strat._substage == "build"


def test_sweep_worklist_drains_then_creative(recursive_spec, settings):
    repo = FieldRepo()
    repo.add("createCategory", "/properties/id", 7, owner="regular", created=True)
    call = PlannedCall(
        op_key="getCategory", identity="attacker",
        parameters=[ParamInput(name="categoryId", location="path",
                               choice=ConcreteChoice(value=7))],
        rationale="replay victim id",
    )
    strat = AIStrategy(_sweep_ctx(recursive_spec, settings, repo),
                       chain=FakeChain(_turn(call)), start_in_attack=True)
    assert strat._substage == "sweep"
    [action] = strat.plan()                  # sweep builds the attacker call for (getCategory, 7)
    assert action.identity == ATTACKER and action.request.path_params["categoryId"] == 7
    # crediting coverage drains the worklist
    res = ExecutionResult(method="GET", url="u", status_code=200, ok=True, response={})
    strat.observe([ActionResult(action=action, result=res)])
    strat.plan()                             # worklist empty -> creative
    assert strat._substage == "creative"


def test_victim_candidates_rank_created_first(recursive_spec, settings):
    repo = FieldRepo()
    repo.add("createCategory", "/properties/id", "seen", owner="regular")
    repo.add("createCategory", "/properties/id", "made", owner="regular", created=True)
    strat = AIStrategy(_sweep_ctx(recursive_spec, settings, repo),
                       chain=FakeChain(_turn()), start_in_attack=True)
    assert strat._victim_candidates("getCategory")[0] == "made"  # created ranked first


def test_creative_budget_terminates(recursive_spec, settings):
    repo = FieldRepo()
    repo.add("createCategory", "/properties/id", 7, owner="regular")
    settings_local = settings
    ctx = _sweep_ctx(recursive_spec, settings_local, repo)  # creative_turns = 1
    strat = AIStrategy(ctx, chain=FakeChain(_turn()), start_in_attack=True)
    strat._substage = "creative"             # jump straight to the creative tail
    strat.plan()
    assert strat.is_done()                   # one creative turn spent -> done


def test_sweep_orders_reads_before_writes_before_deletes(recursive_spec, settings):
    # GET ranks before POST/PATCH/PUT, which rank before DELETE, so a destructive op never wipes an
    # object a later read/write of the same object still needs.
    repo = FieldRepo()
    repo.add("createCategory", "/properties/id", 7, owner="regular")
    strat = AIStrategy(_sweep_ctx(recursive_spec, settings, repo),
                       chain=FakeChain(_turn()), start_in_attack=True)
    assert strat._method_rank("getCategory") == 0       # GET
    assert strat._method_rank("createCategory") == 1     # POST (non-destructive write)


def test_loop_guard_drops_op_after_max_attempts(recursive_spec, settings):
    # an op repeatedly attacked WITHOUT a victim id is dropped from the worklist after the cap, so it
    # cannot starve the sweep (the stuck-loop that hid the P1-big writes).
    settings.test.attack_max_attempts_per_op = 2
    repo = FieldRepo()
    repo.add("createCategory", "/properties/id", 7, owner="regular")
    ctx = _sweep_ctx(recursive_spec, settings, repo)  # consumer op = getCategory
    strat = AIStrategy(ctx, chain=FakeChain(_turn()), start_in_attack=True)
    assert "getCategory" in strat._unexercised()
    res = ExecutionResult(method="GET", url="u", status_code=400, ok=False, response={})
    own = PlannedAction(op_key="getCategory",
                        request=ApiRequest(method="GET", path="/x", path_params={"categoryId": 999}),
                        identity=ATTACKER)
    strat.observe([ActionResult(action=own, result=res)])
    strat.observe([ActionResult(action=own, result=res)])  # 2 attempts, no victim id
    assert "getCategory" not in strat._unexercised()  # dropped by the loop guard


def test_attack_feedback_surfaces_leaking_read_despite_delete_ordering(recursive_spec, settings):
    # the sweep tests deletes last, so the final batch a reasoning turn sees is the refused delete;
    # _attack_feedback keeps the earlier leaking read at the front so memory can record the crossing.
    strat = AIStrategy(_sweep_ctx(recursive_spec, settings), chain=FakeChain(_turn()),
                       start_in_attack=True)
    read = PlannedAction(op_key="getCategory", request=ApiRequest(method="GET", path="/c"),
                         rationale="read", identity=ATTACKER)
    read_res = ExecutionResult(method="GET", url="u", status_code=200, ok=True,
                               response={"id": 7, "secret": "s"})
    delete = PlannedAction(op_key="deleteCategory", request=ApiRequest(method="DELETE", path="/c"),
                           rationale="del", identity=ATTACKER)
    del_res = ExecutionResult(method="DELETE", url="u", status_code=403, ok=False,
                              response={"detail": "Forbidden"})
    strat.observe([ActionResult(action=read, result=read_res)])
    strat.observe([ActionResult(action=delete, result=del_res)])  # last batch = refused delete only
    feedback = strat._attack_feedback()
    assert feedback[0].result.response == {"id": 7, "secret": "s"}  # the leak leads the feedback


def test_sweep_turn_does_not_clobber_reasoning_memory(recursive_spec, settings):
    # the mechanical attack-fill turn is blind to responses; it must not overwrite the memory the
    # build phase accumulated (the creative tail / finalize fold results in instead).
    repo = FieldRepo()
    repo.add("createCategory", "/properties/id", 7, owner="regular", created=True)
    call = PlannedCall(op_key="getCategory", identity="attacker",
                       parameters=[ParamInput(name="categoryId", location="path",
                                              choice=ConcreteChoice(value=7))], rationale="x")
    strat = AIStrategy(_sweep_ctx(recursive_spec, settings, repo),
                       chain=FakeChain(_turn(call)), start_in_attack=True)
    strat.memory = AgentMemory(conclusions=["built a category as victim"])
    strat.plan()  # sweep attack-fill turn — FakeChain returns an empty memory we must NOT adopt
    assert strat.memory.conclusions == ["built a category as victim"]


def test_revise_returns_corrected_action(recursive_spec, settings):
    # revise asks the chain for a fixed call and returns an action keeping the same op + identity
    fixed = PlannedCall(
        op_key="createCategory", identity="attacker",
        body=[BodyInput(media_type="application/json", json_pointer="/properties/name",
                        choice=ConcreteChoice(value="12345"))],
        rationale="fix zip",
    )
    strat = AIStrategy(_sweep_ctx(recursive_spec, settings), chain=FakeChain(_turn(fixed)),
                       start_in_attack=True)
    bad = PlannedAction(op_key="createCategory",
                        request=ApiRequest(method="POST", path="/categories", body={"name": "bad"}),
                        identity="attacker", rationale="orig")
    res = ExecutionResult(method="POST", url="u", status_code=400, ok=False,
                          response={"message": "zip len failed"})
    revised = strat.revise(bad, res)
    assert revised is not None and revised.op_key == "createCategory"
    assert revised.identity == "attacker"
    assert revised.request.body == {"name": "12345"}

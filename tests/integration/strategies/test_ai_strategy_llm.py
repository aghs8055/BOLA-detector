"""Real-LLM AI strategy — costs money, excluded by default.

Run explicitly with:  pytest -m llm tests/integration/strategies
Requires OPENAI_API_KEY (and OPENAI_BASE_URL for a gateway) in the environment / .env.
"""

import pytest
from pydantic import BaseModel, Field

from bola.execution.field_repo import FieldRepo
from bola.llm import build_chat_model
from bola.relations.models import EdgeSource, EdgeTargetParameter, Relation, RelationEdge
from bola.settings import load_settings
from bola.spec import load_spec_from_dict, views
from bola.strategies.ai_strategy import AIStrategy
from bola.strategies.base import StrategyContext

pytestmark = pytest.mark.llm


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


@pytest.fixture(scope="module")
def spec():
    doc = {
        "openapi": "3.0.3",
        "info": {"title": "widgets", "version": "1.0.0"},
        "paths": {
            "/widgets": {
                "post": {
                    "operationId": "createWidget",
                    "summary": "Create a widget",
                    "requestBody": {"content": {"application/json": {"schema": {
                        "type": "object", "properties": {"name": {"type": "string"}}}}}},
                    "responses": {"201": {"description": "created", "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Widget"}}}}},
                }
            },
            "/widgets/{widgetId}": {
                "get": {
                    "operationId": "getWidget",
                    "summary": "Read a widget",
                    "parameters": [{"name": "widgetId", "in": "path", "required": True,
                                    "schema": {"type": "integer"}}],
                    "responses": {"200": {"description": "ok", "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Widget"}}}}},
                }
            },
        },
        "components": {"schemas": {"Widget": {"type": "object", "properties": {
            "id": {"type": "integer"}, "name": {"type": "string"}}}}},
    }
    return load_spec_from_dict(doc, source="<widgets>")


def _relation():
    edge = RelationEdge(
        source=EdgeSource(status_code="201", media_type="application/json",
                          json_pointer="/properties/id"),
        target=EdgeTargetParameter(name="widgetId", location="path"),
        cast=None,
        rationale="created widget id selects the widget to read",
    )
    return Relation(source_key="createWidget", target_key="getWidget", edge=edge).model_dump()


class Judgement(BaseModel):
    plausible: bool = Field(description="Is the planned call sequence a sensible exploration?")
    reason: str


def test_ai_strategy_replays_harvested_id(spec, settings):
    """With an id already harvested and a relation pointing at it, the model should replay it."""
    repo = FieldRepo()
    repo.add("createWidget", "/properties/id", 123)
    ctx = StrategyContext(spec=spec, relations=[_relation()], field_repo=repo,
                          settings=settings, operations=views.operations(spec))
    strat = AIStrategy(ctx, max_turns=1)

    actions = strat.plan()
    assert actions, "expected at least one planned call"
    op_keys = {a.op_key for a in actions}
    assert op_keys <= {"createWidget", "getWidget"}, f"invented op_key: {op_keys}"
    # the harvested id (123) should be replayed into getWidget's path param
    read = next((a for a in actions if a.op_key == "getWidget"), None)
    if read is not None:
        assert read.request.path_params.get("widgetId") == 123


def test_ai_selects_value_from_opaque_enum(settings):
    """A required input whose only valid values are an opaque enum must be filled from that enum.

    The values are arbitrary codes and the parameter name gives no hint, so a model guessing from
    type/name alone cannot produce a valid one — passing proves it read the `constraints.enum`.
    """
    enum = ["qx7", "lp2", "zzk9"]
    doc = {
        "openapi": "3.0.3",
        "info": {"title": "things", "version": "1.0.0"},
        "paths": {
            "/things": {
                "post": {
                    "operationId": "createThing",
                    "summary": "Create a thing",
                    "parameters": [{
                        "name": "category", "in": "query", "required": True,
                        "schema": {"type": "string", "enum": enum},
                    }],
                    "responses": {"201": {"description": "created"}},
                }
            }
        },
    }
    spec = load_spec_from_dict(doc, source="<things>")
    ctx = StrategyContext(spec=spec, relations=[], field_repo=FieldRepo(),
                          settings=settings, operations=views.operations(spec))
    strat = AIStrategy(ctx, max_turns=2)

    # The default sweep flow opens with a deterministic, no-LLM `enumerate` substage; with no GET to
    # enumerate it yields nothing and advances to the build substage, where the create is planned. Drive
    # `plan()` until that first LLM turn produces the create (bounded — enumerate doesn't spend a turn).
    create = None
    for _ in range(3):
        create = next((a for a in strat.plan() if a.op_key == "createThing"), None)
        if create is not None:
            break
    assert create is not None, "model did not plan the only operation"
    value = create.request.query.get("category")
    assert value in enum, f"required enum param not filled from the enum: {value!r}"


def test_llm_judge_rates_plan_plausible(spec, settings):
    ctx = StrategyContext(spec=spec, relations=[_relation()], field_repo=FieldRepo(),
                          settings=settings, operations=views.operations(spec))
    strat = AIStrategy(ctx, max_turns=1)
    actions = strat.plan()

    judge = build_chat_model(settings).with_structured_output(Judgement)
    verdict = judge.invoke(
        "You audit an API exploration agent doing authorization testing. The API has "
        "createWidget (POST, returns a widget with an id) and getWidget (GET by id path param). "
        "The agent planned these calls:\n"
        + "\n".join(f"- {a.op_key}: {a.rationale}" for a in actions)
        + "\nIs this a sensible exploration sequence for the goal of obtaining an id and "
          "replaying it? Reject only if the plan is clearly nonsensical."
    )
    assert verdict.plausible, verdict.reason
"""Unit tests for `bola.strategies.random_strategy`."""

import random

from bola.execution.field_repo import FieldRepo
from bola.relations.models import EdgeSource, EdgeTargetParameter, Relation, RelationEdge
from bola.spec import views
from bola.strategies.base import StrategyContext
from bola.strategies.random_strategy import RandomStrategy


def _op(spec, key):
    return next(o for o in views.operations(spec) if o.key == key)


def _id_relation():
    edge = RelationEdge(
        source=EdgeSource(status_code="201", media_type="application/json",
                          json_pointer="/properties/id"),
        target=EdgeTargetParameter(name="categoryId", location="path"),
        cast=None,
        rationale="id carry-over",
    )
    return Relation(source_key="createCategory", target_key="getCategory", edge=edge).model_dump()


def _ctx(spec, settings, operations, repo=None, relations=None):
    return StrategyContext(
        spec=spec,
        relations=relations or [],
        field_repo=repo or FieldRepo(),
        settings=settings,
        operations=operations,
    )


def test_path_param_filled_from_harvested_candidate(recursive_spec, settings):
    repo = FieldRepo()
    repo.add("createCategory", "/properties/id", 42)
    ctx = _ctx(recursive_spec, settings,
               [_op(recursive_spec, "getCategory")], repo, [_id_relation()])
    strat = RandomStrategy(ctx, max_actions=1, rng=random.Random(0))

    [action] = strat.plan()
    assert action.op_key == "getCategory"
    assert action.request.path_params["categoryId"] == 42  # candidate used, not a generator


def test_path_param_generated_when_no_candidate(recursive_spec, settings):
    ctx = _ctx(recursive_spec, settings, [_op(recursive_spec, "getCategory")])
    strat = RandomStrategy(ctx, max_actions=1, rng=random.Random(0))

    [action] = strat.plan()
    # no relation/repo -> categoryId is generated from its int schema
    assert isinstance(action.request.path_params["categoryId"], int)


def test_body_is_built_from_leaf_fields(recursive_spec, settings):
    ctx = _ctx(recursive_spec, settings, [_op(recursive_spec, "createCategory")])
    strat = RandomStrategy(ctx, max_actions=1, rng=random.Random(0))

    [action] = strat.plan()
    assert action.request.media_type == "application/json"
    assert isinstance(action.request.body, dict)
    assert "name" in action.request.body  # the one leaf field of the create body


def test_budget_and_is_done(recursive_spec, settings):
    ops = views.operations(recursive_spec)
    ctx = _ctx(recursive_spec, settings, ops)
    strat = RandomStrategy(ctx, max_actions=3, batch_size=2, rng=random.Random(0))

    first = strat.plan()
    assert len(first) == 2 and not strat.is_done()
    second = strat.plan()
    assert len(second) == 1  # budget caps the final batch at 1
    assert strat.is_done()
    assert strat.plan() == []  # nothing past the budget


def test_no_operations_is_immediately_done(recursive_spec, settings):
    strat = RandomStrategy(_ctx(recursive_spec, settings, []))
    assert strat.is_done()
    assert strat.plan() == []
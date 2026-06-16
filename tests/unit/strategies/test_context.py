"""Unit tests for `bola.strategies.context`."""

from bola.execution.field_repo import FieldRepo
from bola.spec import load_spec_from_dict, views
from bola.strategies.base import StrategyContext
from bola.strategies.context import build_turn_context
from bola.strategies.models import AgentMemory


def _spec():
    return load_spec_from_dict(
        {
            "openapi": "3.0.3",
            "info": {"title": "t", "version": "1"},
            "paths": {
                "/items": {
                    "post": {
                        "operationId": "createItem",
                        "parameters": [
                            {
                                "name": "status",
                                "in": "query",
                                "required": True,
                                "schema": {"type": "string", "enum": ["active", "archived"]},
                            }
                        ],
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "code": {"type": "string", "pattern": "^[A-Z]{3}$"},
                                            "name": {"type": "string"},
                                        },
                                    }
                                }
                            }
                        },
                        "responses": {"201": {"description": "ok"}},
                    }
                }
            },
        }
    )


def test_operation_view_surfaces_constraints_and_omits_empty(settings):
    spec = _spec()
    ops = views.operations(spec)
    ctx = StrategyContext(
        spec=spec, relations=[], field_repo=FieldRepo(), settings=settings, operations=ops
    )
    doc = build_turn_context(ctx, AgentMemory(), [])
    op = doc["operations"][0]

    status = next(p for p in op["parameters"] if p["name"] == "status")
    assert status["constraints"] == {"enum": ["active", "archived"]}

    fields = op["request_body"][0]["fields"]
    code = next(f for f in fields if f["json_pointer"] == "/properties/code")
    assert code["constraints"] == {"pattern": "^[A-Z]{3}$"}
    name = next(f for f in fields if f["json_pointer"] == "/properties/name")
    assert "constraints" not in name  # empty constraints are omitted from the prompt


def test_operator_notes_emitted_only_when_access_set(settings):
    spec = _spec()
    ops = views.operations(spec)

    with_access = StrategyContext(
        spec=spec, relations=[], field_repo=FieldRepo(), settings=settings,
        operations=ops, access="Account id 50 is out of scope; never call it.",
    )
    doc = build_turn_context(with_access, AgentMemory(), [])
    assert doc["operator_notes"] == "Account id 50 is out of scope; never call it."

    without_access = StrategyContext(
        spec=spec, relations=[], field_repo=FieldRepo(), settings=settings, operations=ops
    )
    assert "operator_notes" not in build_turn_context(without_access, AgentMemory(), [])

def test_compact_relations_shrinks_edges_to_flow_pairs():
    from bola.strategies.context import _compact_relations
    rels = [
        {"source_key": "createX", "target_key": "getX",
         "edge": {"source": {"status_code": "201", "media_type": "application/json",
                             "json_pointer": "/properties/id"},
                  "target": {"type": "parameter", "name": "xId", "location": "path"}}},
        # a duplicate edge (same flow) collapses
        {"source_key": "createX", "target_key": "getX",
         "edge": {"source": {"status_code": "200", "media_type": "application/json",
                             "json_pointer": "/properties/id"},
                  "target": {"type": "parameter", "name": "xId", "location": "path"}}},
        {"source_key": "createX", "target_key": "updateX",
         "edge": {"source": {"status_code": "201", "media_type": "application/json",
                             "json_pointer": "/properties/id"},
                  "target": {"type": "request_body", "media_type": "application/json",
                             "json_pointer": "/properties/parentId"}}},
    ]
    out = _compact_relations(rels)
    assert {"from": "createX", "to": "getX", "into": "xId", "in": "path"} in out
    assert any(e["to"] == "updateX" and e["into"] == "/properties/parentId" for e in out)
    assert len(out) == 2  # the duplicate getX edge is de-duplicated
    # the verbose source coordinates are gone (token-light)
    assert all("edge" not in e and "status_code" not in e for e in out)

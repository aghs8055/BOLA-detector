"""Unit tests for bola.spec.loader.

Focus: the loaded object losslessly represents the whole document, refs resolve both ways,
cycles are handled without hanging/truncation, and load errors are clear and typed.
All network is mocked (responses); no real HTTP.
"""

from pathlib import Path

import pytest
import requests
import responses

from bola.spec import (
    ReferenceResolutionError,
    Spec,
    SpecLoadError,
    SpecParseError,
    SpecValidationError,
    load_spec,
    load_spec_from_dict,
)

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "petstore_3.0.json"


# --------------------------------------------------------------------------------------
# crafted 3.1 spec exercising the constructs a flat model would have destroyed
# --------------------------------------------------------------------------------------


def _spec_31() -> dict:
    return {
        "openapi": "3.1.0",
        "info": {"title": "Crafted", "version": "1.0", "x-team": "sec"},
        "servers": [{"url": "https://api.example.com/{ver}", "variables": {"ver": {"default": "v1"}}}],
        "webhooks": {
            "ping": {
                "post": {
                    "requestBody": {
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Node"}}}
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
        "paths": {
            "/shapes": {
                "get": {
                    "operationId": "listShapes",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Shape"}}},
                        }
                    },
                }
            }
        },
        "components": {
            "schemas": {
                # recursive / circular schema: Node -> children[] -> Node
                "Node": {
                    "type": "object",
                    "properties": {
                        "name": {"type": ["string", "null"]},  # 3.1 type-as-list nullability
                        "children": {"type": "array", "items": {"$ref": "#/components/schemas/Node"}},
                    },
                },
                "Cat": {"type": "object", "properties": {"petType": {"const": "cat"}}},
                "Dog": {"type": "object", "properties": {"petType": {"const": "dog"}}},
                # union + discriminator
                "Shape": {
                    "oneOf": [
                        {"$ref": "#/components/schemas/Cat"},
                        {"$ref": "#/components/schemas/Dog"},
                    ],
                    "anyOf": [{"type": "object"}],
                    "discriminator": {
                        "propertyName": "petType",
                        "mapping": {
                            "cat": "#/components/schemas/Cat",
                            "dog": "#/components/schemas/Dog",
                        },
                    },
                },
            }
        },
    }


# --------------------------------------------------------------------------------------
# loading: sources, parsing, errors
# --------------------------------------------------------------------------------------


def test_load_real_petstore_from_file():
    spec = load_spec(str(FIXTURE))
    assert isinstance(spec, Spec)
    assert spec.version == "3.0.4"
    assert spec.is_3_1 is False
    assert "/pet/{petId}" in spec.model.paths
    # raw document is kept intact
    assert spec.raw["openapi"] == "3.0.4"
    assert spec.raw is not spec.model  # raw is the dict, model is the typed object


def test_load_yaml_file(tmp_path):
    p = tmp_path / "spec.yaml"
    p.write_text(
        "openapi: 3.0.0\n"
        "info:\n  title: Y\n  version: '1.0'\n"
        "paths:\n  /x:\n    get:\n      responses:\n        '200':\n          description: ok\n"
    )
    spec = load_spec(str(p))
    assert spec.version == "3.0.0"
    assert "/x" in spec.model.paths


@responses.activate
def test_load_from_url():
    url = "https://example.com/openapi.json"
    responses.add(responses.GET, url, body=FIXTURE.read_text(), status=200)
    spec = load_spec(url)
    assert spec.version == "3.0.4"
    assert spec.source == url


@responses.activate
def test_url_http_error_raises_load_error():
    url = "https://example.com/missing.json"
    responses.add(responses.GET, url, status=404)
    with pytest.raises(SpecLoadError):
        load_spec(url)


@responses.activate
def test_url_connection_error_raises_load_error():
    url = "https://example.com/boom.json"
    responses.add(responses.GET, url, body=requests.exceptions.ConnectionError("down"))
    with pytest.raises(SpecLoadError):
        load_spec(url)


def test_missing_file_raises_load_error(tmp_path):
    with pytest.raises(SpecLoadError):
        load_spec(str(tmp_path / "nope.json"))


def test_unparseable_raises_parse_error(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ this is : not json or yaml : ][")
    with pytest.raises(SpecParseError):
        load_spec(str(p))


def test_non_object_root_raises_parse_error(tmp_path):
    p = tmp_path / "list.json"
    p.write_text("[1, 2, 3]")
    with pytest.raises(SpecParseError):
        load_spec(str(p))


def test_missing_version_raises_validation_error():
    with pytest.raises(SpecValidationError):
        load_spec_from_dict({"info": {"title": "x", "version": "1"}, "paths": {}})


def test_unsupported_version_raises_validation_error():
    with pytest.raises(SpecValidationError):
        load_spec_from_dict({"openapi": "2.0", "info": {"title": "x", "version": "1"}})


def test_invalid_structure_raises_validation_error():
    # 'paths' must be an object of path-items, not a string
    with pytest.raises(SpecValidationError):
        load_spec_from_dict({"openapi": "3.0.0", "info": {"title": "x", "version": "1"}, "paths": "nope"})


# --------------------------------------------------------------------------------------
# losslessness: nothing collapsed/merged/dropped
# --------------------------------------------------------------------------------------


def test_refs_kept_as_references_not_inlined():
    spec = load_spec(str(FIXTURE))
    pet = spec.model.components.schemas["Pet"]
    category = pet.properties["category"]
    # a flattening loader would have inlined this; it must remain a Reference
    assert hasattr(category, "ref")
    assert category.ref == "#/components/schemas/Category"


def test_union_branches_all_present_and_discriminator_preserved():
    spec = load_spec_from_dict(_spec_31())
    shape = spec.model.components.schemas["Shape"]
    assert len(shape.oneOf) == 2  # both branches kept, not just the first
    assert len(shape.anyOf) == 1
    assert shape.discriminator.propertyName == "petType"
    assert shape.discriminator.mapping == {
        "cat": "#/components/schemas/Cat",
        "dog": "#/components/schemas/Dog",
    }


def test_type_as_list_nullability_preserved():
    spec = load_spec_from_dict(_spec_31())
    name = spec.model.components.schemas["Node"].properties["name"]
    assert [str(t.value) for t in name.type] == ["string", "null"]


def test_const_preserved():
    spec = load_spec_from_dict(_spec_31())
    assert spec.model.components.schemas["Cat"].properties["petType"].const == "cat"


def test_webhooks_preserved_31():
    spec = load_spec_from_dict(_spec_31())
    assert "ping" in spec.model.webhooks


def test_extensions_preserved():
    spec = load_spec_from_dict(_spec_31())
    # x-* extensions land in model_extra (extra='allow')
    assert (spec.model.info.model_extra or {}).get("x-team") == "sec"
    # ...and are trivially intact in the raw document
    assert spec.raw["info"]["x-team"] == "sec"


def test_server_variables_preserved():
    spec = load_spec_from_dict(_spec_31())
    server = spec.model.servers[0]
    assert server.variables["ver"].default == "v1"


# --------------------------------------------------------------------------------------
# reference resolution: both directions, cycle-safe, error cases
# --------------------------------------------------------------------------------------


def test_resolve_ref_returns_live_node():
    spec = load_spec(str(FIXTURE))
    pet = spec.model.components.schemas["Pet"]
    category = spec.resolve_ref(pet.properties["category"])
    assert "name" in category.properties
    # resolves to the same instance stored in the graph (not a copy)
    assert category is spec.model.components.schemas["Category"]


def test_resolve_ref_accepts_string():
    spec = load_spec(str(FIXTURE))
    node = spec.resolve_ref("#/components/schemas/Order")
    assert node is spec.model.components.schemas["Order"]


def test_resolve_ref_is_memoized_same_instance():
    spec = load_spec(str(FIXTURE))
    a = spec.resolve_ref("#/components/schemas/Pet")
    b = spec.resolve_ref("#/components/schemas/Pet")
    assert a is b


def test_resolve_ref_escaped_json_pointer():
    spec = load_spec(str(FIXTURE))
    # ~1 decodes to '/', so this points at the "/pet/{petId}" path item
    path_item = spec.resolve_ref("#/paths/~1pet~1{petId}")
    assert path_item.get.operationId == "getPetById"


def test_recursive_ref_resolves_without_hanging():
    spec = load_spec_from_dict(_spec_31())
    node = spec.model.components.schemas["Node"]
    current = node
    # follow the cycle many times; it must terminate and keep returning the same node,
    # proving nothing is truncated and nothing loops forever
    for _ in range(1000):
        current = spec.resolve_ref(current.properties["children"].items)
    assert current is node


def test_resolve_ref_across_webhook_into_recursive_schema():
    spec = load_spec_from_dict(_spec_31())
    media = spec.model.webhooks["ping"].post.requestBody.content["application/json"]
    target = spec.resolve_ref(media.media_type_schema)
    assert target is spec.model.components.schemas["Node"]


def test_dangling_ref_raises():
    spec = load_spec(str(FIXTURE))
    with pytest.raises(ReferenceResolutionError):
        spec.resolve_ref("#/components/schemas/DoesNotExist")


def test_external_ref_raises_clearly():
    spec = load_spec(str(FIXTURE))
    with pytest.raises(ReferenceResolutionError) as ei:
        spec.resolve_ref("definitions.json#/Pet")
    assert "External" in str(ei.value)


def test_non_pointer_fragment_raises():
    spec = load_spec(str(FIXTURE))
    with pytest.raises(ReferenceResolutionError):
        spec.resolve_ref("#bare-name")


def test_ref_only_cycle_is_guarded():
    # A -> B -> A, a malformed pure-reference cycle; must error, not hang
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "x", "version": "1"},
        "paths": {},
        "components": {
            "schemas": {
                "A": {"$ref": "#/components/schemas/B"},
                "B": {"$ref": "#/components/schemas/A"},
            }
        },
    }
    spec = load_spec_from_dict(doc)
    with pytest.raises(ReferenceResolutionError):
        spec.resolve_ref("#/components/schemas/A")
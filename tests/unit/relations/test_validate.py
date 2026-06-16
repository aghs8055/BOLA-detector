"""Unit tests for `bola.relations.validate`."""

from bola.relations.models import (
    EdgeSource,
    EdgeTargetParameter,
    EdgeTargetRequestBody,
    RelationEdge,
)
from bola.relations.validate import validate_edge
from bola.spec import views


def _op(spec, key):
    return next(o for o in views.operations(spec) if o.key == key)


def _edge(pointer="/properties/id", name="categoryId", location="path", cast=None,
          status="201", media="application/json"):
    return RelationEdge(
        source=EdgeSource(status_code=status, media_type=media, json_pointer=pointer),
        target=EdgeTargetParameter(name=name, location=location),
        cast=cast,
        rationale="x",
    )


def test_valid_id_to_path_param(recursive_spec):
    src = _op(recursive_spec, "createCategory")
    tgt = _op(recursive_spec, "getCategory")
    assert validate_edge(recursive_spec, src, tgt, _edge()) == []


def test_missing_source_pointer(recursive_spec):
    src = _op(recursive_spec, "createCategory")
    tgt = _op(recursive_spec, "getCategory")
    errs = validate_edge(recursive_spec, src, tgt, _edge(pointer="/properties/ghost"))
    assert any("does not exist" in e for e in errs)


def test_missing_target_parameter(recursive_spec):
    src = _op(recursive_spec, "createCategory")
    tgt = _op(recursive_spec, "getCategory")
    errs = validate_edge(recursive_spec, src, tgt, _edge(name="wrongId"))
    assert any("not found" in e for e in errs)


def test_type_mismatch_without_cast(petstore_spec):
    # source string `status` -> integer petId param, no cast
    src = _op(petstore_spec, "addPet")
    tgt = _op(petstore_spec, "getPetById")
    edge = RelationEdge(
        source=EdgeSource(status_code="200", media_type="application/json",
                          json_pointer="/properties/status"),
        target=EdgeTargetParameter(name="petId", location="path"),
        cast=None,
        rationale="x",
    )
    errs = validate_edge(petstore_spec, src, tgt, edge)
    assert any("without cast" in e for e in errs)


def test_type_mismatch_with_valid_cast(petstore_spec):
    src = _op(petstore_spec, "addPet")
    tgt = _op(petstore_spec, "getPetById")
    edge = RelationEdge(
        source=EdgeSource(status_code="200", media_type="application/json",
                          json_pointer="/properties/status"),
        target=EdgeTargetParameter(name="petId", location="path"),
        cast="to_integer",
        rationale="x",
    )
    assert validate_edge(petstore_spec, src, tgt, edge) == []


def test_wrong_cast_rejected(petstore_spec):
    src = _op(petstore_spec, "addPet")
    tgt = _op(petstore_spec, "getPetById")
    edge = RelationEdge(
        source=EdgeSource(status_code="200", media_type="application/json",
                          json_pointer="/properties/status"),
        target=EdgeTargetParameter(name="petId", location="path"),
        cast="to_string",  # target is integer, to_string can't produce it
        rationale="x",
    )
    errs = validate_edge(petstore_spec, src, tgt, edge)
    assert any("cannot produce target type" in e for e in errs)


def test_request_body_target(petstore_spec):
    # updatePet body has /properties/id (int64); addPet returns /properties/id (int64)
    src = _op(petstore_spec, "addPet")
    tgt = _op(petstore_spec, "updatePet")
    edge = RelationEdge(
        source=EdgeSource(status_code="200", media_type="application/json",
                          json_pointer="/properties/id"),
        target=EdgeTargetRequestBody(media_type="application/json", json_pointer="/properties/id"),
        cast=None,
        rationale="x",
    )
    assert validate_edge(petstore_spec, src, tgt, edge) == []
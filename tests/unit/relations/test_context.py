"""Unit tests for `bola.relations.context`."""

from bola.relations.context import build_group_context
from bola.spec import views


def _op(spec, key):
    return next(o for o in views.operations(spec) if o.key == key)


def test_context_has_source_and_targets(recursive_spec):
    src = _op(recursive_spec, "createCategory")
    tgt = _op(recursive_spec, "getCategory")
    ctx = build_group_context(recursive_spec, src, [tgt])
    assert ctx["source"]["key"] == "createCategory"
    assert [t["key"] for t in ctx["targets"]] == ["getCategory"]


def test_source_responses_carry_pointers(recursive_spec):
    src = _op(recursive_spec, "createCategory")
    ctx = build_group_context(recursive_spec, src, [])
    resp = ctx["source"]["responses"][0]
    pointers = {f["json_pointer"] for f in resp["fields"]}
    assert "/properties/id" in pointers


def test_target_exposes_parameters(recursive_spec):
    src = _op(recursive_spec, "createCategory")
    tgt = _op(recursive_spec, "getCategory")
    ctx = build_group_context(recursive_spec, src, [tgt])
    params = ctx["targets"][0]["parameters"]
    assert params[0]["name"] == "categoryId"
    assert params[0]["in"] == "path"


def test_target_exposes_request_body(recursive_spec):
    src = _op(recursive_spec, "getCategory")
    tgt = _op(recursive_spec, "createCategory")
    ctx = build_group_context(recursive_spec, src, [tgt])
    body = ctx["targets"][0]["request_body"]
    assert body[0]["media_type"] == "application/json"
    assert {f["json_pointer"] for f in body[0]["fields"]} == {"/properties/name"}
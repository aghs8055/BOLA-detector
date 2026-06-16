"""Unit tests for `bola.generators.values`."""

import uuid

from bola.generators import values


def test_generate_by_type():
    assert isinstance(values.generate_for_schema("integer"), int)
    assert isinstance(values.generate_for_schema("number"), float)
    assert isinstance(values.generate_for_schema("boolean"), bool)
    assert isinstance(values.generate_for_schema("string"), str)


def test_format_takes_precedence_over_type():
    assert "@" in values.generate_for_schema("string", "email")
    # a valid uuid
    uuid.UUID(values.generate_for_schema("string", "uuid"))
    assert values.generate_for_schema("string", "uri").startswith("https://")


def test_array_and_object_defaults():
    assert values.generate_for_schema("array") == []
    assert values.generate_for_schema("object") == {}


def test_unknown_type_falls_back_to_string():
    assert isinstance(values.generate_for_schema("weird"), str)
    assert isinstance(values.generate_for_schema(None), str)


def test_get_and_describe_all():
    assert values.get("uuid") is not None
    assert values.get("nope") is None
    described = values.describe_all()
    names = {d["name"] for d in described}
    assert {"email", "uuid", "integer"} <= names
    assert all("description" in d for d in described)
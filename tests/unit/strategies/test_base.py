"""Unit tests for `bola.strategies.base`."""

from bola.execution.field_repo import FieldRepo
from bola.strategies.base import (
    build_body,
    generate_for_field,
    resolve_generator,
    resolve_repo,
)


def test_build_body_simple_properties():
    body = build_body([("/properties/id", 5), ("/properties/name", "x")])
    assert body == {"id": 5, "name": "x"}


def test_build_body_nested_properties_merge_siblings():
    body = build_body(
        [("/properties/user/properties/id", 1), ("/properties/user/properties/name", "n")]
    )
    assert body == {"user": {"id": 1, "name": "n"}}


def test_build_body_items_and_additional_properties():
    assert build_body([("/properties/tags/items", "a")]) == {"tags": ["a"]}
    assert build_body([("/properties/meta/additionalProperties", 1)]) == {"meta": {"key": 1}}


def test_resolve_repo_returns_value_and_handles_bad_index():
    repo = FieldRepo()
    repo.add("createX", "/properties/id", 7)
    repo.add("createX", "/properties/id", 9)
    assert resolve_repo(repo, "createX", "/properties/id", 1) == 9
    assert resolve_repo(repo, "createX", "/properties/id", 99) == 7  # out of range -> first
    assert resolve_repo(repo, "createX", "/properties/missing") is None


def test_resolve_generator_known_unknown_and_extra_args_ignored():
    assert isinstance(resolve_generator("integer"), int)
    assert resolve_generator("does-not-exist") is None
    # generators take no params; passing args must not raise, just call with none
    assert isinstance(resolve_generator("integer", {"min": 1}), int)


def test_generate_for_field_format_wins():
    assert "@" in generate_for_field("string", "email")
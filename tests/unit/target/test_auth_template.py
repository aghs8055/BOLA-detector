"""Unit tests for bola.target.auth_template — the {{...}} substitution language for auth."""

import base64

import pytest

from bola.target.auth_template import TemplateError, references_credential, render


def test_substitutes_bare_variables():
    out = render('{"email":"{{email}}","pw":"{{password}}"}', {"email": "a@b.c", "password": "pw"})
    assert out == '{"email":"a@b.c","pw":"pw"}'


def test_whitespace_inside_hole_is_ignored():
    assert render("{{  email  }}", {"email": "x"}) == "x"


def test_credential_is_just_a_variable():
    assert render("Bearer {{credential}}", {"credential": "tok123"}) == "Bearer tok123"


def test_base64_function():
    out = render("{{base64(token)}}", {"token": "secret"})
    assert out == base64.b64encode(b"secret").decode()


def test_basic_function_joins_with_colon():
    out = render("Basic {{basic(user, password)}}", {"user": "alice", "password": "pw"})
    assert out == "Basic " + base64.b64encode(b"alice:pw").decode()


def test_unknown_variable_raises():
    with pytest.raises(TemplateError):
        render("{{missing}}", {"email": "x"})


def test_unknown_function_raises():
    with pytest.raises(TemplateError):
        render("{{md5(token)}}", {"token": "x"})


def test_wrong_arity_raises():
    with pytest.raises(TemplateError):
        render("{{basic(only_one)}}", {"only_one": "x"})


def test_plain_text_passthrough():
    assert render("no holes here", {}) == "no holes here"


def test_references_credential_detects_bare_use():
    assert references_credential("Bearer {{credential}}") is True
    assert references_credential("Bearer {{ credential }}") is True


def test_references_credential_false_for_other_vars():
    assert references_credential("Basic {{basic(email, password)}}") is False
    assert references_credential("no holes") is False
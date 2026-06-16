"""Unit tests for bola.target.manifest.

Focus: a well-formed `make describe` manifest parses into the typed model; everything that
violates the contract (bad version, non-http base_url, url-spec without http, incoherent auth
pieces, missing/empty users, unknown keys) is rejected with a typed error. Auth is the three
template-driven pieces — login / extract / inject — so the cases here are about their coherence,
not a fixed set of auth "types".
"""

import json

import pytest

from bola.target import (
    ExtractFrom,
    HttpMethod,
    InjectInto,
    ManifestParseError,
    ManifestReadError,
    ManifestValidationError,
    SpecKind,
    TargetManifest,
    load_manifest,
    load_manifest_from_dict,
    load_manifest_from_json,
)


def _valid() -> dict:
    return {
        "version": 1,
        "base_url": "http://localhost:3000",
        "spec": {"kind": "url", "value": "http://localhost:3000/api-docs/swagger.json"},
        "auth": {
            "login": {
                "method": "POST",
                "path": "/rest/user/login",
                "headers": {"Content-Type": "application/json"},
                "body": '{"email":"{{email}}","password":"{{password}}"}',
            },
            "extract": {"from": "body", "path": "authentication.token"},
            "inject": {"into": "header", "name": "Authorization", "value": "Bearer {{credential}}"},
        },
        "users": {
            "regular": {"vars": {"email": "user@target.test", "password": "regular-pass"}},
            "attacker": {"vars": {"email": "attacker@target.test", "password": "attacker-pass"}},
        },
    }


# --------------------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------------------


def test_valid_manifest_parses():
    m = load_manifest_from_dict(_valid())
    assert isinstance(m, TargetManifest)
    assert m.base_url == "http://localhost:3000"
    assert m.spec.kind is SpecKind.url
    assert m.auth.login is not None
    assert m.auth.login.method is HttpMethod.POST
    assert m.auth.login.path == "/rest/user/login"
    assert m.auth.extract is not None
    assert m.auth.extract.source is ExtractFrom.body
    assert m.auth.extract.path == "authentication.token"
    assert m.auth.inject is not None
    assert m.auth.inject.into is InjectInto.header
    assert m.users.regular.vars["email"] == "user@target.test"
    assert m.users.attacker.vars["password"] == "attacker-pass"


def test_access_defaults_to_empty_when_omitted():
    assert load_manifest_from_dict(_valid()).access == ""


def test_access_description_is_carried():
    data = _valid()
    data["access"] = "A user may read and write only their own basket and orders."
    m = load_manifest_from_dict(data)
    assert m.access == "A user may read and write only their own basket and orders."


def test_version_defaults_to_supported_when_omitted():
    data = _valid()
    del data["version"]
    assert load_manifest_from_dict(data).version == 1


def test_base_url_trailing_slash_stripped():
    data = _valid()
    data["base_url"] = "http://localhost:3000/"
    assert load_manifest_from_dict(data).base_url == "http://localhost:3000"


def test_file_spec_kind_accepts_plain_path():
    data = _valid()
    data["spec"] = {"kind": "file", "value": "/abs/openapi.json"}
    m = load_manifest_from_dict(data)
    assert m.spec.kind is SpecKind.file
    assert m.spec.value == "/abs/openapi.json"


def test_no_auth_is_valid():
    data = _valid()
    data["auth"] = {}
    m = load_manifest_from_dict(data)
    assert m.auth.login is None
    assert m.auth.extract is None
    assert m.auth.inject is None


def test_basic_auth_is_inject_only():
    data = _valid()
    data["auth"] = {
        "inject": {
            "into": "header",
            "name": "Authorization",
            "value": "Basic {{basic(email, password)}}",
        }
    }
    m = load_manifest_from_dict(data)
    assert m.auth.login is None
    assert m.auth.inject is not None
    assert m.auth.inject.into is InjectInto.header


def test_session_auth_extracts_and_replays_cookie():
    data = _valid()
    data["auth"]["extract"] = {"from": "cookie", "path": "session"}
    data["auth"]["inject"] = {"into": "cookie", "name": "session", "value": "{{credential}}"}
    m = load_manifest_from_dict(data)
    assert m.auth.extract is not None
    assert m.auth.extract.source is ExtractFrom.cookie
    assert m.auth.inject is not None
    assert m.auth.inject.into is InjectInto.cookie


def test_login_headers_default_to_empty():
    data = _valid()
    del data["auth"]["login"]["headers"]
    m = load_manifest_from_dict(data)
    assert m.auth.login is not None
    assert m.auth.login.headers == {}


def test_arbitrary_user_vars_allowed():
    data = _valid()
    data["users"]["regular"]["vars"] = {"username": "u", "pin": "1234", "tenant": "42"}
    m = load_manifest_from_dict(data)
    assert m.users.regular.vars["tenant"] == "42"


# --------------------------------------------------------------------------------------
# validation failures
# --------------------------------------------------------------------------------------


def test_unsupported_version_rejected():
    data = _valid()
    data["version"] = 2
    with pytest.raises(ManifestValidationError):
        load_manifest_from_dict(data)


def test_non_http_base_url_rejected():
    data = _valid()
    data["base_url"] = "localhost:3000"
    with pytest.raises(ManifestValidationError):
        load_manifest_from_dict(data)


def test_url_spec_without_http_value_rejected():
    data = _valid()
    data["spec"] = {"kind": "url", "value": "./openapi.json"}
    with pytest.raises(ManifestValidationError):
        load_manifest_from_dict(data)


def test_login_path_must_be_relative():
    data = _valid()
    data["auth"]["login"]["path"] = "http://elsewhere/login"
    with pytest.raises(ManifestValidationError):
        load_manifest_from_dict(data)


def test_unknown_login_method_rejected():
    data = _valid()
    data["auth"]["login"]["method"] = "FETCH"
    with pytest.raises(ManifestValidationError):
        load_manifest_from_dict(data)


def test_extract_without_login_rejected():
    data = _valid()
    del data["auth"]["login"]
    with pytest.raises(ManifestValidationError):
        load_manifest_from_dict(data)


def test_login_without_inject_rejected():
    data = _valid()
    del data["auth"]["inject"]
    with pytest.raises(ManifestValidationError):
        load_manifest_from_dict(data)


def test_inject_credential_without_extract_rejected():
    data = _valid()
    data["auth"] = {
        "inject": {"into": "header", "name": "Authorization", "value": "Bearer {{credential}}"}
    }
    with pytest.raises(ManifestValidationError):
        load_manifest_from_dict(data)


def test_empty_extract_path_rejected():
    data = _valid()
    data["auth"]["extract"]["path"] = "  "
    with pytest.raises(ManifestValidationError):
        load_manifest_from_dict(data)


def test_empty_inject_name_rejected():
    data = _valid()
    data["auth"]["inject"]["name"] = ""
    with pytest.raises(ManifestValidationError):
        load_manifest_from_dict(data)


def test_missing_attacker_user_rejected():
    data = _valid()
    del data["users"]["attacker"]
    with pytest.raises(ManifestValidationError):
        load_manifest_from_dict(data)


def test_empty_user_var_value_rejected():
    data = _valid()
    data["users"]["regular"]["vars"]["password"] = "  "
    with pytest.raises(ManifestValidationError):
        load_manifest_from_dict(data)


def test_unknown_top_level_key_rejected():
    data = _valid()
    data["extra"] = "nope"
    with pytest.raises(ManifestValidationError):
        load_manifest_from_dict(data)


def test_unknown_auth_key_rejected():
    data = _valid()
    data["auth"]["type"] = "bearer"
    with pytest.raises(ManifestValidationError):
        load_manifest_from_dict(data)


def test_non_object_root_rejected():
    with pytest.raises(ManifestParseError):
        load_manifest_from_dict(["not", "an", "object"])  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# JSON / file entry points
# --------------------------------------------------------------------------------------


def test_load_from_json_string():
    m = load_manifest_from_json(json.dumps(_valid()))
    assert m.auth.extract is not None
    assert m.auth.extract.source is ExtractFrom.body


def test_malformed_json_rejected():
    with pytest.raises(ManifestParseError):
        load_manifest_from_json("{not json")


def test_load_from_file(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_valid()), encoding="utf-8")
    assert load_manifest(str(path)).base_url == "http://localhost:3000"


def test_missing_file_rejected(tmp_path):
    with pytest.raises(ManifestReadError):
        load_manifest(str(tmp_path / "nope.json"))
"""The target manifest: what a target project's `make describe` emits, validated on our side.

The detector never calls `make` — a target may run on another machine. Its owner runs the
Makefile there, captures `make describe` (a JSON document), and hands it to us. This module is
the only thing the detector knows about a target: parse that JSON, validate it, expose it as a
typed `TargetManifest`. See `docs/makefile-contract.md` for the contract and the template Makefile.

Authentication is modelled as three orthogonal, template-driven pieces instead of a fixed set of
auth "types": an optional `login` request, an optional `extract` rule that pulls a credential out
of the login result, and an optional `inject` rule that attaches a credential to every later
request. `none`/`basic`/`bearer`/`session` are then just configurations of these three. Templates
reference per-user `vars` (and `{{credential}}` in `inject`); see `bola/target/auth_template.py`.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from bola.target.auth_template import references_credential

SUPPORTED_VERSION = 1
_HTTP_PREFIXES = ("http://", "https://")


class TargetError(Exception):
    """Base class for every target-manifest failure."""


class ManifestReadError(TargetError):
    """The manifest file could not be read."""


class ManifestParseError(TargetError):
    """The manifest was read but is not parseable JSON, or is not a JSON object."""


class ManifestValidationError(TargetError):
    """The manifest parsed but does not satisfy the contract."""


class SpecKind(str, Enum):
    """Where the OpenAPI spec lives: a fetchable URL or a local file path."""

    url = "url"
    file = "file"


class HttpMethod(str, Enum):
    """HTTP methods permitted for the login request."""

    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"


class ExtractFrom(str, Enum):
    """Where in the login result a credential is read from."""

    body = "body"
    cookie = "cookie"
    header = "header"


class InjectInto(str, Enum):
    """Where a credential is attached on subsequent requests."""

    header = "header"
    cookie = "cookie"


class SpecLocation(BaseModel):
    """The spec's location: its `kind` (url/file) and the URL or path `value`."""

    model_config = ConfigDict(extra="forbid")

    kind: SpecKind
    value: str

    @model_validator(mode="after")
    def _check_value(self) -> "SpecLocation":
        """Require a non-empty value, and an http(s) value when kind is `url`."""
        if not self.value.strip():
            raise ValueError("spec.value must not be empty")
        if self.kind is SpecKind.url and not self.value.startswith(_HTTP_PREFIXES):
            raise ValueError("spec.kind 'url' requires an http(s) value")
        return self


class Login(BaseModel):
    """The HTTP request that authenticates a user. Rendered per-user against `{base_url, **vars}`."""

    model_config = ConfigDict(extra="forbid")

    method: HttpMethod
    path: str
    headers: dict[str, str] = Field(default_factory=dict)
    body: str = ""

    @field_validator("path")
    @classmethod
    def _path_relative(cls, v: str) -> str:
        """Require the login path to be base-url-relative (start with `/`)."""
        v = v.strip()
        if not v.startswith("/"):
            raise ValueError("login.path must start with '/' (it is joined onto base_url)")
        return v


class Extract(BaseModel):
    """How to pull a single credential out of the login result."""

    model_config = ConfigDict(extra="forbid")

    source: ExtractFrom = Field(alias="from")
    path: str

    @field_validator("path")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        """Require a non-empty extract path (JSON path / cookie / header name)."""
        if not v.strip():
            raise ValueError("extract.path must not be empty (JSON path / cookie / header name)")
        return v


class Inject(BaseModel):
    """How a credential is attached to every subsequent request to the target."""

    model_config = ConfigDict(extra="forbid")

    into: InjectInto
    name: str
    value: str

    @field_validator("name", "value")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        """Require inject name and value to be non-empty."""
        if not v.strip():
            raise ValueError("must not be empty")
        return v


class AuthConfig(BaseModel):
    """Auth as three orthogonal pieces. All absent → no auth; the classic types are configurations.

    - none:    everything absent.
    - basic:   `inject` only; its value builds the credential from vars (e.g. `{{basic(email, password)}}`).
    - bearer:  `login` + `extract` (body path) + `inject` (e.g. `Authorization: Bearer {{credential}}`).
    - session: `login` + `extract` (cookie) + `inject` (replay the cookie).
    """

    model_config = ConfigDict(extra="forbid")

    login: Login | None = None
    extract: Extract | None = None
    inject: Inject | None = None

    @model_validator(mode="after")
    def _check_coherence(self) -> "AuthConfig":
        """Enforce the three pieces fit together: extract⇒login, login⇒inject, {{credential}}⇒extract."""
        if self.extract is not None and self.login is None:
            raise ValueError("auth.extract requires auth.login (nothing to extract from)")
        if self.login is not None and self.inject is None:
            raise ValueError("auth.login requires auth.inject (the login result would be unused)")
        if (
            self.inject is not None
            and self.extract is None
            and references_credential(self.inject.value)
        ):
            raise ValueError(
                "auth.inject.value references {{credential}} but there is no auth.extract to produce it"
            )
        return self


class UserVars(BaseModel):
    """A user's template variables (e.g. email/password, or username/pin/tenant). Open-ended."""

    model_config = ConfigDict(extra="forbid")

    vars: dict[str, str] = Field(default_factory=dict)

    @field_validator("vars")
    @classmethod
    def _values_non_empty(cls, v: dict[str, str]) -> dict[str, str]:
        """Require every template variable to have a non-empty value."""
        for key, value in v.items():
            if not value.strip():
                raise ValueError(f"vars[{key!r}] must not be empty")
        return v


class TargetUsers(BaseModel):
    """The two identities a BOLA run drives: the regular user and the attacker."""

    model_config = ConfigDict(extra="forbid")

    regular: UserVars
    attacker: UserVars


class TargetManifest(BaseModel):
    """The validated handshake a target emits (`make describe`): base URL, spec, auth, users, access."""

    model_config = ConfigDict(extra="forbid")

    version: int = SUPPORTED_VERSION
    base_url: str
    spec: SpecLocation
    auth: AuthConfig
    users: TargetUsers
    access: str = Field(
        default="",
        description="Optional free-text description of the target's authorization model — who may "
        "read/write which objects. Set by the target's developer; fed to BOLA analysis as a "
        "business-logic hint. Overridable per analysis pass, so it may be left empty here.",
    )

    @field_validator("version")
    @classmethod
    def _supported_version(cls, v: int) -> int:
        """Reject a manifest whose schema version this detector doesn't support."""
        if v != SUPPORTED_VERSION:
            raise ValueError(f"unsupported manifest version {v} (supported: {SUPPORTED_VERSION})")
        return v

    @field_validator("base_url")
    @classmethod
    def _valid_base_url(cls, v: str) -> str:
        """Require an http(s) base URL and normalize away any trailing slash."""
        v = v.strip()
        if not v.startswith(_HTTP_PREFIXES):
            raise ValueError("base_url must be an http(s) URL")
        return v.rstrip("/")


def load_manifest_from_dict(data: dict[str, Any], *, source: str = "<dict>") -> TargetManifest:
    """Build a TargetManifest from an already-parsed document."""
    if not isinstance(data, dict):
        raise ManifestParseError(
            f"Manifest root must be a JSON object, got {type(data).__name__} ({source})"
        )
    try:
        return TargetManifest.model_validate(data)
    except ValidationError as exc:
        raise ManifestValidationError(f"Target manifest failed validation ({source}):\n{exc}") from exc


def load_manifest_from_json(text: str, *, source: str = "<json>") -> TargetManifest:
    """Parse the JSON emitted by `make describe` (e.g. piped stdout) into a TargetManifest."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestParseError(f"Manifest is not valid JSON ({source}): {exc}") from exc
    return load_manifest_from_dict(data, source=source)


def load_manifest(path: str) -> TargetManifest:
    """Load a manifest from a file on disk (e.g. `--target-manifest manifest.json`)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestReadError(f"Could not read manifest file {path!r}: {exc}") from exc
    return load_manifest_from_json(text, source=path)
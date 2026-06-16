"""Target integration: the manifest a target's `make describe` emits, validated on our side.

The detector never calls `make` (a target may run on another machine). `load_manifest` /
`load_manifest_from_json` are the entry points; `TargetManifest` is the validated handshake the
rest of the detector consumes (base URL, spec location, auth, the two users).

Auth is three template-driven pieces — `Login`, `Extract`, `Inject` — rendered against each user's
`vars` via `bola.target.auth_template`. See `docs/makefile-contract.md`.
"""

from bola.target.auth_template import TemplateError, references_credential, render
from bola.target.manifest import (
    AuthConfig,
    Extract,
    ExtractFrom,
    HttpMethod,
    Inject,
    InjectInto,
    Login,
    ManifestParseError,
    ManifestReadError,
    ManifestValidationError,
    SpecKind,
    SpecLocation,
    TargetError,
    TargetManifest,
    TargetUsers,
    UserVars,
    load_manifest,
    load_manifest_from_dict,
    load_manifest_from_json,
)

__all__ = [
    "TargetManifest",
    "SpecLocation",
    "SpecKind",
    "AuthConfig",
    "Login",
    "Extract",
    "ExtractFrom",
    "Inject",
    "InjectInto",
    "HttpMethod",
    "TargetUsers",
    "UserVars",
    "load_manifest",
    "load_manifest_from_json",
    "load_manifest_from_dict",
    "TargetError",
    "ManifestReadError",
    "ManifestParseError",
    "ManifestValidationError",
    "render",
    "references_credential",
    "TemplateError",
]
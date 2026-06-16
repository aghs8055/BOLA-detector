"""Integration test for bola/target/Makefile.template.

The template's `describe` target assembles the manifest JSON with `jq`. That assembly is shell +
`jq`, not Python, so nothing else can catch a regression in it (e.g. a backslash-continued `jq`
filter, a missing `fromjson`). Here we actually run `make describe` on the shipped template and feed
its output straight into `TargetManifest`, so the template stays in lockstep with the model.

Requires `make` and `jq` on PATH; skipped otherwise. This is the one place we run a Makefile — it
is local and network-free, so it lives in integration/, not unit/ (which mocks subprocess).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from bola.target import load_manifest_from_json, render

pytestmark = pytest.mark.skipif(
    not (shutil.which("make") and shutil.which("jq")),
    reason="requires `make` and `jq` on PATH",
)

TEMPLATE = Path(__file__).resolve().parents[3] / "bola" / "target" / "Makefile.template"


def _describe(makefile_dir: Path) -> str:
    result = subprocess.run(
        ["make", "-s", "describe"],
        cwd=makefile_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _write_template(tmp_path: Path) -> Path:
    (tmp_path / "Makefile").write_text(TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    return tmp_path


def _override(tmp_path: Path, **leaves: str) -> Path:
    """A Makefile that includes the template and replaces specific `get-*` leaves' output."""
    lines = [f"include {TEMPLATE}"]
    for leaf, value in leaves.items():
        lines.append(f"get-{leaf}: ; @echo '{value}'")
    (tmp_path / "Makefile").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp_path


def test_shipped_template_describes_a_valid_bearer_manifest(tmp_path):
    manifest = load_manifest_from_json(_describe(_write_template(tmp_path)))

    assert manifest.base_url == "http://localhost:3000"
    assert manifest.spec.kind.value == "url"
    assert manifest.auth.login is not None
    assert manifest.auth.login.method.value == "POST"
    assert manifest.auth.login.path == "/rest/user/login"
    assert manifest.auth.login.headers == {"Content-Type": "application/json"}
    assert manifest.auth.extract is not None
    assert manifest.auth.extract.source.value == "body"
    assert manifest.auth.extract.path == "authentication.token"
    assert manifest.auth.inject is not None
    assert manifest.auth.inject.value == "Bearer {{credential}}"
    assert manifest.users.regular.vars == {"email": "user@target.test", "password": "regular-pass"}
    assert manifest.users.attacker.vars["email"] == "attacker@target.test"


def test_template_login_and_inject_render_against_user_vars(tmp_path):
    """The strings the template emits are real auth_template templates — they must render."""
    manifest = load_manifest_from_json(_describe(_write_template(tmp_path)))
    user_vars = manifest.users.regular.vars

    assert manifest.auth.login is not None
    body = render(manifest.auth.login.body, user_vars)
    assert body == '{"email":"user@target.test","password":"regular-pass"}'

    assert manifest.auth.inject is not None
    header = render(manifest.auth.inject.value, {"credential": "tok-123"})
    assert header == "Bearer tok-123"


def test_empty_auth_leaves_round_trip_to_no_auth(tmp_path):
    """Emptying the auth key leaves drives every `if == "" then null` branch in the jq filter."""
    out = _describe(_override(tmp_path, **{"login-method": "", "extract-from": "", "inject-into": ""}))
    manifest = load_manifest_from_json(out)

    assert manifest.auth.login is None
    assert manifest.auth.extract is None
    assert manifest.auth.inject is None


def test_basic_style_inject_without_login(tmp_path):
    """Basic auth = inject only; the jq filter must emit inject while login/extract stay null."""
    out = _describe(
        _override(
            tmp_path,
            **{
                "login-method": "",
                "extract-from": "",
                "inject-value": "Basic {{basic(email, password)}}",
            },
        )
    )
    manifest = load_manifest_from_json(out)

    assert manifest.auth.login is None
    assert manifest.auth.extract is None
    assert manifest.auth.inject is not None
    assert manifest.auth.inject.value == "Basic {{basic(email, password)}}"

    rendered = render(manifest.auth.inject.value, manifest.users.regular.vars)
    assert rendered.startswith("Basic ")
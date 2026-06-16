"""Send one HTTP request to the target, applying the manifest's auth, and report the result.

This is the executor *primitive*: it does not decide what to call or with which values — that is
the strategy's job (Step 5). It takes a fully-specified `ApiRequest`, applies an `AuthSession`,
sends it, and returns a structured `ExecutionResult`. Auth is where Step 2's three template
pieces finally execute: `login` is sent, `extract` pulls the credential out of the result,
`inject` attaches it to every later request; a 401 triggers one refresh-and-retry.

HTTP goes through a `Transport` seam so unit tests inject a fake and never touch the network;
`requests_transport` is the real default.
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from typing import Any, Callable

from bola.target.auth_template import references_credential, render
from bola.target.manifest import AuthConfig, ExtractFrom, InjectInto


@dataclass
class FilePart:
    """One file to upload: its contents plus the metadata a multipart part needs.

    `content` is the file body the strategy authored (text today; bytes are accepted too). The
    `filename` and `content_type` ride along so the server sees a real upload, not an opaque blob.
    """

    content: str | bytes
    filename: str = "upload.bin"
    content_type: str | None = None


@dataclass
class HttpRequest:
    """A normalized HTTP request handed to the transport."""

    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    json: Any | None = None
    data: Any | None = None  # raw/form body when not JSON
    files: dict[str, Any] | None = None  # multipart parts, transport-ready (requests `files=` shape)


@dataclass
class HttpResponse:
    """A normalized HTTP response returned by the transport."""

    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    body: Any | None = None  # parsed JSON when possible, else the text
    text: str = ""


# A transport sends a request and returns a response. The real one wraps `requests`; tests
# inject a fake. Timeout is in seconds.
Transport = Callable[[HttpRequest, float], HttpResponse]


@dataclass
class ApiRequest:
    """A request to one operation, before auth and URL assembly."""

    method: str
    path: str  # may contain {name} path placeholders
    path_params: dict[str, Any] = field(default_factory=dict)
    query: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    body: Any | None = None
    media_type: str | None = None  # e.g. application/json | application/x-www-form-urlencoded
    files: dict[str, FilePart] | None = None  # multipart file parts, keyed by form-field name


@dataclass
class ExecutionResult:
    """The outcome of one executed request: what was sent, the response, and any transport error."""

    method: str
    url: str
    status_code: int
    ok: bool
    response: Any | None = None
    media_type: str | None = None  # response Content-Type (sans params), for harvesting
    error: str | None = None


def requests_transport(req: HttpRequest, timeout: float) -> HttpResponse:
    """Default transport: send `req` with the `requests` library."""
    import requests

    kwargs: dict[str, Any] = {
        "params": req.params or None,
        "headers": req.headers or None,
        "cookies": req.cookies or None,
        "timeout": timeout,
    }
    if req.files is not None:
        # A multipart upload: `requests` sets the multipart Content-Type and boundary itself.
        # `json` cannot ride along (it would force application/json), but form fields in `data` can.
        kwargs["files"] = req.files
        if req.data is not None:
            kwargs["data"] = req.data
    elif req.json is not None:
        kwargs["json"] = req.json
    elif req.data is not None:
        kwargs["data"] = req.data

    resp = requests.request(req.method, req.url, **kwargs)
    try:
        body: Any = resp.json()
    except ValueError:
        body = resp.text or None
    return HttpResponse(
        status_code=resp.status_code,
        headers=dict(resp.headers),
        cookies=resp.cookies.get_dict(),
        body=body,
        text=resp.text,
    )


# --------------------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------------------


@dataclass
class AuthSession:
    """One user's auth lifecycle: render the inject credential, refresh it on 401.

    Built from the manifest's `AuthConfig` and a user's `vars` via `build_auth_session`. For
    `none`/`basic` there is no login — `inject` (if any) renders straight from `vars`. For
    `bearer`/`session`, `refresh` runs `login`, `extract`s the credential, and `apply` injects it.
    """

    base_url: str
    inject: Any | None  # manifest Inject | None
    login: Any | None  # manifest Login | None
    extract: Any | None  # manifest Extract | None
    variables: dict[str, str]
    transport: Transport
    timeout: float = 30.0
    _credential: str | None = field(default=None, init=False, repr=False)

    def can_refresh(self) -> bool:
        """True when this session can (re-)authenticate — it has both a login and an extract."""
        return self.login is not None and self.extract is not None

    def refresh(self) -> None:
        """Run the login request and extract the credential from its response."""
        if self.login is None or self.extract is None:
            return
        req = HttpRequest(
            method=self.login.method.value,
            url=self.base_url + render(self.login.path, self.variables),
            headers={k: render(v, self.variables) for k, v in self.login.headers.items()},
            data=render(self.login.body, self.variables) if self.login.body else None,
        )
        resp = self.transport(req, self.timeout)
        self._credential = _extract_credential(self.extract, resp)

    def apply(self, req: HttpRequest) -> None:
        """Inject the credential (refreshing first if the value needs one we don't have)."""
        if self.inject is None:
            return
        if (
            self._credential is None
            and references_credential(self.inject.value)
            and self.can_refresh()
        ):
            self.refresh()
        variables = {**self.variables, "credential": self._credential or ""}
        value = render(self.inject.value, variables)
        if self.inject.into is InjectInto.header:
            req.headers[self.inject.name] = value
        else:  # cookie
            req.cookies[self.inject.name] = value


def build_auth_session(
    auth: AuthConfig,
    user_vars: dict[str, str],
    base_url: str,
    *,
    transport: Transport = requests_transport,
    timeout: float = 30.0,
) -> AuthSession:
    """Build an `AuthSession` from the manifest auth config and one user's vars."""
    return AuthSession(
        base_url=base_url,
        inject=auth.inject,
        login=auth.login,
        extract=auth.extract,
        variables={**user_vars, "base_url": base_url},
        transport=transport,
        timeout=timeout,
    )


def _extract_credential(extract: Any, resp: HttpResponse) -> str | None:
    """Pull the credential from the login response per the manifest's `extract` (body/cookie/header)."""
    if extract.source is ExtractFrom.body:
        return _dig(resp.body, extract.path)
    if extract.source is ExtractFrom.cookie:
        value = resp.cookies.get(extract.path)
        return str(value) if value is not None else None
    # header
    value = resp.headers.get(extract.path)
    return str(value) if value is not None else None


def _dig(body: Any, path: str) -> str | None:
    """Walk a dot-separated path into a parsed JSON body (e.g. `authentication.token`)."""
    node = body
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return str(node) if node is not None else None


# --------------------------------------------------------------------------------------
# Executor
# --------------------------------------------------------------------------------------


class ApiExecutor:
    """Sends `ApiRequest`s to one target `base_url`, applying auth and a single 401 retry."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: Transport = requests_transport,
        timeout: float = 30.0,
        max_retries: int = 2,
    ):
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self.timeout = timeout
        self.max_retries = max(1, max_retries)

    def execute(self, req: ApiRequest, auth: AuthSession | None = None) -> ExecutionResult:
        """Send one request through the transport, applying auth and retrying once on a 401."""
        http_req = self._build(req)
        for attempt in range(self.max_retries):
            fresh = _clone(http_req)
            if auth is not None:
                auth.apply(fresh)
            try:
                resp = self.transport(fresh, self.timeout)
            except Exception as exc:  # noqa: BLE001 - any transport error is a failed call
                return ExecutionResult(
                    method=req.method.upper(),
                    url=http_req.url,
                    status_code=0,
                    ok=False,
                    error=str(exc),
                )
            if resp.status_code == 401 and attempt == 0 and auth is not None and auth.can_refresh():
                auth.refresh()
                continue
            return ExecutionResult(
                method=req.method.upper(),
                url=http_req.url,
                status_code=resp.status_code,
                ok=200 <= resp.status_code < 300,
                response=resp.body,
                media_type=_content_type(resp.headers),
            )
        return ExecutionResult(
            method=req.method.upper(),
            url=http_req.url,
            status_code=0,
            ok=False,
            error="max retries reached",
        )

    def _build(self, req: ApiRequest) -> HttpRequest:
        """Assemble the wire request: fill path placeholders, prefix the base URL, route the body."""
        path = req.path
        for name, value in req.path_params.items():
            path = path.replace(f"{{{name}}}", str(value))
        http_req = HttpRequest(
            method=req.method.upper(),
            url=self.base_url + path,
            headers=dict(req.headers),
            params=dict(req.query),
        )
        if req.files:
            http_req.files = {name: _file_tuple(part) for name, part in req.files.items()}
        if req.body is not None:
            # Alongside file parts the body is multipart form fields (→ data), never a JSON document.
            if req.files or (req.media_type and "json" not in req.media_type):
                http_req.data = req.body
            else:
                http_req.json = req.body
        return http_req


def _file_tuple(part: FilePart) -> tuple:
    """A `FilePart` as the tuple `requests` wants — 3-tuple with a type, else a 2-tuple."""
    if part.content_type:
        return (part.filename, part.content, part.content_type)
    return (part.filename, part.content)


def _content_type(headers: dict[str, str]) -> str | None:
    """The response media type (case-insensitive header, parameters stripped)."""
    for k, v in headers.items():
        if k.lower() == "content-type":
            return v.split(";")[0].strip() or None
    return None


def _clone(req: HttpRequest) -> HttpRequest:
    """A shallow copy with fresh mutable maps, so a retry's auth doesn't stack on the last try."""
    return HttpRequest(
        method=req.method,
        url=req.url,
        headers=dict(req.headers),
        params=dict(req.params),
        cookies=dict(req.cookies),
        json=_json.loads(_json.dumps(req.json)) if req.json is not None else None,
        data=req.data,
        files=dict(req.files) if req.files else None,
    )
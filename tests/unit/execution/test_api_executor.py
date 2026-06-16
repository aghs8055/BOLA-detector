"""Unit tests for `bola.execution.api_executor`."""

import pytest

from bola.execution.api_executor import (
    ApiExecutor,
    ApiRequest,
    FilePart,
    HttpRequest,
    HttpResponse,
    build_auth_session,
)
from bola.target.manifest import AuthConfig, Extract, HttpMethod, Inject, Login


class FakeTransport:
    """Records sent requests and returns queued responses (last one repeats)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.sent: list[HttpRequest] = []

    def __call__(self, req: HttpRequest, timeout: float) -> HttpResponse:
        self.sent.append(req)
        idx = min(len(self.sent) - 1, len(self._responses) - 1)
        return self._responses[idx]


def _ok(body=None):
    return HttpResponse(status_code=200, body=body if body is not None else {"ok": True})


# -- request assembly ------------------------------------------------------------


def test_execute_substitutes_path_params_and_builds_url():
    t = FakeTransport([_ok()])
    ex = ApiExecutor("http://api.test/", transport=t)
    res = ex.execute(ApiRequest(method="get", path="/pets/{petId}", path_params={"petId": 42}))
    assert res.ok and res.status_code == 200
    assert t.sent[0].url == "http://api.test/pets/42"
    assert t.sent[0].method == "GET"


def test_execute_json_vs_form_body():
    t = FakeTransport([_ok(), _ok()])
    ex = ApiExecutor("http://api.test", transport=t)
    ex.execute(ApiRequest(method="post", path="/p", body={"a": 1}, media_type="application/json"))
    ex.execute(
        ApiRequest(
            method="post", path="/p", body={"a": 1}, media_type="application/x-www-form-urlencoded"
        )
    )
    assert t.sent[0].json == {"a": 1} and t.sent[0].data is None
    assert t.sent[1].data == {"a": 1} and t.sent[1].json is None


def test_execute_sends_file_part_as_multipart():
    t = FakeTransport([_ok()])
    ex = ApiExecutor("http://api.test", transport=t)
    ex.execute(ApiRequest(
        method="post", path="/contacts/import", media_type="multipart/form-data",
        files={"file": FilePart(content="+14155550101\n+14155550102",
                                filename="numbers.txt", content_type="text/plain")},
    ))
    sent = t.sent[0]
    assert sent.files == {"file": ("numbers.txt", "+14155550101\n+14155550102", "text/plain")}
    assert sent.json is None and sent.data is None


def test_file_part_without_content_type_is_a_two_tuple():
    t = FakeTransport([_ok()])
    ApiExecutor("http://api.test", transport=t).execute(ApiRequest(
        method="post", path="/u", files={"avatar": FilePart(content="x", filename="a.bin")},
    ))
    assert t.sent[0].files == {"avatar": ("a.bin", "x")}


def test_files_and_form_fields_coexist_body_becomes_data_not_json():
    t = FakeTransport([_ok()])
    ApiExecutor("http://api.test", transport=t).execute(ApiRequest(
        method="post", path="/u", media_type="multipart/form-data",
        body={"label": "vip"}, files={"file": FilePart(content="x")},
    ))
    sent = t.sent[0]
    assert sent.files == {"file": ("upload.bin", "x")}
    assert sent.data == {"label": "vip"} and sent.json is None


def test_execute_passes_query_and_headers():
    t = FakeTransport([_ok()])
    ex = ApiExecutor("http://api.test", transport=t)
    ex.execute(ApiRequest(method="get", path="/p", query={"q": "x"}, headers={"X-Trace": "1"}))
    assert t.sent[0].params == {"q": "x"}
    assert t.sent[0].headers["X-Trace"] == "1"


def test_transport_exception_becomes_failed_result():
    def boom(req, timeout):
        raise ConnectionError("down")

    res = ApiExecutor("http://api.test", transport=boom).execute(ApiRequest(method="get", path="/p"))
    assert not res.ok and res.status_code == 0 and "down" in res.error


def test_non_2xx_is_not_ok():
    t = FakeTransport([HttpResponse(status_code=404, body={"e": "nope"})])
    res = ApiExecutor("http://api.test", transport=t).execute(ApiRequest(method="get", path="/p"))
    assert not res.ok and res.status_code == 404


# -- auth: basic (inject only, no login) -----------------------------------------


def test_basic_auth_injects_header_without_login():
    auth_cfg = AuthConfig(inject=Inject(into="header", name="Authorization", value="Basic {{basic(email, password)}}"))
    t = FakeTransport([_ok()])
    auth = build_auth_session(
        auth_cfg, {"email": "u@test", "password": "pw"}, "http://api.test", transport=t
    )
    ApiExecutor("http://api.test", transport=t).execute(ApiRequest(method="get", path="/p"), auth)
    # base64("u@test:pw")
    assert t.sent[0].headers["Authorization"] == "Basic dUB0ZXN0OnB3"
    assert len(t.sent) == 1  # no login round-trip


# -- auth: bearer (login -> extract -> inject) -----------------------------------


def _bearer_cfg():
    return AuthConfig(
        login=Login(method=HttpMethod.POST, path="/login", body='{"u":"{{email}}"}'),
        extract=Extract.model_validate({"from": "body", "path": "auth.token"}),
        inject=Inject(into="header", name="Authorization", value="Bearer {{credential}}"),
    )


def test_bearer_auth_logs_in_extracts_and_injects():
    login_resp = HttpResponse(status_code=200, body={"auth": {"token": "T0KEN"}})
    t = FakeTransport([login_resp, _ok()])
    auth = build_auth_session(_bearer_cfg(), {"email": "u@test"}, "http://api.test", transport=t)
    ApiExecutor("http://api.test", transport=t).execute(ApiRequest(method="get", path="/p"), auth)

    login_req, api_req = t.sent
    assert login_req.url == "http://api.test/login" and login_req.method == "POST"
    assert login_req.data == '{"u":"u@test"}'  # body template rendered
    assert api_req.headers["Authorization"] == "Bearer T0KEN"


def test_401_triggers_one_refresh_and_retry():
    login1 = HttpResponse(status_code=200, body={"auth": {"token": "OLD"}})
    unauthorized = HttpResponse(status_code=401, body=None)
    login2 = HttpResponse(status_code=200, body={"auth": {"token": "NEW"}})
    ok = _ok()
    t = FakeTransport([login1, unauthorized, login2, ok])
    auth = build_auth_session(_bearer_cfg(), {"email": "u@test"}, "http://api.test", transport=t)
    res = ApiExecutor("http://api.test", transport=t, max_retries=2).execute(
        ApiRequest(method="get", path="/p"), auth
    )
    assert res.ok
    # login, first(401), refresh-login, retry(200)
    assert [r.method for r in t.sent] == ["POST", "GET", "POST", "GET"]
    assert t.sent[-1].headers["Authorization"] == "Bearer NEW"


def test_extract_from_cookie():
    cfg = AuthConfig(
        login=Login(method=HttpMethod.POST, path="/login"),
        extract=Extract.model_validate({"from": "cookie", "path": "session"}),
        inject=Inject(into="cookie", name="session", value="{{credential}}"),
    )
    login_resp = HttpResponse(status_code=200, cookies={"session": "abc"})
    t = FakeTransport([login_resp, _ok()])
    auth = build_auth_session(cfg, {}, "http://api.test", transport=t)
    ApiExecutor("http://api.test", transport=t).execute(ApiRequest(method="get", path="/p"), auth)
    assert t.sent[1].cookies["session"] == "abc"


def test_401_retry_resends_the_file_part():
    # the clone made for each attempt must carry the multipart files, or a refreshed retry uploads nothing
    login = HttpResponse(status_code=200, body={"auth": {"token": "T"}})
    t = FakeTransport([login, HttpResponse(status_code=401), login, _ok()])
    auth = build_auth_session(_bearer_cfg(), {"email": "u@test"}, "http://api.test", transport=t)
    res = ApiExecutor("http://api.test", transport=t, max_retries=2).execute(
        ApiRequest(method="post", path="/up", files={"file": FilePart(content="data")}), auth
    )
    assert res.ok
    assert t.sent[-1].files == {"file": ("upload.bin", "data")}


def test_requests_transport_forwards_files_kwarg(monkeypatch):
    captured = {}

    class _Resp:
        status_code = 200
        headers = {"Content-Type": "application/json"}
        text = "{}"

        def json(self):
            return {}

        @property
        def cookies(self):
            class _C:
                def get_dict(self_inner):
                    return {}
            return _C()

    def fake_request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return _Resp()

    import requests

    monkeypatch.setattr(requests, "request", fake_request)
    from bola.execution.api_executor import requests_transport

    requests_transport(
        HttpRequest(method="POST", url="http://api.test/up", files={"f": ("a.txt", "x", "text/plain")}),
        5.0,
    )
    assert captured["files"] == {"f": ("a.txt", "x", "text/plain")}
    assert "json" not in captured  # multipart must not also send a JSON body


def test_no_auth_session_sends_plain_request():
    t = FakeTransport([_ok()])
    ApiExecutor("http://api.test", transport=t).execute(ApiRequest(method="get", path="/p"), None)
    assert "Authorization" not in t.sent[0].headers
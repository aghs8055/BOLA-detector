"""An in-memory HTTP target with deliberately embedded BOLA bugs, for the runner integration test.

Two principals (`alice` = owner/victim, `bob` = attacker). The app is served over a real socket so
the runner exercises its real transport, auth, snapshotter, and analyzer end to end. Bugs are spread
across the four axes the test cares about: read vs write, easy vs hard, and — crucially —
*detectable during the test* (the response reveals the crossing, so the AI agent can note it in
memory) vs *snapshot-only* (the response hides the effect, so only a before/after diff catches it).

Endpoint map (all but /login require `Authorization: Bearer <token>`):

  POST   /login                  → {token}
  POST   /entries                rich body; validated (422 on bad enum/pattern/range/type)
  GET    /entries                list caller's own entries
  GET    /entries/{id}           BUG read  (easy)  no owner check → 200 leaks another user's entry
  PUT    /entries/{id}           BUG write (easy)  no owner check → 200 echoes the applied change
  PUT    /entries/{id}/status    BUG write (hard)  returns 403 to non-owner BUT applies the change
                                                   (the "403-but-done" case: response says blocked,
                                                   side effect happens; non-destructive so it never
                                                   erases the read-BOLA evidence on the same object)
  DELETE /entries/{id}           CONTROL          proper owner check → 403, object NOT deleted
  PUT    /entries/{id}/flag      TRAP             returns 200 but is a no-op (no mutation)
  GET    /entries/{id}/summary   BUG read  (hard)  partial projection of another user's entry leaks
  POST   /orders                 body {entryId}; create owned by caller
  GET    /orders/{id}            BUG read  (medium) no owner check → 200 leak (relation-chained)
  POST   /records                create owned by caller
  GET    /records/{id}           CONTROL          proper owner check → 403 for non-owner
  PUT    /records/{id}           CONTROL          proper owner check → 403 for non-owner
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_USERS = {"alice": "pw-alice-123", "bob": "pw-bob-456"}
_ENUM_CATEGORY = {"alpha", "beta", "gamma"}
_ENUM_REGION = {"na", "eu", "apac"}
_CODE_RE = re.compile(r"^[A-Z]{3}-\d{4}$")


class _State:
    def __init__(self) -> None:
        self.tokens: dict[str, str] = {}        # token -> username
        self.entries: dict[int, dict] = {}      # id -> {owner, ...}
        self.orders: dict[int, dict] = {}
        self.records: dict[int, dict] = {}
        self._next = 1000

    def new_id(self) -> int:
        self._next += 1
        return self._next


def _validate_entry(b: dict) -> list[str]:
    """Hard-validate the discriminating constraints (enum/pattern/range/type). Formats are lenient
    on purpose — the test checks that the model honours enum/pattern/bounds, not date spelling."""
    errs: list[str] = []
    if not isinstance(b.get("title"), str) or not b["title"].strip():
        errs.append("title")
    if not isinstance(b.get("code"), str) or not _CODE_RE.match(b.get("code") or ""):
        errs.append("code")
    if b.get("category") not in _ENUM_CATEGORY:
        errs.append("category")
    p = b.get("priority")
    if not isinstance(p, int) or isinstance(p, bool) or not (1 <= p <= 5):
        errs.append("priority")
    if "amount" in b and (isinstance(b["amount"], bool) or not isinstance(b["amount"], (int, float))):
        errs.append("amount")
    if "active" in b and not isinstance(b["active"], bool):
        errs.append("active")
    if "tags" in b and not isinstance(b["tags"], list):
        errs.append("tags")
    if "meta" in b and b["meta"] is not None:
        meta = b["meta"]
        if not isinstance(meta, dict):
            errs.append("meta")
        elif "region" in meta and meta["region"] not in _ENUM_REGION:
            errs.append("meta.region")
    return errs


def _make_handler(state: _State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence per-request stderr noise
            pass

        # -- helpers ------------------------------------------------------------
        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return {}

        def _send(self, code: int, obj=None) -> None:
            payload = b"" if obj is None else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if payload:
                self.wfile.write(payload)

        def _user(self) -> str | None:
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                return state.tokens.get(auth[len("Bearer "):])
            return None

        # -- dispatch -----------------------------------------------------------
        def do_POST(self):
            self._route("POST")

        def do_GET(self):
            self._route("GET")

        def do_PUT(self):
            self._route("PUT")

        def do_DELETE(self):
            self._route("DELETE")

        def _route(self, method: str):
            path = self.path.split("?")[0]

            if method == "POST" and path == "/login":
                b = self._body()
                if _USERS.get(b.get("username")) == b.get("password"):
                    tok = uuid.uuid4().hex
                    state.tokens[tok] = b["username"]
                    return self._send(200, {"token": tok})
                return self._send(401, {"error": "bad credentials"})

            user = self._user()
            if user is None:
                return self._send(401, {"error": "unauthenticated"})

            # /entries collection
            if path == "/entries":
                if method == "POST":
                    b = self._body()
                    errs = _validate_entry(b)
                    if errs:
                        return self._send(422, {"error": "invalid", "fields": errs})
                    eid = state.new_id()
                    state.entries[eid] = {"id": eid, "owner": user, **b}
                    return self._send(201, _public(state.entries[eid]))
                if method == "GET":
                    return self._send(200, [_public(e) for e in state.entries.values()
                                            if e["owner"] == user])

            # /records collection
            if path == "/records":
                if method == "POST":
                    b = self._body()
                    rid = state.new_id()
                    state.records[rid] = {"id": rid, "owner": user, "label": b.get("label", "r")}
                    return self._send(201, _public(state.records[rid]))
                if method == "GET":
                    return self._send(200, [_public(r) for r in state.records.values()
                                            if r["owner"] == user])

            # /orders collection
            if path == "/orders":
                if method == "POST":
                    b = self._body()
                    oid = state.new_id()
                    state.orders[oid] = {"id": oid, "owner": user, "entryId": b.get("entryId")}
                    return self._send(201, _public(state.orders[oid]))

            m = re.match(r"^/entries/(\d+)(/summary|/status|/flag)?$", path)
            if m:
                eid = int(m.group(1))
                sub = m.group(2)
                entry = state.entries.get(eid)
                if entry is None:
                    return self._send(404, {"error": "not found"})
                if sub == "/summary" and method == "GET":      # BUG read (hard): partial leak
                    return self._send(200, {"title": entry.get("title"), "amount": entry.get("amount")})
                if sub == "/status" and method == "PUT":         # BUG write (hard): 403-but-done
                    entry["status"] = self._body().get("status", "set")  # ...applied either way
                    if entry["owner"] == user:
                        return self._send(200, {"result": "accepted"})
                    return self._send(403, {"error": "forbidden"})   # reported blocked, but applied
                if sub == "/flag" and method == "PUT":           # TRAP: 200 but no-op
                    return self._send(200, {"result": "accepted"})
                if sub is None and method == "GET":              # BUG read (easy): no owner check
                    return self._send(200, _public(entry))
                if sub is None and method == "PUT":              # BUG write (easy): echoes change
                    entry.update({k: v for k, v in self._body().items() if k not in ("id", "owner")})
                    return self._send(200, _public(entry))
                if sub is None and method == "DELETE":           # CONTROL: proper owner check
                    if entry["owner"] != user:
                        return self._send(403, {"error": "forbidden"})  # refused AND not deleted
                    del state.entries[eid]
                    return self._send(204)

            m = re.match(r"^/orders/(\d+)$", path)
            if m and method == "GET":                            # BUG read (medium): no owner check
                order = state.orders.get(int(m.group(1)))
                return self._send(200, _public(order)) if order else self._send(404, {"error": "x"})

            m = re.match(r"^/records/(\d+)$", path)
            if m:                                                # CONTROL: proper owner check
                rid = int(m.group(1))
                rec = state.records.get(rid)
                if rec is None:
                    return self._send(404, {"error": "not found"})
                if rec["owner"] != user:
                    return self._send(403, {"error": "forbidden"})
                if method == "GET":
                    return self._send(200, _public(rec))
                if method == "PUT":
                    rec.update({k: v for k, v in self._body().items() if k not in ("id", "owner")})
                    return self._send(200, _public(rec))

            return self._send(404, {"error": "no route"})

    return Handler


def _public(obj: dict | None) -> dict | None:
    """Drop the server-internal owner tag from a response body."""
    if obj is None:
        return None
    return {k: v for k, v in obj.items() if k != "owner"}


class StubServer:
    def __init__(self) -> None:
        self.state = _State()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.state))
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address
        return f"http://127.0.0.1:{port}"

    def __enter__(self) -> "StubServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
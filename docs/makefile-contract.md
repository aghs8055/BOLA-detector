# Makefile Target Contract

How a target project tells the detector how to drive and authenticate against it.

## The boundary

The detector **never calls `make`**. A target project may live on another machine entirely, so
shelling into its working directory is not an option. Instead:

1. The target developer copies the template Makefile (below) into their project and fills in the
   blanks.
2. They run it **on the target's machine** — `make start` to bring the app up, then
   `make describe` to print a **manifest** (a single JSON document describing the running target).
3. They hand that manifest to the detector (`--target-manifest manifest.json`, or by piping
   `make describe`).
4. The detector consumes the manifest and talks to the target **over HTTP** using `base_url` and
   the declared auth — no filesystem access to the target required.

So the only thing the detector knows about a target is its manifest plus a reachable `base_url`.
On our side this is a single Pydantic model (`bola/target/` → `TargetManifest`); there is no
adapter that invokes `make` and no "managed" lifecycle mode.

## Two kinds of targets

The Makefile has targets in two roles:

- **Lifecycle actions** — the operator runs these by hand on the target machine. They *do*
  something; the detector never reads their output.
- **Info leaves** — each prints **exactly one line** to stdout. The developer implements these as
  trivial one-line `echo`s. They are composed into the manifest by `describe`.

Plus one target the developer does **not** edit:

- **`describe`** — shipped pre-written in the template. It runs the info leaves and assembles the
  manifest JSON with `jq`. Because we own this target, the developer never hand-writes JSON — they
  only fill in scalar `echo`s, and the error-prone escaping/nesting lives in code we provide.

> **The one-line rule.** Every `get-*` target prints exactly one line (empty is allowed). This
> keeps `describe`'s assembly uniform and lets a developer sanity-check any single fact in
> isolation (`make get-base-url`). A few leaves print that one line as a small JSON object
> (`get-login-headers`, `get-regular-vars`, `get-attacker-vars`); `describe` folds them in with
> `fromjson`. They are still one line and still trivial to eyeball.

## Required targets

### Lifecycle actions (developer implements)

| Target | Responsibility |
|---|---|
| `make start` | Boot the app, init/seed baseline data **including the two test users (regular + attacker)**, and **block until the app is healthy** before returning. |
| `make stop` | Stop the app and clean up. Must be safe to call even if `start` half-failed. |
| `make refresh` | Reset state to baseline. Called **once per detection run** (between runs), not in a tight loop. |

### Info leaves (developer implements — each prints one line)

| Target | Prints |
|---|---|
| `make get-base-url` | Base URL, e.g. `http://localhost:3000`. |
| `make get-spec` | OpenAPI spec location: a URL (`http://…`) or a local path. Kind is inferred (`http(s)://` → fetched over HTTP; otherwise a filesystem path, only meaningful for a local target). For a remote target this **must** be a URL. |
| `make get-regular-vars` | Regular user's template variables as a one-line JSON object, e.g. `{"email":"user@target.test","password":"regular-pass"}`. Keys are open-ended (`username`/`pin`/`tenant`/…) — whatever the login template references. |
| `make get-attacker-vars` | Attacker user's template variables, same shape. |
| `make get-login-method` | Login request HTTP method (`GET`\|`POST`\|`PUT`\|`PATCH`\|`DELETE`). Empty if there is no login request. |
| `make get-login-path` | Login request path, joined onto `base_url` (e.g. `/rest/user/login`). Must start with `/`. Empty if no login. |
| `make get-login-headers` | Login request headers as a one-line JSON object (e.g. `{"Content-Type":"application/json"}`). `{}` if none. |
| `make get-login-body` | Login request body template (e.g. `{"email":"{{email}}","password":"{{password}}"}`). May contain `{{var}}` holes. Empty if none. |
| `make get-extract-from` | Where the credential lives in the login result: `body`\|`cookie`\|`header`. Empty if nothing is extracted. |
| `make get-extract-path` | JSON path (when `body`) or cookie/header name to read the credential. Empty if no extract. |
| `make get-inject-into` | Where to attach the credential on every later request: `header`\|`cookie`. Empty for no-auth. |
| `make get-inject-name` | Header or cookie name to set (e.g. `Authorization`). Empty for no-auth. |
| `make get-inject-value` | Value template for that header/cookie. May reference `{{credential}}` (the extracted value) and user `{{var}}`s / `{{basic(user, pass)}}`. Empty for no-auth. |

### Aggregator (shipped — do not edit)

| Target | Prints |
|---|---|
| `make describe` | The full manifest as JSON (see below), assembled from the info leaves with `jq`. |

## The manifest

`make describe` emits exactly this shape; the detector validates it with the `TargetManifest`
Pydantic model and rejects anything malformed.

```json
{
  "version": 1,
  "base_url": "http://localhost:3000",
  "spec": { "kind": "url", "value": "http://localhost:3000/api-docs/swagger.json" },
  "auth": {
    "login": {
      "method": "POST",
      "path": "/rest/user/login",
      "headers": { "Content-Type": "application/json" },
      "body": "{\"email\":\"{{email}}\",\"password\":\"{{password}}\"}"
    },
    "extract": { "from": "body", "path": "authentication.token" },
    "inject": { "into": "header", "name": "Authorization", "value": "Bearer {{credential}}" }
  },
  "users": {
    "regular":  { "vars": { "email": "user@target.test",     "password": "regular-pass" } },
    "attacker": { "vars": { "email": "attacker@target.test", "password": "attacker-pass" } }
  }
}
```

`spec.kind` is `url` | `file` (inferred from `get-spec`).

**Auth is three orthogonal, template-driven pieces** rather than a fixed set of auth "types". Each
is optional; the classic types are just configurations of them:

- **`login`** — the HTTP request that authenticates one user (`method`/`path`/`headers`/`body`).
  Every field is a template rendered against that user's `vars` plus `base_url`. Omit it when no
  login round-trip is needed (no-auth, or HTTP Basic).
- **`extract`** — where the credential sits in the login *result*: `from` is `body` (read JSON at
  `path`), `cookie` (cookie named `path`), or `header` (response header named `path`).
- **`inject`** — how the credential rides on *every later request*: set header or cookie `name` to
  the rendered `value`. The value template additionally sees `{{credential}}` (the extracted value).

How the familiar cases map:

| Scheme | `login` | `extract` | `inject` |
|---|---|---|---|
| none | — | — | — |
| basic | — | — | `Authorization: Basic {{basic(email, password)}}` |
| bearer | the login POST | `from: body, path: authentication.token` | `Authorization: Bearer {{credential}}` |
| session | the login POST | `from: cookie, path: <cookie>` | `cookie <name>: {{credential}}` |

**Template language** (see `bola/target/auth_template.py`): `{{var}}` substitutes a user var or
`base_url`; in an `inject.value`, `{{credential}}` is the extracted value. Two helpers cover the
encodings HTTP needs: `{{base64(var)}}` and `{{basic(user_var, pass_var)}}` (base64 of
`user:pass`). It is pure substitution — no general expressions.

**Coherence rules** the model enforces: `extract` requires a `login` (nothing to read otherwise);
a `login` requires an `inject` (its result would be unused); and an `inject.value` that references
`{{credential}}` requires an `extract` to produce it.

## Assembly: why `jq`

`describe` builds the manifest with `jq`, so `jq` must be installed on the target machine. This is
deliberate: assembling nested JSON by hand in `make`/bash is fragile (escaping a password that
contains a quote, comma placement, the nested `auth` block). `jq` does the escaping correctly while
the developer only writes flat scalar `echo`s. If `jq` is genuinely unavailable, the developer may
override `describe` to emit the same JSON by other means — it is the one target whose *output*, not
implementation, is the contract.

## Secrets

The manifest contains live credentials. Treat manifest files as secrets: keep them out of version
control, and do not log them.

## Template Makefile

The canonical template is **[`../bola/target/Makefile.template`](../bola/target/Makefile.template)** —
the single source of truth, not duplicated here. Copy it into your target project and fill in the
`# TODO` leaves; `start`/`stop`/`refresh` and the `get-*` leaves are yours, `describe` is provided.

Its shipped default leaves describe a juice-shop-style bearer target and produce a valid manifest
as-is, so `make describe` works before you change anything. That property is enforced by
`tests/integration/target/test_makefile_template.py`, which runs `make describe` on the template
and validates the output through `TargetManifest` — so a regression in the `jq` assembly fails CI.

> The `jq` filter in the template is on a single line on purpose: a backslash-continued line inside
> `jq`'s single-quoted program would pass literal backslashes to `jq`. The `--arg` lines continue
> normally because they sit outside the quoted filter.

## Usage flow

```bash
# on the target's machine
make start                     # boot + seed, blocks until healthy
make describe > manifest.json  # capture the handshake

# wherever the detector runs (same or another machine)
cli/run.py --target-manifest manifest.json   # consumes the manifest, talks HTTP to base_url

# back on the target's machine, between runs
make refresh                   # reset to baseline
# ... and when finished
make stop
```

The juice_shop integration tests (Step 9) are the one place *our* code runs these targets — there
the target is local by definition, and a pytest fixture calls `make start`/`make stop`. That is
test plumbing in `tests/integration/`, not part of the detector's runtime.
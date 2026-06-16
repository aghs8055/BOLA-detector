# `bola.execution` — the actuator layer

Everything before this package reasons on paper: the spec is loaded, the target manifest is
validated, relations are detected. `execution` is the first code that actually **talks to the
target over HTTP and remembers what comes back**. It is a deliberately dumb *primitive*: it
sends a fully-specified request and stores values. It does **not** decide what to call or with
which values — that is the strategy's job (Step 5). Keeping it a primitive is what lets the
random strategy, the AI strategy, the snapshotter, and the run manager all share one seam.

## `api_executor.py` — send one request, applying auth

`ApiExecutor(base_url).execute(req, auth) -> ExecutionResult`. It assembles the URL (path
placeholders substituted, query attached), routes the body by media type (JSON vs form, or
`multipart/form-data` when the request carries file parts), applies the `AuthSession`, sends it
through a `Transport`, and on a `401` refreshes the credential once and retries.

**Auth is where Step 2's three template pieces finally execute.** The manifest models auth as
`login` + `extract` + `inject` (not fixed types). `AuthSession` runs them:

- `refresh()` sends `login` (path/headers/body rendered via `bola.target.auth_template`) and
  `extract`s the credential from the response (`from: body|cookie|header`).
- `apply(req)` renders `inject.value` (with `{{credential}}` available) and attaches it as a
  header or cookie. `none`/`basic` need no login, so `apply` renders straight from `vars`;
  `bearer`/`session` lazily `refresh()` when a credential is first needed.

**The `Transport` seam.** All HTTP goes through `Transport = Callable[[HttpRequest, float],
HttpResponse]`. `requests_transport` is the real default; unit tests inject a fake and never
touch the network. The 401-retry, auth, and URL logic are tested entirely against fakes.

**File uploads.** When an `ApiRequest` carries `files` (a `{field_name: FilePart}` map), the
executor sends a `multipart/form-data` request: each `FilePart` (content + `filename`/`content_type`)
becomes a `requests` file part, and any non-file body fields ride along as form `data`. *What* a file
contains is still strategy-level value production (Step 5) — the AI strategy authors it via a `file`
`InputChoice`; the primitive only transmits it.

## `field_repo.py` — harvest ids from real responses, serve them as inputs

A plain in-memory pool (architecture decision: SQLite + in-memory field repo, **no Redis**),
keyed by `(source operation key, source schema pointer)` — the exact coordinates a relation
edge's source carries, so harvest and lookup share one address.

- `harvest(repo, source_key, status, media, response, relations)` — after a call returns, walk
  the real response at the source pointer of every edge originating from `source_key` (whose
  declared status/media match) and store each scalar value found.
- `candidates_for_parameter` / `candidates_for_body` — given a target input, return the
  harvested values that flow into it (the ids a later call can replay).
- `fork()` — deep copy. The attacker phase inherits the **regular** user's harvested ids and
  replays them under its own auth. This is the BOLA manoeuvre in one method.

## Schema → data pointer translation (`bola.spec.views.data_values`)

Relations speak *schema* pointers (`/properties/<name>`, `/items`, `/additionalProperties`); a
real response is plain JSON. `views.data_values(data, schema_pointer)` bridges them: drop
`/properties`, fan `/items` across a list, fan `/additionalProperties` across a map. It follows
**recursive containers to full depth** — when a property's elements re-nest that same property
(a `$ref` cycle the schema view cuts after one repeat), it re-applies the step at each deeper
level, so an id buried in a self-referential structure is still harvested. Pointer mechanics
live in `spec/views`, never here.

## Files

| File | Responsibility |
|---|---|
| `api_executor.py` | `ApiExecutor.execute`, `AuthSession` (login/extract/inject + 401 retry), the `Transport` seam. |
| `field_repo.py` | In-memory id pool: `harvest`, `candidates_for_*`, `fork`. |

`snapshotter.py` (Step 6) joins this package. Value generation for unmapped fields lives in
`bola.generators`.
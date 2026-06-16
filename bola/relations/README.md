# `bola.relations` — data-flow relation detection

Finds **directed data-flow edges** between operations: a field in one operation's *response*
that can serve as an input to another operation's *parameter* or *request body*. These edges
are the substrate the executor and strategies later use to carry an identifier from one call
into another (the core BOLA manoeuvre — use an id obtained from one context against another).

Detection is **LLM-proposed, spec-validated**: the model brings semantic judgment, a
deterministic validator grounds every edge in the spec. The step is resumable and cached.

## The group model

For each **source** operation `A`, every *other* operation is chunked into **groups of
`relations.group_size`** (default 10). One LLM call analyses `A`'s response fields against one
group of targets → one `GroupRelations`. So the work is `n × ⌈(n-1)/group_size⌉` calls (≈
n²/10), and `(source, group_index)` is the unit of work, the retry boundary, and the resume
checkpoint — all the same seam.

## The detect → validate → revise graph (`detector.py`)

A LangGraph state machine runs per group:

```
detect ──▶ validate ──▶ (errors & attempts left?) ──▶ revise ──┐
              │                                                 │
              └──▶ (clean, or out of attempts) ──▶ END          └─▶ back to validate
```

- **detect** — structured LLM call (`prompt | llm.with_structured_output(GroupRelations)`)
  proposing edges. Source side = `(status_code, media_type, json_pointer)`; target side =
  a parameter `(name, location)` or a request-body `(media_type, json_pointer)`. Each edge ends
  with `rationale` then a `has_relation` verdict (in that order): the model reasons first and then
  commits, and the detector **drops any edge with `has_relation=false`** — this catches the
  structured-output failure mode where the model emits an edge whose own rationale rejects it.
- **validate** (`validate.py`) — every edge is checked against the spec: the source pointer
  exists in that response, the target param / body field exists, and types are equal or
  bridged by the declared `cast`. Errors are plain strings.
- **revise** — only when validation flagged errors and attempts remain: the model is
  re-prompted with its previous proposal **plus the specific errors**, to repair near-misses
  (e.g. a hallucinated parameter name) or drop them. Bounded by `relations.max_attempts`.

**Best-of-attempts.** The graph keeps the validated projection with the most edges seen across
attempts and returns *that* — so a revise pass can only improve the result, never regress below
what `detect` already validated. Only validated edges are ever returned; unvalidated edges are
dropped, never persisted.

**Identifier-only, spec-agnostic prompt.** The detect prompt constrains edges to genuine
identifier / foreign-key carry-overs (the source is an object's identifier, the target the *same*
object's handle) — matching type or field name is explicitly *not* evidence. This is what makes
the step usefully precise rather than pairing every type-compatible field. The prompt names no
fixture field names (describe roles, not example data) so the petstore eval can't pass for the
wrong reason and detection generalises to other specs.

**Deterministic media-type fan-out.** A request body can declare the same field under several
media types (json / xml / x-www-form-urlencoded). Media type is part of an edge's identity (when
schemas differ per media type, the pointers differ), so it is never dropped. The prompt asks for
an edge per media type, but the *guarantee* is in code: `_finalize` expands each validated body
edge to every media type whose schema actually contains that pointer. So completeness across
encodings does not depend on the LLM enumerating them, while a field renamed in one encoding stays
a distinct edge (its pointer only matches that media type's schema).

The two chains are injectable, so unit tests exercise the whole graph with canned outputs and
no network. Real vs. mocked, the graph shape (and the checkpoint seam) is identical.

## Batching (`detect_groups`)

`detect_groups(jobs)` runs several `source × group` units **concurrently** in one
`graph.batch(..., return_exceptions=True)` call; `runner.py` chunks pending units by
`relations.batch_size` (default 1 = sequential). A per-unit failure is returned in place rather
than raised, so the survivors of a failing batch are still checkpointed. `detect_group` (single)
is retained for the CLI and unit tests.

## Resumability (`runner.py`)

Three layers of fault tolerance:

1. **Transport retries** — `ChatOpenAI(max_retries=…)` for transient 429/5xx.
2. **Revision retries** — the bounded validate→revise loop for semantic errors.
3. **Cross-run resume** — each unit is checkpointed in `RelationGroupResult` as `done`/`failed`.

On an unrecoverable error (`relations.on_error: stop`) or a `KeyboardInterrupt`, progress is
left on disk and the run stops; a rerun **skips `done` units** and retries the rest. `failed`
units are *not* `done`, so they are retried on the next run. When every unit is `done`, the
consolidated edge list is written once to `RelationCache` (`spec_hash × model`) for downstream
steps. `on_error: continue` processes the remaining units instead of stopping.

## Where pointers come from

Field pointers are **not** derived here — `relations` consumes `bola.spec.views`
(`leaf_fields`, `operations`, `parameters`, `request_bodies`, `responses`, `spec_hash`), which
owns all OpenAPI mechanics. Pointers use the alphabet `/properties/<name>`, `/items`,
`/additionalProperties`, and `$ref`s are resolved to their live node before the pointer is
built (the pointer keeps the property path, never the `#/components/...` target).

**Recursive `$ref`s** are a cycle in the resolved graph. `leaf_fields` cuts a node after one
repeat on the current path, so a self-referential field still yields **one usable pointer
level** (e.g. `…/subcategories/items/properties/id`) rather than unrolling forever. This is a
*schema-view* cut only: at runtime the field repository walks real response *data* to full
depth, so deep ids are still harvested — the one-level cut never limits the strategy.

## Files

| File | Responsibility |
|---|---|
| `models.py` | Pydantic LLM I/O (`GroupRelations`, `RelationEdge`, …) + persisted `Relation`. |
| `context.py` | Arrange one `source × group` into the prompt context document. |
| `validate.py` | Ground a proposed edge against the spec (existence + type/cast). |
| `detector.py` | The detect/validate/revise LangGraph for one group. |
| `runner.py` | Enumerate, chunk, checkpoint/resume, assemble into the cache. |

Prompts live in `bola/templates/relations/` (`detect_relation_group.md`,
`revise_relation_group.md`) — never inline.
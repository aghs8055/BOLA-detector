# Spec Loader Design — FULL FIDELITY, NOT FLATTENING

**Hard requirement: the loaded object must losslessly represent the entire OpenAPI document.**
No OpenAPI capability may be dropped, collapsed, merged-away, or approximated. The canonical
in-memory representation is a complete, validated Python object model of the spec (OpenAPI 3.0
**and** 3.1).

**Do NOT repeat the previous mistake.** The prior implementation flattened the schema graph
into a list of primitive leaf `Field`s with dot-notation paths. That model *cannot* express a
`$ref`, a `oneOf`/`anyOf`, a `discriminator`, or a recursive type, so it was forced to inline
refs, merge `allOf`, keep only the first union branch, and truncate cycles — all lossy. That
whole approach is rejected.

## Principles for the rewrite

- **Keep everything.** `$ref` preserved as a reference (registry + lazy/on-demand resolution),
  never destructively inlined. `allOf`/`anyOf`/`oneOf` preserved as composition nodes.
  `discriminator`, `enum`, `const`, all formats, all numeric/string/array constraints,
  `nullable`/type-unions, `readOnly`/`writeOnly`, `examples`, `encoding`, parameter `style`/
  `explode`/`content`, headers, links, callbacks, security schemes, servers + variables,
  webhooks (3.1), `externalDocs`, extensions (`x-*`) — all retained.
- **Circular refs are normal, not an error.** Because refs stay refs (graph, not tree), a
  recursive schema is just a cycle in the graph; resolution must be cycle-safe and bounded
  without losing the structure.
- **The canonical model is lossless; convenience is derived.** Downstream needs (value
  generation for strategies; operation/spec description for AI strategy + relation detection)
  are served by *views/helpers computed on top of* the full model — they must never become the
  storage format or discard data. Hold the data now; build those views in their own steps.
- **Version handling:** support 3.0 and 3.1 fully. Prefer exposing each faithfully; only
  normalize where a caller genuinely benefits, and never by discarding the original.
- **Loading:** from file path or http(s) URL; validate on load; clear errors on failure.
- **SE aspects:** evolvability (small stable public surface over the model), testability (pure,
  network mocked), observability (log resolution/validation events, don't silently drop).

Evaluate the library choice deliberately (`openapi-pydantic` already gives a full, validated
3.0/3.1 object model — using it directly, plus a cycle-safe resolver, may beat any custom
translation). Verify against the real `tests/fixtures/petstore_3.0.json` plus crafted 3.1 specs
exercising `oneOf`/`anyOf`/`discriminator`/recursive `$ref`/webhooks.

## What was built (implementation notes)

The canonical model **is** the `openapi-pydantic` 0.5.1 object graph — no hand-translation.
That library validates 3.0 *and* 3.1, keeps `$ref` as a `Reference` node, keeps
`allOf`/`anyOf`/`oneOf` as composition, and every model uses `extra="allow"`, so
`discriminator`, `const`, `prefixItems`, type-as-list nullability, `x-*` extensions, webhooks,
server variables, etc. are all retained. We did NOT write a custom model.

The `spec` package is split by responsibility behind a façade (`bola/spec/__init__.py`):
`errors.py` (exception hierarchy), `model.py` (`Spec` + `resolve_ref` + JSON-Pointer nav),
`loader.py` (`load_spec`/`load_spec_from_dict` + read/parse). Callers only ever
`from bola.spec import ...`, so the internal file layout can change without touching them.

Public surface (re-exported from `bola.spec`), deliberately small:
- `load_spec(source, *, timeout=30) -> Spec` — `source` is a file path or http(s) URL.
- `load_spec_from_dict(data, *, source=...) -> Spec` — for already-parsed docs / tests.
- `Spec` (dataclass): `.model` (validated typed graph — canonical), `.raw` (original
  parsed dict — the ultimate lossless source, also used for JSON-Pointer semantics),
  `.source`, `.version`, `.is_3_1`, and `.resolve_ref(ref)`.
- Typed errors (all subclass `SpecError`): `SpecLoadError` (unreadable/unfetchable),
  `SpecParseError` (not JSON/YAML, or root not an object), `SpecValidationError` (bad/unsupported
  version or pydantic validation failure), `ReferenceResolutionError` (external or dangling ref).

`resolve_ref(ref: str | Reference)` is the cycle-safe resolver. It navigates the JSON Pointer
over the live model graph and returns the **same instance** stored there (never inlines/copies),
so a recursive schema is just a cycle in the graph — resolving it repeatedly is O(1), memoized,
and terminating. Pure `$ref -> $ref` chains are followed with a visited-guard. External-document
refs and dangling pointers raise `ReferenceResolutionError` (the `Reference` node itself stays in
the model — we only decline to dereference). 3.0 and 3.1 define *distinct* `Reference`/`OpenAPI`
classes; the resolver accepts both (`_REFERENCE_TYPES` tuple, in `model.py`).

JSON is tried before YAML for cleaner errors (YAML is the fallback superset). Version is
pre-checked (`3.0`/`3.1` prefix) for a friendly message before pydantic's discriminated parse.
Observability: `logging.getLogger("bola.spec.loader")` logs INFO on load (version + path/schema
counts); `logging.getLogger("bola.spec.model")` logs DEBUG per ref resolution.

Future value-generation and LLM-description views must be **derived helpers over `.model`** —
do not change the storage format or add lossy convenience fields here.

The two earlier lossy attempts (hand-rolled dict walker; a translation that flattened the schema
graph into primitive leaf fields and dropped `$ref` identity, unions, discriminator, recursion)
were discarded — **do not resurrect either.** `tests/unit/spec/test_loader.py` asserts
losslessness: refs stay refs, both union branches present, discriminator/const/type-list
preserved, webhooks/extensions/server-vars kept, recursive `$ref` resolves without hanging,
external/dangling refs raise typed errors.
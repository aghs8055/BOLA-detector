"""Derived, read-only views over a loaded `Spec`.

These helpers flatten the lossless `openapi-pydantic` model into the small, addressable
shapes the rest of the tool reasons about: the list of operations, and the *leaf fields* of a
schema as JSON pointers. They own all OpenAPI mechanics — `$ref` resolution, 3.0/3.1
differences, RFC 6901 pointer construction — so consumers (relations, executor, generators)
never touch schema internals.

Pointers are **schema pointers** over the alphabet `/properties/<name>`, `/items`,
`/additionalProperties` — the same alphabet the relation prompt constrains the LLM to, so a
proposed pointer can be re-walked here for validation.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from openapi_pydantic.v3.v3_0 import Reference as Reference30, Schema as Schema30
from openapi_pydantic.v3.v3_1 import Reference as Reference31, Schema as Schema31

from bola.spec.model import Spec

_REFERENCE_TYPES = (Reference30, Reference31)
_SCHEMA_TYPES = (Schema30, Schema31)
_SCHEMA_OR_REF = _SCHEMA_TYPES + _REFERENCE_TYPES

HTTP_METHODS = ("get", "put", "post", "delete", "patch", "options", "head", "trace")

# Recursive ($ref-cyclic) schemas are cut after one repeat of a node on the current path,
# so a self-referential field still yields one usable pointer level (see bola/relations/README).
_MAX_REPEAT = 1
_MAX_DEPTH = 60


@dataclass(frozen=True)
class FieldRef:
    """A leaf field of a schema: its JSON pointer, normalized type/format, and value constraints.

    `constraints` carries the schema's value-shaping keywords that are actually present (`enum`,
    `pattern`, length/range bounds, `description`, `default`/`example`, …) so a generator — most
    importantly the AI strategy — can produce a value the server will accept instead of guessing
    blindly from `type` alone. See `_constraints`.
    """

    json_pointer: str
    type: str | None
    format: str | None
    constraints: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParamRef:
    """An operation parameter: name, location, normalized type/format, requiredness, constraints."""

    name: str
    location: str  # path | query | header | cookie
    type: str | None
    format: str | None
    required: bool
    constraints: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BodyRef:
    """A request body for one media type, flattened to its leaf fields."""

    media_type: str
    fields: list[FieldRef]


@dataclass(frozen=True)
class ResponseRef:
    """A response body for one (status code, media type), flattened to its leaf fields."""

    status_code: str
    media_type: str
    fields: list[FieldRef]


@dataclass(frozen=True)
class OperationRef:
    """A single operation, keyed by a stable identity (a unique operationId, else `METHOD /path`)."""

    key: str
    method: str  # upper-case
    path: str
    operation: Any = field(repr=False)
    path_level_params: tuple[Any, ...] = field(default=(), repr=False)


# --------------------------------------------------------------------------------------
# Spec-level views
# --------------------------------------------------------------------------------------


def spec_hash(spec: Spec) -> str:
    """Stable content hash of the raw document — the cache/checkpoint key for a spec."""
    canonical = json.dumps(spec.raw, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def operations(spec: Spec) -> list[OperationRef]:
    """Every operation in the spec, in document order, each with a unique key.

    The key is the operationId when present. OpenAPI recommends operationId be unique but does not
    enforce it, and real specs reuse one (e.g. a controller method exposed under two paths). An
    operationId shared by more than one operation is therefore disambiguated by falling back to
    `METHOD /path` (unique by construction) for every operation that shares it — otherwise distinct
    operations would collapse to one key and silently overwrite each other in the keyed structures
    downstream (the relation cache/checkpoints, snapshots, analysis), stalling resumable runs.
    """
    raw: list[tuple[str | None, str, str, Any, tuple[Any, ...]]] = []
    for path, item in (spec.model.paths or {}).items():
        if item is None:
            continue
        path_params = tuple(item.parameters or [])
        for method in HTTP_METHODS:
            op = getattr(item, method, None)
            if op is None:
                continue
            raw.append((op.operationId, method.upper(), path, op, path_params))

    id_counts = Counter(oid for oid, *_ in raw if oid)
    out: list[OperationRef] = []
    for operation_id, method, path, op, path_params in raw:
        key = operation_id if (operation_id and id_counts[operation_id] == 1) else f"{method} {path}"
        out.append(
            OperationRef(
                key=key,
                method=method,
                path=path,
                operation=op,
                path_level_params=path_params,
            )
        )
    return out


def parameters(spec: Spec, opref: OperationRef) -> list[ParamRef]:
    """Operation parameters (path-level merged with operation-level), refs resolved."""
    out: list[ParamRef] = []
    seen: set[tuple[str, str]] = set()
    raw = list(opref.path_level_params) + list(opref.operation.parameters or [])
    for p in raw:
        p = _resolve(spec, p)
        if p is None:
            continue
        location = _enum_value(p.param_in)
        ident = (p.name, location)
        if ident in seen:
            continue
        seen.add(ident)
        schema = _resolve(spec, p.param_schema) if p.param_schema is not None else None
        out.append(
            ParamRef(
                name=p.name,
                location=location,
                type=_type_str(schema),
                format=_format_str(schema),
                required=bool(p.required),
                constraints=_constraints(schema),
            )
        )
    return out


def request_bodies(spec: Spec, opref: OperationRef) -> list[BodyRef]:
    """Request-body fields per media type, as leaf JSON pointers."""
    rb = opref.operation.requestBody
    if rb is None:
        return []
    rb = _resolve(spec, rb)
    out: list[BodyRef] = []
    for media_type, media in (getattr(rb, "content", None) or {}).items():
        schema = getattr(media, "media_type_schema", None)
        out.append(BodyRef(media_type=media_type, fields=leaf_fields(spec, schema)))
    return out


def responses(spec: Spec, opref: OperationRef) -> list[ResponseRef]:
    """Response fields per (status, media type), as leaf JSON pointers. Skips bodiless responses."""
    out: list[ResponseRef] = []
    for status, resp in (opref.operation.responses or {}).items():
        resp = _resolve(spec, resp)
        if resp is None:
            continue
        for media_type, media in (getattr(resp, "content", None) or {}).items():
            schema = getattr(media, "media_type_schema", None)
            out.append(
                ResponseRef(
                    status_code=str(status),
                    media_type=media_type,
                    fields=leaf_fields(spec, schema),
                )
            )
    return out


# --------------------------------------------------------------------------------------
# Leaf-field / JSON-pointer extraction
# --------------------------------------------------------------------------------------


def leaf_fields(spec: Spec, schema: Any) -> list[FieldRef]:
    """Flatten a schema to its leaf fields as schema JSON pointers.

    Walks the `$ref`-resolved graph, appending `/properties/<name>`, `/items`, or
    `/additionalProperties` while descending and emitting a `FieldRef` at each scalar leaf.
    Recursive (`$ref`-cyclic) schemas are cut after one repeat on the current path, so the
    recursive field still surfaces one usable pointer level rather than looping forever.
    """
    if schema is None:
        return []
    out: list[FieldRef] = []
    counts: dict[int, int] = {}

    def walk(node: Any, prefix: str, depth: int) -> None:
        """Descend one schema node, extending the pointer and emitting a FieldRef at each leaf."""
        node = _resolve(spec, node)
        if node is None:
            return
        if depth > _MAX_DEPTH:
            out.append(FieldRef(prefix or "/", _type_str(node), _format_str(node), _constraints(node)))
            return

        key = id(node)
        if counts.get(key, 0) > _MAX_REPEAT:
            return
        counts[key] = counts.get(key, 0) + 1
        try:
            props = getattr(node, "properties", None)
            items = getattr(node, "items", None)
            addl = getattr(node, "additionalProperties", None)
            all_of = getattr(node, "allOf", None)

            if props:
                for name, child in props.items():
                    walk(child, f"{prefix}/properties/{name}", depth + 1)
            elif items is not None:
                walk(items, f"{prefix}/items", depth + 1)
            elif isinstance(addl, _SCHEMA_OR_REF):
                walk(addl, f"{prefix}/additionalProperties", depth + 1)
            elif all_of:
                for sub in all_of:
                    walk(sub, prefix, depth + 1)
            else:
                out.append(FieldRef(prefix or "/", _type_str(node), _format_str(node), _constraints(node)))
        finally:
            counts[key] -= 1

    walk(schema, "", 0)
    return out


# --------------------------------------------------------------------------------------
# Schema pointer -> real-data extraction
# --------------------------------------------------------------------------------------


def data_values(data: Any, schema_pointer: str) -> list[Any]:
    """Collect every value in real JSON `data` addressed by a *schema* pointer.

    Relations and `leaf_fields` speak schema pointers (`/properties/<name>`, `/items`,
    `/additionalProperties`); a real response is plain JSON. This bridges the two: it
    translates the schema alphabet to data access — `/properties/<name>` selects that key,
    `/items` fans out across a list, `/additionalProperties` fans out across a map's values —
    and returns every value reached.

    Recursive containers are followed to **full depth**: when a `/properties/<name>` step
    enters a container whose elements re-nest `<name>` (a self-referential `$ref`, which the
    schema view deliberately cuts after one repeat), this re-applies the step at each deeper
    level. So an id buried in a self-referential structure is still harvested even though its
    schema pointer only spells out one level.
    """
    tokens = [t for t in schema_pointer.split("/") if t]
    out: list[Any] = []

    def walk(node: Any, i: int) -> None:
        """Apply pointer token `i` to the live data node, recursing (and re-entering recursive
        containers at full depth) until the pointer is consumed, then collect the reached value."""
        if i >= len(tokens):
            out.append(node)
            return
        tok = tokens[i]
        if tok == "properties" and i + 1 < len(tokens):
            name = tokens[i + 1]
            if isinstance(node, dict) and name in node:
                walk(node[name], i + 2)
                if i + 2 < len(tokens) and tokens[i + 2] in ("items", "additionalProperties"):
                    for el in _children(node[name]):
                        if isinstance(el, dict) and name in el:
                            walk(el, i)  # re-enter this container step one level deeper
        elif tok == "items":
            if isinstance(node, list):
                for el in node:
                    walk(el, i + 1)
        elif tok == "additionalProperties":
            if isinstance(node, dict):
                for v in node.values():
                    walk(v, i + 1)

    walk(data, 0)
    return out


def _children(node: Any) -> list[Any]:
    """The contained elements of a list or a dict's values; empty for a scalar."""
    if isinstance(node, list):
        return node
    if isinstance(node, dict):
        return list(node.values())
    return []


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _resolve(spec: Spec, node: Any) -> Any:
    """Dereference a `$ref` node to its live target; pass non-references through unchanged."""
    if isinstance(node, _REFERENCE_TYPES):
        return spec.resolve_ref(node)
    return node


def _enum_value(value: Any) -> str:
    """The string form of an enum member (its `.value`) or of a plain value."""
    return value.value if hasattr(value, "value") else str(value)


def _type_str(schema: Any) -> str | None:
    """The schema's normalized `type` (first non-null when 3.1 gives a list), or None."""
    if schema is None:
        return None
    t = getattr(schema, "type", None)
    if t is None:
        return None
    if isinstance(t, list):  # 3.1 allows a list of types; first non-null wins
        non_null = [x for x in t if str(_enum_value(x)) != "null"]
        return _enum_value(non_null[0]) if non_null else None
    return _enum_value(t)


def _format_str(schema: Any) -> str | None:
    """The schema's `format` (e.g. uuid, date-time), or None."""
    if schema is None:
        return None
    return getattr(schema, "schema_format", None)


# Value-shaping keywords surfaced to value generators. `type`/`format` are exposed separately;
# every attribute below defaults to None on the model, so "present" means "explicitly set".
_CONSTRAINT_ATTRS = (
    "enum", "const", "pattern",
    "minLength", "maxLength",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "minItems", "maxItems",
    "default", "example", "examples",
    "description", "nullable",
)


def _constraints(schema: Any) -> dict[str, Any]:
    """The explicitly-set value-shaping keywords of a leaf schema (absent/empty ones omitted)."""
    if schema is None:
        return {}
    out: dict[str, Any] = {}
    for attr in _CONSTRAINT_ATTRS:
        val = getattr(schema, attr, None)
        if val is None or val == [] or val == {} or val == "":
            continue
        out[attr] = val
    return out
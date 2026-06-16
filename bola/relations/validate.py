"""Deterministic validation of proposed relation edges against the spec.

The LLM brings semantic judgment; this module guarantees an edge is *grounded*: the source
pointer really exists in that response, the target parameter / body field really exists, and
the types are equal or covered by the declared cast. Errors are returned as plain strings —
the same strings drive the revise pass (fed back to the LLM) and the final drop of any edge
that survives unvalidated.
"""

from __future__ import annotations

from bola.relations.models import EdgeTargetParameter, EdgeTargetRequestBody, RelationEdge
from bola.spec import views
from bola.spec.model import Spec
from bola.spec.views import FieldRef, OperationRef, ParamRef

# Casts that can legitimately bridge a source type to a target type.
_CAST_TARGETS = {
    "to_string": {"string"},
    "to_integer": {"integer"},
    "to_number": {"number"},
    "to_boolean": {"boolean"},
}


def validate_edge(
    spec: Spec, source: OperationRef, target: OperationRef, edge: RelationEdge
) -> list[str]:
    """Return a list of human-readable errors for `edge`; empty means valid."""
    errors: list[str] = []

    src_field = _source_field(spec, source, edge)
    if src_field is None:
        errors.append(
            f"source {edge.source.status_code}/{edge.source.media_type}"
            f"{edge.source.json_pointer!r} does not exist on {source.key}"
        )

    tgt_type, tgt_err = _target_type(spec, target, edge)
    if tgt_err:
        errors.append(tgt_err)

    if src_field is not None and tgt_type is not None:
        msg = _type_compatible(src_field.type, tgt_type, edge.cast)
        if msg:
            errors.append(msg)

    return errors


def _source_field(spec: Spec, source: OperationRef, edge: RelationEdge) -> FieldRef | None:
    """The source response leaf the edge points at (matching status/media/pointer), or None."""
    for resp in views.responses(spec, source):
        if resp.status_code == edge.source.status_code and resp.media_type == edge.source.media_type:
            for f in resp.fields:
                if f.json_pointer == edge.source.json_pointer:
                    return f
    return None


def _target_type(spec: Spec, target: OperationRef, edge: RelationEdge) -> tuple[str | None, str | None]:
    """Return (target_type, error). Exactly one is non-None."""
    tgt = edge.target
    if isinstance(tgt, EdgeTargetParameter):
        param = _find_param(views.parameters(spec, target), tgt.name, tgt.location)
        if param is None:
            return None, f"target parameter {tgt.name!r} in={tgt.location!r} not found on {target.key}"
        return param.type, None

    if isinstance(tgt, EdgeTargetRequestBody):
        for body in views.request_bodies(spec, target):
            if body.media_type != tgt.media_type:
                continue
            for f in body.fields:
                if f.json_pointer == tgt.json_pointer:
                    return f.type, None
        return None, (
            f"target request-body field {tgt.media_type}{tgt.json_pointer!r} not found on {target.key}"
        )

    return None, "unknown target type"


def _find_param(params: list[ParamRef], name: str, location: str) -> ParamRef | None:
    """The parameter matching (name, location), or None."""
    for p in params:
        if p.name == name and p.location == location:
            return p
    return None


def _type_compatible(src: str | None, tgt: str | None, cast: str | None) -> str | None:
    """Return an error string if the types are incompatible, else None."""
    if src is None or tgt is None:
        return None  # untyped on either side — nothing to contradict
    if src == tgt:
        return None
    if cast is None:
        return f"type mismatch without cast: source={src} target={tgt}"
    if tgt not in _CAST_TARGETS.get(cast, set()):
        return f"cast {cast!r} cannot produce target type {tgt}"
    return None
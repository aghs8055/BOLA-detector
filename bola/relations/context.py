"""Shape one `source × target-group` into the JSON context handed to the LLM.

Pure arrangement: it calls `bola.spec.views` for the flattened operation shapes and lays
them out as the `source` / `targets` document the prompt expects. It knows nothing about how
pointers are derived — only how to present them.
"""

from __future__ import annotations

from typing import Any

from bola.spec import views
from bola.spec.model import Spec
from bola.spec.views import OperationRef


def build_group_context(
    spec: Spec, source: OperationRef, targets: list[OperationRef]
) -> dict[str, Any]:
    """The context document for detecting relations from `source` into each of `targets`."""
    return {
        "source": _source_view(spec, source),
        "targets": [_target_view(spec, t) for t in targets],
    }


def _source_view(spec: Spec, source: OperationRef) -> dict[str, Any]:
    """The source operation as the LLM sees it: identity plus its response leaf fields."""
    return {
        "key": source.key,
        "method": source.method,
        "path": source.path,
        "summary": source.operation.summary,
        "responses": [
            {
                "status_code": r.status_code,
                "media_type": r.media_type,
                "fields": [_field(f) for f in r.fields],
            }
            for r in views.responses(spec, source)
            if r.fields
        ],
    }


def _target_view(spec: Spec, target: OperationRef) -> dict[str, Any]:
    """The target operation as the LLM sees it: identity plus its parameters and request-body fields."""
    return {
        "key": target.key,
        "method": target.method,
        "path": target.path,
        "summary": target.operation.summary,
        "parameters": [
            {
                "name": p.name,
                "in": p.location,
                "type": p.type,
                "format": p.format,
                "required": p.required,
            }
            for p in views.parameters(spec, target)
        ],
        "request_body": [
            {"media_type": b.media_type, "fields": [_field(f) for f in b.fields]}
            for b in views.request_bodies(spec, target)
            if b.fields
        ],
    }


def _field(f: views.FieldRef) -> dict[str, Any]:
    """A leaf field reduced to the pointer/type/format the relation prompt needs."""
    return {"json_pointer": f.json_pointer, "type": f.type, "format": f.format}
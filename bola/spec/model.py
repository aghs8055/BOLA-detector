"""The lossless canonical spec object and its cycle-safe `$ref` resolver.

`Spec` wraps the full `openapi-pydantic` object graph (the validated, lossless model) plus
the original parsed document, and exposes `resolve_ref` to dereference a `$ref` to the
*actual* node in the graph without ever inlining or flattening anything.

This module owns *the thing* (the model); obtaining one from a file/URL/dict lives in
`loader.py`. Future value-generation / LLM-description helpers belong here (or in a sibling
`views.py`) as derived views over `Spec.model`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Union

from openapi_pydantic.v3.v3_0 import OpenAPI as OpenAPI30, Reference as Reference30
from openapi_pydantic.v3.v3_1 import OpenAPI as OpenAPI31, Reference as Reference31
from pydantic import BaseModel

from bola.spec.errors import ReferenceResolutionError

logger = logging.getLogger("bola.spec.model")

OpenAPIModel = Union[OpenAPI30, OpenAPI31]

# 3.0 and 3.1 define distinct Reference classes; accept either everywhere.
_REFERENCE_TYPES = (Reference30, Reference31)
ReferenceModel = Union[Reference30, Reference31]


@dataclass
class Spec:
    """A fully loaded OpenAPI document.

    Attributes:
        model: the validated, lossless `openapi-pydantic` object (the canonical model).
        raw:   the original parsed document (dict) — nothing dropped, used for JSON-Pointer
               resolution which is defined over the JSON document.
        source: where it came from (file path or URL), for logging/debugging.
        version: the `openapi` version string (e.g. "3.0.4", "3.1.0").
    """

    model: OpenAPIModel
    raw: dict[str, Any]
    source: str
    version: str
    _ref_cache: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_3_1(self) -> bool:
        """True for an OpenAPI 3.1.x document (3.0 and 3.1 differ in a few schema constructs)."""
        return self.version.startswith("3.1")

    def resolve_ref(self, ref: Union[str, ReferenceModel]) -> Any:
        """Resolve a `$ref` to the actual node in the canonical model graph.

        Returns the *same* object instance that lives in the graph — references are never
        inlined or copied, so a recursive/circular schema is simply a cycle in the graph
        and resolving it repeatedly is O(1) and terminating (no truncation, no hang).

        Chains of pure `$ref -> $ref` are followed (with a visited guard against malformed
        ref-only cycles) until a non-Reference node is reached.

        Raises:
            ReferenceResolutionError: external document refs (not fetched) or dangling
                internal pointers.
        """
        ref_str = ref.ref if isinstance(ref, _REFERENCE_TYPES) else ref

        seen: set[str] = set()
        current = ref_str
        while True:
            if current in seen:
                raise ReferenceResolutionError(
                    f"Circular $ref chain through references: {current!r}"
                )
            seen.add(current)

            node = self._resolve_one(current)
            if isinstance(node, _REFERENCE_TYPES):
                current = node.ref
                continue
            return node

    def _resolve_one(self, ref_str: str) -> Any:
        """Resolve a single in-document `$ref` string to its node (cached); no chain following."""
        if ref_str in self._ref_cache:
            return self._ref_cache[ref_str]

        doc_uri, _, fragment = ref_str.partition("#")
        if doc_uri:
            raise ReferenceResolutionError(
                f"External $ref not resolved (only in-document refs are supported): {ref_str!r}"
            )
        if fragment and not fragment.startswith("/"):
            raise ReferenceResolutionError(
                f"Unsupported $ref fragment (expected a JSON Pointer): {ref_str!r}"
            )

        tokens = _decode_pointer(fragment)
        try:
            node = _navigate(self.model, tokens)
        except (KeyError, IndexError, AttributeError) as exc:
            raise ReferenceResolutionError(
                f"Dangling $ref — pointer not found in document: {ref_str!r}"
            ) from exc

        logger.debug("resolved $ref %s -> %s", ref_str, type(node).__name__)
        self._ref_cache[ref_str] = node
        return node


# --------------------------------------------------------------------------------------
# JSON-Pointer navigation (RFC 6901) over the model graph — serves resolve_ref only
# --------------------------------------------------------------------------------------


def _decode_pointer(fragment: str) -> list[str]:
    """Decode a JSON Pointer fragment (RFC 6901) into its reference tokens."""
    if fragment in ("", "/"):
        return [] if fragment == "" else [""]
    return [tok.replace("~1", "/").replace("~0", "~") for tok in fragment.lstrip("/").split("/")]


def _navigate(node: Any, tokens: list[str]) -> Any:
    """Walk JSON-Pointer `tokens` over the model graph, returning the live node.

    Handles dicts (key), lists (index), and pydantic models (field by alias or name,
    falling back to extension/extra fields). Raises KeyError/IndexError/AttributeError on
    a miss, which the caller turns into ReferenceResolutionError.
    """
    for token in tokens:
        if isinstance(node, BaseModel):
            node = _model_child(node, token)
        elif isinstance(node, dict):
            node = node[token]
        elif isinstance(node, (list, tuple)):
            node = node[int(token)]
        else:
            raise KeyError(token)
    return node


def _model_child(model: BaseModel, token: str) -> Any:
    """Get a child of a pydantic model by its JSON key (alias-aware, extra-aware)."""
    for name, info in type(model).model_fields.items():
        if info.alias == token or name == token:
            return getattr(model, name)
    extra = model.model_extra or {}
    if token in extra:
        return extra[token]
    raise KeyError(token)
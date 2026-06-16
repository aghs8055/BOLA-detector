"""Obtain a `Spec` from a file path, http(s) URL, or already-parsed dict.

This module is *only* about loading: reading bytes, parsing JSON/YAML, checking the version,
and validating into the `openapi-pydantic` model. The loaded object (`Spec`) and `$ref`
resolution live in `model.py`; the error hierarchy lives in `errors.py`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import requests
import yaml
from openapi_pydantic.v3.parser import parse_obj
from pydantic import ValidationError

from bola.spec.errors import SpecLoadError, SpecParseError, SpecValidationError
from bola.spec.model import Spec

logger = logging.getLogger("bola.spec.loader")

_HTTP_PREFIXES = ("http://", "https://")
_SUPPORTED_VERSION_PREFIXES = ("3.0", "3.1")


def load_spec(source: str, *, timeout: int = 30) -> Spec:
    """Load and validate an OpenAPI 3.0/3.1 document from a file path or http(s) URL.

    Args:
        source: local filesystem path, or an http:// / https:// URL.
        timeout: network timeout in seconds for URL loads.

    Raises:
        SpecLoadError, SpecParseError, SpecValidationError.
    """
    text = _read_source(source, timeout=timeout)
    data = _parse_text(text, source)
    return load_spec_from_dict(data, source=source)


def load_spec_from_dict(data: dict[str, Any], *, source: str = "<dict>") -> Spec:
    """Build a Spec from an already-parsed document. Validates version + structure."""
    if not isinstance(data, dict):
        raise SpecParseError(f"Spec root must be a JSON object, got {type(data).__name__} ({source})")

    version = data.get("openapi")
    if not isinstance(version, str):
        raise SpecValidationError(
            f"Missing or non-string 'openapi' version field ({source})"
        )
    if not version.startswith(_SUPPORTED_VERSION_PREFIXES):
        raise SpecValidationError(
            f"Unsupported OpenAPI version {version!r} — only 3.0.x and 3.1.x are supported ({source})"
        )

    try:
        model = parse_obj(data)
    except ValidationError as exc:
        raise SpecValidationError(
            f"OpenAPI document failed validation ({source}):\n{exc}"
        ) from exc

    spec = Spec(model=model, raw=data, source=source, version=version)
    logger.info(
        "loaded OpenAPI %s from %s (%d paths, %d component schemas)",
        version,
        source,
        len(spec.model.paths or {}),
        len((spec.model.components.schemas if spec.model.components else None) or {}),
    )
    return spec


def _read_source(source: str, *, timeout: int) -> str:
    """Return the raw document text, fetching an http(s) URL or reading a local file."""
    if source.startswith(_HTTP_PREFIXES):
        try:
            resp = requests.get(source, timeout=timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise SpecLoadError(f"Could not fetch spec from URL {source!r}: {exc}") from exc
        return resp.text

    path = Path(source)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SpecLoadError(f"Could not read spec file {source!r}: {exc}") from exc


def _parse_text(text: str, source: str) -> Any:
    """Parse document text as JSON, then YAML. JSON is a subset of YAML, but trying it first gives
    cleaner errors for JSON specs."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SpecParseError(f"Spec is not valid JSON or YAML ({source}): {exc}") from exc
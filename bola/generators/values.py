"""Simple, no-LLM value generators for request inputs a relation does not supply.

When the executor must fill a required parameter or body field and no harvested id flows into
it, a generator produces a plausible value from the field's OpenAPI `type`/`format`. These are
pure functions in a small named registry: `generate_for_schema` dispatches on type/format for
the executor's default fill, while `get`/`describe_all` let the AI strategy (Step 5) pick a
named generator deliberately. No schema mechanics here — callers pass the already-extracted
`type`/`format` from `bola.spec.views`.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Generator:
    """A named value generator: its identifier, a human description, and the producing function."""

    name: str
    description: str
    fn: Callable[[], object]


def _string() -> str:
    """A short random string."""
    return f"bola-{random.randint(1000, 9999)}"


def _integer() -> int:
    """A random positive integer."""
    return random.randint(1, 1000)


def _number() -> float:
    """A random decimal number."""
    return round(random.uniform(1.0, 1000.0), 2)


def _boolean() -> bool:
    """A random boolean."""
    return random.choice([True, False])


def _email() -> str:
    """A random example.com email address."""
    return f"bola-{random.randint(1000, 9999)}@example.com"


def _uuid() -> str:
    """A random UUID v4 string."""
    return str(uuid.uuid4())


def _url() -> str:
    """A random https URL."""
    return f"https://example.com/{random.randint(1000, 9999)}"


def _date() -> str:
    """A fixed ISO date (YYYY-MM-DD)."""
    return "2024-01-15"


def _datetime() -> str:
    """A fixed ISO date-time."""
    return "2024-01-15T10:30:00Z"


def _password() -> str:
    """A random password-shaped string."""
    return f"Bola-{random.randint(1000, 9999)}!"


def _binary() -> str:
    """Stand-in text contents for an uploaded file when the model has no specific content in mind.

    For a file whose contents matter (a description naming a format), the AI strategy authors them
    via a `file` `InputChoice` instead; this is only the no-information fallback.
    """
    return f"bola-file-{random.randint(1000, 9999)}"


_REGISTRY: dict[str, Generator] = {
    g.name: g
    for g in [
        Generator("string", "A short random string", _string),
        Generator("integer", "A random positive integer", _integer),
        Generator("number", "A random decimal number", _number),
        Generator("boolean", "A random true/false", _boolean),
        Generator("email", "A random email address", _email),
        Generator("uuid", "A random UUID v4", _uuid),
        Generator("url", "A random https URL", _url),
        Generator("date", "An ISO date (YYYY-MM-DD)", _date),
        Generator("date-time", "An ISO date-time", _datetime),
        Generator("password", "A random password", _password),
        Generator("binary", "Stand-in contents for an uploaded file", _binary),
    ]
}

# OpenAPI `format` values that map to a dedicated generator (preferred over the bare type).
_FORMAT_GENERATORS = {
    "email": "email",
    "uuid": "uuid",
    "uri": "url",
    "url": "url",
    "date": "date",
    "date-time": "date-time",
    "password": "password",
    "binary": "binary",
    "byte": "binary",
}


def get(name: str) -> Generator | None:
    """A named generator, or None if unknown."""
    return _REGISTRY.get(name)


def describe_all() -> list[dict[str, str]]:
    """Every generator's name + description — context for the AI strategy."""
    return [{"name": g.name, "description": g.description} for g in _REGISTRY.values()]


def generate_for_schema(type_: str | None, format_: str | None = None) -> object:
    """Generate a value for an OpenAPI `type`/`format`, preferring a format-specific generator."""
    if format_ and format_ in _FORMAT_GENERATORS:
        return _REGISTRY[_FORMAT_GENERATORS[format_]].fn()
    if type_ in ("array",):
        return []
    if type_ in ("object",):
        return {}
    gen = _REGISTRY.get(type_ or "string")
    return (gen or _REGISTRY["string"]).fn()
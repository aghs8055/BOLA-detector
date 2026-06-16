"""Typed errors for spec loading and reference resolution.

Kept in their own module so loader, model, and every downstream consumer can import the
exception hierarchy from a stable, dependency-free location.
"""


class SpecError(Exception):
    """Base class for every spec-loading failure."""


class SpecLoadError(SpecError):
    """The document could not be read or fetched."""


class SpecParseError(SpecError):
    """The document was read but is not parseable JSON/YAML, or is not a JSON object."""


class SpecValidationError(SpecError):
    """The document parsed but is not a valid/supported OpenAPI 3.0/3.1 spec."""


class ReferenceResolutionError(SpecError):
    """A `$ref` could not be resolved (external document, or dangling internal pointer).

    The reference itself is still preserved losslessly in the model; this error only means
    we declined/were unable to dereference it to a concrete node.
    """
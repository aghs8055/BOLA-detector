"""OpenAPI spec loading and the lossless canonical model.

`load_spec` is the entry point; `Spec` is the loaded document (typed model + raw dict +
cycle-safe `$ref` resolution). Future value-generation / LLM-description helpers belong
here as derived views over `Spec.model`.
"""

from bola.spec.errors import (
    ReferenceResolutionError,
    SpecError,
    SpecLoadError,
    SpecParseError,
    SpecValidationError,
)
from bola.spec.loader import load_spec, load_spec_from_dict
from bola.spec.model import Spec

__all__ = [
    "Spec",
    "load_spec",
    "load_spec_from_dict",
    "SpecError",
    "SpecLoadError",
    "SpecParseError",
    "SpecValidationError",
    "ReferenceResolutionError",
]
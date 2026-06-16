"""Relation detection: LLM-proposed, spec-validated data-flow edges between operations.

`detect_relations` is the entry point (resumable, cached); `RelationDetector` runs the
detect/validate/revise LangGraph for a single `source × target-group`. See `README.md` for the
group model, the revise loop, and the resume/checkpoint design.
"""

from bola.relations.detector import RelationDetector
from bola.relations.models import (
    GroupRelations,
    Relation,
    RelationEdge,
    TargetRelations,
)
from bola.relations.runner import DetectionResult, detect_relations

__all__ = [
    "detect_relations",
    "DetectionResult",
    "RelationDetector",
    "GroupRelations",
    "TargetRelations",
    "RelationEdge",
    "Relation",
]
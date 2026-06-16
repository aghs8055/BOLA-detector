"""LangGraph detect -> validate -> revise loop for one `source × target-group`.

The graph is the agentic core: `detect` proposes edges (structured LLM call), `validate`
grounds every edge against the spec, and `revise` gives the model one bounded chance to
repair the edges that validation flagged. Only edges that ultimately validate are returned;
unvalidated edges are dropped, never persisted.

The two LLM chains are injectable so unit tests run the full graph with canned outputs and no
network. The graph shape (and thus the resume/checkpoint seam in `runner.py`) is identical
whether the chains are real or fake.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from langchain_core.prompts import PromptTemplate
from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from bola.llm import build_chat_model
from bola.relations.context import build_group_context
from bola.relations.models import (
    EdgeTargetRequestBody,
    GroupRelations,
    RelationEdge,
    TargetRelations,
)
from bola.relations.validate import validate_edge
from bola.settings import Settings
from bola.spec import views
from bola.spec.model import Spec
from bola.spec.views import OperationRef

logger = logging.getLogger("bola.relations.detector")

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "relations"


class GroupState(BaseModel):
    """LangGraph state for one `source × target-group` detection: context, proposal, best-so-far."""

    context: dict
    source_key: str
    proposal: Optional[GroupRelations] = None
    best: Optional[GroupRelations] = None  # most validated edges seen across attempts
    errors: list[str] = Field(default_factory=list)
    attempts: int = 0


class RelationDetector:
    """Runs the detect/validate/revise graph for groups of one spec."""

    def __init__(
        self,
        spec: Spec,
        settings: Settings,
        *,
        detect_chain: Any = None,
        revise_chain: Any = None,
        callbacks: Optional[list] = None,
    ):
        self.spec = spec
        self.settings = settings
        self.max_attempts = max(1, settings.relations.max_attempts)
        self._ops_by_key = {o.key: o for o in views.operations(spec)}
        self.detect_chain = detect_chain or self._build_chain("detect_relation_group.md")
        self.revise_chain = revise_chain or self._build_chain("revise_relation_group.md")
        self._callbacks = callbacks or []
        self.graph = self._build_graph()

    def detect_group(self, source: OperationRef, targets: list[OperationRef]) -> GroupRelations:
        """Detect, validate, and (if needed) revise relations from `source` into `targets`.

        Returns only edges that validate against the spec, expanded across request-body media
        types.
        """
        state = self._initial_state(source, targets)
        config: dict = {"metadata": {"source_key": source.key, "group_size": len(targets)}}
        if self._callbacks:
            config["callbacks"] = self._callbacks
        final = self.graph.invoke(state, config=config)
        return self._finalize(source, _best(final))

    def detect_groups(
        self, jobs: list[tuple[OperationRef, list[OperationRef]]]
    ) -> list[GroupRelations | BaseException]:
        """Run several `source × target-group` detections concurrently (one batch).

        Returns one result per job, in order; a per-job exception is returned in place (not
        raised) so the caller can checkpoint the successes and record the failures.
        """
        if not jobs:
            return []
        states = [self._initial_state(s, t) for s, t in jobs]
        config: dict = {"max_concurrency": len(states)}
        if self._callbacks:
            config["callbacks"] = self._callbacks
        finals = self.graph.batch(states, config=config, return_exceptions=True)
        out: list[GroupRelations | BaseException] = []
        for (source, _targets), final in zip(jobs, finals):
            if isinstance(final, BaseException):
                out.append(final)
            else:
                out.append(self._finalize(source, _best(final)))
        return out

    def _initial_state(self, source: OperationRef, targets: list[OperationRef]) -> GroupState:
        """Build the starting graph state (the LLM context) for one detection unit."""
        return GroupState(
            context=build_group_context(self.spec, source, targets), source_key=source.key
        )

    def _build_graph(self):
        """Compile the detect → validate → (revise → validate)* → END state graph."""
        g = StateGraph(GroupState)
        g.add_node("detect", self._node_detect)
        g.add_node("validate", self._node_validate)
        g.add_node("revise", self._node_revise)
        g.set_entry_point("detect")
        g.add_edge("detect", "validate")
        g.add_conditional_edges(
            "validate",
            self._decide,
            {"done": END, "revise": "revise"},
        )
        g.add_edge("revise", "validate")
        return g.compile()

    def _node_detect(self, state: GroupState) -> dict:
        """Graph node: ask the model for an initial edge proposal."""
        proposal = self.detect_chain.invoke(
            {"context": _json(state.context)}, config=self._chain_config()
        )
        return {"proposal": proposal, "attempts": state.attempts + 1}

    def _node_validate(self, state: GroupState) -> dict:
        """Graph node: validate the proposal against the spec and keep the best projection so far."""
        source = self._ops_by_key.get(state.source_key)
        errors = self._collect_errors(source, state.proposal)
        validated = self._validated_only(source, state.proposal)
        best = state.best
        if best is None or _edge_count(validated) > _edge_count(best):
            best = validated
        return {"errors": errors, "best": best}

    def _node_revise(self, state: GroupState) -> dict:
        """Graph node: ask the model to repair the flagged edges (one bounded revise pass)."""
        previous = state.proposal.model_dump_json(indent=2) if state.proposal else "{}"
        proposal = self.revise_chain.invoke(
            {
                "context": _json(state.context),
                "previous": previous,
                "errors": "\n".join(state.errors) or "(none)",
            },
            config=self._chain_config(),
        )
        return {"proposal": proposal, "attempts": state.attempts + 1}

    def _decide(self, state: GroupState) -> str:
        """Graph branch: finish when there are no errors or the attempt budget is spent, else revise."""
        if not state.errors:
            return "done"
        if state.attempts >= self.max_attempts:
            return "done"
        return "revise"

    def _collect_errors(
        self, source: Optional[OperationRef], proposal: Optional[GroupRelations]
    ) -> list[str]:
        """Spec-validation errors for every model-affirmed edge in the proposal (drives revise)."""
        if proposal is None or source is None:
            return []
        errors: list[str] = []
        for tr in proposal.targets:
            target = self._ops_by_key.get(tr.target_key)
            if target is None:
                errors.append(f"unknown target_key {tr.target_key!r}")
                continue
            for edge in tr.edges:
                if not edge.has_relation:
                    continue  # the model itself rejected this edge; nothing to validate or revise
                errors.extend(validate_edge(self.spec, source, target, edge))
        return errors

    def _validated_only(
        self, source: Optional[OperationRef], proposal: Optional[GroupRelations]
    ) -> GroupRelations:
        """The proposal reduced to only its model-affirmed, spec-valid edges."""
        if proposal is None or source is None:
            return GroupRelations()
        kept: list[TargetRelations] = []
        for tr in proposal.targets:
            target = self._ops_by_key.get(tr.target_key)
            if target is None:
                continue
            edges = [
                e
                for e in tr.edges
                if e.has_relation and not validate_edge(self.spec, source, target, e)
            ]
            if edges:
                kept.append(TargetRelations(target_key=tr.target_key, edges=edges))
        return GroupRelations(targets=kept)

    def _finalize(
        self, source: OperationRef, best: Optional[GroupRelations]
    ) -> GroupRelations:
        """Expand each validated body edge across every media type whose schema carries it.

        `best` already holds only spec-validated edges (the graph filters as it goes); this adds
        the deterministic media-type fan-out the prompt also asks the model for, so completeness
        across encodings does not depend on the LLM.
        """
        if best is None:
            return GroupRelations()
        kept: list[TargetRelations] = []
        for tr in best.targets:
            target = self._ops_by_key.get(tr.target_key)
            if target is None:
                continue
            edges = self._expand_media_types(target, tr.edges)
            if edges:
                kept.append(TargetRelations(target_key=tr.target_key, edges=edges))
        return GroupRelations(targets=kept)

    def _expand_media_types(
        self, target: OperationRef, edges: list[RelationEdge]
    ) -> list[RelationEdge]:
        """Duplicate each validated request-body edge across every media type whose schema carries it."""
        bodies = views.request_bodies(self.spec, target)
        out: list[RelationEdge] = []
        seen: set[tuple] = set()
        for edge in edges:
            variants = [edge]
            tgt = edge.target
            if isinstance(tgt, EdgeTargetRequestBody):
                media_types = [
                    b.media_type
                    for b in bodies
                    if any(f.json_pointer == tgt.json_pointer for f in b.fields)
                ]
                if media_types:
                    variants = [
                        edge.model_copy(
                            update={"target": tgt.model_copy(update={"media_type": mt})}
                        )
                        for mt in media_types
                    ]
            for v in variants:
                key = _edge_key(v)
                if key not in seen:
                    seen.add(key)
                    out.append(v)
        return out

    def _build_chain(self, template_name: str):
        """A named prompt template piped into a chat model with `GroupRelations` structured output."""
        text = (_TEMPLATE_DIR / template_name).read_text(encoding="utf-8")
        prompt = PromptTemplate.from_template(text)
        llm = build_chat_model(self.settings)
        return prompt | llm.with_structured_output(GroupRelations)

    def _chain_config(self) -> dict:
        """LangChain config carrying the Langfuse callbacks, when any are set."""
        return {"callbacks": self._callbacks} if self._callbacks else {}


def _json(obj: Any) -> str:
    """Pretty-printed JSON for embedding in the prompt."""
    return json.dumps(obj, indent=2, default=str)


def _best(final: Any) -> Optional[GroupRelations]:
    """The `best` projection from a finished graph state (dict or model)."""
    return final["best"] if isinstance(final, dict) else final.best


def _edge_count(group: GroupRelations) -> int:
    """Total edges across all targets in a group result."""
    return sum(len(t.edges) for t in group.targets)


def _edge_key(edge: RelationEdge) -> tuple:
    """A hashable identity for an edge (source + target coordinates + cast), for dedupe."""
    t = edge.target
    return (
        edge.source.status_code,
        edge.source.media_type,
        edge.source.json_pointer,
        getattr(t, "name", None),
        getattr(t, "location", None),
        getattr(t, "media_type", None),
        getattr(t, "json_pointer", None),
        edge.cast,
    )
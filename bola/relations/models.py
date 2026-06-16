"""Pydantic models for the relation-detection LLM I/O and the persisted relation record.

`GroupRelations` is the structured output of one `source × target-group` LLM call. A relation
is one directed `RelationEdge`: a field in a SOURCE response that flows into a TARGET input
(a parameter, or a request-body field).
"""

from __future__ import annotations

from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

Cast = Literal["to_string", "to_integer", "to_number", "to_boolean"]


class EdgeSource(BaseModel):
    """A field in a source operation's response."""

    status_code: str = Field(description="HTTP status code of the source response, e.g. '200'")
    media_type: str = Field(description="Media type of the source response content")
    json_pointer: str = Field(
        description="Schema JSON pointer into the source response, e.g. /properties/id"
    )


class EdgeTargetParameter(BaseModel):
    """An edge target that is an operation parameter (path/query/header/cookie)."""

    type: Literal["parameter"] = "parameter"
    name: str = Field(description="Parameter name on the target operation")
    location: str = Field(description="Parameter location: path | query | header | cookie")


class EdgeTargetRequestBody(BaseModel):
    """An edge target that is a field inside the target operation's request body."""

    type: Literal["request_body"] = "request_body"
    media_type: str = Field(default="application/json")
    json_pointer: str = Field(description="Schema JSON pointer into the target request body")


EdgeTarget = Union[EdgeTargetParameter, EdgeTargetRequestBody]


class RelationEdge(BaseModel):
    """One directed data-flow edge: source response field -> target input field.

    `has_relation` comes last, after `rationale`, on purpose: the model writes its reasoning
    first and then commits a verdict, so a rationale that concludes "not a real identifier" can
    set `has_relation` false. Edges with `has_relation` false are dropped, never persisted.
    """

    source: EdgeSource
    target: EdgeTarget
    cast: Optional[Cast] = Field(
        default=None, description="Type cast when source and target types differ; null if equal"
    )
    rationale: str = Field(description="Why this source field maps to this target input")
    has_relation: bool = Field(
        default=True,
        description="After reasoning in `rationale`, your verdict: true only if this is a genuine "
        "identifier/foreign-key carry-over. Set false if the rationale concludes it is not.",
    )


class TargetRelations(BaseModel):
    """All edges from the group's source operation into one target operation."""

    target_key: str = Field(description="Stable key of the target operation, exactly as given")
    edges: list[RelationEdge] = Field(default_factory=list)


class GroupRelations(BaseModel):
    """Structured output of one source × target-group detection call."""

    model_config = ConfigDict(extra="ignore")

    targets: list[TargetRelations] = Field(default_factory=list)


class Relation(BaseModel):
    """A persisted/assembled relation edge, flattened with both operation keys.

    This is the consolidated shape written to the relation cache for downstream steps;
    `source_key` is carried alongside each edge so the full list is self-contained.
    """

    source_key: str
    target_key: str
    edge: RelationEdge
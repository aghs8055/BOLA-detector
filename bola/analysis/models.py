"""Pydantic models for the BOLA analysis LLM I/O.

The analyzer judges one object at a time: given that object's three snapshots (victim-before,
attacker, victim-after) plus an access-model hint, it returns an `ObjectVerdict`. A batch of
objects is packed into one call (`AnalysisBatch`), which returns one verdict per object.

`is_bola` is placed **after** `rationale`, like the relation `has_relation` verdict: the model
reasons first and then commits, so a rationale that concludes "this is the attacker's own object"
can set `is_bola` false. Calls are independent — each verdict is a local judgement over fixed
evidence, so no memory is threaded between them.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ObjectVerdict(BaseModel):
    """The BOLA verdict for one object, after reasoning over its three snapshots."""

    object_key: str = Field(description="Object identity, exactly as given in the context")
    rationale: str = Field(description="Why this is or is not a BOLA, citing the snapshot differences")
    unauthorized_read: bool = Field(
        default=False,
        description="True if the attacker snapshot returned the victim's object data it should not see",
    )
    unauthorized_write: bool = Field(
        default=False,
        description="True if the victim's after-snapshot shows a change the attacker caused",
    )
    is_bola: bool = Field(
        default=False,
        description="After reasoning in `rationale`, your verdict: true only if this object is a "
        "genuine broken-object-level-authorization finding (an unauthorized read or write).",
    )


class AnalysisBatch(BaseModel):
    """Structured output of one analysis call: a verdict per object in the batch."""

    model_config = ConfigDict(extra="ignore")

    verdicts: list[ObjectVerdict] = Field(default_factory=list)
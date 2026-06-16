"""Pydantic models for the AI strategy's LLM I/O — one turn in, one turn out.

The model is handed the operations, the known relations, the live id pool, the value
generators, its own memory, and the most recent results; it returns an `AgentTurn`: an updated
`AgentMemory`, a batch of `PlannedCall`s, and an `is_done` verdict. Each input on a call is an
`InputChoice` — the model decides per input whether to reuse a harvested id (`repo`), produce one
from a named generator (`generator`, optionally parametrised), or supply a literal (`concrete`).

`AgentMemory` is genuine reasoning state, not reconstructable by replaying harvests, so the run
manager (Step 7) snapshots it per run and feeds it back on resume. The strategy itself only reads
and rewrites it; it never persists.
"""

from __future__ import annotations

from typing import Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class GenArg(BaseModel):
    """One argument to a parametrised generator (scalar value, schema-friendly)."""

    name: str
    value: Union[str, int, float, bool]


class GeneratorChoice(BaseModel):
    """Fill the input from a named value generator (see the generator catalogue in context)."""

    kind: Literal["generator"] = "generator"
    name: str = Field(description="Generator name, exactly as listed in `generators`")
    args: list[GenArg] = Field(default_factory=list)

    def args_dict(self) -> dict:
        """The generator arguments as a plain name→value mapping."""
        return {a.name: a.value for a in self.args}


class RepoChoice(BaseModel):
    """Reuse a harvested id from the field-repo pool — the BOLA replay move."""

    kind: Literal["repo"] = "repo"
    source_key: str = Field(description="Source operation key the value was harvested from")
    json_pointer: str = Field(description="Schema pointer the value was harvested at")
    index: int = Field(default=0, description="Which harvested value, when several exist")


class ConcreteChoice(BaseModel):
    """Supply a literal value for the input."""

    kind: Literal["concrete"] = "concrete"
    value: Union[str, int, float, bool, None] = None


class FileChoice(BaseModel):
    """Upload a file for the input, with contents the model authors itself.

    Unlike `concrete` (a scalar folded into the JSON/form body), this is sent as a real
    `multipart/form-data` file part: the model decides the `content`, and `filename`/`content_type`
    let the server treat it as a genuine upload. Use it for a body field whose `format` is `binary`
    (or whose description says it takes a file).
    """

    kind: Literal["file"] = "file"
    content: str = Field(description="The file's contents, authored by you (e.g. one item per line)")
    filename: str = Field(default="upload.txt", description="Filename to send for the part")
    content_type: Union[str, None] = Field(
        default=None, description="MIME type of the part, e.g. text/plain (omit to let it default)"
    )


# A plain Union (not a discriminated one): a `Field(discriminator=...)` emits a JSON-Schema
# `oneOf`, which some providers (Anthropic) reject for tool/structured output. A plain Union
# emits `anyOf` (as the relation models do) and the `kind` literal still selects the right member.
InputChoice = Union[GeneratorChoice, RepoChoice, ConcreteChoice, FileChoice]


class ParamInput(BaseModel):
    """A value for one operation parameter."""

    name: str
    location: str = Field(description="path | query | header | cookie")
    choice: InputChoice


class BodyInput(BaseModel):
    """A value for one request-body leaf field, addressed by its schema pointer."""

    media_type: str
    json_pointer: str
    choice: InputChoice


class ExtraBodyField(BaseModel):
    """An UNDOCUMENTED top-level body field to inject (not in the schema) — for mass-assignment probes."""

    name: str = Field(description="The undocumented field name to inject at the body root")
    choice: InputChoice


class PlannedCall(BaseModel):
    """One operation the model wants to call, with a value chosen for each input it fills."""

    op_key: str = Field(description="Operation key, exactly as listed in `operations`")
    identity: Literal["regular", "attacker"] = Field(
        default="attacker",
        description="Which user's credentials to send this call with. During the BUILD stage every "
        "call runs as `regular` regardless of this field; during the ATTACK stage choose per call "
        "(default `attacker` — replaying the regular user's ids under the attacker's creds is the "
        "core of the test).",
    )
    parameters: list[ParamInput] = Field(default_factory=list)
    body: list[BodyInput] = Field(default_factory=list)
    extra_body: list[ExtraBodyField] = Field(
        default_factory=list,
        description="UNDOCUMENTED top-level body fields to inject — fields NOT in the operation's "
        "schema. Use to probe mass-assignment (e.g. an owner/scope field the API honours but does not "
        "advertise). Each is a name plus a value chosen like any other input.",
    )
    rationale: str = Field(description="Why this call, in one line")


class AgentMemory(BaseModel):
    """The model's working memory — carried across turns and persisted by the run manager."""

    notes: str = Field(default="", description="Free-form scratch notes")
    conclusions: list[str] = Field(default_factory=list, description="What has been established")
    plan: list[str] = Field(default_factory=list, description="What to try next, in order")
    open_questions: list[str] = Field(default_factory=list)


class AgentTurn(BaseModel):
    """Structured output of one strategy turn."""

    model_config = ConfigDict(extra="ignore")

    memory: AgentMemory = Field(default_factory=AgentMemory)
    calls: list[PlannedCall] = Field(default_factory=list)
    ready_to_attack: bool = Field(
        default=False,
        description="Set true once you have built up enough object identifiers and want to switch "
        "from the BUILD stage (acting as the regular user) to the ATTACK stage (acting as the "
        "attacker). From the turn you set this, your calls run with attacker credentials. The "
        "snapshot of the victim's baseline is taken at this boundary, so only set it when you are "
        "genuinely ready to start crossing.",
    )
    is_done: bool = Field(
        default=False, description="True when no further calls are worth making"
    )
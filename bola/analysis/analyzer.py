"""The LLM-as-judge: decide, per object, whether its snapshots show a BOLA.

The analyzer is a thin wrapper over one structured LLM call. It is handed a batch of objects (each
with its three snapshots) plus the access hint, and returns an `AnalysisBatch` — one
`ObjectVerdict` per object. Calls are **independent and stateless**: unlike the AI strategy, which
explores and carries `AgentMemory`, analysis judges fixed evidence, so each call stands alone. That
is what lets the runner batch and resume freely and re-run a whole pass reproducibly.

`objects_per_call` (how many objects packed into one prompt) is the runner's concern; the analyzer
just runs the calls it is given, optionally several concurrently (`analyze_batches`, the
`graph.batch` equivalent — a per-call exception is returned in place, not raised). The chat chain is
injectable so unit tests run with a canned `AnalysisBatch` and no network, exactly like the relation
detector and the AI strategy.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from langchain_core.prompts import PromptTemplate

from bola.analysis.context import build_object_context, build_response_context
from bola.analysis.models import AnalysisBatch
from bola.llm import build_chat_model
from bola.settings import Settings

logger = logging.getLogger("bola.analysis.analyzer")

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "bola"


class Analyzer:
    """Judge batches of objects for BOLA with one structured LLM call each."""

    def __init__(
        self,
        settings: Settings,
        *,
        access_description: str = "",
        chain: Any = None,
        response_chain: Any = None,
        callbacks: Optional[list] = None,
    ):
        self.settings = settings
        self.access_description = access_description
        self._callbacks = callbacks or []
        self.model_name = settings.llm.analysis_model or settings.llm.model
        self.chain = chain or self._build_chain()
        self._response_chain = response_chain  # built lazily so an unused path needs no credentials

    def analyze_batch(self, objects: list[dict]) -> AnalysisBatch:
        """Judge one batch of objects (one LLM call)."""
        context = build_object_context(objects, self.access_description)
        config = {"callbacks": self._callbacks} if self._callbacks else {}
        return self.chain.invoke({"context": _json(context)}, config=config)

    def analyze_batches(
        self, batches: list[list[dict]]
    ) -> list[AnalysisBatch | BaseException]:
        """Judge several object-batches concurrently (one batch == one prompt).

        Returns one result per batch, in order; a per-batch exception is returned in place (not
        raised) so the caller can checkpoint the successes and record the failures.
        """
        if not batches:
            return []
        inputs = [
            {"context": _json(build_object_context(objs, self.access_description))}
            for objs in batches
        ]
        config: dict = {"max_concurrency": len(inputs)}
        if self._callbacks:
            config["callbacks"] = self._callbacks
        return self.chain.batch(inputs, config=config, return_exceptions=True)

    def analyze_response_batches(
        self, batches: list[list[dict]]
    ) -> list[AnalysisBatch | BaseException]:
        """Judge several batches of attacker replies (reply-only read crossings), one prompt each.

        Mirrors `analyze_batches` but over `response_objects` evidence and the response-judge prompt;
        a per-batch exception is returned in place so the caller can checkpoint and record failures.
        """
        if not batches:
            return []
        inputs = [
            {"context": _json(build_response_context(objs, self.access_description))}
            for objs in batches
        ]
        config: dict = {"max_concurrency": len(inputs)}
        if self._callbacks:
            config["callbacks"] = self._callbacks
        if self._response_chain is None:
            self._response_chain = self._build_chain("response_judge.md")
        return self._response_chain.batch(inputs, config=config, return_exceptions=True)

    def _build_chain(self, template_name: str = "analyze.md"):
        """A named analysis prompt piped into a chat model with `AnalysisBatch` structured output."""
        text = (_TEMPLATE_DIR / template_name).read_text(encoding="utf-8")
        prompt = PromptTemplate.from_template(text)
        llm = build_chat_model(self.settings, model=self.model_name)
        return prompt | llm.with_structured_output(AnalysisBatch)


def _json(obj: Any) -> str:
    """Pretty-printed JSON for embedding in the prompt."""
    return json.dumps(obj, indent=2, default=str)
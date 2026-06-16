"""The report aggregator: one structured LLM call that fuses a run's evidence into a report.

A thin wrapper over a single structured call, exactly like the analyzer and the relation detector:
given the assembled context (target + access model + spec summary + analysis findings + metrics +
AI memory) it returns one `AggregatedReport`. Stateless and injectable, so unit tests run it with a
canned report and no network. The model defaults to `analysis_model` (the judge model) and falls
back to the main model.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from langchain_core.prompts import PromptTemplate

from bola.aggregate.models import AggregatedReport
from bola.llm import build_chat_model
from bola.settings import Settings

logger = logging.getLogger("bola.aggregate.aggregator")

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "aggregate"


class ReportAggregator:
    """Fuse a run's findings + AI memory into one structured report with a single LLM call."""

    def __init__(
        self,
        settings: Settings,
        *,
        chain: Any = None,
        callbacks: Optional[list] = None,
    ):
        self.settings = settings
        self._callbacks = callbacks or []
        self.model_name = settings.llm.analysis_model or settings.llm.model
        self.chain = chain or self._build_chain()

    def aggregate(self, context: dict) -> AggregatedReport:
        """Produce the aggregated report for one run (one structured LLM call)."""
        config = {"callbacks": self._callbacks} if self._callbacks else {}
        return self.chain.invoke({"context": _json(context)}, config=config)

    def _build_chain(self):
        """The aggregation prompt piped into a chat model with `AggregatedReport` structured output."""
        text = (_TEMPLATE_DIR / "aggregate_report.md").read_text(encoding="utf-8")
        prompt = PromptTemplate.from_template(text)
        llm = build_chat_model(self.settings, model=self.model_name)
        return prompt | llm.with_structured_output(AggregatedReport)


def _json(obj: Any) -> str:
    """Pretty-printed JSON for embedding in the prompt."""
    return json.dumps(obj, indent=2, default=str)
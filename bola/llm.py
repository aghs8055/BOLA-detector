"""Chat-model and Langfuse construction from `Settings`.

Cross-cutting because every LLM step (relations now; strategies/analysis later) needs the
same OpenAI-compatible chat model and the same observability callback. Keeping it here means
retries/timeout/base-url and the Langfuse wiring are configured once, from `settings.yaml`,
never hard-coded per call site.
"""

from __future__ import annotations

import atexit
import logging

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_openai import ChatOpenAI

from bola.settings import Settings

logger = logging.getLogger("bola.llm")


def usage_totals(handler: UsageMetadataCallbackHandler) -> dict[str, int]:
    """Collapse a usage callback's per-model token counts into one input/output/total triple.

    The handler tallies usage per model name; a single strategy turn only ever hits one model, but
    summing across keys keeps this correct (and zero-valued) regardless of how many calls it saw.
    """
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for usage in handler.usage_metadata.values():
        for key in totals:
            totals[key] += usage.get(key, 0) or 0
    return totals


def build_chat_model(settings: Settings, *, model: str | None = None) -> ChatOpenAI:
    """An OpenAI-compatible chat model wired from settings (model, retries, timeout, base-url)."""
    kwargs: dict = {
        "model": model or settings.llm.model,
        "temperature": settings.llm.temperature,
        "timeout": settings.llm.timeout,
        "max_retries": settings.llm.max_retries,
    }
    if settings.openai_api_key:
        kwargs["api_key"] = settings.openai_api_key
    if settings.openai_base_url:
        kwargs["base_url"] = settings.openai_base_url
    return ChatOpenAI(**kwargs)


_langfuse_client = None  # the process-wide Langfuse client, built once on first use


def langfuse_handler(settings: Settings):
    """A Langfuse callback handler, or None when disabled or unconfigured.

    Returned as a value to pass in `config={"callbacks": [...]}`; LangChain/LangGraph
    propagate it to every nested runnable, so one handler traces a whole graph invocation.

    Telemetry must never break a run. The handler swallows callback exceptions itself
    (``raise_error`` is False), but the underlying OTLP span export to Langfuse Cloud is a network
    call that can be slow or fail — and on a long run that span traffic sits on the hot path. So the
    client is built once with a generous, configurable export timeout (``observability.langfuse_timeout``)
    and a best-effort flush is registered at exit; a slow or failing export then degrades to dropped
    traces rather than a blocked or aborted run.
    """
    if not settings.observability.langfuse_enabled:
        return None
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return None
    try:
        from langfuse import Langfuse
        from langfuse.langchain import CallbackHandler

        global _langfuse_client
        if _langfuse_client is None:
            _langfuse_client = Langfuse(
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key,
                host=settings.langfuse_host or None,
                timeout=settings.observability.langfuse_timeout,
            )
            atexit.register(_flush_langfuse, _langfuse_client)
        return CallbackHandler()
    except Exception as exc:  # pragma: no cover - defensive: never block a run on telemetry
        logger.warning("Langfuse disabled (handler init failed): %s", exc)
        return None


def _flush_langfuse(client) -> None:  # pragma: no cover - defensive shutdown hook
    """Flush buffered traces on exit, swallowing any error so shutdown can't fail on telemetry."""
    try:
        client.flush()
    except Exception as exc:
        logger.warning("Langfuse flush at exit failed (traces may be dropped): %s", exc)
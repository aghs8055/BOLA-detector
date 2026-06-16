"""Unit tests for `bola.llm`."""

import sys
import types

import pytest
from langchain_core.callbacks import UsageMetadataCallbackHandler

import bola.llm as llm
from bola.llm import usage_totals
from bola.settings import Settings


def test_usage_totals_zero_for_unused_handler():
    assert usage_totals(UsageMetadataCallbackHandler()) == {
        "input_tokens": 0, "output_tokens": 0, "total_tokens": 0
    }


def test_usage_totals_sums_across_models():
    handler = UsageMetadataCallbackHandler()
    handler.usage_metadata = {
        "model-a": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
        "model-b": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
    }
    assert usage_totals(handler) == {
        "input_tokens": 13, "output_tokens": 5, "total_tokens": 18
    }


def _configured_settings() -> Settings:
    """Settings with Langfuse enabled and credentials present."""
    s = Settings()
    s.observability.langfuse_enabled = True
    s.observability.langfuse_timeout = 7
    s.langfuse_public_key = "pk"
    s.langfuse_secret_key = "sk"
    s.langfuse_host = "https://example.test"
    return s


@pytest.fixture
def fake_langfuse(monkeypatch):
    """Install fake `langfuse` / `langfuse.langchain` modules and reset the cached client.

    Records the kwargs the client was built with and the handlers created, so tests can assert
    the timeout is wired through without touching the network.
    """
    monkeypatch.setattr(llm, "_langfuse_client", None)
    captured = {"client_kwargs": None, "handlers": 0, "flushed": False}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        def flush(self):
            captured["flushed"] = True

    class FakeHandler:
        def __init__(self, **kwargs):
            captured["handlers"] += 1

    lf = types.ModuleType("langfuse")
    lf.Langfuse = FakeClient
    lf_lc = types.ModuleType("langfuse.langchain")
    lf_lc.CallbackHandler = FakeHandler
    monkeypatch.setitem(sys.modules, "langfuse", lf)
    monkeypatch.setitem(sys.modules, "langfuse.langchain", lf_lc)
    return captured


def test_langfuse_handler_none_when_disabled():
    s = _configured_settings()
    s.observability.langfuse_enabled = False
    assert llm.langfuse_handler(s) is None


def test_langfuse_handler_none_without_credentials():
    s = _configured_settings()
    s.langfuse_public_key = ""
    assert llm.langfuse_handler(s) is None


def test_langfuse_handler_wires_timeout_and_caches_client(fake_langfuse, monkeypatch):
    registered = []
    monkeypatch.setattr(llm.atexit, "register", lambda fn, *a: registered.append((fn, a)))

    handler = llm.langfuse_handler(_configured_settings())

    assert handler is not None
    assert fake_langfuse["client_kwargs"]["timeout"] == 7
    assert fake_langfuse["client_kwargs"]["host"] == "https://example.test"
    assert registered and registered[0][0] is llm._flush_langfuse

    llm.langfuse_handler(_configured_settings())
    assert fake_langfuse["handlers"] == 2  # second handler...
    assert len(registered) == 1  # ...but the client (and its atexit hook) is built only once


def test_flush_langfuse_swallows_errors():
    class Boom:
        def flush(self):
            raise RuntimeError("network down")

    llm._flush_langfuse(Boom())  # must not raise
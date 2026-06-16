"""Unit tests for `bola.settings`."""

import pytest
from bola.settings import load_settings


def test_defaults_without_files(tmp_path):
    s = load_settings(
        settings_path=str(tmp_path / "none.yaml"),
        env_path=str(tmp_path / ".env"),
    )
    assert s.llm.model == "anthropic/claude-sonnet-4-6"
    assert s.llm.temperature == 0.0
    assert s.test.regular_ops == 50
    assert s.test.strategy == "random"
    assert s.storage.db_path == "bola.db"
    assert s.observability.log_level == "INFO"
    assert s.analysis.objects_per_call == 5
    assert s.analysis.batch_size == 1
    assert s.analysis.on_error == "stop"


def test_analysis_overrides_from_yaml(tmp_path):
    (tmp_path / "settings.yaml").write_text(
        "analysis:\n  objects_per_call: 3\n  batch_size: 4\n  on_error: continue\n"
    )
    s = load_settings(settings_path=str(tmp_path / "settings.yaml"), env_path=str(tmp_path / ".env"))
    assert s.analysis.objects_per_call == 3
    assert s.analysis.batch_size == 4
    assert s.analysis.on_error == "continue"


def test_overrides_from_yaml(tmp_path):
    (tmp_path / "settings.yaml").write_text(
        "llm:\n  model: openai/gpt-4\n  temperature: 0.7\ntest:\n  regular_ops: 100\n"
    )
    s = load_settings(settings_path=str(tmp_path / "settings.yaml"), env_path=str(tmp_path / ".env"))
    assert s.llm.model == "openai/gpt-4"
    assert s.llm.temperature == 0.7
    assert s.test.regular_ops == 100
    # un-overridden keys keep defaults
    assert s.test.strategy == "random"


def test_secrets_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "lf-pub")
    s = load_settings(settings_path=str(tmp_path / "none.yaml"), env_path=str(tmp_path / ".env"))
    assert s.openai_api_key == "sk-test"
    assert s.langfuse_public_key == "lf-pub"


def test_secrets_from_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-from-file\n")
    s = load_settings(settings_path=str(tmp_path / "none.yaml"), env_path=str(tmp_path / ".env"))
    assert s.openai_api_key == "sk-from-file"


def test_env_var_takes_precedence_over_env_file(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-from-file\n")
    s = load_settings(settings_path=str(tmp_path / "none.yaml"), env_path=str(tmp_path / ".env"))
    assert s.openai_api_key == "sk-from-env"

"""Settings resolution + override coercion — the seam every command shares."""

import pytest

from cli.common import CliError, GlobalState


def _state(**kw):
    # Point at non-existent config so load_settings falls back to defaults deterministically.
    kw.setdefault("settings_path", "no-such-settings.yaml")
    kw.setdefault("env_path", "no-such.env")
    return GlobalState(**kw)


def test_set_overrides_coerce_to_field_type():
    s = _state(overrides=[
        "test.strategy=ai",
        "relations.batch_size=4",
        "llm.temperature=0.7",
        "observability.langfuse_enabled=false",
    ]).load_settings()
    assert s.test.strategy == "ai"
    assert s.relations.batch_size == 4 and isinstance(s.relations.batch_size, int)
    assert s.llm.temperature == 0.7 and isinstance(s.llm.temperature, float)
    assert s.observability.langfuse_enabled is False


def test_named_flags_layer_over_set():
    s = _state(model="m1", db_path="x.db", runs_dir="r", log_level="DEBUG").load_settings()
    assert s.llm.model == "m1"
    assert s.storage.db_path == "x.db"
    assert s.storage.runs_dir == "r"
    assert s.observability.log_level == "DEBUG"


@pytest.mark.parametrize("bad", ["nosep", "nope.x=1", "relations.unknown=1"])
def test_bad_overrides_raise_clierror(bad):
    with pytest.raises(CliError):
        _state(overrides=[bad]).load_settings()


@pytest.mark.parametrize("bad", ["relations.batch_size=notint", "llm.temperature=NaNish"])
def test_bad_value_type_raises_clierror(bad):
    with pytest.raises(CliError):
        _state(overrides=[bad]).load_settings()


def test_bool_coercion_rejects_garbage():
    with pytest.raises(CliError):
        _state(overrides=["observability.langfuse_enabled=maybe"]).load_settings()


def test_show_progress_inverts_no_progress():
    assert _state(no_progress=True).show_progress is False
    assert _state(no_progress=False).show_progress is True

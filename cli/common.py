"""Shared CLI plumbing: settings resolution, global options, logging, observability.

Every command resolves its `Settings` the same way — load `settings.yaml` + `.env`, then layer the
global overrides on top — so the precedence is uniform and predictable:

    settings.yaml / defaults  <  --set section.key=value  <  dedicated global flags

`--set` is the escape hatch that makes *every* settings field reachable from the command line
without a dedicated flag for each; the common ones (model, db, runs dir, log level) also get named
flags for convenience. Values are coerced to the type of the field they target, so `--set
relations.batch_size=4` yields an int, not the string `"4"`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, is_dataclass
from typing import Optional

import typer
from rich.console import Console

from bola.llm import langfuse_handler
from bola.settings import Settings, load_settings

console = Console()
err_console = Console(stderr=True)


class CliError(Exception):
    """A user-facing error. Caught by `run_cli` and rendered as a clean one-line message with a
    non-zero exit — no traceback. (A plain exception, not a ClickException: Typer's own exception
    handling has changed across versions, so we own the rendering rather than depend on it.)"""


def load_or_error(fn, *args, **kwargs):
    """Call a spec/manifest loader, turning a known load failure into a clean `CliError`.

    The loaders raise descriptive `SpecError`/`TargetError` subclasses (bad path, unparseable
    document, invalid OpenAPI/manifest); without this they'd surface as a traceback. Their own
    messages already name the file and the cause, so we pass them through verbatim.
    """
    from bola.spec.errors import SpecError
    from bola.target.manifest import TargetError

    try:
        return fn(*args, **kwargs)
    except (SpecError, TargetError) as exc:
        raise CliError(str(exc)) from exc


@dataclass
class GlobalState:
    """Global options collected by the root callback; carried on the Typer context.

    Commands call `load_settings()` to get a fully-resolved `Settings` with every global override
    already applied, so a command body never re-parses `--set` or the common flags itself.
    """

    settings_path: str = "settings.yaml"
    env_path: str = ".env"
    overrides: list[str] = field(default_factory=list)
    model: Optional[str] = None
    db_path: Optional[str] = None
    runs_dir: Optional[str] = None
    log_level: Optional[str] = None
    no_progress: bool = False

    def load_settings(self) -> Settings:
        """Resolve `Settings` with the global `--set` overrides and named flags layered on top."""
        settings = load_settings(self.settings_path, self.env_path)
        for assignment in self.overrides:
            _apply_override(settings, assignment)
        if self.model:
            settings.llm.model = self.model
        if self.db_path:
            settings.storage.db_path = self.db_path
        if self.runs_dir:
            settings.storage.runs_dir = self.runs_dir
        if self.log_level:
            settings.observability.log_level = self.log_level
        return settings

    @property
    def show_progress(self) -> bool:
        """Whether progress bars are enabled (the inverse of `--no-progress`)."""
        return not self.no_progress


def get_state(ctx: typer.Context) -> GlobalState:
    """The `GlobalState` the root callback stored on the context."""
    if not isinstance(ctx.obj, GlobalState):
        ctx.obj = GlobalState()
    return ctx.obj


def _apply_override(settings: Settings, assignment: str) -> None:
    """Apply one `section.key=value` override, coercing to the target field's type."""
    if "=" not in assignment:
        raise CliError(f"--set expects 'section.key=value', got {assignment!r}")
    path, raw = assignment.split("=", 1)
    parts = path.strip().split(".")
    target = settings
    for name in parts[:-1]:
        target = getattr(target, name, None)
        if not is_dataclass(target):
            raise CliError(f"--set: unknown settings section {path!r}")
    leaf = parts[-1]
    if not hasattr(target, leaf):
        raise CliError(f"--set: unknown setting {path!r}")
    setattr(target, leaf, _coerce(getattr(target, leaf), raw, path))


def _coerce(current, raw: str, path: str):
    """Coerce a string to the type of the value it replaces."""
    if isinstance(current, bool):
        if raw.lower() in ("true", "1", "yes", "on"):
            return True
        if raw.lower() in ("false", "0", "no", "off"):
            return False
        raise CliError(f"--set {path}: expected a boolean, got {raw!r}")
    if isinstance(current, int):
        try:
            return int(raw)
        except ValueError as exc:
            raise CliError(f"--set {path}: expected an integer, got {raw!r}") from exc
    if isinstance(current, float):
        try:
            return float(raw)
        except ValueError as exc:
            raise CliError(f"--set {path}: expected a number, got {raw!r}") from exc
    return raw


def setup_logging(settings: Settings) -> None:
    """Configure root logging at the level from settings."""
    import logging

    logging.basicConfig(
        level=getattr(logging, settings.observability.log_level.upper(), logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
    )


def make_callbacks(settings: Settings) -> Optional[list]:
    """The Langfuse callback list, or None when observability is disabled/unconfigured."""
    handler = langfuse_handler(settings)
    return [handler] if handler else None

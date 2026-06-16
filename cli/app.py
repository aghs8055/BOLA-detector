"""The root `bola` command — wires the namespaces and the global options.

Command tree:

    bola
    ├── relations  detect | show                  # detect & inspect data-flow relations (one step)
    ├── run        start | analyze                 # full pipeline, or a re-runnable analysis pass
    └── report     list | show | aggregate | html  # inspect finished runs; render report.json to HTML

Global options live on the root callback and are stored on the Typer context as a `GlobalState`,
so every command resolves its `Settings` identically (see `cli/common.py`). `--set section.key=value`
is the general escape hatch — *any* settings field is reachable from the command line without a
dedicated flag.
"""

from __future__ import annotations

import sys
from typing import List, Optional

import typer

from cli import __version__, relations, report, run
from cli.common import CliError, GlobalState, err_console

app = typer.Typer(
    name="bola",
    no_args_is_help=True,
    add_completion=True,
    rich_markup_mode="rich",
    # User-facing errors are raised as `CliError` and rendered by `run_cli`; Typer's pretty-traceback
    # wrapper would otherwise dump a full Rich traceback instead of the one-line message.
    pretty_exceptions_enable=False,
    help="BOLA Detector — semi-automatic Broken Object Level Authorization detection.",
)

app.add_typer(relations.app, name="relations")
app.add_typer(run.app, name="run")
app.add_typer(report.app, name="report")


def _version_callback(value: bool) -> None:
    """Print the version and exit when `--version` is passed (eager option)."""
    if value:
        typer.echo(f"bola {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    settings_path: str = typer.Option(
        "settings.yaml", "--settings", help="Path to the settings YAML file.", metavar="PATH"
    ),
    env_path: str = typer.Option(
        ".env", "--env", help="Path to the .env file (secrets).", metavar="PATH"
    ),
    set_: Optional[List[str]] = typer.Option(
        None, "--set", "-s", metavar="SECTION.KEY=VALUE",
        help="Override any setting (repeatable), e.g. -s test.strategy=ai -s relations.batch_size=4.",
    ),
    model: Optional[str] = typer.Option(
        None, "--model", help="Override the LLM model for this invocation."
    ),
    db_path: Optional[str] = typer.Option(
        None, "--db", help="Override the SQLite database path.", metavar="PATH"
    ),
    runs_dir: Optional[str] = typer.Option(
        None, "--runs-dir", help="Override the directory run result files are written to.", metavar="PATH"
    ),
    log_level: Optional[str] = typer.Option(
        None, "--log-level", help="Override the log level (DEBUG/INFO/WARNING/ERROR)."
    ),
    no_progress: bool = typer.Option(
        False, "--no-progress", help="Disable progress bars (useful for logs/CI)."
    ),
    version: Optional[bool] = typer.Option(
        None, "--version", callback=_version_callback, is_eager=True, help="Show the version and exit."
    ),
) -> None:
    """Global options apply to every subcommand. Run a subcommand with --help for its options."""
    ctx.obj = GlobalState(
        settings_path=settings_path,
        env_path=env_path,
        overrides=list(set_ or []),
        model=model,
        db_path=db_path,
        runs_dir=runs_dir,
        log_level=log_level,
        no_progress=no_progress,
    )


def run_cli() -> None:
    """Console entry point — render a `CliError` as a clean one-line message + exit 1 (no traceback)."""
    try:
        app()
    except CliError as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        sys.exit(1)


if __name__ == "__main__":
    run_cli()

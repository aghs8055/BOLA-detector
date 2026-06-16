"""`bola relations` — detect and inspect data-flow relations for an OpenAPI spec.

Relation detection is resumable and cached in SQLite: rerunning `detect` continues an interrupted
run, skipping the `(source, target-group)` units already done. This is the "run a single step"
entry point — relations are the prerequisite the full `run` consumes, so they can be detected,
reviewed, and re-detected independently.
"""

from __future__ import annotations

import typer

from bola.db.store import Store
from bola.relations import detect_relations
from bola.spec import load_spec
from bola.spec.views import spec_hash
from cli.common import CliError, get_state, load_or_error, make_callbacks, setup_logging
from cli.render import render_relations

app = typer.Typer(no_args_is_help=True, help="Detect & inspect API data-flow relations.")


@app.command()
def detect(
    ctx: typer.Context,
    spec: str = typer.Argument(..., help="OpenAPI spec: a file path or an http(s) URL."),
    fresh: bool = typer.Option(
        False, "--fresh", help="Discard prior checkpoints for this (spec, model) before detecting."
    ),
    show: bool = typer.Option(
        True, "--show/--no-show", help="Print the detected relation edges when done."
    ),
) -> None:
    """Detect data-flow relations and cache them (resumable — rerun to continue)."""
    state = get_state(ctx)
    settings = state.load_settings()
    setup_logging(settings)

    loaded = load_or_error(load_spec, spec)
    store = Store(db_path=settings.storage.db_path)

    if fresh:
        _clear_checkpoints(store, spec_hash(loaded), settings.llm.model)

    callbacks = make_callbacks(settings)
    try:
        result = detect_relations(
            loaded, settings, store,
            show_progress=state.show_progress, callbacks=callbacks,
            reraise_interrupt=False,
        )
    except KeyboardInterrupt:
        raise CliError("interrupted — progress saved, rerun to resume") from None

    from cli.common import console

    console.print(
        f"\nunits: [green]{result.units_done} done[/green], "
        f"{result.units_skipped} skipped, "
        f"[red]{result.units_failed} failed[/red] / {result.units_total} total"
    )
    if not result.completed:
        raise CliError("incomplete — rerun to resume")

    console.print(
        f"[green]complete[/green] — {len(result.relations)} relation edge(s) cached "
        f"in {result.elapsed_s}s ({result.tokens['total_tokens']} tokens)"
    )
    if show:
        render_relations(result.relations)


@app.command(name="show")
def show_relations(
    ctx: typer.Context,
    spec: str = typer.Argument(..., help="OpenAPI spec whose cached relations to print."),
) -> None:
    """Print the cached relations for a spec (without re-detecting)."""
    state = get_state(ctx)
    settings = state.load_settings()
    loaded = load_or_error(load_spec, spec)
    store = Store(db_path=settings.storage.db_path)
    relations = store.get_relations(spec_hash(loaded), settings.llm.model)
    if relations is None:
        raise CliError("no cached relations for this (spec, model) — run `bola relations detect` first")
    render_relations(relations)


def _clear_checkpoints(store: Store, sh: str, model: str) -> None:
    """Delete prior relation-detection checkpoints for a (spec, model) — backs `--fresh`."""
    from sqlalchemy import delete

    from bola.db.models import RelationGroupResult

    with store._session() as s:  # noqa: SLF001 - CLI-only maintenance path
        s.execute(
            delete(RelationGroupResult).where(
                RelationGroupResult.spec_hash == sh,
                RelationGroupResult.model_name == model,
            )
        )
        s.commit()

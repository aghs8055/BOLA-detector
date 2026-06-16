"""`bola report` — render finished runs from what's persisted (SQLite + JSON).

Read-only: `list` shows recent runs; `show` renders one run's metrics, the findings of each
analysis pass (a run may have several — different access models or LLMs), and the explorer's
per-turn memory trail. Nothing here re-contacts the target or the LLM; it reports stored state.
"""

from __future__ import annotations

import json
import webbrowser
from pathlib import Path
from typing import Optional

import typer

from bola.aggregate import aggregate_run, render_report_html, write_report_json
from bola.db.store import Store
from bola.spec import load_spec
from bola.target import load_manifest
from cli.common import (
    CliError,
    console,
    get_state,
    load_or_error,
    make_callbacks,
    setup_logging,
)
from cli.render import (
    render_aggregated_report,
    render_findings,
    render_memory_trail,
    render_metrics,
    render_run_header,
    render_run_list,
)

app = typer.Typer(no_args_is_help=True, help="Inspect finished runs: metrics, findings, memory.")


@app.command(name="list")
def list_runs(
    ctx: typer.Context,
    limit: int = typer.Option(20, "--limit", "-n", help="How many recent runs to show."),
) -> None:
    """List recent runs (most recent first)."""
    state = get_state(ctx)
    settings = state.load_settings()
    store = Store(db_path=settings.storage.db_path)
    render_run_list(store.list_runs(limit=limit))


@app.command()
def show(
    ctx: typer.Context,
    run_id: str = typer.Argument(..., help="The run id to report on."),
    analysis_id: Optional[str] = typer.Option(
        None, "--analysis-id", help="Show only this analysis pass (default: all passes)."
    ),
    memory: bool = typer.Option(
        True, "--memory/--no-memory", help="Include the AI explorer's per-turn memory trail."
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit the raw run + analyses as JSON instead of tables."
    ),
) -> None:
    """Render a finished run: metrics, per-pass findings, and the memory trail."""
    state = get_state(ctx)
    settings = state.load_settings()
    store = Store(db_path=settings.storage.db_path)

    run = store.get_run(run_id)
    if run is None:
        raise CliError(f"no run with id {run_id!r} — list them with `bola report list`")

    analyses = store.list_analyses(run_id)
    if analysis_id:
        analyses = [a for a in analyses if a["analysis_id"] == analysis_id]
        if not analyses:
            raise CliError(f"run {run_id!r} has no analysis pass {analysis_id!r}")

    if as_json:
        _emit_json(store, run, analyses)
        return

    render_run_header(run)
    if run.metrics_json:
        render_metrics(json.loads(run.metrics_json))

    if not analyses:
        console.print("\n[yellow]No analysis passes yet — run `bola run analyze "
                      f"{run_id}`.[/yellow]")
    for entry in analyses:
        full = store.get_analysis(entry["analysis_id"])
        console.print(f"\n[bold]Analysis pass[/bold] [cyan]{entry['analysis_id']}[/cyan] "
                      f"(status: {entry['status']})")
        quality = (full or {}).get("metrics") or {}
        if quality.get("quality"):
            from cli.render import _render_quality

            _render_quality(quality["quality"])
        render_findings((full or {}).get("findings") or [])

    if memory:
        render_memory_trail(store.get_strategy_memories(run_id))


def _emit_json(store: Store, run, analyses: list[dict]) -> None:
    """Print the run, its analyses, and the memory trail as a single raw JSON document."""
    payload = {
        "run_id": run.run_id,
        "target": run.target_name,
        "strategy": run.strategy,
        "status": run.status,
        "metrics": json.loads(run.metrics_json) if run.metrics_json else None,
        "analyses": [store.get_analysis(a["analysis_id"]) for a in analyses],
        "strategy_memory": store.get_strategy_memories(run.run_id),
    }
    console.print_json(json.dumps(payload, default=str))


@app.command()
def aggregate(
    ctx: typer.Context,
    run_id: str = typer.Argument(..., help="The finished run id to aggregate into a report."),
    manifest_path: Optional[str] = typer.Option(
        None, "--manifest", "-m",
        help="Target manifest — enriches the report with the spec summary + access model.",
    ),
    access: Optional[str] = typer.Option(
        None, "--access", help="Override the access/authorization model description for the report."
    ),
    analysis_id: Optional[str] = typer.Option(
        None, "--analysis-id", help="Which analysis pass to fold in (default: the newest)."
    ),
    fresh: bool = typer.Option(
        False, "--fresh", help="Regenerate (one LLM call) even if a report is already stored."
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit the stored report payload as JSON instead of tables."
    ),
) -> None:
    """Fuse a finished run's analysis findings and AI memory into one structured report.

    Reads only persisted state (no re-test); one cheap LLM call. A stored report is shown as-is
    unless `--fresh` forces regeneration, so re-viewing never spends tokens.
    """
    state = get_state(ctx)
    settings = state.load_settings()
    setup_logging(settings)
    store = Store(db_path=settings.storage.db_path)

    run = store.get_run(run_id)
    if run is None:
        raise CliError(f"no run with id {run_id!r} — list them with `bola report list`")

    existing = store.get_aggregated_report(run_id)
    if existing and not fresh:
        console.print("[dim]Showing the stored report — pass --fresh to regenerate.[/dim]")
        _emit_report(existing, as_json)
        return

    spec = None
    manifest = None
    access_desc = access or ""
    if manifest_path:
        manifest = load_or_error(load_manifest, manifest_path)
        spec = load_or_error(load_spec, manifest.spec.value)
        if not access:
            access_desc = manifest.access

    result = aggregate_run(
        run_id, settings, store,
        spec=spec,
        manifest=manifest,
        access_description=access_desc,
        analysis_id=analysis_id,
        callbacks=make_callbacks(settings),
    )
    path = write_report_json(settings, run_id, result.payload)
    console.print(f"[green]report written[/green] — {path}")
    _emit_report(result.payload, as_json)


@app.command()
def html(
    ctx: typer.Context,
    run_id: Optional[str] = typer.Argument(
        None, help="Run id whose runs/<id>/report.json to render (omit when using --input)."
    ),
    input_path: Optional[str] = typer.Option(
        None, "--input", "-i", help="Render this report.json directly instead of resolving a run id."
    ),
    out_path: Optional[str] = typer.Option(
        None, "--out", "-o", help="Where to write the HTML (default: report.html beside the JSON)."
    ),
    open_browser: bool = typer.Option(
        False, "--open", help="Open the generated page in the default browser."
    ),
) -> None:
    """Render a run's `report.json` into one self-contained HTML page (all details, no DB needed).

    Reads the portable bundle that `run start`/`report aggregate` already wrote — it never
    re-contacts the target or the LLM. Note the page embeds the manifest verbatim, so it inherits
    the JSON's credentials and is sensitive.
    """
    state = get_state(ctx)
    source = _resolve_report_json(state, run_id, input_path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CliError(f"could not read report JSON {str(source)!r}: {exc}") from exc

    out = Path(out_path) if out_path else source.with_suffix(".html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_report_html(payload), encoding="utf-8")
    console.print(f"[green]HTML report written[/green] — {out}")
    if open_browser:
        webbrowser.open(out.resolve().as_uri())


def _resolve_report_json(state, run_id: Optional[str], input_path: Optional[str]) -> Path:
    """Pick the report.json to render: an explicit --input, else runs/<run_id>/report.json."""
    if input_path:
        source = Path(input_path)
    elif run_id:
        settings = state.load_settings()
        source = Path(settings.storage.runs_dir) / run_id / "report.json"
    else:
        raise CliError("pass a run id or --input PATH to a report.json")
    if not source.is_file():
        raise CliError(
            f"no report JSON at {str(source)!r} — generate one with `bola report aggregate`"
        )
    return source


def _emit_report(payload: dict, as_json: bool) -> None:
    """Render an aggregated-report payload as tables, or print it raw as JSON."""
    if as_json:
        console.print_json(json.dumps(payload, default=str))
    else:
        render_aggregated_report(payload)

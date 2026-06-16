"""`bola run` — drive a full BOLA test against a target, or re-analyze a finished run.

`start` is the whole pipeline: load the target manifest + spec, make sure relations exist (detect
them if not cached), then `execute_run` (build → snapshots → attack → snapshots → analyze). It is
**resumable**: pass `--run-id` of an interrupted run and it continues from the append-only log,
re-sending no completed work. `analyze` is the re-runnable analysis pass — a fresh verdict over the
same frozen snapshots under a different access model or LLM, accumulating alongside earlier passes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from bola.db.store import Store
from bola.relations import detect_relations
from bola.runner import analyze_and_report, execute_run
from bola.spec import load_spec
from bola.spec.views import operations, spec_hash
from bola.target import load_manifest
from cli.common import CliError, console, get_state, load_or_error, make_callbacks, setup_logging
from cli.render import render_findings, render_memory_trail, render_metrics

app = typer.Typer(no_args_is_help=True, help="Run a full BOLA test, or re-analyze a finished run.")


@app.command()
def start(
    ctx: typer.Context,
    manifest_path: str = typer.Argument(
        ..., metavar="MANIFEST", help="Path to the target manifest (the JSON `make describe` emits)."
    ),
    strategy: Optional[str] = typer.Option(
        None, "--strategy", help="Strategy: 'ai' (LLM-driven) or 'random' (baseline)."
    ),
    run_id: Optional[str] = typer.Option(
        None, "--run-id", help="Resume the run with this id (continues from the log). Omit to start fresh."
    ),
    fresh: bool = typer.Option(
        False, "--fresh", help="With --run-id: discard that run's prior records and restart it."
    ),
    no_analyze: bool = typer.Option(
        False, "--no-analyze", help="Run execution + snapshots only; skip the analysis pass."
    ),
    detect: bool = typer.Option(
        True, "--detect/--no-detect",
        help="Detect relations if none are cached. --no-detect errors instead.",
    ),
    ground_truth: Optional[str] = typer.Option(
        None, "--ground-truth", help="Path to a ground-truth file (vulnerable op keys) for quality metrics."
    ),
) -> None:
    """Run the full pipeline against a target (resumable via --run-id)."""
    state = get_state(ctx)
    settings = state.load_settings()
    if strategy:
        if strategy not in ("ai", "random"):
            raise CliError("--strategy must be 'ai' or 'random'")
        settings.test.strategy = strategy
    setup_logging(settings)

    manifest = load_or_error(load_manifest, manifest_path)
    loaded = load_or_error(load_spec, manifest.spec.value)
    store = Store(db_path=settings.storage.db_path)

    if fresh and run_id:
        _clear_run(store, run_id)

    relations = _ensure_relations(
        loaded, settings, store, detect=detect, show_progress=state.show_progress
    )
    gt = _load_ground_truth(ground_truth)
    callbacks = make_callbacks(settings)

    console.print(
        f"[bold]Running[/bold] {settings.test.strategy} strategy against {manifest.base_url} "
        f"({len(operations(loaded))} ops, {len(relations)} relations)"
    )
    try:
        result = execute_run(
            loaded, manifest, relations, settings, store,
            run_id=run_id,
            ground_truth=gt,
            analyze=not no_analyze,
            callbacks=callbacks,
            show_progress=state.show_progress,
        )
    except KeyboardInterrupt:
        raise CliError(
            f"interrupted — progress saved, resume with: bola run start {manifest_path} "
            f"--run-id {run_id or '<run-id printed above>'}"
        ) from None

    console.print(f"\n[green]run complete[/green] — id [cyan]{result.run_id}[/cyan]")
    render_metrics(result.metrics)
    if result.analysis is not None:
        render_findings(result.analysis.findings)
    render_memory_trail(store.get_strategy_memories(result.run_id))


@app.command()
def analyze(
    ctx: typer.Context,
    run_id: str = typer.Argument(..., help="The execution run id to analyze."),
    access: Optional[str] = typer.Option(
        None, "--access", help="Override the access/authorization model description for this pass."
    ),
    analysis_id: Optional[str] = typer.Option(
        None, "--analysis-id", help="Id for this analysis pass (resume it, or compare passes)."
    ),
    ground_truth: Optional[str] = typer.Option(
        None, "--ground-truth", help="Path to a ground-truth file for quality metrics."
    ),
) -> None:
    """Run a fresh analysis pass over a finished run's snapshots."""
    state = get_state(ctx)
    settings = state.load_settings()
    setup_logging(settings)
    store = Store(db_path=settings.storage.db_path)
    if store.get_run(run_id) is None:
        raise CliError(f"no run with id {run_id!r} — list them with `bola report list`")

    callbacks = make_callbacks(settings)
    try:
        analysis, metrics = analyze_and_report(
            run_id, settings, store,
            access_description=access or "",
            ground_truth=_load_ground_truth(ground_truth),
            analysis_id=analysis_id,
            callbacks=callbacks,
            show_progress=state.show_progress,
        )
    except KeyboardInterrupt:
        raise CliError("interrupted — progress saved, rerun with the same --analysis-id to resume") from None

    console.print(
        f"\n[green]analysis complete[/green] — pass [cyan]{analysis.analysis_id}[/cyan]"
    )
    render_metrics(metrics)
    render_findings(analysis.findings)


def _ensure_relations(loaded, settings, store, *, detect: bool, show_progress: bool) -> list[dict]:
    """Return cached relations, detecting them first when missing (unless `detect` is False)."""
    relations = store.get_relations(spec_hash(loaded), settings.llm.model)
    if relations is not None:
        return relations
    if not detect:
        raise CliError(
            "no cached relations for this (spec, model). Run `bola relations detect` first, "
            "or drop --no-detect to detect them now."
        )
    console.print("[dim]No cached relations — detecting first…[/dim]")
    result = detect_relations(
        loaded, settings, store, show_progress=show_progress, callbacks=make_callbacks(settings)
    )
    if not result.completed:
        raise CliError("relation detection incomplete — rerun to resume")
    return result.relations


def _load_ground_truth(path: Optional[str]) -> Optional[list[str]]:
    """Read vulnerable operation keys from a YAML/JSON file (a list, or `{vulnerable: [...]}`)."""
    if not path:
        return None
    import yaml

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        for key in ("vulnerable", "ground_truth"):
            if key in data:
                data = data[key]
                break
    if not isinstance(data, list):
        raise CliError(f"ground-truth file {path!r} must be a list of op keys, or have a 'vulnerable' list")
    return [str(item) for item in data]


def _clear_run(store: Store, run_id: str) -> None:
    """Delete every record for a run so --fresh can restart it cleanly."""
    from sqlalchemy import delete, select

    from bola.db.models import (
        AnalysisItemResult,
        AnalysisRecord,
        ExecutionRecord,
        RunRecord,
        SnapshotRecord,
        StrategyMemoryRecord,
    )

    with store._session() as s:  # noqa: SLF001 - CLI-only maintenance path
        analysis_ids = list(
            s.scalars(select(AnalysisRecord.analysis_id).where(AnalysisRecord.run_id == run_id))
        )
        for aid in analysis_ids:
            s.execute(delete(AnalysisItemResult).where(AnalysisItemResult.analysis_id == aid))
        for model in (ExecutionRecord, SnapshotRecord, StrategyMemoryRecord, AnalysisRecord):
            s.execute(delete(model).where(model.run_id == run_id))
        s.execute(delete(RunRecord).where(RunRecord.run_id == run_id))
        s.commit()

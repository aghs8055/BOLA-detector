"""Rich rendering of CLI results — relations, run metrics, findings, and the AI memory trail.

Pure presentation: every function takes plain dicts/lists (exactly what the store and the runner
return) and prints to the shared `console`. Keeping rendering here means the command modules stay
thin orchestration and the output style is consistent across `run` and `report`.
"""

from __future__ import annotations

from typing import Any, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from cli.common import console


def render_relations(relations: list[dict], *, console: Console = console) -> None:
    """Print the detected relation edges as a table (source/target op + field, cast)."""
    if not relations:
        console.print("[yellow]No relations detected.[/yellow]")
        return
    table = Table(title=f"Detected relations ({len(relations)} edge(s))", show_lines=False)
    table.add_column("Source op", style="cyan", no_wrap=False)
    table.add_column("Source field", style="dim")
    table.add_column("Target op", style="green", no_wrap=False)
    table.add_column("Target input", style="dim")
    table.add_column("Cast", style="magenta")
    for rel in relations:
        edge = rel.get("edge", {})
        src = edge.get("source", {})
        tgt = edge.get("target", {})
        table.add_row(
            rel.get("source_key", ""),
            f"{src.get('status_code', '')} {src.get('media_type', '')}\n{src.get('json_pointer', '')}",
            rel.get("target_key", ""),
            _target_input(tgt),
            edge.get("cast") or "—",
        )
    console.print(table)


def _target_input(tgt: dict) -> str:
    """A one-line label for an edge target — a parameter or a request-body pointer."""
    if "name" in tgt:
        return f"param {tgt.get('name', '')} ({tgt.get('location', '')})"
    return f"body {tgt.get('media_type', '')}\n{tgt.get('json_pointer', '')}"


def render_metrics(metrics: dict, *, console: Console = console) -> None:
    """Print the coverage / execution / llm metric groups (and quality, when present)."""
    if not metrics:
        console.print("[yellow]No metrics available.[/yellow]")
        return

    cov = metrics.get("coverage", {})
    exe = metrics.get("execution", {})
    llm = metrics.get("llm", {})

    table = Table(title="Run metrics", show_header=False, box=None, pad_edge=False)
    table.add_column("Metric", style="bold")
    table.add_column("Value")

    table.add_row("[cyan]Coverage[/cyan]", "")
    table.add_row(
        "  endpoint coverage",
        f"{cov.get('endpoint_coverage_pct', 0)}%  "
        f"({cov.get('operations_called', 0)}/{cov.get('total_operations', 0)} ops)",
    )
    table.add_row("  objects snapshotted", str(cov.get("objects_snapshotted", 0)))

    table.add_row("[cyan]Execution[/cyan]", "")
    table.add_row("  regular calls", str(exe.get("regular_calls", 0)))
    table.add_row("  attacker calls", str(exe.get("hacker_calls", 0)))
    table.add_row("  snapshot calls", str(exe.get("snapshot_calls", 0)))
    codes = exe.get("status_codes", {})
    if codes:
        table.add_row(
            "  status codes",
            ", ".join(f"{k}={v}" for k, v in codes.items()),
        )
    table.add_row("  total time", f"{exe.get('total_time_s', 0)}s")
    phases = exe.get("phase_times_s", {})
    if phases:
        table.add_row(
            "  phase times",
            ", ".join(f"{k}={v}s" for k, v in phases.items()),
        )

    table.add_row("[cyan]LLM[/cyan]", "")
    table.add_row("  strategy turns", str(llm.get("strategy_turns", 0)))
    for label, key in (("strategy", "strategy_tokens"), ("analysis", "analysis_tokens")):
        tok = llm.get(key)
        if tok:
            table.add_row(
                f"  {label} tokens (in/out/total)",
                f"{tok.get('input_tokens', 0)}/{tok.get('output_tokens', 0)}/"
                f"{tok.get('total_tokens', 0)}",
            )

    console.print(table)

    quality = metrics.get("quality")
    if quality:
        _render_quality(quality, console=console)


def _render_quality(quality: dict, *, console: Console = console) -> None:
    """Print the precision/recall/f1/accuracy + confusion-matrix block."""
    table = Table(title="Quality vs. ground truth", show_header=False, box=None)
    table.add_column("Metric", style="bold")
    table.add_column("Value")
    for label in ("precision", "recall", "f1", "accuracy"):
        table.add_row(label, str(quality.get(label, 0)))
    table.add_row(
        "confusion (tp/fp/fn/tn)",
        f"{quality.get('tp', 0)}/{quality.get('fp', 0)}/"
        f"{quality.get('fn', 0)}/{quality.get('tn', 0)}",
    )
    console.print(table)


def render_findings(findings: list[dict], *, console: Console = console) -> None:
    """Print the per-object analysis verdicts (BOLA findings first), with rationale."""
    if not findings:
        console.print("[yellow]No objects analyzed.[/yellow]")
        return
    bolas = [f for f in findings if f.get("is_bola")]
    table = Table(
        title=f"Analysis findings — {len(bolas)} BOLA / {len(findings)} object(s)",
        show_lines=True,
    )
    table.add_column("Object", style="cyan", no_wrap=False)
    table.add_column("Verdict", justify="center")
    table.add_column("R/W", justify="center")
    table.add_column("Rationale", no_wrap=False)
    for f in sorted(findings, key=lambda x: not x.get("is_bola")):
        is_bola = f.get("is_bola")
        verdict = Text("BOLA", style="bold red") if is_bola else Text("ok", style="green")
        rw = []
        if f.get("unauthorized_read"):
            rw.append("R")
        if f.get("unauthorized_write"):
            rw.append("W")
        table.add_row(
            f.get("object_key", ""),
            verdict,
            "".join(rw) or "—",
            f.get("rationale", ""),
        )
    console.print(table)


def render_aggregated_report(payload: dict, *, console: Console = console) -> None:
    """Print an aggregated report payload: target summary, categorized findings, open questions."""
    report = payload.get("report") or {}
    summary = report.get("target_summary") or "(no summary)"
    meta = f"run {payload.get('run_id', '?')} · model {payload.get('model', '?')}"
    console.print(Panel(summary, title="Target summary", subtitle=meta))

    _render_finding_group(report.get("bola_findings") or [], "BOLA findings", "red", console)
    _render_finding_group(
        report.get("non_bola_findings") or [], "Non-BOLA findings", "yellow", console
    )

    questions = report.get("open_questions") or []
    if questions:
        table = Table(title=f"Open questions — {len(questions)} for human review", show_lines=True)
        table.add_column("Question", no_wrap=False)
        table.add_column("Why unresolved", no_wrap=False)
        table.add_column("Next step", no_wrap=False)
        for q in questions:
            table.add_row(
                q.get("question", ""),
                q.get("why_unresolved", ""),
                q.get("suggested_next_step", ""),
            )
        console.print(table)


def _render_finding_group(
    findings: list[dict], title: str, color: str, console: Console
) -> None:
    """One category of aggregated findings as a table (silent when the category is empty)."""
    if not findings:
        console.print(f"[dim]{title}: none[/dim]")
        return
    table = Table(title=f"{title} — {len(findings)}", show_lines=True)
    table.add_column("Title", style=color, no_wrap=False)
    table.add_column("APIs", no_wrap=False)
    table.add_column("Sev", justify="center")
    table.add_column("Source", justify="center")
    table.add_column("Description", no_wrap=False)
    table.add_column("Fix", no_wrap=False)
    for f in findings:
        table.add_row(
            f.get("title", ""),
            "\n".join(f.get("apis") or []),
            f.get("severity", ""),
            f.get("evidence_source", ""),
            f.get("description", ""),
            f.get("fix_suggestion", ""),
        )
    console.print(table)


def render_memory_trail(memories: list[dict], *, console: Console = console) -> None:
    """The AI strategy's per-turn reasoning — kept distinct from evidence-based findings."""
    if not memories:
        return
    console.print("\n[bold]Explorer memory trail[/bold] [dim](the AI's self-reported reasoning, "
                  "not evidence-based findings)[/dim]")
    for entry in memories:
        mem = entry.get("memory", {})
        body = _format_memory(mem)
        console.print(
            Panel(
                body,
                title=f"turn {entry.get('turn_index', '?')} ({entry.get('phase', '')})",
                title_align="left",
                border_style="dim",
            )
        )


def _format_memory(mem: dict) -> str:
    """Render one turn's AgentMemory (notes/conclusions/plan/open_questions) as panel text."""
    lines: list[str] = []
    for key in ("notes", "conclusions", "plan", "open_questions"):
        value = mem.get(key)
        if not value:
            continue
        lines.append(f"[bold]{key}[/bold]:")
        if isinstance(value, list):
            lines.extend(f"  • {item}" for item in value)
        else:
            lines.append(f"  {value}")
    return "\n".join(lines) or "[dim](empty)[/dim]"


def render_run_list(runs: list, *, console: Console = console) -> None:
    """Print a table of runs (id, target, strategy, status, created)."""
    if not runs:
        console.print("[yellow]No runs found.[/yellow]")
        return
    table = Table(title="Runs", show_lines=False)
    table.add_column("Run ID", style="cyan")
    table.add_column("Target")
    table.add_column("Strategy")
    table.add_column("Status")
    table.add_column("Created")
    for r in runs:
        table.add_row(
            r.run_id,
            r.target_name,
            r.strategy,
            _status_text(r.status),
            r.created_at.isoformat(sep=" ", timespec="seconds") if r.created_at else "—",
        )
    console.print(table)


def _status_text(status: str) -> Text:
    """A run status colored by outcome (green/red/yellow)."""
    color = {"completed": "green", "failed": "red", "running": "yellow"}.get(status, "white")
    return Text(status, style=color)


def render_run_header(run: Any, *, console: Console = console) -> None:
    """Print a run's identity panel (id, target, strategy, status)."""
    console.print(
        Panel.fit(
            f"[bold]{run.run_id}[/bold]\n"
            f"target:   {run.target_name}\n"
            f"strategy: {run.strategy}\n"
            f"status:   {_status_text(run.status)}",
            title="Run",
            title_align="left",
        )
    )

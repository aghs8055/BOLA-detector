"""Render a run's `report.json` payload as one self-contained HTML page.

`render_report_html` is a pure function: it takes the aggregation payload (exactly what
`aggregate_run` builds and `write_report_json` writes) and returns a single HTML document with
inline CSS and no external assets — so the file opens offline and can be shared as-is. Every detail
in the payload is shown: the LLM report (findings + open questions), the deterministic metrics and
per-step costs, and — folded into native `<details>` blocks so the page stays scannable — the full
evidence (API call log, snapshots, AI memory, the analysis pass, the raw spec, and the manifest).

The manifest embeds live credentials (see the runner), so the page is as sensitive as the JSON it
renders; the manifest section is labelled accordingly rather than redacted.
"""

from __future__ import annotations

import json
from html import escape
from typing import Any

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

_STYLE = """
:root {
  --bg: #f6f7f9; --card: #ffffff; --ink: #1b1f24; --muted: #6b7280; --line: #e5e7eb;
  --red: #b42318; --red-bg: #fef3f2; --amber: #b54708; --amber-bg: #fffaeb;
  --green: #067647; --blue: #175cd3; --code-bg: #0f172a; --code-ink: #e2e8f0;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
main { max-width: 1080px; margin: 0 auto; padding: 32px 20px 80px; }
h1 { font-size: 26px; margin: 0 0 4px; }
h2 { font-size: 19px; margin: 36px 0 12px; padding-bottom: 6px; border-bottom: 2px solid var(--line); }
h3 { font-size: 16px; margin: 0 0 8px; }
a { color: var(--blue); }
.sub { color: var(--muted); font-size: 13px; }
.chips { display: flex; flex-wrap: wrap; gap: 8px; margin: 16px 0 0; }
.chip { background: var(--card); border: 1px solid var(--line); border-radius: 999px;
  padding: 5px 12px; font-size: 13px; }
.chip b { font-size: 15px; }
.chip.red { background: var(--red-bg); border-color: #fecdca; color: var(--red); }
.chip.amber { background: var(--amber-bg); border-color: #fedf89; color: var(--amber); }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 16px 18px; margin: 0 0 14px; }
.card.red { border-left: 4px solid var(--red); }
.card.amber { border-left: 4px solid var(--amber); }
.finding-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.badge { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .03em;
  padding: 2px 8px; border-radius: 6px; }
.badge.critical, .badge.high { background: var(--red-bg); color: var(--red); }
.badge.medium { background: var(--amber-bg); color: var(--amber); }
.badge.low, .badge.info { background: #eef2ff; color: var(--blue); }
.badge.src { background: #ecfdf3; color: var(--green); }
.field { margin-top: 10px; }
.field .label { font-size: 12px; font-weight: 700; color: var(--muted);
  text-transform: uppercase; letter-spacing: .04em; }
.apis code, code.api { background: #eef2ff; color: var(--blue); border-radius: 5px;
  padding: 1px 6px; font-size: 12px; margin-right: 6px; display: inline-block; }
ol, ul { margin: 6px 0; padding-left: 22px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--line);
  vertical-align: top; }
th { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .03em; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.status-2 { color: var(--green); } .status-4, .status-5 { color: var(--red); }
.tag { font-size: 11px; padding: 1px 7px; border-radius: 5px; background: #f1f5f9; color: #334155; }
.tag.attacker { background: var(--red-bg); color: var(--red); }
details { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  margin: 0 0 12px; overflow: hidden; }
details > summary { cursor: pointer; padding: 13px 18px; font-weight: 600; list-style: none;
  display: flex; justify-content: space-between; align-items: center; }
details > summary::-webkit-details-marker { display: none; }
details > summary::after { content: "▸"; color: var(--muted); }
details[open] > summary::after { content: "▾"; }
details > summary:hover { background: #fafbfc; }
.details-body { padding: 4px 18px 18px; }
pre { background: var(--code-bg); color: var(--code-ink); border-radius: 8px; padding: 14px;
  overflow: auto; font-size: 12.5px; line-height: 1.5; margin: 8px 0;
  font-family: "SF Mono", Menlo, Consolas, monospace; }
.warn { background: var(--amber-bg); border: 1px solid #fedf89; color: var(--amber);
  border-radius: 8px; padding: 8px 12px; font-size: 13px; margin: 0 0 10px; }
.empty { color: var(--muted); font-style: italic; }
.mono { font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 12px; }
"""


def render_report_html(payload: dict) -> str:
    """Build the full self-contained HTML document for one aggregation `payload`."""
    report = payload.get("report") or {}
    title = escape(str(payload.get("target") or payload.get("run_id") or "BOLA report"))
    body = "\n".join(
        section
        for section in (
            _header(payload, report),
            _summary(report),
            _metrics(payload.get("metrics") or {}),
            _steps(payload.get("steps") or {}),
            _digest(payload.get("summary") or {}),
            _findings(report.get("bola_findings") or [], "BOLA findings", "red"),
            _findings(report.get("non_bola_findings") or [], "Non-BOLA findings", "amber"),
            _open_questions(report.get("open_questions") or []),
            _api_calls(payload.get("api_calls") or {}),
            _snapshots(payload.get("snapshots") or []),
            _memory(payload.get("ai_memory") or []),
            _analysis(payload.get("analysis") or {}),
            _raw_block("OpenAPI spec", payload.get("spec")),
            _manifest(payload.get("manifest")),
        )
        if section
    )
    return (
        "<!doctype html>\n"
        f"<html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>BOLA report — {title}</title><style>{_STYLE}</style></head>"
        f"<body><main>{body}</main></body></html>\n"
    )


def _header(payload: dict, report: dict) -> str:
    """Title bar: target/run identity plus at-a-glance BOLA / non-BOLA / question counts."""
    bolas = len(report.get("bola_findings") or [])
    others = len(report.get("non_bola_findings") or [])
    questions = len(report.get("open_questions") or [])
    meta = " · ".join(
        escape(str(v)) for v in (
            f"run {payload.get('run_id', '?')}",
            f"model {payload.get('model', '?')}",
            f"generated {payload.get('generated_at', '?')}",
        )
    )
    chips = (
        _chip(bolas, "BOLA findings", "red" if bolas else "")
        + _chip(others, "non-BOLA", "amber" if others else "")
        + _chip(questions, "open questions", "")
    )
    return (
        f"<h1>BOLA report — {escape(str(payload.get('target') or payload.get('run_id') or ''))}</h1>"
        f"<div class=\"sub\">{meta}</div>"
        f"<div class=\"chips\">{chips}</div>"
    )


def _summary(report: dict) -> str:
    """The LLM's plain-language description of the target and its intended authorization model."""
    text = report.get("target_summary")
    if not text:
        return ""
    return f"<h2>Target summary</h2><div class=\"card\">{escape(str(text))}</div>"


def _metrics(metrics: dict) -> str:
    """Coverage / execution / LLM / quality numbers as key-value tables."""
    if not metrics:
        return ""
    cards: list[str] = []
    cov = metrics.get("coverage") or {}
    if cov:
        cards.append(_kv_card("Coverage", {
            "Endpoint coverage": f"{cov.get('endpoint_coverage_pct', 0)}% "
            f"({cov.get('operations_called', 0)}/{cov.get('total_operations', 0)} ops)",
            "Objects snapshotted": cov.get("objects_snapshotted", 0),
        }))
    exe = metrics.get("execution") or {}
    if exe:
        rows = {
            "Regular calls": exe.get("regular_calls", 0),
            "Attacker calls": exe.get("hacker_calls", 0),
            "Snapshot calls": exe.get("snapshot_calls", 0),
            "Total time": f"{exe.get('total_time_s', 0)}s",
        }
        codes = exe.get("status_codes") or {}
        if codes:
            rows["Status codes"] = ", ".join(f"{k}={v}" for k, v in codes.items())
        cards.append(_kv_card("Execution", rows))
    llm = metrics.get("llm") or {}
    if llm:
        rows = {"Strategy turns": llm.get("strategy_turns", 0)}
        for label, key in (("Strategy", "strategy_tokens"), ("Analysis", "analysis_tokens")):
            tok = llm.get(key)
            if tok:
                rows[f"{label} tokens (in/out/total)"] = (
                    f"{tok.get('input_tokens', 0)}/{tok.get('output_tokens', 0)}/"
                    f"{tok.get('total_tokens', 0)}"
                )
        cards.append(_kv_card("LLM", rows))
    quality = metrics.get("quality") or {}
    if quality:
        cards.append(_kv_card("Quality vs. ground truth", {
            "Precision": quality.get("precision", 0),
            "Recall": quality.get("recall", 0),
            "F1": quality.get("f1", 0),
            "Accuracy": quality.get("accuracy", 0),
            "Confusion (tp/fp/fn/tn)": f"{quality.get('tp', 0)}/{quality.get('fp', 0)}/"
            f"{quality.get('fn', 0)}/{quality.get('tn', 0)}",
        }))
    if not cards:
        return ""
    return "<h2>Metrics</h2>" + "".join(cards)


def _steps(steps: dict) -> str:
    """Per-step token and wall-clock cost (relations / strategy / analysis / aggregation)."""
    if not steps:
        return ""
    rows = []
    for name, data in steps.items():
        tok = (data or {}).get("tokens") or {}
        duration = (data or {}).get("duration_s")
        rows.append(
            f"<tr><td>{escape(name)}</td>"
            f"<td class=\"num\">{tok.get('input_tokens', 0)}</td>"
            f"<td class=\"num\">{tok.get('output_tokens', 0)}</td>"
            f"<td class=\"num\">{tok.get('total_tokens', 0)}</td>"
            f"<td class=\"num\">{'—' if duration is None else f'{duration}s'}</td></tr>"
        )
    return (
        "<h2>Step costs</h2><div class=\"card\"><table>"
        "<thead><tr><th>Step</th><th class=\"num\">In</th><th class=\"num\">Out</th>"
        "<th class=\"num\">Total</th><th class=\"num\">Duration</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def _digest(summary: dict) -> str:
    """At-a-glance spec surface and API-call breakdown from the payload's `summary` digest."""
    spec = summary.get("spec")
    calls = summary.get("api_calls") or {}
    if not spec and not calls:
        return ""
    cards: list[str] = []
    if spec:
        by_method = spec.get("operations_by_method") or {}
        rows = {"Total operations": spec.get("total_operations", 0)}
        rows.update({m.upper(): n for m, n in by_method.items()})
        cards.append(_kv_card("Spec surface", rows))
    if calls:
        ident = calls.get("by_identity") or {}
        rows = {
            "Total calls": calls.get("total", 0),
            "Regular / attacker": f"{ident.get('regular', 0)} / {ident.get('attacker', 0)}",
        }
        codes = calls.get("by_status_code") or {}
        if codes:
            rows["By status code"] = ", ".join(f"{k}={v}" for k, v in codes.items())
        cards.append(_kv_card("API calls", rows))
    return "<h2>Overview</h2>" + "".join(cards)


def _findings(findings: list[dict], heading: str, color: str) -> str:
    """A category of report findings as severity-sorted cards (silent when empty)."""
    if not findings:
        return ""
    ordered = sorted(findings, key=lambda f: _SEVERITY_ORDER.get(f.get("severity", "info"), 9))
    return f"<h2>{escape(heading)} ({len(findings)})</h2>" + "".join(
        _finding_card(f, color) for f in ordered
    )


def _finding_card(f: dict, color: str) -> str:
    """One finding: title + severity/source badges, then its descriptive and remediation fields."""
    severity = str(f.get("severity", "info"))
    source = str(f.get("evidence_source", ""))
    head = (
        f"<div class=\"finding-head\"><h3>{escape(str(f.get('title', 'Untitled')))}</h3>"
        f"<span class=\"badge {escape(severity)}\">{escape(severity)}</span>"
        + (f"<span class=\"badge src\">{escape(source)}</span>" if source else "")
        + "</div>"
    )
    parts = [head]
    apis = f.get("apis") or []
    if apis:
        parts.append(
            "<div class=\"field apis\">"
            + "".join(f"<code class=\"api\">{escape(str(a))}</code>" for a in apis)
            + "</div>"
        )
    parts.append(_field("Description", f.get("description")))
    parts.append(_field("How it was found", f.get("how_it_was_found")))
    parts.append(_field_list("How to regenerate", f.get("how_to_regenerate") or [], ordered=True))
    parts.append(_field("Fix suggestion", f.get("fix_suggestion")))
    return f"<div class=\"card {color}\">" + "".join(p for p in parts if p) + "</div>"


def _open_questions(questions: list[dict]) -> str:
    """Unresolved items flagged for a human, as a table."""
    if not questions:
        return ""
    rows = "".join(
        f"<tr><td>{escape(str(q.get('question', '')))}</td>"
        f"<td>{escape(str(q.get('why_unresolved', '')))}</td>"
        f"<td>{escape(str(q.get('suggested_next_step', '')))}</td></tr>"
        for q in questions
    )
    return (
        f"<h2>Open questions ({len(questions)})</h2><div class=\"card\"><table>"
        "<thead><tr><th>Question</th><th>Why unresolved</th><th>Next step</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
    )


def _api_calls(api_calls: dict) -> str:
    """The full execution log (who called what, with which ids, and the real status) — collapsed."""
    full = api_calls.get("full") or []
    if not full:
        return ""
    rows = []
    for e in full:
        req = e.get("request") or {}
        identity = str(e.get("identity", "regular"))
        rows.append(
            f"<tr><td class=\"num\">{e.get('seq', '')}</td>"
            f"<td><span class=\"tag {escape(identity)}\">{escape(identity)}</span></td>"
            f"<td class=\"mono\">{escape(str(req.get('method', '')))} "
            f"{escape(str(req.get('path', e.get('op_key', ''))))}</td>"
            f"<td class=\"mono\">{escape(json.dumps(req.get('path_params') or {}))}</td>"
            f"<td class=\"num {_status_class(e.get('status_code'))}\">{e.get('status_code', '')}</td>"
            f"<td>{'✓' if e.get('ok') else '✗'}</td></tr>"
        )
    table = (
        "<table><thead><tr><th class=\"num\">#</th><th>Identity</th><th>Operation</th>"
        "<th>Path params</th><th class=\"num\">Status</th><th>OK</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )
    return _details(f"API calls ({len(full)})", table)


def _snapshots(snapshots: list[dict]) -> str:
    """Captured request/response snapshots the analyzer judged — collapsed, JSON per snapshot."""
    if not snapshots:
        return ""
    blocks = []
    for s in snapshots:
        head = (
            f"<h3>{escape(str(s.get('object_key', s.get('op_key', ''))))} "
            f"<span class=\"tag {escape(str(s.get('phase', '')))}\">{escape(str(s.get('phase', '')))}</span> "
            f"<span class=\"{_status_class(s.get('status_code'))}\">{s.get('status_code', '')}</span></h3>"
        )
        blocks.append("<div class=\"card\">" + head + _json_pre(s.get("response")) + "</div>")
    return _details(f"Snapshots ({len(snapshots)})", "".join(blocks))


def _memory(memories: list[dict]) -> str:
    """The AI explorer's per-turn reasoning trail (self-reported, not evidence) — collapsed."""
    if not memories:
        return ""
    blocks = []
    for entry in memories:
        mem = entry.get("memory") or {}
        items = []
        for key in ("notes", "conclusions", "plan", "open_questions"):
            value = mem.get(key)
            if not value:
                continue
            if isinstance(value, list):
                body = "<ul>" + "".join(f"<li>{escape(str(v))}</li>" for v in value) + "</ul>"
            else:
                body = f"<div>{escape(str(value))}</div>"
            items.append(f"<div class=\"field\"><span class=\"label\">{key}</span>{body}</div>")
        head = (
            f"<h3>turn {entry.get('turn_index', '?')} "
            f"<span class=\"tag\">{escape(str(entry.get('phase', '')))}</span></h3>"
        )
        blocks.append("<div class=\"card\">" + head + ("".join(items) or "<span class=\"empty\">(empty)</span>") + "</div>")
    return _details(f"AI memory trail ({len(memories)})", "".join(blocks))


def _analysis(analysis: dict) -> str:
    """The analysis pass's per-object verdicts as a table — collapsed."""
    findings = (analysis or {}).get("findings") or []
    if not findings:
        return ""
    rows = []
    for f in sorted(findings, key=lambda x: not x.get("is_bola")):
        is_bola = f.get("is_bola")
        verdict = (
            "<span class=\"badge critical\">BOLA</span>" if is_bola
            else "<span class=\"tag\">ok</span>"
        )
        rw = "".join(c for c, k in (("R", "unauthorized_read"), ("W", "unauthorized_write")) if f.get(k))
        rows.append(
            f"<tr><td class=\"mono\">{escape(str(f.get('object_key', '')))}</td>"
            f"<td>{verdict}</td><td>{rw or '—'}</td>"
            f"<td>{escape(str(f.get('rationale', '')))}</td></tr>"
        )
    table = (
        "<table><thead><tr><th>Object</th><th>Verdict</th><th>R/W</th><th>Rationale</th></tr>"
        f"</thead><tbody>{''.join(rows)}</tbody></table>"
    )
    return _details(f"Analysis pass ({len(findings)} object(s))", table)


def _manifest(manifest: Any) -> str:
    """The target manifest — embedded verbatim, so it carries credentials; labelled as sensitive."""
    if not manifest:
        return ""
    warn = "<div class=\"warn\">⚠ Contains live credentials — treat this page as sensitive.</div>"
    return _details("Target manifest (sensitive)", warn + _json_pre(manifest))


def _raw_block(label: str, value: Any) -> str:
    """A collapsed pretty-printed JSON block for a raw payload section (e.g. the spec)."""
    if not value:
        return ""
    return _details(label, _json_pre(value))


# --- small shared builders --------------------------------------------------


def _chip(value: Any, label: str, color: str) -> str:
    """A pill showing a count and its label, optionally tinted."""
    cls = f"chip {color}".strip()
    return f"<span class=\"{cls}\"><b>{escape(str(value))}</b> {escape(label)}</span>"


def _kv_card(title: str, rows: dict) -> str:
    """A titled card wrapping a two-column key/value table."""
    body = "".join(
        f"<tr><th>{escape(str(k))}</th><td>{escape(str(v))}</td></tr>" for k, v in rows.items()
    )
    return f"<div class=\"card\"><h3>{escape(title)}</h3><table><tbody>{body}</tbody></table></div>"


def _field(label: str, value: Any) -> str:
    """A labelled paragraph field (empty string when the value is missing)."""
    if not value:
        return ""
    return (
        f"<div class=\"field\"><span class=\"label\">{escape(label)}</span>"
        f"<div>{escape(str(value))}</div></div>"
    )


def _field_list(label: str, items: list, *, ordered: bool) -> str:
    """A labelled ordered/unordered list field (empty string when there are no items)."""
    if not items:
        return ""
    tag = "ol" if ordered else "ul"
    body = "".join(f"<li>{escape(str(i))}</li>" for i in items)
    return (
        f"<div class=\"field\"><span class=\"label\">{escape(label)}</span>"
        f"<{tag}>{body}</{tag}></div>"
    )


def _details(summary: str, body: str) -> str:
    """A collapsible section (native <details>) wrapping arbitrary HTML body."""
    return (
        f"<details><summary>{escape(summary)}</summary>"
        f"<div class=\"details-body\">{body}</div></details>"
    )


def _json_pre(value: Any) -> str:
    """Pretty-printed, escaped JSON inside a <pre> block."""
    text = json.dumps(value, indent=2, default=str, ensure_ascii=False)
    return f"<pre>{escape(text)}</pre>"


def _status_class(code: Any) -> str:
    """A CSS class keyed by the HTTP status family (2xx green, 4xx/5xx red)."""
    try:
        return f"status-{int(code) // 100}"
    except (TypeError, ValueError):
        return ""
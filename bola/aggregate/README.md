# `bola.aggregate` — report aggregation

Fuse a finished run's two independent evidence streams into one uniform, human-facing report:

- **analysis findings** — the analyzer's per-object BOLA verdicts, judged from snapshots (the
  strongest evidence; may be empty when the spec declared no readable response bodies);
- **AI memory** — the explorer's per-turn notes/conclusions (a witness account of what it attempted
  and observed; weaker, since it is self-reported, not a measured diff).

Confirmation policy (why reads and writes are treated differently): a `200` on an attacker **read**
is strong on its own — the leaked data *is* the crossing — but a `200` on a **write** is not, since
endpoints often return success while changing nothing. A write BOLA is confirmed only by a snapshot
`unauthorized_write` or a destructive effect in the execution log; a write supported only by AI
memory is routed to `open_questions`, not asserted. And because a snapshot proves only that the
*object* changed (not *which* request changed it), when several attacker writes hit one object the
confirmed BOLA is credited to the single operation whose effect matches the observed change — the
rest are open questions. This keeps the report from inflating no-op/ambiguous writes into findings.

## Shape (mirrors `relations` / `analysis`)

- `models.py` — the structured LLM output: `AggregatedReport` = `target_summary` +
  `bola_findings` / `non_bola_findings` (both the uniform `ReportFinding`) + `open_questions`.
  Each finding carries `evidence_source` (`analysis` | `ai_memory` | `both`) so provenance — and the
  gap between confirmed and merely-claimed — is explicit.
- `context.py` — assemble the prompt context from already-fetched evidence; `summarize_spec` adds a
  compact per-operation summary (never full schemas).
- `aggregator.py` — `ReportAggregator`: one structured LLM call (prompt in
  `templates/aggregate/aggregate_report.md`). Injectable for tests.
- `runner.py` — `aggregate_run` gathers the inputs, calls the aggregator, and **upserts** the report
  (one per run; regenerating overwrites). `write_report_json` emits `runs/<run_id>/report.json` —
  the artifact the HTML view renders.
- `html.py` — `render_report_html`: a pure function turning a `report.json` payload into one
  self-contained HTML page (inline CSS, no external assets; the full evidence folded into native
  `<details>` blocks). Exposed as `bola report html <run-id>` / `--input <report.json>`. The page
  embeds the manifest verbatim, so it inherits the JSON's credentials and is equally sensitive.

## The `report.json` payload — a self-contained run bundle

The persisted payload is **portable**: the file alone reconstructs the run with no database. On top
of the report it embeds the full evidence the report is grounded in:

- `report` — the `AggregatedReport` (above);
- `metrics` / `steps` — deterministic run metrics and the per-step token + duration breakdown;
- `summary` — an at-a-glance digest: `spec` (`total_operations` + `operations_by_method`, `null`
  when no spec was supplied) and `api_calls` (`total`, `by_identity` regular vs attacker, and a
  `by_status_code` histogram), derived straight from the spec and the execution log;
- `manifest` — the target manifest **verbatim** (note: this includes the users' credentials, so
  treat a shared `report.json` as sensitive). `null` when no manifest was supplied;
- `spec` — the raw OpenAPI document (`Spec.raw`). `null` when no spec was supplied;
- `api_calls` — `summary` (compact `method` + `path` + `path_params` per call) and `full` (the
  complete execution log, request and response bodies included);
- `snapshots` — every captured object snapshot across the three phases;
- `ai_memory` — the explorer's full per-turn memory trail;
- `analysis` — the analysis pass folded into the report (findings, metrics, tokens, status).

`manifest`/`spec` are embedded only when passed to `aggregate_run`; `run start` always passes both,
while the standalone `bola report aggregate <run-id>` only does so when given `--manifest`.

## Where it runs

A single LLM call over **already-persisted** evidence — it never re-contacts the target, and does
not re-run relation detection, execution, or analysis. It is the final phase of `run start` (runs
automatically after the analysis pass) and is also exposed standalone as `bola report aggregate
<run-id>`, which shows a stored report as-is unless `--fresh` forces one regeneration.

## Tokens

The step records its own `{input_tokens, output_tokens, total_tokens}` slice — in the report payload
and in `AggregatedReportRecord.tokens_json` — consistent with how every other step persists its
token cost separately (strategy per turn, relation detection on the cache, analysis on its record).
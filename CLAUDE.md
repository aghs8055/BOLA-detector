# BOLA Detector — Claude Operating Guide

> Durable rules for working in this repo. This file changes rarely.
> Human overview → `README.md` · Deep design → `docs/` · Current work → `.dev/PLAN.md` · History → `.dev/LOG.md`

## Answering questions

Keep answers to questions **short** by default — a direct answer, minimal elaboration. Give a full,
detailed explanation only when the user explicitly asks to answer completely. (This applies to
discussion; code changes still follow the conventions below.)

## What this is

Semi-automatic BOLA (Broken Object Level Authorization) detection from OpenAPI specs (MSc
thesis). The operator runs the tool, reviews findings, and filters false positives. The goal is
**reduced manual effort, not zero false negatives** — it is explicitly semi-automatic. See
`README.md` for the full overview and `docs/` for design detail.

## Session protocol — follow every session

1. **Orient** — read `.dev/PLAN.md` (what's next) and the latest entry in `.dev/LOG.md` (what
   just happened). Read `docs/*` only when the current step needs it.
2. **Work** — execute the current step from `.dev/PLAN.md`. Keep `PLAN.md` status current as you go.
3. **Wrap up** — run the **`wrap-up`** skill before finishing. It updates `.dev/PLAN.md`, appends a
   `.dev/LOG.md` entry, and syncs `docs/`/`README.md` if a durable fact changed.

`.dev/` is **branch-scoped working state**: commit it on the feature branch, but **never merge it
to `main`** (remove `.dev/` before merging). `docs/`, `README.md`, and this file *are* durable and
do merge to main.

## Where things live

| Need | File |
|---|---|
| Human overview, how to run | `README.md` |
| Package layout + naming rules | `docs/architecture.md` |
| Why each tech choice was made | `docs/design-decisions.md` |
| Git commit + branching conventions | `docs/contributing.md` |
| Makefile target contract | `docs/makefile-contract.md` |
| Run phases + thesis metrics | `bola/runner/README.md` |
| Spec package (full-fidelity loader) design | `bola/spec/README.md` |
| Design invariants (non-negotiable rules) | `docs/architecture.md` + `bola/spec/README.md` |
| Current dev plan + step status | `.dev/PLAN.md` |
| Session-by-session history | `.dev/LOG.md` |

## Coding conventions

- Dataclasses for data, plain functions for logic — no classes unless state is needed.
- Pydantic for LLM structured outputs **and** the OpenAPI spec model.
- **Docstrings everywhere in `bola/` and `cli/`:** every module, class, and function/method (public
  *and* private) has one. State the *responsibility* — what it is for and any non-obvious WHY — not
  a restatement of the signature. One line is enough for small helpers; reserve multi-line docstrings
  for genuine design rationale. A Pydantic/dataclass field whose meaning isn't obvious from its name
  uses `Field(description=...)` / a trailing comment rather than prose in the class docstring.
- **No comments unless the WHY is non-obvious (never the what).** Inline `#` comments are for
  rationale a reader can't infer from the code; do not narrate what the next line does. (Docstrings,
  above, carry purpose — so the two never overlap.)
- Tests mock all HTTP, LLM, and subprocess — no real network in unit tests. `pytest` with the
  `tmp_path` fixture for any file I/O.
- Tests **mirror the package**: `bola/<sub>/<mod>.py` → `tests/unit/<sub>/test_<mod>.py`. No
  `__init__.py` in `tests/`; pytest runs with `--import-mode=importlib`. Test files carry a **module**
  docstring; individual test functions rely on descriptive names instead (the one docstring exception).
- Config files (`requirements.txt`, `settings.yaml`) stay terse: pin versions / set values, no
  narrative comments. `settings.yaml` may keep a short inline note where a value's range is non-obvious.
- Simple > clever. Complexity must originate from the problem, not the implementation.

## AI software-engineering practices

- Structured outputs: Pydantic models for all LLM responses.
- Observability: Langfuse callback on every LangChain call.
- **When verifying correctness or debugging an LLM step, read the actual traces via the Langfuse
  MCP** (`mcp__langfuse__listObservations` / `getObservation`) — the per-call input context and
  structured output (edges + `rationale` + `has_relation`) show *why* a result was produced, not
  just the cached outcome. Filter by `output`/`input` contains, or a `startTime` window (note the
  trace clock is UTC and ingestion can lag a minute). Setup: see `README.md` → Observability.
- Retry + timeout configured via `settings.yaml` — never hard-coded.
- All prompts live in `templates/` — never inline strings.
- LLM-as-judge in integration tests for the AI strategy and analyzer.
- Strategy / analysis / detection are independent; the runner orchestrates.

## Design invariants

This project has non-negotiable design rules (e.g. the full-fidelity spec loader, the package
naming rules). They are **not duplicated here** — read them from their homes before changing those
areas: `docs/architecture.md` (naming rules) and `bola/spec/README.md` (the full-fidelity loader,
including the two discarded lossy attempts — do not resurrect them).
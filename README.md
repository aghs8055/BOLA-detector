# BOLA Detector

Semi-automatic detection of **BOLA** (Broken Object Level Authorization) vulnerabilities from
an API's OpenAPI specification. MSc thesis project.

The tool is **semi-automatic by design**: it does the heavy, repetitive work — exploring the API,
collecting real object IDs, replaying them across user boundaries, and comparing responses — and
surfaces candidate findings. The operator reviews the findings and filters false positives. The
goal is to **reduce manual effort**, not to reach zero false negatives.

## How it works

1. Load the OpenAPI spec → parse into a full, lossless canonical model.
2. Detect relations between operations via an LLM group-check (cached by spec hash in SQLite).
3. Authenticate two users (a regular user and an attacker) using the target's manifest.
4. The regular user explores the API (random or AI strategy), populating an in-memory repo with
   real object IDs.
5. The attacker reuses the regular user's IDs to attempt unauthorized access.
6. Snapshot each object three ways (GET by harvested ID): the owner before the attack, the
   attacker, and the owner after — persisted as evidence.
7. An LLM judges each object's snapshots (stateless, one verdict per object) and emits structured
   findings. Analysis is decoupled and re-runnable: the same run can be re-judged under a different
   access description or model without re-touching the target.
8. Compute thesis metrics (coverage, TP/FP/FN/TN, precision/recall/F1, timing, LLM cost).

See [`docs/`](docs/) for the design detail behind each of these.

---

## Requirements

- **Python 3.12+** (developed on 3.12.3).
- An **OpenAI-compatible LLM endpoint** — an API key plus a base URL (any gateway: OpenRouter,
  Azure OpenAI, a local proxy, …). The default model in `settings.yaml`
  (`anthropic/claude-sonnet-4-6`) is served via a gateway, so the base URL matters.
- Python libraries are pinned in [`requirements.txt`](requirements.txt) — the load-bearing ones:
  `pydantic` + `openapi-pydantic` (spec model & structured LLM I/O), `langchain` / `langchain-openai`
  / `langgraph` (LLM orchestration), `langfuse` (tracing), `SQLAlchemy` (SQLite persistence),
  `requests` (HTTP), `typer` + `rich` (CLI), `pytest` + `responses` (tests).
- **No Docker, Postgres, or Redis** — persistence is a local SQLite file and the ID pool is
  in-memory (see [`docs/design-decisions.md`](docs/design-decisions.md) for why).
- `make` and `jq` are needed **only on a target project's machine** (to emit its manifest), never by
  the detector itself — see [`docs/makefile-contract.md`](docs/makefile-contract.md).

## Setup

```bash
# 1. Clone, then create and activate a virtualenv at the repo root
python3.12 -m venv .venv
source .venv/bin/activate          # zsh/bash

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure secrets — copy the template and fill it in
cp .env.example .env
$EDITOR .env                       # set OPENAI_API_KEY + OPENAI_BASE_URL (Langfuse keys optional)

# 4. Smoke-test: the offline test suite should pass with no network
pytest                             # LLM tests are excluded by default (see Testing)
```

`bin/bola` auto-detects the virtualenv (the repo's own `.venv`, else a parent `.venv`, else the
`python` on `PATH`), so once `.venv` exists you can call it from anywhere. Put it on your `PATH`:

```bash
ln -s "$PWD/bin/bola" ~/.local/bin/bola
```

### Configuration

- **Secrets** live in `.env` only (never `settings.yaml`, never committed — `.env` is git-ignored).
  Keys and what they're for are documented in [`.env.example`](.env.example).
- **Non-secret config** lives in [`settings.yaml`](settings.yaml): model, temperature, retries, the
  per-stage budgets (relations group/batch size, analysis objects-per-call, exploration op counts),
  storage paths, and observability toggles. Each value carries a short inline note where its range
  isn't obvious.
- **Any setting is overridable per-invocation** with `-s/--set section.key=value` (repeatable),
  e.g. `bola -s test.strategy=ai -s relations.batch_size=4 run start target.json`. Common ones also
  have named flags (`--model`, `--db`, `--strategy`, `--no-progress`, …).

---

## Running the detector

The CLI is a hierarchical [Typer](https://typer.tiangolo.com/) app — run it via `bin/bola` (or
`python -m cli`). Every command takes `--help`.

```bash
bola relations detect <spec>          # detect & cache operation relations (resumable)
bola run start <manifest>             # run the full pipeline against a target
bola run start <manifest> --run-id ID # resume an interrupted run from its log
bola run analyze <run-id>             # re-run analysis over a finished run's snapshots
bola report list                      # list recent runs
bola report show <run-id>             # render metrics + findings + the AI memory trail
bola report aggregate <run-id>        # fuse findings + AI memory into one report.json (one LLM call)
bola report html <run-id>             # render report.json into one self-contained HTML page
```

### Pointing it at a target

The detector **never calls `make`** and never touches a target's filesystem — it talks HTTP. A
target project copies a template `Makefile`, fills in a few one-line targets, and runs
`make describe` **on its own machine** to print a **manifest** (base URL, spec location, auth, the
two users). The operator hands that manifest to `bola run start`. The full contract — required
targets, the manifest shape, the template-driven auth model — is in
[`docs/makefile-contract.md`](docs/makefile-contract.md). Run phases and the thesis metrics schema
are in [`bola/runner/README.md`](bola/runner/README.md).

---

## Observability & debugging (Langfuse + MCP)

Every LLM call is traced to Langfuse when `LANGFUSE_*` keys are present in `.env` (the callback is
wired from `settings.yaml`; tracing is silently skipped if the keys are unset). To inspect those
traces **from Claude Code** while verifying or debugging a run, add Langfuse's remote MCP server
(HTTP transport, Basic-auth from your Langfuse keys):

```bash
# keys come from your .env (same LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY)
source .env
claude mcp add --transport http langfuse https://cloud.langfuse.com/api/public/mcp \
  --header "Authorization: Basic $(printf '%s:%s' "$LANGFUSE_PUBLIC_KEY" "$LANGFUSE_SECRET_KEY" | base64)"
claude mcp list   # should show: langfuse ... ✓ Connected
```

Then ask Claude to read traces via the `mcp__langfuse__*` tools (`listObservations`,
`getObservation`). Each generation's input (the prompt context) and structured output (proposed
edges with their `rationale` and `has_relation` verdict) reveal *why* a result was produced — the
fastest way to explain a false positive or a "missing" edge. Notes: trace timestamps are **UTC**
and ingestion can lag ~a minute; filter by `output`/`input` *contains* or a `startTime` window.

---

## Testing

`pytest` is configured in [`pyproject.toml`](pyproject.toml). Tests **mirror the package**
(`bola/<sub>/<mod>.py` → `tests/unit/<sub>/test_<mod>.py`) and run with `--import-mode=importlib`.

```bash
pytest                                   # all offline tests; LLM tests excluded by default
pytest tests/unit/spec                   # one area
pytest -m llm                            # only the real-LLM tests (cost money — see below)
pytest -m llm tests/integration/relations
```

- **Unit tests mock all HTTP, LLM, and subprocess calls** — no network, safe to run anywhere.
- Tests marked **`llm`** make **real LLM calls** and are **excluded by default** (the `addopts`
  `-m 'not llm'`). They need `OPENAI_API_KEY` (and `OPENAI_BASE_URL`) in `.env` and will otherwise
  skip. They cost money and are slower; run them explicitly with `-m llm`. They use small,
  self-contained specs to keep token cost and result-variance low.

---

## Project structure

Everything lives under a single top-level `bola/` package, with subpackages named by
**responsibility** (no `core`/`utils`/`common`); the CLI (`cli/`), entry script (`bin/bola`), and
tests sit beside it. The full directory tree, the naming invariants, the code conventions, and the
test layout are documented once in **[`docs/architecture.md`](docs/architecture.md)** — not repeated
here. Each subpackage with non-trivial design also carries its own `README.md`
(e.g. [`bola/spec/README.md`](bola/spec/README.md), [`bola/runner/README.md`](bola/runner/README.md)).

## Documentation

| Topic | File |
|---|---|
| Package layout + naming rules + conventions | [docs/architecture.md](docs/architecture.md) |
| Technology choices + rationale | [docs/design-decisions.md](docs/design-decisions.md) |
| Makefile target contract (target handshake) | [docs/makefile-contract.md](docs/makefile-contract.md) |
| Run phases + thesis metrics | [bola/runner/README.md](bola/runner/README.md) |
| Spec package (full-fidelity loader) design | [bola/spec/README.md](bola/spec/README.md) |
| Relation detection design | [bola/relations/README.md](bola/relations/README.md) |
| Execution / harvest layer | [bola/execution/README.md](bola/execution/README.md) |
| Strategies (random / AI planners) | [bola/strategies/README.md](bola/strategies/README.md) |
| Analysis (LLM-as-judge) | [bola/analysis/README.md](bola/analysis/README.md) |

---

## Working on this repo with Claude Code

This project is set up to be developed with [Claude Code](https://claude.com/claude-code) (install
it per the official docs, then run `claude` in the repo root).

- **`CLAUDE.md`** is the operating guide Claude reads automatically every session — durable rules,
  coding conventions, the design invariants, and the **session protocol** (orient → work → wrap up).
  Read it before making changes; it points at everything else.
- **Skills** are available for the recurring chores: `update-plan`, `update-log`, `update-docs`, and
  the `wrap-up` orchestrator that runs them at the end of a session. Invoke a skill by typing
  `/<skill-name>`.
- Set up the **Langfuse MCP** (above) so Claude can read live traces when verifying an LLM step.

### Development workflow & the `.dev/` folder

`.dev/` is **branch-scoped working state**: it is committed on a feature branch but **never merged
to `main`** (remove it before merging). It holds two living documents:

| File | Role | Maintained by |
|---|---|---|
| `.dev/PLAN.md` | **Forward-looking** — the single source of "what we're building and what's next": steps, their status, the current marker, the next action, open decisions. | `update-plan` skill |
| `.dev/LOG.md` | **Backward-looking** — a session-by-session history of what changed, decisions made and why, and where to pick up next. | `update-log` skill |

By contrast, `docs/`, `README.md`, and `CLAUDE.md` are **durable** and *do* merge to `main`.

**Starting a new feature:**

1. Branch off `main` (e.g. `git checkout -b feature/<name>`).
2. In `.dev/PLAN.md`, add the feature's steps and set the current marker / next action
   (use the `update-plan` skill, or edit directly).
3. Read `docs/*` only for the area you're touching; follow the conventions in `CLAUDE.md` and
   `docs/architecture.md`. Ship each step with its mirrored unit tests (HTTP/LLM/subprocess mocked).
4. Keep `PLAN.md` status current as you go; run the **`wrap-up`** skill before ending a session — it
   updates `PLAN.md`, appends a `LOG.md` entry, and syncs `docs/`/`README.md` if a durable fact
   changed.
5. Before merging to `main`, **remove `.dev/`** (it does not belong on `main`).
# Architecture

Everything lives under a single top-level package `bola/` (no generic top-level import
names, no `core` catch-all). Subpackages are named by responsibility. CLI entry points and
tests sit beside the package. Folders not yet created are built in their step.

```
new/
├── bola/                         # single top-level package — import as `bola.<subpkg>`
│   ├── settings.py               # Load settings.yaml + env var secrets
│   ├── llm.py                    # build_chat_model + langfuse_handler from Settings (cross-cutting)
│   ├── spec/                     # OpenAPI loading + lossless canonical model
│   │   ├── __init__.py           # façade: re-exports the public surface (Spec, load_spec, errors)
│   │   ├── errors.py             # SpecError hierarchy (Load/Parse/Validation/ReferenceResolution)
│   │   ├── model.py              # Spec dataclass + cycle-safe $ref resolver (+ JSON-Pointer nav)
│   │   ├── loader.py             # load_spec / load_spec_from_dict (read → parse → validate)
│   │   └── views.py              # derived views over Spec: operations, leaf_fields, data_values (schema→data pointer), spec_hash
│   ├── relations/                # LLM data-flow relation detection (resumable); see README.md
│   │   ├── __init__.py           # façade: detect_relations, RelationDetector, GroupRelations, …
│   │   ├── models.py             # Pydantic LLM I/O (GroupRelations, RelationEdge) + Relation record
│   │   ├── context.py            # arrange one source × target-group into the prompt context
│   │   ├── validate.py           # ground a proposed edge against the spec (existence + type/cast)
│   │   ├── detector.py           # detect→validate→revise LangGraph for one group (best-of-attempts)
│   │   └── runner.py             # enumerate, chunk, checkpoint/resume, assemble into RelationCache
│   ├── execution/                # the actuator layer (HTTP + harvest); see README.md
│   │   ├── __init__.py           # façade: ApiExecutor, AuthSession, FieldRepo, harvest, …
│   │   ├── api_executor.py       # execute(req, auth)→ExecutionResult; AuthSession (login/extract/inject + 401 retry); Transport seam
│   │   ├── field_repo.py         # in-memory id pool: harvest, candidates_for_*, fork (no Redis)
│   │   └── snapshotter.py        # capture_snapshots: replay GET-by-id over the id pool, per phase, resumable
│   ├── analysis/                 # judge snapshots for BOLA (decoupled + re-runnable); see README.md
│   │   ├── __init__.py           # façade: Analyzer, analyze_run, ObjectVerdict, group_objects, …
│   │   ├── models.py             # Pydantic LLM I/O: ObjectVerdict (is_bola after rationale), AnalysisBatch
│   │   ├── context.py            # group snapshots by object_key into per-object evidence (hacker-view required)
│   │   ├── analyzer.py           # stateless LLM-as-judge: a verdict per object; injectable chain
│   │   └── runner.py             # analyze_run: resumable pass (objects_per_call/batch_size) → AnalysisRecord
│   ├── aggregate/                 # fuse findings + AI memory + exec log into one report; see README.md
│   │   ├── __init__.py           # façade: ReportAggregator, aggregate_run, AggregatedReport, write_report_json, render_report_html
│   │   ├── models.py             # Pydantic LLM I/O: ReportFinding (evidence_source), OpenQuestion, AggregatedReport
│   │   ├── context.py            # summarize_spec + assemble the prompt context (findings/memory/executions/metrics)
│   │   ├── aggregator.py         # stateless single structured LLM call; injectable chain
│   │   ├── runner.py             # aggregate_run: gather→aggregate→persist (DB + runs/<id>/report.json); per-step steps block
│   │   └── html.py               # render_report_html: report.json → one self-contained HTML page (bola report html)
│   ├── strategies/                # decide what to call (pure planners); see README.md
│   │   ├── __init__.py           # façade: Strategy, StrategyContext, RandomStrategy, AIStrategy, …
│   │   ├── base.py               # Strategy ABC + StrategyContext/PlannedAction + resolve/build_body
│   │   ├── models.py             # AI LLM I/O: AgentTurn, AgentMemory, PlannedCall, InputChoice
│   │   ├── context.py            # arrange ops/relations/id-pool/generators/memory into the prompt
│   │   ├── random_strategy.py    # no-LLM baseline: sample ops, fill from candidate id or generator
│   │   └── ai_strategy.py        # LLM picks ops + per-input choice (repo/generator/concrete) + memory
│   ├── runner/                   # run_manager (phases/checkpoints), metrics
│   ├── target/                   # TargetManifest model: validate the target's describe-manifest (we never call make)
│   ├── generators/               # simple value generators (no LLM); registry + generate_for_schema
│   │   ├── __init__.py           # façade: generate_for_schema, get, describe_all
│   │   └── values.py             # named pure-function generators dispatched by type/format
│   ├── db/                       # models (RunRecord, RelationCache, SnapshotRecord, AnalysisRecord, AggregatedReportRecord, …) + store (repository)
│   └── templates/                # relations/, strategies/, bola/, aggregate/ — LLM prompt files
├── cli/                          # Typer app: app.py (root) + common/render + relations.py, run.py, report.py
├── bin/                          # `bola` — bash dispatcher (runs `python -m cli` via the project venv)
├── tests/
│   ├── unit/                     # All external calls mocked
│   ├── integration/              # Real subprocess/tools (make+jq template test) or juice_shop + network
│   └── fixtures/                 # petstore_3.0.json, crafted specs
├── juice_shop/                   # Makefile + ground_truth.yaml (TP/FP/FN/TN)
├── settings.yaml                 # All non-secret config
├── requirements.txt
└── pyproject.toml                # pytest config
```

## Naming rules (invariants)

These fell out of the design and are enforced everywhere:

- **One top-level package `bola/`**; import as `bola.<subpkg>`. No generic top-level import
  names.
- **Folders are named for *what they do*, never for importance** — so no `core`/`utils`/`common`.
- **The central type is `Spec`**, not `LoadedSpec` (the state of being loaded is implied once
  you hold one).
- **A subpackage is only introduced when it will hold more than one module** (e.g. `spec/`
  will gain `views.py`).

## Where design docs live

- **Cross-cutting design → `docs/`** — concerns that span the whole project (this file,
  `design-decisions.md`, `makefile-contract.md`).
- **Package-specific design → `<package>/README.md`** — co-located with the code it governs, so it
  is updated alongside that code and rendered in the folder view. Example: `bola/spec/README.md`
  holds the full-fidelity loader design. New packages add their own `README.md` once they carry
  non-trivial design.

## Code conventions

The full rules live in `CLAUDE.md` (Coding conventions); the load-bearing ones:

- **Docstrings on everything in `bola/` and `cli/`** — every module, class, and function/method
  (public *and* private) states its responsibility, not a restatement of its signature.
- **Comments only for non-obvious WHY**, never to narrate the what. Purpose lives in the docstring;
  inline `#` comments are reserved for rationale a reader can't infer (and short value annotations
  like `# random | ai`).
- **Config files stay terse** — `requirements.txt` is a bare pinned list; `settings.yaml` sets
  values with at most a short inline note where a range is non-obvious.

## Test layout

- Tests **mirror the package**: `bola/<sub>/<mod>.py` → `tests/unit/<sub>/test_<mod>.py`
  (e.g. `bola/spec/loader.py` → `tests/unit/spec/test_loader.py`).
- No `__init__.py` in `tests/`; pytest runs with `--import-mode=importlib`, so mirrored
  filenames never collide.
- Each test **file** carries a module docstring naming what it covers; individual test functions
  rely on descriptive names instead of docstrings.
# `bola/runner` — run orchestration + metrics

The runner is the **loop and the bookkeeping** the pure components (strategy, snapshotter,
analyzer) deliberately lack. They decide / capture / judge; the runner sequences the phases,
persists everything, and resumes.

## Phases (`execute_run`)

```
setup → run (build → snap_before → attack) → snap_hacker → snap_after → analyze → aggregate
```

- **setup** — build the `ApiExecutor`, an `AuthSession` per identity (`regular` + `attacker`), and
  the operation list. The relations are passed in (loaded/detected by the caller).
- **run (`_run_combined`)** — **one continuous strategy** drives the whole build→attack lifecycle:
  `plan()` → execute each action under **its identity's** `AuthSession` → `harvest` into the single
  shared `FieldRepo` → `observe(results)`. The strategy tags each action `regular` or `attacker`;
  the runner takes the victim baseline (`snap_before`) the moment the **first `attacker`-tagged
  action** appears (or at the end if the run never attacks). One shared repo, no `fork()` — the
  attacker simply replays ids already in the pool.
- **snap_hacker / snap_after** — `capture_snapshots` over the shared repo with attacker then regular
  auth, after the run completes.
- **analyze** — `analyze_and_report`, one analysis pass + metrics (skippable via `analyze=False`).
- **aggregate** — `aggregate_run` (in `bola.aggregate`): a single LLM call fuses the analysis
  findings + AI memory + the execution log into a human-facing `AggregatedReport`, persisted to the
  DB and `runs/<id>/report.json`. Runs after analysis; wrapped so a failure never fails the run.
  Also available standalone as `bola report aggregate <run-id>`.

The boundary is **strategy-driven** for the AI strategy and a **fixed budget split** for the random
baseline (`regular_ops` then `hacker_ops`). In the AI strategy's default **sweep** mode the substage
machine owns the boundary (enumerate/build run under `regular`, sweep/creative under `attacker`); in
`freeform` mode it is agent-declared (`ready_to_attack`), hard-capped by `ai_build_max_turns`. Either
way the runner reacts only to the per-action `identity` tag, so it is agnostic to the substage flow.
When an attacker request fails with a `4xx` *input* error (400/422, not an auth/not-found
401/403/404), the runner gives the strategy a bounded `revise` retry (`revise_max`) to fix the bad
field and resend the same crossing. The analyzer stays phase-based: `snap_before` is the owner's view
by construction, so ownership is encoded by the snapshot phase (the *true-interleaving* variant with
explicit ownership tags is logged as deferred in `.dev/PLAN.md`).

## The execution log is the source of truth

Every executed action is appended to `ExecutionRecord` (`run_id, seq`) the instant it is sent. The
`FieldRepo` is **not** persisted — it is a pure projection rebuilt on resume by replaying `harvest`
over the logged responses. Per-action granularity is required because both phases mutate the
target, so neither is safe to re-run from scratch.

## Resume = reduced budget

Re-invoking `execute_run` with the same `run_id` continues an interrupted run. For each strategy
phase the runner counts the actions/turns already logged and builds the strategy with that much
**less budget**, so completed work is never re-sent. Snapshot capture and analysis are each
independently resumable (they skip what is already on disk).

**Ctrl-C halts cleanly everywhere.** Every action/turn/snapshot/verdict is committed the instant it
happens, and each phase (strategy loop, snapshotting, analysis, relation detection) checkpoints and
then **re-raises** the interrupt by default (`reraise_interrupt=True`), so one Ctrl-C stops the whole
run with no lost or half-applied work — a rerun resumes. Two details make this correct under the
merged loop: `snap_before` is gated on *"has an attacker acted"* (not on a row existing), so an
interrupted baseline is **completed** on resume while the victim is still pristine; and the auto
analysis pass uses a **stable `analysis_id`** (`<run_id>-auto`), so an interrupted analysis resumes
rather than forking a second pass. Standalone callers can pass `reraise_interrupt=False` to instead
get a partial result back.

## AI memory

`AgentMemory` is the one piece of state that is *not* replayable from harvests, so it is persisted
in its own right (`StrategyMemoryRecord`, one row per turn). The latest snapshot is fed back into a
restored strategy on resume; the full per-turn trail is surfaced in the result files as the
explorer's reasoning — kept **separate** from the analyzer's evidence-based findings.

A turn's memory is written reasoning over the *previous* turn's results, so the runner calls the
strategy's `finalize()` after the loop — one last reflection so the final turn's results (typically
the attack) are not lost. The loop also treats an empty plan as "nothing this turn", deferring to
`is_done()` rather than ending, so a turn that crosses build→attack without proposing calls does not
stop the run prematurely.

## Persistence

| What | SQLite | JSON file (`runs/<run_id>/`) |
|---|---|---|
| Execution log | `ExecutionRecord` | — |
| Per-turn AI memory | `StrategyMemoryRecord` | in `run.json` / `analysis_*.json` |
| Run-level metrics | `RunRecord.metrics_json` | `run.json` |
| Per-analysis findings + metrics | `AnalysisRecord.{findings,metrics}_json` | `analysis_<id>.json` |

A fresh analysis pass (new `analysis_id`, different access description or model) writes its own
findings + metrics to both stores, so many comparable passes accumulate per execution `run_id`.

## `metrics.py` — pure, runner-independent

`metrics` reads only the persisted records (execution log, snapshots, verdicts) and derives the
numbers, so it is testable in isolation and recomputable for any past run. Four groups: `coverage`,
`execution` (counts incl. a per-HTTP-status-code breakdown + wall-clock spans from record
timestamps — `phase_times_s` also carries the analysis pass's wall-clock when one ran), `llm`
(strategy turn count + input/output/total tokens summed from the per-turn cost persisted on each
memory snapshot), and `quality`
(precision/recall vs. ground truth — **omitted entirely when no ground truth is supplied**, since
most targets have none). Quality is scored at **operation** granularity: object ids are unstable
across runs, so the stable unit of truth is the endpoint (`ground_truth` = vulnerable op keys).
# `bola.analysis` — judge whether the snapshots show a BOLA

The executor acts and the snapshotter freezes the result; this package *judges* it. Given the three
snapshots of an object — the owner before (`snap_before`), the attacker (`snap_hacker`), the owner
after (`snap_after`) — an LLM-as-judge decides whether one user reached another's object (an
unauthorized read), or changed it (an unauthorized write).

Two complementary evidence paths feed the judge, because the snapshots alone cannot capture every
crossing:
- **Snapshot path** — re-readable GET-by-id objects, judged from the three snapshots.
- **Response path** — reply-only crossings (a reveal, a report, a search, a deep-chain read) whose
  leak lives in the *attacker's own response body*, never in a re-readable object. `response_objects`
  reconstructs these from the execution log: an attacker `2xx` whose request addressed an identifier
  that the **owner's** responses exposed (so it is the owner's id, not the attacker's own). The two
  paths are complementary, not strong-vs-weak — writes surface via the snapshot diff, reply-only
  reads via the response path — and both are judged the same way (`response_judge.md`).

## Decoupled and re-runnable

Analysis never touches the target. It is a **pure function of persisted snapshots**, so one
execution `run_id` can be analysed many times:

```
execution (once) → snapshots persisted → analyze_run (×N)
```

Each pass is a fresh `analysis_id` and is tagged with the inputs that produced it
(`access_desc_hash`, `model`). Re-run with a different `access` description or a different model and
the passes accumulate side by side as `AnalysisRecord`s under the same `run_id` — comparable, never
overwriting each other.

## Stateless verdicts (no memory)

Each verdict is a **local** question — "do these three snapshots indicate a cross-user access?" —
answerable from that object's evidence alone. Unlike the AI strategy, which *explores* and carries
`AgentMemory`, analysis *judges fixed evidence*, so the calls are independent. Statelessness is what
keeps resume, batching, and reproducibility clean: object units are order-free, and a re-run gives
comparable results. Object ownership is not threaded as state — it is implicit in the phase
(`snap_before` is the owner's read, `snap_hacker` the attacker's).

## The pieces

- `models.py` — `ObjectVerdict` (`is_bola` placed after `rationale`, the relation-verdict pattern)
  and `AnalysisBatch` (one call → a verdict per object).
- `context.py` — groups a run's snapshot rows by `object_key` into per-object evidence, and does the
  deterministic reductions *before any LLM call* so the judge sees a small, precise input:
  - **Ownership filter** — only an object the legitimate owner could actually read (`snap_before` is
    a `2xx`) can be the owner's; a refused/absent baseline means it is the attacker's own object (or
    never existed), so it is dropped. This is what stops the attacker's *own* objects being reported.
  - **Deterministic diff** — the owner before→after change is computed here as a git-style path-level
    diff (`victim_change`), not delegated to the model.
  - **Signal gating** — an object with no signal (attacker refused *and* nothing changed) is dropped
    without an LLM call; the model only ever sees objects that actually leaked or actually changed.
  Also builds the response-path evidence (`response_objects` / `build_response_context`).
- `analyzer.py` — the LLM-as-judge. `analyze_batch` is one call; `analyze_batches` runs several
  concurrently (the `graph.batch` equivalent, per-call exceptions returned in place);
  `analyze_response_batches` is the same over the response-path evidence and the `response_judge.md`
  prompt. The chain is injectable, so unit tests run with a canned `AnalysisBatch` and no network.
- `runner.py` — `analyze_run`: the resumable pass. It runs both evidence paths — the snapshot pass
  then the response pass — into one finding set. `objects_per_call` packs objects into one prompt;
  `batch_size` runs that many prompts concurrently. Per-object checkpoints (`AnalysisItemResult`)
  give skip-done / retry-failed / survivors-in-a-failing-batch; the `AnalysisRecord` projection is
  written once every object is done.

## Settings (`settings.yaml → analysis`)

- `objects_per_call` — objects packed into one LLM prompt (the call returns a list of verdicts).
- `batch_size` — prompts run concurrently per batch (same dispatcher as the relation runner).
- `on_error` — `stop | continue` when a batch (or a missing per-object verdict) fails.

The access hint comes from `TargetManifest.access` (set by the target's developer), but is an
overridable argument to the pass, so it can be swapped without editing the manifest. The prompt
(`templates/bola/analyze.md`) is **spec-agnostic** — it describes roles (owner vs attacker), never
fixture field names.
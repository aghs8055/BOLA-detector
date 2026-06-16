# `bola.strategies` — decide *what* to call

The actuator (`bola.execution`) sends a fully-specified request and remembers the ids that come
back; it never chooses an operation or fills an input. A **strategy** makes exactly that decision
and nothing else. It is a **pure planner**: no HTTP, no store, no harvesting. The run manager
(Step 7) drives the loop and owns all persistence:

```
while not strategy.is_done():
    actions = strategy.plan()          # decide a batch of calls
    results = [execute(a) for a in actions]   # runner: ApiExecutor.execute
    harvest(results)                   # runner: into the FieldRepo
    strategy.observe(results)          # strategy reacts
```

Keeping the strategy pure is the same discipline the `FieldRepo` follows — lifecycle policy lives
in the runner, not pushed into the primitive.

## The seam (`base.py`)

- `Strategy` — `plan() -> list[PlannedAction]`, `observe(results)`, `is_done()`.
- `StrategyContext` — the spec, the detected `relations`, a **read** reference to the live
  `FieldRepo`, settings, the operation list, and the operator's neutral `access` note (surfaced to
  the model as `operator_notes` when set — e.g. accounts that are out of scope). The strategy pulls
  candidate ids from the repo; the runner harvests into it between turns.
- `PlannedAction` — an `op_key` + a concrete `ApiRequest` + a `rationale` + an `identity`
  (`regular` | `attacker`). The `op_key` tells the runner which operation ran (for harvesting and
  the execution log); the `identity` tells it which user's credentials to send it under.

## One continuous loop, two stages (build → attack)

A run is a **single** strategy loop that spans two stages: **build** (act as the regular user,
create/read objects to harvest their ids) then **attack** (replay those ids under the attacker's
credentials). The AI carries **one continuous memory** across the boundary. The run manager takes
the victim baseline snapshot (`snap_before`) the moment the first `attacker`-tagged action appears,
then `snap_hacker`/`snap_after` after the attack — so the temporal evidence windows are preserved
even though decision-making is unified. (The deeper *true-interleaving* variant — per-object
baselines + `FieldRepo` ownership tags — is logged as deferred in `.dev/PLAN.md`.)

### Sweep flow — deterministic coverage on the part that needs no intelligence (default)

A single stochastic LLM attack pass *samples* a different subset of operations each run, so recall
drifts run-to-run. The default `attack_mode: sweep` moves single-step coverage onto **deterministic
rails** while keeping the LLM for what it is good at (filling fields, and a creative tail). The
two-stage loop is reshaped into a four-substage machine, all riding the *same* per-turn loop:

```
enumerate → build → sweep → creative
```

- **enumerate** (deterministic, no LLM) — BFS the GET frontier as the regular user: emit every GET
  whose path params the pool can already fill, harvest, repeat until fixpoint. Ensures every object
  *type* the victim can see has an id, so reads have a victim handle to replay.
- **build** (LLM) — the regular user *creates* objects until every producer op has harvested an id
  (the `creation_plan` says how many instances each mutated entity needs so writes don't collide).
- **sweep** (deterministic worklist, LLM fills fields) — the runner enumerates the complete worklist
  = relation-consuming ops × selected victim ids; the LLM only builds one attacker request per pair
  via a **focused** prompt (`attack_fill.md`). Victim ids are ranked created → exclusive → visible
  (`FieldRepo` ownership tiers) and capped (`attack_ids_per_op`); pairs are ordered reads → writes →
  deletes so a delete never wipes an object a later read still needs. This is the robustness fix:
  *which* (op × id) to attack is no longer sampled.
- **creative** (LLM, bounded by `creative_turns`) — the free-form tail for what single-step replay
  cannot express: multi-step sequences, array bodies, and **mass-assignment** (injecting undocumented
  owner/scope fields via `PlannedCall.extra_body`, judged FP-safe by the diff/response paths).

`attack_mode: freeform` keeps the original single-pass LLM attack runnable for the thesis A/B
comparison. A `4xx` *input* error (400/422, not 401/403/404) triggers a bounded `revise` loop
(`revise_request.md`, up to `revise_max`) so a malformed field doesn't waste the pair.

The mechanical sweep fill-turn must **not** overwrite the agent's reasoning memory (it is blind to
responses), and because the sweep tests deletes last, the reasoning turns (creative + `finalize`)
reflect over the *leaking reads* across the whole attack — `_attack_feedback`, not just the last
(usually refused) batch — so memory records the real crossings instead of concluding "all refused".

Two lifecycle guarantees keep a run from ending before it tests anything: a build-stage `is_done`
means *done building*, so it **crosses into attack** rather than terminating (only an attack-stage
`is_done` ends the run); and after the loop the strategy gets one **final reflection** pass
(`finalize`) so the last turn's results — typically the attack — reach memory, since `plan` only ever
reasons over the *previous* turn's results.
- Shared resolution — both strategies turn an abstract choice (a harvested id / a generator / a
  literal) into a value and assemble body-field values addressed by schema pointers into a nested
  JSON body (`build_body`, the inverse of `views.data_values`). They differ only in *how they
  decide*, not in *how they build*.

## `random_strategy.py` — the baseline

No LLM. Samples operations (seedable `rng`) and, per input, applies one fixed rule: if a detected
relation supplies a harvested id, use it; otherwise generate from the field's type/format. Explores
by volume — the contrast the experiment measures the AI strategy against. Spans the same two stages,
but the boundary is a **fixed budget split**, not a reasoned decision: `regular_ops` calls tagged
`regular`, then `attacker_actions` (`hacker_ops`) calls tagged `attacker`.

## `ai_strategy.py` + `models.py` — the LLM-driven strategy

Each turn the model is shown the operations, relations, id pool, generator catalogue, its memory,
the most recent results, and (when set) the operator's `operator_notes` scope constraints
(`context.py`), and returns an `AgentTurn`:

- `memory` (`AgentMemory`: notes / conclusions / plan / open_questions) — rewritten every turn.
- `calls` — a batch of `PlannedCall`s, each with an `identity` (build-stage calls are forced to
  `regular`; attack-stage calls honour the field, default `attacker`). Each input is an
  `InputChoice` discriminated on `kind`:
  - `repo` — reuse a harvested id by `(source_key, json_pointer, index)` (the BOLA replay move),
  - `generator` — a named generator, optionally parametrised (`args`),
  - `concrete` — a literal value.
- `ready_to_attack` — the model declares the build→attack boundary. The runner snapshots the victim
  baseline at this point. A hard cap (`ai_build_max_turns`) forces the switch if the model dawdles;
  the prompt is told the cap (`build_turns_left`) so it paces itself.
- `is_done` — the model's own verdict; also bounded by `ai_max_turns`.

The chat chain is injectable, so unit tests run the whole turn loop with a canned `AgentTurn` and
no network (same pattern as the relation detector). The prompt (`templates/strategies/ai_turn.md`)
is **spec-agnostic** — it describes roles (identifier-producing vs identifier-consuming operations),
never fixture field names.

**Memory persistence is the run manager's job (Step 7), not this package's.** `AgentMemory` is
genuine reasoning state — unlike the `FieldRepo` it cannot be rebuilt by replaying harvests — so it
gets its own per-run snapshot that the runner pulls after each turn and feeds back on resume. The
strategy only reads and rewrites it.

## Multipart / file uploads

A file body field can be filled two ways. The `binary` generator (and the `binary`/`byte` format
mapping) produces a **stand-in** when the contents don't matter. When they do — a field whose
`format` is `binary` or whose `description` names a structure ("one phone number per line") — the AI
strategy picks a **`file` `InputChoice`** (`FileChoice`) and authors the `content` itself, plus an
optional `filename`/`content_type`.

`_apply_body` splits `file` inputs off the ordinary body into `ApiRequest.files` (a
`{field_name: FilePart}` map; the field name is the last named segment of the input's schema pointer,
or `file` for a whole-body upload). The executor sends these as a real `multipart/form-data` request
(`requests` `files=`): the multipart Content-Type/boundary is set by the transport, JSON cannot ride
along, but non-file body fields travel as form `data`. Plain JSON + urlencoded bodies are unchanged.
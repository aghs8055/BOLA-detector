# Key Design Decisions

| Decision | Choice | Reason |
|---|---|---|
| Persistence | SQLite | Zero-infrastructure, file-based, single-user research tool |
| Spec storage | Never stored in DB | Parsed on demand; only sha256 hash stored for cache key |
| Spec model | Full, lossless object (no flattening) | Must preserve every OpenAPI construct — $ref, oneOf/anyOf/allOf, discriminator, cycles. See [bola/spec/README.md](../bola/spec/README.md). |
| Field repo | In-memory Python dict | Phases are sequential; no need for Redis |
| Target interface | Copied Makefile → JSON manifest | Language-agnostic, bounded integration cost; the manifest decouples the detector from the target so it can run on another machine (the detector never calls `make`) |
| Auth model | `login` + `extract` + `inject`, template-driven (not fixed auth types) | Three orthogonal pieces express none/basic/bearer/session and beyond; per-user `vars` bag generalizes past email/password. Structured login (not raw curl) is already fully general — curl would only be parsed back into the same fields. See [makefile-contract.md](makefile-contract.md). |
| Project versioning | None | Not useful for the thesis |
| LLM framework | LangChain + Langfuse | Observability on every LLM call |
| Secrets | `.env` file only | No encryption; gitignored |
| CLI framework | Typer (Click) + Rich | Native hierarchical command tree + auto-generated help; Rich for readable tables/panels. `-s/--set section.key=value` makes every setting overridable without a flag per field. |
| AI attack flow | Deterministic **sweep** (enumerate→build→sweep→creative), not a single LLM pass | A single stochastic attack pass samples a different op subset each run, so recall drifts. The runner enumerates the (op × victim id) worklist deterministically and the LLM only fills fields → feasible vulns caught *every* run; intelligence is kept for field-filling and a bounded creative tail (multi-step + mass-assignment). The old single-pass attack stays runnable as `attack_mode: freeform` for A/B comparison. See [bola/strategies/README.md](../bola/strategies/README.md). |
| Analysis pre-filtering | Deterministic ownership-filter + git-diff + signal-gate **before** the LLM | The attacker's *own* objects (`snap_before` not 2xx) and no-signal objects (refused + unchanged) are dropped in code, so the judge sees only real read/write evidence — fewer calls, no false positives from self-owned objects. Reply-only reads the snapshots can't hold are recovered via a second response-judge path. See [bola/analysis/README.md](../bola/analysis/README.md). |
| Integration-test model | Subject-under-test on cheap **Haiku**; complex LLM-judge **oracle** on a strong model | Running the strategy/analysis/aggregation on Haiku is the point (validate the cheap model). But the end-to-end test's 3-stream LLM-judge oracle grades incoherently on Haiku (conflates streams, contradicts its own rubric), so the *oracle* uses a strong model — a reliable verdict over the same Haiku-produced evidence. Simple single-boolean judges stay on Haiku. |
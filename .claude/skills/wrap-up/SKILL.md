---
name: wrap-up
description: End-of-session orchestrator for the BOLA Detector repo. Run before finishing a session to record progress and history. Updates .dev/PLAN.md (step status + next action), appends a .dev/LOG.md entry, and syncs docs/ and README.md when a durable fact changed. Use when the user says "wrap up", "end the session", "save progress", or before stopping work.
---

# Wrap up a session

Run this before ending a work session. It captures what happened so the next session can orient
from `.dev/PLAN.md` + `.dev/LOG.md` alone. It composes the three granular skills — invoke each in
order, skipping any that genuinely has nothing to record.

## Steps

1. **Review what changed this session.** Look at the conversation and `git status`/`git diff` to
   know what was actually done, decided, and left unfinished. Do not invent progress — only record
   what truly happened.

2. **Update the plan** — apply the `update-plan` skill to `.dev/PLAN.md`:
   - Mark completed steps ✅ and move the "CURRENT" marker / "Next action" to the right place.
   - Add/resolve entries under "Open decisions / questions".

3. **Append a log entry** — apply the `update-log` skill to `.dev/LOG.md`:
   - Prepend a new dated `## Session N` entry below the header (newest first).
   - Include: focus, what changed, decisions + their *why*, and an explicit "Start here next".

4. **Sync durable docs (only if needed)** — apply the `update-docs` skill *only* when a durable
   fact changed (architecture, a design decision, the Makefile contract, metrics schema, the spec
   loader design, conventions). Transient progress does NOT belong in `docs/`, `README.md`, or
   `CLAUDE.md` — it belongs in `.dev/`. If nothing durable changed, skip this and say so.

5. **Report** a one-line summary of which files you touched.

## Guardrails

- `.dev/PLAN.md` and `.dev/LOG.md` are branch-scoped: they are committed on the feature branch but
  **never merged to `main`**. Do not move their content into `docs/`/`README.md`/`CLAUDE.md`.
- Do not commit or push unless the user asks.
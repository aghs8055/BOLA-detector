---
name: update-plan
description: Update the forward-looking development plan at .dev/PLAN.md for the BOLA Detector repo — step statuses, the current step marker, the next action, and open decisions. Use when a step finishes or starts, when the next action changes, or when a planning decision is made or resolved.
---

# Update the development plan (.dev/PLAN.md)

`.dev/PLAN.md` is the single source of "what we're building and what's next" on this branch. Keep
it forward-looking — history goes in `.dev/LOG.md`, durable design in `docs/`.

## What to edit

- **Step statuses:** flip `⬜` → `✅ DONE` when a step is genuinely complete (code + mirrored tests
  passing). Move the `⬜ CURRENT` marker to the step now in progress.
- **Next action:** keep the top "Next action" line pointing at the very next concrete task.
- **Open decisions / questions:** add newly-surfaced questions; remove or mark resolved ones (record
  the resolution + why in `.dev/LOG.md`).
- **New steps:** if scope grows, add a step in the right place using the existing format
  (files + tests).

## Rules

- Only mark a step DONE if it really is — don't aspirationally tick boxes.
- Keep entries terse: files to create + tests to write, mirroring the package layout.
- Branch-scoped: this file is committed on the feature branch but **never merged to `main`**.
- Do not duplicate durable design here — link to `docs/` instead.
---
name: update-docs
description: Sync the durable design reference (docs/, per-package <package>/README.md, README.md) and operating rules (CLAUDE.md) for the BOLA Detector repo when a durable fact changes — architecture, a design decision, the Makefile contract, the metrics schema, a package's design, conventions, or naming rules. Use only for permanent facts, not transient progress (which goes in .dev/).
---

# Update durable docs (docs/, <package>/README.md, README.md, CLAUDE.md)

These files are **durable and merge to `main`**. Only edit them when a *permanent* fact changes —
not session progress (that goes in `.dev/PLAN.md`/`.dev/LOG.md`).

## Where durable docs live

- **Cross-cutting design → `docs/`** (whole-project concerns that don't belong to one package).
- **Package-specific design → `<package>/README.md`** (co-located with the code it governs, e.g.
  `bola/spec/README.md`). New packages get their own `README.md` as they gain non-trivial design.
- **Human overview → `README.md`** (root). **Claude operating rules → `CLAUDE.md`.**

## Pick the right file

| Changed fact | File to edit |
|---|---|
| Package layout, naming rules, test layout | `docs/architecture.md` |
| A technology choice / its rationale | `docs/design-decisions.md` |
| Makefile target contract | `docs/makefile-contract.md` |
| Run phases, execution log, metrics schema | `bola/runner/README.md` |
| A specific package's design or impl notes | `bola/<package>/README.md` |
| Project purpose, how-to-run, overview | `README.md` |
| Claude operating rules, conventions, session protocol | `CLAUDE.md` |

## Rules

- Keep `CLAUDE.md` lean: durable rules + the session protocol + a pointer index. It must **not**
  contain design invariants — link to their homes (`docs/architecture.md`, `bola/spec/README.md`).
- Cross-cutting reference belongs in `docs/`; package-specific reference in `<package>/README.md`;
  human overview in `README.md`.
- When you add or rename a doc file, update the link tables in `README.md` and `CLAUDE.md`.
- Don't duplicate the same fact across files — put it in one place and link to it.
- Don't put transient/branch-scoped state here. If it's about "what we're doing now", it belongs
  in `.dev/`.
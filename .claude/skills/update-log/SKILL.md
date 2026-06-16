---
name: update-log
description: Append a session entry to the backward-looking history at .dev/LOG.md for the BOLA Detector repo. Use to record what changed in a session, decisions made and why, and where the next session should start.
---

# Append a session log entry (.dev/LOG.md)

`.dev/LOG.md` is append-only history, newest first. One entry per session. It is what the next
session reads (with `.dev/PLAN.md`) to know what just happened.

## How to write an entry

Prepend a new entry directly below the header block, above the most recent session:

```markdown
## Session N (YYYY-MM-DD)

**Focus:** one line on the session's theme.

- What changed (files/modules, tests).
- Each decision made, with its **why**.
- Anything left unfinished or deferred.

**Start here next:** the concrete next task (should match `.dev/PLAN.md`'s "Next action").
```

## Rules

- Use today's date. Convert any relative dates ("yesterday") to absolute.
- Increment the session number from the latest entry.
- Record only what actually happened — no aspirational claims. If tests failed, say so.
- Keep decisions with their rationale; the "why" is the most valuable part for future sessions.
- Branch-scoped: this file is committed on the feature branch but **never merged to `main`**.
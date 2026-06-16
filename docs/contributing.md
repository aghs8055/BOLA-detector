# Git & Development Conventions

> Durable rules for commits and branches. Project operating rules → `CLAUDE.md`.

## Commit messages — Conventional Commits

Every commit follows the [Conventional Commits](https://www.conventionalcommits.org/) spec:

```
<type>(<optional scope>): <description>

<optional body>

<optional footer(s)>
```

- **Subject** is imperative, lower-case, no trailing period, ≤ 72 chars.
- **Type** is one of:

  | Type | Use for |
  |---|---|
  | `feat` | A new user-facing feature |
  | `fix` | A bug fix |
  | `docs` | Documentation only |
  | `refactor` | Code change that neither fixes a bug nor adds a feature |
  | `test` | Adding or correcting tests |
  | `chore` | Tooling, deps, config, housekeeping |
  | `perf` | Performance improvement |
  | `style` | Formatting only, no logic change |
  | `ci` | CI/build pipeline changes |

- **Scope** (optional) names the affected package or area, e.g. `feat(relations):`,
  `fix(spec):`, `refactor(cli):`.
- **Breaking changes** add a `!` after the type/scope (`feat(spec)!: ...`) and/or a
  `BREAKING CHANGE:` footer describing the migration.

Examples:

```
feat(strategies): add AI-driven request mutation strategy
fix(spec): resolve cyclic $ref without infinite recursion
docs: document the runner phase metrics
refactor(cli): split run command out of app module
```

## Branching & merging

1. **Branch off `main`** for every new feature or fix — never commit feature work directly to
   `main`:

   ```
   git switch -c feat/<short-topic> main
   ```

2. **Commit freely on the branch** following the message conventions above. Granular commits are
   fine here; they get squashed on merge.

3. **Keep up to date by rebasing** onto `main` (never merge `main` in):

   ```
   git fetch origin
   git rebase origin/main
   ```

4. **Merge with rebase + squash** so each feature lands as a single, well-formed commit on `main`:

   ```
   git switch main
   git merge --squash feat/<short-topic>
   git commit            # write one Conventional Commit summarizing the feature
   ```

   The squashed commit message must itself follow Conventional Commits.

5. **Delete the branch** after it lands:

   ```
   git branch -d feat/<short-topic>
   ```

`main` therefore has a **linear history of one squashed Conventional Commit per feature** — no merge
commits, no work-in-progress noise.

> Note: `.dev/` is branch-scoped working state — commit it on the feature branch but remove it
> before the squashed commit reaches `main` (see `CLAUDE.md`).
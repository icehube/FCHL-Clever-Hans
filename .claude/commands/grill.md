---
description: "Adversarial code review -- don't ship until it passes"
---

Adversarial code review. Don't let me ship until the changes pass your scrutiny.

## 1. Establish the diff

Review everything not yet on the main branch, in this order:

1. `git status --short` and `git diff HEAD` — uncommitted work (staged and unstaged)
2. If the current branch is not `main`: `git diff main...HEAD` — commits on this branch

Review the union of both. If both are empty, say so and stop — there is nothing to grill.

## 2. Review every change as a skeptical staff engineer

- **Correctness** — logic errors, edge cases, race conditions. Is the change complete, or does it half-implement the intent?
- **Tests** — missing coverage for new or changed behavior. Assert-nothing tests count as missing.
- **Error handling** — adequate and not swallowed. Per CLAUDE.md: don't skip error handling.
- **Project conventions** — type hints on signatures, money in millions, market-adjusted prices in the optimizer (never raw model prices), flat module layout, comments explain WHY not WHAT. Check `.claude/rules/` for the domain rules that apply to the files touched.
- **Breaking changes** — to endpoints, state serialization, or data formats. State files on disk must still load.
- **Security** — injection, auth, data exposure.
- **Performance** — regressions, especially extra MILP solves on the request path.

## 3. Rate the changes

**SHIP IT** / **NEEDS WORK** / **BLOCK**

## 4. Report

- If NEEDS WORK or BLOCK: list each issue with file, line, and what to fix
- If SHIP IT: state what you verified, not just the verdict

## 5. Iterate

After I make fixes, re-review from step 1. Only give SHIP IT when every issue is resolved.

Per CLAUDE.md commit discipline, `/quick-commit` after each issue resolved.

## 6. Don't drop findings

Anything you flag that we decide *not* to fix now — out of scope, judgment-call skip, valid-but-deferred refactor — goes into `BACKLOG.md` under **Open findings**, in the documented format. Scan for an existing entry on the same file:line first and update its date rather than duplicating.

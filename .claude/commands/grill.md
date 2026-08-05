---
description: "Adversarial code review -- don't ship until it passes"
---

Adversarial code review. Don't let me ship until the changes pass your scrutiny.

## 1. Establish the diff

`$ARGUMENTS` may scope the review. Take the first rule that applies:

1. **`$ARGUMENTS` names a scope** — a commit range (`abc123..def456`), a count ("last 3 commits"), a date ("since yesterday"), or a batch in prose ("the changes before today"). Resolve it with `git log --format='%h %ad %s' --date=format:'%Y-%m-%d %H:%M' -15` to see where the batches fall, then review `git diff <base>..<head>`. State the range you picked so I can correct it.
2. **Uncommitted work exists** (`git status --short` is non-empty) — review `git diff HEAD`, staged and unstaged.
3. **Branch is not `main`** — review `git diff main...HEAD`.
4. **Otherwise** — this project commits straight to `main`, so a clean tree is the normal state after any `/quick-commit`; it does NOT mean there is nothing to review. Show `git log --oneline -10` and review the most recent batch of related commits, saying which ones you took.

Never answer "nothing to grill" just because the working tree is clean.

Before reporting, check `BACKLOG.md` — findings already tracked there under **Open findings** are known and should not be re-reported as new. Say so if a change touches one.

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

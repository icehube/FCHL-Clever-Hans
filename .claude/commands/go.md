---
description: "Verify, simplify, and commit — the ship sequence"
---

Run the full ship sequence on the current uncommitted changes.

1. **Test** — `.venv/bin/pytest tests/`. If any test fails, stop and report.
2. **Verify** — invoke the `verify-app` agent for end-to-end testing.
3. **Solver check (conditional)** — if `git diff --name-only HEAD` lists `optimizer.py` or `market.py`, invoke the `solver-checker` agent. **If it reports any unchecked boxes, stop and do not commit.**
4. **Simplify** — invoke the `code-simplifier` agent to clean up the diff.
5. **Re-test** — run `.venv/bin/pytest tests/` again to confirm simplification didn't break anything.
6. **Commit** — invoke the `quick-commit` skill to stage and commit with a descriptive message.

If any step fails, stop and report. Do not commit broken or unverified code.

After commit, report:
- Files changed (count and one-line summary)
- Tests run and pass count
- Commit SHA and message
- Anything the simplifier flagged but didn't auto-fix

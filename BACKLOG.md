# Backlog

Findings flagged by review agents (`/grill`, `/go`, `/simplify`, `/review-changes`, etc.) that were **not** addressed in the change that surfaced them. Triage and act on (or close out) when you have time.

Format: `- [YYYY-MM-DD] [source] file:line — finding — reason deferred`

---

- [2026-05-02] [simplify] main.py:852 — `/move-to-minors` calls `save_snapshot()` before validation, so rejected requests pay a full JSON serialize+deserialize round-trip — pre-existing pattern across `/move-to-roster`, `/adjust-salary`; fix is a cross-endpoint refactor, not introduced by the bench-check change
- [2026-05-02] [simplify] main.py — many endpoints repeat `save_snapshot → try → except ValueError → restore + _toast(str(e))`; could be extracted to a shared helper/decorator — out of scope for behavioral changes
- [2026-05-02] [simplify] state.py:176 — `send_to_minors` duplicates the "find by name in a player list" loop pattern also used in `remove_player` and `find_player`; could centralize into a shared helper — out of scope for behavioral changes
- [2026-05-02] [simplify] tests/test_state.py:9 — `_make_player_on_roster` accepts `is_minor` but not `is_bench`; callers set `p.is_bench = True` post-construction — only 2 callers today, marginal benefit
- [2026-05-02] [simplify] tests/test_endpoints.py:241 — no `_draft_and_bench` composite test helper; round-trip tests call `_draft_to` then `/toggle-bench` separately — only 1 caller today, marginal benefit
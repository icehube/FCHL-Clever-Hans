# Backlog

Findings flagged by review agents (`/grill`, `/go`, `/simplify`, `/review-changes`, etc.) that were **not** addressed in the change that surfaced them. Triage and act on (or close out) when you have time.

Format: `- [YYYY-MM-DD] [source] file:line — finding — reason deferred`

---

- [2026-05-02] [simplify] main.py:852 — `/move-to-minors` calls `save_snapshot()` before validation, so rejected requests pay a full JSON serialize+deserialize round-trip — pre-existing pattern across `/move-to-roster`, `/adjust-salary`; fix is a cross-endpoint refactor, not introduced by the bench-check change
- [2026-05-02] [simplify] main.py — many endpoints repeat `save_snapshot → try → except ValueError → restore + _toast(str(e))`; could be extracted to a shared helper/decorator — out of scope for behavioral changes
- [2026-05-02] [simplify] state.py:176 — `send_to_minors` duplicates the "find by name in a player list" loop pattern also used in `remove_player` and `find_player`; could centralize into a shared helper — out of scope for behavioral changes
- [2026-05-02] [simplify] tests/test_state.py:9 — `_make_player_on_roster` accepts `is_minor` but not `is_bench`; callers set `p.is_bench = True` post-construction — only 2 callers today, marginal benefit
- [2026-05-02] [simplify] tests/test_endpoints.py:241 — no `_draft_and_bench` composite test helper; round-trip tests call `_draft_to` then `/toggle-bench` separately — only 1 caller today, marginal benefit

## 2026-07-05 full-app review (5 parallel reviewers + live testing)

Triaged 2026-07-05. Fixed items moved to "Resolved" below; the rest remain open.

### Open — engine/market

- [2026-07-05] [review] optimizer.py:87 — positive-point pool smaller than remaining spots (or cheapest legal roster > budget) → MILP Infeasible → bid advice degrades to floor values. UI warning badge added in auction_control.html so it's no longer silent; actual short-roster planning (optimize the N players you CAN buy) still unbuilt — needs a plan-with-fewer-spots MILP mode
- [2026-07-05] [review] optimizer.py:401 — drain score divides by can_afford, so it nominates players FEW opponents can afford — gifting the rich opponent a bargain at second-highest ceiling instead of draining budgets — rework drain heuristic
- [2026-07-05] [review] state.py:141 — physical_max_bid=0 when roster full excludes that team from market ceiling, but CBA allows drafting beyond 24 (extras to minors, full cap) — a cap-rich full team is an invisible live bidder — needs design: spots vs minors-overflow in physical max

### Open — domain/state

- [2026-07-05] [review] trade.py:240 — buyout of a non-cap-counting minors player (groups A-E) INCREASES cap hit while evaluator reports positive net_cap_freed (verified: +2.0 cap on $4M group-E buyout) — buyout math needs a minors-aware branch
- [2026-07-05] [review] state.py:199 — recall_from_minors/add_acquired_player have no 24-man check: roster_count 25 reachable via legal endpoints (verified); team then vanishes from market math — awaiting triage

### Open — frontend/UX

- [2026-07-05] [review] templates/base.html:8 — htmx, DaisyUI, and Tailwind Play CDN (runtime JIT, not for production) all load from third-party CDNs with no local fallback; draft-night network blip = dead cockpit — vendor locally before draft night
- [2026-07-05] [review] templates/partials/auction_control.html:139 — Assign form's hidden salary field holds render-time price: typing the final price then clicking Assign races the change-triggered re-render and can record a stale salary — awaiting triage
- [2026-07-05] [review] main.py:840 — ctx["team"] override in /toggle-bench and /adjust-salary leaks the edited team into ALL panels: opponent roster shows up in Trade "I Give" and buyout buttons — awaiting triage
- [2026-07-05] [review] main.py:781 — /set-nominator (and /nominate, and unknown-player /bid-check) re-renders auction_control with base context, destroying an in-flight bidding session (player, price, bidder toggles live only in the DOM) — awaiting triage

### Open — test infrastructure

- [2026-07-05] [review] tests/test_data_loader.py — 15+ assertions pin the live players.csv (704 biddable, salaries, penalties, McDavid's team); every data refresh breaks them with no correctness signal — awaiting triage
- [2026-07-05] [review] tests/ — coverage gaps: /trade-between happy path, undo-after-{adjust-salary,move-to-minors,move-to-roster,set-nominator}, MILP-infeasible rendering, corrupt-state startup fallback; assert-nothing tests in test_stress.py:146 (pass-body loop), test_bid_calculator.py:157 (>=0 tautology), test_edge_cases.py:312 (500 accepted) — partially reduced (trade guards, combo turn, endgame, live ceiling now tested); rest awaiting triage

### Resolved (fix commits 200e80d, e4a5871, c30a636, 8622f74, 6dd17e6, 9aece0d + docs)

- optimizer.py:111 — endgame spots=0 marginal collapse → fixed 200e80d: full roster + non-negative budget is Optimal; forced-fill valued correctly
- optimizer.py:215 — must-have player valued at floor when without-solve infeasible → fixed 200e80d: valued at physical max
- optimizer.py:238 — binary search never evaluated hi (−0.1 at cap) → fixed 200e80d
- optimizer.py:274 — max_bid clamped UP to $0.5 when physical_max ≤ 0 → fixed 200e80d: DROP with max_bid 0.0
- market.py:130 — live ceiling included BOT → fixed 200e80d: compute_live_ceiling excludes MY_TEAM; highest-opponent max when BOT bids
- optimizer.py/market.py — float artifacts ($4.300000000000001M) → fixed 200e80d: rounded at source
- config.py:12 — hard 14F/7D/3G composition → owner decision 2026-07-05: intentional shape, made SOFT via starter/bench MILP (e4a5871): BACKUP_TARGETS + BACKUP_BONUS + BENCH_WEIGHT; only starting lineup scores
- market.py+optimizer.py — RFA sealed-bid/ROFR mechanics → CLOSED by owner decision 2026-07-05: not modeled; operator places sealed bid = advisor's optimal bid at that moment
- trade.py:147 — ACCEPT on cap-violating trades → fixed c30a636: legality guard (cap ≥ 0 AND MILP Optimal) on all scenarios
- trade.py:293 — received players hardcoded group="3" → fixed c30a636: group/salary/points taken from source roster
- trade.py:301 — pool re-entrants with team_probability=0/nhl_team="" → fixed c30a636: DEFAULT_TEAM_PROBABILITY, nhl_team preserved
- main.py:517 — stale trade eval executable → fixed c30a636: trade_id round-trip + _recompute clears last_trade_eval
- main.py:923 — /trade-between one-sided trades + self-trade → fixed 6dd17e6: validate-all-before-mutate, team_a==team_b rejected, is_minor/is_bench reset
- main.py:821 — /toggle-bench had no snapshot → fixed 6dd17e6
- main.py:346 — /assign snapshotted before validation → fixed 6dd17e6: validate first; clamp toast added
- main.py:857 — /adjust-salary 500 on unknown player → fixed 6dd17e6: validate first, toast error
- state.py:292 — restore_snapshot skipped change_log → fixed 6dd17e6
- main.py:383 — combo turn double-advance → owner confirmed 1-RFA+1-UFA turns; fixed 9aece0d: RFA sale keeps turn, UFA sale advances
- static/shortcuts.js:14 — Ctrl+Z while typing / key-repeat / no feedback → fixed 6dd17e6: typing guard, e.repeat guard, undo toasts what it undid
- static/shortcuts.js:21 — browser-reserved Ctrl+N → fixed 6dd17e6: plain 'n' with typing guard
- static/shortcuts.js — no htmx error listeners → fixed 6dd17e6: responseError + sendError → toast
- tests/ — pytest clobbered data/state/auction_state.json → fixed 8622f74: conftest.py monkeypatches main.STATE_DIR to tmp dir
- CLAUDE.md — doc drift (buyout dots, missing endpoints, market.py:20 docstring) → fixed in docs commit; owner decisions recorded in CLAUDE.md
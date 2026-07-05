# Backlog

Findings flagged by review agents (`/grill`, `/go`, `/simplify`, `/review-changes`, etc.) that were **not** addressed in the change that surfaced them. Triage and act on (or close out) when you have time.

Format: `- [YYYY-MM-DD] [source] file:line — finding — reason deferred`

---

- [2026-05-02] [simplify] main.py:852 — `/move-to-minors` calls `save_snapshot()` before validation, so rejected requests pay a full JSON serialize+deserialize round-trip — pre-existing pattern across `/move-to-roster`, `/adjust-salary`; fix is a cross-endpoint refactor, not introduced by the bench-check change
- [2026-05-02] [simplify] main.py — many endpoints repeat `save_snapshot → try → except ValueError → restore + _toast(str(e))`; could be extracted to a shared helper/decorator — out of scope for behavioral changes
- [2026-05-02] [simplify] state.py:176 — `send_to_minors` duplicates the "find by name in a player list" loop pattern also used in `remove_player` and `find_player`; could centralize into a shared helper — out of scope for behavioral changes
- [2026-05-02] [simplify] tests/test_state.py:9 — `_make_player_on_roster` accepts `is_minor` but not `is_bench`; callers set `p.is_bench = True` post-construction — only 2 callers today, marginal benefit
- [2026-05-02] [simplify] tests/test_endpoints.py:241 — no `_draft_and_bench` composite test helper; round-trip tests call `_draft_to` then `/toggle-bench` separately — only 1 caller today, marginal benefit

## 2026-07-05 full-app review (5 parallel reviewers + live testing) — triage pending

### Critical (runtime-confirmed unless noted)

- [2026-07-05] [review] optimizer.py:111 — forcing a player with exactly 1 roster spot left makes spots=0 → Infeasible guard → marginal value $0.5 for ANY player (verified: same star $0.5 with 1 spot vs $11.3 with 2); advisor says DROP on BOT's last pick — awaiting triage
- [2026-07-05] [review] optimizer.py:215 — when the without-player solve is infeasible (e.g. only remaining goalie), marginal value returns MIN_SALARY — a must-have player valued at the floor, exactly backwards — awaiting triage
- [2026-07-05] [review] trade.py:147 — evaluate_trade recommends ACCEPT on cap-violating trades: scenarios compare total_points only, never MILP status or negative cap (verified: accept with cap_remaining=-7.1, status=Infeasible) — awaiting triage
- [2026-07-05] [review] config.py:12 — POSITION_MINIMUMS 14F/7D/3G sums to exactly 24, so the MILP hard-forces that composition and never explores the CBA's 4-any-position bench (verified: 90 pts left on table vs legal 16F/6D/2G); contradicts CLAUDE.md — needs owner decision: house heuristic or bug
- [2026-07-05] [review] static/shortcuts.js:14 — global Ctrl+Z fires POST /undo even while typing in an input; no toast on undo; key-repeat unwinds multiple picks silently — awaiting triage
- [2026-07-05] [review] main.py:857 — /adjust-salary unknown player → uncaught ValueError → HTTP 500 (invisible to user; salary input auto-submits on change) + phantom snapshot retained — awaiting triage

### Major — engine/market

- [2026-07-05] [review] optimizer.py:274 — max_bid clamped UP to $0.5 BID when physical_max_bid ≤ 0 (full roster or overcommitted); violates never-exceed-physical-max invariant — awaiting triage
- [2026-07-05] [review] optimizer.py:87 — positive-point pool smaller than remaining spots (or cheapest legal roster > budget) → MILP Infeasible → every panel silently degrades to $0.5/DROP with no UI signal — awaiting triage
- [2026-07-05] [review] market.py:130 — compute_live_ceiling includes BOT itself (default_bidders includes BOT), so live ceiling can be highest-opponent-max instead of second-highest; live vs idle modes disagree with each other and the documented invariant — awaiting triage
- [2026-07-05] [review] optimizer.py:401 — drain score divides by can_afford, so it nominates players FEW opponents can afford — gifting the rich opponent a bargain at second-highest ceiling instead of draining budgets — awaiting triage
- [2026-07-05] [review] state.py:141 — physical_max_bid=0 when roster full excludes that team from market ceiling, but CBA allows drafting beyond 24 (extras to minors, full cap) — a cap-rich full team is an invisible live bidder — awaiting triage
- [2026-07-05] [review] optimizer.py:238 — marginal-value binary search never evaluates hi: systematic −0.1 understatement at the cap — awaiting triage
- [2026-07-05] [review] market.py+optimizer.py — RFA sealed-bid + right-of-first-refusal mechanics entirely unimplemented; second-highest-ceiling math is ascending-auction logic and systematically loses sealed RFA auctions — product gap, needs owner decision

### Major — domain/state

- [2026-07-05] [review] trade.py:293 — players BOT receives in trades arrive hardcoded group="3" (give side preserves group); breaks minors cap semantics for group C/A-E players — awaiting triage
- [2026-07-05] [review] trade.py:240 — buyout of a non-cap-counting minors player (groups A-E) INCREASES cap hit while evaluator reports positive net_cap_freed (verified: +2.0 cap on $4M group-E buyout) — awaiting triage
- [2026-07-05] [review] state.py:199 — recall_from_minors/add_acquired_player have no 24-man check: roster_count 25 reachable via legal endpoints (verified); team then vanishes from market math — awaiting triage
- [2026-07-05] [review] main.py:517 — last_trade_eval survives /undo, /reset, /load-scenario and TradeEvaluation.trade_id is never sent/checked; stale eval executable against a different world; legacy flow also doesn't validate pool membership (duplication possible) — awaiting triage
- [2026-07-05] [review] main.py:923 — /trade-between swallows per-player ValueError → silent one-sided trades; also allows team_a == team_b (self-trade launders keeper into acquired_players, defeating keeper minors-block) — awaiting triage
- [2026-07-05] [review] main.py:821 — /toggle-bench is the only mutating endpoint without save_snapshot: undo after a toggle silently reverts the PREVIOUS action instead — awaiting triage
- [2026-07-05] [review] main.py:383 — nomination advances once per /assign, but a CBA combo turn (1 RFA + 1 UFA) would advance twice, skipping teams — needs owner confirmation of real-draft flow
- [2026-07-05] [review] main.py:346 — /assign snapshots before validating player name; failed assign leaves a no-op snapshot so next undo does nothing (double-click reachable: assign form has no hx-sync guard) — awaiting triage
- [2026-07-05] [review] state.py:292 — restore_snapshot restores everything except change_log: undone edits remain in the audit log — awaiting triage
- [2026-07-05] [review] trade.py:301 — pool re-entrants rebuilt with team_probability=0.0, nhl_team="" → out-of-distribution repricing (model floor ~1-3%); also is_bench never reset on trade/minors moves — awaiting triage

### Major — frontend/UX

- [2026-07-05] [review] templates/base.html:8 — htmx, DaisyUI, and Tailwind Play CDN (runtime JIT, not for production) all load from third-party CDNs with no local fallback; draft-night network blip = dead cockpit — awaiting triage
- [2026-07-05] [review] static/shortcuts.js — no htmx:responseError/sendError/timeout listener anywhere: failed POSTs are completely invisible — awaiting triage
- [2026-07-05] [review] templates/partials/auction_control.html:139 — Assign form's hidden salary field holds render-time price: typing the final price then clicking Assign races the change-triggered re-render and can record a stale salary — awaiting triage
- [2026-07-05] [review] main.py:840 — ctx["team"] override in /toggle-bench and /adjust-salary leaks the edited team into ALL panels: opponent roster shows up in Trade "I Give" and buyout buttons — awaiting triage
- [2026-07-05] [review] main.py:781 — /set-nominator (and /nominate, and unknown-player /bid-check) re-renders auction_control with base context, destroying an in-flight bidding session (player, price, bidder toggles live only in the DOM) — awaiting triage
- [2026-07-05] [review] static/shortcuts.js:21 — Ctrl+N is browser-reserved (opens new window in Chrome/Firefox); shortcut is dead or destructive — awaiting triage
- [2026-07-05] [review] optimizer.py:274/market.py — market_ceiling and reasoning strings rendered unrounded: float artifacts like $4.300000000000001M visible mid-auction — awaiting triage

### Test infrastructure

- [2026-07-05] [review] tests/ — pytest writes through to data/state/auction_state.json (verified by checksum): a real draft state on disk is clobbered by running the suite; needs conftest.py with STATE_DIR tmp-dir monkeypatch + shared TestClient fixture — awaiting triage
- [2026-07-05] [review] tests/test_data_loader.py — 15+ assertions pin the live players.csv (704 biddable, salaries, penalties, McDavid's team); every data refresh breaks them with no correctness signal — awaiting triage
- [2026-07-05] [review] tests/ — zero coverage: /trade-between, undo-after-{adjust-salary,move-to-minors,move-to-roster,set-nominator}, MILP-infeasible rendering, corrupt-state startup fallback; assert-nothing tests in test_stress.py:146 (pass-body loop), test_bid_calculator.py:157 (>=0 tautology), test_edge_cases.py:312 (500 accepted) — awaiting triage
- [2026-07-05] [review] CLAUDE.md — doc drift: buyout dots documented as hx-trigger="load" but implemented as manual scan button; endpoint table missing /move-to-minors, /move-to-roster, /load-scenario; demand_count docstring stale in market.py:20 — awaiting triage
# Backlog

The single work list for this project: deferred review findings plus forward-looking ideas.

**Open findings** are things flagged by review agents (`/grill`, `/go`, `/simplify`, etc.) that were **not** addressed in the change that surfaced them. Format: `- [YYYY-MM-DD] [source] file:line — finding — reason deferred`

**Ideas / future work** are unprompted improvements with no specific defect behind them. No file:line.

---

## Open findings

Triaged 2026-07-05.

### engine/market

- [2026-07-05] [review] optimizer.py:87 — positive-point pool smaller than remaining spots (or cheapest legal roster > budget) → MILP Infeasible → bid advice degrades to floor values. UI warning badge added in auction_control.html so it's no longer silent; actual short-roster planning (optimize the N players you CAN buy) still unbuilt — needs a plan-with-fewer-spots MILP mode
- [2026-07-05] [review] optimizer.py:401 — drain score divides by can_afford, so it nominates players FEW opponents can afford — gifting the rich opponent a bargain at second-highest ceiling instead of draining budgets — rework drain heuristic
- [2026-07-05] [review] state.py:141 — physical_max_bid=0 when roster full excludes that team from market ceiling, but CBA allows drafting beyond 24 (extras to minors, full cap) — a cap-rich full team is an invisible live bidder — needs design: spots vs minors-overflow in physical max
- [2026-08-05] [owner] optimizer.py:362 — **advisor says DROP on a bargain once BOT is the last bidder standing.** Reported live: `DROP / Max bid $0.6M / Marginal $4.2M / Ceiling $0.5M / Price $2.5M exceeds max bid $0.6M` on a good player at a good price. Root cause chain: last opponent drops out → `active_bidders` is `[MY_TEAM]` only → `compute_live_ceiling` skips MY_TEAM, finds no opponent ceilings, returns MIN_SALARY (market.py:161) → `max_bid = min(marginal, ceiling + INCREMENT, physical_max)` collapses to $0.6M → `current_price >= max_bid` → DROP (optimizer.py:377). The ceiling is a *forecast of the clearing price*, valid for planning; it is invalid as a bid cap once a real price is on the table, because the price cannot go back down. With no opponent left, the only question is whether current_price < marginal value ($2.5M < $4.2M → take it). Fix direction: when there is no live opponent, cap at marginal value bounded by physical_max, not by the ceiling — the ceiling should only bind while the price is still below it — **draft-night critical, this tells you to walk away from free points**

### domain/state

- [2026-07-05] [review] trade.py:240 — buyout of a non-cap-counting minors player (groups A-E) INCREASES cap hit while evaluator reports positive net_cap_freed (verified: +2.0 cap on $4M group-E buyout) — buyout math needs a minors-aware branch
- [2026-07-05] [review] state.py:199 — recall_from_minors/add_acquired_player have no 24-man check: roster_count 25 reachable via legal endpoints (verified); team then vanishes from market math — awaiting triage

### frontend/UX

- [2026-07-05] [review] templates/base.html:8 — htmx, DaisyUI, and Tailwind Play CDN (runtime JIT, not for production) all load from third-party CDNs with no local fallback; draft-night network blip = dead cockpit — vendor locally before draft night
- [2026-07-05] [review] templates/partials/auction_control.html:139 — Assign form's hidden salary field holds render-time price: typing the final price then clicking Assign races the change-triggered re-render and can record a stale salary — awaiting triage
- [2026-07-05] [review] main.py:840 — ctx["team"] override in /toggle-bench and /adjust-salary leaks the edited team into ALL panels: opponent roster shows up in Trade "I Give" and buyout buttons — awaiting triage
- [2026-07-05] [review] main.py:781 — /set-nominator (and /nominate, and unknown-player /bid-check) re-renders auction_control with base context, destroying an in-flight bidding session (player, price, bidder toggles live only in the DOM) — awaiting triage
- [2026-08-05] [owner] market.py:107 — Model $ and Market $ columns are always identical, which reads as a broken display. The math is correct: `market_price = min(model_price, market_ceiling)`, and while budgets are full the ceiling sits at MAX_SALARY ($11.4M), which no model price exceeds — so `min()` returns the model price on every row until opponent budgets drain late in the draft. Correct but useless: two identical columns for most of the auction train you to ignore both. Fix is presentational — collapse to one column and surface the ceiling only when it actually binds (e.g. show Market $ struck-through against Model $ only when `ceiling < model_price`) — needs a UI decision, not an engine change

### code quality

- [2026-05-02] [simplify] main.py:852 — `/move-to-minors` calls `save_snapshot()` before validation, so rejected requests pay a full JSON serialize+deserialize round-trip — pre-existing pattern across `/move-to-roster`, `/adjust-salary`; fix is a cross-endpoint refactor, not introduced by the bench-check change
- [2026-05-02] [simplify] main.py — many endpoints repeat `save_snapshot → try → except ValueError → restore + _toast(str(e))`; could be extracted to a shared helper/decorator — out of scope for behavioral changes
- [2026-05-02] [simplify] state.py:176 — `send_to_minors` duplicates the "find by name in a player list" loop pattern also used in `remove_player` and `find_player`; could centralize into a shared helper — out of scope for behavioral changes

### test infrastructure

- [2026-07-05] [review] tests/test_data_loader.py — 15+ assertions pin the live players.csv (704 biddable, salaries, penalties, McDavid's team); every data refresh breaks them with no correctness signal — awaiting triage
- [2026-07-05] [review] tests/ — coverage gaps: /trade-between happy path, undo-after-{adjust-salary,move-to-minors,move-to-roster,set-nominator}, MILP-infeasible rendering, corrupt-state startup fallback; assert-nothing tests in test_stress.py:146 (pass-body loop), test_bid_calculator.py:157 (>=0 tautology), test_edge_cases.py:312 (500 accepted) — partially reduced (trade guards, combo turn, endgame, live ceiling now tested); rest awaiting triage
- [2026-05-02] [simplify] tests/test_state.py:9 — `_make_player_on_roster` accepts `is_minor` but not `is_bench`; callers set `p.is_bench = True` post-construction — only 2 callers today, marginal benefit
- [2026-05-02] [simplify] tests/test_endpoints.py:241 — no `_draft_and_bench` composite test helper; round-trip tests call `_draft_to` then `/toggle-bench` separately — only 1 caller today, marginal benefit

---

## Ideas / future work

### Price model (Layer 1)

Track these; don't implement upfront. The market layer (Layer 2) already compensates for some of them — only build one if draft-day testing shows the base model plus market layer isn't accurate enough.

- **Dynamic budget deflation** — scale model price by (remaining league budget / starting league budget) as a simple auction-phase correction. The model is currently static and does not adjust for budget depletion mid-auction.
- **Positional scarcity in the model layer** — boost model price when a position's supply/demand ratio is tight. The market layer partially handles this via demand count, but the model price itself doesn't adjust.
- **Price momentum** — rolling correction based on recent actual-vs-predicted ratios during the draft.
- **Goalie features** — games played, save percentage, team defense quality. Goalies were the weakest position pre-rebuild; the round-2 rebuild (July 2026) moved them onto projected wins, so **re-measure goalie accuracy against the current wins-based model before adding features** — the old accuracy numbers no longer apply.
- **Auction position effect** — early picks tend to sell higher than the model predicts.
- **Non-linear points × team_probability interaction term.**

### UI / UX

From live debugging and testing, 2026-08-05. These are cockpit-ergonomics items — the engine is right, the interface makes it hard to act on.

- **Auto-show the counterfactual for the player being bid on.** Right now it's a separate lookup; during live bidding there's no time to go get it. It should appear as soon as a player is under the hammer.
- **Make the counterfactual text area say what to *do* with it.** The numbers are there but the call to action isn't — it should read as advice, not as a data dump.
- **Buyout Analyzer: replace the current flow with Scan button + dropdown of my roster → select a player → show "Execute Buyout".** Fewer steps, no typing a name during a live break.
- **Decompose Model $ into its drivers — how much comes from projected points vs. NHL team quality** (and reputation/lag salary, which is the third big term). Needs a per-coefficient contribution breakdown out of `price_model.py`; the two-stage log-normal form means contributions are multiplicative on price, so decide whether to show them in log space or as "% of predicted price".
- **Save State button that jumps between live state and a scenario**, so testing a what-if doesn't cost the real draft state. Interacts with the scenario loader (`POST /load-scenario`) and the undo snapshot chain — check that switching can't strand a snapshot.
- **Standardize font size in the price-distribution graph** (`GET /player-chart/{name}` SVG) — sizes are currently inconsistent across labels and axes.

### Testing

- **More scenarios.** Extend the pre-baked set behind `POST /load-scenario` — the gaps worth covering are the ones that keep producing findings: last-goalie endgame, drained-budget late draft, a cap-rich team with a full roster, and bidding down to a single remaining bidder (see the DROP finding above).

### Performance

- **Interaction budget: every UI interaction < 500ms.** Carried over from the original build plan; never asserted anywhere. Worth a test that times the MILP-triggering endpoints (`/assign`, `/bid-check`, `/trade-evaluate`) against a full late-draft state.

---

## Resolved

Resolved findings live in git history — see fix commits `200e80d`, `e4a5871`, `c30a636`, `8622f74`, `6dd17e6`, `9aece0d`.

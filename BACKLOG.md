# Backlog

The single work list for this project: deferred review findings plus forward-looking ideas.

**Open findings** are things flagged by review agents (`/grill`, `/go`, `/simplify`, etc.) that were **not** addressed in the change that surfaced them. Format:

`- [YYYY-MM-DD] [source] file:line (symbol) — finding — reason deferred`

Name the enclosing function or property in `(symbol)`. Line numbers drift every time the file above them changes — on 2026-08-05 a third of the references in this file pointed at unrelated code — and a stale line sends you somewhere wrong without saying so. The symbol survives the drift and makes the entry greppable. Templates have no symbols, so those entries carry a line only.

`tests/test_backlog_refs.py` enforces this: every `file.py:line` here must resolve, and must sit inside the function it names. That is why editing code can fail the suite on a docs file — re-anchor the affected entries in the same commit. A Python reference without an identifier-shaped symbol fails too, since a prose parenthetical would opt out of the only check that catches drift.

**Ideas / future work** are unprompted improvements with no specific defect behind them. No file:line.

---

## Open findings

Last triaged 2026-08-06. No entries are waiting on a manual check — both
outstanding ones were confirmed in a browser that day.

### engine/market

- [2026-07-05] [review] optimizer.py:247 (solve_optimal_roster) — positive-point pool smaller than remaining spots (or cheapest legal roster > budget) → MILP Infeasible → bid advice degrades to floor values. UI warning badge added in auction_control.html so it's no longer silent; actual short-roster planning (optimize the N players you CAN buy) still unbuilt — needs a plan-with-fewer-spots MILP mode
- [2026-08-05] [grill] optimizer.py:327 (compute_marginal_value) — `hi = min(team.spendable_budget + MIN_SALARY, MAX_SALARY)` re-derives the with-spots branch of `physical_max_bid` by hand instead of calling it. They agree today only because the `total_spots_remaining <= 0` early return at :290 means this line never runs for a full roster. If `physical_max_bid` changes again the binary search silently won't follow — deferred: no bug today, but it is the same duplicate-predicate trap that left the drain filter stale

### domain/state

- [2026-08-05] [review] state.py:259 (add_acquired_player / recall_from_minors) — recall_from_minors/add_acquired_player have no 24-man check, so roster_count 25 is reachable via legal endpoints (verified). The "team then vanishes from market math" half is fixed — a full team now reports its real budget as physical_max_bid. What remains is whether a 25th player should sit in acquired_players at all: the CBA says extras go to *minors*, so this is arguably an invalid state rather than a market bug. Needs a decision (auto-route to minors on assign vs. refuse the recall). Note a 25-man roster makes total_spots_remaining negative, which makes solve_optimal_roster infeasible for that team — harmless for an opponent, but it would degrade every recommendation if it ever happened to BOT — awaiting that decision

### frontend/UX

- [2026-08-06] [owner] static/vendor/tailwindcss-play-3.4.17.js — Tailwind's Play bundle JITs utility classes in the browser on every page load; a real build would ship a fraction of the CSS with no runtime cost — deferred: needs node + npm + the daisyui plugin and a rebuild on every template edit, and it is *riskier* here, because `static/shortcuts.js` builds class names at runtime (`'alert-' + type`) which a source-scanning build cannot see. That case survives today only because DaisyUI's prebuilt CSS carries every `alert-*` variant. Revisit only if page load becomes a real complaint
- [2026-08-06] [grill] templates/partials/buyout_dots.html:3 — the "Scan Roster" dots cover eligible ACTIVE-roster players only, so group 2/3 players in the minors get no dot even though they are buyout-eligible and the Buyout Analyzer lists them. Not a correctness bug — the panel button still evaluates them on demand — but the scan silently under-reports. Fixing it means giving minors rows (and dot placeholders) in team_panel.html, which today renders only `keeper_players + acquired_players` — deferred: needs a minors section in the team table, which is a layout change, not a one-liner
- [2026-08-05] [owner] templates/partials/auction_control.html:124 — the price input's `change` (fires on BLUR) swaps the whole `#auction-control`, so a /bid-check response landing between mousedown and mouseup removes the Assign button and swallows the click — it looks like nothing happened. The stale-salary half of this is fixed (Assign now reads #bid-price at submit time), and /bid-check runs a binary search over MILP solves so the response almost always lands after mouseup — deferred: fixing it properly means reworking how the price input re-renders the panel
- [2026-08-05] [owner] main.py:925 (toggle_bench / adjust_salary) — editing an opponent's roster via /toggle-bench or /adjust-salary now snaps the team panel back to BOT, because the ctx["team"] override that kept it on the edited team was leaking that team into the Trade and buyout panels (fixed 2026-08-05). Restoring the view without the leak needs a separate `viewed_team` context key used only by team_panel.html, leaving `team` as BOT everywhere else — and a decision about which team the buyout "Scan Roster" dots belong to, since they OOB-swap into whichever roster the panel is showing — deferred: ~20 mechanical edits in team_panel.html plus that decision
- [2026-07-05] [review] main.py:885 (set_nominator) — /set-nominator (and /nominate, and unknown-player /bid-check) re-renders auction_control with base context, destroying an in-flight bidding session (player, price, bidder toggles live only in the DOM) — awaiting triage
- [2026-08-05] [grill] templates/partials/team_panel.html:35 — the Spots box renders `total_spots_remaining` raw, which goes negative past 24 (a 25-man roster shows "-1"). Cosmetic, but it reads like a bug to the operator mid-draft. Related: the same negative value makes `solve_optimal_roster` infeasible for that team — see the `state.py (add_acquired_player / recall_from_minors)` entry, which owns the underlying question of whether a 25-man active roster should exist at all — deferred: display fix is trivial, but pointless until that decision lands
- [2026-08-05] [grill] templates/partials/auction_control.html:101 — "Max bid" is a single number blending two different things: the value cap (`min(marginal, physical_max)` — a hard never-exceed) and the expected stop (`ceiling + 0.1` — a forecast of where bidding ends). The verdict ladder no longer runs on the blend (fixed a3de737), but the displayed number still jumps without explanation when the forecast releases — e.g. $1.1M at a price of $1.0M, then $4.1M at $1.1M. Surfacing both ("worth up to $4.1M; $1.1M should win it") would make the jump legible and the advice self-explaining — deferred: UI redesign of the bid panel, and the engine now behaves correctly either way

### code quality

- [2026-05-02] [simplify] main.py:1017 (move_to_minors) — `/move-to-minors` calls `save_snapshot()` before validation, so rejected requests pay a full JSON serialize+deserialize round-trip — pre-existing pattern across `/move-to-roster`, `/adjust-salary`; fix is a cross-endpoint refactor, not introduced by the bench-check change
- [2026-05-02] [simplify] main.py — many endpoints repeat `save_snapshot → try → except ValueError → restore + _toast(str(e))`; could be extracted to a shared helper/decorator — out of scope for behavioral changes

### test infrastructure

- [2026-08-05] [review] tests/test_data_loader.py — 15+ assertions pin the live players.csv (704 biddable, salaries, penalties, McDavid's team); every data refresh breaks them with no correctness signal. Same class: `test_endpoints.py::TestPriceColumn::test_nothing_capped_at_full_budgets` holds only because the top model price (~$9.5M) sits under the full-budget ceiling ($11.4M) — a pricier pool would fail it without anything being wrong — awaiting triage
- [2026-08-05] [grill] tests/test_endpoints.py:11 (client) — the `client` fixture is `scope="module"`, so `POST /reset` runs ONCE for the whole file, not per test. Any test that mutates global state without restoring it leaks into every later test in the module (`TestPanelContextIsolation` leaves a bench toggle flipped; the salary tests rely on `/undo` to clean up). Nothing is broken today, but it makes test order load-bearing and it is invisible at the call site — I asserted the opposite during a review before checking. Options: switch to function scope and eat the per-test `/reset` cost, or add a teardown that snapshots and restores — awaiting triage
- [2026-07-05] [review] tests/ — coverage gaps: /trade-between happy path, undo-after-{adjust-salary,move-to-minors,move-to-roster,set-nominator}, MILP-infeasible rendering, corrupt-state startup fallback; assert-nothing tests in tests/test_stress.py:150 (_check_invariants) — `pass`-body loop, tests/test_bid_calculator.py:339 (test_counterfactual_shows_alternatives) — `len(...) >= 0` tautology, tests/test_edge_cases.py:406 (test_team_players_nonexistent) — accepts a 500 as a pass — partially reduced (trade guards, combo turn, endgame, live ceiling now tested); rest awaiting triage

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
- **Buyout Analyzer: replace the current flow with Scan button + dropdown of my roster → select a player → show "Execute Buyout".** Fewer steps, no typing a name during a live break.
- **Decompose Model $ into its drivers — how much comes from projected points vs. NHL team quality** (and reputation/lag salary, which is the third big term). Needs a per-coefficient contribution breakdown out of `price_model.py`; the two-stage log-normal form means contributions are multiplicative on price, so decide whether to show them in log space or as "% of predicted price".
- **Save State button that jumps between live state and a scenario**, so testing a what-if doesn't cost the real draft state. Interacts with the scenario loader (`POST /load-scenario`) and the undo snapshot chain — check that switching can't strand a snapshot.

### Testing

- **More scenarios.** Extend the pre-baked set behind `POST /load-scenario` — the gaps worth covering are the ones that keep producing findings: last-goalie endgame, drained-budget late draft, a cap-rich team with a full roster (now a live bidder as of `4dc59da` — a scenario would let you see its effect on ceilings rather than trusting the unit tests), and bidding down to a single remaining bidder.

### Performance

- **Interaction budget: every UI interaction < 500ms.** Carried over from the original build plan; never asserted anywhere. Worth a test that times the MILP-triggering endpoints (`/assign`, `/bid-check`, `/trade-evaluate`) against a full late-draft state.

---

## Resolved

Resolved findings live in git history. Fix commits, oldest first:

- Pre-2026-08: `200e80d`, `e4a5871`, `c30a636`, `8622f74`, `6dd17e6`, `9aece0d`
- 2026-08-05 bid advisor: `8b928eb` (WIN not DROP when last bidder standing), `a3de737` (verdict ladder runs on value, not the blended max_bid), `ce20814` (one definition of "last bidder standing")
- 2026-08-05 money handling: `fa60955` (floor budgets to the $0.1M step), `d9e4fd2` + `4d77c21` (quantize salary on /assign and /adjust-salary via `_legal_salary`)
- 2026-08-05 market: `4dc59da` (a full roster is capacity, not a zero ceiling), `0bb86c0` (demand counts use "can bid", not "has spots")
- 2026-08-05 UI: `d57d344` (Assign reads the live price — **manually confirmed in a browser 2026-08-06**: typing a new price and clicking Assign with no Tab/Enter records the typed price), `713f029` (zero counterfactual delta is a toss-up), `a779ec2` (one price column), `fcd9647` (chart label size)
- 2026-08-06 offline: htmx, DaisyUI and the Tailwind Play CDN are vendored under `static/vendor/` and pinned by `tests/test_offline_assets.py` — **manually confirmed working offline 2026-08-06**
- 2026-08-06 CSS size: vendored DaisyUI trimmed 2.93 MB → 468 KB (84%) by `trim_daisyui.py`, which drops unused opacity-suffixed colour utilities and copies everything else through byte-for-byte. **The old entry here was wrong twice.** It blamed the ~30 stock themes (those are 53 KB of the 2.93 MB; the real bulk is 21,588 generated colour-utility rules, 84%) and recommended `dist/styled.min.css`, which defines 609 classes to full's 24,940 and is missing 19 the app uses — `btn-sm`, `badge-xs`, `table-xs`, `tooltip`, `text-warning`, `bg-base-200` among them. Following that advice would have caused exactly the mid-draft visual regression the entry warned about. Guarded by two tests in `tests/test_offline_assets.py`: one drives the running app and checks every colour class it emits is defined, the other fails on an undeclared `bg-{{ … }}/NN` in a template
- 2026-08-06 drain nominations: ranked on market price, tie-broken on least surplus gifted (`_best_drain_candidate`), pinned by `tests/test_nomination.py::TestDrainStrategy`. **The old entry's stated mechanism did not reproduce.** It claimed dividing by `can_afford` made the tool favour players few opponents could afford; measured over 291 randomised budget spreads and two simulated drafts, the old formula's pick matched the drain-maximising pick *every time*, zero divergences — the clearing price is monotone in model price, so both rules collapse to "nominate the priciest unwanted player". Its conclusion ("gifting the rich opponent a bargain") was right, one level down: once the ceiling binds, every player above it drains *exactly* the ceiling (35 of 636 UFAs tied at $2.5M in the test state), and ranking that tied set by model price picked the biggest bargain for the buyer — $5.2M of surplus vs $0.0M for the same $2.5M drained. Two further defects rode along: drain was the only optimizer path reading raw model prices, so the panel showed `Expected: ~$7.7M` for a player who could fetch $2.5M; and `max(can_afford, 1)` scored the unaffordable case highest, printing `0 can afford` as a reason to nominate. The `needing_position` multiplier also cost real drain dollars where the ceiling did *not* bind — it took Aho at $7.5M over Vasilevskiy at $7.7M, leaving $0.3M unburned (pinned by `test_position_need_never_outranks_dollars`). Fix reuses `compute_market_price`, verified over 4,000 random league states to be exactly the second-highest-willingness clearing price. Measured over a 200-pick simulated draft: the new rule drains strictly less in 0 of 16 sampled decisions (total $33.1M → $33.4M) while surplus gifted falls from $9.6M to $0.1M
- 2026-08-06 buyouts: eligibility restricted to groups 2/3 (`can_be_bought_out`), the panel now lists eligible players wherever they sit, trade scenarios stop proposing illegal buyouts, keepers can be sent to the minors, and both buyout endpoints report the real reason. **The old entry here asked for "minors-aware buyout math" — that premise was wrong.** Under the owner's rules (2026-08-06) a legal buyout only ever targets a group 2/3 player, whose salary is fully on the cap wherever they sit, so `salary_freed`/`net_cap_freed` needed no branch and got none. The actual defects were a missing legality guard in both directions: the panel offered A-E prospects on the active roster (illegal, and the numbers looked plausible because those *do* count on cap) while hiding group 2/3 players in the minors (legal, and where everyone drafted past 24 lands). Pinned by `tests/test_buyout_eligibility.py`

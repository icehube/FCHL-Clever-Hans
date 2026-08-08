# Backlog

The single work list for this project: deferred review findings plus forward-looking ideas.

**Open findings** are things flagged by review agents (`/grill`, `/go`, `/simplify`, etc.) that were **not** addressed in the change that surfaced them. Format:

`- [YYYY-MM-DD] [source] file:line (symbol) — finding — reason deferred`

Name the enclosing function or property in `(symbol)`. Line numbers drift every time the file above them changes — on 2026-08-05 a third of the references in this file pointed at unrelated code — and a stale line sends you somewhere wrong without saying so. The symbol survives the drift and makes the entry greppable. Templates have no symbols, so those entries carry a line only.

`tests/test_backlog_refs.py` enforces this: every `file.py:line` here must resolve, and must sit inside the function it names. That is why editing code can fail the suite on a docs file — re-anchor the affected entries in the same commit. A Python reference without an identifier-shaped symbol fails too, since a prose parenthetical would opt out of the only check that catches drift.

**Ideas / future work** are unprompted improvements with no specific defect behind them. No file:line.

---

## Open findings

Last triaged 2026-08-07, when three entries were swept and closed (see
Resolved). No entries are waiting on a manual check.

### engine/market

- [2026-07-05] [review] optimizer.py:247 (solve_optimal_roster) — positive-point pool smaller than remaining spots (or cheapest legal roster > budget) → MILP Infeasible → bid advice degrades to floor values. UI warning badge added in auction_control.html so it's no longer silent; actual short-roster planning (optimize the N players you CAN buy) still unbuilt — deferred, and **probably not worth building**: measured 2026-08-06, position slack on the live pool is F +333 / D +197 / G +53 against league-wide open needs, so the pool-too-small trigger is unreachable, and the budget-too-tight trigger is the commissioner-prevented case closed below. Left open only because a future pool could be thinner; re-measure before building anything


### frontend/UX

- [2026-08-06] [owner] static/vendor/tailwindcss-play-3.4.17.js — Tailwind's Play bundle JITs utility classes in the browser on every page load; a real build would ship a fraction of the CSS with no runtime cost — deferred: needs node + npm + the daisyui plugin and a rebuild on every template edit, and it is *riskier* here, because `static/shortcuts.js` builds class names at runtime (`'alert-' + type`) which a source-scanning build cannot see. That case survives today only because DaisyUI's prebuilt CSS carries every `alert-*` variant. Revisit only if page load becomes a real complaint
- [2026-08-07] [browser] main.py (undo) — `/undo` calls `_view_my_team()`, so undoing a *roster edit on an opponent* throws the panel back to BOT. Observed in Chrome: view SRL, bench a player, Ctrl+Z, and you are looking at your own team. This is the 2026-08-07 owner decision working as written (draft actions reset the view, and undo can revert an `/assign` where returning to BOT is right), but the decision was made about draft actions and undo is now the one endpoint that is *both* — it reverts whichever kind of action came last. Deferred: the fix is to reset only when the undone action was a draft action, which means `restore_snapshot` reporting what it undid, and that is a state-layer change to serve a view concern. Raise it only if it bites during a real audit
- [2026-08-06] [owner] main.py (_counterfactual) — the auto-shown counterfactual is computed at the MARKET price, not the live bid, so it does not sharpen as bidding climbs. That is what makes it cacheable: re-solving per $0.1M increment costs ~200ms and would put a response back inside the Assign mousedown/mouseup window. If it reads as stale in a real draft the follow-up is a manual "recompute at this price" button, never an automatic one — deferred pending draft-day experience

### code quality

- [2026-08-07] [grill] main.py:250 (lifespan) — if the `.corrupt` rename itself fails, the `except OSError` logs and carries on, and the next `_save_state` then rotates the unusable current file over the good backup — precisely the destruction the rename exists to prevent. Deferred: it needs a state dir that can be read but not written to (permissions, read-only mount, full disk), where saving the draft is already broken and the operator has a louder problem; the log names the file. Revisit only if the recovery ladder grows a second on-disk step
- [2026-08-06] [grill] main.py:587 (_context) — every endpoint builds the full context (~8.5ms, including a 704-row `bid_limits` list for the available-players table) regardless of how small a fragment it renders. `/bid-check`, `/nominate` and now `/explain?inline=1` reference a handful of its 16 keys and none touches `bid_limits`. `/explain` made this sharper on 2026-08-06: it fires on every bidder toggle and its warm response is ~9ms, essentially all of it this context build for a fragment that uses three keys. Pre-existing — the old whole-panel `auction_control.html` didn't use it either — but the 2026-08-06 panel split made fragments narrower and the waste correspondingly larger. Deferred: small next to the binary search over MILP solves that dominates `/bid-check`, and fixing it properly means a per-panel context builder, which is a cross-endpoint refactor
- [2026-08-07] [simplify] main.py — many endpoints repeat `capture_snapshot → try → except ValueError → _toast(str(e)) → commit_snapshot`; could be extracted to a shared helper or context manager — out of scope for behavioral changes. The *ordering* half of this was fixed 2026-08-07 (see Resolved); what is left is the boilerplate, which is now four lines rather than three and correspondingly more worth extracting
- [2026-08-07] [grill] tests/test_endpoints.py (TestPlayerChart) — `test_player_chart_valid` and `test_the_chart_body_carries_no_mount_id` both `GET /player-chart/Steven Stamkos`, a hard-coded name, against the CLAUDE.md rule. `/player-chart/<gone>` answers 200 with an empty state, so a refresh that drops him turns both into assertions about a page with no chart — `"Price Model" in r.text` would fail loudly, but `'id="player-chart-container"' not in r.text` would pass forever. Deferred: noticed while adding the unknown-player bid-check tests next door, unrelated to that change; the fix is a one-line derivation from the pool

### test infrastructure

- [2026-08-07] [refresh-drill] data_loader.py (_disambiguated_names) — the duplicate-name suffix is a workaround for a naming assumption, not a repair of it: the player NAME is still the primary key, so two players who share one are kept apart by a display string rather than by identity. A stable player id as the key would make the ambiguity structurally impossible and keep names clean on screen. Deferred by owner decision (2026-08-07), with the inventory recorded here so the follow-up does not have to rediscover it: `available_players`, `market_prices`/`model_prices`, `find_player`, ~20 endpoints taking a `player` form field, the transaction log, the trade dropdowns, and the `bo-<name>` DOM ids — plus `to_json`/`from_json`, so saved drafts and the undo chain need a migration. Large, and it touches the assign and bidding paths a live draft depends on
- [2026-07-05] [review] tests/ — coverage gaps. Mostly closed: trade guards, combo turn, endgame and live ceiling tested; all three assert-nothing tests fixed and mutation-checked 2026-08-07; corrupt-state startup fallback closed 2026-08-07 by `tests/test_crash_recovery.py`; `/trade-between` happy path and undo-after-{adjust-salary, toggle-bench, move-to-minors, move-to-roster, set-nominator} closed 2026-08-07 by `tests/test_trade_buyout_undo.py::TestUndoRevertsEveryRosterEdit`. **What is left is MILP-infeasible rendering** — awaiting triage, and note the engine finding above measures the trigger as currently unreachable, so this may be untestable without a synthetic pool

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

- ~~**Buyout Analyzer: Scan button + dropdown of my roster → select → "Execute Buyout".**~~ Closed 2026-08-06 as **already built** — `buyout_panel.html` has the Scan button plus a row of one-click per-player buttons (better than a dropdown: no open-then-select), the verdict block, and Execute Buyout. Nothing is typed. The entry described a flow that had already been replaced; the only real gap left there is the minors-have-no-dots finding under frontend/UX.
- **Decompose Model $ into its drivers — how much comes from projected points vs. NHL team quality** (and reputation/lag salary, which is the third big term). Needs a per-coefficient contribution breakdown out of `price_model.py`; the two-stage log-normal form means contributions are multiplicative on price, so decide whether to show them in log space or as "% of predicted price".
- **Save State button that jumps between live state and a scenario**, so testing a what-if doesn't cost the real draft state. Interacts with the scenario loader (`POST /load-scenario`) and the undo snapshot chain — check that switching can't strand a snapshot.

### Testing

- **More scenarios.** Extend the pre-baked set behind `POST /load-scenario` — the gaps worth covering are the ones that keep producing findings: last-goalie endgame, drained-budget late draft, a cap-rich team with a full roster (now a live bidder as of `4dc59da` — a scenario would let you see its effect on ceilings rather than trusting the unit tests), and bidding down to a single remaining bidder.

### Performance

- ~~**Interaction budget: every UI interaction < 500ms.**~~ Met. Measured 2026-08-06 on a fresh state (BOT 12 rostered, 704-player pool): warm `/bid-check` **9ms**, `/assign` **150ms**, `/nominate` **130ms**, `/undo` **127ms**, `GET /` **20ms**, `/explain` **215ms** cold and **9ms** warm once cached. Nothing is over budget, so the entry is closed on measurement rather than on more work. Two things stay true and are not defects: the *first* bid check on a new player is still ~1000ms (~10 MILP solves in the marginal — the lever there is a cheaper solve, e.g. pool pruning, not fewer solves), and `/trade-evaluate` is still unmeasured because it needs a built-up trade form. Where a regression would actually hurt, the guard is a solve count rather than wall-clock (`tests/test_bid_cache.py`, `tests/test_counterfactual_cache.py`) — timing assertions go flaky under load and the solve count is the cause anyway.

---

## Resolved

Everything that has been fixed, added or changed lives in
[CHANGELOG.md](CHANGELOG.md), newest first. It was 81% of this file and buried
the open items it shared it with.

Moving an entry there is what "done" means: delete it from **Open findings**
above and write it up under the date it landed, in the same commit as the fix.

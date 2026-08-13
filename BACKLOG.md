# Backlog

The single work list for this project: deferred review findings plus forward-looking ideas.

**Open findings** are things flagged by review agents (`/grill`, `/go`, `/simplify`, etc.) that were **not** addressed in the change that surfaced them. Format:

`- [YYYY-MM-DD] [source] file:line (symbol) — finding — reason deferred`

Name the enclosing function or property in `(symbol)`. Line numbers drift every time the file above them changes — on 2026-08-05 a third of the references in this file pointed at unrelated code — and a stale line sends you somewhere wrong without saying so. The symbol survives the drift and makes the entry greppable. Templates have no symbols, so those entries carry a line only.

`tests/test_backlog_refs.py` enforces this: every `file.py:line` here must resolve, and must sit inside the function it names. That is why editing code can fail the suite on a docs file — re-anchor the affected entries in the same commit. A Python reference without an identifier-shaped symbol fails too, since a prose parenthetical would opt out of the only check that catches drift.

**Ideas / future work** are unprompted improvements with no specific defect behind them. No file:line.

---

## Open findings

Last triaged 2026-08-13, walking every entry to pick the next piece of work. The
outcome is worth recording, because most of what is below is parked for a
**reason that has to expire before the entry is actionable**: the Proj heuristic
and the stale counterfactual both need a real draft to re-measure, the
short-roster MILP path is measured currently unreachable, the opponent-edit
exposure needs an actual accidental edit to justify a gate, and the stable
player-id refactor touches the assign and bidding paths. So a quiet backlog here
does not mean a healthy one — it means the cheap items are gone. Two new entries
were filed the same day from a second grill pass over the layout batch.

2026-08-11 closed the `main.py (undo)` view finding together with the
opponent-pick view swap from the testing-pass section below (see
[CHANGELOG.md](CHANGELOG.md)); 2026-08-07 swept three entries and a live testing
pass added the `[owner-testing]` entries — each of those was reproduced against
the running app before being written down, so they record a mechanism rather than
a symptom. No entries are waiting on a manual check.

**Two of the entries below carry a deferral reason that has already been
disproved once.** The 2026-08-11 pair were both deferred on a diagnosis that
turned out to be wrong — one claimed a `state.py` change was needed when the
information was already local to the endpoint, the other claimed `/assign`
needed an out-of-band response when it has always returned `all_panels.html` by
design. Re-check the mechanism before trusting "deferred because X" here; the
prose is a hypothesis, not a measurement, unless it says what was measured.

### engine/market

- [2026-08-08] [owner-testing] main.py:655 (_context) — **the League State "Proj" column is still computed two ways**, now labelled rather than reconciled. BOT's is `milp_solution.total_points`, a real MILP optimum under BOT's actual budget; every opponent's is `current + starter_slots × mean(points of the top slots×3 affordable players)`, a heuristic that deliberately costs no solve — 11 extra MILPs per action is what it avoids. The 2026-08-08 tooltip says so, which was the operator's question, and measurement supported labelling over changing it: BOT reads 1257 by MILP against 1262 by the opponents' rule on a fresh state, **5 points apart with the same rank either way**. Left open because the gap is not stable — the heuristic filters on per-player affordability (`market_price <= physical_max_bid`) and never checks that the team can afford the *whole set*, so a team with 10 spots and $10.5M counts every player under $6.0M as affordable and projects ten of them. **Re-measured 2026-08-13** against a real per-team MILP, using the new `endgame-ceiling-binds` scenario instead of waiting for a real draft — so the "re-measure late in a draft" condition is discharged. The heuristic is NOT systematically optimistic, which is what the entry assumed: on a fresh state it runs +62 mean and +193 worst (+14.2%, SRL), but among the still-drafting teams in the endgame it goes both ways — one team +146 (+11.3%), the other −72 (−5.5%). So a budget-aware greedy fill would not simply shave a known bias off, and the remaining error is not obviously worth 11 MILPs per action. What the re-measurement DID find was a separate and far larger bug in the same block — done teams were projected forward as if still drafting, +673 to +1101 points each — and that is fixed (see CHANGELOG 2026-08-13). Left open on the original narrow point only: the affordability filter still tests players one at a time and never asks whether the team can afford the whole set. Re-measure again once a real draft's late budgets are available, since the scenario's are synthetic
- [2026-07-05] [review] optimizer.py:247 (solve_optimal_roster) — positive-point pool smaller than remaining spots (or cheapest legal roster > budget) → MILP Infeasible → bid advice degrades to floor values. UI warning badge added in auction_control.html so it's no longer silent; actual short-roster planning (optimize the N players you CAN buy) still unbuilt — deferred, and **probably not worth building**: measured 2026-08-06, position slack on the live pool is F +333 / D +197 / G +53 against league-wide open needs, so the pool-too-small trigger is unreachable, and the budget-too-tight trigger is the commissioner-prevented case closed below. Left open only because a future pool could be thinner; re-measure before building anything


### frontend/UX

- [2026-08-13] [grill] templates/partials/league_state.html:8 — **the three `.table-scroll-x` regions cannot be scrolled by keyboard** (no `tabindex`, so they are not focusable; WCAG 2.1.1). Introduced 2026-08-11 with the grid fix, which made the League State and roster tables scroll inside their own panels rather than paint across the next one — so their right-hand columns are now reachable only with a pointer or a trackpad gesture. Deferred deliberately rather than overlooked: `tabindex="0"` on three wrappers adds three tab stops to the panels you tab through while a bid is live, and the draft is a single operator on a mouse. The content is not lost, it is one drag away. Revisit if the draft is ever run from the keyboard, or if a screen reader is ever in play — at which point the fix is `tabindex="0"` plus `role="region"` and an `aria-label` naming the table, not tabindex alone
- [2026-08-11] [grill] templates/partials/team_panel.html:120 — **an opponent's pick now auto-presents their EDITABLE panel, which used to require a deliberate click.** The roster-edit forms are not gated on `is_my_team` by design (auditing a rival is the point), but before 2026-08-11 `/assign` always came home to BOT, so a rival's Bench / `$` / ↓ Minors / Recall controls only appeared when you asked for them. Now a sale puts them on screen at the highest-tempo moment of the draft. Measured, and this is why it is filed rather than fixed: the salary box is `hx-trigger="change"` (`team_panel.html:132`), so it needs a typed value plus a blur, and every other control is a discrete small button — a stray click cannot fire one. No test or gate added: gating them on `is_my_team` would remove the working feature the 2026-08-07 view work exists to provide. Revisit only if a real draft produces an accidental edit; the fix would be a confirm on opponent edits, not a gate
- [2026-08-11] [grill] main.py:1293 (load_scenario) — **`/load-scenario`'s and `/buyout`'s view resets have no test in either direction.** Both call `_view_team(MY_TEAM)` and both are pre-existing behaviour that 2026-08-11 preserved rather than changed, so this is a gap the change inherited, not one it opened — `/reset`'s equivalent IS covered (`test_reset_returns_the_view_to_my_team`) and `/buyout`'s reset is covered on the *undo* side only (`test_undoing_a_buyout_shows_my_team`). Deleting either call leaves the suite green. Deferred as low-value: `MY_TEAM` is always a live code (`_context` does `teams[MY_TEAM]` unconditionally), so the new validation guard cannot make these silently no-op, and the failure mode is a panel showing the wrong team after an operation that replaces the world anyway. Two one-line tests next to the existing `/reset` one if anyone is in there
- [2026-08-08] [review] main.py:101 (_backfill_keeper_flags) — **the backfill repairs the live state but not the undo chain**, so after booting a pre-`is_keeper` save file, undoing back past everything done this session restores minors with no provenance and the next recall of one colours him as a purchase again. `AuctionState._snapshots` is a list of whole JSON documents rather than of dicts, so repairing them from `main.py` means hard-coding a second copy of the state's JSON key names — a wrong key would silently do nothing, which is worse than the bug. Deferred as narrow and cosmetic: it needs a legacy file, an undo past the whole session, and it costs a row colour. If it ever matters, the fix belongs in `state.py` as a `from_json` hook, not here
- [2026-08-08] [grill] templates/partials/trade_panel.html:111 — **neither JS-built "(M)" marker has an automated test**, in the Trade Evaluator's "I Receive" (here) or the Trade Between Teams form's "Receives" (`static/shortcuts.js`, `loadTradePartner`). Both halves are assembled client-side from `/team-players`, so the marker exists only in the DOM; the endpoint tests cover the JSON carrying `is_minor` and both Jinja "gives" lists, but deleting either `p.is_minor ? ' (M)' : ''` leaves the suite green. Both verified in Chrome 2026-08-08 (SRL → 23 options, 18 marked, matching the roster). Deferred because a browser case for a two-character suffix is a poor trade against the ~100x slower harness, and the failure mode is a label reading wrong rather than a wrong trade — but note the grill found the *endpoint* half of this exact blind spot was a real bug, so fold both into the next browser test that opens either form
- [2026-08-08] [grill] templates/partials/bid_limits.html:41 — **8 of the 20 `data-tip` tooltips are never placement-checked**, so the 2026-08-08 CSS block's guarantee is narrower than it reads. `TestTooltipsStayInsideTheirPanel` measures whatever the page renders in one state (fresh reset + live bid) and that is ~12: the five `stop_status` branches are mutually exclusive so only one is ever on screen, the Penalty tile needs `penalties > 0`, and this line — the only `tooltip-left` in the app — renders only when the market ceiling caps a model price, which never happens on a fresh state because every team starts at `MAX_SALARY`. **Not a regression risk from that change**: the global rule is `max-width`, which can only make a bubble narrower and therefore reduce horizontal overflow. The one real exposure is vertical — narrower means taller, and this tooltip is the only one living inside a `.scroll-container` (`overflow-y: auto`, `templates/partials/bid_limits.html:14`), which clips. **Partly closed 2026-08-13**: `POST /load-scenario` grew `endgame-ceiling-binds`, and `TestTooltipsStayInsideTheirPanel` now runs against it at 375/1024/1280 with the capped tip required BY NAME, so the `tooltip-left` is placement-checked on the horizontal axis for the first time and passes. The **vertical** exposure this entry predicted is real but bounded, and was measured rather than asserted: the bubble is 99px tall against ~64px rows, so on the last row visible inside the 405px `.scroll-container` it overhangs the bottom edge by **~25px** — and scrolling one row cures it, which is why no assertion was added (a naive check flags every row below the fold as clipped, since an unscrolled row is trivially outside the client box). Still open for the remaining tips: the five `stop_status` branches are mutually exclusive and the Penalty tile needs `penalties > 0`, so ~4 are still never measured. Deferred: each needs its own page state for a cosmetic property
- [2026-08-06] [owner] static/vendor/tailwindcss-play-3.4.17.js — Tailwind's Play bundle JITs utility classes in the browser on every page load; a real build would ship a fraction of the CSS with no runtime cost — deferred: needs node + npm + the daisyui plugin and a rebuild on every template edit, and it is *riskier* here, because `static/shortcuts.js` builds class names at runtime (`'alert-' + type`) which a source-scanning build cannot see. That case survives today only because DaisyUI's prebuilt CSS carries every `alert-*` variant. Revisit only if page load becomes a real complaint
- [2026-08-06] [owner] main.py (_counterfactual) — the auto-shown counterfactual is computed at the MARKET price, not the live bid, so it does not sharpen as bidding climbs. That is what makes it cacheable: re-solving per $0.1M increment costs ~200ms and would put a response back inside the Assign mousedown/mouseup window. If it reads as stale in a real draft the follow-up is a manual "recompute at this price" button, never an automatic one — deferred pending draft-day experience

### code quality

- [2026-08-07] [grill] main.py:288 (lifespan) — if the `.corrupt` rename itself fails, the `except OSError` logs and carries on, and the next `_save_state` then rotates the unusable current file over the good backup — precisely the destruction the rename exists to prevent. Deferred: it needs a state dir that can be read but not written to (permissions, read-only mount, full disk), where saving the draft is already broken and the operator has a louder problem; the log names the file. Revisit only if the recovery ladder grows a second on-disk step
- [2026-08-06] [grill] main.py:655 (_context) — every endpoint builds the full context (~8.5ms, including a 704-row `bid_limits` list for the available-players table) regardless of how small a fragment it renders. `/bid-check`, `/nominate` and now `/explain?inline=1` reference a handful of its 16 keys and none touches `bid_limits`. `/explain` made this sharper on 2026-08-06: it fires on every bidder toggle and its warm response is ~9ms, essentially all of it this context build for a fragment that uses three keys. Pre-existing — the old whole-panel `auction_control.html` didn't use it either — but the 2026-08-06 panel split made fragments narrower and the waste correspondingly larger. Deferred: small next to the binary search over MILP solves that dominates `/bid-check`, and fixing it properly means a per-panel context builder, which is a cross-endpoint refactor
- [2026-08-07] [simplify] main.py — many endpoints repeat `capture_snapshot → try → except ValueError → _toast(str(e)) → commit_snapshot`; could be extracted to a shared helper or context manager — out of scope for behavioral changes. The *ordering* half of this was fixed 2026-08-07 (see Resolved); what is left is the boilerplate, which is now four lines rather than three and correspondingly more worth extracting
- [2026-08-07] [grill] tests/test_endpoints.py (TestPlayerChart) — `test_player_chart_valid` and `test_the_chart_body_carries_no_mount_id` both `GET /player-chart/Steven Stamkos`, a hard-coded name, against the CLAUDE.md rule. `/player-chart/<gone>` answers 200 with an empty state, so a refresh that drops him turns both into assertions about a page with no chart — `"Price Model" in r.text` would fail loudly, but `'id="player-chart-container"' not in r.text` would pass forever. Deferred: noticed while adding the unknown-player bid-check tests next door, unrelated to that change; the fix is a one-line derivation from the pool

### test infrastructure

- [2026-08-13] [grill] tests/measure_layout.py:174 (report / min_contents) — **a stale selector in `TARGETS` is indistinguishable from an element that legitimately does not render.** `report` prints `(absent)` for both, and `min_contents` skips `None` with no line at all — on min-content, the number a layout investigation actually turns on. This is not hypothetical: `#league-state > table` went stale the moment the 2026-08-11 fix wrapped that table, so for two days the instrument silently could not see the 955px element it was written to find (repaired 2026-08-13 by matching descendants, and `--whatif` now injects the bug so the failure is reproducible). Deferred because the only real check is "every selector resolves", which needs a Chrome launch for a dev-only instrument pytest deliberately does not collect — and a test that must be run by hand is the same trust problem one level up. Cheaper mitigation if it bites again: print `(NO MATCH)` versus `(not rendered)` by testing the selector against `document` before the element lookup
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

- ~~**Buyout Analyzer: Scan button + dropdown of my roster → select → "Execute Buyout".**~~ Closed 2026-08-06 as **already built** — `buyout_panel.html` has the Scan button plus a row of one-click per-player buttons (better than a dropdown: no open-then-select), the verdict block, and Execute Buyout. Nothing is typed. The entry described a flow that had already been replaced; the only real gap left there is the minors-have-no-dots finding under frontend/UX. **The dropdown half was reopened and decided the other way on 2026-08-08** — see the testing-pass section below. Don't read the parenthetical above as still standing: it was right about the interaction and wrong about the length.
- **Decompose Model $ into its drivers — how much comes from projected points vs. NHL team quality** (and reputation/lag salary, which is the third big term). Needs a per-coefficient contribution breakdown out of `price_model.py`; the two-stage log-normal form means contributions are multiplicative on price, so decide whether to show them in log space or as "% of predicted price".
- **Save State button that jumps between live state and a scenario**, so testing a what-if doesn't cost the real draft state. Interacts with the scenario loader (`POST /load-scenario`) and the undo snapshot chain — check that switching can't strand a snapshot.

### From the 2026-08-07 testing pass

Cockpit ergonomics from a live run-through, source tag `[owner-testing]`. These
are wants, not defects — the things that were actually *broken* are under
**Open findings** above, and they should be fixed first. No `file:line` here,
because nothing is wrong at one; the file names are orientation only. (This said
"the five things" until 2026-08-11; a count in prose goes stale on the first
entry that closes, and one had already closed by then.)

**Nomination panel**

- **Show the model price beside the market price.** Today's "Expected" figure is
  `market_prices`, deliberately — drain nominations rank on the money that
  actually leaves a rival's budget, and the model/market gap is already the
  tie-break (see `.claude/rules/pricing-pipeline.md`). So this is an *additional*
  column, not a correction: the two side by side show at a glance who is cheap
  because the market is thin versus cheap because the model rates him low.
- **Hide the recommendations once "Bid on X" is clicked.** They are stale the
  moment the auction starts, and they stay on screen competing with the bid
  panel for attention.

**Buyout Analyzer**

- **A list, not a row of ~15 buttons.** Owner decision 2026-08-08, and it
  supersedes the 2026-08-06 judgment recorded in the UI/UX section above, which
  closed this as already-built on the grounds that buttons beat a dropdown (no
  open-then-select). What changed is the count: the candidate set is
  `all_players|selectattr('can_be_bought_out')`, so it grows with the roster and
  keeps growing all draft — the button row already wraps, and open-then-select
  costs less than hunting a name in a wrapped block. The list has to keep reading
  that same expression, per CLAUDE.md — the scan, the dots and this list are
  deliberately one expression, and a `roster_players` copy is the 2026-08-07 bug
  that silently hid 11 of BOT's 15 eligible players. Two constraints that come
  with it: the buyout **dots** are per-player OOB swap targets keyed by
  `main._dom_id`, so if the list collapses to a `<select>` the dots need somewhere
  to live (per-option text, or the list stays expanded and only the *actions*
  collapse); and it stays BOT-only, since `_recompute_buyout_indicators` scores
  against BOT's MILP total.

**Logs**

- **Split into three tabs: Auction, Transaction, Change.** Auction = draft
  picks; Transaction = trades and buyouts; Change = roster edits (bench,
  salary corrections, minors moves). This is a filter over `transaction_log`'s
  existing type tags, not new plumbing — check the tags actually partition
  cleanly before building the tabs.
- **Make the team name in a log row clickable**, opening that team's panel
  (`GET /team-view/{code}`) — the log is where you notice a rival's pick, and
  the panel is two clicks away.
- **NHL team logos in both logs.** Same asset path the roster tables use.

**League State table**

- **Three-letter codes only.** Full names wrap and cost the column width the
  numbers need.
- **Render Done as an X**, not the current text — it is a binary flag in a
  dense table.

**Available Players**

- **RFA filter.** Rows already carry the data (`is-rfa`), and this extends the
  existing `filterPosition()` / `data-position` pattern rather than adding a new
  one.

**Bid panel**

- **Drop the `$` submit button.** `team_panel.html` already documents it as a
  fallback for the auto-submit; if the auto-submit is trusted, the button is
  noise in the busiest panel on screen. Confirm the auto-submit fires on every
  input path first — that is what the fallback was for.
- **Tooltip on Sigma**, explaining that it is the spread of the price
  distribution (a function of the predicted log-price, not of points) and that a
  wide sigma means the model is unsure rather than that the player is expensive.
- **Tooltip on marginal value vs. price**, explaining that marginal value is
  what this player adds to the *optimal roster* — so it can sit below the model
  price on a good player the roster does not need, and that is the tool working.

**Trade form**

- **Bigger, clearer multi-selects.** Both sides are cramped and the multi-select
  affordance is not obvious; this is the panel most likely to be used under time
  pressure during a break.

**Counterfactual**

- **Close button.** Copy `player_chart.html`'s `this.closest('.the-card').remove()`
  — *not* `getElementById`, per the CLAUDE.md rule about partials mounted in two
  places.

### Testing

- **More scenarios.** Extend the pre-baked set behind `POST /load-scenario`. `endgame-ceiling-binds` landed 2026-08-13 (most teams done, ceiling binding at ~$2.8M, stars unsold) and immediately paid for itself twice — it placement-checked the `tooltip-left` that no fresh state can render, and it surfaced the done-team projection bug. Still worth covering: last-goalie endgame, drained-budget late draft, a cap-rich team with a full roster (now a live bidder as of `4dc59da` — a scenario would let you see its effect on ceilings rather than trusting the unit tests), and bidding down to a single remaining bidder.

### Performance

- ~~**Interaction budget: every UI interaction < 500ms.**~~ Met. Measured 2026-08-06 on a fresh state (BOT 12 rostered, 704-player pool): warm `/bid-check` **9ms**, `/assign` **150ms**, `/nominate` **130ms**, `/undo` **127ms**, `GET /` **20ms**, `/explain` **215ms** cold and **9ms** warm once cached. Nothing is over budget, so the entry is closed on measurement rather than on more work. Two things stay true and are not defects: the *first* bid check on a new player is still ~1000ms (~10 MILP solves in the marginal — the lever there is a cheaper solve, e.g. pool pruning, not fewer solves), and `/trade-evaluate` is still unmeasured because it needs a built-up trade form. Where a regression would actually hurt, the guard is a solve count rather than wall-clock (`tests/test_bid_cache.py`, `tests/test_counterfactual_cache.py`) — timing assertions go flaky under load and the solve count is the cause anyway.

---

## Resolved

Everything that has been fixed, added or changed lives in
[CHANGELOG.md](CHANGELOG.md), newest first. It was 81% of this file and buried
the open items it shared it with.

Moving an entry there is what "done" means: delete it from **Open findings**
above and write it up under the date it landed, in the same commit as the fix.

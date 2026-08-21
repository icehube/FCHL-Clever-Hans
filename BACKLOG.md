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

- [2026-08-16] [investigation] market.py:43 (compute_market_ceiling) — **the planning ceiling is second-highest-of-ten, and two rich teams pin it at `MAX_SALARY` for as long as they stay rich.** Open design question, **not a correctness bug**: `min(model_price, market_ceiling)` is never *wrong*, it is only sometimes inert. Measured 2026-08-16 with `tests/measure_ceiling.py` over a full 165-pick auction, and the two spending models are far apart — buyers paying the tool's own market price never bind it (0/165 picks, 18% of the league cap unspent, three teams — JHN $19.8M, GVR $14.1M, VPP $12.0M — finishing above the line the rule needs two of), buyers paying what the reserve rule allows bind it on 133/165, stepping `11.4M@1 -> 7.3M@33 -> 4.5M@41 -> 0.5M@44` (1-based ordinals; the instrument printed 0-based indices until 2026-08-17). So the layer works as designed and the question is empirical: a real draft's spending decides whether Layer 2 contributes anything to *planning*, and if it lands near the model-price end, a demand-aware price (how many teams need the position, how much money is chasing this tier) would do more than a ceiling nobody reaches. Deferred because changing how planning prices are derived moves every bid recommendation in the tool and there is a draft coming. **The blocker is no longer collection** — `TransactionRecord` has logged `model_price` and `market_price` on every pick all along, and `tests/measure_spend.py` (2026-08-17) reads them back, so the condition on this entry is now "run the reader after the draft", not "find a way to get the numbers". Note the reader measures the sharper quantity: `market_price < model_price` (the ceiling changed a planning price) rather than `ceiling < MAX_SALARY`. On the drain run those are 122 and 133, and the 122 all start at pick 44 when the ceiling hit the floor — the intermediate steps capped nothing. The threshold is pinned meanwhile by `tests/test_market.py::TestWhenTheCeilingLeavesTheCap`. Note this is the **idle** ceiling only; the live one the advisor uses is below `MAX_SALARY` in 7/10 single-rival matchups by mid-draft and needs nothing. As of 2026-08-18 the mid-range case is also **loadable**: `scenarios.load("drained-late-draft")` puts the idle ceiling at $3.3M with 25 of 597 pool prices capped, so the question of what the layer contributes to planning can be looked at on screen rather than only in an instrument — the entry stays open because the condition is still a real draft's spending
- [2026-07-05] [review] optimizer.py:247 (solve_optimal_roster) — positive-point pool smaller than remaining spots (or cheapest legal roster > budget) → MILP Infeasible → bid advice degrades to floor values. UI warning badge added in `templates/partials/bid_panel.html:15 (milp.status != "Optimal")` so it's no longer silent, and pinned in both directions 2026-08-13 by `tests/test_endpoints.py::TestRenderingWhenTheOptimizerFails`; actual short-roster planning (optimize the N players you CAN buy) still unbuilt — deferred, and **probably not worth building**: measured 2026-08-06, position slack on the live pool is F +333 / D +197 / G +53 against league-wide open needs, so the pool-too-small trigger is unreachable, and the budget-too-tight trigger is unreachable through **bidding** (the commissioner-prevented case, closed 2026-08-06 — see `CHANGELOG.md`) though NOT through play: buyout penalties, `/trade-between` and `/adjust-salary` all raise cap load and warn rather than refuse, and $20.5M of penalties on a fresh BOT reaches it (measured 2026-08-13). Left open only because a future pool could be thinner; re-measure before building anything — and note 2026-08-20 measured the **adjacent** idea, shrinking the pool handed to a solve that is otherwise fine, and found it silently wrong once BOT's budget per open spot drops toward the reserve floor (see the `main.py (bid_check)` entry). That is the same regime this entry is about, so a short-roster path has to be exact rather than a heuristic over "the N players you CAN buy"


### frontend/UX

- [2026-08-17] [review] templates/partials/team_panel.html:143 (text-info opacity-50 italic) — **nothing on screen says what an italic blue roster row means.** MILP target rows are merged into the same table as owned players and distinguished only by styling (`text-info opacity-50 italic`) plus the absence of an Actions cell; there is no legend, no header, and no per-row marker. So the panel's most valuable output — "these are the players to buy" — is conveyed entirely by a colour and a slant that the operator has to already know. Noticed while fixing the failed-MILP empty state on 2026-08-17 (the *absence* of these rows is now explained; their *presence* still is not). Deferred as a real design question rather than a quick label: the obvious fixes each cost something — a legend line adds permanent chrome to a panel already tight at 1280px, a `Target` badge per row widens the table that only just stopped overflowing its column (see `.table-scroll-x`), and a separate table loses the position-sorted comparison against what you already own, which is the point of merging them. Decide it deliberately, ideally against a real draft's experience of whether it actually confuses
- [2026-08-20] [audit] templates/partials/bid_limits.html:47 (for p in bid_limits) — **a filter combination with no matches renders the pool table's headers and nothing else**, with no row saying so. Reachable in a real draft: G + RFA late on, once the last restricted goalie sells. Flagged during the 2026-08-16 grill of `c14eb59` and **deliberately not filed** — "I'd rather you see it in use first and tell me whether it's actually confusing" — which is a legitimate deferral reason but the wrong place to keep it, since the question never reached you. Filed now by the 2026-08-20 audit of all 31 grill rounds. Confirmed still true: no empty-state row in the template, and `syncFilterButtons` in `static/shortcuts.js` counts nothing, so nothing knows the table is empty. Mitigating context, and why this stays deferred rather than becoming a quick fix: the filter buttons do show which filter is active, so the state is explicable rather than mysterious. The fix wants a visible-row count in the filter JS plus a `<tr>` the count toggles — not a Jinja `{% else %}`, because the filtering is entirely client-side and `bid_limits` is never empty server-side. Decide it against a real draft's experience
- [2026-08-13] [grill] templates/partials/league_state.html:43 (table-scroll-x) — **the three `.table-scroll-x` regions cannot be scrolled by keyboard** (no `tabindex`, so they are not focusable; WCAG 2.1.1). Introduced 2026-08-11 with the grid fix, which made the League State and roster tables scroll inside their own panels rather than paint across the next one — so their right-hand columns are now reachable only with a pointer or a trackpad gesture. Deferred deliberately rather than overlooked: `tabindex="0"` on three wrappers adds three tab stops to the panels you tab through while a bid is live, and the draft is a single operator on a mouse. The content is not lost, it is one drag away. Revisit if the draft is ever run from the keyboard, or if a screen reader is ever in play — at which point the fix is `tabindex="0"` plus `role="region"` and an `aria-label` naming the table, not tabindex alone
- [2026-08-11] [grill] templates/partials/team_panel.html:152 (not p.is_target and p.can_be_bought_out) — **an opponent's pick now auto-presents their EDITABLE panel, which used to require a deliberate click.** The roster-edit forms are not gated on `is_my_team` by design (auditing a rival is the point), but before 2026-08-11 `/assign` always came home to BOT, so a rival's Bench / `$` / ↓ Minors / Recall controls only appeared when you asked for them. Now a sale puts them on screen at the highest-tempo moment of the draft. Measured, and this is why it is filed rather than fixed: the salary box is `templates/partials/team_panel.html:188 (hx-trigger="change")`, so it needs a typed value plus a blur, and every other control is a discrete small button — a stray click cannot fire one. No test or gate added: gating them on `is_my_team` would remove the working feature the 2026-08-07 view work exists to provide. Revisit only if a real draft produces an accidental edit; the fix would be a confirm on opponent edits, not a gate
- [2026-08-08] [review] main.py:116 (_backfill_keeper_flags) — **the backfill repairs the live state but not the undo chain**, so after booting a pre-`is_keeper` save file, undoing back past everything done this session restores minors with no provenance and the next recall of one colours him as a purchase again. `AuctionState._snapshots` is a list of whole JSON documents rather than of dicts, so repairing them from `main.py` means hard-coding a second copy of the state's JSON key names — a wrong key would silently do nothing, which is worse than the bug. Deferred as narrow and cosmetic: it needs a legacy file, an undo past the whole session, and it costs a row colour. If it ever matters, the fix belongs in `state.py` as a `from_json` hook, not here
- [2026-08-08] [grill] templates/partials/bid_limits.html:56 (tooltip-left) — **8 of the 20 `data-tip` tooltips are never placement-checked**, so the 2026-08-08 CSS block's guarantee is narrower than it reads. `TestTooltipsStayInsideTheirPanel` measures whatever the page renders in one state (fresh reset + live bid) and that is ~12: the five `stop_status` branches are mutually exclusive so only one is ever on screen, the Penalty tile needs `penalties > 0`, and this line — the only `tooltip-left` in the app — renders only when the market ceiling caps a model price, which never happens on a fresh state because every team starts at `MAX_SALARY`. **Not a regression risk from that change**: the global rule is `max-width`, which can only make a bubble narrower and therefore reduce horizontal overflow. The one real exposure is vertical — narrower means taller, and this tooltip is the only one living inside a `.scroll-container` with `overflow-y: auto` — `templates/partials/bid_limits.html:29 (scroll-container)` — which clips. **Partly closed 2026-08-13**: `POST /load-scenario` grew `endgame-ceiling-binds`, and `TestTooltipsStayInsideTheirPanel` now runs against it at 375/1024/1280 with the capped tip required BY NAME, so the `tooltip-left` is placement-checked on the horizontal axis for the first time and passes. The **vertical** exposure this entry predicted is real but bounded, and was measured rather than asserted: the bubble is 99px tall against ~64px rows, so on the last row visible inside the 405px `.scroll-container` it overhangs the bottom edge by **~25px** — and scrolling one row cures it, which is why no assertion was added (a naive check flags every row below the fold as clipped, since an unscrolled row is trivially outside the client box). Still open for the remaining tips: the five `stop_status` branches are mutually exclusive and the Penalty tile needs `penalties > 0`, so ~4 are still never measured. Deferred: each needs its own page state for a cosmetic property
- [2026-08-06] [owner] static/vendor/tailwindcss-play-3.4.17.js — Tailwind's Play bundle JITs utility classes in the browser on every page load; a real build would ship a fraction of the CSS with no runtime cost — deferred: needs node + npm + the daisyui plugin and a rebuild on every template edit, and it is *riskier* here, because `static/shortcuts.js` builds class names at runtime (`'alert-' + type`) which a source-scanning build cannot see. That case survives today only because DaisyUI's prebuilt CSS carries every `alert-*` variant. Revisit only if page load becomes a real complaint
- [2026-08-06] [owner] main.py (_counterfactual) — the auto-shown counterfactual is computed at the MARKET price, not the live bid, so it does not sharpen as bidding climbs. That is what makes it cacheable: re-solving per $0.1M increment costs ~200ms and would put a response back inside the Assign mousedown/mouseup window. If it reads as stale in a real draft the follow-up is a manual "recompute at this price" button, never an automatic one — deferred pending draft-day experience

### code quality

- [2026-08-07] [grill] main.py:319 (lifespan) — if the `.corrupt` rename itself fails, the `except OSError` logs and carries on, and the next `_save_state` then rotates the unusable current file over the good backup — precisely the destruction the rename exists to prevent. Deferred: it needs a state dir that can be read but not written to (permissions, read-only mount, full disk), where saving the draft is already broken and the operator has a louder problem; the log names the file. Revisit only if the recovery ladder grows a second on-disk step
- [2026-08-20] [audit] main.py:257 (_warn_at_startup) — **the startup banner is assembled by string concatenation, so two warning sources render as run-together sentences in one strip.** Flagged during the 2026-08-07 grill of `bed70c9` ("if you ever add a third warning source it's worth making that a list") and **never filed** — found by the 2026-08-20 audit of all 31 grill rounds, which is the only reason it is written down now. The docstring has since grown a paragraph defending *accumulate rather than overwrite*, which is the correct half of the decision and not the half that was flagged: there are exactly two sources today (a skipped backfill and a fallback to `.backup`) and a boot hitting both is the recovery case the banner exists for. Cosmetic, and deferred because the fix is not the one-liner it looks like — `_startup_warning` would become a list, which touches `_context`, `base.html`'s banner markup and `POST /reset`'s clear, and the two-banner split (`#startup-warning` vs `#data-warning`) means there are two renderers to keep honest. Worth doing **when a third warning source is added**, not before; that is the trigger the original finding named
- [2026-08-19] [grill] main.py:1194 (bid_check) — **`/bid-check` and `/explain` are still `async def` wrapped around synchronous MILP work, so a cold one holds the event loop for its whole duration.** *Partly closed 2026-08-19*: the two multi-solve SCANS now hand their solve loops to a worker thread and publish only if the state has not moved underneath (see `CHANGELOG.md`), which took a warm `/bid-check` during a roster scan from **1682ms back to 3ms** and `/state` from 1564ms to 12ms. What is left is the two endpoints on the bidding path itself — measured 2026-08-19, a **cold** `/bid-check` is 935ms (a binary search over MILP solves) and `/explain` 165ms — and neither is the same cheap change. (1) `/explain`'s serialisation is load-bearing: `tests/test_counterfactual_cache.py::TestResponsesCannotOvertakeEachOther` exists because FIFO ordering is the only thing stopping a late counterfactual landing in a mount that now shows a different player, and its docstring already names the prerequisite — `hx-sync="#app:replace"` on the mount in `bid_panel.html` — so this needs a template change and a browser check rather than a keyword. (2) The remaining stall is self-inflicted rather than cross-request: the operator is one person, and the request that queues behind a cold `/bid-check` is their own next keystroke on the same player, which the marginal cache then answers in 9ms. Revisit if a draft-day stall on the FIRST bid of a player is actually felt; the lever there is a cheaper solve — and **pool pruning, the one this entry used to name, is measured unsafe.** Cold `/bid-check` is 988–1030ms across 10 solves, so the solve really is the whole cost. (This used to say **98–99% inside CBC**; re-measured 2026-08-21 it is **89.8%** — see below. The conclusion the figure was supporting still holds, which is why it was never caught.) Keeping the top 50 by points per position (705 → 150) gives a **byte-identical answer on all 7 pinned states** (the six in `scenarios.SCENARIOS` plus the fresh pool — "7 scenarios" was loose, there are 6) and a 2–3.7x faster solve, which is exactly the trap: every scenario sits at $1.9M+ of BOT budget per open spot. Squeeze the budget toward the reserve floor and it goes **silently wrong** — at $1.00M/spot it returns 1069 against a true 1076, status still `Optimal`; at $0.70M and $0.60M it returns **Infeasible** where the true answers are 999 and 961. Adding "plus the K cheapest per position" does not rescue it (912 against 999), because which players matter depends on the budget *interaction*, not on points or price separately. A wrong `Optimal` wearing a confident number is the `keepFiles=True` failure class `TestTwoSolvesAtOnceAgreeWithTwoSolvesInARow` exists for, and the tight-budget regime is reachable **through play** — buyout penalties, `/trade-between` and `/adjust-salary` all warn rather than refuse, as the `optimizer.py (solve_optimal_roster)` entry above records ($20.5M of penalties on a fresh BOT reaches it). **Measured 2026-08-21, and the three surviving candidates now have numbers** — `tests/measure_marginal.py`, which stays as the harness. Two of this entry's own figures were wrong: it is **89.8% inside CBC**, not 98-99% (the rest is a flat ~9.2ms per solve of model build and extraction, paid ten times over for ten models that differ in one number), and the solve count is bimodal rather than ~10, since a floor player short-circuits after two and a must-have after three. On the candidates: model reuse alone is **1.06x**, reuse plus `warmStart=True` **1.14x**, and the best is one the entry did not name — the probe search is a sequence over a single budget RHS, so it has a single-solve dual, "cheapest roster containing him that still beats the without-him total", giving **1.55-1.60x on the big-pool states, 1.45x overall, and 0.79x on `endgame-sole-bidder`** where the reference already short-circuits. Worst single subject 1511ms → 772ms. All three reproduce the reference byte-for-byte on **168 subjects** (28 scenario + 140 swept to $0.60M/spot). **Not shipped, deliberately**: 1.6x on the slow cases does not buy a second MILP formulation plus a confirm loop plus two float-epsilon subtleties on the hottest path in the app, each of which took a wrong draft to find — one an increment high from smoothing solver tolerance, the other an increment low from `1.9 / 0.1 == 18.999999999999996`, and both invisible on the scenario set. `warmStart` is at least cleared as safe: its `.mst` comes from the same `create_tmp_files` call as the `.lp`, so it carries the per-solve `uuid4().hex` and is nothing like `keepFiles=True`. Trigger unchanged — a draft-day stall on the first bid actually felt — but the answer is now costed rather than open, and re-running is `--sweep`
- [2026-08-06] [grill] main.py:912 (_context) — every endpoint builds the full context (~8.5ms, including a 704-row `bid_limits` list for the available-players table) regardless of how small a fragment it renders. `/bid-check`, `/nominate` and now `/explain?inline=1` reference a handful of its 16 keys and none touches `bid_limits`. `/explain` made this sharper on 2026-08-06: it fires on every bidder toggle and its warm response is ~9ms, essentially all of it this context build for a fragment that uses three keys. Pre-existing — the old whole-panel `auction_control.html` didn't use it either — but the 2026-08-06 panel split made fragments narrower and the waste correspondingly larger. Deferred: small next to the binary search over MILP solves that dominates `/bid-check`, and fixing it properly means a per-panel context builder, which is a cross-endpoint refactor
- [2026-08-20] [review] scenarios.py:253 (_scenario_endgame_ceiling_binds) — **an inline copy of `_reserved_top`**: `set(sorted(price, key=lambda n: (-price[n], n))[:25])`, character for character what `_reserved_top(price)` returns with its default `count=25`. Found while measuring the tie-breaks for the cross-process determinism test — both sites were mutated separately and behave identically, and their comments already cross-reference each other (`_reserved_top`'s docstring cites this scenario by name for the 25-rather-than-40 reasoning). Deferred rather than folded in on the spot: the plan for that work said `scenarios.py` stays untouched unless the measurement found a real gap, and a dedup is not one. One-line fix when someone is next in the file, verifiable by digesting the six scenarios before and after


### test infrastructure

- [2026-08-17] [review] tests/test_browser_ui.py:401 (test_an_over_cap_adjust_salary_toast_renders_and_dismisses) — **flakes under full-suite load with `Page.evaluate: Resulting promise was garbage collected`**, a Playwright teardown error rather than an assertion failure. Observed once in a 777-test run 2026-08-17; the same test passes 3/3 in isolation and the whole browser file passes 35/35 on its own (98s), so it is contention, not a regression — nothing in that commit touched the browser suite. The mechanism fits the test: it fires `htmx.ajax` from the page and then evaluates against the resulting toast, so a slow response under load can outlive the evaluate's promise. Deferred rather than papered over with a retry: a `flaky`/rerun decorator would hide a real regression in the one suite that checks things `TestClient` physically cannot, and CLAUDE.md already names `-m "not browser"` as the draft-day escape hatch. Worth a fix only if it recurs — at which point the shape is awaiting the specific toast element before evaluating, not a blanket rerun. Note the existing closed interaction-budget entry covers a *different* problem (wall-clock assertions going flaky under load); this one has no timing assertion at all
- [2026-08-07] [refresh-drill] data_loader.py (_disambiguated_names) — the duplicate-name suffix is a workaround for a naming assumption, not a repair of it: the player NAME is still the primary key, so two players who share one are kept apart by a display string rather than by identity. A stable player id as the key would make the ambiguity structurally impossible and keep names clean on screen. Deferred by owner decision (2026-08-07), with the inventory recorded here so the follow-up does not have to rediscover it: `available_players`, `market_prices`/`model_prices`, `find_player`, ~20 endpoints taking a `player` form field, the transaction log, the trade dropdowns, and the `bo-<name>` DOM ids — plus `to_json`/`from_json`, so saved drafts and the undo chain need a migration. Large, and it touches the assign and bidding paths a live draft depends on

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
- **Make the exact standings automatic rather than a button.** `GET /solve-standings`
  (2026-08-17) answers the Proj column exactly and the operator has to remember to
  press it — the default on screen is still the estimate, now labelled. The want is
  real; **the mechanism this entry proposed until 2026-08-19 is dead, measured.** It
  said the cheap path was a per-team cache invalidated only when THAT team's roster
  or budget changes, because "the pool losing one player rarely moves an opponent's
  optimum". Rarely was doing far too much work: over five picks on a fresh league,
  **28 of 45 cached rows (62%) would have been stale**, single-pick swings reached
  **−26 points**, and one pick moved **9 of 9** other opponents twice in five. Every
  one of those figures renders as exact and BOT's carries a rank badge, so this is
  the same class of error as the done-team projection bug (`#2` when BOT was `#1`).
  The whole-column invalidation `_recompute()` already does is correct. So the only
  honest route is a cheaper solve, and the parallel scan work (2026-08-19f) took the
  fresh-league cost **1294ms → 384ms** — still far too much for an action path,
  where it would sit on top of `/assign`'s 150ms against a 500ms budget. What is
  left is not a cache: it is either a cheaper solve still, or an out-of-band refresh
  that lets the column arrive a beat after the pick (which needs a polling or SSE
  channel the app does not have). Revisit if draft-day use shows the button being
  forgotten — and do **not** re-propose the per-team cache without re-measuring the
  62%.
- **Save State button that jumps between live state and a scenario**, so testing a what-if doesn't cost the real draft state. Interacts with the scenario loader (`POST /load-scenario`) and the undo snapshot chain — check that switching can't strand a snapshot.

### From the 2026-08-07 testing pass

Cockpit ergonomics from a live run-through, source tag `[owner-testing]`. These
are wants, not defects — the things that were actually *broken* are under
**Open findings** above, and they should be fixed first. No `file:line` here,
because nothing is wrong at one; the file names are orientation only. (This said
"the five things" until 2026-08-11; a count in prose goes stale on the first
entry that closes, and one had already closed by then.)

**Nomination panel**

Landed 2026-08-18 (see `CHANGELOG.md`). Two corrections worth keeping. The entry
called it an additional **column** — the panel is two cards, not a table, so it is
a second labelled figure on the existing line (`Expected: ~$2.8M ▼ · Model
$9.5M`), which is what let it reuse `bid_limits.html`'s struck-model grammar
rather than invent one. And it assumed the pair would routinely differ: measured,
the UFA half's drain ranking breaks ties toward **least surplus**, so it actively
selects the candidate whose two figures agree — a $2.51M model against a $2.50M
market is that half's normal case, and the divergence the want is about shows up
on the **RFA half and on target picks**. That is also why `capped` is quantized to
one decimal: a cent of gap would otherwise strike through a figure identical to
the one beside it.

**Buyout Analyzer**

Landed 2026-08-15 (see `CHANGELOG.md`). Two corrections worth keeping, because
both are the kind of thing the next want will hit. The entry said the buyout
**dots** would "need somewhere to live" if the list collapsed to a `<select>`;
they never lived in the Analyzer at all — they are in `team_panel.html`'s two
roster tables, and duplicating them into a picker would collide on `_dom_id`.
And it did not mention what turned out to be the only real defect in there:
`hx-get="/buyout-check/{{ p.name }}"` was the one place in the app a raw player
name went into a URL unencoded.

One thing was deliberately **not** built, and would be the follow-up if the
picker ever reads as thin: each option showing its scan verdict.
`buyout_indicators` is already in the template context, but
`/buyout-indicators` returns only the OOB dot spans with `hx-swap="none"` — so
the labels would be right on page load and silently stale the moment you scan,
which is worse than absent. Making them live means the picker joins the scan's
out-of-band response, and it would then re-render mid-scan and drop whatever
the operator had selected. The roster table's dots answer "who"; the picker
answers "what would it cost".

**Logs**

All three original items landed 2026-08-15 (see `CHANGELOG.md`). One correction
worth keeping, because it is the kind of thing the next want will hit too: "NHL
team logos in **both** logs" was not buildable as written. `ChangeRecord` holds
`timestamp`/`kind`/`team_code`/`description` and no player at all, so an NHL
club logo has nothing to resolve from there. Both logs carry an FCHL team logo;
only the transaction side carries an NHL one.

**League State table**

Both original items — three-letter codes only, and Done as an X — landed
2026-08-13. What they bought is worth recording, because the premise under them
was wrong and the same premise would sink the follow-up:

- **Shorter column headers are the only lever left, and it is a real trade.**
  Removing the full team name and the "Stopped Drafting" label took min-content
  from **955px to 868px** — only 87px, because min-content is each column's
  longest *word*, not its longest string, so a two-word name never cost more
  than "Johannesburg" and the wrapped button never more than "Drafting".
  Measured per column afterwards, **all 12 columns are now floored by their own
  header text** and they sum to exactly the 868: Remaining 102, Spendable 100,
  Cap Used 92, Max Bid 83, Penalty 81, Roster 73, Needs 72, Team 66, Proj 57,
  Done 53, Pts 51, logo 38. So nothing in the table *body* can narrow this
  further — `Rem` / `Spend` / `Cap` / `Max` / `Pen` would, by roughly 200px, and
  that is abbreviating the labels on a dense grid of money figures that all look
  alike. Not filed as a want because it was not asked for and the legibility
  cost is real; filed as the measurement so the next person does not re-derive
  it or reach for the body again. The table still overflows its column at every
  width including 1920, so `.table-scroll-x` stays load-bearing either way.

**Available Players**

All three items in this section and the one below closed 2026-08-16 (see
`CHANGELOG.md`). The RFA filter was built; **both bid-panel tooltips already
existed** and are recorded here rather than deleted, because this is the second
time a want in this list turned out to be built already (the Buyout Analyzer,
2026-08-06) and the cost each time is a re-investigation:

- Sigma — `player_chart.html`, on the chart's meta line, and already required
  BY NAME in `TestTooltipsStayInsideTheirPanel`.
- Marginal value — `bid_panel.html`, in the `.bid-details` row.

Check the template before filing a tooltip want.

**Trade form**

Landed 2026-08-15 (see `CHANGELOG.md`). "Cramped" turned out to understate it —
measured at 1280, the four controls rendered 120–183px against labels wanting
229–316px, so the salary and points were off the edge on every row. Both forms
are now stacked one-column `.choice-list` checkbox blocks.

Two things deliberately **not** built, and both would be follow-ups rather than
oversights. **Re-ticking the boxes after an evaluate**: the response re-renders
`#trade-panel` and the form comes back empty, exactly as the selects did — and
unlike the buyout picker there is no control contradicting the answer beside it,
since the verdict block lists what you gave and received. Restoring it means
re-fetching and re-ticking the JS-built half. **A search box over the 49 rows**:
the height cap plus full-width labels is the measured fix; revisit only if
scrolling still bites in a real break.

One accepted rough edge, measured rather than assumed: at **1024px** the widest
Give row wants 305px against a 293px list and scrolls 12px inside it. The draft
runs at 1280–1600, where everything fits, and scrolling 12px is a different
class of thing from the 157px the old select clipped silently.


### Performance

- ~~**Interaction budget: every UI interaction < 500ms.**~~ Met. Measured 2026-08-06 on a fresh state (BOT 12 rostered, 704-player pool): warm `/bid-check` **9ms**, `/assign` **150ms**, `/nominate` **130ms**, `/undo` **127ms**, `GET /` **20ms**, `/explain` **215ms** cold and **9ms** warm once cached. Nothing is over budget, so the entry is closed on measurement rather than on more work. Two things stay true and are not defects: the *first* bid check on a new player is still ~1000ms (~10 MILP solves in the marginal — the lever there is a cheaper solve, not fewer solves — but **not pool pruning**, which was measured unsafe 2026-08-20; see the `main.py (bid_check)` open finding for the numbers), and `/trade-evaluate` is still unmeasured because it needs a built-up trade form. Where a regression would actually hurt, the guard is a solve count rather than wall-clock (`tests/test_bid_cache.py`, `tests/test_counterfactual_cache.py`) — timing assertions go flaky under load and the solve count is the cause anyway.

---

## Resolved

Everything that has been fixed, added or changed lives in
[CHANGELOG.md](CHANGELOG.md), newest first. It was 81% of this file and buried
the open items it shared it with.

Moving an entry there is what "done" means: delete it from **Open findings**
above and write it up under the date it landed, in the same commit as the fix.

# Backlog

The single work list for this project: deferred review findings plus forward-looking ideas.

**Open findings** are things flagged by review agents (`/grill`, `/go`, `/simplify`, etc.) that were **not** addressed in the change that surfaced them. Format:

`- [YYYY-MM-DD] [source] file:line (symbol) — finding — reason deferred`

Name the enclosing function or property in `(symbol)`. Line numbers drift every time the file above them changes — on 2026-08-05 a third of the references in this file pointed at unrelated code — and a stale line sends you somewhere wrong without saying so. The symbol survives the drift and makes the entry greppable. Templates have no symbols, so those entries carry a line only.

`tests/test_backlog_refs.py` enforces this: every `file.py:line` here must resolve, and must sit inside the function it names. That is why editing code can fail the suite on a docs file — re-anchor the affected entries in the same commit. A Python reference without an identifier-shaped symbol fails too, since a prose parenthetical would opt out of the only check that catches drift.

**Ideas / future work** are unprompted improvements with no specific defect behind them. No file:line.

---

## Open findings

### unsorted

<!-- Named for the SORTING, not for the age. "recently filed" was the first
     draft and it rots: it is accurate only until something newer is filed
     under one of the headings below, after which it labels the older half of
     the file "recent". This heading exists because the newest findings sat
     above the first `###` with three paragraphs of general guidance wedged
     under them, so a reader scanning headings missed them entirely. -->

- [2026-09-11] [grill] `main.py:1762 (trade_execute)` — **a trade whose form
  has been edited since the evaluate is still executable if JavaScript does not
  run.** `/trade-execute` posts only `trade_id` and acts on the server's
  `last_trade_eval`, so the *only* thing standing between a modified selection
  and executing the trade you evaluated is `markTradeEvalStale` disabling the
  button. That guard shipped with the 2026-09-10 fix that made the hazard
  reachable, and it is enough in practice — every panel in this app is an htmx
  request, so a browser with broken JS has no working UI to reach this from.
  Deferred rather than closed because the server-side version is not free: it
  means posting the give/receive lists to `/trade-execute` and comparing them to
  `last_trade_eval`, which is a second serialisation of the receive side (JSON
  blobs) purely to re-derive something the server already knows.

- [2026-09-10] [owner-question] `main.py:1435 (bid_check)` — **the
  `highest_bidder` form field is dead and looks load-bearing.** Surfaced while
  answering "does it matter whose turn it is to bid": no JavaScript ever writes
  it (`bid_panel.html`'s hidden input is round-tripped from `ctx` and
  `shortcuts.js` never touches it, so in practice it is always `""`), it lands
  in `MarketInfo.highest_bidder`, and `compute_bid_recommendation` never reads
  that field. `compute_market_ceiling` *does* populate it and `tests/test_market.py`
  asserts on it, so the dataclass field stays either way — this is about the
  form field and its hidden input. Deferred because removing it edits eight
  `tests/test_endpoints.py` payloads for no behavioural change, and it was out
  of scope for a question about turn order.


- [2026-09-10] [design-review] `state.py:810 (_searchable)` — **two different
  people sharing one name yield one search hit, and the pool copy is the one
  dropped.** `_disambiguated_names` renames duplicates *within* the biddable
  pool, but a roster row and a biddable row carrying the same string go to
  different dicts and neither is renamed — its own docstring names `Jack
  Hughes` and `Elias Pettersson` as the case. `_searchable` keys by name and
  `setdefault`s, so the roster copy wins and the draftable one is invisible in
  the one tool built to answer "where is he?". Deferred because it is not
  reachable on today's data: the zero-point exclusion hides both halves of
  every such pair (measured 2026-09-10, zero pool/roster name collisions), and
  the fix changes the index value to a list, which moves `total`, the tier
  ranking and four tests. `players.csv` is replaced before every draft and the
  next projection refresh removes that cover, so re-check this at refresh time
  rather than waiting for it to be reported.

- [2026-09-07] [nhl-team-join] `data/players.csv` (data, no symbol — line numbers
  drift on every refresh) — **15 player names are corrupted by two careless
  find-and-replaces, and 9 rows carry the FCHL placeholder `UFA` in the NHL TEAM
  column.** The name damage is (a) a case-insensitive `ARI` -> `UTH` rename
  (Arizona to Utah) that also hit the substring inside names — `Eetu
  LuostUTHnen`, `MUTHo Ferraro`, `John MUTHno`, `Zach PUTHse`, `Alexandr DUTHn`,
  `Vili SaUTHjarvi`, `GUTHn Bjorklund` — and (b) `-` -> `0`, giving `Oliver
  Ekman0Larsson`, `Nicolas Aube0Kubel`, `Alex Barre0Boulet`, `Trey
  Fix0Wolansky`, `Benoit0Olivier Groulx`, `Carl0Johan Lerby`, `Jon0Randall
  Avon`, `Marc0Andre Gaudet`. These are the names the draft tool **displays and
  matches on** — the player name is the app's primary key — so a corrupted one
  cannot be typed into the Start Auction field as it is spelled anywhere else.
  The `UFA`-in-NHL-TEAM rows are invisible to pricing (`_get_team_probability`
  falls through to the default for an unknown code) but render as the player's
  NHL club. Also present: `Ryan OReilly` and `Ryan O'Reilly` as separate rows on
  different clubs, which may be one player duplicated. — Deferred because
  `players.csv` is the operator's live pool and is regenerated from the pricer
  repo before every draft: the fix belongs upstream, in whatever produced these
  substitutions, or the same damage returns on the next refresh.
  `convert_legacy_players.normalize_name` works around both patterns for
  matching only, and deliberately does not rewrite the file.


**Last triaged 2026-09-11**, walking every entry and re-checking the mechanism
rather than the prose. Nothing was closed by the walk — all nineteen findings
reproduced at that moment — but four were closed later the SAME DAY by the work
that followed (`d89e4ba` the `nhl_logo_src` UTA case, `8ca89f4` `_driver_rows`
and `floor_logit_delta`, `5b8aacc` the Tailwind Play question), so the list is
**fifteen** now. Re-audited 2026-09-11 end to end: all fifteen still reproduce,
all fifteen `file:line (symbol)` references still resolve, and nothing in the
file has already been fixed. The walk also corrected four claims: `.table-scroll-x` is
four regions now, not three; `bid_limits` is 705 rows, not 704; the exact
standings entry under **Ideas** described a button-only feature that had
auto-solved on every pick since 2026-09-10; and the previous triage note ended
"No entries are waiting on a manual check", which has not been true since the
draft-day items were filed.

**What the shape of this list means.** Most of what is below is parked on a
**reason that has to expire before the entry is actionable**, and the reason is
usually *a real draft*: the planning ceiling, the stale counterfactual, the
opponent-edit exposure and the cold-`/bid-check` stall all need auction-day
behaviour to decide, not more analysis. Two more are parked on the next
`players.csv` refresh. So a quiet backlog here does not mean a healthy one — it
means the cheap items are gone and what is left is waiting on events.

**Trust the measurements, not the deferral reasons.** Three entries have now been
found deferred on a diagnosis that was wrong rather than merely stale: one
claimed a `state.py` change was needed when the information was already local to
the endpoint, one claimed `/assign` needed an out-of-band response when it had
always returned `all_panels.html` by design, and the trade form's called the fix
"re-ticking the boxes" when the actual fix was to stop destroying them. Re-check
the mechanism before trusting "deferred because X" here; the prose is a
hypothesis unless it says what was measured.

### engine/market

- [2026-08-16] [investigation] market.py:43 (compute_market_ceiling) — **the planning ceiling is second-highest-of-ten, and two rich teams pin it at `MAX_SALARY` for as long as they stay rich.** Open design question, **not a correctness bug**: `min(model_price, market_ceiling)` is never *wrong*, it is only sometimes inert. Measured 2026-08-16 with `tests/measure_ceiling.py` over a full 165-pick auction, and the two spending models are far apart — buyers paying the tool's own market price never bind it (0/165 picks, 18% of the league cap unspent, three teams — JHN $19.8M, GVR $14.1M, VPP $12.0M — finishing above the line the rule needs two of), buyers paying what the reserve rule allows bind it on 133/165, stepping `11.4M@1 -> 7.3M@33 -> 4.5M@41 -> 0.5M@44` (1-based ordinals; the instrument printed 0-based indices until 2026-08-17). So the layer works as designed and the question is empirical: a real draft's spending decides whether Layer 2 contributes anything to *planning*, and if it lands near the model-price end, a demand-aware price (how many teams need the position, how much money is chasing this tier) would do more than a ceiling nobody reaches. Deferred because changing how planning prices are derived moves every bid recommendation in the tool and there is a draft coming. **The blocker is no longer collection** — `TransactionRecord` has logged `model_price` and `market_price` on every pick all along, and `tests/measure_spend.py` (2026-08-17) reads them back, so the condition on this entry is now "run the reader after the draft", not "find a way to get the numbers". Note the reader measures the sharper quantity: `market_price < model_price` (the ceiling changed a planning price) rather than `ceiling < MAX_SALARY`. On the drain run those are 122 and 133, and the 122 all start at pick 44 when the ceiling hit the floor — the intermediate steps capped nothing. The threshold is pinned meanwhile by `tests/test_market.py::TestWhenTheCeilingLeavesTheCap`. Note this is the **idle** ceiling only; the live one the advisor uses is below `MAX_SALARY` in 7/10 single-rival matchups by mid-draft and needs nothing. As of 2026-08-18 the mid-range case is also **loadable**: `scenarios.load("drained-late-draft")` puts the idle ceiling at $3.3M with 25 of 597 pool prices capped, so the question of what the layer contributes to planning can be looked at on screen rather than only in an instrument — the entry stays open because the condition is still a real draft's spending
- [2026-07-05] [review] optimizer.py:282 (solve_optimal_roster) — positive-point pool smaller than remaining spots (or cheapest legal roster > budget) → MILP Infeasible → bid advice degrades to floor values. UI warning badge added in `templates/partials/bid_panel.html:15 (milp.status != "Optimal")` so it's no longer silent, and pinned in both directions 2026-08-13 by `tests/test_endpoints.py::TestRenderingWhenTheOptimizerFails`; actual short-roster planning (optimize the N players you CAN buy) still unbuilt — deferred, and **probably not worth building**: measured 2026-08-06, position slack on the live pool is F +333 / D +197 / G +53 against league-wide open needs, so the pool-too-small trigger is unreachable, and the budget-too-tight trigger is unreachable through **bidding** (the commissioner-prevented case, closed 2026-08-06 — see `CHANGELOG.md`) though NOT through play: buyout penalties, `/trade-between` and `/adjust-salary` all raise cap load and warn rather than refuse, and $20.5M of penalties on a fresh BOT reaches it (measured 2026-08-13). Left open only because a future pool could be thinner; re-measure before building anything — and note 2026-08-20 measured the **adjacent** idea, shrinking the pool handed to a solve that is otherwise fine, and found it silently wrong once BOT's budget per open spot drops toward the reserve floor (see the `main.py (bid_check)` entry). That is the same regime this entry is about, so a short-roster path has to be exact rather than a heuristic over "the N players you CAN buy"


### frontend/UX

- [2026-08-13] [grill] templates/partials/league_state.html:61 (table-scroll-x) — **the four `.table-scroll-x` regions cannot be scrolled by keyboard** (no `tabindex`, so they are not focusable; WCAG 2.1.1). Introduced 2026-08-11 with the grid fix, which made the League State and roster tables scroll inside their own panels rather than paint across the next one — so their right-hand columns are now reachable only with a pointer or a trackpad gesture. Deferred deliberately rather than overlooked: `tabindex="0"` on four wrappers adds four tab stops to the panels you tab through while a bid is live, and the draft is a single operator on a mouse. The content is not lost, it is one drag away. Revisit if the draft is ever run from the keyboard, or if a screen reader is ever in play — at which point the fix is `tabindex="0"` plus `role="region"` and an `aria-label` naming the table, not tabindex alone
- [2026-08-11] [grill] templates/partials/team_panel.html:203 (not p.is_target and p.can_be_bought_out) — **an opponent's pick now auto-presents their EDITABLE panel, which used to require a deliberate click.** The roster-edit forms are not gated on `is_my_team` by design (auditing a rival is the point), but before 2026-08-11 `/assign` always came home to BOT, so a rival's Bench / `$` / ↓ Minors / Recall controls only appeared when you asked for them. Now a sale puts them on screen at the highest-tempo moment of the draft. Measured, and this is why it is filed rather than fixed: the salary box is `templates/partials/team_panel.html:246 (hx-trigger="change")`, so it needs a typed value plus a blur, and every other control is a discrete small button — a stray click cannot fire one. No test or gate added: gating them on `is_my_team` would remove the working feature the 2026-08-07 view work exists to provide. Revisit only if a real draft produces an accidental edit; the fix would be a confirm on opponent edits, not a gate
- [2026-08-08] [review] main.py:162 (_backfill_keeper_flags) — **the backfill repairs the live state but not the undo chain**, so after booting a pre-`is_keeper` save file, undoing back past everything done this session restores minors with no provenance and the next recall of one colours him as a purchase again. `AuctionState._snapshots` is a list of whole JSON documents rather than of dicts, so repairing them from `main.py` means hard-coding a second copy of the state's JSON key names — a wrong key would silently do nothing, which is worse than the bug. Deferred as narrow and cosmetic: it needs a legacy file, an undo past the whole session, and it costs a row colour. If it ever matters, the fix belongs in `state.py` as a `from_json` hook, not here
- [2026-08-08] [grill] templates/partials/bid_limits.html:64 (tooltip-left) — **8 of the 20 `data-tip` tooltips are never placement-checked**, so the 2026-08-08 CSS block's guarantee is narrower than it reads. `TestTooltipsStayInsideTheirPanel` measures whatever the page renders in one state (fresh reset + live bid) and that is ~12: the five `stop_status` branches are mutually exclusive so only one is ever on screen, the Penalty tile needs `penalties > 0`, and this line — the only `tooltip-left` in the app — renders only when the market ceiling caps a model price, which never happens on a fresh state because every team starts at `MAX_SALARY`. **Not a regression risk from that change**: the global rule is `max-width`, which can only make a bubble narrower and therefore reduce horizontal overflow. The one real exposure is vertical — narrower means taller, and this tooltip is the only one living inside a `.scroll-container` with `overflow-y: auto` — `templates/partials/bid_limits.html:30 (scroll-container)` — which clips. **Partly closed 2026-08-13**: `POST /load-scenario` grew `endgame-ceiling-binds`, and `TestTooltipsStayInsideTheirPanel` now runs against it at 375/1024/1280 with the capped tip required BY NAME, so the `tooltip-left` is placement-checked on the horizontal axis for the first time and passes. The **vertical** exposure this entry predicted is real but bounded, and was measured rather than asserted: the bubble is 99px tall against ~64px rows, so on the last row visible inside the 405px `.scroll-container` it overhangs the bottom edge by **~25px** — and scrolling one row cures it, which is why no assertion was added (a naive check flags every row below the fold as clipped, since an unscrolled row is trivially outside the client box). Still open for the remaining tips: the five `stop_status` branches are mutually exclusive and the Penalty tile needs `penalties > 0`, so ~4 are still never measured. Deferred: each needs its own page state for a cosmetic property
- [2026-08-06] [owner] main.py (_counterfactual) — the auto-shown counterfactual is computed at the MARKET price, not the live bid, so it does not sharpen as bidding climbs. That is what makes it cacheable: re-solving per $0.1M increment costs ~200ms and would put a response back inside the Assign mousedown/mouseup window. If it reads as stale in a real draft the follow-up is a manual "recompute at this price" button, never an automatic one — deferred pending draft-day experience

### code quality

- [2026-08-19] [grill] main.py:1430 (bid_check) — **`/bid-check` and `/explain` are still `async def` wrapped around synchronous MILP work, so a cold one holds the event loop for its whole duration.** *Partly closed 2026-08-19*: the two multi-solve SCANS now hand their solve loops to a worker thread and publish only if the state has not moved underneath (see `CHANGELOG.md`), which took a warm `/bid-check` during a roster scan from **1682ms back to 3ms** and `/state` from 1564ms to 12ms. What is left is the two endpoints on the bidding path itself — measured 2026-08-19, a **cold** `/bid-check` is 935ms (a binary search over MILP solves) and `/explain` 165ms — and neither is the same cheap change. (1) `/explain`'s serialisation is load-bearing: `tests/test_counterfactual_cache.py::TestResponsesCannotOvertakeEachOther` exists because FIFO ordering is the only thing stopping a late counterfactual landing in a mount that now shows a different player, and its docstring already names the prerequisite — `hx-sync="#app:replace"` on the mount in `bid_panel.html` — so this needs a template change and a browser check rather than a keyword. (2) The remaining stall is self-inflicted rather than cross-request: the operator is one person, and the request that queues behind a cold `/bid-check` is their own next keystroke on the same player, which the marginal cache then answers in 9ms. Revisit if a draft-day stall on the FIRST bid of a player is actually felt; the lever there is a cheaper solve — and **pool pruning, the one this entry used to name, is measured unsafe.** Cold `/bid-check` is 988–1030ms across 10 solves, so the solve really is the whole cost. (This used to say **98–99% inside CBC**; re-measured 2026-08-21 the aggregate is **89.5–89.8%** and the per-subject range is **65–92%** — see below. The conclusion the figure was supporting still holds on the states that cost a second, which is why it was never caught.) Keeping the top 50 by points per position (705 → 150) gives a **byte-identical answer on all 7 pinned states** (the six in `scenarios.SCENARIOS` plus the fresh pool — "7 scenarios" was loose, there are 6) and a 2–3.7x faster solve, which is exactly the trap: every scenario sits at $1.9M+ of BOT budget per open spot. Squeeze the budget toward the reserve floor and it goes **silently wrong** — at $1.00M/spot it returns 1069 against a true 1076, status still `Optimal`; at $0.70M and $0.60M it returns **Infeasible** where the true answers are 999 and 961. Adding "plus the K cheapest per position" does not rescue it (912 against 999), because which players matter depends on the budget *interaction*, not on points or price separately. A wrong `Optimal` wearing a confident number is the `keepFiles=True` failure class `TestTwoSolvesAtOnceAgreeWithTwoSolvesInARow` exists for, and the tight-budget regime is reachable **through play** — buyout penalties, `/trade-between` and `/adjust-salary` all warn rather than refuse, as the `optimizer.py (solve_optimal_roster)` entry above records ($20.5M of penalties on a fresh BOT reaches it). **Measured 2026-08-21, and the three surviving candidates now have numbers** — `tests/measure_marginal.py`, which stays as the harness. Two of this entry's own figures were wrong: the CBC share is **89.5–89.8%** in aggregate, not 98-99% (the rest is a flat ~9.2ms per solve of model build and extraction, paid ten times over for ten models that differ in one number) — and per subject it runs **65–92%**, because that ~9.2ms is charged per *solve* so its share tracks how expensive each solve is. "The solve is the whole cost" holds on the big-pool states and not on the cheap ones, which is C2's regression below seen from the other side. The solve count is not ~10 and not bimodal either: over the 28 scenario subjects it is **2, 3, 9 or 10** (×4/×8/×8/×8) — two for a floor player, three for a must-have, nine or ten for the full search depending on where `physical_max_bid` puts the bracket. On the candidates: model reuse alone is **1.06x**, reuse plus `warmStart=True` **1.14x**, and the best is one the entry did not name — the probe search is a sequence over a single budget RHS, so it has a single-solve dual, "cheapest roster containing him that still beats the without-him total", giving **1.55-1.60x on the big-pool states, 1.45x overall, and ~0.8x on `endgame-sole-bidder`** where the reference already short-circuits. Worst single subject 1511ms → 772ms. All three reproduce the reference's marginal byte-for-byte on **168 subjects** (28 scenario + 140 swept to $0.60M/spot) — which is the whole recommendation, since `compute_bid_recommendation` takes `marginal_value` as an argument and that float is a candidate's only channel to the five fields the panel renders, so comparing those too would be a check that cannot fail. **Not shipped, deliberately**: 1.6x on the slow cases does not buy a second MILP formulation plus a confirm loop plus two float-epsilon subtleties on the hottest path in the app, each of which took a wrong draft to find — one an increment high from smoothing solver tolerance, the other an increment low from `1.9 / 0.1 == 18.999999999999996`, and both invisible on the scenario set. `warmStart` is at least cleared as safe: its `.mst` comes from the same `create_tmp_files` call as the `.lp`, so it carries the per-solve `uuid4().hex` and is nothing like `keepFiles=True`. Trigger unchanged — a draft-day stall on the first bid actually felt — but the answer is now costed rather than open, and re-running is `--sweep`
- [2026-08-06] [grill] main.py:1145 (_context) — every endpoint builds the full context (~8.5ms, including a `bid_limits` list of the whole pool — 705 rows today — for the available-players table) regardless of how small a fragment it renders. `/bid-check`, `/nominate` and now `/explain?inline=1` reference a handful of its 23 keys and none touches `bid_limits`. `/explain` made this sharper on 2026-08-06: it fires on every bidder toggle and its warm response is ~9ms, essentially all of it this context build for a fragment that uses three keys. Pre-existing — the old whole-panel `auction_control.html` didn't use it either — but the 2026-08-06 panel split made fragments narrower and the waste correspondingly larger. Deferred: small next to the binary search over MILP solves that dominates `/bid-check`, and fixing it properly means a per-panel context builder, which is a cross-endpoint refactor


### test infrastructure

- [2026-08-17] [review] tests/test_browser_ui.py:474 (test_an_over_cap_adjust_salary_toast_renders_and_dismisses) — **flakes under full-suite load with `Page.evaluate: Resulting promise was garbage collected`**, a Playwright teardown error rather than an assertion failure. Observed once in a 777-test run 2026-08-17; the same test passes 3/3 in isolation and the whole browser file passes 35/35 on its own (98s), so it is contention, not a regression — nothing in that commit touched the browser suite. The mechanism fits the test: it fires `htmx.ajax` from the page and then evaluates against the resulting toast, so a slow response under load can outlive the evaluate's promise. Deferred rather than papered over with a retry: a `flaky`/rerun decorator would hide a real regression in the one suite that checks things `TestClient` physically cannot, and CLAUDE.md already names `-m "not browser"` as the draft-day escape hatch. Worth a fix only if it recurs — at which point the shape is awaiting the specific toast element before evaluating, not a blanket rerun. Note the existing closed interaction-budget entry covers a *different* problem (wall-clock assertions going flaky under load); this one has no timing assertion at all
- [2026-08-07] [refresh-drill] data_loader.py (_disambiguated_names) — the duplicate-name suffix is a workaround for a naming assumption, not a repair of it: the player NAME is still the primary key, so two players who share one are kept apart by a display string rather than by identity. A stable player id as the key would make the ambiguity structurally impossible and keep names clean on screen. Deferred by owner decision (2026-08-07), with the inventory recorded here so the follow-up does not have to rediscover it: `available_players`, `market_prices`/`model_prices`, `find_player`, ~20 endpoints taking a `player` form field, the transaction log, the trade dropdowns, and the `bo-<name>` DOM ids — plus `to_json`/`from_json`, so saved drafts and the undo chain need a migration. Large, and it touches the assign and bidding paths a live draft depends on

---

## Ideas / future work

### Price model (Layer 1)

Track these; don't implement upfront. The market layer (Layer 2) already compensates for some of them — only build one if draft-day testing shows the base model plus market layer isn't accurate enough.

- **Dynamic budget deflation** — scale model price by (remaining league budget / starting league budget) as a simple auction-phase correction. The model is currently static and does not adjust for budget depletion mid-auction.
- **Positional scarcity in the model layer** — boost model price when a position's supply/demand ratio is tight. The market layer partially handles this via demand count, but the model price itself doesn't adjust.
- **Price momentum** — rolling correction based on recent actual-vs-predicted ratios during the draft.
- **Auction position effect** — early picks tend to sell higher than the model predicts.
- **Non-linear points × team_probability interaction term.**

### UI / UX

From live debugging and testing, 2026-08-05. These are cockpit-ergonomics items — the engine is right, the interface makes it hard to act on.

- **The exact standings still degrade to estimates on every mutation except a
  pick.** The original want — "make the exact standings automatic rather than a
  button" — **shipped on 2026-09-10**: `POST /assign` fires
  `HX-Trigger-After-Settle: {"solveStandings": true}` and `shortcuts.js` answers
  with a coalesced `htmx.ajax('GET', '/solve-standings')`, so the column
  re-solves itself after every pick without anyone pressing anything. Do not
  read the rest of this entry as saying the button is still the only way.

  What is left is narrower and was an explicit owner decision: **only `/assign`
  asks.** Every other mutation — a bench toggle, a salary edit, a trade, a
  buyout, an undo — still calls `_recompute()`, still clears
  `exact_projections`, and still drops the column back to estimates, and the
  manual button is the only way back from those. `#proj-basis` says which basis
  is on screen, so it is honest rather than silent, and the estimate is not
  small: +68 mean / +193 worst, moving 9 of 10 teams in rank order.

  **Two measured things that must survive any follow-up.** Do not re-propose a
  per-team cache invalidated only when that team's roster or budget changes: over
  five picks on a fresh league **28 of 45 cached rows (62%) would have been
  stale**, single-pick swings reached **−26 points**, and one pick moved 9 of 9
  opponents twice in five. And do not put a synchronous solve on the other action
  paths — the parallel scan is **384ms** fresh, which is why the one that does
  fire is out-of-band and after-settle rather than inline. The honest routes left
  are a cheaper solve, or extending the same after-settle trigger to the other
  mutations and accepting the cost, which is a call to make with draft-day feel
  rather than in advance.

- **Save State button that jumps between live state and a scenario**, so testing a what-if doesn't cost the real draft state. Interacts with the scenario loader (`POST /load-scenario`) and the undo snapshot chain — check that switching can't strand a snapshot.

### From the 2026-08-07 testing pass

Every item from that live run-through has landed. The corrections the closed
entries were carrying — what each want got wrong, and why the next one will hit
the same thing — moved to `CHANGELOG.md` on 2026-09-11 under "Notes kept from
closed wants", because this file holds open work and that was a hundred lines of
finished work sitting on top of it.

Two things in that batch were deliberately **not** built and are still open:

- **The buyout picker showing each option's scan verdict.** `buyout_indicators`
  is already in the template context, but `/buyout-indicators` returns only the
  out-of-band dot spans with `hx-swap="none"` — so labels would be right on page
  load and silently stale the moment you scan, which is worse than absent. Making
  them live means the picker joins the scan's OOB response, and it would then
  re-render mid-scan and drop whatever the operator had selected. The roster
  table's dots answer "who"; the picker answers "what would it cost". Follow-up
  only if the picker ever reads as thin.
- **A search box over the trade form's 49 give rows.** The height cap plus
  full-width labels is the measured fix; revisit only if scrolling still bites in
  a real break. One accepted rough edge, measured rather than assumed: at 1024px
  the widest Give row wants 305px against a 293px list and scrolls 12px inside
  it. The draft runs at 1280–1600, where everything fits.

---

## Resolved

Everything that has been fixed, added or changed lives in
[CHANGELOG.md](CHANGELOG.md), newest first. It was 81% of this file and buried
the open items it shared it with.

Moving an entry there is what "done" means: delete it from **Open findings**
above and write it up under the date it landed, in the same commit as the fix.

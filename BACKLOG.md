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

- [2026-09-20] [plan] `trade.py:416 (execute_buyout)` — **a mid-auction drop of a player with no NHL contract cannot be recorded.** `execute_buyout` is the only removal that is not a trade, and it charges the CBA 11.4 penalty — 50% of salary stays on the cap — which is exactly wrong here: there is no contract to buy out, so the league charges nothing and the full salary comes off. Today the two are indistinguishable to the tool, and entering the buyout would leave the team carrying a penalty the league never assessed. Deferred because the owner scoped this pre-draft only (2026-09-17), where deleting the row from `players.csv` is exact, durable through `/reset`, and needs no code — Laine was handled that way. **Cost measured at defer time**, so the next person does not have to re-derive it: a new `transaction_type`, plus a new `where` threaded through `state.py:809 (_searchable)`, `state.py:872 (_locate)`, `main.py:1744 (_search_rows)` for both the label lookup and the penalty it computes, `templates/partials/search_results.html:68 (bought-out)` and `static/style.css:442` — miss any one and the player becomes findable **nowhere**, which is worse than a buyout, since that at least surfaces him; a branch in `tests/measure_replay.py:214 (_apply)`, which raises on an unknown type by design; and a new eligibility property beside `state.py:94 (can_be_bought_out)`, because a no-contract player is typically group 2/3 and the picker in `templates/partials/team_panel.html:378 (can_be_bought_out)` would otherwise keep offering him the penalising path. **Re-raised 2026-09-20 in a cheaper shape, and re-deferred on SCOPE rather than on cost** — the owner asked whether a *checkbox beside the buyout* saying "no penalty" would beat the hand edit. It is genuinely cheaper than the endpoint costed above: keeping `transaction_type="buyout"` leaves the player findable, so `state.py:809 (_searchable)`, `state.py:872 (_locate)`, `templates/partials/search_results.html:68 (bought-out)` and `static/style.css:442` need no change at all and the findable-nowhere hazard that dominates the estimate above never arises. It loses anyway, because it cannot express the only case in scope: a buyout logs a transaction, and `bake_roster_state.py:112 (require_pre_draft)` refuses to bake **any** state holding one, so a free buyout entered pre-draft never reaches the data files — and `/reset` rebuilds from `players.csv` + `fchl_teams.json` alone, so it wipes it. The checkbox would have to be re-ticked after every reset, which is precisely the failure the bake path exists to remove; deleting the row is the only expression of "gone this season" that survives one. **What it would still have to fix if the scope ever changes**, recorded here because it is a live correctness trap rather than a cost: the penalty is nowhere stored — `TeamState.penalties` is one aggregate float with no per-player breakdown, and the 50% is recomputed from salary at `main.py:1745 (_search_rows)` (the figure in the header search), `main.py:2270 (buyout)` (the success toast) and `tests/measure_replay.py:248 (_apply)` (the replay), so "this one was free" has to be stored on the `TransactionRecord` and read back at all three — miss one and the tool reports a penalty the league never assessed, which is this same defect in a new place. **The trigger to build it is a free drop happening MID-AUCTION, never the hand edit feeling tedious** — the owner has now scoped it pre-draft-only twice (2026-09-17, 2026-09-20)

- [2026-09-22] [plan] `convert_fchl_online.py:518 (convert_all)` — **the FCHL Online converter has no test module of its own.** It built the whole 2026-27 pool (1268 rows, every roster, every projection join) and shipped in `4a08a6f` with its behaviour pinned only by `TestDataFingerprint` and the live-data invariants, both of which check the *output* file rather than the conversion. `tests/test_fchl_online_conversion.py` now covers `no_nhl_club` and the rostered-no-contract invariant, which is the part that had a live defect, and — since 2026-09-22 — the Dobber join's same-name case, which had a second live defect (the defenceman Elias Pettersson priced off the forward's 69 points; see `CHANGELOG.md`). **That is two live defects out of the untested half, found by looking rather than by the suite**, which is the argument for doing the rest before the next run rather than after it. Still uncovered: the name split, the group parse, the unique-name exact join, and the first-initial fallback's club guard — the one measured to reject 5 impostors. The 2026-09-22 grill measured three converter mutants surviving every data-pipeline test: removing that club gate, `ACTIVE_GROUPS = {"2"}`, and dropping the `'RFA'` quote strip (which holds back 21 of 22 RFAs). Deferred because the script runs once a season and its next run is eleven months away; the time to do it is the next refresh, before it runs, when a regression would actually cost something

- [2026-09-22] [refresh-drill] `tests/conftest.py:21 (isolated_state_dir)` — **the write-through guard on the operator's live draft is a pytest fixture, so anything that is not pytest walks straight past it.** `main.STATE_DIR` defaults to `data/state`, and `isolated_state_dir` redirects it for the suite only. An ad-hoc diagnostic script doing `with TestClient(main.app)` — the obvious way to reproduce an endpoint failure outside the suite — therefore writes to `data/state/`, and `_save_state` rotates the previous file into `.backup`, so **two** such saves destroy both copies of a real draft. Hit on 2026-09-15 while diagnosing `test_19_bid_check_changed`: nine picks from a replayed `_script()` landed in `data/state/auction_state.json` and its backup, over a state that happened to be empty. Nothing warned; the file is `.gitignore`d, so `git status` said nothing either, and it was found only by opening the file before clearing it. **Corrected 2026-09-22 by the grill, on two counts.** This entry said `main.py` hardcodes the directory "with no env override", and so did CLAUDE.md and the three `tests/measure_*.py` docstrings: false since `3eb6ed9` on 2026-09-07, eight days before the incident — `FCHL_STATE_DIR` has always been the escape hatch, and the incident is a script that did not use it. And the guard it proposed, refusing at `lifespan` to save over a state whose pool came from a different CSV, would **not** have caught that incident: the script loaded the default pool over the default state, so the CSVs agreed. The real hazard is "a process that is not the operator's server writing the operator's state", which no property of the state can see. Still deferred, because every cheap guard found so far is wrong for draft day — changing the default moves the path the live server writes, and refusing to save under `pytest` in `sys.modules` misses a bare script and fires in the suite. The mitigation in force is procedural: a script that imports `main` sets `FCHL_STATE_DIR` or redirects `main.STATE_DIR` first, as `tests/measure_ceiling.py` and `tests/measure_layout.py` do

- [2026-09-11] [grill] `tests/test_browser_ui.py:1246 (test_no_tooltip_renders_outside_the_scrollable_content)` — **the `counted >= 10` floor now sits at exactly 10**, with zero slack: the 2026-09-11 removal of the league-table `data-tip` took the measured count from 11 to 10, so the next tooltip deleted anywhere in the app fails this rather than the named `required` inventory, and the failure message ("the page must render the bid panel's four and the team panel's stat tiles") will not describe what actually happened — deferred because that is the tripwire working as designed and the STATES are deterministic, so it is not flaky; revisit only if a legitimate removal trips it, at which point the fix is to re-derive the floor from the `required` inventory rather than to lower a magic number

- [2026-09-11] [grill] `main.py:2109 (trade_execute)` — **a trade whose form
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
  rather than waiting for it to be reported. Re-checked 2026-09-13 against the
  pool a full draft was actually run on (`players-25.csv`): **0** pool/roster
  name collisions and 0 zero-point pool players, so it did not reproduce there
  either — which is a second pool agreeing, not evidence the bug is gone.
  **The refresh has now happened and it did not fire.** Re-checked 2026-09-15
  against the 2026-27 pool this entry was waiting for: the file has **one**
  colliding group, `Elias Pettersson` (VAN F / VAN D), and both halves are
  BIDDABLE — so `_disambiguated_names` renames them and `_searchable` sees two
  distinct keys. Zero pool/roster collisions, for the third pool running, and
  this time not because the zero-point exclusion hid anything: the roster-vs-
  biddable shape is simply absent. That removes the trigger this entry named
  without removing the hazard, so it stays open with a new one — the next
  refresh, again. Three pools agreeing is worth recording as the reason this is
  cheap to keep deferred, not as a reason to close it.


**Last triaged 2026-09-11**, walking every entry and re-checking the mechanism
rather than the prose. Nothing was closed by the walk — all nineteen findings
reproduced at that moment — but four were closed later the SAME DAY by the work
that followed (`d89e4ba` the `nhl_logo_src` UTA case, `8ca89f4` `_driver_rows`
and `floor_logit_delta`, `5b8aacc` the Tailwind Play question), taking it to
fifteen, and the grill that closed that day's batch filed one more — so the
list went to sixteen — and the dead `highest_bidder` form field was removed
the same day, so it was **fifteen**. **2026-09-12 took it to twelve**: the
owner walked the draft-day items and answered all of them — the stale
counterfactual was closed by building the Recompute button, and the cold
`/bid-check` and the opponent-edit exposure were closed as decisions
(`CHANGELOG.md`). **2026-09-13 took it to eleven**: the owner ran a full
139-pick draft and the planning-ceiling entry, the only one genuinely waiting on
an auction rather than on an opinion, closed on the numbers it asked for.
**2026-09-14 put it back to twelve**: refreshing `team_odds.json` to the
2026-2027 Cup odds filed one, and it is a finding about this file's own safety
net rather than about the app — the refresh changed all 32 clubs and the suite
noticed nothing. **2026-09-15 took it to thirteen**: rebuilding `players.csv`
for the 2026 auction filed one more, also about tooling rather than the app —
an ad-hoc `TestClient` script wrote nine picks into the operator's live
`data/state/`, because the guard against exactly that is a pytest fixture. The
same refresh expired the trigger on the `_searchable` entry without closing it,
so the "waiting on a `players.csv` refresh" class is now **one**, not two, and
that one has a new trigger of the same kind. **2026-09-17 took it to fifteen**:
dropping Laine from the pool filed the mid-auction no-contract drop and the
missing converter test module. **2026-09-22 left it at fifteen**: that day's
grill filed the renamed-keeper skip and closed the 2026-09-07 corrupted-names
entry, whose premise no pool in the repo still carries (`CHANGELOG.md`,
Investigated). Keep
the count and the file in step: it has now been wrong four times, every one of
them an entry added or closed without the prose being touched — the fourth
said thirteen for a week while two entries landed beneath it. The walk also corrected four claims: `.table-scroll-x` is
four regions now, not three; `bid_limits` is 705 rows, not 704; the exact
standings entry under **Ideas** described a button-only feature that had
auto-solved on every pick since 2026-09-10; and the previous triage note ended
"No entries are waiting on a manual check", which has not been true since the
draft-day items were filed.

**What the shape of this list means.** Most of what is below is parked on a
**reason that has to expire before the entry is actionable**, and the reason was
usually *a real draft*. **That whole class is now empty.** Three of the four
expired on 2026-09-12, when the draft-day list was put to the owner directly
rather than waiting for the draft to produce an opinion; the fourth — the
planning ceiling — expired on 2026-09-13 when the draft was actually run and
`tests/measure_replay.py` answered it at **0 of 139 picks**. What is left is
parked on two different kinds of thing: the next `players.csv` refresh (two
entries — **one** after 2026-09-15, see above), the next `team_odds.json`
refresh (one), and ordinary cost-versus-benefit. **Asking is a way to expire one and
so is doing it**, and the ratio is worth knowing — four of seven draft-day items
needed a sentence from the operator, one needed the auction, and the auction
then took eleven hours and produced a single closed entry.

The lesson the ceiling entry leaves behind is about the *instrument*, not the
answer. It had been parked since 2026-08-16 on "needs a real draft's numbers",
and `tests/measure_spend.py` was written in August specifically to read them
back — but it reads the transaction log, which only contains players who
**sold**, while the question was about the prices the MILP plans over. The
entry's condition was satisfiable by a file that could not actually answer it,
and nobody noticed for four weeks. **When an entry names the instrument that
will close it, check that the instrument measures the entry's quantity.**

**Trust the measurements, not the deferral reasons.** Three entries have now been
found deferred on a diagnosis that was wrong rather than merely stale: one
claimed a `state.py` change was needed when the information was already local to
the endpoint, one claimed `/assign` needed an out-of-band response when it had
always returned `all_panels.html` by design, and the trade form's called the fix
"re-ticking the boxes" when the actual fix was to stop destroying them. Re-check
the mechanism before trusting "deferred because X" here; the prose is a
hypothesis unless it says what was measured.

### engine/market

- [2026-07-05] [review] optimizer.py:282 (solve_optimal_roster) — positive-point pool smaller than remaining spots (or cheapest legal roster > budget) → MILP Infeasible → bid advice degrades to floor values. UI warning badge added in `templates/partials/bid_panel.html:15 (milp.status != "Optimal")` so it's no longer silent, and pinned in both directions 2026-08-13 by `tests/test_endpoints.py::TestRenderingWhenTheOptimizerFails`; actual short-roster planning (optimize the N players you CAN buy) still unbuilt — deferred, and **probably not worth building**: measured 2026-08-06, position slack on the live pool is F +333 / D +197 / G +53 against league-wide open needs, so the pool-too-small trigger is unreachable, and the budget-too-tight trigger is unreachable through **bidding** (the commissioner-prevented case, closed 2026-08-06 — see `CHANGELOG.md`) though NOT through play: buyout penalties, `/trade-between` and `/adjust-salary` all raise cap load and warn rather than refuse, and $20.5M of penalties on a fresh BOT reaches it (measured 2026-08-13). Left open only because a future pool could be thinner; re-measure before building anything — and note 2026-08-20 measured the **adjacent** idea, shrinking the pool handed to a solve that is otherwise fine, and found it silently wrong once BOT's budget per open spot drops toward the reserve floor (see the cold-`/bid-check` write-up in `CHANGELOG.md` under 2026-09-12, and `tests/measure_marginal.py --sweep`). That is the same regime this entry is about, so a short-roster path has to be exact rather than a heuristic over "the N players you CAN buy"


### frontend/UX

- [2026-08-13] [grill] templates/partials/league_state.html:61 (table-scroll-x) — **the four `.table-scroll-x` regions cannot be scrolled by keyboard** (no `tabindex`, so they are not focusable; WCAG 2.1.1). Introduced 2026-08-11 with the grid fix, which made the League State and roster tables scroll inside their own panels rather than paint across the next one — so their right-hand columns are now reachable only with a pointer or a trackpad gesture. Deferred deliberately rather than overlooked: `tabindex="0"` on four wrappers adds four tab stops to the panels you tab through while a bid is live, and the draft is a single operator on a mouse. The content is not lost, it is one drag away. Revisit if the draft is ever run from the keyboard, or if a screen reader is ever in play — at which point the fix is `tabindex="0"` plus `role="region"` and an `aria-label` naming the table, not tabindex alone. **Premise confirmed by the owner 2026-09-12** ("I'll be using a mouse"), so the deferral is a decision rather than an assumption — it stays open because it is a real WCAG 2.1.1 gap and the next operator may not be this one
- [2026-08-08] [review] main.py:164 (_backfill_keeper_flags) — **the backfill repairs the live state but not the undo chain**, so after booting a pre-`is_keeper` save file, undoing back past everything done this session restores minors with no provenance and the next recall of one colours him as a purchase again. `AuctionState._snapshots` is a list of whole JSON documents rather than of dicts, so repairing them from `main.py` means hard-coding a second copy of the state's JSON key names — a wrong key would silently do nothing, which is worse than the bug. Deferred as narrow and cosmetic: it needs a legacy file, an undo past the whole session, and it costs a row colour. If it ever matters, the fix belongs in `state.py` as a `from_json` hook, not here
- [2026-08-08] [grill] templates/partials/bid_limits.html:64 (tooltip-left) — **8 of the 20 `data-tip` tooltips are never placement-checked**, so the 2026-08-08 CSS block's guarantee is narrower than it reads. `TestTooltipsStayInsideTheirPanel` measures whatever the page renders in one state (fresh reset + live bid) and that is ~12: the five `stop_status` branches are mutually exclusive so only one is ever on screen, the Penalty tile needs `penalties > 0`, and this line — the only `tooltip-left` in the app — renders only when the market ceiling caps a model price, which never happens on a fresh state because every team starts at `MAX_SALARY`. **Not a regression risk from that change**: the global rule is `max-width`, which can only make a bubble narrower and therefore reduce horizontal overflow. The one real exposure is vertical — narrower means taller, and this tooltip is the only one living inside a `.scroll-container` with `overflow-y: auto` — `templates/partials/bid_limits.html:30 (scroll-container)` — which clips. **Partly closed 2026-08-13**: `POST /load-scenario` grew `endgame-ceiling-binds`, and `TestTooltipsStayInsideTheirPanel` now runs against it at 375/1024/1280 with the capped tip required BY NAME, so the `tooltip-left` is placement-checked on the horizontal axis for the first time and passes. The **vertical** exposure this entry predicted is real but bounded, and was measured rather than asserted: the bubble is 99px tall against ~64px rows, so on the last row visible inside the 405px `.scroll-container` it overhangs the bottom edge by **~25px** — and scrolling one row cures it, which is why no assertion was added (a naive check flags every row below the fold as clipped, since an unscrolled row is trivially outside the client box). Still open for the remaining tips: the five `stop_status` branches are mutually exclusive and the Penalty tile needs `penalties > 0`, so ~4 are still never measured. Deferred: each needs its own page state for a cosmetic property. **Sharpened 2026-09-13**: this tip needs `market.is_capped` to be true of a pool player, and `tests/measure_replay.py` shows that happened **zero times in a 139-pick draft** — pool-wide, not merely among the players who sold. So it is not an under-measured tooltip, it is one a whole real auction never drew, which is the strongest argument yet that the remaining four are cheaper to delete than to place-check

### code quality

- [2026-08-06] [grill] main.py:1273 (_context) — every endpoint builds the full context (~8.5ms, including a `bid_limits` list of the whole pool — 678 rows on the 2026-27 pool, 705 when filed — for the available-players table) regardless of how small a fragment it renders. `/bid-check`, `/nominate` and now `/explain?inline=1` reference a handful of its 23 keys and none touches `bid_limits`. `/explain` made this sharper on 2026-08-06: it fires on every bidder toggle and its warm response is ~9ms, essentially all of it this context build for a fragment that uses three keys. Pre-existing — the old whole-panel `auction_control.html` didn't use it either — but the 2026-08-06 panel split made fragments narrower and the waste correspondingly larger. Deferred: small next to the binary search over MILP solves that dominates `/bid-check`, and fixing it properly means a per-panel context builder, which is a cross-endpoint refactor. **Cost measured against a real draft 2026-09-13**, which is the rule this file states for a deferral parked on "if it bites again": the median gap between picks over 139 sales was **27.2 seconds**, so an 8.5ms context build is ~0.03% of the operator's own cadence and the owner reported no stall ("It's not slow at all", 2026-09-12). Still real waste and still worth removing with the refactor; it is not worth a smaller fix


### test infrastructure

- [2026-09-22] [grill] tests/test_crash_recovery.py:738 (test_a_renamed_keeper_is_found_too) — **the renamed-keeper backfill test skips on the live pool, so `_backfill_keeper_flags` matching on the state's disambiguated name has no running guard.** It needs a keeper whose name `_disambiguated_names` renamed; the 2026-27 pool's only colliding group is the Elias Pettersson pair, and neither is a keeper, so it has skipped since the 2026-09-15 refresh. The six sibling skips fixed the same day were supplied by editing the loaded state; this one cannot be, because the collision is decided at load from the CSV and the test boots the app over it. Deferred because supplying it means a pool fixture with a duplicated keeper row and pointing the boot at it — `data_loader.PLAYERS_CSV` plus `main.STATE_DIR`, both module globals, inside a test that already builds a legacy state — which is a fixture of its own rather than a line. **Cost measured at defer time:** the mutant it guards (`row["PLAYER"]` for the disambiguated name) survives today; that is the whole cost, and it bites only on a pool where a keeper collides, which the 2025-26 file had twice
- [2026-08-17] [review] tests/test_browser_ui.py:524 (test_an_over_cap_adjust_salary_toast_renders_and_dismisses) — **flakes under full-suite load with `Page.evaluate: Resulting promise was garbage collected`**, a Playwright teardown error rather than an assertion failure. Observed once in a 777-test run 2026-08-17; the same test passes 3/3 in isolation and the whole browser file passes 35/35 on its own (98s), so it is contention, not a regression — nothing in that commit touched the browser suite. The mechanism fits the test: it fires `htmx.ajax` from the page and then evaluates against the resulting toast, so a slow response under load can outlive the evaluate's promise. Deferred rather than papered over with a retry: a `flaky`/rerun decorator would hide a real regression in the one suite that checks things `TestClient` physically cannot, and CLAUDE.md already names `-m "not browser"` as the draft-day escape hatch. Worth a fix only if it recurs — at which point the shape is awaiting the specific toast element before evaluating, not a blanket rerun. Note the existing closed interaction-budget entry covers a *different* problem (wall-clock assertions going flaky under load); this one has no timing assertion at all
- [2026-08-07] [refresh-drill] data_loader.py (_disambiguated_names) — the duplicate-name suffix is a workaround for a naming assumption, not a repair of it: the player NAME is still the primary key, so two players who share one are kept apart by a display string rather than by identity. A stable player id as the key would make the ambiguity structurally impossible and keep names clean on screen. Deferred by owner decision (2026-08-07), with the inventory recorded here so the follow-up does not have to rediscover it: `available_players`, `market_prices`/`model_prices`, `find_player`, ~20 endpoints taking a `player` form field, the transaction log, the trade dropdowns, and the `bo-<name>` DOM ids — plus `to_json`/`from_json`, so saved drafts and the undo chain need a migration. Large, and it touches the assign and bidding paths a live draft depends on
- [2026-09-14] [refresh-drill] tests/test_data_loader.py:477 (_fingerprint) — **a complete `team_odds.json` refresh moves no number in the fingerprint, so the documented "expect exactly one failure" step does not fire.** The only odds field is `odds_sum_percent`, and a correctly de-vigged odds file sums to ~100 *by construction* — so the one quantity pinned is the one that cannot change. Measured on the 2025-2026 → 2026-2027 refresh: all 32 clubs took new values (EDM 11.04% → 6.76%, SJS 0.17% → 4.96%), the season string changed, the pool re-priced $637.5M → $631.2M, and the suite went 1213-green with no diff to read. The invariants beside it are genuinely blind here too: `test_every_nhl_club_in_every_pool_has_cup_odds` checks that every club has *an* entry, never which. So the failure mode the fingerprint exists to catch — a refresh that silently drops or respells half the file — is caught for `players.csv` and not for this one, and a file that swapped two clubs' odds would ship silently. Deferred because the obvious fix is the mistake the three-way split removed: pinning 32 per-club values recreates the 19 exact live numbers that drowned two real bugs in a refresh diff. The shape worth considering is one line that is *derived* and still scannable — the sorted top-5 codes, or a digest of the canonical dict — which needs a decision about what a useful odds diff actually reads like, not just more numbers

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
- **The trade form needs work, and since 2026-09-13 the want is specific.** The
  owner said on 2026-09-12 that it "will need some work" and this entry was
  parked waiting for the particular complaint; the 2026-09-13 draft supplied one,
  having run **eight** `/trade-between` calls (nine player moves — one call sent
  a player each way) and one `/trade-execute` moving six. Two things, named by
  the owner:

  1. **Finding players in the lists.** The give list is 49 rows and the receive
     list is whatever the partner has; there is no way to search either, so
     picking a known player means scrolling for him. The 2026-08-15 rebuild
     (checkboxes replacing `<select multiple>`) fixed *width* and *affordance*
     and deliberately left this — "the height cap plus full-width labels is the
     measured fix; revisit only if scrolling still bites in a real break". It
     bit in a real break.
  2. **The evaluate-then-execute flow.** `/trade-execute` acts on the server's
     `last_trade_eval` rather than on the form, so `markTradeEvalStale` disables
     the submit on any change and the verdict has to be re-earned before
     Execute comes back. That is correct — it is what stops you executing a
     trade you did not evaluate — but it makes narrowing a proposal a loop of
     tick, re-evaluate, tick.

  Neither has an agreed fix yet, and the search box in particular is **not** it
  by default: point 2 is about the shape of the interaction, and bolting a
  search box onto a form that may change shape is the work that gets thrown
  away. One accepted rough edge is still on the record and still small: at
  1024px the widest Give row wants 305px against a 293px list and scrolls 12px
  inside it, and the draft runs at 1280–1600 where everything fits.

---

## Resolved

Everything that has been fixed, added or changed lives in
[CHANGELOG.md](CHANGELOG.md), newest first. It was 81% of this file and buried
the open items it shared it with.

Moving an entry there is what "done" means: delete it from **Open findings**
above and write it up under the date it landed, in the same commit as the fix.

# Backlog

The single work list for this project: deferred review findings plus forward-looking ideas.

**Open findings** are things flagged by review agents (`/grill`, `/go`, `/simplify`, etc.) that were **not** addressed in the change that surfaced them. Format:

`- [YYYY-MM-DD] [source] file:line (symbol) — finding — reason deferred`

Name the enclosing function or property in `(symbol)`. Line numbers drift every time the file above them changes — on 2026-08-05 a third of the references in this file pointed at unrelated code — and a stale line sends you somewhere wrong without saying so. The symbol survives the drift and makes the entry greppable. Templates have no symbols, so those entries carry a literal anchor from the cited line in the same position instead (`templates/partials/league_state.html:NNN (table-scroll-x)`).

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

- [2026-09-20] [plan] `trade.py:627 (execute_buyout)` — **a mid-auction drop of a player with no NHL contract cannot be recorded.** `execute_buyout` is the only removal that is not a trade, and it charges the CBA 11.4 penalty — 50% of salary stays on the cap — which is exactly wrong here: there is no contract to buy out, so the league charges nothing and the full salary comes off. Today the two are indistinguishable to the tool, and entering the buyout would leave the team carrying a penalty the league never assessed. Deferred because the owner scoped this pre-draft only (2026-09-17), where deleting the row from `players.csv` is exact, durable through `/reset`, and needs no code — Laine was handled that way. **Cost measured at defer time**, so the next person does not have to re-derive it: a new `transaction_type`, plus a new `where` threaded through `state.py:845 (_searchable)`, `state.py:908 (_locate)`, `main.py:1884 (_search_rows)` for both the label lookup and the penalty it computes, `templates/partials/search_results.html:68 (bought-out)` and `static/style.css:442` — miss any one and the player becomes findable **nowhere**, which is worse than a buyout, since that at least surfaces him; a branch in `tests/measure_replay.py:214 (_apply)`, which raises on an unknown type by design; and a new eligibility property beside `state.py:124 (can_be_bought_out)`, because a no-contract player is typically group 2/3 and the picker in `templates/partials/team_panel.html:389 (can_be_bought_out)` would otherwise keep offering him the penalising path. **Re-raised 2026-09-20 in a cheaper shape, and re-deferred on SCOPE rather than on cost** — the owner asked whether a *checkbox beside the buyout* saying "no penalty" would beat the hand edit. It is genuinely cheaper than the endpoint costed above: keeping `transaction_type="buyout"` leaves the player findable, so `state.py:845 (_searchable)`, `state.py:908 (_locate)`, `templates/partials/search_results.html:68 (bought-out)` and `static/style.css:442` need no change at all and the findable-nowhere hazard that dominates the estimate above never arises. It loses anyway, because it cannot express the only case in scope: a buyout logs a transaction, and `bake_roster_state.py:113 (require_pre_draft)` refuses to bake **any** state holding one, so a free buyout entered pre-draft never reaches the data files — and `/reset` rebuilds from `players.csv` + `fchl_teams.json` alone, so it wipes it. The checkbox would have to be re-ticked after every reset, which is precisely the failure the bake path exists to remove; deleting the row is the only expression of "gone this season" that survives one. **What it would still have to fix if the scope ever changes**, recorded here because it is a live correctness trap rather than a cost: the penalty is nowhere stored — `TeamState.penalties` is one aggregate float with no per-player breakdown, and the 50% is recomputed from salary at `main.py:1885 (_search_rows)` (the figure in the header search), `main.py:2410 (buyout)` (the success toast) and `tests/measure_replay.py:248 (_apply)` (the replay), so "this one was free" has to be stored on the `TransactionRecord` and read back at all three — miss one and the tool reports a penalty the league never assessed, which is this same defect in a new place. **The trigger to build it is a free drop happening MID-AUCTION, never the hand edit feeling tedious** — the owner has now scoped it pre-draft-only twice (2026-09-17, 2026-09-20)

- [2026-09-23] [plan] `main.py:122 (_hold_state_lock)` — **a process started while no server is running can still write the operator's live draft.** The folder lock (`CHANGELOG.md`, 2026-09-23) makes a second process refuse to start while the server holds `data/state/`, which covers the draft-day window. But nothing holds the lock while the server is down, so an ad-hoc `with TestClient(main.app)` script then writes `data/state/` exactly as before. `_save_state` rotates the previous file into `.backup`, so **two** such saves destroy both copies. The 2026-09-15 incident, nine picks from a replayed `_script()` over a state that happened to be empty, may well have been this case, and nothing records whether a server was up at the time. **Candidate guard, recorded and not built:** refuse the *derived default* folder (no `FCHL_STATE_DIR`, `STATE_DIR` never reassigned) when `uvicorn` is not in `sys.modules`. Unlike the rejected check for `pytest` in `sys.modules`, it neither fires in the suite, which redirects the folder, nor misses a bare script. Deferred for two reasons. It is a heuristic keyed on an import side effect, and it goes silently inert the day `main.py` imports `uvicorn`, so it would need its own subprocess test pinning that `import main` does not. And the owner scoped the 2026-09-22 plan to the lock. The mitigation still in force is procedural: a script that imports `main` sets `FCHL_STATE_DIR` or redirects `main.STATE_DIR` first, as `tests/measure_ceiling.py` and `tests/measure_layout.py` do. (Filed 2026-09-22 against `tests/conftest.py (isolated_state_dir)`. The write-up of the lock has what that entry got wrong.)

- [2026-09-25] [grill] `main.py:2169 (trade_evaluate)` — `/trade-evaluate` now runs ~2 MILP solves per eligible contract plus two stress solves on the event loop (~1.7s on the live draft, up from ~0.9s), so a `/bid-check` landing mid-evaluate waits that long, and `tests/test_event_loop.py`'s guard cannot see it because the solves are in `trade.py`, not `main.py` — deferred: trades happen in auction breaks with no bid in flight, and moving it to `run_in_threadpool` needs the `_state_version` discard the two scans use, because `last_trade_eval` is what `/trade-execute` executes and a pick landing mid-solve would make it stale.


**Last triaged 2026-09-22**, at the owner's request, walking every entry
against the code and the data rather than the prose. Most of the list was no
longer open work. It closed seven with no code change (`CHANGELOG.md`,
Investigated) — the stale trade without JavaScript, the `_searchable` name
collision, the keyboard-scroll gap, a legacy save's undo chain, the unmeasured
tooltips, the full-context build and the one-off browser flake — and moved the
player-id refactor to **Ideas**, since no defect stands behind it, taking the
list from fifteen to **seven**. Two of those seven had been deferred on a
premise that was false rather than stale; see the last paragraph below. The
four found small enough to fix then closed one per commit: the optimizer's
thin-pool entry on a test that asks its question at every refresh (**six**),
then the fingerprint's odds blind spot, by pinning the season and the top five
clubs (**five**), then the renamed-keeper skip, by supplying the collision in
a two-row CSV instead of hoping the pool carries one (**four**), then the
tooltip floor, whose "zero slack" turned out to be twenty-five and which could
not see the stat tiles it existed to hold (**three**). The owner then approved
all ten RFA prior-team corrections, closing the last entry waiting on them
(**two**). Under **Ideas**, the five price-model ideas moved to
`FCHL-auction-pricer/BACKLOG.md`, where the model is fit, and two wants closed
(`CHANGELOG.md`, Investigated). **2026-09-23 left it at two**: the state-folder
lock closed the draft-day half of the `isolated_state_dir` entry, and the half it
cannot see, a script run while no server is up, stays open as a narrowed entry
against `_hold_state_lock`.

**The walk before that, 2026-09-11**, also re-checked the mechanism
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
grill filed the renamed-keeper skip and the stale RFA prior teams, and closed
the 2026-09-07 corrupted-names entry, whose premise no pool in the repo still
carries (`CHANGELOG.md`, Investigated), and the converter test module, whose
every listed gap its re-review pinned. Keep
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
parked on a real event (a no-contract drop mid-auction) and on a guard nobody
has chosen to build yet (a script run with no server up, the half the 2026-09-23
lock cannot see). The `players.csv`-refresh class is **empty**: its last entry,
`_searchable`, turned out to be guarded at load all along. **Asking is a way to
expire one and so is doing it**, and the ratio is worth knowing — four of seven draft-day items
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

**Trust the measurements, not the deferral reasons.** Four entries have now been
found deferred on a diagnosis that was wrong rather than merely stale: one
claimed a `state.py` change was needed when the information was already local to
the endpoint, one claimed `/assign` needed an out-of-band response when it had
always returned `all_panels.html` by design, and the trade form's called the fix
"re-ticking the boxes" when the actual fix was to stop destroying them, and
`_searchable`'s said the loader renamed duplicate names only within the biddable
pool when it renames across the whole file — so three refreshes' worth of "zero
collisions" were measuring a guarantee, which is why they always agreed. Re-check
the mechanism before trusting "deferred because X" here; the prose is a
hypothesis unless it says what was measured.

---

## Ideas / future work

### Architecture

- **A stable player id as the primary key.** The player NAME is the key
  everywhere, so two players who share one are kept apart by
  `_disambiguated_names`' display suffix rather than by identity; an id would
  make the ambiguity structurally impossible and keep names clean on screen.
  Deferred by owner decision (2026-08-07), and moved here from **Open
  findings** on 2026-09-22 because no defect stands behind it: the suffix covers
  every row of the file, rostered and biddable alike (`CHANGELOG.md`,
  2026-09-22, Investigated). The inventory, so the follow-up does not have to
  rediscover it: `available_players`, `market_prices`/`model_prices`,
  `find_player`, ~20 endpoints taking a `player` form field, the transaction
  log, the trade dropdowns and every id `_dom_id` mints — plus
  `to_json`/`from_json`, so saved drafts and the undo chain need a migration.
  Large, and it touches the assign and bidding paths a live draft depends on.

### From the 2026-08-07 testing pass

Every item from that live run-through has landed. The corrections the closed
entries were carrying — what each want got wrong, and why the next one will hit
the same thing — moved to `CHANGELOG.md` on 2026-09-11 under "Notes kept from
closed wants", because this file holds open work and that was a hundred lines of
finished work sitting on top of it.

Two things in that batch were deliberately **not** built. The buyout picker
showing each option's scan verdict was dropped on 2026-09-22 (`CHANGELOG.md`,
Investigated); the other is still open:

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

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

- [2026-09-22] [grill] `convert_fchl_online.py:669 (main)` — **the 2026-27 `PRIOR FCHL TEAM` column came from the 2024-25 pool, so it names who held each RFA two seasons back rather than in the season just ended.** `--prior` defaults to `data/players.csv`, and when the 2026-27 file was built at `4a08a6f` that path still held the pre-2024-auction pool (`.claude/rules/data-formats.md`, "Which season each pool is"). Measured 2026-09-22: 9 of the 22 RFAs disagree with `players-25.csv`, and two are confirmed wrong against the league workbook — Luukkonen reads GVR where the 2024 sheet's pick 22 signed him to LGN at $2.4M (the export's Cap agrees), and Drysdale reads VPP where the 2025 sheet's pick 38 sent him to HSM at $0.6M (again the Cap agrees). BOT's own Strome, Mercer and Gustavsson are labelled as other teams'. It is display only — the RFA badge in Available Players and "Prior: X @ $Y" on the nomination card; no engine module reads it, since ROFR is not modelled (owner decision 2026-07-05) — but in a sealed RFA bid it is what tells the owner who can match. A proposed correction, built from the saved 2025 draft state (which matched the workbook on 131 of 139 picks), changes 10 and keeps 12: Gustavsson→BOT, Seider→GVR, Cozens→BOT, Strome→BOT, Wolf→LPT, Luukkonen→LGN, McMichael→BOT, Mercer→GVR, Kakko→SHF, Drysdale→HSM, with McMichael and Mercer on weaker evidence than the rest. Two things to know when applying it: it is a hand edit of `players.csv` (safe after the bake, which carries STATUS only), and `Player.prior_fchl_team` is frozen into the saved state, so the edit reaches the running app only through `/reset`. — deferred for the owner's confirmation of the ten. The `--prior` default is less wrong than it looks and should not simply become required: it is the dest itself, which is right for a same-season re-run (a placeholder row's team is read back out of its own PRIOR column, so a correction survives — pinned by `test_the_default_prior_reads_a_corrected_prior_team_back`) and right for a refresh exactly when `players.csv` holds the season just ended. In 2026 it did not, because the 2025 draft ran off `players-25.csv` and `players.csv` was never rolled forward. What would have caught it is the converter printing which season its prior file is — the ages alone give it away — rather than a different default.

- [2026-09-20] [plan] `trade.py:416 (execute_buyout)` — **a mid-auction drop of a player with no NHL contract cannot be recorded.** `execute_buyout` is the only removal that is not a trade, and it charges the CBA 11.4 penalty — 50% of salary stays on the cap — which is exactly wrong here: there is no contract to buy out, so the league charges nothing and the full salary comes off. Today the two are indistinguishable to the tool, and entering the buyout would leave the team carrying a penalty the league never assessed. Deferred because the owner scoped this pre-draft only (2026-09-17), where deleting the row from `players.csv` is exact, durable through `/reset`, and needs no code — Laine was handled that way. **Cost measured at defer time**, so the next person does not have to re-derive it: a new `transaction_type`, plus a new `where` threaded through `state.py:809 (_searchable)`, `state.py:872 (_locate)`, `main.py:1744 (_search_rows)` for both the label lookup and the penalty it computes, `templates/partials/search_results.html:68 (bought-out)` and `static/style.css:442` — miss any one and the player becomes findable **nowhere**, which is worse than a buyout, since that at least surfaces him; a branch in `tests/measure_replay.py:214 (_apply)`, which raises on an unknown type by design; and a new eligibility property beside `state.py:94 (can_be_bought_out)`, because a no-contract player is typically group 2/3 and the picker in `templates/partials/team_panel.html:378 (can_be_bought_out)` would otherwise keep offering him the penalising path. **Re-raised 2026-09-20 in a cheaper shape, and re-deferred on SCOPE rather than on cost** — the owner asked whether a *checkbox beside the buyout* saying "no penalty" would beat the hand edit. It is genuinely cheaper than the endpoint costed above: keeping `transaction_type="buyout"` leaves the player findable, so `state.py:809 (_searchable)`, `state.py:872 (_locate)`, `templates/partials/search_results.html:68 (bought-out)` and `static/style.css:442` need no change at all and the findable-nowhere hazard that dominates the estimate above never arises. It loses anyway, because it cannot express the only case in scope: a buyout logs a transaction, and `bake_roster_state.py:113 (require_pre_draft)` refuses to bake **any** state holding one, so a free buyout entered pre-draft never reaches the data files — and `/reset` rebuilds from `players.csv` + `fchl_teams.json` alone, so it wipes it. The checkbox would have to be re-ticked after every reset, which is precisely the failure the bake path exists to remove; deleting the row is the only expression of "gone this season" that survives one. **What it would still have to fix if the scope ever changes**, recorded here because it is a live correctness trap rather than a cost: the penalty is nowhere stored — `TeamState.penalties` is one aggregate float with no per-player breakdown, and the 50% is recomputed from salary at `main.py:1745 (_search_rows)` (the figure in the header search), `main.py:2270 (buyout)` (the success toast) and `tests/measure_replay.py:248 (_apply)` (the replay), so "this one was free" has to be stored on the `TransactionRecord` and read back at all three — miss one and the tool reports a penalty the league never assessed, which is this same defect in a new place. **The trigger to build it is a free drop happening MID-AUCTION, never the hand edit feeling tedious** — the owner has now scoped it pre-draft-only twice (2026-09-17, 2026-09-20)

- [2026-09-22] [refresh-drill] `tests/conftest.py:21 (isolated_state_dir)` — **the write-through guard on the operator's live draft is a pytest fixture, so anything that is not pytest walks straight past it.** `main.STATE_DIR` defaults to `data/state`, and `isolated_state_dir` redirects it for the suite only. An ad-hoc diagnostic script doing `with TestClient(main.app)` — the obvious way to reproduce an endpoint failure outside the suite — therefore writes to `data/state/`, and `_save_state` rotates the previous file into `.backup`, so **two** such saves destroy both copies of a real draft. Hit on 2026-09-15 while diagnosing `test_19_bid_check_changed`: nine picks from a replayed `_script()` landed in `data/state/auction_state.json` and its backup, over a state that happened to be empty. Nothing warned; the file is `.gitignore`d, so `git status` said nothing either, and it was found only by opening the file before clearing it. **Corrected 2026-09-22 by the grill, on two counts.** This entry said `main.py` hardcodes the directory "with no env override", and so did CLAUDE.md and the three `tests/measure_*.py` docstrings: false since `3eb6ed9` on 2026-09-07, eight days before the incident — `FCHL_STATE_DIR` has always been the escape hatch, and the incident is a script that did not use it. And the guard it proposed, refusing at `lifespan` to save over a state whose pool came from a different CSV, would **not** have caught that incident: the script loaded the default pool over the default state, so the CSVs agreed. The real hazard is "a process that is not the operator's server writing the operator's state", which no property of the state can see. Still deferred, because every cheap guard found so far is wrong for draft day — changing the default moves the path the live server writes, and refusing to save under `pytest` in `sys.modules` misses a bare script and fires in the suite. The mitigation in force is procedural: a script that imports `main` sets `FCHL_STATE_DIR` or redirects `main.STATE_DIR` first, as `tests/measure_ceiling.py` and `tests/measure_layout.py` do

- [2026-09-11] [grill] `tests/test_browser_ui.py:1246 (test_no_tooltip_renders_outside_the_scrollable_content)` — **the `counted >= 10` floor now sits at exactly 10**, with zero slack: the 2026-09-11 removal of the league-table `data-tip` took the measured count from 11 to 10, so the next tooltip deleted anywhere in the app fails this rather than the named `required` inventory, and the failure message ("the page must render the bid panel's four and the team panel's stat tiles") will not describe what actually happened — deferred because that is the tripwire working as designed and the STATES are deterministic, so it is not flaky; revisit only if a legitimate removal trips it, at which point the fix is to re-derive the floor from the `required` inventory rather than to lower a magic number


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
clubs (**five**).

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
parked on the owner (the RFA prior teams), a real event (a no-contract drop
mid-auction), and work small enough to simply be next. The `players.csv`-refresh class is **empty**: its last entry,
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

### test infrastructure

- [2026-09-22] [grill] tests/test_crash_recovery.py:738 (test_a_renamed_keeper_is_found_too) — **the renamed-keeper backfill test skips on the live pool, so `_backfill_keeper_flags` matching on the state's disambiguated name has no running guard.** It needs a keeper whose name `_disambiguated_names` renamed; the 2026-27 pool's only colliding group is the Elias Pettersson pair, and neither is a keeper, so it has skipped since the 2026-09-15 refresh. The six sibling skips fixed the same day were supplied by editing the loaded state; this one cannot be, because the collision is decided at load from the CSV and the test boots the app over it. Deferred because supplying it means a pool fixture with a duplicated keeper row and pointing the boot at it — `data_loader.PLAYERS_CSV` plus `main.STATE_DIR`, both module globals, inside a test that already builds a legacy state — which is a fixture of its own rather than a line. **Cost measured at defer time:** the mutant it guards (`row["PLAYER"]` for the disambiguated name) survives today; that is the whole cost, and it bites only on a pool where a keeper collides, which the 2024-25 file had twice

---

## Ideas / future work

### Price model (Layer 1)

Track these; don't implement upfront. The market layer (Layer 2) already compensates for some of them — only build one if draft-day testing shows the base model plus market layer isn't accurate enough.

- **Dynamic budget deflation** — scale model price by (remaining league budget / starting league budget) as a simple auction-phase correction. The model is currently static and does not adjust for budget depletion mid-auction.
- **Positional scarcity in the model layer** — boost model price when a position's supply/demand ratio is tight. The market layer partially handles this via demand count, but the model price itself doesn't adjust.
- **Price momentum** — rolling correction based on recent actual-vs-predicted ratios during the draft.
- **Auction position effect** — early picks tend to sell higher than the model predicts.
- **Non-linear points × team_probability interaction term.**

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

# FCHL Auction Manager

A live auction draft tool for an 11-team fantasy hockey league. During a multi-hour, 150+ pick auction, the simulator tracks all teams, computes market-adjusted bid limits, recommends nominations, provides real-time bidding advice, evaluates trades and buyouts on the fly, and recalculates the ideal roster after every transaction.

**Stack**: FastAPI + HTMX + Jinja2 + PuLP (MILP solver)

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn main:app --reload
# Opens at http://localhost:8000
```

Run tests with `.venv/bin/pytest tests/`.

## Architecture

```
Browser (HTMX)              FastAPI Server                    Engine
+-----------------+     +---------------------+    +----------------------+
| Auction control  |--->| POST /assign        |--->| price_model.py       |
| Bidding advisor  |<---| POST /bid-check     |    |       |              |
| Nomination helper|    | GET  /nominate      |    | market.py            |
| Trade evaluator  |    | POST /trade-evaluate|    |       |              |
| My team view     |    | GET  /buyout-check  |    | optimizer.py         |
| League dashboard |    | POST /team-done     |    |       |              |
+-----------------+     | POST /undo          |    | trade.py             |
        ^               +---------------------+    +----------------------+
        |                        |
        |                        v
   HTMX partial              AuctionState
   HTML swaps                (JSON on disk)
```

<!-- Pricing pipeline details in .claude/rules/pricing-pipeline.md (always loaded) -->
<!-- Tokyo Night theme rules in .claude/rules/tokyo-night-theme.md (loaded when editing CSS/HTML) -->
<!-- Data format specs in .claude/rules/data-formats.md (loaded when editing data/) -->

All state-modifying endpoints trigger: update state -> recompute market prices -> re-solve MILP -> save snapshot -> return HTML partials.

### Full endpoint reference

| Method | Route | Purpose |
|--------|-------|---------|
| GET | `/` | Main page with all panels |
| POST | `/assign` | Draft player to team (validates team, clamps salary) |
| POST | `/bid-check` | Live bidding advice (BID/CAUTION/DROP, or WIN when uncontested) |
| GET | `/nominate` | Nomination recommendations (target/drain/depth) |
| GET | `/explain/{name}` | Counterfactual: roster with vs without player |
| POST | `/trade-evaluate` | Evaluate proposed trade (ACCEPT/DECLINE) |
| POST | `/trade-execute` | Execute previously evaluated trade |
| GET | `/buyout-check?player_name=` | Preview buyout impact (BUYOUT/KEEP) |
| POST | `/buyout` | Execute buyout (50% penalty) |
| GET | `/buyout-indicators` | Lazy-load buyout dots via HTMX OOB swap |
| GET | `/solve-standings` | Replace the League State Proj estimates with real per-team MILP optima (OOB) |
| POST | `/team-done` | Toggle team drafting status |
| POST | `/undo` | Restore previous snapshot |
| POST | `/reset` | Reset to fresh state from CSV |
| GET | `/player-chart/{name}` | SVG price distribution visualization |
| POST | `/set-nominator` | Override nomination turn |
| GET | `/team-view/{code}` | Detailed team roster view |
| GET | `/team-players/{code}` | JSON player list (for trade dropdowns) |
| POST | `/toggle-bench` | Toggle player active/bench status |
| POST | `/adjust-salary` | Correct a player's salary |
| POST | `/move-to-minors` | Send a benched acquired player to minors |
| POST | `/move-to-roster` | Recall a player from minors |
| POST | `/trade-between` | Execute trade between any two teams (atomic) |
| POST | `/load-scenario` | Load a pre-baked test scenario |
| GET | `/state` | JSON state dump for debugging |

### UI patterns

- **Toast notifications**: Mutation endpoints return `HX-Trigger: {"showToast": {...}}` header. JS listener in `shortcuts.js` shows auto-dismissing alerts.
- **Buyout indicators**: A manual "Scan Roster" button fires `GET /buyout-indicators` (one MILP solve per eligible player), which returns OOB-swapped green/red dots into the placeholder dots. **The scan, the dots and the Analyzer's picker all read `team.all_players|selectattr('can_be_bought_out')` — the same expression, deliberately**, and `test_it_offers_exactly_the_eligible_set` states it as one set equality. Eligibility is a property of the contract group alone, so a group 2/3 player in the minors is a legal buyout whose salary is fully on cap; the scan read `roster_players` until 2026-08-07 and silently reported on 11 of BOT's 15 eligible players, which is indistinguishable on screen from "no buyout helps". **The dots live in `team_panel.html`'s two roster tables and nowhere else** — never add one to the Analyzer, which is a `<select>` since 2026-08-15: `_dom_id` mints one id per player, so a second copy is a duplicate id the scan swaps twice, and an `<option>` may not carry markup anyway. The picker's *empty first option* is load-bearing for a different reason — without it the top candidate is pre-selected and choosing him fires no `change` at all. Pin that one in the endpoint tests, not the browser: Playwright's `select_option` dispatches `change` unconditionally, so the harness that looks right for it cannot see the bug.
- **Exact standings are a second manual scan, and the basis marker is what makes them safe.** The League State **Proj** column is two rules: BOT's is `milp_solution.total_points`, every opponent's is an estimate in `_context` that costs no solve. `GET /solve-standings` replaces the opponents' figures with real per-team MILP optima, OOB, on a click — the buyout-scan idiom, for the same reason (measured 2026-08-17: **1262ms** on a fresh league, **259ms** in the endgame, because done teams are final and BOT is already solved and neither is asked). **Never put this on an action path.** The estimate is not a small error and was mis-bounded for nine days: the 2026-08-08 measurement compared the two rules **on BOT**, whose figure never uses the estimate, and concluded "5 points apart, same rank either way". Measured properly it runs +68 mean / +193 worst (+14.2%), overstates the *achievable* optimum for 6 of 10 opponents, and moves **9 of 10** teams in rank order — in the endgame scenario BOT's own badge read **#2 when BOT was #1**, the same class of error as the done-team projection bug. Do **not** try to fix the estimate instead: three budget-aware replacements were measured and all three are worse (mean |err| 94 against 147/176/401), because greedy-by-points spends the budget on one star and floors the rest. `exact_projections` joins `_recompute()`'s invalidation list, so **one pick returns the whole column to estimates** — and `#proj-basis` says which basis is on screen as a **count** (`exact 9/10`), never a boolean, because an Infeasible opponent keeps its estimate and absence from the dict is what makes that harmless. The marker is unconditional with only its `hx-swap-oob` conditional, same rule as `buyout_scan.html`. Unlike the buyout dots this needs no gating: all 11 `proj-<CODE>` spans render unconditionally, so no swap can miss.
- **The two manual scans compute off the event loop, and publish only if the state has not moved.** Every handler in `main.py` is `async def`, which FastAPI dispatches to the loop rather than a threadpool — correct for the mutating endpoints, and a real stall for `/solve-standings` (10 solves) and `/buyout-indicators` (~15): measured 2026-08-19, a warm `/bid-check` cost **10ms alone against 1682ms behind a roster scan**, and `/state`, which solves nothing at all, 1564ms. So each scan reads what it needs on the loop (a `deepcopy` of the state, the prices, the figure to beat), hands a **pure** `_solve_*` function to `run_in_threadpool`, and writes its result through `_publish_if_current` — also on the loop, so no worker thread ever touches a module global. The deepcopy is safety, not speed (3ms against a 78ms solve): the solver iterates `available_players` while an `/assign` can now run alongside it. **A version counter, not an emptiness check**: `_recompute()` bumps `_state_version` and already clears `exact_projections`, so empty cannot distinguish "nobody scanned" from "a pick landed mid-solve". A discarded standings solve is harmless — the column falls back to its estimate and `#proj-basis` says so — but a discarded buyout scan must return an **empty body**, because `buyout_dots.html` defaults a missing verdict to `keep` and would paint all 15 dots green, which is the 2026-08-07 "no buyout helps" failure again. `/bid-check` and `/explain` deliberately stay on the loop: `TestResponsesCannotOvertakeEachOther` pins `/explain`'s FIFO ordering, and moving it needs `hx-sync="#app:replace"` on its mount first. **Calling CBC from two threads at once is now reachable and is therefore load-bearing** — the loop solves for BOT on every pick while a scan is in flight. It is safe because PuLP names its scratch files `uuid4().hex` per solve (`pulp/apis/core.py`, `LpSolver.create_tmp_files`) and CBC is a subprocess, so the GIL is released: measured 2026-08-19, 11 concurrent solves × 3 rounds gave zero errors and zero disagreements with the serial answers, in 401ms against 1134ms. Do not treat that as free — adding `keepFiles=True` to `optimizer.py`'s one `PULP_CBC_CMD` puts every solve on the same filename and does **not** raise: a team came back `status="Optimal"` with **950** points against **1355** solved alone, a silently wrong figure wearing a rank badge. `TestTwoSolvesAtOnceAgreeWithTwoSolvesInARow` asserts the answers, which is why. A third multi-solve endpoint joins `THREADED_SCANS` in `tests/test_event_loop.py` or says why not — and it cannot simply be forgotten: `test_only_recompute_may_solve_on_the_loop` requires every caller of `solve_optimal_roster` to be a `_solve_*` (which is forced into a thread) or to name itself in `SOLVES_ON_THE_LOOP` with a reason.
- **A player name becomes a DOM id only through `main._dom_id`** (registered as the `dom_id` Jinja filter). htmx resolves an out-of-band target by **selector** — `"#" + id` into `querySelectorAll` — from a loop with no `try`/`catch`, so one id that isn't a legal CSS identifier throws and abandons **every remaining swap in the response**. Measured in Chrome: `Matt Murray (DAL)` on BOT gave 12 placeholders, **0** resolved, and a Scan button that looked like it had not finished. `players.csv` carries backticks (`Drew O`Connor` — note U+0060, which the old `replace("'", '')` did not match) and parentheses (`Tony DeAngelo (NCM)`), and `_disambiguated_names` adds ` (TEAM)`, ` (TEAM POS)` and ` (#n)` on top. The filter's sha1 suffix is load-bearing: a slug alone is lossy, and two players colliding on a derived key is the failure the name disambiguation removed. Never hand-roll the id in a template or a test — `TestNamesSurviveBecomingDomIds` checks every pool and roster name, and a hand-rolled copy in a test turned an `assert dot_id not in html` into an assertion that could not fail.
- **Atomic saves**: `_save_state()` writes to `.tmp` then `os.replace()` (POSIX atomic). Previous state kept as `.backup`.
- **Startup recovery**: `lifespan` walks current → `.backup` → fresh, and a file that fails to **parse** is renamed `.corrupt` rather than left in place — otherwise the next save rotates it over the good backup and both copies are gone. `_load_saved_state` catches broad `Exception` on purpose: at startup of a tool that may be four hours into a live auction, degrading beats failing to boot. **Only the parse decides usability.** The three `_backfill_*` calls each get their own net and are never fatal — none is load-bearing for the draft record, and folding them into the parse net meant one raise on a legacy snapshot renamed a byte-perfect draft `.corrupt` and started fresh, silently. Any degraded startup sets `_startup_warning`, which `_context` passes to `base.html` as a banner **outside `#app`** (a panel swap replaces `#app`, so an inside banner would vanish on the first pick); `POST /reset` clears it. Pinned by `tests/test_crash_recovery.py`. **On draft day**: the backup is one save behind by construction, so a recovery costs the most recent transaction — check the last pick is still there and re-enter it if not.
- **Two banners, not one.** `#startup-warning` describes *this boot* and `/reset` clears it; `#data-warning` describes *the CSV* (duplicate names the loader had to rename) and survives a reset, because the renames do. Merging them breaks both directions: a permanent data note turns the degraded-boot alarm into wallpaper — which is exactly what `test_the_happy_path_shows_no_banner` guards — and routing the renames through `_warn_at_startup` would make them vanish on a reset that repopulates them. `_data_warning()` composes at render time rather than being pushed at startup, so it always describes the pool actually loaded; booting onto a *saved* state says nothing, correctly. It reads `data_loader.loaded_disambiguations`, written **only** by `build_initial_state`, not `last_disambiguations`, which any `load_players` caller resets — a test fixture or the pre-auction runbook loading a different CSV would otherwise blank the banner for whatever ran next.
- **Undo restores by enumeration, not by a list.** `rollback_to` (which `restore_snapshot` delegates to) loops over `fields(self)` and copies everything except `_snapshots` — never re-introduce hand-written `self.X = restored.X` lines, because a field added to `AuctionState` would silently stop being restored, on the one operation with nothing behind it. The `_snapshots` skip is load-bearing: snapshots are written with `include_snapshots=False`, so the restored chain is always empty and copying it makes `Ctrl+Z` work exactly once per session. `to_json`/`from_json` are still hand-written, so two guards in `tests/test_state.py::TestSnapshotFieldsCannotDrift` cover them — one structural (fields == JSON keys), one behavioural (every field survives a round trip); they catch different mutants and neither is redundant.
- **A new mutating `@app.post` either snapshots or joins `NO_SNAPSHOT_NEEDED`** in `tests/test_state.py::TestEveryMutatingPostTakesASnapshot`, in the same commit, with the reason. It walks `main.py`'s ast, so forgetting fails the suite instead of surfacing as a wrong `Ctrl+Z` four hours into a draft. `save_snapshot()` and `commit_snapshot()` both count; `capture_snapshot()` deliberately does not, because capturing without committing snapshots nothing.
- **An endpoint that can reject snapshots on the success path only.** `save_snapshot()` captures *and* commits, so calling it before you know the operation will succeed is not free: it evicts the oldest entry once the chain is at `MAX_SNAPSHOTS`, and the `restore_snapshot()` that used to follow on the error path pops from the *other end* — so a rejected request read as a no-op while quietly destroying a real undo step (measured: depth 50 → 49, oldest gone). Use `capture_snapshot()` → attempt → `commit_snapshot(before)`, and `rollback_to(before)` — never `restore_snapshot()` — if the operation can mutate before it raises. `restore_snapshot()` is `Ctrl+Z`, and spending a chain entry is what it means. `send_to_minors` and `recall_from_minors` validate before mutating and so need no rollback at all; `execute_trade` strips both rosters before adding to either and very much does. **A rollback test needs a failure that is genuinely partial** — the first version gave `/trade-execute` one player, whose removal raised before anything moved, and passed against a build with no `rollback_to`.
- **Undo tests need an empty snapshot chain**, which is why they use a function-scoped `client` shadow. With a chain carried over, `/undo` pops *somebody else's* snapshot and a broken endpoint looks restored — and the shared chain can come from the test's own setup, not just from earlier tests: `test_undo_reverts_move_to_minors` has to bench a player first, `/toggle-bench` snapshots, and pre-bench has the same roster and minors counts as post-bench, so a counts-only reading passed against a `/move-to-minors` that had stopped snapshotting entirely. **Read something that separates the two states**, not just the thing the endpoint moved.
- **Keyboard shortcuts**: exactly two, both in `static/shortcuts.js` — `Ctrl/Cmd+Z` (undo) and `N` (nomination recommendations), each inert while focus is in an INPUT/TEXTAREA/SELECT. The navbar's **⌨ Shortcuts** button opens a `<dialog>` listing them, and each row carries `data-shortcut-key` matching the key the handler binds. `TestShortcutsModal` asserts those two sets are equal **in both directions**, so adding a shortcut without documenting it fails the suite — a shortcut list that drifts is worse than none, because it gets believed. Add a new shortcut and the modal row in the same commit.
- **A partial mounted in two places carries no id of its own.** `counterfactual.html` and `player_chart.html` are bodies; each mount owns its id and its own empty state. htmx resolves `hx-target` by id and takes the *first* match without complaining, so a body that carries the mount id puts two copies in the document and swaps land in the wrong column — and an `innerHTML` swap nests the response's copy inside the mount, duplicating it even on a quiet page. Close buttons use `this.closest('.the-card').remove()`, never `getElementById`, for the same reason — and **the direction you test it from matters**: `all_panels.html` puts `.area-auction` before `.area-players`, so the bid panel's copy is already first in document order and closing *that* one cannot distinguish `closest()` from a document-wide query (measured 2026-08-14 — the mutation sailed through). Close the other mount. A close button must also leave its **mount** standing: `getElementById('explanation').remove()` satisfies every "the right card went away" assertion while deleting the target every future swap lands in. `tests/test_htmx_interactions.py` guards id uniqueness within `GET /`; the cross-fragment case only exists in the assembled DOM, so it lives in `tests/test_browser_ui.py`.
- **Dismiss-on-interaction removes the element on `htmx:afterRequest`, never on click.** htmx **aborts an in-flight request whose triggering element leaves the DOM**, so removing a card when its own button is clicked cancels the request that button exists to send. `shortcuts.js` removes `.nomination-pick` on `afterRequest`, gated on `event.detail.successful` so a failed request leaves the recommendation on screen (`/nominate` is the only way back). It removes **only** the half acted on: per the CBA a nomination turn is 1 RFA + 1 UFA and an RFA sale *keeps the turn*, so the other half is the next thing needed, not clutter. Client-side on purpose — `/bid-check` deliberately does not touch the nomination panel, and an out-of-band swap would re-couple exactly what the panel split separated. Pinned by `tests/test_browser_ui.py::TestMidBidClutterCanBeDismissed`, whose bid-panel assertion is what catches the abort.
- **`#bid-advice` is the bid panel's "what the advisor says" slot, and both branches of `bid_panel.html` own it** — the verdict block when there is advice, the not-found note when `/bid-check` was given a name that isn't in the pool. They are mutually exclusive, so the document still holds exactly one; reusing the id is deliberate and load-bearing. The price input carries `hx-select="#bid-advice"` with `hx-swap="outerHTML"`, and **an unmatched `hx-select` on an outerHTML swap DELETES the target** — htmx swaps the empty selection in. Measured in Chrome 2026-08-07 against the pre-fix template: after the player left the pool, `#bid-advice` count went to **0** while `#bid-panel`, `#bid-form` and `#bid-price` all survived, with no console error. So the verdict block silently disappeared mid-bid and left a half-built panel. (Both `BACKLOG.md` and an earlier draft of this bullet said it "swaps nothing at all" — that was reasoned, not measured, and it is what kept the entry deferred for a month on the grounds that silence was the lesser evil.) The other way in is more common: the Start Auction field is free text (`required` plus a datalist, *not* readonly), so a typo swapped the whole panel back to a blank form and the name you typed simply vanished. Any future branch of that template answers in `#bid-advice` or answers nowhere.
- **Responsive layout**: `.auction-grid`, 1-col (mobile), 2-col (768px+), 3-col (1024px+). **The draft runs on a 1280–1600px laptop** — that is the width to check, not 1920. `tests/test_browser_ui.py::test_the_grid_never_overflows_its_own_width` pins containment at 1024/1280/1600; `tests/measure_layout.py` is the instrument for asking *which* element forced a column wide (it redirects `main.STATE_DIR` to a temp dir first, because `main.py:57` hardcodes the operator's real state with no env override).
- **A multi-pick player list is a `.choice-list` of checkboxes, never a `<select multiple>`.** Both trade forms used one until 2026-08-15, and both failure modes are worth knowing. **Width**: a select is sized by its column, not its content, so it *clips silently* — measured at 1280, the four controls rendered 120–183px against labels wanting 229–316px, putting the salary and points off the edge on every row. **Affordance**: a plain click discards every prior selection, and in a 3-row window onto 49 options nothing on screen says so. The replacement stacks its column (that is what buys the width — two columns of the 521px panel at 1600 is still only 236px), wraps each checkbox in its `<label>` so the row text becomes its accessible name, names the **group** with `role="group"` + `aria-label`, and shows a running `N selected · $X.XM`. **Checkboxes sharing a `name` serialize identically to a multi-select**, which is why `/trade-evaluate` (`form.getlist`) needed no change and its tests are the equivalence proof. `white-space: nowrap` is safe only because the list scrolls: `overflow-y: auto` forces `overflow-x` to `auto` (see two bullets down), so a long row can never set its grid column's min-content. **One JS builder (`loadTradeChoices`) fills both fetched lists** — there were two copies differing only in the value and whether the label carried points, and the duplicated `(M)` suffix was a real finding because deleting either left the suite green.
- **Never a bare `1fr` in a hand-written grid — always `minmax(0, 1fr)`.** `1fr` *is* `minmax(auto, 1fr)`, and per css-grid §6.6 an item takes a content-based automatic minimum whenever it spans a track whose **min sizing function** is `auto`: the widest panel silently sets its own column's floor and the others divide the remainder. Measured at 1280px before the fix, `.auction-grid`'s tracks were **292 / 991 / 585** against 409 for an equal third, and `#team-panel` — your cap, your roster, the buyout dots — began at **x=1310**, off-screen, on the width the draft is run at. Do **not** also add `min-width: 0` to the `.area-*` children: §6.6 keys on the track, not the item (that is the *flexbox* rule, §4.5), so it is a redundant second source of truth. Every Tailwind grid utility already complies (`grid-cols-3` → `repeat(3, minmax(0, 1fr))`); `.auction-grid` was the one hand-written grid that did not.
- **A table wide enough to overflow its column needs its own scroll wrapper** (`.table-scroll-x`, or `.scroll-container` when it also wants the height cap and sticky `thead`). `minmax(0, 1fr)` shrinks the *item* box and does nothing to its descendants, which keep their min-content and `overflow: visible` — measured with the minmax fix and no wrapper, `#league-state`'s 12-column table painted out to x=1405 **across** `#team-panel`, under an opaque card, with no scrollbar offering it. That is strictly worse than the original bug, so the two halves ship together. Also note nothing looks wrong either way: `all_panels.html`'s inline `overflow-y: auto` forces `overflow-x` to compute to `auto` (CSS Overflow §3.2), so overflow hides behind the **grid's** scrollbar and no **page** scrollbar ever appears. Never "fix" that with `overflow-x: hidden`, which makes the panel unreachable rather than merely off-screen.
- **No CDNs**: htmx, DaisyUI and Tailwind are vendored in `static/vendor/` — the app must run with the network down, because every panel and every Assign is an htmx request. `tests/test_offline_assets.py` fails any template that loads an asset from another origin. Also: **do not add a CSP** without reading `static/vendor/README.md` — the Assign button's `hx-vals='js:{...}'` needs htmx's eval path.

## Auction rules (from CBA)

- UFA: circular bidding, $0.1M increments, drop out = permanent for that player
- RFA: secret bids, prior team can match (ROFR)
- Combo: 1 RFA + 1 UFA per nomination turn. The nomination pointer advances only when the UFA half sells (an RFA sale keeps the turn).
- Min salary $0.5M, max $11.4M
- Roster: 24 active (playing: 12F + 6D + 2G, bench: 4 any position). Teams can draft beyond 24 -- extras go to minors with salary fully on cap. Teams can also finish with fewer than 24.
- **Only the starting lineup scores**: weekly points come from the best 12F/6D/2G. Bench players contribute nothing to the total -- they are insurance.
- Snake draft for nominations
- Trades allowed during auction breaks
- Buyouts (CBA Article 11.4): player removed, 50% salary penalty remains on team's cap. ANYONE can be bought out -- keepers and fresh draftees alike.
- Teams can voluntarily stop drafting before filling all 24 spots

### Owner decisions (2026-07-05)

- 14F/7D/3G roster shape is a **soft preference** (good backups: 2F/1D/1G), not a constraint -- encoded as `BACKUP_TARGETS`/`BACKUP_BONUS`/`BENCH_WEIGHT` in config.py. The MILP maximizes starting-lineup points.
- RFA sealed bids are NOT separately modeled: run them like a regular auction and bid the advisor's current optimal bid. No ROFR logic in the tool.

### Owner decisions (2026-08-06)

- The league **commissioner software refuses any bid that would leave a team unable to fill a full roster**. So `remaining_budget < spots_remaining * MIN_SALARY` is unreachable through legal bidding, and the MILP's `== spots` constraint is correct rather than over-strict — don't "fix" it.
- **Going over the cap warns, it does not refuse.** The league permits temporary over-cap states that get resolved by buyouts, so every endpoint that can raise a team's cap load executes and returns a warning toast naming the team and the overage — `/trade-between`, `/trade-execute`, `/assign`, `/adjust-salary`, `/move-to-roster`. Use the shared `_cap_overages()` helper; any new cap-raising endpoint joins the list.
- Drafting past 24 **auto-routes to the minors** rather than being blocked, so a live sale never gets stopped by the tool.

### Owner decisions (2026-08-07)

- **Two team keys in the template context, and they mean different things.** `viewed_team` is the roster on screen and is read by `team_panel.html` alone; `team` is always BOT and is what the Trade "I Give" list and Buyout Analyzer act on. **Never point a panel other than `team_panel.html` at `viewed_team`**: that is the 2026-08-05 leak that put an opponent's players in BOT's trade form, and `TestPanelContextIsolation` exists to catch it.
- **The view lives in `main._viewed_team`, not in the request.** `_context` reads that global; `GET /team-view/{code}` and `_view_team()` are the only things that write it (the latter from `/assign`, `/undo`, `/buyout`, `/reset` and `/load-scenario` — see below), and every other endpoint preserves it by doing nothing. Endpoints used to carry a team code through `_panels_viewing()`, which failed open — the ones that forgot (`/team-done`, `/trade-execute`, and the error branch of all five roster-edit endpoints) silently threw you back to BOT. Do not reintroduce a per-request view. Do not move it onto `AuctionState` either: there it would serialize into the save file and `/undo` would restore a *view*, which is not a draft action.
- **A test that exercises the view must open the panel first** (`GET /team-view/SRL`), because that is now the only thing that sets it — and it is also the only way the edit happens for real, since every roster-edit control renders inside `team_panel.html`. Posting an edit for an opponent no longer implies you are looking at them, which is how the move to a global silently disarmed `TestPanelContextIsolation`: with the view still on BOT, the leak mutant leaked BOT into BOT and the guard passed.
- **The view follows whichever roster the action changed** — `_view_team(code)`, which replaced an unconditional `_view_my_team()` on 2026-08-11. **`/assign` points it at the buyer** (owner decision 2026-08-08, amending 2026-08-07): on your own pick that is still BOT, which is the only case the original reasoning was ever about — reading an opponent's Cap Used as yours at the moment a pick of *yours* lands. On an opponent's pick nothing of yours moved and the panel that just went stale is theirs, so it swaps to them; `team_panel.html` renders the **← My Team** link exactly then, and League State still carries BOT's budget in the same response. Success path only: a *rejected* assign is not a draft action, and the error branches return before the write. `/buyout` passes `MY_TEAM` because `execute_buyout` can only touch BOT; `/reset` and `/load-scenario` pass it because they replace the world. Moving the view on a pick is only safe because of the two bullets above — `viewed_team` reaches `team_panel.html` and nothing else, and `team` stays BOT. Note `/assign` returning `all_panels.html` is **correct** and is not the mistake the next bullet's `/team-view` rule describes: the sale ends that bidding session, so replacing `#bid-panel` is the point. A backlog entry asserted the opposite for three days and that is what kept this deferred.
- **`/undo` mirrors the view policy of the action it reverted**, read off the same `pre_txn[-1]` the toast already uses — no `state.py` change and no `restore_snapshot` reporting what it undid, which is the other thing that kept this deferred. A reverted `draft` or `buyout` points the view at `t.team_code`; **everything else leaves it alone.** For a buyout that code *is* `MY_TEAM` (`trade.execute_buyout` is BOT-only), so the two are one branch, not two. Trades change two rosters, so there is no single answer and both forward endpoints deliberately touch nothing. A change-log undo leaves it alone because the roster-edit endpoints do — and because **`team-done` is a `ChangeRecord` kind too**, so mirroring `pre_chg[-1].team_code` would swap the panel to an uninvolved third team, exactly what the 2026-08-07 fix removed (`test_marking_another_team_done_does_not_snap_the_panel_back`). The gap that accepts: edit an opponent, navigate away, then `Ctrl+Z`, and the view stays put — your `/team-view` click is newer information than the log.
- **Allowlist `transaction_type`, never denylist it, and `_view_team` validates.** The real vocabulary is `draft` / `trade_out` / `trade_in` / `trade` / `buyout`; `state.py`'s field comment said `trade_give`/`trade_receive` — two values the code has never emitted — until 2026-08-11, and a first draft of the `/undo` rule was reasoned against it. `/trade-between` logs `team_code` as `f"{source}→{dest}"`, so that field is **not always a team code**: a denylist that missed one string would point the view at `"SRL→MAC"`. **The Logs panel is the one deliberate exception, and it inverts the reasoning rather than ignoring it**: `logs_panel.html` splits the log `draft` vs *everything else*, because a record matching no tab disappears from the log entirely, which is worse than one landing in the wrong tab — so the two lists always sum to `len(transaction_log)` and a new type is visible by default. The `"SRL→MAC"` hazard is handled where it actually bites, in `_log_team_link.html`, which renders a `/team-view` link only when `row.team_code in teams` and plain text otherwise. `_view_team` therefore ignores a code that is not in `auction_state.teams`, giving both writers of the global one contract (the same rule as `/team-view/FAKE`) and making "`_viewed_team` is always a live team code" a real invariant. `_context`'s `teams.get(_viewed_team, team)` fallback stays as belt-and-braces rather than as the mechanism: it is silent, and it renders BOT's roster *and* BOT's Scan gate from the same fallback object, so a dead code would look completely normal on screen while every later `/team-view` no-op'd on top of the garbage. The two guards cover different mutants, and **each needs its own test** — measured 2026-08-11, not reasoned. Widening the allowlist is caught only by the `/trade-execute` case, whose logged codes are real team codes; removing the guard is caught only by `test_the_view_is_always_a_live_team_code`. The `/trade-between` case catches **neither alone** — with the guard in place a widened allowlist no-ops on `"SRL→MAC"`, and with the correct allowlist a missing guard is never handed a bad string — it fires only on the *combination*. An earlier draft of this bullet claimed it pinned the guard by itself; it does not.
- **An unknown team code changes nothing.** `/team-view/FAKE` leaves the view where it was rather than falling back to BOT, so a stale link cannot move your panel.
- **Buyout dots are BOT-only.** `_solve_buyout_indicators` scores every hypothetical against BOT's MILP total, so the scan cannot answer anything about an opponent; their panel renders no dot placeholders rather than ones that stay grey forever. The Scan button is gated to match, on the derived `buyout_dots_on_screen` boolean — a DOM fact about whether the OOB swap targets exist, deliberately not `viewed_team`, which the bullet above forbids that template from reading. Ungated, every one of its 11 OOB swaps missed and htmx logged `htmx:oobErrorNoTarget`.
- **`/team-view` returns the team panel plus out-of-band fragments** (`team_view_response.html`). It must not return `all_panels.html` — that replaces `#bid-panel` and destroys the bidding session, which lives only in the DOM. So anything outside the team panel whose rendering depends on *which* team is on screen has to come back OOB, one fragment at a time; today that is `buyout_scan.html`, and a second one joins the list rather than widening the swap. Its wrapper div is unconditional and only the `hx-swap-oob` attribute is conditional (`scan_oob`): a swap target that disappears with its contents can only be swapped one way, which is exactly the bug — the Scan button vanished on the way to an opponent and never came back.

## Key design decisions

| Decision | Why |
|---|---|
| Three-layer pricing | Model alone ignores budget constraints. Market layer ensures bids reflect reality — always for the bid advisor, and for the MILP's planning prices only once the league has actually spent its cap (measured: `tests/measure_ceiling.py`). |
| Market ceiling from exact budgets | Perfect visibility during draft. Use it. |
| "Team done" toggle | 3+ teams finish early per draft. Their dead budget distorts market calculations if not excluded. |
| Trade eval via hypothetical MILP | Same optimizer, just run on a cloned state. No new algorithm needed. |
| Buyout as penalty math | CBA rule: 50% stays on cap. Simple to model: remove salary, add penalty. |
| PuLP + CBC | Fast enough for ~200 binary vars. CBC bundled. |
| FastAPI + HTMX | Partial updates, no full-page re-runs. Single-page layout -- no tab switching. |
| JSON snapshots for undo | Simple, crash-safe, human-readable. |
| Term not tracked | Nobody caps out. Irrelevant. |

## Design rationale

This app replaced a Streamlit tool that was used for a live draft and found wanting. Every problem below drove a specific architectural choice -- don't undo one without knowing which problem it re-opens.

| Problem in the old Streamlit app | Root cause | How this app fixes it |
|---|---|---|
| App got slower as the draft progressed | Streamlit full re-runs on every interaction | HTMX partial updates, no re-runs |
| Had to tab between pages constantly | Multi-page Streamlit layout | Single-page multi-panel layout |
| Editing a cell meant edit -> wait -> save -> wait -> switch tab | Streamlit `data_editor` widget | Single `POST /assign` endpoint |
| Red/green/yellow light was confusing | Z-score deviation from mean -- not intuitive | Replaced with max bid from the MILP. One number. |
| Mediocre players got "good value" ratings, rare players didn't | Z-score treats players independently | MILP plans the whole roster. Scarcity captured by the market layer's demand count. |
| Optimizer page required a manual refresh | Streamlit tab isolation | Optimizer runs after every action, always visible |
| "What if I go slightly over?" was unanswerable | No marginal analysis | Counterfactual shows the exact impact of any price |
| Started in deficit; the "value overbid" feature was useless | Assumed a budget surplus | MILP works from any starting position -- deficit or surplus |
| Couldn't evaluate trades fast enough | No trade UI | Dedicated trade evaluator with one-click evaluation |
| Done teams inflated market prices | No concept of team completion | `is_done` toggle excludes them from market calculations |
| A competitor ended up with more points | Z-score optimized $/point, not total points | MILP maximizes projected starting-lineup points |

## Development workflow

Verification loop for every change:

1. Make changes
2. Run tests: `.venv/bin/pytest tests/ -v`
3. Fix any failures before moving on
4. Before committing: run full test suite

```bash
pytest tests/ -v              # Run all tests
pytest tests/test_market.py   # Run specific module tests
pytest tests/ -m "not browser"  # Skip the Playwright tests
```

### Browser tests

`tests/test_browser_ui.py` drives the installed Google Chrome via Playwright,
for the handful of things `TestClient` physically cannot answer: where an
element sits on screen, whether a trigger re-fires, whether a click lands.

```bash
.venv/bin/pip install -r requirements-dev.txt   # one-off; NO `playwright install`
```

`channel="chrome"` uses the system browser, so nothing is downloaded. The file
`importorskip`s, so a checkout without the dev requirements still runs the full
suite green — **keep it that way**: playwright must not become required, and it
must never appear in `requirements.txt`, which has to install with the network
down on draft day.

Add a test there only if the endpoint tests genuinely cannot cover it; they are
~100x faster. If one goes flaky mid-draft-prep, `-m "not browser"` is the
escape hatch rather than deleting it.

## Testing

TDD. Key validations:

- Price predictions match Colab notebook
- Market ceiling <= opponents' physical max; bid rec <= market ceiling
- "Done" teams excluded from market calculations
- MILP produces valid rosters (positions, cap compliance)
- Trade evaluator: accept trade iff post-trade points > pre-trade points
- Buyout: penalty correctly computed, freed cap space = 50% of salary
- State serialization round-trips cleanly
- Endpoints update state correctly

Shared non-fixture test utilities live in `tests/helpers.py` (`squeeze`,
`toast_of`, `assign`, `a_buyout_candidate`), not in `conftest.py` — that file is for fixtures. Import
from there rather than copy-pasting; `squeeze` reached three copies before it
was folded in.

**Never hard-code a player name in a test.** Derive the target from the loaded
state by the ROLE it needs to play — BOT's worst points-per-dollar keeper, the
top available forward, the first two names in the pool — because `players.csv`
is replaced before every draft and a literal name silently stops matching.
Draft picks go through `helpers.assign`, which fails at the pick: `/assign`
answers **200 with a toast** when it rejects, so `assert r.status_code == 200`
passes on a pick that never happened, and the 2026-08-07 drill saw one missing
name surface as `assert 24 == 25` three tests downstream, naming neither the
player nor the reason.

**Refreshing the data.** `data/players.csv`,
`data/goalie_projection_stats.csv`, `data/model_params.json` (+ the matching
`tests/fixtures/auction_predictions_current.csv`) and `data/team_odds.json`
move together from the pricer repo. Then:

1. `.venv/bin/pytest tests/ -q` — expect **exactly one** failure,
   `TestDataFingerprint`, with a field-by-field diff of what changed.
2. `FCHL_WRITE_FINGERPRINT=1 .venv/bin/pytest tests/test_data_loader.py -k fingerprint`
   rewrites `tests/fixtures/data_fingerprint.json` and **fails on purpose**.
3. `git diff` that file. A pool that halved, a team that vanished, or a
   nomination order that shuffled is a data problem, not a test problem.
4. Re-run without the variable to go green, then commit the data and the
   fingerprint in the same commit.

Any *other* failure is a real one. `tests/test_data_loader.py` is split three
ways so this holds: loader **rules** run against `tests/fixtures/players_sample.csv`
(ours, never refreshed), live data is checked only by **invariants**, and the
fingerprint is the single place a number is pinned. It used to assert 19 exact
live numbers, and a refresh drill drowned two real bugs in them.

**Take `client` from `conftest.py`; don't declare your own.** It is
function-scoped and resets the auction before each test, over a session-scoped
`_app_client` transport that pays the lifespan once (a naive per-test
`TestClient` costs 221ms against 107ms for a reset alone). Files whose tests are
a deliberate *sequence* — `test_dry_run.py`'s 40-pick auction,
`test_auction_draft.py`, the numbered flow in `test_trade_buyout_undo.py` —
shadow it with a module-scoped one and must be listed in
`tests/test_fixture_scopes.py::SEQUENTIAL_BY_DESIGN` with the reason, plus "ON
PURPOSE" in the fixture docstring. Anything else declaring a module-scoped
`client` fails that guard. The coupling it removes is not theoretical: it let
`TestPanelContextIsolation` keep passing against a reproduction of the
2026-08-05 leak, and let an undo test keep passing against an endpoint that had
stopped snapshotting, because a shared **snapshot chain** let `/undo` pop
somebody else's. Both were caught by mutation testing, not by the suite.

**A test must be able to fail.** Before claiming one covers something, break the
thing it claims to cover and watch it go red. Three tests in this suite asserted
nothing for months (`len(...) >= 0`, a `pass`-body loop, `status_code in (200,
404, 422, 500)`) and every one of them read as coverage. When the mutation
doesn't fail the test, either the assertion is wrong or the test is aimed at the
wrong operation — the stress ownership invariant could not fail under `/assign`
at all, and only became real once it also ran after `/undo`.

**A mutation that applied to nothing is not a passing test.** A scripted mutant
whose anchor missed — shell escaping, a wrapped line, indentation one level off —
runs the suite against the *unmutated* file and prints green, which reads exactly
like coverage. It has happened four times here (2026-08-17, 2026-08-18), each
time on a multi-line anchor. Assert the patch replaced **exactly one** site
before running the suite, and be suspicious of any mutant that dies in no test at
all: the likely explanation is that it was never applied.

## Code conventions

- Python 3.12, type hints on signatures
- All money in millions (4.6 = $4.6M)
- Market-adjusted prices everywhere in optimizer -- never raw model prices
- Flat module layout, no nested packages
- Comments explain WHY not WHAT

## Things Claude should NOT do

- Don't skip error handling
- Don't commit without running tests first
- Don't make breaking API changes without discussion
- Don't edit `data/model_params.json` manually (generated by pricer repo)

## Self-improvement

After every correction or mistake, update CLAUDE.md or the relevant rules file with a rule to prevent repeating it.

## Deferred findings

When `/grill`, `/go`, `/simplify`, or any review agent flags an issue that is **not** addressed in the current change (out of scope, judgment-call skip, valid-but-deferred refactor), append it to `BACKLOG.md` at the repo root. Don't drop it on the floor — even if you decide not to act on it now, the user should be able to see what was flagged and triage it later.

**Two files, and the split is what "done" means.** `BACKLOG.md` holds only open work — findings and ideas. `CHANGELOG.md` holds everything fixed, added or changed, newest first, grouped by the date it landed (no version numbers: this project ships straight to `main` and has never cut a release). Resolving a finding means deleting it from `BACKLOG.md` and writing it up in `CHANGELOG.md`, **in the same commit as the fix**. The write-ups are deliberately long — what was actually wrong, what was measured, and what the original entry got wrong, because several findings sat deferred for weeks on a diagnosis that turned out to be incorrect. `### Investigated` is for work that closed with **no code change**; filing a not-a-bug under *Fixed* misrepresents it, and deleting it invites the next person to rediscover the same non-problem. The split happened 2026-08-07, when Resolved was 81% of `BACKLOG.md` and buried the 12 open items it shared the file with.

`tests/test_backlog_refs.py` reads **both** files. It went quiet-green the moment the resolved entries moved out — still passing, checking a fraction of what it had — so `test_both_docs_are_still_being_read` now guards the list itself. Note `CHANGELOG.md` normally contributes zero `file:line` references (resolved entries cite commits), so nothing else would notice if it were dropped.

Format per entry:

```
- [YYYY-MM-DD] [source] file:line (symbol) — finding (one sentence) — reason deferred
```

Example (`NNN` stands in for the real line — an example carrying a live line number would rot the same way real entries do, and it is the thing people copy): `- [2026-05-02] [simplify] main.py:NNN (move_to_minors) — save_snapshot runs before validation; full JSON round-trip on rejected requests — pre-existing pattern across endpoints, fix would be cross-endpoint refactor`

**Always name the enclosing function/property in `(symbol)`.** Line numbers drift whenever anything above them changes; on 2026-08-05 a third of this file's references pointed at unrelated code. The symbol survives the drift and keeps the entry greppable. Templates have no symbols, so those carry a line only.

Before appending, scan `BACKLOG.md` for an existing entry covering the same symbol + finding — update the date instead of duplicating. When a change shifts line numbers in a file the backlog references, re-anchor those entries in the same commit.

## Working with plan mode

- Start every complex task in plan mode
- Pour energy into the plan so implementation can be done in one shot
- When something goes sideways, switch back to plan mode and re-plan -- don't keep pushing
- Use plan mode for verification steps too, not just for the build

## Commit discipline

- After each step in a plan is executed, do a `/quick-commit`
- After each issue resolved during a `/grill`, do a `/quick-commit`
- Keep commits small and atomic -- one logical change per commit

## Slash commands

| Command | Description |
|---|---|
| `/dev` | Start the FastAPI dev server and verify it responds |
| `/go` | Verify, simplify, and commit -- the ship sequence |
| `/quick-commit` | Stage all changes and commit with a descriptive message |
| `/test-and-fix` | Run tests and fix any failures |
| `/grill` | Adversarial code review -- don't ship until it passes |
| `/techdebt` | End-of-session sweep for duplicated and dead code |

## Subagents

| Agent | Purpose |
|---|---|
| `pre-auction-check` | Draft-day readiness runbook: data, state, solver, UI |
| `solver-checker` | Audit MILP formulation and pricing-layer correctness |
| `verify-app` | Validate the build and thoroughly test the app works |
| `code-simplifier` | Simplify code after Claude is done working |
| `code-architect` | Design reviews and architectural decisions |
| `staff-reviewer` | Review plans and architectures as a skeptical staff engineer |

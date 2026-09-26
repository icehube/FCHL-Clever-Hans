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

**A what-if against the live draft is a second server on a copy of the state,
not a feature**: `cp -r data/state data/state-whatif && FCHL_STATE_DIR=data/state-whatif .venv/bin/uvicorn main:app --port 8001`.
The live draft and its undo chain are untouched, `.gitignore` covers the copy,
and `rm -rf data/state-whatif` ends it. Prefer it to `/load-scenario`, whose
round trip hands the live draft back with an **empty** undo chain.

**The pool is selectable, and the saved state follows it.**
`data_loader.PLAYERS_CSV` defaults to `data/players.csv` and is overridden by
`FCHL_PLAYERS_CSV`; `main._default_state_dir()` then derives
`data/state-<stem>` instead of `data/state`. That derivation is safety, not
convenience: `lifespan` reads the saved state **before** it reads any CSV, so an
alternate pool sharing `data/state/` would load the real draft's JSON, backfill
it from the wrong file, and save over it — the same write-through
`tests/conftest.py` exists to stop pytest doing. `FCHL_STATE_DIR` overrides the
directory explicitly, both are logged at startup, and `.gitignore` covers
`data/state*/`. The folder lock (Startup recovery, below) does not make the
derivation redundant: it stops a second process only while the first is still
running. The path is a **module global, not a default argument**: a
default binds at import and could not be monkeypatched. Never export
`FCHL_PLAYERS_CSV` while running pytest — `TestDataFingerprint` reads the global
and fails, correctly.

`data/players-23.csv` (the 2023 snapshot) is in an older schema and must go
through `convert_legacy_players.py` first; the trap is that it encodes blank
`STATUS` as `"0"`, which `load_players` drops **silently**, giving an empty pool
that looks exactly like a finished draft. It has no `NHL TEAM` column either, so
the converter joins one from the current `players.csv` by normalized name —
refusing to guess when two players share a name, and writing only real NHL club
codes, because the 2024-25 `players.csv` put the FCHL placeholder `UFA` in that
column on 9 rows (no pool in the repo carries it today, and the filter stays for
the export after next). See `.claude/rules/data-formats.md` for the full conversion rules and what
the still-missing columns cost the price model.

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
| GET | `/explain/{name}` | Counterfactual: roster with vs without player (`?price=` re-solves at the live bid) |
| POST | `/trade-evaluate` | Evaluate proposed trade (ACCEPT/EVEN/DECLINE, plus the overpay stress line) |
| POST | `/trade-execute` | Execute previously evaluated trade |
| GET | `/buyout-check?player_name=` | Preview buyout impact (BUYOUT/KEEP) |
| POST | `/buyout` | Execute buyout on any team (50% penalty) |
| GET | `/buyout-indicators` | Lazy-load buyout dots via HTMX OOB swap |
| GET | `/solve-standings` | Replace the League State Proj estimates with real per-team MILP optima (OOB) |
| POST | `/team-done` | Toggle team drafting status |
| POST | `/undo` | Restore previous snapshot |
| POST | `/reset` | Reset to fresh state from CSV |
| GET | `/player-chart/{name}` | SVG price distribution visualization |
| GET | `/team-view/{code}` | Detailed team roster view |
| GET | `/team-players/{code}` | JSON player list (for trade dropdowns) |
| POST | `/toggle-bench` | Toggle player active/bench status |
| POST | `/adjust-salary` | Correct a player's salary |
| POST | `/move-to-minors` | Send a benched acquired player to minors |
| POST | `/move-to-roster` | Recall a player from minors |
| POST | `/trade-between` | Execute trade between any two teams (atomic) |
| POST | `/load-scenario` | Load a pre-baked test scenario |
| GET | `/find-player?q=` | Platform-wide player finder (where he is + the money) |
| GET | `/nhl-odds` | Cup odds per NHL club + how many of each are left (navbar modal) |
| GET | `/state` | JSON state dump for debugging |

### UI patterns

- **Toast notifications**: Mutation endpoints return `HX-Trigger: {"showToast": {...}}` header. JS listener in `shortcuts.js` shows auto-dismissing alerts.
- **Buyout indicators**: A manual "Scan Roster" button fires `GET /buyout-indicators` (one MILP solve per eligible player), which returns OOB-swapped green/red dots into the placeholder dots. **The scan, the dots and the Analyzer's picker all read `team.all_players|selectattr('can_be_bought_out')` — the same expression, deliberately**, and `test_it_offers_exactly_the_eligible_set` states it as one set equality. Eligibility is a property of the contract group alone, so a group 2/3 player in the minors is a legal buyout whose salary is fully on cap; the scan read `roster_players` until 2026-08-07 and silently reported on 11 of BOT's 15 eligible players, which is indistinguishable on screen from "no buyout helps". **The dots live in `team_panel.html`'s two roster tables and nowhere else** — never add one to the Analyzer, which is a `<select>` since 2026-08-15: `_dom_id` mints one id per player, so a second copy is a duplicate id the scan swaps twice, and an `<option>` may not carry markup anyway. The picker's *empty first option* is load-bearing for a different reason — without it the top candidate is pre-selected and choosing him fires no `change` at all. Pin that one in the endpoint tests, not the browser: Playwright's `select_option` dispatches `change` unconditionally, so the harness that looks right for it cannot see the bug.
- **Exact standings re-solve themselves after a pick, and the basis marker is what makes that safe.** The League State **Proj** column is two rules: BOT's is `milp_solution.total_points`, every opponent's is an estimate in `_context` that costs no solve. `GET /solve-standings` replaces the opponents' figures with real per-team MILP optima, OOB, on a click — the buyout-scan idiom, for the same reason (measured 2026-08-17: **1262ms** on a fresh league, **259ms** in the endgame, because done teams are final and BOT is already solved and neither is asked; **384ms** fresh once the scan itself fans out `SCAN_WORKERS` CBC subprocesses). **Since 2026-09-10 `POST /assign` also asks for it, and ONLY `/assign`** (owner decision): the success path returns `HX-Trigger-After-Settle: {"solveStandings": true}` and `shortcuts.js` answers with `htmx.ajax('GET', '/solve-standings', {swap: 'none'})`. A **rejected** assign fires nothing, and neither does any other mutation — every one of them still degrades the column to estimates, and the manual button is still the only way back from those. Three things about the shape. It is **server-driven** rather than an `hx-trigger="load"` mount in `league_state.html`, because that template ships inside `all_panels.html`, which answers thirteen different things **including `GET /`** — a load trigger would fire a ten-solve scan on initial page load and after every bench toggle. It is **after-settle** rather than plain `HX-Trigger`, which htmx 1.9.10 dispatches before the swap; the obvious justification for that is wrong and the comment in `_toast` says so — the OOB targets are fresh either way, because the scan's response arrives ~400ms after a synchronous swap, and the mutation swapping the headers survives both browser tests. It buys a guarantee instead of a timing margin. And it is **coalesced, not stacked**: a second pick mid-scan makes `_publish_if_current` discard the first scan silently, so the listener holds at most one in flight and re-fires once on completion if a pick arrived while it ran — a latch with no queue would leave the column on estimates exactly when drafting fast. The cost is real and scales with `SCAN_WORKERS`: a warm `/bid-check` measures **43ms** during a scan against 3ms idle, and this now happens after every one of ~165 picks. **If typing feels sticky on draft day, lower `SCAN_WORKERS` before looking anywhere else.** The estimate is not a small error and was mis-bounded for nine days: the 2026-08-08 measurement compared the two rules **on BOT**, whose figure never uses the estimate, and concluded "5 points apart, same rank either way". Measured properly it runs +68 mean / +193 worst (+14.2%), overstates the *achievable* optimum for 6 of 10 opponents, and moves **9 of 10** teams in rank order — in the endgame scenario BOT's own badge read **#2 when BOT was #1**, the same class of error as the done-team projection bug. Do **not** try to fix the estimate instead: three budget-aware replacements were measured and all three are worse (mean |err| 94 against 147/176/401), because greedy-by-points spends the budget on one star and floors the rest. `exact_projections` joins `_recompute()`'s invalidation list, so **one pick returns the whole column to estimates** — which is exactly why the pick then asks for a re-solve, and why the marker still has to be honest about the window in between. `#proj-basis` says which basis is on screen as a **count** (`9/10 solved`), never a boolean, because an Infeasible opponent keeps its estimate and absence from the dict is what makes that harmless. It branches on the count of **estimates** and never on the count of exact figures: reading the latter labelled the column `estimated` in the one state where every figure on screen is exact — all opponents done, so their finals plus BOT's optimum are all there is (measured 2026-08-18), and two tests catch that mutant today. The marker is unconditional with only its `hx-swap-oob` conditional, same rule as `buyout_scan.html`. Unlike the buyout dots this needs no gating: all 11 `proj-<CODE>` spans render unconditionally, so no swap can miss. **It was removed on 2026-09-09 and restored on 2026-09-10, and what changed is the words, not the mechanism** — the owner deleted it as unintelligible and asked for it back on learning what it did. It read a bare `exact`, a word appearing nowhere else in the feature while the button says Solve; it now reads `solved` / `9/10 solved` / `estimated` and carries a `title` naming the button and saying the label is about the OPPONENTS' figures, since BOT's own Proj is a real optimum in every state and the label never covered it. **The label stays one word, and that is measured**: DaisyUI sets `.table :where(thead,tfoot){white-space:nowrap}`, so a header cell's min-content is its whole string rather than its longest word, and a two-word `opponents estimated` measured **829px** against 776px for `estimated` and 768px with no marker at all — 53px of the widest table in the app for something the tooltip says better.
- **The two manual scans compute off the event loop, and publish only if the state has not moved.** Every handler in `main.py` is `async def`, which FastAPI dispatches to the loop rather than a threadpool — correct for the mutating endpoints, and a real stall for `/solve-standings` (10 solves) and `/buyout-indicators` (~15): measured 2026-08-19, a warm `/bid-check` cost **10ms alone against 1682ms behind a roster scan**, and `/state`, which solves nothing at all, 1564ms. So each scan reads what it needs on the loop (a `deepcopy` of the state, the prices, the figure to beat), hands a **pure** `_solve_*` function to `run_in_threadpool`, and writes its result through `_publish_if_current` — also on the loop, so no worker thread ever touches a module global. The deepcopy is safety, not speed (3ms against a 78ms solve): the solver iterates `available_players` while an `/assign` can now run alongside it. **A version counter, not an emptiness check**: `_recompute()` bumps `_state_version` and already clears `exact_projections`, so empty cannot distinguish "nobody scanned" from "a pick landed mid-solve". A discarded standings solve is harmless — the column falls back to its estimate and `#proj-basis` says so — but a discarded buyout scan must return an **empty body**, because `buyout_dots.html` defaults a missing verdict to `keep` and would paint all 15 dots green, which is the 2026-08-07 "no buyout helps" failure again. `/bid-check` and `/explain` deliberately stay on the loop: `TestResponsesCannotOvertakeEachOther` pins `/explain`'s FIFO ordering, and moving it needs `hx-sync="#app:replace"` on its mount first. **Calling CBC from two threads at once is now reachable and is therefore load-bearing** — the loop solves for BOT on every pick while a scan is in flight. It is safe because PuLP names its scratch files `uuid4().hex` per solve (`pulp/apis/core.py`, `LpSolver.create_tmp_files`) and CBC is a subprocess, so the GIL is released: measured 2026-08-19, 11 concurrent solves × 3 rounds gave zero errors and zero disagreements with the serial answers, in 401ms against 1134ms. Do not treat that as free — adding `keepFiles=True` to `optimizer.py`'s one `PULP_CBC_CMD` puts every solve on the same filename and does **not** raise: a team came back `status="Optimal"` with **950** points against **1355** solved alone, a silently wrong figure wearing a rank badge. `TestTwoSolvesAtOnceAgreeWithTwoSolvesInARow` asserts the answers, which is why. **Both scans now use that concurrency for their own loops** (2026-08-19), fanning out `SCAN_WORKERS` at a time: measured, `/solve-standings` went 1294ms → **384ms** and Scan Roster 1630ms → **454ms** on a fresh league, with every published figure byte-identical. `SCAN_WORKERS` is capped at 8 and lives in `main.py` rather than `config.py`, which is the league rather than the machine — each solve is a CBC **subprocess**, so it is a core budget, and the draft runs on a laptop, not the 20-core dev box. `pool.map` is load-bearing: it yields in **input** order, which is the only reason the result is deterministic, and `TestAScanInParallelAgreesWithItselfInSeries` asserts key order as well as values so an `as_completed` refactor cannot slip through. The cost is that a request landing mid-scan is slower than it was — a warm `/bid-check` measures **43ms** during a scan against **3ms** when the scan was serial, because 8 CBC subprocesses now compete with the loop for cores. Well inside the 500ms budget and 39x better than the 1682ms this started from, but it scales with the cap: if typing feels sticky during a scan on draft day, lower `SCAN_WORKERS` before looking anywhere else. Note the roster scan's worst case is the **endgame** (23 eligible contracts against 15 fresh, 2174ms → 569ms) — the opposite of standings, which gets cheaper as teams finish. A third multi-solve endpoint joins `THREADED_SCANS` in `tests/test_event_loop.py` or says why not — and it cannot simply be forgotten: `test_only_recompute_may_solve_on_the_loop` requires every caller of `solve_optimal_roster` to be a `_solve_*` (which is forced into a thread) or to name itself in `SOLVES_ON_THE_LOOP` with a reason.
- **The header player search is the only global "where is he?" in the app, and it deliberately renders no ids.** Mid-auction a name gets called and nothing else answers it: the pool table lists only the undrafted, a rostered player needs the right one of eleven panels opened, a minor sits in a second table inside that panel, and a bought-out player appears **nowhere** — `execute_buyout` removes him from every list and leaves a nameless float in `team.penalties`, so his `buyout` `TransactionRecord` is the sole evidence he existed. `AuctionState.locate_players` resolves keeper → acquired → minors → pool → log, live state always beating the log; `main._search_rows` turns a `PlayerLocation` into every figure and every string; `GET /find-player?q=` renders `search_results.html`. **Matching folds to LETTERS ONLY plus word-start offsets** — `players.csv` carries apostrophes, sixteen hyphens and the parentheses `_disambiguated_names` adds, and the 2024-25 file carried four names where it encoded the hyphen as the DIGIT ZERO (`Oliver Ekman0Larsson`), so a fold that keeps punctuation makes the operator reproduce a data-entry bug; measured, eight plainly-typed surnames returned zero hits before the change. The digit-zero artefact is **gone from every pool in the repo** as of the 2026-09-15 refresh; the fold keeps handling it, because the file is replaced before every draft and the next export may reintroduce it. The offsets are the load-bearing half: the fold concatenates, so a split-token match would put every punctuated surname in the substring tier. Ranking is **two** tiers (prefix anywhere, then substring), by projected points then name — a full-name prefix is not the stronger match, it only means "his first name starts with this", and as its own tier it filled all ten slots with the wrong people. **Three things this endpoint must keep**: it fires on every keystroke, so it bypasses `_context` through `_render`'s short-circuit (~8.5ms and a `bid_limits` the size of the pool — 704 rows when that was measured, 678 today — against 0.21ms of actual work) and computes **no** max bid, which is a binary search over MILP solves; `q` is a query param, not a path segment, for the reason `/buyout-check` documents; and a blank query answers **200 with an empty body**, never 204, which htmx reads as "do not swap" and which would strand the last results on screen after the box was cleared. `team_code` (display) and `link_team` (navigation) are separate fields — a bought-out player's penalty sits on the cap of the team that bought him out — any team since 2026-09-12 — and is worth naming, but he is on no roster, so that row navigates nowhere.
- **The NHL odds modal is fetched on every open, and that is the whole design.** `team_probability` is one of the five drivers `decompose_price` reports and nothing else in the app showed it. The odds themselves are fixed for the season, so the table could have been baked into `base.html` — but the columns worth having are *how many of that club are left in the pool* and *how many are already rostered*, and those move with every pick. Markup rendered at page load would be wrong by the second nomination while looking authoritative, so the navbar button fires `hx-get="/nhl-odds"` into `#nhl-odds-body` and opens the `<dialog>` in the same click (htmx does not swallow an inline `onclick`). The mount is unconditional and **non-empty** — a swap into a missing target is a dead swap, and an empty box flashes as "no odds" on every open. Two folds are load-bearing and each has its own test: rows are counted through `_nhl_canonical`, because the 2024-25 `players.csv` spelled Utah `UTH` on 78 rows while `team_odds.json` says `UTA` and a raw `Counter` splits one club in two (the 2026-27 file spells it `UTA` on 37 rows, so no pool in the repo exercises the alias today — the fold stays for the pool after next, and its test PLANTS the alias spelling on one pool player and one rostered one, because without that both folds could be deleted with the suite green, measured 2026-09-22); and the odds dict's **alias keys are dropped** (`load_team_odds` adds `UTH` beside `UTA` pointing at one number), because iterating it raw prints Utah twice. **The table and the prices are two reads of the file, and the modal flags when they disagree.** A draft freezes each player's `team_probability` when it is built, and a saved draft never re-reads the file; the table is read at boot and again by `/reset` and `/load-scenario`, which re-price the pool off it. So a file changed under a draft in progress shows its new odds beside a `priced at` figure for every club whose frozen value differs at 2dp. The season label is `main.nhl_odds_season`, captured by the same read as the table — not `data_loader.last_odds_season`, which every `load_team_odds` call rebinds, so until 2026-09-22 a reset over a changed file labelled the boot-time table with the new file's season. Clubs the pool names that the odds file does not are listed in a footer rather than hidden: `_get_team_probability` answers for them with `DEFAULT_TEAM_PROBABILITY` **silently**, so a club respelled by a refresh would price its whole roster at 3.1% with nothing on screen — `test_every_nhl_club_in_every_pool_has_cup_odds` fails the refresh itself, parametrized over every `data/players*.csv` like the logo sweep beside it. `data/players.csv`'s `UFA` placeholder is the one allowed exception, named explicitly so a second placeholder fails rather than joining it. The dialog lives outside `#app` for the reason the shortcuts modal does, and carries no `data-shortcut-key`: `TestShortcutsModal` asserts those rows and the bound keys are equal in both directions, and a navbar button is not a shortcut.
- **A player name becomes a DOM id only through `main._dom_id`** (registered as the `dom_id` Jinja filter). htmx resolves an out-of-band target by **selector** — `"#" + id` into `querySelectorAll` — from a loop with no `try`/`catch`, so one id that isn't a legal CSS identifier throws and abandons **every remaining swap in the response**. Measured in Chrome: `Matt Murray (DAL)` on BOT gave 12 placeholders, **0** resolved, and a Scan button that looked like it had not finished. `players.csv` carries backticks (`Drew O`Connor` — note U+0060, which the old `replace("'", '')` did not match) and parentheses (`Tony DeAngelo (NCM)`), and `_disambiguated_names` adds ` (TEAM)`, ` (TEAM POS)` and ` (#n)` on top. The filter's sha1 suffix is load-bearing: a slug alone is lossy, and two players colliding on a derived key is the failure the name disambiguation removed. Never hand-roll the id in a template or a test — `TestNamesSurviveBecomingDomIds` checks every pool and roster name, and a hand-rolled copy in a test turned an `assert dot_id not in html` into an assertion that could not fail.
- **An NHL club badge is built only by `main._nhl_logo_src`** (the `nhl_logo_src` filter) and rendered only by `templates/macros/nhl.html`. Same rule as `_dom_id`, and it was learned the same way: the assets are named by the **canonical** tricode, `config.NHL_TEAM_ALIASES` declares `{"UTH": "UTA"}`, and the file on disk was `UTH.svg` — named after the *alias* — while six templates pasted the raw CSV value into the path. So which pool you loaded decided whether Utah had a logo: the 2024-25 `players.csv` spelled it `UTH` on 78 rows and worked by accident, `players-25.csv` spells it `UTA` on 30 and 404'd on every one. (The 2026-27 `players.csv` spells it `UTA` on 37, so the accident is gone and the canonical name is what the file now says.) The macro also owns the blank guard, and the 2026-09-15 refresh made it matter far more: 3 rows of `players.csv`, 3 of `players-25.csv` and **162** of `players-23-converted.csv` have no club, up from 7, because the legacy NHL join lost most of its donor. **A blank club is two different things**, and `convert_fchl_online.no_nhl_club` now reports them at conversion time rather than letting them collapse: a club the odds file does not know (what the 162 are), and the export's `UFA` placeholder, which is the league saying **this player has no NHL contract**. The second is a roster decision, not a data problem — a cap-counting player who will not play a game — and it was silent until 2026-09-17, when the one such row (group 3, $2.3M, on an active roster) had to be found by hand. `tests/test_fchl_online_conversion.py` now fails on a new one, scoped to `data/players.csv` alone because only the live pool is built from an export carrying that signal. `/nhl_logos/.svg` is a broken image plus a 404. `UFA.svg` is the FCHL placeholder the 2024-25 `players.csv` carried on 9 rows — no pool carries it today, since `convert_fchl_online.py` blanks it — and `ARI.svg` is a retired club; neither is cruft. Two guards, and they catch different things: `test_no_template_builds_the_path_itself` stops the six sites drifting back one at a time, and `TestLiveDataInvariants::test_every_nhl_club_in_every_pool_has_a_logo` is parametrized over **every** `data/players*.csv`, so a refresh that respells a club fails at pytest rather than on screen. The `pre-auction-check` runbook could not have caught it: it hardcoded `data/players.csv` and compared raw spellings, which is the same mistake the templates made.

- **Atomic saves**: `_save_state()` writes to `.tmp` then `os.replace()` (POSIX atomic). Previous state kept as `.backup`.
- **Startup recovery**: `lifespan` walks current → `.backup` → fresh, and a file that fails to **parse** is renamed `.corrupt` rather than left in place — otherwise the next save rotates it over the good backup and both copies are gone. `_load_saved_state` catches broad `Exception` on purpose: at startup of a tool that may be four hours into a live auction, degrading beats failing to boot. **Only the parse decides usability.** The four `_backfill_*` calls each get their own net and are never fatal — none is load-bearing for the draft record, and folding them into the parse net meant one raise on a legacy snapshot renamed a byte-perfect draft `.corrupt` and started fresh, silently. Any degraded startup sets `_startup_warning`, which `_context` passes to `base.html` as a banner **outside `#app`** (a panel swap replaces `#app`, so an inside banner would vanish on the first pick); `POST /reset` clears it. Pinned by `tests/test_crash_recovery.py`. **The folder is locked before any of that runs.** `main._hold_state_lock` takes an advisory `flock` on `STATE_DIR/.lock` straight after `os.makedirs`, and `_save_state` takes it again. A second process on the same folder, such as a diagnostic `TestClient` script or a second `uvicorn`, raises and exits instead of booting. This is the one startup path that **refuses rather than degrades**, because there booting is the damage: the ladder's `.corrupt` rename is a write into the other process's draft. Only `BlockingIOError` refuses. A filesystem that cannot lock at all, such as Windows or ENOLCK, warns once and boots. The kernel drops the lock with its holder, so a crash cannot strand a restart. It is re-entrant per process (one fd per folder, never closed), because pytest boots lifespan repeatedly on one folder. It does **not** cover a script run while no server is up; that residual is in `BACKLOG.md`. Pinned by `tests/test_state_lock.py`. **On draft day**: the backup is one save behind by construction, so a recovery costs the most recent transaction — check the last pick is still there and re-enter it if not.
- **Two banners, not one.** `#startup-warning` describes *this boot* and `/reset` clears it; `#data-warning` describes *the CSV* (duplicate names the loader had to rename) and survives a reset, because the renames do. Merging them breaks both directions: a permanent data note turns the degraded-boot alarm into wallpaper — which is exactly what `test_the_happy_path_shows_no_banner` guards — and routing the renames through `_warn_at_startup` would make them vanish on a reset that repopulates them. `_data_warning()` composes at render time rather than being pushed at startup, so it always describes the pool actually loaded; booting onto a *saved* state says nothing, correctly. It reads `data_loader.loaded_disambiguations`, written **only** by `build_initial_state`, not `last_disambiguations`, which any `load_players` caller resets — a test fixture or the pre-auction runbook loading a different CSV would otherwise blank the banner for whatever ran next.
- **Undo restores by enumeration, not by a list.** `rollback_to` (which `restore_snapshot` delegates to) loops over `fields(self)` and copies everything except `_snapshots` — never re-introduce hand-written `self.X = restored.X` lines, because a field added to `AuctionState` would silently stop being restored, on the one operation with nothing behind it. The `_snapshots` skip is load-bearing: snapshots are written with `include_snapshots=False`, so the restored chain is always empty and copying it makes `Ctrl+Z` work exactly once per session. `to_json`/`from_json` are still hand-written, so two guards in `tests/test_state.py::TestSnapshotFieldsCannotDrift` cover them — one structural (fields == JSON keys), one behavioural (every field survives a round trip); they catch different mutants and neither is redundant.
- **A new mutating `@app.post` either snapshots or joins `NO_SNAPSHOT_NEEDED`** in `tests/test_state.py::TestEveryMutatingPostTakesASnapshot`, in the same commit, with the reason. It walks `main.py`'s ast, so forgetting fails the suite instead of surfacing as a wrong `Ctrl+Z` four hours into a draft. Two shapes count and they live in two constants, because the ast walk matches them differently: `save_snapshot()` (an `auction_state.method()` call) and `with _undoable(rollback=...)` (a bare name). `capture_snapshot()` deliberately does not, because capturing without committing snapshots nothing — and since 2026-08-20 neither does `commit_snapshot()`, whose only caller in `main.py` is `_undoable`: accepting the name would accept a hand-rolled pairing that an endpoint can get half right. If a fifth endpoint ever needs the raw pair, put `commit_snapshot` back in `SNAPSHOTTING_CALLS` and say why the context manager did not fit.
- **An endpoint that can reject snapshots on the success path only.** `save_snapshot()` captures *and* commits, so calling it before you know the operation will succeed is not free: it evicts the oldest entry once the chain is at `MAX_SNAPSHOTS`, and the `restore_snapshot()` that used to follow on the error path pops from the *other end* — so a rejected request read as a no-op while quietly destroying a real undo step (measured: depth 50 → 49, oldest gone). So wrap the operation in `with _undoable(rollback=...)`, which is that sequence in one place — capture, commit on the success path only, and `rollback_to` where asked. **Never `restore_snapshot()`**: that is `Ctrl+Z`, and spending a chain entry is what it means. `rollback` is not a style choice — pass `False` only when the operation validates before it mutates, so a refusal has changed nothing, and `True` when it can mutate and then raise. The contract is `ValueError` and nothing wider: another exception mid-mutation neither rolls back nor commits, which is what the four hand-rolled sites did before the helper existed. `send_to_minors` and `recall_from_minors` validate before mutating and so need no rollback at all; `execute_trade` strips both rosters before adding to either and very much does. **A rollback test needs a failure that is genuinely partial** — the first version gave `/trade-execute` one player, whose removal raised before anything moved, and passed against a build with no `rollback_to`.
- **Undo tests need an empty snapshot chain**, which is why they use a function-scoped `client` shadow. With a chain carried over, `/undo` pops *somebody else's* snapshot and a broken endpoint looks restored — and the shared chain can come from the test's own setup, not just from earlier tests: `test_undo_reverts_move_to_minors` has to bench a player first, `/toggle-bench` snapshots, and pre-bench has the same roster and minors counts as post-bench, so a counts-only reading passed against a `/move-to-minors` that had stopped snapshotting entirely. **Read something that separates the two states**, not just the thing the endpoint moved.
- **Keyboard shortcuts**: exactly three, all in `static/shortcuts.js` — `Ctrl/Cmd+Z` (undo), `N` (nomination recommendations) and `/` (focus the header player search), each inert while focus is in an INPUT/TEXTAREA/SELECT. `/` needs `preventDefault()` because Firefox binds it to Quick Find, and must be written `e.key.toLowerCase() === '/'` so the modal guard's regex can see it — the character class is `[a-z/]`. **`Escape` is bound too and is deliberately not in that contract**: it closes the search results, the capture group is a single character so `'Escape'` could never match, and it is scoped to focus inside `#player-search` so the shortcuts `<dialog>` keeps its own native Escape. It is described in the `/` row's prose rather than given a `data-shortcut-key` row it could not satisfy. The navbar's **⌨ Shortcuts** button opens a `<dialog>` listing them, and each row carries `data-shortcut-key` matching the key the handler binds. `TestShortcutsModal` asserts those two sets are equal **in both directions**, so adding a shortcut without documenting it fails the suite — a shortcut list that drifts is worse than none, because it gets believed. Add a new shortcut and the modal row in the same commit.
- **A partial mounted in two places carries no id of its own.** `counterfactual.html` and `player_chart.html` are bodies; each mount owns its id and its own empty state. htmx resolves `hx-target` by id and takes the *first* match without complaining, so a body that carries the mount id puts two copies in the document and swaps land in the wrong column — and an `innerHTML` swap nests the response's copy inside the mount, duplicating it even on a quiet page. Close buttons use `this.closest('.the-card').remove()`, never `getElementById`, for the same reason — and **the direction you test it from matters**. For `counterfactual.html`, `all_panels.html` includes `auction_control.html` (`#bid-panel`) before `explanation.html` (`#explanation`) within `.area-auction`; for `player_chart.html` the two mounts are in different columns and `.area-auction` comes before `.area-players`, so `#bid-panel` is first there too. Either way the bid panel's copy is already first in document order, and closing *that* one cannot distinguish `closest()` from a document-wide query (measured 2026-08-14 — the mutation sailed through). Close the other mount. (Both orderings were briefly the other way for `player_chart.html` under the 2026-09-17 two-column grid, reverted 2026-09-20 — see the Responsive layout bullet; `TestTheChartLandsWhereYouClicked` in `tests/test_browser_ui.py` is the guard and it asserts screen position, so it moves with the grid.) A close button must also leave its **mount** standing: `getElementById('explanation').remove()` satisfies every "the right card went away" assertion while deleting the target every future swap lands in. `tests/test_htmx_interactions.py` guards id uniqueness within `GET /`; the cross-fragment case only exists in the assembled DOM, so it lives in `tests/test_browser_ui.py`. **A control INSIDE such a body targets `closest .the-card`, never the mount id, and the reason is not only the two-mount ambiguity**: `counterfactual.html`'s Recompute button (2026-09-12) does an `outerHTML` swap, so naming `#bid-counterfactual` would consume the mount the lazy `hx-trigger="load"` lands in on every later panel swap — the `#bid-advice` failure, silent on screen. Targeting the card means an empty response removes the card and leaves the mount standing, which is the correct degradation. Both directions are pinned by `TestRecomputeUsesThePriceOnTheTable`, in the browser because neither is visible from `TestClient`.
- **Dismiss-on-interaction removes the element on `htmx:afterRequest`, never on click — and the reason is the `successful` gate, not an abort.** This bullet said until 2026-09-10 that htmx **aborts an in-flight request whose triggering element leaves the DOM**. It does not. Measured that day in Chrome against the vendored htmx 1.9.10: removing a trigger element during `htmx:beforeSend` fires no `htmx:abort` and the swap lands anyway, because `b.onload` calls the swap `M(n,I)` unconditionally and only *then* fires events (re-firing them on the nearest surviving ancestor via the `if(!se(n))` branch, which is why such a handler must be idempotent). The claim was reasoned off the minified source rather than run, and it had been copied into `shortcuts.js` twice and into a browser-test docstring before anyone tried it — a `click`-time clear was run against the player-search tests and all three passed, which is how it surfaced. **What is actually load-bearing**: `event.detail.successful` is only knowable after the response, and a failed request must leave the card on screen (`/nominate` is the only way back). `shortcuts.js` removes `.nomination-pick` and clears `#player-search-results` on that gate. The nomination handler removes **only** the half acted on: per the CBA a nomination turn is 1 RFA + 1 UFA and an RFA sale *keeps the turn*, so the other half is the next thing needed, not clutter. Client-side on purpose — `/bid-check` deliberately does not touch the nomination panel, and an out-of-band swap would re-couple exactly what the panel split separated. Pinned by `tests/test_browser_ui.py::TestMidBidClutterCanBeDismissed`, whose bid-panel assertion is what separates "the card went away" from "the card went away and the request still landed" — a weaker property than the abort story claimed, and the one the test actually has.
- **The nomination turn is not tracked, and "Get Recommendations" is therefore unconditional.** Until 2026-09-11 `AuctionState` carried `nomination_index` / `nomination_round` / `snake_draft`, `/assign` advanced the pointer on every UFA sale, a badge printed whose turn it was, an Override `<select>` posted to `POST /set-nominator`, and the button was gated on `current_nominator == my_team`. **That gate was the only thing the whole feature did to the running tool** — `current_nominator()` had exactly one non-test caller (putting it in the template context), `recommend_nomination` is hard-coded to `MY_TEAM` and turn-blind, `/nominate` never checked it, and `/assign` validated only the team and the player. The `n` shortcut fired `/nominate` unconditionally all along, so the recommendations were already available out of turn — by keyboard, while the button documenting them was hidden. Two latent bugs went with it: `/team-done` flipped `is_done` without adjusting `nomination_index`, so `_effective_order()` shrank and the same index silently resolved to a different team; and the Override dropdown listed `nomination_order` forward while the pointer indexed a list *reversed* on odd rounds. **`nomination_order` stays, under that name** — it is the app's canonical team display order, read by four templates (`league_state.html`, `standings_cells.html`, `bid_panel.html`, `team_panel.html` — the Override dropdown was the fifth) plus `default_bidders`, it is in the data fingerprint, and `standings_cells.html` iterating the same unfiltered list is what makes its OOB cells match League State's rows by construction. The button is **on demand, never `hx-trigger="load"`**: each `/nominate` is a MILP solve and the fragment ships inside `all_panels.html`, which answers `GET /` and a dozen other things — the same hazard `/solve-standings` documents. Three guards, and they catch different mutants: `test_the_button_survives_every_offset_in_the_order` walks one lap of the order **plus one** (a single checkpoint at eleven picks wraps a round-robin back to BOT and passes against a fully restored gate — measured, that mutant survived), `test_the_override_endpoint_is_gone` requires 404/405 rather than a silent 200, and `test_nothing_names_the_removed_turn_api` greps the templates and the four modules, because a template naming a context key that no longer exists renders **empty** rather than raising. A state file written before the removal still carries the three keys; `from_json` read them with **bracket** access, so dropping the reads is what makes the operator's live `data/state/auction_state.json` load unchanged, pinned by `TestALegacySaveStillLoads`. **The panel's × clears both cards and is gated on there being cards**, in the slot the Override dropdown vacated: an × beside a heading with nothing under it is a control that does nothing. The Jinja gate only re-evaluates when the server renders, so `shortcuts.js`'s `htmx:afterRequest` handler also removes it when the last `.nomination-pick` goes — bidding both halves otherwise strands it. Assert the cleared count at **zero**, never "fewer": `querySelector` for `querySelectorAll` is the obvious slip and it satisfies every "the card went away" reading.
- **`#bid-advice` is the bid panel's "what the advisor says" slot, and both branches of `bid_panel.html` own it** — the verdict block when there is advice, the not-found note when `/bid-check` was given a name that isn't in the pool. They are mutually exclusive, so the document still holds exactly one; reusing the id is deliberate and load-bearing. The price input carries `hx-select="#bid-advice"` with `hx-swap="outerHTML"`, and **an unmatched `hx-select` on an outerHTML swap DELETES the target** — htmx swaps the empty selection in. Measured in Chrome 2026-08-07 against the pre-fix template: after the player left the pool, `#bid-advice` count went to **0** while `#bid-panel`, `#bid-form` and `#bid-price` all survived, with no console error. So the verdict block silently disappeared mid-bid and left a half-built panel. (Both `BACKLOG.md` and an earlier draft of this bullet said it "swaps nothing at all" — that was reasoned, not measured, and it is what kept the entry deferred for a month on the grounds that silence was the lesser evil.) The other way in is more common: the Start Auction field is free text (`required` plus a datalist, *not* readonly), so a typo swapped the whole panel back to a blank form and the name you typed simply vanished. Any future branch of that template answers in `#bid-advice` or answers nowhere.
- **`/trade-evaluate` swaps `#trade-result` and nothing else, because the trade form lives only in the DOM.** Same class of thing as `#bid-advice`, found the same way: until 2026-09-10 the evaluate form posted with `hx-target="#trade-panel"` `hx-swap="outerHTML"`, so the answer and the question were swapped together — and the replacement is stateless by construction. The template renders no `checked`, no `selected` on any partner `<option>`, and `#trade-receive-list` back at its "Select a team first" placeholder; nothing re-fires `loadTradeChoices` (its only trigger is the select's `onchange`), so the fetched half came back **gone**, taking the partner selection with it. `templates/partials/trade_verdict.html` is now the whole response (27KB → 1.7KB, since the 49-row give list stops being serialised), and **both its branches carry `id="trade-result"`** — the empty one is load-bearing for the reason `bid_panel.html` records, a target that vanishes with its contents can be swapped once. The `.trade-verdict` **class** stays on the populated branch alone: it is the hook that means "there is an answer on screen". Preserving the form re-opened a hazard that ships closed beside it — `/trade-execute` posts the SERVER's `last_trade_eval`, not the form, so a modified selection would execute the trade you *evaluated*. `markTradeEvalStale` marks the verdict and disables its submits on any change, and it is called **first** in `updateTradeSummary`, ahead of the `!picked.length` early return: at the bottom it fired on every change except unticking the last box, which is the most likely way a trade gets narrowed. No opacity dim on the stale verdict — `opacity` creates a group, so the warning cannot be brighter than what it warns about.

- **The team panel's roster carries a lineup-slot number, and what it counts is the `is_bench` FLAG.** `main._roster_slots` (the `roster_slots` filter) labels each row `F1`..`F12` / `D1`..`D6` / `G1`..`G2` for starters and `BF1`..`BG4` for benched players, so the last STARTER number in a group is how many of that position start, and the holding is that plus the group's bench rows (`F1`..`F5` with a `BF1` is six forwards, what `roster_needs` counts) — the two agree until someone is benched, and this said "the last number is the holding" until 2026-09-22. Three rules, each load-bearing. A **MILP suggested-buy row gets no label and consumes no counter** — it is not owned, so numbering it would report a roster that does not exist, and dismissing a suggestion must not renumber the players around it. A position is **never clamped** at its starting size: `F13` is the true holding and the signal that someone belongs on the bench, and auto-benching the overflow would overwrite the operator's `/toggle-bench` decision. And it is applied **after** `team_panel.html`'s `pos_rank`+points sort, because the label means "the Nth row of this group as drawn" — sorting inside the filter would let a label and its row disagree. **It makes an older inconsistency visible and must not be "fixed" by chasing it**: `is_bench` reaches no engine module, so `lineup_points` scores the best 12F/6D/2G regardless and the Lineup PTS tile still counts a player this column prints as `B1`. Deriving slots from points instead would contradict the row dimming, the Bench button and `send_to_minors`' precondition, all of which read the flag. A filter and not Jinja `namespace` counters for `_dom_id`'s reason — one pure function a unit test can call. The header uses `title`, never `data-tip` (horizontal scroller), and its wording may not name a row state an opponent's panel cannot render: `test_it_is_absent_on_an_opponent` caught a `title` saying "suggested buy". **The label replaced the `Pos` column**, which restated it on 20 of 24 rows — and a bench row therefore carries its position too, `BF1`/`BD2`/`BG3`, since `B1` alone left exactly four rows whose position nothing on the panel stated. **The bench NUMBER is one running sequence and must not restart per position**: it answers "how full is the bench" against the hard `BENCH_SIZE` of 4, whereas per-position counting would report progress against `BACKUP_TARGETS`, a soft MILP preference nothing enforces. The **Minors** table keeps its own `Pos` and stays unnumbered, deliberately: a minor has no lineup slot to read a position off. Cost measured 2026-09-20: the table's min-content went 508px → 542px with the `#` column and → **501px** once `Pos` came out, in a 379px scroller — net 7px narrower than before either change, and it stays inside the existing `.table-scroll-x`, as a further column must or `#team-panel` paints over its neighbour. `tests/test_roster_slots.py` also carries the header-to-cell parity guard this table lacked, and `test_the_roster_table_has_no_position_column` pins the removal.
- **Responsive layout**: `.auction-grid`, 1-col (mobile), 2-col (768px+), 3-col (1024px+). Three areas: `.area-auction` (Auction Control, Explanation, Logs, Trade, Buyout), `.area-players` (Available Players, League State) and `.area-team` (Team panel). **It was collapsed to two columns on 2026-09-17 and restored on 2026-09-20, both on the owner's request** — the fold moved Available Players into `.area-auction` above Auction Control, League State into `.area-team` above the Team panel, and deleted the 1024px tier. Nothing was wrong with the 2-col mechanics and the tracks were genuinely wider (~499/~627/~787px at 1024/1280/1600 against ~329/~409/~511px here); it came back because of how it looked with three panels stacked in one column, which is a judgement about the screen and not something to re-derive from the numbers. **The draft runs on a 1280–1600px laptop** — that is the width to check, not 1920. `tests/test_browser_ui.py::test_the_grid_never_overflows_its_own_width` pins containment at 1024/1280/1600; `tests/measure_layout.py` is the instrument for asking *which* element forced a column wide (it redirects `main.STATE_DIR` to a temp dir first, because the default is the operator's real state and a script sets no `FCHL_STATE_DIR`). **The navbar is a separate `flex-nowrap` row and is full at 1024**: the title group is the only flexible item, it reaches 0 there, and a control added past that point pushes the last one off the edge — `test_the_navbar_stays_one_row_with_the_search_box` is the guard, and it failed by **3px** when the 🏒 Odds button was added (2026-09-12). Width for a new control comes out of an existing one, and the place to look first is any `<select>`, which is sized by its **widest option**: the scenario picker measured **469px** — 46% of the viewport — for a closed state reading `Scenario...`, and capping it bought 268px of slack.
- **A multi-pick player list is a `.choice-list` of checkboxes, never a `<select multiple>`.** Both trade forms used one until 2026-08-15, and both failure modes are worth knowing. **Width**: a select is sized by its column, not its content, so it *clips silently* — measured at 1280, the four controls rendered 120–183px against labels wanting 229–316px, putting the salary and points off the edge on every row. **Affordance**: a plain click discards every prior selection, and in a 3-row window onto 49 options nothing on screen says so. The replacement stacks its column (that is what buys the width — two columns of the 521px panel at 1600 is still only 236px), wraps each checkbox in its `<label>` so the row text becomes its accessible name, names the **group** with `role="group"` + `aria-label`, and shows a running `N selected · $X.XM`. **Checkboxes sharing a `name` serialize identically to a multi-select**, which is why `/trade-evaluate` (`form.getlist`) needed no change and its tests are the equivalence proof. `white-space: nowrap` is safe only because the list scrolls: `overflow-y: auto` forces `overflow-x` to `auto` (see two bullets down), so a long row can never set its grid column's min-content. **One JS builder (`loadTradeChoices`) fills both fetched lists** — there were two copies differing only in the value and whether the label carried points, and the duplicated `(M)` suffix was a real finding because deleting either left the suite green.
- **Never a bare `1fr` in a hand-written grid — always `minmax(0, 1fr)`.** `1fr` *is* `minmax(auto, 1fr)`, and per css-grid §6.6 an item takes a content-based automatic minimum whenever it spans a track whose **min sizing function** is `auto`: the widest panel silently sets its own column's floor and the others divide the remainder. Measured at 1280px before the fix, `.auction-grid`'s tracks were **292 / 991 / 585** against 409 for an equal third, and `#team-panel` — your cap, your roster, the buyout dots — began at **x=1310**, off-screen, on the width the draft is run at. Do **not** also add `min-width: 0` to the `.area-*` children: §6.6 keys on the track, not the item (that is the *flexbox* rule, §4.5), so it is a redundant second source of truth. Every Tailwind grid utility already complies (`grid-cols-3` → `repeat(3, minmax(0, 1fr))`); `.auction-grid` was the one hand-written grid that did not.
- **A table wide enough to overflow its column needs its own scroll wrapper** (`.table-scroll-x`, or `.scroll-container` when it also wants the height cap and sticky `thead`). `minmax(0, 1fr)` shrinks the *item* box and does nothing to its descendants, which keep their min-content and `overflow: visible` — measured with the minmax fix and no wrapper, `#league-state`'s 12-column table painted out to x=1405 **across** `#team-panel`, under an opaque card, with no scrollbar offering it. That is strictly worse than the original bug, so the two halves ship together. Also note nothing looks wrong either way: `all_panels.html`'s inline `overflow-y: auto` forces `overflow-x` to compute to `auto` (CSS Overflow §3.2), so overflow hides behind the **grid's** scrollbar and no **page** scrollbar ever appears. Never "fix" that with `overflow-x: hidden`, which makes the panel unreachable rather than merely off-screen.
- **No DaisyUI `data-tip` may live inside a `.table-scroll-x`, and the browser suite cannot tell you when one does.** DaisyUI centres a bubble on its trigger and has no flip logic, so a bubble anchored to a `th` in a horizontal scroller is clipped at essentially every scroll position — the shipped 270px bubble on `Proj` at **100%** of the positions where that header is fully visible. **The fraction does not depend on the viewport width**, measured closed-form at 1600/1280/1024/928 and identical at all four: the binding constraint is the trigger's clearance from the **table's right edge** (82px for a column second from last of eleven), which is content geometry, not viewport geometry. Quote it that way — an earlier draft of this bullet gave per-width counts (5 of 6 at 1280, 4 of 5 at 1024) and percentages (~50/~30/~20) that were sampled, width-specific, and too kind. **Re-anchoring and narrowing both fail** — over the shipped geometry a 200px bubble is clipped 100% of the time, 120px **57%**, and even an 80px one **32%**; a 27px trigger with 82px of clearance has no width that works. Carrying the `data-tip` at all also cost **39px** of table width (`scrollWidth` 776 → 815), because an absolutely positioned descendant contributes to its ancestor's scrollable overflow. Use a native `title`, which has no box to clip — the same conclusion `.price-drivers` records in `style.css` and the `#proj-basis` marker records in `standings_basis.html`. `TestTooltipsStayInsideTheirPanel` was green against the 375-char offender for a month because it measures at **`scrollLeft: 0`**, where that header is not on screen, and bounds containment against the scroller's `scrollWidth` rather than against what is visible — so it is structurally blind here, and `test_the_header_explanation_is_not_a_daisyui_bubble` is a cheap static guard standing in for it. No `data-tip` is inside a horizontal scroller today; if one is added, it needs a scroll sweep, not that suite. Note also that the `<th>` already holds the `#proj-basis` `title`: **one hover explanation per cell** — the static one restated the state-aware one at greater length and said "estimates" even when every figure on screen was solved.
- **Every solve is time-boxed, and the wait is on screen.** `optimizer.SOLVE_TIME_LIMIT` (10s) is passed to CBC, whose default is unbounded — a request on the event loop that never returns hangs the whole UI. A solve cut off with a feasible roster still reports status `"Optimal"` (PuLP maps CBC's stopped-on-time to 1), so `sol_status == LpSolutionIntegerFeasible` is what sets `MILPSolution.timed_out` and logs a warning; the roster is used, because every caller needs one. `#request-timer` (`base.html`, driven by `shortcuts.js`) is a corner badge counting how long the server has been on a request, any htmx request, the background standings solve included (owner request, same day). It lives outside `#app` for the banners' reason and is a fixed badge because the navbar is full at 1024px; it stays hidden under 0.3s so ordinary requests never flash it. **In-flight requests are keyed by XHR in a `Map`, never counted**: htmx re-fires `afterRequest` on a surviving ancestor (see dismiss-on-interaction above), so a counter would reach zero with a slow request still out. Its browser tests dispatch the htmx events directly, because a sleeping `page.route` handler blocks the sync test thread and the "slow" request finished before the test looked.
- **No CDNs**: htmx, DaisyUI and Tailwind are vendored in `static/vendor/` — the app must run with the network down, because every panel and every Assign is an htmx request. `tests/test_offline_assets.py` fails any template that loads an asset from another origin. Also: **do not add a CSP** without reading `static/vendor/README.md` — the Assign button's `hx-vals='js:{...}'` needs htmx's eval path.

## Auction rules (from CBA)

- UFA: circular bidding, $0.1M increments, drop out = permanent for that player
- RFA: secret bids, prior team can match (ROFR)
- Combo: 1 RFA + 1 UFA per nomination turn; the turn passes only when the UFA half sells (an RFA sale keeps it). **A league rule the tool does not model.** It tracked a pointer until 2026-09-11 and the pointer drove nothing — see "The nomination turn is not tracked" below.
- Min salary $0.5M, max $11.4M
- Roster: 24 active (playing: 12F + 6D + 2G, bench: 4 any position). Teams can draft beyond 24 -- extras go to minors with salary fully on cap. Teams can also finish with fewer than 24.
- **Only the starting lineup scores**, and the manager sets it: points come from the 12F/6D/2G you start, and a lineup can change only when a player is injured or at the monthly adjustments. So a backup scores whenever he covers for an absent or benched starter — which is why the tool values the bench at its **expected** contribution rather than at zero (owner decision 2026-09-25, below).
- Snake draft for nominations (again, a league rule the tool does not model)
- Trades allowed during auction breaks
- Buyouts (CBA Article 11.4): player removed, 50% salary penalty remains on team's cap. ANYONE can be bought out -- keepers and fresh draftees alike.
- Teams can voluntarily stop drafting before filling all 24 spots

### Owner decisions (2026-07-05)

- 14F/7D/3G roster shape is a **soft preference** (good backups: 2F/1D/1G), not a constraint. **Superseded 2026-09-25**: the shape is no longer encoded at all. `BACKUP_TARGETS`/`BACKUP_BONUS`/`BENCH_WEIGHT` paid a flat 5 points per filled backup slot whoever filled it, credited only players the MILP bought, and reached no figure any decision compared. The bench is now priced by `config.BENCH_DEPTH_WEIGHTS` (see the next section), and the shape the solver picks is whatever those weights and the prices make best.
- RFA sealed bids are NOT separately modeled: run them like a regular auction and bid the advisor's current optimal bid. No ROFR logic in the tool.

### Owner decisions (2026-09-25)

- **The bench is worth its expected contribution, everywhere.** A backup plays when a starter at his position is out (`config.OUT_RATE`, default 0.15 per starter, a guess — the league DB archives hold no games-played data to fit it). So the k-th backup at a position is worth `P(Binomial(n, m) >= k)` of his points: F 0.86/0.56/0.26/0.09, D 0.62/0.22, G 0.28 (slots under `MIN_DEPTH_WEIGHT` = 0.05 are not modelled). `state.expected_points` computes it; `MILPSolution.total_points` **is** it, and every decision compares it — trade and buyout verdicts, Scan Roster, max bids, the Proj column. `lineup_points` stays for display only ("starters N"); comparing two solutions on it makes a 40-point backup worth nothing again. League State's **Pts** is still the starters; **Proj** is expected.
- **The cost is real and was accepted**, measured on the live draft (17 open spots): a solve went 260ms → **463ms** median, and a cold max bid (the first `/bid-check` on a player after a pick, ~10 solves) from about 2.5s to **4s in a script and 5.3–5.9s through the running server** (verify pass, 2026-09-25; the owner had been quoted ~4s, and was told). `optimizer._bench_candidates` gives bench-slot variables only to each position's top 60 by points **and** top 60 by points-per-dollar — a good backup is a cheap good player, and ranking by points alone missed up to 2.6 points at N=40; the union measured a worst shortfall of 0.14 points. The bench variables are binary, which CBC solved faster than the continuous form. **If bidding feels slow on draft day, `BENCH_CANDIDATES_PER_POSITION` is the dial.**

### Owner decisions (2026-08-06)

- The league **commissioner software refuses any bid that would leave a team unable to fill a full roster**. So `remaining_budget < spots_remaining * MIN_SALARY` is unreachable through legal bidding, and the MILP's `== spots` constraint is correct rather than over-strict — don't "fix" it.
- **Going over the cap warns, it does not refuse.** The league permits temporary over-cap states that get resolved by buyouts, so every endpoint that can raise a team's cap load executes and returns a warning toast naming the team and the overage — `/trade-between`, `/trade-execute`, `/assign`, `/adjust-salary`, `/move-to-roster`. Use the shared `_cap_overages()` helper; any new cap-raising endpoint joins the list.
- Drafting past 24 **auto-routes to the minors** rather than being blocked, so a live sale never gets stopped by the tool.

### Owner decisions (2026-08-07)

- **Two team keys in the template context, and they mean different things.** `viewed_team` is the roster on screen and is read by `team_panel.html` alone; `team` is always BOT and is what the Trade "I Give" list and Buyout Analyzer act on. **Never point a panel other than `team_panel.html` at `viewed_team`**: that is the 2026-08-05 leak that put an opponent's players in BOT's trade form, and `TestPanelContextIsolation` exists to catch it.
- **The view lives in `main._viewed_team`, not in the request.** `_context` reads that global; `GET /team-view/{code}` and `_view_team()` are the only things that write it (the latter from `/assign`, `/undo`, `/buyout`, `/reset` and `/load-scenario` — see below), and every other endpoint preserves it by doing nothing. Endpoints used to carry a team code through `_panels_viewing()`, which failed open — the ones that forgot (`/team-done`, `/trade-execute`, and the error branch of all five roster-edit endpoints) silently threw you back to BOT. Do not reintroduce a per-request view. Do not move it onto `AuctionState` either: there it would serialize into the save file and `/undo` would restore a *view*, which is not a draft action.
- **A test that exercises the view must open the panel first** (`GET /team-view/SRL`), because that is now the only thing that sets it — and it is also the only way the edit happens for real, since every roster-edit control renders inside `team_panel.html`. Posting an edit for an opponent no longer implies you are looking at them, which is how the move to a global silently disarmed `TestPanelContextIsolation`: with the view still on BOT, the leak mutant leaked BOT into BOT and the guard passed.
- **The view follows whichever roster the action changed** — `_view_team(code)`, which replaced an unconditional `_view_my_team()` on 2026-08-11. **`/assign` points it at the buyer** (owner decision 2026-08-08, amending 2026-08-07): on your own pick that is still BOT, which is the only case the original reasoning was ever about — reading an opponent's Cap Used as yours at the moment a pick of *yours* lands. On an opponent's pick nothing of yours moved and the panel that just went stale is theirs, so it swaps to them; `team_panel.html` renders the **← My Team** link exactly then, and League State still carries BOT's budget in the same response. Success path only: a *rejected* assign is not a draft action, and the error branches return before the write. `/buyout` passes the team whose player was bought out — BOT for your own, the rival for theirs, since 2026-09-12 made `execute_buyout` take a team at all; `/reset` and `/load-scenario` pass `MY_TEAM` because they replace the world. Moving the view on a pick is only safe because of the two bullets above — `viewed_team` reaches `team_panel.html` and nothing else, and `team` stays BOT. Note `/assign` returning `all_panels.html` is **correct** and is not the mistake the next bullet's `/team-view` rule describes: the sale ends that bidding session, so replacing `#bid-panel` is the point. A backlog entry asserted the opposite for three days and that is what kept this deferred.
- **`/undo` mirrors the view policy of the action it reverted**, read off the same `pre_txn[-1]` the toast already uses — no `state.py` change and no `restore_snapshot` reporting what it undid, which is the other thing that kept this deferred. A reverted `draft` or `buyout` points the view at `t.team_code`; **everything else leaves it alone.** The two are one branch because both log the team whose roster moved — which for a buyout was always `MY_TEAM` until 2026-09-12 and is now whichever team the panel's picker named. The `/undo` code needed no change for that: it read `t.team_code` all along. Trades change two rosters, so there is no single answer and both forward endpoints deliberately touch nothing. A change-log undo leaves it alone because the roster-edit endpoints do — and because **`team-done` is a `ChangeRecord` kind too**, so mirroring `pre_chg[-1].team_code` would swap the panel to an uninvolved third team, exactly what the 2026-08-07 fix removed (`test_marking_another_team_done_does_not_snap_the_panel_back`). The gap that accepts: edit an opponent, navigate away, then `Ctrl+Z`, and the view stays put — your `/team-view` click is newer information than the log.
- **Allowlist `transaction_type`, never denylist it, and `_view_team` validates.** The real vocabulary is `draft` / `trade_out` / `trade_in` / `trade` / `buyout`; `state.py`'s field comment said `trade_give`/`trade_receive` — two values the code has never emitted — until 2026-08-11, and a first draft of the `/undo` rule was reasoned against it. `/trade-between` logs `team_code` as `f"{source}→{dest}"`, so that field is **not always a team code**: a denylist that missed one string would point the view at `"SRL→MAC"`. **The Logs panel is the one deliberate exception, and it inverts the reasoning rather than ignoring it**: `logs_panel.html` splits the log `draft` vs *everything else*, because a record matching no tab disappears from the log entirely, which is worse than one landing in the wrong tab — so the two lists always sum to `len(transaction_log)` and a new type is visible by default. The `"SRL→MAC"` hazard is handled where it actually bites, in `_log_team_link.html`, which renders a `/team-view` link only when `row.team_code in teams` and plain text otherwise. `_view_team` therefore ignores a code that is not in `auction_state.teams`, giving both writers of the global one contract (the same rule as `/team-view/FAKE`) and making "`_viewed_team` is always a live team code" a real invariant. `_context`'s `teams.get(_viewed_team, team)` fallback stays as belt-and-braces rather than as the mechanism: it is silent, and it renders BOT's roster *and* BOT's Scan gate from the same fallback object, so a dead code would look completely normal on screen while every later `/team-view` no-op'd on top of the garbage. The two guards cover different mutants, and **each needs its own test** — measured 2026-08-11, not reasoned. Widening the allowlist is caught only by the `/trade-execute` case, whose logged codes are real team codes; removing the guard is caught only by `test_the_view_is_always_a_live_team_code`. The `/trade-between` case catches **neither alone** — with the guard in place a widened allowlist no-ops on `"SRL→MAC"`, and with the correct allowlist a missing guard is never handed a bad string — it fires only on the *combination*. An earlier draft of this bullet claimed it pinned the guard by itself; it does not.
- **An unknown team code changes nothing.** `/team-view/FAKE` leaves the view where it was rather than falling back to BOT, so a stale link cannot move your panel.
- **Buying out is two controls, and the split is advice vs record.** The **Buyout Analyzer** (`buyout_panel.html`, `/buyout-check`) is BOT-only advice: `evaluate_buyout` scores the hypothetical against BOT's MILP total and the panel offers Execute only when the verdict is `buyout`. The **team panel's picker** (`team_panel.html`, posting `team_code` + `player` to `/buyout`) is the record: it renders for whichever team `viewed_team` names and executes whatever the league actually did, a `keep`-rated buyout included — which until 2026-09-12 could not be entered for BOT either, and for a rival could not be entered at all, leaving that team's cap wrong in every calculation the tool makes about them. `evaluate_buyout` deliberately did **not** follow `execute_buyout` in taking a team: run on SRL's roster it would answer a question about BOT. The picker is a `<select>` for the reason the Analyzer became one and it has no confirm dialog (owner decision 2026-09-12): the buyout is a logged transaction and `Ctrl+Z` reverts it. Field name is `player`, never `player_name` — that is `/buyout-check`'s query param, and two pickers sharing a name is a reader that cannot tell them apart. **Buyout dots stay BOT-only.** `_solve_buyout_indicators` scores every hypothetical against BOT's MILP total, so the scan cannot answer anything about an opponent; their panel renders no dot placeholders rather than ones that stay grey forever. The Scan button is gated to match, on the derived `buyout_dots_on_screen` boolean — a DOM fact about whether the OOB swap targets exist, deliberately not `viewed_team`, which the bullet above forbids that template from reading. Ungated, every one of its 11 OOB swaps missed and htmx logged `htmx:oobErrorNoTarget`.
- **`/team-view` returns the team panel plus out-of-band fragments** (`team_view_response.html`). It must not return `all_panels.html` — that replaces `#bid-panel` and destroys the bidding session, which lives only in the DOM. So anything outside the team panel whose rendering depends on *which* team is on screen has to come back OOB, one fragment at a time; today that is `buyout_scan.html`, and a second one joins the list rather than widening the swap. Its wrapper div is unconditional and only the `hx-swap-oob` attribute is conditional (`scan_oob`): a swap target that disappears with its contents can only be swapped one way, which is exactly the bug — the Scan button vanished on the way to an opponent and never came back.

## Key design decisions

| Decision | Why |
|---|---|
| Three-layer pricing | Model alone ignores budget constraints. Market layer ensures bids reflect reality — always for the bid advisor, and for the MILP's planning prices **essentially never**: replayed over a real 139-pick draft that spent 98% of the league cap, the idle ceiling cut **0** pool prices (`tests/measure_replay.py`, 2026-09-13). This row said "only once the league has actually spent its cap" until then, which the draft falsified — spending hard does not bind a second-highest-of-ten ceiling, two teams going broke does. Kept anyway, by owner decision the same day: it is one `min()`, and the ceiling that does the work is the **live** one the advisor uses. |
| Market ceiling from exact budgets | Perfect visibility during draft. Use it. |
| "Team done" toggle | 3+ teams finish early per draft. Their dead budget distorts market calculations if not excluded. |
| Trade eval via hypothetical MILP | Same optimizer, just run on a cloned state. No new algorithm needed. **Both sides get the same buyout menu** (none, or any one legal buyout), and the trade must beat the best NO-trade option: until 2026-09-25 only the trade side got buyouts, so dumping one of BOT's own bad contracts read as a trade gain. |
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

**Never hard-code a player name in a test** — enforced since 2026-08-19 by
`tests/test_no_literal_player_names.py`, which walks every test file's string
constants against the loaded pool; the one allowlisted file carries its reason in
`NAMES_ARE_THE_POINT`. It checks full names only, so a bare *surname* assertion
still slips through — assert the derived name, not a fragment of it. Derive the
target from the loaded state by the ROLE it needs to play — BOT's worst points-per-dollar keeper, the
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

**Preparing a baseline that survives `/reset`.** `POST /reset` rebuilds every
team from `data/players.csv` + `data/fchl_teams.json` and reads the saved state
not at all, so pre-draft prep — recalling a prospect, demoting a keeper — is
destroyed by the first reset after it. None of the four `_backfill_*` helpers
saves it: they fill a blank NHL club, set `is_keeper`, copy a logo, and repair a
legacy snapshot's model inputs. **Nothing moves a player between the roster and
the minors.** So the baseline lives in the data files, and `bake_roster_state.py`
puts it there:

1. Prep in the UI — recalls and demotions.
2. `.venv/bin/python bake_roster_state.py` — dry run, prints the diff.
3. Read it, then re-run with `--write`, `git diff`, and commit.
4. Test freely. `/reset` now returns to the prepared baseline.

It carries **exactly one thing**, which list a player is in (`STATUS`), and it
**refuses** on a state holding transactions or acquired players rather than
writing draft picks into the pool file as keepers; a refusal exits 1 with the
reason on stderr and both files byte-identical. **Penalties and salaries are
hand edits of the data files** — `penalty` in `fchl_teams.json`, `SALARY` in the
pool — and the bake only REPORTS where the state disagrees, with both figures.
It carried penalties until 2026-09-22 and that half could only do harm: no
pre-draft UI action produces a penalty it would accept (`execute_buyout` logs a
transaction), so a disagreement meant a state older than a hand edit, and baking
it reverted the edit — SHF's $2.8M went to $0.0M. `fchl_teams.json` is also
shared by every pool while `players.csv` is not. Its defaults follow the server's
(`FCHL_PLAYERS_CSV`, `FCHL_STATE_DIR`, via `data_loader.default_state_dir`), and
it writes `.tmp` then `os.replace`, having rendered the whole file first. It
never adds or deletes a row: dropping a player from the league — the
no-NHL-contract case, where the CBA charges no buyout penalty because there is
no contract to buy out — is a deliberate hand edit, and deleting his row is the
only way to say "gone this season". Two things it cannot carry and reports every
run: `is_done`, and bench flags, which have no column and cost nothing —
**`is_bench` reaches no engine module**, `lineup_points` scores the best
12F/6D/2G from every roster player regardless, and none of
`total_salary`/`roster_count`/`spendable_budget`/`physical_max_bid` reads it. Do
**not** re-run `convert_fchl_online.py` over a baked file: it derives `STATUS`
from the contract group and would overwrite every placement, restore every
deleted row, and put back the export's `SALARY` and `PRIOR FCHL TEAM` over any
hand correction. Since 2026-09-22 it lists all of them and **refuses** unless
`--force` (measured on the live pool that day: three recalls and one dropped
player); a refresh from a new export is `--force`, then a re-bake, then the
hand edits again. `PTS` and `NHL TEAM` are not guarded, because a new workbook
or export moves them legitimately — which is also why a hand-edited projection
does not survive a re-run.

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

**The mutant harness has two more ways to lie, both seen 2026-08-20.** (1) A loop
that runs past its tool timeout is killed with SIGTERM, `finally` never runs, and
the file is left **mutated** in the working tree — a later `git add -A` would
commit it. Install a `SIGTERM`/`SIGINT` handler that restores, run anything over
a few minutes detached rather than in the foreground, and verify restoration by
comparing the file against the original text, never by assuming the `finally`
fired. (2) Backticks inside a double-quoted `python3 -c "..."` are command
substitution: a patch that writes a comment containing `` `code` `` silently
writes it with the backticked text **deleted**. Use a quoted heredoc
(`<<'PY'`) for anything that embeds code in prose, and read back what landed.
That one put a factually wrong claim into a comment — the reasoning it stated had
not been measured, and when measured it was false.

**Also 2026-08-20: a data guard can mask the flag beside it.** `{% if show_prior
and pick.player.prior_fchl_team %}` — flipping `show_prior`'s default and passing
it explicitly *both* survived the whole suite, because `players.csv` fills PRIOR
FCHL TEAM for 22 of 2158 rows and no UFA is one of them (still 22, of 1268 rows,
after the 2026-09-15 refresh — the column is an RFA column by construction). Equivalent mutants, not
coverage. When a mutant survives, ask whether the data makes it equivalent before
concluding the code is untested — and if the data is what saves you, pin the
intent by supplying the data that would break it, because `players.csv` is
replaced before every draft.

**2026-09-17 collected the bill on that, three times in one commit, and the
lesson is sharper than "pin the intent": a test that DERIVES its subject from
the pool has a precondition it usually does not state.** Deleting one row and
recalling three prospects broke three unrelated tests, none of which was wrong
about the code. `test_every_rendered_colour_class_is_defined` scanned for a
contract the MILP would buy out, and the pool's only one was the deleted player
— so it reached `KEEP` alone and every colour on the `BUYOUT` branch silently
went unchecked, the same shape as the `show_prior` mutant. `test_trade_buyout_undo`'s
`targets` fixture took three independent argmaxes over one roster and asserted
they were distinct, which the data had been arranging for free until BOT's
dearest contract also became its top scorer. And `_scenario_endgame_ceiling_binds`
drained BOT to a budget target, which quietly became a roster-size target once
BOT started three players richer — leaving a full roster, the one state the
advisor cannot advise in. **Each fix is the same move**: derive the subject so
it cannot collide (exclude `keep` before picking `buyout`), or SUPPLY it
(`/adjust-salary` makes a bad contract when the pool has none), or CAP the loop
so the intended constraint is the one that binds (`up_to=ROSTER_SIZE - 2`).
Ask of any data-derived test: *what is it about today's pool that makes this
work, and would I notice if that changed?* All three answered "no" and all three
passed on the pool before.

## Code conventions

- Python 3.12+ (the venv runs 3.14.4 — verified 2026-08-19; nothing pins a version, so this is the floor, not the target), type hints on signatures
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

**Always name the enclosing function/property in `(symbol)`.** Line numbers drift whenever anything above them changes; on 2026-08-05 a third of this file's references pointed at unrelated code. The symbol survives the drift and keeps the entry greppable. **A template has no symbol, so it carries a literal ANCHOR in the same position instead** — a distinctive string from the cited line, e.g. `templates/partials/league_state.html:NNN (table-scroll-x)` — `NNN` for the same reason the example above uses it. It used to carry a line and nothing else, on the documented grounds that there was no symbol to name; that was a real reason and a costly one. Measured 2026-08-20, **six of the nine** template references across the two docs had drifted, one by 35 lines, with `tests/test_backlog_refs.py` green on every one. The anchor must be **on** the cited line, not merely in the file (`table-scroll-x` is on three lines of `team_panel.html`), and comparison strips backticks and collapses whitespace so a re-indent does not fail an entry. A missing parenthetical is now a loud failure on both paths, so a new entry cannot opt out. The parenthetical must sit **inside the same backtick span as the digits** — the regex looks for `\s*\(` immediately after the line number, so `` `foo.html:43` (anchor) `` does not parse. **A *historical* line number is not a reference and must not be written in `file:line` form**: a changelog sentence saying which line used to be wrong points nowhere you should go, and writing it as a reference makes the guard flag the changelog forever — name the file and describe the drift instead. The general lesson from that entry, which sat deferred for three days on a cost it had counted once: **a deferral whose trigger is "if it bites again" needs the cost measured at defer time**, because nothing will measure it later; it had already bitten four more times unnoticed.

Before appending, scan `BACKLOG.md` for an existing entry covering the same symbol + finding — update the date instead of duplicating. When a change shifts line numbers in a file the backlog references, re-anchor those entries in the same commit — **every** reference into that file, not only the ones `tests/test_backlog_refs.py` fails. The guard checks that the line is inside the named symbol's span, so a reference that slides within its own function stays green while pointing at a docstring: on 2026-09-22 two `_search_rows` references and one `buyout` reference had slid onto a comment, a docstring and an unrelated line, through commits that each re-anchored exactly the failures.

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

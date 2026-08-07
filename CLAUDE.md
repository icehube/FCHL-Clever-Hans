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
| GET | `/buyout-check/{name}` | Preview buyout impact (BUYOUT/KEEP) |
| POST | `/buyout` | Execute buyout (50% penalty) |
| GET | `/buyout-indicators` | Lazy-load buyout dots via HTMX OOB swap |
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
- **Buyout indicators**: A manual "Scan Roster" button fires `GET /buyout-indicators` (one MILP solve per roster player), which returns OOB-swapped green/red dots into the placeholder dots.
- **Atomic saves**: `_save_state()` writes to `.tmp` then `os.replace()` (POSIX atomic). Previous state kept as `.backup`.
- **Responsive layout**: CSS grid with 1-col (mobile), 2-col (768px+), 3-col (1024px+) breakpoints.
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

- **Two team keys in the template context, and they mean different things.** `viewed_team` is the roster on screen and is read by `team_panel.html` alone; `team` is always BOT and is what the Trade "I Give" list and Buyout Analyzer act on. Roster edits carry the view via `_panels_viewing()` — `/toggle-bench`, `/adjust-salary`, `/move-to-minors`, `/move-to-roster`, `/trade-between`. **Never point a panel other than `team_panel.html` at `viewed_team`**: that is the 2026-08-05 leak that put an opponent's players in BOT's trade form, and `TestPanelContextIsolation` exists to catch it.
- **Draft actions reset the view to BOT.** `/assign`, `/undo`, `/buyout` and a page load return the panel to your own team by omission — deliberate, because reading an opponent's Cap Used as yours right after a pick lands is worse than re-opening their roster.
- **Buyout dots are BOT-only.** `_recompute_buyout_indicators` scores every hypothetical against BOT's MILP total, so the scan cannot answer anything about an opponent; their panel renders no dot placeholders rather than ones that stay grey forever.

## Key design decisions

| Decision | Why |
|---|---|
| Three-layer pricing | Model alone ignores budget constraints. Market layer ensures bids reflect reality. |
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
`toast_of`), not in `conftest.py` — that file is for fixtures. Import from there
rather than copy-pasting; `squeeze` reached three copies before it was folded in.

**A test must be able to fail.** Before claiming one covers something, break the
thing it claims to cover and watch it go red. Three tests in this suite asserted
nothing for months (`len(...) >= 0`, a `pass`-body loop, `status_code in (200,
404, 422, 500)`) and every one of them read as coverage. When the mutation
doesn't fail the test, either the assertion is wrong or the test is aimed at the
wrong operation — the stress ownership invariant could not fail under `/assign`
at all, and only became real once it also ran after `/undo`.

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

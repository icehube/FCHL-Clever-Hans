# FCHL Auction Manager

A live auction draft tool for an 11-team fantasy hockey league. During a multi-hour, 150+ pick auction, the simulator tracks all teams, computes market-adjusted bid limits, recommends nominations, provides real-time bidding advice, evaluates trades and buyouts on the fly, and recalculates the ideal roster after every transaction.

**Stack**: FastAPI + HTMX + Jinja2 + PuLP (MILP solver)

## Quick start

```bash
pip install -r requirements.txt
uvicorn main:app --reload
# Opens at http://localhost:8000
```

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
<!-- Nord theme rules in .claude/rules/nord-theme.md (loaded when editing CSS/HTML) -->
<!-- Data format specs in .claude/rules/data-formats.md (loaded when editing data/) -->

All state-modifying endpoints trigger: update state -> recompute market prices -> re-solve MILP -> save snapshot -> return HTML partials.

### Full endpoint reference

| Method | Route | Purpose |
|--------|-------|---------|
| GET | `/` | Main page with all panels |
| POST | `/assign` | Draft player to team (validates team, clamps salary) |
| POST | `/bid-check` | Live bidding advice (BID/CAUTION/DROP) |
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
| POST | `/trade-between` | Execute trade between non-BOT teams |
| GET | `/state` | JSON state dump for debugging |

### UI patterns

- **Toast notifications**: Mutation endpoints return `HX-Trigger: {"showToast": {...}}` header. JS listener in `shortcuts.js` shows auto-dismissing alerts.
- **Lazy buyout indicators**: Roster panel renders grey placeholder dots, then `hx-trigger="load"` fires `GET /buyout-indicators` which returns OOB-swapped green/red dots.
- **Atomic saves**: `_save_state()` writes to `.tmp` then `os.replace()` (POSIX atomic). Previous state kept as `.backup`.
- **Responsive layout**: CSS grid with 1-col (mobile), 2-col (768px+), 3-col (1024px+) breakpoints.

## Auction rules (from CBA)

- UFA: circular bidding, $0.1M increments, drop out = permanent for that player
- RFA: secret bids, prior team can match (ROFR)
- Combo: 1 RFA + 1 UFA per nomination turn
- Min salary $0.5M, max $11.4M
- Roster: 24 active (playing: 12F + 6D + 2G, bench: 4 any position). Teams can draft beyond 24 -- extras go to minors with salary fully on cap. Teams can also finish with fewer than 24.
- Snake draft for nominations
- Trades allowed during auction breaks
- Buyouts: player removed, 50% salary penalty remains on team's cap
- Teams can voluntarily stop drafting before filling all 24 spots

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

## Development workflow

Verification loop for every change:

1. Make changes
2. Run tests: `pytest tests/ -v`
3. Fix any failures before moving on
4. Before committing: run full test suite

```bash
pytest tests/ -v              # Run all tests
pytest tests/test_market.py   # Run specific module tests
```

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
| `/commit-push-pr` | Commit, push, and open a PR |
| `/quick-commit` | Stage all changes and commit with a descriptive message |
| `/test-and-fix` | Run tests and fix any failures |
| `/review-changes` | Review uncommitted changes and suggest improvements |
| `/worktree` | Create a git worktree for parallel Claude sessions |
| `/grill` | Adversarial code review -- don't ship until it passes |
| `/techdebt` | End-of-session sweep for duplicated and dead code |

## Subagents

| Agent | Purpose |
|---|---|
| `code-simplifier` | Simplify code after Claude is done working |
| `code-architect` | Design reviews and architectural decisions |
| `verify-app` | Thoroughly test the application works correctly |
| `build-validator` | Ensure project builds correctly for deployment |
| `oncall-guide` | Help diagnose and resolve production issues |
| `staff-reviewer` | Review plans and architectures as a skeptical staff engineer |

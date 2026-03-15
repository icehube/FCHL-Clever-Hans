# FCHL Auction Simulator — Implementation Plan

## Context

All data files and specs are in place, but zero application code exists. This plan defines the build order for the complete auction simulator: 7 Python modules, 10 templates, 2 static files, 8 test modules, and requirements.txt — roughly 2,600 lines across 25+ files.

The build follows the dependency graph bottom-up: `config → state → data_loader / price_model → market → optimizer → trade → main + templates`. Each step is independently testable.

## Validated Data Facts

- BOT: 12 keepers ($28.3M), 37 minors ($2.0M cap-eligible), $20.5M spendable, needs 7F+4D+1G
- 165 total picks needed league-wide, 1,910 biddable players (705 with PTS > 0)
- Only NHL team alias needed: `UTH` → `UTA` (78 players)
- JHN and LGN each have $0.3M pre-auction penalty

## Recommendations vs Build Plan v3

The v3 plan is solid. My changes:

1. **Merge phases 1-2 into 4 smaller steps** (config+state, data_loader, price_model, market) — each is one-shot buildable and testable
2. **Add `tests/__init__.py`** to step 1 (forgotten in v3)
3. **Filter MILP candidates to PTS > 0** (~705 players, not 1,910) — keeps CBC solve time well under 500ms
4. **Handle RFA group conversion in `/assign`** — RFA1→GROUP 2, RFA2→GROUP 3 on draft
5. **Track snake draft state** — `nomination_round` + `nomination_index` in AuctionState, reverse order on even rounds, skip `is_done` teams

### Prerequisite before Step 1
- **Add `PRIOR FCHL TEAM` column to `data/players.csv`** for the 22 RFA players. You'll need to fill in which FCHL team previously held each RFA. All non-RFA rows get a blank value. This enables ROFR display (e.g., "BOT can match at $X.XM").

---

## Build Steps

### Step 1: `config.py` + `state.py` + `requirements.txt`
**Complexity**: Simple (~200 lines)
**Depends on**: Nothing

**Files**:
- `config.py` — all constants from CLAUDE.md (SALARY_CAP, MIN_SALARY, etc.) plus `NHL_TEAM_ALIASES = {"UTH": "UTA"}` and `DEFAULT_TEAM_PROBABILITY = 0.031`
- `state.py` — dataclasses: `Player`, `PlayerOnRoster`, `TeamState`, `TransactionRecord`, `AuctionState`
  - `TeamState` properties: `total_salary` (keepers + cap-eligible minors + penalties), `remaining_budget`, `roster_needs`, `spendable_budget`, `physical_max_bid`
  - `TeamState` keeps separate lists: `keeper_players` (START), `minor_players` (MINOR), `acquired_players` (drafted)
  - Minor salary on cap: GROUP 2/3 yes, everything else no
  - `AuctionState`: teams dict, available_players dict, transaction_log, nomination tracking, snapshot stack (50 max)
  - JSON serialization/deserialization for save/restore/undo
- `requirements.txt` — fastapi, uvicorn[standard], jinja2, python-multipart, pulp, pytest, httpx
- `tests/__init__.py`
- `tests/test_state.py` — salary math, budget properties, roster needs, serialization round-trip, snapshot save/restore

**Verify**: `pytest tests/test_state.py -v`

---

### Step 2: `data_loader.py`
**Complexity**: Medium (~150 lines)
**Depends on**: Step 1

**Files**:
- `data_loader.py`
- `tests/test_data_loader.py`

**Key functions**:
- `load_team_metadata("data/fchl_teams.json")` → team configs, nomination order, penalties
- `load_team_odds("data/team_odds.json")` → odds dict with UTH→UTA alias, 0.031 default
- `load_players("data/players.csv", team_odds)` → `(dict[str, TeamState], dict[str, Player])`
  - Classify by STATUS + FCHL TEAM: keepers (START+team), minors (MINOR+team), biddable (UFA/RFA)
  - Set `is_rfa = GROUP in ("RFA1", "RFA2")`
  - Set `team_probability` from odds with alias mapping
  - UFA salary: store but mark as stale (don't use in pricing)
  - RFA `PRIOR FCHL TEAM` column: store on Player for ROFR display
- `build_initial_state()` → full `AuctionState`

**Critical tests**:
- BOT: 12 keepers, 37 minors, total_salary=30.3, remaining=26.5, spendable=20.5
- 1,910 biddable (1,888 UFA + 22 RFA), 705 with PTS > 0
- Connor McDavid: is_rfa=True, group=RFA2
- EDM player team_probability=0.1104, UTH player maps to 0.0202
- JHN penalty=0.3, LGN penalty=0.3
- Nomination order matches fchl_teams.json

**Verify**: `pytest tests/test_data_loader.py -v`

---

### Step 3: `price_model.py`
**Complexity**: Medium (~120 lines)
**Depends on**: Step 1 (can run in parallel with Step 2)

**Files**:
- `price_model.py`
- `tests/test_price_model.py`

**Key functions**:
- `load_model_params("data/model_params.json")` → per-position coefficients
- `predict_price(position, projected_points, team_probability, is_rfa, params)` → `PricePrediction`
  - Stage 1: logistic → P(floor) using `floor_*` coefficients
  - Stage 2: log-normal → median, expected price using OLS coefficients
  - `sigma = max(sigma_floor, sigma_intercept + sigma_slope * pts)`
  - `expected = p_floor * 0.5 + (1 - p_floor) * exp(log_mu + sigma²/2)`
  - Clamp to `[min_bid, max_bid]` per position
- `predict_all_prices(players, params)` → dict of predictions

**Tests**: high-pts forward predicts above floor, 0-pts near floor, RFA increases price, goalie sigma wider, all clamped to bounds

**Verify**: `pytest tests/test_price_model.py -v`

---

### Step 4: `market.py`
**Complexity**: Medium (~150 lines)
**Depends on**: Steps 1-3

**Files**:
- `market.py`
- `tests/test_market.py`

**Key functions**:
- `compute_opponent_ceiling(team, position)` → `float | None`
  - `physical_max = remaining_budget - (spots_remaining - 1) * MIN_SALARY`, capped at MAX_SALARY
  - Returns None if is_done or no roster need for position
- `compute_market_ceiling(position, all_teams, exclude=MY_TEAM)` → `MarketInfo`
  - Second-highest physical_max among active opponents needing position
  - demand_count = number of such teams
- `compute_market_price(model_price, market_info)` → `min(model_price, ceiling)`
- `compute_all_market_prices(players, model_prices, all_teams)` → full dict
- `compute_live_ceiling(active_bidders, teams, position)` → ceiling from specific bidders

**Tests**: physical_max math, is_done exclusion, second-highest logic, 0-demand→floor, live ceiling

**Verify**: `pytest tests/test_market.py -v`

---

### Step 5: `optimizer.py`
**Complexity**: Complex (~350 lines)
**Depends on**: Steps 1-4

**Files**:
- `optimizer.py`
- `tests/test_optimizer.py`
- `tests/test_bid_calculator.py`
- `tests/test_nomination.py`

**Key functions**:
- `solve_optimal_roster(team, available_players, market_prices, excluded, forced)` → `MILPSolution`
  - Maximize Σ(pts × x[i]), subject to budget + position constraints (≥ for safety)
  - Only candidates with PTS > 0 (~705 players)
  - Uses market-adjusted prices, never raw model prices
- `compute_marginal_value(player, team, available, market_prices)` → float
  - Binary search: salary where with-player = without-player in points
- `compute_bid_recommendation(player, team, ...)` → `BidRecommendation`
  - `max_bid = min(marginal_value, ceiling + 0.1, spendable)`
  - Action: BID / CAUTION / DROP
- `generate_counterfactual(player, salary, team, ...)` → side-by-side comparison
- `recommend_nomination(team, ...)` → `(RFA pick | None, UFA pick)`
  - Strategies: target (want to buy), drain (force opponents to spend), depth (fill cheap)
  - Snake draft: reverse order on even rounds, skip is_done teams

**Tests**: valid roster (positions, budget), marginal value ordering, bid ≤ ceiling, nomination returns RFA+UFA, counterfactual shows alternatives, solve < 500ms

**Verify**: `pytest tests/test_optimizer.py tests/test_bid_calculator.py tests/test_nomination.py -v`

---

### Step 6: `trade.py`
**Complexity**: Medium (~200 lines)
**Depends on**: Steps 1-5

**Files**:
- `trade.py`
- `tests/test_trade.py`

**Key functions**:
- `evaluate_trade(state, give, receive, market_prices, auto_check_buyouts=True)` → `TradeEvaluation`
  - Solve MILP on current state vs cloned post-trade state
  - Auto-test buyout of each received player, pick best scenario
- `evaluate_buyout(state, player_name, market_prices)` → `BuyoutEvaluation`
  - Remove player, add penalty (salary × 0.5), re-solve MILP
- `execute_trade(state, give, receive, buyout_players)` → mutated state
- `execute_buyout(state, player_name)` → mutated state

**Tests**: accept when pts gained, decline when pts lost, buyout penalty math, auto-buyout finds better option, execute modifies state correctly

**Verify**: `pytest tests/test_trade.py -v`

---

### Step 7: `main.py` + Templates + Static
**Complexity**: Medium-High (~1,100 lines total)
**Depends on**: Steps 1-6

**Files**:
- `main.py` — FastAPI app, 13 endpoints, startup loader, `recompute()` after state changes
  - `/assign` must handle RFA group conversion (RFA1→GROUP 2, RFA2→GROUP 3)
  - All state-modifying endpoints: snapshot → mutate → recompute → save → return partials
- `templates/base.html` — HTML shell, HTMX CDN, CSS, dark mode
- `templates/index.html` — single-page grid layout, all panels visible
- `templates/partials/` — 8 partials (auction_control, nomination, roster_panel, bid_limits, league_state, explanation, trade_panel, buyout_panel, transaction_log)
  - Each uses `hx-swap-oob` for multi-panel updates from single endpoints
- `static/style.css` — dark mode, compact layout for multi-hour auction day
- `static/shortcuts.js` — Ctrl+Z undo, keyboard shortcuts
- `tests/test_endpoints.py` — all 13 endpoints, state mutation verification

**Verify**: `pytest tests/test_endpoints.py -v && uvicorn main:app --reload`

---

## Parallelism

```
Session 1 (critical path): Step 1 → Step 2 → Step 4 → Step 5 → Step 6
Session 2 (after Step 1):  Step 3 (price_model — no dependency on data_loader)
Session 3 (after Step 5):  Step 7 templates + static (endpoint contracts known)
```

## Full Verification

After all steps: `pytest tests/ -v` then manual browser test at `http://localhost:8000`.

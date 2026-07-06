# Pricing Pipeline (Critical Domain Concept)

Three layers, each adding real-world context:

```
price_model.py  -->  market.py  -->  optimizer.py
(Layer 1)            (Layer 2)       (Layer 3)
Historical           Market          Decision
prediction           reality         engine
```

**Layer 1 -- Model price** (`price_model.py`): What the historical model says a player typically sells for. Two-stage per-position log-normal model trained on 8 seasons of data (round-2 rebuild, July 2026). A starting point -- a prediction in a vacuum.

- **Stage 1** (logistic): P(sells at $0.5M floor). **Stage 2** (OLS on log salary): price distribution for above-floor players.
- **Skaters are piecewise-linear in points**: hinge terms `max(pts-60, 0)` (F and D) and `max(pts-80, 0)` (F only) capture star-threshold kinks; the pts^2 coefficient is exported as 0.0.
- **Goalies are priced on projected WINS** (`Player.proj_wins`, from `data/goalie_projection_stats.csv`), not the 2W+3SO composite -- shutouts are unprojectable noise. Fallback when wins are missing: `pts / metadata.goalie_pts_per_win` (~2.31).
- **Reputation feature**: last season's salary (`Player.salary` for biddables; 0 = new to league) feeds `log_lag`/`has_lag`.
- **Scarcity feature**: `Player.pos_rank` = rank by projected points within position, computed once against the draft-time pool and frozen -- never re-rank the shrinking pool mid-draft.
- **Units**: `team_probability` is in PERCENT (EDM = 11.04, league sums to ~100). `team_odds.json` stores fractions; `load_team_odds` converts.
- **Sigma** is a function of the predicted log-price (not points): `max(sigma_intercept + sigma_slope * log_pred, sigma_floor)`; exported values are already MAD->SD corrected.
- **Expected price** = `p_floor * 0.5 + (1 - p_floor) * clipped-lognormal MEAN` (closed form) -- never the median, which under-forecasts total spend.
- Unused features export coefficient 0.0, so one formula serves F/D/G.
- Golden test: `tests/fixtures/auction_predictions_current.csv` (exported by the pricer notebook alongside the params) must be reproduced within rounding by `predict_price`. When `data/model_params.json` is regenerated, copy the matching predictions CSV into the fixture too.

**Layer 2 -- Market price** (`market.py`): Adjusts model prices using real-time auction state. Computes market ceilings from each opponent's exact remaining budget, roster needs, and minimum reserve requirements. We have perfect budget visibility during the draft, so these calculations are precise. Teams marked as "done" are excluded from market calculations.

**Layer 3 -- Bid recommendation** (`optimizer.py`): Uses market-adjusted prices in the MILP to plan the optimal roster. Computes BOT's max bid as the marginal value of each player.

- **The MILP maximizes STARTING LINEUP points** (best 12F/6D/2G); bench players score nothing. Joint roster+starter selection: binary x (rostered) and s (starter) per player, s <= x, starter slots capped at 12/6/2.
- Soft bench preference: `BACKUP_TARGETS` (2F/1D/1G -> the classic 14/7/3 shape) earns `BACKUP_BONUS` objective credit per filled slot; bench players' points count at `BENCH_WEIGHT` (10%) so backups are good players. The solver may deviate from 14/7/3 when starters/budget win more than the bonus.
- Position minimums are 12/6/2 (must be able to field the lineup), NOT 14/7/3.
- `MILPSolution.total_points` and `TeamState.current_roster_points` are lineup points (`state.lineup_points`, greedy top-k -- exact).
- **Endgame semantics**: forced players exactly filling the roster = Optimal (not Infeasible); a player whose exclusion makes the roster unsolvable (e.g. last goalie) is valued at physical max; `physical_max_bid < MIN_SALARY` -> DROP with max_bid 0.0, never clamped up.

## Key formulas

**Opponent physical max** (absolute ceiling any team can bid):
```
spendable_budget = remaining_budget - (total_spots_remaining * MIN_SALARY)
physical_max = min(spendable_budget + MIN_SALARY, MAX_SALARY)
```
The `+ MIN_SALARY` accounts for the spot being filled by this bid -- one reserved slot is replaced by the actual bid amount.

**Market ceiling** (highest bidding can realistically reach):
```
ceiling = second-highest physical_max among all active (non-done) opponents
```
Position-agnostic -- any team can bid on any player (extras go to bench or minors). Second-highest because auction price is set when second-to-last bidder drops out.

**Market-adjusted price** (what the MILP uses for roster planning):
```
market_price = min(model_price, market_ceiling)
```

**Final bid recommendation**:
```
recommended_bid = min(marginal_value, market_ceiling + 0.1, physical_max_bid)
```

**MILP budget** (different from single-bid budget):
```
milp_budget = remaining_budget (not spendable_budget)
milp_constraint: must fill exactly remaining_spots players
```
The MILP uses `remaining_budget` because the `== spots` constraint forces filling all slots, so min-salary reservation is implicit. Using `spendable_budget` would double-count the reserve.

## Critical rule

The bid recommendation must **NEVER** exceed what opponents can force BOT to pay. Two ceiling contexts, both computed from OPPONENTS only (BOT's own budget never sets its own cap):

- **Idle/market ceiling** (`compute_market_ceiling`): second-highest opponent physical max -- the expected clearing price if BOT abstains. Feeds the MILP's market prices.
- **Live ceiling** (`compute_live_ceiling`): when BOT is among the active bidders, the HIGHEST opponent max is the price-to-beat (that opponent must drop out for BOT to win); when BOT is only observing, second-highest. Caps the bid advisor.

If no opponent can bid above $5.5M, BOT's max recommendation is $5.6M -- regardless of what the model or marginal value says.

## "Team done" exclusion

When `is_done = True`:
- Team is excluded from market ceiling calculations (their budget doesn't count)
- Team's roster needs are excluded from demand counts
- Team is removed from nomination order
- Zero demand (all opponents done) = floor price

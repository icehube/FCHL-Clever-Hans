# Pricing Pipeline (Critical Domain Concept)

Three layers, each adding real-world context:

```
price_model.py  -->  market.py  -->  optimizer.py
(Layer 1)            (Layer 2)       (Layer 3)
Historical           Market          Decision
prediction           reality         engine
```

**Layer 1 -- Model price** (`price_model.py`): What the historical model says a player typically sells for. Two-stage per-position log-normal model trained on 8 seasons of data. A starting point -- a prediction in a vacuum.

**Layer 2 -- Market price** (`market.py`): Adjusts model prices using real-time auction state. Computes market ceilings from each opponent's exact remaining budget, roster needs, and minimum reserve requirements. We have perfect budget visibility during the draft, so these calculations are precise. Teams marked as "done" are excluded from market calculations.

**Layer 3 -- Bid recommendation** (`optimizer.py`): Uses market-adjusted prices in the MILP to plan the optimal roster. Computes BOT's max bid as the marginal value of each player.

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

The bid recommendation must **NEVER** exceed the market ceiling. If no opponent can bid above $5.5M, BOT's max recommendation is $5.6M -- regardless of what the model or marginal value says.

## "Team done" exclusion

When `is_done = True`:
- Team is excluded from market ceiling calculations (their budget doesn't count)
- Team's roster needs are excluded from demand counts
- Team is removed from nomination order
- Zero demand (all opponents done) = floor price

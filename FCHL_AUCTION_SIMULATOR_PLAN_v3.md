# FCHL Auction Simulator — Project Plan v3

## What Changed Since v2

Three new features from draft-day retrospective:

1. **Trade Evaluator** with automatic buyout analysis
2. **Buyout Analyzer** as standalone feature
3. **Team Done toggle** — mark teams as finished to fix market calculations

Also captured: the old Z-score system failed because it valued players independently rather than as part of an optimal roster. The MILP fundamentally fixes this — it plans the whole team, not individual player value.

---

## Three-Layer Pricing (unchanged from v2)

```
price_model.py → market.py → optimizer.py
(historical)     (reality)    (decisions)
```

Layer 2 (market) now excludes "done" teams from all calculations.

---

## Trade Evaluator (trade.py)

### The Problem

During auction breaks, other owners offer trades. Last year, the only way to evaluate was manually entering the trade, seeing if the optimizer improved, then deciding. This was too slow for live decision-making.

### The Solution

A dedicated trade panel where you input "I give X, I receive Y" and instantly see the optimizer's verdict.

### How It Works

The trade evaluator runs the MILP on hypothetical states:

```
Current state → MILP → optimal roster A (1,285 projected points)

Apply trade to cloned state → MILP → optimal roster B (1,293 projected points)

Verdict: Trade gains +8 projected points. Accept.
```

### Combined Trade + Buyout

The evaluator automatically checks buyout options on every received player:

For each player BOT receives:

- Scenario: keep the player (they're on your roster at their salary)
- Scenario: immediately buy them out (remove player, add 50% salary as penalty)
- Pick whichever produces a better MILP solution

Example:

```
Trade offer: Give Shea Theodore ($3.2M, 31pts)
             Receive Star Player ($6.0M, 75pts) + Dud Player ($3.0M, 12pts)

Evaluator tests:
  A) Keep both:       Net +56pts, cap goes from $12.3M to $6.1M remaining
  B) Keep Star, buy out Dud: Net +44pts, cap $7.6M remaining (Dud's $1.5M penalty)
  C) Decline trade:   Current roster unchanged at $12.3M remaining

Recommendation: Option B if you need cap space for auction.
                Option A if you need every point and can fill cheaply.
```

### Endpoints

```
POST /trade-evaluate
  Input: {give: [{name, salary}], receive: [{name, salary, position, projected_points}]}
  Returns: HTML partial showing comparison table + recommendation

POST /trade-execute
  Input: {trade_id}  (references a previously evaluated trade)
  Effect: Applies trade + any buyouts to real state, triggers full recompute
  Returns: Updated panels
```

### UI

Trade panel is accessible from the main layout. Input form:

- "I give" section: select players from BOT's current roster
- "I receive" section: enter player details (name, salary, position, points)
- "Evaluate" button → shows comparison
- "Execute" button → applies the trade

---

## Buyout Analyzer (trade.py)

### The Problem

At any point before or during the auction, BOT might want to buy out a player on the roster to free cap space. Need to know: is this worth it?

### CBA Rules (Article 11.4)

- Player is removed from roster (their spot opens up)
- Penalty: 50% of player's salary stays on team's cap for remaining contract duration
- Since we ignore term: penalty = 50% of salary, applied immediately
- Net cap space freed = 50% of salary

### How It Works

```python
def evaluate_buyout(state, player_name) -> BuyoutEvaluation:
    # Current situation
    current_milp = solve(state)

    # Hypothetical: buy out the player
    clone = deepcopy(state)
    player = clone.teams[MY_TEAM].remove_player(player_name)
    clone.teams[MY_TEAM].penalties += player.salary * BUYOUT_PENALTY_RATE
    buyout_milp = solve(clone)

    # Compare
    return BuyoutEvaluation(
        player=player_name,
        salary_freed=player.salary,
        penalty_added=player.salary * BUYOUT_PENALTY_RATE,
        net_cap_freed=player.salary * (1 - BUYOUT_PENALTY_RATE),
        current_points=current_milp.total_points,
        buyout_points=buyout_milp.total_points,
        recommendation="buyout" if buyout_milp.total_points > current_milp.total_points else "keep",
        new_targets=buyout_milp.key_differences(current_milp),
    )
```

### Endpoints

```
GET  /buyout-check/{player}  → Preview: cap impact + roster comparison
POST /buyout                 → Execute: remove player, add penalty, recompute
```

### When Buyouts Make Sense

- Player has high salary but low projected points (bad value)
- The 50% freed cap space can acquire a better replacement at auction
- Usually makes sense for players with salary > 2× their market value

---

## Team Done Toggle

### The Problem

During the auction, teams finish drafting before all 24 spots are filled. They leave with unspent cap space. Last year this happened 3+ times. If those teams' budgets are still counted in market calculations, market ceilings are inflated — the system thinks there's more bidding competition than actually exists.

### Implementation

```python
@dataclass
class TeamState:
    ...
    is_done: bool = False
```

When `is_done = True`:

- **Market layer**: team excluded from opponent ceiling calculations, demand counts, and all market price computations
- **Nomination order**: team skipped in snake draft
- **Dashboard**: team shows as "Done" with their final roster and unspent cap grayed out
- **Remaining supply/demand**: their unfilled spots are NOT counted as demand

### Endpoint

```
POST /team-done
  Input: {team_code: "GVR"}
  Effect: Toggle is_done, trigger full recompute of market prices + MILP
  Returns: Updated league state panel + bid limits panel
```

### Impact on Market Layer

This can dramatically change bid recommendations. Example:

- Before toggle: 4 teams need a goalie, market ceiling is $3.2M
- GVR marks done (they needed a goalie but stopped drafting): now 3 teams need a goalie
- If one of the remaining 3 has a low budget: ceiling drops to $1.8M
- BOT's bid recommendation for goalies drops accordingly

---

## Revised Build Plan

### Phase 1: Data Foundation + Price Model (Week 1-2)

- [ ] `config.py`: all league constants including BUYOUT_PENALTY_RATE
- [ ] `state.py`: Player, TeamState (with is_done), AuctionState, serialization
- [ ] `data_loader.py`: parse all input files, build initial state
- [ ] `price_model.py`: predict_price() matching HANDOFF spec
- [ ] Compute Layer 1 prices for all biddable players
- [ ] Tests: data loading, price predictions, state round-trips

### Phase 2: Market Layer + Optimizer (Week 3-4)

- [ ] `market.py`: opponent ceiling, market ceiling, market-adjusted prices
- [ ] Market layer respects `is_done` flag (excludes done teams)
- [ ] `optimizer.py`: MILP team builder using market prices
- [ ] Bid calculator: marginal value, capped at market ceiling
- [ ] Counterfactual generator: "why not bid" explanations
- [ ] Tests: market ceilings, valid rosters, bids ≤ ceilings, done-team exclusion

### Phase 3: Trade + Buyout + Nomination + Bidding Advisor (Week 5)

- [ ] `trade.py`: trade evaluator with auto-buyout analysis
- [ ] Buyout analyzer (standalone)
- [ ] Nomination engine: three strategies + RFA/UFA combos
- [ ] Live bidding advisor: bid/caution/drop with opponent analysis
- [ ] Tests: trade evaluations, buyout math, nominations, bidding advice

### Phase 4: FastAPI + HTMX Frontend (Week 6-7)

- [ ] All endpoints wired up
- [ ] Jinja2 templates with HTMX partial swaps
- [ ] Single-page layout: no tab switching, all panels visible
- [ ] Trade panel: input form + evaluation display
- [ ] Buyout panel: preview + execute
- [ ] Team-done toggle on league dashboard
- [ ] Bidding advisor: quick input for active bidders + price
- [ ] Undo, auto-save, keyboard shortcuts

### Phase 5: Testing + Hardening (Week 8)

- [ ] Simulate full auction with historical data
- [ ] Test trade scenarios from last year
- [ ] Test team-done impact on market prices
- [ ] Edge cases: tight budgets, last picks, all goalies gone
- [ ] Performance: every interaction < 500ms
- [ ] Draft-day dry run

---

## Lessons from Last Year's Retrospective

These are problems from the old Streamlit app. Each is addressed in the new architecture:

| Last Year's Problem                                             | Root Cause                                  | New Architecture Fix                                                     |
| --------------------------------------------------------------- | ------------------------------------------- | ------------------------------------------------------------------------ |
| App got slower as draft progressed                              | Streamlit full re-runs on every interaction | HTMX partial updates, no re-runs                                         |
| Had to tab between pages constantly                             | Multi-page Streamlit layout                 | Single-page four-panel layout                                            |
| Editing a cell required: edit → wait → save → wait → switch tab | Streamlit data_editor widget                | Single POST /assign endpoint                                             |
| Red/green/yellow light was confusing                            | Z-score deviation from mean — not intuitive | Replaced with max bid from MILP. One number.                             |
| Mediocre players got "good value" ratings, rare players didn't  | Z-score treats players independently        | MILP plans whole roster. Scarcity captured by market layer demand count. |
| Optimizer page required manual refresh                          | Streamlit tab isolation                     | Optimizer runs after every action, always visible                        |
| "What if I go slightly over?" was unanswerable                  | No marginal analysis                        | Counterfactual shows exact impact of any price                           |
| Started in deficit, "value overbid" feature was useless         | Assumed budget surplus                      | MILP works from any starting position — deficit or surplus               |
| Couldn't evaluate trades fast enough                            | No trade UI                                 | Dedicated trade evaluator with one-click evaluation                      |
| Done teams inflated market prices                               | No concept of team completion               | is_done toggle excludes them from market calculations                    |
| Competitor ended up with more points                            | Z-score optimized $/point, not total points | MILP maximizes total projected points                                    |

---

## Open Items Before Coding

1. **keepers.json** — all 11 teams' keeper rosters + salaries
2. **biddable_players.csv** — all auction-eligible players
3. **fchl_teams.json** — nomination order, team metadata
4. **model_params.json** — copy from pricer repo
5. **team_odds.json** — Stanley Cup odds (placeholder ok)

---

## Price Model Improvement Opportunities

Track during development, don't implement upfront:

1. **Dynamic budget deflation**: multiply model price by (remaining league budget / starting league budget) as a simple auction-phase correction
2. **Positional scarcity**: boost model price when supply/demand ratio is tight for a position (market layer partially handles via demand count, but model price itself doesn't adjust)
3. **Goalie model**: weakest position (R²=0.61). Consider simpler approach for goalies.
4. **Price momentum**: rolling correction based on recent actual-vs-predicted ratios

These are all potential Phase 5 improvements if testing reveals the base model + market layer isn't accurate enough.

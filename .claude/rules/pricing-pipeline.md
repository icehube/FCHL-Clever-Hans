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
- **Drain nominations** (`_best_drain_candidate`) rank on **market** price -- the money that actually leaves an opponent's budget -- then break ties toward the **least surplus** (`model_price - market_price`). The tie-break is the load-bearing half: once the ceiling binds, every player above it clears at exactly the ceiling, so the choice is purely "which of these equally-draining players do I hand to a rival cheapest". Position need is context in the reasoning string only, never in the ranking -- bidding is position-agnostic, and a team already holding 12F reads as "doesn't need forwards" in `roster_needs` while remaining free to bid. Gate: `MIN_DRAIN_PRICE`, checked on the market price.

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

**How often that `min` actually fires depends entirely on how fast the league spends**, and the two ends of the range are far apart -- measured 2026-08-16 over a full 165-pick auction by `tests/measure_ceiling.py`:

| buyers pay | ceiling below `MAX` | **actually changed a price** | league cap unspent |
|---|---|---|---|
| the tool's own market price | 0 of 165 picks | **0 of 165**, never | 18% ($59.4M of $337.7M) |
| what the reserve rule allows | 133 of 165 picks | **122 of 165**, from pick 43 | 0% |

**Those are two different measurements and the second is the one that means "Layer 2 did something".** `ceiling < MAX_SALARY` says the ceiling moved; `market_price < model_price` says it moved *past a player's model price* and changed what the MILP planned on. `tests/measure_ceiling.py` reports the first (live, per pick), `tests/measure_spend.py` reports the second (from the logged `model_price`/`market_price` on every `draft` record). Cross-checked on the same run 2026-08-17, which is why both columns are here.

The gap between them is the interesting part: in the drain run **the ceiling changed nothing until it hit the $0.5M floor at pick 43.** The intermediate steps -- $7.3M at pick 32, $4.5M at pick 40 -- were below `MAX_SALARY` and above every remaining model price, because a top-down draft has already sold the players those ceilings would have capped. So 133 overstates when the layer started mattering by 11 picks, and every one of the 122 is the floor case. A first pass predicted the price-changing count would be *much* smaller than 133 on the grounds that most of the pool is floor-priced; that reasoning was wrong about the magnitude -- once the ceiling itself reaches the floor it caps essentially everything, so the counts converge. (It also quoted "563 of 705" for the floor count, which reproduces under no definition. Re-measured 2026-08-17: **534 of 705**, where floor means `round(expected_price, 1) == 0.5` -- always carry the definition, since the count runs 0 to 604 without it.)

In the drain run the ceiling steps `11.4M@0 -> 7.3M@32 -> 4.5M@40 -> 0.5M@43` and never moves again. So the layer is **not** inert -- it binds readily, and reaches the floor in a quarter of a draft, once the money is gone. The pinned run is the artefact: paying exactly the model price is the one behaviour the model cannot be wrong about, so it leaves 18% of the cap unspent and **three** teams (JHN $19.8M, GVR $14.1M, VPP $12.0M) finish above the line -- one more than the two the second-highest rule needs. A real draft is somewhere between, and which end it lands nearer decides how much Layer 2 contributes to *planning* -- see `BACKLOG.md`. **Do not restate either run as "the" behaviour of the ceiling.**

Quote the step sequence from the instrument's `ceiling steps` line, never off its checkpoint rows -- those are `--every` picks apart and land wherever the interval happens to land. Reading the floor off a `--every 20` run is how "by pick 60" got into this file when the measured answer was pick 43.

The threshold underneath both numbers: the ceiling is the second-highest of ten, so it holds at `MAX_SALARY` until **all but one** opponent is priced out -- not until the league is broke. Pinned by `tests/test_market.py::TestWhenTheCeilingLeavesTheCap`.

None of this is true of the **live** ceiling, which is a different computation over a different set -- see the Critical rule below.

**Final bid recommendation** -- two caps that mean different things, kept apart:
```
value_cap     = min(marginal_value, physical_max_bid)   # hard: past this he isn't worth it
expected_stop = market_ceiling + 0.1                    # FORECAST: where bidding runs out

max_bid = value_cap                       if uncontested or at cap or current_price >= expected_stop
        = min(value_cap, expected_stop)   otherwise
```
`expected_stop` exists so BOT never pays more than winning requires. It's a prediction, valid only while the standing price is below it -- see the Critical rule.

**Both are surfaced in the UI** (`BidRecommendation.value_cap` / `.expected_stop` / `.stop_status`, rendered as "Worth up to" and "Should win it"). They used to be blended into one "Max bid" figure, which doubled when the price rose one increment past the forecast -- the number lurching mid-auction with nothing on screen to explain it. `stop_status` says *why there is no figure*, because a bare dash for all of them is uninformative: `live` (a figure is shown), `uncontested` (no rivals left), `passed` (a real price falsified it), `unaffordable` (no spot or budget for any bid, so a target price would invite one the engine just refused), `at_cap` (the ceiling is `MAX_SALARY`, so no forecast exists -- see below). The template names each one explicitly and falls back to a bare dash for anything else -- an unrecognized status must explain nothing rather than borrow another's wording. `max_bid` still exists and still means "the min of the two while the forecast holds" -- it is what the ladder does NOT run on.

**`expected_stop` is never returned above `MAX_SALARY`.** Every ceiling reaching `compute_bid_recommendation` is clamped there by `physical_max_bid`, so `ceiling + 0.1` overshoots the legal maximum exactly when the ceiling *is* the maximum -- and that is the common case early, not an edge: measured 2026-08-08, all 11 teams sit at `physical_max_bid = MAX_SALARY` on a fresh state, so the panel advertised **$11.5M**, a bid the league forbids, on every player in the pool at once. It is also why the figure looked identical pool-wide: while nobody is budget-constrained the forecast says nothing about any particular player. That case reports `at_cap` with `expected_stop=None` instead. The arm is ordered ahead of the `passed` check, since `current_price >= expected_stop` is unreachable through legal bidding when `expected_stop > MAX_SALARY`. `max_bid` is unchanged by construction (`value_cap <= MAX_SALARY < expected_stop`, so the old `min()` already returned `value_cap`) -- this was a display bug only, and `tests/test_bid_calculator.py::TestTheForecastAtTheCap` pins both halves.

`expected_stop` is `round(ceiling + SALARY_INCREMENT, 1)`, quantized like every other money value here: raw float addition puts 8 of the 110 legal ceilings just above their own 1-decimal rendering ($1.1M -> 1.2000000000000002), so bidding exactly the figure the panel advertised did not retire the forecast. The verdict text quotes `value_cap` and calls it "worth", matching the panel's label -- an explanation has to name the number that fired it.

**The BID/CAUTION/DROP ladder runs on `value_cap`, never on `max_bid`:**
```
DROP     if current_price >= value_cap
CAUTION  if current_price >= value_cap - CAUTION_BAND
BID      otherwise
```
`value_cap` does not move with price, so the verdict can only soften as the price climbs. Judging the ladder on `max_bid` made advice non-monotonic: the forecast releasing one increment above itself flipped DROP into BID and tripled `max_bid` (fixed a3de737, pinned by `tests/test_bid_calculator.py::TestUncontestedBidding::test_advice_never_inverts_as_price_rises`).

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

**The two are computed over different SETS, and that is why they behave nothing alike.** The idle one takes all 10 opponents, so any two rich teams pin it at `MAX_SALARY`; the live one takes only the named bidders, which is usually two or three, so a single poor rival puts it well below. Measured mid-draft on the same states where the idle ceiling never left the cap:

| live ceiling below `MAX_SALARY` | fresh | mid-draft |
|---|---|---|
| 1 rival | 0/10 | **7/10** |
| 2 rivals | 0/45 | **21/45** |

A first pass at the planning-price question above measured the *idle* ceiling and concluded the panel's "Should win it" figure would never show a number and `stop_status` would read `at_cap` all draft. **That is wrong** -- `/bid-check` builds its own `MarketInfo` from `compute_live_ceiling` over the named bidders, so the forecast fires routinely from mid-draft. Recorded because reasoning from one ceiling to the other is the specific mistake, and it was made here.

### The one exception: a real price beats a forecast

The ceiling is a *forecast of the clearing price*. It caps the bid only **while the standing price is strictly below it**. Once `current_price` reaches `ceiling + 0.1`, the cap drops out and value binds instead (`optimizer.py`, `compute_bid_recommendation`) -- either because a real price on the table has falsified the forecast, or because `ceiling + 0.1` is *by construction* the price that outbids the strongest opponent, so reaching it means BOT has already won. Same reason the cap never applies when uncontested: with no opponent there is nothing to forecast.

**"By construction" has one boundary: `ceiling == MAX_SALARY`.** There, `ceiling + 0.1` is not a price at all, so it outbids nobody -- a rival at the cap can match $11.4M and the winner is decided by who bids it, not by budget. The forecast does not *retire* in that state, it never starts, which is why `at_cap` is its own status rather than a reuse of `passed` (a real price falsified it -- untrue) or `uncontested` (no rivals left -- also untrue, and the opposite of the situation). The cap still binds nothing: `value_cap` is already `<= MAX_SALARY`, so value was the only constraint all along.

Two ways this happens: the last opponent drops out (live ceiling collapses to `MIN_SALARY`), or an opponent bids above the max we computed for them (stale budget data). Without the exception, both produce a spurious DROP on a bargain -- the advisor telling you to walk away from a player you have already won. Regression tests: `tests/test_bid_calculator.py::TestUncontestedBidding`.

**Uncontested semantics** (`bot_uncontested=True`, i.e. `market.bid_winner()` returns `MY_TEAM`): the auction is over and BOT has won at `current_price`. `bid_winner` is the ONE definition of "last bidder standing" -- the advisor's WIN verdict and the template's Assign button both derive from it, because when they were computed separately they disagreed and the panel rendered WIN with no Assign button (fixed ce20814). Verdict is **WIN** when `current_price <= max_bid`, else **DROP** naming the overpay. CAUTION is meaningless here -- nobody can push the price higher. Note the `<=`: the contested ladder uses `>=` because it asks "will I have to go higher?", while uncontested asks "is this final price at or below value?", where break-even is indifferent.

## "Team done" exclusion

When `is_done = True`:
- Team is excluded from market ceiling calculations (their budget doesn't count)
- Team's roster needs are excluded from demand counts
- Team is removed from nomination order
- Zero demand (all opponents done) = floor price

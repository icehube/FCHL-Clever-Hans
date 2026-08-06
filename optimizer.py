"""MILP optimizer, bid calculator, nomination engine, counterfactuals (Layer 3)."""

from __future__ import annotations

from dataclasses import dataclass

import pulp

from config import (
    BACKUP_BONUS,
    BACKUP_TARGETS,
    BENCH_WEIGHT,
    CAUTION_BAND,
    MAX_SALARY,
    MIN_DRAIN_PRICE,
    MIN_SALARY,
    MY_TEAM,
    POSITION_MINIMUMS,
    SALARY_INCREMENT,
    STARTING_LINEUP,
)
from market import MarketInfo
from state import AuctionState, Player, TeamState, lineup_points


@dataclass
class MILPSolution:
    """Result of a MILP roster optimization.

    total_points is STARTING LINEUP points (12F/6D/2G) — bench players
    contribute nothing, per league scoring.
    """

    total_points: float
    roster: list[Player]
    total_cost: float
    by_position: dict[str, list[Player]]
    status: str  # "Optimal", "Infeasible", etc.


@dataclass
class BidRecommendation:
    """Recommendation for whether to bid on a player."""

    player_name: str
    max_bid: float
    marginal_value: float
    market_ceiling: float
    reasoning: str
    action: str  # "BID", "CAUTION", "DROP", "WIN"
    uncontested: bool = False  # No opponent left — market_ceiling is meaningless

    # The two halves max_bid is made of, surfaced so the panel can show them
    # apart. Blended into one figure they read as a single number that doubles
    # when the price moves one increment; named, that jump is just the forecast
    # being retired. max_bid stays the min of the two while the forecast holds.
    value_cap: float = 0.0          # HARD: past this he isn't worth it
    expected_stop: float | None = None  # FORECAST: None once it stops binding
    # Why the forecast retired — a bare dash reads the same for all three, and
    # they call for different reactions: wait vs bid on value vs don't bid.
    stop_status: str = "live"  # "live" | "passed" | "uncontested" | "unaffordable"


@dataclass
class NominationPick:
    """A recommended player to nominate."""

    player: Player
    strategy: str  # "target", "drain", "depth"
    reasoning: str
    expected_price: float


@dataclass
class CounterfactualResult:
    """Side-by-side comparison: roster with vs without a player."""

    with_player: MILPSolution
    without_player: MILPSolution
    points_difference: float
    budget_difference: float
    alternative_players: list[Player]


def solve_optimal_roster(
    team: TeamState,
    available_players: dict[str, Player],
    market_prices: dict[str, float],
    excluded_players: set[str] | None = None,
    forced_players: dict[str, float] | None = None,
) -> MILPSolution:
    """
    MILP: maximize projected points subject to budget and position constraints.

    Args:
        team: current team state (keepers determine remaining needs/budget)
        available_players: biddable players to choose from
        market_prices: player_name -> market-adjusted price
        excluded_players: names to exclude from candidate pool
        forced_players: name -> salary to force-include (for bid calculation)
    """
    if excluded_players is None:
        excluded_players = set()
    if forced_players is None:
        forced_players = {}

    # Filter candidates: must have points > 0 and not excluded
    candidates = {
        name: p for name, p in available_players.items()
        if p.projected_points > 0
        and name not in excluded_players
        and name not in forced_players
    }

    # Budget available after forced players
    # Use remaining_budget (not spendable) because the MILP fills ALL spots,
    # so min-salary reservation is already handled by the == spots constraint.
    forced_cost = sum(forced_players.values())
    budget = team.remaining_budget - forced_cost

    # Spots remaining after forced players
    spots = team.total_spots_remaining - len(forced_players)

    # Position needs after keepers + forced
    needs = dict(team.roster_needs)
    for name, salary in forced_players.items():
        if name in available_players:
            pos = available_players[name].position
            if needs.get(pos, 0) > 0:
                needs[pos] -= 1

    forced_objs = [
        available_players[n] for n in forced_players if n in available_players
    ]

    if spots == 0 and budget >= 0:
        # Forced players exactly fill the roster — a complete roster is a
        # valid outcome, not an infeasibility. (Returning Infeasible here
        # made compute_marginal_value price ANY player at the floor when
        # one spot remained.)
        return MILPSolution(
            total_points=lineup_points(list(team.roster_players) + forced_objs),
            roster=[],
            total_cost=forced_cost,
            by_position={"F": [], "D": [], "G": []},
            status="Optimal",
        )

    if spots < 0 or budget < 0 or budget < spots * MIN_SALARY:
        return MILPSolution(
            total_points=lineup_points(team.roster_players),
            roster=[],
            total_cost=0.0,
            by_position={"F": [], "D": [], "G": []},
            status="Infeasible",
        )

    # Cap position needs so their sum doesn't exceed spots
    # (e.g., team with all-F keepers may need 6D+2G=8 but only have 6 spots)
    total_needs = sum(needs.values())
    if total_needs > spots:
        excess = total_needs - spots
        # Reduce largest needs first (they have the most flexibility)
        for pos in sorted(needs, key=lambda p: -needs[p]):
            if excess <= 0:
                break
            reduction = min(needs[pos], excess)
            needs[pos] -= reduction
            excess -= reduction

    # Build MILP. Roster selection (x) and starter assignment (s) are chosen
    # together: only the best 12F/6D/2G score points, so the objective is
    # starter points, with small terms for bench quality and a soft 2F/1D/1G
    # backup-composition preference (the classic 14/7/3 shape) — see config.
    prob = pulp.LpProblem("roster_optimizer", pulp.LpMaximize)

    # Roster decision variables for candidates (use index for LP-safe names)
    x = {}
    for i, name in enumerate(candidates):
        x[name] = pulp.LpVariable(f"x_{i}", cat="Binary")

    # Starter variables: existing roster members and forced players are
    # locked onto the roster (s free-standing); candidates only if selected.
    fixed_members = list(team.roster_players) + forced_objs
    s_fixed = {
        j: pulp.LpVariable(f"sf_{j}", cat="Binary")
        for j in range(len(fixed_members))
    }
    s_cand = {}
    for i, name in enumerate(candidates):
        s_cand[name] = pulp.LpVariable(f"sc_{i}", cat="Binary")
        prob += s_cand[name] <= x[name]

    # Starter slots per position (12F/6D/2G)
    for pos, slots in STARTING_LINEUP.items():
        prob += (
            pulp.lpSum(
                s_fixed[j] for j, p in enumerate(fixed_members) if p.position == pos
            )
            + pulp.lpSum(
                s_cand[n] for n in candidates if candidates[n].position == pos
            )
        ) <= slots

    # Soft backup-composition credit: bk_pos counts bench players at pos,
    # capped at the 2F/1D/1G target. rostered - starters >= bench by
    # construction, so the constraint is always satisfiable.
    bk = {}
    for pos, target in BACKUP_TARGETS.items():
        bk[pos] = pulp.LpVariable(f"bk_{pos}", lowBound=0, upBound=target)
        rostered_pos = (
            sum(1 for p in fixed_members if p.position == pos)
            + pulp.lpSum(x[n] for n in candidates if candidates[n].position == pos)
        )
        starters_pos = (
            pulp.lpSum(
                s_fixed[j] for j, p in enumerate(fixed_members) if p.position == pos
            )
            + pulp.lpSum(
                s_cand[n] for n in candidates if candidates[n].position == pos
            )
        )
        prob += bk[pos] <= rostered_pos - starters_pos

    # Objective: starter points + 10%-weighted bench points + backup credits
    starter_pts = (
        pulp.lpSum(
            p.projected_points * s_fixed[j] for j, p in enumerate(fixed_members)
        )
        + pulp.lpSum(
            candidates[n].projected_points * s_cand[n] for n in candidates
        )
    )
    bench_pts = pulp.lpSum(
        candidates[n].projected_points * (x[n] - s_cand[n]) for n in candidates
    )
    prob += starter_pts + BENCH_WEIGHT * bench_pts + BACKUP_BONUS * pulp.lpSum(bk.values())

    # Budget constraint
    prob += pulp.lpSum(
        market_prices.get(name, MIN_SALARY) * x[name] for name in candidates
    ) <= budget

    # Total players constraint (must fill all remaining spots)
    prob += pulp.lpSum(x[name] for name in candidates) == spots

    # Position minimum constraints (must be able to field the lineup)
    for pos, need in needs.items():
        if need > 0:
            pos_players = [n for n in candidates if candidates[n].position == pos]
            prob += pulp.lpSum(x[n] for n in pos_players) >= need

    # Solve
    prob.solve(pulp.PULP_CBC_CMD(msg=0))

    status = pulp.LpStatus[prob.status]
    if status != "Optimal":
        return MILPSolution(
            total_points=lineup_points(team.roster_players),
            roster=[],
            total_cost=0.0,
            by_position={"F": [], "D": [], "G": []},
            status=status,
        )

    # Extract solution
    selected = [candidates[n] for n in candidates if x[n].varValue and x[n].varValue > 0.5]
    total_cost = sum(market_prices.get(p.name, MIN_SALARY) for p in selected) + forced_cost
    # Report pure lineup points (recomputed greedily — exact for a fixed
    # roster), not the objective value, which includes the soft-bonus terms
    total_points = lineup_points(fixed_members + selected)

    by_position: dict[str, list[Player]] = {"F": [], "D": [], "G": []}
    for p in selected:
        by_position[p.position].append(p)
    # Add forced players to position breakdown
    for name in forced_players:
        if name in available_players:
            p = available_players[name]
            by_position[p.position].append(p)

    return MILPSolution(
        total_points=total_points,
        roster=selected,
        total_cost=total_cost,
        by_position=by_position,
        status="Optimal",
    )


def compute_marginal_value(
    player: Player,
    team: TeamState,
    available_players: dict[str, Player],
    market_prices: dict[str, float],
) -> float:
    """
    Binary search for the salary at which adding the player no longer
    improves the optimal roster. That salary is the marginal value.
    """
    if team.total_spots_remaining <= 0:
        # A full roster can't seat him: the extra goes to minors and scores
        # nothing, so no price makes him worth having. Kept separate from
        # CAPACITY, which is real — physical_max_bid reports the team's whole
        # remaining budget so opponents' ceilings still see them (2026-08-05).
        # Conflating the two is what this guard prevents: without it the
        # binary search returns MIN_SALARY, which reads as "worth the floor"
        # and the ladder recommends BID on a player who cannot play.
        return 0.0

    # Check if player improves the roster at MIN_SALARY
    with_at_min = solve_optimal_roster(
        team, available_players, market_prices,
        excluded_players=set(),
        forced_players={player.name: MIN_SALARY},
    )
    if with_at_min.status != "Optimal":
        # Can't roster him even at the floor — worth nothing extra
        return MIN_SALARY

    # Solve without the player
    without = solve_optimal_roster(
        team, available_players, market_prices,
        excluded_players={player.name},
    )
    if without.status != "Optimal":
        # No legal roster exists WITHOUT this player (e.g. the last goalie
        # in the pool) — he's a must-have, worth everything we can pay.
        return round(max(team.physical_max_bid, MIN_SALARY), 1)

    if with_at_min.total_points <= without.total_points:
        return MIN_SALARY

    # Binary search for the break-even salary
    # Search in discrete increments of SALARY_INCREMENT
    lo = MIN_SALARY
    hi = min(team.spendable_budget + MIN_SALARY, MAX_SALARY)

    # Check the top first: a player still worth it at our absolute max is
    # valued at hi exactly (the loop below can only ever reach hi - 0.1).
    with_at_hi = solve_optimal_roster(
        team, available_players, market_prices,
        excluded_players=set(),
        forced_players={player.name: round(hi, 1)},
    )
    if with_at_hi.status == "Optimal" and with_at_hi.total_points > without.total_points:
        return round(hi, 1)

    while hi - lo > SALARY_INCREMENT:
        mid = round(lo + (hi - lo) / 2, 1)
        # Ensure mid actually advances past lo
        if mid <= lo:
            mid = round(lo + SALARY_INCREMENT, 1)
        if mid >= hi:
            break
        with_at_mid = solve_optimal_roster(
            team, available_players, market_prices,
            excluded_players=set(),
            forced_players={player.name: mid},
        )
        if with_at_mid.status == "Optimal" and with_at_mid.total_points > without.total_points:
            lo = mid
        else:
            hi = mid

    return round(lo, 1)


def compute_bid_recommendation(
    player: Player,
    team: TeamState,
    available_players: dict[str, Player],
    market_prices: dict[str, float],
    market_info: MarketInfo,
    current_price: float = 0.0,
    bot_uncontested: bool = False,
) -> BidRecommendation:
    """
    Compute max bid and recommend BID / CAUTION / DROP / WIN.

    Two caps, deliberately kept apart because they mean different things:

    * **value cap** = min(marginal_value, physical_max_bid). A hard limit —
      past it the player costs more than he is worth to this roster.
    * **expected stop** = market_ceiling + INCREMENT. A *forecast* of where
      bidding runs out, so we never pay more than winning requires.

    `max_bid` blends the two, but the BID/CAUTION/DROP ladder runs on the value
    cap ALONE. Running it on the blend made advice non-monotonic in price: the
    forecast releasing one increment above itself flipped DROP into BID.

    Pass bot_uncontested=True when BOT is the only bidder left — the auction is
    over, and the verdict is WIN or DROP against the value cap.
    """
    marginal = compute_marginal_value(player, team, available_players, market_prices)
    ceiling = market_info.market_ceiling
    value_cap = round(min(marginal, team.physical_max_bid), 1)

    # The forecast is worthless in two cases: no opponent is left (there is
    # nothing to forecast), or the price has already reached it — a real price
    # on the table cannot come back down, so only value still binds. Capping at
    # a falsified forecast is what made the advisor say DROP at $2.5M on a
    # $4.2M player whose ceiling had collapsed to the $0.5M floor.
    # Quantized to the $0.1M step like every other money value in this app
    # (_legal_salary, _floor_to_increment). Raw float addition puts 8 of the 110
    # legal ceilings just ABOVE their own 1-decimal rendering — $1.1M yields
    # 1.2000000000000002 — so an operator bidding exactly the $1.2M the panel
    # advertises did not clear it, and the panel went on recommending a stop the
    # price had already reached.
    expected_stop = round(ceiling + SALARY_INCREMENT, 1)
    if bot_uncontested:
        # Distinguish the two reasons the forecast is gone, so the panel can say
        # which — "no rivals left" and "bidding went past it" call for different
        # reactions, and a bare dash for both tells the operator nothing.
        max_bid, stop_status, shown_stop = value_cap, "uncontested", None
    elif current_price >= expected_stop:
        max_bid, stop_status, shown_stop = value_cap, "passed", None
    else:
        max_bid = min(value_cap, expected_stop)
        stop_status, shown_stop = "live", expected_stop

    if max_bid < MIN_SALARY:
        # We can't legally place even a floor bid (roster full or budget
        # exhausted) — never clamp UP to a fake $0.5 recommendation.
        return BidRecommendation(
            player_name=player.name,
            max_bid=0.0,
            marginal_value=marginal,
            market_ceiling=ceiling,
            reasoning="No roster spot or budget for any bid",
            action="DROP",
            uncontested=bot_uncontested,
            value_cap=value_cap,
            # No forecast here: pairing "you can't bid at all" with a target
            # price invites a bid the engine has just refused.
            expected_stop=None,
            stop_status="unaffordable",
        )
    max_bid = round(max_bid, 1)

    if bot_uncontested:
        # Nobody left to outbid us: the price is final, so the only question is
        # whether it's at or below what the player is worth. Note the <= against
        # the contested path's >= — contested asks "will I have to go HIGHER?",
        # so matching max_bid means stop. Here there is no next increment, and
        # break-even is indifferent, so take the player.
        if current_price <= max_bid:
            action = "WIN"
            reasoning = f"You've won at ${current_price}M — worth up to ${max_bid}M. Take it."
        else:
            action = "DROP"
            overpay = round(current_price - max_bid, 1)
            reasoning = (
                f"No one left to outbid you, but ${current_price}M is above "
                f"${max_bid}M — overpaying by ${overpay}M"
            )
    # Ladder on value_cap, never on max_bid: value_cap doesn't move with price,
    # so the verdict can only soften as the price climbs — never harden then
    # soften again.
    #
    # All three quote value_cap and call it what the panel calls it ("worth").
    # They used to say "max bid", a label the panel dropped when the figure was
    # split in two, and CAUTION quoted max_bid — the forecast — while the verdict
    # had been fired by value_cap, so the sentence explained itself with a number
    # that wasn't the trigger. An explanation has to name the thing it explains.
    elif current_price >= value_cap:
        action = "DROP"
        reasoning = f"Price ${current_price}M exceeds what he's worth to you (${value_cap}M)"
    elif current_price >= value_cap - CAUTION_BAND:
        action = "CAUTION"
        reasoning = (
            f"Price ${current_price}M is closing on what he's worth "
            f"(${value_cap}M) — proceed carefully"
        )
    else:
        action = "BID"
        reasoning = f"Worth up to ${value_cap}M (marginal value ${marginal}M, ceiling ${ceiling}M)"

    return BidRecommendation(
        player_name=player.name,
        max_bid=max_bid,
        marginal_value=marginal,
        market_ceiling=ceiling,
        reasoning=reasoning,
        action=action,
        uncontested=bot_uncontested,
        value_cap=value_cap,
        expected_stop=shown_stop,
        stop_status=stop_status,
    )


def generate_counterfactual(
    player: Player,
    salary: float,
    team: TeamState,
    available_players: dict[str, Player],
    market_prices: dict[str, float],
) -> CounterfactualResult:
    """
    Show side-by-side: roster WITH player at salary vs optimal WITHOUT.
    Identifies which alternative players the optimizer would choose instead.
    """
    with_player = solve_optimal_roster(
        team, available_players, market_prices,
        forced_players={player.name: salary},
    )
    without_player = solve_optimal_roster(
        team, available_players, market_prices,
        excluded_players={player.name},
    )

    # Find players in without that aren't in with (the alternatives)
    with_names = {p.name for p in with_player.roster}
    alternatives = [p for p in without_player.roster if p.name not in with_names]

    return CounterfactualResult(
        with_player=with_player,
        without_player=without_player,
        points_difference=with_player.total_points - without_player.total_points,
        budget_difference=(with_player.total_cost - without_player.total_cost),
        alternative_players=alternatives,
    )


def recommend_nomination(
    state: AuctionState,
    market_prices: dict[str, float],
    model_prices: dict[str, float],
) -> tuple[NominationPick | None, NominationPick | None]:
    """
    Recommend RFA + UFA nominations for BOT's turn.

    Returns (rfa_pick or None, ufa_pick or None).

    Strategies:
    - target: players in BOT's optimal roster — nominate to acquire
    - drain: expensive players BOT doesn't want — force opponents to spend
    - depth: cheap players to fill remaining spots at floor
    """
    team = state.teams[MY_TEAM]
    available = state.available_players

    # Solve current optimal to know who BOT wants
    optimal = solve_optimal_roster(team, available, market_prices)
    wanted_names = {p.name for p in optimal.roster} if optimal.status == "Optimal" else set()

    # Split into RFA and UFA pools
    rfas = {n: p for n, p in available.items() if p.is_rfa and p.projected_points > 0}
    ufas = {n: p for n, p in available.items() if not p.is_rfa and p.projected_points > 0}

    rfa_pick = _pick_best_rfa(rfas, wanted_names, market_prices, model_prices)
    ufa_pick = _pick_best_ufa(ufas, wanted_names, market_prices, model_prices, state)

    return rfa_pick, ufa_pick


def _pick_best_rfa(
    rfas: dict[str, Player],
    wanted: set[str],
    market_prices: dict[str, float],
    model_prices: dict[str, float],
) -> NominationPick | None:
    """Pick the best RFA to nominate."""
    if not rfas:
        return None

    # Prefer RFAs that BOT wants (target strategy)
    wanted_rfas = [(n, p) for n, p in rfas.items() if n in wanted]
    if wanted_rfas:
        # Pick the one with best value (highest points per dollar)
        best_name, best = max(
            wanted_rfas,
            key=lambda x: x[1].projected_points / max(market_prices.get(x[0], MIN_SALARY), MIN_SALARY),
        )
        return NominationPick(
            player=best,
            strategy="target",
            reasoning=f"BOT wants {best.name} — nominate to acquire via secret bid",
            expected_price=market_prices.get(best_name, MIN_SALARY),
        )

    # Otherwise drain. No MIN_DRAIN_PRICE gate and no depth fallback here: a
    # combo turn is 1 RFA + 1 UFA, so the RFA half must nominate someone
    # regardless of whether the market can pay. Ranked on market price for the
    # same reason as the UFA branch — see _best_drain_candidate.
    best_name, best = _best_drain_candidate(rfas, market_prices, model_prices)
    price = market_prices.get(best_name, MIN_SALARY)
    return NominationPick(
        player=best,
        strategy="drain",
        reasoning=f"{best.name} (~${price:.1f}M) is the priciest RFA the market can "
                  f"reach — forces opponents to spend their sealed bid",
        expected_price=price,
    )


def _bidding_opponents(state: AuctionState) -> list[TeamState]:
    """Opponents who can still bid on something.

    Gates on physical_max_bid, NOT on spots remaining. A 24-man team with cap
    space is still a bidder — the extra goes to minors at full cap — so
    filtering it out understated demand and drain scores exactly the way it
    understated market ceilings before this predicate was shared (2026-08-05).
    market.live_opponents applies the same rule to an in-flight bidder list.
    """
    return [
        t for code, t in state.teams.items()
        if code != MY_TEAM and not t.is_done and t.physical_max_bid >= MIN_SALARY
    ]


def _best_drain_candidate(
    pool: dict[str, Player],
    market_prices: dict[str, float],
    model_prices: dict[str, float],
) -> tuple[str, Player]:
    """The player whose sale burns the most opponent cap, per dollar gifted.

    `pool` must be non-empty — both callers already check, and returning an
    Optional here got handled two different ways: _pick_best_ufa guarded it,
    _pick_best_rfa unpacked it bare and was safe only because an early return
    sat 25 lines above. A total contract removes the second way to be wrong.

    Ranked on MARKET price, not model price, because that is the money that
    actually leaves an opponent's budget. compute_market_price IS the auction
    clearing price: min(model, second-highest opponent max) is identical to the
    second-highest willingness to pay among bidders, which is what the
    second-to-last bidder dropping out sets. So there is no separate drain
    formula to derive — Layer 2 already computed the answer.

    Ties are the normal case here, not an edge case: once the ceiling binds,
    EVERY player priced above it clears at exactly the ceiling. Ranking that
    tied set by model price (what the old score did) picks the player who hands
    a rival the most value for the same money — a $7.7M goalie and a $2.5M
    defenceman both cost the buyer $2.5M, but only one of them is a bargain.
    Break toward the least surplus instead: same cap drained, no gift.

    Position need is deliberately absent. It multiplied a dollar figure by a
    team count, which is what made the old score uninterpretable, and it
    contradicts the position-agnostic bidding rule — a team already holding 12F
    reads as "doesn't need forwards" in roster_needs while remaining perfectly
    free to bid on one. It survives as context in _drain_reasoning.
    """
    def rank(item: tuple[str, Player]) -> tuple[float, float, str]:
        name, _ = item
        market = market_prices.get(name, MIN_SALARY)
        surplus = model_prices.get(name, 0.0) - market
        # Name last so the pick can't drift with dict insertion order.
        return (market, -surplus, name)

    return max(pool.items(), key=rank)


def _drain_reasoning(player: Player, market_price: float, state: AuctionState) -> str:
    """Context for a drain pick: who still needs the position, who can pay.

    can_afford is counted at the MARKET price, so it is never zero when anyone
    can bid — the market price is by construction the second-highest opponent
    max, so the top two can always reach it. Counted at the model price it read
    "0 can afford" while recommending the player, which is self-contradicting:
    someone nobody can bid on drains nothing.
    """
    opponents = _bidding_opponents(state)
    needing = sum(1 for t in opponents if t.roster_needs.get(player.position, 0) > 0)
    can_afford = sum(1 for t in opponents if t.physical_max_bid >= market_price)
    return (
        f"{player.name} (${market_price:.1f}M, {needing} need {player.position}, "
        f"{can_afford} can afford) — drains opponent budgets"
    )


def _pick_best_ufa(
    ufas: dict[str, Player],
    wanted: set[str],
    market_prices: dict[str, float],
    model_prices: dict[str, float],
    state: AuctionState,
) -> NominationPick | None:
    """Pick the best UFA to nominate."""
    if not ufas:
        return None

    # Strategy 1: Target — nominate a player BOT wants
    wanted_ufas = [(n, p) for n, p in ufas.items() if n in wanted]
    if wanted_ufas:
        best_name, best = max(
            wanted_ufas,
            key=lambda x: x[1].projected_points / max(market_prices.get(x[0], MIN_SALARY), MIN_SALARY),
        )
        return NominationPick(
            player=best,
            strategy="target",
            reasoning=f"BOT wants {best.name} — nominate to buy",
            expected_price=market_prices.get(best_name, MIN_SALARY),
        )

    # Strategy 2: Drain — nominate player that forces opponents into bidding wars
    unwanted = {n: p for n, p in ufas.items() if n not in wanted}
    if unwanted:
        drain_name, drain = _best_drain_candidate(unwanted, market_prices, model_prices)
        price = market_prices.get(drain_name, MIN_SALARY)
        # Gating the top candidate is sound only because the ranking is in
        # dollars: its market price is the most any nomination can drain, so
        # failing the threshold means no drain is worth the turn. The old score
        # wasn't a price, so this check could reject a perfectly good drain.
        if price >= MIN_DRAIN_PRICE:
            return NominationPick(
                player=drain,
                strategy="drain",
                reasoning=_drain_reasoning(drain, price, state),
                expected_price=price,
            )

    # Strategy 3: Depth — nominate a cheap player for BOT's roster
    cheap = [(n, p) for n, p in ufas.items() if market_prices.get(n, MIN_SALARY) <= 1.0]
    if cheap:
        depth_name, depth = max(cheap, key=lambda x: x[1].projected_points)
        return NominationPick(
            player=depth,
            strategy="depth",
            reasoning=f"{depth.name} ({depth.projected_points}pts) — cheap fill at ~${market_prices.get(depth_name, MIN_SALARY):.1f}M",
            expected_price=market_prices.get(depth_name, MIN_SALARY),
        )

    # Fallback: highest points available
    best_name, best = max(ufas.items(), key=lambda x: x[1].projected_points)
    return NominationPick(
        player=best,
        strategy="target",
        reasoning=f"Best available: {best.name} ({best.projected_points}pts)",
        expected_price=market_prices.get(best_name, MIN_SALARY),
    )

"""MILP optimizer, bid calculator, nomination engine, counterfactuals (Layer 3)."""

from __future__ import annotations

from dataclasses import dataclass

import pulp

from config import (
    MAX_SALARY,
    MIN_SALARY,
    MY_TEAM,
    POSITION_MINIMUMS,
    SALARY_INCREMENT,
)
from market import MarketInfo
from state import AuctionState, Player, TeamState


@dataclass
class MILPSolution:
    """Result of a MILP roster optimization."""

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
    action: str  # "BID", "CAUTION", "DROP"


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

    if spots <= 0 or budget < 0 or budget < spots * MIN_SALARY:
        return MILPSolution(
            total_points=sum(p.projected_points for p in team.roster_players),
            roster=[],
            total_cost=0.0,
            by_position={"F": [], "D": [], "G": []},
            status="Infeasible",
        )

    # Cap position needs so their sum doesn't exceed spots
    # (e.g., team with all-F keepers may need 7D+3G=10 but only have 8 spots)
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

    # Build MILP
    prob = pulp.LpProblem("roster_optimizer", pulp.LpMaximize)

    # Decision variables (use index for LP-safe names)
    x = {}
    for i, name in enumerate(candidates):
        x[name] = pulp.LpVariable(f"x_{i}", cat="Binary")

    # Objective: maximize total projected points
    prob += pulp.lpSum(
        candidates[name].projected_points * x[name] for name in candidates
    )

    # Budget constraint
    prob += pulp.lpSum(
        market_prices.get(name, MIN_SALARY) * x[name] for name in candidates
    ) <= budget

    # Total players constraint (must fill all remaining spots)
    prob += pulp.lpSum(x[name] for name in candidates) == spots

    # Position minimum constraints
    for pos, need in needs.items():
        if need > 0:
            pos_players = [n for n in candidates if candidates[n].position == pos]
            prob += pulp.lpSum(x[n] for n in pos_players) >= need

    # Solve
    prob.solve(pulp.PULP_CBC_CMD(msg=0))

    status = pulp.LpStatus[prob.status]
    if status != "Optimal":
        return MILPSolution(
            total_points=sum(p.projected_points for p in team.roster_players),
            roster=[],
            total_cost=0.0,
            by_position={"F": [], "D": [], "G": []},
            status=status,
        )

    # Extract solution
    selected = [candidates[n] for n in candidates if x[n].varValue and x[n].varValue > 0.5]
    total_cost = sum(market_prices.get(p.name, MIN_SALARY) for p in selected) + forced_cost
    total_points = (
        sum(p.projected_points for p in team.roster_players)
        + sum(p.projected_points for p in selected)
        + sum(
            available_players[n].projected_points
            for n in forced_players
            if n in available_players
        )
    )

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
    # Solve without the player
    without = solve_optimal_roster(
        team, available_players, market_prices,
        excluded_players={player.name},
    )

    if without.status != "Optimal":
        return MIN_SALARY

    # Check if player improves the roster at MIN_SALARY
    with_at_min = solve_optimal_roster(
        team, available_players, market_prices,
        excluded_players=set(),
        forced_players={player.name: MIN_SALARY},
    )

    if with_at_min.status != "Optimal" or with_at_min.total_points <= without.total_points:
        return MIN_SALARY

    # Binary search for the break-even salary
    # Search in discrete increments of SALARY_INCREMENT
    lo = MIN_SALARY
    hi = min(team.spendable_budget + MIN_SALARY, MAX_SALARY)

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
) -> BidRecommendation:
    """
    Compute max bid and recommend BID / CAUTION / DROP.

    max_bid = min(marginal_value, market_ceiling + INCREMENT, physical_max_bid)
    """
    marginal = compute_marginal_value(player, team, available_players, market_prices)
    ceiling = market_info.market_ceiling

    max_bid = min(marginal, ceiling + SALARY_INCREMENT, team.physical_max_bid)
    max_bid = round(max(max_bid, MIN_SALARY), 1)

    if current_price >= max_bid:
        action = "DROP"
        reasoning = f"Price ${current_price}M exceeds max bid ${max_bid}M"
    elif current_price >= max_bid - 0.3:
        action = "CAUTION"
        reasoning = f"Price ${current_price}M is near max bid ${max_bid}M — proceed carefully"
    else:
        action = "BID"
        reasoning = f"Worth up to ${max_bid}M (marginal value ${marginal}M, ceiling ${ceiling}M)"

    return BidRecommendation(
        player_name=player.name,
        max_bid=max_bid,
        marginal_value=marginal,
        market_ceiling=ceiling,
        reasoning=reasoning,
        action=action,
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
    market_info: MarketInfo,
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
    ufa_pick = _pick_best_ufa(ufas, wanted_names, team, market_prices, model_prices, market_info)

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

    # Otherwise pick the most expensive RFA to drain opponent budgets
    best_name, best = max(
        rfas.items(),
        key=lambda x: model_prices.get(x[0], 0),
    )
    return NominationPick(
        player=best,
        strategy="drain",
        reasoning=f"{best.name} is expensive — forces opponents to spend on RFA bid",
        expected_price=model_prices.get(best_name, MIN_SALARY),
    )


def _pick_best_ufa(
    ufas: dict[str, Player],
    wanted: set[str],
    team: TeamState,
    market_prices: dict[str, float],
    model_prices: dict[str, float],
    market_info: MarketInfo,
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

    # Strategy 2: Drain — nominate an expensive player BOT doesn't want
    # Pick player with highest model price that BOT doesn't want
    unwanted = [(n, p) for n, p in ufas.items() if n not in wanted]
    if unwanted:
        drain_name, drain = max(
            unwanted,
            key=lambda x: model_prices.get(x[0], 0),
        )
        if model_prices.get(drain_name, 0) > 2.0:
            return NominationPick(
                player=drain,
                strategy="drain",
                reasoning=f"{drain.name} (${model_prices.get(drain_name, 0):.1f}M model) — drains opponent budgets",
                expected_price=model_prices.get(drain_name, MIN_SALARY),
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

"""Trade evaluator and buyout analyzer."""

from __future__ import annotations

import uuid
from copy import deepcopy
from dataclasses import dataclass, field

from config import BUYOUT_PENALTY_RATE, DEFAULT_TEAM_PROBABILITY, MY_TEAM
from optimizer import MILPSolution, solve_optimal_roster
from state import AuctionState, Player, PlayerOnRoster


def _pool_rank(pool: dict[str, Player], position: str, projected_points: float) -> int:
    """Rank a player re-entering the pool against the remaining pool (ties=min)."""
    return 1 + sum(
        1 for q in pool.values()
        if q.position == position and q.projected_points > projected_points
    )


@dataclass
class PlayerTrade:
    """A player involved in a trade."""

    name: str
    position: str
    salary: float
    projected_points: int


@dataclass
class BuyoutEvaluation:
    """Result of evaluating a potential buyout."""

    player_name: str
    salary_freed: float
    penalty_added: float
    net_cap_freed: float
    current_points: float
    buyout_points: float
    delta_points: float
    recommendation: str  # "buyout" or "keep"
    current_roster: MILPSolution
    buyout_roster: MILPSolution


@dataclass
class TradeScenario:
    """One possible outcome of a trade (keep all, buyout one, etc.)."""

    description: str
    total_points: float
    cap_remaining: float
    roster: MILPSolution
    buyouts: list[str] = field(default_factory=list)


@dataclass
class TradeEvaluation:
    """Complete evaluation of a proposed trade."""

    trade_id: str
    give: list[PlayerTrade]
    receive: list[PlayerTrade]
    current_scenario: TradeScenario
    scenarios: list[TradeScenario]
    best_scenario: TradeScenario
    recommendation: str  # "accept" or "decline"
    reasoning: str
    source_team_code: str | None = None  # The team BOT is trading with, if any


def evaluate_trade(
    state: AuctionState,
    give: list[PlayerTrade],
    receive: list[PlayerTrade],
    market_prices: dict[str, float],
    auto_check_buyouts: bool = True,
    source_team_code: str | None = None,
) -> TradeEvaluation:
    """
    Evaluate a proposed trade by comparing MILP solutions.

    1. Solve current state → baseline
    2. Clone state, apply trade, solve → "keep all" scenario
    3. If auto_check_buyouts: test buying out each received player
    4. Pick best scenario, recommend accept/decline
    """
    team = state.teams[MY_TEAM]
    trade_id = str(uuid.uuid4())[:8]

    # Baseline: current optimal
    current_sol = solve_optimal_roster(team, state.available_players, market_prices)
    current_scenario = TradeScenario(
        description="Current roster (no trade)",
        total_points=current_sol.total_points,
        cap_remaining=team.remaining_budget,
        roster=current_sol,
    )

    # Apply trade to cloned state
    trade_state = deepcopy(state)
    trade_team = trade_state.teams[MY_TEAM]

    # Two-team trades: take salary/points from the source team's roster,
    # not the client-supplied form JSON (stale if adjusted after the
    # dropdown loaded). Mutating the PlayerTrade DTOs here also corrects
    # the values execute_trade will apply later.
    if source_team_code and source_team_code in trade_state.teams:
        src_team = trade_state.teams[source_team_code]
        for p in receive:
            actual = src_team.find_player(p.name)
            if actual is not None:
                p.salary = actual.salary
                p.projected_points = actual.projected_points

    # Remove players BOT gives away
    for p in give:
        try:
            trade_team.remove_player(p.name)
        except ValueError:
            pass  # Player might not be on roster (shouldn't happen but be safe)

    # Add players BOT receives
    for p in receive:
        trade_team.add_acquired_player(PlayerOnRoster(
            name=p.name,
            position=p.position,
            group="3",  # Acquired players are group 3
            salary=p.salary,
            projected_points=p.projected_points,
            nhl_team=getattr(p, "nhl_team", ""),
        ))

    # Remove received players from available pool (they're now on BOT)
    trade_available = dict(trade_state.available_players)
    for p in receive:
        trade_available.pop(p.name, None)

    # Free-agent flow only: given players return to the auction pool so the
    # post-trade MILP can model re-acquiring them. In a two-team trade they
    # live on the source team's roster and are not re-acquireable.
    if source_team_code is None:
        for p in give:
            trade_available[p.name] = Player(
                name=p.name,
                position=p.position,
                group="3",
                nhl_team="",
                age=0,
                projected_points=p.projected_points,
                is_rfa=False,
                salary=p.salary,
                # League-average odds, not 0.0 — 0% Cup probability is
                # out-of-distribution for the price model
                team_probability=DEFAULT_TEAM_PROBABILITY,
                pos_rank=_pool_rank(trade_available, p.position, p.projected_points),
            )

    # Scenario: keep all received players
    keep_sol = solve_optimal_roster(trade_team, trade_available, market_prices)
    scenarios = [TradeScenario(
        description="Keep all received players",
        total_points=keep_sol.total_points,
        cap_remaining=trade_team.remaining_budget,
        roster=keep_sol,
    )]

    # Auto-check buyouts on each received player
    if auto_check_buyouts:
        for p in receive:
            buyout_state = deepcopy(trade_state)
            buyout_team = buyout_state.teams[MY_TEAM]
            try:
                buyout_team.remove_player(p.name)
            except ValueError:
                continue
            buyout_team.penalties += p.salary * BUYOUT_PENALTY_RATE
            buyout_sol = solve_optimal_roster(buyout_team, trade_available, market_prices)
            scenarios.append(TradeScenario(
                description=f"Buy out {p.name} (penalty ${p.salary * BUYOUT_PENALTY_RATE:.1f}M)",
                total_points=buyout_sol.total_points,
                cap_remaining=buyout_team.remaining_budget,
                roster=buyout_sol,
                buyouts=[p.name],
            ))

    # A scenario is only acceptable if it leaves a legal team: cap space
    # non-negative and a solvable roster. Comparing raw total_points let
    # cap-violating trades win (Infeasible solves still report baseline
    # lineup points).
    def _is_legal(s: TradeScenario) -> bool:
        return s.cap_remaining >= 0 and s.roster.status == "Optimal"

    legal = [s for s in scenarios if _is_legal(s)]
    if not legal:
        worst_cap = min(s.cap_remaining for s in scenarios)
        return TradeEvaluation(
            trade_id=trade_id,
            give=give,
            receive=receive,
            current_scenario=current_scenario,
            scenarios=scenarios,
            best_scenario=max(scenarios, key=lambda s: s.total_points),
            recommendation="decline",
            reasoning=(
                f"No legal outcome: trade leaves the roster over the cap "
                f"(${worst_cap:.1f}M remaining) or unsolvable"
            ),
            source_team_code=source_team_code,
        )

    # Find best legal scenario
    best = max(legal, key=lambda s: s.total_points)

    # Compare best to current
    if best.total_points > current_scenario.total_points:
        recommendation = "accept"
        delta = best.total_points - current_scenario.total_points
        reasoning = f"Trade gains +{delta:.0f} projected points ({best.description})"
    elif best.total_points == current_scenario.total_points:
        if best.cap_remaining > current_scenario.cap_remaining:
            recommendation = "accept"
            reasoning = f"Same points but frees ${best.cap_remaining - current_scenario.cap_remaining:.1f}M cap space ({best.description})"
        else:
            recommendation = "decline"
            reasoning = "No improvement in points or cap space"
    else:
        recommendation = "decline"
        delta = current_scenario.total_points - best.total_points
        reasoning = f"Trade loses {delta:.0f} projected points"

    return TradeEvaluation(
        trade_id=trade_id,
        give=give,
        receive=receive,
        current_scenario=current_scenario,
        scenarios=scenarios,
        best_scenario=best,
        recommendation=recommendation,
        reasoning=reasoning,
        source_team_code=source_team_code,
    )


def evaluate_buyout(
    state: AuctionState,
    player_name: str,
    market_prices: dict[str, float],
) -> BuyoutEvaluation:
    """
    Evaluate buying out a player on BOT's roster.

    Buyout removes the player but adds a penalty of 50% salary to cap.
    """
    team = state.teams[MY_TEAM]

    # Current optimal
    current_sol = solve_optimal_roster(team, state.available_players, market_prices)

    # Find the player
    player = team.find_player(player_name)
    if player is None:
        raise ValueError(f"Player '{player_name}' not found on {MY_TEAM}")

    # Clone and apply buyout
    buyout_state = deepcopy(state)
    buyout_team = buyout_state.teams[MY_TEAM]
    buyout_team.remove_player(player_name)
    penalty = player.salary * BUYOUT_PENALTY_RATE
    buyout_team.penalties += penalty

    buyout_sol = solve_optimal_roster(buyout_team, state.available_players, market_prices)

    delta = buyout_sol.total_points - current_sol.total_points
    recommendation = "buyout" if delta > 0 else "keep"

    return BuyoutEvaluation(
        player_name=player_name,
        salary_freed=player.salary,
        penalty_added=penalty,
        net_cap_freed=player.salary * (1 - BUYOUT_PENALTY_RATE),
        current_points=current_sol.total_points,
        buyout_points=buyout_sol.total_points,
        delta_points=delta,
        recommendation=recommendation,
        current_roster=current_sol,
        buyout_roster=buyout_sol,
    )


def execute_trade(
    state: AuctionState,
    give: list[PlayerTrade],
    receive: list[PlayerTrade],
    source_team_code: str | None = None,
) -> None:
    """
    Execute a trade on the live state.

    With source_team_code set: a real two-team trade -- give players move to
    that team's roster, receive players come from that team's roster.
    Without it: legacy free-agent flow -- give players return to the available
    pool, receive players are pulled from it.
    """
    bot = state.teams[MY_TEAM]

    if source_team_code:
        if source_team_code == MY_TEAM:
            raise ValueError("Cannot trade with self")
        if source_team_code not in state.teams:
            raise ValueError(f"Unknown team {source_team_code}")
        other = state.teams[source_team_code]

        for p in give:
            removed = bot.remove_player(p.name)
            other.add_acquired_player(PlayerOnRoster(
                name=removed.name,
                position=removed.position,
                group=removed.group,
                salary=removed.salary,
                projected_points=removed.projected_points,
                nhl_team=removed.nhl_team,
            ))

        for p in receive:
            removed = other.remove_player(p.name)
            # Carry the authoritative roster object's identity: group drives
            # minors cap semantics (A-E don't count), salary is unchanged by
            # a trade, and a fresh PlayerOnRoster resets is_bench/is_minor.
            bot.add_acquired_player(PlayerOnRoster(
                name=removed.name,
                position=removed.position,
                group=removed.group,
                salary=removed.salary,
                projected_points=removed.projected_points,
                nhl_team=removed.nhl_team,
            ))
        return

    # Legacy free-agent flow
    for p in give:
        removed = bot.remove_player(p.name)
        state.available_players[p.name] = Player(
            name=removed.name,
            position=removed.position,
            group=removed.group,
            nhl_team=removed.nhl_team,
            age=0,
            projected_points=removed.projected_points,
            is_rfa=False,
            salary=removed.salary,
            # League-average odds, not 0.0 — 0% Cup probability is
            # out-of-distribution for the price model
            team_probability=DEFAULT_TEAM_PROBABILITY,
            # Re-entering the pool: rank against the remaining pool so the
            # price model's scarcity feature doesn't treat them as rank 1
            pos_rank=_pool_rank(state.available_players, removed.position, removed.projected_points),
        )

    for p in receive:
        state.available_players.pop(p.name, None)
        bot.add_acquired_player(PlayerOnRoster(
            name=p.name,
            position=p.position,
            group="3",
            salary=p.salary,
            projected_points=p.projected_points,
            nhl_team=getattr(p, "nhl_team", ""),
        ))


def execute_buyout(
    state: AuctionState,
    player_name: str,
) -> None:
    """Execute a buyout on a player on BOT's roster."""
    team = state.teams[MY_TEAM]
    player = team.remove_player(player_name)
    team.penalties += player.salary * BUYOUT_PENALTY_RATE

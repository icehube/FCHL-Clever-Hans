"""Trade evaluator and buyout analyzer."""

from __future__ import annotations

import uuid
from copy import deepcopy
from dataclasses import dataclass, field

from config import BUYOUT_PENALTY_RATE, MY_TEAM
from optimizer import MILPSolution, solve_optimal_roster
from state import AuctionState, Player, PlayerOnRoster


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


def evaluate_trade(
    state: AuctionState,
    give: list[PlayerTrade],
    receive: list[PlayerTrade],
    market_prices: dict[str, float],
    auto_check_buyouts: bool = True,
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

    # Add given players back to available pool
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
            team_probability=0.0,
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

    # Find best scenario
    best = max(scenarios, key=lambda s: s.total_points)

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
    buyout_players: list[str] | None = None,
) -> None:
    """
    Execute a trade on the live state.

    Removes given players from BOT, adds received players,
    optionally buys out specified received players.
    """
    if buyout_players is None:
        buyout_players = []

    team = state.teams[MY_TEAM]

    # Remove players BOT gives
    for p in give:
        removed = team.remove_player(p.name)
        # Add back to available pool
        state.available_players[p.name] = Player(
            name=p.name,
            position=p.position,
            group=removed.group,
            nhl_team="",
            age=0,
            projected_points=p.projected_points,
            is_rfa=False,
            salary=p.salary,
            team_probability=0.0,
        )

    # Add players BOT receives
    for p in receive:
        # Remove from available pool
        state.available_players.pop(p.name, None)

        if p.name in buyout_players:
            # Buyout: don't add to roster, just add penalty
            team.penalties += p.salary * BUYOUT_PENALTY_RATE
        else:
            team.add_acquired_player(PlayerOnRoster(
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

"""Market-adjusted prices using real-time auction state (Layer 2)."""

from __future__ import annotations

from dataclasses import dataclass

from config import MIN_SALARY, MY_TEAM
from price_model import PricePrediction
from state import Player, TeamState


@dataclass
class MarketInfo:
    """Market ceiling and demand information for a player's position."""

    market_ceiling: float  # Second-highest physical_max among active needing teams
    highest_bidder: str | None  # Team with highest physical_max
    highest_bid: float  # Their physical_max
    second_bidder: str | None  # Team with second-highest
    demand_count: int  # Active teams needing this position that can afford
    floor_demand: bool  # True if demand_count == 0 → player sells at floor


def compute_opponent_ceiling(team: TeamState) -> float | None:
    """
    Physical max a team can bid on any player.

    Returns None if team is_done.
    Teams can draft any position — extras go to bench or minors.
    The only constraint is budget.
    """
    if team.is_done:
        return None

    if team.physical_max_bid < MIN_SALARY:
        return None

    return team.physical_max_bid


def compute_market_ceiling(
    all_teams: dict[str, TeamState],
    exclude_team: str = MY_TEAM,
) -> MarketInfo:
    """
    Market ceiling = second-highest physical_max among active opponents.

    Second-highest because auction price is set when the second-to-last
    bidder drops out. Position-agnostic — any team can bid on any player.
    """
    ceilings: list[tuple[str, float]] = []

    for code, team in all_teams.items():
        if code == exclude_team:
            continue
        ceiling = compute_opponent_ceiling(team)
        if ceiling is not None:
            ceilings.append((code, ceiling))

    if not ceilings:
        return MarketInfo(
            market_ceiling=MIN_SALARY,
            highest_bidder=None,
            highest_bid=0.0,
            second_bidder=None,
            demand_count=0,
            floor_demand=True,
        )

    # Sort descending by ceiling
    ceilings.sort(key=lambda x: -x[1])
    demand_count = len(ceilings)

    highest_code, highest_bid = ceilings[0]

    if len(ceilings) == 1:
        # Only one team can bid — they set the price
        return MarketInfo(
            market_ceiling=highest_bid,
            highest_bidder=highest_code,
            highest_bid=highest_bid,
            second_bidder=None,
            demand_count=demand_count,
            floor_demand=False,
        )

    second_code, second_bid = ceilings[1]
    return MarketInfo(
        market_ceiling=second_bid,
        highest_bidder=highest_code,
        highest_bid=highest_bid,
        second_bidder=second_code,
        demand_count=demand_count,
        floor_demand=False,
    )


def compute_market_price(
    model_price: float,
    market_info: MarketInfo,
) -> float:
    """Market-adjusted price: min of model price and market ceiling."""
    if market_info.floor_demand:
        return MIN_SALARY
    return min(model_price, market_info.market_ceiling)


def compute_all_market_prices(
    players: dict[str, Player],
    model_prices: dict[str, PricePrediction],
    all_teams: dict[str, TeamState],
) -> dict[str, tuple[float, MarketInfo]]:
    """
    Compute market-adjusted prices for all biddable players.

    Returns dict mapping player_name -> (market_price, MarketInfo).
    """
    # Single ceiling for all positions — any team can bid on any player
    market_info = compute_market_ceiling(all_teams)

    results = {}
    for name, player in players.items():
        model_price = model_prices[name].expected_price
        market_price = compute_market_price(model_price, market_info)
        results[name] = (market_price, market_info)

    return results


def compute_live_ceiling(
    active_bidders: list[str],
    teams: dict[str, TeamState],
) -> float:
    """
    During active bidding (Mode 3): ceiling from the specific active bidders.

    More precise than general market ceiling — uses only the teams
    still actively bidding on this player.
    """
    ceilings: list[float] = []

    for code in active_bidders:
        if code not in teams:
            continue
        team = teams[code]
        if team.is_done:
            continue
        ceiling = team.physical_max_bid
        if ceiling >= MIN_SALARY:
            ceilings.append(ceiling)

    if not ceilings:
        return MIN_SALARY

    ceilings.sort(reverse=True)

    if len(ceilings) == 1:
        return ceilings[0]

    # Second-highest sets the ceiling
    return ceilings[1]

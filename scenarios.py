"""Pre-baked auction-state scenarios for live testing.

Each scenario takes a freshly initialized AuctionState and mutates it in
place. Used by POST /load-scenario to drop the user into a specific
draft state without manually drafting players.
"""

from __future__ import annotations

from config import MIN_SALARY, MY_TEAM
from data_loader import build_initial_state
from state import AuctionState, PlayerOnRoster


def _scenario_goalie_asymmetry(state: AuctionState) -> None:
    """Every non-BOT team has 2 goalies; BOT keeps however many it started with.

    Pulls the cheapest available goalies (by projected points ascending) and
    assigns them as acquired players at $0.5M each. Stops when each non-BOT
    team has exactly 2 G on the active roster.
    """
    target = 2

    # Collect available goalies cheapest-first (low projected points = back-end).
    pool = sorted(
        (p for p in state.available_players.values() if p.position == "G"),
        key=lambda p: p.projected_points,
    )
    pool_iter = iter(pool)

    for code, team in state.teams.items():
        if code == MY_TEAM:
            continue
        have = sum(1 for p in team.roster_players if p.position == "G")
        while have < target:
            try:
                src = next(pool_iter)
            except StopIteration:
                return
            team.add_acquired_player(PlayerOnRoster(
                name=src.name,
                position=src.position,
                group=src.group,
                salary=MIN_SALARY,
                projected_points=src.projected_points,
                nhl_team=src.nhl_team,
            ))
            del state.available_players[src.name]
            have += 1


SCENARIOS = {
    "goalie-asymmetry": _scenario_goalie_asymmetry,
}


def load(name: str) -> AuctionState:
    """Build a fresh state and apply the named scenario. Raises KeyError if unknown."""
    if name not in SCENARIOS:
        raise KeyError(f"Unknown scenario: {name}")
    state = build_initial_state()
    SCENARIOS[name](state)
    return state

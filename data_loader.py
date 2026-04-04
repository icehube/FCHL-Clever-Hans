"""Load all data files and build the initial AuctionState."""

import csv
import json

from config import (
    DEFAULT_TEAM_PROBABILITY,
    MINOR_CAP_GROUPS,
    NHL_TEAM_ALIASES,
    RFA_GROUPS,
)
from state import AuctionState, Player, PlayerOnRoster, TeamState

# Team codes that are real FCHL teams (not UFA/RFA placeholders)
_PLACEHOLDER_TEAMS = {"UFA", "RFA"}


def load_team_metadata(path: str = "data/fchl_teams.json") -> dict:
    """Load team configs, nomination order, and penalties."""
    with open(path) as f:
        data = json.load(f)
    return data


def load_team_odds(path: str = "data/team_odds.json") -> dict[str, float]:
    """Load Stanley Cup odds. Applies NHL team aliases and default for missing teams."""
    with open(path) as f:
        data = json.load(f)
    odds = data["odds"]
    # Apply aliases so lookups work with either name
    for alias, canonical in NHL_TEAM_ALIASES.items():
        if canonical in odds and alias not in odds:
            odds[alias] = odds[canonical]
    return odds


def _get_team_probability(nhl_team: str, odds: dict[str, float]) -> float:
    """Look up team probability with alias resolution and default fallback."""
    canonical = NHL_TEAM_ALIASES.get(nhl_team, nhl_team)
    return odds.get(canonical, DEFAULT_TEAM_PROBABILITY)


def load_players(
    path: str = "data/players.csv",
    team_odds: dict[str, float] | None = None,
) -> tuple[dict[str, list], dict[str, Player]]:
    """
    Parse players.csv into team rosters and biddable players.

    Returns:
        team_players: dict mapping team_code -> {"keepers": [...], "minors": [...]}
        biddable: dict mapping player_name -> Player
    """
    if team_odds is None:
        team_odds = {}

    team_players: dict[str, dict[str, list]] = {}
    biddable: dict[str, Player] = {}

    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row["PLAYER"].strip()
            position = row["POS"].strip()
            group = row["GROUP"].strip()
            status = row["STATUS"].strip()
            fchl_team = row["FCHL TEAM"].strip()
            nhl_team = row["NHL TEAM"].strip()
            age = int(row["AGE"]) if row["AGE"].strip() else 0
            salary = float(row["SALARY"]) if row["SALARY"].strip() else 0.0
            pts = int(row["PTS"]) if row["PTS"].strip() else 0
            prior_fchl_team = row.get("PRIOR FCHL TEAM", "").strip()

            team_prob = _get_team_probability(nhl_team, team_odds)

            if fchl_team in _PLACEHOLDER_TEAMS and status == "":
                # Biddable player (UFA or RFA)
                is_rfa = group in RFA_GROUPS
                biddable[name] = Player(
                    name=name,
                    position=position,
                    group=group,
                    nhl_team=nhl_team,
                    age=age,
                    projected_points=pts,
                    is_rfa=is_rfa,
                    salary=salary,
                    team_probability=team_prob,
                    prior_fchl_team=prior_fchl_team,
                )
            elif fchl_team not in _PLACEHOLDER_TEAMS and fchl_team != "":
                # Player on a real team (keeper or minor)
                is_minor = status == "MINOR"
                roster_player = PlayerOnRoster(
                    name=name,
                    position=position,
                    group=group,
                    salary=salary,
                    projected_points=pts,
                    nhl_team=nhl_team,
                    is_minor=is_minor,
                )
                if fchl_team not in team_players:
                    team_players[fchl_team] = {"keepers": [], "minors": []}
                if is_minor:
                    team_players[fchl_team]["minors"].append(roster_player)
                else:
                    team_players[fchl_team]["keepers"].append(roster_player)

    return team_players, biddable


def build_initial_state(
    teams_path: str = "data/fchl_teams.json",
    players_path: str = "data/players.csv",
    odds_path: str = "data/team_odds.json",
    model_params_path: str = "data/model_params.json",
) -> AuctionState:
    """Full startup pipeline: load all data, build initial AuctionState."""
    metadata = load_team_metadata(teams_path)
    team_odds = load_team_odds(odds_path)
    team_players, biddable = load_players(players_path, team_odds)

    # Build TeamState for each team defined in metadata
    teams: dict[str, TeamState] = {}
    for code, info in metadata.items():
        if not isinstance(info, dict):
            continue  # Skip nomination_order, snake_draft, etc.
        if "id" not in info:
            continue

        players_data = team_players.get(code, {"keepers": [], "minors": []})
        teams[code] = TeamState(
            code=code,
            name=info["name"],
            keeper_players=players_data["keepers"],
            minor_players=players_data["minors"],
            penalties=info.get("penalty", 0.0),
            colors=info.get("colors", {}),
            logo=info.get("logo", ""),
            is_my_team=info.get("is_my_team", False),
        )

    nomination_order = metadata.get("nomination_order", [])
    snake_draft = metadata.get("snake_draft", True)

    return AuctionState(
        teams=teams,
        available_players=biddable,
        nomination_order=nomination_order,
        snake_draft=snake_draft,
    )

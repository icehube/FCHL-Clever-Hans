"""Load all data files and build the initial AuctionState."""

import csv
import json
import logging
import os
from collections import defaultdict

from config import (
    DEFAULT_TEAM_PROBABILITY,
    MINOR_CAP_GROUPS,
    NHL_TEAM_ALIASES,
    RFA_GROUPS,
)
from price_model import compute_pos_ranks
from state import AuctionState, Player, PlayerOnRoster, TeamState

# Team codes that are real FCHL teams (not UFA/RFA placeholders)
_PLACEHOLDER_TEAMS = {"UFA", "RFA"}

# Renames the most recent load_players() applied: original name -> [new names].
# Module-level rather than a third element of the return tuple, because both
# callers unpack positionally and neither wants it. Reset on every call.
last_disambiguations: dict[str, list[str]] = {}

# The same thing for the pool the APP is running on. Written only by
# build_initial_state, which is the only path that produces an AuctionState, so
# the banner cannot end up describing a CSV the draft is not using — a test
# loading a fixture through load_players clears the global above, and with one
# shared dict that silently emptied the banner for every test after it.
loaded_disambiguations: dict[str, list[str]] = {}


def load_team_metadata(path: str = "data/fchl_teams.json") -> dict:
    """Load team configs, nomination order, and penalties."""
    with open(path) as f:
        data = json.load(f)
    return data


def load_team_odds(path: str = "data/team_odds.json") -> dict[str, float]:
    """Load Stanley Cup odds as PERCENT (11.04 = 11.04%).

    team_odds.json stores fractions; the price model was trained on
    vig-removed percentages (each season sums to 100), so convert here.
    """
    with open(path) as f:
        data = json.load(f)
    odds = {team: prob * 100.0 for team, prob in data["odds"].items()}
    # Apply aliases so lookups work with either name
    for alias, canonical in NHL_TEAM_ALIASES.items():
        if canonical in odds and alias not in odds:
            odds[alias] = odds[canonical]
    return odds


def _get_team_probability(nhl_team: str, odds: dict[str, float]) -> float:
    """Look up team probability with alias resolution and default fallback."""
    canonical = NHL_TEAM_ALIASES.get(nhl_team, nhl_team)
    return odds.get(canonical, DEFAULT_TEAM_PROBABILITY)


def load_goalie_wins(path: str = "data/goalie_projection_stats.csv") -> dict[str, float]:
    """Load projected goalie WINS for the most recent season in the file.

    The pricer repo's parse_projections.py regenerates this CSV from the
    current Dobber projections — goalies are priced on wins, not the 2W+3SO
    composite. Goalies missing here fall back to points / goalie_pts_per_win
    inside the price model, so a missing file degrades gracefully.
    """
    if not os.path.exists(path):
        logging.warning("No goalie projection stats at %s — using points fallback", path)
        return {}

    rows = []
    with open(path) as f:
        for row in csv.DictReader(f):
            if row.get("proj_wins", "").strip():
                rows.append(row)
    if not rows:
        return {}

    latest = max(r["league_year"] for r in rows)
    return {
        r["player_name"].strip(): float(r["proj_wins"])
        for r in rows
        if r["league_year"] == latest
    }


def _disambiguated_names(rows: list[dict]) -> list[str]:
    """One name per CSV row, unique across the file.

    The player NAME is this app's primary key everywhere — `biddable`,
    `available_players`, `find_player`, `market_prices`, the transaction log,
    ~20 endpoints' `player` form field, the buyout dots' DOM ids. A repeated
    name is therefore not cosmetic. It fails two different ways:

    - two BIDDABLE rows: `biddable[name] = ...` overwrites and one player is
      silently missing from the pool (measured 2026-08-07: `Matt Murray` DAL
      and TOR, so 705 eligible rows loaded as 704 and the DAL one was
      undraftable);
    - one ROSTER row and one biddable row: nothing overwrites, because they go
      to different dicts — the same name is simply owned *and* draftable
      (`Jack Hughes`, `Elias Pettersson`, each a keeper on HSM and a UFA row).
      Today the zero-point exclusion hides both; the next projection refresh
      removes that cover.

    Escalates only as far as it must, because a suffix shows up on rosters and
    in the nomination panel: NHL team, then team + position (both Petterssons
    are VAN), then an ordinal. The tier is chosen per NAME GROUP rather than
    per row, so every member of a group is suffixed the same way — renaming
    only the second gives "Elias Pettersson" beside "Elias Pettersson (VAN D)",
    which reads as one player listed twice.

    File order decides the ordinal, so the same CSV always produces the same
    names — a shuffling key would move DOM ids and `available_players` keys
    between loads for no reason.
    """
    last_disambiguations.clear()
    names = [row["PLAYER"].strip() for row in rows]

    groups: dict[str, list[int]] = defaultdict(list)
    for i, name in enumerate(names):
        groups[name].append(i)

    # Every raw name is off-limits as a replacement: a file that already
    # literally contains "Matt Murray (DAL)" must not have one generated for it.
    taken = set(names)

    for base, idxs in groups.items():
        if len(idxs) == 1:
            continue

        def by_team(i: int) -> str:
            return f"{base} ({rows[i]['NHL TEAM'].strip()})"

        def by_team_and_pos(i: int) -> str:
            return f"{base} ({rows[i]['NHL TEAM'].strip()} {rows[i]['POS'].strip()})"

        for tier in (by_team, by_team_and_pos):
            candidates = [tier(i) for i in idxs]
            # `taken` still holds `base` itself, which every candidate differs
            # from by construction — discount it, or every tier would fail
            # against the very name it is replacing.
            if len(set(candidates)) == len(candidates) and not (
                set(candidates) & (taken - {base})
            ):
                break
        else:
            # Same name, same NHL team, same position. Ordinals always work.
            candidates = []
            n = 1
            for _ in idxs:
                while f"{base} (#{n})" in taken:
                    n += 1
                candidates.append(f"{base} (#{n})")
                n += 1

        for i, name in zip(idxs, candidates):
            names[i] = name
        taken.update(candidates)
        last_disambiguations[base] = candidates

    if last_disambiguations:
        logging.warning(
            "players.csv has %d duplicate name(s); renamed to keep them apart: %s",
            len(last_disambiguations),
            "; ".join(f"{k} -> {', '.join(v)}" for k, v in last_disambiguations.items()),
        )

    return names


def load_players(
    path: str = "data/players.csv",
    team_odds: dict[str, float] | None = None,
    goalie_wins: dict[str, float] | None = None,
) -> tuple[dict[str, list], dict[str, Player]]:
    """
    Parse players.csv into team rosters and biddable players.

    Returns:
        team_players: dict mapping team_code -> {"keepers": [...], "minors": [...]}
        biddable: dict mapping player_name -> Player
    """
    if team_odds is None:
        team_odds = {}
    if goalie_wins is None:
        goalie_wins = {}

    team_players: dict[str, dict[str, list]] = {}
    biddable: dict[str, Player] = {}

    with open(path) as f:
        rows = list(csv.DictReader(f))

    # Materialized rather than streamed because uniqueness is a property of the
    # whole file — a collision cannot be spotted one row at a time. 2158 rows.
    names = _disambiguated_names(rows)

    for row, name in zip(rows, names):
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
            # Biddable player (UFA or RFA) — skip zero-point players
            if pts == 0:
                continue
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
                # Keyed on the SOURCE name, not `name`: goalie_projection_stats.csv
                # carries the raw CSV name and cannot disambiguate either, so
                # looking a rename up in it would silently drop proj_wins for
                # every renamed goalie and quietly fall back to the pts/win
                # approximation. Two goalies sharing a name therefore share a
                # wins figure — that is the source data's limit, not ours.
                proj_wins=(
                    goalie_wins.get(row["PLAYER"].strip()) if position == "G" else None
                ),
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
                # BOTH destinations are keepers by provenance: this branch is
                # "already on an FCHL team before the auction", and STATUS only
                # says where on that team. A pre-auction MINOR recalled during
                # the draft is not a player you bought, so he must not colour
                # like one — the original bug report only noticed the
                # START -> minors -> recall path.
                is_keeper=True,
            )
            if fchl_team not in team_players:
                team_players[fchl_team] = {"keepers": [], "minors": []}
            if is_minor:
                team_players[fchl_team]["minors"].append(roster_player)
            else:
                team_players[fchl_team]["keepers"].append(roster_player)

    # Scarcity feature: rank within the draft-time pool, fixed for the whole
    # auction (re-ranking the shrinking pool would inflate remaining prices).
    for name, rank in compute_pos_ranks(biddable).items():
        biddable[name].pos_rank = rank

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
    goalie_wins = load_goalie_wins()
    team_players, biddable = load_players(players_path, team_odds, goalie_wins)
    # Snapshot for the banner. Taken HERE rather than read from
    # `last_disambiguations` at render time, so that anything else calling
    # load_players — a test fixture, the pre-auction runbook — cannot make the
    # banner describe a file this pool did not come from.
    loaded_disambiguations.clear()
    loaded_disambiguations.update(last_disambiguations)

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

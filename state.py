"""Auction state: players, teams, serialization, undo."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field

from config import (
    MAX_SALARY,
    MIN_SALARY,
    MINOR_CAP_GROUPS,
    POSITION_MINIMUMS,
    ROSTER_SIZE,
    SALARY_CAP,
)

MAX_SNAPSHOTS = 50


@dataclass
class Player:
    """A biddable player available at auction."""

    name: str
    position: str  # "F", "D", "G"
    group: str  # "2", "3", "RFA1", "RFA2", etc.
    nhl_team: str
    age: int
    projected_points: int
    is_rfa: bool
    salary: float  # Prior salary for RFAs, stale for UFAs
    team_probability: float  # Stanley Cup odds for their NHL team
    prior_fchl_team: str = ""  # For RFAs: which FCHL team previously held them


@dataclass
class PlayerOnRoster:
    """A player on a team's roster (keeper, minor, or acquired)."""

    name: str
    position: str
    group: str
    salary: float
    projected_points: int
    nhl_team: str = ""
    is_minor: bool = False
    is_bench: bool = False

    @property
    def counts_on_cap(self) -> bool:
        """Whether this player's salary counts toward the team's cap."""
        if not self.is_minor:
            return True
        return self.group in MINOR_CAP_GROUPS


@dataclass
class TeamState:
    """State of one FCHL team during the auction."""

    code: str
    name: str
    keeper_players: list[PlayerOnRoster] = field(default_factory=list)
    minor_players: list[PlayerOnRoster] = field(default_factory=list)
    acquired_players: list[PlayerOnRoster] = field(default_factory=list)
    penalties: float = 0.0
    is_done: bool = False
    colors: dict[str, str] = field(default_factory=dict)
    logo: str = ""
    is_my_team: bool = False
    _roster_cache: list[PlayerOnRoster] | None = field(default=None, repr=False)

    def _invalidate_cache(self) -> None:
        self._roster_cache = None

    @property
    def roster_players(self) -> list[PlayerOnRoster]:
        """All players on active roster (keepers + acquired, NOT minors)."""
        if self._roster_cache is None:
            self._roster_cache = self.keeper_players + self.acquired_players
        return self._roster_cache

    @property
    def all_players(self) -> list[PlayerOnRoster]:
        """All players including minors."""
        return self.roster_players + self.minor_players

    @property
    def total_salary(self) -> float:
        """Cap-counted salary: roster salaries + cap-eligible minor salaries + penalties."""
        roster_sal = sum(p.salary for p in self.roster_players)
        minor_sal = sum(p.salary for p in self.minor_players if p.counts_on_cap)
        return roster_sal + minor_sal + self.penalties

    @property
    def remaining_budget(self) -> float:
        """How much cap space is left."""
        return SALARY_CAP - self.total_salary

    @property
    def roster_count(self) -> int:
        """Active roster size (keepers + acquired, NOT minors)."""
        return len(self.roster_players)

    @property
    def total_spots_remaining(self) -> int:
        """How many more players can be added to active roster."""
        return ROSTER_SIZE - self.roster_count

    @property
    def position_counts(self) -> dict[str, int]:
        """F/D/G counts on active roster (not minors)."""
        counts = {"F": 0, "D": 0, "G": 0}
        for p in self.roster_players:
            counts[p.position] = counts.get(p.position, 0) + 1
        return counts

    @property
    def roster_needs(self) -> dict[str, int]:
        """How many more F/D/G needed to meet position minimums."""
        counts = self.position_counts
        return {
            pos: max(0, minimum - counts.get(pos, 0))
            for pos, minimum in POSITION_MINIMUMS.items()
        }

    @property
    def min_budget_reserved(self) -> float:
        """Budget that must be reserved for remaining roster spots at MIN_SALARY."""
        return self.total_spots_remaining * MIN_SALARY

    @property
    def spendable_budget(self) -> float:
        """Budget available for the next pick (remaining minus reserved)."""
        return self.remaining_budget - self.min_budget_reserved

    @property
    def physical_max_bid(self) -> float:
        """Maximum this team can bid on any single player.

        This is spendable + MIN_SALARY because bidding on a player fills one
        of the reserved spots (replacing its MIN_SALARY reservation with the
        actual bid amount).
        """
        if self.total_spots_remaining <= 0:
            return 0.0
        return min(self.spendable_budget + MIN_SALARY, MAX_SALARY)

    @property
    def current_roster_points(self) -> int:
        """Sum of projected points for all active roster players."""
        return sum(p.projected_points for p in self.roster_players)

    def find_player(self, name: str) -> PlayerOnRoster | None:
        """Find a player by name across all lists."""
        for p in self.all_players:
            if p.name == name:
                return p
        return None

    def remove_player(self, name: str) -> PlayerOnRoster:
        """Remove and return a player by name. Raises ValueError if not found."""
        for player_list in [self.keeper_players, self.acquired_players, self.minor_players]:
            for i, p in enumerate(player_list):
                if p.name == name:
                    self._invalidate_cache()
                    return player_list.pop(i)
        raise ValueError(f"Player '{name}' not found on team {self.code}")

    def add_acquired_player(self, player: PlayerOnRoster) -> None:
        """Add a player drafted during the auction."""
        self.acquired_players.append(player)
        self._invalidate_cache()

    def adjust_salary(self, player_name: str, new_salary: float) -> None:
        """Correct a player's salary (typo fix)."""
        p = self.find_player(player_name)
        if p is None:
            raise ValueError(f"Player '{player_name}' not found on team {self.code}")
        p.salary = new_salary
        self._invalidate_cache()


@dataclass
class TransactionRecord:
    """Record of a single auction transaction."""

    player_name: str
    position: str
    team_code: str
    salary: float
    model_price: float
    market_price: float
    timestamp: str
    transaction_type: str  # "draft", "trade_give", "trade_receive", "buyout"


@dataclass
class AuctionState:
    """Complete state of the auction at any point in time."""

    teams: dict[str, TeamState] = field(default_factory=dict)
    available_players: dict[str, Player] = field(default_factory=dict)
    transaction_log: list[TransactionRecord] = field(default_factory=list)
    nomination_order: list[str] = field(default_factory=list)
    nomination_round: int = 0
    nomination_index: int = 0
    snake_draft: bool = True
    _snapshots: list[str] = field(default_factory=list, repr=False)

    def current_nominator(self) -> str | None:
        """Which team nominates next, respecting snake draft and is_done."""
        order = self._effective_order()
        if not order:
            return None
        idx = self.nomination_index % len(order)
        return order[idx]

    def advance_nomination(self) -> None:
        """Move to the next nominator."""
        order = self._effective_order()
        if not order:
            return
        self.nomination_index += 1
        if self.nomination_index >= len(order):
            self.nomination_index = 0
            self.nomination_round += 1

    def _effective_order(self) -> list[str]:
        """Nomination order for the current round, skipping done teams."""
        active = [t for t in self.nomination_order if not self.teams[t].is_done]
        if self.snake_draft and self.nomination_round % 2 == 1:
            active = list(reversed(active))
        return active

    def save_snapshot(self) -> None:
        """Save current state for undo. Keeps last MAX_SNAPSHOTS."""
        snapshot = self.to_json(include_snapshots=False)
        self._snapshots.append(snapshot)
        if len(self._snapshots) > MAX_SNAPSHOTS:
            self._snapshots.pop(0)

    def restore_snapshot(self) -> bool:
        """Restore the most recent snapshot. Returns False if no snapshots."""
        if not self._snapshots:
            return False
        snapshot = self._snapshots.pop()
        restored = AuctionState.from_json(snapshot)
        self.teams = restored.teams
        self.available_players = restored.available_players
        self.transaction_log = restored.transaction_log
        self.nomination_order = restored.nomination_order
        self.nomination_round = restored.nomination_round
        self.nomination_index = restored.nomination_index
        self.snake_draft = restored.snake_draft
        return True

    def to_json(self, include_snapshots: bool = True) -> str:
        """Serialize state to JSON string."""
        data = {
            "teams": {
                code: _team_to_dict(team) for code, team in self.teams.items()
            },
            "available_players": {
                name: _player_to_dict(p) for name, p in self.available_players.items()
            },
            "transaction_log": [_transaction_to_dict(t) for t in self.transaction_log],
            "nomination_order": self.nomination_order,
            "nomination_round": self.nomination_round,
            "nomination_index": self.nomination_index,
            "snake_draft": self.snake_draft,
        }
        if include_snapshots:
            data["_snapshots"] = self._snapshots
        return json.dumps(data)

    @classmethod
    def from_json(cls, json_str: str) -> AuctionState:
        """Deserialize state from JSON string."""
        data = json.loads(json_str)
        state = cls()
        state.teams = {
            code: _team_from_dict(d) for code, d in data["teams"].items()
        }
        state.available_players = {
            name: _player_from_dict(d)
            for name, d in data["available_players"].items()
        }
        state.transaction_log = [
            _transaction_from_dict(d) for d in data["transaction_log"]
        ]
        state.nomination_order = data["nomination_order"]
        state.nomination_round = data["nomination_round"]
        state.nomination_index = data["nomination_index"]
        state.snake_draft = data["snake_draft"]
        state._snapshots = data.get("_snapshots", [])
        return state


# -- Serialization helpers --

def _player_on_roster_to_dict(p: PlayerOnRoster) -> dict:
    return {
        "name": p.name,
        "position": p.position,
        "group": p.group,
        "salary": p.salary,
        "projected_points": p.projected_points,
        "nhl_team": p.nhl_team,
        "is_minor": p.is_minor,
        "is_bench": p.is_bench,
    }


def _player_on_roster_from_dict(d: dict) -> PlayerOnRoster:
    return PlayerOnRoster(
        name=d["name"],
        position=d["position"],
        group=d["group"],
        salary=d["salary"],
        projected_points=d["projected_points"],
        nhl_team=d.get("nhl_team", ""),
        is_minor=d.get("is_minor", False),
        is_bench=d.get("is_bench", False),
    )


def _team_to_dict(t: TeamState) -> dict:
    return {
        "code": t.code,
        "name": t.name,
        "keeper_players": [_player_on_roster_to_dict(p) for p in t.keeper_players],
        "minor_players": [_player_on_roster_to_dict(p) for p in t.minor_players],
        "acquired_players": [_player_on_roster_to_dict(p) for p in t.acquired_players],
        "penalties": t.penalties,
        "is_done": t.is_done,
        "colors": t.colors,
        "logo": t.logo,
        "is_my_team": t.is_my_team,
    }


def _team_from_dict(d: dict) -> TeamState:
    team = TeamState(
        code=d["code"],
        name=d["name"],
        keeper_players=[_player_on_roster_from_dict(p) for p in d["keeper_players"]],
        minor_players=[_player_on_roster_from_dict(p) for p in d["minor_players"]],
        acquired_players=[_player_on_roster_from_dict(p) for p in d["acquired_players"]],
        penalties=d["penalties"],
        is_done=d.get("is_done", False),
        colors=d.get("colors", {}),
        logo=d.get("logo", ""),
        is_my_team=d.get("is_my_team", False),
    )
    team._invalidate_cache()
    return team


def _player_to_dict(p: Player) -> dict:
    return {
        "name": p.name,
        "position": p.position,
        "group": p.group,
        "nhl_team": p.nhl_team,
        "age": p.age,
        "projected_points": p.projected_points,
        "is_rfa": p.is_rfa,
        "salary": p.salary,
        "team_probability": p.team_probability,
        "prior_fchl_team": p.prior_fchl_team,
    }


def _player_from_dict(d: dict) -> Player:
    return Player(
        name=d["name"],
        position=d["position"],
        group=d["group"],
        nhl_team=d["nhl_team"],
        age=d["age"],
        projected_points=d["projected_points"],
        is_rfa=d["is_rfa"],
        salary=d["salary"],
        team_probability=d["team_probability"],
        prior_fchl_team=d.get("prior_fchl_team", ""),
    )


def _transaction_to_dict(t: TransactionRecord) -> dict:
    return {
        "player_name": t.player_name,
        "position": t.position,
        "team_code": t.team_code,
        "salary": t.salary,
        "model_price": t.model_price,
        "market_price": t.market_price,
        "timestamp": t.timestamp,
        "transaction_type": t.transaction_type,
    }


def _transaction_from_dict(d: dict) -> TransactionRecord:
    return TransactionRecord(
        player_name=d["player_name"],
        position=d["position"],
        team_code=d["team_code"],
        salary=d["salary"],
        model_price=d["model_price"],
        market_price=d["market_price"],
        timestamp=d["timestamp"],
        transaction_type=d["transaction_type"],
    )

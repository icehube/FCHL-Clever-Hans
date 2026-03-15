"""Tests for state.py: TeamState properties, serialization, snapshots."""

import pytest

from config import MAX_SALARY, MIN_SALARY, ROSTER_SIZE, SALARY_CAP
from state import AuctionState, Player, PlayerOnRoster, TeamState, TransactionRecord


def _make_player_on_roster(
    name: str = "Test Player",
    position: str = "F",
    group: str = "3",
    salary: float = 2.0,
    projected_points: int = 50,
    is_minor: bool = False,
) -> PlayerOnRoster:
    return PlayerOnRoster(
        name=name,
        position=position,
        group=group,
        salary=salary,
        projected_points=projected_points,
        is_minor=is_minor,
    )


def _make_team(
    code: str = "TST",
    keepers: list[PlayerOnRoster] | None = None,
    minors: list[PlayerOnRoster] | None = None,
    acquired: list[PlayerOnRoster] | None = None,
    penalties: float = 0.0,
) -> TeamState:
    return TeamState(
        code=code,
        name="Test Team",
        keeper_players=keepers or [],
        minor_players=minors or [],
        acquired_players=acquired or [],
        penalties=penalties,
        colors={"primary": "#000", "secondary": "#fff"},
        logo="1.gif",
    )


class TestPlayerOnRoster:
    def test_roster_player_counts_on_cap(self):
        p = _make_player_on_roster(is_minor=False, group="C")
        assert p.counts_on_cap is True

    def test_minor_group_2_counts_on_cap(self):
        p = _make_player_on_roster(is_minor=True, group="2")
        assert p.counts_on_cap is True

    def test_minor_group_3_counts_on_cap(self):
        p = _make_player_on_roster(is_minor=True, group="3")
        assert p.counts_on_cap is True

    def test_minor_group_C_does_not_count_on_cap(self):
        p = _make_player_on_roster(is_minor=True, group="C")
        assert p.counts_on_cap is False

    def test_minor_group_A_does_not_count_on_cap(self):
        p = _make_player_on_roster(is_minor=True, group="A")
        assert p.counts_on_cap is False


class TestTeamStateSalary:
    def test_total_salary_keepers_only(self):
        keepers = [
            _make_player_on_roster("P1", salary=5.0),
            _make_player_on_roster("P2", salary=3.0),
        ]
        team = _make_team(keepers=keepers)
        assert team.total_salary == 8.0

    def test_total_salary_with_cap_eligible_minors(self):
        keepers = [_make_player_on_roster("P1", salary=10.0)]
        minors = [
            _make_player_on_roster("M1", salary=0.5, group="3", is_minor=True),
            _make_player_on_roster("M2", salary=0.5, group="2", is_minor=True),
        ]
        team = _make_team(keepers=keepers, minors=minors)
        assert team.total_salary == 11.0  # 10.0 + 0.5 + 0.5

    def test_total_salary_excludes_non_cap_minors(self):
        keepers = [_make_player_on_roster("P1", salary=10.0)]
        minors = [
            _make_player_on_roster("M1", salary=3.0, group="C", is_minor=True),
            _make_player_on_roster("M2", salary=0.5, group="A", is_minor=True),
        ]
        team = _make_team(keepers=keepers, minors=minors)
        assert team.total_salary == 10.0  # Minors don't count

    def test_total_salary_includes_penalties(self):
        keepers = [_make_player_on_roster("P1", salary=10.0)]
        team = _make_team(keepers=keepers, penalties=1.5)
        assert team.total_salary == 11.5

    def test_total_salary_with_acquired(self):
        keepers = [_make_player_on_roster("P1", salary=5.0)]
        acquired = [_make_player_on_roster("A1", salary=2.0)]
        team = _make_team(keepers=keepers, acquired=acquired)
        assert team.total_salary == 7.0


class TestTeamStateBudget:
    def test_remaining_budget(self):
        keepers = [_make_player_on_roster("P1", salary=30.0)]
        team = _make_team(keepers=keepers)
        assert team.remaining_budget == SALARY_CAP - 30.0

    def test_roster_count_excludes_minors(self):
        keepers = [_make_player_on_roster("P1"), _make_player_on_roster("P2")]
        minors = [_make_player_on_roster("M1", is_minor=True)]
        team = _make_team(keepers=keepers, minors=minors)
        assert team.roster_count == 2  # Not 3

    def test_total_spots_remaining(self):
        keepers = [_make_player_on_roster(f"P{i}") for i in range(12)]
        team = _make_team(keepers=keepers)
        assert team.total_spots_remaining == ROSTER_SIZE - 12

    def test_min_budget_reserved(self):
        keepers = [_make_player_on_roster(f"P{i}") for i in range(12)]
        team = _make_team(keepers=keepers)
        assert team.min_budget_reserved == 12 * MIN_SALARY

    def test_spendable_budget(self):
        keepers = [_make_player_on_roster(f"P{i}", salary=2.0) for i in range(12)]
        team = _make_team(keepers=keepers)
        # remaining = 56.8 - 24.0 = 32.8
        # reserved = 12 * 0.5 = 6.0
        # spendable = 32.8 - 6.0 = 26.8
        assert team.spendable_budget == pytest.approx(26.8)

    def test_physical_max_bid_capped(self):
        """When spendable > MAX_SALARY, physical max is capped."""
        keepers = [_make_player_on_roster("P1", salary=1.0)]
        team = _make_team(keepers=keepers)
        assert team.physical_max_bid == MAX_SALARY

    def test_physical_max_bid_limited_by_budget(self):
        """When budget is tight, physical max is below MAX_SALARY."""
        keepers = [_make_player_on_roster(f"P{i}", salary=2.5) for i in range(22)]
        team = _make_team(keepers=keepers)
        # remaining = 56.8 - 55.0 = 1.8
        # reserved = 2 * 0.5 = 1.0
        # spendable = 0.8
        assert team.spendable_budget == pytest.approx(0.8)
        assert team.physical_max_bid == pytest.approx(0.8)
        assert team.physical_max_bid < MAX_SALARY


class TestTeamStateRosterNeeds:
    def test_empty_team_needs_all(self):
        team = _make_team()
        needs = team.roster_needs
        assert needs == {"F": 14, "D": 7, "G": 3}

    def test_partial_roster(self):
        keepers = [
            _make_player_on_roster(f"F{i}", position="F") for i in range(7)
        ] + [
            _make_player_on_roster(f"D{i}", position="D") for i in range(3)
        ] + [
            _make_player_on_roster("G0", position="G")
        ]
        team = _make_team(keepers=keepers)
        needs = team.roster_needs
        assert needs == {"F": 7, "D": 4, "G": 2}

    def test_full_roster_needs_zero(self):
        keepers = (
            [_make_player_on_roster(f"F{i}", position="F") for i in range(14)]
            + [_make_player_on_roster(f"D{i}", position="D") for i in range(7)]
            + [_make_player_on_roster(f"G{i}", position="G") for i in range(3)]
        )
        team = _make_team(keepers=keepers)
        needs = team.roster_needs
        assert needs == {"F": 0, "D": 0, "G": 0}

    def test_minors_dont_count_toward_needs(self):
        minors = [_make_player_on_roster(f"F{i}", position="F", is_minor=True) for i in range(5)]
        team = _make_team(minors=minors)
        assert team.roster_needs["F"] == 14  # Minors don't help


class TestTeamStatePlayerOps:
    def test_find_player(self):
        keepers = [_make_player_on_roster("Alice"), _make_player_on_roster("Bob")]
        team = _make_team(keepers=keepers)
        assert team.find_player("Bob") is not None
        assert team.find_player("Charlie") is None

    def test_find_player_in_minors(self):
        minors = [_make_player_on_roster("Minor1", is_minor=True)]
        team = _make_team(minors=minors)
        assert team.find_player("Minor1") is not None

    def test_remove_player(self):
        keepers = [_make_player_on_roster("Alice"), _make_player_on_roster("Bob")]
        team = _make_team(keepers=keepers)
        removed = team.remove_player("Alice")
        assert removed.name == "Alice"
        assert len(team.keeper_players) == 1
        assert team.find_player("Alice") is None

    def test_remove_player_not_found(self):
        team = _make_team()
        with pytest.raises(ValueError, match="not found"):
            team.remove_player("Nobody")

    def test_add_acquired_player(self):
        team = _make_team()
        p = _make_player_on_roster("New Guy", salary=3.0)
        team.add_acquired_player(p)
        assert len(team.acquired_players) == 1
        assert team.roster_count == 1


class TestAuctionStateSerialization:
    def _make_state(self) -> AuctionState:
        team = _make_team(
            code="BOT",
            keepers=[_make_player_on_roster("Keeper1", salary=5.0)],
            minors=[_make_player_on_roster("Minor1", group="3", salary=0.5, is_minor=True)],
        )
        player = Player(
            name="Available1",
            position="F",
            group="3",
            nhl_team="TOR",
            age=25,
            projected_points=80,
            is_rfa=False,
            salary=0.0,
            team_probability=0.04,
        )
        state = AuctionState(
            teams={"BOT": team},
            available_players={"Available1": player},
            transaction_log=[
                TransactionRecord(
                    player_name="Drafted1",
                    position="F",
                    team_code="SRL",
                    salary=3.0,
                    model_price=2.5,
                    market_price=2.8,
                    timestamp="2026-03-15T10:00:00",
                    transaction_type="draft",
                )
            ],
            nomination_order=["BOT", "SRL"],
            nomination_round=1,
            nomination_index=0,
            snake_draft=True,
        )
        return state

    def test_round_trip(self):
        state = self._make_state()
        json_str = state.to_json()
        restored = AuctionState.from_json(json_str)

        assert restored.teams["BOT"].code == "BOT"
        assert restored.teams["BOT"].keeper_players[0].name == "Keeper1"
        assert restored.teams["BOT"].minor_players[0].name == "Minor1"
        assert restored.teams["BOT"].total_salary == pytest.approx(5.5)
        assert "Available1" in restored.available_players
        assert restored.available_players["Available1"].projected_points == 80
        assert len(restored.transaction_log) == 1
        assert restored.nomination_round == 1
        assert restored.snake_draft is True

    def test_round_trip_preserves_types(self):
        state = self._make_state()
        json_str = state.to_json()
        restored = AuctionState.from_json(json_str)

        assert isinstance(restored.teams["BOT"], TeamState)
        assert isinstance(restored.teams["BOT"].keeper_players[0], PlayerOnRoster)
        assert isinstance(restored.available_players["Available1"], Player)
        assert isinstance(restored.transaction_log[0], TransactionRecord)


class TestAuctionStateSnapshots:
    def test_save_and_restore(self):
        state = AuctionState(
            teams={"BOT": _make_team(code="BOT", keepers=[_make_player_on_roster("P1", salary=5.0)])},
            available_players={},
            nomination_order=["BOT"],
        )
        state.save_snapshot()

        # Mutate state
        state.teams["BOT"].acquired_players.append(
            _make_player_on_roster("NewGuy", salary=3.0)
        )
        assert state.teams["BOT"].roster_count == 2

        # Restore
        assert state.restore_snapshot() is True
        assert state.teams["BOT"].roster_count == 1

    def test_restore_empty_returns_false(self):
        state = AuctionState()
        assert state.restore_snapshot() is False

    def test_max_snapshots(self):
        state = AuctionState(
            teams={"BOT": _make_team(code="BOT")},
            available_players={},
            nomination_order=["BOT"],
        )
        for _ in range(60):
            state.save_snapshot()
        assert len(state._snapshots) == 50


class TestNominationOrder:
    def test_current_nominator(self):
        state = AuctionState(
            teams={
                "A": _make_team(code="A"),
                "B": _make_team(code="B"),
                "C": _make_team(code="C"),
            },
            nomination_order=["A", "B", "C"],
        )
        assert state.current_nominator() == "A"

    def test_advance_nomination(self):
        state = AuctionState(
            teams={
                "A": _make_team(code="A"),
                "B": _make_team(code="B"),
                "C": _make_team(code="C"),
            },
            nomination_order=["A", "B", "C"],
        )
        state.advance_nomination()
        assert state.current_nominator() == "B"

    def test_snake_draft_reverses_on_odd_round(self):
        state = AuctionState(
            teams={
                "A": _make_team(code="A"),
                "B": _make_team(code="B"),
                "C": _make_team(code="C"),
            },
            nomination_order=["A", "B", "C"],
            nomination_round=1,  # Odd round → reversed
            snake_draft=True,
        )
        assert state.current_nominator() == "C"

    def test_skips_done_teams(self):
        team_b = _make_team(code="B")
        team_b.is_done = True
        state = AuctionState(
            teams={
                "A": _make_team(code="A"),
                "B": team_b,
                "C": _make_team(code="C"),
            },
            nomination_order=["A", "B", "C"],
        )
        assert state.current_nominator() == "A"
        state.advance_nomination()
        assert state.current_nominator() == "C"  # B skipped

    def test_wrap_around_increments_round(self):
        state = AuctionState(
            teams={
                "A": _make_team(code="A"),
                "B": _make_team(code="B"),
            },
            nomination_order=["A", "B"],
            snake_draft=True,
        )
        assert state.nomination_round == 0
        state.advance_nomination()  # A done
        state.advance_nomination()  # B done, wraps
        assert state.nomination_round == 1
        # Round 1 is odd → reversed → first is B
        assert state.current_nominator() == "B"

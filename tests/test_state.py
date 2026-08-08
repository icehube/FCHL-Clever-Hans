"""Tests for state.py: TeamState properties, serialization, snapshots."""

import json

import pytest

from config import MAX_SALARY, MIN_SALARY, ROSTER_SIZE, SALARY_CAP
from state import (
    AuctionState,
    ChangeRecord,
    Player,
    PlayerOnRoster,
    TeamState,
    TransactionRecord,
)


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
        # approx, not ==: remaining_budget is quantized to the $0.1M increment
        # all league money moves in, so it will not equal the raw subtraction
        assert team.remaining_budget == pytest.approx(SALARY_CAP - 30.0)

    def test_budget_never_overstates_real_cap_space(self):
        """Reported budget must never exceed the cap space that actually exists.

        A buyout penalty is 50% of salary, so it lands on a half-increment:
        buying out a $2.1M player leaves a genuine $1.05M on the cap. Rounding
        that to nearest inflated remaining_budget by $0.05M for 10 of the 100
        reachable penalty values — enough for the MILP to plan a roster over
        the cap, and for physical_max_bid to name a bid the team can't make.
        """
        keepers = [_make_player_on_roster(f"P{i}", salary=2.0) for i in range(23)]
        team = _make_team(keepers=keepers)
        for step in range(1, 100):
            team.penalties = step * 0.05  # every buyout penalty is a half-step
            true_space = SALARY_CAP - team.total_salary
            assert team.remaining_budget <= true_space + 1e-9, (
                f"penalties=${team.penalties:.2f}M: reported "
                f"${team.remaining_budget}M of ${true_space}M real space"
            )
            # One reserved spot is replaced by the bid itself, so the physical
            # max may exceed remaining by at most that reservation.
            assert team.physical_max_bid <= true_space + MIN_SALARY + 1e-9
            tenths = team.remaining_budget * 10
            assert abs(tenths - round(tenths)) < 1e-9, "must land on a $0.1M step"

    def test_budget_keeps_the_full_increment_it_is_owed(self):
        """Flooring must not eat a legitimate increment.

        The float-error case this quantization exists for: $52.6M committed
        leaves exactly $4.2M, which arrives as 4.199999999999996.
        """
        keepers = [_make_player_on_roster(f"P{i}", salary=2.0) for i in range(23)]
        team = _make_team(keepers=keepers)
        team.penalties = 6.6
        assert SALARY_CAP - team.total_salary != 4.2, "precondition: float error"
        assert team.remaining_budget == 4.2

    def test_full_roster_with_cap_space_can_still_bid(self):
        """A 24-man team is not a spent force.

        The CBA lets teams draft past 24 — the extra goes to minors, and since
        every biddable player ends up in group 2 or 3 the salary counts fully
        on the cap. Returning 0.0 here made a cap-rich full team invisible to
        market.py, so late-draft ceilings read too low. Owner confirmed
        2026-08-05 that teams in this league do draft past 24.
        """
        keepers = [_make_player_on_roster(f"P{i}", salary=1.0) for i in range(ROSTER_SIZE)]
        team = _make_team(keepers=keepers)
        assert team.total_spots_remaining == 0
        assert team.remaining_budget == pytest.approx(SALARY_CAP - ROSTER_SIZE)
        # No spot reservation left to replace, so the whole budget is biddable
        # (capped at the max any single bid can be).
        assert team.physical_max_bid == min(team.remaining_budget, MAX_SALARY)

    def test_full_roster_without_cap_space_still_cannot_bid(self):
        """Budget, not roster size, is what actually stops a team bidding."""
        keepers = [_make_player_on_roster(f"P{i}", salary=1.0) for i in range(ROSTER_SIZE)]
        team = _make_team(keepers=keepers)
        team.penalties = SALARY_CAP - ROSTER_SIZE  # spend every last dollar
        assert team.total_spots_remaining == 0
        assert team.physical_max_bid < MIN_SALARY

    def test_over_full_roster_reserves_nothing(self):
        """Past 24, total_spots_remaining goes negative — the reserve must not
        follow it down, or spendable_budget reads ABOVE the real budget."""
        keepers = [_make_player_on_roster(f"P{i}", salary=1.0) for i in range(ROSTER_SIZE + 1)]
        team = _make_team(keepers=keepers)
        assert team.total_spots_remaining == -1
        assert team.min_budget_reserved == 0.0
        assert team.spendable_budget == team.remaining_budget

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
        # remaining = 56.8 - 55.0 = 1.8, spots = 2
        # physical_max = remaining - (spots-1)*MIN = 1.8 - 0.5 = 1.3
        assert team.spendable_budget == pytest.approx(0.8)
        assert team.physical_max_bid == pytest.approx(1.3)
        assert team.physical_max_bid < MAX_SALARY


class TestTeamStateRosterNeeds:
    def test_empty_team_needs_all(self):
        # Needs = starting-lineup requirements (12F/6D/2G); the 4 bench
        # spots are position-agnostic and never "needed"
        team = _make_team()
        needs = team.roster_needs
        assert needs == {"F": 12, "D": 6, "G": 2}

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
        assert needs == {"F": 5, "D": 3, "G": 1}

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
        assert team.roster_needs["F"] == 12  # Minors don't help


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


class TestSnapshotFieldsCannotDrift:
    """The same field set is maintained by hand in three places.

    `AuctionState`'s dataclass fields, `to_json`'s literal keys, and — until
    this class was written — eight assignments in `restore_snapshot`. They
    agreed, and nothing checked that they kept agreeing. Add a field and undo
    silently stops restoring it, in the one operation with nothing behind it:
    mid-draft there is no second Ctrl+Z to reach for and the loss is invisible
    until much later.

    `restore_snapshot` now enumerates `fields(self)`, so the restore side
    cannot drift at all. These two cover the serialization side, which the
    enumeration does NOT help — a field `to_json` never writes comes back from
    `from_json` as its DEFAULT, so undo would restore a zero rather than
    restore nothing, which is the worse of the two failures.

    Both are needed and they catch different things: dropping a key from
    `to_json` fails `test_every_field_reaches_the_json`, while making
    `from_json` ignore a key it still writes fails only
    `test_every_field_survives_a_round_trip`.
    """

    def _fields(self) -> set[str]:
        from dataclasses import fields

        # _snapshots is the undo chain itself, deliberately outside the
        # snapshot — see restore_snapshot.
        return {f.name for f in fields(AuctionState)} - {"_snapshots"}

    def _loaded(self) -> AuctionState:
        """A state with a DISTINCTIVE NON-DEFAULT in every single field.

        Its own builder rather than `TestAuctionStateSerialization._make_state`,
        which is shared with `test_round_trip` and asserts specific values —
        changing it in place would break a passing test to serve a new one. The
        non-defaults are the whole point: a field left at its default round-trips
        correctly even when nothing carries it.
        """
        return AuctionState(
            teams={"BOT": _make_team(code="BOT", keepers=[_make_player_on_roster("K1")])},
            available_players={
                "A1": Player(
                    name="A1", position="D", group="3", nhl_team="EDM", age=30,
                    projected_points=44, is_rfa=True, salary=1.5,
                    team_probability=0.09,
                )
            },
            transaction_log=[
                TransactionRecord(
                    player_name="Drafted", position="G", team_code="SRL",
                    salary=3.0, model_price=2.5, market_price=2.8,
                    timestamp="2026-03-15T10:00:00", transaction_type="draft",
                )
            ],
            change_log=[
                ChangeRecord(
                    timestamp="2026-03-15T10:05:00", kind="adjust-salary",
                    team_code="BOT", description="K1 2.0 -> 4.0",
                )
            ],
            nomination_order=["SRL", "BOT"],  # not alphabetical, not default
            nomination_round=3,
            nomination_index=1,
            snake_draft=False,
        )

    def test_every_field_reaches_the_json(self):
        """Structural: a field that was never serialized."""
        payload = json.loads(self._loaded().to_json(include_snapshots=False))
        assert self._fields() == set(payload), (
            "AuctionState fields and to_json keys have drifted — a field on "
            "one side and not the other is a field undo restores as a default"
        )

    @staticmethod
    def _value(obj) -> str:
        """Serialized form of a field, ignoring PRIVATE attributes.

        Records deserialize into equal-valued objects rather than identical
        ones, so these compare serialized forms — that is what lets one
        assertion serve every field with no per-field special case.

        The `startswith("_")` filter is the load-bearing part. A plain `vars`
        includes `TeamState._roster_cache`, which `roster_players` fills in
        lazily, so whether two equal teams compare equal would depend on
        whether anything happened to read that property first. These tests
        passed only because nothing did; touching `roster_players` before the
        comparison turns them red on a correct build. A caller-order-dependent
        false FAILURE is worse than a false pass — it fires at random on
        correct code and gets "fixed" by deleting the assertion. A cache is
        derived, not state, so undo owes it nothing.
        """
        return json.dumps(
            obj,
            default=lambda o: {
                k: v for k, v in vars(o).items() if not k.startswith("_")
            },
        )

    def test_every_field_survives_a_round_trip(self):
        """Behavioural: a field `to_json` writes and `from_json` ignores.

        Structural equality cannot see that — the key is present, it is simply
        never read back.
        """
        state = self._loaded()
        restored = AuctionState.from_json(state.to_json(include_snapshots=False))
        for name in sorted(self._fields()):
            assert self._value(getattr(restored, name)) == self._value(
                getattr(state, name)
            ), f"{name} did not survive to_json -> from_json"

    def test_a_populated_cache_does_not_change_the_answer(self):
        """Pins the filter above, which is invisible at every call site.

        `_roster_cache` is populated by reading `roster_players` — something a
        future assertion, or a property like `total_salary`, does incidentally.
        Without the filter this test fails; with it, the comparison is about
        state and nothing else.
        """
        state = self._loaded()
        restored = AuctionState.from_json(state.to_json(include_snapshots=False))
        assert state.teams["BOT"].roster_players  # populate on ONE side only
        assert state.teams["BOT"]._roster_cache is not None, "precondition"
        assert restored.teams["BOT"]._roster_cache is None, "precondition"
        assert self._value(restored.teams) == self._value(state.teams)

    def test_undo_restores_every_field(self):
        """The claim itself, end to end through save/restore.

        `TestAuctionStateSnapshots.test_save_and_restore` checks `teams` alone.
        This is the same question asked of all eight, which is what the
        enumeration in `restore_snapshot` promises.
        """
        state = self._loaded()
        state.save_snapshot()
        before = {n: self._value(getattr(state, n)) for n in self._fields()}

        state.teams = {}
        state.available_players = {}
        state.transaction_log = []
        state.change_log = []
        state.nomination_order = []
        state.nomination_round = 99
        state.nomination_index = 98
        state.snake_draft = True
        assert all(
            self._value(getattr(state, n)) != before[n] for n in self._fields()
        ), "the mutation left a field untouched, so restoring it proves nothing"

        assert state.restore_snapshot() is True
        for name in sorted(self._fields()):
            assert self._value(getattr(state, name)) == before[name], (
                f"undo did not restore {name}"
            )

    def test_the_undo_chain_is_not_itself_restored(self):
        """`_snapshots` is skipped, and that skip is load-bearing.

        Snapshots are written with `include_snapshots=False`, so the restored
        object's chain is always the empty default. Copy it and the first undo
        wipes every earlier one — Ctrl+Z would work exactly once per session.
        """
        state = self._loaded()
        state.save_snapshot()
        state.nomination_round = 10
        state.save_snapshot()
        state.nomination_round = 20

        assert state.restore_snapshot() is True
        assert state.nomination_round == 10
        assert state._snapshots, "the first undo emptied the chain"
        assert state.restore_snapshot() is True
        assert state.nomination_round == 3, "the second undo did not go back further"


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


class TestMinorsMovement:
    def test_send_to_minors_from_acquired(self):
        p = _make_player_on_roster(name="Acq Star", group="A")
        p.is_bench = True
        team = _make_team(acquired=[p])
        team.send_to_minors("Acq Star")
        assert team.acquired_players == []
        assert len(team.minor_players) == 1
        assert team.minor_players[0].is_minor is True

    def test_recall_from_minors(self):
        p = _make_player_on_roster(name="Demoted", group="A", is_minor=True)
        team = _make_team(minors=[p])
        team.recall_from_minors("Demoted")
        assert team.minor_players == []
        assert len(team.acquired_players) == 1
        assert team.acquired_players[0].is_minor is False

    def test_send_then_recall_round_trip(self):
        p = _make_player_on_roster(name="Yo-yo", group="B")
        p.is_bench = True
        team = _make_team(acquired=[p])
        team.send_to_minors("Yo-yo")
        team.recall_from_minors("Yo-yo")
        assert len(team.acquired_players) == 1
        assert team.minor_players == []
        assert team.acquired_players[0].is_minor is False

    def test_demoted_keeper_recalls_into_acquired(self):
        """A demoted keeper loses only the provenance label, and cap math holds.

        Nothing in the app branches on keeper-vs-acquired — every other reader
        concatenates the two — so landing in acquired_players on recall is
        cosmetic. Pinned because it is the one visible consequence of dropping
        the keeper refusal, and because the cap must return to where it started.
        """
        p = _make_player_on_roster(name="Round Tripper", group="A", salary=2.5)
        p.is_bench = True
        team = _make_team(keepers=[p])
        cap_before = team.total_salary

        team.send_to_minors("Round Tripper")
        assert team.total_salary == 0.0, "group A in the minors costs nothing"

        team.recall_from_minors("Round Tripper")
        assert team.keeper_players == [], "keeper label is not restored"
        assert [q.name for q in team.acquired_players] == ["Round Tripper"]
        assert team.total_salary == cap_before, "cap must return to where it began"
        assert len(team.roster_players) == 1, "still on the active roster either way"

    def test_send_active_player_raises(self):
        p = _make_player_on_roster(name="Starter", group="A")
        team = _make_team(acquired=[p])
        with pytest.raises(ValueError, match="benched"):
            team.send_to_minors("Starter")
        assert len(team.acquired_players) == 1
        assert team.minor_players == []

    def test_send_unknown_player_raises(self):
        team = _make_team()
        with pytest.raises(ValueError):
            team.send_to_minors("Nobody")

    def test_benched_keeper_can_be_sent_down(self):
        """Keepers may be demoted — "keeper" is provenance, not a league rule.

        Refusing them stranded group A-E players: they can't be bought out, and
        the minors is the only place their cap hit goes to zero. Inverted from
        test_send_keeper_raises, which pinned the old refusal.
        """
        keeper = _make_player_on_roster(name="Locked-In", group="A")
        keeper.is_bench = True
        team = _make_team(keepers=[keeper])

        team.send_to_minors("Locked-In")

        assert team.keeper_players == []
        assert [p.name for p in team.minor_players] == ["Locked-In"]
        assert team.minor_players[0].is_minor is True
        assert not team.minor_players[0].counts_on_cap, (
            "the whole point: a group-A player costs $0 in the minors"
        )

    def test_send_unbenched_keeper_still_raises(self):
        """Lifting the keeper rule must not lift the benched-first rule."""
        keeper = _make_player_on_roster(name="Locked-In", group="A")
        team = _make_team(keepers=[keeper])
        with pytest.raises(ValueError, match="benched"):
            team.send_to_minors("Locked-In")
        assert len(team.keeper_players) == 1
        assert team.minor_players == []


class TestEveryMutatingPostTakesASnapshot:
    """The third hand-maintained list, at the endpoint layer.

    Ten POST endpoints call `save_snapshot()`. Nothing says the eleventh has
    to. A new mutating endpoint that forgets is invisible until someone hits
    Ctrl+Z mid-draft and the wrong thing comes back — the failure surfaces at
    the worst possible moment, from a line of code written weeks earlier.

    Same shape as `TestShortcutsModal`: a set-equality check over a list that
    is otherwise maintained by memory. Adding an endpoint means either taking a
    snapshot or naming it here with a reason, in the same commit.

    **What this can and cannot prove.** It proves that a `save_snapshot()` call
    appears somewhere in each handler's body. It does NOT prove the call is
    reachable, that it runs before the mutation, or that it runs on every path
    — a call inside a branch that never fires reads as covered. And it only
    inspects `@app.post`; a GET that mutated `auction_state` would sail past.
    No GET does today (checked 2026-08-07 across all 24 routes, and the two
    that write anything write view state and the indicator cache, neither of
    which undo is responsible for), but nothing here enforces that.

    Those gaps are deliberate: the failure this exists to catch is a whole
    endpoint written without a snapshot at all, which is what actually happens.
    The per-endpoint undo tests in `tests/test_trade_buyout_undo.py` are what
    prove a snapshot is taken on the path that matters.
    """

    # Two ways to put a state on the undo chain, and both count. `save_snapshot`
    # captures and commits in one step, which is right for an endpoint that
    # cannot reject after that point. An endpoint that CAN reject captures
    # first and calls `commit_snapshot` only on the success path, so a refusal
    # leaves the chain untouched — `capture_snapshot` deliberately does NOT
    # appear here, because capturing without committing snapshots nothing.
    SNAPSHOTTING_CALLS = {"save_snapshot", "commit_snapshot"}

    # Every POST that legitimately takes no snapshot, and why. Not a
    # suppression list — an entry is a claim that the endpoint does not change
    # state that undo is responsible for.
    NO_SNAPSHOT_NEEDED = {
        "/bid-check": "POST but read-only — computes advice, changes nothing",
        "/trade-evaluate": "POST but read-only — computes a verdict, changes nothing",
        "/undo": "pops the chain; snapshotting here would fight itself",
        "/reset": "replaces the world, so the old chain is meaningless",
        "/load-scenario": "takes its own explicitly, to survive replacing the global",
    }

    def _post_routes(self) -> dict[str, bool]:
        """Every `@app.post` route in main.py -> does its handler snapshot."""
        import ast
        from pathlib import Path

        source = (Path(__file__).resolve().parent.parent / "main.py").read_text()
        found: dict[str, bool] = {}
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                fn = dec.func
                if not (isinstance(fn, ast.Attribute) and fn.attr == "post"):
                    continue
                if not (isinstance(fn.value, ast.Name) and fn.value.id == "app"):
                    continue
                route = dec.args[0].value
                found[route] = any(
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr in self.SNAPSHOTTING_CALLS
                    for n in ast.walk(node)
                )
        return found

    def test_the_walk_finds_the_routes(self):
        """The guard below is vacuous if the ast walk matches nothing.

        A decorator rename or a router refactor would empty `_post_routes` and
        turn every assertion here green — the failure mode of every test that
        derives its own subject.
        """
        routes = self._post_routes()
        assert len(routes) >= 12, f"only found {len(routes)} POST routes: {sorted(routes)}"
        assert "/assign" in routes and routes["/assign"], (
            "/assign is the canonical snapshotting endpoint; if the walk says "
            "otherwise the walk is broken, not /assign"
        )

    def test_every_mutating_post_snapshots(self):
        routes = self._post_routes()
        missing = sorted(
            r for r, snaps in routes.items()
            if not snaps and r not in self.NO_SNAPSHOT_NEEDED
        )
        assert not missing, (
            f"these POST endpoints change state but take no undo snapshot: "
            f"{missing}. Either call auction_state.save_snapshot() (or "
            f"capture_snapshot/commit_snapshot if the endpoint can reject), or "
            f"add the route to NO_SNAPSHOT_NEEDED with the reason it needs none."
        )

    def test_the_allow_list_has_no_stale_entries(self):
        """A route that starts snapshotting must leave the list.

        An exemption nobody removes reads as a decision that was made, and the
        next person adding an endpoint copies it.
        """
        routes = self._post_routes()
        gone = sorted(set(self.NO_SNAPSHOT_NEEDED) - set(routes))
        assert not gone, f"NO_SNAPSHOT_NEEDED names routes that no longer exist: {gone}"
        redundant = sorted(r for r in self.NO_SNAPSHOT_NEEDED if routes.get(r))
        assert not redundant, (
            f"these are exempted but do snapshot; drop them from the list: {redundant}"
        )

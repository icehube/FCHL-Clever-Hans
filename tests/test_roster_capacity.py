"""The 24-man active roster is a hard cap; extras belong in the minors.

CLAUDE.md has carried the rule since the first commit — "24 active … Teams can
draft beyond 24, extras go to minors with salary fully on cap" — but nothing
enforced it: `ROSTER_SIZE` appeared once, in `total_spots_remaining`, and was
only ever subtracted from.

The damage is not to bid advice (at 24 the MILP already says "no room, don't
bid", and at 25 it says the same thing for a different reason). It is to
`lineup_points`, which picks the best 12F/6D/2G off the ACTIVE roster: a 25th
active player competes for a starting slot he cannot legally hold. Measured, one
120-point forward on an otherwise-full roster added 70 phantom points — and that
number is what `evaluate_trade` accepts or declines on.
"""

import pytest
from fastapi.testclient import TestClient

from config import MY_TEAM, ROSTER_SIZE, SALARY_CAP
from state import PlayerOnRoster


@pytest.fixture
def client():
    """Function-scoped, unlike test_endpoints.py's module-scoped fixture.

    Every test here deliberately mutates a roster to capacity; sharing one
    reset across the file would leak a 24-man roster into whatever ran next.
    """
    from main import app

    with TestClient(app) as c:
        c.post("/reset")
        yield c
        c.post("/reset")


def _fill_active_roster(team, count: int = ROSTER_SIZE, points: int = 50) -> None:
    """Seat `count` players on the ACTIVE roster, bypassing add_acquired_player.

    Assigning keeper_players directly is deliberate: add_acquired_player is the
    code under test in most of this file, so building the fixture with it would
    make the tests agree with themselves. Same trick as
    tests/test_nomination.py::TestBiddingOpponents.
    """
    roster = []
    for i in range(count):
        # 12F/6D/2G first so position minimums are met, then bench-grade filler.
        pos = "F" if i < 12 else "D" if i < 18 else "G" if i < 20 else "F"
        roster.append(PlayerOnRoster(
            name=f"FILL{i}", position=pos, group="3",
            salary=0.5, projected_points=points,
        ))
    team.keeper_players = roster
    team.acquired_players = []
    team.minor_players = []
    team.penalties = 0.0
    team._invalidate_cache()


def _some_available(state) -> str:
    return next(iter(state.available_players))


class TestAssignAtCapacity:
    def test_assign_to_full_roster_lands_in_minors(self, client):
        import main

        team = main.auction_state.teams["SRL"]
        _fill_active_roster(team)
        name = _some_available(main.auction_state)

        r = client.post("/assign", data={"player": name, "team": "SRL", "salary": "2.0"})

        assert r.status_code == 200
        assert team.roster_count == ROSTER_SIZE, "active roster must not grow past 24"
        assert any(p.name == name for p in team.minor_players), (
            f"{name} should have been routed to the minors"
        )
        assert name not in main.auction_state.available_players, "the sale still happened"

    def test_operator_is_told_the_player_went_down(self, client):
        """Silent routing during a live draft is how you end up with a roster
        you did not intend."""
        import main

        team = main.auction_state.teams["SRL"]
        _fill_active_roster(team)
        name = _some_available(main.auction_state)

        r = client.post("/assign", data={"player": name, "team": "SRL", "salary": "2.0"})

        assert "minors" in r.headers.get("HX-Trigger", "").lower(), (
            f"toast must say where the player went, got: {r.headers.get('HX-Trigger')}"
        )

    def test_full_roster_salary_still_counts_on_cap(self, client):
        """The owner decision rests on this: every biddable player is group 3,
        so a minors salary is a full cap hit. If group handling ever changes,
        auto-routing silently starts freeing cap and this fails loudly."""
        import main

        team = main.auction_state.teams["SRL"]
        _fill_active_roster(team)
        before = team.total_salary
        name = _some_available(main.auction_state)

        client.post("/assign", data={"player": name, "team": "SRL", "salary": "2.0"})

        assert team.total_salary == pytest.approx(before + 2.0), (
            "a minors player drafted in the auction is a full cap hit"
        )

    def test_illegal_25th_cannot_inflate_lineup_points(self, client):
        """The reason this work exists.

        lineup_points takes the best 12F/6D/2G off the ACTIVE roster. Before the
        guard, a 25th active player displaced a starter he was not eligible to
        displace — measured at +70 points for one 120-point forward — and that
        number is what evaluate_trade decides on.
        """
        import main

        team = main.auction_state.teams["SRL"]
        _fill_active_roster(team, points=50)
        legal_points = team.current_roster_points

        stud = max(main.auction_state.available_players.values(),
                   key=lambda p: p.projected_points if p.position == "F" else -1)
        assert stud.projected_points > 50, "fixture needs a player who WOULD start"

        client.post("/assign", data={"player": stud.name, "team": "SRL", "salary": "2.0"})

        assert team.current_roster_points == legal_points, (
            f"{stud.name} ({stud.projected_points}pts) entered the starting lineup "
            f"from a full roster: {legal_points} -> {team.current_roster_points}"
        )

    def test_milp_stays_optimal_at_capacity(self, client):
        """spots never goes negative through an endpoint, so the Infeasible
        branch (optimizer.py) and its warning badge stop firing on a legal draft."""
        import main
        from optimizer import solve_optimal_roster

        bot = main.auction_state.teams[MY_TEAM]
        _fill_active_roster(bot)
        name = _some_available(main.auction_state)

        client.post("/assign", data={"player": name, "team": MY_TEAM, "salary": "2.0"})

        assert bot.total_spots_remaining == 0, "must be 0, never negative"
        sol = solve_optimal_roster(
            bot, main.auction_state.available_players, main.market_prices,
        )
        assert sol.status == "Optimal"


class TestRecallRefusedAtCapacity:
    """Recall is the one move that cannot auto-route — recalling INTO a full
    roster is the illegal act itself, so it has to be refused."""

    def test_recall_into_full_roster_raises(self):
        from data_loader import build_initial_state

        state = build_initial_state()
        team = state.teams["SRL"]
        _fill_active_roster(team)
        team.minor_players = [PlayerOnRoster(
            name="DOWNSTAIRS", position="F", group="3",
            salary=1.0, projected_points=60, is_minor=True, is_bench=True,
        )]
        team._invalidate_cache()

        with pytest.raises(ValueError, match="full"):
            team.recall_from_minors("DOWNSTAIRS")

        assert team.roster_count == ROSTER_SIZE
        assert [p.name for p in team.minor_players] == ["DOWNSTAIRS"], (
            "a refused recall must leave the player exactly where they were"
        )

    def test_endpoint_reports_the_capacity_reason(self, client):
        """/move-to-roster hardcoded "not in minors" for every ValueError, which
        would now be an actively wrong explanation for a real capacity refusal."""
        import main

        team = main.auction_state.teams["SRL"]
        _fill_active_roster(team)
        team.minor_players = [PlayerOnRoster(
            name="DOWNSTAIRS", position="F", group="3",
            salary=1.0, projected_points=60, is_minor=True, is_bench=True,
        )]
        team._invalidate_cache()

        r = client.post("/move-to-roster",
                        data={"team_code": "SRL", "player_name": "DOWNSTAIRS"})

        trigger = r.headers.get("HX-Trigger", "")
        assert "not in minors" not in trigger, (
            f"misleading reason — the player IS in the minors. Got: {trigger}"
        )
        assert "full" in trigger.lower(), f"should name the real reason, got: {trigger}"


class TestTradeOrdering:
    """Auto-routing makes trade ordering load-bearing: if a full team GAINS
    before it LOSES, the incoming player hits a 24-man roster and is wrongly
    sent to the minors, even though the trade nets to no change in size."""

    def test_one_for_one_with_a_full_team_stays_on_the_active_roster(self, client):
        import main

        ta = main.auction_state.teams["SRL"]
        tb = main.auction_state.teams["MAC"]
        _fill_active_roster(ta)
        _fill_active_roster(tb)
        out_a, out_b = "FILL0", "FILL1"

        r = client.post("/trade-between", data={
            "team_a": "SRL", "team_b": "MAC",
            "players_from_a": out_a, "players_from_b": out_b,
        })
        assert r.status_code == 200

        for team, incoming in ((ta, out_b), (tb, out_a)):
            assert team.roster_count == ROSTER_SIZE, f"{team.code} changed size"
            assert any(p.name == incoming for p in team.acquired_players), (
                f"{incoming} was sent to {team.code}'s minors by a 1-for-1 trade "
                f"that left the roster the same size"
            )
            assert not any(p.name == incoming for p in team.minor_players)


class TestFullRosterTrades:
    """Routing to the minors changed what a trade is worth, and nothing covered it.

    A player received onto a full roster now lands in the minors and scores
    nothing, so a pure acquisition that used to look like an upgrade is correctly
    a decline. The guarantee worth pinning is that the PREVIEW cannot promise
    points the EXECUTION won't deliver — before this change the two could
    disagree, because evaluate seated the player where execute would not.
    """

    def _full_bot(self):
        from data_loader import build_initial_state
        from market import compute_market_ceiling, compute_market_price
        from price_model import load_model_params, predict_all_prices

        state = build_initial_state()
        _fill_active_roster(state.teams[MY_TEAM])
        preds = predict_all_prices(state.available_players, load_model_params())
        model = {n: p.expected_price for n, p in preds.items()}
        info = compute_market_ceiling(state.teams)
        mp = {n: compute_market_price(model[n], info) for n in model}
        return state, mp

    def _best_forward(self, state):
        return max((p for p in state.available_players.values() if p.position == "F"),
                   key=lambda p: p.projected_points)

    def test_pure_acquisition_onto_a_full_roster_is_declined(self):
        from trade import PlayerTrade, evaluate_trade

        state, mp = self._full_bot()
        stud = self._best_forward(state)
        assert stud.projected_points > 50, "must out-score every filler starter"

        result = evaluate_trade(
            state, give=[],
            receive=[PlayerTrade(name=stud.name, position=stud.position,
                                 salary=2.0, projected_points=stud.projected_points)],
            market_prices=mp,
        )

        assert result.recommendation == "decline", (
            f"{stud.name} ({stud.projected_points}pts) cannot start from the minors, "
            "so acquiring him onto a full roster buys nothing but cap"
        )

    def test_preview_points_match_what_execution_delivers(self):
        from copy import deepcopy

        from trade import PlayerTrade, evaluate_trade, execute_trade

        state, mp = self._full_bot()
        stud = self._best_forward(state)
        recv = [PlayerTrade(name=stud.name, position=stud.position,
                            salary=2.0, projected_points=stud.projected_points)]

        previewed = evaluate_trade(state, give=[], receive=recv,
                                   market_prices=mp).best_scenario.total_points

        live = deepcopy(state)
        execute_trade(live, give=[], receive=[PlayerTrade(
            name=stud.name, position=stud.position,
            salary=2.0, projected_points=stud.projected_points)])
        bot = live.teams[MY_TEAM]

        assert bot.roster_count == ROSTER_SIZE
        assert any(p.name == stud.name for p in bot.minor_players)
        assert bot.current_roster_points == previewed, (
            f"preview promised {previewed} lineup points, execution delivered "
            f"{bot.current_roster_points}"
        )

    def test_received_player_still_costs_full_cap(self):
        from copy import deepcopy

        from trade import PlayerTrade, execute_trade

        state, mp = self._full_bot()
        stud = self._best_forward(state)
        before = state.teams[MY_TEAM].total_salary

        live = deepcopy(state)
        execute_trade(live, give=[], receive=[PlayerTrade(
            name=stud.name, position=stud.position,
            salary=2.0, projected_points=stud.projected_points)])

        assert live.teams[MY_TEAM].total_salary == pytest.approx(before + 2.0), (
            "a group-3 player in the minors is a full cap hit — the trade costs "
            "real money for zero lineup points, which is why it declines"
        )


class TestSpotsDisplay:
    def test_spots_never_renders_negative(self, client):
        """A 25-man roster is no longer reachable through an endpoint, but a
        state file or fchl_teams.json written before this guard can still hold
        one. "-1" reads as a bug to the operator and there is no action it
        implies, so the display clamps."""
        import main

        team = main.auction_state.teams["SRL"]
        _fill_active_roster(team, count=ROSTER_SIZE + 1)
        assert team.total_spots_remaining == -1, "property stays signed for the MILP"

        r = client.get("/team-view/SRL")

        assert r.status_code == 200
        assert ">-1<" not in r.text.replace(" ", ""), "Spots must not render negative"

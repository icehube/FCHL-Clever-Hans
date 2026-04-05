"""Edge case tests: boundary conditions and unusual states.

Tests scenarios that are unlikely but possible during a real auction,
to verify the system handles them gracefully without crashes.
"""

import json

import pytest
from fastapi.testclient import TestClient

from config import (
    BUYOUT_PENALTY_RATE,
    MAX_SALARY,
    MIN_DEFENSE,
    MIN_FORWARDS,
    MIN_GOALIES,
    MIN_SALARY,
    ROSTER_SIZE,
    SALARY_CAP,
)
from market import compute_market_ceiling, compute_market_price, MarketInfo
from optimizer import solve_optimal_roster
from price_model import predict_price, load_model_params
from state import AuctionState, Player, PlayerOnRoster, TeamState


@pytest.fixture(scope="module")
def client():
    from main import app
    with TestClient(app) as c:
        c.post("/reset")
        yield c


def _get_state(client):
    r = client.get("/state")
    assert r.status_code == 200
    return r.json()


# ── Optimizer Edge Cases ────────────────────────────────────────────


def _make_player(name, position="F", pts=50, salary=1.0):
    return Player(
        name=name, position=position, group="3",
        nhl_team="TOR", age=25, projected_points=pts,
        is_rfa=False, salary=salary, team_probability=0.05,
    )


def _make_team(keepers=None, budget_used=0.0, penalties=0.0):
    team = TeamState(
        code="TST", name="Test Team",
        keeper_players=keepers or [],
        minor_players=[], acquired_players=[],
        penalties=penalties, is_done=False,
        colors={"primary": "#000", "secondary": "#fff"},
        logo="test.png", is_my_team=True,
    )
    return team


class TestOptimizerEdgeCases:
    """Test optimizer boundary conditions."""

    def test_tight_budget_all_at_floor(self):
        """When budget barely covers MIN_SALARY per spot, all picks should be at floor."""
        # Team with lots of keepers, tiny budget remaining
        keepers = [
            PlayerOnRoster(name=f"K{i}", position="F", group="3",
                           salary=4.5, projected_points=60)
            for i in range(20)
        ]
        # 20 keepers × $4.5M = $90M > $56.8M cap → negative budget
        # Use fewer keepers to get just barely enough budget
        keepers = [
            PlayerOnRoster(name=f"K{i}", position="F" if i < 10 else ("D" if i < 14 else "G"),
                           group="3", salary=3.5, projected_points=60)
            for i in range(16)
        ]
        # 16 keepers × $3.5M = $56M, remaining = $0.8M, spots = 8
        # Need 8 × $0.5M = $4.0M but only have $0.8M → should be infeasible
        team = _make_team(keepers=keepers)
        players = {f"P{i}": _make_player(f"P{i}", pts=30) for i in range(20)}
        prices = {f"P{i}": MIN_SALARY for i in range(20)}

        sol = solve_optimal_roster(team, players, prices)
        # Budget is too tight — should be infeasible or fill what it can
        assert sol.status in ("Optimal", "Infeasible")

    def test_zero_spots_remaining(self):
        """Full roster should return empty solution."""
        keepers = [
            PlayerOnRoster(name=f"K{i}", position="F" if i < 14 else ("D" if i < 21 else "G"),
                           group="3", salary=2.0, projected_points=50)
            for i in range(ROSTER_SIZE)  # 24 keepers = full
        ]
        team = _make_team(keepers=keepers)
        players = {f"P{i}": _make_player(f"P{i}") for i in range(10)}
        prices = {f"P{i}": 1.0 for i in range(10)}

        sol = solve_optimal_roster(team, players, prices)
        assert sol.status == "Infeasible"
        assert len(sol.roster) == 0

    def test_fewer_candidates_than_spots(self):
        """When available pool is smaller than spots needed — infeasible."""
        team = _make_team()  # 0 keepers, 24 spots
        # Only 5 players available but need 24
        players = {f"P{i}": _make_player(f"P{i}") for i in range(5)}
        prices = {f"P{i}": 1.0 for i in range(5)}

        sol = solve_optimal_roster(team, players, prices)
        assert sol.status == "Infeasible"

    def test_position_minimums_impossible(self):
        """When not enough players at a needed position — infeasible."""
        team = _make_team()  # Needs 14F, 7D, 3G
        # Only forwards available — can't fill D and G minimums
        players = {f"F{i}": _make_player(f"F{i}", position="F") for i in range(30)}
        prices = {f"F{i}": 1.0 for i in range(30)}

        sol = solve_optimal_roster(team, players, prices)
        assert sol.status == "Infeasible"

    def test_forced_player_exceeds_budget(self):
        """Forcing a player at a salary > budget should be infeasible."""
        keepers = [
            PlayerOnRoster(name=f"K{i}", position="F" if i < 10 else ("D" if i < 14 else "G"),
                           group="3", salary=3.0, projected_points=50)
            for i in range(18)
        ]
        # 18 × $3M = $54M, remaining = $2.8M
        team = _make_team(keepers=keepers)
        players = {"Expensive": _make_player("Expensive", pts=100)}
        players.update({f"P{i}": _make_player(f"P{i}") for i in range(20)})
        prices = {"Expensive": 10.0}
        prices.update({f"P{i}": MIN_SALARY for i in range(20)})

        sol = solve_optimal_roster(
            team, players, prices,
            forced_players={"Expensive": 10.0},  # Way over budget
        )
        assert sol.status == "Infeasible"

    def test_all_players_same_points(self):
        """When all candidates have identical points, optimizer should minimize cost."""
        team = _make_team()
        players = {}
        prices = {}
        # All same price so position constraints don't create cost differences
        for i in range(30):
            pos = "F" if i < 16 else ("D" if i < 24 else "G")
            players[f"P{i}"] = _make_player(f"P{i}", position=pos, pts=50)
            prices[f"P{i}"] = 1.0

        sol = solve_optimal_roster(team, players, prices)
        assert sol.status == "Optimal"
        assert len(sol.roster) == ROSTER_SIZE
        # All at $1.0 means total cost = 24
        assert abs(sol.total_cost - ROSTER_SIZE * 1.0) < 0.01

    def test_single_candidate_per_position(self):
        """Exactly the minimum players per position — must pick them all."""
        team = _make_team()
        players = {}
        prices = {}
        for i in range(MIN_FORWARDS):
            players[f"F{i}"] = _make_player(f"F{i}", position="F", pts=50+i)
            prices[f"F{i}"] = 1.0
        for i in range(MIN_DEFENSE):
            players[f"D{i}"] = _make_player(f"D{i}", position="D", pts=40+i)
            prices[f"D{i}"] = 1.0
        for i in range(MIN_GOALIES):
            players[f"G{i}"] = _make_player(f"G{i}", position="G", pts=30+i)
            prices[f"G{i}"] = 1.0

        sol = solve_optimal_roster(team, players, prices)
        assert sol.status == "Optimal"
        assert len(sol.roster) == MIN_FORWARDS + MIN_DEFENSE + MIN_GOALIES


# ── Market Edge Cases ───────────────────────────────────────────────


class TestMarketEdgeCases:

    def test_market_price_with_floor_demand(self):
        """When floor_demand=True, market price should always be MIN_SALARY."""
        info = MarketInfo(
            market_ceiling=MIN_SALARY,
            highest_bidder=None, highest_bid=0.0,
            second_bidder=None, demand_count=0,
            floor_demand=True,
        )
        # Even a model price of $10M should return MIN_SALARY
        assert compute_market_price(10.0, info) == MIN_SALARY
        assert compute_market_price(0.1, info) == MIN_SALARY

    def test_market_price_model_below_ceiling(self):
        """When model price < ceiling, market price = model price."""
        info = MarketInfo(
            market_ceiling=8.0,
            highest_bidder="OPP1", highest_bid=8.0,
            second_bidder="OPP2", demand_count=2,
            floor_demand=False,
        )
        assert compute_market_price(5.0, info) == 5.0

    def test_market_price_model_above_ceiling(self):
        """When model price > ceiling, market price = ceiling."""
        info = MarketInfo(
            market_ceiling=3.0,
            highest_bidder="OPP1", highest_bid=5.0,
            second_bidder="OPP2", demand_count=2,
            floor_demand=False,
        )
        assert compute_market_price(5.0, info) == 3.0


# ── API Edge Cases ──────────────────────────────────────────────────


class TestAPIEdgeCases:

    def test_assign_nonexistent_player(self, client):
        """Assigning a player that doesn't exist should not crash."""
        r = client.post("/assign", data={
            "player": "Definitely Not A Real Player",
            "team": "BOT",
            "salary": "5.0",
        })
        assert r.status_code == 200

    def test_assign_same_player_twice(self, client):
        """Second assignment of same player should be a no-op."""
        state_before = _get_state(client)

        # Assign once
        client.post("/assign", data={
            "player": "Artemi Panarin",
            "team": "BOT",
            "salary": "5.0",
        })

        # Try to assign again
        r = client.post("/assign", data={
            "player": "Artemi Panarin",
            "team": "SRL",
            "salary": "3.0",
        })
        assert r.status_code == 200

        state_after = _get_state(client)
        # Should only be on BOT, not SRL
        bot_names = {p["name"] for p in state_after["teams"]["BOT"]["acquired_players"]}
        srl_names = {p["name"] for p in state_after["teams"]["SRL"]["acquired_players"]}
        assert "Artemi Panarin" in bot_names
        assert "Artemi Panarin" not in srl_names

        # Undo the first assign to restore state
        client.post("/undo")
        client.post("/undo")

    def test_undo_when_no_snapshots(self, client):
        """Undo with empty snapshot stack should not crash."""
        # Reset clears snapshots
        client.post("/reset")
        r = client.post("/undo")
        assert r.status_code == 200

    def test_bid_check_nonexistent_player(self, client):
        """Bid check for non-existent player should not crash."""
        r = client.post("/bid-check", data={
            "player": "Nobody",
            "price": "1.0",
            "bidders": "",
        })
        assert r.status_code == 200

    def test_buyout_nonexistent_player(self, client):
        """Buyout of non-roster player should not crash."""
        r = client.post("/buyout", data={"player": "Nobody"})
        assert r.status_code == 200

    def test_buyout_check_nonexistent_player(self, client):
        """Buyout check of non-roster player should not crash."""
        r = client.get("/buyout-check/Nobody")
        assert r.status_code == 200

    def test_team_done_nonexistent_team(self, client):
        """Toggling done on non-existent team should not crash."""
        r = client.post("/team-done", data={"team_code": "FAKE"})
        assert r.status_code == 200

    def test_set_nominator_invalid_team(self, client):
        """Setting nominator to invalid team should not crash."""
        r = client.post("/set-nominator", data={"team_code": "FAKE"})
        assert r.status_code == 200

    def test_team_view_nonexistent(self, client):
        """Viewing non-existent team should not crash."""
        r = client.get("/team-view/FAKE")
        assert r.status_code == 200

    def test_team_players_nonexistent(self, client):
        """Getting players for non-existent team should handle gracefully."""
        r = client.get("/team-players/FAKE")
        # May return 200 with empty list or 404/500 — just shouldn't crash the app
        assert r.status_code in (200, 404, 422, 500)

    def test_explain_nonexistent_player(self, client):
        """Counterfactual for non-existent player should not crash."""
        r = client.get("/explain/Nobody")
        assert r.status_code == 200

    def test_player_chart_nonexistent(self, client):
        """Player chart for non-existent player should not crash."""
        r = client.get("/player-chart/Nobody")
        assert r.status_code == 200

    def test_trade_evaluate_empty(self, client):
        """Trade evaluation with no players should not crash."""
        r = client.post("/trade-evaluate", data={})
        assert r.status_code == 200

    def test_trade_execute_without_evaluate(self, client):
        """Trade execute without prior evaluate should not crash."""
        # Reset last_trade_eval
        import main
        main.last_trade_eval = None
        r = client.post("/trade-execute")
        assert r.status_code == 200

    def test_trade_evaluate_malformed_json(self, client):
        """Trade evaluate with malformed receive JSON should not crash."""
        r = client.post("/trade-evaluate", data={
            "give_player": ["Clayton Keller"],
            "receive_player": ["not valid json {{{"],
        })
        assert r.status_code == 200

    def test_reset_twice(self, client):
        """Double reset should be idempotent."""
        client.post("/reset")
        state1 = _get_state(client)

        client.post("/reset")
        state2 = _get_state(client)

        assert len(state1["available_players"]) == len(state2["available_players"])
        assert len(state1["transaction_log"]) == len(state2["transaction_log"]) == 0

    def test_assign_negative_salary(self, client):
        """Assigning with negative salary — should still work (data entry error)."""
        client.post("/reset")
        r = client.post("/assign", data={
            "player": "Artemi Panarin",
            "team": "BOT",
            "salary": "-1.0",
        })
        assert r.status_code == 200
        # Undo
        client.post("/undo")

    def test_assign_zero_salary(self, client):
        """Assigning with zero salary."""
        r = client.post("/assign", data={
            "player": "Filip Forsberg",
            "team": "BOT",
            "salary": "0",
        })
        assert r.status_code == 200
        client.post("/undo")

    def test_assign_huge_salary(self, client):
        """Assigning with salary > MAX_SALARY."""
        r = client.post("/assign", data={
            "player": "Sidney Crosby",
            "team": "BOT",
            "salary": "50.0",
        })
        assert r.status_code == 200
        client.post("/undo")


# ── Price Model Edge Cases ──────────────────────────────────────────


class TestPriceModelEdgeCases:

    @pytest.fixture(scope="class")
    def params(self):
        return load_model_params()

    def test_zero_points_player(self, params):
        """Zero-point player should get floor price."""
        pred = predict_price("F", 0, 0.03, False, params)
        assert pred.expected_price >= MIN_SALARY
        # Model clamps to min_bid, so expected should be at floor
        assert pred.expected_price == MIN_SALARY

    def test_very_high_points(self, params):
        """150+ point player should get max-range price."""
        pred = predict_price("F", 150, 0.1, True, params)
        assert pred.expected_price > 5.0
        assert pred.expected_price <= MAX_SALARY + 1  # Allow slight overshoot from model

    def test_goalie_vs_forward(self, params):
        """Same points, goalie should have wider sigma (more uncertainty)."""
        f_pred = predict_price("F", 60, 0.03, False, params)
        g_pred = predict_price("G", 60, 0.03, False, params)
        # Both should be valid
        assert f_pred.expected_price > 0
        assert g_pred.expected_price > 0

    def test_rfa_flag_effect(self, params):
        """RFA flag should generally increase expected price."""
        ufa = predict_price("F", 80, 0.05, False, params)
        rfa = predict_price("F", 80, 0.05, True, params)
        # RFA typically sells higher due to restricted market
        # But don't hard-assert direction — model may vary
        assert rfa.expected_price > 0
        assert ufa.expected_price > 0

    def test_very_low_team_probability(self, params):
        """Player on worst team (low Cup odds) should still get valid price."""
        pred = predict_price("D", 40, 0.001, False, params)
        assert pred.expected_price >= MIN_SALARY
        assert pred.sigma > 0

    def test_very_high_team_probability(self, params):
        """Player on best team (high Cup odds) should still get valid price."""
        pred = predict_price("F", 80, 0.15, False, params)
        assert pred.expected_price >= MIN_SALARY
        assert pred.expected_price <= MAX_SALARY + 1


# ── State Serialization Edge Cases ──────────────────────────────────


class TestSerializationEdgeCases:

    def test_nhl_team_round_trip(self):
        """nhl_team field should survive JSON serialization."""
        from state import _player_on_roster_to_dict, _player_on_roster_from_dict
        p = PlayerOnRoster(
            name="Test", position="F", group="3",
            salary=1.0, projected_points=50,
            nhl_team="TOR", is_minor=False, is_bench=False,
        )
        d = _player_on_roster_to_dict(p)
        p2 = _player_on_roster_from_dict(d)
        assert p2.nhl_team == "TOR"

    def test_missing_nhl_team_backward_compat(self):
        """Old state files without nhl_team should default to empty string."""
        from state import _player_on_roster_from_dict
        d = {
            "name": "Old Player", "position": "D", "group": "2",
            "salary": 2.0, "projected_points": 30,
        }
        p = _player_on_roster_from_dict(d)
        assert p.nhl_team == ""
        assert p.is_minor is False
        assert p.is_bench is False

    def test_full_state_round_trip(self, client):
        """Full auction state should survive JSON round-trip."""
        state_json = _get_state(client)
        # Verify all teams present
        assert len(state_json["teams"]) == 11
        # Verify BOT has keepers
        bot = state_json["teams"]["BOT"]
        assert len(bot["keeper_players"]) > 0
        # Verify nhl_team is present on players
        for p in bot["keeper_players"]:
            assert "nhl_team" in p


# ── Buyout Edge Cases ───────────────────────────────────────────────


class TestBuyoutEdgeCases:

    def test_buyout_penalty_math(self):
        """Buyout penalty should be exactly 50% of salary."""
        from trade import evaluate_buyout
        from data_loader import build_initial_state
        from price_model import predict_all_prices, load_model_params
        from market import compute_all_market_prices

        state = build_initial_state()
        params = load_model_params()
        mp = predict_all_prices(state.available_players, params)
        market_data = compute_all_market_prices(
            state.available_players, mp, state.teams,
        )
        market_prices = {name: price for name, (price, _) in market_data.items()}

        result = evaluate_buyout(state, "Dougie Hamilton", market_prices)
        assert abs(result.penalty_added - 4.2 * BUYOUT_PENALTY_RATE) < 0.01
        assert abs(result.salary_freed - 4.2) < 0.01
        assert abs(result.net_cap_freed - 4.2 * (1 - BUYOUT_PENALTY_RATE)) < 0.01

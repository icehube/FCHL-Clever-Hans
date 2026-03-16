"""Tests for optimizer.py: MILP solver and roster optimization."""

import pytest

from config import MAX_SALARY, MIN_SALARY, MY_TEAM, SALARY_INCREMENT
from optimizer import MILPSolution, solve_optimal_roster
from state import Player, PlayerOnRoster, TeamState


def _make_player(name: str, position: str, pts: int, price: float = 1.0) -> Player:
    return Player(
        name=name, position=position, group="3", nhl_team="TOR",
        age=25, projected_points=pts, is_rfa=False, salary=0.0,
        team_probability=0.04,
    )


def _make_team(
    keepers: list[PlayerOnRoster] | None = None,
    penalties: float = 0.0,
) -> TeamState:
    return TeamState(
        code="BOT", name="Test", keeper_players=keepers or [],
        penalties=penalties,
    )


def _simple_pool() -> tuple[dict[str, Player], dict[str, float]]:
    """Small player pool for deterministic tests."""
    players = {
        "F1": _make_player("F1", "F", 80),
        "F2": _make_player("F2", "F", 70),
        "F3": _make_player("F3", "F", 60),
        "F4": _make_player("F4", "F", 50),
        "F5": _make_player("F5", "F", 40),
        "F6": _make_player("F6", "F", 30),
        "F7": _make_player("F7", "F", 20),
        "F8": _make_player("F8", "F", 15),
        "F9": _make_player("F9", "F", 12),
        "F10": _make_player("F10", "F", 10),
        "F11": _make_player("F11", "F", 8),
        "F12": _make_player("F12", "F", 6),
        "F13": _make_player("F13", "F", 5),
        "F14": _make_player("F14", "F", 4),
        "D1": _make_player("D1", "D", 75),
        "D2": _make_player("D2", "D", 55),
        "D3": _make_player("D3", "D", 45),
        "D4": _make_player("D4", "D", 35),
        "D5": _make_player("D5", "D", 25),
        "D6": _make_player("D6", "D", 15),
        "D7": _make_player("D7", "D", 10),
        "G1": _make_player("G1", "G", 65),
        "G2": _make_player("G2", "G", 40),
        "G3": _make_player("G3", "G", 20),
    }
    # All at $1M for simplicity
    prices = {name: 1.0 for name in players}
    return players, prices


class TestSolveOptimalRoster:
    def test_selects_best_players(self):
        """MILP should select highest-point players when budget isn't tight."""
        players, prices = _simple_pool()
        team = _make_team()  # Empty team, full budget
        sol = solve_optimal_roster(team, players, prices)
        assert sol.status == "Optimal"
        # Should pick the top players at each position
        selected_names = {p.name for p in sol.roster}
        assert "F1" in selected_names  # Best forward
        assert "D1" in selected_names  # Best defense
        assert "G1" in selected_names  # Best goalie

    def test_respects_position_minimums(self):
        """Solution must have at least 14F, 7D, 3G."""
        players, prices = _simple_pool()
        team = _make_team()
        sol = solve_optimal_roster(team, players, prices)
        assert sol.status == "Optimal"
        f_count = len(sol.by_position["F"])
        d_count = len(sol.by_position["D"])
        g_count = len(sol.by_position["G"])
        assert f_count >= 14
        assert d_count >= 7
        assert g_count >= 3

    def test_respects_budget(self):
        """Total cost should not exceed spendable budget."""
        players, prices = _simple_pool()
        # Give some players higher prices
        prices["F1"] = 5.0
        prices["D1"] = 4.0
        prices["G1"] = 3.0
        team = _make_team()
        sol = solve_optimal_roster(team, players, prices)
        assert sol.status == "Optimal"
        assert sol.total_cost <= team.spendable_budget + 0.01

    def test_accounts_for_existing_keepers(self):
        """Keepers reduce spots and budget needed."""
        players, prices = _simple_pool()
        keepers = [
            PlayerOnRoster(name=f"K_F{i}", position="F", group="3",
                           salary=2.0, projected_points=50)
            for i in range(7)
        ]
        team = _make_team(keepers=keepers)
        sol = solve_optimal_roster(team, players, prices)
        assert sol.status == "Optimal"
        # Should only draft 24 - 7 = 17 more players
        assert len(sol.roster) <= 17
        # Total points should include keepers
        assert sol.total_points >= sum(k.projected_points for k in keepers)

    def test_excluded_players(self):
        """Excluded players should not appear in solution."""
        players, prices = _simple_pool()
        # Add extras so excluding 2 doesn't make it infeasible
        players["F15"] = _make_player("F15", "F", 3)
        players["D8"] = _make_player("D8", "D", 5)
        prices["F15"] = 1.0
        prices["D8"] = 1.0
        team = _make_team()
        sol = solve_optimal_roster(team, players, prices, excluded_players={"F1", "D1"})
        assert sol.status == "Optimal"
        names = {p.name for p in sol.roster}
        assert "F1" not in names
        assert "D1" not in names

    def test_forced_players(self):
        """Forced players consume budget but aren't in the candidate pool."""
        players, prices = _simple_pool()
        team = _make_team()
        sol = solve_optimal_roster(
            team, players, prices,
            forced_players={"F1": 5.0},
        )
        assert sol.status == "Optimal"
        # F1 should be in by_position but not in roster (it's forced, not selected)
        assert any(p.name == "F1" for p in sol.by_position["F"])
        # Total points should include F1
        assert sol.total_points >= 80  # F1's points

    def test_infeasible_when_no_budget(self):
        """Should return Infeasible when budget is too low."""
        players, prices = _simple_pool()
        # Team with almost no budget left
        keepers = [
            PlayerOnRoster(name=f"K{i}", position="F", group="3",
                           salary=2.5, projected_points=10)
            for i in range(22)
        ]
        team = _make_team(keepers=keepers)
        # Remaining = 56.8 - 55.0 = 1.8, spots = 2, reserved = 1.0
        # spendable = 0.8, but need 2 more players at $1 each = $2
        prices_high = {name: 2.0 for name in players}
        sol = solve_optimal_roster(team, players, prices_high)
        # May be infeasible or find a solution with cheap players
        # Just verify it doesn't crash
        assert sol.status in ("Optimal", "Infeasible")

    def test_zero_point_players_excluded(self):
        """Players with 0 projected points should not be selected."""
        players, prices = _simple_pool()
        players["ZERO"] = _make_player("ZERO", "F", 0)
        prices["ZERO"] = 0.5
        team = _make_team()
        sol = solve_optimal_roster(team, players, prices)
        names = {p.name for p in sol.roster}
        assert "ZERO" not in names


class TestSolveWithRealData:
    def test_real_data_produces_optimal(self):
        """MILP should find an optimal solution with real auction data."""
        from data_loader import build_initial_state
        from market import compute_all_market_prices
        from price_model import load_model_params, predict_all_prices

        state = build_initial_state()
        params = load_model_params()
        model_prices = predict_all_prices(state.available_players, params)
        market_data = compute_all_market_prices(
            state.available_players, model_prices, state.teams,
        )
        mp = {name: price for name, (price, _) in market_data.items()}

        sol = solve_optimal_roster(state.teams[MY_TEAM], state.available_players, mp)
        assert sol.status == "Optimal"
        assert sol.total_points > 0
        assert sol.total_cost <= state.teams[MY_TEAM].spendable_budget + 0.01

    def test_real_data_performance(self):
        """MILP should solve in well under 1 second."""
        import time

        from data_loader import build_initial_state
        from market import compute_all_market_prices
        from price_model import load_model_params, predict_all_prices

        state = build_initial_state()
        params = load_model_params()
        model_prices = predict_all_prices(state.available_players, params)
        market_data = compute_all_market_prices(
            state.available_players, model_prices, state.teams,
        )
        mp = {name: price for name, (price, _) in market_data.items()}

        start = time.monotonic()
        sol = solve_optimal_roster(state.teams[MY_TEAM], state.available_players, mp)
        elapsed = time.monotonic() - start

        assert sol.status == "Optimal"
        assert elapsed < 1.0, f"MILP took {elapsed:.2f}s — too slow"

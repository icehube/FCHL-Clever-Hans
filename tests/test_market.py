"""Tests for market.py: market ceilings and adjusted prices."""

import pytest

from config import MAX_SALARY, MIN_SALARY, MY_TEAM
from market import (
    MarketInfo,
    compute_all_market_prices,
    compute_live_ceiling,
    compute_market_ceiling,
    compute_market_price,
    compute_opponent_ceiling,
)
from state import PlayerOnRoster, TeamState


def _make_team(
    code: str,
    keeper_salary: float = 0.0,
    num_keepers: int = 0,
    penalties: float = 0.0,
    is_done: bool = False,
    keeper_positions: dict[str, int] | None = None,
) -> TeamState:
    """Helper to create a team with specified budget characteristics."""
    keepers = []
    if keeper_positions:
        for pos, count in keeper_positions.items():
            for i in range(count):
                keepers.append(PlayerOnRoster(
                    name=f"{code}_{pos}{i}",
                    position=pos,
                    group="3",
                    salary=keeper_salary / max(num_keepers, 1) if num_keepers else 0,
                    projected_points=50,
                ))
    else:
        per_salary = keeper_salary / max(num_keepers, 1) if num_keepers else 0
        for i in range(num_keepers):
            keepers.append(PlayerOnRoster(
                name=f"{code}_P{i}",
                position="F",
                group="3",
                salary=per_salary,
                projected_points=50,
            ))
    return TeamState(
        code=code,
        name=f"Team {code}",
        keeper_players=keepers,
        penalties=penalties,
        is_done=is_done,
    )


class TestComputeOpponentCeiling:
    def test_basic_ceiling(self):
        """Active team with budget should return physical_max."""
        team = _make_team("OPP", keeper_salary=30.0, num_keepers=12,
                          keeper_positions={"F": 7, "D": 3, "G": 2})
        ceiling = compute_opponent_ceiling(team)
        assert ceiling is not None
        assert ceiling == team.physical_max_bid

    def test_done_team_returns_none(self):
        """Done teams are excluded."""
        team = _make_team("OPP", keeper_salary=30.0, num_keepers=12,
                          is_done=True,
                          keeper_positions={"F": 7, "D": 3, "G": 2})
        assert compute_opponent_ceiling(team) is None

    def test_team_can_bid_any_position(self):
        """Team with 14F can still bid on forwards (extras go to bench/minors)."""
        team = _make_team("OPP", keeper_salary=14.0, num_keepers=14,
                          keeper_positions={"F": 14})
        ceiling = compute_opponent_ceiling(team)
        assert ceiling is not None  # Can still bid

    def test_ceiling_capped_at_max_salary(self):
        """Physical max should be capped at MAX_SALARY."""
        team = _make_team("OPP", keeper_salary=5.0, num_keepers=1,
                          keeper_positions={"F": 1})
        ceiling = compute_opponent_ceiling(team)
        assert ceiling is not None
        assert ceiling == MAX_SALARY

    def test_tight_budget_ceiling(self):
        """Team with tight budget has low ceiling."""
        # 22 keepers totaling $55.0, remaining=$1.8
        # spots=2, physical_max = remaining - (spots-1)*MIN = 1.8 - 0.5 = 1.3
        team = _make_team("OPP", keeper_salary=55.0, num_keepers=22,
                          keeper_positions={"F": 14, "D": 5, "G": 3})
        ceiling = compute_opponent_ceiling(team)
        assert ceiling is not None
        assert ceiling == pytest.approx(1.3)


class TestComputeMarketCeiling:
    def _make_league(self, **overrides) -> dict[str, TeamState]:
        """Create a basic league with 3 opponent teams + BOT."""
        teams = {
            MY_TEAM: _make_team(MY_TEAM, keeper_salary=28.0, num_keepers=12,
                                keeper_positions={"F": 7, "D": 3, "G": 2}),
            "OPP1": _make_team("OPP1", keeper_salary=20.0, num_keepers=10,
                               keeper_positions={"F": 5, "D": 3, "G": 2}),
            "OPP2": _make_team("OPP2", keeper_salary=30.0, num_keepers=10,
                               keeper_positions={"F": 5, "D": 3, "G": 2}),
            "OPP3": _make_team("OPP3", keeper_salary=40.0, num_keepers=10,
                               keeper_positions={"F": 5, "D": 3, "G": 2}),
        }
        teams.update(overrides)
        return teams

    def test_second_highest_is_ceiling(self):
        """Market ceiling should be the second-highest physical_max."""
        teams = self._make_league()
        info = compute_market_ceiling(teams)
        # OPP1 has most budget, OPP2 second, OPP3 least
        opp1_max = teams["OPP1"].physical_max_bid
        opp2_max = teams["OPP2"].physical_max_bid
        assert info.market_ceiling == opp2_max
        assert info.highest_bid == opp1_max

    def test_excludes_my_team(self):
        """BOT should be excluded from market ceiling calculation."""
        teams = self._make_league()
        info = compute_market_ceiling(teams)
        assert info.highest_bidder != MY_TEAM
        if info.second_bidder:
            assert info.second_bidder != MY_TEAM

    def test_done_team_excluded(self):
        """Teams marked as done should be excluded."""
        teams = self._make_league()
        teams["OPP1"].is_done = True  # Richest opponent done
        info = compute_market_ceiling(teams)
        assert info.highest_bidder != "OPP1"

    def test_all_done_gives_floor(self):
        """If all opponents are done, floor_demand is True."""
        teams = self._make_league()
        for code in ["OPP1", "OPP2", "OPP3"]:
            teams[code].is_done = True
        info = compute_market_ceiling(teams)
        assert info.floor_demand is True
        assert info.market_ceiling == MIN_SALARY
        assert info.demand_count == 0

    def test_single_opponent_is_ceiling(self):
        """With only one active opponent, they set the ceiling."""
        teams = self._make_league()
        teams["OPP2"].is_done = True
        teams["OPP3"].is_done = True
        info = compute_market_ceiling(teams)
        assert info.demand_count == 1
        assert info.market_ceiling == teams["OPP1"].physical_max_bid

    def test_demand_count_accurate(self):
        """Demand count should reflect all active opponents."""
        teams = self._make_league()
        info = compute_market_ceiling(teams)
        assert info.demand_count == 3  # All 3 opponents active


class TestComputeMarketPrice:
    def test_model_below_ceiling(self):
        """When model price < ceiling, market price = model price."""
        info = MarketInfo(
            market_ceiling=8.0, highest_bidder="A", highest_bid=10.0,
            second_bidder="B", demand_count=3, floor_demand=False,
        )
        assert compute_market_price(5.0, info) == 5.0

    def test_model_above_ceiling(self):
        """When model price > ceiling, market price = ceiling."""
        info = MarketInfo(
            market_ceiling=3.0, highest_bidder="A", highest_bid=5.0,
            second_bidder="B", demand_count=2, floor_demand=False,
        )
        assert compute_market_price(5.0, info) == 3.0

    def test_floor_demand_gives_min(self):
        """When no demand, market price = MIN_SALARY."""
        info = MarketInfo(
            market_ceiling=MIN_SALARY, highest_bidder=None, highest_bid=0.0,
            second_bidder=None, demand_count=0, floor_demand=True,
        )
        assert compute_market_price(5.0, info) == MIN_SALARY

    def test_model_equals_ceiling(self):
        """When model price == ceiling, market price = ceiling."""
        info = MarketInfo(
            market_ceiling=5.0, highest_bidder="A", highest_bid=7.0,
            second_bidder="B", demand_count=2, floor_demand=False,
        )
        assert compute_market_price(5.0, info) == 5.0


class TestComputeLiveCeiling:
    def test_second_highest_active_bidder(self):
        """Live ceiling uses second-highest of active bidders only."""
        teams = {
            "A": _make_team("A", keeper_salary=10.0, num_keepers=5,
                            keeper_positions={"F": 5}),
            "B": _make_team("B", keeper_salary=30.0, num_keepers=5,
                            keeper_positions={"F": 5}),
            "C": _make_team("C", keeper_salary=20.0, num_keepers=5,
                            keeper_positions={"F": 5}),
        }
        ceiling = compute_live_ceiling(["A", "B", "C"], teams)
        # A has most budget, C second, B least
        assert ceiling == teams["C"].physical_max_bid

    def test_single_active_bidder(self):
        """With one bidder, their max is the ceiling."""
        teams = {
            "A": _make_team("A", keeper_salary=10.0, num_keepers=5,
                            keeper_positions={"F": 5}),
        }
        ceiling = compute_live_ceiling(["A"], teams)
        assert ceiling == teams["A"].physical_max_bid

    def test_no_active_bidders(self):
        """With no valid bidders, ceiling is MIN_SALARY."""
        ceiling = compute_live_ceiling([], {})
        assert ceiling == MIN_SALARY

    def test_done_bidder_excluded(self):
        """Done teams in active bidder list are excluded."""
        teams = {
            "A": _make_team("A", keeper_salary=10.0, num_keepers=5, is_done=True,
                            keeper_positions={"F": 5}),
            "B": _make_team("B", keeper_salary=20.0, num_keepers=5,
                            keeper_positions={"F": 5}),
        }
        ceiling = compute_live_ceiling(["A", "B"], teams)
        assert ceiling == teams["B"].physical_max_bid


class TestComputeAllMarketPrices:
    def test_returns_all_players(self):
        """Should return a result for every player."""
        from data_loader import build_initial_state
        from price_model import load_model_params, predict_all_prices

        state = build_initial_state()
        params = load_model_params()
        model_prices = predict_all_prices(state.available_players, params)
        market_prices = compute_all_market_prices(
            state.available_players, model_prices, state.teams,
        )
        assert len(market_prices) == len(state.available_players)

    def test_market_price_never_exceeds_ceiling(self):
        """Market price should never exceed the market ceiling."""
        from data_loader import build_initial_state
        from price_model import load_model_params, predict_all_prices

        state = build_initial_state()
        params = load_model_params()
        model_prices = predict_all_prices(state.available_players, params)
        market_prices = compute_all_market_prices(
            state.available_players, model_prices, state.teams,
        )
        for name, (price, info) in market_prices.items():
            if not info.floor_demand:
                assert price <= info.market_ceiling + 0.001, \
                    f"{name}: market price {price} > ceiling {info.market_ceiling}"

    def test_market_price_at_least_min(self):
        """Market price should always be at least MIN_SALARY."""
        from data_loader import build_initial_state
        from price_model import load_model_params, predict_all_prices

        state = build_initial_state()
        params = load_model_params()
        model_prices = predict_all_prices(state.available_players, params)
        market_prices = compute_all_market_prices(
            state.available_players, model_prices, state.teams,
        )
        for name, (price, _) in market_prices.items():
            assert price >= MIN_SALARY, f"{name}: market price {price} < MIN_SALARY"

    def test_done_team_changes_ceiling(self):
        """Marking a team as done should change market ceilings."""
        # Use synthetic teams where budgets differ enough to matter
        teams = {
            MY_TEAM: _make_team(MY_TEAM, keeper_salary=28.0, num_keepers=12,
                                keeper_positions={"F": 7, "D": 3, "G": 2}),
            "RICH": _make_team("RICH", keeper_salary=10.0, num_keepers=5,
                               keeper_positions={"F": 3, "D": 1, "G": 1}),
            "MID": _make_team("MID", keeper_salary=40.0, num_keepers=15,
                              keeper_positions={"F": 8, "D": 4, "G": 3}),
            "POOR": _make_team("POOR", keeper_salary=50.0, num_keepers=20,
                               keeper_positions={"F": 12, "D": 5, "G": 3}),
        }
        info_before = compute_market_ceiling(teams)
        assert info_before.highest_bidder == "RICH"

        # Mark RICH as done
        teams["RICH"].is_done = True
        info_after = compute_market_ceiling(teams)

        # Ceiling should drop since RICH is gone
        assert info_after.market_ceiling < info_before.market_ceiling or \
               info_after.highest_bidder != "RICH"

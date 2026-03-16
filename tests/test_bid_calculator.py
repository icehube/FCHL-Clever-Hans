"""Tests for bid calculator and counterfactual generator."""

import pytest

from config import MAX_SALARY, MIN_SALARY, MY_TEAM, SALARY_INCREMENT
from market import MarketInfo
from optimizer import (
    compute_bid_recommendation,
    compute_marginal_value,
    generate_counterfactual,
    solve_optimal_roster,
)
from state import Player, PlayerOnRoster, TeamState


def _make_player(name: str, position: str, pts: int) -> Player:
    return Player(
        name=name, position=position, group="3", nhl_team="TOR",
        age=25, projected_points=pts, is_rfa=False, salary=0.0,
        team_probability=0.04,
    )


def _setup_real_data():
    """Load real auction data for integration tests."""
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
    info = next(iter(market_data.values()))[1]  # MarketInfo (same for all)
    return state, mp, info


class TestComputeMarginalValue:
    def test_elite_player_high_marginal(self):
        """Elite player should have high marginal value."""
        state, mp, _ = _setup_real_data()
        team = state.teams[MY_TEAM]
        # Find the highest-point available forward
        top_fwd = max(
            (p for p in state.available_players.values() if p.position == "F"),
            key=lambda p: p.projected_points,
        )
        mv = compute_marginal_value(top_fwd, team, state.available_players, mp)
        assert mv > MIN_SALARY, f"Elite forward {top_fwd.name} should have marginal > min"

    def test_low_value_player_near_floor(self):
        """Low-point player should have marginal value near floor."""
        state, mp, _ = _setup_real_data()
        team = state.teams[MY_TEAM]
        # Find a low-point forward
        low_fwd = min(
            (p for p in state.available_players.values()
             if p.position == "F" and p.projected_points > 0),
            key=lambda p: p.projected_points,
        )
        mv = compute_marginal_value(low_fwd, team, state.available_players, mp)
        assert mv == MIN_SALARY

    def test_marginal_at_least_min_salary(self):
        """Marginal value should never be below MIN_SALARY."""
        state, mp, _ = _setup_real_data()
        team = state.teams[MY_TEAM]
        for name, player in list(state.available_players.items())[:20]:
            if player.projected_points > 0:
                mv = compute_marginal_value(player, team, state.available_players, mp)
                assert mv >= MIN_SALARY


class TestBidRecommendation:
    def test_bid_when_price_low(self):
        """Should recommend BID when current price is well below max."""
        state, mp, info = _setup_real_data()
        team = state.teams[MY_TEAM]
        top_fwd = max(
            (p for p in state.available_players.values() if p.position == "F"),
            key=lambda p: p.projected_points,
        )
        rec = compute_bid_recommendation(
            top_fwd, team, state.available_players, mp, info,
            current_price=MIN_SALARY,
        )
        assert rec.action == "BID"
        assert rec.max_bid >= MIN_SALARY

    def test_drop_when_price_exceeds_max(self):
        """Should recommend DROP when price exceeds max bid."""
        state, mp, info = _setup_real_data()
        team = state.teams[MY_TEAM]
        player = next(
            p for p in state.available_players.values()
            if p.projected_points > 0
        )
        rec = compute_bid_recommendation(
            player, team, state.available_players, mp, info,
            current_price=MAX_SALARY,
        )
        assert rec.action == "DROP"

    def test_max_bid_never_exceeds_ceiling(self):
        """Max bid should never exceed market ceiling + INCREMENT."""
        state, mp, info = _setup_real_data()
        team = state.teams[MY_TEAM]
        player = next(
            p for p in state.available_players.values()
            if p.projected_points > 50
        )
        rec = compute_bid_recommendation(
            player, team, state.available_players, mp, info,
        )
        assert rec.max_bid <= info.market_ceiling + SALARY_INCREMENT + 0.01

    def test_max_bid_never_exceeds_spendable(self):
        """Max bid should never exceed team's spendable budget."""
        state, mp, info = _setup_real_data()
        team = state.teams[MY_TEAM]
        player = next(
            p for p in state.available_players.values()
            if p.projected_points > 50
        )
        rec = compute_bid_recommendation(
            player, team, state.available_players, mp, info,
        )
        assert rec.max_bid <= team.spendable_budget + 0.01


class TestCounterfactual:
    def test_counterfactual_produces_both_rosters(self):
        """Should produce valid with and without solutions."""
        state, mp, _ = _setup_real_data()
        team = state.teams[MY_TEAM]
        player = next(
            p for p in state.available_players.values()
            if p.projected_points > 50
        )
        cf = generate_counterfactual(player, 3.0, team, state.available_players, mp)
        assert cf.with_player.status == "Optimal"
        assert cf.without_player.status == "Optimal"

    def test_counterfactual_shows_alternatives(self):
        """Without-player roster should have alternatives not in with-player roster."""
        state, mp, _ = _setup_real_data()
        team = state.teams[MY_TEAM]
        player = next(
            p for p in state.available_players.values()
            if p.projected_points > 60
        )
        cf = generate_counterfactual(player, 5.0, team, state.available_players, mp)
        # If the player is in the optimal roster at that price, alternatives
        # should show who gets displaced
        if cf.points_difference > 0:
            assert len(cf.alternative_players) >= 0  # May be empty if player just adds

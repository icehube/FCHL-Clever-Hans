"""Tests for price_model.py: two-stage log-normal price predictions."""

import math

import pytest

from price_model import PricePrediction, load_model_params, predict_all_prices, predict_price


@pytest.fixture
def params():
    return load_model_params()


class TestLoadModelParams:
    def test_loads_all_positions(self, params):
        assert "F" in params
        assert "D" in params
        assert "G" in params

    def test_has_metadata(self, params):
        assert "metadata" in params
        assert params["metadata"]["model_type"] == "two_stage_logistic_OLS_log_normal"

    def test_forward_has_all_coefficients(self, params):
        f = params["F"]
        for key in [
            "floor_intercept", "floor_coef_projected_points",
            "floor_coef_projected_points_sq", "floor_coef_team_probability",
            "floor_coef_is_rfa", "intercept", "coef_projected_points",
            "coef_projected_points_sq", "coef_team_probability", "coef_is_rfa",
            "residual_std", "sigma_intercept", "sigma_slope", "sigma_floor",
            "min_bid", "max_bid",
        ]:
            assert key in f, f"Missing key: {key}"


class TestPredictPrice:
    def test_high_points_forward_above_floor(self, params):
        """100-pt EDM forward should predict well above floor."""
        pred = predict_price("F", 100, 0.1104, False, params)
        assert pred.expected_price == pytest.approx(6.72, abs=0.1)
        assert pred.p_floor < 0.01

    def test_zero_points_clamps_to_min(self, params):
        """0-pt player expected price clamps to min_bid ($0.5)."""
        pred = predict_price("F", 0, 0.031, False, params)
        assert pred.expected_price == params["F"]["min_bid"]

    def test_rfa_increases_price(self, params):
        """RFA flag should increase predicted price (positive coef_is_rfa)."""
        ufa = predict_price("F", 100, 0.1104, False, params)
        rfa = predict_price("F", 100, 0.1104, True, params)
        assert rfa.expected_price > ufa.expected_price

    def test_rfa_decreases_p_floor(self, params):
        """RFA flag should decrease P(floor) — negative floor_coef_is_rfa."""
        # Use moderate points where p_floor is non-trivial
        ufa = predict_price("F", 30, 0.05, False, params)
        rfa = predict_price("F", 30, 0.05, True, params)
        assert rfa.p_floor < ufa.p_floor

    def test_goalie_sigma_wider_than_forward(self, params):
        """Goalie predictions should have wider sigma than forwards."""
        fwd = predict_price("F", 75, 0.05, False, params)
        goalie = predict_price("G", 75, 0.05, False, params)
        assert goalie.sigma > fwd.sigma

    def test_goalie_high_points_clamps_to_max(self, params):
        """High-sigma goalie prediction clamps to max_bid."""
        pred = predict_price("G", 75, 0.0974, False, params)
        assert pred.expected_price == params["G"]["max_bid"]

    def test_defense_prediction(self, params):
        """Defense prediction should be reasonable for a good player."""
        pred = predict_price("D", 80, 0.092, False, params)
        assert pred.expected_price > 1.0
        assert pred.expected_price <= params["D"]["max_bid"]

    def test_all_outputs_within_bounds(self, params):
        """All output fields should be within position bounds."""
        for pos in ["F", "D", "G"]:
            min_bid = params[pos]["min_bid"]
            max_bid = params[pos]["max_bid"]
            for pts in [0, 30, 60, 90, 120]:
                pred = predict_price(pos, pts, 0.05, False, params)
                assert pred.expected_price >= min_bid, f"{pos} {pts}pts expected below min"
                assert pred.expected_price <= max_bid, f"{pos} {pts}pts expected above max"
                assert pred.median_price >= min_bid
                assert pred.median_price <= max_bid
                assert pred.ci_low >= min_bid
                assert pred.ci_high <= max_bid
                assert pred.ci_low <= pred.ci_high

    def test_p_floor_between_0_and_1(self, params):
        """P(floor) should always be a valid probability."""
        for pos in ["F", "D", "G"]:
            for pts in [0, 50, 100]:
                pred = predict_price(pos, pts, 0.05, False, params)
                assert 0.0 <= pred.p_floor <= 1.0

    def test_sigma_at_least_sigma_floor(self, params):
        """Sigma should never go below sigma_floor."""
        for pos in ["F", "D", "G"]:
            sigma_floor = params[pos]["sigma_floor"]
            for pts in [0, 50, 100, 150]:
                pred = predict_price(pos, pts, 0.05, False, params)
                assert pred.sigma >= sigma_floor

    def test_higher_points_generally_higher_price(self, params):
        """More projected points should generally mean higher expected price."""
        low = predict_price("F", 40, 0.05, False, params)
        high = predict_price("F", 90, 0.05, False, params)
        assert high.expected_price > low.expected_price

    def test_prediction_dataclass_fields(self, params):
        """PricePrediction should have all expected fields."""
        pred = predict_price("F", 80, 0.05, False, params)
        assert hasattr(pred, "expected_price")
        assert hasattr(pred, "median_price")
        assert hasattr(pred, "p_floor")
        assert hasattr(pred, "sigma")
        assert hasattr(pred, "log_mu")
        assert hasattr(pred, "ci_low")
        assert hasattr(pred, "ci_high")


class TestPredictAllPrices:
    def test_predicts_for_all_players(self, params):
        """predict_all_prices should return one prediction per player."""
        from data_loader import load_players, load_team_odds
        odds = load_team_odds()
        _, biddable = load_players(team_odds=odds)
        predictions = predict_all_prices(biddable, params)
        assert len(predictions) == len(biddable)

    def test_all_predictions_valid(self, params):
        """Every prediction should have reasonable values."""
        from data_loader import load_players, load_team_odds
        odds = load_team_odds()
        _, biddable = load_players(team_odds=odds)
        predictions = predict_all_prices(biddable, params)
        for name, pred in predictions.items():
            assert pred.expected_price >= 0.5, f"{name} expected below min"
            assert pred.expected_price <= 11.4, f"{name} expected above global max"
            assert 0.0 <= pred.p_floor <= 1.0, f"{name} p_floor out of range"

"""Tests for price_model.py: two-stage log-normal price predictions.

The authoritative check is the golden-file test: the pricer notebook exports
auction_predictions_current.csv alongside model_params.json, and every one of
its 139 predictions must be reproduced from the params within rounding.
"""

import csv

import pytest

from price_model import (
    PricePrediction,
    compute_pos_ranks,
    load_model_params,
    predict_all_prices,
    predict_price,
)

GOLDEN_CSV = "tests/fixtures/auction_predictions_current.csv"


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
        assert params["metadata"]["goalie_pts_per_win"] > 0

    def test_all_positions_have_all_coefficients(self, params):
        features = [
            "projected_points", "projected_points_sq", "pts_hinge_60",
            "pts_hinge_80", "team_probability", "is_rfa", "log_rank",
            "log_lag", "has_lag", "proj_wins",
        ]
        for pos in ["F", "D", "G"]:
            p = params[pos]
            for feat in features:
                assert f"floor_coef_{feat}" in p, f"{pos} missing floor_coef_{feat}"
                assert f"coef_{feat}" in p, f"{pos} missing coef_{feat}"
            for key in [
                "floor_intercept", "intercept", "residual_std",
                "sigma_intercept", "sigma_slope", "sigma_floor",
                "min_bid", "max_bid",
            ]:
                assert key in p, f"{pos} missing {key}"


class TestGoldenPredictions:
    """Reproduce the notebook's exported predictions from the params JSON."""

    def test_matches_all_exported_predictions(self, params):
        with open(GOLDEN_CSV) as f:
            rows = list(csv.DictReader(f))
        assert len(rows) >= 100, "golden fixture looks truncated"

        for row in rows:
            pred = predict_price(
                position=row["position"],
                projected_points=float(row["projected_points"]),
                team_probability=float(row["team_probability"]),
                is_rfa=row["auction_type"] == "RFA",
                params=params,
                last_salary=float(row["last_salary"]) if row["last_salary"] else None,
                pos_rank=int(row["pos_rank"]),
                proj_wins=float(row["proj_wins"]) if row["proj_wins"] else None,
            )
            name = row["player_name"]
            # CSV rounds prices to 2dp and team_probability to 2dp on export
            assert pred.p_floor == pytest.approx(
                float(row["p_floor"]), abs=0.002), f"{name} p_floor"
            assert pred.expected_price == pytest.approx(
                float(row["predicted_expected"]), abs=0.02), f"{name} expected"
            assert pred.median_price == pytest.approx(
                float(row["predicted_median"]), abs=0.02), f"{name} median"
            assert pred.ci_low == pytest.approx(
                float(row["predicted_80_lower"]), abs=0.02), f"{name} ci_low"
            assert pred.ci_high == pytest.approx(
                float(row["predicted_80_upper"]), abs=0.02), f"{name} ci_high"


class TestPredictPrice:
    def test_star_forward_priced_high(self, params):
        """100-pt contender forward with a track record prices like a star."""
        pred = predict_price("F", 100, 11.0, False, params,
                             last_salary=8.0, pos_rank=3)
        assert pred.expected_price > 6.0
        assert pred.p_floor < 0.01

    def test_depth_player_near_floor(self, params):
        """Low-rank newcomer with modest points should sit near the floor."""
        pred = predict_price("F", 30, 3.0, False, params,
                             last_salary=None, pos_rank=150)
        assert pred.p_floor > 0.5
        assert pred.expected_price < 1.5

    def test_rfa_increases_price(self, params):
        ufa = predict_price("F", 100, 11.0, False, params, pos_rank=3)
        rfa = predict_price("F", 100, 11.0, True, params, pos_rank=3)
        assert rfa.expected_price > ufa.expected_price

    def test_lagged_salary_increases_price(self, params):
        """Reputation premium: returning $6M player beats a newcomer."""
        newcomer = predict_price("F", 70, 5.0, False, params, pos_rank=20)
        returning = predict_price("F", 70, 5.0, False, params,
                                  last_salary=6.0, pos_rank=20)
        assert returning.expected_price > newcomer.expected_price

    def test_worse_rank_decreases_price(self, params):
        """Scarcity: same points, deeper rank -> cheaper (F/D only)."""
        scarce = predict_price("F", 70, 5.0, False, params, pos_rank=5)
        deep = predict_price("F", 70, 5.0, False, params, pos_rank=60)
        assert scarce.expected_price > deep.expected_price

    def test_goalie_priced_on_wins(self, params):
        """More projected wins -> higher price at identical composite points."""
        low = predict_price("G", 70, 8.0, False, params,
                            last_salary=4.0, proj_wins=25.0)
        high = predict_price("G", 70, 8.0, False, params,
                             last_salary=4.0, proj_wins=38.0)
        assert high.expected_price > low.expected_price

    def test_goalie_wins_fallback(self, params):
        """proj_wins=None falls back to points / goalie_pts_per_win."""
        pts_per_win = params["metadata"]["goalie_pts_per_win"]
        implicit = predict_price("G", 70, 8.0, False, params, last_salary=4.0)
        explicit = predict_price("G", 70, 8.0, False, params,
                                 last_salary=4.0, proj_wins=70 / pts_per_win)
        assert implicit.expected_price == pytest.approx(explicit.expected_price)

    def test_forward_hinges_flatten_slope_past_80(self, params):
        """Deployed F fit is piecewise-linear with a damped slope after 80 pts."""
        f = params["F"]
        slope_to_60 = f["coef_projected_points"]
        slope_past_80 = (f["coef_projected_points"] + f["coef_pts_hinge_60"]
                         + f["coef_pts_hinge_80"])
        assert slope_past_80 < slope_to_60
        # And the deployed skater fits are piecewise-linear: pts^2 dropped
        assert f["coef_projected_points_sq"] == 0.0
        assert params["D"]["coef_projected_points_sq"] == 0.0

    def test_all_outputs_within_bounds(self, params):
        for pos in ["F", "D", "G"]:
            min_bid = params[pos]["min_bid"]
            max_bid = params[pos]["max_bid"]
            for pts in [0, 30, 60, 90, 120]:
                for last_salary in [None, 2.0]:
                    pred = predict_price(pos, pts, 5.0, False, params,
                                         last_salary=last_salary, pos_rank=10)
                    assert min_bid <= pred.expected_price <= max_bid
                    assert min_bid <= pred.median_price <= max_bid
                    assert min_bid <= pred.ci_low <= pred.ci_high <= max_bid

    def test_p_floor_between_0_and_1(self, params):
        for pos in ["F", "D", "G"]:
            for pts in [0, 50, 100]:
                pred = predict_price(pos, pts, 5.0, False, params, pos_rank=10)
                assert 0.0 <= pred.p_floor <= 1.0

    def test_sigma_at_least_sigma_floor(self, params):
        for pos in ["F", "D", "G"]:
            sigma_floor = params[pos]["sigma_floor"]
            for pts in [0, 50, 100, 150]:
                pred = predict_price(pos, pts, 5.0, False, params, pos_rank=10)
                assert pred.sigma >= sigma_floor

    def test_higher_points_generally_higher_price(self, params):
        low = predict_price("F", 40, 5.0, False, params, pos_rank=30)
        high = predict_price("F", 90, 5.0, False, params, pos_rank=30)
        assert high.expected_price > low.expected_price

    def test_prediction_dataclass_fields(self, params):
        pred = predict_price("F", 80, 5.0, False, params)
        for field in ["expected_price", "median_price", "p_floor",
                      "sigma", "log_mu", "ci_low", "ci_high"]:
            assert hasattr(pred, field)


class TestComputePosRanks:
    def test_ranks_within_position(self):
        from state import Player

        def mk(name, pos, pts):
            return Player(name=name, position=pos, group="3", nhl_team="BOS",
                          age=25, projected_points=pts, is_rfa=False,
                          salary=0.0, team_probability=5.0)

        players = {
            "f1": mk("f1", "F", 100), "f2": mk("f2", "F", 80),
            "f3": mk("f3", "F", 80), "f4": mk("f4", "F", 50),
            "d1": mk("d1", "D", 60),
        }
        ranks = compute_pos_ranks(players)
        assert ranks["f1"] == 1
        # ties=min: both 80-pt forwards share rank 2
        assert ranks["f2"] == 2
        assert ranks["f3"] == 2
        assert ranks["f4"] == 4
        # Ranks are per-position: the lone D is rank 1
        assert ranks["d1"] == 1


class TestPredictAllPrices:
    def test_predicts_for_all_players(self, params):
        from data_loader import load_goalie_wins, load_players, load_team_odds
        odds = load_team_odds()
        _, biddable = load_players(team_odds=odds, goalie_wins=load_goalie_wins())
        predictions = predict_all_prices(biddable, params)
        assert len(predictions) == len(biddable)

    def test_all_predictions_valid(self, params):
        from data_loader import load_goalie_wins, load_players, load_team_odds
        odds = load_team_odds()
        _, biddable = load_players(team_odds=odds, goalie_wins=load_goalie_wins())
        predictions = predict_all_prices(biddable, params)
        for name, pred in predictions.items():
            assert pred.expected_price >= 0.5, f"{name} expected below min"
            assert pred.expected_price <= 11.4, f"{name} expected above global max"
            assert 0.0 <= pred.p_floor <= 1.0, f"{name} p_floor out of range"

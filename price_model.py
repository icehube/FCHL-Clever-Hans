"""Two-stage per-position price prediction model (Layer 1)."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

from config import MIN_SALARY


@dataclass
class PricePrediction:
    """Full price prediction output for a single player."""

    expected_price: float  # E[salary] = p_floor * MIN + (1-p_floor) * mean_above
    median_price: float  # (1-p_floor) * exp(log_mu) + p_floor * MIN
    p_floor: float  # P(sells at floor)
    sigma: float  # Log-normal sigma for above-floor distribution
    log_mu: float  # Log-normal mu for above-floor distribution
    ci_low: float  # 10th percentile
    ci_high: float  # 90th percentile


def load_model_params(path: str = "data/model_params.json") -> dict:
    """Load per-position model coefficients."""
    with open(path) as f:
        return json.load(f)


def predict_price(
    position: str,
    projected_points: float,
    team_probability: float,
    is_rfa: bool,
    params: dict,
) -> PricePrediction:
    """
    Two-stage price prediction.

    Stage 1 (Logistic): P(player sells at floor)
    Stage 2 (Log-normal): salary distribution conditional on above-floor
    """
    pos_params = params[position]
    pts = projected_points
    pts_sq = pts * pts
    rfa = 1.0 if is_rfa else 0.0

    # Stage 1: P(floor) via logistic regression
    logit = (
        pos_params["floor_intercept"]
        + pos_params["floor_coef_projected_points"] * pts
        + pos_params["floor_coef_projected_points_sq"] * pts_sq
        + pos_params["floor_coef_team_probability"] * team_probability
        + pos_params["floor_coef_is_rfa"] * rfa
    )
    p_floor = _sigmoid(logit)

    # Stage 2: log-normal parameters for above-floor distribution
    log_mu = (
        pos_params["intercept"]
        + pos_params["coef_projected_points"] * pts
        + pos_params["coef_projected_points_sq"] * pts_sq
        + pos_params["coef_team_probability"] * team_probability
        + pos_params["coef_is_rfa"] * rfa
    )
    sigma = max(
        pos_params["sigma_floor"],
        pos_params["sigma_intercept"] + pos_params["sigma_slope"] * pts,
    )

    # Derived values
    median_above = math.exp(log_mu)
    mean_above = math.exp(log_mu + sigma * sigma / 2.0)

    # Expected price: weighted by P(floor)
    expected = p_floor * MIN_SALARY + (1.0 - p_floor) * mean_above

    # Median: weighted blend (not strictly correct but useful approximation)
    median = p_floor * MIN_SALARY + (1.0 - p_floor) * median_above

    # Confidence intervals (10th/90th percentile of above-floor distribution)
    # Blend with floor probability
    ci_low_above = math.exp(log_mu + sigma * _Z_10)
    ci_high_above = math.exp(log_mu + sigma * _Z_90)
    ci_low = p_floor * MIN_SALARY + (1.0 - p_floor) * ci_low_above
    ci_high = p_floor * MIN_SALARY + (1.0 - p_floor) * ci_high_above

    # Clamp to position bounds
    min_bid = pos_params["min_bid"]
    max_bid = pos_params["max_bid"]
    expected = max(min_bid, min(max_bid, expected))
    median = max(min_bid, min(max_bid, median))
    ci_low = max(min_bid, min(max_bid, ci_low))
    ci_high = max(min_bid, min(max_bid, ci_high))

    return PricePrediction(
        expected_price=expected,
        median_price=median,
        p_floor=p_floor,
        sigma=sigma,
        log_mu=log_mu,
        ci_low=ci_low,
        ci_high=ci_high,
    )


def predict_all_prices(
    players: dict,
    params: dict,
) -> dict[str, PricePrediction]:
    """Compute price predictions for all biddable players."""
    predictions = {}
    for name, player in players.items():
        predictions[name] = predict_price(
            position=player.position,
            projected_points=player.projected_points,
            team_probability=player.team_probability,
            is_rfa=player.is_rfa,
            params=params,
        )
    return predictions


# Standard normal quantiles for CIs
_Z_10 = -1.2816  # 10th percentile
_Z_90 = 1.2816  # 90th percentile


def _sigmoid(x: float) -> float:
    """Numerically stable sigmoid that avoids exp overflow."""
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    else:
        z = math.exp(x)
        return z / (1.0 + z)

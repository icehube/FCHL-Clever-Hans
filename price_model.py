"""Two-stage per-position price prediction model (Layer 1).

Round-2 model (July 2026): skaters are piecewise-linear in projected points
(hinge terms at 60/80 for F, 60 for D), goalies are priced on projected WINS
(shutouts are unprojectable noise), and both stages use lagged-salary
(reputation) and positional-scarcity-rank features. Coefficients live in
data/model_params.json, exported by the FCHL-auction-pricer notebook —
unused features carry coefficient 0.0 so one formula serves all positions.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

from config import MIN_SALARY

# Feature keys shared by both stages; params hold floor_coef_<key> and
# coef_<key> for each (0.0 when a position doesn't use the feature).
_FEATURE_KEYS = (
    "projected_points",
    "projected_points_sq",
    "pts_hinge_60",
    "pts_hinge_80",
    "team_probability",
    "is_rfa",
    "log_rank",
    "log_lag",
    "has_lag",
    "proj_wins",
)

# 90th-percentile z: the exported CIs are the 10th/90th percentiles (80% band)
_Z_80 = 1.2815515655446004


@dataclass
class PricePrediction:
    """Full price prediction output for a single player."""

    expected_price: float  # p_floor * MIN + (1-p_floor) * clipped-lognormal mean
    median_price: float  # clip(exp(log_mu), min_bid, max_bid) — above-floor median
    p_floor: float  # P(sells at floor)
    sigma: float  # Log-normal sigma for above-floor distribution
    log_mu: float  # Log-normal mu for above-floor distribution
    ci_low: float  # 10th percentile (above-floor, clipped)
    ci_high: float  # 90th percentile (above-floor, clipped)


def load_model_params(path: str = "data/model_params.json") -> dict:
    """Load per-position model coefficients."""
    with open(path) as f:
        return json.load(f)


def compute_pos_ranks(players: dict) -> dict[str, int]:
    """Rank each player by projected points within their position (ties=min).

    The scarcity feature (log_rank) was trained on ranks within each season's
    auction pool — compute it once against the draft-time pool and keep it
    fixed; re-ranking the shrinking pool mid-draft would inflate prices.
    """
    by_pos: dict[str, list[float]] = {}
    for player in players.values():
        by_pos.setdefault(player.position, []).append(player.projected_points)
    for pts in by_pos.values():
        pts.sort(reverse=True)

    ranks: dict[str, int] = {}
    for name, player in players.items():
        pool = by_pos[player.position]
        # ties=min: 1 + count of same-position players strictly above
        lo, hi = 0, len(pool)
        while lo < hi:
            mid = (lo + hi) // 2
            if pool[mid] > player.projected_points:
                lo = mid + 1
            else:
                hi = mid
        ranks[name] = 1 + lo
    return ranks


def predict_price(
    position: str,
    projected_points: float,
    team_probability: float,
    is_rfa: bool,
    params: dict,
    last_salary: float | None = None,
    pos_rank: int = 1,
    proj_wins: float | None = None,
) -> PricePrediction:
    """
    Two-stage price prediction.

    Stage 1 (Logistic): P(player sells at floor)
    Stage 2 (Log-normal): salary distribution conditional on above-floor

    Args:
        last_salary: player's FCHL salary last season ($M); None if new to league
        pos_rank: rank by projected points among same-position players in the
            draft-time pool (1 = best)
        proj_wins: goalies only — projected wins; None falls back to
            projected_points / metadata.goalie_pts_per_win
    """
    pos_params = params[position]
    pts = projected_points

    if position == "G" and proj_wins is None:
        proj_wins = pts / params["metadata"]["goalie_pts_per_win"]

    feats = {
        "projected_points": pts,
        "projected_points_sq": pts * pts,
        "pts_hinge_60": max(pts - 60.0, 0.0),
        "pts_hinge_80": max(pts - 80.0, 0.0),
        "team_probability": team_probability,
        "is_rfa": 1.0 if is_rfa else 0.0,
        "log_rank": math.log(max(pos_rank, 1)),
        "log_lag": math.log(max(last_salary, MIN_SALARY))
        if last_salary is not None
        else math.log(MIN_SALARY),
        "has_lag": 1.0 if last_salary is not None else 0.0,
        "proj_wins": proj_wins if position == "G" else 0.0,
    }

    # Stage 1: P(floor) via logistic regression
    logit = pos_params["floor_intercept"] + sum(
        pos_params[f"floor_coef_{key}"] * feats[key] for key in _FEATURE_KEYS
    )
    p_floor = _sigmoid(logit)

    # Stage 2: log-normal parameters for above-floor distribution
    log_mu = pos_params["intercept"] + sum(
        pos_params[f"coef_{key}"] * feats[key] for key in _FEATURE_KEYS
    )
    # Sigma is a function of the *prediction* (not points); exported values
    # already include the MAD->SD correction — use directly as a normal SD.
    sigma = max(
        pos_params["sigma_intercept"] + pos_params["sigma_slope"] * log_mu,
        pos_params["sigma_floor"],
    )

    min_bid = pos_params["min_bid"]
    max_bid = pos_params["max_bid"]

    median = _clamp(math.exp(log_mu), min_bid, max_bid)

    # Budget math needs the mean, not the median: the clipped-lognormal mean
    # has a closed form (probability mass outside [min_bid, max_bid] collapses
    # onto the bounds).
    z_lo = (math.log(min_bid) - log_mu) / sigma
    z_hi = (math.log(max_bid) - log_mu) / sigma
    mean_above = (
        min_bid * _norm_cdf(z_lo)
        + math.exp(log_mu + sigma * sigma / 2.0)
        * (_norm_cdf(z_hi - sigma) - _norm_cdf(z_lo - sigma))
        + max_bid * (1.0 - _norm_cdf(z_hi))
    )

    expected = p_floor * min_bid + (1.0 - p_floor) * mean_above

    # 80% interval: 10th/90th percentiles of the above-floor distribution
    ci_low = _clamp(math.exp(log_mu - _Z_80 * sigma), min_bid, max_bid)
    ci_high = _clamp(math.exp(log_mu + _Z_80 * sigma), min_bid, max_bid)

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
    """Compute price predictions for all biddable players.

    Player.salary carries last season's FCHL salary (0 = new to league),
    which feeds the lag/reputation feature.
    """
    predictions = {}
    for name, player in players.items():
        predictions[name] = predict_price(
            position=player.position,
            projected_points=player.projected_points,
            team_probability=player.team_probability,
            is_rfa=player.is_rfa,
            params=params,
            last_salary=player.salary if player.salary > 0 else None,
            pos_rank=player.pos_rank,
            proj_wins=player.proj_wins,
        )
    return predictions


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via erf — avoids a scipy dependency."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _sigmoid(x: float) -> float:
    """Numerically stable sigmoid that avoids exp overflow."""
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    else:
        z = math.exp(x)
        return z / (1.0 + z)

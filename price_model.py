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


def build_features(
    position: str,
    projected_points: float,
    team_probability: float,
    is_rfa: bool,
    params: dict,
    last_salary: float | None = None,
    pos_rank: int = 1,
    proj_wins: float | None = None,
) -> dict[str, float]:
    """The 10-feature vector both stages score — the ONE place it is built.

    Extracted from predict_price so a decomposition reads exactly the numbers
    the prediction read. A second copy of this dict would be free to drift on
    the details that matter most: `log_lag` is ln(MIN_SALARY) rather than 0 for
    a player new to the league, and `pos_rank` 0 (the "unset" default) silently
    becomes rank 1, the best possible scarcity value.
    """
    pts = projected_points

    if position == "G" and proj_wins is None:
        proj_wins = pts / params["metadata"]["goalie_pts_per_win"]

    return {
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


def _score(pos_params: dict, feats: dict[str, float]) -> tuple[float, float]:
    """(stage-1 logit, stage-2 log_mu) — the ONE place coefficients are summed.

    Both stages are the same linear form over the same feature vector, which is
    what makes `decompose_price` exact: log_mu is additive in the per-feature
    products, so grouping them and subtracting a reference reconstructs it.
    """
    logit = pos_params["floor_intercept"] + sum(
        pos_params[f"floor_coef_{key}"] * feats[key] for key in _FEATURE_KEYS
    )
    log_mu = pos_params["intercept"] + sum(
        pos_params[f"coef_{key}"] * feats[key] for key in _FEATURE_KEYS
    )
    return logit, log_mu


def _player_inputs(player) -> dict:
    """The predict_price kwargs a pool Player implies. One rule, two callers.

    `salary == 0` means "new to the league", NOT "$0M last season" — the two
    produce completely different has_lag/log_lag, so a second copy of this line
    on the decomposition path would explain a prediction the app never made.
    """
    return {
        "position": player.position,
        "projected_points": player.projected_points,
        "team_probability": player.team_probability,
        "is_rfa": player.is_rfa,
        "last_salary": player.salary if player.salary > 0 else None,
        "pos_rank": player.pos_rank,
        "proj_wins": player.proj_wins,
    }


def points_slopes(pos_params: dict) -> tuple[tuple[float, float], ...]:
    """Stage-2 log-price slope per projected point, by segment.

    Returns ((breakpoint, slope), ...) with the hinges ACCUMULATED, so each
    entry is the total slope from that breakpoint up — which is the form you
    need to ask whether more points ever costs less. Goalies price on wins
    (`coef_projected_points` is 0.0 for G), so G returns one flat segment.

    A function rather than three coefficients summed at the call site: the
    guard test summed them inline and was the weaker for it.
    """
    base = pos_params["coef_projected_points"]
    h60 = pos_params["coef_pts_hinge_60"]
    h80 = pos_params["coef_pts_hinge_80"]
    return ((0.0, base), (60.0, base + h60), (80.0, base + h60 + h80))


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

    feats = build_features(
        position, projected_points, team_probability, is_rfa, params,
        last_salary=last_salary, pos_rank=pos_rank, proj_wins=proj_wins,
    )
    logit, log_mu = _score(pos_params, feats)
    p_floor = _sigmoid(logit)
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
    return {
        name: predict_price(params=params, **_player_inputs(player))
        for name, player in players.items()
    }


# ---------------------------------------------------------------------------
# Price decomposition: what each driver contributes to the stage-2 median.
# ---------------------------------------------------------------------------

# The five drivers, and the grouping is not cosmetic.
#
# `log_lag` and `has_lag` MUST stay together: log_lag is ln(MIN_SALARY), not 0,
# for a player new to the league, so split apart every newcomer shows a
# spurious negative "reputation" contribution.
#
# `log_rank` and `is_rfa` get their own rows rather than an "other" bucket
# because they are not small — measured on the live pool, Scarcity is the
# LARGEST driver for every expensive forward (McDavid x7.97 against Points
# x1.39). Note that `pos_rank` is computed FROM projected_points, so Points and
# Scarcity are collinear by construction and have to be read together; the card
# says so.
DRIVER_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Points", ("projected_points", "projected_points_sq",
                "pts_hinge_60", "pts_hinge_80", "proj_wins")),
    ("NHL team", ("team_probability",)),
    ("Reputation", ("log_lag", "has_lag")),
    ("Scarcity", ("log_rank",)),
    ("RFA", ("is_rfa",)),
)


@dataclass(frozen=True)
class DriverContribution:
    """One driver's effect on the stage-2 median, relative to the reference."""

    group: str
    keys: tuple[str, ...]
    log_delta: float  # sum of coef_k * (feats[k] - reference[k]); ADDITIVE
    factor: float  # exp(log_delta); MULTIPLICATIVE and order-invariant
    floor_logit_delta: float  # the same sum over stage-1 coefficients


@dataclass(frozen=True)
class PriceBreakdown:
    """Why the model priced this player where it did."""

    position: str
    features: dict[str, float]
    reference: dict[str, float]
    base_log_mu: float  # log_mu of the reference vector
    base_price: float  # exp(base_log_mu), UNCLAMPED — may sit below MIN_SALARY
    base_p_floor: float
    drivers: tuple[DriverContribution, ...]
    unclamped_price: float  # exp(prediction.log_mu), before the bid clips
    clamped: str | None  # "min" | "max" | None
    prediction: PricePrediction
    min_bid: float
    max_bid: float


def decompose_price(
    position: str,
    projected_points: float,
    team_probability: float,
    is_rfa: bool,
    params: dict,
    reference: dict[str, float],
    last_salary: float | None = None,
    pos_rank: int = 1,
    proj_wins: float | None = None,
) -> PriceBreakdown:
    """Per-driver decomposition of the stage-2 median, against a reference player.

        log_mu(player) = log_mu(reference) + SUM_groups SUM_keys coef_k * (x_k - r_k)

    exactly, because both sides are the same linear form over the same feature
    vector. Exponentiating turns the additive log deltas into FACTORS whose
    product is price / reference price.

    **Report the factor, never a per-row dollar step.** A dollar step is
    exp(running + delta) - exp(running), so it depends on where the row sits in
    the list: measured 2026-09-09 across all 120 orderings of the five groups,
    McDavid's Points step runs $0.11M to $2.72M and Scarcity's $2.06M to
    $8.50M. Every one of those is arithmetically correct, which is exactly what
    makes displaying one dangerous — the figure gets quoted. The factor does not
    move.

    Stage 1 is decomposed too (`floor_logit_delta`) and is deliberately NOT the
    headline: it is log-odds, and its coefficients frequently point the OTHER
    way from stage 2 (for F, `floor_coef_log_rank` is +3.006 while
    `coef_log_rank` is -0.391 — a deep rank makes a player both more likely to
    be a floor sale and cheaper if he is not). `expected_price` is not a linear
    function of either stage: sigma depends on log_mu and the clip bounds are
    per-position. **This explains the MEDIAN and nothing else.**
    """
    pos_params = params[position]
    feats = build_features(
        position, projected_points, team_probability, is_rfa, params,
        last_salary=last_salary, pos_rank=pos_rank, proj_wins=proj_wins,
    )
    prediction = predict_price(
        position, projected_points, team_probability, is_rfa, params,
        last_salary=last_salary, pos_rank=pos_rank, proj_wins=proj_wins,
    )

    base_logit, base_log_mu = _score(pos_params, reference)

    drivers = tuple(
        DriverContribution(
            group=group,
            keys=keys,
            log_delta=(
                d := sum(
                    pos_params[f"coef_{k}"] * (feats[k] - reference[k])
                    for k in keys
                )
            ),
            factor=math.exp(d),
            floor_logit_delta=sum(
                pos_params[f"floor_coef_{k}"] * (feats[k] - reference[k])
                for k in keys
            ),
        )
        for group, keys in DRIVER_GROUPS
    )

    unclamped = math.exp(prediction.log_mu)
    min_bid, max_bid = pos_params["min_bid"], pos_params["max_bid"]
    clamped = "min" if unclamped < min_bid else "max" if unclamped > max_bid else None

    return PriceBreakdown(
        position=position,
        features=feats,
        reference=dict(reference),
        base_log_mu=base_log_mu,
        base_price=math.exp(base_log_mu),
        base_p_floor=_sigmoid(base_logit),
        drivers=drivers,
        unclamped_price=unclamped,
        clamped=clamped,
        prediction=prediction,
        min_bid=min_bid,
        max_bid=max_bid,
    )


def decompose_player(
    player, params: dict, reference: dict[str, float]
) -> PriceBreakdown:
    """decompose_price for a pool Player, through the same input rule as pricing."""
    return decompose_price(params=params, reference=reference,
                           **_player_inputs(player))


def compute_reference_features(
    players: dict, params: dict
) -> dict[str, dict[str, float]]:
    """The per-position "typical player" every breakdown is measured against.

    Median of the RAW inputs, then through `build_features` — not the median of
    each derived feature. The two agree on this pool because the medians happen
    to land on the same player, but only by luck: on an even-sized pool
    median(pts**2) != median(pts)**2, and a reference whose `pts_hinge_60`
    disagreed with its own `projected_points` would describe no player at all,
    so the card could not name it on screen. Booleans take the majority value;
    `last_salary` is the median among those who have one, and None when most of
    the position does not.

    Computed ONCE against the draft-time pool and frozen, for exactly the reason
    `compute_pos_ranks` is frozen: the pool shrinks. Measured over the first 165
    picks the reference log_mu moves F -0.194, D -0.139, G -0.794 — a goalie
    baseline that halves — so recomputing it per request would silently restate
    every explanation given earlier in the draft.

    A position with no players is absent from the result; callers must cope.
    """
    by_pos: dict[str, list] = {}
    for player in players.values():
        by_pos.setdefault(player.position, []).append(player)

    refs: dict[str, dict[str, float]] = {}
    for position, roster in by_pos.items():
        lags = sorted(p.salary for p in roster if p.salary > 0)
        wins = sorted(p.proj_wins for p in roster if p.proj_wins is not None)
        refs[position] = build_features(
            position=position,
            projected_points=_median(sorted(p.projected_points for p in roster)),
            team_probability=_median(sorted(p.team_probability for p in roster)),
            is_rfa=sum(1 for p in roster if p.is_rfa) * 2 > len(roster),
            params=params,
            last_salary=_median(lags) if len(lags) * 2 > len(roster) else None,
            pos_rank=round(_median(sorted(p.pos_rank for p in roster))) or 1,
            proj_wins=_median(wins) if wins else None,
        )
    return refs


def _median(values: list[float]) -> float:
    """Median of an already-sorted list. Local so this module keeps no deps."""
    n = len(values)
    mid = n // 2
    return values[mid] if n % 2 else (values[mid - 1] + values[mid]) / 2.0


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

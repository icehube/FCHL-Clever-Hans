"""Tests for price_model.py: two-stage log-normal price predictions.

The authoritative check is the golden-file test: the pricer notebook exports
auction_predictions_current.csv alongside model_params.json, and every one of
its 139 predictions must be reproduced from the params within rounding.
"""

import csv

import pytest

from price_model import (
    DRIVER_GROUPS,
    PricePrediction,
    _FEATURE_KEYS,
    build_features,
    compute_pos_ranks,
    compute_reference_features,
    decompose_price,
    load_model_params,
    points_slopes,
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
        segments = dict(points_slopes(params["F"]))
        assert segments[80.0] < segments[0.0]
        # And the deployed skater fits are piecewise-linear: pts^2 dropped
        assert params["F"]["coef_projected_points_sq"] == 0.0
        assert params["D"]["coef_projected_points_sq"] == 0.0

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "KNOWN BAD as of 2026-09-09. F coef_pts_hinge_80 = -0.0504 against a "
            "60-80 slope of +0.0310 puts the stage-2 points slope at -0.0194 above "
            "80 pts, so the forward price curve PEAKS at 80 and falls: at rank 10 "
            "with a $6M lag, 80pts -> $6.60M and 132pts -> $2.44M, below a 40-pt "
            "forward's $2.51M. NOT a bug in price_model.py — the golden fixture "
            "reproduces it, so it is in the coefficients the pricer notebook "
            "exported, and data/model_params.json may not be hand-edited. Needs a "
            "refit; see BACKLOG.md. strict=True so a corrected export fails here "
            "as XPASS and forces this marker and that entry to be deleted."
        ),
    )
    def test_the_points_slope_never_goes_negative(self, params):
        """More projected points must never lower the predicted price.

        The property the test above is too weak to state: it asserts only that
        the slope is DAMPED past 80, which a negative slope satisfies. That is
        how a fit which prices a 132-point forward below a 40-point one stayed
        green for two months.
        """
        for position in ("F", "D", "G"):
            for breakpoint_, slope in points_slopes(params[position]):
                assert slope >= 0.0, (
                    f"{position}: the points slope is {slope:+.4f} above "
                    f"{breakpoint_:.0f} pts, so scoring more costs less"
                )

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
        # 40 vs 90 straddles the peak and passes on a curve that turns over at
        # 80 — the word "generally" was doing load-bearing work here. The real
        # comparison is `test_more_points_never_costs_less` below.
        low = predict_price("F", 40, 5.0, False, params, pos_rank=30)
        high = predict_price("F", 90, 5.0, False, params, pos_rank=30)
        assert high.expected_price > low.expected_price

    @pytest.mark.parametrize("position", [
        pytest.param("F", marks=pytest.mark.xfail(
            strict=True,
            reason="KNOWN BAD 2026-09-09: the F curve peaks at 80 pts and falls. "
                   "See test_the_points_slope_never_goes_negative for the whole "
                   "reason and BACKLOG.md for the refit.",
        )),
        "D",
        "G",
    ])
    def test_more_points_never_costs_less(self, params, position):
        """The behavioural form of the slope guard, in dollars.

        Holds rank and lag fixed and walks the points axis, which is the
        comparison an owner actually makes when the panel says a 120-point
        forward is cheaper than an 85-point one. D and G are real legs, not
        decoration: both are monotone today and would catch a refit breaking
        them.
        """
        prices = [
            predict_price(position, pts, 5.0, False, params,
                          last_salary=6.0, pos_rank=10).expected_price
            for pts in range(0, 141, 5)
        ]
        for (a, pts_a), (b, pts_b) in zip(
            list(zip(prices, range(0, 141, 5))),
            list(zip(prices, range(0, 141, 5)))[1:],
        ):
            assert b >= a - 1e-9, (
                f"{position}: {pts_b} pts prices at ${b:.2f}M against "
                f"${a:.2f}M for {pts_a} pts"
            )

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


@pytest.fixture(scope="module")
def pool():
    """The live biddable pool, loaded once — several tests below sweep it."""
    from data_loader import load_goalie_wins, load_players, load_team_odds

    _, biddable = load_players(
        team_odds=load_team_odds(), goalie_wins=load_goalie_wins()
    )
    return biddable


@pytest.fixture(scope="module")
def refs(pool):
    return compute_reference_features(pool, load_model_params())


class TestDriverGroups:
    def test_every_feature_belongs_to_exactly_one_group(self):
        """An 11th feature must join a group or fail here.

        The reconstruction test below also catches it, but as a bare arithmetic
        mismatch with no cause attached. This one names it.
        """
        grouped = [k for _, keys in DRIVER_GROUPS for k in keys]
        assert sorted(grouped) == sorted(_FEATURE_KEYS), (
            "DRIVER_GROUPS and _FEATURE_KEYS disagree; a feature is unexplained "
            "or explained twice"
        )
        assert len(grouped) == len(set(grouped)), "a feature is in two groups"

    def test_reputation_keeps_its_two_features_together(self):
        """log_lag is ln(MIN_SALARY) for a newcomer, not 0.

        Split across two groups, every player new to the league shows a
        spurious negative "reputation" — the model is not saying that, the
        encoding is.
        """
        reputation = dict(DRIVER_GROUPS)["Reputation"]
        assert set(reputation) == {"log_lag", "has_lag"}


class TestPriceBreakdown:
    """The decomposition has to reconstruct the prediction, or it is decoration."""

    CASES = [
        ("F", 120, 5.0, False, 7.3, 2, None),
        ("F", 20, 2.0, False, None, 203, None),
        ("F", 85, 9.7, True, 6.4, 7, None),
        ("D", 75, 11.0, True, 4.0, 1, None),
        ("D", 10, 0.4, False, None, 300, None),
        ("G", 99, 3.6, False, 5.5, 1, 41.0),
        ("G", 60, 3.1, True, None, 20, None),
    ]

    @pytest.mark.parametrize("position,pts,prob,rfa,lag,rank,wins", CASES)
    def test_contributions_reconstruct_log_mu(
        self, params, refs, position, pts, prob, rfa, lag, rank, wins
    ):
        b = decompose_price(
            position, pts, prob, rfa, params, refs[position],
            last_salary=lag, pos_rank=rank, proj_wins=wins,
        )
        total = b.base_log_mu + sum(d.log_delta for d in b.drivers)
        # approx, not ==: the two sums are algebraically identical and float
        # REASSOCIATED, so bitwise equality is not a property this code has.
        # Do not "tighten" this to == — it will pass here and flake elsewhere.
        assert total == pytest.approx(b.prediction.log_mu, rel=1e-12)

    @pytest.mark.parametrize("position,pts,prob,rfa,lag,rank,wins", CASES)
    def test_floor_contributions_reconstruct_the_logit(
        self, params, refs, position, pts, prob, rfa, lag, rank, wins
    ):
        """Stage 1 too — nothing on screen exercises it, so nothing else would."""
        import math

        b = decompose_price(
            position, pts, prob, rfa, params, refs[position],
            last_salary=lag, pos_rank=rank, proj_wins=wins,
        )
        base_logit = math.log(b.base_p_floor / (1.0 - b.base_p_floor))
        logit = base_logit + sum(d.floor_logit_delta for d in b.drivers)
        recovered = 1.0 / (1.0 + math.exp(-logit))
        assert recovered == pytest.approx(b.prediction.p_floor, abs=1e-9)

    def test_the_whole_pool_reconstructs(self, params, pool, refs):
        """Every player, not a sample — the sweep is cheap and the claim is total."""
        from price_model import decompose_player

        worst = 0.0
        for name, player in pool.items():
            b = decompose_player(player, params, refs[player.position])
            total = b.base_log_mu + sum(d.log_delta for d in b.drivers)
            worst = max(worst, abs(total - b.prediction.log_mu))
        assert worst < 1e-9, f"worst reconstruction error {worst:.3e}"

    def test_the_factors_multiply_to_the_unclamped_price(self, params, refs):
        """The form the card actually prints: base x f1 x ... x fn = price."""
        import math

        b = decompose_price("F", 120, 5.0, False, params, refs["F"],
                            last_salary=7.3, pos_rank=2)
        product = b.base_price
        for d in b.drivers:
            product *= d.factor
        assert product == pytest.approx(b.unclamped_price, rel=1e-12)

    def test_the_factors_do_not_depend_on_their_order(self, params, refs):
        """Why the card shows factors and not per-row dollar steps.

        A dollar step is exp(running + delta) - exp(running) and moves with the
        row's position in the list; the factor does not. Asserted because the
        dollar column is the obvious "improvement" someone will reach for.
        """
        import itertools
        import math

        b = decompose_price("F", 132, 11.0, True, params, refs["F"],
                            last_salary=11.4, pos_rank=1)
        deltas = {d.group: d.log_delta for d in b.drivers}
        factors = {d.group: d.factor for d in b.drivers}

        steps = {g: set() for g in deltas}
        for order in itertools.permutations(deltas):
            running = b.base_log_mu
            for g in order:
                steps[g].add(round(math.exp(running + deltas[g]) - math.exp(running), 6))
                running += deltas[g]

        assert any(len(v) > 1 for v in steps.values()), (
            "no group's dollar step moved with the ordering, so this test is "
            "not exercising the hazard it exists for"
        )
        for g, f in factors.items():
            assert math.exp(deltas[g]) == pytest.approx(f, rel=1e-12), (
                f"{g}'s factor is not exp of its log delta"
            )

    def test_a_player_against_his_own_features_has_no_drivers(self, params):
        """Pins the sign convention, which reconstruction alone cannot see.

        Swap the subtraction and every log_delta flips sign while the totals
        still reconcile against the swapped baseline.
        """
        own = build_features("F", 90, 6.0, True, params, last_salary=5.0, pos_rank=4)
        b = decompose_price("F", 90, 6.0, True, params, own,
                            last_salary=5.0, pos_rank=4)
        for d in b.drivers:
            assert d.log_delta == pytest.approx(0.0, abs=1e-12), f"{d.group} moved"
            assert d.factor == pytest.approx(1.0, abs=1e-12)
        assert b.base_log_mu == pytest.approx(b.prediction.log_mu, rel=1e-12)

    def test_a_better_player_than_the_reference_prices_above_it(self, params, refs):
        """Direction, so a sign flip that survives the two tests above dies here."""
        b = decompose_price("F", 110, 9.0, True, params, refs["F"],
                            last_salary=8.0, pos_rank=3)
        assert b.unclamped_price > b.base_price


class TestReferenceFeatures:
    def test_it_covers_every_position_in_the_pool(self, pool, refs):
        assert set(refs) == {p.position for p in pool.values()}

    def test_the_reference_is_a_coherent_feature_vector(self, refs):
        """It describes a real player, which is what lets the card name it.

        Median of the raw inputs THEN through build_features, not the median of
        each derived feature — a reference whose pts_hinge_60 disagreed with its
        own projected_points would be unnameable on screen.
        """
        import math

        for position, r in refs.items():
            pts = r["projected_points"]
            assert r["projected_points_sq"] == pytest.approx(pts * pts)
            assert r["pts_hinge_60"] == pytest.approx(max(pts - 60.0, 0.0))
            assert r["pts_hinge_80"] == pytest.approx(max(pts - 80.0, 0.0))
            assert r["has_lag"] in (0.0, 1.0)
            assert r["is_rfa"] in (0.0, 1.0)
            if not r["has_lag"]:
                assert r["log_lag"] == pytest.approx(math.log(0.5)), (
                    f"{position}: a no-lag reference must carry the ln(MIN_SALARY) "
                    f"encoding, not 0, or every newcomer reads as below typical"
                )
            assert math.exp(r["log_rank"]) >= 1.0

    def test_an_empty_pool_yields_no_reference(self, params):
        assert compute_reference_features({}, params) == {}

    def test_it_does_not_move_as_the_pool_shrinks_by_itself(self, params, pool):
        """Not a property of the function — a warning about how it must be CALLED.

        Recomputed against a drafted-down pool the reference moves a long way
        (measured: F -0.194, D -0.139, G -0.794 in log_mu over 165 picks), which
        is why the caller freezes it at draft time rather than rebuilding it per
        request. This test states the size of the drift so the freezing is not
        mistaken for ceremony.
        """
        from price_model import _score

        full = compute_reference_features(pool, params)
        ranked = sorted(pool.values(), key=lambda p: p.pos_rank)
        survivors = {p.name: p for p in ranked if p.pos_rank > 15}
        shrunk = compute_reference_features(survivors, params)

        moved = {
            pos: _score(params[pos], shrunk[pos])[1] - _score(params[pos], full[pos])[1]
            for pos in full
            if pos in shrunk
        }
        assert any(abs(v) > 0.05 for v in moved.values()), (
            f"the reference barely moved ({moved}), so this test no longer "
            f"demonstrates why it has to be frozen"
        )

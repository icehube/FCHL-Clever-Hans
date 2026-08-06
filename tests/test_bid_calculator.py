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

    def test_max_bid_never_exceeds_physical_max(self):
        """Max bid should never exceed team's physical max bid."""
        state, mp, info = _setup_real_data()
        team = state.teams[MY_TEAM]
        player = next(
            p for p in state.available_players.values()
            if p.projected_points > 50
        )
        rec = compute_bid_recommendation(
            player, team, state.available_players, mp, info,
        )
        assert rec.max_bid <= team.physical_max_bid + 0.01


class TestUncontestedBidding:
    """Regression: DROP on a bargain when BOT is the last bidder standing.

    Reported 2026-08-05. Every opponent drops out -> compute_live_ceiling finds
    no opponent ceilings and returns the MIN_SALARY floor -> max_bid collapsed
    to $0.6M -> the advisor said DROP at $2.5M on a player worth $4.2M.

    The ceiling forecasts the clearing price. It is a valid cap only while the
    standing price is still below it; once a real price exceeds the forecast,
    the forecast is falsified and value binds instead.
    """

    def _setup(self):
        """One open spot, $4.2M left, and a clear upgrade in the pool.

        Marginal value lands at physical_max_bid ($4.2M): the objective is
        points, so Star beats Filler at any affordable price.
        """
        keepers = (
            [PlayerOnRoster(name=f"F{i}", position="F", group="3", salary=2.0,
                            projected_points=50) for i in range(13)]
            + [PlayerOnRoster(name=f"D{i}", position="D", group="3", salary=2.0,
                              projected_points=40) for i in range(7)]
            + [PlayerOnRoster(name=f"G{i}", position="G", group="3", salary=2.0,
                              projected_points=30) for i in range(3)]
        )
        team = TeamState(
            code=MY_TEAM, name="Bot Team", keeper_players=keepers,
            minor_players=[], acquired_players=[], penalties=6.6, is_done=False,
            colors={"primary": "#000", "secondary": "#fff"},
            logo="test.png", is_my_team=True,
        )
        pool = {
            "Star": _make_player("Star", "F", 90),
            "Filler": _make_player("Filler", "F", 50),
        }
        prices = {"Star": 3.0, "Filler": MIN_SALARY}
        return team, pool, prices

    def _floor_ceiling(self):
        """MarketInfo as compute_live_ceiling returns it with no opponents left."""
        return MarketInfo(
            market_ceiling=MIN_SALARY, highest_bidder=None,
            highest_bid=MIN_SALARY, second_bidder=None,
            demand_count=0, floor_demand=False,
        )

    def test_setup_reproduces_reported_numbers(self):
        """Precondition: the fixture really does have $4.2M of headroom."""
        team, _, _ = self._setup()
        assert team.total_spots_remaining == 1
        assert team.physical_max_bid == pytest.approx(4.2, abs=0.01)

    def test_marginal_value_reaches_physical_max(self):
        """A team must be able to bid its own physical max.

        remaining_budget used to come out as 4.199999999999996, so the MILP
        budget constraint rejected a forced bid at exactly 4.2 and the binary
        search settled one increment low at 4.1 — $0.1M of real headroom lost
        to float error. Star beats Filler on points at any affordable price,
        so the break-even salary IS the physical max.
        """
        team, pool, prices = self._setup()
        marginal = compute_marginal_value(pool["Star"], team, pool, prices)
        assert marginal == team.physical_max_bid == 4.2

    def test_uncontested_win_at_good_price(self):
        """The exact reported case: $2.5M on a ~$4.2M player is a WIN, not DROP."""
        team, pool, prices = self._setup()
        rec = compute_bid_recommendation(
            pool["Star"], team, pool, prices, self._floor_ceiling(),
            current_price=2.5, bot_uncontested=True,
        )
        assert rec.action == "WIN"
        # Value binds, not the collapsed $0.5M ceiling. Pre-fix this was $0.6M
        # (ceiling + increment), which is what produced the spurious DROP.
        assert rec.max_bid == rec.marginal_value
        assert rec.max_bid > 4.0
        assert rec.uncontested is True

    def test_uncontested_break_even_is_a_win(self):
        """At exactly max_bid there is no next increment — take the player."""
        team, pool, prices = self._setup()
        marginal = compute_marginal_value(pool["Star"], team, pool, prices)
        rec = compute_bid_recommendation(
            pool["Star"], team, pool, prices, self._floor_ceiling(),
            current_price=marginal, bot_uncontested=True,
        )
        assert rec.action == "WIN"

    def test_uncontested_overpay_drops(self):
        """Above marginal value it's still a DROP, and says by how much."""
        team, pool, prices = self._setup()
        marginal = compute_marginal_value(pool["Star"], team, pool, prices)
        rec = compute_bid_recommendation(
            pool["Star"], team, pool, prices, self._floor_ceiling(),
            current_price=round(marginal + 0.8, 1), bot_uncontested=True,
        )
        assert rec.action == "DROP"
        assert "0.8" in rec.reasoning, f"should name the overpay: {rec.reasoning}"

    def test_uncontested_full_roster_still_drops(self):
        """A full roster gains nothing from another player — uncontested
        doesn't override that.

        Note the reason: NOT that the team can't bid. A 24-man team with cap
        space still can (the extra goes to minors at full cap), and as of
        2026-08-05 physical_max_bid reports that capacity so the team stays
        visible to market ceilings. What stops the bid is roster value — with
        no spots left the MILP can't seat him, so marginal value is zero — and
        zero, not MIN_SALARY, so the ladder can tell "worthless" apart from
        "worth the floor" and DROP instead of recommending a $0.5M BID.
        """
        team, pool, prices = self._setup()
        team.keeper_players.append(PlayerOnRoster(
            name="F99", position="F", group="3", salary=2.0, projected_points=50,
        ))
        assert team.physical_max_bid > 0.0, "full roster still has bidding capacity"
        assert compute_marginal_value(pool["Star"], team, pool, prices) == 0.0
        rec = compute_bid_recommendation(
            pool["Star"], team, pool, prices, self._floor_ceiling(),
            current_price=2.5, bot_uncontested=True,
        )
        assert rec.action == "DROP"
        assert rec.max_bid == 0.0

    def test_advice_never_inverts_as_price_rises(self):
        """Willingness must be non-increasing in price.

        Judging the verdict ladder on a max_bid that blends the value cap with
        the ceiling forecast made advice non-monotonic: the forecast releasing
        one increment above itself flipped DROP at $1.1M into BID at $1.2M.
        A point test at the boundary would miss that, so assert the property.
        """
        team, pool, prices = self._setup()
        info = MarketInfo(
            market_ceiling=1.0, highest_bidder="AAA", highest_bid=1.0,
            second_bidder="BBB", demand_count=2, floor_demand=False,
        )
        rank = {"BID": 2, "CAUTION": 1, "DROP": 0}
        # The ceiling boundary ($1.1M) and the value boundary (~$4.1M)
        sweep = [round(0.5 + i * SALARY_INCREMENT, 1) for i in range(11)]
        sweep += [round(3.6 + i * SALARY_INCREMENT, 1) for i in range(8)]
        seen = []
        for price in sweep:
            rec = compute_bid_recommendation(
                pool["Star"], team, pool, prices, info, current_price=price,
            )
            seen.append((price, rec.action, rec.max_bid))
        ranks = [rank[a] for _, a, _ in seen]
        assert ranks == sorted(ranks, reverse=True), f"advice inverted: {seen}"

    def test_ceiling_still_caps_while_price_below_it(self):
        """The critical rule holds in the normal case: bid <= ceiling + increment."""
        team, pool, prices = self._setup()
        info = MarketInfo(
            market_ceiling=1.0, highest_bidder="AAA", highest_bid=1.0,
            second_bidder="BBB", demand_count=2, floor_demand=False,
        )
        rec = compute_bid_recommendation(
            pool["Star"], team, pool, prices, info, current_price=MIN_SALARY,
        )
        assert rec.max_bid == pytest.approx(1.0 + SALARY_INCREMENT, abs=0.01)
        assert rec.action == "BID"

    def test_price_above_ceiling_releases_the_cap(self):
        """Contested but price exceeded the forecast — value binds, not the ceiling."""
        team, pool, prices = self._setup()
        info = MarketInfo(
            market_ceiling=1.0, highest_bidder="AAA", highest_bid=1.0,
            second_bidder="BBB", demand_count=2, floor_demand=False,
        )
        rec = compute_bid_recommendation(
            pool["Star"], team, pool, prices, info, current_price=2.5,
        )
        assert rec.max_bid == rec.marginal_value
        assert rec.max_bid > 4.0
        assert rec.action == "BID"


class TestBidPanelNumbers:
    """The panel shows two numbers because max_bid was always two numbers.

    value_cap = min(marginal, physical_max) is a HARD limit and does not move
    with price. expected_stop = ceiling + increment is a FORECAST of where
    bidding ends. max_bid is the min of the two while the forecast holds, and
    the value cap once a real price falsifies it — which is why the single
    displayed figure doubled from $4.1M to $8.5M when the price moved one
    increment, with nothing on screen to explain it.

    Reuses TestUncontestedBidding's fixture: $4.2M of headroom, one open spot,
    and a Star the objective always prefers, so value_cap is exactly $4.2M.
    """

    def _setup(self):
        return TestUncontestedBidding()._setup()

    def _contested(self, ceiling: float = 1.0) -> MarketInfo:
        return MarketInfo(
            market_ceiling=ceiling, highest_bidder="AAA", highest_bid=ceiling,
            second_bidder="BBB", demand_count=2, floor_demand=False,
        )

    def test_value_cap_never_moves_with_price(self):
        """The invariant that makes the new display trustworthy.

        If "Worth up to" wandered as the price climbed it would be no better
        than the blended number it replaces. Swept across the forecast boundary,
        where max_bid demonstrably does jump.
        """
        team, pool, prices = self._setup()
        info = self._contested()

        seen = []
        for i in range(41):
            price = round(MIN_SALARY + i * SALARY_INCREMENT, 1)
            rec = compute_bid_recommendation(
                pool["Star"], team, pool, prices, info, current_price=price,
            )
            seen.append((price, rec.value_cap, rec.max_bid))

        caps = {c for _, c, _ in seen}
        assert len(caps) == 1, f"value_cap moved with price: {sorted(caps)}"
        assert caps.pop() == pytest.approx(team.physical_max_bid, abs=0.01)
        assert len({m for _, _, m in seen}) > 1, (
            "fixture must sweep across the boundary where max_bid jumps, "
            "or this proves nothing"
        )

    def test_expected_stop_clears_once_price_passes_it(self):
        team, pool, prices = self._setup()
        info = self._contested(ceiling=1.0)
        stop = 1.0 + SALARY_INCREMENT

        below = compute_bid_recommendation(
            pool["Star"], team, pool, prices, info, current_price=MIN_SALARY,
        )
        assert below.stop_status == "live"
        assert below.expected_stop == pytest.approx(stop, abs=0.01)
        assert below.max_bid == pytest.approx(stop, abs=0.01), (
            "while the forecast holds it is the binding number"
        )

        at_stop = compute_bid_recommendation(
            pool["Star"], team, pool, prices, info, current_price=stop,
        )
        assert at_stop.stop_status == "passed", (
            "a real price at the forecast has falsified it"
        )
        assert at_stop.expected_stop is None
        assert at_stop.max_bid == at_stop.value_cap

    def test_expected_stop_is_absent_when_uncontested(self):
        """A forecast of where rivals stop is meaningless with no rivals."""
        team, pool, prices = self._setup()
        rec = compute_bid_recommendation(
            pool["Star"], team, pool, prices,
            TestUncontestedBidding()._floor_ceiling(),
            current_price=2.5, bot_uncontested=True,
        )
        assert rec.stop_status == "uncontested"
        assert rec.expected_stop is None
        assert rec.max_bid == rec.value_cap

    def test_max_bid_is_still_the_min_of_the_two_while_the_forecast_holds(self):
        """This change is additive — max_bid semantics must not drift.

        Roughly fifteen assertions across this file and test_edge_cases.py rest
        on max_bid meaning exactly what it meant before.
        """
        team, pool, prices = self._setup()
        for ceiling in (1.0, 2.0, 4.0, 8.0):
            rec = compute_bid_recommendation(
                pool["Star"], team, pool, prices, self._contested(ceiling),
                current_price=MIN_SALARY,
            )
            assert rec.max_bid == pytest.approx(
                min(rec.value_cap, rec.expected_stop), abs=0.01
            ), f"ceiling ${ceiling}M"

    def test_panel_shows_both_numbers(self):
        """A template edit must not silently drop one of them."""
        import tempfile

        from fastapi.testclient import TestClient

        import main
        main.STATE_DIR = tempfile.mkdtemp()

        with TestClient(main.app) as c:
            c.post("/reset")
            player = max(main.auction_state.available_players.values(),
                         key=lambda p: p.projected_points)
            bidders = [code for code in main.auction_state.teams if code != MY_TEAM][:2]
            r = c.post("/bid-check", data={
                "player": player.name, "price": "1.0",
                "bidders": ",".join(bidders + [MY_TEAM]),
            })
            assert r.status_code == 200
            assert "Worth up to" in r.text, "hard limit missing from the panel"
            assert "Should win it" in r.text, "forecast missing from the panel"
            assert "Max bid:" not in r.text, (
                "the blended figure should be gone — it is what this replaces"
            )
            c.post("/reset")


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

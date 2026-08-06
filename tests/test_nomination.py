"""Tests for nomination engine."""

import pytest

from config import MY_TEAM
from optimizer import recommend_nomination
from market import MarketInfo


def _setup():
    from data_loader import build_initial_state
    from market import compute_all_market_prices, compute_market_ceiling
    from price_model import load_model_params, predict_all_prices

    state = build_initial_state()
    params = load_model_params()
    model_preds = predict_all_prices(state.available_players, params)
    market_data = compute_all_market_prices(
        state.available_players, model_preds, state.teams,
    )
    mp = {name: price for name, (price, _) in market_data.items()}
    model_expected = {name: pred.expected_price for name, pred in model_preds.items()}
    info = compute_market_ceiling(state.teams)
    return state, mp, model_expected, info


def _drain_state(leave_each: float = 3.0):
    """A state where BOT is roster-full and every opponent is ceiling-bound.

    Both squeezes are needed to reach the drain path with anything to measure:

    - BOT full (roster_count == ROSTER_SIZE) makes the MILP want nobody, which
      is the ONLY way `wanted_ufas` empties and the drain branch runs.
    - Opponents need nearly-full rosters *and* a small budget. Starving
      `penalties` alone leaves 24 empty spots, so min_budget_reserved (24 x
      MIN_SALARY = $12M) swallows the budget, physical_max_bid falls below
      MIN_SALARY and every opponent correctly drops out of _bidding_opponents —
      a state with no bidders, which measures nothing.

    Stars stay on the board while nobody can pay for them, so the market ceiling
    binds and model price diverges from what a player can actually fetch.
    """
    from data_loader import build_initial_state
    from market import compute_market_ceiling, compute_market_price
    from price_model import load_model_params, predict_all_prices
    from state import PlayerOnRoster
    from config import ROSTER_SIZE, SALARY_CAP

    state = build_initial_state()
    # Cheapest-first, so the expensive players remain available to nominate.
    filler = sorted(state.available_players.items(), key=lambda kv: kv[1].projected_points)
    supply = iter(filler)

    for code, team in state.teams.items():
        target = ROSTER_SIZE if code == MY_TEAM else 22
        while team.roster_count < target:
            name, p = next(supply)
            state.available_players.pop(name, None)
            team.add_acquired_player(PlayerOnRoster(
                name=p.name, position=p.position, group="3",
                salary=0.5, projected_points=p.projected_points,
            ))
        if code != MY_TEAM:
            team.penalties = round(max(0.0, SALARY_CAP - team.total_salary - leave_each), 1)
        team._invalidate_cache()

    preds = predict_all_prices(state.available_players, load_model_params())
    model = {n: pred.expected_price for n, pred in preds.items()}
    info = compute_market_ceiling(state.teams)
    mp = {n: compute_market_price(model[n], info) for n in model}
    return state, mp, model, info


class TestBiddingOpponents:
    """Demand counts must use the same "can still bid" rule as the ceilings.

    The drain heuristic filtered opponents on total_spots_remaining > 0, so a
    24-man team with cap space was counted as gone — understating how many
    rivals can afford a player, exactly the way it understated market ceilings
    before 4dc59da. Fixing physical_max_bid didn't reach here, because this
    filter never consulted it.
    """

    def test_full_but_cap_rich_opponent_still_counts(self):
        from config import ROSTER_SIZE
        from optimizer import _bidding_opponents
        from state import PlayerOnRoster

        state, _, _, _ = _setup()
        victim = state.teams["SRL"]
        victim.keeper_players = [
            PlayerOnRoster(name=f"S{i}", position="F", group="3",
                           salary=1.0, projected_points=50)
            for i in range(ROSTER_SIZE)
        ]
        victim.acquired_players, victim.minor_players = [], []
        victim._invalidate_cache()  # roster_players is memoized

        assert victim.total_spots_remaining == 0, "fixture must be roster-full"
        assert victim.physical_max_bid > 0, "but still hold cap space"
        assert victim in _bidding_opponents(state), "a full team with money bids"

    def test_broke_opponent_does_not_count(self):
        from config import SALARY_CAP
        from optimizer import _bidding_opponents

        state, _, _, _ = _setup()
        victim = state.teams["SRL"]
        victim.penalties = SALARY_CAP
        victim._invalidate_cache()
        assert victim not in _bidding_opponents(state), "no money, no bid"

    def test_done_opponent_does_not_count(self):
        from optimizer import _bidding_opponents

        state, _, _, _ = _setup()
        victim = state.teams["SRL"]
        victim.is_done = True
        assert victim not in _bidding_opponents(state)


class TestRecommendNomination:
    def test_returns_rfa_and_ufa(self):
        """Should return both an RFA and UFA pick."""
        state, mp, model_expected, info = _setup()
        rfa_pick, ufa_pick = recommend_nomination(state, mp, model_expected, info)
        assert rfa_pick is not None, "Should recommend an RFA"
        assert ufa_pick is not None, "Should recommend a UFA"
        assert rfa_pick.player.is_rfa is True
        assert ufa_pick.player.is_rfa is False

    def test_rfa_pick_has_points(self):
        """RFA pick should have projected points > 0."""
        state, mp, model_expected, info = _setup()
        rfa_pick, _ = recommend_nomination(state, mp, model_expected, info)
        assert rfa_pick is not None
        assert rfa_pick.player.projected_points > 0

    def test_ufa_pick_has_points(self):
        """UFA pick should have projected points > 0."""
        state, mp, model_expected, info = _setup()
        _, ufa_pick = recommend_nomination(state, mp, model_expected, info)
        assert ufa_pick is not None
        assert ufa_pick.player.projected_points > 0

    def test_picks_have_strategy(self):
        """Both picks should have a valid strategy."""
        state, mp, model_expected, info = _setup()
        rfa_pick, ufa_pick = recommend_nomination(state, mp, model_expected, info)
        valid_strategies = {"target", "drain", "depth"}
        assert rfa_pick.strategy in valid_strategies
        assert ufa_pick.strategy in valid_strategies

    def test_picks_have_reasoning(self):
        """Both picks should have non-empty reasoning."""
        state, mp, model_expected, info = _setup()
        rfa_pick, ufa_pick = recommend_nomination(state, mp, model_expected, info)
        assert len(rfa_pick.reasoning) > 0
        assert len(ufa_pick.reasoning) > 0

    def test_no_rfas_returns_none(self):
        """When no RFAs available, rfa_pick should be None."""
        state, mp, model_expected, info = _setup()
        # Remove all RFAs
        rfa_names = [n for n, p in state.available_players.items() if p.is_rfa]
        for name in rfa_names:
            del state.available_players[name]
        rfa_pick, ufa_pick = recommend_nomination(state, mp, model_expected, info)
        assert rfa_pick is None
        assert ufa_pick is not None


class TestDrainStrategy:
    """Drain nominations must be priced in dollars an opponent will actually pay.

    Everything above this class asserts only shape — that a pick exists, has a
    strategy, has non-empty reasoning. Nothing pinned the economics, which is
    how three defects shipped together: the drain branch was the one place in
    the optimizer reading raw model prices, so it showed the panel a price no
    opponent could reach; it broke ceiling ties toward the player handing the
    buyer the most surplus; and `max(can_afford, 1)` scored the unaffordable
    case highest, printing "0 can afford" as a reason TO nominate.

    Four of the five fail against the pre-2026-08-06 heuristic.
    `test_can_afford_count_is_at_least_two` passed even then — it guards the
    invariant the other fixes rest on rather than reproducing a defect.
    """

    def test_expected_price_never_exceeds_market_price(self):
        """The panel renders expected_price as "Expected: ~$X M" — it has to be
        the clearing price, not the model's vacuum price."""
        state, mp, model, info = _drain_state()
        rfa_pick, ufa_pick = recommend_nomination(state, mp, model, info)

        for pick in (rfa_pick, ufa_pick):
            assert pick is not None
            if pick.strategy != "drain":
                continue
            market = mp[pick.player.name]
            assert pick.expected_price == pytest.approx(market), (
                f"{pick.player.name}: panel shows ${pick.expected_price:.1f}M but "
                f"the market can only reach ${market:.1f}M"
            )

    def test_tie_at_ceiling_picks_least_surplus(self):
        """Once the ceiling binds, every player above it drains exactly the
        ceiling. Same cap burned either way — so pick the one that does NOT
        hand a rival a star at a discount."""
        from optimizer import _bidding_opponents

        state, mp, model, info = _drain_state()
        _, ufa_pick = recommend_nomination(state, mp, model, info)
        assert ufa_pick is not None and ufa_pick.strategy == "drain"
        assert _bidding_opponents(state), "fixture must leave someone able to bid"

        ufas = {n: p for n, p in state.available_players.items()
                if not p.is_rfa and p.projected_points > 0}
        best_drain = max(mp[n] for n in ufas)
        tied = [n for n in ufas if mp[n] == pytest.approx(best_drain)]
        assert len(tied) > 1, "fixture must actually produce a tie to break"

        picked = ufa_pick.player.name
        assert mp[picked] == pytest.approx(best_drain), "must still maximise cap drained"

        surplus = lambda n: model[n] - mp[n]
        assert surplus(picked) == pytest.approx(min(surplus(n) for n in tied)), (
            f"{picked} gifts ${surplus(picked):.1f}M of surplus; "
            f"a tied alternative gifts ${min(surplus(n) for n in tied):.1f}M "
            f"for the same ${best_drain:.1f}M drained"
        )

    def test_position_need_never_outranks_dollars(self):
        """When the ceiling does NOT bind, prices differ and the drain pick is
        simply the priciest. The old score multiplied price by a team count, so
        a position more teams still needed could outrank a pricier player —
        measured mid-draft, it took Aho at $7.5M over Vasilevskiy at $7.7M and
        left $0.3M of opponent cap unburned."""
        state, mp, model, info = _drain_state(leave_each=14.0)
        _, ufa_pick = recommend_nomination(state, mp, model, info)
        assert ufa_pick is not None and ufa_pick.strategy == "drain"

        ufas = {n: p for n, p in state.available_players.items()
                if not p.is_rfa and p.projected_points > 0}
        prices = {mp[n] for n in ufas}
        assert len(prices) > 1, (
            "fixture must leave prices unbound by the ceiling, or this asserts nothing"
        )
        assert mp[ufa_pick.player.name] == pytest.approx(max(prices)), (
            f"{ufa_pick.player.name} drains ${mp[ufa_pick.player.name]:.1f}M when "
            f"${max(prices):.1f}M was available"
        )

    def test_reasoning_never_claims_zero_can_afford(self):
        """"0 can afford — drains opponent budgets" is self-contradicting: a
        player nobody can bid on drains nothing."""
        state, mp, model, info = _drain_state()
        rfa_pick, ufa_pick = recommend_nomination(state, mp, model, info)

        for pick in (rfa_pick, ufa_pick):
            if pick and pick.strategy == "drain":
                assert "0 can afford" not in pick.reasoning, pick.reasoning

    def test_can_afford_count_is_at_least_two(self):
        """Structural invariant, not luck: the market price IS the second-highest
        opponent max, so the top two opponents can always reach it. Asserted
        directly because it is what makes the reasoning string trustworthy."""
        from optimizer import _bidding_opponents

        state, mp, model, info = _drain_state()
        _, ufa_pick = recommend_nomination(state, mp, model, info)
        assert ufa_pick is not None and ufa_pick.strategy == "drain"

        opponents = _bidding_opponents(state)
        assert len(opponents) >= 2, "fixture must have at least two bidders"
        price = mp[ufa_pick.player.name]
        can_afford = sum(1 for t in opponents if t.physical_max_bid >= price)
        assert can_afford >= 2, (
            f"only {can_afford} opponent(s) can reach ${price:.1f}M — "
            "market price should be reachable by the top two by construction"
        )

    def test_drain_falls_through_to_depth_below_min_drain_price(self):
        """A star behind a $0.7M ceiling drains $0.7M, not his model price.
        Gating on the model price called that a drain and burned the turn."""
        from config import MIN_DRAIN_PRICE

        state, mp, model, info = _drain_state(leave_each=1.2)
        assert info.market_ceiling < MIN_DRAIN_PRICE, "fixture must starve the market"

        expensive = max(model.values())
        assert expensive > MIN_DRAIN_PRICE, (
            "fixture must leave a player whose MODEL price clears the gate, "
            "so the test distinguishes model-gating from market-gating"
        )

        _, ufa_pick = recommend_nomination(state, mp, model, info)
        assert ufa_pick is not None
        assert ufa_pick.strategy != "drain", (
            f"nobody can pay more than ${info.market_ceiling:.1f}M, but "
            f"{ufa_pick.player.name} was recommended as a drain"
        )


class TestComboNominationTurn:
    """League rule: a turn is 1 RFA (silent) + 1 UFA (open). The nomination
    pointer must advance only when the UFA half sells."""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        import main
        with TestClient(main.app) as c:
            c.post("/reset")
            yield c

    def _nominator(self, client):
        import json as _json
        state = _json.loads(client.get("/state").text)
        import main
        return main.auction_state.current_nominator()

    def test_rfa_sale_keeps_turn_ufa_sale_advances(self, client):
        import main
        first = main.auction_state.current_nominator()

        rfa = next(p for p in main.auction_state.available_players.values() if p.is_rfa)
        client.post("/assign", data={"player": rfa.name, "team": first, "salary": "2.0"})
        assert main.auction_state.current_nominator() == first, (
            "RFA half of a combo must not pass the nomination turn"
        )

        ufa = next(p for p in main.auction_state.available_players.values() if not p.is_rfa)
        client.post("/assign", data={"player": ufa.name, "team": first, "salary": "2.0"})
        assert main.auction_state.current_nominator() != first, (
            "UFA sale completes the combo turn"
        )

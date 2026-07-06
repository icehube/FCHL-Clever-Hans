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

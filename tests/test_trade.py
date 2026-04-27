"""Tests for trade.py: trade evaluator and buyout analyzer."""

import pytest

from config import BUYOUT_PENALTY_RATE, MY_TEAM
from trade import (
    PlayerTrade,
    evaluate_buyout,
    evaluate_trade,
    execute_buyout,
    execute_trade,
)


def _setup():
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
    return state, mp


class TestEvaluateTrade:
    def test_good_trade_recommends_accept(self):
        """Trading a low player for a high player should recommend accept."""
        state, mp = _setup()
        bot = state.teams[MY_TEAM]

        # Find worst keeper by points
        worst = min(bot.keeper_players, key=lambda p: p.projected_points)
        # Find best available player at same position
        best_avail = max(
            (p for p in state.available_players.values()
             if p.position == worst.position and p.projected_points > worst.projected_points),
            key=lambda p: p.projected_points,
        )

        give = [PlayerTrade(worst.name, worst.position, worst.salary, worst.projected_points)]
        receive = [PlayerTrade(best_avail.name, best_avail.position, 2.0, best_avail.projected_points)]

        result = evaluate_trade(state, give, receive, mp)
        assert result.recommendation == "accept"
        assert result.best_scenario.total_points > result.current_scenario.total_points

    def test_bad_trade_recommends_decline(self):
        """Trading a good cheap player for a bad expensive one should decline."""
        state, mp = _setup()
        bot = state.teams[MY_TEAM]

        # Find a good cheap keeper (high pts, low salary)
        best = max(bot.keeper_players, key=lambda p: p.projected_points - p.salary * 10)
        # Receive a 1-point player at high salary — wastes cap and loses points
        give = [PlayerTrade(best.name, best.position, best.salary, best.projected_points)]
        receive = [PlayerTrade("Fake Bad Player", best.position, 10.0, 1)]

        result = evaluate_trade(state, give, receive, mp)
        assert result.recommendation == "decline"

    def test_trade_has_scenarios(self):
        """Trade evaluation should produce at least one scenario."""
        state, mp = _setup()
        bot = state.teams[MY_TEAM]
        worst = min(bot.keeper_players, key=lambda p: p.projected_points)
        best_avail = next(
            p for p in state.available_players.values()
            if p.position == worst.position and p.projected_points > 0
        )

        give = [PlayerTrade(worst.name, worst.position, worst.salary, worst.projected_points)]
        receive = [PlayerTrade(best_avail.name, best_avail.position, 1.0, best_avail.projected_points)]

        result = evaluate_trade(state, give, receive, mp)
        assert len(result.scenarios) >= 1
        assert result.trade_id  # Should have an ID

    def test_auto_buyout_creates_extra_scenarios(self):
        """With auto_check_buyouts, should have buyout scenarios for each received player."""
        state, mp = _setup()
        bot = state.teams[MY_TEAM]
        worst = min(bot.keeper_players, key=lambda p: p.projected_points)

        p1 = next(p for p in state.available_players.values() if p.position == "F" and p.projected_points > 50)
        p2 = next(p for p in state.available_players.values() if p.position == "F" and p.projected_points > 30 and p.name != p1.name)

        give = [PlayerTrade(worst.name, worst.position, worst.salary, worst.projected_points)]
        receive = [
            PlayerTrade(p1.name, p1.position, 3.0, p1.projected_points),
            PlayerTrade(p2.name, p2.position, 1.0, p2.projected_points),
        ]

        result = evaluate_trade(state, give, receive, mp, auto_check_buyouts=True)
        # Should have: keep_all + buyout_p1 + buyout_p2 = 3 scenarios
        assert len(result.scenarios) == 3

    def test_no_buyout_scenarios_when_disabled(self):
        """Without auto_check_buyouts, should have only keep-all scenario."""
        state, mp = _setup()
        bot = state.teams[MY_TEAM]
        worst = min(bot.keeper_players, key=lambda p: p.projected_points)
        avail = next(p for p in state.available_players.values() if p.projected_points > 0)

        give = [PlayerTrade(worst.name, worst.position, worst.salary, worst.projected_points)]
        receive = [PlayerTrade(avail.name, avail.position, 1.0, avail.projected_points)]

        result = evaluate_trade(state, give, receive, mp, auto_check_buyouts=False)
        assert len(result.scenarios) == 1


class TestEvaluateBuyout:
    def test_buyout_penalty_math(self):
        """Buyout penalty should be salary * BUYOUT_PENALTY_RATE."""
        state, mp = _setup()
        bot = state.teams[MY_TEAM]
        player = bot.keeper_players[0]

        result = evaluate_buyout(state, player.name, mp)
        assert result.salary_freed == player.salary
        assert result.penalty_added == pytest.approx(player.salary * BUYOUT_PENALTY_RATE)
        assert result.net_cap_freed == pytest.approx(player.salary * (1 - BUYOUT_PENALTY_RATE))

    def test_buyout_player_not_found(self):
        """Should raise ValueError for non-existent player."""
        state, mp = _setup()
        with pytest.raises(ValueError, match="not found"):
            evaluate_buyout(state, "Nobody McFake", mp)

    def test_buyout_recommends_keep_or_buyout(self):
        """Recommendation should be either 'keep' or 'buyout'."""
        state, mp = _setup()
        bot = state.teams[MY_TEAM]
        player = bot.keeper_players[0]

        result = evaluate_buyout(state, player.name, mp)
        assert result.recommendation in ("keep", "buyout")

    def test_buyout_has_both_rosters(self):
        """Should include both current and buyout MILP solutions."""
        state, mp = _setup()
        bot = state.teams[MY_TEAM]
        player = bot.keeper_players[0]

        result = evaluate_buyout(state, player.name, mp)
        assert result.current_roster.status == "Optimal"
        assert result.buyout_roster.status == "Optimal"


class TestExecuteTrade:
    def test_execute_moves_players(self):
        """Executing a trade should move players between roster and pool."""
        state, mp = _setup()
        bot = state.teams[MY_TEAM]
        keeper = bot.keeper_players[0]
        avail = next(p for p in state.available_players.values() if p.projected_points > 0)

        give = [PlayerTrade(keeper.name, keeper.position, keeper.salary, keeper.projected_points)]
        receive = [PlayerTrade(avail.name, avail.position, 1.0, avail.projected_points)]

        initial_roster_count = bot.roster_count
        execute_trade(state, give, receive)

        # Given player should be in available pool now
        assert keeper.name in state.available_players
        # Received player should be on roster now
        assert bot.find_player(avail.name) is not None
        # Received player should NOT be in available pool
        assert avail.name not in state.available_players
        # Roster count should be the same (1 out, 1 in)
        assert bot.roster_count == initial_roster_count

    def test_execute_two_team_trade_swaps_rosters(self):
        """With source_team_code, players move between rosters -- not via available pool."""
        state, mp = _setup()
        bot = state.teams[MY_TEAM]
        # Pick any non-BOT team that has at least one keeper
        other_code = next(c for c, t in state.teams.items() if c != MY_TEAM and t.keeper_players)
        other = state.teams[other_code]

        bot_give = bot.keeper_players[0]
        other_give = other.keeper_players[0]

        give = [PlayerTrade(bot_give.name, bot_give.position, bot_give.salary, bot_give.projected_points)]
        receive = [PlayerTrade(other_give.name, other_give.position, other_give.salary, other_give.projected_points)]

        bot_count_before = bot.roster_count
        other_count_before = other.roster_count
        avail_before = set(state.available_players.keys())

        execute_trade(state, give, receive, source_team_code=other_code)

        # BOT lost the give player, gained the receive player
        assert bot.find_player(bot_give.name) is None
        assert bot.find_player(other_give.name) is not None
        # Source team mirror: gained the give, lost the receive
        assert other.find_player(bot_give.name) is not None
        assert other.find_player(other_give.name) is None
        # Roster counts unchanged on both sides
        assert bot.roster_count == bot_count_before
        assert other.roster_count == other_count_before
        # Neither traded player touched the available pool
        assert set(state.available_players.keys()) == avail_before

    def test_execute_rejects_self_trade(self):
        state, mp = _setup()
        with pytest.raises(ValueError, match="self"):
            execute_trade(state, [], [], source_team_code=MY_TEAM)


class TestExecuteBuyout:
    def test_execute_removes_player_adds_penalty(self):
        """Buyout should remove player and add penalty."""
        state, mp = _setup()
        bot = state.teams[MY_TEAM]
        player = bot.keeper_players[0]
        salary = player.salary

        initial_penalties = bot.penalties
        initial_count = bot.roster_count

        execute_buyout(state, player.name)

        assert bot.find_player(player.name) is None
        assert bot.penalties == pytest.approx(initial_penalties + salary * BUYOUT_PENALTY_RATE)
        assert bot.roster_count == initial_count - 1

    def test_execute_buyout_not_found(self):
        """Should raise ValueError for non-existent player."""
        state, mp = _setup()
        with pytest.raises(ValueError, match="not found"):
            execute_buyout(state, "Ghost Player")

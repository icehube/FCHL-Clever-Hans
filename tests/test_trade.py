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


class TestTradeLegalityGuards:
    """Regression (2026-07-05 review): the evaluator recommended ACCEPT on
    cap-violating trades because scenarios compared raw points and ignored
    MILP status / negative cap."""

    def test_cap_violating_trade_declined(self):
        state, mp = _setup()
        bot = state.teams[MY_TEAM]

        # Nearly exhaust BOT's cap so any big incoming salary busts it
        bot.penalties += bot.remaining_budget - 1.0
        assert bot.remaining_budget == pytest.approx(1.0)

        cheap = min(bot.keeper_players, key=lambda p: p.salary)
        star = max(state.available_players.values(), key=lambda p: p.projected_points)
        give = [PlayerTrade(cheap.name, cheap.position, cheap.salary, cheap.projected_points)]
        receive = [PlayerTrade(star.name, star.position, 11.0, star.projected_points)]

        result = evaluate_trade(state, give, receive, mp)
        assert result.recommendation == "decline"
        assert "over the cap" in result.reasoning or "unsolvable" in result.reasoning

    def test_two_team_receive_preserves_group_and_salary(self):
        state, mp = _setup()
        bot = state.teams[MY_TEAM]
        other_code = next(c for c in state.teams if c != MY_TEAM)
        other = state.teams[other_code]

        src = other.keeper_players[0]
        src_group, src_salary = src.group, src.salary
        give_p = bot.keeper_players[0]

        execute_trade(
            state,
            give=[PlayerTrade(give_p.name, give_p.position, give_p.salary, give_p.projected_points)],
            # Deliberately wrong client-supplied salary — roster value must win
            receive=[PlayerTrade(src.name, src.position, 99.0, src.projected_points)],
            source_team_code=other_code,
        )
        arrived = bot.find_player(src.name)
        assert arrived is not None
        assert arrived.group == src_group
        assert arrived.salary == src_salary


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
        # keep_all + one per eligible contract on the POST-trade roster: both
        # received players (fresh draftees are group 3) and BOT's own.
        eligible_after = (
            sum(1 for q in bot.all_players if q.can_be_bought_out and q.name != worst.name)
            + len(receive)
        )
        assert len(result.scenarios) == 1 + eligible_after
        assert {p1.name, p2.name} <= {b for s in result.scenarios for b in s.buyouts}

    def test_two_team_trade_does_not_reuse_give_in_milp(self):
        """Free-agent flow can re-acquire give-player; two-team flow cannot."""
        state, mp = _setup()
        bot = state.teams[MY_TEAM]
        other_code = next(c for c, t in state.teams.items() if c != MY_TEAM and t.keeper_players)
        bot_give = max(bot.keeper_players, key=lambda p: p.projected_points)
        other_give = state.teams[other_code].keeper_players[0]
        give = [PlayerTrade(bot_give.name, bot_give.position, bot_give.salary, bot_give.projected_points)]
        receive = [PlayerTrade(other_give.name, other_give.position, other_give.salary, other_give.projected_points)]

        fa = evaluate_trade(state, give, receive, mp, source_team_code=None)
        two = evaluate_trade(state, give, receive, mp, source_team_code=other_code)

        fa_keep = next(s for s in fa.scenarios if s.description == "Keep all received players")
        two_keep = next(s for s in two.scenarios if s.description == "Keep all received players")
        assert fa_keep.total_points >= two_keep.total_points, (
            "Free-agent simulation can rebuy bot_give and should be at least as good "
            "as two-team simulation, where bot_give is unavailable."
        )

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


class TestTheNoTradeSideGetsBuyoutsToo:
    """Regression (2026-09-25): buyouts were tried on the trade side only.

    A trade that merely moved one of BOT's own bad contracts scored the salary
    relief as a trade gain, because the bar it had to clear was the bare
    current roster. On the live draft: give an overpaid goalie, receive a
    forward, buy the forward out -> ACCEPT at +7, while buying the goalie out
    with no trade at all was +10.
    """

    def _salary_dump(self):
        """BOT's overpaid contract X for a rival's worse player Y at a HIGHER salary.

        Both salaries are SUPPLIED rather than found, because the pool is
        replaced before every draft and need not carry a bad contract. Built so
        the old comparison provably accepts: trading X and buying Y out frees
        less cap than buying X out directly, and costs the same roster spot.
        """
        state, _ = _setup()
        bot = state.teams[MY_TEAM]
        rival_code, rival = next(
            (c, t) for c, t in state.teams.items()
            if c != MY_TEAM and any(q.can_be_bought_out for q in t.roster_players)
        )
        y = min((q for q in rival.roster_players if q.can_be_bought_out),
                key=lambda q: q.projected_points)
        x = min((q for q in bot.roster_players
                 if q.can_be_bought_out and q.projected_points > y.projected_points),
                key=lambda q: q.projected_points)
        x.salary = 6.0
        y.salary = 6.4

        from market import compute_all_market_prices
        from price_model import load_model_params, predict_all_prices
        model = predict_all_prices(state.available_players, load_model_params())
        mp = {n: pr for n, (pr, _) in compute_all_market_prices(
            state.available_players, model, state.teams).items()}
        give = [PlayerTrade(x.name, x.position, x.salary, x.projected_points)]
        receive = [PlayerTrade(y.name, y.position, y.salary, y.projected_points)]
        return state, mp, give, receive, rival_code, x

    def test_a_salary_dump_loses_to_buying_the_contract_out_yourself(self):
        state, mp, give, receive, rival_code, x = self._salary_dump()

        result = evaluate_trade(state, give, receive, mp, source_team_code=rival_code)

        # The precondition that makes this a regression test: measured against
        # the bare roster, the trade looks like a win. Without it this would
        # pass against the old code too.
        assert result.best_scenario.total_points > result.current_scenario.total_points
        assert result.recommendation == "decline", result.reasoning
        assert result.baseline_best.buyouts, "the bar should be a no-trade buyout"
        assert result.baseline_best.buyouts[0] in result.reasoning

    def test_the_verdict_table_names_the_move_that_beat_the_trade(self):
        from main import templates
        state, mp, give, receive, rival_code, x = self._salary_dump()
        result = evaluate_trade(state, give, receive, mp, source_team_code=rival_code)

        html = templates.env.get_template("partials/trade_verdict.html").render(
            trade_result=result)
        bar = result.baseline_best.description
        assert bar in html
        highlighted = [row for row in html.split("<tr") if "text-accent" in row]
        assert len(highlighted) == 1 and bar in highlighted[0], (
            "the highlighted row must be the side that WON, not the trade's best"
        )

    def test_the_trade_side_may_buy_out_a_player_bot_already_had(self):
        state, mp = _setup()
        bot = state.teams[MY_TEAM]
        mine = next(q for q in bot.roster_players if q.can_be_bought_out)
        avail = max((p for p in state.available_players.values() if p.position == "F"),
                    key=lambda p: p.projected_points)
        result = evaluate_trade(
            state, [], [PlayerTrade(avail.name, avail.position, 1.0, avail.projected_points)], mp)
        assert any(s.buyouts == [mine.name] for s in result.scenarios)

    def test_the_no_trade_menu_is_exactly_the_eligible_contracts(self):
        state, mp = _setup()
        bot = state.teams[MY_TEAM]
        worst = min(bot.keeper_players, key=lambda p: p.projected_points)
        avail = next(p for p in state.available_players.values() if p.projected_points > 0)
        result = evaluate_trade(
            state,
            [PlayerTrade(worst.name, worst.position, worst.salary, worst.projected_points)],
            [PlayerTrade(avail.name, avail.position, 1.0, avail.projected_points)], mp)
        assert [s.buyouts[0] for s in result.baseline_scenarios] == [
            q.name for q in bot.all_players if q.can_be_bought_out
        ]

    def test_disabling_buyouts_disables_both_sides(self):
        state, mp = _setup()
        bot = state.teams[MY_TEAM]
        worst = min(bot.keeper_players, key=lambda p: p.projected_points)
        avail = next(p for p in state.available_players.values() if p.projected_points > 0)
        result = evaluate_trade(
            state,
            [PlayerTrade(worst.name, worst.position, worst.salary, worst.projected_points)],
            [PlayerTrade(avail.name, avail.position, 1.0, avail.projected_points)], mp,
            auto_check_buyouts=False)
        assert result.baseline_scenarios == []
        assert result.baseline_best is result.current_scenario

    def test_in_parallel_it_agrees_with_itself_in_series(self):
        state, mp, give, receive, rival_code, _ = self._salary_dump()

        def run(workers):
            from copy import deepcopy
            r = evaluate_trade(deepcopy(state), deepcopy(give), deepcopy(receive), mp,
                               source_team_code=rival_code, workers=workers)
            return [(s.description, s.total_points, s.cap_remaining)
                    for s in [r.current_scenario, *r.scenarios, *r.baseline_scenarios]]

        assert run(4) == run(1)


class TestATieIsJudgedOnWhatIsLeftAfterThePlan:
    """Regression (2026-09-25): a points tie was broken on `cap_remaining`.

    Dobson + Gustavsson for Kyrou-then-buyout read ACCEPT, "same points but
    frees $1.6M", against buying Gustavsson out alone. The trade opened one
    more roster spot, and the solver spent exactly that $1.6M filling it with a
    Dobson-alike. Both plans spent every dollar. A tie is now EVEN, and the
    tie-break is `left_after_plan`, which nets out what the plan buys.

    The solves are STUBBED so each case can be a tie by construction -- the
    pool is replaced before every draft and will not reliably produce one.
    """

    def _evaluate(self, monkeypatch, outcomes):
        """`outcomes(mine, incoming)` maps a description prefix to
        (points, cap_remaining, plan_cost). A callable, because the players are
        derived from the pool here and the caller cannot name them in advance.

        Anything unlisted scores 0 so it can never win.
        """
        import trade
        from optimizer import MILPSolution

        def fake(jobs, market_prices, workers):
            out = []
            for description, _team, _pool, buyouts in jobs:
                pts, cap, cost = next(
                    (v for k, v in table.items() if description.startswith(k)),
                    (0, 1.0, 0.0),
                )
                sol = MILPSolution(total_points=pts, roster=[], total_cost=cost,
                                   by_position={}, status="Optimal")
                out.append(trade.TradeScenario(description, pts, cap, sol, buyouts))
            return out

        state, mp = _setup()
        bot = state.teams[MY_TEAM]
        mine = next(q for q in bot.roster_players if q.can_be_bought_out)
        other = next(q for q in bot.roster_players
                     if q.can_be_bought_out and q.name != mine.name)
        incoming = max(state.available_players.values(), key=lambda p: p.projected_points)
        give = [PlayerTrade(q.name, q.position, q.salary, q.projected_points)
                for q in (mine, other)]
        receive = [PlayerTrade(incoming.name, incoming.position, 4.0,
                               incoming.projected_points)]
        table = outcomes(mine, incoming)
        monkeypatch.setattr(trade, "_solve_jobs", fake)
        return evaluate_trade(state, give, receive, mp), mine, incoming

    def test_more_cap_that_the_plan_spends_is_not_a_win(self, monkeypatch):
        """The Dobson case: $1.6M more cap, all of it spent on the extra spot."""
        result, mine, incoming = self._evaluate(monkeypatch, lambda mine, incoming: {
            "Current roster": (1341, 40.3, 40.3),
            f"No trade, buy out {mine.name}": (1351, 41.9, 41.9),
            f"Trade + buy out {incoming.name}": (1351, 43.5, 43.5),
        })
        assert result.recommendation == "even", result.reasoning
        assert "spends the same" in result.reasoning
        assert "more unspent" not in result.reasoning

    def test_a_tie_that_genuinely_leaves_money_over_is_even_and_says_so(self, monkeypatch):
        result, mine, incoming = self._evaluate(monkeypatch, lambda mine, incoming: {
            "Current roster": (1341, 40.3, 40.3),
            f"No trade, buy out {mine.name}": (1351, 41.9, 41.9),
            f"Trade + buy out {incoming.name}": (1351, 43.5, 42.5),
        })
        assert result.recommendation == "even"
        assert "$1.0M more unspent" in result.reasoning

    def test_a_tie_that_costs_money_is_declined(self, monkeypatch):
        result, mine, incoming = self._evaluate(monkeypatch, lambda mine, incoming: {
            "Current roster": (1341, 40.3, 40.3),
            f"No trade, buy out {mine.name}": (1351, 41.9, 40.9),
            f"Trade + buy out {incoming.name}": (1351, 43.5, 43.5),
        })
        assert result.recommendation == "decline", result.reasoning

    def test_buying_out_everything_received_is_called_a_salary_dump(self, monkeypatch):
        result, mine, incoming = self._evaluate(monkeypatch, lambda mine, incoming: {
            "Current roster": (1341, 40.3, 40.3),
            f"Trade + buy out {incoming.name}": (1360, 43.5, 43.5),
        })
        assert result.recommendation == "accept"
        assert result.reasoning.startswith("Salary dump")

    def test_keeping_what_you_receive_is_not_a_salary_dump(self, monkeypatch):
        result, mine, incoming = self._evaluate(monkeypatch, lambda mine, incoming: {
            "Current roster": (1341, 40.3, 40.3),
            "Keep all received": (1360, 40.0, 40.0),
        })
        assert result.recommendation == "accept"
        assert "Salary dump" not in result.reasoning

    def test_an_even_verdict_renders_amber_with_both_sides_highlighted(self, monkeypatch):
        from main import templates
        result, mine, incoming = self._evaluate(monkeypatch, lambda mine, incoming: {
            "Current roster": (1341, 40.3, 40.3),
            f"No trade, buy out {mine.name}": (1351, 41.9, 41.9),
            f"Trade + buy out {incoming.name}": (1351, 43.5, 43.5),
        })
        html = templates.env.get_template("partials/trade_verdict.html").render(
            trade_result=result)
        assert "text-warning\">EVEN" in html
        assert "/trade-execute" in html, "an even trade is still the owner's call"
        highlighted = [row for row in html.split("<tr") if "text-accent" in row]
        assert len(highlighted) == 2


class TestAReleasedPlayerIsNotFreeToBuyBack:
    """Regression (2026-09-25): the free-agent flow returned given players to
    the pool with no PRICE, and the MILP prices a missing name at MIN_SALARY.

    So a give-for-nothing trade with no partner selected let the plan buy the
    same players straight back at $0.5M: Larkin + Dobson measured a bogus +35.
    """

    def test_buying_him_back_costs_his_market_price(self):
        from config import MIN_SALARY
        state, mp = _setup()
        bot = state.teams[MY_TEAM]
        # The first of BOT's scorers the plan wants back. The guard below is
        # what keeps this from passing vacuously on a pool where nobody is.
        for q in sorted(bot.roster_players, key=lambda q: -q.projected_points):
            result = evaluate_trade(
                state, [PlayerTrade(q.name, q.position, q.salary, q.projected_points)],
                [], mp, auto_check_buyouts=False)
            keep = result.scenarios[0].roster
            if any(r.name == q.name for r in keep.roster):
                break
        else:
            pytest.fail("the plan bought none of BOT's released players back")

        implied = keep.total_cost - sum(
            mp[r.name] for r in keep.roster if r.name != q.name)
        assert implied > MIN_SALARY + 0.05, (
            f"{q.name} was bought back for ${implied:.2f}M -- the minimum, "
            "i.e. he re-entered the pool without a price"
        )


class TestTheOverpayStress:
    """Every plan buys its open spots at the model's EXPECTED price, and the
    one replayed draft paid 27% over the model. The stress solves re-run the two
    winners with prices marked up and say whether the verdict survives.
    """

    def _trade(self):
        state, mp = _setup()
        bot = state.teams[MY_TEAM]
        worst = min(bot.keeper_players, key=lambda p: p.projected_points)
        best_avail = max(
            (p for p in state.available_players.values() if p.position == worst.position),
            key=lambda p: p.projected_points)
        give = [PlayerTrade(worst.name, worst.position, worst.salary, worst.projected_points)]
        receive = [PlayerTrade(best_avail.name, best_avail.position, 2.0,
                               best_avail.projected_points)]
        return state, mp, give, receive

    def test_no_markup_reproduces_the_unstressed_answer(self):
        """The mechanism check: at 1.0x the stress solves ARE the verdict's
        solves, so any difference means they re-solved the wrong scenarios."""
        state, mp, give, receive = self._trade()
        r = evaluate_trade(state, give, receive, mp, overpay=1.0)
        assert r.stress_trade_points == r.best_scenario.total_points
        assert r.stress_baseline_points == r.baseline_best.total_points

    def test_dearer_prices_never_buy_more_points(self):
        state, mp, give, receive = self._trade()
        r = evaluate_trade(state, give, receive, mp)
        assert r.stress_rate > 1.0
        assert r.stress_trade_points <= r.best_scenario.total_points
        assert r.stress_baseline_points <= r.baseline_best.total_points
        assert r.overpay_outcome in ("ahead", "level", "behind")

    def test_an_accept_that_does_not_survive_is_flagged(self):
        from main import templates
        state, mp, give, receive = self._trade()
        r = evaluate_trade(state, give, receive, mp)
        render = templates.env.get_template("partials/trade_verdict.html").render
        r.recommendation = "accept"
        r.stress_trade_points, r.stress_baseline_points = 1270, 1276
        assert "Falls behind" in render(trade_result=r)
        r.stress_trade_points, r.stress_baseline_points = 1280, 1276
        html = render(trade_result=r)
        assert "Still ahead" in html and "Falls behind" not in html
        r.stress_trade_points, r.stress_baseline_points = 1276, 1276
        html = render(trade_result=r)
        assert "Still level" in html and "Falls behind" not in html, (
            "a tie at stressed prices is not falling behind"
        )

    def test_a_tight_budget_can_still_be_stressed(self):
        """Barely enough to fill the roster at the minimum. Marking the FLOOR
        up would make that roster unfillable; marking up only what sits above
        it cannot, because the cheapest players stay the cheapest."""
        from config import ROSTER_SIZE
        state, mp, _, _ = self._trade()
        bot = state.teams[MY_TEAM]
        spots = ROSTER_SIZE - bot.roster_count
        # $0.58M a spot: the pool has hundreds of near-floor players at every
        # position, so this fills -- and a marked-up floor ($0.625M) does not.
        bot.penalties += bot.remaining_budget - spots * 0.58
        r = evaluate_trade(state, [], [], mp)
        assert r.current_scenario.roster.status == "Optimal", "precondition"
        assert r.stress_trade_points is not None, (
            "the stressed roster could not be filled -- the floor was marked up"
        )

    def test_an_unsolvable_stress_is_not_compared(self, monkeypatch):
        """If a stress solve is not Optimal its points are the bare roster's,
        not a plan -- so the outcome must be unknown, not a verdict."""
        import trade
        from optimizer import MILPSolution
        real = trade._solve_jobs
        calls = []

        def second_call_infeasible(jobs, prices, workers):
            calls.append(1)
            out = real(jobs, prices, workers)
            if len(calls) == 2:
                out[0].roster = MILPSolution(0, [], 0.0, {}, "Infeasible")
            return out

        monkeypatch.setattr(trade, "_solve_jobs", second_call_infeasible)
        state, mp, give, receive = self._trade()
        r = evaluate_trade(state, give, receive, mp, auto_check_buyouts=False)
        assert len(calls) == 2
        assert r.overpay_outcome is None


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
        with pytest.raises(ValueError, match="Cannot trade with self"):
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


class TestBuyingOutAnotherTeamsPlayer:
    """CBA 11.4 is not a BOT-only rule, and this tool is the league's record.

    Until 2026-09-12 `execute_buyout` read `state.teams[MY_TEAM]`, so a rival's
    buyout could not be entered at all — and an unrecorded one leaves that
    team's cap wrong in everything the app computes about them, the market
    ceiling included.
    """

    def _eligible_on(self, state, code):
        return next(p for p in state.teams[code].all_players if p.can_be_bought_out)

    def test_the_penalty_lands_on_that_team_and_not_on_bot(self):
        state, mp = _setup()
        rival = state.teams["SRL"]
        bot = state.teams[MY_TEAM]
        victim = self._eligible_on(state, "SRL")
        before = (rival.penalties, bot.penalties, rival.roster_count)

        execute_buyout(state, victim.name, "SRL")

        assert rival.find_player(victim.name) is None
        assert rival.penalties == pytest.approx(
            before[0] + victim.salary * BUYOUT_PENALTY_RATE
        )
        assert bot.penalties == before[1], "the penalty landed on the wrong cap"
        assert rival.roster_count == before[2] - 1

    def test_a_player_on_another_roster_is_refused(self):
        """The guard that makes the team argument mean something.

        `find_player` runs against the NAMED team, so asking SRL to buy out one
        of BOT's players has to refuse. A lookup that searched the league would
        buy out the right player from the wrong cap — a silent, plausible-looking
        corruption of two teams at once.
        """
        state, mp = _setup()
        mine = self._eligible_on(state, MY_TEAM)
        before = (state.teams["SRL"].penalties, state.teams[MY_TEAM].penalties)

        with pytest.raises(ValueError, match="SRL"):
            execute_buyout(state, mine.name, "SRL")

        assert state.teams[MY_TEAM].find_player(mine.name) is not None
        assert (state.teams["SRL"].penalties, state.teams[MY_TEAM].penalties) == before

    def test_an_unknown_team_is_refused(self):
        state, mp = _setup()
        with pytest.raises(ValueError, match="Unknown team"):
            execute_buyout(state, self._eligible_on(state, MY_TEAM).name, "NOPE")

    def test_eligibility_is_not_a_function_of_who_owns_him(self):
        """Group A-E on a rival is as ineligible as group A-E on BOT."""
        state, mp = _setup()
        prospect = next(
            p for p in state.teams["SRL"].all_players if not p.can_be_bought_out
        )
        before = state.teams["SRL"].penalties
        with pytest.raises(ValueError, match="cannot be bought out"):
            execute_buyout(state, prospect.name, "SRL")
        assert state.teams["SRL"].find_player(prospect.name) is not None
        assert state.teams["SRL"].penalties == before

    def test_the_default_is_still_bot(self):
        """Two callers still omit it — the Analyzer's Execute button and the
        older tests above. A default that had drifted would move the penalty
        somewhere else entirely.
        """
        state, mp = _setup()
        victim = self._eligible_on(state, MY_TEAM)
        before = state.teams[MY_TEAM].penalties
        execute_buyout(state, victim.name)
        assert state.teams[MY_TEAM].penalties == pytest.approx(
            before + victim.salary * BUYOUT_PENALTY_RATE
        )

"""Tests for scenarios.py."""

import re
from pathlib import Path

import pytest

import market
import optimizer
import scenarios
from config import (
    BACKUP_TARGETS,
    MAX_SALARY,
    MIN_SALARY,
    MY_TEAM,
    POSITION_MINIMUMS,
    ROSTER_SIZE,
    SALARY_CAP,
    SALARY_INCREMENT,
)
from data_loader import build_initial_state
from price_model import load_model_params, predict_all_prices

ENDGAME = "endgame-ceiling-binds"
LAST_GOALIE = "endgame-last-goalie"
SOLE_BIDDER = "endgame-sole-bidder"
LATE_DRAFT = "drained-late-draft"
FULL_ROSTER = "full-roster-still-bidding"


def _priced(state):
    """(model price, market price, MarketInfo) for a loaded scenario.

    Rebuilds what `main._recompute` builds, so these tests measure what the
    panels would actually render rather than a private restatement of it.
    """
    predictions = predict_all_prices(state.available_players, load_model_params())
    all_market = market.compute_all_market_prices(
        state.available_players, predictions, state.teams
    )
    model = {n: round(p.expected_price, 1) for n, p in predictions.items()}
    live = {n: price for n, (price, _) in all_market.items()}
    return model, live, market.compute_market_ceiling(state.teams)


def test_goalie_asymmetry_non_bot_have_two_goalies():
    state = scenarios.load("goalie-asymmetry")
    for code, team in state.teams.items():
        if code == MY_TEAM:
            continue
        goalies = [p for p in team.roster_players if p.position == "G"]
        assert len(goalies) >= 2, f"{code} only has {len(goalies)} goalies"


def test_goalie_asymmetry_assigned_at_min_salary():
    state = scenarios.load("goalie-asymmetry")
    for code, team in state.teams.items():
        if code == MY_TEAM:
            continue
        for p in team.acquired_players:
            if p.position == "G":
                assert p.salary == 0.5


def test_unknown_scenario_raises():
    with pytest.raises(KeyError):
        scenarios.load("not-a-scenario")


class TestEndgameCeilingBinds:
    """The state a fresh reset cannot reach: the ceiling actually capping prices.

    On `/reset` all 11 teams sit at `physical_max_bid` = MAX_SALARY, so the
    ceiling IS the cap, every bid reports `stop_status = at_cap` with no forecast,
    and no row in Available Players is `capped`. Three separate backlog findings
    were parked on that — including the app's only `tooltip-left`, which renders
    solely on a capped row and had therefore never been placement-checked.

    Numbers here are FLOORS and bands, never the measured value. `players.csv` is
    replaced before every draft, which moves every price in the pool; an exact
    assertion would be a fingerprint test in the wrong file.
    """

    def test_the_ceiling_stops_being_the_salary_cap(self):
        _, _, info = _priced(scenarios.load(ENDGAME))
        assert MIN_SALARY <= info.market_ceiling < MAX_SALARY, (
            f"ceiling is ${info.market_ceiling}M — the whole point of this "
            f"scenario is a ceiling BELOW ${MAX_SALARY}M, where a forecast exists"
        )

    def test_the_two_live_opponents_do_not_tie(self):
        """`second_bidder` is the ceiling, so a tie hides which team sets it.

        Not cosmetic: the first version of `_drain` bought the most expensive
        affordable player each time, overshot both targets, and landed both live
        opponents on the same $0.9M max from targets of $3.0M and $2.2M.
        """
        state = scenarios.load(ENDGAME)
        live = sorted(
            state.teams[c].physical_max_bid
            for c, t in state.teams.items()
            if not t.is_done and c != MY_TEAM
        )
        assert len(live) == 2, f"expected 2 live opponents, got {len(live)}"
        assert live[0] < live[1], (
            f"both live opponents max out at ${live[0]}M, so the ceiling and the "
            f"top bid are the same number and second_bidder means nothing"
        )

    def test_stars_are_still_unsold_above_the_ceiling(self):
        """The capped branch needs something to render, which is the hard part.

        Two earlier attempts produced ZERO capped rows: draining top-down removes
        exactly the players the ceiling would cap, and reserving the top 40
        reserves everything over $3.0M (534 of 705 players sit at the floor,
        where floor means `round(expected_price, 1) == 0.5`), so teams fill up
        on floor-priced depth and stay rich.

        Measured against the DRAFT-TIME pool, not the surviving one. The first
        version of this asked whether `max(available)` was expensive, which is
        very nearly a tautology — whatever is left is by definition the richest
        thing left, so it passed with the reserved set deleted entirely and the
        biggest name in the league sold off. That mutant survived all 13 tests.
        """
        fresh_model, _, _ = _priced(build_initial_state())
        stars = sorted(fresh_model, key=lambda n: (-fresh_model[n], n))[:5]

        state = scenarios.load(ENDGAME)
        model, live, info = _priced(state)

        sold = [n for n in stars if n not in state.available_players]
        assert not sold, (
            f"{sold} were bought during setup — the point of holding back the top "
            f"of the pool is that the players nobody can afford are STILL THERE "
            f"to be priced at the ceiling"
        )
        for star in stars:
            assert model[star] > 2 * info.market_ceiling, (
                f"{star} is ${model[star]}M against a ${info.market_ceiling}M "
                f"ceiling — not a convincing endgame"
            )
            assert live[star] == pytest.approx(info.market_ceiling), (
                f"{star} should price at the ceiling, not ${live[star]}M"
            )

    def test_a_substantial_share_of_the_pool_is_capped(self):
        """Counted the way the PANEL counts, which is not the same number.

        This asserted a raw `live < model` for months, and raw is the wrong
        definition for the claim it makes: the tooltip and the strike-through
        render off `market.is_capped`, which quantizes to the one decimal both
        panels print. Measured 2026-08-18 on this state — **83 raw against 28
        quantized** of 677, because 55 of the 83 differ by less than a cent and
        render as two identical figures. The old floor of 40 sat between the two,
        so it passed only by counting rows that show nothing.

        Same rule, one definition, for the same reason `market.is_capped` exists
        at all (2026-08-18, `2176a56`). `TestPriceColumn`'s hand-written copy is
        deliberate and different: it asserts an equivalence against rendered
        markup, where importing the predicate would make the test a tautology.
        """
        state = scenarios.load(ENDGAME)
        model, live, _ = _priced(state)
        capped = [n for n in model if market.is_capped(model[n], live[n])]
        assert len(capped) >= 20, (
            f"only {len(capped)} of {len(model)} players are capped — the "
            f"tooltip-left in bid_limits.html renders per capped row, so a "
            f"handful makes the placement check a coin flip"
        )

    def test_bot_can_still_plan_and_still_bid(self):
        """An endgame BOT cannot act in is a scenario that tests nothing.

        If the MILP goes Infeasible every panel degrades to floor values and the
        advisor stops answering, which would make this scenario worse than
        `/reset` rather than better.
        """
        state = scenarios.load(ENDGAME)
        _, live, _ = _priced(state)
        bot = state.teams[MY_TEAM]
        solution = optimizer.solve_optimal_roster(bot, state.available_players, live)
        assert solution.status == "Optimal", (
            f"BOT's MILP is {solution.status} — the advisor cannot answer"
        )
        assert bot.physical_max_bid >= MIN_SALARY, "BOT cannot afford any bid"
        assert not bot.is_done, "BOT must still be drafting"

    def test_the_forecast_is_live_rather_than_at_cap(self):
        """The other half of what a fresh state cannot reach.

        With the ceiling at MAX_SALARY, `expected_stop` is suppressed and
        `stop_status` reads `at_cap` for every player in the pool. Below the cap
        it becomes a real figure — so this is the only state in which the panel's
        "Should win it" number renders at all.
        """
        state = scenarios.load(ENDGAME)
        model, live, info = _priced(state)
        richest = max(model, key=lambda n: model[n])
        rec = optimizer.compute_bid_recommendation(
            state.available_players[richest], state.teams[MY_TEAM],
            state.available_players, live, info, current_price=MIN_SALARY,
        )
        assert rec.stop_status == "live", (
            f"stop_status is {rec.stop_status!r}, so the panel still shows no "
            f"forecast and this scenario buys nothing over /reset"
        )
        assert rec.expected_stop is not None

    def test_done_teams_leave_the_demand_count(self):
        state = scenarios.load(ENDGAME)
        _, _, info = _priced(state)
        done = [c for c, t in state.teams.items() if t.is_done]
        assert len(done) == len(state.teams) - 3, (
            f"expected all but BOT and two live opponents to be done, got {done}"
        )
        assert MY_TEAM not in done, "BOT must never be marked done by a scenario"
        assert info.demand_count == 2, (
            f"demand_count is {info.demand_count} — done teams are supposed to "
            f"be excluded from market calculations entirely"
        )

    def test_loading_it_twice_gives_the_same_state(self):
        """Ties break on name for this reason; the tests below would flake without it."""
        first, second = scenarios.load(ENDGAME), scenarios.load(ENDGAME)
        for code in first.teams:
            a, b = first.teams[code], second.teams[code]
            assert [(p.name, p.salary) for p in a.all_players] == \
                   [(p.name, p.salary) for p in b.all_players], f"{code} differs"
            assert a.is_done == b.is_done, f"{code} is_done differs"
        assert first.available_players.keys() == second.available_players.keys()


class TestEndgameLastGoalie:
    """One spot, a goalie need, and nothing in the pool BOT can afford instead.

    Two engine semantics meet here and both used to be pinned only against
    synthetic players: a must-have is valued at the physical max, and forcing a
    player into the LAST spot is Optimal rather than Infeasible.

    Figures are derived from the loaded state, never quoted: `players.csv` is
    replaced before every draft, which moves every price in the pool.
    """

    def _target(self, state, price):
        """The must-have: the only goalie left that BOT can pay for."""
        affordable = [
            n for n, p in state.available_players.items()
            if p.position == "G" and price[n] <= state.teams[MY_TEAM].remaining_budget
        ]
        assert len(affordable) == 1, (
            f"{len(affordable)} affordable goalies ({affordable}) — with more "
            f"than one, excluding any single goalie still leaves a legal roster "
            f"and there is no must-have to value"
        )
        return affordable[0]

    def test_bot_has_one_spot_and_needs_a_goalie(self):
        bot = scenarios.load(LAST_GOALIE).teams[MY_TEAM]
        assert bot.total_spots_remaining == 1, (
            f"{bot.total_spots_remaining} spots left — the `spots == 0` branch "
            f"only fires when the forced player fills the roster exactly"
        )
        assert bot.roster_needs == {"F": 0, "D": 0, "G": 1}, (
            f"needs are {bot.roster_needs}; anything else and the MILP can fill "
            f"the last seat with a skater, so no goalie is a must-have"
        )
        assert not bot.is_done

    def test_bots_budget_is_below_the_league_maximum(self):
        """Otherwise the claim this scenario makes cannot be measured.

        `physical_max_bid` clamps at MAX_SALARY, so a must-have on a team sitting
        at the cap returns the same number whether the must-have branch fired or
        the clamp did — and it is WHICH that this scenario exists to show.
        """
        bot = scenarios.load(LAST_GOALIE).teams[MY_TEAM]
        assert MIN_SALARY <= bot.physical_max_bid < MAX_SALARY, (
            f"BOT's physical max is ${bot.physical_max_bid}M"
        )

    def test_the_goalies_still_on_the_board_are_out_of_reach(self):
        """The pool keeps goalies — they just cost more than BOT's whole budget.

        This is the difference between "goaltending is gone" and the state a real
        endgame reaches, and it is the load-bearing half: one cheap goalie left
        behind is a legal alternative, and the marginal drops back to a binary
        search over points.
        """
        state = scenarios.load(LAST_GOALIE)
        model, _, _ = _priced(state)
        target = self._target(state, model)
        others = {
            n: model[n] for n, p in state.available_players.items()
            if p.position == "G" and n != target
        }
        assert others, "no goalies left at all — the pool should still show them"
        budget = state.teams[MY_TEAM].remaining_budget
        assert min(others.values()) > budget, (
            f"cheapest alternative is ${min(others.values())}M against a "
            f"${budget}M budget"
        )

    def test_without_him_there_is_no_legal_roster(self):
        state = scenarios.load(LAST_GOALIE)
        model, live, _ = _priced(state)
        bot = state.teams[MY_TEAM]
        without = optimizer.solve_optimal_roster(
            bot, state.available_players, live,
            excluded_players={self._target(state, model)},
        )
        assert without.status == "Infeasible", (
            f"solving without him is {without.status} — the must-have branch in "
            f"compute_marginal_value is reached only when it is Infeasible"
        )

    def test_forcing_him_fills_the_roster_exactly(self):
        """The other semantic: spots == 0 is a complete roster, not a failure.

        Asserted through the shape of the answer rather than just its status,
        because that branch never runs the MILP — it returns an empty roster and
        the forced salary as the whole cost. Returning Infeasible here is what
        used to floor-price every player in the pool once one spot was left.
        """
        state = scenarios.load(LAST_GOALIE)
        model, live, _ = _priced(state)
        forced = optimizer.solve_optimal_roster(
            state.teams[MY_TEAM], state.available_players, live,
            forced_players={self._target(state, model): MIN_SALARY},
        )
        assert forced.status == "Optimal", f"forcing him is {forced.status}"
        assert forced.roster == [], "the spots == 0 branch selects nobody else"
        assert forced.total_cost == pytest.approx(MIN_SALARY)

    def test_he_is_worth_every_dollar_bot_can_pay(self):
        state = scenarios.load(LAST_GOALIE)
        model, live, info = _priced(state)
        bot = state.teams[MY_TEAM]
        target = self._target(state, model)
        marginal = optimizer.compute_marginal_value(
            state.available_players[target], bot, state.available_players, live,
        )
        assert marginal == pytest.approx(bot.physical_max_bid), (
            f"marginal is ${marginal}M against a ${bot.physical_max_bid}M "
            f"physical max — a must-have is worth everything we can pay"
        )
        assert marginal > model[target], (
            f"${marginal}M is not above his ${model[target]}M model price, so "
            f"this scenario says nothing the market layer does not"
        )
        rec = optimizer.compute_bid_recommendation(
            state.available_players[target], bot, state.available_players, live,
            info, current_price=MIN_SALARY, marginal_value=marginal,
        )
        assert rec.action == "BID"
        assert rec.value_cap == pytest.approx(bot.physical_max_bid)

    @pytest.mark.parametrize("keeper_goalies", [1, 2, 3])
    def test_it_works_from_any_size_of_crease(self, monkeypatch, keeper_goalies):
        """How many goalies BOT keeps is data, and it changes every season.

        Today's roster has two, so a single demotion and a demote-until-one loop
        are indistinguishable on live data — which is why this doctors the count.
        Measured against the single-demotion version: three keeper goalies left
        BOT needing NO goalie, so the scenario silently stopped being about a
        must-have; one left it with an empty crease and a lineup it cannot legally
        field. Neither failed anything, because neither can happen this season.
        """
        real_build = scenarios.build_initial_state

        def doctored():
            state = real_build()
            bot = state.teams[MY_TEAM]
            crease = [p for p in bot.roster_players if p.position == "G"]
            while len(crease) > keeper_goalies:
                spare = crease.pop()
                spare.is_bench = True
                bot.send_to_minors(spare.name)
            while len(crease) < keeper_goalies:
                spare = next(
                    (p for p in bot.minor_players if p.position == "G"), None,
                )
                assert spare is not None, "BOT has no minor-league goalie to recall"
                bot.recall_from_minors(spare.name)
                crease.append(spare)
            return state

        monkeypatch.setattr(scenarios, "build_initial_state", doctored)
        bot = scenarios.load(LAST_GOALIE).teams[MY_TEAM]
        assert bot.position_counts["G"] == 1, (
            f"{keeper_goalies} keeper goalies left {bot.position_counts['G']} in "
            f"the crease; one short of the minimum is the whole scenario"
        )
        assert bot.roster_needs == {"F": 0, "D": 0, "G": 1}, bot.roster_needs

    def test_every_opponent_gets_its_crease_filled(self):
        """The shape the scenario claims, asserted directly.

        Indirectly was not enough. This started life as "every opponent's MILP
        still solves", on the theory that a team left needing goalies would go
        Infeasible — and deleting the crease-filling altogether failed NOTHING,
        because these opponents are rich and ten goalies are still on the board at
        $3.2M-$7.7M. Unaffordable to BOT is pocket change to a team with $20M. The
        filling is about the state being readable, so read it.
        """
        state = scenarios.load(LAST_GOALIE)
        crease = {
            code: sum(1 for p in team.roster_players if p.position == "G")
            for code, team in state.teams.items()
            if code != MY_TEAM
        }
        expected = POSITION_MINIMUMS["G"] + BACKUP_TARGETS["G"]
        assert set(crease.values()) == {expected}, (
            f"goalies per opponent: {crease} — every rival should carry "
            f"{expected}, the 14/7/3 shape"
        )

    def test_every_opponent_can_still_be_solved(self):
        """A loadable scenario must not degrade a panel to floor values.

        `main._recompute_exact_projections` drops an Infeasible team back to its
        ESTIMATE, and BOT's own advice falls back to floor values when its MILP
        cannot solve — so a scenario that leaves a team unsolvable is worse than
        `/reset` rather than better.

        This one holds for a boring reason today (nobody is short of anything they
        can afford) and it did not catch the crease-filling being removed, which is
        what the test above is for. What it does guard is a future construction
        that strips a POSITION out of the pool while teams still need it — the trap
        this scenario walks right past, since it removes 53 of 64 goalies.
        """
        state = scenarios.load(LAST_GOALIE)
        _, live, _ = _priced(state)
        unsolved = {
            code: optimizer.solve_optimal_roster(
                team, state.available_players, live,
            ).status
            for code, team in state.teams.items()
            if code != MY_TEAM
        }
        assert set(unsolved.values()) == {"Optimal"}, unsolved

    def test_loading_it_twice_gives_the_same_state(self):
        first, second = scenarios.load(LAST_GOALIE), scenarios.load(LAST_GOALIE)
        for code in first.teams:
            a, b = first.teams[code], second.teams[code]
            assert [(p.name, p.salary) for p in a.all_players] == \
                   [(p.name, p.salary) for p in b.all_players], f"{code} differs"
        assert first.available_players.keys() == second.available_players.keys()


class TestEndgameSoleBidder:
    """Nobody left who can raise a bid — so whatever BOT bids, it has won.

    The 2026-08-05 report on the real pool: with the live ceiling collapsed to
    the floor, an advisor capping on `ceiling + increment` recommends DROP on a
    bargain. Everything here is derived from the loaded state; `players.csv` moves
    under it before every draft.
    """

    def _opponents(self, state):
        return {c: t for c, t in state.teams.items() if c != MY_TEAM}

    def test_every_opponent_is_full_and_cannot_reach_the_floor(self):
        """A full 24 under $0.5M is the ONLY legal way to price a team out.

        With a spot open the commissioner's reserve rule guarantees the team can
        still bid MIN_SALARY, so `physical_max_bid` cannot fall below it — which
        is why the fill to 24 is not decoration.
        """
        state = scenarios.load(SOLE_BIDDER)
        for code, team in self._opponents(state).items():
            assert team.roster_count == ROSTER_SIZE, (
                f"{code} has {team.roster_count} players and "
                f"{team.total_spots_remaining} spots, so it can bid the floor"
            )
            assert team.physical_max_bid < MIN_SALARY, (
                f"{code} can still bid ${team.physical_max_bid}M"
            )

    def test_no_opponent_is_marked_done(self):
        """Done would produce the same verdict for entirely the wrong reason.

        `bid_panel.html` filters the bidder grid on `is_done` alone, so a done
        team disappears from it while a cap-full one stays clickable. The bug
        this state covers is the clickable one: WIN rendering with no Assign
        button because the advisor and the button disagreed about who was left.
        Mark them done instead and every other test in this class still passes.
        """
        state = scenarios.load(SOLE_BIDDER)
        done = [c for c, t in self._opponents(state).items() if t.is_done]
        assert not done, f"{done} are done, so they leave the bidder grid entirely"

    def test_nobody_can_outbid_bot(self):
        state = scenarios.load(SOLE_BIDDER)
        everyone = list(state.teams)
        assert market.live_opponents(everyone, state.teams) == [], (
            "with the whole grid toggled on, not one team may raise the price"
        )
        assert market.bid_winner(everyone, state.teams) == MY_TEAM
        assert market.compute_live_ceiling(everyone, state.teams) == MIN_SALARY

    def test_the_market_falls_back_to_the_floor(self):
        """Zero demand = floor price, and this is the only loadable state showing it."""
        state = scenarios.load(SOLE_BIDDER)
        model, live, info = _priced(state)
        assert info.demand_count == 0 and info.floor_demand is True, info
        assert info.market_ceiling == MIN_SALARY
        assert set(live.values()) == {MIN_SALARY}, (
            f"market prices should all be the floor, got "
            f"{sorted(set(live.values()))[:5]}"
        )
        assert max(model.values()) > MIN_SALARY, (
            "the pool still has players the MODEL prices above the floor — "
            "otherwise the ceiling is not what flattened them"
        )

    def test_bot_can_still_act(self):
        state = scenarios.load(SOLE_BIDDER)
        bot = state.teams[MY_TEAM]
        assert not bot.is_done
        assert bot.total_spots_remaining >= 1, "no spot left to win a player into"
        assert MIN_SALARY <= bot.physical_max_bid < MAX_SALARY, (
            f"BOT's physical max is ${bot.physical_max_bid}M; at the league "
            f"maximum a value cap equal to it proves nothing about which "
            f"constraint bound"
        )
        _, live, _ = _priced(state)
        assert optimizer.solve_optimal_roster(
            bot, state.available_players, live,
        ).status == "Optimal"

    def _best_left(self, state, live):
        """The player worth winning, and what he is worth."""
        player = max(state.available_players.values(), key=lambda p: p.projected_points)
        return player, optimizer.compute_marginal_value(
            player, state.teams[MY_TEAM], state.available_players, live,
        )

    def test_a_bargain_is_a_win_not_a_drop(self):
        """The reported bug, on real data: value binds, the collapsed ceiling does not.

        Checked at three prices below the cap including the floor, because the
        pre-fix failure was price-dependent: `min(value_cap, ceiling + increment)`
        capped max_bid at $0.6M, so every price above that inverted to DROP.
        """
        state = scenarios.load(SOLE_BIDDER)
        _, live, info = _priced(state)
        player, marginal = self._best_left(state, live)
        assert marginal > round(info.market_ceiling + SALARY_INCREMENT, 1), (
            f"{player.name} is worth ${marginal}M — for this state to say "
            f"anything he has to be worth more than the collapsed ceiling"
        )
        for price in (MIN_SALARY, round(marginal / 2, 1), marginal):
            rec = optimizer.compute_bid_recommendation(
                player, state.teams[MY_TEAM], state.available_players, live, info,
                current_price=price, bot_uncontested=True, marginal_value=marginal,
            )
            assert rec.action == "WIN", (
                f"${price}M on a ${marginal}M player is {rec.action} — the "
                f"advisor is capping on a ceiling nobody can reach"
            )
            assert rec.max_bid == pytest.approx(rec.value_cap)
            assert rec.stop_status == "uncontested"

    def test_overpaying_still_drops(self):
        """The other side of the uncontested `<=`: at value it is a win, above it is not."""
        state = scenarios.load(SOLE_BIDDER)
        _, live, info = _priced(state)
        player, marginal = self._best_left(state, live)
        rec = optimizer.compute_bid_recommendation(
            player, state.teams[MY_TEAM], state.available_players, live, info,
            current_price=round(marginal + SALARY_INCREMENT, 1),
            bot_uncontested=True, marginal_value=marginal,
        )
        assert rec.action == "DROP"

    def test_loading_it_twice_gives_the_same_state(self):
        first, second = scenarios.load(SOLE_BIDDER), scenarios.load(SOLE_BIDDER)
        for code in first.teams:
            a, b = first.teams[code], second.teams[code]
            assert [(p.name, p.salary) for p in a.all_players] == \
                   [(p.name, p.salary) for p in b.all_players], f"{code} differs"
        assert first.available_players.keys() == second.available_players.keys()


def _assert_bot_is_still_in_the_draft(state, live):
    """Both late-draft scenarios build BOT with `_leave_bot_planning`, so this is one check.

    A scenario whose BOT cannot act tests nothing: an Infeasible MILP degrades
    every panel to floor values and the advisor stops answering. The two halves
    that are not obvious:

    * `physical_max_bid < MAX_SALARY` — a physical max sitting AT the league
      maximum cannot be told apart from the clamp inside `physical_max_bid`, so
      any figure derived from it would be unattributable.
    * a nonzero `roster_needs` — this is what the drain target was chosen for. A
      shallower drain buys BOT past its position minimums and leaves nothing on
      the board it has to have; see `_leave_bot_planning` for the four targets
      measured. If a data refresh moves this, retune that target rather than
      deleting the assertion.
    """
    bot = state.teams[MY_TEAM]
    solution = optimizer.solve_optimal_roster(bot, state.available_players, live)
    assert solution.status == "Optimal", (
        f"BOT's MILP is {solution.status} — the advisor cannot answer"
    )
    assert not bot.is_done, "BOT must always still be drafting"
    assert bot.total_spots_remaining >= 1, "BOT has no spots left to plan for"
    assert MIN_SALARY < bot.physical_max_bid < MAX_SALARY, (
        f"BOT's physical max is ${bot.physical_max_bid}M — it has to be a real "
        f"figure strictly under ${MAX_SALARY}M, or it is indistinguishable from "
        f"the clamp"
    )
    assert sum(bot.roster_needs.values()) >= 1, (
        f"BOT is at {bot.roster_count} players with no position needs left "
        f"({dict(bot.roster_needs)}) — `_leave_bot_planning`'s drain target is "
        f"tuned to leave a hole on the board"
    )


class TestSqueezeHitsItsTarget:
    """`scenarios._squeeze` directly, because from outside a scenario it cannot fail.

    The helper inverts `physical_max_bid` to land a team on a named max, and it
    needs two branches: with spots open the commissioner's reserve has to be added
    back, at `spots == 0` there is no reserve at all. Get the second one wrong and
    the team lands $0.5M off — and **every scenario-level assertion still passes**,
    because the ceiling still equals the full team's max, just half a million
    lower. So the branch is pinned here, on the helper, rather than through
    `full-roster-still-bidding`.

    The only test in this file that reaches for a private: the alternative is a
    scenario constant asserted in two places, which is a second source of truth
    for a figure that exists to be arbitrary.
    """

    def _an_opponent(self, state):
        return state.teams[sorted(c for c in state.teams if c != MY_TEAM)[0]]

    def test_with_spots_open_the_reserve_is_added_back(self):
        state = build_initial_state()
        team = self._an_opponent(state)
        scenarios._squeeze(team, 4.2)
        assert team.total_spots_remaining > 0, "precondition: this is the open branch"
        assert team.physical_max_bid == pytest.approx(4.2), (
            f"asked for a $4.2M max, got ${team.physical_max_bid}M on "
            f"{team.total_spots_remaining} open spots"
        )

    def test_a_full_roster_has_no_reserve_to_add_back(self):
        state = build_initial_state()
        team = self._an_opponent(state)
        model, _, _ = _priced(state)
        scenarios._fill(team, state, model, set())
        assert team.total_spots_remaining == 0, "precondition: this is the full branch"
        scenarios._squeeze(team, 6.0)
        assert team.physical_max_bid == pytest.approx(6.0), (
            f"asked for a $6.0M max on a full roster, got "
            f"${team.physical_max_bid}M — the open-spots formula adds a reserve "
            f"that does not exist at 24 players"
        )
        assert team.remaining_budget == pytest.approx(6.0), (
            "at zero spots the physical max IS the remaining budget"
        )

    def test_an_impossible_target_says_so(self):
        """Penalties only take money away, so some targets cannot be reached.

        The `max(0.0, ...)` this replaced returned a team parked somewhere else
        entirely while reporting success — and a scenario that quietly builds a
        state other than the one it names produces failures three assertions from
        the cause, which is the argument `_fill`'s docstring already makes.

        The target is derived, not picked: with zero penalties the best max a team
        can show is its whole cap room less the reserve it must keep for the spots
        it has NOT filled.
        """
        state = build_initial_state()
        team = self._an_opponent(state)
        before = team.penalties
        achievable = (
            SALARY_CAP - team.total_salary
            - (team.total_spots_remaining - 1) * MIN_SALARY
        )
        with pytest.raises(RuntimeError, match=team.code):
            scenarios._squeeze(team, round(achievable + 1.0, 1))
        assert team.penalties == before, (
            "a raised squeeze left the team half-modified — the next scenario to "
            "catch this error would be squeezing debris"
        )


class TestDrainedLateDraft:
    """Sixty picks in: the money is gone, the rosters are not full, nobody is done.

    The state between `/reset` and the two endgames, and the one a real draft
    spends its second half in. `endgame-ceiling-binds` reaches a binding ceiling by
    marking eight teams DONE — demand collapses and two teams bid.
    `endgame-sole-bidder` reaches it by filling every roster — the ceiling hits the
    floor. Here all ten opponents are live, every one of them still needs players,
    and the ceiling binds anyway, mid-range, because the budgets are spent.

    Bands and floors, never the measured value: `players.csv` is replaced before
    every draft.
    """

    def _opponents(self, state):
        return {c: t for c, t in state.teams.items() if c != MY_TEAM}

    def test_the_ceiling_binds_strictly_between_the_floor_and_the_cap(self):
        """Neither of the two states already loadable: not the cap, not the floor."""
        _, _, info = _priced(scenarios.load(LATE_DRAFT))
        assert MIN_SALARY < info.market_ceiling < MAX_SALARY, (
            f"ceiling is ${info.market_ceiling}M — at ${MAX_SALARY}M this is "
            f"/reset and at ${MIN_SALARY}M it is endgame-sole-bidder; the point "
            f"of this scenario is the range in between"
        )

    def test_the_ceiling_is_the_second_highest_opponent_max(self):
        """The rule itself, on ten live teams — and the top three must differ.

        With ties, "second-highest" is indistinguishable from "highest" or "any of
        them" and this assertion could not fail. `_late_draft_shape` staggers the
        squeeze targets for exactly this reason.
        """
        state = scenarios.load(LATE_DRAFT)
        _, _, info = _priced(state)
        maxes = sorted(
            (t.physical_max_bid for t in self._opponents(state).values()), reverse=True
        )
        assert len(set(maxes[:3])) == 3, (
            f"the top three opponent maxes are {maxes[:3]} — a tie hides which "
            f"team sets the ceiling"
        )
        assert info.market_ceiling == pytest.approx(maxes[1]), (
            f"ceiling ${info.market_ceiling}M is not the second-highest of {maxes}"
        )

    def test_every_opponent_is_live_and_still_shopping(self):
        """The premise: money gone, holes left, nobody finished.

        A done team leaves the ceiling and the demand count entirely, so one of
        those would quietly turn this into `endgame-ceiling-binds`.
        """
        state = scenarios.load(LATE_DRAFT)
        _, _, info = _priced(state)
        for code, team in self._opponents(state).items():
            assert not team.is_done, f"{code} is done — that is the other scenario"
            assert team.total_spots_remaining >= 1, (
                f"{code} has a full roster; this scenario is about teams that "
                f"still need players and cannot pay for them"
            )
            assert team.physical_max_bid >= MIN_SALARY, (
                f"{code} cannot reach the floor, so it is not a bidder at all"
            )
            assert ROSTER_SIZE // 2 < team.roster_count < ROSTER_SIZE, (
                f"{code} has {team.roster_count} players — a late draft is most "
                f"of the way to {ROSTER_SIZE}, and none of the way is a reset"
            )
        assert info.demand_count == len(state.teams) - 1, (
            f"demand_count is {info.demand_count}, not all "
            f"{len(state.teams) - 1} opponents"
        )
        assert not info.floor_demand, (
            "floor_demand means zero demand, which floors every price in the pool"
        )

    def test_a_real_share_of_the_pool_is_capped(self):
        """What the ceiling is FOR: prices the MILP plans on, cut below the model.

        Counted with `market.is_capped`, the same predicate the Available Players
        Price column and the nomination panel's ▼ marker both render from — a
        private restatement here would pass while the panels showed nothing.
        """
        state = scenarios.load(LATE_DRAFT)
        model, live, info = _priced(state)
        capped = [n for n in model if market.is_capped(model[n], live[n])]
        assert len(capped) >= 10, (
            f"only {len(capped)} of {len(model)} players price below their model "
            f"at a ${info.market_ceiling}M ceiling — with a handful, whether the "
            f"panels show the capped branch at all is luck"
        )

    def test_the_nomination_panel_has_a_model_price_to_strike_through(self):
        """The two-price line, outside an endgame.

        Both figures always render; the ▼ and the strike-through only when the
        ceiling cut the price. A recommendation whose two figures agree renders a
        panel that cannot show what this scenario was built to show.
        """
        state = scenarios.load(LATE_DRAFT)
        model, live, _ = _priced(state)
        picks = [p for p in optimizer.recommend_nomination(state, live, model) if p]
        assert picks, "no nomination recommendation at all"
        assert any(p.capped for p in picks), (
            "neither nomination pick is capped: "
            + ", ".join(
                f"{p.player.name} model ${p.model_price:.1f}M vs market "
                f"${p.expected_price:.1f}M"
                for p in picks
            )
        )

    def test_the_forecast_says_something_about_the_player(self):
        """`stop_status = live` with a real figure — impossible on a fresh state.

        At `ceiling == MAX_SALARY` the forecast never starts and every player in
        the pool reports `at_cap` with no number.
        """
        state = scenarios.load(LATE_DRAFT)
        model, live, info = _priced(state)
        richest = max(model, key=lambda n: (model[n], n))
        rec = optimizer.compute_bid_recommendation(
            state.available_players[richest], state.teams[MY_TEAM],
            state.available_players, live, info, current_price=MIN_SALARY,
        )
        assert rec.stop_status == "live", (
            f"stop_status is {rec.stop_status!r} — no forecast, so this scenario "
            f"buys nothing over /reset"
        )
        assert rec.expected_stop is not None
        assert rec.expected_stop <= MAX_SALARY, (
            f"expected_stop ${rec.expected_stop}M is a bid the league forbids"
        )

    def test_bot_can_still_plan_and_still_bid(self):
        state = scenarios.load(LATE_DRAFT)
        _, live, _ = _priced(state)
        _assert_bot_is_still_in_the_draft(state, live)

    def test_every_team_still_solves(self):
        """League State's Proj column is a MILP per team; one Infeasible blanks a row."""
        state = scenarios.load(LATE_DRAFT)
        _, live, _ = _priced(state)
        for code, team in state.teams.items():
            solution = optimizer.solve_optimal_roster(team, state.available_players, live)
            assert solution.status == "Optimal", f"{code} is {solution.status}"

    def test_loading_it_twice_gives_the_same_state(self):
        first, second = scenarios.load(LATE_DRAFT), scenarios.load(LATE_DRAFT)
        for code in first.teams:
            a, b = first.teams[code], second.teams[code]
            assert [(p.name, p.salary) for p in a.all_players] == \
                   [(p.name, p.salary) for p in b.all_players], f"{code} differs"
            assert a.penalties == b.penalties, f"{code} penalties differ"
        assert first.available_players.keys() == second.available_players.keys()


class TestFullRosterStillBidding:
    """A team at 24 with cap space is still a bidder — and here it sets the price.

    `4dc59da` fixed a ceiling that gated on roster space; extras go to the minors
    with their salary fully on the cap, so a full team can raise a bid and someone
    has to outbid it. Until now that lived only in unit tests on synthetic teams.
    `endgame-sole-bidder` is the opposite case — full AND broke — and proves
    nothing about this one.
    """

    def _opponents(self, state):
        return {c: t for c, t in state.teams.items() if c != MY_TEAM}

    def _the_full_team(self, state):
        full = [c for c, t in self._opponents(state).items() if t.roster_count == ROSTER_SIZE]
        assert len(full) == 1, (
            f"expected exactly one full opponent, got {full} — see the scenario "
            f"docstring on why two makes the claim below unfalsifiable"
        )
        return full[0], state.teams[full[0]]

    def test_exactly_one_opponent_is_full_with_nothing_it_needs(self):
        state = scenarios.load(FULL_ROSTER)
        code, team = self._the_full_team(state)
        assert team.total_spots_remaining == 0, f"{code} still has a spot open"
        assert sum(team.roster_needs.values()) == 0, (
            f"{code} is at {ROSTER_SIZE} players but still reports "
            f"{dict(team.roster_needs)} — it cannot roster anyone to fix that"
        )
        assert not team.is_done, (
            f"{code} is marked done, which excludes it from the ceiling for a "
            f"completely different reason and hides what this scenario tests"
        )

    def test_the_full_team_sets_the_market_ceiling(self):
        """It is SECOND-highest on purpose: the ceiling IS the second-highest max.

        As the highest it would set nothing at all, and swapping the two squeeze
        targets is a mutation this catches.
        """
        state = scenarios.load(FULL_ROSTER)
        _, _, info = _priced(state)
        code, team = self._the_full_team(state)
        assert MIN_SALARY < team.physical_max_bid < MAX_SALARY, (
            f"{code}'s max is ${team.physical_max_bid}M — at ${MAX_SALARY}M it "
            f"cannot be told apart from the clamp"
        )
        assert info.second_bidder == code, (
            f"second_bidder is {info.second_bidder}, not the full team {code}"
        )
        assert info.market_ceiling == pytest.approx(team.physical_max_bid), (
            f"ceiling ${info.market_ceiling}M is not {code}'s "
            f"${team.physical_max_bid}M"
        )

    def test_gating_the_ceiling_on_roster_space_would_underprice_the_pool(self):
        """The 2026-08-05 regression as a number, which is the point of the state."""
        state = scenarios.load(FULL_ROSTER)
        _, _, info = _priced(state)
        code, _ = self._the_full_team(state)
        with_spots = sorted(
            (t.physical_max_bid for t in self._opponents(state).values()
             if t.total_spots_remaining > 0),
            reverse=True,
        )
        gated = with_spots[1]
        assert info.market_ceiling - gated >= 1.0, (
            f"a ceiling counting only teams with spots would be ${gated}M against "
            f"the real ${info.market_ceiling}M — a ${info.market_ceiling - gated:.1f}M "
            f"gap is too small to notice when {code} stops counting"
        )

    def test_the_live_ceiling_counts_it_too(self):
        """The bid advisor's own ceiling is a different computation over a different set.

        `/bid-check` builds its `MarketInfo` from `compute_live_ceiling` over the
        named bidders, so a full team dropping out of THAT set would produce a
        spurious DROP against the one rival who can actually outbid BOT.
        """
        state = scenarios.load(FULL_ROSTER)
        code, team = self._the_full_team(state)
        bidders = [MY_TEAM, code]
        assert market.live_opponents(bidders, state.teams) == [code], (
            f"{code} is not a live opponent, so bidding against it alone reads "
            f"as uncontested"
        )
        assert market.compute_live_ceiling(bidders, state.teams) == pytest.approx(
            team.physical_max_bid
        )

    def test_bot_can_still_plan_and_still_bid(self):
        state = scenarios.load(FULL_ROSTER)
        _, live, _ = _priced(state)
        _assert_bot_is_still_in_the_draft(state, live)

    def test_every_team_still_solves(self):
        """Including the full one, whose MILP has zero spots to fill."""
        state = scenarios.load(FULL_ROSTER)
        _, live, _ = _priced(state)
        for code, team in state.teams.items():
            solution = optimizer.solve_optimal_roster(team, state.available_players, live)
            assert solution.status == "Optimal", f"{code} is {solution.status}"

    def test_loading_it_twice_gives_the_same_state(self):
        first, second = scenarios.load(FULL_ROSTER), scenarios.load(FULL_ROSTER)
        for code in first.teams:
            a, b = first.teams[code], second.teams[code]
            assert [(p.name, p.salary) for p in a.all_players] == \
                   [(p.name, p.salary) for p in b.all_players], f"{code} differs"
            assert a.penalties == b.penalties, f"{code} penalties differ"
        assert first.available_players.keys() == second.available_players.keys()


@pytest.mark.parametrize("name", sorted(scenarios.SCENARIOS))
def test_no_scenario_builds_a_state_the_league_forbids(name):
    """Every scenario is a position a real auction could actually have reached.

    The reserve rule is the one that matters: the commissioner software refuses
    any bid that would leave a team unable to fill a full roster at MIN_SALARY,
    so `remaining_budget < spots * MIN_SALARY` is unreachable through legal
    bidding — and a scenario that produces it would be testing the engine against
    an auction the league cannot hold. The others are structural: over 24 on the
    active roster puts a 25th player in the starting-lineup calculation, and BOT
    marked done means every panel it is supposed to exercise renders nothing.
    """
    state = scenarios.load(name)
    for code, team in state.teams.items():
        assert team.remaining_budget >= team.total_spots_remaining * MIN_SALARY, (
            f"{code} has ${team.remaining_budget}M for "
            f"{team.total_spots_remaining} spots — the commissioner would have "
            f"refused the bid that got it here"
        )
        assert team.remaining_budget >= 0, f"{code} is over the cap"
        assert team.roster_count <= ROSTER_SIZE, (
            f"{code} has {team.roster_count} on the active roster"
        )
    assert not state.teams[MY_TEAM].is_done, "BOT must always still be drafting"


def _ui_scenario_values() -> set[str]:
    """Scenario names the navbar picker actually offers."""
    html = (Path(__file__).resolve().parent.parent / "templates" / "base.html").read_text()
    select = re.search(r'hx-post="/load-scenario".*?</select>', html, re.S)
    assert select, "could not find the scenario <select> in base.html"
    # The placeholder carries value="" and is `disabled selected`, not a scenario.
    return {v for v in re.findall(r'<option value="([^"]*)"', select.group(0)) if v}


def test_every_scenario_is_reachable_from_the_ui():
    """Both directions, like TestShortcutsModal does for keyboard shortcuts.

    A scenario in `SCENARIOS` with no `<option>` is invisible — reachable only by
    hand-posting the form, which nobody does mid-draft. An `<option>` with no
    scenario is worse: it looks available and answers with an error toast. Neither
    is detectable from either side alone, so the two sets must be equal.
    """
    assert _ui_scenario_values() == set(scenarios.SCENARIOS), (
        f"picker offers {sorted(_ui_scenario_values())} but SCENARIOS has "
        f"{sorted(scenarios.SCENARIOS)} — add the <option> and the registration "
        f"in the same commit"
    )


def test_the_ui_scan_finds_options_at_all():
    """Guard the guard: an empty parse would make the set comparison vacuous."""
    assert len(_ui_scenario_values()) >= 2, (
        f"only parsed {_ui_scenario_values()} from base.html — if the select's "
        f"markup changed, the equality check above is comparing nothing"
    )

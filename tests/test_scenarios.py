"""Tests for scenarios.py."""

import re
from pathlib import Path

import pytest

import market
import optimizer
import scenarios
from config import MAX_SALARY, MIN_SALARY, MY_TEAM
from data_loader import build_initial_state
from price_model import load_model_params, predict_all_prices

ENDGAME = "endgame-ceiling-binds"


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
        state = scenarios.load(ENDGAME)
        model, live, _ = _priced(state)
        capped = [n for n in model if live[n] < model[n] - 1e-9]
        assert len(capped) >= 40, (
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

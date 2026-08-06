"""Buyouts are legal only on group 2/3 — everywhere, and only there.

Owner's rules (2026-08-06):

1. Buyouts can only happen on group 2/3.
2. Group A-E cannot be bought out — they go to the minors for a $0 cap hit instead.
3. A buyout can target active, bench or minors; the 50% penalty is the same.
4. Every drafted player is a full cap hit wherever they sit.
5. "Keeper" is only provenance — already on an FCHL team before the auction.

The tool used to get this wrong in both directions at once: it offered buyouts on
group A-E players sitting on the active roster (illegal, and the numbers looked
plausible because those DO count on cap), while hiding the group 2/3 players in
the minors (legal, often worth it, and where every player drafted past 24 lands).

Note what rule 1 implies for the reported figures: an eligible player is always
fully on the cap, so salary_freed and net_cap_freed need no minors-aware branch.
The BACKLOG entry that asked for one was working from a wrong premise.
"""

import pytest

from config import BUYOUT_PENALTY_RATE, MY_TEAM
from trade import evaluate_buyout, execute_buyout


def _setup():
    from data_loader import build_initial_state
    from market import compute_all_market_prices
    from price_model import load_model_params, predict_all_prices

    state = build_initial_state()
    preds = predict_all_prices(state.available_players, load_model_params())
    market_data = compute_all_market_prices(
        state.available_players, preds, state.teams,
    )
    return state, {name: price for name, (price, _) in market_data.items()}


def _find(state, group_in, minor):
    """A BOT player in one of `group_in`, in minors or not. Skip if the data moves."""
    pool = state.teams[MY_TEAM].all_players
    match = [p for p in pool if p.is_minor is minor and p.group in group_in]
    if not match:
        pytest.skip(f"no BOT player with group in {group_in}, is_minor={minor}")
    return match[0]


class TestIneligibleGroupsAreRefused:
    """Group A-E can't be bought out at any location.

    Tim Stutzle is the live case: group B, active roster, $2.5M. Because a
    non-minors player always counts on cap, evaluate_buyout happily reported
    "+$1.25M net cap freed" for an illegal move — no visual tell that anything
    was wrong.
    """

    def test_evaluate_refuses_prospect_on_active_roster(self):
        state, mp = _setup()
        victim = _find(state, {"A", "B", "C", "D", "E"}, minor=False)
        assert victim.counts_on_cap, "fixture must be the deceptive case: on cap"

        with pytest.raises(ValueError, match=victim.group):
            evaluate_buyout(state, victim.name, mp)

    def test_evaluate_refuses_prospect_in_minors(self):
        state, mp = _setup()
        victim = _find(state, {"A", "B", "C", "D", "E"}, minor=True)

        with pytest.raises(ValueError, match=victim.group):
            evaluate_buyout(state, victim.name, mp)

    def test_execute_refuses_too(self):
        """The evaluator is advisory; /buyout posts a name straight to execute.

        Guarding only the evaluator would leave the illegal move one hand-made
        POST away, and it mutates the cap.
        """
        state, _ = _setup()
        victim = _find(state, {"A", "B", "C", "D", "E"}, minor=False)
        team = state.teams[MY_TEAM]
        before_salary, before_penalties = team.total_salary, team.penalties

        with pytest.raises(ValueError, match=victim.group):
            execute_buyout(state, victim.name)

        assert team.find_player(victim.name) is not None, "refused buyout removed him"
        assert team.total_salary == before_salary
        assert team.penalties == before_penalties

    def test_refusal_names_the_alternative(self):
        """The operator needs to know what to do instead, mid-draft."""
        state, mp = _setup()
        victim = _find(state, {"A", "B", "C", "D", "E"}, minor=False)

        with pytest.raises(ValueError) as exc:
            evaluate_buyout(state, victim.name, mp)
        assert "minors" in str(exc.value).lower()


class TestEligibleGroupsStillWork:
    def test_group_2_3_on_active_roster(self):
        state, mp = _setup()
        target = _find(state, {"2", "3"}, minor=False)

        result = evaluate_buyout(state, target.name, mp)
        assert result.penalty_added == pytest.approx(
            target.salary * BUYOUT_PENALTY_RATE
        )

    def test_group_2_3_in_minors_is_buyable(self):
        """The capability the panel was hiding.

        Every player drafted past the 24-man roster lands in group 2/3 minors at
        full cap hit, so mid-draft this is the population you'd actually reach
        for — and it was unreachable from the UI.
        """
        state, mp = _setup()
        target = _find(state, {"2", "3"}, minor=True)
        assert target.counts_on_cap, "group 2/3 in minors is still fully on cap"

        result = evaluate_buyout(state, target.name, mp)
        assert result.penalty_added == pytest.approx(
            target.salary * BUYOUT_PENALTY_RATE
        )

    def test_reported_cap_figures_need_no_minors_branch(self):
        """Rule 1 makes the minors-aware branch the BACKLOG asked for unnecessary.

        An eligible player is fully on the cap wherever they sit, so freeing
        their cap hit always equals freeing their salary. Pinned so nobody
        re-adds that branch from the old backlog entry.
        """
        state, mp = _setup()
        for minor in (False, True):
            target = _find(state, {"2", "3"}, minor=minor)
            result = evaluate_buyout(state, target.name, mp)
            assert result.salary_freed == pytest.approx(target.salary)
            assert result.net_cap_freed == pytest.approx(
                target.salary * (1 - BUYOUT_PENALTY_RATE)
            )


class TestPanelOffersExactlyTheEligible:
    """The buttons and the engine must agree on who can be bought out.

    They disagreed in both directions: the panel listed team.roster_players,
    which includes A-E prospects (illegal) and excludes group 2/3 minors
    (legal, and where everyone drafted past 24 lands).
    """

    @pytest.fixture(scope="class")
    def client(self):
        from fastapi.testclient import TestClient
        import main

        with TestClient(main.app) as c:
            c.post("/reset")
            yield c

    def _panel(self, client):
        return client.get("/buyout-check/Nobody McFake").text

    def test_ineligible_players_are_not_offered(self, client):
        import main

        html = self._panel(client)
        bot = main.auction_state.teams[MY_TEAM]
        ineligible = [p for p in bot.all_players if not p.can_be_bought_out]
        assert ineligible, "fixture must contain prospects to exclude"

        offered = [p.name for p in ineligible if f"/buyout-check/{p.name}" in html]
        assert not offered, f"panel offers illegal buyouts: {offered}"

    def test_eligible_minors_are_offered(self, client):
        import main

        html = self._panel(client)
        bot = main.auction_state.teams[MY_TEAM]
        eligible_minors = [
            p for p in bot.minor_players if p.can_be_bought_out
        ]
        if not eligible_minors:
            pytest.skip("no group 2/3 players in BOT's minors in current data")

        missing = [
            p.name for p in eligible_minors
            if f"/buyout-check/{p.name}" not in html
        ]
        assert not missing, f"eligible minors hidden from the panel: {missing}"

    def test_refusal_explains_itself_instead_of_a_blank_panel(self, client):
        import main

        bot = main.auction_state.teams[MY_TEAM]
        victim = next(p for p in bot.all_players if not p.can_be_bought_out)

        r = client.get(f"/buyout-check/{victim.name}")
        assert r.status_code == 200
        assert "showToast" in r.headers.get("HX-Trigger", ""), (
            "an ineligible player rendered an empty panel with no explanation"
        )

    def test_execute_is_refused_with_the_real_reason(self, client):
        import main

        bot = main.auction_state.teams[MY_TEAM]
        victim = next(p for p in bot.all_players if not p.can_be_bought_out)
        penalties_before = bot.penalties

        r = client.post("/buyout", data={"player": victim.name})
        assert r.status_code == 200
        trigger = r.headers.get("HX-Trigger", "")
        assert "showToast" in trigger
        assert "not found" not in trigger, (
            "reported the wrong reason — he was found, he's just ineligible"
        )
        assert main.auction_state.teams[MY_TEAM].penalties == penalties_before
        assert main.auction_state.teams[MY_TEAM].find_player(victim.name) is not None


class TestTradeScenariosRespectEligibility:
    """evaluate_trade auto-proposes buying out each received player.

    Unfiltered, trading for a prospect made the tool recommend an illegal move —
    and a scenario can be the recommended one.
    """

    def test_no_buyout_scenario_for_ineligible_received_player(self):
        from trade import PlayerTrade, evaluate_trade

        state, mp = _setup()
        other = next(
            t for c, t in state.teams.items()
            if c != MY_TEAM and any(
                p.group in {"A", "B", "C", "D", "E"} for p in t.all_players
            )
        )
        prospect = next(
            p for p in other.all_players
            if p.group in {"A", "B", "C", "D", "E"}
        )
        give = state.teams[MY_TEAM].roster_players[0]

        result = evaluate_trade(
            state,
            give=[PlayerTrade(name=give.name, position=give.position,
                              salary=give.salary, projected_points=give.projected_points)],
            receive=[PlayerTrade(name=prospect.name, position=prospect.position,
                                 salary=prospect.salary,
                                 projected_points=prospect.projected_points)],
            market_prices=mp,
            source_team_code=other.code,
        )

        assert not any(prospect.name in s.buyouts for s in result.scenarios), (
            f"proposed an illegal buyout of group-{prospect.group} {prospect.name}"
        )

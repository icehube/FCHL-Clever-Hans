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
from main import _dom_id
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


class TestUIMatchesEligibility:
    """The rest of the cockpit has to agree with the eligibility rule too.

    Guarding the engine while leaving the UI on the old rule produces the worst
    outcome: a button that looks available and a dot that looks like a verdict,
    both about a decision that doesn't exist.
    """

    @pytest.fixture(scope="class")
    def client(self):
        from fastapi.testclient import TestClient
        import main

        with TestClient(main.app) as c:
            c.post("/reset")
            yield c

    def test_ineligible_player_gets_no_buyout_dot(self, client):
        """A grey dot on an un-buyoutable player reads as "not analyzed yet".

        It would never fill in, because the scan skips him — so it promises an
        answer that isn't coming.
        """
        import main

        bot = main.auction_state.teams[MY_TEAM]
        victim = next(
            (p for p in bot.roster_players if not p.can_be_bought_out), None
        )
        if victim is None:
            pytest.skip("no ineligible player on BOT's active roster in current data")

        html = client.get("/team-view/" + MY_TEAM).text
        # Through `_dom_id`, the one derivation both templates use. This used to
        # hand-roll `name.replace(" ", "-")`, which stopped matching the moment
        # the id changed — and a "not in html" assertion against a string the
        # app never emits passes no matter what the app does.
        assert f'id="bo-{_dom_id(victim.name)}"' not in html, (
            f"{victim.name} is group {victim.group} and can't be bought out, "
            f"but the panel renders a buyout indicator for him"
        )

    def test_scan_skips_ineligible_players(self, client):
        """Every solve the scan runs must correspond to a real decision."""
        import main

        client.get("/buyout-indicators")
        bot = main.auction_state.teams[MY_TEAM]
        scanned = set(main.buyout_indicators)

        illegal = {p.name for p in bot.all_players if not p.can_be_bought_out}
        assert not (scanned & illegal), (
            f"scan ran MILP solves for un-buyoutable players: {scanned & illegal}"
        )
        assert scanned, "scan produced nothing at all"

    def test_the_scan_covers_eligible_minors(self, client):
        """Group 2/3 in the minors is a legal buyout with a full cap hit.

        This test used to assert the OPPOSITE — that the scan skips minors —
        on the premise that they had no row in the team table to fill. That
        premise expired when team_panel.html grew a Minors table, and the
        Analyzer had been offering these players the whole time, so Scan
        reported on BOT's 11 active players and said nothing about 4 more
        holding $2.0M of cap. "No buyout helps" is a very different statement
        from "I didn't look".
        """
        import main

        bot = main.auction_state.teams[MY_TEAM]
        eligible_minors = {p.name for p in bot.minor_players if p.can_be_bought_out}
        if not eligible_minors:
            pytest.skip("BOT holds no group 2/3 minors in current data")

        client.get("/buyout-indicators")
        missing = eligible_minors - set(main.buyout_indicators)
        assert not missing, f"scan skipped buyout-eligible minors: {sorted(missing)}"

    def test_eligible_minors_have_a_dot_to_fill(self, client):
        """A solve with no placeholder is discarded work, and an OOB swap with
        no target logs `htmx:oobErrorNoTarget`. The scan and the panel have to
        cover the same set."""
        import main

        bot = main.auction_state.teams[MY_TEAM]
        eligible_minors = [p for p in bot.minor_players if p.can_be_bought_out]
        if not eligible_minors:
            pytest.skip("BOT holds no group 2/3 minors in current data")

        html = client.get(f"/team-view/{MY_TEAM}").text
        for p in eligible_minors:
            assert f'id="bo-{_dom_id(p.name)}"' in html, (
                f"{p.name} (group {p.group}, in the minors) is buyout-eligible "
                f"but the panel gives the scan nowhere to report it"
            )

    def test_ineligible_minors_get_no_dot(self, client):
        """The eligibility rule is the same wherever the player sits."""
        import main

        bot = main.auction_state.teams[MY_TEAM]
        victim = next(
            (p for p in bot.minor_players if not p.can_be_bought_out), None
        )
        if victim is None:
            pytest.skip("BOT holds no A-E minors in current data")

        html = client.get(f"/team-view/{MY_TEAM}").text
        assert f'id="bo-{_dom_id(victim.name)}"' not in html, (
            f"{victim.name} is group {victim.group} — a dot there promises a "
            f"verdict on a move the engine refuses"
        )

    def test_an_opponents_minors_get_no_dots(self, client):
        """Dots are BOT-only by construction: `_recompute_buyout_indicators`
        scores every hypothetical against BOT's MILP total. Widening the scan
        to the minors must not widen it to other teams."""
        import main

        other = next(c for c in main.auction_state.teams if c != MY_TEAM)
        html = client.get(f"/team-view/{other}").text
        assert 'class="buyout-light' not in html, (
            f"{other}'s panel renders buyout dots that can never be filled"
        )

    def test_benched_keeper_can_be_demoted_from_the_ui(self, client):
        """The engine allows it; the button has to exist or nothing changed.

        This is the whole point of lifting the keeper restriction — a group A-E
        player can't be bought out, so the minors is his only route off the cap.
        """
        import main

        bot = main.auction_state.teams[MY_TEAM]
        victim = next(
            (p for p in bot.keeper_players if not p.can_be_bought_out), None
        )
        if victim is None:
            pytest.skip("no ineligible keeper on BOT's active roster")

        try:
            client.post(
                "/toggle-bench", data={"team_code": MY_TEAM, "player_name": victim.name}
            )
            html = client.get(f"/team-view/{MY_TEAM}").text
            assert "/move-to-minors" in html, "no demote button rendered at all"

            r = client.post(
                "/move-to-minors", data={"team_code": MY_TEAM, "player_name": victim.name}
            )
            assert r.status_code == 200
            moved = main.auction_state.teams[MY_TEAM].find_player(victim.name)
            assert moved.is_minor, f"{victim.name} (keeper) could not be sent down"
            assert not moved.counts_on_cap, "the point of the move: $0 cap hit"
        finally:
            # /reset, not /undo: this mutates twice (bench, then demote) and one
            # undo would leave the bench toggle flipped in the module-global
            # auction_state, which every other test module shares.
            client.post("/reset")


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

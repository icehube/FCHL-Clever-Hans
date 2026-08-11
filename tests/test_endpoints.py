"""Tests for main.py: FastAPI endpoints."""

import html
import json
import re
from contextlib import contextmanager

import pytest

from config import MIN_SALARY, MINOR_CAP_GROUPS, SALARY_CAP
from tests.helpers import (
    a_buyout_candidate,
    a_roster_player,
    assign,
    section_of,
    squeeze,
    toast_of,
)


@contextmanager
def cannot_raise(code: str):
    """Make a team unable to legally raise the price, without marking it done.

    A FULL ROSTER is not enough: a 24-man team with cap space can still draft
    (the extra goes to minors at full cap), so it is a live bidder and sets
    ceilings like anyone else. Exhaust the budget instead — that is the only
    thing that actually stops a team bidding.

    The team stays not-done so it remains clickable in the bidder grid, which
    is the situation these tests exist to cover.
    """
    import main

    team = main.auction_state.teams[code]
    saved = team.penalties
    team.penalties = SALARY_CAP
    team._invalidate_cache()  # roster_players is memoized
    try:
        assert team.physical_max_bid < MIN_SALARY, "fixture must be unable to bid"
        assert not team.is_done, "must be broke but NOT done, to stay clickable"
        yield team
    finally:
        team.penalties = saved
        team._invalidate_cache()


class TestIndexPage:
    def test_index_returns_200(self, client):
        r = client.get("/")
        assert r.status_code == 200

    def test_index_has_panels(self, client):
        r = client.get("/")
        assert "Auction" in r.text
        assert "League State" in r.text
        assert "Bridlewood AI" in r.text


class TestAssign:
    def test_assign_player(self, client):
        """Assigning a player should update state."""
        r = client.post("/assign", data={
            "player": "Artemi Panarin",
            "team": "BOT",
            "salary": "5.0",
        })
        assert r.status_code == 200
        assert "Artemi Panarin" in r.text

    def test_assign_invalid_player(self, client):
        """Assigning non-existent player should not crash."""
        r = client.post("/assign", data={
            "player": "Fake McPlayer",
            "team": "BOT",
            "salary": "1.0",
        })
        assert r.status_code == 200


class TestBidCheck:
    def test_bid_check(self, client):
        """Bid check should return advice."""
        r = client.post("/bid-check", data={
            "player": "J.T. Miller",
            "bidders": "SRL,MAC",
            "price": "2.0",
            "highest_bidder": "SRL",
        })
        assert r.status_code == 200

    def test_bid_check_invalid_player(self, client):
        r = client.post("/bid-check", data={
            "player": "Nobody",
            "bidders": "",
            "price": "0.5",
            "highest_bidder": "",
        })
        assert r.status_code == 200

    def test_last_bidder_standing_wins_not_drops(self, client):
        """Regression (2026-08-05): BOT alone at a fair price is a WIN.

        The collapsed live ceiling used to cap max_bid at $0.6M and render DROP
        on a bargain. Elite player, low price, no opponents left.
        """
        r = client.post("/bid-check", data={
            "player": "Connor McDavid",
            "bidders": "BOT",
            "price": "2.5",
            "highest_bidder": "BOT",
        })
        assert r.status_code == 200
        assert "bid-win" in r.text
        assert "You" in r.text and "won at $2.5M" in r.text
        # Structural, not textual: this asserted on the DROP reasoning wording
        # until that wording changed, at which point it would have passed for
        # the wrong reason. The CSS class derives straight from `action`.
        assert "bid-drop" not in r.text
        # Ceiling is meaningless with nobody left — must not show the $0.5M floor
        assert "Ceiling: &mdash;" in r.text

    def test_contested_bidding_unaffected(self, client):
        """Opponents still active: normal BID advice and a real ceiling."""
        r = client.post("/bid-check", data={
            "player": "Connor McDavid",
            "bidders": "BOT,SRL,MAC",
            "price": "2.5",
            "highest_bidder": "SRL",
        })
        assert r.status_code == 200
        assert "bid-win" not in r.text
        assert "Ceiling: $" in r.text

    def test_win_comes_with_an_assign_button(self, client):
        """Regression (2026-08-05): WIN must never render without Assign.

        A broke team stays clickable in the bidder grid (the grid filters on
        is_done only), so BOT + a team that cannot raise satisfied the
        advisor's uncontested check while the Assign gate's
        len(active_bidders) == 1 hid the button.
        """
        with cannot_raise("HSM"):
            r = client.post("/bid-check", data={
                "player": "Connor McDavid",
                "bidders": "BOT,HSM",
                "price": "2.5",
                "highest_bidder": "BOT",
            })
            assert r.status_code == 200
            assert "bid-win" in r.text, "should be a WIN — HSM cannot raise the price"
            assert 'name="team" value="BOT"' in r.text, "Assign form must be present"

    def test_uncontested_overpay_still_drops(self, client):
        """No opponents left, but above value — DROP and name the overpay."""
        r = client.post("/bid-check", data={
            "player": "Connor McDavid",
            "bidders": "BOT",
            "price": "11.4",
            "highest_bidder": "BOT",
        })
        assert r.status_code == 200
        assert "bid-drop" in r.text
        assert "overpaying by" in r.text


class TestAssignSalaryIsLive:
    """Assign must post the price that's in the box right now.

    It used to carry a hidden salary field snapshotted at render time. The
    price input re-renders this panel on `change`, and `change` on a number
    input fires on BLUR — so clicking Assign straight after typing blurred the
    input, started a /bid-check re-render, and posted the PREVIOUS price. A
    wrong salary lands silently and skews every cap and ceiling after it.

    The race is browser event ordering and cannot be reproduced from
    TestClient. These pin the wiring so it can't silently revert.
    """

    def _panel(self, client) -> str:
        """A rendered auction panel with the Assign form showing.

        Break the only other bidder so BOT is last standing — same fixture as
        test_win_comes_with_an_assign_button, shared so the two can't drift.
        """
        with cannot_raise("HSM"):
            r = client.post("/bid-check", data={
                "player": "Connor McDavid",
                "bidders": "BOT,HSM",
                "price": "2.5",
                "highest_bidder": "BOT",
            })
            assert r.status_code == 200
            assert 'name="team" value="BOT"' in r.text, "Assign form must render"
            return r.text

    def test_panel_holds_no_salary_snapshot(self, client):
        """The assertion that pins the fix: no render-time salary to go stale."""
        assert 'name="salary"' not in self._panel(client)

    def test_assign_reads_the_price_input_at_submit_time(self, client):
        html = self._panel(client)
        assert "hx-vals=" in html, "Assign must supply salary at request time"
        assert 'document.getElementById("bid-price").value' in html

    def test_button_label_is_syncable(self, client):
        """shortcuts.js rewrites this span as the price changes, so the button
        never promises a price different from the one it will post."""
        html = self._panel(client)
        assert 'id="assign-price"' in html
        assert ">$2.5M<" in html, "label should start at the rendered price"


class TestCounterfactualVerdict:
    """The panel must say what to DO, and name the price it judged at.

    It used to end in "Delta: +8 points, -3.2M cap" — numbers with no verdict,
    and it never showed the price the comparison was run at, so "is that good?"
    was unanswerable from the panel.
    """

    def _delta(self, main, name: str) -> float:
        """The engine's roster delta for this player at his market price.

        Takes the price from `main._cf_price` rather than re-deriving it: the
        endpoint solves at the quantized price, and a hand-rolled copy here
        would predict the verdict from a different number than the one the
        panel was rendered with — which at a sign boundary is a test that
        fails for no real reason.
        """
        from optimizer import generate_counterfactual

        pool = main.auction_state.available_players
        cf = generate_counterfactual(
            pool[name], main._cf_price(name),
            main.auction_state.teams[main.MY_TEAM], pool, main.market_prices,
        )
        return cf.points_difference

    # Verdict wording keyed by the sign of the engine's delta. THREE entries,
    # not two: the panel has a break-even branch (see
    # `test_break_even_is_a_toss_up_not_a_skip`) and a two-way `if gain > 0 /
    # else` here silently demanded "Skip him at $" for a zero. The 2026-08-07
    # refresh drill produced exactly that — a goalie whose delta came out 0 on
    # perturbed points — and the test failed on correct behaviour.
    _VERDICTS = {
        1: ("buy", "Worth having at $", "lineup points over your best roster without him"),
        # The unconditional half of each sentence: the clauses naming an
        # alternative player are all `{% if alt %}`, so asserting on one would
        # fail whenever the counterfactual happens to find no replacement.
        0: ("even", "Toss-up at $", "scores the same with or without him"),
        -1: ("skip", "Skip him at $", "costs you"),
    }

    def test_verdict_follows_the_engine_every_way(self, client):
        """Each branch renders, and matches the sign of the engine's delta.

        Don't pin a player: points_difference is the roster delta AT THAT PRICE,
        so an elite player can be a "skip" (McDavid forced in at $9.5M costs
        lineup points elsewhere) while a mid-tier one is a "buy". Derive the
        expected branch instead of assuming it.

        Walks down the pool until both verdict branches have been seen rather
        than taking a fixed slice, because every hit is a MILP solve and today's
        data covers both inside the first few. A dataset where the top of the
        pool is all one way widens the sample instead of failing.
        """
        import main

        ranked = sorted(
            main.auction_state.available_players.values(),
            key=lambda p: -p.projected_points,
        )[:12]
        seen = set()
        for p in ranked:
            gain = self._delta(main, p.name)
            label, verdict, detail = self._VERDICTS[(gain > 0) - (gain < 0)]
            r = client.get(f"/explain/{p.name}")
            assert r.status_code == 200
            assert verdict in r.text, f"{p.name} scored {gain}, wanted {label}"
            assert detail in r.text
            seen.add(label)
            if {"buy", "skip"} <= seen:
                break
        # A toss-up is a legitimate extra, but it must not be the whole sample:
        # the two branches carrying an actual recommendation are the ones worth
        # pinning against the engine.
        assert {"buy", "skip"} <= seen, (
            f"sample of {len(ranked)} never covered both verdicts, got {seen}"
        )

    def _render_verdict(self, gain: int, alt: str | None = None) -> str:
        """Render the panel against a stubbed counterfactual.

        A zero delta is hard to conjure from the live pool, but trivial to
        state directly — and the branch is pure presentation.
        """
        from types import SimpleNamespace

        import main

        sol = SimpleNamespace(total_points=100, total_cost=30.0)
        alts = [SimpleNamespace(name=alt, position="F", projected_points=20)] if alt else []
        return main.templates.env.get_template("partials/explanation.html").render(
            counterfactual=SimpleNamespace(
                with_player=sol,
                without_player=sol,
                points_difference=gain,
                budget_difference=0.0,
                alternative_players=alts,
            ),
            cf_player=SimpleNamespace(name="Filler", position="F", projected_points=20),
            cf_price=1.2,
        )

    def test_break_even_is_a_toss_up_not_a_skip(self):
        """gain == 0 fell into the Skip branch and read "costs you 0 lineup
        points" — self-contradictory. Reachable late, when the players left
        are interchangeable and none of them moves the lineup."""
        html = self._render_verdict(0, alt="Someone Else")
        assert "Toss-up at $1.2M" in html
        assert "Someone Else does the same job" in html
        assert "costs you" not in html
        assert "Worth having" not in html

    def test_nonzero_deltas_still_pick_a_side(self):
        """The third branch must not swallow the two that carry the verdict."""
        assert "Worth having at $1.2M" in self._render_verdict(8)
        assert "Skip him at $1.2M" in self._render_verdict(-8)

    def test_verdict_names_the_price(self, client):
        """A points delta with no price attached can't be judged."""
        import main

        name = next(iter(main.auction_state.available_players))
        r = client.get(f"/explain/{name}")
        expected = round(main.market_prices[name], 1)
        # Anchored to the verdict's <strong>: a bare "$4.9M" would also match
        # the with/without roster costs rendered above it.
        assert f"at ${expected}M</strong>" in r.text


class TestPriceColumn:
    """One Price column, marked only when the market ceiling actually binds.

    market_price = min(model_price, ceiling), and the ceiling sits at
    MAX_SALARY while budgets are full — so two columns showed identical
    numbers for most of the auction and trained you to ignore both.
    """

    def test_single_price_header(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert ">Price<" in r.text
        assert ">Model $<" not in r.text and ">Market $<" not in r.text

    def _capped(self, main, name: str) -> bool:
        """The same flag _context puts on each bid_limits row."""
        return round(main.market_prices[name], 1) < round(
            main.model_prices[name].expected_price, 1
        )

    def test_capped_means_exactly_over_the_ceiling(self, client):
        """The RULE, not today's outcome.

        This used to assert that NOTHING is capped at full budgets, which held
        only because the priciest model price (~$9.5M) happens to sit under the
        full-budget ceiling ($11.4M). A pricier pool would have failed it with
        nothing wrong — it was on the backlog as data-coupled. Asserting the
        equivalence instead is both data-independent and strictly stronger: it
        catches a row capped when it should not be AND one uncapped when it
        should be.
        """
        import main

        self._assert_rule_holds(main, "full budgets")

    def _assert_rule_holds(self, main, label: str) -> int:
        """Check the equivalence and return how many rows are capped."""
        ceiling = main.market_info.market_ceiling
        wrong = [
            n for n in main.auction_state.available_players
            if self._capped(main, n)
            != (round(main.model_prices[n].expected_price, 1) > round(ceiling, 1))
        ]
        assert not wrong, (
            f"{label}: capped flag disagrees with the ${ceiling:.1f}M ceiling "
            f"for {wrong[:5]}"
        )
        return sum(
            1 for n in main.auction_state.available_players if self._capped(main, n)
        )

    def test_capped_flips_once_the_ceiling_bites(self, client):
        """Squeeze every opponent to a $3M ceiling; the top prices then cut.

        Also where the equivalence above gets its teeth. At full budgets the
        ceiling ($11.4M) sits over every model price, so both sides of that
        assertion are false for every row and it cannot fail — verified by
        mutation: capping a dollar low, and not capping at all, both leave it
        green. The rule only has content once something IS capped.

        **The ceiling is now solved for rather than approximated.** This used to
        set `penalties = 54.0` with a comment claiming "~$2.8M of cap", which
        ignored the salary already on the roster: every opponent's physical max
        fell under MIN_SALARY, they all dropped out of demand, and
        `compute_market_price` returned `MIN_SALARY` from its `floor_demand`
        branch **without reaching the ceiling line at all**. So the test named
        after the ceiling was exercising the floor, and both cap mutations
        survived it. `floor_demand` is asserted False for that reason.
        """
        import main

        model = {n: p.expected_price for n, p in main.model_prices.items()}
        priciest = max(model, key=model.get)
        target = 3.0
        assert model[priciest] > target, "need a player priced above the ceiling"

        saved = {c: t.penalties for c, t in main.auction_state.teams.items()}
        try:
            for code, t in main.auction_state.teams.items():
                if code == main.MY_TEAM:
                    continue
                # Invert physical_max_bid to land on `target` exactly, the same
                # way helpers.squeeze inverts total_salary: zero the penalties
                # first so total_salary reads the roster alone.
                t.penalties = 0.0
                t._invalidate_cache()
                wanted = target - MIN_SALARY + t.total_spots_remaining * MIN_SALARY
                t.penalties = round(SALARY_CAP - t.total_salary - wanted, 1)
                t._invalidate_cache()
            main._recompute()

            assert not main.market_info.floor_demand, (
                "every opponent dropped out, so prices come from the floor branch "
                "and the ceiling is never consulted"
            )
            assert main.market_info.market_ceiling == pytest.approx(target, abs=0.11)
            assert self._capped(main, priciest)
            capped = self._assert_rule_holds(main, "opponents squeezed")
            assert capped, "fixture stopped capping anything — the rule asserts nothing"
        finally:
            for code, pen in saved.items():
                main.auction_state.teams[code].penalties = pen
                main.auction_state.teams[code]._invalidate_cache()
            main._recompute()


class TestPanelContextIsolation:
    """Editing an opponent's roster must not leak them into BOT's panels.

    /toggle-bench and /adjust-salary used to override ctx["team"] to the edited
    team and render all_panels.html. But `team` defaults to BOT and feeds the
    Trade "I Give" dropdown and buyout controls, so editing an opponent loaded
    THEIR players into BOT's trade form — a wrong trade waiting to happen.

    Each case OPENS the opponent's panel first, and that line is the whole test.
    While the edited code travelled with the request, posting the edit was
    enough to make `viewed_team` an opponent; now the view is held in
    `main._viewed_team`, so a request that never opened SRL leaves it on BOT and
    a leak of `viewed_team` into another panel would leak BOT into BOT — a guard
    passing because there is nothing on the far side of it. Opening the panel is
    also the only way the edit happens for real: every one of these controls
    renders inside team_panel.html.
    """

    def _give_options(self, html: str) -> str:
        """The <select name="give_player"> block from the Trade panel."""
        start = html.index('name="give_player"')
        return html[start:html.index("</select>", start)]

    def test_toggle_bench_on_opponent_keeps_trade_panel_on_bot(self, client):
        import main

        client.get("/team-view/SRL")
        opponent = main.auction_state.teams["SRL"]
        bot = main.auction_state.teams["BOT"]
        victim = opponent.roster_players[0].name
        bot_players = {p.name for p in bot.roster_players}
        opponent_players = {p.name for p in opponent.roster_players}

        r = client.post("/toggle-bench", data={
            "team_code": "SRL", "player_name": victim,
        })
        assert r.status_code == 200
        options = self._give_options(r.text)
        assert any(n in options for n in bot_players), "Trade should offer BOT's players"
        leaked = [n for n in opponent_players - bot_players if n in options]
        assert not leaked, f"opponent players leaked into Trade 'I Give': {leaked}"

    def test_adjust_salary_on_opponent_keeps_trade_panel_on_bot(self, client):
        import main

        client.get("/team-view/MAC")
        opponent = main.auction_state.teams["MAC"]
        bot = main.auction_state.teams["BOT"]
        target = opponent.roster_players[0]
        bot_players = {p.name for p in bot.roster_players}
        opponent_players = {p.name for p in opponent.roster_players}

        r = client.post("/adjust-salary", data={
            "team_code": "MAC", "player_name": target.name,
            "new_salary": str(target.salary),
        })
        assert r.status_code == 200
        options = self._give_options(r.text)
        leaked = [n for n in opponent_players - bot_players if n in options]
        assert not leaked, f"opponent players leaked into Trade 'I Give': {leaked}"


class TestViewedTeamSurvivesEdits:
    """The other half of the 2026-08-05 fix above, finally built.

    Closing the leak cost the view: every roster edit posts from whichever panel
    is open, and returning the default context snapped it back to BOT, so
    auditing another team meant re-opening it after each edit.

    The two are one decision, which is why they sit together. `viewed_team` is
    the panel on screen; `team` stays BOT so the Trade and Buyout panels keep
    acting on BOT's roster. A change that satisfies this class by moving `team`
    fails the class above.

    Each case opens the team first, because that is the only way the edit can
    happen: every one of these controls is rendered INSIDE team_panel.html, so
    you cannot click Bench for a roster that is not on screen. The endpoints no
    longer take the view along with them — `main._viewed_team` holds it, and
    they simply do not disturb it — which is what also fixed their error
    branches, covered by TestTheViewSticks.
    """

    def _panel_team(self, html: str) -> str:
        """The team code the rendered team panel is showing."""
        panel = section_of(html, "team-panel")
        m = re.search(r"<h2[^>]*>[^(]*\(([A-Z]+)\)</h2>", panel)
        assert m, f"team panel has no identifiable header: {panel[:300]}"
        return m.group(1)

    def test_toggle_bench_stays_on_the_edited_team(self, client):
        client.get("/team-view/SRL")
        r = client.post("/toggle-bench", data={
            "team_code": "SRL", "player_name": a_roster_player("SRL").name,
        })
        assert r.status_code == 200
        assert self._panel_team(r.text) == "SRL"

    def test_adjust_salary_stays_on_the_edited_team(self, client):
        client.get("/team-view/SRL")
        p = a_roster_player("SRL")
        r = client.post("/adjust-salary", data={
            "team_code": "SRL", "player_name": p.name, "new_salary": str(p.salary),
        })
        assert r.status_code == 200
        assert self._panel_team(r.text) == "SRL"

    def test_move_to_minors_stays_on_the_edited_team(self, client):
        client.get("/team-view/SRL")
        p = a_roster_player("SRL")
        # Benched is the precondition for the ↓ Minors control (team_panel.html)
        client.post("/toggle-bench", data={"team_code": "SRL", "player_name": p.name})
        r = client.post("/move-to-minors", data={
            "team_code": "SRL", "player_name": p.name,
        })
        assert r.status_code == 200
        assert self._panel_team(r.text) == "SRL"

    def test_move_to_roster_stays_on_the_edited_team(self, client):
        import main
        client.get("/team-view/SRL")
        p = a_roster_player("SRL")
        client.post("/toggle-bench", data={"team_code": "SRL", "player_name": p.name})
        client.post("/move-to-minors", data={"team_code": "SRL", "player_name": p.name})
        assert any(m.name == p.name for m in main.auction_state.teams["SRL"].minor_players)

        r = client.post("/move-to-roster", data={
            "team_code": "SRL", "player_name": p.name,
        })
        assert r.status_code == 200
        assert self._panel_team(r.text) == "SRL"

    def test_trade_between_stays_on_the_initiating_team(self, client):
        """team_a is the panel the form was posted from, not a trade participant
        chosen at random — the hidden input is that panel's own code."""
        client.get("/team-view/SRL")
        r = client.post("/trade-between", data={
            "team_a": "SRL", "team_b": "MAC",
            "players_from_a": a_roster_player("SRL").name,
            "players_from_b": "",
        })
        assert r.status_code == 200
        assert toast_of(r).get("type") in ("success", "warning"), toast_of(r)
        assert self._panel_team(r.text) == "SRL"

    def test_assign_swaps_to_the_buyer(self, client):
        """Owner decision 2026-08-08, amending 2026-08-07.

        The original rule reset to BOT on every pick, "because reading an
        opponent's Cap Used as your own right after a pick lands is worse than
        re-opening their roster". That only ever bit on YOUR OWN pick — the
        moment you glance at the header — and that case still resets, which
        TestTheViewSticks::test_assign_returns_the_view_to_my_team covers. Here
        the buyer is MAC: nothing of BOT's moved, and the roster that just went
        stale is theirs.
        """
        import main
        client.post("/toggle-bench", data={
            "team_code": "SRL", "player_name": a_roster_player("SRL").name,
        })
        name = max(main.auction_state.available_players.values(),
                   key=lambda p: p.projected_points).name
        r = client.post("/assign", data={
            "player": name, "team": "MAC", "salary": "1.0",
        })
        assert r.status_code == 200
        assert self._panel_team(r.text) == "MAC"

    def test_undoing_an_opponents_roster_edit_keeps_them_on_screen(self, client):
        """The 2026-08-07 finding: /undo used to reset the view unconditionally.

        Opening SRL first is what makes this able to fail. The version before
        2026-08-11 asserted "BOT" without it — and with the view never moved off
        BOT, "the endpoint left it alone" and "the endpoint reset it to BOT" are
        the same answer, so it passed against both the bug and the fix. It was
        testing the autouse `default_viewed_team` fixture.
        """
        client.get("/team-view/SRL")
        client.post("/toggle-bench", data={
            "team_code": "SRL", "player_name": a_roster_player("SRL").name,
        })
        r = client.post("/undo")
        assert r.status_code == 200
        assert self._panel_team(r.text) == "SRL"

    def test_an_unknown_team_code_falls_back_to_my_team(self, client):
        """A bad code renders BOT's panel rather than 500ing.

        Note what this does NOT test: /toggle-bench validates and returns early,
        so `_context_viewing`'s own fallback is never reached from here. Deleting
        that fallback leaves this test green — measured. The branch belongs to
        /team-view, and test_edge_cases.test_team_view_nonexistent is what covers
        it.
        """
        r = client.post("/toggle-bench", data={
            "team_code": "FAKE", "player_name": "Nobody",
        })
        assert r.status_code == 200
        assert self._panel_team(r.text) == "BOT"

    def test_the_edited_panel_posts_back_its_own_team(self, client):
        """The hidden team_code inputs must follow the view.

        If the panel renders SRL's roster while its forms still carry BOT, the
        next Bench click edits a player BOT doesn't have — a wrong write, not
        just a wrong-looking panel.
        """
        panel = section_of(client.get("/team-view/SRL").text, "team-panel")
        codes = set(re.findall(r'name="team_code" value="([A-Z]+)"', panel))
        assert codes == {"SRL"}, f"forms post to {codes}, panel shows SRL"
        assert re.search(r'name="team_a" value="SRL"', panel), "trade form too"

    def test_buyout_dots_render_for_bot_only(self, client):
        """The scan is BOT-only by construction — _recompute_buyout_indicators
        scores every hypothetical against BOT's MILP total — so a placeholder on
        SRL's roster could only ever sit grey, reading as "not analyzed" when
        the answer is "this analysis isn't about you"."""
        mine = section_of(client.get("/team-view/BOT").text, "team-panel")
        assert 'id="bo-' in mine, "BOT's eligible players must keep their dots"

        theirs = section_of(client.get("/team-view/SRL").text, "team-panel")
        assert 'id="bo-' not in theirs, "an opponent's dots can never be filled"


class TestNominate:
    def test_nominate(self, client):
        """Nomination should return picks in auction control."""
        r = client.get("/nominate")
        assert r.status_code == 200
        assert "Auction" in r.text


class TestExplain:
    def test_explain_player(self, client):
        """Explain should return counterfactual."""
        r = client.get("/explain/Sidney Crosby")
        assert r.status_code == 200
        assert "Counterfactual" in r.text

    def test_explain_invalid(self, client):
        r = client.get("/explain/Nobody")
        assert r.status_code == 200


class TestTeamDone:
    def test_toggle_done(self, client):
        """Toggling done should work."""
        r = client.post("/team-done", data={"team_code": "MAC"})
        assert r.status_code == 200
        # Toggle back
        r = client.post("/team-done", data={"team_code": "MAC"})
        assert r.status_code == 200


class TestUndo:
    def test_undo(self, client):
        """Undo should restore previous state."""
        r = client.post("/undo")
        assert r.status_code == 200


class TestState:
    def test_state_json(self, client):
        """State endpoint should return JSON."""
        r = client.get("/state")
        assert r.status_code == 200
        data = r.json()
        assert "teams" in data
        assert "available_players" in data



class TestLognormalPdfPath:
    def test_returns_valid_svg_path(self):
        """PDF path should be a valid SVG path string."""
        from main import _lognormal_pdf_path

        curve_d, floor_bar = _lognormal_pdf_path(
            log_mu=1.0, sigma=0.3, p_floor=0.0,
            scale_max=8.0, min_salary=0.5,
        )
        assert curve_d.startswith("M ")
        assert "L " in curve_d
        assert curve_d.endswith("Z")
        assert floor_bar is None

    def test_floor_bar_when_p_floor_high(self):
        """Floor spike bar should appear when p_floor > 0.05."""
        from main import _lognormal_pdf_path

        _, floor_bar = _lognormal_pdf_path(
            log_mu=0.5, sigma=0.3, p_floor=0.5,
            scale_max=5.0, min_salary=0.5,
        )
        assert floor_bar is not None
        assert len(floor_bar) == 4

    def test_no_floor_bar_when_p_floor_low(self):
        """Floor spike bar should not appear when p_floor <= 0.05."""
        from main import _lognormal_pdf_path

        _, floor_bar = _lognormal_pdf_path(
            log_mu=1.0, sigma=0.3, p_floor=0.03,
            scale_max=8.0, min_salary=0.5,
        )
        assert floor_bar is None

    def test_sigma_zero_returns_empty(self):
        """Zero sigma should return empty path without crashing."""
        from main import _lognormal_pdf_path

        curve_d, floor_bar = _lognormal_pdf_path(
            log_mu=1.0, sigma=0.0, p_floor=0.0,
            scale_max=8.0, min_salary=0.5,
        )
        assert curve_d == ""
        assert floor_bar is None

    def test_sigma_negative_returns_empty(self):
        """Negative sigma should return empty path without crashing."""
        from main import _lognormal_pdf_path

        curve_d, floor_bar = _lognormal_pdf_path(
            log_mu=1.0, sigma=-0.1, p_floor=0.0,
            scale_max=8.0, min_salary=0.5,
        )
        assert curve_d == ""
        assert floor_bar is None


class TestPlayerChart:
    """The chart is mounted in two places, so the body must own no id.

    It used to carry `id="player-chart-container"` itself while
    `bid_limits.html` rendered an empty div with the same id as the table's
    swap target. htmx resolves a target by id and takes the first match, and
    `area-auction` precedes `area-players`, so during a live bid a chart link
    in the table rendered the chart into the bid panel in the other column.
    """

    def test_player_chart_valid(self, client):
        """Player chart should return SVG visualization."""
        r = client.get("/player-chart/Steven Stamkos")
        assert r.status_code == 200
        assert "Price Model" in r.text
        assert "<svg" in r.text
        assert "<path" in r.text

    def test_the_chart_body_carries_no_mount_id(self, client):
        """The property that makes two mounts legal."""
        r = client.get("/player-chart/Steven Stamkos")
        assert 'id="player-chart-container"' not in r.text, (
            "the chart body owns the mount id again — an innerHTML swap nests "
            "it inside the mount and duplicates the id"
        )

    def test_the_mount_appears_exactly_once_on_the_page(self, client):
        page = client.get("/").text
        assert page.count('id="player-chart-container"') == 1

    def test_unknown_player_does_not_leak_the_counterfactual_panel(self, client):
        """The failure path rendered explanation.html — a whole other panel.

        Both tests that covered this asserted `status_code == 200` and nothing
        else, so they passed on any response at all. That is why it survived.
        """
        r = client.get("/player-chart/Nobody")
        assert r.status_code == 200
        assert "Nobody" in r.text, "the empty state does not name the player"
        assert 'id="explanation"' not in r.text
        assert "<svg" not in r.text

    def test_unknown_player_response_is_small(self, client):
        """Size is the check that catches a whole-panel render generically.

        The counterfactual panel this used to return is ~2KB even when empty;
        an anchored id assertion only catches the one template that was wrong.
        """
        r = client.get("/player-chart/Nobody")
        assert len(r.text) < 300, f"expected an empty state, got {len(r.text)} bytes"


class TestBidCheckOnAPlayerItCannotFind:
    """A misspelled name must not vanish without a word.

    `/bid-check` used to answer the unknown-player case with the bare empty
    form, which fails silently in both directions the panel is driven from:

    * The "Start Auction" field is free text — `required` with a datalist, but
      NOT readonly, so any typo gets here. Its form swaps `#bid-panel`
      outerHTML, so the response replaced the panel with a blank form: the name
      you typed disappeared, no toast, 1178 bytes, nothing to read.
    * The price input carries `hx-select="#bid-advice"`, and that response had
      no such id, so htmx swapped **nothing at all** — a player who left the
      pool mid-bid left the panel frozen on advice that had stopped updating.

    Reachability is the part the backlog entry got wrong: it reasoned from the
    *live-auction* field, which is readonly, and never looked at the start form.
    """

    # Characters Jinja's autoescape rewrites, so a raw `name in r.text` is
    # false for them even when the app is perfectly right.
    _ESCAPED = re.compile(r"[<>&'\"]")

    def _missing(self, client) -> str:
        """A name the pool does not hold, preferring one escaping would mangle.

        Derived rather than invented — appending to a real name keeps it
        realistic, and the containment check makes a `players.csv` that somehow
        carries it fail loudly instead of quietly testing the found-player path.

        The preference is the load-bearing part. Two live pool names carry
        apostrophes (`Ryan O'Reilly`, `K'Andre Miller`) and Jinja renders `'` as
        `&#39;`, so the naive `f'value="{name}"' in r.text` this class was first
        written with is FALSE for them. It passed only because CSV order happens
        to put `Connor McDavid` first — a refresh that reordered two rows would
        have failed the suite on correct behaviour. Take the awkward case on
        purpose, and sort so the choice does not depend on file order either.
        """
        pool = client.get("/state").json()["available_players"]
        awkward = sorted(n for n in pool if self._ESCAPED.search(n))
        name = f"{(awkward or sorted(pool))[0]} Jr."
        assert name not in pool, f"{name!r} is in the pool — pick another"
        return name

    def _typed_back(self, response) -> str:
        """What the player input actually holds, with escaping undone.

        Reads the property that matters — "the box still has what I typed" —
        rather than a substring of the markup, so the assertion is right for
        every name instead of only the ones that need no escaping.
        """
        m = re.search(r'name="player"[^>]*\svalue="([^"]*)"', response.text)
        assert m is not None, "the form rendered no player input carrying a value"
        return html.unescape(m.group(1))

    def test_it_says_the_player_was_not_found(self, client):
        name = self._missing(client)
        r = client.post("/bid-check", data={"player": name, "bidders": "", "price": "0.5"})
        assert r.status_code == 200
        assert name in html.unescape(r.text), (
            "the response does not name the player that wasn't found, so the "
            "operator is told nothing about why the box emptied"
        )
        assert "No player named" in r.text

    def test_it_gives_the_price_input_something_to_swap(self, client):
        """The load-bearing half.

        Without an `#bid-advice` in this response, htmx's hx-select finds no
        match and performs no swap — the difference between "the panel shows an
        error" and "the panel does nothing and looks fine".
        """
        name = self._missing(client)
        r = client.post("/bid-check", data={"player": name, "bidders": "", "price": "0.5"})
        assert 'id="bid-advice"' in r.text, (
            "the unknown-player response carries no #bid-advice, so a price "
            "change on a sold player swaps nothing and the advice goes stale "
            "with no sign of it"
        )

    def test_the_typed_name_survives_so_a_typo_can_be_corrected(self, client):
        name = self._missing(client)
        r = client.post("/bid-check", data={"player": name, "bidders": "", "price": "0.5"})
        assert self._typed_back(r) == name, (
            f"the box came back holding {self._typed_back(r)!r} instead of "
            f"{name!r}, so a one-letter typo costs a full retype mid-auction"
        )

    def test_a_player_who_leaves_the_pool_mid_bid_still_answers(self, client):
        """The second way in, and a different shape from a typo.

        Here the field held a real name when the bid started; the player was
        sold in another tab. The price input's next `/bid-check` used to come
        back with no `#bid-advice`, so hx-select matched nothing and the panel
        kept showing advice for a player who was gone — no error, no change,
        nothing to notice.
        """
        import main

        p = max(main.auction_state.available_players.values(),
                key=lambda q: q.projected_points)
        live = client.post("/bid-check", data={
            "player": p.name, "bidders": "BOT,SRL", "price": "3.0"})
        assert 'id="bid-advice"' in live.text, "precondition: a live bid has advice"

        del main.auction_state.available_players[p.name]
        gone = client.post("/bid-check", data={
            "player": p.name, "bidders": "BOT,SRL", "price": "3.1"})
        assert 'id="bid-advice"' in gone.text, (
            "no #bid-advice for a player who left the pool, so the price input "
            "swaps nothing and the panel keeps showing his old advice"
        )
        assert p.name in html.unescape(gone.text)

    def test_a_quiet_page_carries_no_bid_advice_block(self, client):
        """The other side of reusing the id: it must appear only on the two
        branches that own it, or `GET /` would hold a stray swap target.

        Anchored on the panel being there at all — an absence assertion with
        nothing positive beside it goes green on a `GET /` that returned an
        error page, which is the shape of the three assert-nothing tests this
        suite carried for months.
        """
        page = client.get("/").text
        assert 'id="bid-panel"' in page, "GET / did not render the bid panel"
        assert 'id="bid-advice"' not in page


class TestTeamView:
    """The success path of /team-view renders partials/team_detail.html, which
    imports the player_label macro. test_edge_cases.test_team_view_nonexistent
    only exercises the t-is-None fallback (roster_panel.html), so a missing
    macro import in team_detail.html slips past CI. These tests hit a real
    team to lock the rendered template in place."""

    def test_team_view_valid_renders_team_detail(self, client):
        r = client.get("/team-view/BOT")
        assert r.status_code == 200
        # team_detail.html section headers — proves we hit the success path,
        # not the roster_panel.html fallback.
        assert "Trade Between Teams" in r.text

    @pytest.mark.parametrize("code", ["BOT", "SRL", "MAC", "LGN", "JHN"])
    def test_team_view_each_real_team(self, client, code):
        r = client.get(f"/team-view/{code}")
        assert r.status_code == 200
        assert code in r.text


class TestSetNominator:
    def test_set_nominator_valid(self, client):
        """Setting a valid nominator should update auction control."""
        r = client.post("/set-nominator", data={"team_code": "LGN"})
        assert r.status_code == 200
        assert "Auction" in r.text

    def test_set_nominator_invalid(self, client):
        """Setting an invalid team code should not crash."""
        r = client.post("/set-nominator", data={"team_code": "FAKE"})
        assert r.status_code == 200
        # Was `"Nomination" in r.text`, which matched only the HTML comment
        # "Nomination recommendations" — moving a comment broke it, and it
        # would have passed on any fragment that happened to carry the word.
        assert 'id="nomination-panel"' in r.text


class TestBidSessionSurvives:
    """The live bidding session exists only in #bid-panel's DOM.

    Player, price and bidder toggles are never persisted server-side, so any
    response that replaces that region loses them. Before the panel was split,
    /nominate and /set-nominator both returned the whole #auction-control from
    base context and wiped the session — and /nominate fires on a bare `n`
    keypress, whose guard only covers INPUT/TEXTAREA/SELECT, so focus on a
    bidder-logo button left it live.

    These assert the STRUCTURAL property rather than any rendered value: an
    endpoint cannot clobber a region it does not return.
    """

    def test_nominate_cannot_touch_the_bid_region(self, client):
        r = client.get("/nominate")
        assert r.status_code == 200
        assert 'id="nomination-panel"' in r.text
        assert 'id="bid-form"' not in r.text
        assert 'id="bidder-logos"' not in r.text

    @pytest.mark.parametrize("team_code", ["LGN", "FAKE"])
    def test_set_nominator_cannot_touch_the_bid_region(self, client, team_code):
        """Both paths — the valid one and the rejected team code."""
        r = client.post("/set-nominator", data={"team_code": team_code})
        assert r.status_code == 200
        assert 'id="nomination-panel"' in r.text
        assert 'id="bid-form"' not in r.text
        assert 'id="bidder-logos"' not in r.text

    def test_bid_check_cannot_touch_the_nomination_region(self, client):
        """The converse: a price change must not drop a nomination pick."""
        r = client.post("/bid-check", data={
            "player": "Connor McDavid", "price": "3.0", "bidders": "BOT,LGN,SRL",
        })
        assert r.status_code == 200
        assert 'id="bid-panel"' in r.text
        assert 'id="nomination-panel"' not in r.text

    def test_bid_check_does_not_resend_the_player_datalist(self, client):
        """~700 options on the hottest endpoint in the app.

        The datalist lives in the shell and resolves by document id, so the
        bid form still finds it without it riding along on every keystroke.
        """
        bid = client.post("/bid-check", data={
            "player": "Connor McDavid", "price": "3.0", "bidders": "BOT,LGN",
        })
        assert 'id="player-list"' not in bid.text
        assert 'list="player-list"' in bid.text, "the input must still reference it"
        assert 'id="player-list"' in client.get("/").text

    def test_full_render_still_contains_both_panels(self, client):
        """An include dropped in the split would only show up here."""
        r = client.get("/")
        for marker in ('id="auction-control"', 'id="nomination-panel"',
                       'id="bid-panel"', 'id="player-list"'):
            assert marker in r.text, f"{marker} missing from the full page"


class TestAssignSurvivesAPriceChange:
    """The price input must not be able to delete the Assign button.

    `change` on a number input fires on BLUR, and clicking Assign is what
    blurs it — so that request is triggered by the very click it can destroy.
    If the response replaces the region Assign lives in before mouseup, the
    browser never fires `click` (mousedown and mouseup landed on different
    elements) and the pick is silently not recorded.

    This was dormant while /bid-check took ~1000ms: the swap landed ~900ms
    after the click finished. Caching the marginal value took it to ~9ms,
    squarely inside a 70-150ms click, so the swap is now scoped to the advice
    block. Structural again — the request cannot remove what it does not
    return into.
    """

    def _bid(self, client):
        return client.post("/bid-check", data={
            "player": "Connor McDavid", "price": "3.0", "bidders": "BOT,LGN,SRL",
        })

    def test_price_input_swaps_only_the_advice_block(self, client):
        html = self._bid(client).text
        price_input = re.search(r'<input[^>]*id="bid-price"[^>]*>', html, re.S)
        assert price_input, "the price input is gone"
        attrs = price_input.group(0)
        assert 'hx-target="#bid-advice"' in attrs, (
            f"price input must not swap a region containing Assign: {attrs}"
        )
        assert 'hx-select="#bid-advice"' in attrs, (
            "without hx-select the full bid panel would be swapped in anyway"
        )

    def test_the_advice_block_excludes_the_assign_button(self, client):
        """The whole point: the swapped region must not contain Assign."""
        html = self._bid(client).text
        start = html.find('id="bid-advice"')
        assert start != -1, "#bid-advice is missing — the price input has no target"
        # The advice block is a leaf-ish div; take everything up to the bid form
        # that follows it, which is the outer bound of what the swap can replace.
        block = html[start:html.find('id="bid-form"', start)]
        assert "/assign" not in block, "Assign is inside the region a price change replaces"
        assert 'id="bid-price"' not in block, "the price input replaces itself"
        assert 'id="bidder-logos"' not in block, "bidder toggles are inside the swapped region"

    def test_assign_and_the_price_input_are_still_both_present(self, client):
        """Narrowing the swap must not have dropped anything from the panel."""
        html = self._bid(client).text
        for marker in ('id="bid-advice"', 'id="bid-price"', 'id="bid-form"',
                       'id="bidder-logos"'):
            assert marker in html, f"{marker} missing after the swap was narrowed"


class TestBuyout:
    def test_buyout_check(self, client):
        """Buyout check should return preview."""
        r = client.get(f"/buyout-check/{a_buyout_candidate().name}")
        assert r.status_code == 200
        assert "Buyout" in r.text

    def test_buyout_check_invalid(self, client):
        r = client.get("/buyout-check/Nobody")
        assert r.status_code == 200


class TestRoundThreeMutators:
    """Round 3 mutators: minors movement, scenario load, change_log + cascade."""

    def _draft_to(self, client, team: str, player: str = "Artemi Panarin", salary: str = "5.0"):
        """Draft a player to a team so it has at least one acquired player to test on."""
        r = client.post("/assign", data={"player": player, "team": team, "salary": salary})
        assert r.status_code == 200
        return player

    def test_move_to_minors_round_trip(self, client):
        client.post("/reset")
        player = self._draft_to(client, "BOT")

        r = client.post("/toggle-bench", data={"team_code": "BOT", "player_name": player})
        assert r.status_code == 200

        r = client.post("/move-to-minors", data={"team_code": "BOT", "player_name": player})
        assert r.status_code == 200
        state = client.get("/state").json()
        bot = state["teams"]["BOT"]
        assert player not in [p["name"] for p in bot["acquired_players"]]
        assert player in [p["name"] for p in bot["minor_players"]]
        kinds = [c["kind"] for c in state["change_log"]]
        assert "move-to-minors" in kinds

        r = client.post("/move-to-roster", data={"team_code": "BOT", "player_name": player})
        assert r.status_code == 200
        state = client.get("/state").json()
        bot = state["teams"]["BOT"]
        assert player in [p["name"] for p in bot["acquired_players"]]
        assert player not in [p["name"] for p in bot["minor_players"]]
        kinds = [c["kind"] for c in state["change_log"]]
        assert kinds.count("move-to-roster") == 1
        assert kinds.count("move-to-minors") == 1

    def test_move_to_minors_invalid_player(self, client):
        client.post("/reset")
        before = client.get("/state").json()
        r = client.post("/move-to-minors", data={"team_code": "BOT", "player_name": "Nobody"})
        assert r.status_code == 200
        after = client.get("/state").json()
        # Roster unchanged
        assert before["teams"]["BOT"]["acquired_players"] == after["teams"]["BOT"]["acquired_players"]
        # No change_log entry was added
        kinds = [c["kind"] for c in after["change_log"]]
        assert "move-to-minors" not in kinds

    def test_move_to_minors_active_player_rejected(self, client):
        client.post("/reset")
        player = self._draft_to(client, "BOT")

        r = client.post("/move-to-minors", data={"team_code": "BOT", "player_name": player})
        assert r.status_code == 200
        state = client.get("/state").json()
        bot = state["teams"]["BOT"]
        assert player in [p["name"] for p in bot["acquired_players"]]
        assert player not in [p["name"] for p in bot["minor_players"]]
        kinds = [c["kind"] for c in state["change_log"]]
        assert "move-to-minors" not in kinds

    def test_load_scenario_valid(self, client):
        client.post("/reset")
        r = client.post("/load-scenario", data={"name": "goalie-asymmetry"})
        assert r.status_code == 200
        state = client.get("/state").json()
        # Some non-BOT team should have a goalie in acquired_players
        found_goalie = False
        for code, team in state["teams"].items():
            if code == "BOT":
                continue
            if any(p["position"] == "G" for p in team["acquired_players"]):
                found_goalie = True
                break
        assert found_goalie

    def test_load_scenario_undo_returns_to_prior_state(self, client):
        client.post("/reset")
        # Baseline: how many goalies has BOT got, and total acquired across non-BOT?
        before = client.get("/state").json()
        before_acquired = sum(
            len(t["acquired_players"])
            for c, t in before["teams"].items() if c != "BOT"
        )

        client.post("/load-scenario", data={"name": "goalie-asymmetry"})
        loaded = client.get("/state").json()
        loaded_acquired = sum(
            len(t["acquired_players"])
            for c, t in loaded["teams"].items() if c != "BOT"
        )
        assert loaded_acquired > before_acquired

        # Undo should restore the pre-scenario acquired counts
        r = client.post("/undo")
        assert r.status_code == 200
        restored = client.get("/state").json()
        restored_acquired = sum(
            len(t["acquired_players"])
            for c, t in restored["teams"].items() if c != "BOT"
        )
        assert restored_acquired == before_acquired

    def test_load_scenario_unknown(self, client):
        client.post("/reset")
        before = client.get("/state").json()
        r = client.post("/load-scenario", data={"name": "not-a-scenario"})
        assert r.status_code == 200
        after = client.get("/state").json()
        assert before["teams"] == after["teams"]

    def test_adjust_salary_returns_full_app(self, client):
        client.post("/reset")
        player = self._draft_to(client, "BOT", salary="3.0")
        r = client.post("/adjust-salary", data={
            "team_code": "BOT",
            "player_name": player,
            "new_salary": "4.5",
        })
        assert r.status_code == 200
        # Cascade contract: response is the full panel grid, not just team panel
        assert "League State" in r.text
        assert "Available Players" in r.text or "bid-limits" in r.text

    def test_adjust_salary_logs_change(self, client):
        client.post("/reset")
        player = self._draft_to(client, "BOT", salary="3.0")
        client.post("/adjust-salary", data={
            "team_code": "BOT", "player_name": player, "new_salary": "4.5",
        })
        kinds = [c["kind"] for c in client.get("/state").json()["change_log"]]
        assert "adjust-salary" in kinds

    def test_toggle_bench_logs_change(self, client):
        client.post("/reset")
        player = self._draft_to(client, "BOT")
        client.post("/toggle-bench", data={"team_code": "BOT", "player_name": player})
        kinds = [c["kind"] for c in client.get("/state").json()["change_log"]]
        assert "toggle-bench" in kinds

    def test_team_done_logs_change(self, client):
        client.post("/reset")
        client.post("/team-done", data={"team_code": "MAC"})
        kinds = [c["kind"] for c in client.get("/state").json()["change_log"]]
        assert "team-done" in kinds


class TestOverCapRosterEdits:
    """Roster edits that can push a team over the cap must say so.

    Owner decision 2026-08-06, first applied to trades and extended here: warn,
    do not refuse. The league permits temporary over-cap states and resolves
    them with buyouts, so blocking would stop a legal manoeuvre. The bug is that
    an accidental over-cap edit looks exactly like a deliberate one.
    """

    def _cap_free_minor(self, code: str):
        """The priciest minor whose salary is NOT already on `code`'s cap.

        Group A-E, i.e. the ordinary case: 145 of the 149 minors at reset. Their
        salary lands on the cap only once they are recalled, which is the whole
        gap under test.
        """
        import main
        team = main.auction_state.teams[code]
        cap_free = [m for m in team.minor_players if m.group not in MINOR_CAP_GROUPS]
        assert cap_free, f"{code} has no cap-free minor to recall"
        return max(cap_free, key=lambda m: m.salary)

    def _recall(self, client, code: str, name: str):
        r = client.post("/move-to-roster", data={
            "team_code": code, "player_name": name})
        assert r.status_code == 200
        return r

    def test_recall_over_cap_warns_and_names_the_team(self, client):
        minor = self._cap_free_minor("BOT")
        squeeze("BOT", headroom=minor.salary - 0.5)

        toast = toast_of(self._recall(client, "BOT", minor.name))

        assert toast.get("type") == "warning", toast
        assert "BOT $0.5M over cap" in toast.get("message", ""), toast
        assert minor.name in toast.get("message", ""), toast

    def test_recall_over_cap_still_happens(self, client):
        """The owner decision, asserted on its own: warned, not refused."""
        minor = self._cap_free_minor("BOT")
        squeeze("BOT", headroom=minor.salary - 0.5)

        self._recall(client, "BOT", minor.name)

        bot = client.get("/state").json()["teams"]["BOT"]
        # Active roster, not `acquired_players` specifically: every minor at
        # reset was on an FCHL team before the auction, so he recalls back into
        # `keeper_players` (2026-08-08). The claim here is that the over-cap
        # recall HAPPENED, and reading one list made that claim depend on
        # provenance, which this test is not about.
        on_roster = [p["name"] for p in bot["keeper_players"] + bot["acquired_players"]]
        assert minor.name in on_roster
        assert minor.name not in [p["name"] for p in bot["minor_players"]]

    def test_legal_recall_stays_silent(self, client):
        """The control — and a pin on the deliberate lack of a success toast.

        This endpoint had no toast at all before the cap check; the re-rendered
        panels already show the move. Adding a green one would be a UX change
        nobody asked for, so a legal recall must attach nothing.
        """
        minor = self._cap_free_minor("BOT")  # BOT has ~$26M of room at reset

        r = self._recall(client, "BOT", minor.name)

        assert r.headers.get("HX-Trigger") is None, r.headers.get("HX-Trigger")

    def test_recalling_an_auto_routed_draftee_is_cap_neutral(self, client):
        """Group 2/3 minors already count on the cap, so recall cannot add to it.

        Proves the warning tracks `counts_on_cap` rather than merely firing
        whenever a recall happens near the cap. Squeezed to $0.1M of room —
        anything that charged the salary again would blow through it.
        """
        import main
        team = main.auction_state.teams["BOT"]
        already_counted = next(
            m for m in team.minor_players if m.group in MINOR_CAP_GROUPS)
        squeeze("BOT", headroom=0.1)

        r = self._recall(client, "BOT", already_counted.name)

        assert r.headers.get("HX-Trigger") is None, r.headers.get("HX-Trigger")

    def _cheapest_bot_player(self):
        """Cheapest player on BOT's active roster.

        Cheapest so the raises below stay well under MAX_SALARY — a clamp there
        would add a second note and muddy which warning is being asserted.
        """
        import main
        return min(main.auction_state.teams["BOT"].roster_players,
                   key=lambda p: p.salary)

    def _adjust(self, client, name: str, new_salary: float):
        r = client.post("/adjust-salary", data={
            "team_code": "BOT", "player_name": name, "new_salary": str(new_salary)})
        assert r.status_code == 200
        return r

    def test_adjust_salary_over_cap_warns(self, client):
        """`_legal_salary` clamps to MIN/MAX/increment but knows nothing of the cap."""
        p = self._cheapest_bot_player()
        squeeze("BOT", headroom=1.0)

        new_salary = round(p.salary + 1.5, 1)
        toast = toast_of(self._adjust(client, p.name, new_salary))

        assert toast.get("type") == "warning", toast
        # Named subject, not a bare fact about the team: with no clamp note to
        # lead, the cap note on its own would not say what caused it.
        assert toast.get("message") == (
            f"{p.name} set to ${new_salary}M — BOT $0.5M over cap"
        ), toast

    def test_adjust_salary_reports_clamp_and_overage_together(self, client):
        """One toast, both clauses.

        A fat-fingered figure is often off-increment AND too big at once. The
        early `return` this replaced would have shipped the clamp note and
        dropped the cap note on the floor — the more serious of the two.
        """
        p = self._cheapest_bot_player()
        squeeze("BOT", headroom=1.0)

        from main import _legal_salary
        typed = round(p.salary + 1.55, 2)
        # Assert the precondition rather than trust the arithmetic: which way
        # $x.x5 quantizes depends on its float repr, and a data refresh moving
        # the cheapest salary could land on an already-legal value, leaving this
        # test passing for the wrong reason.
        assert _legal_salary(typed) != typed, typed

        message = toast_of(self._adjust(client, p.name, typed)).get("message", "")

        assert "adjusted from" in message, message
        assert "over cap" in message, message

    def test_adjust_salary_within_cap_keeps_its_old_behaviour(self, client):
        """The control: neither wording nor silence changed for legal input."""
        p = self._cheapest_bot_player()  # BOT has ~$26M of room at reset

        legal = toast_of(self._adjust(client, p.name, round(p.salary + 1.0, 1)))
        assert legal == {}, legal

        off_increment = toast_of(self._adjust(client, p.name, 2.55))
        assert off_increment.get("type") == "warning", off_increment
        assert off_increment.get("message") == (
            f"{p.name} set to $2.5M (adjusted from $2.55M)"
        ), off_increment

    def test_assign_over_cap_warns(self, client):
        """The safety net, and the `to_minors or over` tier alongside it."""
        import main
        pool = sorted(main.auction_state.available_players.values(),
                      key=lambda p: -p.projected_points)

        legal = toast_of(client.post("/assign", data={
            "player": pool[0].name, "team": "BOT", "salary": "2.0"}))
        assert legal.get("type") == "success", legal

        squeeze("BOT", headroom=1.0)
        toast = toast_of(client.post("/assign", data={
            "player": pool[1].name, "team": "BOT", "salary": "2.0"}))

        assert toast.get("type") == "warning", toast
        assert "BOT $1.0M over cap" in toast.get("message", ""), toast
        assert f"{pool[1].name} → BOT at $2.0M" in toast.get("message", ""), toast


class TestTheViewSticks:
    """Which roster is on screen must survive anything that is not a pick.

    Before the view moved server-side, each endpoint that rendered
    all_panels.html had to pass the team code along, and the ones that forgot
    threw you back to BOT: /team-done, /trade-execute, and — worst — the ERROR
    branch of all five roster-edit endpoints, so a failed salary fix cost you
    the roster you were auditing at the moment you most needed to look at it.

    `_viewed_team` is now read in `_context`, so there is nothing left to
    forget. These pin the behaviour rather than the mechanism: every case goes
    through the HTTP surface and asserts on the rendered panel.
    """

    @pytest.fixture
    def viewing_srl(self, client):
        """Open an opponent's roster, the way clicking their row does."""
        r = client.get("/team-view/SRL")
        assert "(SRL)" in section_of(r.text, "team-panel")
        yield "SRL"
        client.get(f"/team-view/{'BOT'}")

    def _panel_team(self, html: str) -> str:
        panel = section_of(html, "team-panel")
        m = re.search(r"\((\w{3})\)", panel)
        assert m, "no team code in the team panel"
        return m.group(1)

    def test_team_done_keeps_the_view(self, client, viewing_srl):
        """The backlog entry: a League State toggle is not a view change."""
        r = client.post("/team-done", data={"team_code": "GVR"})
        assert self._panel_team(r.text) == "SRL"
        client.post("/team-done", data={"team_code": "GVR"})

    def test_a_failed_roster_edit_keeps_the_view(self, client, viewing_srl):
        """The unflagged one, and the reason this was worth doing.

        A player traded away between render and click returns a warning — and
        used to return you to BOT with it, right when you need to look again at
        what you just tried to edit.
        """
        r = client.post("/adjust-salary", data={
            "team_code": "SRL", "player_name": "Nobody At All", "new_salary": "2.0",
        })
        assert "no longer on SRL" in toast_of(r)["message"]
        assert self._panel_team(r.text) == "SRL"

    def test_an_unknown_team_edit_keeps_the_view(self, client, viewing_srl):
        """The other error branch — validation rejects before touching state."""
        r = client.post("/toggle-bench", data={
            "team_code": "FAKE", "player_name": "Nobody",
        })
        assert self._panel_team(r.text) == "SRL"

    def test_assign_returns_the_view_to_my_team(self, client, viewing_srl):
        """Your OWN pick still resets to BOT — the half 2026-08-08 preserved.

        Reading an opponent's Cap Used as your own right after a pick lands is
        worse than having to re-open their roster. That is the whole of the
        2026-08-07 reasoning, and it is about the pick being YOURS; the
        opponent-pick half is the test below.
        """
        import main

        player = next(iter(main.auction_state.available_players))
        r = client.post("/assign", data={
            "player": player, "team": "BOT", "salary": "1.0",
        })
        assert self._panel_team(r.text) == "BOT"
        client.post("/undo")

    def test_an_opponents_pick_swaps_the_panel_to_them(self, client, viewing_srl):
        """Owner decision 2026-08-08: the view follows the buyer.

        Viewing SRL and assigning to MAC has to land on MAC, not on either the
        team you were reading or your own — so this distinguishes the new rule
        from both the old one and a do-nothing.
        """
        import main

        player = next(iter(main.auction_state.available_players))
        r = client.post("/assign", data={
            "player": player, "team": "MAC", "salary": "1.0",
        })
        assert self._panel_team(r.text) == "MAC"

    def test_undoing_an_opponents_pick_shows_them(self, client, viewing_srl):
        """/undo mirrors the view policy of the action it reverted.

        The interposed /team-view/GVR is what makes this able to fail: the
        assign already left the view on MAC, so without moving it away first,
        "pointed at the reverted pick's buyer" and "left alone" agree.
        """
        import main

        player = next(iter(main.auction_state.available_players))
        assign(client, player, "MAC", 1.0)
        client.get("/team-view/GVR")
        r = client.post("/undo")
        assert self._panel_team(r.text) == "MAC"

    def test_undoing_a_trade_between_two_teams_leaves_the_view_alone(
        self, client, viewing_srl
    ):
        """A trade moves two rosters, so there is no single team to point at.

        What this DOES catch: an unconditional reset (the pre-2026-08-11 bug),
        and a denylist that let "trade" through while `_view_team` had no guard —
        /trade-between logs `team_code` as f"{source}→{dest}", so the view would
        become "SRL→MAC" and `_context`'s silent fallback would render BOT.

        What it does NOT catch, deliberately recorded because the first draft of
        this docstring claimed otherwise: widening the allowlist to include
        "trade" on its own. `_view_team("SRL→MAC")` is rejected by the guard, so
        the view stays GVR and this still passes. The allowlist and the guard are
        belt-and-braces for each other here; the test below is the one that pins
        the allowlist alone, because /trade-execute logs REAL team codes.
        """
        import main

        victim = main.auction_state.teams["SRL"].roster_players[0].name
        r = client.post("/trade-between", data={
            "team_a": "SRL", "team_b": "MAC",
            "players_from_a": victim, "players_from_b": "",
        })
        assert toast_of(r).get("type") in ("success", "warning"), toast_of(r)
        client.get("/team-view/GVR")
        r = client.post("/undo")
        assert "Undid trade" in toast_of(r)["message"], toast_of(r)
        assert self._panel_team(r.text) == "GVR"

    def test_undoing_a_trade_execute_leaves_the_view_alone(self, client, viewing_srl):
        """The case that pins the ALLOWLIST rather than the guard.

        /trade-execute logs `trade_out`/`trade_in` with real team codes, so
        allowlisting either one moves the view to a live team and this goes red —
        which is exactly what the /trade-between case above cannot detect. With a
        `source_team` the last record written is a `trade_out` for that opponent,
        so the wrong behaviour would land on a THIRD team, not on BOT.
        """
        import main

        mine = main.auction_state.teams["BOT"].roster_players[0]
        source = next(c for c, t in main.auction_state.teams.items()
                      if c not in ("BOT", "GVR") and t.roster_players)
        incoming = main.auction_state.teams[source].roster_players[0]

        client.post("/trade-evaluate", data={
            "give_player": [mine.name],
            "source_team": source,
            "receive_player": [json.dumps({
                "name": incoming.name,
                "position": incoming.position,
                "salary": incoming.salary,
                "projected_points": incoming.projected_points,
            })],
        })
        assert main.last_trade_eval is not None, "the trade form proposed nothing"
        r = client.post("/trade-execute",
                        data={"trade_id": main.last_trade_eval.trade_id})
        assert toast_of(r).get("type") != "error", toast_of(r)

        client.get("/team-view/GVR")
        r = client.post("/undo")
        assert "Undid trade_" in toast_of(r)["message"], toast_of(r)
        assert self._panel_team(r.text) == "GVR"

    def test_undoing_a_team_done_toggle_leaves_the_view_alone(
        self, client, viewing_srl
    ):
        """The case the /undo change-log comment argues from, finally pinned.

        `team-done` is a `ChangeRecord` kind, and unlike every roster edit it is
        posted from `league_state.html` — so its `team_code` can name a team you
        are NOT looking at. That makes it the one construction that tells
        "mirror pre_chg[-1].team_code" apart from "leave the view alone": here
        the record says GVR while the panel says SRL, and mirroring would swap
        you to an uninvolved third team, exactly what the 2026-08-07 fix removed
        from the forward path.

        Added during a 2026-08-11 grill, which measured that mirroring the change
        log passed the ENTIRE suite (667 tests) — the six-line comment forbidding
        it was the only thing standing behind the behaviour.
        """
        client.post("/team-done", data={"team_code": "GVR"})
        r = client.post("/undo")
        assert "GVR" in toast_of(r)["message"], toast_of(r)
        assert self._panel_team(r.text) == "SRL", (
            "undo mirrored a team-done onto the panel — GVR was never on screen"
        )
        assert client.get("/state").json()["teams"]["GVR"]["is_done"] is False, (
            "the toggle itself did not revert, so this proved nothing"
        )

    def test_undoing_an_edit_you_navigated_away_from_stays_put(
        self, client, viewing_srl
    ):
        """The accepted gap, pinned so it stays a decision rather than a drift.

        Edit an opponent, open somebody else, then Ctrl+Z: the view stays where
        you last put it. Your /team-view click is newer information than the log.
        Mirroring the change log would send you back to SRL instead — the other
        mutant the test above catches, from the opposite direction.
        """
        victim = a_roster_player("SRL").name
        client.post("/toggle-bench", data={
            "team_code": "SRL", "player_name": victim,
        })
        client.get("/team-view/GVR")
        r = client.post("/undo")
        assert "Undid:" in toast_of(r)["message"], toast_of(r)
        assert self._panel_team(r.text) == "GVR"

    def test_undoing_a_buyout_shows_my_team(self, client, viewing_srl):
        """A buyout can only ever touch BOT, so its reversal belongs on BOT.

        Worth its own case rather than folding into the draft one: the Penalties
        tile is conditional on `viewed_team.penalties > 0`, so undoing a buyout
        makes a tile appear or vanish on YOUR panel. Watching nothing change on
        SRL while ~half a salary moves on your own cap is the mismatch between
        an action and its undo that the 2026-08-07 finding was about.
        """
        victim = a_buyout_candidate().name
        r = client.post("/buyout", data={"player": victim})
        assert toast_of(r).get("type") == "success", toast_of(r)
        client.get("/team-view/SRL")
        r = client.post("/undo")
        assert self._panel_team(r.text) == "BOT"

    def test_nothing_to_undo_leaves_the_view_alone(self, client, viewing_srl):
        """No action reverted means no view policy to mirror.

        Relies on the function-scoped `client` from conftest.py leaving the
        snapshot chain empty — with a chain carried over, /undo would pop
        somebody else's snapshot and this would be a test of that instead.
        """
        r = client.post("/undo")
        assert toast_of(r)["message"] == "Nothing to undo", toast_of(r)
        assert self._panel_team(r.text) == "SRL"

    def test_the_view_is_always_a_live_team_code(self, client, viewing_srl):
        """`_view_team` validates, so the global can never hold a dead code.

        Called directly, and the docstring says so rather than dressing this as
        an endpoint test: no HTTP path can reach it with a bad code today
        (/assign validates the buyer at the top, and the only other callers pass
        MY_TEAM or an allowlisted log field). The guard exists because
        `_context`'s `teams.get(_viewed_team, team)` fallback is SILENT — a dead
        code would render BOT's roster and BOT's Scan gate from the same
        fallback object, looking entirely normal while every later /team-view
        no-op'd on top of the garbage.
        """
        import main

        assert main._viewed_team == "SRL"
        main._view_team("FAKE")
        assert main._viewed_team == "SRL", "a dead code must change nothing"
        main._view_team("SRL→MAC")  # /trade-between's team_code shape
        assert main._viewed_team == "SRL"

    def test_a_rejected_assign_does_not_move_the_view(self, client, viewing_srl):
        """A pick that never happened is not a draft action."""
        r = client.post("/assign", data={
            "player": "Nobody At All", "team": "BOT", "salary": "1.0",
        })
        assert "not found" in toast_of(r)["message"].lower()
        assert self._panel_team(r.text) == "SRL"

    def test_undo_returns_the_view_to_my_team(self, client, viewing_srl):
        import main

        player = next(iter(main.auction_state.available_players))
        client.post("/assign", data={
            "player": player, "team": "BOT", "salary": "1.0",
        })
        client.get("/team-view/SRL")
        r = client.post("/undo")
        assert self._panel_team(r.text) == "BOT"

    def test_reset_returns_the_view_to_my_team(self, client, viewing_srl):
        r = client.post("/reset")
        assert self._panel_team(r.text) == "BOT"

    def test_the_view_never_reaches_the_state_file(self, client, viewing_srl):
        """It is UI state, so it must not serialize or ride the undo chain.

        On AuctionState it would be saved to disk and /undo would restore a
        *view*, which is not a draft action — and a state file written while
        looking at SRL would reopen showing SRL after a crash.
        """
        import main

        blob = main.auction_state.to_json()
        assert "viewed" not in blob.lower()
        assert not hasattr(main.auction_state, "viewed_team")


class TestBuyoutScanIsOfferedOnlyWhereItWorks:
    """The scan's OOB swaps land in `bo-` dots that exist for BOT only.

    On an opponent's panel every swap missed and htmx logged
    htmx:oobErrorNoTarget — a button whose only effect was console noise.
    """

    def test_the_scan_button_is_absent_on_an_opponent(self, client):
        client.get("/team-view/SRL")
        try:
            panel = section_of(client.get("/").text, "buyout-panel")
            assert "/buyout-indicators" not in panel
        finally:
            client.get("/team-view/BOT")

    def test_the_scan_button_is_present_on_my_own_team(self, client):
        panel = section_of(client.get("/").text, "buyout-panel")
        assert "/buyout-indicators" in panel

    def test_the_per_player_buttons_stay_on_an_opponent(self, client):
        """They act on `team` (always BOT) and render into #buyout-panel, so
        they work whoever is on screen — gating them too would remove a working
        control along with the broken one."""
        client.get("/team-view/SRL")
        try:
            panel = section_of(client.get("/").text, "buyout-panel")
            assert "/buyout-check/" in panel
        finally:
            client.get("/team-view/BOT")

    def _scan_fragment(self, html: str) -> str:
        m = re.search(r'<div id="buyout-scan".*?</div>', html, re.S)
        assert m, "no #buyout-scan fragment in the response"
        return m.group(0)

    def test_switching_teams_carries_the_button_out_of_band(self, client):
        """The gap the endpoint tests missed and a browser found.

        `/team-view` swaps `#team-panel` alone — it cannot return all_panels,
        which would replace `#bid-panel` and destroy the bidding session. So a
        button that lives in the Buyout Analyzer but depends on the team panel
        went missing when you switched BACK to your own team, and stayed missing
        until some unrelated full-page swap happened to restore it. Both
        directions, because a one-way fix is what the original gating was.
        """
        away = self._scan_fragment(client.get("/team-view/SRL").text)
        assert "hx-swap-oob" in away
        assert "/buyout-indicators" not in away

        home = self._scan_fragment(client.get("/team-view/BOT").text)
        assert "hx-swap-oob" in home
        assert "/buyout-indicators" in home, (
            "coming home left the Scan button missing — the swap only ever "
            "removed it"
        )

    def test_the_full_page_carries_no_out_of_band_swap(self, client):
        """`GET /` builds the document; there is nothing to swap into yet.

        htmx processes `hx-swap-oob` on anything it swaps in, and all_panels.html
        goes into `#app` on every mutation — so an unguarded attribute here would
        make the fragment swap itself over itself on every pick.
        """
        assert "hx-swap-oob" not in self._scan_fragment(client.get("/").text)

    def test_an_opponents_pick_takes_the_scan_button_with_it(self, client):
        """The one new claim the 2026-08-11 view change makes about /assign.

        An opponent's pick now swaps the team panel to them, and `/assign`
        answers with the whole of all_panels.html — so the panel and the Scan
        gate re-render from ONE context in ONE response and cannot disagree.
        That is what makes the OOB dance `/team-view` needs unnecessary here.
        Asserted on the assign response itself, not on a following GET /, because
        a response that was briefly inconsistent is exactly the bug.
        """
        import main

        player = next(iter(main.auction_state.available_players))
        r = assign(client, player, "SRL", 1.0)

        panel = section_of(r.text, "team-panel")
        assert "(SRL)" in panel, "the pick should have swapped the panel to SRL"
        assert 'id="bo-' not in panel, "an opponent's dots can never be filled"

        scan = self._scan_fragment(r.text)
        assert "/buyout-indicators" not in scan, (
            "the Scan button outlived the swap — its 11 OOB swaps would all miss"
        )
        assert "hx-swap-oob" not in scan, "a full #app swap has nothing to OOB into"


class TestARecalledKeeperIsNotColouredAsAPurchase:
    """The 2026-08-07 testing-pass symptom, at the level the operator sees it.

    `team_panel.html` renders a row `text-success` when the player is in
    `acquired_players` — green means "I bought him at auction". Before
    2026-08-08 `recall_from_minors` put EVERYBODY into that list, so a keeper
    who went Active -> Bench -> Minors -> Recall came back permanently green
    and the roster lied at a glance.

    Driven through the endpoints rather than `TeamState` directly, because the
    state-level tests can only show which list he is in; this shows the colour,
    which is the thing that was wrong. The panel is opened first, per the
    CLAUDE.md rule that `_viewed_team` is the only thing deciding what
    `team_panel.html` renders.
    """

    def _row_of(self, html: str, name: str) -> str:
        """The one `<tr>` for this player in the team panel.

        Matched as a whole element, not by splitting on `<tr` — team_panel.html
        also carries the `/trade-between` form, whose `<select>` lists the same
        players as `<option>`s, so a naive split put the roster row and the
        trade form in one chunk and the colour assertion read the wrong markup.
        """
        panel = section_of(html, "team-panel")
        rows = [
            m.group(0)
            for m in re.finditer(r"<tr\b.*?</tr>", panel, re.S)
            if name in m.group(0)
        ]
        assert rows, f"{name} is in no table row of the team panel"
        assert len(rows) == 1, f"{name} appears in {len(rows)} rows"
        return rows[0]

    def _a_benchable_keeper(self):
        """A keeper BOT can legally send down: on the active roster, worst first.

        Derived by role — a hard-coded name stops matching the moment
        players.csv is replaced, which CLAUDE.md forbids for exactly this.
        """
        import main
        keepers = main.auction_state.teams["BOT"].keeper_players
        assert keepers, "BOT has no keepers — the fixture is wrong"
        return min(keepers, key=lambda p: p.projected_points)

    def test_a_keeper_survives_the_round_trip_uncoloured(self, client):
        keeper = self._a_benchable_keeper()
        client.get("/team-view/BOT")

        before = self._row_of(client.get("/").text, keeper.name)
        assert "text-success" not in before, (
            "precondition: a keeper is not green before anything happens"
        )

        for endpoint, payload in (
            ("/toggle-bench", {"team_code": "BOT", "player_name": keeper.name}),
            ("/move-to-minors", {"team_code": "BOT", "player_name": keeper.name}),
            ("/move-to-roster", {"team_code": "BOT", "player_name": keeper.name}),
        ):
            r = client.post(endpoint, data=payload)
            assert r.status_code == 200, f"{endpoint} failed"
            assert toast_of(r).get("type") != "error", f"{endpoint}: {toast_of(r)}"

        after = self._row_of(client.get("/team-view/BOT").text, keeper.name)
        assert "text-success" not in after, (
            f"{keeper.name} is a keeper but renders green after a trip through "
            f"the minors — green means BOT bought him at auction"
        )

    def test_a_drafted_player_is_still_coloured_after_the_round_trip(self, client):
        """The other half — the fix must not stop green meaning anything.

        Without this, routing every recall into `keeper_players` would pass the
        test above while making the colour permanently dead.
        """
        import main
        top = max(main.auction_state.available_players.values(),
                  key=lambda p: p.projected_points)
        assign(client, top.name, "BOT", 1.0)
        client.get("/team-view/BOT")

        assert "text-success" in self._row_of(client.get("/").text, top.name), (
            "precondition: a player BOT just drafted renders green"
        )

        for endpoint in ("/toggle-bench", "/move-to-minors", "/move-to-roster"):
            r = client.post(endpoint, data={"team_code": "BOT", "player_name": top.name})
            assert r.status_code == 200 and toast_of(r).get("type") != "error"

        after = self._row_of(client.get("/team-view/BOT").text, top.name)
        assert "text-success" in after, (
            f"{top.name} was bought at auction and must stay green through a "
            f"trip to the minors"
        )


class TestTheTradeFormCanSeeTheMinors:
    """A minor-league player is tradeable, and both dropdowns hid him.

    The engine never restricted this — `remove_player` walks all three lists and
    `evaluate_trade` resolves the incoming player with `find_player`, which
    searches the minors — so `/trade-between` has always accepted one. The
    restriction lived entirely in the two lists that feed the form, which means
    a legal trade could not be *proposed*. For a group 2/3 player his salary is
    fully on cap, so it is a trade with real cap consequences: the same fact
    that made the buyout dots wrong on 2026-08-07.
    """

    @staticmethod
    def _a_minor(code: str):
        """Someone in `code`'s minors, by role rather than by name."""
        import main
        minors = main.auction_state.teams[code].minor_players
        assert minors, f"{code} has no minor-league players — nothing to exercise"
        return minors[0]

    def test_team_players_returns_them_flagged(self, client):
        """The "I Receive" side is built in JS from this JSON."""
        import main
        code = next(c for c, t in main.auction_state.teams.items() if t.minor_players)
        expected = {p.name for p in main.auction_state.teams[code].minor_players}

        rows = client.get(f"/team-players/{code}").json()
        assert {r["name"] for r in rows if r["is_minor"]} == expected
        assert {r["name"] for r in rows} >= expected, "the minors are missing entirely"

    def test_the_give_list_offers_them_marked(self, client):
        """The "I Give" side, rendered by Jinja — same list, different half."""
        minor = self._a_minor("BOT")
        panel = section_of(client.get("/").text, "trade-panel")

        def option_for(name: str) -> str | None:
            m = re.search(
                rf'<option value="{re.escape(html.escape(name))}">(.*?)</option>',
                panel, re.S)
            return m.group(1) if m else None

        offered = option_for(minor.name)
        assert offered is not None, (
            f"{minor.name} is in BOT's minors and cannot be offered in a trade"
        )
        assert "(M)" in offered, f"nothing marks {minor.name} as a minor: {offered!r}"

        # The marker has to mean something, or it is noise on every row.
        import main
        active = main.auction_state.teams["BOT"].roster_players[0]
        control = option_for(active.name)
        # Found FIRST, then checked: `"(M)" not in None-or-empty` is true for a
        # row that is simply absent, so without this the control could stop
        # controlling anything and still read as a passing assertion.
        assert control is not None, f"{active.name} is missing from the Give list"
        assert "(M)" not in control, (
            f"{active.name} is on the active roster and must not be marked"
        )

    def test_the_trade_between_form_offers_them_too(self, client):
        """The app has TWO trade forms, and the first fix reached only one.

        `team_panel.html`'s "Trade Between Teams" is how a trade between two
        OTHER teams gets recorded during a break. Measured in Chrome before this
        was fixed: BOT could offer 12 of its 49 players there, while the
        "Receives" half of the same form — fed by the already-widened
        `/team-players` — listed 18 of SRL's minors unmarked.
        """
        import main
        minor = self._a_minor("BOT")
        bot = main.auction_state.teams["BOT"]
        panel = section_of(client.get("/").text, "team-panel")

        block = re.search(r"<select[^>]*trade-from-a-BOT[^>]*>(.*?)</select>",
                          panel, re.S)
        assert block, "the Trade Between Teams 'sends' list is not on the page"
        sends = block.group(1)

        assert sends.count("<option") == len(bot.all_players), (
            f"the sends list offers {sends.count('<option')} of "
            f"{len(bot.all_players)} players"
        )
        opt = re.search(
            rf'<option value="{re.escape(html.escape(minor.name))}">(.*?)</option>',
            sends, re.S)
        assert opt, f"{minor.name} cannot be offered in the Trade Between form"
        assert "(M)" in opt.group(1), f"nothing marks {minor.name}: {opt.group(1)!r}"

    def test_a_traded_keeper_is_acquired_by_the_team_that_gets_him(self, client):
        """`/trade-between` reuses the roster object; `execute_trade` rebuilds.

        That difference is invisible until provenance rides on the object. This
        path resets `is_minor` and `is_bench` on arrival and used to leave
        `is_keeper` alone, so another team's keeper arrived still flagged — and
        a later bench → minors → recall filed him under THEIR `keeper_players`,
        the 2026-08-08 colouring bug pointing the other way. It self-heals on
        reload, which is exactly why it would never reproduce after a restart.
        """
        import main
        giver = next(c for c, t in main.auction_state.teams.items()
                     if c != "BOT" and t.keeper_players)
        taker = next(c for c in main.auction_state.teams if c not in (giver, "BOT"))
        keeper = main.auction_state.teams[giver].keeper_players[0]
        assert keeper.is_keeper, "precondition: he is his own team's keeper"

        r = client.post("/trade-between", data={
            "team_a": giver, "team_b": taker,
            "players_from_a": keeper.name, "players_from_b": "",
        })
        assert r.status_code == 200, r.text
        assert toast_of(r).get("type") != "error", toast_of(r)

        arrived = main.auction_state.teams[taker].find_player(keeper.name)
        assert arrived is not None, f"{keeper.name} never reached {taker}"
        assert not arrived.is_keeper, (
            f"{taker} acquired {keeper.name} in a trade — he is not their keeper"
        )

        # The consequence, not just the flag: a round trip must keep him theirs.
        for endpoint in ("/toggle-bench", "/move-to-minors", "/move-to-roster"):
            rr = client.post(endpoint,
                             data={"team_code": taker, "player_name": keeper.name})
            assert rr.status_code == 200 and toast_of(rr).get("type") != "error", (
                f"{endpoint}: {toast_of(rr)}"
            )
        team = main.auction_state.teams[taker]
        assert keeper.name in {p.name for p in team.acquired_players}
        assert keeper.name not in {p.name for p in team.keeper_players}

    def test_a_trade_that_gives_a_minor_executes(self, client):
        """The end-to-end claim the finding made: propose it, then run it.

        Asserted on both rosters rather than on the response, because the
        failure this replaces was a form that rendered perfectly well while
        being unable to name the player.
        """
        import main
        minor = self._a_minor("BOT")
        source = next(c for c, t in main.auction_state.teams.items()
                      if c != "BOT" and t.roster_players)
        incoming = main.auction_state.teams[source].roster_players[0]

        r = client.post("/trade-evaluate", data={
            "give_player": [minor.name],
            "source_team": source,
            "receive_player": [json.dumps({
                "name": incoming.name,
                "position": incoming.position,
                "salary": incoming.salary,
                "projected_points": incoming.projected_points,
            })],
        })
        assert r.status_code == 200, r.text
        assert main.last_trade_eval is not None, (
            f"the form could not even propose giving {minor.name}"
        )

        r = client.post("/trade-execute",
                        data={"trade_id": main.last_trade_eval.trade_id})
        assert r.status_code == 200, r.text
        assert toast_of(r).get("type") != "error", toast_of(r)

        bot = {p.name for p in main.auction_state.teams["BOT"].all_players}
        theirs = {p.name for p in main.auction_state.teams[source].all_players}
        assert minor.name not in bot, f"{minor.name} never left BOT"
        assert minor.name in theirs, f"{minor.name} left BOT but arrived nowhere"
        assert incoming.name in bot

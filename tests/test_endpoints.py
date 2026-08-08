"""Tests for main.py: FastAPI endpoints."""

import re
from contextlib import contextmanager

import pytest

from config import MIN_SALARY, MINOR_CAP_GROUPS, SALARY_CAP
from tests.helpers import a_buyout_candidate, section_of, squeeze, toast_of


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

    def test_verdict_follows_the_engine_both_ways(self, client):
        """Both branches render, and each matches the sign of the engine's delta.

        Don't pin a player: points_difference is the roster delta AT THAT PRICE,
        so an elite player can be a "skip" (McDavid forced in at $9.5M costs
        lineup points elsewhere) while a mid-tier one is a "buy". Derive the
        expected branch instead of assuming it.
        """
        import main

        ranked = sorted(
            main.auction_state.available_players.values(),
            key=lambda p: -p.projected_points,
        )[:4]
        seen = set()
        for p in ranked:
            gain = self._delta(main, p.name)
            r = client.get(f"/explain/{p.name}")
            assert r.status_code == 200
            if gain > 0:
                assert "Worth having at $" in r.text, f"{p.name} gained {gain}"
                assert "lineup points over your best roster without him" in r.text
                seen.add("buy")
            else:
                assert "Skip him at $" in r.text, f"{p.name} lost {gain}"
                assert "costs you" in r.text
                seen.add("skip")
        assert seen == {"buy", "skip"}, f"sample should cover both branches, got {seen}"

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

    def _victim(self, code: str):
        import main
        return main.auction_state.teams[code].roster_players[0]

    def test_toggle_bench_stays_on_the_edited_team(self, client):
        client.get("/team-view/SRL")
        r = client.post("/toggle-bench", data={
            "team_code": "SRL", "player_name": self._victim("SRL").name,
        })
        assert r.status_code == 200
        assert self._panel_team(r.text) == "SRL"

    def test_adjust_salary_stays_on_the_edited_team(self, client):
        client.get("/team-view/SRL")
        p = self._victim("SRL")
        r = client.post("/adjust-salary", data={
            "team_code": "SRL", "player_name": p.name, "new_salary": str(p.salary),
        })
        assert r.status_code == 200
        assert self._panel_team(r.text) == "SRL"

    def test_move_to_minors_stays_on_the_edited_team(self, client):
        client.get("/team-view/SRL")
        p = self._victim("SRL")
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
        p = self._victim("SRL")
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
            "players_from_a": self._victim("SRL").name,
            "players_from_b": "",
        })
        assert r.status_code == 200
        assert toast_of(r).get("type") in ("success", "warning"), toast_of(r)
        assert self._panel_team(r.text) == "SRL"

    def test_assign_returns_to_my_team(self, client):
        """Owner decision 2026-08-07: a draft action resets the view.

        Not a shortcoming of the fix — reading SRL's Cap Used as your own right
        after a pick lands is the failure mode a sticky view invites.
        """
        import main
        client.post("/toggle-bench", data={
            "team_code": "SRL", "player_name": self._victim("SRL").name,
        })
        name = max(main.auction_state.available_players.values(),
                   key=lambda p: p.projected_points).name
        r = client.post("/assign", data={
            "player": name, "team": "MAC", "salary": "1.0",
        })
        assert r.status_code == 200
        assert self._panel_team(r.text) == "BOT"

    def test_undo_returns_to_my_team(self, client):
        client.post("/toggle-bench", data={
            "team_code": "SRL", "player_name": self._victim("SRL").name,
        })
        r = client.post("/undo")
        assert r.status_code == 200
        assert self._panel_team(r.text) == "BOT"

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
        assert minor.name in [p["name"] for p in bot["acquired_players"]]
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
        """Owner decision 2026-08-07, now pinned rather than assumed.

        Reading an opponent's Cap Used as your own right after a pick lands is
        worse than having to re-open their roster.
        """
        import main

        player = next(iter(main.auction_state.available_players))
        r = client.post("/assign", data={
            "player": player, "team": "BOT", "salary": "1.0",
        })
        assert self._panel_team(r.text) == "BOT"
        client.post("/undo")

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

"""Tests for main.py: FastAPI endpoints."""

import re
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from config import MIN_SALARY, SALARY_CAP


@pytest.fixture(scope="module")
def client():
    from main import app
    with TestClient(app) as c:
        # Reset to fresh state in case other test modules modified globals
        c.post("/reset")
        yield c


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
        """The engine's roster delta for this player at his market price."""
        from optimizer import generate_counterfactual

        pool = main.auction_state.available_players
        cf = generate_counterfactual(
            pool[name], main.market_prices.get(name, 0.5),
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

    def test_nothing_capped_at_full_budgets(self, client):
        import main

        assert not any(
            self._capped(main, n) for n in main.auction_state.available_players
        ), "no row should be ceiling-capped while every team still has budget"

    def test_capped_flips_once_the_ceiling_bites(self, client):
        """Drain every opponent's cap; the ceiling then cuts the top prices."""
        import main

        model = {n: p.expected_price for n, p in main.model_prices.items()}
        priciest = max(model, key=model.get)
        assert model[priciest] > 1.0, "need a player priced above the floor"

        saved = {c: t.penalties for c, t in main.auction_state.teams.items()}
        try:
            for code, t in main.auction_state.teams.items():
                if code != main.MY_TEAM:
                    t.penalties = 54.0  # leaves each opponent ~$2.8M of cap
                    t._invalidate_cache()
            main._recompute()
            assert main.market_info.market_ceiling < model[priciest]
            assert self._capped(main, priciest)
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
    """

    def _give_options(self, html: str) -> str:
        """The <select name="give_player"> block from the Trade panel."""
        start = html.index('name="give_player"')
        return html[start:html.index("</select>", start)]

    def test_toggle_bench_on_opponent_keeps_trade_panel_on_bot(self, client):
        import main

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
    def test_player_chart_valid(self, client):
        """Player chart should return SVG visualization."""
        r = client.get("/player-chart/Steven Stamkos")
        assert r.status_code == 200
        assert "Price Model" in r.text
        assert "<svg" in r.text
        assert "<path" in r.text

    def test_player_chart_invalid(self, client):
        """Invalid player should return fallback without crashing."""
        r = client.get("/player-chart/Nobody")
        assert r.status_code == 200


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
        r = client.get("/buyout-check/Clayton Keller")
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

"""Tests for main.py: FastAPI endpoints."""

import html
import json
import math
import re
from contextlib import contextmanager
from pathlib import Path

import pytest

from config import (
    MIN_SALARY,
    MINOR_CAP_GROUPS,
    MY_TEAM,
    NHL_TEAM_ALIASES,
    SALARY_CAP,
)
from tests.helpers import (
    a_buyout_candidate,
    a_roster_player,
    assign,
    buyout_options,
    pool_top,
    set_headroom,
    trade_choices,
    section_of,
    squeeze,
    toast_of,
)


REPO = Path(__file__).resolve().parent.parent


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
        player = pool_top(1)[0]
        r = client.post("/assign", data={
            "player": player,
            "team": "BOT",
            "salary": "5.0",
        })
        assert r.status_code == 200
        assert player in r.text

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
            "player": pool_top(1)[0],
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
            "player": pool_top(1)[0],
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
            "player": pool_top(1)[0],
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
                "player": pool_top(1)[0],
                "bidders": "BOT,HSM",
                "price": "2.5",
                "highest_bidder": "BOT",
            })
            assert r.status_code == 200
            assert "bid-win" in r.text, "should be a WIN — HSM cannot raise the price"
            assert 'name="team" value="BOT"' in r.text, "Assign form must be present"

    def test_the_whole_grid_toggled_on_and_bot_still_wins(self, client):
        """`endgame-sole-bidder`, the state where nobody left can raise a bid.

        The complement of the test above, which breaks ONE team with
        `cannot_raise`: here every opponent is genuinely spent out (full 24, under
        $0.5M of cap, none marked done) and the operator toggles the entire grid
        on, which is what actually happens mid-auction. Both halves matter — the
        buttons must still be there to click, since a done team would vanish from
        the grid and cover none of this, and `live_opponents` has to filter the
        whole list rather than stop at the first live-looking code.
        """
        import main

        client.post("/load-scenario", data={"name": "endgame-sole-bidder"})
        codes = list(main.auction_state.teams)
        r = client.post("/bid-check", data={
            "player": pool_top(1)[0],
            "bidders": ",".join(codes),
            "price": str(MIN_SALARY),
            "highest_bidder": main.MY_TEAM,
        })
        # `data-team` exists only on the bidder grid's buttons, and the grid ships
        # with the advice — a fresh GET / has no bidding session to render one.
        offered = re.findall(r'data-team="([^"]+)"', r.text)
        # A set: the grid iterates nomination_order, which is snake-draft order.
        assert set(offered) == set(codes) and len(offered) == len(codes), (
            f"grid offers {offered} of {codes} — a spent-out team is not done, "
            f"so every one of them must stay clickable"
        )
        assert "bid-win" in r.text, (
            f"{len(codes)} bidders toggled on and none can raise the price, so "
            f"this is a WIN"
        )
        assert f'name="team" value="{main.MY_TEAM}"' in r.text, "Assign form present"
        assert "(no rivals left)" in r.text, (
            "the forecast should say why there is no figure, not show one"
        )

    def test_uncontested_overpay_still_drops(self, client):
        """No opponents left, but above value — DROP and name the overpay."""
        r = client.post("/bid-check", data={
            "player": pool_top(1)[0],
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
                "player": pool_top(1)[0],
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
        """The same flag _context puts on each bid_limits row.

        RECOMPUTED here rather than read off the response on purpose — that is
        what makes the assertions below an equivalence rather than a tautology.
        The cost is that nothing in this class sees the rendered flag, which is
        why `test_capped_flips_once_the_ceiling_bites` also reads the markup:
        `market.is_capped` returning False breaks the Price column's marker and
        every recomputation in here would still agree with itself.
        """
        return round(main.market_prices[name], 1) < round(
            main.model_prices[name].expected_price, 1
        )

    def _row(self, page: str, name: str) -> str:
        """The bid_limits table row for `name`, as rendered.

        Matched against the UNESCAPED row rather than by escaping the name:
        Jinja writes an apostrophe as `&#39;` and `html.escape` produces
        `&#x27;`, so escaping here silently matches nothing for two names in
        today's pool (`Ryan O'Reilly`, `K'Andre Miller`) — and which name this
        test picks depends on the data.
        """
        rows = [r for r in page.split("<tr") if f">{name}</a>" in html.unescape(r)]
        assert len(rows) == 1, (
            f"expected exactly one Available Players row for {name}, got {len(rows)}"
        )
        return rows[0]

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

            # And the column actually SAYS so. Everything above recomputes the
            # rule from the two price dicts, so it holds identically against a
            # `capped` flag that is wrong for every row — measured 2026-08-18,
            # `market.is_capped` returning False leaves this class green.
            page = client.get("/").text
            marked = self._row(page, priciest)
            assert "line-through" in marked, (
                f"{priciest} is capped (${model[priciest]:.1f}M model vs "
                f"${main.market_prices[priciest]:.1f}M market) but his row is unmarked"
            )
            assert f"${round(main.market_prices[priciest], 1)}M" in marked

            plain = next(n for n in main.auction_state.available_players
                         if not self._capped(main, n))
            assert "line-through" not in self._row(page, plain), (
                f"{plain} is priced at his model price and must not be marked"
            )
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
        """The scan is BOT-only by construction — _solve_buyout_indicators
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

    def test_only_the_rfa_card_shows_the_prior_team(self, client):
        """`show_prior` is the one flag `pick_card` added, and it defaults off.

        Not a styling toggle: only an RFA has a prior team holding rights over
        him, so the line is absent from the UFA card because there is nothing to
        print. Before the 2026-08-20 macro the two cards were separate markup and
        that was structural; afterwards it is one keyword argument with a
        default — the shape that let the duplicated `(M)` suffix in the trade
        lists survive two reviews, because deleting either copy left the suite
        green.

        Added because the mutation check for that refactor found this uncovered:
        deleting the whole `show_prior` block passed all 892 tests. The heading
        parameter was already covered (by `TestMidBidClutterCanBeDismissed`,
        which locates a card by its heading text); this half was not.
        """
        page = client.get("/nominate").text
        cards = re.findall(r'nomination-pick"(.*?)</form>', page, re.S)
        assert len(cards) == 2, f"expected an RFA and a UFA card, got {len(cards)}"
        rfa, ufa = cards
        assert "RFA Pick" in rfa and "UFA Pick" in ufa, "the cards came back in another order"
        assert "Prior:" in rfa, "the RFA card lost its prior-team line"
        assert "Prior:" not in ufa, (
            "the UFA card grew a prior-team line, which it can never fill"
        )


    def test_the_ufa_card_refuses_a_prior_team_even_when_the_data_carries_one(self, client):
        """The flag, not the data guard, is what keeps the line off the UFA card.

        `players.csv` populates PRIOR FCHL TEAM for RFA1/RFA2 rows and nothing
        else (22 of 2158 today), so `{% if show_prior and ... %}`'s second half
        masks the first: measured 2026-08-20, flipping the default to `True` and
        passing `show_prior=True` on the UFA call BOTH survived the whole suite.
        That is an equivalent mutant on this CSV, not coverage — and the CSV is
        replaced before every draft.

        So stamp a prior team onto the pool's UFAs and check the card still
        refuses it. A UFA has no prior team holding rights over him under the
        CBA; if a future export starts filling that column for group 3, the
        answer is still no.
        """
        import main

        a_real_code = next(iter(main.auction_state.teams))
        for player in main.auction_state.available_players.values():
            if not player.is_rfa:
                player.prior_fchl_team = a_real_code

        cards = re.findall(r'nomination-pick"(.*?)</form>',
                           client.get("/nominate").text, re.S)
        assert len(cards) == 2, f"expected an RFA and a UFA card, got {len(cards)}"
        rfa, ufa = cards
        assert "UFA Pick" in ufa, "the cards came back in another order"
        assert "Prior:" not in ufa, (
            "the UFA card printed a prior team because the DATA had one — "
            "show_prior is what is supposed to decide that"
        )
        assert "Prior:" in rfa, "and the RFA card still shows its own"


class TestNominationPanelPrices:
    """Both prices on screen, and the marker only where the ceiling actually cuts.

    "Expected" is the market price — the clearing price a nomination fetches —
    and since 2026-08-18 the model price renders beside it, labelled, so the
    operator can tell a player cheap because the market is thin from one cheap
    because the model rates him low (the 2026-08-07 owner want).

    `GET /nominate` returns the nomination panel alone, so the whole body is the
    fragment; `section_of` is no use here because the panel is a `<div>`.
    """

    CARD = re.compile(r'nomination-pick"(.*?)</form>', re.S)

    def _cards(self, page: str) -> list[dict]:
        """What each recommendation on screen says, as the operator reads it.

        The figures come back as the rendered STRINGS, compared below against
        `f"{price:.1f}"` — the template's own formatting. Comparing floats would
        re-implement the rounding rather than check it.
        """
        cards = []
        for body in self.CARD.findall(page):
            flat = re.sub(r"\s+", " ", body)
            player = re.search(r'name="player" value="([^"]+)"', flat)
            market = re.search(r"Expected: (?:<span[^>]*>)?~\$([\d.]+)M", flat)
            model = re.search(r'Model <span( class="line-through")?>\$([\d.]+)M', flat)
            assert player and market and model, (
                f"a nomination card did not render both figures: {flat[:300]}"
            )
            # Anchored to the price line's own opening tag: the card carries an
            # earlier `title` on the NHL logo, and a bare search took that one
            # (it read "EDM").
            title = re.search(r'title="([^"]*)"[^>]*>\s*Expected:', flat)
            cards.append({
                "player": html.unescape(player.group(1)),
                "market": market.group(1),
                "model": model.group(2),
                "struck": model.group(1) is not None,
                "arrow": "&#9660;" in flat,
                # The hover sentence quotes both figures, so it is the one place
                # they can silently be the SAME figure twice.
                "title": html.unescape(title.group(1)) if title else "",
            })
        return cards

    def _assert_figures_match(self, main, cards: list[dict]) -> int:
        """Each figure is that player's own price, and the marker follows them.

        Returns how many cards are marked, so the caller can insist the state it
        chose actually produces one — an equivalence where both sides are always
        false cannot fail, which is what `TestPriceColumn` records for the
        bid_limits flag at full budgets.
        """
        assert len(cards) == 2, f"expected an RFA and a UFA card, got {len(cards)}"
        for card in cards:
            name = card["player"]
            market = main.market_prices[name]
            model = main.model_prices[name].expected_price
            assert card["market"] == f"{market:.1f}", (
                f"{name}: panel says Expected ${card['market']}M, market price is "
                f"${market:.1f}M"
            )
            assert card["model"] == f"{model:.1f}", (
                f"{name}: panel says Model ${card['model']}M, model price is "
                f"${model:.1f}M"
            )
            capped = round(model, 1) > round(market, 1)
            assert card["struck"] is capped and card["arrow"] is capped, (
                f"{name}: model ${model:.1f}M vs market ${market:.1f}M is "
                f"{'capped' if capped else 'not capped'}, but the card renders "
                f"struck={card['struck']} arrow={card['arrow']}"
            )
        return sum(1 for c in cards if c["struck"])

    def test_both_figures_render_in_both_cards(self, client):
        """Neither half may show one price. Both cards call the same macro, so a
        deleted call is the failure this catches — the RFA and UFA blocks were
        two hand-maintained copies of this line until the macro landed."""
        import main

        self._assert_figures_match(main, self._cards(client.get("/nominate").text))

    def test_the_marker_tracks_the_ceiling(self, client):
        """`endgame-ceiling-binds` is where the equivalence above gets its teeth.

        At full budgets the ceiling is $11.4M and the priciest model price is
        ~$9.5M, so nothing is capped and the marker half of that assertion is
        vacuous. Measured in this scenario 2026-08-18: a $2.8M ceiling, 28 of 677
        pool prices cut, and the RFA pick is McDavid at $2.8M against a $9.5M
        model price — which also makes this the only state here that reaches the
        RFA *target* branch.
        """
        import main

        client.post("/load-scenario", data={"name": "endgame-ceiling-binds"})
        page = client.get("/nominate").text
        marked = self._assert_figures_match(main, self._cards(page))

        assert marked, (
            "no recommendation in endgame-ceiling-binds is priced under its model "
            "price any more, so this test no longer exercises the marker — the "
            "state has lost its teeth, pick one where the ceiling cuts a pick"
        )
        marked = [c for c in self._cards(page) if c["struck"]]
        for card in marked:
            model = main.model_prices[card["player"]].expected_price
            market = main.market_prices[card["player"]]
            assert f"Model says ${model:.1f}M" in card["title"], (
                f"{card['player']}: the hover sentence must quote the MODEL "
                f"figure (${model:.1f}M); it says {card['title']!r}"
            )
            assert f"caps it at ${market:.1f}M" in card["title"], (
                f"{card['player']}: the hover sentence must quote the MARKET "
                f"figure (${market:.1f}M); it says {card['title']!r}"
            )


class TestExplain:
    def test_explain_player(self, client):
        """Explain should return a populated counterfactual.

        Asserted on `.counterfactual-card`, not on the word "Counterfactual":
        that word is the PANEL HEADING and it is in the 267-byte empty state
        too, so with the hard-coded name this test carried until 2026-08-14
        (`Sidney Crosby`) a data refresh would have left it passing against a
        page holding no counterfactual at all. Name derived for the same reason.
        """
        player = pool_top()[0]
        r = client.get(f"/explain/{player}")
        assert r.status_code == 200
        assert "counterfactual-card" in r.text, (
            "no counterfactual body — the panel rendered its empty state"
        )
        assert player in html.unescape(r.text)

    def test_explain_invalid(self, client):
        """The empty state, asserted rather than merely reached.

        This checked `status_code == 200` alone until 2026-08-14, which is true
        of every response the endpoint can produce.
        """
        r = client.get("/explain/Nobody")
        assert r.status_code == 200
        assert "counterfactual-card" not in r.text
        assert len(r.text) < 400, f"expected an empty state, got {len(r.text)} bytes"

    def test_both_mounts_carry_a_close_button(self, client):
        """The panel and the inline copy under the bid panel both get one.

        The inline mount is the one the operator cannot otherwise dismiss: it
        auto-loads on every whole-panel swap and sits directly under the live
        bid advice.
        """
        player = pool_top()[0]
        for url in (f"/explain/{player}", f"/explain/{player}?inline=1"):
            body = client.get(url).text
            assert "counterfactual-card" in body, url
            assert "this.closest('.counterfactual-card')" in body, url
            assert "getElementById" not in body, (
                f"{url} closes by id — mounted twice, so that removes the FIRST "
                f"counterfactual in the document, not the one clicked"
            )
            # A glyph is not a name: with no aria-label the button's accessible
            # name computes to "×" (U+00D7), announced as "times". The League
            # State done toggle carries one for exactly this reason.
            assert "aria-label=" in body, f"{url}'s close button has no name"


    def test_the_inline_mount_carries_no_panel_id(self, client):
        """The invariant the close button's `closest()` depends on.

        Same rule as the chart: the body is mounted in two places, so it owns no
        id. If the inline copy carried `id="explanation"` the document would hold
        two, and htmx would resolve `hx-target` to whichever came first.
        """
        body = client.get(f"/explain/{pool_top()[0]}?inline=1").text
        assert "counterfactual-card" in body, "nothing rendered, so nothing proven"
        assert 'id="explanation"' not in body


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


class TestTheTeamPanelsPointsFigures:
    """Three "points" numbers read as one, and one of them was wrong.

    League State's **Pts** is `current_roster_points` (best 12F/6D/2G right
    now); its **Proj** is the optimal roster once filled; the team panel's tile
    said **Proj PTS** and was neither — it was a raw sum over every roster
    player, bench included. Reported as "in League State it says Proj. Est /
    Solved. But then in the Team Panel it also shows Proj PTS. These values are
    different."
    """

    def _tile(self, html: str) -> int:
        """The Lineup PTS figure out of the team panel's stat strip."""
        panel = section_of(html, "team-panel")
        m = re.search(
            r'Lineup PTS</div>\s*<div[^>]*>(\d+)</div>', panel
        )
        assert m, "no Lineup PTS tile in the team panel"
        return int(m.group(1))

    def test_the_tile_is_the_lineup_not_the_sum(self, client):
        """`full-roster-still-bidding` because the two agree everywhere else:
        until a roster exceeds 12F/6D/2G at some position every player starts,
        which is why this shipped unnoticed and bit only at the end of a draft.
        """
        import main

        client.post("/load-scenario", data={"name": "full-roster-still-bidding"})

        divergent = [
            code for code, t in main.auction_state.teams.items()
            if sum(p.projected_points for p in t.roster_players)
            > t.current_roster_points
        ]
        assert divergent, (
            "this scenario no longer has a team whose bench is carrying phantom "
            "points — the test cannot fail and needs a new fixture"
        )

        for code in divergent:
            t = main.auction_state.teams[code]
            shown = self._tile(client.get(f"/team-view/{code}").text)
            assert shown == t.current_roster_points
            assert shown < sum(p.projected_points for p in t.roster_players), (
                f"{code}: the tile is still the bench-inclusive sum"
            )

    def test_the_tile_matches_league_states_pts_column(self, client):
        """Same quantity, so the same number — that is the whole point of
        renaming it away from "Proj"."""
        import main

        client.post("/load-scenario", data={"name": "full-roster-still-bidding"})
        code = max(
            main.auction_state.teams,
            key=lambda c: main.auction_state.teams[c].roster_count,
        )

        html = client.get(f"/team-view/{code}").text
        expected = main.auction_state.teams[code].current_roster_points

        assert self._tile(html) == expected

    def test_the_milp_headline_matches_bots_proj_cell(self, client):
        """Two panels, one number — compared as RENDERED, not against the
        source both would read.

        Asserting each against `_context` would pass on a build where one panel
        had been changed to show something else entirely, because the
        comparison would go through the value rather than the screen. The
        owner's complaint was about two figures disagreeing on screen.
        """
        import main

        page = client.get("/").text
        assert main.milp_solution and main.milp_solution.status == "Optimal"

        panel = section_of(page, "team-panel")
        m = re.search(r"Optimal Projected Points: (\d+)", panel)
        assert m, "no MILP headline on BOT's panel"

        cell = re.search(
            rf'<span id="proj-{MY_TEAM}"[^>]*>(\d+)', section_of(page, "league-state")
        )
        assert cell, "no Proj figure for BOT in League State"

        assert m.group(1) == cell.group(1), (
            f"team panel says {m.group(1)}, League State says {cell.group(1)} "
            f"— the same quantity rendered two ways"
        )

    def test_the_panel_names_the_column_it_agrees_with(self, client):
        """Without this the two figures are still unlabelled strangers, which
        is what the report was actually about."""
        panel = section_of(client.get("/").text, "team-panel")

        assert "Optimal Projected Points" in panel
        assert "<em>Proj</em> in League State" in panel


class TestPlayerChart:
    """The chart is mounted in two places, so the body must own no id.

    It used to carry `id="player-chart-container"` itself while
    `bid_limits.html` rendered an empty div with the same id as the table's
    swap target. htmx resolves a target by id and takes the first match, and
    `area-auction` precedes `area-players`, so during a live bid a chart link
    in the table rendered the chart into the bid panel in the other column.
    """

    def test_player_chart_valid(self, client):
        """Player chart should return SVG visualization.

        Also the guard on its sibling below: both derive the name from the pool,
        so a name that stopped matching fails HERE, loudly, instead of turning
        the mount-id assertion into one that cannot fail.
        """
        r = client.get(f"/player-chart/{pool_top()[0]}")
        assert r.status_code == 200
        assert "Price Model" in r.text
        assert "<svg" in r.text
        assert "<path" in r.text

    def test_the_close_button_has_an_accessible_name(self, client):
        """`&times;` alone computes to "times", which names nothing.

        Same gap the counterfactual shipped with on 2026-08-14 and the League
        State done toggle had already fixed a day earlier — caught by grilling
        the copy, so both were labelled together.
        """
        r = client.get(f"/player-chart/{pool_top()[0]}")
        assert "price-chart-card').remove()" in r.text, "no close button rendered"
        assert 'aria-label="Close the price chart"' in r.text

    def test_the_chart_body_carries_no_mount_id(self, client):
        """The property that makes two mounts legal.

        `/player-chart/<gone>` answers 200 with a ~250-byte empty state that
        contains no mount id either, so this assertion passes on a page with no
        chart in it at all — which is exactly what a hard-coded name became once
        `players.csv` was replaced. Derived from the pool for that reason.
        """
        r = client.get(f"/player-chart/{pool_top()[0]}")
        assert 'id="player-chart-container"' not in r.text, (
            "the chart body owns the mount id again — an innerHTML swap nests "
            "it inside the mount and duplicates the id"
        )

    def test_the_mount_appears_exactly_once_on_the_page(self, client):
        page = client.get("/").text
        assert page.count('id="player-chart-container"') == 1

    def test_the_card_offers_a_way_into_the_bidding_form(self, client):
        """No JS of its own — `.btn-add-bid` is delegated on `document` and
        reads `data-player`, so the class and the attribute ARE the contract."""
        name = pool_top()[0]

        r = client.get(f"/player-chart/{name}")

        assert r.text.count("btn-add-bid") == 1
        assert f'data-player="{name}"' in r.text
        assert 'aria-label="Start an auction for this player"' in r.text

    def test_the_inline_mount_offers_none(self, client):
        """During a live auction the button is pointless — you are bidding on
        him already, `.bid-form` (Start Auction) does not exist while an auction
        is live, and the handler would answer a click with "finish the current
        auction first"."""
        name = pool_top()[0]

        r = client.post("/bid-check", data={
            "player": name, "bidders": "SRL", "price": 1.0, "highest_bidder": "",
        })

        assert "Price Model" in r.text, "the inline chart did not render"
        assert "btn-add-bid" not in r.text

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


class TestTheChartExplainsThePrice:
    """The price-driver breakdown: why the model landed where it did.

    Closes the "decompose Model $ into its drivers" backlog entry. The
    breakdown explains the stage-2 MEDIAN; E[$] mixes in a separate logistic
    and is stated rather than attributed, which the last test here pins.
    """

    GROUPS = ("Points", "NHL team", "Reputation", "Scarcity", "Contract")

    def _block(self, html_text: str) -> str:
        # `[^>]*` on the opening tag deliberately: anchored to the exact
        # string, adding an attribute made every test in this class fail with
        # "no breakdown" instead of the one that was actually about it.
        found = re.search(
            r'<details class="price-drivers"[^>]*>.*?</details>', html_text, re.S
        )
        assert found, "no price-driver breakdown in the chart card"
        return found.group(0)

    def _a_priced_forward(self) -> str:
        """The pool's top forward — well clear of the floor, so every driver
        has something to say."""
        return pool_top(1, position="F")[0]

    @staticmethod
    def _live_drivers(b) -> list:
        """The drivers the card renders, in engine order.

        BOTH columns, and rounded. Reading `log_delta != 0.0` — which the two
        callers below did until 2026-09-11 — is wrong in two directions at
        once now: it keeps a row whose price factor prints x1.00, and it would
        drop a row that does nothing to the price while multiplying the floor
        odds by 20.

        ONE helper, because the hazard here is a test zipping group names onto
        rendered widths: a predicate that disagrees with the template by a
        single row silently labels every width with the wrong group, which is
        the failure `test_the_bars_are_scaled_in_log_space...` already carried
        a comment about before its own inline copy went stale.
        """
        import main

        return [
            d for d in b.drivers
            if not (main._is_unit(d.factor)
                    and main._is_unit(main._odds_ratio(d.floor_logit_delta)))
        ]

    def _decompose(self, name: str):
        import main
        from price_model import decompose_player

        player = main.auction_state.available_players[name]
        return decompose_player(
            player, main.model_params,
            main.auction_state.price_reference[player.position],
        )

    def _live_groups(self, name: str) -> set[str]:
        return {d.group for d in self._live_drivers(self._decompose(name))}

    def test_the_test_helper_agrees_with_the_card_on_every_pool_player(self, client):
        """`_live_drivers` is a second statement of the hiding rule, so pin it.

        Every assertion in this class that zips engine facts onto rendered rows
        trusts that helper to pick the same set the card does — and a predicate
        that disagrees by ONE row mislabels every width rather than failing.
        That is not hypothetical: the inline copy in
        `test_the_bars_are_scaled_in_log_space...` went stale on 2026-09-11 when
        the rule changed, and mutation testing showed reverting the helper to
        the old predicate broke nothing, because only one player in the pool
        separates the two and no test happened to select him.

        Swept over the whole pool for that reason: the separating case is one
        row in 3525 and picking a subject cannot be trusted to find it.
        """
        import main

        mismatches = []
        for name, player in main.auction_state.available_players.items():
            ref = main.auction_state.price_reference.get(player.position)
            if not ref:
                continue
            b = self._decompose(name)
            mine = [d.group for d in self._live_drivers(b)]
            card = [r["group"] for r in main._driver_rows(b)["rows"]]
            if mine != card:
                mismatches.append((name, mine, card))
        assert not mismatches, (
            f"the helper disagrees with the card on {len(mismatches)} player(s), "
            f"so every zipped assertion in this class is suspect: "
            f"{mismatches[:3]}"
        )

    def test_it_names_exactly_the_drivers_that_are_in_play(self, client):
        """Set equality, both directions, against the engine.

        This asserted "all five groups appear" until 2026-09-10, which stopped
        being right when inert rows started being hidden: the reference is a
        UFA, so Contract is dead for every UFA in the pool, and the top forward
        is one. Naming a driver that does nothing is the reported confusion
        ("Does Scarcity not apply to Goalies?" — no, and the row said x1.00
        rather than saying nothing); dropping one that DOES something is worse.
        Only equality catches both.
        """
        import main

        # A MIXED player specifically. The top forward has all five drivers
        # live, so against him this equality cannot fail in the hiding
        # direction — measured 2026-09-10, removing the filter left it green.
        mixed = None
        for name in main.auction_state.available_players:
            player = main.auction_state.available_players[name]
            if player.position not in main.auction_state.price_reference:
                continue
            live = self._live_groups(name)
            if live and len(live) < len(self.GROUPS):
                mixed = (name, live)
                break
        assert mixed, (
            "no pool player has both a live and an inert driver, so this "
            "equality is vacuous and must be re-derived"
        )
        name, live = mixed

        block = self._block(client.get(f"/player-chart/{name}").text)
        named = {g for g in self.GROUPS if g in block}
        assert named == live, (
            f"{name}: the card names {sorted(named)} but the engine moves "
            f"{sorted(live)} — hidden rows and inert rows have drifted apart"
        )

    def test_a_goalie_card_drops_scarcity_and_keeps_points(self, client):
        """Item 2 of the 2026-09-09 review, and the decision taken on it.

        `coef_log_rank` is exactly 0.0 for G — ~20 goalies a season is too
        coarse a field to fit rank against — so the row could only ever read
        x1.00 and is dropped. Points STAYS, and stays called "Points": a
        goalie's is driven by `proj_wins`, which the detail line says, and the
        owner's call was to keep one label across all three positions rather
        than have the row rename itself per card.
        """
        name = pool_top(1, position="G")[0]
        assert "Scarcity" not in self._live_groups(name), (
            "this goalie's Scarcity is live, so the fixture no longer "
            "demonstrates the structural case"
        )
        block = self._block(client.get(f"/player-chart/{name}").text)
        assert "Scarcity" not in block, (
            "a goalie card still carries a Scarcity row, which can only ever "
            "say x1.00"
        )
        assert "Points" in block, "the goalie card lost its Points row"
        assert "projected wins" in block, (
            "the Points row does not say what a goalie's points actually are"
        )

    def test_reputation_appears_only_where_there_is_a_reputation(self, client):
        """Item 1, in both directions — the half that keeps this from being
        "hide Reputation".

        `log_lag`/`has_lag` ARE in the model, but a player new to the league
        sits on exactly the reference's encoding, so the row says nothing. On
        a pool where NOBODY carries a prior FCHL salary — `players-25.csv`, the
        one this was reported against — that is every card, which is why it
        read as "Reputation isn't in the model". It has to come back for a
        player who has one.
        """
        import main

        with_lag, without = None, None
        for name, player in main.auction_state.available_players.items():
            if player.position not in main.auction_state.price_reference:
                continue
            live = "Reputation" in self._live_groups(name)
            if live and with_lag is None:
                with_lag = name
            elif not live and without is None:
                without = name
            if with_lag and without:
                break
        assert with_lag and without, (
            "the pool no longer has both a player with a prior FCHL salary and "
            "one without, so this cannot test both directions"
        )
        assert "Reputation" in self._block(
            client.get(f"/player-chart/{with_lag}").text)
        assert "Reputation" not in self._block(
            client.get(f"/player-chart/{without}").text)

    def test_a_card_with_no_live_driver_still_prints_base_and_median(self):
        """The all-hidden case: the table must not collapse into an exception.

        No pool player hits it (the worst measured is four of five hidden), so
        it is constructed — decomposing a player against his OWN features, the
        same move `test_a_player_against_his_own_features_has_no_drivers` uses.
        `widest` is 0 there and every bar divides by it.
        """
        import main
        from price_model import build_features, decompose_price

        own = build_features("F", 90, 6.0, True, main.model_params,
                             last_salary=5.0, pos_rank=4)
        b = decompose_price("F", 90, 6.0, True, main.model_params, own,
                            last_salary=5.0, pos_rank=4)
        rows = main._driver_rows(b)
        assert rows["rows"] == [], "a self-referenced player has no live driver"
        assert rows["headline"] == [], "the summary advertises a hidden driver"
        assert rows["base_price"] > 0 and rows["median_price"] > 0

    def test_it_renders_the_numbers_the_engine_computed(self, client):
        """The reconstruction test at the UI layer.

        The unit test proves base x factors == the median exactly. This proves
        the TEMPLATE renders that arithmetic and not some other field — a
        formatting or wrong-variable bug the unit test cannot see.

        Compared value-by-value against the engine rather than by multiplying
        the printed figures: the card prints 2dp, the F baseline is $0.2951M,
        and re-multiplying six rounded numbers compounds to ~2% — which would
        force a tolerance so loose it stopped catching anything.
        """
        import main
        from price_model import decompose_player

        name = self._a_priced_forward()
        player = main.auction_state.available_players[name]
        b = decompose_player(
            player, main.model_params,
            main.auction_state.price_reference[player.position],
        )
        block = self._block(client.get(f"/player-chart/{name}").text)

        # Anchored to the rows by label, not to "the first and last dollar
        # figure": the E[$] note below the table prints one too, and reading
        # that as the median is how the first version of this failed.
        # The LAST figure in the row, which is the effect cell. A non-greedy
        # `This player.*?\$([\d.]+)M` reads the first one instead, and the
        # clamp note sits between the label and the effect — so the moment a
        # star forward's unclamped median cleared $11.4M this read $15.85M
        # and called it the median.
        def _effect(label: str) -> float:
            row = re.search(r"<tr>(?:(?!</tr>).)*?" + label + r".*?</tr>", block, re.S)
            assert row, f"no {label} row in the drivers card"
            return float(re.findall(r"\$([\d.]+)M", row.group(0))[-1])

        base = _effect(r"Typical \w")
        median = _effect("This player")
        assert base == pytest.approx(b.base_price, abs=0.005)
        assert median == pytest.approx(b.prediction.median_price, abs=0.005)

        # The LIVE drivers, not all five. Comparing against b.drivers passed
        # only because this subject happens to have every driver in play; the
        # first player with an inert one would have failed it as a phantom
        # mismatch, and it quietly asserted the OPPOSITE of the hiding rule.
        live = self._live_drivers(b)
        printed = [float(m) for m in re.findall(r"&times;([\d.]{4,})", block)]
        assert printed == [
            pytest.approx(d.factor, abs=0.005) for d in live
        ], (
            f"the card printed factors {printed} against the engine's "
            f"{[round(d.factor, 2) for d in live]}"
        )

    def test_it_is_collapsed_by_default(self, client):
        """The bid-panel copy re-renders on every bidder toggle, which snaps an
        open <details> shut mid-auction. Collapsed, that is a no-op."""
        block = self._block(client.get(f"/player-chart/{self._a_priced_forward()}").text)
        opening = block.split(">", 1)[0]
        assert " open" not in opening, "the breakdown starts expanded"

    def test_the_bars_are_scaled_to_the_effects_they_show(self, client):
        """The bar is the only thing that makes the table scannable, and a
        constant width would misread as "every driver contributed equally".

        Scaled on |log_delta|, which is order-invariant — never on a dollar
        step. Checked as an ORDERING against the factors, plus the one pixel
        fact that carries meaning: the largest effect fills the scale.

        The pattern anchors on the price bar's EXACT class and takes the factor
        from the cell immediately after it. It used to read
        `driver-bar[^"]*" ... .*?&times;` across the row, which silently began
        pairing each FLOOR bar with the next row's price factor when the second
        column landed 2026-09-11 — five matches, every assertion green, every
        pairing wrong. A regex that spans cells is not reading a row.
        """
        block = self._block(client.get(f"/player-chart/{self._a_priced_forward()}").text)
        rows = re.findall(
            r'<span class="driver-bar (?:up|down)" style="width: (\d+)px">'
            r'</span></td>\s*<td class="driver-effect">&times;([\d.]{4,})',
            block, re.S,
        )
        assert len(rows) == len(self.GROUPS), f"found {len(rows)} bars"

        widths = [int(w) for w, _ in rows]
        effects = [abs(math.log(float(f))) for _, f in rows]
        assert len(set(widths)) > 1, "every bar is the same width"
        assert max(widths) == 60, "the largest effect should fill the scale"
        biggest = effects.index(max(effects))
        assert widths[biggest] == max(widths), (
            f"the widest bar is not the largest effect: widths {widths} "
            f"against effects {[round(e, 2) for e in effects]}"
        )

    def test_the_bars_are_scaled_in_log_space_not_on_factor_minus_one(self, client):
        """A halving and a doubling are the same size effect. |factor - 1| says
        they are not — 0.5 against 1.0 — which is the wrong visual for a
        multiplicative model, and it flips which bar is widest.

        The player is derived rather than named, and derived by the ROLE of
        separating the two scalings: on the top forward every factor is above
        1, where the two orderings agree, so the test above cannot tell them
        apart. That is the data making a mutant equivalent — 11 players in the
        current pool break the tie and this finds one.
        """
        import main
        from price_model import decompose_player

        def widest_by(b, key):
            return max(b.drivers, key=lambda d: abs(key(d))).group

        separating = None
        for name, player in main.auction_state.available_players.items():
            b = decompose_player(
                player, main.model_params,
                main.auction_state.price_reference[player.position],
            )
            if widest_by(b, lambda d: d.log_delta) != widest_by(
                b, lambda d: d.factor - 1.0
            ):
                separating = (name, b)
                break
        assert separating, (
            "no player in this pool separates log scaling from |factor - 1|, "
            "so this test cannot fail and must be re-derived"
        )
        name, b = separating

        block = self._block(client.get(f"/player-chart/{name}").text)
        rows = re.findall(
            r'driver-bar[^"]*" style="width: (\d+)px"[^>]*></span></td>\s*'
            r'<td class="driver-effect">&times;([\d.]+)',
            block, re.S,
        )
        # Against the LIVE drivers in engine order, not against GROUPS: inert
        # rows are hidden since 2026-09-10, so zipping the five names onto
        # three bars silently labelled every width with the wrong group. Via
        # the shared helper since 2026-09-11 — this was an inline copy of the
        # predicate and went stale the moment the rule changed, which is the
        # mislabelling this very comment warns about.
        live = [d.group for d in self._live_drivers(b)]
        assert len(rows) == len(live), (
            f"found {len(rows)} bars for {name} against {len(live)} live drivers"
        )
        widths = {g: int(w) for g, (w, _) in zip(live, rows)}
        assert widths[widest_by(b, lambda d: d.log_delta)] == max(widths.values()), (
            f"{name}: the widest bar is not the largest LOG effect — widths "
            f"{widths} look scaled on |factor - 1|"
        )

    def test_the_summary_answers_without_being_opened(self, client):
        """A collapsed card that says nothing is a card nobody opens."""
        block = self._block(client.get(f"/player-chart/{self._a_priced_forward()}").text)
        summary = re.search(r"<summary>(.*?)</summary>", block, re.S).group(1)
        assert "&times;" in summary, "the summary carries no driver at all"
        assert any(g in summary for g in self.GROUPS)
        assert "% floor" in summary, (
            "the closed card does not say whether he goes at the minimum"
        )

    def test_the_summary_never_names_a_driver_only_to_print_one(self, client):
        """The same rule as the table, at the summary's own precision.

        The table learned to hide a row that says nothing on 2026-09-11 and the
        summary did not, which made it the worse offender: measured, **135 of
        705** players had a closed summary naming a driver and printing `×1.0`
        — against 12 such rows inside — and for **82** of them that driver
        moved the floor odds by 1.5x or more, so the summary had something to
        say and said nothing instead.

        Dropping the entry is not lossy, which is the half worth asserting: the
        floor percentage beside it is the answer for exactly those players.
        """
        import main
        from price_model import decompose_player

        # A player whose STRONGEST price driver prints x1.0 at the summary's
        # one decimal — the shape that used to fill a headline slot with
        # nothing. Derived, never named.
        subject = None
        for name, player in main.auction_state.available_players.items():
            ref = main.auction_state.price_reference.get(player.position)
            if not ref:
                continue
            rows = main._driver_rows(decompose_player(player, main.model_params, ref))
            ranked = sorted(rows["rows"], key=lambda r: -abs(math.log(r["factor"])))
            if ranked and any(main._is_unit(r["factor"], places=1) for r in ranked[:2]):
                subject = name
                break
        assert subject, (
            "no pool player's top-two price drivers round to 1.0, so this test "
            "cannot fail and must be re-derived"
        )

        block = self._block(client.get(f"/player-chart/{subject}").text)
        summary = re.search(r"<summary>(.*?)</summary>", block, re.S).group(1)
        printed = re.findall(r"&times;([\d.]+)", summary)
        assert "1.0" not in printed, (
            f"{subject}: the closed summary names a driver and prints "
            f"&times;1.0 — {' '.join(summary.split())}"
        )
        assert "% floor" in summary, (
            "the summary dropped an empty driver and put nothing in its place"
        )

    def test_a_floor_player_says_his_price_was_raised_to_the_minimum(self, client):
        """The clamp is the COMMON case, not an edge: 490 of 705 pool players
        have an unclamped median below their position's min_bid."""
        import main
        from price_model import decompose_player

        clamped = next(
            name for name, p in main.auction_state.available_players.items()
            if decompose_player(
                p, main.model_params, main.auction_state.price_reference[p.position]
            ).clamped == "min"
        )
        block = self._block(client.get(f"/player-chart/{clamped}").text)
        assert "minimum bid" in block, (
            f"{clamped} prices below the floor and the card does not say his "
            f"figure was raised to it"
        )

    def test_it_does_not_claim_to_explain_the_expected_price(self, client):
        """E[$] is where the two chains MEET, and neither column reaches it.

        Both columns terminate — price at the model median, floor odds at
        P(floor) — and E[$] combines them through a clipped log-normal whose
        sigma depends on log_mu and whose bounds are per-position. So it is a
        stated blend, never a third column. The note used to say "P(floor) is a
        separate model and is not broken down here", which stopped being true
        on 2026-09-11 when stage 1 got its own column; what has to stay true is
        that E[$] is not attributed.
        """
        block = self._block(client.get(f"/player-chart/{self._a_priced_forward()}").text)
        note = re.search(r'<p class="driver-note">(.*?)</p>', block, re.S)
        assert note, "the card lost the note that bounds what it explains"
        note = " ".join(note.group(1).split())
        assert "model median" in note, (
            "the note does not say where the price column stops"
        )
        assert "blends the two" in note and "floor" in note, (
            f"the note does not say E[$] is a blend rather than a row: {note}"
        )
        # The guard that matters: no per-row attribution of E[$] anywhere in
        # the table. Three effect columns would mean someone built one.
        # The lookahead is load-bearing: `<th[^>]*>` also matches `<thead>`,
        # which swallowed the whole header row into one bogus "cell".
        header_cells = re.findall(r"<th(?=[\s>])[^>]*>(.*?)</th>", block, re.S)
        assert sum(1 for h in header_cells if h.strip()) == 2, (
            f"the table grew a third labelled column: {header_cells}"
        )

    def test_the_chart_body_still_carries_no_ids_at_all(self, client):
        """Mount-agnostic, unlike the older assertion next door.

        That one names one specific id string, so a new `<details id=...>`
        would sail past it while putting a duplicate in the document the
        moment both mounts hold a chart.
        """
        r = client.get(f"/player-chart/{self._a_priced_forward()}")
        assert 'id="' not in r.text, (
            "the chart body grew an id; it is mounted twice, so that is a "
            "duplicate id in the assembled page"
        )

    def test_the_card_survives_a_state_with_no_reference(self, client):
        """A legacy snapshot whose backfill was skipped must degrade, not 500."""
        import main

        saved = main.auction_state.price_reference
        main.auction_state.price_reference = {}
        try:
            r = client.get(f"/player-chart/{self._a_priced_forward()}")
            assert r.status_code == 200
            assert "price-drivers" not in r.text, "a breakdown with no reference"
            assert "price-chart" in r.text, "the chart itself went too"
        finally:
            main.auction_state.price_reference = saved


class TestTheFloorOddsColumn:
    """Stage 1 reaches the screen since 2026-09-11, and it is not a price factor.

    490 of 705 pool players clamp up to their position's min_bid, so for most
    of the pool P(floor) IS the price story and the median waterfall explains a
    figure nobody pays. The column shows the ODDS RATIO on a floor sale, which
    is the only per-row quantity stage 1 has that does not move with the row's
    position in the list.
    """

    def _block(self, text: str) -> str:
        start = text.index('<details class="price-drivers">')
        return text[start:text.index("</details>", start)]

    def _decomposed(self, name: str):
        import main
        from price_model import decompose_player

        player = main.auction_state.available_players[name]
        return decompose_player(
            player, main.model_params,
            main.auction_state.price_reference[player.position],
        )

    def test_a_row_inert_in_price_but_live_on_the_floor_odds_is_shown(self, client):
        """The mutant this whole pairing exists to stop.

        Measured 2026-09-11 over the fresh pool, 11 of the 12 rows whose price
        factor prints x1.00 move the floor odds by something worth seeing —
        against exactly 1 row inert in both. So hiding on the price column
        alone, which is what the 2026-09-11 rounding change would have done had
        it shipped by itself, drops eleven rows whose entire content is in the
        other column.

        The player is derived by that ROLE and the search asserts it found one,
        so a pool refresh that removes the case fails loudly rather than
        passing vacuously.
        """
        import main

        subject = None
        for name, player in main.auction_state.available_players.items():
            if player.position not in main.auction_state.price_reference:
                continue
            b = self._decomposed(name)
            for d in b.drivers:
                if (main._is_unit(d.factor)
                        and not main._is_unit(math.exp(d.floor_logit_delta))):
                    subject = (name, d)
                    break
            if subject:
                break
        assert subject, (
            "no pool player has a driver that is inert on price and live on "
            "the floor odds, so this test cannot fail and must be re-derived"
        )
        name, driver = subject

        block = self._block(client.get(f"/player-chart/{name}").text)
        assert driver.group in block, (
            f"{name}: {driver.group} multiplies the floor odds by "
            f"{math.exp(driver.floor_logit_delta):.3g} and the card hid the row "
            f"because its PRICE factor rounds to 1.00"
        )
        assert main._odds_label(math.exp(driver.floor_logit_delta)) in block

    def test_a_row_that_says_nothing_in_either_column_is_hidden(self, client):
        """The other direction, and the whole practical effect of the rounding
        change: measured, exactly one row across 705 players.

        The subject is NUDGED, not zeroed, and that is the entire point. A
        player decomposed against his own features has every delta at exactly
        0.0, where the old `log_delta != 0.0` rule and the new rounding rule
        agree — measured 2026-09-11, reverting to the old rule against a
        zeroed subject leaves this green. So one input moves by an epsilon:
        both of that driver's deltas become non-zero and both still print
        1.00, which is the only shape that separates the two rules.

        Constructed rather than hunted because the one real pool row is one
        refresh away from zero, and a test that silently stops finding its
        subject is worse than no test.
        """
        import main
        from price_model import build_features, decompose_price

        own = build_features("F", 90, 6.0, True, main.model_params,
                             last_salary=5.0, pos_rank=4)
        b = decompose_price("F", 90, 6.0 + 1e-6, True, main.model_params, own,
                            last_salary=5.0, pos_rank=4)

        nudged = [d for d in b.drivers if d.log_delta != 0.0]
        assert len(nudged) == 1 and nudged[0].group == "NHL team", (
            f"the nudge moved {[d.group for d in nudged]}, not just NHL team"
        )
        assert nudged[0].floor_logit_delta != 0.0, (
            "the nudge did not reach stage 1, so this cannot separate the rules"
        )
        assert all(
            main._is_unit(d.factor) and main._is_unit(math.exp(d.floor_logit_delta))
            for d in b.drivers
        ), "the nudged driver is visible at 2dp, so it is not an inert row"

        assert main._driver_rows(b)["rows"] == [], (
            "a driver that prints 1.00 in BOTH columns is still on the card"
        )

    def test_the_column_reconstructs_the_players_own_floor_percentage(self, client):
        """Base odds x the RENDERED ratios lands on the RENDERED final figure.

        The unit test in tests/test_price_model.py proves the logit chain
        reconstructs; this proves the TEMPLATE renders that chain and not some
        other field. Tolerance is loose on purpose — the card prints 2sf
        multipliers and 0dp percentages, so re-multiplying is lossy by design,
        and a tight bound here would only be re-deriving the unit test.
        """
        import main

        name = next(
            n for n, p in main.auction_state.available_players.items()
            if p.position in main.auction_state.price_reference
        )
        b = self._decomposed(name)
        block = self._block(client.get(f"/player-chart/{name}").text)

        pct = [int(m) for m in re.findall(r'driver-effect">(\d+)%<', block)]
        assert len(pct) == 2, f"expected a base and a final percentage, got {pct}"
        base_pct, final_pct = pct
        assert base_pct == round(b.base_p_floor * 100)
        assert final_pct == round(b.prediction.p_floor * 100)

        base_odds = b.base_p_floor / (1.0 - b.base_p_floor)
        for d in b.drivers:
            base_odds *= math.exp(d.floor_logit_delta)
        assert round(100 * base_odds / (1 + base_odds)) == final_pct, (
            "the chain of odds ratios does not land on the printed P(floor)"
        )

    def test_the_two_columns_are_coloured_independently(self, client):
        """Measured, the two disagree about the price on 195 of 2361 rows — a
        driver that makes a player dearer while also making a $0.5M sale
        likelier. Those rows are the reason the column exists, so the floor bar
        carries its OWN direction and its own hues; painting both green/red off
        one reading would make them look self-contradictory instead.
        """
        import main

        def visible(d):
            return (not main._is_unit(d.factor)
                    and not main._is_unit(math.exp(d.floor_logit_delta)))

        # OPPOSITE SIGNS specifically. That is the only shape that can tell a
        # floor bar painted from `floor_up` apart from one painted from `up` —
        # measured 2026-09-11, a subject whose two deltas share a sign leaves
        # the mutant alive, because both readings then agree.
        subject = None
        for name, player in main.auction_state.available_players.items():
            if player.position not in main.auction_state.price_reference:
                continue
            for d in self._decomposed(name).drivers:
                if visible(d) and (d.log_delta > 0) != (d.floor_logit_delta > 0):
                    subject = (name, d)
                    break
            if subject:
                break
        assert subject, (
            "no pool player has a driver whose two columns point opposite "
            "ways, so this test cannot fail and must be re-derived"
        )
        name, driver = subject

        block = self._block(client.get(f"/player-chart/{name}").text)
        # Indexed against the live drivers in ENGINE order, the way
        # test_the_bars_are_scaled_in_log_space_not_on_factor_minus_one does.
        # Matching on the group NAME does not work here: Scarcity's tooltip
        # explains that it is derived from points and so contains the string
        # "Points", which quietly matched two rows.
        live = [d for d in self._decomposed(name).drivers
                if not (main._is_unit(d.factor)
                        and main._is_unit(math.exp(d.floor_logit_delta)))]
        rows = [r for r in re.findall(r"<tr>.*?</tr>", block, re.S)
                if "driver-bar" in r]
        assert len(rows) == len(live), (
            f"{len(rows)} rendered rows against {len(live)} live drivers"
        )
        row = rows[[d.group for d in live].index(driver.group)]
        price_dir = "up" if driver.log_delta >= 0 else "down"
        floor_dir = "up" if driver.floor_logit_delta >= 0 else "down"
        assert price_dir != floor_dir
        assert f'class="driver-bar {price_dir}"' in row, (
            f"{name}/{driver.group}: the price bar is not {price_dir}"
        )
        assert f'class="driver-bar odds {floor_dir}"' in row, (
            f"{name}/{driver.group}: the floor bar reads {price_dir}, so it is "
            f"painted from the PRICE direction rather than its own"
        )

    def test_the_floor_column_is_never_a_percentage_point_step(self, client):
        """The obvious 'improvement', and the reason it is wrong.

        A per-row '+12pp' is exp()'s dollar-step trap in a logistic costume:
        the sigmoid is nonlinear, so the step depends on what the running odds
        were when the row was applied. Only the two ENDS of the chain are
        quoted as percentages, so a percentage inside a driver row means
        somebody built the order-dependent version.
        """
        import main

        name = next(
            n for n, p in main.auction_state.available_players.items()
            if p.position in main.auction_state.price_reference
        )
        block = self._block(client.get(f"/player-chart/{name}").text)
        driver_rows = [
            r for r in re.findall(r"<tr>.*?</tr>", block, re.S)
            if "driver-bar" in r
        ]
        assert driver_rows, "no driver rows to check"
        # The EFFECT cells only. A row's label cell legitimately prints "11.0%
        # Cup odds (typical 2.0%)" — that is the player's INPUT, not a step.
        for r in driver_rows:
            for cell in re.findall(r'<td class="driver-effect">(.*?)</td>', r, re.S):
                assert "%" not in cell, (
                    f"a driver row's effect cell prints a percentage: {cell!r}"
                )
                assert "pp" not in cell


class TestOddsLabel:
    """`main._odds_label` — the format, which cannot be a plain %.2f.

    Measured over the fresh 705-player pool the stage-1 odds ratios span
    8.6e-08 to 51.6, so two fixed decimals print `x0.00` on 66 rows: a column
    reporting "this driver did nothing" about the single largest effect on the
    card. Magnitude percentiles are p50 1.99, p90 14.2, p99 3392.
    """

    def test_a_ratio_below_one_is_shown_as_a_division(self):
        import main

        assert main._odds_label(0.5) == "÷2.00"
        assert main._odds_label(2.0) == "×2.00"

    def test_the_reciprocal_pair_reads_the_same_magnitude(self):
        """x2.00 and /2.00 are the same size effect pointing opposite ways —
        that is the whole reason the reciprocal is shown rather than 0.50."""
        import main

        for r in (1.5, 2.0, 7.25, 40.0, 300.0):
            up, down = main._odds_label(r), main._odds_label(1.0 / r)
            assert up[1:] == down[1:], f"{up} and {down} read differently"
            assert (up[0], down[0]) == ("×", "÷")

    def test_precision_follows_magnitude(self):
        import main

        assert main._odds_label(1.994) == "×1.99"
        assert main._odds_label(14.18) == "×14.2"
        assert main._odds_label(56.97) == "×57.0"
        assert main._odds_label(339.2) == "×339"

    def test_it_caps_rather_than_printing_seven_digits(self):
        """The measured maximum is 11,691,617. `/11691617` is not a number an
        operator uses; "he does not go at the minimum" is the whole content,
        and the bar carries the magnitude."""
        import main

        assert main._odds_label(1.0 / 11_691_617) == "÷1000+"
        assert main._odds_label(3391.8) == "×1000+"
        # Just below the cap still prints digits, so the boundary is real.
        assert main._odds_label(999.4) == "×999"
        # ...and anything that would ROUND to the cap takes the cap, rather
        # than printing a bare "1000" that looks like a measured figure.
        assert main._odds_label(999.6) == "×1000+"

    def test_exactly_one_is_not_a_division(self):
        import main

        assert main._odds_label(1.0) == "×1.00"

    def test_a_non_positive_ratio_reports_the_cap_rather_than_dividing(self):
        """The reciprocal is a division, and `math.exp` UNDERFLOWS to 0.0 below
        about -745 rather than raising. The pool has 729 of headroom today
        (floor_logit_delta runs -16.27 to +3.94) so the card cannot reach it —
        but model_params.json is regenerated by another repo, and this card is
        embedded in `/bid-check`, so the failure mode would be a 500 on the
        bidding path.
        """
        import main

        assert main._odds_label(0.0) == "÷1000+"
        assert main._odds_label(-1.0) == "÷1000+"

    def test_the_ratio_helper_cannot_overflow_or_underflow_exp(self):
        """`_odds_ratio` clamps in LOG space, before the exponential, so the
        pathological inputs never reach `math.exp` at all. Plain
        `math.exp(-800)` returns 0.0 and `math.exp(800)` raises."""
        import main

        assert math.exp(-800.0) == 0.0, "the underflow this guards stopped happening"
        with pytest.raises(OverflowError):
            math.exp(800.0)

        assert main._odds_ratio(-800.0) == pytest.approx(1.0 / 1000.0)
        assert main._odds_ratio(800.0) == pytest.approx(1000.0)
        assert main._odds_label(main._odds_ratio(-800.0)) == "÷1000+"
        assert main._odds_label(main._odds_ratio(800.0)) == "×1000+"

    def test_the_clamp_is_invisible_on_every_real_row(self, client):
        """It binds only past a thousandfold, which is where the label stops
        printing digits anyway — so no pool row renders differently because of
        it. Asserted, because a clamp that changed a printed figure would be a
        silent lie rather than a guard."""
        import main
        from price_model import decompose_player

        for player in main.auction_state.available_players.values():
            ref = main.auction_state.price_reference.get(player.position)
            if not ref:
                continue
            for d in decompose_player(player, main.model_params, ref).drivers:
                clamped = main._odds_label(main._odds_ratio(d.floor_logit_delta))
                raw = main._odds_label(math.exp(d.floor_logit_delta))
                assert clamped == raw, (
                    f"{player.name} {d.group}: the clamp changed {raw} to {clamped}"
                )

    def test_every_pool_row_gets_a_label_the_card_can_show(self, client):
        """No row anywhere in the live pool renders as x0.00 or an empty
        string — the failure a plain %.2f produces on 66 of them."""
        import main
        from price_model import decompose_player

        seen = 0
        for player in main.auction_state.available_players.values():
            ref = main.auction_state.price_reference.get(player.position)
            if not ref:
                continue
            for d in decompose_player(player, main.model_params, ref).drivers:
                label = main._odds_label(math.exp(d.floor_logit_delta))
                seen += 1
                assert label[0] in "×÷" and len(label) > 1, label
                assert label not in ("×0.00", "÷0.00"), (
                    f"{player.name} {d.group} rendered as {label}"
                )
        assert seen > 1000, f"only {seen} rows swept; the pool did not load"


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


class TestRenderingWhenTheOptimizerFails:
    """A failed MILP changes TWO panels, and each has to say so in its own words.

    `bid_panel.html` warns that every bid number is a floor fallback,
    deliberately OUTSIDE the `bid_advice` gate, so it qualifies the panel whether
    or not a bid is live. `team_panel.html` drops its headline number and its
    whole buy list, and since 2026-08-17 leaves a quiet empty-state note where
    they were — **not** a copy of the bid panel's alert. The two are on screen
    together at 1024px+ and are answering different questions: one says the
    numbers are fallbacks, the other says the content is gone. Both halves are
    covered here — the class was badge-only for the first hour of its life, and
    closing the "MILP-infeasible rendering" backlog item on that basis was an
    over-claim. Untested until
    2026-08-13 on the grounds that the trigger was unreachable — half true. It is
    unreachable through *bidding* (owner decision 2026-08-06: the commissioner
    refuses any bid that would leave a team unable to fill 24), but buyout
    penalties, `/trade-between` and `/adjust-salary` all raise cap load and
    **warn rather than refuse**, so the state is legal, just expensive to reach.
    """

    def _degrade(self) -> None:
        """Leave BOT unable to fill its roster at the floor, so the MILP fails.

        The precondition assert is the load-bearing line: with a nearly-full
        roster $1.0M IS a solvable budget, and all three tests below would then
        be asserting against an Optimal MILP without saying so.
        """
        import main

        bot = main.auction_state.teams[main.MY_TEAM]
        assert bot.total_spots_remaining >= 3, (
            f"BOT has only {bot.total_spots_remaining} spots left — $1.0M would "
            f"be a solvable budget and this would test nothing"
        )
        squeeze(main.MY_TEAM, 1.0)
        # GET / renders the context; it does not re-solve. Without this the page
        # would show the previous, Optimal solution.
        main._recompute()
        assert main.milp_solution.status != "Optimal", main.milp_solution.status

    def test_the_page_says_the_advice_is_degraded(self, client):
        self._degrade()
        r = client.get("/")
        assert "Optimizer Infeasible" in r.text, (
            "the MILP failed and the panel showed floor-value bid advice with "
            "nothing on screen to say the numbers are fallbacks"
        )

    def test_the_swapped_bid_fragment_carries_it_too(self, client):
        """/bid-check replaces #bid-panel, so the warning has to be inside it.

        Rendered only on `GET /` it would vanish on the first bidder toggle —
        i.e. exactly when the operator starts leaning on the numbers.
        """
        self._degrade()
        r = client.post("/bid-check", data={
            "player": pool_top()[0], "bidders": "BOT,SRL", "price": "1.0",
        })
        assert "Optimizer Infeasible" in r.text

    def test_a_solvable_optimizer_shows_no_warning(self, client):
        """A warning that is always up is wallpaper, which is worse than none.

        Anchored on the panel rendering at all, per the same reasoning as
        `test_a_quiet_page_carries_no_bid_advice_block`: a bare absence
        assertion goes green on an error page.
        """
        import main

        assert main.milp_solution.status == "Optimal", main.milp_solution.status
        page = client.get("/").text
        assert 'id="bid-panel"' in page, "GET / did not render the bid panel"
        assert "Optimizer" not in page

    def test_the_team_panel_stops_showing_a_buy_list(self, client):
        """The other half of a failed MILP: the buy list has to GO.

        `team_panel.html` gates both the Optimal Projected Points headline and
        every MILP *target* row on `status == "Optimal"`, so an Infeasible solve
        strips the buy list out of BOT's own panel — 12 of 63 rows and ~5.6KB,
        measured 2026-08-13. That removal is correct: there is no solution, and
        the prices beside those names would be floor fallbacks dressed as a plan.

        What was wrong until 2026-08-17 is that it happened SILENTLY, with the
        word "Optimizer" nowhere inside this panel. That half is now covered by
        `test_the_team_panel_says_why_the_buy_list_is_gone` — this test still
        owns the removal, and the two fail for opposite reasons.

        Asserted on the target NAMES rather than on a row count or the styling
        class that marks them, so a restyle cannot quietly make this vacuous.
        Narrowed to the names actually on screen while Optimal, which is what
        stops the second half passing for the wrong reason — an HTML-escaped
        name would never match either string either way.

        One mutation this deliberately does NOT kill: deleting
        `milp.status == "Optimal"` from `show_milp` (team_panel.html:72) leaves
        it green, because every non-Optimal return in `solve_optimal_roster`
        sets `roster=[]` (optimizer.py:147, 156, 262) and the `and milp.roster`
        term already gates the list. The status term there is belt-and-braces,
        not a second source of truth — the *headline* alert on line 61 has no
        such backstop and dropping its check does redden this test. Recorded so
        the surviving mutant reads as redundancy rather than as a weak assert.
        """
        import main

        bot = main.auction_state.teams[main.MY_TEAM]
        rostered = {p.name for p in bot.keeper_players + bot.acquired_players}
        targets = [p.name for p in main.milp_solution.roster if p.name not in rostered]
        assert targets, "a fresh BOT has nothing left to buy — this tests nothing"

        panel = section_of(client.get("/").text, "team-panel")
        assert "Optimal Projected Points" in panel
        visible = [t for t in targets if t in panel]
        assert visible, f"none of {len(targets)} targets is on screen to lose"

        self._degrade()
        after = section_of(client.get("/").text, "team-panel")
        assert "Optimal Projected Points" not in after
        assert not [t for t in visible if t in after], (
            "the optimizer failed but the panel is still recommending buys, so "
            "the prices and points beside them are floor fallbacks presented as "
            "a plan"
        )

    def test_the_team_panel_says_why_the_buy_list_is_gone(self, client):
        """The panel that lost the content is the panel that has to explain it.

        Sliced with `section_of` and NOT optional: `#bid-panel` carries
        "Optimizer Infeasible" on the same page, so a whole-page assertion here
        would pass against the bug — the entire finding was that the two live in
        different grid columns.

        Asserts the status word too, not just "optimizer". A note that says
        something went wrong without saying what is a shrug, and `milp.status`
        is the one word that tells the operator whether to look at the cap or
        the pool.
        """
        import main

        self._degrade()
        panel = section_of(client.get("/").text, "team-panel")
        assert "optimizer" in panel.lower(), (
            "BOT's own panel dropped its projection and its whole buy list with "
            "nothing in it to say why — the explanation is in #bid-panel, a "
            "different grid column and a scroll away on mobile"
        )
        assert main.milp_solution.status in panel, (
            f"the note does not name the status ({main.milp_solution.status}), "
            f"so it says something is wrong without saying what"
        )

    def test_a_solvable_optimizer_leaves_the_team_panel_quiet(self, client):
        """A note that is always up is wallpaper — the mirror of the bid-panel guard.

        Anchored on the panel having rendered its healthy headline before
        asserting absence, for the reason spelled out in
        `test_a_solvable_optimizer_shows_no_warning`: a bare absence assertion
        goes green on an error page.

        What this uniquely kills — measured, because the first version of this
        docstring guessed and guessed wrong — is the note being split out of the
        `elif` into a **separate `if viewed_team.is_my_team`**, the obvious
        "simplification". That keeps it BOT-only, so the opponent test below
        still passes, but it stops being exclusive with the healthy headline and
        the note then shows on every solve. Turning the `elif` into `{% else %}`
        or dropping its `is_my_team` are NOT this test's mutants; both are
        invisible while the view is on BOT and belong to the opponent test.
        """
        import main

        assert main.milp_solution.status == "Optimal", main.milp_solution.status
        panel = section_of(client.get("/").text, "team-panel")
        assert "Optimal Projected Points" in panel, "the panel never rendered"
        assert "optimizer" not in panel.lower()

    def test_an_opponents_panel_stays_silent_about_bots_optimizer(self, client):
        """BOT's optimizer says nothing about a rival, so their panel must not either.

        `_recompute` solves for BOT alone, exactly like the buyout dots — an
        opponent's panel has no projection and no buy list whether or not the
        MILP is healthy, so a note about a failed solve there describes nothing
        the operator is looking at.

        The `GET /team-view` is the mechanism, not ceremony. Since 2026-08-07 the
        view lives in `main._viewed_team` and `/team-view` is the only thing that
        writes it, so without this call the panel is still BOT's — and a mutant
        that dropped `is_my_team` would leak BOT into BOT and pass, the same way
        the move to a global once disarmed `TestPanelContextIsolation`.

        Anchored on the **← My Team** link, which `team_panel.html` renders only
        when `not viewed_team.is_my_team`, so it is positive proof of whose panel
        this is. `opponent in panel` — the first version — proves nothing:
        measured, BOT's OWN panel contains every opponent's code, because the
        Trade Between form lists all eleven in a `<select>`. That is the exact
        trap `section_of`'s own docstring documents, and slicing the panel out
        does not help when the string is inside the slice.
        """
        import main

        self._degrade()
        opponent = next(c for c in main.auction_state.teams if c != main.MY_TEAM)
        panel = section_of(client.get(f"/team-view/{opponent}").text, "team-panel")
        assert "← My Team" in panel, (
            f"the panel on screen is not an opponent's — /team-view/{opponent} "
            f"did not move the view, so this asserts nothing about the gate"
        )
        assert "optimizer" not in panel.lower(), (
            f"{opponent}'s panel reports BOT's optimizer status as though it "
            f"described them"
        )


class TestMarginalOnlyShowsWhenItDiffers:
    """"Worth up to" and "Marginal" were the same number in every normal state.

    `value_cap = round(min(marginal, physical_max_bid), 1)` and
    `compute_marginal_value` is itself bounded by `physical_max_bid` at four of
    its five exits, so the panel printed one figure under two labels with two
    tooltips calling them different concepts. The one regime where they diverge
    had no test at all before this class.
    """

    def _advice(self, client):
        player = pool_top()[0]
        return client.post(
            "/bid-check",
            data={"player": player, "current_price": "0.5", "bidders": f"{MY_TEAM},SRL"},
        ).text

    def test_it_is_absent_when_it_equals_the_cap(self, client):
        assert "Worth up to" in (body := self._advice(client))
        assert "Marginal" not in body, (
            "Marginal is being printed when it equals 'Worth up to' — the same "
            "number twice, two labels apart"
        )

    def test_it_is_shown_when_the_cap_clamped_it(self, client):
        """An over-committed team: spendable negative, so `physical_max_bid`
        floors below `MIN_SALARY` while the marginal falls back to it.

        This is the ONLY reachable divergence, and it is what makes the row
        worth rendering at all — here the two figures genuinely mean different
        things ("what you can bid" against "what he would be worth").
        """
        import main

        bot = main.auction_state.teams[MY_TEAM]
        spots = main.auction_state.teams[MY_TEAM].total_spots_remaining
        set_headroom(bot, round(spots * MIN_SALARY - 0.3, 1))
        main._recompute()
        assert bot.physical_max_bid < MIN_SALARY, (
            "the fixture did not reach the clamped regime, so the assertion "
            "below would pass for the wrong reason"
        )

        body = self._advice(client)
        assert "Marginal" in body
        cap = re.search(r"Worth up to: <strong>\$([\d.]+)M", body)
        marginal = re.search(r"Marginal: \$([\d.]+)M", body)
        assert cap and marginal, body[:400]
        assert cap.group(1) != marginal.group(1), (
            "the row rendered but shows the same figure — the guard is not "
            "comparing what the panel prints"
        )


class TestLeagueStateIsNarrow:
    """The densest table in the app answers by code and by glyph, not in prose.

    `#league-state` is the widest element on screen and the one that used to set
    its whole grid column's floor. Sliced with `section_of` on purpose: both
    trade dropdowns render `code — name` for all eleven teams, so a whole-page
    check for a team name is true no matter what this table does — the exact
    trap `section_of`'s own docstring documents.
    """

    def _panel(self, client) -> str:
        return section_of(client.get("/").text, "league-state")

    def test_every_row_has_a_cell_for_every_header(self, client):
        """The Penalty column is conditional in BOTH `<th>` and `<td>`.

        Two `{% if any_penalties %}` guards 40 lines apart have to agree, and
        nothing checked that they did. Written after this table's column count
        was found recorded as 13 in four separate documents when it is 12 — a
        number nobody could check is a number that rots, so this asserts the
        shape instead of restating the figure. Deliberately relative: it pins
        header-to-cell agreement, not a hard-coded 12, so adding a column is a
        one-line template change rather than a test edit.
        """
        import main

        for penalties in (True, False):
            for t in main.auction_state.teams.values():
                t.penalties = 0.3 if penalties else 0.0
                t._invalidate_cache()
            panel = self._panel(client)
            head = panel[panel.index("<thead>"):panel.index("</thead>")]
            # `<th>` and `<th ` specifically: `count("<th")` also matches
            # `<thead>`, and that off-by-one is almost certainly where the
            # recorded 13 came from — the first draft of THIS test reproduced
            # it, which is the best argument for the test existing.
            headers = head.count("<th>") + head.count("<th ")
            body = panel[panel.index("<tbody>"):]
            rows = [r for r in body.split("<tr")[1:]]
            assert rows, "League State rendered no rows"
            for row in rows:
                assert row.count("<td") == headers, (
                    f"{headers} headers but {row.count('<td')} cells with "
                    f"penalties={penalties} — the two `any_penalties` guards "
                    f"disagree, so a column is silently offset"
                )

    def test_every_sort_index_matches_its_own_column(self, client):
        """`data-sort-col` must equal the header's real position in the row.

        `sortTable` indexes `a.cells[col]` directly (`static/shortcuts.js`), so a
        stale index does not fail — it silently sorts a DIFFERENT column while
        the arrow appears over the one clicked. Nothing else can catch that:
        `test_every_row_has_a_cell_for_every_header` above pins header-to-cell
        COUNTS, which stay correct under a renumbering mistake.

        Both `any_penalties` states, because the indices are written as
        `{{ n if any_penalties else n-1 }}` and a hand-edit can get one branch
        right and the other wrong. Written when the Spendable column was removed
        and every header after it had to shift down one.
        """
        import main

        for penalties in (True, False):
            for t in main.auction_state.teams.values():
                t.penalties = 0.3 if penalties else 0.0
                t._invalidate_cache()
            panel = self._panel(client)
            head = panel[panel.index("<thead>"):panel.index("</thead>")]
            cells = re.findall(r"<th\b[^>]*>", head)
            for position, tag in enumerate(cells):
                declared = re.search(r'data-sort-col="(\d+)"', tag)
                if declared is None:
                    continue  # unsortable columns carry no index
                assert int(declared.group(1)) == position, (
                    f"header {position} declares data-sort-col="
                    f"{declared.group(1)} with penalties={penalties} — "
                    f"sortTable would sort column {declared.group(1)} instead"
                )

    def test_it_has_no_spendable_column(self, client):
        """Removed 2026-09-07 — Remaining is the figure the operator reads, and
        Spendable is derivable from it. The `spendable_budget` PROPERTY stays;
        `TeamState.physical_max_bid` is built on it.
        """
        assert "Spendable" not in self._panel(client)

    def test_it_shows_codes_and_not_full_names(self, client):
        import main

        panel = self._panel(client)
        teams = main.auction_state.teams
        missing = [c for c in teams if c not in panel]
        assert not missing, f"League State is missing team codes: {missing}"
        # The name is still in the markup, as the `title=` the sibling test
        # requires — so "not in panel" would be false by construction. Count
        # instead: every occurrence must BE that attribute. Restoring the name
        # span puts a second one in the cell text and this fires.
        spelled_out = [
            t.name for t in teams.values()
            if panel.count(t.name) > panel.count(f'title="{t.name}"')
        ]
        assert not spelled_out, (
            f"full team names are rendered as text in League State "
            f"({spelled_out}) — they cost width in the one table that already "
            f"scrolls to fit"
        )

    def test_the_name_is_still_one_hover_away(self, client):
        """Narrower must not mean unreadable: the code still resolves to a name."""
        import main

        panel = self._panel(client)
        for t in main.auction_state.teams.values():
            assert f'title="{t.name}"' in panel, (
                f"{t.code} carries no title, so its code decodes to nothing"
            )

    def test_done_reads_as_a_glyph_both_ways(self, client):
        """Driven through the real toggle, not read off a fresh template.

        The colour classes already encode the state, so asserting on those would
        pass against a button that still said "Stopped Drafting" — the glyph is
        the thing this change is about.
        """
        import main

        victim = next(c for c in main.auction_state.teams if c != main.MY_TEAM)
        before = self._panel(client)
        assert "○" in before and "✕" not in before, "a fresh league has nobody done"

        r = client.post("/team-done", data={"team_code": victim})
        assert main.auction_state.teams[victim].is_done, (
            f"/team-done did not mark {victim} done, so the glyph below would "
            f"be asserting against an unchanged table"
        )
        after = section_of(r.text, "league-state")
        assert "✕" in after, f"{victim} is done but the row does not say so"
        assert 'aria-label="Stopped drafting"' in after, (
            "the glyph replaced the button's accessible name instead of moving it"
        )


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


class TestBidSessionSurvives:
    """The live bidding session exists only in #bid-panel's DOM.

    Player, price and bidder toggles are never persisted server-side, so any
    response that replaces that region loses them. Before the panel was split,
    /nominate returned the whole #auction-control from base context and wiped
    the session — and it fires on a bare `n` keypress, whose guard only covers
    INPUT/TEXTAREA/SELECT, so focus on a bidder-logo button left it live.

    These assert the STRUCTURAL property rather than any rendered value: an
    endpoint cannot clobber a region it does not return.
    """

    def test_nominate_cannot_touch_the_bid_region(self, client):
        r = client.get("/nominate")
        assert r.status_code == 200
        assert 'id="nomination-panel"' in r.text
        assert 'id="bid-form"' not in r.text
        assert 'id="bidder-logos"' not in r.text

    def test_bid_check_cannot_touch_the_nomination_region(self, client):
        """The converse: a price change must not drop a nomination pick."""
        r = client.post("/bid-check", data={
            "player": pool_top(1)[0], "price": "3.0", "bidders": "BOT,LGN,SRL",
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
            "player": pool_top(1)[0], "price": "3.0", "bidders": "BOT,LGN",
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
            "player": pool_top(1)[0], "price": "3.0", "bidders": "BOT,LGN,SRL",
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
        r = client.get(
            "/buyout-check", params={"player_name": a_buyout_candidate().name}
        )
        assert r.status_code == 200
        assert "Buyout" in r.text

    def test_buyout_check_invalid(self, client):
        r = client.get("/buyout-check", params={"player_name": "Nobody"})
        assert r.status_code == 200


class TestTheBuyoutPicker:
    """The `<select>` that replaced the row of ~15 buttons on 2026-08-15."""

    def test_it_offers_exactly_the_eligible_set(self, client):
        """The scan, the dots and this picker read ONE expression, per CLAUDE.md
        — `all_players|selectattr('can_be_bought_out')`.

        Stated as an equality because the two halves fail differently and both
        have happened: a `roster_players` copy here is the 2026-08-07 bug that
        silently hid 11 of BOT's 15 eligible players (reads on screen as "no
        buyout helps"), while offering an A-E prospect is a button for a move
        the engine will refuse. The existing eligibility tests cover
        ineligible-are-absent and minors-are-present; neither notices an
        eligible ACTIVE player going missing.
        """
        import main

        offered = set(buyout_options(client.get("/").text))
        bot = main.auction_state.teams[main.MY_TEAM]
        eligible = {p.name for p in bot.all_players if p.can_be_bought_out}
        assert eligible, "BOT has no eligible player — the fixture is wrong"
        assert offered == eligible, (
            f"missing: {sorted(eligible - offered)}; "
            f"offered illegally: {sorted(offered - eligible)}"
        )

    def test_the_checked_player_stays_selected(self, client):
        """`/buyout-check` swaps the whole panel, this select included.

        Without the `selected` the box snaps back to "Check a buyout…" while a
        verdict for someone else sits underneath it — the control disagreeing
        with the answer directly below it, on an irreversible move.
        """
        victim = a_buyout_candidate().name
        panel = section_of(
            client.get("/buyout-check", params={"player_name": victim}).text,
            "buyout-panel",
        )
        chosen = re.findall(r'<option value="([^"]*)"[^>]*\bselected\b', panel)
        assert chosen == [victim], f"selected option is {chosen}, wanted [{victim}]"

    def test_the_first_option_is_an_empty_placeholder(self, client):
        """Otherwise the top candidate is pre-selected and choosing him fires no
        `change` — the first click on the most likely buyout does nothing.

        Asserted here rather than in the browser because Playwright's
        `select_option` dispatches `change` unconditionally, so the harness that
        looks like the right place to catch this physically cannot.
        """
        panel = section_of(client.get("/").text, "buyout-panel")
        first = re.search(r"<option[^>]*>", panel).group(0)
        assert 'value=""' in first and "selected" in first, (
            f"first option is {first!r} — a pre-selected real candidate"
        )

    def test_a_name_the_markup_escapes_still_round_trips(self, client):
        """`buyout_options` must hand back `Player.name`, not the escaped form.

        Jinja escapes the attribute, so an apostrophe name comes out of the
        panel as `Ryan O&#39;Reilly` and compares unequal to every `p.name` the
        eligibility tests check it against. The direction that matters is the
        silent one: `test_ineligible_players_are_not_offered` asks
        `p.name in offered`, so an illegally-offered apostrophe player is simply
        not found and the guard reports clean. Two such names are in the pool
        today and one pick makes one BOT's, group 3 and eligible.

        The app was never affected — the browser unescapes before htmx reads
        `select.value` — which is why nothing on screen would have shown it.
        """
        import main
        from markupsafe import escape

        victim = next(
            (n for n in main.auction_state.available_players
             if str(escape(n)) != n),
            None,
        )
        if victim is None:
            pytest.skip("no pool name today contains a character Jinja escapes")

        assign(client, victim, main.MY_TEAM, 1.0)
        p = main.auction_state.teams[main.MY_TEAM].find_player(victim)
        assert p.can_be_bought_out, (
            f"{victim} drafted as group {p.group} — pick a different probe"
        )
        assert victim in buyout_options(client.get("/").text)

    def test_a_team_with_nothing_to_buy_out_says_so(self, client):
        """The `{% else %}` branch, which nothing reached: BOT always has
        candidates, so the whole thing was dead code that read as handled.

        It also guards the materializer. `selectattr` returns a GENERATOR and
        `{% if %}` on one is always truthy, so a `candidates` that is not a
        container renders an empty `<select>` here — a control that looks
        broken — while every other test stays green. Today `sort` supplies the
        materializing (it returns a list; the trailing `|list` is belt-and-
        braces for the day someone asks why a dropdown needs sorting), so the
        mutation this dies on is dropping BOTH, not the `|list` alone.

        Groups are edited in place rather than by buying fifteen players out,
        which would be fifteen MILP solves for a one-line branch. `client` is
        function-scoped and resets, so the mutation does not leak.
        """
        import main

        bot = main.auction_state.teams[main.MY_TEAM]
        for p in bot.all_players:
            if p.can_be_bought_out:
                p.group = "A"
        assert not any(p.can_be_bought_out for p in bot.all_players)

        panel = section_of(client.get("/").text, "buyout-panel")
        assert "<select" not in panel, "an empty picker is a control that looks broken"
        assert "No group 2/3 players" in panel

    def test_the_picker_is_named(self, client):
        """It replaced buttons whose visible text was their accessible name.

        Measured in Chrome after the swap: role=combobox, name '', no name
        source — so a screen reader announces an unlabelled combo box. Same gap
        the close buttons, the League State done glyph and the two filter groups
        were fixed for.
        """
        panel = section_of(client.get("/").text, "buyout-panel")
        select = re.search(r"<select[^>]*>", panel).group(0)
        assert "aria-label=" in select, f"the picker has no name: {select!r}"

    def test_the_picker_carries_no_buyout_dots(self, client):
        """The `bo-` placeholders live in team_panel.html's roster tables only.

        `_dom_id` mints ONE id per player and htmx resolves an out-of-band
        target with `querySelectorAll("#"+id)`, so a second copy in here would
        be a duplicate id that the scan swaps twice. BACKLOG.md's entry for this
        want assumed the dots lived in the Analyzer and had to be rehoused; they
        never did, and moving them in would be the defect.
        """
        panel = section_of(client.get("/").text, "buyout-panel")
        assert 'id="bo-' not in panel


class TestRoundThreeMutators:
    """Round 3 mutators: minors movement, scenario load, change_log + cascade."""

    def _draft_to(self, client, team: str, player: str | None = None, salary: str = "5.0"):
        """Draft a player to a team so it has at least one acquired player to test on.

        Derived and routed through `helpers.assign`, which fails AT the pick.
        This helper used to default to a literal name and assert only
        `status_code == 200` — and `/assign` answers 200 WITH A TOAST when it
        rejects, so the day that name left `players.csv` all five tests built on
        it would have passed against a pick that never happened, surfacing
        somewhere else entirely.
        """
        player = player or pool_top(1)[0]
        assign(client, player, team, float(salary))
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

    def test_a_scenario_that_cannot_build_says_so_and_changes_nothing(
        self, client, monkeypatch, caplog,
    ):
        """`_fill` raises when the pool cannot supply what a construction asks for.

        A refreshed `players.csv` is the realistic cause, and unhandled it is a
        500 — on which htmx swaps nothing, so the operator gets a click that
        appears to do nothing at all. The live draft has to survive it too: every
        mutation in the endpoint happens after `scenarios.load` returns, and this
        is the test that keeps it that way.
        """
        import main
        import scenarios

        client.post("/reset")
        before = client.get("/state").json()
        name = sorted(scenarios.SCENARIOS)[0]

        def explode(_name):
            raise RuntimeError("SRL stalled at 19 of 23")

        monkeypatch.setattr(scenarios, "load", explode)
        with caplog.at_level("ERROR"):
            r = client.post("/load-scenario", data={"name": name})

        assert r.status_code == 200, "a broken scenario must not 500 at the operator"
        toast = toast_of(r)
        assert toast.get("type") == "error", toast
        assert "stalled at 19 of 23" in toast.get("message", ""), (
            f"the toast has to name what went wrong, got {toast}"
        )
        assert "failed to build" in caplog.text, "and it has to be logged"
        assert client.get("/state").json() == before, (
            "the live draft must be exactly as it was"
        )

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

    def test_a_buyout_shows_my_team(self, client, viewing_srl):
        """`execute_buyout` is BOT-only, so your cap is the only thing that moved.

        The undo side of this was already covered; the forward side was not, and
        `test_undoing_a_buyout_shows_my_team` reopening SRL after the buyout
        *implied* this without asserting it.
        """
        r = client.post("/buyout", data={"player": a_buyout_candidate().name})
        assert toast_of(r).get("type") == "success", toast_of(r)
        assert self._panel_team(r.text) == "BOT"

    def test_a_failed_buyout_does_not_move_the_view(self, client, viewing_srl):
        """The error branch returns before `_view_team`, and must keep doing so.

        A refusal is not an operation on your roster, so it has nothing to show
        you about it.
        """
        r = client.post("/buyout", data={"player": "Nobody At All"})
        assert toast_of(r).get("type") == "error", toast_of(r)
        assert self._panel_team(r.text) == "SRL"

    def test_loading_a_scenario_shows_my_team(self, client, viewing_srl):
        """A scenario replaces the world, so a view into the old one means nothing."""
        import scenarios

        name = sorted(scenarios.SCENARIOS)[0]
        r = client.post("/load-scenario", data={"name": name})
        assert toast_of(r).get("type") == "success", toast_of(r)
        assert self._panel_team(r.text) == "BOT"

    def test_an_unknown_scenario_leaves_the_view_alone(self, client, viewing_srl):
        """Nothing was replaced, so there is nothing to point the panel at.

        This is the half that pins the comment on `/load-scenario`'s
        `_view_team` call: move it above the `except KeyError` return and it
        starts snapping the panel back on a request that changed nothing.
        """
        r = client.post("/load-scenario", data={"name": "not-a-scenario"})
        assert toast_of(r).get("type") == "error", toast_of(r)
        assert self._panel_team(r.text) == "SRL"

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


class TestTheRosterKeyExplainsTheTargetRows:
    """The panel's best output was conveyed by a colour and a slant, unlabelled.

    A MILP target row is `text-info opacity-50 italic` and carries no Actions
    cell; nothing on screen said so, so "these are the players to buy" read as
    possibly-already-mine. The key is conditional on targets being present,
    because that conditionality is what pays for it — a panel with no target rows
    has no ambiguity to resolve and is tight at 1280px.
    """

    KEY = "suggested buy"

    def test_the_key_is_there_when_targets_are(self, client):
        panel = section_of(client.get("/").text, "team-panel")
        assert "italic" in panel, "no target rows on a fresh BOT — wrong premise"
        assert self.KEY in panel, "the target rows are still unexplained"

    def test_it_names_all_three_row_states(self, client):
        """Naming only the blue rows leaves green-vs-plain as a second mystery."""
        panel = section_of(client.get("/").text, "team-panel")
        for state in (self.KEY, "drafted", "keeper"):
            assert state in panel, f"the key does not mention {state!r}"

    def test_it_is_absent_on_an_opponent(self, client):
        """The half that can fail against a key rendered unconditionally.

        `show_milp` requires `is_my_team`, so an opponent's panel has no target
        rows at all — a key there would describe a styling that is not on screen.
        """
        client.get("/team-view/SRL")
        try:
            panel = section_of(client.get("/").text, "team-panel")
            assert "italic" not in panel, "an opponent should have no target rows"
            assert self.KEY not in panel, \
                "the key describes rows this panel does not render"
        finally:
            client.get("/team-view/BOT")


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

    def test_the_picker_stays_on_an_opponent(self, client):
        """It acts on `team` (always BOT) and renders into #buyout-panel, so it
        works whoever is on screen — gating it too would remove a working
        control along with the broken one.

        Reads the offered NAMES, not just the presence of the control: an empty
        select is still a `<select>`, and the failure this guards against is the
        candidates disappearing when you look at a rival.
        """
        client.get("/team-view/SRL")
        try:
            assert buyout_options(client.get("/").text)
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


class TestCapCountingMinorsAreColoured:
    """A group 2/3 minor's salary is fully on cap, and the table said so in one
    word.

    `counts_on_cap` was rendered only as the On Cap cell's Yes/No, so the rows
    that actually cost money were unscannable in a table that is mostly the
    other kind — at reset the whole league has 4 cap-counting minors against
    145 that are free. The row now carries `text-warning` and drops the
    `opacity-70` every other row has.

    Keyed on `counts_on_cap` rather than on `group == "3"`: `MINOR_CAP_GROUPS`
    is {"2", "3"}, and reading the same property the On Cap cell reads is what
    stops the colour and the word disagreeing.
    """

    def _minors_rows(self, html_text: str) -> dict[str, str]:
        """Every `<tr>` of the Minors table, keyed by player name.

        Sliced from the Minors heading to the end of its table rather than
        matched across the whole panel: the active-roster table above has the
        same shape, and `/trade-between`'s form lists the same players as
        `<option>`s.
        """
        panel = section_of(html_text, "team-panel")
        start = panel.find(">Minors (")
        assert start != -1, "no Minors table in this panel"
        end = panel.find("</table>", start)
        body = panel[start:end]
        rows = {}
        for m in re.finditer(r"<tr\b.*?</tr>", body, re.S):
            row = m.group(0)
            for cell in re.findall(r"<td>([^<]+)</td>", row):
                rows.setdefault(cell.strip(), row)
        return rows

    def _bot_minors(self):
        import main

        return main.auction_state.teams[MY_TEAM].minor_players

    def test_a_cap_counting_minor_is_warned_and_undimmed(self, client):
        on_cap = [p for p in self._bot_minors() if p.counts_on_cap]
        if not on_cap:
            pytest.skip("BOT has no cap-counting minor in this pool")
        rows = self._minors_rows(client.get(f"/team-view/{MY_TEAM}").text)
        for p in on_cap:
            row = rows.get(p.name)
            assert row, f"{p.name} has no minors row"
            assert "text-warning" in row, (
                f"{p.name} is group {p.group}, fully on cap, and reads like "
                f"every free minor around him"
            )
            assert "opacity-70" not in row.split(">", 1)[0], (
                f"{p.name} should not be dimmed — he is the row that costs money"
            )

    def test_a_free_minor_is_not(self, client):
        free = [p for p in self._bot_minors() if not p.counts_on_cap]
        if not free:
            pytest.skip("BOT has no cap-free minor in this pool")
        rows = self._minors_rows(client.get(f"/team-view/{MY_TEAM}").text)
        for p in free:
            row = rows.get(p.name)
            assert row, f"{p.name} has no minors row"
            assert "text-warning" not in row.split(">", 1)[0], (
                f"{p.name} is group {p.group} and costs nothing against the cap"
            )

    def test_the_colour_agrees_with_the_on_cap_cell(self, client):
        """The two readings of the same property, on every row.

        This is the assertion that makes the colour trustworthy: it can only
        pass while both sides read `counts_on_cap`. Swap either one for a
        literal group test and a group 2 minor breaks it.
        """
        rows = self._minors_rows(client.get(f"/team-view/{MY_TEAM}").text)
        assert rows, "BOT has no minors to check"
        for name, row in rows.items():
            warned = "text-warning" in row.split(">", 1)[0]
            says_yes = "<td>Yes</td>" in row
            assert warned == says_yes, (
                f"{name}: row colour says {warned}, On Cap cell says {says_yes}"
            )

    def test_an_opponents_minors_are_coloured_the_same_way(self, client):
        """Not gated on `is_my_team` — the buyout dots are, this is not.

        Reading an opponent's cap load is the point of opening their panel, and
        their group 2/3 minors count against *their* cap identically.

        The group is supplied rather than found: at reset all four of the
        league's cap-counting minors happen to be BOT's, so an `is_my_team`
        gate on the colour would pass every other test in this class against
        today's players.csv. That is the data making a mutant equivalent, which
        CLAUDE.md says to fix by providing the data that breaks it — the file
        is replaced before every draft and the coincidence will not survive it.
        """
        import main

        code, team = next(
            (c, t) for c, t in main.auction_state.teams.items()
            if c != MY_TEAM and t.minor_players
        )
        victim = team.minor_players[0]
        assert not victim.counts_on_cap, "pick a free minor to promote"
        original, victim.group = victim.group, sorted(MINOR_CAP_GROUPS)[0]
        try:
            assert victim.counts_on_cap
            rows = self._minors_rows(client.get(f"/team-view/{code}").text)
            assert "text-warning" in rows[victim.name], (
                f"{code}'s {victim.name} is on their cap and is not marked"
            )
        finally:
            victim.group = original


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
        offered = trade_choices(
            section_of(client.get("/").text, "trade-panel"), "Players I give"
        )

        assert minor.name in offered, (
            f"{minor.name} is in BOT's minors and cannot be offered in a trade"
        )
        assert "(M)" in offered[minor.name], (
            f"nothing marks {minor.name} as a minor: {offered[minor.name]!r}"
        )

        # The marker has to mean something, or it is noise on every row.
        import main
        active = main.auction_state.teams["BOT"].roster_players[0]
        # Membership FIRST, then the marker: `"(M)" not in ""` is true for a row
        # that is simply absent, so without this the control could stop
        # controlling anything and still read as a passing assertion.
        assert active.name in offered, f"{active.name} is missing from the Give list"
        assert "(M)" not in offered[active.name], (
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

        sends = trade_choices(panel, "Players BOT sends")

        assert len(sends) == len(bot.all_players), (
            f"the sends list offers {len(sends)} of {len(bot.all_players)} players"
        )
        assert minor.name in sends, (
            f"{minor.name} cannot be offered in the Trade Between form"
        )
        assert "(M)" in sends[minor.name], (
            f"nothing marks {minor.name}: {sends[minor.name]!r}"
        )

    def test_no_control_in_either_trade_form_is_unnamed(self, client):
        """Stated as the general rule, not as four specific labels.

        Both forms went from `<select multiple>` to checkbox lists on
        2026-08-15, and four of their six controls had no accessible name at all
        before that — measured in Chrome, `role=combobox`/`group` with an empty
        name and no name source. A fifth list added later is the case a
        label-by-label assertion would wave through, and it is invisible on
        screen either way.

        Each `<label class="choice-row">` names its own checkbox by wrapping it,
        so only the GROUPS and the two remaining `<select>`s need naming here.

        Every `<div>` is examined and then filtered on its class list, rather
        than matched as `<div class="choice-list"`. The literal form was written
        first and only worked because today's markup happens to put `class`
        first with nothing else in it: `<div id="x" class="choice-list">` and
        `<div class="choice-list mb-2">` both slipped through, which is the
        exact "fifth list added later" case this claims to catch.
        """
        page = client.get("/").text
        forms = section_of(page, "trade-panel") + section_of(page, "team-panel")

        controls = [
            tag for tag in re.findall(r"<(?:select|div)\b[^>]*>", forms)
            if tag.startswith("<select")
            or re.search(r'class="[^"]*\bchoice-list\b', tag)
        ]
        assert controls, "found no trade controls at all — the selector rotted"
        unnamed = [t for t in controls if "aria-label=" not in t]
        assert not unnamed, f"controls with no accessible name: {unnamed}"

    def test_the_between_form_takes_repeated_values(self, client):
        """One field per ticked box, which is what checkboxes post.

        Replaced a comma-joined hidden input. Deliberately a TWO-for-one: a
        1-for-1 exercises the same code path as the old single string and would
        pass against an endpoint that still read only the first value.
        """
        import main

        give = [p.name for p in main.auction_state.teams["SRL"].roster_players[:2]]
        get_ = [main.auction_state.teams["MAC"].roster_players[0].name]

        r = client.post("/trade-between", data={
            "team_a": "SRL", "team_b": "MAC",
            "players_from_a": give, "players_from_b": get_,
        })
        assert r.status_code == 200
        mac = main.auction_state.teams["MAC"]
        assert all(mac.find_player(n) is not None for n in give), (
            f"MAC did not receive both of {give} — only the first value was read"
        )
        assert main.auction_state.teams["SRL"].find_player(get_[0]) is not None

    def test_an_empty_submission_still_reports_no_players(self, client):
        """The "No players selected" branch, reached BOTH ways.

        Unticked checkboxes post nothing at all, so the live form sends no field
        and this checks `Form([])` really defaults to `[]`. A caller that sends
        an explicit empty value is the other shape — every pre-2026-08-15 test
        does, and there it arrives as `[""]`, which is truthy and would sail
        past the guard unfiltered. Both, because they exercise different code:
        the default and the `if n.strip()`.
        """
        for label, payload in (
            ("field omitted", {}),
            ("explicit empty", {"players_from_a": "", "players_from_b": ""}),
        ):
            r = client.post(
                "/trade-between", data={"team_a": "BOT", "team_b": "SRL", **payload}
            )
            assert toast_of(r).get("message") == "No players selected for trade", (
                f"{label}: the empty-trade guard did not fire"
            )

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


class TestEvaluatingATradeKeepsTheForm:
    """The reported bug: "the players get deselected, so I have to readd all of
    them to modify the trade to recheck things."

    The form used to sit inside the swap — `hx-target="#trade-panel"` with
    `hx-swap="outerHTML"` — and the template renders no `checked` anywhere, no
    `selected` on any partner option, and `#trade-receive-list` back at its
    "Select a team first" placeholder. So every evaluate handed back a stateless
    copy of the form you had just filled in. The answer is the same one
    `#bid-advice` uses inside `#bid-panel`: when the state lives only in the DOM,
    swap the answer and not the room it is standing in.

    The browser tests prove the ticks survive. These prove the RESPONSE cannot
    destroy them, which is the half that runs in 50ms.
    """

    def _evaluate(self, client):
        """Give BOT's lowest-points player, receive the partner's best.

        Derived by role, never by name. Chosen because the engine accepts it —
        asserted below — so the Execute form is actually rendered and the tests
        that care about it are not vacuous.
        """
        import main

        bot = main.auction_state.teams[MY_TEAM]
        give = min(bot.all_players, key=lambda p: p.projected_points)
        source = next(c for c, t in main.auction_state.teams.items()
                      if c != MY_TEAM and t.roster_players)
        incoming = max(main.auction_state.teams[source].roster_players,
                       key=lambda p: p.projected_points)
        return client.post("/trade-evaluate", data={
            "give_player": [give.name],
            "source_team": source,
            "receive_player": [json.dumps({
                "name": incoming.name,
                "position": incoming.position,
                "salary": incoming.salary,
                "projected_points": incoming.projected_points,
            })],
        })

    def test_the_response_is_the_verdict_and_not_the_form(self, client):
        """The one assertion that could not pass before the split: the give list
        is 49 checkboxes and every one of them used to come back unticked."""
        r = self._evaluate(client)

        assert r.status_code == 200, r.text
        assert 'class="trade-verdict' in r.text, "no verdict in the response"
        assert 'name="give_player"' not in r.text, (
            "the evaluate response still carries the give list, so the swap "
            "replaces the form and every tick is lost"
        )
        assert 'id="trade-receive-list"' not in r.text, (
            "the receive list is in the response, so it reverts to its "
            "placeholder and the partner selection goes with it"
        )

    def test_an_empty_evaluate_leaves_the_swap_target_standing(self, client):
        """A target that disappears with its contents can be swapped once.

        `bid_panel.html` documents the same trap from the other side: both
        branches of that template own `#bid-advice` for exactly this reason.
        Submitting with nothing ticked sets `result = None`, and the placeholder
        branch is what keeps the next evaluate able to land.
        """
        r = client.post("/trade-evaluate", data={})

        assert r.status_code == 200
        assert 'id="trade-result"' in r.text, (
            "an empty evaluate returned no swap target, so #trade-result is "
            "gone from the document and every later evaluate swaps nowhere"
        )
        assert "trade-verdict" not in r.text, "an empty trade produced a verdict"

    def test_the_form_targets_the_id_the_response_renders(self, client):
        """Two strings in two files with no import between them.

        A typo in either is completely silent: htmx logs nothing useful for a
        target it cannot find, and the panel simply stops answering.
        """
        form = section_of(client.get("/").text, "trade-panel")
        m = re.search(r'hx-post="/trade-evaluate"[^>]*hx-target="#([^"]+)"', form)
        assert m, "the evaluate form has no hx-target"

        assert f'id="{m.group(1)}"' in self._evaluate(client).text, (
            f'the form targets #{m.group(1)}, which the response does not render'
        )

    def test_the_page_holds_exactly_one_swap_target(self, client):
        """htmx takes the FIRST id match without complaining, so a second copy
        sends swaps into the wrong one — the `counterfactual.html` lesson."""
        assert client.get("/").text.count('id="trade-result"') == 1

    @staticmethod
    def _verdict_div(html: str) -> str:
        """The `.trade-verdict` element's own markup, by depth-counting `<div>`.

        Slicing from the class attribute to the END of the string is not this,
        and the difference is the entire point of the test below: measured
        2026-09-10, moving the Execute form out to a SIBLING of the verdict left
        a to-end-of-string slice green. A mutant that dies in no test is the
        thing to be suspicious of.
        """
        at = html.index('class="trade-verdict')
        start = html.rindex("<div", 0, at)
        depth, i = 0, start
        while True:
            opened = html.find("<div", i)
            closed = html.find("</div>", i)
            assert closed != -1, "unbalanced <div> in the verdict"
            if opened != -1 and opened < closed:
                depth, i = depth + 1, opened + len("<div")
            else:
                depth, i = depth - 1, closed + len("</div>")
                if depth == 0:
                    return html[start:i]

    def test_the_execute_button_is_inside_the_verdict(self, client):
        """`markTradeEvalStale` disables `button[type=submit]` scoped to
        `.trade-verdict`, so an Execute button rendered as a sibling would stay
        live against a selection that no longer matches the evaluation."""
        r = self._evaluate(client)
        assert "ACCEPT" in r.text, (
            "this fixture needs a trade the engine accepts, or no Execute "
            "button is rendered and the assertions below cannot fail"
        )

        verdict = self._verdict_div(r.text)
        assert 'hx-post="/trade-execute"' in verdict, (
            "the Execute form is outside .trade-verdict, so the staleness "
            "guard cannot reach its button"
        )
        assert 'name="trade_id"' in verdict
        assert "trade-stale-note" in verdict, (
            "the staleness warning is not in the verdict, so nothing on screen "
            "would say the form no longer matches the answer"
        )


class TestNoBidControlIsUnnamed:
    """Stated as the general rule, the same shape as
    `TestTheTradeFormCanSeeTheMinors::test_no_control_in_either_trade_form_is_unnamed`
    and for the same reason.

    `bid_panel.html` has TWO forms and the backlog entry named one attribute on
    one of them. The real inventory was six unnamed or uselessly-named controls:
    both player inputs, both price inputs, and the four `-`/`+` steppers. The
    steppers are the `&times;` close-button class from 2026-08-14 — a name
    exists and names nothing, which is worse than absent because it looks
    handled.

    Both branches of the template are covered, because they are mutually
    exclusive: the active form renders only with `bid_advice`, the Start Auction
    form only without, so a per-branch fix can pass a single-page assertion
    while leaving the other half silent.

    A `<button>` is named by its own text, so requiring `aria-label` on all of
    them would be wrong (Assign, Start Auction and the bidder logos are fine).
    The rule is "visible text with no letter or digit in it is not a name",
    which is what makes a glyph stepper a finding and `Assign to BOT` not one.
    """

    def _bid_panel(self, markup: str) -> str:
        """The #bid-panel fragment out of a full page or a /bid-check response.

        `/bid-check` returns the panel alone, so it needs no slicing; `GET /`
        buries it at the end of `<section id="auction-control">`, after the
        nomination panel. Slice rather than scan the page: the team panel has
        inputs of its own and `.choice-row` labels that name their checkboxes by
        wrapping them, which this rule would read as unnamed.
        """
        if 'id="auction-control"' in markup:
            markup = section_of(markup, "auction-control")
        return markup[markup.index('<div id="bid-panel"'):]

    def _unnamed(self, panel: str) -> list[str]:
        """Every control here that cannot take a name from its own content.

        `<select>` and `<textarea>` are scanned even though the bid panel has
        none today, for the same reason the trade-form test scans `<select>`:
        the control added later is the one a narrower scan waves through, and
        the panel is where a filter dropdown would plausibly land.

        The two arms are counted SEPARATELY. A single `assert suspects` fired
        only when both came back empty, so losing the whole field arm to an
        attribute-style change left the glyph buttons holding the assertion up
        while the input coverage silently vanished — measured 2026-08-20, the
        pre-auction branch is 2 fields + 2 glyph buttons and the live one 2 + 3,
        so either arm alone satisfies it. Floors rather than equalities, so
        adding a control is not a test edit.
        """
        fields = [
            tag for tag in re.findall(r"<(?:input|select|textarea)\b[^>]*>", panel)
            if not re.search(r'type="hidden"', tag)
        ]
        glyphs = []
        for tag, inner in re.findall(r"(<button\b[^>]*>)(.*?)</button>", panel, re.S):
            text = html.unescape(re.sub(r"<[^>]*>", "", inner))
            if not any(c.isalnum() for c in text):
                glyphs.append(tag)
        assert len(fields) >= 2, (
            f"found {len(fields)} non-hidden fields, expected the player and the "
            f"price at least — the field scan rotted, not the template"
        )
        assert len(glyphs) >= 2, (
            f"found {len(glyphs)} glyph buttons, expected the two price steppers "
            f"at least — the button scan rotted, not the template"
        )
        return [t for t in fields + glyphs if "aria-label=" not in t]

    def test_the_start_auction_form_names_every_control(self, client):
        panel = self._bid_panel(client.get("/").text)
        assert "Start Auction" in panel, "this is not the pre-auction branch"
        unnamed = self._unnamed(panel)
        assert not unnamed, f"controls with no accessible name: {unnamed}"

    def test_the_live_bid_form_names_every_control(self, client):
        name = pool_top(1)[0]
        r = client.post(
            "/bid-check", data={"player": name, "bidders": "", "price": "0.5"}
        )
        assert r.status_code == 200
        panel = self._bid_panel(r.text)
        assert "bid-advice" in panel, "this is not the live-advice branch"
        unnamed = self._unnamed(panel)
        assert not unnamed, f"controls with no accessible name: {unnamed}"

    def test_the_placeholder_is_not_the_name(self, client):
        """`placeholder` is not an accessible name, and it disappears on type.

        Kept on the Start Auction field because it is useful AS a placeholder —
        that field is free text and the hint is what stops a typo — but the
        entry filed it as the anti-pattern it is, so pin that the name no longer
        rides on it.
        """
        panel = self._bid_panel(client.get("/").text)
        field = re.search(r'<input[^>]*name="player"[^>]*>', panel)
        assert field and "placeholder=" in field.group(0)
        assert "aria-label=" in field.group(0), (
            f"the player field is still named by its placeholder: {field.group(0)}"
        )


class TestDoneTeamsAreNotProjectedForward:
    """A team that has stopped drafting must not be projected as if it hadn't.

    `_context` estimates every opponent's finished total as
    `current + unfilled_starter_slots × mean(points of the affordable top)`. For
    a DONE team there are no more picks, so every one of those points is
    invented — and the error is enormous rather than marginal, because a team
    that stopped early never spent its budget, so its `physical_max_bid` is still
    MAX_SALARY and the affordability filter hands it the best players in the pool.

    Measured 2026-08-13 against the `endgame-ceiling-binds` scenario, whose eight
    done teams read **+673 to +1101 points** above their real finals (SRL: 390
    actual, 1491 shown). It corrupts the rank badge specifically, which is the
    figure you read to know whether you are winning: BOT's real 1311 sat behind
    five phantom teams and the panel said #6 while BOT was first by a distance.

    Not an edge case — the design notes put 3+ early finishers in every draft, so
    this was wrong on draft day, in the second half, every time.
    """

    # Proj is read off its `proj-<CODE>` span rather than by counting cells: the
    # span is what GET /solve-standings swaps, so it is now the one place the
    # figure lives, and a positional regex broke the moment it was introduced.
    # Pts is still the bare cell immediately before it.
    _CELLS = re.compile(
        r"<td>(\d+)</td>\s*<td class=\"font-semibold[^\"]*\"><span id=\"proj-\w+\">(\d+)", re.S
    )

    def _proj(self, html: str, code: str) -> tuple[int, int]:
        """(Pts, Proj) for one team's League State row."""
        start = html.index(f"<strong>{code}</strong>")
        row = html[start:html.index("</tr>", start)]
        found = self._CELLS.search(row)
        assert found, f"could not read Pts/Proj out of {code}'s row: {row[:400]}"
        return int(found.group(1)), int(found.group(2))

    def test_marking_a_team_done_drops_its_projection_to_what_it_has(self, client):
        import main

        victim = next(
            c for c, t in main.auction_state.teams.items()
            if c != main.MY_TEAM and sum(t.roster_needs.values()) > 0
        )
        current, before = self._proj(client.get("/").text, victim)
        # Precondition, so this cannot pass by both numbers being equal already.
        assert before > current, (
            f"{victim} is projected {before} against {current} now — pick a team "
            f"with unfilled slots or this test proves nothing"
        )

        r = client.post("/team-done", data={"team_code": victim})
        assert r.status_code == 200, r.text

        current_after, after = self._proj(client.get("/").text, victim)
        assert after == current_after, (
            f"{victim} has stopped drafting but is still projected {after} "
            f"against {current_after} actual — {after - current_after} invented points"
        )
        assert after < before, "the projection did not move at all"

    def test_a_done_team_cannot_outrank_a_team_still_drafting(self, client):
        """The consequence, stated as the thing the operator actually reads."""
        import main

        victim = next(
            c for c, t in main.auction_state.teams.items()
            if c != main.MY_TEAM and sum(t.roster_needs.values()) > 0
        )
        client.post("/team-done", data={"team_code": victim})
        html = client.get("/").text
        _, theirs = self._proj(html, victim)
        _, mine = self._proj(html, main.MY_TEAM)
        assert theirs < mine, (
            f"{victim} stopped drafting on a part-built roster yet still projects "
            f"{theirs} against BOT's {mine}, so the rank badge reads backwards"
        )

    def test_marking_my_own_team_done_stops_projecting_my_purchases(self, client):
        """Why the `is_done` branch is ordered ahead of the MILP branch.

        Marking your own team done is a legal move in that table, and a MILP that
        keeps planning purchases you have sworn off is the same lie aimed at
        yourself.
        """
        import main

        current, before = self._proj(client.get("/").text, main.MY_TEAM)
        assert before > current, "BOT needs open slots for this to prove anything"

        client.post("/team-done", data={"team_code": main.MY_TEAM})
        current_after, after = self._proj(client.get("/").text, main.MY_TEAM)
        assert after == current_after, (
            f"BOT is done but still projects {after} against {current_after}"
        )


class TestTheLogsPanel:
    """The merged Auction / Transaction / Change log (2026-08-15).

    Replaced two separate cards. Three properties are worth pinning: every
    record stays visible, a team code that is not a team code never becomes a
    link, and the NHL club survives the player leaving every roster.
    """

    def _panel(self, client) -> str:
        return section_of(client.get("/").text, "logs-panel")

    def _tab_rows(self, panel: str) -> list[int]:
        """Body row counts, one per tab, in document order.

        Sliced on the three radio inputs rather than on `<tbody>`: an empty tab
        renders its empty-state paragraph and no table at all, so counting
        tables silently renumbers the tabs — which is how the first draft of
        this read the Change tab's count off the Transaction tab.
        """
        starts = [m.start() for m in re.finditer(r'<input type="radio" name="log-tab"', panel)]
        assert len(starts) == 3, f"expected 3 tabs, found {len(starts)}"
        bounds = starts + [len(panel)]
        counts = []
        for i, start in enumerate(starts):
            chunk = panel[start:bounds[i + 1]]
            body = re.search(r"<tbody>(.*?)</tbody>", chunk, re.S)
            counts.append(body.group(1).count("<tr>") if body else 0)
        return counts

    def test_every_transaction_lands_in_exactly_one_tab(self, client):
        """The split is a TOTAL partition, deliberately not an allowlist.

        `logs_panel.html` reads `draft` versus everything-else. An allowlist
        would be the CLAUDE.md rule for `transaction_type`, but that rule guards
        against a mis-routed value reaching `_view_team`; here a record matching
        no branch vanishes from the draft record entirely, which is the worse
        failure. So the tabs must sum to the whole log.

        Driven through three DIFFERENT writers, because they emit three
        different type strings and a partition that only ever sees `draft` is
        not being tested at all.
        """
        import main

        assign(client, pool_top()[0], main.MY_TEAM, 2.0)
        client.post("/buyout", data={"player": a_buyout_candidate().name})
        victim = main.auction_state.teams["SRL"].roster_players[0].name
        client.post("/trade-between", data={
            "team_a": "SRL", "team_b": "MAC",
            "players_from_a": victim, "players_from_b": "",
        })

        logged = len(main.auction_state.transaction_log)
        assert logged >= 3, f"only {logged} records; the setup did not take"

        auction, other, _change = self._tab_rows(self._panel(client))
        assert auction + other == logged, (
            f"tabs show {auction}+{other}={auction + other} of {logged} "
            f"transactions — {logged - auction - other} are invisible"
        )

    def test_a_drafted_players_team_opens_that_teams_panel(self, client):
        """The want this closes: notice a rival's pick, click through to them."""
        import main

        buyer = next(c for c in main.auction_state.teams if c != main.MY_TEAM)
        assign(client, pool_top()[0], buyer, 2.0)

        panel = self._panel(client)
        assert f'hx-get="/team-view/{buyer}"' in panel, (
            f"no /team-view link for {buyer} in the logs panel"
        )
        assert 'hx-target="#team-panel"' in panel

    def test_a_two_team_trades_arrow_code_is_never_a_link(self, client):
        """/trade-between logs `team_code` as f"{source}→{dest}".

        That is not a team code, so `_log_team_link.html` must render it as
        text. Unguarded, `teams[...]` raises outright — and if it were made
        forgiving instead, the row would offer a link to `/team-view/SRL→MAC`,
        which silently no-ops against `_view_team`'s validation and reads as a
        dead control.
        """
        import main

        victim = main.auction_state.teams["SRL"].roster_players[0].name
        r = client.post("/trade-between", data={
            "team_a": "SRL", "team_b": "MAC",
            "players_from_a": victim, "players_from_b": "",
        })
        assert toast_of(r).get("type") in ("success", "warning"), toast_of(r)

        panel = self._panel(client)
        assert "SRL→MAC" in panel, "the trade row is missing entirely"
        assert 'hx-get="/team-view/SRL→MAC"' not in panel
        # html.escape leaves → alone but a future encoder might not; assert on
        # the shape rather than the one spelling.
        assert not re.search(r'hx-get="/team-view/[^"]*(→|&#\d+;)', panel), (
            "a non-team code became a /team-view link"
        )

    def test_a_fresh_pick_carries_its_nhl_badge(self, client):
        """/assign must put the club on the record it writes.

        Separate from the buyout case below, which reads a club captured on a
        different code path (`bo_nhl_team`), and from the legacy-file tests in
        `test_crash_recovery.py`, where `_backfill_nhl_teams` refills it on the
        way in and so hides a writer that stopped passing it. Deleting
        `nhl_team=p.nhl_team` from /assign turned all three green — this is the
        one that catches it.
        """
        import main

        p = next(
            p for p in sorted(
                main.auction_state.available_players.values(),
                key=lambda p: -p.projected_points,
            )
            if p.nhl_team
        )
        assign(client, p.name, main.MY_TEAM, 2.0)

        record = next(
            t for t in main.auction_state.transaction_log if t.player_name == p.name)
        assert record.nhl_team == p.nhl_team, (
            f"the log recorded {record.nhl_team!r} for {p.name}, not {p.nhl_team!r}"
        )
        assert f'src="{main._nhl_logo_src(p.nhl_team)}"' in self._panel(client)

    def test_the_nhl_club_outlives_the_roster(self, client):
        """Why `nhl_team` is stored on the record instead of looked up.

        A bought-out player is on no roster AND gone from the pool, so resolving
        the club by name at render time draws nothing on exactly the rows the
        Transaction tab exists for. Buy one out, then require his badge.
        """
        import main

        victim = a_buyout_candidate()
        club = victim.nhl_team
        assert club, f"{victim.name} has no NHL club; pick a different fixture"

        r = client.post("/buyout", data={"player": victim.name})
        assert toast_of(r).get("type") in ("success", "warning"), toast_of(r)

        assert main.auction_state.teams[main.MY_TEAM].find_player(victim.name) is None
        assert victim.name not in main.auction_state.available_players, (
            "he is back in the pool, so a lookup would still work and this "
            "test cannot distinguish the two designs"
        )

        panel = self._panel(client)
        assert f'src="{main._nhl_logo_src(club)}"' in panel, (
            f"{victim.name}'s {club} badge is missing after the buyout"
        )


class TestOneLogoPathForTheWholeApp:
    """Every NHL badge resolves through `main._nhl_logo_src`, and only that.

    Six templates pasted `/nhl_logos/{{ raw }}.svg` by hand until 2026-09-10,
    which is how the asset could be named after the ALIAS (`UTH.svg`) while the
    canonical spelling a different pool uses (`UTA`) rendered 30 broken images.
    The path is now built in one Python function, so the render sites and the
    tests asserting on them cannot drift apart -- the `_dom_id` rule.
    """

    def test_the_alias_and_the_canonical_code_reach_the_same_file(self):
        import main

        canonical, alias = "UTA", "UTH"
        assert NHL_TEAM_ALIASES[alias] == canonical, (
            "this test is written against the Utah alias; update it with the map"
        )
        assert main._nhl_logo_src(alias) == main._nhl_logo_src(canonical)
        assert main._nhl_logo_src(canonical).endswith(f"/{canonical}.svg")

    def test_an_unknown_code_passes_through(self):
        """No silent fallback: a missing logo must stay visibly missing, which
        is what the data invariant and the pre-auction runbook are there to
        catch. A default would hide both."""
        import main

        assert main._nhl_logo_src("ZZZ") == "/nhl_logos/ZZZ.svg"

    def test_no_template_builds_the_path_itself(self):
        """The regression is one template at a time, so guard it statically.

        Scoped to an attribute so the macro's own explanatory comment -- which
        names the directory -- is not a false positive, the same construction
        `tests/test_offline_assets.py` uses for cross-origin URLs.
        """
        hand_rolled = {
            f.relative_to(REPO).as_posix(): m.group(0)
            for f in sorted((REPO / "templates").rglob("*.html"))
            for m in [re.search(r"""(?:src|href)\s*=\s*["'][^"']*nhl_logos""",
                                f.read_text())]
            if m
        }
        assert not hand_rolled, (
            "these templates build the logo path themselves instead of calling "
            f"the nhl_logo macro: {hand_rolled}"
        )

    def test_no_script_builds_the_path_either(self):
        """`loadTradeChoices` already builds player rows in JS, so that is where
        the next hand-rolled badge would go -- and the attribute regex above
        cannot see `el.src = '/nhl_logos/' + code`. A bare literal is the right
        test here: shortcuts.js has no legitimate reason to name the directory.
        """
        named = {
            f.relative_to(REPO).as_posix()
            for f in sorted((REPO / "static").glob("*.js"))
            if "nhl_logos" in f.read_text()
        }
        assert not named, (
            f"{named} names the logo directory; the path belongs to "
            "main._nhl_logo_src and the nhl_logo macro"
        )

    def test_a_player_with_no_club_gets_no_image(self, client):
        """3 rows of players-25.csv and 7 of players-23-converted.csv carry no
        NHL TEAM. Unguarded that rendered `/nhl_logos/.svg` -- a broken image
        and a 404 per row. Only `_log_nhl_logo.html` guarded it before the macro.
        """
        import main

        env = main.templates.env
        rendered = env.from_string(
            '{% from "macros/nhl.html" import nhl_logo %}[{{ nhl_logo(code) }}]'
        )
        assert rendered.render(code="") == "[]"
        assert rendered.render(code=None) == "[]"
        assert "<img" in rendered.render(code="BOS")

    def test_the_badge_labels_the_club_it_actually_drew(self, client):
        """`alt`/`title` resolve the alias, same as `src`.

        Until 2026-09-11 only the image did, so a badge pointing at UTA.svg
        announced itself as `UTH` -- a tricode no NHL club has -- on the pool
        the draft is actually run from.
        """
        import main

        env = main.templates.env
        rendered = env.from_string(
            '{% from "macros/nhl.html" import nhl_logo %}{{ nhl_logo(code) }}'
        )
        alias = next(iter(NHL_TEAM_ALIASES))
        canonical = NHL_TEAM_ALIASES[alias]
        html = rendered.render(code=alias)
        assert f'alt="{canonical}"' in html and f'title="{canonical}"' in html, (
            f"the badge for {alias} labels itself {alias}: {html}"
        )
        assert f'>{alias}<' not in html and f'"{alias}"' not in html

    def test_a_clubs_label_does_not_depend_on_which_pool_is_loaded(self):
        """The invariant, rather than a spelling check on one club.

        `players.csv` spells Utah `UTH` on 78 rows and `players-25.csv` spells
        it `UTA` on 30, so before the canonical filter the same club read two
        different ways depending on the file -- and the operator has no way to
        know which is "right" from the screen.

        Swept over every pool, so a refresh that introduces a new FCHL spelling
        fails here rather than on draft night.
        """
        import csv

        import main

        aliases = set(NHL_TEAM_ALIASES)
        assert aliases, "no aliases declared, so this sweep proves nothing"

        checked = 0
        for path in sorted((REPO / "data").glob("players*.csv")):
            with path.open(newline="", encoding="utf-8-sig") as fh:
                rows = list(csv.DictReader(fh))
            if not rows or "NHL TEAM" not in rows[0]:
                continue  # the legacy schema has no club column at all
            for row in rows:
                code = (row["NHL TEAM"] or "").strip()
                if not code:
                    continue
                checked += 1
                assert main._nhl_canonical(code) not in aliases, (
                    f"{path.name}: {code} canonicalises to another alias"
                )
                if code in aliases:
                    assert main._nhl_canonical(code) == NHL_TEAM_ALIASES[code]
        assert checked > 100, f"only {checked} club codes swept; the glob missed"

    def test_the_player_search_prints_the_canonical_code_too(self, client):
        """The one place in the app a club code is TEXT rather than an image.

        Derived by role -- a pool player whose club is an alias key -- because
        players.csv is replaced before every draft.
        """
        import main

        subject = next(
            (p for p in main.auction_state.available_players.values()
             if p.nhl_team in NHL_TEAM_ALIASES),
            None,
        )
        assert subject, (
            "no pool player carries an aliased club code, so this test cannot "
            "fail and must be re-derived"
        )
        canonical = NHL_TEAM_ALIASES[subject.nhl_team]
        html = client.get("/find-player", params={"q": subject.name}).text
        assert f"· {canonical}<" in html, (
            f"the search row for {subject.name} does not name {canonical}: "
            f"{html[:400]}"
        )
        assert f"· {subject.nhl_team}<" not in html


class TestFilterGroupsAreDistinguishable:
    """Two filter groups, and both start with a button reading "All".

    Unlabelled, the bar announces "All, F, D, G, All, RFA, UFA" and nothing
    tells the two Alls apart — the group name is the only thing that does.
    Same class of gap as the `&times;` close buttons and the League State done
    glyph, both labelled after a grill found them.
    """

    def _bar(self, client) -> str:
        html = section_of(client.get("/").text, "bid-limits")
        start = html.index("flex flex-wrap items-center")
        return html[start:html.index('<div id="player-chart-container"', start)]

    def test_both_groups_carry_a_name(self, client):
        bar = self._bar(client)
        assert 'aria-label="Filter by position"' in bar
        assert 'aria-label="Filter by contract status"' in bar

    def test_every_group_of_filter_buttons_is_labelled(self, client):
        """The rule, not the two instances — a third group must be named too.

        Written this way because the failure is silent: an unnamed group looks
        identical on screen and only a screen reader can tell.
        """
        bar = self._bar(client)
        groups = re.findall(r"<div[^>]*class=\"flex gap-1\"[^>]*>", bar)
        assert len(groups) >= 2, f"expected the two filter groups, found {groups}"
        unnamed = [g for g in groups if "aria-label=" not in g]
        assert not unnamed, (
            f"{len(unnamed)} filter group(s) have no accessible name: {unnamed}"
        )

    def test_the_ambiguity_is_real(self, client):
        """Guards the premise. If the duplicate "All" ever goes away, the two
        tests above stop protecting anything and should be reconsidered rather
        than left as decoration."""
        bar = self._bar(client)
        assert bar.count(">All</button>") == 2, (
            "the two groups no longer both start with All — re-read whether "
            "the group labels are still what disambiguates them"
        )


class TestEverySortableColumnCanActuallySort:
    """`sortTable` compares cell text, so a column with none is an inert control.

    Page-wide on purpose. The first version of this lived inside
    `TestTheLogsPanel` and checked only that panel — which is precisely how the
    bug it was written for survived: `bid_limits.html`'s NHL header had been
    inert since the column was added, in the table scanned most during a draft,
    and a logs-scoped guard could never see it. Measured 2026-08-15 at 0 of 705
    rows with text; clicking it left the row order byte-identical.

    It also reads EVERY body row, not the first. A column can be legitimately
    blank on row 1 and still sort: Available Players' RFA cell is empty for the
    683 non-RFA players and carries a prior team for the other 22, and grouping
    those 22 is the whole point of clicking it. "Row 1 is empty" would have
    condemned a working control.
    """

    def _sortable_columns(self, html: str):
        """(table_index, header, col, [cell html for every body row])."""
        found = []
        for n, table in enumerate(re.findall(r"<table.*?</table>", html, re.S)):
            head = re.search(r"<thead>(.*?)</thead>", table, re.S)
            # `<tbody[^>]*>`, not `<tbody>`: the pool table carries
            # id="pool-rows" and a literal match silently skipped it, taking
            # the only alt-fallback column on the page with it.
            body = re.search(r"<tbody[^>]*>(.*?)</tbody>", table, re.S)
            if not head or not body:
                continue
            rows = re.findall(r"<tr[^>]*>(.*?)</tr>", body.group(1), re.S)
            if not rows:
                continue
            for th in re.findall(r"<th[^>]*>.*?</th>", head.group(1), re.S):
                col = re.search(r'data-sort-col="(\d+)"', th)
                if not col or "sortTable" not in th:
                    continue
                i = int(col.group(1))
                cells = []
                for row in rows:
                    row_cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
                    if i < len(row_cells):
                        cells.append(row_cells[i])
                label = re.sub(r"<[^>]+>", "", th).strip()
                found.append((n, label, i, cells))
        return found

    def _sort_key(self, cell_html: str) -> str:
        """What `cellSortText` would return, including the img[alt] fallback."""
        text = re.sub(r"<[^>]+>", "", cell_html).strip()
        if text:
            return text
        alt = re.search(r'<img[^>]*\balt="([^"]*)"', cell_html)
        return alt.group(1).strip() if alt else ""

    def test_no_sortable_header_is_inert(self, client):
        """Every clickable header must have something to sort on somewhere."""
        import main

        assign(client, pool_top()[0], main.MY_TEAM, 2.0)
        client.post("/buyout", data={"player": a_buyout_candidate().name})

        columns = self._sortable_columns(client.get("/").text)
        assert len(columns) >= 15, (
            f"only found {len(columns)} sortable columns; the scraper is broken, "
            f"not the app"
        )
        inert = [
            (label, col, len(cells))
            for _n, label, col, cells in columns
            if not any(self._sort_key(c) for c in cells)
        ]
        assert not inert, (
            f"clickable headers with nothing to sort on: {inert} — sortTable "
            f"ties every row, so the click does nothing"
        )

    def test_the_alt_fallback_is_what_rescues_the_logo_columns(self, client):
        """Pins the mechanism, not just the outcome.

        Without this, deleting the `img[alt]` fallback from `cellSortText`
        leaves `test_no_sortable_header_is_inert` green only for as long as no
        logo column is sortable — and the fallback is the entire fix. Asserts
        that at least one sortable column depends on it.
        """
        columns = self._sortable_columns(client.get("/").text)
        rescued = [
            (label, col) for _n, label, col, cells in columns
            if not any(re.sub(r"<[^>]+>", "", c).strip() for c in cells)
            and any(self._sort_key(c) for c in cells)
        ]
        assert rescued, (
            "no column relies on the alt fallback, so nothing here would notice "
            "if cellSortText stopped applying it"
        )

    def test_the_javascript_actually_uses_the_fallback(self):
        """The tests above read HTML; the fallback lives in JS they never run.

        A guard against the pair passing while `sortTable` still reads
        `textContent` directly — which is the state the app shipped in.
        """
        js = open("static/shortcuts.js").read()
        assert "cellSortText" in js, "the helper is gone"
        assert "img[alt]" in js, "the fallback no longer reads alt"
        sort_body = js[js.index("function sortTable"):js.index("function renumberRows")]
        assert "cellSortText(a.cells[col])" in sort_body, (
            "sortTable stopped routing through cellSortText"
        )
        assert ".textContent" not in sort_body, (
            "sortTable reads textContent directly again, bypassing the fallback"
        )


class TestTheRfaBadgeAlwaysMarksTheRow:
    """An RFA with no prior team must still be visibly an RFA.

    The badge in the Available Players table carries `prior_fchl_team`, which
    is filled for 22 of 2158 rows in `players.csv` and for none at all in the
    two alternate pools — neither the 2023 legacy schema nor the 2025 Streamlit
    auto-save has a column for it. Rendered bare it produced an empty orange
    box on every RFA row, which reads as a broken template rather than as
    missing data, and made an RFA indistinguishable from a UFA in the one
    column that distinguishes them.

    Matters beyond cosmetics: a nomination turn is 1 RFA + 1 UFA, so telling
    them apart at a glance is what the column is for.
    """

    def _rfa_cell(self, client) -> str:
        panel = section_of(client.get("/").text, "bid-limits")
        m = re.findall(r'<span class="badge badge-warning badge-xs">([^<]*)</span>', panel)
        assert m, "no RFA badge rendered at all — the pool has no RFAs?"
        return m[0]

    def test_a_prior_team_is_shown_when_there_is_one(self, client):
        import main

        rfa = next(p for p in main.auction_state.available_players.values() if p.is_rfa)
        rfa.prior_fchl_team = "ZZZ"
        assert "ZZZ" in section_of(client.get("/").text, "bid-limits")

    def test_an_rfa_without_one_still_gets_a_badge(self, client):
        """The alternate-pool case. Break the fallback and this reads ''."""
        import main

        for p in main.auction_state.available_players.values():
            if p.is_rfa:
                p.prior_fchl_team = ""
        assert self._rfa_cell(client).strip() == "RFA"


class TestExactStandingsOnDemand:
    """`GET /solve-standings` replaces the Proj estimates with real MILP optima.

    The estimate in `_context` exists because 11 solves per action is
    unaffordable, and it cannot be cheaply improved — measured 2026-08-17, three
    budget-aware replacements were all WORSE (mean |err| 94 against 147/176/401),
    because greedy-by-points spends the budget on one star and floors the rest.
    So the only thing better than the estimate is the solve, and the solve has to
    be asked for.

    What it costs to get wrong is the rank badge. On the `endgame-ceiling-binds`
    scenario the estimate put BOT at #2 when BOT was #1 — the same class of error
    as the done-team projection bug fixed 2026-08-13, whose class docstring is a
    few hundred lines above this one.
    """

    def _figure(self, html: str, code: str) -> int:
        """The Proj figure out of a team's `proj-<CODE>` span.

        By id, not by cell position: the span is what the OOB swap targets, so
        reading it is reading the thing the endpoint actually writes.
        """
        found = re.search(rf'id="proj-{code}"[^>]*>\s*(\d+)', html)
        assert found, f"no proj-{code} span in the response"
        return int(found.group(1))

    def _basis(self, html: str) -> str:
        found = re.search(r'id="proj-basis"[^>]*>\s*([^<]*?)\s*<', html)
        assert found, "no proj-basis marker in the response"
        return found.group(1)

    def _live_opponents(self) -> list[str]:
        import main

        return [
            c for c, t in main.auction_state.teams.items()
            if not t.is_done and c != main.MY_TEAM
        ]

    def test_every_fragment_the_scan_returns_has_a_target(self, client):
        """The `htmx:oobErrorNoTarget` failure, which is silent on screen.

        htmx resolves an out-of-band target by selector into `querySelectorAll`
        from a loop with no try/catch, so one id that matches nothing costs the
        console a line and the operator nothing visible — the Scan button's
        original bug, where all 11 swaps missed on an opponent's panel.

        Every id carrying `hx-swap-oob` is collected, with NO assumption about
        what it is called. The first version of this matched `id="proj-..."` and
        so was blind to exactly the mutation it existed to catch: renaming the
        fragments to `projection-<CODE>` left it seeing only the basis marker,
        whose target was still fine, and it passed against 11 dead swaps.
        """
        import main

        page = section_of(client.get("/").text, "league-state")
        fragments = re.findall(
            r'id="([^"]+)"[^>]*hx-swap-oob', client.get("/solve-standings").text
        )
        assert fragments, "the scan returned no out-of-band fragments at all"
        missing = [f for f in fragments if f'id="{f}"' not in page]
        assert not missing, (
            f"{missing} are swapped by the scan but render nowhere in League "
            f"State, so those swaps silently do nothing"
        )
        # One per team plus the basis marker. A count as well as the resolution
        # check, because a fragment that stops being emitted resolves vacuously.
        expected = len(main.auction_state.nomination_order) + 1
        assert len(fragments) == expected, (
            f"the scan returned {len(fragments)} fragments against "
            f"{expected} (one per team in League State, plus the basis marker)"
        )

    def test_an_opponents_figure_becomes_its_milp_optimum(self, client):
        """The point of the exercise: the number on screen is the real optimum."""
        import main
        from optimizer import solve_optimal_roster

        code = self._live_opponents()[0]
        client.get("/solve-standings")
        truth = solve_optimal_roster(
            main.auction_state.teams[code],
            main.auction_state.available_players,
            main.market_prices,
        )
        assert truth.status == "Optimal", f"{code} cannot be solved; pick another"
        shown = self._figure(section_of(client.get("/").text, "league-state"), code)
        assert shown == int(truth.total_points), (
            f"{code} shows {shown} against a MILP optimum of "
            f"{int(truth.total_points)} — the scan's figure is not reaching the page"
        )

    def test_a_pick_returns_the_column_to_estimates(self, client):
        """The load-bearing one: an exact figure must not outlive its state.

        `_recompute()` empties `exact_projections` on every mutation, for the
        reason its docstring already gives about the caches beside it. Without
        that, the column keeps showing optima computed against a pool that has
        since lost players and a buyer whose budget has changed — and it says
        "exact" while doing it, on the figure that carries the rank badge.

        The team with the WIDEST estimate-to-exact gap is chosen deliberately: a
        single pick by someone else moves an estimate by a few points at most, so
        "the figure is no longer the exact one" cannot pass by coincidence.
        """
        import main

        before = section_of(client.get("/").text, "league-state")
        client.get("/solve-standings")
        after_scan = section_of(client.get("/").text, "league-state")
        code = max(
            self._live_opponents(),
            key=lambda c: abs(self._figure(after_scan, c) - self._figure(before, c)),
        )
        exact = self._figure(after_scan, code)
        assert exact != self._figure(before, code), (
            "no opponent's figure changed at all, so this test cannot tell the "
            "two bases apart — the scan is not doing anything"
        )

        assign(client, pool_top(1)[0], main.MY_TEAM, 1.0)
        page = section_of(client.get("/").text, "league-state")
        assert main.exact_projections == {}, (
            f"a pick left {len(main.exact_projections)} exact figures cached, so "
            f"the column now describes the state before the pick"
        )
        assert self._basis(page) == "estimated", (
            f"the marker still reads {self._basis(page)!r} after a pick"
        )
        assert self._figure(page, code) != exact, (
            f"{code} still shows its pre-pick exact figure {exact}"
        )

    def test_the_basis_marker_renders_in_both_states(self, client):
        """A target that disappears with its contents can only be swapped once.

        The `buyout_scan.html` bug exactly: the Scan button vanished on the way
        to an opponent's panel and never came back, because the wrapper was
        conditional rather than only its `hx-swap-oob` attribute.
        """
        page = section_of(client.get("/").text, "league-state")
        assert self._basis(page) == "estimated"

        client.get("/solve-standings")
        after = section_of(client.get("/").text, "league-state")
        assert self._basis(after) == "solved", (
            f"after a clean scan the marker reads {self._basis(after)!r}. No "
            f"count in this state: it carries information only when the column "
            f"is mixed, and 'N/N' is worse than nothing when nothing is a guess"
        )

    def test_the_marker_explains_itself_in_every_state(self, client):
        """The label is one word because a second costs 53px of the widest
        table in the app (measured 2026-09-10), so the `title` is where the
        meaning lives — and it is the half that was missing when this was
        removed on 2026-09-09 for being unintelligible.

        Both states, because a tooltip on only the alarming one leaves the
        resting state as the bare adjective it was. The estimated state also
        has to name the control, since knowing the figures are guesses is no
        use without knowing what to press.
        """
        def title(html: str) -> str:
            found = re.search(r'id="proj-basis"[^>]*\stitle="([^"]*)"', html)
            assert found, "the basis marker carries no title at all"
            return found.group(1)

        page = section_of(client.get("/").text, "league-state")
        assert self._basis(page) == "estimated", "precondition"
        estimated = title(page)
        assert "Solve Standings" in estimated, (
            f"the estimated state does not name the button that fixes it: "
            f"{estimated!r}"
        )

        client.get("/solve-standings")
        after = section_of(client.get("/").text, "league-state")
        assert self._basis(after) == "solved", "precondition"
        solved = title(after)
        assert solved and solved != estimated, (
            "the solved state reuses the estimated state's tooltip"
        )
        # Both must say the marker is about the OPPONENTS' figures. BOT's own
        # Proj is a real optimum in every state and the label never covers it —
        # which is the thing a bare adjective over a mixed column gets wrong.
        for state, text in (("estimated", estimated), ("solved", solved)):
            assert "pponent" in text, (
                f"the {state} tooltip does not say whose figures it describes: "
                f"{text!r}"
            )

    def test_the_header_and_the_marker_do_not_say_the_same_thing(self, client):
        """They sit in the SAME `<th>`, and one of them is state-aware.

        Until 2026-09-11 the `Proj` header carried a 375-char bubble that
        restated the marker's own title at greater length and less accurately:
        the marker branches three ways while the header said "estimates"
        unconditionally, including in the state where every figure on screen is
        a real solve. The split now is that the marker owns the BASIS and the
        header owns what the column MEASURES.

        Keyed on "Solve Standings", which is the marker's job to name — it is
        the control that changes the basis, and the reason the marker stopped
        saying `exact` on 2026-09-10 was that no word in it pointed at a
        button. If that phrase ever reappears in the header, the two have
        re-converged and the state-aware one is no longer the only answer.
        """
        page = section_of(client.get("/").text, "league-state")
        header = re.search(r'<span title="([^"]*)">Proj</span>', page)
        assert header, "the Proj header carries no explanation at all"
        header_text = header.group(1)

        assert "Solve Standings" not in header_text, (
            f"the header tooltip names the button again, which is the marker's "
            f"job and cannot be said correctly by a static string: {header_text!r}"
        )
        assert "12F/6D/2G" in header_text, (
            f"the header no longer says what the column measures — the one "
            f"thing the marker below it cannot say: {header_text!r}"
        )
        marker = re.search(r'id="proj-basis"[^>]*\stitle="([^"]*)"', page)
        assert marker and "Solve Standings" in marker.group(1), (
            "the basis marker stopped naming the button, so nothing does"
        )

    def test_the_header_explanation_is_not_a_daisyui_bubble(self, client):
        """A bubble cannot be readable in this cell, and that is measured.

        `Proj` is a `th` inside `.table-scroll-x`, whose VISIBLE width is 293px
        at 1024 and 379px at 1280. DaisyUI centres a bubble on its trigger and
        has no flip logic, so sweeping every scroll position at which the header
        is fully visible put a 270px bubble outside the visible box at 5 of 6 of
        them at 1280 (worst 110px), 4 of 5 at 1024 and 6 of 7 at 928. Narrowing
        does not rescue it — an 80px bubble, too narrow for a sentence, is still
        clipped at ~20% of hover positions — and re-anchoring only moves the
        failure to the other edge.

        Cheap and static because the browser suite structurally cannot catch a
        regression here: `TestTooltipsStayInsideTheirPanel` measures at
        `scrollLeft: 0`, where this header is not on screen, which is how the
        375-char bubble passed it for a month.
        """
        page = section_of(client.get("/").text, "league-state")
        # Scoped to the SCROLLER, not to the panel. The measured problem is a
        # bubble anchored inside `.table-scroll-x`; the panel header above it —
        # where Solve Standings lives — is outside that box and a bubble there
        # is fine. An assertion over the whole section would fail a legitimate
        # change while giving a reason that does not apply to it.
        start = page.index('class="table-scroll-x"')
        scroller = page[start:page.index("</table>", start)]
        assert "data-tip" not in scroller, (
            "a DaisyUI bubble is back inside the league TABLE, which scrolls "
            "horizontally and is too narrow for one at every width measured. "
            "The browser tooltip suite measures unscrolled, so nothing else "
            "will catch this"
        )

    def test_the_scan_solves_live_opponents_only(self, monkeypatch, client):
        """Done teams and BOT are skipped, and neither is an optimization.

        A done team's roster is FINAL — solving it invents purchases it has sworn
        off, which is the 2026-08-13 bug worth +673 to +1101 points a team. BOT's
        figure is already `milp_solution.total_points` from the same solve over
        the same inputs.

        Patched on `main`, not on `optimizer`: main.py does `from optimizer
        import solve_optimal_roster`, so the name is bound in main's namespace and
        patching the optimizer module misses every call (see
        `tests/test_counterfactual_cache.py`, which documents the same trap).
        """
        import main
        from optimizer import solve_optimal_roster as real_solve

        calls = []

        def counting(team, *a, **kw):
            calls.append(team.code)
            return real_solve(team, *a, **kw)

        monkeypatch.setattr(main, "solve_optimal_roster", counting)
        client.post("/team-done", data={"team_code": self._live_opponents()[-1]})
        calls.clear()

        client.get("/solve-standings")
        expected = self._live_opponents()
        assert calls, "the counter saw no solves at all, so it proves nothing"
        assert sorted(calls) == sorted(expected), (
            f"the scan solved {sorted(calls)} against the live opponents "
            f"{sorted(expected)} — a done team or BOT was solved, or one was missed"
        )

    def test_a_done_teams_figure_is_untouched_by_the_scan(self, client):
        """Its roster is final, so there is nothing for a solve to improve."""
        import main

        code = self._live_opponents()[-1]
        client.post("/team-done", data={"team_code": code})
        before = self._figure(section_of(client.get("/").text, "league-state"), code)

        client.get("/solve-standings")
        after = self._figure(section_of(client.get("/").text, "league-state"), code)
        assert after == before == main.auction_state.teams[code].current_roster_points, (
            f"done team {code} went {before} -> {after} across a scan; a finished "
            f"roster projects what it has"
        )

    def test_an_unsolvable_opponent_keeps_its_estimate_rather_than_reading_zero(
        self, client
    ):
        """A solver failure must not be reported as a team with no points.

        Absence from `exact_projections` is what makes this safe — the team falls
        through to the estimate. Storing a zero would put a plausible-looking
        last place on the board, and the marker's count is what tells you one
        cell is still a guess.
        """
        import main

        code = self._live_opponents()[0]
        squeeze(code, 1.0)  # $1.0M against a dozen unfilled spots: no legal roster
        estimate = self._figure(section_of(client.get("/").text, "league-state"), code)

        r = client.get("/solve-standings")
        assert r.status_code == 200, "an unsolvable opponent broke the whole scan"
        assert code not in main.exact_projections, (
            f"{code} has no feasible roster but was cached as exact"
        )
        page = section_of(client.get("/").text, "league-state")
        assert self._figure(page, code) == estimate, (
            f"{code} moved to {self._figure(page, code)} from its {estimate} "
            f"estimate despite having no solution"
        )
        n = len(self._live_opponents())
        assert self._basis(page) == f"{n - 1}/{n} solved", (
            f"the marker reads {self._basis(page)!r} while one cell is still an "
            f"estimate — a count exists precisely so this case is visible"
        )

    def _badge(self, html: str, code: str) -> int | None:
        """BOT's rank badge, read out of the same span as its figure.

        Inside the figure's span deliberately: the badge has to travel WITH the
        number, so reading it from anywhere else would not notice if it stopped.
        """
        span = re.search(rf'id="proj-{code}">(.*?)</span></td>|id="proj-{code}">(.*?)$',
                         html, re.S)
        assert span, f"no proj-{code} span"
        found = re.search(r"#(\d+)", span.group(1) or span.group(2) or "")
        return int(found.group(1)) if found else None

    def test_the_rank_badge_agrees_with_the_figures_beside_it(self, client):
        """The badge is the reason this feature exists, and nothing covered it.

        Measured 2026-08-18: deleting the badge from `macros/standings.html`
        passed all twelve tests in this class. A scan changes every team's rank,
        so a fragment that swaps the number without the badge leaves an
        authoritative "#2" describing figures that are no longer on screen —
        which is the original bug wearing a different hat.

        Stated as an invariant over what is RENDERED rather than against an
        expected number: BOT's badge must be BOT's position among the figures in
        the table. True in every state, so it needs no fixture and cannot rot
        when `players.csv` is replaced.
        """
        import main

        for stage in ("before the scan", "after the scan"):
            if stage == "after the scan":
                client.get("/solve-standings")
            page = section_of(client.get("/").text, "league-state")
            figures = {c: int(v) for c, v in
                       re.findall(r'id="proj-([A-Z]{3})">\s*(\d+)', page)}
            assert len(figures) == len(main.auction_state.nomination_order)
            mine = figures[main.MY_TEAM]
            expected = sorted(figures.values(), reverse=True).index(mine) + 1
            assert self._badge(page, main.MY_TEAM) == expected, (
                f"{stage}: the badge reads #{self._badge(page, main.MY_TEAM)} "
                f"while BOT's {mine} is {expected}th of "
                f"{sorted(figures.values(), reverse=True)}"
            )

    def test_the_scans_own_fragment_carries_the_badge(self, client):
        """Read the SWAP PAYLOAD, not the page rendered after it.

        The two are not the same evidence and this is the gap that proved it. A
        mutant that emitted the badge inline but omitted it from the out-of-band
        fragment passed every other test here, because they re-fetch `GET /` and
        get a fresh inline render. A browser does not: htmx replaces the span
        with what the response contains, so the badge would simply disappear from
        the live DOM until some later full-panel swap put it back.

        So this asserts the fragment BOT will actually receive, and that its rank
        agrees with the figures in the same response.
        """
        import main

        body = client.get("/solve-standings").text
        figures = {c: int(v) for c, v in
                   re.findall(r'id="proj-([A-Z]{3})"[^>]*>\s*(\d+)', body)}
        assert len(figures) == len(main.auction_state.nomination_order), (
            f"the response carries {len(figures)} figures"
        )
        mine = figures[main.MY_TEAM]
        expected = sorted(figures.values(), reverse=True).index(mine) + 1
        badge = re.search(rf'id="proj-{main.MY_TEAM}"[^>]*>.*?#(\d+)', body, re.S)
        assert badge, (
            "the scan's fragment for BOT carries no rank badge, so the swap "
            "would remove it from the page it is meant to update"
        )
        assert int(badge.group(1)) == expected, (
            f"the fragment says #{badge.group(1)} while BOT's {mine} is "
            f"{expected}th of the figures in the same response"
        )

    def test_on_the_endgame_scenario_the_estimate_flatters_an_opponent(self, client):
        """The mechanism the feature exists for, on the pinned state.

        `endgame-ceiling-binds` is a pinned scenario whose shape
        `tests/test_scenarios.py` guards (BOT plus exactly two live opponents).

        This asserted `after < before` on BOT's rank BADGE until 2026-09-10 —
        measured 2026-08-17, the badge read #2 while BOT was #1 because the
        estimate handed a live opponent 146 points it could not reach. The
        overstatement is still here and still 128 points, but the price refit
        moved every team's total and it no longer happens to cross BOT, so the
        rank flip was the incidental half. A gap between two teams' point
        totals is as much a data fingerprint as a literal rank; what is NOT
        incidental is that the estimate is above the achievable optimum and the
        scan corrects it downward. That is what this pins, plus BOT's own
        figure holding still — which is the half that says the column really is
        two different rules. `test_the_scan_changes_the_standings` carries the
        rank claim, on the state where it reproduces.
        """
        import main

        client.post("/load-scenario", data={"name": "endgame-ceiling-binds"})
        opponents = self._live_opponents()
        assert opponents, "the scenario has no live opponent to solve for"

        page = section_of(client.get("/").text, "league-state")
        before = {c: self._figure(page, c) for c in opponents + [main.MY_TEAM]}
        client.get("/solve-standings")
        page = section_of(client.get("/").text, "league-state")
        after = {c: self._figure(page, c) for c in opponents + [main.MY_TEAM]}

        flattered = {c: before[c] - after[c] for c in opponents
                     if after[c] < before[c]}
        assert flattered, (
            f"no live opponent's figure fell when solved exactly "
            f"({ {c: (before[c], after[c]) for c in opponents} }) — the estimate "
            f"is supposed to overstate the achievable optimum, and if it no "
            f"longer does on this scenario the scan is pinned by nothing here"
        )
        assert after[main.MY_TEAM] == before[main.MY_TEAM], (
            f"BOT's Proj moved {before[main.MY_TEAM]} -> {after[main.MY_TEAM]} "
            f"across the scan; BOT's figure is `milp_solution.total_points` in "
            f"both states and the scan must not touch it"
        )

    def test_the_scan_changes_the_standings(self, client):
        """Solving exactly must actually move somebody, or the button is decor.

        On a fresh league the estimate is worst — measured 2026-09-10 it runs
        +95.5 mean / +220 worst against the true optima and overstates for 8 of
        10 opponents — so this is where the rank claim reproduces. Asserted as
        "some badge moves", not as a particular team's rank: which teams swap
        is a `players.csv` fingerprint, that any swap happens at all is the
        feature.
        """
        import main

        codes = list(main.auction_state.teams)
        page = section_of(client.get("/").text, "league-state")
        before = {c: self._badge(page, c) for c in codes}
        client.get("/solve-standings")
        page = section_of(client.get("/").text, "league-state")
        after = {c: self._badge(page, c) for c in codes}

        moved = {c: (before[c], after[c]) for c in codes if before[c] != after[c]}
        assert moved, (
            "no team's rank badge moved across the scan, so replacing every "
            "estimate with a real MILP optimum changed nothing on screen"
        )
        assert main.exact_projections, "the scan solved nobody on this scenario"

    def test_a_solver_that_raises_is_logged_and_costs_only_that_team(
        self, monkeypatch, caplog, client
    ):
        """The `except Exception` path, which no other test reaches.

        `test_an_unsolvable_opponent...` covers a solve that returns non-Optimal;
        this covers one that blows up. Broad-catching it is right — a solver
        failing on one opponent must not cost the other nine — but a silent skip
        is not, because the marker can say `exact 9/10` and never which team or
        why. So the log line IS the error handling, and it is asserted.
        """
        import logging as _logging

        import main
        from optimizer import solve_optimal_roster as real_solve

        victim = self._live_opponents()[0]
        estimate = self._figure(section_of(client.get("/").text, "league-state"), victim)

        def exploding(team, *a, **kw):
            if team.code == victim:
                raise RuntimeError("CBC fell over")
            return real_solve(team, *a, **kw)

        monkeypatch.setattr(main, "solve_optimal_roster", exploding)
        with caplog.at_level(_logging.WARNING):
            assert client.get("/solve-standings").status_code == 200

        assert victim in caplog.text and "CBC fell over" in caplog.text, (
            f"the failure was swallowed without naming the team or the cause: "
            f"{caplog.text!r}"
        )
        others = [c for c in self._live_opponents() if c != victim]
        assert all(c in main.exact_projections for c in others), (
            f"one team's exception cost the others: solved "
            f"{sorted(main.exact_projections)} of {sorted(self._live_opponents())}"
        )
        page = section_of(client.get("/").text, "league-state")
        assert self._figure(page, victim) == estimate, (
            f"{victim} moved off its estimate despite never being solved"
        )

    def test_with_every_opponent_done_the_column_is_exact_already(self, client):
        """With nobody left to solve, every figure on screen is already exact.

        A done team projects its final roster and BOT projects its MILP optimum,
        so the column is exact BY CONSTRUCTION — and both halves of that are
        asserted here rather than assumed.

        Reachable: the design notes put 3+ early finishers in every draft and
        `endgame-ceiling-binds` already has 8 of 10 done. This is the state that
        caught a basis label branching on the wrong count, and it stays worth
        testing without one: the button must still perform zero solves and
        leave every figure where it was.
        """
        import main
        from optimizer import solve_optimal_roster

        for code in [c for c in main.auction_state.teams if c != main.MY_TEAM]:
            client.post("/team-done", data={"team_code": code})
        assert not self._live_opponents(), "setup failed to retire every opponent"

        page = section_of(client.get("/").text, "league-state")
        for code, team in main.auction_state.teams.items():
            if team.is_done:
                assert self._figure(page, code) == team.current_roster_points, (
                    f"done team {code} is not showing its final roster, so "
                    f"'exact' would be the wrong label for a different reason"
                )
        bot = main.auction_state.teams[main.MY_TEAM]
        sol = solve_optimal_roster(bot, main.auction_state.available_players,
                                   main.market_prices)
        assert self._figure(page, main.MY_TEAM) == int(sol.total_points)

        # And the button cannot change any of it, so it must not claim to have.
        client.get("/solve-standings")
        after = section_of(client.get("/").text, "league-state")
        assert main.exact_projections == {}, "there was nothing to solve"
        for code, team in main.auction_state.teams.items():
            if team.is_done:
                assert self._figure(after, code) == team.current_roster_points, (
                    f"the button performed zero solves, so done team {code}'s "
                    f"figure must be exactly where it was"
                )


class TestFindingAPlayerAnywhere:
    """`GET /find-player` — the header search.

    The engine's five locations and its ranking are pinned in
    `tests/test_player_search.py`; these are about the RESPONSE — what reaches
    the screen, where a click goes, and the two things about this endpoint
    that are easy to regress silently.
    """

    def _find(self, client, query: str) -> str:
        return client.get("/find-player", params={"q": query}).text

    def _surname(self, name: str) -> str:
        """The last ALPHABETIC token. `_disambiguated_names` appends " (DAL)"
        and " (#2)", so `split()[-1]` is not reliably a surname — it folds to
        the suffix and matches for the wrong reason."""
        words = [w for w in name.split() if w.isalpha()] or [name]
        return words[-1][:4]

    def _row(self, html_text: str, name: str) -> str:
        """The one `.search-row` naming this player."""
        rows = re.findall(r'<div class="search-row"[^>]*>.*?(?=<div class="search-row"|</div>\s*$)',
                          html_text, re.S)
        found = [r for r in rows if html.unescape(name) in html.unescape(r)]
        assert found, f"no search row for {name} in:\n{html_text[:600]}"
        return found[0]

    def test_an_available_player_says_so_and_opens_his_chart(self, client):
        name = pool_top(1)[0]
        row = self._row(self._find(client, self._surname(name)), name)
        assert "available" in row
        assert "/player-chart/" in row, "a pool hit must open the price chart"
        assert "/team-view/" not in row, "a pool player has no holder to open"

    def test_an_owned_player_opens_his_holder_and_not_the_whole_page(self, client):
        """`/team-view` rather than a full render is what keeps a live bidding
        session alive — it swaps `#team-panel` and never touches `#bid-panel`.
        """
        code = next(c for c in main_state().teams if c != MY_TEAM)
        player = a_roster_player(code)
        row = self._row(self._find(client, self._surname(player.name)), player.name)
        assert f'hx-get="/team-view/{code}"' in row
        assert 'hx-target="#team-panel"' in row
        assert "all_panels" not in row and 'hx-target="#app"' not in row

    def test_a_minor_off_cap_is_marked_and_one_on_cap_is_not(self, client):
        """Both branches, supplied — the live pool's cap-counting minors are
        all one group, so a hard-coded answer would look right on it."""
        from state import PlayerOnRoster

        team = main_state().teams[MY_TEAM]
        shared = dict(position="F", salary=1.0, projected_points=10, is_minor=True)
        team.minor_players.append(PlayerOnRoster(name="Zzq Oncap Minor", group="3", **shared))
        team.minor_players.append(PlayerOnRoster(name="Zzq Offcap Minor", group="5", **shared))
        team._invalidate_cache()

        page = self._find(client, "Zzq")
        assert "off cap" in self._row(page, "Zzq Offcap Minor")
        assert "off cap" not in self._row(page, "Zzq Oncap Minor"), (
            "a group 3 minor's salary is fully on the cap and must not be "
            "marked as free"
        )

    def test_a_bought_out_player_shows_the_penalty_and_links_nowhere(self, client):
        from config import BUYOUT_PENALTY_RATE

        victim = a_buyout_candidate()
        expected = victim.salary * BUYOUT_PENALTY_RATE
        client.post("/buyout", data={"player": victim.name})

        row = self._row(self._find(client, self._surname(victim.name)), victim.name)
        assert "bought out" in row
        assert f"${expected:.1f}M penalty" in row, (
            f"the row does not name the ${expected:.1f}M still on the cap: {row}"
        )
        assert "hx-get" not in row, "he is on no roster; there is nothing to open"

    def test_a_drafted_player_shows_what_he_went_for(self, client):
        """The commonest provenance of all, and the one the trade test does
        not reach — measured, deleting this branch survived the class."""
        name = pool_top(1)[0]
        code = next(c for c in main_state().teams if c != MY_TEAM)
        assign(client, name, code, 3.7)

        row = self._row(self._find(client, self._surname(name)), name)
        assert f"drafted by {code} for $3.7M" in row, (
            f"the row does not say what he went for: {row}"
        )

    def test_a_trade_between_two_teams_renders_as_text_not_a_link(self, client):
        """`/trade-between` logs `team_code` as f"{source}→{dest}". It reaches
        the row as provenance, and an unguarded renderer would point a link at
        `/team-view/SRL→MAC`."""
        from state import TransactionRecord

        state = main_state()
        code = next(c for c in state.teams if c != MY_TEAM)
        player = a_roster_player(code)
        state.transaction_log.append(TransactionRecord(
            player_name=player.name, position=player.position,
            team_code=f"{code}→{MY_TEAM}", salary=player.salary,
            model_price=1.0, market_price=1.0, timestamp="now",
            transaction_type="trade",
        ))
        row = self._row(self._find(client, self._surname(player.name)), player.name)
        assert html.unescape(f"traded {code}→{MY_TEAM}") in html.unescape(row)
        assert f"/team-view/{code}→{MY_TEAM}" not in html.unescape(row), (
            "the arrow form was rendered as a link target"
        )

    def test_it_says_how_many_it_did_not_show(self, client):
        """Silent truncation on a common surname reads as "not in the league"."""
        page = self._find(client, "son")
        assert "more — keep typing" in page

    def test_a_query_that_matches_nothing_says_so(self, client):
        page = self._find(client, "zzzznotaplayer")
        assert "No player matching" in page

    def test_a_query_too_short_renders_nothing_at_all(self, client):
        """The dropdown is this fragment; an empty body is how it stays shut."""
        assert self._find(client, "m").strip() == ""
        assert self._find(client, "").strip() == ""

    def test_the_response_carries_no_ids(self, client):
        """`_dom_id` mints one id per player and `team_panel.html` owns it for
        the buyout dots. A second copy here is a duplicate id the roster scan
        swaps twice."""
        page = self._find(client, pool_top(1)[0][:4])
        assert 'id="' not in page

    def test_a_blank_query_answers_200_with_an_empty_body(self, client):
        """200, never 204. htmx reads `204 No Content` as "do not swap", so a
        204 here would leave the previous results on screen after the box was
        cleared — the one state in which the list is guaranteed to be wrong.
        """
        r = client.get("/find-player", params={"q": ""})
        assert r.status_code == 200, "204 tells htmx to leave stale results up"
        assert r.text.strip() == "", f"a blank query rendered {r.text[:200]!r}"

    def test_the_input_and_its_mount_live_outside_app(self, client):
        """Both in the navbar, and the navbar is outside `#app`.

        Every panel swap replaces `#app`'s innerHTML, so a mount inside it
        would be destroyed by the first pick and every later search would swap
        into nothing. Same rule as the startup banner and the shortcuts
        dialog; ordering is how a response-level test can see it.
        """
        page = client.get("/").text
        app_at = page.index('id="app"')
        assert page.index('id="player-search"') < app_at
        assert page.index('id="player-search-results"') < app_at
        assert 'hx-get="/find-player"' in page, "nothing fires the search"
        assert 'hx-target="#player-search-results"' in page

    def test_it_does_not_build_the_whole_page_context(self, client, monkeypatch):
        """The reason this endpoint bypasses `_context`: it fires on every
        keystroke, and `_context` assembles a 704-row `bid_limits` list plus
        the per-team projections regardless of what is rendered — ~8.5ms
        against 0.33ms of actual work. Trivially reintroduced by a later
        `ctx = _context(request)`, and nothing else would notice."""
        import main

        def explode(*a, **k):
            raise AssertionError("/find-player built the full page context")

        monkeypatch.setattr(main, "_context", explode)
        r = client.get("/find-player", params={"q": pool_top(1)[0][:4]})
        assert r.status_code == 200
        assert "search-row" in r.text


class TestTheStandingsResolveThemselvesAfterAPick:
    """`_recompute()` empties `exact_projections`, so one pick returns all ten
    opponents' Proj figures to an estimate that runs +68 mean / +193 worst and
    moves 9 of 10 teams in rank order. Owner decision 2026-09-10: re-solve
    after every pick rather than leave the column degraded until somebody reads
    the marker.

    These tests can see the header and the contract; only the browser test can
    see the scan actually happen, because the trigger is JS.
    """

    def _settle_events(self, response) -> dict:
        header = response.headers.get("HX-Trigger-After-Settle")
        return json.loads(header) if header else {}

    def test_a_pick_asks_for_a_re_solve(self, client):
        import main

        target = pool_top()[0]

        r = assign(client, target, MY_TEAM, 1.0)

        assert self._settle_events(r).get(main.SOLVE_STANDINGS_EVENT) is True

    def test_the_toast_still_rides_the_plain_header(self, client):
        """Two headers, two purposes. `HX-Trigger` fires BEFORE the swap, which
        is right for a toast and wrong for a scan whose out-of-band targets the
        swap is about to replace."""
        r = assign(client, pool_top()[0], MY_TEAM, 1.0)

        assert toast_of(r), "the toast moved off HX-Trigger"
        assert "showToast" not in r.headers.get("HX-Trigger-After-Settle", "")

    def test_a_rejected_pick_asks_for_nothing(self, client):
        """A refused assign is not a draft action and must not spend ten MILP
        solves. `/assign` answers 200 when it rejects, so the status code
        proves nothing here — the toast type is what separates them."""
        r = client.post("/assign", data={
            "player": pool_top()[0], "team": "NOPE", "salary": 1.0,
        })

        assert r.status_code == 200
        assert toast_of(r).get("type") == "error", "this was supposed to reject"
        assert self._settle_events(r) == {}

    def test_no_other_mutation_asks_for_one(self, client):
        """Deliberately narrow. Every mutation invalidates the column, but the
        owner asked for it on a PICK, and the cost is ~384ms of up to 8 CBC
        subprocesses each time."""
        import main

        bot = main.auction_state.teams[MY_TEAM]
        victim = bot.roster_players[0].name

        quiet = [
            client.post("/toggle-bench",
                        data={"team_code": MY_TEAM, "player_name": victim}),
            client.post("/team-done", data={"team_code": "SRL"}),
            client.post("/adjust-salary", data={
                "team_code": MY_TEAM, "player_name": victim, "new_salary": 1.1,
            }),
        ]

        for r in quiet:
            assert self._settle_events(r) == {}, (
                "a non-pick mutation is firing a ten-solve scan"
            )

    def test_the_event_name_is_the_same_string_in_both_files(self):
        """A cross-file contract with no import between the two sides, so a
        typo on either is completely silent — the header fires an event nothing
        listens for, the column quietly stays on estimates, and the only symptom
        is a marker the operator was already ignoring. Same reasoning as
        `TestShortcutsModal`, which pins the shortcut sets in both directions.
        """
        from pathlib import Path

        import main

        js = (Path(main.__file__).resolve().parent / "static"
              / "shortcuts.js").read_text()

        assert f"'{main.SOLVE_STANDINGS_EVENT}'" in js, (
            f"nothing in shortcuts.js listens for "
            f"'{main.SOLVE_STANDINGS_EVENT}'"
        )
        assert "'/solve-standings'" in js, (
            "the listener exists but no longer calls the scan"
        )

    def test_the_manual_button_is_still_there(self, client):
        """The auto-scan is a convenience on top, not a replacement: it fires
        only on a pick, and every other mutation still degrades the column."""
        assert 'hx-get="/solve-standings"' in section_of(
            client.get("/").text, "league-state"
        )


class TestNominationIsNeverGatedByATurn:
    """The tool stopped tracking a nomination pointer on 2026-09-11.

    It gated exactly one thing — the visibility of the Get Recommendations
    button — and gated nothing the engine computes: `recommend_nomination` is
    hard-coded to MY_TEAM and reads no pointer, `/nominate` never checked one,
    and the `n` shortcut fired the endpoint out of turn regardless. So the gate
    only hid the button that documents a request the keyboard could already
    make. These pin the removal in the three places it could creep back.
    """

    # Page-wide rather than sliced: `section_of` wants a <section> and the
    # nomination panel is a <div>, and the attribute form `hx-get="/nominate"`
    # appears in exactly one place in the rendered app — the button itself.
    BUTTON = 'hx-get="/nominate"'

    def test_the_button_is_offered_on_a_fresh_league(self, client):
        assert self.BUTTON in client.get("/").text

    def test_the_button_survives_every_offset_in_the_order(self, client):
        """The case the gate used to break, asserted at EVERY phase.

        The old pointer advanced on each UFA sale and the button showed only
        while it pointed at BOT, so it was hidden for ten sales in eleven. One
        checkpoint is not enough to pin that: a round-robin over an eleven-team
        order returns to BOT after exactly eleven picks, so a single assertion
        taken there passes against a fully restored gate — measured, a mutant
        doing precisely that survived. Walking one lap PLUS one lands on every
        offset, so no phase of any round-robin can dodge it.

        Sales go to an OPPONENT, so nothing BOT owns moves and the gate is the
        only thing under test.
        """
        order = main_state().nomination_order
        buyer = next(c for c in order if c != "BOT")
        for pick, name in enumerate(pool_top(len(order) + 1), start=1):
            assign(client, name, buyer, 0.5)
            assert self.BUTTON in client.get("/").text, (
                f"the button disappeared after pick {pick}"
            )

    def test_the_recommendations_themselves_come_back_out_of_turn(self, client):
        """Not just the button: the endpoint answers with real picks."""
        order = main_state().nomination_order
        buyer = next(c for c in order if c != "BOT")
        for name in pool_top(len(order) - 1):
            assign(client, name, buyer, 0.5)

        r = client.get("/nominate")
        assert r.status_code == 200
        assert "UFA Pick" in r.text

    def test_the_clear_control_appears_only_with_something_to_clear(self, client):
        """Both halves, and the ABSENCE is the one that can fail.

        An x beside "Auction" with no cards under it is a control that does
        nothing, which mid-draft reads as a broken button — so it is gated on
        the same `is defined and` pair the cards themselves use. A test that
        only asserted its presence would pass against an ungated one.
        """
        assert 'id="nomination-clear"' not in client.get("/").text, (
            "the clear control renders on a fresh league, where there are no "
            "recommendations to clear"
        )
        r = client.get("/nominate")
        assert r.status_code == 200
        assert 'id="nomination-clear"' in r.text, (
            "recommendations came back with no way to dismiss them"
        )

    def test_the_clear_control_survives_a_lone_recommendation(self, client):
        """The `or` in the gate, which the both-cards tests cannot reach.

        Late in a draft the RFA half runs out and `/nominate` answers with a UFA
        pick alone. Flipping that `or` to `and` passes every other test in this
        batch — measured — and the x would silently stop appearing exactly when
        the panel is at its least useful. The pool carries 22 scoring RFAs, so
        emptying it is the only way to reach the state.
        """
        import main

        rfas = [p.name for p in main.auction_state.available_players.values()
                if p.is_rfa and p.projected_points > 0]
        assert rfas, "no RFAs in the pool, so this test proves nothing"
        buyer = next(c for c in main.auction_state.nomination_order if c != "BOT")
        for name in rfas:
            assign(client, name, buyer, 0.5)

        r = client.get("/nominate")
        assert r.status_code == 200
        assert "RFA Pick" not in r.text, (
            "an RFA recommendation survived the pool being emptied of RFAs, so "
            "this is still the two-card case and the `or` is untested"
        )
        assert "UFA Pick" in r.text, "precondition: a UFA half to clear"
        assert 'id="nomination-clear"' in r.text, (
            "one recommendation on screen and no way to dismiss it — the gate "
            "wants BOTH halves rather than either"
        )

    def test_the_override_endpoint_is_gone(self, client):
        """404/405, not a silent 200 — a stale bookmark must not look like it
        worked."""
        r = client.post("/set-nominator", data={"team_code": "LGN"})
        assert r.status_code in (404, 405), (
            f"/set-nominator still answers {r.status_code}"
        )

    def test_nothing_names_the_removed_turn_api(self):
        """Static guard, in the shape `test_no_template_builds_the_path_itself`
        uses: the badge and the dropdown came back one template at a time
        before, and a template naming a context key that no longer exists
        renders EMPTY rather than raising."""
        dead = ("current_nominator", "advance_nomination", "set-nominator",
                "nomination_index", "nomination_round", "snake_draft")
        searched = sorted((REPO / "templates").rglob("*.html")) + [
            REPO / "static" / "shortcuts.js",
            REPO / "main.py",
            REPO / "state.py",
            REPO / "data_loader.py",
        ]
        found = {
            f.relative_to(REPO).as_posix(): name
            for f in searched
            for name in dead
            if name in f.read_text()
        }
        assert not found, f"the nomination turn is referenced again: {found}"


class TestALegacySaveStillLoads:
    """A state file written before 2026-09-11 carries three keys that no
    longer exist on `AuctionState`.

    `from_json` read them with BRACKET access, so the removal had to drop the
    reads as well as the fields — and the operator's live
    `data/state/auction_state.json` is exactly this file. A KeyError here is a
    tool that will not boot four hours into a draft; a silent `.corrupt` rename
    is worse, because `lifespan` catches broad `Exception` by design.
    """

    def test_the_three_removed_keys_are_ignored(self, client):
        import state as state_mod

        payload = json.loads(main_state().to_json(include_snapshots=False))
        assert "nomination_round" not in payload, "precondition: no longer written"
        payload["nomination_round"] = 7
        payload["nomination_index"] = 4
        payload["snake_draft"] = False

        restored = state_mod.AuctionState.from_json(json.dumps(payload))

        assert restored.nomination_order == main_state().nomination_order
        assert len(restored.teams) == len(main_state().teams)
        assert not hasattr(restored, "nomination_round")


def main_state():
    import main

    return main.auction_state

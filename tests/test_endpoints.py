"""Tests for main.py: FastAPI endpoints."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from main import app
    with TestClient(app) as c:
        # Reset to fresh state in case other test modules modified globals
        c.post("/reset")
        yield c


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
        assert "exceeds max bid" not in r.text
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

        A cap-full team stays clickable in the bidder grid (the grid filters on
        is_done only), so BOT + a full team satisfied the advisor's uncontested
        check while the Assign gate's len(active_bidders) == 1 hid the button.
        """
        import main
        from state import PlayerOnRoster

        victim = main.auction_state.teams["HSM"]
        saved = list(victim.keeper_players)
        victim.keeper_players = [
            PlayerOnRoster(name=f"HSM_F{i}", position="F", group="3",
                           salary=1.0, projected_points=50)
            for i in range(24)
        ]
        victim._invalidate_cache()  # roster_players is memoized
        try:
            assert victim.physical_max_bid == 0.0, "fixture must be cap-full"
            assert not victim.is_done, "must be full but NOT done to be clickable"
            r = client.post("/bid-check", data={
                "player": "Connor McDavid",
                "bidders": "BOT,HSM",
                "price": "2.5",
                "highest_bidder": "BOT",
            })
            assert r.status_code == 200
            assert "bid-win" in r.text, "should be a WIN — HSM cannot raise the price"
            assert 'name="team" value="BOT"' in r.text, "Assign form must be present"
        finally:
            victim.keeper_players = saved
            victim._invalidate_cache()

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
        assert "Nomination" in r.text


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

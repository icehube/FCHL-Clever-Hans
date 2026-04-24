"""Integration tests for trade evaluation/execution, buyout, and undo flows.

Tests multi-step workflows through the HTTP API:
- Trade: evaluate → verify recommendation → execute → verify state
- Buyout: check → verify recommendation → execute → verify penalty
- Undo: perform action → undo → verify full state revert
"""

import json

import pytest
from fastapi.testclient import TestClient

from config import BUYOUT_PENALTY_RATE, MIN_SALARY, SALARY_CAP


@pytest.fixture(scope="module")
def client():
    from main import app
    with TestClient(app) as c:
        c.post("/reset")
        yield c


def _get_state(client):
    r = client.get("/state")
    assert r.status_code == 200
    return r.json()


def _team_salary(team_data):
    total = 0.0
    for p in team_data.get("keeper_players", []):
        total += p["salary"]
    for p in team_data.get("acquired_players", []):
        total += p["salary"]
    for p in team_data.get("minor_players", []):
        if p["group"] in ("2", "3"):
            total += p["salary"]
    total += team_data.get("penalties", 0.0)
    return total


def _find_player_on_roster(team_data, name):
    for p in team_data.get("keeper_players", []):
        if p["name"] == name:
            return p
    for p in team_data.get("acquired_players", []):
        if p["name"] == name:
            return p
    return None


def _roster_names(team_data):
    names = set()
    for p in team_data.get("keeper_players", []):
        names.add(p["name"])
    for p in team_data.get("acquired_players", []):
        names.add(p["name"])
    return names


# ── Buyout ──────────────────────────────────────────────────────────


class TestBuyoutFlow:
    """Check a buyout, verify recommendation, execute, verify penalty."""

    def test_00_buyout_check_low_value_player(self, client):
        """Buyout check on a low-point expensive player should return advice."""
        # Dougie Hamilton: 16pts, $4.2M — likely a buyout candidate
        r = client.get("/buyout-check/Dougie Hamilton")
        assert r.status_code == 200
        assert any(v in r.text for v in ["BUYOUT", "KEEP"]), (
            "Buyout check should return BUYOUT or KEEP verdict"
        )
        # Verify penalty math is shown
        assert "$4.2M" in r.text or "4.2" in r.text, "Should show salary info"

    def test_01_buyout_check_high_value_player(self, client):
        """Buyout check on a high-point player should recommend KEEP."""
        r = client.get("/buyout-check/Clayton Keller")
        assert r.status_code == 200
        assert "KEEP" in r.text, "Should recommend KEEP for top player"

    def test_02_buyout_check_invalid_player(self, client):
        """Buyout check on non-roster player should not crash."""
        r = client.get("/buyout-check/Nobody McFake")
        assert r.status_code == 200

    def test_03_execute_buyout(self, client):
        """Execute buyout: player removed, 50% penalty applied."""
        state_before = _get_state(client)
        bot_before = state_before["teams"]["BOT"]
        penalty_before = bot_before["penalties"]
        salary_before = _team_salary(bot_before)
        roster_before = _roster_names(bot_before)

        # Buyout Dougie Hamilton ($4.2M salary → $2.1M penalty)
        target = "Dougie Hamilton"
        target_salary = 4.2
        expected_penalty = target_salary * BUYOUT_PENALTY_RATE

        assert target in roster_before, f"{target} should be on roster before buyout"

        r = client.post("/buyout", data={"player": target})
        assert r.status_code == 200

        state_after = _get_state(client)
        bot_after = state_after["teams"]["BOT"]

        # Player removed from roster
        roster_after = _roster_names(bot_after)
        assert target not in roster_after, f"{target} should be removed after buyout"

        # Penalty increased by 50% of salary
        penalty_after = bot_after["penalties"]
        assert abs(penalty_after - penalty_before - expected_penalty) < 0.01, (
            f"Penalty should increase by ${expected_penalty}M, "
            f"was ${penalty_before}M, now ${penalty_after}M"
        )

        # Player NOT added back to available pool (bought out = gone)
        assert target not in state_after["available_players"], (
            "Bought-out player should NOT return to available pool"
        )

        # Net cap freed = salary - penalty = 50% of salary
        salary_after = _team_salary(bot_after)
        net_freed = salary_before - salary_after
        assert abs(net_freed - expected_penalty) < 0.01, (
            f"Net cap freed should be ${expected_penalty}M (50% of salary)"
        )

    def test_04_buyout_logs_transaction(self, client):
        """Buyout from test_03 should appear in transaction_log with full salary."""
        state = _get_state(client)
        log = state["transaction_log"]

        buyout_entries = [t for t in log if t["transaction_type"] == "buyout"
                          and t["player_name"] == "Dougie Hamilton"]
        assert len(buyout_entries) == 1, (
            f"Expected 1 buyout log entry for Dougie Hamilton, got {len(buyout_entries)}"
        )
        entry = buyout_entries[0]
        assert entry["team_code"] == "BOT"
        # Full salary, not penalty — reader uses badge to know it's 50%
        assert abs(entry["salary"] - 4.2) < 0.01, (
            f"Buyout log salary should be player's full salary $4.2M, got ${entry['salary']}M"
        )


# ── Undo ────────────────────────────────────────────────────────────


class TestUndoFlow:
    """Verify undo fully reverts state after various operations."""

    def test_00_undo_reverts_assign(self, client):
        """Assign a player, undo, verify complete revert."""
        state_before = _get_state(client)
        available_before = len(state_before["available_players"])
        log_before = len(state_before["transaction_log"])

        # Assign a player
        r = client.post("/assign", data={
            "player": "Artemi Panarin",
            "team": "BOT",
            "salary": "5.0",
        })
        assert r.status_code == 200

        # Verify assignment happened
        state_mid = _get_state(client)
        assert len(state_mid["available_players"]) == available_before - 1
        assert "Artemi Panarin" not in state_mid["available_players"]

        # Undo
        r = client.post("/undo")
        assert r.status_code == 200

        # Verify full revert
        state_after = _get_state(client)
        assert len(state_after["available_players"]) == available_before, (
            "Available count should revert after undo"
        )
        assert "Artemi Panarin" in state_after["available_players"], (
            "Player should return to available pool after undo"
        )
        assert len(state_after["transaction_log"]) == log_before, (
            "Transaction log should revert after undo"
        )

    def test_01_undo_reverts_buyout(self, client):
        """Buyout a player, undo, verify player restored and penalty removed."""
        state_before = _get_state(client)
        bot_before = state_before["teams"]["BOT"]
        penalty_before = bot_before["penalties"]
        roster_before = _roster_names(bot_before)

        # Pick a player to buyout
        target = "Aaron Ekblad"
        assert target in roster_before

        # Execute buyout
        r = client.post("/buyout", data={"player": target})
        assert r.status_code == 200

        # Verify buyout happened
        state_mid = _get_state(client)
        assert target not in _roster_names(state_mid["teams"]["BOT"])
        assert state_mid["teams"]["BOT"]["penalties"] > penalty_before

        # Undo
        r = client.post("/undo")
        assert r.status_code == 200

        # Verify full revert
        state_after = _get_state(client)
        bot_after = state_after["teams"]["BOT"]
        assert target in _roster_names(bot_after), (
            "Player should be back on roster after undo"
        )
        assert abs(bot_after["penalties"] - penalty_before) < 0.01, (
            "Penalty should revert after undo"
        )

    def test_02_undo_reverts_team_done(self, client):
        """Toggle team done, undo, verify reverted."""
        state_before = _get_state(client)
        was_done = state_before["teams"]["SRL"]["is_done"]

        r = client.post("/team-done", data={"team_code": "SRL"})
        assert r.status_code == 200

        state_mid = _get_state(client)
        assert state_mid["teams"]["SRL"]["is_done"] != was_done

        r = client.post("/undo")
        assert r.status_code == 200

        state_after = _get_state(client)
        assert state_after["teams"]["SRL"]["is_done"] == was_done, (
            "Team done status should revert after undo"
        )

    def test_03_multiple_undos(self, client):
        """Assign two players, undo twice, verify both reverted."""
        state_original = _get_state(client)
        available_original = len(state_original["available_players"])

        # Assign first
        client.post("/assign", data={
            "player": "Steven Stamkos",
            "team": "MAC",
            "salary": "3.5",
        })
        # Assign second
        client.post("/assign", data={
            "player": "Vincent Trocheck",
            "team": "SRL",
            "salary": "3.0",
        })

        state_mid = _get_state(client)
        assert len(state_mid["available_players"]) == available_original - 2

        # Undo both
        client.post("/undo")
        client.post("/undo")

        state_after = _get_state(client)
        assert len(state_after["available_players"]) == available_original, (
            "Two undos should restore both players"
        )
        assert "Steven Stamkos" in state_after["available_players"]
        assert "Vincent Trocheck" in state_after["available_players"]


# ── Trade ───────────────────────────────────────────────────────────


class TestTradeFlow:
    """Evaluate a trade, verify recommendation, execute, verify state."""

    def test_00_setup_acquire_player(self, client):
        """First acquire a player so we have someone to trade away."""
        # Assign Panarin to BOT so we can trade him
        r = client.post("/assign", data={
            "player": "Artemi Panarin",
            "team": "BOT",
            "salary": "5.0",
        })
        assert r.status_code == 200
        state = _get_state(client)
        assert _find_player_on_roster(state["teams"]["BOT"], "Artemi Panarin")

    def test_01_trade_evaluate_good_trade(self, client):
        """Evaluate giving a low player for a better player at similar salary — should ACCEPT."""
        # Give Evander Kane (44pts, $1.1M), receive Steven Stamkos (81pts, $1.5M)
        # Modest salary increase but big points upgrade
        r = client.post("/trade-evaluate", data={
            "give_player": ["Evander Kane"],
            "receive_player": [json.dumps({
                "name": "Steven Stamkos",
                "position": "F",
                "salary": 1.5,
                "projected_points": 81,
            })],
        })
        assert r.status_code == 200
        assert "ACCEPT" in r.text, (
            "Trading 44pts for 81pts at similar salary should recommend ACCEPT"
        )

    def test_02_trade_evaluate_returns_verdict(self, client):
        """Trade evaluation should always return a verdict (ACCEPT or DECLINE)."""
        # Give Clayton Keller (76pts, $2.0M) for an expensive low player
        r = client.post("/trade-evaluate", data={
            "give_player": ["Clayton Keller"],
            "receive_player": [json.dumps({
                "name": "Zach Sanford",
                "position": "F",
                "salary": 5.0,
                "projected_points": 6,
            })],
        })
        assert r.status_code == 200
        assert "ACCEPT" in r.text or "DECLINE" in r.text, (
            "Trade evaluation should return ACCEPT or DECLINE verdict"
        )

    def test_03_trade_execute(self, client):
        """Execute a trade: give low player, receive better player."""
        state_before = _get_state(client)
        bot_before = state_before["teams"]["BOT"]
        roster_before = _roster_names(bot_before)
        available_before = set(state_before["available_players"].keys())

        give_name = "Evander Kane"
        receive_name = "Steven Stamkos"
        receive_salary = 1.5

        assert give_name in roster_before
        assert receive_name in available_before

        # First evaluate (required — sets last_trade_eval)
        client.post("/trade-evaluate", data={
            "give_player": [give_name],
            "receive_player": [json.dumps({
                "name": receive_name,
                "position": "F",
                "salary": receive_salary,
                "projected_points": 81,
            })],
        })

        # Then execute
        r = client.post("/trade-execute")
        assert r.status_code == 200

        state_after = _get_state(client)
        bot_after = state_after["teams"]["BOT"]
        roster_after = _roster_names(bot_after)
        available_after = set(state_after["available_players"].keys())

        # Given player removed from roster, added to available pool
        assert give_name not in roster_after, (
            f"{give_name} should be removed from roster after trade"
        )
        assert give_name in available_after, (
            f"{give_name} should return to available pool after trade"
        )

        # Received player added to roster, removed from available pool
        assert receive_name in roster_after, (
            f"{receive_name} should be on roster after trade"
        )
        assert receive_name not in available_after, (
            f"{receive_name} should be removed from available pool"
        )

        # Received player has group "3" (acquired)
        received = _find_player_on_roster(bot_after, receive_name)
        assert received["group"] == "3", (
            f"Received player should have group 3, got {received['group']}"
        )

    def test_04_undo_reverts_trade(self, client):
        """Undo the trade, verify both players return to original positions."""
        # Undo the trade executed in test_03
        r = client.post("/undo")
        assert r.status_code == 200

        state = _get_state(client)
        bot = state["teams"]["BOT"]
        roster = _roster_names(bot)

        # Evander Kane should be back on roster
        assert "Evander Kane" in roster, (
            "Given player should return to roster after undo"
        )
        # Steven Stamkos should be back in available pool
        assert "Steven Stamkos" in state["available_players"], (
            "Received player should return to available pool after undo"
        )
        assert "Steven Stamkos" not in roster

    def test_05_trade_with_buyout(self, client):
        """Execute trade with immediate buyout of received player."""
        state_before = _get_state(client)
        penalty_before = state_before["teams"]["BOT"]["penalties"]

        give_name = "Chandler Stephenson"
        receive_name = "Sidney Crosby"
        receive_salary = 5.5
        expected_penalty = receive_salary * BUYOUT_PENALTY_RATE

        # Evaluate
        client.post("/trade-evaluate", data={
            "give_player": [give_name],
            "receive_player": [json.dumps({
                "name": receive_name,
                "position": "F",
                "salary": receive_salary,
                "projected_points": 94,
            })],
        })

        # Execute WITH buyout of received player
        r = client.post("/trade-execute", data={
            "buyout_player": [receive_name],
        })
        assert r.status_code == 200

        state_after = _get_state(client)
        bot_after = state_after["teams"]["BOT"]
        roster_after = _roster_names(bot_after)

        # Given player gone from roster
        assert give_name not in roster_after

        # Received player NOT on roster (was bought out)
        assert receive_name not in roster_after, (
            "Bought-out received player should not be on roster"
        )

        # Penalty increased
        penalty_after = bot_after["penalties"]
        assert abs(penalty_after - penalty_before - expected_penalty) < 0.01, (
            f"Penalty should increase by ${expected_penalty}M for buyout"
        )

        # Given player back in available pool
        assert give_name in state_after["available_players"]

    def test_06_undo_reverts_trade_with_buyout(self, client):
        """Undo the trade-with-buyout from test_05."""
        r = client.post("/undo")
        assert r.status_code == 200

        state = _get_state(client)
        bot = state["teams"]["BOT"]

        # Chandler Stephenson should be back
        assert "Chandler Stephenson" in _roster_names(bot)
        # Sidney Crosby should be in available pool
        assert "Sidney Crosby" in state["available_players"]

    def test_07_trade_execute_logs_transactions(self, client):
        """Execute a trade and verify trade_out + trade_in records are logged."""
        state_before = _get_state(client)
        log_before_count = len(state_before["transaction_log"])

        give_name = "Artemi Panarin"  # on BOT from test_00
        assert give_name in _roster_names(state_before["teams"]["BOT"])

        # Pick any available forward dynamically
        receive_name = next(
            n for n, p in state_before["available_players"].items()
            if p["position"] == "F"
        )

        client.post("/trade-evaluate", data={
            "give_player": [give_name],
            "receive_player": [json.dumps({
                "name": receive_name,
                "position": "F",
                "salary": 3.0,
                "projected_points": 50,
            })],
        })
        r = client.post("/trade-execute")
        assert r.status_code == 200

        state_after = _get_state(client)
        new_entries = state_after["transaction_log"][log_before_count:]

        trade_outs = [e for e in new_entries if e["transaction_type"] == "trade_out"]
        trade_ins = [e for e in new_entries if e["transaction_type"] == "trade_in"]

        assert len(trade_outs) == 1 and trade_outs[0]["player_name"] == give_name, (
            f"Expected one trade_out for {give_name}, got {trade_outs}"
        )
        assert len(trade_ins) == 1 and trade_ins[0]["player_name"] == receive_name, (
            f"Expected one trade_in for {receive_name}, got {trade_ins}"
        )

    def test_08_trade_with_buyout_logs_buyout_not_trade_in(self, client):
        """Received player bought out via trade flow should log as `buyout`, not `trade_in`."""
        state_before = _get_state(client)
        log_before_count = len(state_before["transaction_log"])

        # Give the player received in test_07 (last trade_in on BOT)
        give_name = next(
            p["name"] for p in state_before["teams"]["BOT"]["acquired_players"]
        )
        # Pick any available forward for receive+buyout
        receive_name = next(
            n for n, p in state_before["available_players"].items()
            if p["position"] == "F"
        )
        receive_salary = 6.0

        client.post("/trade-evaluate", data={
            "give_player": [give_name],
            "receive_player": [json.dumps({
                "name": receive_name,
                "position": "F",
                "salary": receive_salary,
                "projected_points": 82,
            })],
        })
        r = client.post("/trade-execute", data={"buyout_player": [receive_name]})
        assert r.status_code == 200

        state_after = _get_state(client)
        new_entries = state_after["transaction_log"][log_before_count:]

        # Should have: 1 trade_out (give) + 1 buyout (receive+buyout), NOT trade_in
        types = [e["transaction_type"] for e in new_entries]
        assert types.count("trade_out") == 1
        assert types.count("buyout") == 1
        assert "trade_in" not in types, (
            f"Received-and-bought-out player should log as buyout, not trade_in; got {types}"
        )

        buyout_entry = next(e for e in new_entries if e["transaction_type"] == "buyout")
        assert buyout_entry["player_name"] == receive_name
        assert abs(buyout_entry["salary"] - receive_salary) < 0.01, (
            "Buyout log should record player's full salary"
        )

"""Integration tests for trade evaluation/execution, buyout, and undo flows.

Tests multi-step workflows through the HTTP API:
- Trade: evaluate → verify recommendation → execute → verify state
- Buyout: check → verify recommendation → execute → verify penalty
- Undo: perform action → undo → verify full state revert
"""

import json

import pytest
from fastapi.testclient import TestClient

from config import (
    BUYOUT_ELIGIBLE_GROUPS,
    BUYOUT_PENALTY_RATE,
    MIN_SALARY,
    SALARY_CAP,
)
from tests.helpers import assign, squeeze, toast_of


@pytest.fixture(scope="module")
def client():
    """Module-scoped ON PURPOSE — do not convert to conftest's `client`.

    The numbered tests (test_00 onward) are a sequence: trade evaluated, then
    executed, then undone. The classes that are NOT sequential shadow this with
    their own function-scoped fixture — `TestOverCapTradesWarn` because it
    mangles a team's cap, `TestUndoRevertsEveryRosterEdit` because each of its
    cases needs an EMPTY snapshot chain or `/undo` pops somebody else's.

    That second one is the argument for the whole split: with a shared chain
    the test passed against a `/move-to-minors` that had stopped snapshotting
    entirely.

    Listed in tests/test_fixture_scopes.py.
    """
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


@pytest.fixture(scope="module")
def targets(client):
    """The players this file acts on, chosen by ROLE rather than by name.

    Every one of these used to be a literal ("Dougie Hamilton", "Clayton
    Keller", "Evander Kane", "Steven Stamkos"), which coupled the whole file to
    a dataset that is replaced before every draft. The 2026-08-07 refresh drill
    showed how that reads when it breaks: `/buyout-check/<gone>` still answers
    200, so the failure was "Should recommend KEEP for top player" against a
    player who was not on the roster at all.

    Resolved ONCE, before the first buyout — these tests share one auction, and
    re-deriving after test_03 removes a player would quietly pick a different
    one for the tests that follow.
    """
    state = _get_state(client)
    bot = state["teams"]["BOT"]
    roster = bot["keeper_players"] + bot["acquired_players"]
    assert len(roster) >= 3, "BOT needs three distinct roster players for this file"

    # Both buyout targets must be LEGAL ones. BOT's active roster carries a
    # group-B prospect, and the engine correctly refuses to buy one out, so an
    # unfiltered pick answers "not eligible for buyout" where these tests demand
    # BUYOUT or KEEP — naming nothing about the fixture that chose him. The
    # margin is thinner than it looks: on 2026-08-07 the ineligible player was
    # 6 projected points off being BOT's top scorer, i.e. inside one refresh.
    # Same reason `helpers.a_buyout_candidate` filters; this fixture predates it.
    eligible = [p for p in roster if p["group"] in BUYOUT_ELIGIBLE_GROUPS]
    assert len(eligible) >= 2, "BOT has too few buyout-eligible players"

    # Worst money-per-point on the roster: exactly what a buyout is for.
    buyout = max(eligible, key=lambda p: p["salary"] / max(p["projected_points"], 1))
    # Best player on the roster: a buyout check must answer KEEP.
    keep = max(eligible, key=lambda p: p["projected_points"])
    # Lowest scorer who is neither, so the trade tests cannot collide with the
    # player the buyout tests permanently remove. Drawn from the WHOLE roster,
    # not just the eligible ones — trading an ineligible player is legal, and
    # narrowing it here would only make a collision more likely.
    spare = min(
        (p for p in roster if p["name"] not in {buyout["name"], keep["name"]}),
        key=lambda p: p["projected_points"],
    )
    assert len({buyout["name"], keep["name"], spare["name"]}) == 3

    pool = state["available_players"]
    # The incoming half of the upgrade trade plays `spare`'s position, so the
    # swap cannot lose a lineup slot and turn an upgrade into a DECLINE.
    incoming = max(
        (p for p in pool.values() if p["position"] == spare["position"]),
        key=lambda p: p["projected_points"],
    )
    # A second, unrelated pool player for the acquire-then-trade-away sequence.
    acquired = max(
        (p for p in pool.values() if p["name"] != incoming["name"]),
        key=lambda p: p["projected_points"],
    )
    return {
        "buyout": buyout,
        "keep": keep,
        "spare": spare,
        "incoming": incoming,
        "acquired": acquired,
    }


# ── Buyout ──────────────────────────────────────────────────────────


class TestBuyoutFlow:
    """Check a buyout, verify recommendation, execute, verify penalty."""

    def test_00_buyout_check_low_value_player(self, client, targets):
        """Buyout check on a low-point expensive player should return advice."""
        target = targets["buyout"]
        r = client.get(f"/buyout-check/{target['name']}")
        assert r.status_code == 200
        assert any(v in r.text for v in ["BUYOUT", "KEEP"]), (
            "Buyout check should return BUYOUT or KEEP verdict"
        )
        # Verify penalty math is shown
        assert f"{target['salary']:g}" in r.text, "Should show salary info"

    def test_01_buyout_check_high_value_player(self, client, targets):
        """Buyout check on a high-point player should recommend KEEP."""
        r = client.get(f"/buyout-check/{targets['keep']['name']}")
        assert r.status_code == 200
        assert "KEEP" in r.text, "Should recommend KEEP for top player"

    def test_02_buyout_check_invalid_player(self, client):
        """Buyout check on non-roster player should not crash."""
        r = client.get("/buyout-check/Nobody McFake")
        assert r.status_code == 200

    def test_03_execute_buyout(self, client, targets):
        """Execute buyout: player removed, 50% penalty applied."""
        state_before = _get_state(client)
        bot_before = state_before["teams"]["BOT"]
        penalty_before = bot_before["penalties"]
        salary_before = _team_salary(bot_before)
        roster_before = _roster_names(bot_before)

        target = targets["buyout"]["name"]
        target_salary = targets["buyout"]["salary"]
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

    def test_04_buyout_logs_transaction(self, client, targets):
        """Buyout from test_03 should appear in transaction_log with full salary."""
        state = _get_state(client)
        log = state["transaction_log"]
        name = targets["buyout"]["name"]
        salary = targets["buyout"]["salary"]

        buyout_entries = [t for t in log if t["transaction_type"] == "buyout"
                          and t["player_name"] == name]
        assert len(buyout_entries) == 1, (
            f"Expected 1 buyout log entry for {name}, got {len(buyout_entries)}"
        )
        entry = buyout_entries[0]
        assert entry["team_code"] == "BOT"
        # Full salary, not penalty — reader uses badge to know it's 50%
        assert abs(entry["salary"] - salary) < 0.01, (
            f"Buyout log salary should be the full ${salary}M, got ${entry['salary']}M"
        )


# ── Undo ────────────────────────────────────────────────────────────


class TestUndoFlow:
    """Verify undo fully reverts state after various operations."""

    def test_00_undo_reverts_assign(self, client, targets):
        """Assign a player, undo, verify complete revert."""
        state_before = _get_state(client)
        available_before = len(state_before["available_players"])
        log_before = len(state_before["transaction_log"])
        player = targets["acquired"]["name"]

        assign(client, player, "BOT", 5.0)

        # Verify assignment happened
        state_mid = _get_state(client)
        assert len(state_mid["available_players"]) == available_before - 1
        assert player not in state_mid["available_players"]

        # Undo
        r = client.post("/undo")
        assert r.status_code == 200

        # Verify full revert
        state_after = _get_state(client)
        assert len(state_after["available_players"]) == available_before, (
            "Available count should revert after undo"
        )
        assert player in state_after["available_players"], (
            "Player should return to available pool after undo"
        )
        assert len(state_after["transaction_log"]) == log_before, (
            "Transaction log should revert after undo"
        )

    def test_01_undo_reverts_buyout(self, client, targets):
        """Buyout a player, undo, verify player restored and penalty removed."""
        state_before = _get_state(client)
        bot_before = state_before["teams"]["BOT"]
        penalty_before = bot_before["penalties"]
        roster_before = _roster_names(bot_before)

        # The best player on the roster — nothing else mutates him, and undoing
        # a buyout of the most valuable player is the case worth proving.
        target = targets["keep"]["name"]
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
        first, second = list(state_original["available_players"])[:2]

        assign(client, first, "MAC", 3.5)
        assign(client, second, "SRL", 3.0)

        state_mid = _get_state(client)
        assert len(state_mid["available_players"]) == available_original - 2

        # Undo both
        client.post("/undo")
        client.post("/undo")

        state_after = _get_state(client)
        assert len(state_after["available_players"]) == available_original, (
            "Two undos should restore both players"
        )
        assert first in state_after["available_players"]
        assert second in state_after["available_players"]


# ── Trade ───────────────────────────────────────────────────────────


class TestTradeFlow:
    """Evaluate a trade, verify recommendation, execute, verify state."""

    def test_00_setup_acquire_player(self, client, targets):
        """First acquire a player so we have someone to trade away."""
        player = targets["acquired"]["name"]
        assign(client, player, "BOT", 5.0)
        state = _get_state(client)
        assert _find_player_on_roster(state["teams"]["BOT"], player)

    def test_01_trade_evaluate_good_trade(self, client, targets):
        """Evaluate giving a low player for a better player at similar salary — should ACCEPT."""
        # The incoming player is built FROM the outgoing one: same position, so
        # no lineup slot is lost, and a big points gain for a small salary rise.
        # Absolute figures would go stale with the projections.
        give = targets["spare"]
        r = client.post("/trade-evaluate", data={
            "give_player": [give["name"]],
            "receive_player": [json.dumps({
                "name": "Trade Target Upgrade",
                "position": give["position"],
                "salary": give["salary"] + 0.4,
                "projected_points": give["projected_points"] + 37,
            })],
        })
        assert r.status_code == 200
        assert "ACCEPT" in r.text, (
            f"Trading {give['projected_points']}pts for "
            f"{give['projected_points'] + 37}pts at a $0.4M rise should ACCEPT"
        )

    def test_02_trade_evaluate_returns_verdict(self, client, targets):
        """Trade evaluation should always return a verdict (ACCEPT or DECLINE)."""
        # A good player for an expensive bad one — the verdict itself is not
        # what is being asserted, only that one comes back.
        r = client.post("/trade-evaluate", data={
            "give_player": [targets["keep"]["name"]],
            "receive_player": [json.dumps({
                "name": "Trade Target Downgrade",
                "position": targets["keep"]["position"],
                "salary": 5.0,
                "projected_points": 6,
            })],
        })
        assert r.status_code == 200
        assert "ACCEPT" in r.text or "DECLINE" in r.text, (
            "Trade evaluation should return ACCEPT or DECLINE verdict"
        )

    def test_03_trade_execute(self, client, targets):
        """Execute a trade: give low player, receive better player."""
        state_before = _get_state(client)
        bot_before = state_before["teams"]["BOT"]
        roster_before = _roster_names(bot_before)
        available_before = set(state_before["available_players"].keys())

        give_name = targets["spare"]["name"]
        receive = targets["incoming"]
        receive_name = receive["name"]
        receive_salary = 1.5

        assert give_name in roster_before
        assert receive_name in available_before

        # First evaluate (required — sets last_trade_eval)
        client.post("/trade-evaluate", data={
            "give_player": [give_name],
            "receive_player": [json.dumps({
                "name": receive_name,
                "position": receive["position"],
                "salary": receive_salary,
                "projected_points": receive["projected_points"],
            })],
        })

        # Then execute
        import main as _main
        r = client.post("/trade-execute", data={"trade_id": _main.last_trade_eval.trade_id})
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

    def test_04_undo_reverts_trade(self, client, targets):
        """Undo the trade, verify both players return to original positions."""
        # Undo the trade executed in test_03
        r = client.post("/undo")
        assert r.status_code == 200

        state = _get_state(client)
        bot = state["teams"]["BOT"]
        roster = _roster_names(bot)
        given, received = targets["spare"]["name"], targets["incoming"]["name"]

        assert given in roster, "Given player should return to roster after undo"
        assert received in state["available_players"], (
            "Received player should return to available pool after undo"
        )
        assert received not in roster

    def test_07_trade_execute_logs_transactions(self, client, targets):
        """Execute a trade and verify trade_out + trade_in records are logged."""
        state_before = _get_state(client)
        log_before_count = len(state_before["transaction_log"])

        give_name = targets["acquired"]["name"]  # on BOT from test_00
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
        import main as _main
        r = client.post("/trade-execute", data={"trade_id": _main.last_trade_eval.trade_id})
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


# ── Two-team trade ──────────────────────────────────────────────────


class TestTwoTeamTradeFlow:
    """Trade between BOT and a specific source team: both rosters must mutate
    and both teams must appear in the transaction log."""

    def test_two_team_trade_swaps_rosters_and_logs_both(self, client):
        client.post("/reset")
        state_before = _get_state(client)

        # Find a non-BOT team with at least one keeper.
        source_code, source_before = next(
            (code, t) for code, t in state_before["teams"].items()
            if code != "BOT" and t.get("keeper_players")
        )
        receive_player = source_before["keeper_players"][0]
        receive_name = receive_player["name"]

        # Acquire any forward to BOT to use as the give-side.
        give_name = next(
            n for n, p in state_before["available_players"].items()
            if p["position"] == "F"
        )
        client.post("/assign", data={
            "player": give_name,
            "team": "BOT",
            "salary": "1.0",
        })

        log_before_count = len(_get_state(client)["transaction_log"])

        client.post("/trade-evaluate", data={
            "give_player": [give_name],
            "source_team": source_code,
            "receive_player": [json.dumps({
                "name": receive_name,
                "position": receive_player["position"],
                "salary": receive_player["salary"],
                "projected_points": receive_player["projected_points"],
            })],
        })
        import main as _main
        r = client.post("/trade-execute", data={"trade_id": _main.last_trade_eval.trade_id})
        assert r.status_code == 200

        state_after = _get_state(client)
        bot_after = state_after["teams"]["BOT"]
        source_after = state_after["teams"][source_code]
        bot_roster = _roster_names(bot_after)
        source_roster = _roster_names(source_after)

        # Receive player moved BOT-ward.
        assert receive_name in bot_roster
        assert receive_name not in source_roster
        # Give player moved source-ward.
        assert give_name not in bot_roster
        assert give_name in source_roster
        # Neither traded player touched the available pool.
        assert give_name not in state_after["available_players"]
        assert receive_name not in state_after["available_players"]

        # Both teams logged: BOT trade_out + trade_in, source trade_in + trade_out.
        new_entries = state_after["transaction_log"][log_before_count:]
        by_team = {}
        for e in new_entries:
            by_team.setdefault(e["team_code"], []).append(e["transaction_type"])
        assert sorted(by_team["BOT"]) == ["trade_in", "trade_out"], by_team
        assert sorted(by_team[source_code]) == ["trade_in", "trade_out"], by_team


def toast_of(response) -> dict:
    """The showToast payload an endpoint attached, or {} if it attached none."""
    header = response.headers.get("HX-Trigger")
    return json.loads(header)["showToast"] if header else {}


class TestOverCapTradesWarn:
    """A trade may leave a team over the cap; it must not look like it didn't.

    Owner decision 2026-08-06: warn, do not refuse — the league permits
    temporary over-cap states and resolves them with buyouts. So the bug was
    never that the trade went through, it was that it returned the same green
    "Trade executed" as a legal one.
    """

    @pytest.fixture
    def client(self):
        """Function-scoped, shadowing the module fixture deliberately.

        These tests push a team near the cap by writing `penalties`. The module
        fixture resets once for the whole file, so that mangled cap would leak
        into every test after them — which is a tracked backlog finding on its
        own and not worth feeding.
        """
        from main import app
        with TestClient(app) as c:
            c.post("/reset")
            yield c
            c.post("/reset")

    def _stock(self, client, code: str, salary: float) -> str:
        """Assign the priciest available player to `code` at `salary`."""
        import main as _main
        name = max(_main.auction_state.available_players.values(),
                   key=lambda p: p.projected_points).name
        r = client.post("/assign", data={
            "player": name, "team": code, "salary": str(salary)})
        assert r.status_code == 200
        return name

    def _trade(self, client, a: str, b: str, from_a: str, from_b: str):
        return client.post("/trade-between", data={
            "team_a": a, "team_b": b,
            "players_from_a": from_a, "players_from_b": from_b,
        })

    def test_over_cap_trade_still_executes(self, client):
        """The owner decision, asserted first: warn, never refuse."""
        expensive = self._stock(client, "BOT", 9.0)
        cheap = self._stock(client, "SRL", 0.5)
        squeeze("SRL", headroom=1.0)

        r = self._trade(client, "BOT", "SRL", expensive, cheap)
        assert r.status_code == 200

        srl = _get_state(client)["teams"]["SRL"]
        assert expensive in _roster_names(srl), (
            "the trade was refused or rolled back — the decision was to warn"
        )

    def test_over_cap_trade_warns_and_names_the_team(self, client):
        expensive = self._stock(client, "BOT", 9.0)
        cheap = self._stock(client, "SRL", 0.5)
        squeeze("SRL", headroom=1.0)

        toast = toast_of(self._trade(client, "BOT", "SRL", expensive, cheap))
        assert toast.get("type") == "warning", toast
        assert "SRL" in toast["message"] and "over cap" in toast["message"], toast
        # SRL had $1.0M of room, gave up $0.5M and took on $9.0M -> $7.5M over.
        assert "$7.5M over cap" in toast["message"], toast

    def test_legal_trade_stays_a_plain_success(self, client):
        """The control. If a legal trade also warns, the warning means nothing."""
        a_player = self._stock(client, "BOT", 2.0)
        b_player = self._stock(client, "SRL", 2.0)

        toast = toast_of(self._trade(client, "BOT", "SRL", a_player, b_player))
        assert toast.get("type") == "success", toast
        assert "over cap" not in toast["message"], toast

    def test_both_sides_over_cap_are_both_named(self, client):
        """Reporting one side would hide half the problem.

        Note what the setup has to do. In a two-team trade the salary deltas
        are equal and opposite, so a trade cannot push both sides over on its
        own — one side's gain is exactly the other's relief. Both being over
        therefore means both were already over (buyout penalties, typically)
        and the swap left them there. That is the case this pins: the helper
        reports post-trade truth for every team it was given, not just the one
        whose salary went up.
        """
        a_player = self._stock(client, "BOT", 2.0)
        b_player = self._stock(client, "SRL", 1.0)
        squeeze("BOT", headroom=-3.0)   # already $3.0M over
        squeeze("SRL", headroom=-3.0)

        toast = toast_of(self._trade(client, "BOT", "SRL", a_player, b_player))
        assert toast.get("type") == "warning", toast
        # BOT sheds $1.0M net (over by 2.0), SRL takes it on (over by 4.0).
        assert "BOT $2.0M over cap" in toast["message"], toast
        assert "SRL $4.0M over cap" in toast["message"], toast
        # Worst first, so the most urgent number is the one you read.
        assert toast["message"].index("SRL") < toast["message"].index("BOT $"), toast

    def test_exactly_at_the_cap_is_not_over(self, client):
        """Boundary: at SALARY_CAP a team is legal, not over.

        This is what the rounding in _cap_overages is for — total_salary sums
        many $0.1M values, and raw float noise reports "$0.0M over cap" on a
        team that is exactly legal.
        """
        import main as _main
        a_player = self._stock(client, "BOT", 2.0)
        b_player = self._stock(client, "SRL", 2.0)
        # Leave SRL landing exactly on the cap after an even-salary swap.
        squeeze("SRL", headroom=0.0)

        toast = toast_of(self._trade(client, "BOT", "SRL", a_player, b_player))
        srl = _main.auction_state.teams["SRL"]
        assert round(srl.total_salary, 1) == SALARY_CAP, "fixture missed the boundary"
        assert toast.get("type") == "success", toast
        assert "over cap" not in toast["message"], toast

    def test_missing_team_codes_are_skipped(self, client):
        """The documented no-op path, which the signature originally denied.

        `TradeEvaluation.source_team_code` is `str | None` and /trade-execute
        passes it straight through, so None and "" reach this helper on any
        BOT-side trade with no counterparty. The first version annotated
        `*team_codes: str`, which was simply false about its own caller.
        """
        import main as _main
        assert _main._cap_overages(None) == []
        assert _main._cap_overages("") == []
        assert _main._cap_overages("NOPE") == []
        # A real over-cap team is still found alongside the junk.
        squeeze("SRL", headroom=-2.0)
        assert _main._cap_overages(None, "SRL", "") == ["SRL $2.0M over cap"]

    def test_trade_execute_warns_too(self, client):
        """The BOT-side path has the same gap."""
        import main as _main
        state = _get_state(client)
        source_code, source = next(
            (c, t) for c, t in state["teams"].items()
            if c != "BOT" and t.get("keeper_players")
        )
        receive = source["keeper_players"][0]
        give = self._stock(client, "BOT", 0.5)
        squeeze("BOT", headroom=0.0)

        client.post("/trade-evaluate", data={
            "give_player": [give],
            "source_team": source_code,
            "receive_player": [json.dumps({
                "name": receive["name"], "position": receive["position"],
                "salary": receive["salary"],
                "projected_points": receive["projected_points"],
            })],
        })
        r = client.post("/trade-execute", data={
            "trade_id": _main.last_trade_eval.trade_id})
        assert r.status_code == 200
        toast = toast_of(r)
        assert toast.get("type") == "warning", toast
        assert "BOT" in toast["message"] and "over cap" in toast["message"], toast



class TestUndoRevertsEveryRosterEdit:
    """The six snapshot-taking endpoints that had no undo test.

    Ten POST endpoints call `save_snapshot()`; four were covered above
    (`test_00`–`test_04`). These six were not: /adjust-salary, /toggle-bench,
    /move-to-minors, /move-to-roster, /set-nominator, /trade-between. All six
    restore correctly today — this is insurance on the one operation that
    cannot be worked around. Mid-draft there is no second Ctrl+Z, so an undo
    that quietly restores less than it should is unrecoverable and invisible.

    Every case is baseline -> act -> **assert it changed** -> undo -> assert
    restored. The middle assertion is not decoration: without it an endpoint
    that silently does nothing passes, which is how three tests in this suite
    asserted nothing for months.
    """

    @pytest.fixture
    def client(self):
        """Function-scoped, shadowing the module fixture deliberately.

        Each test needs an EMPTY snapshot chain. The mutation these tests exist
        to catch is "the endpoint stopped calling save_snapshot", and with a
        chain carried over from earlier tests `/undo` would pop somebody else's
        snapshot and could restore the right answer for the wrong reason.
        `POST /reset` builds a fresh AuctionState, so the chain starts empty.
        """
        from main import app
        with TestClient(app) as c:
            c.post("/reset")
            yield c
            c.post("/reset")

    def _bot(self):
        import main
        return main.auction_state.teams["BOT"]

    def _undo_cycle(self, client, act, read, label):
        """baseline -> act -> assert changed -> undo -> assert restored."""
        before = read()
        r = act()
        assert r.status_code == 200, f"{label} returned {r.status_code}"
        during = read()
        assert during != before, (
            f"{label} changed nothing, so undoing it proves nothing "
            f"(still {before!r})"
        )
        u = client.post("/undo")
        assert u.status_code == 200
        assert read() == before, (
            f"undo did not revert {label}: {before!r} -> {during!r} -> {read()!r}"
        )

    def test_undo_reverts_adjust_salary(self, client):
        name = self._bot().roster_players[0].name
        self._undo_cycle(
            client,
            lambda: client.post("/adjust-salary", data={
                "team_code": "BOT", "player_name": name, "new_salary": "9.9"}),
            lambda: next(p.salary for p in self._bot().roster_players if p.name == name),
            "/adjust-salary",
        )

    def test_undo_reverts_toggle_bench(self, client):
        """Reads the SCORE, not just the flag.

        Only the starting lineup scores, so benching a starter has to move
        `current_roster_points`. Asserting on `is_bench` alone would pass
        against a restore that flipped the flag back while leaving the lineup
        computed from a stale roster.
        """
        starter = max(
            (p for p in self._bot().roster_players if not p.is_bench),
            key=lambda p: p.projected_points,
        ).name
        self._undo_cycle(
            client,
            lambda: client.post("/toggle-bench", data={
                "team_code": "BOT", "player_name": starter}),
            lambda: (
                next(p.is_bench for p in self._bot().roster_players if p.name == starter),
                self._bot().current_roster_points,
            ),
            "/toggle-bench",
        )

    def test_undo_reverts_move_to_minors(self, client):
        """Benching first is a precondition, not part of what is being tested.

        The demote button only renders for a benched player, so the bench
        toggle happens before the baseline is read.

        That precondition is also why the reading carries `is_bench`, and it is
        the load-bearing part. /toggle-bench takes a snapshot of its own, so
        with a counts-only reading this test PASSED against a build where
        /move-to-minors had stopped snapshotting: `/undo` popped the bench
        toggle's snapshot instead, and pre-bench has the same roster and minors
        counts as post-bench. The endpoint under test was doing nothing and the
        assertion could not tell. Reading the flag separates the two states.
        """
        name = self._bot().roster_players[0].name
        client.post("/toggle-bench", data={"team_code": "BOT", "player_name": name})

        def where_he_is():
            p = self._bot().find_player(name)
            return (
                len(self._bot().roster_players),
                len(self._bot().minor_players),
                (p.is_minor, p.is_bench) if p else None,
            )

        self._undo_cycle(
            client,
            lambda: client.post("/move-to-minors", data={
                "team_code": "BOT", "player_name": name}),
            where_he_is,
            "/move-to-minors",
        )

    def test_undo_reverts_move_to_roster(self, client):
        name = self._bot().minor_players[0].name
        self._undo_cycle(
            client,
            lambda: client.post("/move-to-roster", data={
                "team_code": "BOT", "player_name": name}),
            lambda: (
                len(self._bot().roster_players),
                len(self._bot().minor_players),
                round(self._bot().total_salary, 2),
            ),
            "/move-to-roster",
        )

    def test_undo_reverts_set_nominator(self, client):
        import main

        target = next(
            c for c in main.auction_state._effective_order()
            if c != main.auction_state.current_nominator()
        )
        self._undo_cycle(
            client,
            lambda: client.post("/set-nominator", data={"team_code": target}),
            lambda: (
                main.auction_state.nomination_index,
                main.auction_state.current_nominator(),
            ),
            "/set-nominator",
        )

    def test_undo_reverts_trade_between(self, client):
        """Reads NAME SETS, not counts.

        A 1-for-1 trade leaves both rosters the same size, so a count-based
        reading cannot fail — the probe that first checked this endpoint used
        counts and proved nothing. It also used `player_a`/`player_b`, which
        the endpoint does not take (`players_from_a`/`players_from_b`), so it
        hit the "no players named" early return and never traded at all.
        """
        import main

        def names():
            return (
                sorted(p.name for p in main.auction_state.teams["BOT"].roster_players),
                sorted(p.name for p in main.auction_state.teams["SRL"].roster_players),
            )

        gives = main.auction_state.teams["BOT"].roster_players[0].name
        gets = main.auction_state.teams["SRL"].roster_players[0].name
        self._undo_cycle(
            client,
            lambda: client.post("/trade-between", data={
                "team_a": "BOT", "players_from_a": gives,
                "team_b": "SRL", "players_from_b": gets}),
            names,
            "/trade-between",
        )

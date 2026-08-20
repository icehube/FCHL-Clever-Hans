"""Integration tests: auction draft simulations.

TestAuctionDraftSimulation: 10-player early-auction draft verifying per-pick
invariants, bid advice shifts, and MILP direction.

TestLateAuctionCollapse: Late-auction scenario where most opponents are done
drafting, market ceiling collapses, and BOT picks up bargains.
"""

import pytest
from fastapi.testclient import TestClient

from config import SALARY_CAP, MIN_SALARY
from tests.helpers import pool_top

# Module-level state shared across ordered test methods
_state: dict = {}

BID_CHECK_PRICE = 2.0

# Which teams buy, in which order, at what price. The TEAMS and SALARIES are the
# script; the PLAYERS are derived (see `_script`).
_SCRIPT = [
    ("GVR", 11.4), ("BOT", 5.0), ("SRL", 5.5), ("BOT", 4.5), ("LPT", 7.0),
    ("HSM", 4.7), ("BOT", 3.0), ("MAC", 3.5), ("BOT", 3.0), ("ZSK", 5.0),
]

# Derived from the table above rather than written out beside it. `test_15` and
# `test_16` used to carry `== 4` and `5.0 + 4.5 + 3.0 + 3.0`, which is the table
# copied by hand: editing a row of the script would leave the copy stale, and
# the assertion would then fail describing the wrong reason — or, if two edits
# cancelled, pass while checking nothing.
_BOT_PICKS = sum(1 for team, _ in _SCRIPT if team == "BOT")
_BOT_SPEND = round(sum(salary for team, salary in _SCRIPT if team == "BOT"), 1)


def _script() -> tuple[list[tuple[str, str, float]], str]:
    """The ten picks plus a spare, derived by the ROLE each assertion needs.

    This table used to be ten literal names. It was, as it happens, almost
    exactly the top ten by projected points — which is the tell that the names
    were never the point. Three of them were, though, and the derivation has to
    keep them or the assertions stop meaning anything:

    * **pick 5** is described as the top D-man,
    * **pick 6** must be an **RFA2**, because `test_08` asserts the sale
      converts him to group 3,
    * **pick 7** is a goalie.

    The eleventh name is the bid-check target for `test_04` and `test_12`, and
    the one property it needs is that the ten picks do NOT take him: those two
    tests compare advice on the same player early and late, and a player who has
    left the pool answers with the not-found branch instead. Taking him from the
    same `pool_top(20)` slice the picks come from is what guarantees that.

    Called from `test_01`, after the reset and before pick 1: `pool_top` ranks
    whatever is in the pool right now, and `main.auction_state` does not exist
    at all until the client starts, so this cannot be module-level.
    """
    d = pool_top(1, position="D")[0]
    g = pool_top(1, position="G")[0]
    # Skipping the two already claimed, because the roles genuinely overlap: the
    # pool holds 9 RFA2s today and they include a D and a G, so on another CSV
    # the top D could BE the top RFA2. Asserting they came out distinct would
    # only turn that into a hard failure of this whole file — in the one file
    # whose point is surviving a new players.csv.
    rfa = pool_top(1, group="RFA2", skip={d, g})[0]
    roles = {d, g, rfa}
    # Enough of the top of the pool to fill the seven remaining picks and the
    # spare, with the three role picks excluded so nobody is bought twice.
    rest = [n for n in pool_top(20) if n not in roles]
    ordered = [rest[0], rest[1], rest[2], rest[3], d, rfa, g, rest[4], rest[5], rest[6]]
    picks = [(name, team, salary) for name, (team, salary) in zip(ordered, _SCRIPT)]
    return picks, rest[7]


@pytest.fixture(scope="module")
def client():
    """Module-scoped ON PURPOSE — do not convert to conftest's `client`.

    Every test here is numbered and builds on the one before it: this file is a
    draft played forward, not a set of independent checks. A per-test reset
    would not make it isolated, it would make it meaningless.

    Note it does NOT reset on entry either, which is deliberate in the same
    way: the file starts from whatever `lifespan` built.

    Listed in tests/test_fixture_scopes.py, which is what stops a future
    "why is this module-scoped?" from quietly breaking the sequence.
    """
    from main import app
    with TestClient(app) as c:
        yield c


def _get_state(client):
    """Fetch JSON state from /state endpoint."""
    r = client.get("/state")
    assert r.status_code == 200
    return r.json()


def _team_salary(team_data):
    """Compute total salary for a team from JSON state (mirrors TeamState.total_salary)."""
    total = 0.0
    for p in team_data.get("keeper_players", []):
        total += p["salary"]
    for p in team_data.get("acquired_players", []):
        total += p["salary"]
    # Cap-eligible minors (groups 2 and 3)
    for p in team_data.get("minor_players", []):
        if p["group"] in ("2", "3"):
            total += p["salary"]
    total += team_data.get("penalties", 0.0)
    return total


def _roster_count(team_data):
    """Count roster players (keepers + acquired, not minors)."""
    return len(team_data.get("keeper_players", [])) + len(team_data.get("acquired_players", []))


class TestAuctionDraftSimulation:
    """Simulate a 10-player draft and verify system consistency."""

    def test_00_reset(self, client):
        """Reset to fresh state before simulation."""
        r = client.post("/reset")
        assert r.status_code == 200

    def test_01_baseline(self, client):
        """Capture baseline state before any picks."""
        state = _get_state(client)
        import main

        _state["baseline_available"] = len(state["available_players"])
        _state["baseline_bot_salary"] = _team_salary(state["teams"]["BOT"])
        _state["baseline_bot_roster"] = _roster_count(state["teams"]["BOT"])
        _state["baseline_milp_points"] = main.milp_solution.total_points
        _state["baseline_market_ceiling"] = main.market_info.market_ceiling
        _state["milp_history"] = [main.milp_solution.total_points]
        _state["picks"], _state["spare"] = _script()
        _state["prev_available"] = len(state["available_players"])
        _state["team_salaries"] = {
            code: _team_salary(t) for code, t in state["teams"].items()
        }
        _state["team_rosters"] = {
            code: _roster_count(t) for code, t in state["teams"].items()
        }

        assert _state["baseline_available"] > 600, "Should have hundreds of available players"
        assert _state["baseline_bot_roster"] > 0, "BOT should have keepers"

    def test_02_pick_01_top_of_the_pool_to_gvr(self, client):
        """Pick 1: the top available player to GVR at $11.4M — big budget hit."""
        self._assign_and_verify(client, 0)

    def test_03_pick_02_to_bot(self, client):
        """Pick 2: $5.0M — BOT pick."""
        self._assign_and_verify(client, 1)

    def test_04_bid_check_early(self, client):
        """Bid-check the spare early (BOT has plenty of budget)."""
        r = client.post("/bid-check", data={
            "player": _state["spare"],
            "price": str(BID_CHECK_PRICE),
            "bidders": "",
        })
        assert r.status_code == 200
        _state["early_bid_check"] = r.text

    def test_05_pick_03_to_srl(self, client):
        """Pick 3: SRL at $5.5M."""
        self._assign_and_verify(client, 2)

    def test_06_pick_04_to_bot(self, client):
        """Pick 4: BOT at $4.5M."""
        self._assign_and_verify(client, 3)

    def test_07_pick_05_top_d_to_lpt(self, client):
        """Pick 5: the top D-man to LPT at $7.0M."""
        self._assign_and_verify(client, 4)

    def test_08_pick_06_an_rfa2_to_hsm(self, client):
        """Pick 6: an RFA2 to HSM at $4.7M — should convert to group 3."""
        self._assign_and_verify(client, 5)
        # Verify RFA group conversion
        name = _state["picks"][5][0]
        state = _get_state(client)
        hsm_acquired = state["teams"]["HSM"]["acquired_players"]
        sold = next((p for p in hsm_acquired if p["name"] == name), None)
        assert sold is not None, f"{name} should be in HSM acquired"
        assert sold["group"] == "3", f"RFA2 should convert to group 3, got {sold['group']}"

    def test_09_pick_07_a_goalie_to_bot(self, client):
        """Pick 7: a goalie to BOT at $3.0M."""
        self._assign_and_verify(client, 6)

    def test_10_pick_08_to_mac(self, client):
        """Pick 8: MAC at $3.5M."""
        self._assign_and_verify(client, 7)

    def test_11_pick_09_to_bot(self, client):
        """Pick 9: BOT at $3.0M."""
        self._assign_and_verify(client, 8)

    def test_12_bid_check_late(self, client):
        """Bid-check the same spare late (BOT budget is tighter)."""
        r = client.post("/bid-check", data={
            "player": _state["spare"],
            "price": str(BID_CHECK_PRICE),
            "bidders": "",
        })
        assert r.status_code == 200
        _state["late_bid_check"] = r.text

    def test_13_pick_10_to_zsk(self, client):
        """Pick 10: ZSK at $5.0M — final pick."""
        self._assign_and_verify(client, 9)

    # --- Cross-cutting assertions after all 10 picks ---

    def test_14_available_count(self, client):
        """Available players should have decreased by exactly 10."""
        state = _get_state(client)
        expected = _state["baseline_available"] - 10
        actual = len(state["available_players"])
        assert actual == expected, f"Expected {expected} available, got {actual}"

    def test_15_bot_roster_grew(self, client):
        """BOT should have one more player per BOT row in the script."""
        state = _get_state(client)
        bot_acquired = len(state["teams"]["BOT"]["acquired_players"])
        assert bot_acquired == _BOT_PICKS, (
            f"BOT should have {_BOT_PICKS} acquired, got {bot_acquired}"
        )

    def test_16_bot_budget_consistent(self, client):
        """BOT remaining budget = SALARY_CAP - total_salary."""
        state = _get_state(client)
        total_salary = _team_salary(state["teams"]["BOT"])
        remaining = SALARY_CAP - total_salary
        bot_salary_increase = total_salary - _state["baseline_bot_salary"]
        expected_increase = _BOT_SPEND
        assert abs(bot_salary_increase - expected_increase) < 0.01, (
            f"BOT salary should increase by ${expected_increase}M, "
            f"got ${bot_salary_increase:.1f}M"
        )
        assert remaining > 0, f"BOT should have positive remaining budget, got ${remaining:.1f}M"

    def test_17_milp_still_optimal(self, client):
        """MILP should still produce an Optimal solution after 10 picks."""
        import main
        assert main.milp_solution.status == "Optimal"
        assert main.milp_solution.total_points > 0

    def test_18_market_ceiling_valid(self, client):
        """Market ceiling should still be positive and at most MAX_SALARY."""
        import main
        from config import MAX_SALARY
        final_ceiling = main.market_info.market_ceiling
        assert final_ceiling > 0, "Market ceiling should be positive"
        assert final_ceiling <= MAX_SALARY, (
            f"Market ceiling should not exceed MAX_SALARY (${MAX_SALARY}M), "
            f"got ${final_ceiling:.1f}M"
        )

    def test_19_bid_check_changed(self, client):
        """Bid advice should differ between early and late in the draft."""
        early = _state.get("early_bid_check", "")
        late = _state.get("late_bid_check", "")
        assert early, "Early bid check should have been captured"
        assert late, "Late bid check should have been captured"
        assert early != late, "Bid advice should change as budget/market shifts"

    def test_20_milp_direction(self, client):
        """MILP points should have changed over the draft."""
        history = _state.get("milp_history", [])
        assert len(history) == 11, f"Should have 11 MILP snapshots (baseline + 10), got {len(history)}"
        # Points should not be identical throughout — the draft should cause movement
        assert len(set(history)) > 1, "MILP points should not be static across 10 picks"

    # --- Helper ---

    def _assign_and_verify(self, client, pick_index: int):
        """Assign a player and verify per-pick invariants."""
        player, team, salary = _state["picks"][pick_index]

        # Snapshot before
        state_before = _get_state(client)
        available_before = len(state_before["available_players"])
        acquired_before = len(state_before["teams"][team]["acquired_players"])
        salary_before = _team_salary(state_before["teams"][team])
        log_before = len(state_before["transaction_log"])

        # Assign
        r = client.post("/assign", data={
            "player": player,
            "team": team,
            "salary": str(salary),
        })
        assert r.status_code == 200, f"Assign {player} failed with {r.status_code}"

        # Snapshot after
        state_after = _get_state(client)

        # 1. Available count decreased by 1
        available_after = len(state_after["available_players"])
        assert available_after == available_before - 1, (
            f"Pick {pick_index+1} ({player}): available should be {available_before-1}, got {available_after}"
        )

        # 2. Player no longer available
        assert player not in state_after["available_players"], (
            f"{player} should no longer be in available_players"
        )

        # 3. Team's acquired grew by 1
        acquired_after = len(state_after["teams"][team]["acquired_players"])
        assert acquired_after == acquired_before + 1, (
            f"Pick {pick_index+1}: {team} acquired should be {acquired_before+1}, got {acquired_after}"
        )

        # 4. Team salary increased by salary amount
        salary_after = _team_salary(state_after["teams"][team])
        assert abs(salary_after - salary_before - salary) < 0.01, (
            f"Pick {pick_index+1}: {team} salary should increase by ${salary}M, "
            f"got ${salary_after - salary_before:.1f}M"
        )

        # 5. Transaction log grew by 1 with correct data
        log_after = len(state_after["transaction_log"])
        assert log_after == log_before + 1, (
            f"Pick {pick_index+1}: transaction log should grow by 1"
        )
        txn = state_after["transaction_log"][-1]
        assert txn["player_name"] == player
        assert txn["team_code"] == team
        assert abs(txn["salary"] - salary) < 0.01
        assert txn["transaction_type"] == "draft"

        # Track MILP history
        import main
        _state["milp_history"].append(main.milp_solution.total_points)
        _state["prev_available"] = available_after


# --- Late Auction Collapse ---

# All opponent team codes (BOT excluded)
OPPONENT_TEAMS = ["SRL", "MAC", "LPT", "SHF", "JHN", "GVR", "ZSK", "LGN", "VPP", "HSM"]

# Module-level state for the late-auction test
_late: dict = {}


@pytest.fixture(scope="module")
def late_client():
    from main import app
    with TestClient(app) as c:
        c.post("/reset")
        yield c


class TestLateAuctionCollapse:
    """Simulate late auction: most opponents done, market ceiling collapses."""

    def test_00_baseline(self, late_client):
        """Capture baseline before marking teams done."""
        import main
        _late["baseline_ceiling"] = main.market_info.market_ceiling
        _late["baseline_milp_points"] = main.milp_solution.total_points
        _late["baseline_milp_cost"] = main.milp_solution.total_cost

        assert _late["baseline_ceiling"] > MIN_SALARY, "Baseline ceiling should be well above floor"

    def test_01_mark_8_teams_done(self, late_client):
        """Mark 8 of 10 opponents as done — only 2 remain active."""
        done_teams = OPPONENT_TEAMS[:8]  # SRL, MAC, LPT, SHF, JHN, GVR, ZSK, LGN
        for code in done_teams:
            r = late_client.post("/team-done", data={"team_code": code})
            assert r.status_code == 200

        import main
        _late["ceiling_8_done"] = main.market_info.market_ceiling
        _late["demand_8_done"] = main.market_info.demand_count

        # With 8 teams done, demand drops to 2 active opponents
        assert _late["demand_8_done"] == 2, (
            f"Should have 2 active opponents, got {_late['demand_8_done']}"
        )
        # Ceiling may still be MAX_SALARY if remaining teams have large budgets
        assert _late["ceiling_8_done"] > 0, "Ceiling should be positive"

    def test_02_milp_still_optimal_with_fewer_opponents(self, late_client):
        """MILP should still produce optimal solution with reduced field."""
        import main
        assert main.milp_solution.status == "Optimal"
        assert main.milp_solution.total_points > 0

    def test_03_bid_check_with_reduced_field(self, late_client):
        """Bid advice should reflect the smaller field."""
        r = late_client.post("/bid-check", data={
            "player": pool_top(1)[0],
            "price": "2.0",
            "bidders": "",
        })
        assert r.status_code == 200
        _late["bid_check_8_done"] = r.text
        # Should get advice (BID/CAUTION/DROP) — not crash
        assert any(action in r.text for action in ["BID", "CAUTION", "DROP"]), (
            "Bid check should return actionable advice"
        )

    def test_04_mark_last_2_done(self, late_client):
        """Mark remaining 2 opponents done — total collapse, floor pricing."""
        for code in OPPONENT_TEAMS[8:]:  # VPP, HSM
            r = late_client.post("/team-done", data={"team_code": code})
            assert r.status_code == 200

        import main
        assert main.market_info.floor_demand is True, "All opponents done should trigger floor_demand"
        assert main.market_info.demand_count == 0, "No active opponents should mean demand_count=0"
        assert main.market_info.market_ceiling == MIN_SALARY, (
            f"Market ceiling should be MIN_SALARY (${MIN_SALARY}M), "
            f"got ${main.market_info.market_ceiling:.1f}M"
        )

    def test_05_all_market_prices_at_floor(self, late_client):
        """With zero demand, every player's market price should be MIN_SALARY."""
        import main
        for name, price in main.market_prices.items():
            assert price == MIN_SALARY, (
                f"{name} should be at floor ${MIN_SALARY}M, got ${price:.1f}M"
            )

    def test_06_milp_fills_roster_at_floor(self, late_client):
        """MILP should still produce optimal solution at floor prices."""
        import main
        assert main.milp_solution.status == "Optimal"
        # With all players at $0.5M, the optimizer should pick the highest-point
        # players available — cost should be very low
        team = main.auction_state.teams["BOT"]
        expected_cost = team.total_spots_remaining * MIN_SALARY
        assert abs(main.milp_solution.total_cost - expected_cost) < 0.01, (
            f"At floor prices, cost should be {team.total_spots_remaining} × "
            f"${MIN_SALARY}M = ${expected_cost:.1f}M, got ${main.milp_solution.total_cost:.1f}M"
        )

    def test_07_assign_at_floor_price(self, late_client):
        """Assign a top player at floor price — the late-auction bargain."""
        state_before = _get_state(late_client)
        bot_before = _team_salary(state_before["teams"]["BOT"])

        player = pool_top(1)[0]
        r = late_client.post("/assign", data={
            "player": player,
            "team": "BOT",
            "salary": str(MIN_SALARY),
        })
        assert r.status_code == 200

        state_after = _get_state(late_client)
        bot_after = _team_salary(state_after["teams"]["BOT"])
        assert abs(bot_after - bot_before - MIN_SALARY) < 0.01, (
            f"Salary should increase by ${MIN_SALARY}M"
        )
        assert player not in state_after["available_players"]

    def test_08_bid_check_at_floor(self, late_client):
        """Bid recommendation should reflect floor ceiling.

        `pool_top` reads the live pool, so this is the NEXT player down — test_07
        just bought the one above him. A name that has left the pool answers with
        the not-found branch, which carries no verdict at all.
        """
        r = late_client.post("/bid-check", data={
            "player": pool_top(1)[0],
            "price": str(MIN_SALARY),
            "bidders": "",
        })
        assert r.status_code == 200
        # Max bid should be at or near floor since no competition
        assert "BID" in r.text or "CAUTION" in r.text or "DROP" in r.text

    def test_09_reactivate_team_restores_ceiling(self, late_client):
        """Un-marking a team as done should restore market ceiling above floor."""
        import main
        assert main.market_info.floor_demand is True

        # Reactivate VPP (a team with decent budget)
        r = late_client.post("/team-done", data={"team_code": "VPP"})
        assert r.status_code == 200

        assert main.market_info.floor_demand is False, (
            "Reactivating a team should clear floor_demand"
        )
        assert main.market_info.market_ceiling > MIN_SALARY, (
            f"Ceiling should rise above floor after reactivation, "
            f"got ${main.market_info.market_ceiling:.1f}M"
        )
        assert main.market_info.demand_count == 1, (
            f"Should have 1 active opponent, got {main.market_info.demand_count}"
        )

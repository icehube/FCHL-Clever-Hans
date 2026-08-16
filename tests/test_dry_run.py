"""Dry run: 40-pick simulated auction exercising all major workflows.

Simulates a realistic auction through the HTTP API: early picks, mid-auction
trades and buyouts, late-auction with teams done, and final verification.
Tests interaction between all systems (optimizer, market, nominations, toasts,
OOB swaps, atomic saves) in a single continuous flow.
"""

import json
import os
import re

import pytest
from fastapi.testclient import TestClient

from config import MAX_SALARY, MIN_SALARY, SALARY_CAP
from tests.helpers import assign

TEAMS = ["BOT", "SRL", "MAC", "LPT", "SHF", "JHN", "GVR", "ZSK", "LGN", "VPP", "HSM"]

# The SHAPE of the 40 picks — who buys, and for how much — across all teams (4
# BOT, 36 opponents), with prices falling as the auction runs. WHO gets bought
# is decided by `picks` below, against the live pool.
#
# This list used to name 40 real players, and the 2026-08-07 refresh drill broke
# it twice over: a name that leaves players.csv makes `/assign` a silent no-op,
# and the failure then surfaced as a transaction-log count in a LATER test,
# several picks downstream of the rejection and naming neither the player nor
# the reason.
PICK_SHAPE = [
    # Phase 1: Early auction (picks 1-15)
    ("GVR", 11.4), ("BOT", 5.0), ("ZSK", 5.0), ("SRL", 5.5), ("MAC", 5.5),
    ("LPT", 4.7), ("SHF", 7.0), ("HSM", 4.7), ("JHN", 3.0), ("LGN", 3.5),
    ("BOT", 3.9), ("VPP", 3.9), ("GVR", 3.9), ("ZSK", 3.0), ("SRL", 3.0),
    # Phase 2: Mid-auction (picks 16-25)
    ("MAC", 10.5), ("LPT", 3.0), ("SHF", 7.1), ("HSM", 2.8), ("JHN", 2.5),
    ("LGN", 2.1), ("VPP", 2.5), ("GVR", 3.0), ("BOT", 3.0), ("ZSK", 2.5),
    # Phase 3: Late auction with teams done (picks 26-35)
    ("SRL", 2.0), ("MAC", 2.0), ("LPT", 2.0), ("SHF", 2.0), ("HSM", 2.0),
    ("JHN", 1.5), ("LGN", 1.5), ("VPP", 1.5), ("GVR", 1.5), ("BOT", 2.0),
    # Phase 4: Final picks (picks 36-40)
    ("ZSK", 1.5), ("SRL", 1.0), ("MAC", 1.5), ("LPT", 1.5), ("SHF", 1.0),
]


@pytest.fixture(scope="module")
def client():
    """Module-scoped ON PURPOSE — do not convert to conftest's `client`.

    This file is one continuous 40-pick auction: early picks, mid-auction
    trades and buyouts, teams finishing, final verification. The shared state
    IS the test — resetting between the numbered phases would leave each one
    asserting against a fresh draft it never played.

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


@pytest.fixture(scope="module")
def picks(client):
    """The 40 picks as (player, team, salary), best players first.

    Module-scoped and resolved ONCE, before pick 1: the phases consume slices
    of the same list, and re-ranking a pool that has already lost its top 15
    would hand phase 2 a different set than phase 1 skipped.

    Descending projected points is roughly how a real auction runs, and it puts
    the expensive picks at the top of `PICK_SHAPE`'s price ladder where the
    shape expects them.
    """
    pool = _get_state(client)["available_players"].values()
    ranked = sorted(pool, key=lambda p: -p["projected_points"])
    assert len(ranked) >= len(PICK_SHAPE), (
        f"pool has {len(ranked)} players; a {len(PICK_SHAPE)}-pick dry run needs more"
    )
    return [
        (player["name"], team, salary)
        for player, (team, salary) in zip(ranked, PICK_SHAPE)
    ]


def _bot_roster(client) -> list[dict]:
    """BOT's keepers plus anything drafted or traded in, in roster order."""
    bot = _get_state(client)["teams"]["BOT"]
    return bot["keeper_players"] + bot["acquired_players"]


class TestDryRun:
    """40-pick simulated auction: the full auction-day experience."""

    def test_00_phase1_early_auction(self, client, picks):
        """Picks 1-15: early auction with basic invariant checks."""
        state = _get_state(client)
        initial_available = len(state["available_players"])

        for i in range(15):
            r = assign(client, *picks[i])

            # Every assign should have a toast
            trigger = r.headers.get("HX-Trigger", "")
            assert "showToast" in trigger, f"Pick {i+1}: missing toast header"

        state = _get_state(client)
        assert len(state["available_players"]) == initial_available - 15
        assert len(state["transaction_log"]) == 15

    def test_01_nomination_works(self, client):
        """Nomination recommendations should work after 15 picks."""
        r = client.get("/nominate")
        assert r.status_code == 200
        # Should have RFA or UFA pick with strategy info
        assert any(s in r.text for s in ["target", "drain", "depth"])

    def test_02_bid_check_with_price_increments(self, client, picks):
        """Bid check at different prices should return different advice."""
        # The last pick of the run: still in the pool, and cheap enough that
        # $0.5M and $5.0M land on opposite sides of any sane verdict.
        target = picks[-1][0]
        r1 = client.post("/bid-check", data={
            "player": target, "price": "0.5", "bidders": "",
        })
        r2 = client.post("/bid-check", data={
            "player": target, "price": "5.0", "bidders": "",
        })
        assert r1.status_code == 200
        assert r2.status_code == 200
        # At $0.5 should likely BID, at $5.0 likely DROP or CAUTION
        assert r1.text != r2.text

    def test_03_phase2_mid_auction(self, client, picks):
        """Picks 16-25: mid-auction drafting."""
        for i in range(15, 25):
            assign(client, *picks[i])

        state = _get_state(client)
        assert len(state["transaction_log"]) == 25

    def test_04_trade_flow(self, client):
        """Execute a trade mid-auction: evaluate → execute → verify."""
        # Give away whatever BOT drafted first; take back a hypothetical player
        # who is deliberately NOT in the pool, so the trade cannot collide with
        # a real name (the incoming side of a trade is free-form by design).
        give = _bot_roster(client)[-1]["name"]
        r = client.post("/trade-evaluate", data={
            "give_player": [give],
            "receive_player": [json.dumps({
                "name": "Traded In Centreman",
                "position": "F",
                "salary": 1.5,
                "projected_points": 67,
            })],
        })
        assert r.status_code == 200
        assert "ACCEPT" in r.text or "DECLINE" in r.text

        # Execute (regardless of recommendation — testing the flow)
        import main
        r = client.post("/trade-execute", data={"trade_id": main.last_trade_eval.trade_id})
        assert r.status_code == 200
        trigger = r.headers.get("HX-Trigger", "")
        assert "Trade executed" in trigger, "Trade must actually execute, not be rejected as stale"

        # Undo the trade to restore state
        client.post("/undo")

    def test_05_buyout_flow(self, client):
        """Execute a buyout mid-auction."""
        # BOT's lowest-scoring keeper: the one a buyout would plausibly target.
        target = min(_bot_roster(client), key=lambda p: p["projected_points"])["name"]

        r = client.get("/buyout-check", params={"player_name": target})
        assert r.status_code == 200

        # Execute buyout
        r = client.post("/buyout", data={"player": target})
        assert r.status_code == 200
        trigger = r.headers.get("HX-Trigger", "")
        assert "showToast" in trigger, "Buyout should have toast"

        # Undo
        client.post("/undo")

    def test_06_phase3_teams_done(self, client, picks):
        """Mark 5 teams as done, verify market adjusts."""
        import main

        ceiling_before = main.market_info.market_ceiling

        done_teams = ["SRL", "MAC", "LPT", "SHF", "JHN"]
        for code in done_teams:
            r = client.post("/team-done", data={"team_code": code})
            assert r.status_code == 200

        # Market should still function
        assert main.market_info.demand_count < 10

        # Continue drafting picks 26-35
        for i in range(25, 35):
            assign(client, *picks[i])

        # Un-done the teams for remaining picks
        for code in done_teams:
            client.post("/team-done", data={"team_code": code})

    def test_07_phase4_final_picks(self, client, picks):
        """Picks 36-40: final picks."""
        for i in range(35, 40):
            assign(client, *picks[i])

        state = _get_state(client)
        assert len(state["transaction_log"]) == 40

    def test_08_final_state_coherent(self, client):
        """Final state should be internally consistent."""
        state = _get_state(client)

        # No player on multiple teams
        all_names = set()
        for code, team in state["teams"].items():
            for p in team["keeper_players"] + team["acquired_players"]:
                assert p["name"] not in all_names, f"{p['name']} on multiple teams"
                all_names.add(p["name"])

        # No roster player in the available pool. `overlap` used to be computed
        # here and never asserted on — the whole check rested on the narrower
        # acquired-only loop below. Since the loader disambiguates duplicate
        # names, the broad version holds too, and it is the one that catches a
        # keeper being re-drafted.
        available_names = set(state["available_players"].keys())
        overlap = sorted(all_names & available_names)
        assert not overlap, f"{overlap} are on a roster AND still biddable"
        for code, team in state["teams"].items():
            for p in team["acquired_players"]:
                assert p["name"] not in available_names, (
                    f"{p['name']} is both acquired and available"
                )

        # Budget consistency
        for code, team in state["teams"].items():
            total = sum(p["salary"] for p in team["keeper_players"])
            total += sum(p["salary"] for p in team["acquired_players"])
            for p in team["minor_players"]:
                if p["group"] in ("2", "3"):
                    total += p["salary"]
            total += team["penalties"]
            remaining = SALARY_CAP - total
            assert remaining >= -0.01, f"{code} over cap: ${total:.1f}M"

    def test_09_milp_still_optimal(self, client):
        """MILP should still produce optimal solution after 40 picks."""
        import main
        assert main.milp_solution.status == "Optimal"
        assert main.milp_solution.total_points > 0

    def test_10_buyout_indicators_oob(self, client):
        """Buyout indicators endpoint should return matching OOB IDs."""
        # Get main page to find placeholder IDs
        idx = client.get("/")
        main_ids = set(re.findall(r'id="bo-([^"]+)"', idx.text))

        # Get buyout indicators
        r = client.get("/buyout-indicators")
        assert r.status_code == 200
        dot_ids = set(re.findall(r'id="bo-([^"]+)"', r.text))

        # All dot IDs should have matching placeholders
        orphans = dot_ids - main_ids
        assert not orphans, f"OOB orphan IDs (no placeholder): {orphans}"

    def test_11_position_filter_attributes(self, client):
        """Every available player row should have data-position attribute."""
        r = client.get("/")
        assert r.status_code == 200
        # Find all data-position values
        positions = re.findall(r'data-position="([^"]+)"', r.text)
        assert len(positions) > 0, "No data-position attributes found"
        # All should be F, D, or G
        for pos in positions:
            assert pos in ("F", "D", "G"), f"Invalid position: {pos}"

    def test_12_atomic_save_backup(self, client):
        """Atomic save should create backup file."""
        # main.STATE_DIR, not the hardcoded data/state/ — conftest redirects the
        # app to a temp dir precisely so pytest cannot read or clobber a real
        # draft. Hardcoding it made this pass only on a machine that happened to
        # have a live state file, and pass without ever exercising the app's own
        # backup path.
        import main
        backup_path = os.path.join(main.STATE_DIR, "auction_state.json.backup")
        assert os.path.exists(backup_path), "Backup file should exist after saves"
        # Backup should be valid JSON
        with open(backup_path) as f:
            data = json.load(f)
        assert "teams" in data

    def test_13_projected_standings(self, client):
        """Projected standings should appear in league state."""
        r = client.get("/")
        assert r.status_code == 200
        assert "Proj" in r.text
        assert "Pts" in r.text

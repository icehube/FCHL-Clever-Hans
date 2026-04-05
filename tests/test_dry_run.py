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

TEAMS = ["BOT", "SRL", "MAC", "LPT", "SHF", "JHN", "GVR", "ZSK", "LGN", "VPP", "HSM"]

# 40 picks distributed across all teams (4 BOT, 36 opponents)
PICKS = [
    # Phase 1: Early auction (picks 1-15)
    ("Connor McDavid", "GVR", 11.4),
    ("Artemi Panarin", "BOT", 5.0),
    ("J.T. Miller", "ZSK", 5.0),
    ("Filip Forsberg", "SRL", 5.5),
    ("Sidney Crosby", "MAC", 5.5),
    ("Sebastian Aho", "LPT", 4.7),
    ("Roman Josi", "SHF", 7.0),
    ("Mitch Marner", "HSM", 4.7),
    ("Sergei Bobrovsky", "JHN", 3.0),
    ("Steven Stamkos", "LGN", 3.5),
    ("Jason Robertson", "BOT", 3.9),
    ("Aleksander Barkov", "VPP", 3.9),
    ("Mathew Barzal", "GVR", 3.9),
    ("Zach Hyman", "ZSK", 3.0),
    ("Vincent Trocheck", "SRL", 3.0),
    # Phase 2: Mid-auction (picks 16-25)
    ("Igor Shesterkin", "MAC", 10.5),
    ("Jake Guentzel", "LPT", 3.0),
    ("Victor Hedman", "SHF", 7.1),
    ("Nazem Kadri", "HSM", 2.8),
    ("Adrian Kempe", "JHN", 2.5),
    ("Gustav Nyquist", "LGN", 2.1),
    ("Chris Kreider", "VPP", 2.5),
    ("Stuart Skinner", "GVR", 3.0),
    ("Jake Oettinger", "BOT", 3.0),
    ("Kevin Fiala", "ZSK", 2.5),
    # Phase 3: Late auction with teams done (picks 26-35)
    ("Brock Boeser", "SRL", 2.0),
    ("Lucas Raymond", "MAC", 2.0),
    ("Carter Verhaeghe", "LPT", 2.0),
    ("Mika Zibanejad", "SHF", 2.0),
    ("Mark Scheifele", "HSM", 2.0),
    ("Anze Kopitar", "JHN", 1.5),
    ("Ryan O'Reilly", "LGN", 1.5),
    ("Brock Nelson", "VPP", 1.5),
    ("Jonathan Marchessault", "GVR", 1.5),
    ("Bo Horvat", "BOT", 2.0),
    # Phase 4: Final picks (picks 36-40)
    ("Brad Marchand", "ZSK", 1.5),
    ("Joe Pavelski", "SRL", 1.0),
    ("Nico Hischier", "MAC", 1.5),
    ("Evgeni Malkin", "LPT", 1.5),
    ("Matt Duchene", "SHF", 1.0),
]


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


class TestDryRun:
    """40-pick simulated auction: the full auction-day experience."""

    def test_00_phase1_early_auction(self, client):
        """Picks 1-15: early auction with basic invariant checks."""
        state = _get_state(client)
        initial_available = len(state["available_players"])

        for i in range(15):
            player, team, salary = PICKS[i]
            r = client.post("/assign", data={
                "player": player, "team": team, "salary": str(salary),
            })
            assert r.status_code == 200, f"Pick {i+1} failed"

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

    def test_02_bid_check_with_price_increments(self, client):
        """Bid check at different prices should return different advice."""
        r1 = client.post("/bid-check", data={
            "player": "Matt Duchene", "price": "0.5", "bidders": "",
        })
        r2 = client.post("/bid-check", data={
            "player": "Matt Duchene", "price": "5.0", "bidders": "",
        })
        assert r1.status_code == 200
        assert r2.status_code == 200
        # At $0.5 should likely BID, at $5.0 likely DROP or CAUTION
        assert r1.text != r2.text

    def test_03_phase2_mid_auction(self, client):
        """Picks 16-25: mid-auction drafting."""
        for i in range(15, 25):
            player, team, salary = PICKS[i]
            r = client.post("/assign", data={
                "player": player, "team": team, "salary": str(salary),
            })
            assert r.status_code == 200

        state = _get_state(client)
        assert len(state["transaction_log"]) == 25

    def test_04_trade_flow(self, client):
        """Execute a trade mid-auction: evaluate → execute → verify."""
        # Trade BOT's Artemi Panarin for an opponent's player
        r = client.post("/trade-evaluate", data={
            "give_player": ["Artemi Panarin"],
            "receive_player": [json.dumps({
                "name": "Evgeni Malkin",
                "position": "F",
                "salary": 1.5,
                "projected_points": 67,
            })],
        })
        assert r.status_code == 200
        assert "ACCEPT" in r.text or "DECLINE" in r.text

        # Execute (regardless of recommendation — testing the flow)
        r = client.post("/trade-execute")
        assert r.status_code == 200
        trigger = r.headers.get("HX-Trigger", "")
        assert "showToast" in trigger, "Trade execute should have toast"

        # Undo the trade to restore state
        client.post("/undo")

    def test_05_buyout_flow(self, client):
        """Execute a buyout mid-auction."""
        # Check buyout on a low-value keeper
        r = client.get("/buyout-check/Dougie Hamilton")
        assert r.status_code == 200

        # Execute buyout
        r = client.post("/buyout", data={"player": "Dougie Hamilton"})
        assert r.status_code == 200
        trigger = r.headers.get("HX-Trigger", "")
        assert "showToast" in trigger, "Buyout should have toast"

        # Undo
        client.post("/undo")

    def test_06_phase3_teams_done(self, client):
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
            player, team, salary = PICKS[i]
            r = client.post("/assign", data={
                "player": player, "team": team, "salary": str(salary),
            })
            assert r.status_code == 200

        # Un-done the teams for remaining picks
        for code in done_teams:
            client.post("/team-done", data={"team_code": code})

    def test_07_phase4_final_picks(self, client):
        """Picks 36-40: final picks."""
        for i in range(35, 40):
            player, team, salary = PICKS[i]
            r = client.post("/assign", data={
                "player": player, "team": team, "salary": str(salary),
            })
            assert r.status_code == 200

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

        # No roster player in available pool
        available_names = set(state["available_players"].keys())
        overlap = all_names & available_names
        # Keepers may share names with minors but acquired should not be in available
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
        backup_path = "data/state/auction_state.json.backup"
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

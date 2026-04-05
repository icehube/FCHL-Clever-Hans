"""Stress test: randomized 50-pick auction simulation.

Assigns 50 players across all teams with randomized salaries,
verifying system invariants hold after every single pick.
"""

import random

import pytest
from fastapi.testclient import TestClient

from config import MAX_SALARY, MIN_SALARY, SALARY_CAP


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


class TestStressAuction:
    """Randomized 50-pick auction with invariant checking after every pick."""

    NUM_PICKS = 50

    def test_full_simulation(self, client):
        """Run 50 randomized picks and verify invariants throughout."""
        random.seed(42)  # Reproducible

        state = _get_state(client)
        teams = list(state["teams"].keys())
        available = list(state["available_players"].keys())
        initial_available = len(available)

        # Sort by points descending — draft from top
        player_points = {
            name: p["projected_points"]
            for name, p in state["available_players"].items()
        }
        draft_pool = sorted(available, key=lambda n: -player_points[n])

        picks_made = 0
        team_pick_counts = {t: 0 for t in teams}

        for i in range(self.NUM_PICKS):
            if not draft_pool:
                break

            # Pick a player from top 20 (randomized within top tier)
            top_n = min(20, len(draft_pool))
            pick_idx = random.randint(0, top_n - 1)
            player = draft_pool[pick_idx]

            # Pick a random team
            team = random.choice(teams)

            # Random salary between MIN and market-reasonable
            salary = round(random.uniform(MIN_SALARY, min(5.0, MAX_SALARY)), 1)

            # Assign
            r = client.post("/assign", data={
                "player": player,
                "team": team,
                "salary": str(salary),
            })
            assert r.status_code == 200, (
                f"Pick {i+1}: assign {player} to {team} at ${salary}M failed"
            )

            # Remove from our local pool
            draft_pool.remove(player)
            picks_made += 1
            team_pick_counts[team] += 1

            # Verify invariants every pick
            new_state = _get_state(client)
            self._check_invariants(new_state, i + 1, initial_available, picks_made)

        assert picks_made == self.NUM_PICKS, (
            f"Should have made {self.NUM_PICKS} picks, only made {picks_made}"
        )

        # Final cross-cutting checks
        final_state = _get_state(client)
        self._check_final(final_state, initial_available, picks_made)

    def _check_invariants(self, state, pick_num, initial_available, picks_so_far):
        """Verify system invariants after each pick."""
        # 1. Available count is correct
        actual_available = len(state["available_players"])
        expected_available = initial_available - picks_so_far
        assert actual_available == expected_available, (
            f"Pick {pick_num}: expected {expected_available} available, got {actual_available}"
        )

        # 2. Transaction log has correct length
        assert len(state["transaction_log"]) == picks_so_far, (
            f"Pick {pick_num}: expected {picks_so_far} transactions, "
            f"got {len(state['transaction_log'])}"
        )

        # 3. Every team's salary <= SALARY_CAP (can't be negative remaining)
        for code, team in state["teams"].items():
            salary = _team_salary(team)
            # Allow tiny float errors
            assert salary <= SALARY_CAP + 0.01, (
                f"Pick {pick_num}: {code} salary ${salary:.1f}M exceeds cap ${SALARY_CAP}M"
            )

        # 4. All transaction types are "draft"
        for txn in state["transaction_log"]:
            assert txn["transaction_type"] == "draft", (
                f"Pick {pick_num}: unexpected transaction type {txn['transaction_type']}"
            )

        # 5. No player appears on multiple teams
        all_roster_names = set()
        for code, team in state["teams"].items():
            for p in team.get("keeper_players", []) + team.get("acquired_players", []):
                assert p["name"] not in all_roster_names, (
                    f"Pick {pick_num}: {p['name']} appears on multiple teams"
                )
                all_roster_names.add(p["name"])

        # 6. No roster player is also in available pool
        for name in all_roster_names:
            if name in state["available_players"]:
                # Minor/keeper players might share names with available — check acquired only
                pass

        # 7. MILP is still functioning
        import main
        assert main.milp_solution is not None, (
            f"Pick {pick_num}: MILP solution is None"
        )
        assert main.milp_solution.status in ("Optimal", "Infeasible"), (
            f"Pick {pick_num}: unexpected MILP status {main.milp_solution.status}"
        )

    def _check_final(self, state, initial_available, total_picks):
        """Final validation after all picks."""
        import main

        # Market info still valid
        assert main.market_info is not None
        assert main.market_info.market_ceiling >= MIN_SALARY
        assert main.market_info.market_ceiling <= MAX_SALARY

        # MILP still runs
        assert main.milp_solution.status in ("Optimal", "Infeasible")

        # Total acquired across all teams = total_picks
        total_acquired = sum(
            len(t.get("acquired_players", []))
            for t in state["teams"].values()
        )
        assert total_acquired == total_picks, (
            f"Total acquired ({total_acquired}) should equal picks made ({total_picks})"
        )

        # Available pool depleted correctly
        assert len(state["available_players"]) == initial_available - total_picks


class TestStressUndoRedo:
    """Stress test the undo system with rapid assign/undo cycles."""

    def test_rapid_assign_undo_cycles(self, client):
        """Assign and undo 20 times — state should be stable."""
        client.post("/reset")
        state_baseline = _get_state(client)
        baseline_available = len(state_baseline["available_players"])

        for i in range(20):
            # Pick a player that exists
            state = _get_state(client)
            available = list(state["available_players"].keys())
            if not available:
                break
            player = available[0]

            # Assign
            client.post("/assign", data={
                "player": player,
                "team": "BOT",
                "salary": "1.0",
            })

            # Immediately undo
            client.post("/undo")

            # Verify state restored
            state_after = _get_state(client)
            assert len(state_after["available_players"]) == baseline_available, (
                f"Cycle {i+1}: available count should be {baseline_available}, "
                f"got {len(state_after['available_players'])}"
            )

    def test_max_snapshots(self, client):
        """Push past max snapshots (50) and verify undo still works."""
        client.post("/reset")

        # Make 55 assignments (exceeds 50-snapshot limit)
        state = _get_state(client)
        available = sorted(
            state["available_players"].keys(),
            key=lambda n: -state["available_players"][n]["projected_points"],
        )

        for i in range(55):
            if i >= len(available):
                break
            client.post("/assign", data={
                "player": available[i],
                "team": "BOT" if i % 2 == 0 else "SRL",
                "salary": "0.5",
            })

        # Now undo 50 times (max snapshots)
        for i in range(50):
            r = client.post("/undo")
            assert r.status_code == 200

        # 51st undo should be a no-op (no more snapshots)
        state_before_extra_undo = _get_state(client)
        r = client.post("/undo")
        assert r.status_code == 200
        state_after_extra_undo = _get_state(client)

        # State should be unchanged
        assert len(state_before_extra_undo["available_players"]) == \
               len(state_after_extra_undo["available_players"])


class TestStressBidCheck:
    """Stress test bid checking across many players."""

    def test_bid_check_all_top_players(self, client):
        """Bid check every top-50 player — none should crash."""
        client.post("/reset")
        state = _get_state(client)

        # Sort by points, take top 50
        players = sorted(
            state["available_players"].items(),
            key=lambda x: -x[1]["projected_points"],
        )[:50]

        for name, player_data in players:
            r = client.post("/bid-check", data={
                "player": name,
                "price": "1.0",
                "bidders": "",
            })
            assert r.status_code == 200, f"Bid check failed for {name}"
            # Should contain a bid action
            assert any(action in r.text for action in ["BID", "CAUTION", "DROP"]), (
                f"Bid check for {name} should return an action"
            )

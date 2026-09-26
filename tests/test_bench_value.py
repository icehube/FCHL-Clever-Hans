"""The bench is worth points: expected points, depth weights, and the MILP.

A backup plays when a starter at his position is out -- injured, or swapped out
at a monthly adjustment -- so the k-th backup at a position is worth
BENCH_DEPTH_WEIGHTS[pos][k-1] of his points (config.OUT_RATE). Until 2026-09-25
every decision compared starters only, and the solver's own bench term was a
flat 5-point bonus per filled 2F/1D/1G slot, whoever filled it, counted only for
players it bought.
"""

import itertools
import math
import random

import pytest

import state
from config import BENCH_DEPTH_WEIGHTS, BENCH_SIZE, OUT_RATE, STARTING_LINEUP
from config import _depth_weights
from optimizer import solve_optimal_roster
from state import Player, PlayerOnRoster, TeamState, expected_points, lineup_points


def _player(name, pos, pts):
    return Player(name=name, position=pos, group="3", nhl_team="TOR", age=25,
                  projected_points=pts, is_rfa=False, salary=0.0,
                  team_probability=0.04)


def _keeper(name, pos, pts, salary=0.5):
    return PlayerOnRoster(name=name, position=pos, group="3", salary=salary,
                          projected_points=pts)


def _brute_force(players) -> float:
    """Every legal bench, scored directly: the best 12F/6D/2G start, then try
    every subset of up to BENCH_SIZE of the rest, each position's chosen
    backups scored in descending order against its depth weights."""
    by_pos = {pos: sorted((p.projected_points for p in players if p.position == pos),
                          reverse=True) for pos in STARTING_LINEUP}
    starters = sum(sum(v[:STARTING_LINEUP[pos]]) for pos, v in by_pos.items())
    rest = [(pos, pts) for pos, v in by_pos.items() for pts in v[STARTING_LINEUP[pos]:]]
    best = 0.0
    for size in range(min(BENCH_SIZE, len(rest)) + 1):
        for chosen in itertools.combinations(rest, size):
            total = 0.0
            for pos in STARTING_LINEUP:
                mine = sorted((pts for p, pts in chosen if p == pos), reverse=True)
                total += sum(w * pts for w, pts in zip(BENCH_DEPTH_WEIGHTS[pos], mine))
            best = max(best, total)
    return starters + best


class TestTheWeights:
    def test_they_are_the_binomial_tail(self):
        m = OUT_RATE["F"]
        assert BENCH_DEPTH_WEIGHTS["F"][0] == pytest.approx(1 - (1 - m) ** 12)
        n, k = 6, 2
        tail = sum(math.comb(n, i) * m**i * (1 - m) ** (n - i) for i in range(k, n + 1))
        assert BENCH_DEPTH_WEIGHTS["D"][1] == pytest.approx(tail)

    def test_they_fall_with_depth(self):
        """`expected_points`' greedy is exact only because of this."""
        for pos, ws in BENCH_DEPTH_WEIGHTS.items():
            assert list(ws) == sorted(ws, reverse=True), pos
            assert all(0 < w < 1 for w in ws), pos

    def test_a_position_with_more_starters_covers_more(self):
        """12 forwards miss more games between them than 2 goalies do."""
        assert BENCH_DEPTH_WEIGHTS["F"][0] > BENCH_DEPTH_WEIGHTS["D"][0] > BENCH_DEPTH_WEIGHTS["G"][0]

    def test_no_absences_means_the_bench_is_worth_nothing(self, monkeypatch):
        """With every OUT_RATE at 0 the model reduces EXACTLY to the old
        starters-only figure -- the backward-compatibility anchor."""
        zero = {pos: tuple(w for w in _depth_weights(n, 0.0, 4) if w > 0)
                for pos, n in STARTING_LINEUP.items()}
        monkeypatch.setattr(state, "BENCH_DEPTH_WEIGHTS", zero)
        rng = random.Random(7)
        for _ in range(20):
            roster = [_player(f"p{i}", rng.choice("FFFDDG"), rng.randint(0, 90))
                      for i in range(24)]
            assert state.expected_points(roster) == lineup_points(roster)


class TestExpectedPoints:
    def test_the_greedy_matches_brute_force(self):
        rng = random.Random(2026)
        for trial in range(60):
            size = rng.randint(18, 26)
            roster = [_player(f"p{i}", rng.choice("FFFFDDDG"), rng.randint(0, 90))
                      for i in range(size)]
            assert expected_points(roster) == pytest.approx(_brute_force(roster)), trial

    def test_a_backup_adds_his_share_and_a_starter_all_of_his(self):
        base = ([_player(f"F{i}", "F", 50) for i in range(12)]
                + [_player(f"D{i}", "D", 40) for i in range(6)]
                + [_player(f"G{i}", "G", 60) for i in range(2)])
        assert expected_points(base) == lineup_points(base)
        backup = _player("Fb", "F", 30)
        assert expected_points(base + [backup]) == pytest.approx(
            lineup_points(base) + BENCH_DEPTH_WEIGHTS["F"][0] * 30)

    def test_the_minors_do_not_play(self):
        """Beyond BENCH_SIZE nobody adds anything."""
        base = ([_player(f"F{i}", "F", 50) for i in range(12)]
                + [_player(f"D{i}", "D", 40) for i in range(6)]
                + [_player(f"G{i}", "G", 60) for i in range(2)])
        bench = [_player(f"Fb{i}", "F", 30) for i in range(BENCH_SIZE)]
        extra = _player("Fx", "F", 30)
        assert expected_points(base + bench) > lineup_points(base)
        assert expected_points(base + bench + [extra]) == pytest.approx(
            expected_points(base + bench))


def _starting_core():
    """A complete, strong starting lineup: no candidate below will start."""
    return ([_keeper(f"sF{i}", "F", 80) for i in range(12)]
            + [_keeper(f"sD{i}", "D", 70) for i in range(6)]
            + [_keeper(f"sG{i}", "G", 75) for i in range(2)])


class TestTheSolverValuesTheBench:
    def test_its_figure_is_expected_points_of_the_roster_it_picked(self):
        team = TeamState(code="BOT", name="T", keeper_players=_starting_core())
        pool = {f"c{i}": _player(f"c{i}", pos, pts)
                for i, (pos, pts) in enumerate([("F", 45), ("F", 40), ("D", 35),
                                                 ("G", 30), ("F", 20), ("D", 10)])}
        sol = solve_optimal_roster(team, pool, {n: 0.5 for n in pool})
        assert sol.status == "Optimal"
        assert sol.total_points == pytest.approx(
            expected_points(team.roster_players + sol.roster))
        assert sol.lineup_points == lineup_points(team.roster_players + sol.roster)
        assert sol.total_points > sol.lineup_points

    def test_a_useless_backup_does_not_earn_the_slot(self):
        """The flat-bonus bug. With two backup forwards already on the bench and
        one spot left, the old objective paid 5 points for ANY defenceman in the
        empty D backup slot -- so a 1-point D beat a 30-point F (5.1 vs 3.0).
        By depth weight the F is a 3rd backup forward (0.26 x 30 = 7.9) and the
        D a 1st backup D (0.62 x 1 = 0.6)."""
        keepers = _starting_core() + [_keeper("bF1", "F", 40), _keeper("bF2", "F", 38),
                                      _keeper("bG1", "G", 30)]
        team = TeamState(code="BOT", name="T", keeper_players=keepers)
        pool = {"useless D": _player("useless D", "D", 1),
                "decent F": _player("decent F", "F", 30)}
        sol = solve_optimal_roster(team, pool, {n: 0.5 for n in pool})
        assert [p.name for p in sol.roster] == ["decent F"]

    def test_the_bench_you_have_counts_like_the_bench_you_buy(self):
        """The asymmetry. BENCH_WEIGHT credited only players the MILP bought,
        so the backups already on the roster were invisible. Here two backup
        defencemen are already on the bench, filling both modelled D slots: a
        candidate D is worth nothing more, and the first backup goalie
        (0.28 x 20 = 5.6) is the pick. With the keepers invisible, the D looks
        like a FIRST backup D (0.62 x 35 = 21.8) and wins."""
        keepers = _starting_core() + [_keeper("bD1", "D", 40), _keeper("bD2", "D", 38),
                                      _keeper("bF1", "F", 40)]
        team = TeamState(code="BOT", name="T", keeper_players=keepers)
        pool = {"cand D": _player("cand D", "D", 35),
                "cand G": _player("cand G", "G", 20)}
        sol = solve_optimal_roster(team, pool, {n: 0.5 for n in pool})
        assert [p.name for p in sol.roster] == ["cand G"]

    def test_a_second_backup_is_worth_less_than_a_first(self):
        """Depth, in the solver. One backup D already sits on the bench, so a
        candidate D is the SECOND backup D (0.22 x 40 = 9.0) and loses to the
        first backup goalie (0.28 x 35 = 9.7). Priced as if every backup were
        a first backup, the D (0.62 x 40 = 24.9) would win."""
        keepers = _starting_core() + [_keeper("bD1", "D", 45), _keeper("bF1", "F", 40),
                                      _keeper("bF2", "F", 38)]
        team = TeamState(code="BOT", name="T", keeper_players=keepers)
        pool = {"cand D": _player("cand D", "D", 40),
                "cand G": _player("cand G", "G", 35)}
        sol = solve_optimal_roster(team, pool, {n: 0.5 for n in pool})
        assert [p.name for p in sol.roster] == ["cand G"]

    def test_it_finds_the_true_optimum_by_brute_force(self):
        """The objective must BE expected points, not merely be reported as it.

        `total_points` is recomputed from the chosen roster, so an objective
        that credits the wrong thing still reports an honest figure -- for the
        wrong roster. The solver-checker found exactly that hole on
        2026-09-25: letting a STARTING candidate also hold a bench slot (drop
        `- s_cand[n]`) double-credits him up to 1.86x, and no other test
        noticed. So: small random cases with two open spots, every legal pair
        of purchases scored by `expected_points` directly, and the solver must
        match the best of them.
        """
        rng = random.Random(925)
        for trial in range(25):
            keepers = ([_keeper(f"sF{i}", "F", rng.randint(40, 90)) for i in range(11)]
                       + [_keeper(f"sD{i}", "D", rng.randint(30, 70)) for i in range(6)]
                       + [_keeper(f"sG{i}", "G", rng.randint(40, 80)) for i in range(2)]
                       + [_keeper(f"b{i}", rng.choice("FDG"), rng.randint(0, 50))
                          for i in range(3)])
            team = TeamState(code="BOT", name="T", keeper_players=keepers)
            assert team.total_spots_remaining == 2
            pool = {f"c{i}": _player(f"c{i}", rng.choice("FFDG"), rng.randint(5, 95))
                    for i in range(8)}
            prices = {n: rng.choice([0.5, 0.5, 1.0, 2.0, 4.0]) for n in pool}
            budget = team.remaining_budget
            need_f = 12 - sum(1 for k in keepers if k.position == "F")
            best = max(
                expected_points(team.roster_players + [pool[a], pool[b]])
                for a, b in itertools.combinations(pool, 2)
                if prices[a] + prices[b] <= budget
                and sum(pool[n].position == "F" for n in (a, b)) >= need_f
            )
            sol = solve_optimal_roster(team, pool, prices)
            assert sol.status == "Optimal", trial
            assert sol.total_points == pytest.approx(best), (
                f"trial {trial}: solver chose {[p.name for p in sol.roster]} "
                f"worth {sol.total_points:.2f}; the best legal pair is worth {best:.2f}"
            )


class TestTheSolveIsTimeBoxed:
    """CBC's default is unbounded; a request that never returns hangs the UI."""

    def test_every_solve_passes_the_time_limit(self, monkeypatch):
        import pulp
        import optimizer
        seen = []
        real = pulp.PULP_CBC_CMD

        def spy(*args, **kwargs):
            seen.append(kwargs.get("timeLimit"))
            return real(*args, **kwargs)

        monkeypatch.setattr(optimizer.pulp, "PULP_CBC_CMD", spy)
        team = TeamState(code="BOT", name="T", keeper_players=_starting_core())
        pool = {"c": _player("c", "F", 40), "d": _player("d", "D", 30)}
        optimizer.solve_optimal_roster(team, pool, {"c": 0.5, "d": 0.5})
        assert seen == [optimizer.SOLVE_TIME_LIMIT]

    def test_a_solve_cut_off_by_the_limit_says_so(self, monkeypatch):
        """PuLP reports status "Optimal" for a CBC run stopped on time with a
        feasible roster; only sol_status tells them apart."""
        import pulp
        real = pulp.LpProblem.solve

        def cut_off(self, *a, **k):
            result = real(self, *a, **k)
            self.sol_status = pulp.LpSolutionIntegerFeasible
            return result

        monkeypatch.setattr(pulp.LpProblem, "solve", cut_off)
        team = TeamState(code="BOT", name="T", keeper_players=_starting_core())
        # Four open spots, six candidates: a fillable roster.
        pool = {f"c{i}": _player(f"c{i}", "FDFGFD"[i], 30 + i) for i in range(6)}
        sol = solve_optimal_roster(team, pool, {n: 0.5 for n in pool})
        assert sol.status == "Optimal" and sol.roster, "the best roster is still used"
        assert sol.timed_out


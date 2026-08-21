"""Measure where a cold `/bid-check` spends its second, and whether it has to.

NOT a test — pytest ignores it. Named `measure_marginal.py` for the same reason
`measure_ceiling.py` and `measure_layout.py` are: this is an instrument, not an
assertion. It answers "how many MILP solves does `compute_marginal_value`
actually run, how long is each, and does a cheaper formulation give the same
answer" — questions the suite cannot ask, because the answer is a property of a
whole state rather than of any one assertion.

The question exists because a cold `/bid-check` measured **935-1030ms** on
2026-08-19, and the cost is paid roughly **once per nomination** rather than once
per draft: `main._marginal_cache` is keyed by player and cleared at every epoch,
so the first bid-check on a player after each pick is cold and every later
keystroke on the same player is ~9ms.

**What this measured, 2026-08-21.** The wall time reproduces (956-1511ms) but the
split does not: the backlog entry says 98-99% of it is inside CBC and the
aggregate is **89.5-89.8%** over two full runs, the remainder being a flat ~9.2ms
per solve of model build and extraction, paid ten times for ten models that
differ in one number.

That aggregate is worth distrusting, though — read the per-subject column, which
runs **65.3% to 92.1%**. The ~9.2ms is per *solve*, so the share tracks how
expensive each solve is: a fresh 705-player pool is 92% CBC, and
`endgame-sole-bidder`, whose three solves are over a nearly-full roster, is 65%.
That is the same fact as C2's regression on that state seen from the other side —
a candidate that removes solves cannot help where a third of the cost is not in
the solves. "The solve is the whole cost" is true of the states that cost a
second and false of the ones that do not.

The solve count is not "~10" either, and not one distribution: over the 28
scenario subjects it lands on **2, 3, 9 or 10** (x4 / x8 / x8 / x8). Two is a
floor-priced player, short-circuiting on `with_at_min <= without`; three is a
must-have, where excluding him is Infeasible; nine and ten are the full search,
differing by where `physical_max_bid` puts the bracket.

**And the answer to the question is: about 1.6x is available, on the cases that
cost a second, and it was not judged worth the surface.** All three candidates
below reproduce the reference's marginal byte-for-byte on 168 subjects (28
scenario + 140 swept), which is the whole recommendation for the reason `compare`
documents. The fastest is C2, one min-cost solve in place of the probe search:
1.55-1.60x on the big-pool states, 1.45x overall, and **~0.8x** on
`endgame-sole-bidder`, where the reference already short-circuits in three
solves. (Two runs of that one gave 0.79x and 0.84x. Quoting either to two
figures would be false precision: the noise floor is 1-2% on the ~1100ms
subjects and wider on a 1238ms total, which is why `--null` exists.)
Shipping it would put a second MILP formulation, a confirm loop and two
float-epsilon subtleties on the hottest path in the app, each of which took a
wrong draft to get right; see `CHANGELOG.md`. The harness stays so the next
attempt starts here rather than from scratch.

**What a previous pass got wrong, recorded so it is not repeated.** Pool pruning
— keeping the top 50 by points per position, 705 candidates down to 150 — gives
a **byte-identical answer on every pinned scenario** and a 2-3.7x faster solve.
It is also silently wrong: every scenario sits at $1.9M+ of BOT budget per open
spot, and squeezing toward the reserve floor returns 1069 against a true 1076 at
$1.00M/spot, then `Infeasible` against true answers of 999 and 961 at $0.70M and
$0.60M. So **the scenario set is not the acceptance criterion** — a budget sweep
is, and `--sweep` is what runs it. Anything that agrees on the scenarios and has
not been swept is not yet known to be correct.

State safety: this imports `scenarios`, `optimizer` and `market` and NOT `main`,
which is what makes a `STATE_DIR` redirect unnecessary — `scenarios.load` builds
a state and never saves. That is asserted below rather than assumed, because
`main.py` hardcodes `STATE_DIR = "data/state"` with no env override, so anything
that imports and drives the app writes the OPERATOR'S state. If a future edit
here needs `main`, the redirect goes in first, exactly as `measure_ceiling.py`
and `measure_layout.py` do.

Usage:
    .venv/bin/python -m tests.measure_marginal                    # profile
    .venv/bin/python -m tests.measure_marginal --compare --faithful
    .venv/bin/python -m tests.measure_marginal --sweep            # the criterion
    .venv/bin/python -m tests.measure_marginal --quick --compare   # fast smoke
    .venv/bin/python -m tests.measure_marginal --scenario endgame-last-goalie

`--faithful` is worth the extra column whenever `build` has been touched: it
drives the model COPY through the production search and compares, so a copy that
has drifted is caught before any timing from it is believed.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from dataclasses import dataclass, field

import pulp

import market
import optimizer
import scenarios
from config import (
    BACKUP_BONUS,
    BACKUP_TARGETS,
    BENCH_WEIGHT,
    MIN_SALARY,
    MY_TEAM,
    SALARY_INCREMENT,
    STARTING_LINEUP,
)
from data_loader import build_initial_state
from price_model import load_model_params, predict_all_prices
from state import AuctionState, Player, TeamState, lineup_points
from tests.helpers import set_headroom

assert "main" not in sys.modules, (
    "measure_marginal imported main, which hardcodes STATE_DIR to the operator's "
    "real state. Redirect main.STATE_DIR to a temp dir before anything touches "
    "it, the way measure_ceiling.py does, or drop the import."
)


# ---------------------------------------------------------------------------
# Instrumentation
# ---------------------------------------------------------------------------


@dataclass
class SolveRecord:
    """One `solve_optimal_roster` call: what it was asked and what it cost."""

    forced_at: float | None
    excluded: bool
    status: str
    total_points: float
    wall_ms: float
    cbc_ms: float

    @property
    def overhead_ms(self) -> float:
        """Everything that is not CBC — model build, extraction, lineup_points."""
        return self.wall_ms - self.cbc_ms


@dataclass
class Probe:
    """Counts and times every solve underneath one `compute_marginal_value`.

    Patches two layers because the interesting split is between them:
    `optimizer.solve_optimal_roster` for the per-solve wall time, and
    `pulp.LpProblem.solve` for how much of it is CBC. `compute_marginal_value`
    calls the module-global name, so patching the attribute on `optimizer` is
    enough — no need to touch the function's own globals.
    """

    records: list[SolveRecord] = field(default_factory=list)
    _cbc_ms: float = 0.0
    _real_solve: object = None
    _real_pulp: object = None

    def __enter__(self) -> Probe:
        self._real_solve = optimizer.solve_optimal_roster
        self._real_pulp = pulp.LpProblem.solve

        def timed_pulp(prob, *a, **kw):
            t0 = time.perf_counter()
            try:
                return self._real_pulp(prob, *a, **kw)
            finally:
                self._cbc_ms += (time.perf_counter() - t0) * 1000

        def timed_solve(team, available, prices, **kw):
            before = self._cbc_ms
            t0 = time.perf_counter()
            sol = self._real_solve(team, available, prices, **kw)
            wall = (time.perf_counter() - t0) * 1000
            forced = kw.get("forced_players") or {}
            self.records.append(SolveRecord(
                forced_at=next(iter(forced.values()), None),
                excluded=bool(kw.get("excluded_players")),
                status=sol.status,
                total_points=sol.total_points,
                wall_ms=wall,
                cbc_ms=self._cbc_ms - before,
            ))
            return sol

        pulp.LpProblem.solve = timed_pulp
        optimizer.solve_optimal_roster = timed_solve
        return self

    def __exit__(self, *exc) -> None:
        optimizer.solve_optimal_roster = self._real_solve
        pulp.LpProblem.solve = self._real_pulp

    @property
    def wall_ms(self) -> float:
        return sum(r.wall_ms for r in self.records)

    @property
    def cbc_ms(self) -> float:
        return sum(r.cbc_ms for r in self.records)

    @property
    def cbc_share(self) -> float:
        return 100 * self.cbc_ms / self.wall_ms if self.wall_ms else 0.0


# ---------------------------------------------------------------------------
# States and subjects
# ---------------------------------------------------------------------------


def priced(state: AuctionState) -> tuple[dict[str, float], object]:
    """(market prices, MarketInfo) the way `main._recompute` builds them.

    Deliberately the same two calls the app makes rather than a private
    restatement, so a measurement here describes what the panel would render.
    """
    predictions = predict_all_prices(state.available_players, load_model_params())
    all_market = market.compute_all_market_prices(
        state.available_players, predictions, state.teams
    )
    return (
        {n: price for n, (price, _) in all_market.items()},
        market.compute_market_ceiling(state.teams),
    )


def subjects(state: AuctionState, prices: dict[str, float]) -> list[tuple[str, Player]]:
    """(role, player) pairs chosen by the ROLE each has to play, never by name.

    `players.csv` is replaced before every draft, so a literal name silently
    stops matching — `tests/test_no_literal_player_names.py` enforces this for
    the suite and the same reasoning applies to an instrument, which is worse off
    if anything: it fails by measuring the wrong thing rather than by going red.

    The floor player and the priciest are both here on purpose. They exercise
    different paths: a floor player short-circuits after two solves
    (`with_at_min.total_points <= without.total_points`), so averaging him in
    with a star hides the case that costs a second.
    """
    pool = state.available_players
    if not pool:
        return []
    out: list[tuple[str, Player]] = []
    for pos in ("F", "D", "G"):
        at_pos = [p for p in pool.values() if p.position == pos]
        if at_pos:
            out.append((f"top {pos}", max(at_pos, key=lambda p: p.projected_points)))
    out.append(("priciest", max(pool.values(), key=lambda p: prices.get(p.name, 0.0))))
    floor = [p for p in pool.values() if round(prices.get(p.name, 0.0), 1) <= MIN_SALARY]
    if floor:
        out.append(("floor", max(floor, key=lambda p: p.projected_points)))
    # De-duplicate while keeping the first role that claimed each player: on a
    # thin pool "top G" and "priciest" can be the same person, and measuring him
    # twice would double-count the expensive case.
    seen: set[str] = set()
    unique = []
    for role, p in out:
        if p.name not in seen:
            seen.add(p.name)
            unique.append((role, p))
    return unique


def states(quick: bool, only: str | None) -> list[tuple[str, AuctionState]]:
    """The fresh pool plus every scenario, or a subset."""
    if only:
        return [(only, scenarios.load(only))]
    out = [("fresh (/reset)", build_initial_state())]
    if not quick:
        out += [(name, scenarios.load(name)) for name in sorted(scenarios.SCENARIOS)]
    return out


# ---------------------------------------------------------------------------
# Phase 1: profile
# ---------------------------------------------------------------------------


def profile(label: str, state: AuctionState) -> list[dict]:
    """One row per (state, subject): solves, wall time, CBC share, answer."""
    prices, _ = priced(state)
    bot = state.teams[MY_TEAM]
    rows = []
    for role, player in subjects(state, prices):
        with Probe() as probe:
            value = optimizer.compute_marginal_value(
                player, bot, state.available_players, prices
            )
        rows.append({
            "state": label,
            "role": role,
            "player": player.name,
            "pos": player.position,
            "pts": player.projected_points,
            "price": prices.get(player.name, 0.0),
            "spots": bot.total_spots_remaining,
            "budget_per_spot": (
                bot.remaining_budget / bot.total_spots_remaining
                if bot.total_spots_remaining else 0.0
            ),
            "phys_max": bot.physical_max_bid,
            "marginal": value,
            "solves": len(probe.records),
            "wall_ms": probe.wall_ms,
            "cbc_ms": probe.cbc_ms,
            "cbc_pct": probe.cbc_share,
            "records": probe.records,
        })
    return rows


def report(rows: list[dict], verbose: bool) -> None:
    print()
    print(f"{'state':24} {'role':9} {'pos':3} {'$/spot':>7} {'physmax':>8} "
          f"{'marginal':>9} {'solves':>7} {'wall ms':>9} {'CBC%':>6}")
    print("-" * 96)
    for r in rows:
        print(f"{r['state'][:24]:24} {r['role']:9} {r['pos']:3} "
              f"{r['budget_per_spot']:7.2f} {r['phys_max']:8.1f} "
              f"{r['marginal']:9.1f} {r['solves']:7d} {r['wall_ms']:9.1f} "
              f"{r['cbc_pct']:6.1f}")
        if verbose:
            for rec in r["records"]:
                at = "excluded" if rec.excluded else (
                    f"forced@{rec.forced_at:.1f}" if rec.forced_at is not None
                    else "baseline")
                print(f"{'':24} {'':9}   {at:16} {rec.status:11} "
                      f"pts={rec.total_points:8.1f} "
                      f"{rec.wall_ms:7.1f}ms (cbc {rec.cbc_ms:6.1f}, "
                      f"other {rec.overhead_ms:5.1f})")

    if not rows:
        print("  no rows — every state had an empty pool?")
        return

    counts: dict[int, int] = {}
    for r in rows:
        counts[r["solves"]] = counts.get(r["solves"], 0) + 1
    total_wall = sum(r["wall_ms"] for r in rows)
    total_cbc = sum(r["cbc_ms"] for r in rows)
    worst = max(rows, key=lambda r: r["wall_ms"])

    print()
    print("solve-count distribution (a mean would hide the floor short-circuit):")
    for n in sorted(counts):
        print(f"  {n:2d} solves  x{counts[n]:<3} {'#' * counts[n]}")
    print(f"\nCBC share overall: {100 * total_cbc / total_wall:.1f}%  "
          f"({total_cbc:.0f}ms of {total_wall:.0f}ms across {len(rows)} subjects)")
    print(f"worst single subject: {worst['wall_ms']:.0f}ms — "
          f"{worst['role']} in {worst['state']} "
          f"({worst['solves']} solves, {worst['cbc_pct']:.1f}% CBC)")
# ---------------------------------------------------------------------------
# Phase 2: the candidates
#
# Both build the SAME model `solve_optimal_roster` builds. That copy is the
# hazard — a copy that has drifted makes every comparison below meaningless —
# so `--faithful` solves it head-to-head against the real thing on every
# subject and reports any disagreement before any timing is believed.
# ---------------------------------------------------------------------------

BUDGET_CONSTRAINT = "budget"


@dataclass
class Model:
    """The parts of a built roster model a candidate needs to reach into."""

    prob: pulp.LpProblem
    x: dict[str, pulp.LpVariable]
    s_fixed: dict[int, pulp.LpVariable]
    s_cand: dict[str, pulp.LpVariable]
    candidates: dict[str, Player]
    fixed_members: list[Player]
    spots: int
    budget: float
    forced_cost: float


def build(
    team: TeamState,
    available: dict[str, Player],
    prices: dict[str, float],
    excluded: set[str] | None = None,
    forced: dict[str, float] | None = None,
    *,
    objective: str = "max_points",
    points_floor: int | None = None,
) -> tuple[optimizer.MILPSolution | None, Model | None]:
    """A faithful copy of `solve_optimal_roster`'s build, with two knobs.

    `objective="min_cost"` swaps the maximise-points objective for
    minimise-roster-cost and drops the budget constraint, which is what C2 needs;
    `points_floor` adds `starter_pts >= floor`. Everything else is line-for-line
    the production build, including the needs reduction and both early returns.

    Returns (early_solution, None) when the production code would return without
    solving, else (None, model).
    """
    excluded = excluded or set()
    forced = forced or {}

    candidates = {
        name: p for name, p in available.items()
        if p.projected_points > 0 and name not in excluded and name not in forced
    }
    forced_cost = sum(forced.values())
    budget = team.remaining_budget - forced_cost
    spots = team.total_spots_remaining - len(forced)

    needs = dict(team.roster_needs)
    for name in forced:
        if name in available:
            pos = available[name].position
            if needs.get(pos, 0) > 0:
                needs[pos] -= 1

    forced_objs = [available[n] for n in forced if n in available]

    if spots == 0 and budget >= 0:
        return optimizer.MILPSolution(
            total_points=lineup_points(list(team.roster_players) + forced_objs),
            roster=[], total_cost=forced_cost,
            by_position={"F": [], "D": [], "G": []}, status="Optimal",
        ), None

    if spots < 0 or budget < 0 or budget < spots * MIN_SALARY:
        return optimizer.MILPSolution(
            total_points=lineup_points(team.roster_players),
            roster=[], total_cost=0.0,
            by_position={"F": [], "D": [], "G": []}, status="Infeasible",
        ), None

    total_needs = sum(needs.values())
    if total_needs > spots:
        excess = total_needs - spots
        for pos in sorted(needs, key=lambda p: -needs[p]):
            if excess <= 0:
                break
            reduction = min(needs[pos], excess)
            needs[pos] -= reduction
            excess -= reduction

    sense = pulp.LpMinimize if objective == "min_cost" else pulp.LpMaximize
    prob = pulp.LpProblem("roster_optimizer", sense)

    x = {n: pulp.LpVariable(f"x_{i}", cat="Binary")
         for i, n in enumerate(candidates)}
    fixed_members = list(team.roster_players) + forced_objs
    s_fixed = {j: pulp.LpVariable(f"sf_{j}", cat="Binary")
               for j in range(len(fixed_members))}
    s_cand = {}
    for i, n in enumerate(candidates):
        s_cand[n] = pulp.LpVariable(f"sc_{i}", cat="Binary")
        prob += s_cand[n] <= x[n]

    def starters_at(pos):
        return (
            pulp.lpSum(s_fixed[j] for j, p in enumerate(fixed_members)
                       if p.position == pos)
            + pulp.lpSum(s_cand[n] for n in candidates
                         if candidates[n].position == pos)
        )

    for pos, slots in STARTING_LINEUP.items():
        prob += starters_at(pos) <= slots

    starter_pts = (
        pulp.lpSum(p.projected_points * s_fixed[j]
                   for j, p in enumerate(fixed_members))
        + pulp.lpSum(candidates[n].projected_points * s_cand[n]
                     for n in candidates)
    )
    cost = pulp.lpSum(prices.get(n, MIN_SALARY) * x[n] for n in candidates)

    if objective == "min_cost":
        # No backup-credit terms: they exist only to shape the objective, and
        # this objective is cost. Dropping them removes 3 variables and 3
        # constraints rather than leaving them to be optimised over for nothing.
        prob += cost
        # No budget constraint either — that is the quantity being solved FOR.
        # Safe because `with_at_min` Optimal already proves a roster beating the
        # target exists at cost <= remaining_budget - MIN_SALARY, so the minimum
        # cannot exceed it.
    else:
        bk = {}
        for pos, target in BACKUP_TARGETS.items():
            bk[pos] = pulp.LpVariable(f"bk_{pos}", lowBound=0, upBound=target)
            rostered_pos = (
                sum(1 for p in fixed_members if p.position == pos)
                + pulp.lpSum(x[n] for n in candidates
                             if candidates[n].position == pos)
            )
            prob += bk[pos] <= rostered_pos - starters_at(pos)
        bench_pts = pulp.lpSum(
            candidates[n].projected_points * (x[n] - s_cand[n])
            for n in candidates
        )
        prob += starter_pts + BENCH_WEIGHT * bench_pts \
            + BACKUP_BONUS * pulp.lpSum(bk.values())
        # Named so C1 can move its RHS instead of rebuilding the model.
        prob += (cost <= budget, BUDGET_CONSTRAINT)

    if points_floor is not None:
        prob += starter_pts >= points_floor

    prob += pulp.lpSum(x[n] for n in candidates) == spots
    for pos, need in needs.items():
        if need > 0:
            prob += pulp.lpSum(
                x[n] for n in candidates if candidates[n].position == pos
            ) >= need

    return None, Model(prob, x, s_fixed, s_cand, candidates, fixed_members,
                       spots, budget, forced_cost)


def extract(model: Model, team: TeamState, prices: dict[str, float],
            forced: dict[str, float], available: dict[str, Player]):
    """`solve_optimal_roster`'s extraction, so the copy is comparable end to end."""
    status = pulp.LpStatus[model.prob.status]
    if status != "Optimal":
        return optimizer.MILPSolution(
            total_points=lineup_points(team.roster_players), roster=[],
            total_cost=0.0, by_position={"F": [], "D": [], "G": []}, status=status,
        )
    selected = [model.candidates[n] for n in model.candidates
                if model.x[n].varValue and model.x[n].varValue > 0.5]
    by_position: dict[str, list[Player]] = {"F": [], "D": [], "G": []}
    for p in selected:
        by_position[p.position].append(p)
    for name in forced:
        if name in available:
            by_position[available[name].position].append(available[name])
    return optimizer.MILPSolution(
        total_points=lineup_points(model.fixed_members + selected),
        roster=selected,
        total_cost=sum(prices.get(p.name, MIN_SALARY) for p in selected)
        + model.forced_cost,
        by_position=by_position,
        status="Optimal",
    )


def solve_via_copy(team, available, prices, excluded_players=None,
                   forced_players=None):
    """The copy, driven exactly like `solve_optimal_roster` — the faithfulness probe."""
    early, model = build(team, available, prices, excluded_players, forced_players)
    if early is not None:
        return early
    model.prob.solve(pulp.PULP_CBC_CMD(msg=0))
    return extract(model, team, prices, forced_players or {}, available)


def _floor_to_grid(value: float) -> float:
    """Largest $0.1M step at or below `value`, WITHOUT smoothing.

    The reference returns `lo`, the largest PROBED step that still improved, so a
    derived answer has to floor rather than round — naming a price the budget
    cannot pay is the one direction that matters.

    **There are two error scales here and the epsilon has to separate them**,
    which took two wrong drafts to see. The first smoothed with
    `round(value, 6)`; measured on `endgame-ceiling-binds`, market prices are
    themselves off-grid (`0.5000000106310717`), so a genuinely-$6.1M roster costs
    `6.100000041203467` and `remaining_budget - min_cost` comes out
    `2.9999999587965327`. Smoothing calls that 3.0. The reference calls it 2.9,
    because at a forced 3.0 the budget is `6.100000000000001` and the roster
    misses it by 4e-8 — the reference's 2.9 is itself a float artefact, but
    reproducing it is the job.

    The second draft dropped the epsilon entirely, and that broke the OTHER
    scale: `1.9 / 0.1` is `18.999999999999996`, so a bare floor turns an exact
    $1.9M into $1.8M. Every `endgame-last-goalie` subject in the budget sweep
    came back an increment low, all three candidates alike, because they share
    this function.

    So: representation error of a grid multiple is ~1e-16 relative, solver
    tolerance is ~4e-8, and 1e-9 on the QUOTIENT sits between them — it lifts
    `18.999999999999996` to 19 and leaves `29.999999587965327` at 29. The room
    above 1e-9 is real but finite: measured, the smallest epsilon that regresses
    the second case is **4.12e-7**, so 1e-8 and 1e-7 are both still safe and 1e-6
    is not. (An earlier draft of this said 1e-7 regressed. That was reasoned from
    the ~4e-8 error scale rather than measured, and it is wrong by a factor of
    four — the error is in the quotient, not the value.)
    """
    return math.floor(value / SALARY_INCREMENT + 1e-9) * SALARY_INCREMENT


def marginal_c1(player, team, available, prices, warm=True):
    """C1: build the search model ONCE, then move the budget RHS per probe.

    The eight probe solves differ in one number — `forced_cost` feeds
    `budget = remaining_budget - forced_cost` and nothing else — so rebuilding
    the model for each is ~9.2ms x 8 of pure waste, and CBC gets no chance to
    warm-start from the previous answer.

    `warmStart=True` is safe here: PuLP writes the start file through the same
    `create_tmp_files` call as the .lp and .sol (`pulp/apis/coin_api.py:156`), so
    it carries the per-solve `uuid4().hex` prefix and two concurrent solves cannot
    collide. That is the opposite of `keepFiles=True`, which puts every solve on
    one filename and returns silently wrong answers.
    """
    if team.total_spots_remaining <= 0:
        return 0.0

    cmd = pulp.PULP_CBC_CMD(msg=0, warmStart=warm)
    at_min_early, at_min = build(team, available, prices,
                                 forced={player.name: MIN_SALARY})
    if at_min_early is not None:
        with_at_min = at_min_early
    else:
        at_min.prob.solve(cmd)
        with_at_min = extract(at_min, team, prices, {player.name: MIN_SALARY},
                              available)
    if with_at_min.status != "Optimal":
        return MIN_SALARY

    wo_early, wo = build(team, available, prices, excluded={player.name})
    if wo_early is not None:
        without = wo_early
    else:
        wo.prob.solve(cmd)
        without = extract(wo, team, prices, {}, available)
    if without.status != "Optimal":
        return round(max(team.physical_max_bid, MIN_SALARY), 1)
    if with_at_min.total_points <= without.total_points:
        return MIN_SALARY

    if at_min is None:
        # He fills the last spot himself, so `build` returned early and there is
        # no model to search: every probe would take the same `spots == 0`
        # branch, which is Optimal iff `price <= remaining_budget` and whose
        # points do not depend on price at all. The reference reaches the answer
        # through `with_at_hi`; this reads it off directly. Returning MIN_SALARY
        # here — which is what the first draft did — priced every player in
        # `endgame-last-goalie` at the floor instead of the physical max.
        return round(min(round(team.physical_max_bid, 1),
                         _floor_to_grid(team.remaining_budget)), 1)

    # One model for every remaining probe. `at_min` is already built with this
    # player forced, so only its budget moves.
    model = at_min

    def probe(price: float) -> optimizer.MILPSolution:
        # Only the RHS moves — that is the whole trick, and `changeRHS` replaces
        # it rather than adjusting, so the MIN_SALARY this model was built with
        # is gone. `model.forced_cost` still holds it though, so the returned
        # `total_cost` is short by `price - MIN_SALARY`. Harmless because the
        # search reads `status` and `total_points` only; noted because a future
        # reader reaching for `total_cost` off a probe would get a wrong number
        # with no sign of it.
        model.prob.constraints[BUDGET_CONSTRAINT].changeRHS(
            team.remaining_budget - price
        )
        model.prob.solve(cmd)
        return extract(model, team, prices, {player.name: price}, available)

    lo, hi = MIN_SALARY, team.physical_max_bid
    at_hi = probe(round(hi, 1))
    if at_hi.status == "Optimal" and at_hi.total_points > without.total_points:
        return round(hi, 1)

    while hi - lo > SALARY_INCREMENT:
        mid = round(lo + (hi - lo) / 2, 1)
        if mid <= lo:
            mid = round(lo + SALARY_INCREMENT, 1)
        if mid >= hi:
            break
        at_mid = probe(mid)
        if at_mid.status == "Optimal" and at_mid.total_points > without.total_points:
            lo = mid
        else:
            hi = mid
    return round(lo, 1)


def marginal_c2(player, team, available, prices):
    """C2: one min-cost solve instead of the ~8-probe search.

    The search asks for the largest `P` with `V(remaining_budget - P) > B`, where
    `B` is `without.total_points` and `V` is non-decreasing in budget. So it is
    asking which budget is the least that still beats `B` — and that is a single
    minimisation: cheapest roster containing this player that reaches `B + 1`,
    then `P* = remaining_budget - min_cost`.

    `B + 1` rather than `B + epsilon` because `projected_points` is an `int` and
    `lineup_points` returns an `int` (`state.py:23`), so the strict `>` the
    reference uses is exactly `>= B + 1`. No tolerance to tune, which is one
    fewer thing to get wrong.

    Sound because `s` is free and slot-capped, so `starter_pts >= t` says "some
    lineup reaches t", and `lineup_points` IS the max over lineups.
    """
    if team.total_spots_remaining <= 0:
        return 0.0

    cmd = pulp.PULP_CBC_CMD(msg=0)
    at_min_early, at_min = build(team, available, prices,
                                 forced={player.name: MIN_SALARY})
    if at_min_early is not None:
        with_at_min = at_min_early
    else:
        at_min.prob.solve(cmd)
        with_at_min = extract(at_min, team, prices, {player.name: MIN_SALARY},
                              available)
    if with_at_min.status != "Optimal":
        return MIN_SALARY

    wo_early, wo = build(team, available, prices, excluded={player.name})
    if wo_early is not None:
        without = wo_early
    else:
        wo.prob.solve(cmd)
        without = extract(wo, team, prices, {}, available)
    if without.status != "Optimal":
        return round(max(team.physical_max_bid, MIN_SALARY), 1)
    if with_at_min.total_points <= without.total_points:
        return MIN_SALARY

    target = without.total_points + 1

    if team.total_spots_remaining - 1 == 0:
        # He fills the roster alone, so there is nothing left to buy and the
        # whole remaining budget is available to him. The reference reaches the
        # same answer through `with_at_hi`, whose points do not depend on price.
        min_cost = 0.0
    else:
        early, cheap = build(team, available, prices,
                            forced={player.name: MIN_SALARY},
                            objective="min_cost", points_floor=target)
        if early is not None:
            return MIN_SALARY
        cheap.prob.solve(cmd)
        if pulp.LpStatus[cheap.prob.status] != "Optimal":
            # Cannot happen if `with_at_min` was Optimal and beat `without` —
            # that is a feasible point for this problem. Reported rather than
            # swallowed, because it would mean the copy has drifted.
            return float("nan")
        min_cost = pulp.value(cheap.prob.objective)

    exact = team.remaining_budget - min_cost
    # Start ONE INCREMENT ABOVE the bound, then walk down. `min_cost` can come
    # back a shade high on CBC's tolerance, which floors the bound one step below
    # the true answer — and a walk-down cannot recover from starting low. Measured
    # in the budget sweep: `endgame-sole-bidder @0.70` returned 2.2 against a
    # reference 2.3 until the start moved up. Costs one extra confirm in the
    # common case and is what makes the result an upper bound rather than a guess.
    price = round(min(_floor_to_grid(exact) + SALARY_INCREMENT,
                      round(team.physical_max_bid, 1)), 1)

    # `min_cost` is a LOWER bound on any improving roster's cost, so `exact` is an
    # UPPER bound on the answer — but CBC's tolerance and off-grid market prices
    # put it within an increment either way, so the grid step above is a
    # candidate, not the answer. Confirm it with the reference's own test and walk
    # down until one passes. That is what makes this reproduce the reference by
    # CONSTRUCTION rather than by float luck: the first draft returned the
    # unconfirmed step and was one increment high on `endgame-ceiling-binds`.
    #
    # The confirms reuse `at_min`'s model, so each costs a solve and no rebuild.
    # Terminates because MIN_SALARY is already known to improve — that was
    # checked above.
    if at_min is not None:
        while price > MIN_SALARY:
            at_min.prob.constraints[BUDGET_CONSTRAINT].changeRHS(
                team.remaining_budget - price
            )
            at_min.prob.solve(cmd)
            at_price = extract(at_min, team, prices, {player.name: price}, available)
            if (at_price.status == "Optimal"
                    and at_price.total_points > without.total_points):
                break
            price = round(price - SALARY_INCREMENT, 1)
            C2_WALKDOWN.append(1)
    return max(round(price, 1), MIN_SALARY)


# How many increments each C2 call had to walk down from its min-cost bound.
# Counted rather than assumed, and the count is why the comment above it is
# phrased as it is: measured over the 140-subject budget sweep, C2 walked down
# **100 increments**, so the bound is NOT usually exact once budget-per-spot
# approaches the reserve floor — about 0.7 extra confirms per call. On the
# 28-subject scenario set it walked down 0. That difference is most of why C2's
# speedup is 2.2x on the scenarios and 1.45x on the sweep.
C2_WALKDOWN: list[int] = []

# ---------------------------------------------------------------------------
# Phase 2/3: compare
# ---------------------------------------------------------------------------


CANDIDATES = {
    "C1 warm": lambda *a: marginal_c1(*a, warm=True),
    "C1 cold": lambda *a: marginal_c1(*a, warm=False),
    "C2 mincost": marginal_c2,
}

# The reference, entered as a candidate. `--null` adds it, and it is the only
# thing that makes the SMALL numbers here believable: C1's 1.06x is meaningless
# unless a candidate that is literally the reference scores ~1.00x. Measured
# 2026-08-21 on a fresh pool, 6 repetitions per subject — spread 1.01-1.02x,
# stdev 6-8ms on ~1100ms, and the null candidate scored **1.00x** (0.99x on a
# 4-subject `--quick` run — the same 1-2% band, and read as a band rather than
# as a number, which is the mistake the 0.79x figure made). So the harness
# resolves a few percent and C1's 1.06x is signal. Re-run it before believing
# any future claim under ~1.1x.
#
# The floor is not one number, it scales with the total: on `endgame-sole-bidder`
# (1275ms across 15 cheap subjects) the null itself reads **1.04x**, so C1's
# 1.04x/1.13x on that state are not resolvable and only C2's direction is. Read
# the null column for the state you are quoting, never the aggregate.
NULL_CANDIDATE = {"null (=ref)": optimizer.compute_marginal_value}


def _timed(fn, *args) -> tuple[float, float]:
    t0 = time.perf_counter()
    value = fn(*args)
    return value, (time.perf_counter() - t0) * 1000


def compare(label: str, state: AuctionState, faithful: bool) -> list[dict]:
    """Reference against every candidate on the same subjects, in one run.

    One run matters: a laptop's CBC timings move by tens of percent between
    invocations, so a before/after taken from two runs is not a comparison.

    **Agreement is checked on the marginal alone, and that is the whole check —
    not a shortcut.** The plan for this work asked for agreement on what reaches
    the screen (`value_cap`, `max_bid`, `expected_stop`, `stop_status` and the
    BID/CAUTION/DROP verdict), on the pool-pruning precedent that agreeing on one
    number proves nothing. But `compute_bid_recommendation` takes
    `marginal_value` as an *argument* (`optimizer.py`) and `main.bid_check` passes
    it in from `_marginal_value`, so a candidate's only channel to any of those
    five fields is this one float. Equal float in, equal recommendation out, at
    every price — so comparing them here would be a check that cannot fail, which
    is the thing this project treats as worse than no check. Recorded rather than
    built. Note what that argument rests on: if `compute_bid_recommendation` ever
    grows a second dependency on the roster the marginal came from, this stops
    being true and the downstream comparison becomes real work.
    """
    prices, _ = priced(state)
    bot = state.teams[MY_TEAM]
    rows = []
    for role, player in subjects(state, prices):
        args = (player, bot, state.available_players, prices)
        ref, ref_ms = _timed(optimizer.compute_marginal_value, *args)
        row = {"state": label, "role": role, "player": player.name,
               "ref": ref, "ref_ms": ref_ms, "cands": {}}

        if faithful:
            # The copy driven through the production search. Any disagreement
            # here invalidates every candidate below it, so it is reported
            # first and separately.
            real = optimizer.solve_optimal_roster
            optimizer.solve_optimal_roster = solve_via_copy
            try:
                row["copy"], row["copy_ms"] = _timed(
                    optimizer.compute_marginal_value, *args)
            finally:
                optimizer.solve_optimal_roster = real

        for name, fn in CANDIDATES.items():
            row["cands"][name] = _timed(fn, *args)
        rows.append(row)
    return rows


def report_compare(rows: list[dict], faithful: bool) -> None:
    names = list(CANDIDATES)
    print()
    head = f"{'state':24} {'role':9} {'ref':>6} {'ms':>7}"
    if faithful:
        head += f" {'copy':>6}"
    for n in names:
        head += f" | {n:>10} {'ms':>7}"
    print(head)
    print("-" * len(head))

    bad: list[str] = []
    for r in rows:
        line = f"{r['state'][:24]:24} {r['role']:9} {r['ref']:6.1f} {r['ref_ms']:7.0f}"
        if faithful:
            ok = "" if r["copy"] == r["ref"] else " !!"
            line += f" {r['copy']:6.1f}{ok}"
            if r["copy"] != r["ref"]:
                bad.append(f"COPY DRIFT {r['state']}/{r['role']}: "
                           f"copy {r['copy']} vs reference {r['ref']}")
        for n in names:
            v, ms = r["cands"][n]
            mark = "" if v == r["ref"] else "!"
            line += f" | {v:9.1f}{mark} {ms:7.0f}"
            if v != r["ref"]:
                bad.append(f"{n} DISAGREES {r['state']}/{r['role']}: "
                           f"{v} vs reference {r['ref']}")
        print(line)

    print()
    ref_total = sum(r["ref_ms"] for r in rows)
    print(f"reference total: {ref_total:.0f}ms across {len(rows)} subjects")
    if faithful:
        # The copy driven through the PRODUCTION search, so this is the copy's
        # own build cost against `optimizer`'s on identical work. It was measured
        # and discarded until 2026-08-21, which mattered: if the copy's build were
        # materially dearer, every candidate speedup below would be understated by
        # that difference and the 1.06x figures would be unreadable.
        copy_total = sum(r["copy_ms"] for r in rows)
        print(f"  copy         {copy_total:7.0f}ms  ({ref_total / copy_total:5.2f}x)"
              f"  same search, model built here — a large gap understates every "
              f"candidate below")
    for n in names:
        tot = sum(r["cands"][n][1] for r in rows)
        wrong = sum(1 for r in rows if r["cands"][n][0] != r["ref"])
        verdict = "AGREES on all" if not wrong else f"DISAGREES on {wrong}"
        print(f"  {n:12} {tot:7.0f}ms  ({ref_total / tot:5.2f}x)  {verdict}")
    print(f"  C2 walked down {sum(C2_WALKDOWN)} increment(s) across "
          f"{len(rows)} subjects ({sum(C2_WALKDOWN) / max(len(rows), 1):.2f} per "
          f"call) — each one is an extra confirm solve")

    # Per state, because the aggregate hides the decision. A candidate that is
    # 1.6x on the states that cost a second and 0.8x on the ones that already
    # short-circuit is a different proposition from one that is 1.45x evenly, and
    # the first pass at this had to be recomputed by hand from the rows to see it.
    by_state: dict[str, list[float]] = {}
    for r in rows:
        key = r["state"].rsplit("@", 1)[0].strip()
        acc = by_state.setdefault(key, [0.0] + [0.0] * len(names) + [0.0])
        acc[0] += r["ref_ms"]
        for i, n in enumerate(names):
            acc[1 + i] += r["cands"][n][1]
        acc[-1] += 1
    print()
    print(f"per state (the aggregate above hides which cases actually cost a second):")
    print(f"  {'state':26} {'n':>3} {'ref ms':>8} "
          + " ".join(f"{n:>11}" for n in names))
    for key in sorted(by_state, key=lambda k: -by_state[k][0]):
        acc = by_state[key]
        speeds = " ".join(
            f"{acc[0] / acc[1 + i]:10.2f}x" if acc[1 + i] else f"{'-':>11}"
            for i in range(len(names))
        )
        print(f"  {key[:26]:26} {int(acc[-1]):3d} {acc[0]:8.0f} {speeds}")

    if bad:
        print(f"\n{len(bad)} disagreement(s) — a candidate that disagrees anywhere "
              f"is dead, per the pool-pruning precedent:")
        for b in bad:
            print(f"  {b}")
    else:
        print("\nEvery candidate byte-identical to the reference on every subject "
              "here. NOT yet a pass: run --sweep, which is the regime that caught "
              "pool pruning.")


# ---------------------------------------------------------------------------
# Phase 3: the budget sweep — the acceptance criterion
# ---------------------------------------------------------------------------


# Budget per OPEN SPOT, in $M. The first is roughly where every shipped scenario
# already sits, and the rest walk down toward the reserve floor (MIN_SALARY),
# which is the regime that caught pool pruning: byte-identical on every scenario,
# then 1069 against a true 1076 at 1.00, and Infeasible against 999 and 961 at
# 0.70 and 0.60. Reachable through play, not just by construction — buyout
# penalties, /trade-between and /adjust-salary all warn rather than refuse.
SWEEP_PER_SPOT = (1.90, 1.50, 1.00, 0.70, 0.60)


def sweep(label: str, state: AuctionState) -> list[dict]:
    """Re-run the comparison with BOT squeezed toward the reserve floor.

    Squeezes via `helpers.set_headroom`, which takes a team object — the by-code
    `helpers.squeeze` reaches into `main.auction_state`, and this module must not
    import `main`.
    """
    bot = state.teams[MY_TEAM]
    spots = bot.total_spots_remaining
    if spots <= 0:
        print(f"    {label}: BOT has no open spots, nothing to sweep")
        return []

    # Priced ONCE, outside the loop. Both ceilings are computed from opponents
    # only — BOT's own budget never sets its own cap — so squeezing BOT cannot
    # move a market price, and re-pricing per level is pure cost. Measured rather
    # than reasoned: 0 of 705 prices differ at 1.90, 1.00 and 0.60 per spot, and
    # the ceiling stays 11.4 throughout. An earlier draft re-priced inside the
    # loop with a comment claiming prices move with the squeeze; they do not.
    prices, _ = priced(state)
    rows = []
    for per_spot in SWEEP_PER_SPOT:
        set_headroom(bot, round(per_spot * spots, 1))
        for role, player in subjects(state, prices):
            args = (player, bot, state.available_players, prices)
            ref, ref_ms = _timed(optimizer.compute_marginal_value, *args)
            row = {"state": f"{label} @{per_spot:.2f}", "role": role,
                   "player": player.name, "ref": ref, "ref_ms": ref_ms,
                   "cands": {}}
            for name, fn in CANDIDATES.items():
                row["cands"][name] = _timed(fn, *args)
            rows.append(row)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scenario", help="measure one scenario instead of all")
    ap.add_argument("--quick", action="store_true",
                    help="fresh state only — the cheap smoke run")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print every solve, not just the per-subject total")
    ap.add_argument("--compare", action="store_true",
                    help="reference against every candidate, same run")
    ap.add_argument("--faithful", action="store_true",
                    help="with --compare, also check the model copy has not drifted")
    ap.add_argument("--null", action="store_true",
                    help="also run the reference AS a candidate — it must score "
                         "~1.00x, which is what makes a 1.06x claim believable")
    ap.add_argument("--sweep", action="store_true",
                    help="squeeze BOT toward the reserve floor — the acceptance "
                         "criterion, since the scenario set alone passed pool pruning")
    args = ap.parse_args()
    # `sweep` has no faithfulness column — it varies the budget, not the model —
    # so `--sweep --faithful` used to accept the flag and drop it. Same inert-flag
    # failure as `--null` below: refuse it rather than measure something the
    # operator did not ask for.
    if args.sweep and args.faithful:
        ap.error("--faithful is a --compare check; --sweep varies the budget "
                 "instead and has no copy column. Run them separately.")
    if args.null:
        CANDIDATES.update(NULL_CANDIDATE)
        # A candidate is only ever run by `compare`/`sweep`, so `--null` alone
        # printed a profile and no null figure — a flag that silently does
        # nothing, which is the whole failure class this instrument was written
        # during. Imply the cheaper of the two rather than erroring: `--sweep`
        # is the acceptance run and stays opt-in.
        args.compare = args.compare or not args.sweep

    rows: list[dict] = []
    for label, state in states(args.quick, args.scenario):
        print(f"  measuring {label} ...", flush=True)
        if args.sweep:
            rows += sweep(label, state)
        elif args.compare:
            rows += compare(label, state, args.faithful)
        else:
            rows += profile(label, state)
    if args.compare or args.sweep:
        report_compare(rows, args.faithful)
    else:
        report(rows, args.verbose)


if __name__ == "__main__":
    main()

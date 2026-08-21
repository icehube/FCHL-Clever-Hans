"""Measure where a cold `/bid-check` spends its second, and whether it has to.

NOT a test — pytest ignores it. Named `measure_marginal.py` for the same reason
`measure_ceiling.py` and `measure_layout.py` are: this is an instrument, not an
assertion. It answers "how many MILP solves does `compute_marginal_value`
actually run, how long is each, and does a cheaper formulation give the same
answer" — questions the suite cannot ask, because the answer is a property of a
whole state rather than of any one assertion.

The question exists because a cold `/bid-check` measured **935-1030ms** on
2026-08-19, 98-99% of it inside CBC, and the cost is paid roughly **once per
nomination** rather than once per draft: `main._marginal_cache` is keyed by
player and cleared at every epoch, so the first bid-check on a player after each
pick is cold and every later keystroke on the same player is ~9ms.

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
    .venv/bin/python -m tests.measure_marginal              # profile (phase 1)
    .venv/bin/python -m tests.measure_marginal --scenario endgame-last-goalie
    .venv/bin/python -m tests.measure_marginal --quick      # fresh state only
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field

import pulp

import market
import optimizer
import scenarios
from config import MIN_SALARY, MY_TEAM
from data_loader import build_initial_state
from price_model import load_model_params, predict_all_prices
from state import AuctionState, Player, TeamState

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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scenario", help="measure one scenario instead of all")
    ap.add_argument("--quick", action="store_true",
                    help="fresh state only — the cheap smoke run")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print every solve, not just the per-subject total")
    args = ap.parse_args()

    rows: list[dict] = []
    for label, state in states(args.quick, args.scenario):
        print(f"  measuring {label} ...", flush=True)
        rows += profile(label, state)
    report(rows, args.verbose)


if __name__ == "__main__":
    main()

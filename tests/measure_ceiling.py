"""Measure whether the market layer's ceiling ever changes a planning price.

NOT a test — pytest ignores it. Named `measure_ceiling.py` rather than
`test_ceiling.py` for the same reason `measure_layout.py` is: this is an
instrument, not an assertion. It answers "does `min(model_price, ceiling)` ever
differ from `model_price`, and if so at what point in a draft" — a question the
suite cannot ask, because the answer is a property of a whole auction rather
than of any one state.

Two ceilings, and conflating them is the mistake this exists to prevent:

* the **idle** ceiling (`compute_market_ceiling`, second-highest `physical_max_bid`
  across all 11 opponents) feeds `market_prices`, which is what the MILP plans on;
* the **live** ceiling (`compute_live_ceiling`, over the named bidders only) is
  built per request by `/bid-check` and is what the advisor's forecast uses.

A first pass at this measured the idle one and concluded the panel's "Should win
it" figure never appears. It does — routinely, from mid-draft. Both are reported
here so the next reader cannot repeat that.

State safety: `main.py` hardcodes `STATE_DIR = "data/state"` with no env
override, so importing and driving the app writes the OPERATOR'S state. This
redirects `main.STATE_DIR` to a temp dir before anything else touches it,
exactly as `tests/measure_layout.py` and `tests/conftest.py` do. Never remove
that: a measurement run must not be able to touch a live draft.

Usage:
    .venv/bin/python -m tests.measure_ceiling            # buy at market price
    .venv/bin/python -m tests.measure_ceiling --drain    # buy at what buyers can afford
    .venv/bin/python -m tests.measure_ceiling --every 20 # checkpoint interval
"""

import argparse
import itertools
import tempfile

# Redirect state BEFORE the app can load or save anything real.
import main

main.STATE_DIR = tempfile.mkdtemp(prefix="measure-ceiling-state-")

from fastapi.testclient import TestClient  # noqa: E402

from config import MAX_SALARY, MIN_SALARY, ROSTER_SIZE, MY_TEAM  # noqa: E402
from market import compute_live_ceiling  # noqa: E402
from state import AuctionState  # noqa: E402

# The idle ceiling is the SECOND-highest opponent max, so it only leaves
# MAX_SALARY once at most one opponent can still reach the cap. A team reaches
# it when `spendable_budget + MIN_SALARY >= MAX_SALARY`.
PIN_LINE = MAX_SALARY - MIN_SALARY


def _pinning_teams(state: AuctionState) -> list[tuple[str, float]]:
    """Opponents still able to bid the league maximum, with their spendable."""
    return [
        (t.code, round(t.spendable_budget, 1))
        for t in state.teams.values()
        if t.code != MY_TEAM and not t.is_done and t.spendable_budget >= PIN_LINE
    ]


def _live_survey(state: AuctionState) -> tuple[int, int, int, int]:
    """How often a live matchup's ceiling falls below the league maximum.

    Every 1-rival and 2-rival combination, not a sample: there are only 10 and
    45 of them, and which pairs bind is the whole question — an average would
    hide that it is always the same rich teams pinning it.
    """
    codes = [c for c, t in state.teams.items() if c != MY_TEAM and not t.is_done]
    solo = sum(
        compute_live_ceiling([MY_TEAM, c], state.teams) < MAX_SALARY for c in codes
    )
    pairs = list(itertools.combinations(codes, 2))
    duo = sum(
        compute_live_ceiling([MY_TEAM, a, b], state.teams) < MAX_SALARY
        for a, b in pairs
    )
    return solo, len(codes), duo, len(pairs)


def _next_buyer(state: AuctionState) -> tuple[str | None, int]:
    """The still-drafting team with the most holes, or None when the draft ends."""
    open_teams = [
        (t.code, ROSTER_SIZE - len(t.roster_players))
        for t in state.teams.values()
        if len(t.roster_players) < ROSTER_SIZE and not t.is_done
    ]
    if not open_teams or not state.available_players:
        return None, 0
    return max(open_teams, key=lambda x: x[1])


def _price(state: AuctionState, code: str, spots: int, name: str, drain: bool) -> float:
    """What this buyer pays — the two spending models the flags select between.

    `room` is the commissioner's rule, not ours: the league software refuses a
    bid that would leave a team unable to fill a full roster, so a run that
    ignored it would be modelling an auction that cannot happen.
    """
    team = state.teams[code]
    room = round(max(MIN_SALARY, team.remaining_budget - (spots - 1) * MIN_SALARY), 1)
    # Model price is what the tool itself recommends, so the default run answers
    # "what happens if everyone behaves the way the tool predicts". --drain
    # answers the opposite: what if the money actually gets spent.
    wanted = room if drain else round(main.market_prices.get(name, MIN_SALARY), 1)
    return max(MIN_SALARY, min(wanted, room, MAX_SALARY))


def _health(state: AuctionState) -> list[str]:
    """Anything a full-length auction could break that a ceiling number hides.

    `tests/test_dry_run.py` stops at 40 picks and points here for full length,
    so these have to be checked rather than assumed — a run that reported a
    tidy ceiling curve while the MILP had gone Infeasible at pick 90 would be
    worse than no run at all.
    """
    bad: list[str] = []
    if main.milp_solution is not None and main.milp_solution.status != "Optimal":
        bad.append(f"MILP {main.milp_solution.status}")
    # Over-cap is LEGAL in this app — the league permits it and buyouts resolve
    # it (owner decision 2026-08-06), so this is not an app invariant. It is a
    # harness one: `_price` clamps every bid to `room`, so a negative budget
    # here means the simulated auction did something the commissioner would
    # have refused, and the run's numbers describe an auction that cannot happen.
    bad += [
        f"{t.code} budget {t.remaining_budget:.1f}"
        for t in state.teams.values()
        if t.remaining_budget < 0
    ]
    bad += [
        f"{t.code} roster {len(t.roster_players)}"
        for t in state.teams.values()
        if len(t.roster_players) > ROSTER_SIZE
    ]
    return bad


def run(drain: bool, every: int) -> None:
    label = "DRAIN — buyers pay what they can afford" if drain else \
            "BASELINE — buyers pay the tool's market price"
    print(f"\n{'=' * 74}\n{label}\n{'=' * 74}")

    with TestClient(main.app) as client:
        client.post("/reset")
        state = main.auction_state
        start = {t.code: t.remaining_budget for t in state.teams.values()}
        at_cap = picks = 0
        seen: set[float] = set()
        first_bind = None
        steps: list[tuple[int, float]] = []
        unhealthy: list[str] = []

        print(f"  {'pick':>5} {'idle':>7} {'pinning teams':>14} {'1-rival':>9} {'2-rival':>9}")
        while True:
            code, spots = _next_buyer(state)
            if code is None:
                break

            ceiling = main.market_info.market_ceiling
            seen.add(round(ceiling, 1))
            # Every value the ceiling actually took, with the pick it took it
            # on. Reading the step sequence off the checkpoint rows instead is
            # how "at the floor by pick 60" got into the docs when the measured
            # answer was pick 43 — the checkpoints were 20 apart.
            if not steps or steps[-1][1] != round(ceiling, 1):
                steps.append((picks, round(ceiling, 1)))
            if ceiling >= MAX_SALARY:
                at_cap += 1
            elif first_bind is None:
                first_bind = (picks, round(ceiling, 1))

            unhealthy += [f"pick {picks}: {b}" for b in _health(state)]

            if picks % every == 0:
                solo, n_solo, duo, n_duo = _live_survey(state)
                print(f"  {picks:>5} {ceiling:>6.1f}M {len(_pinning_teams(state)):>10} of 10"
                      f" {solo:>4}/{n_solo:<4} {duo:>4}/{n_duo:<4}")

            name = max(
                state.available_players.values(), key=lambda p: p.projected_points
            ).name
            client.post("/assign", data={
                "player": name, "team": code,
                "salary": str(_price(state, code, spots, name, drain)),
            })
            picks += 1

        spent = sum(start[t.code] - t.remaining_budget for t in state.teams.values())
        total = sum(start.values())
        print(f"\n  picks                 : {picks}")
        print(f"  idle ceiling at MAX   : {at_cap}/{picks}"
              f"  ({100 * at_cap / picks:.0f}% of the draft)")
        print(f"  distinct idle ceilings: {sorted(seen)}")
        print(f"  ceiling steps         : {' -> '.join(f'{c}M@{p}' for p, c in steps)}")
        print(f"  first pick it bound   : {first_bind or 'never'}")
        print(f"  league cap            : spent ${spent:.1f}M of ${total:.1f}M"
              f"  ({100 * (total - spent) / total:.0f}% unspent)")
        left = sorted(
            ((t.code, round(t.remaining_budget, 1)) for t in state.teams.values()),
            key=lambda x: -x[1],
        )
        print(f"  richest at the end    : {left[:3]}")
        print(f"  still able to bid max : {_pinning_teams(state) or 'none'}")

        # The app still has to WORK after 165 picks, not just report a number.
        # These are what test_dry_run.py's docstring points here for, so they
        # run every time rather than living in a throwaway triage script.
        after = {
            path: client.get(path).status_code
            for path in ("/", "/nominate", "/buyout-indicators")
        }
        broken = {p: c for p, c in after.items() if c != 200}
        # Count first, then a sample — one Infeasible MILP tends to persist for
        # every pick after it, and 100 near-identical lines would bury the pick
        # number that matters. Never print the sample without the count.
        health = "clean" if not unhealthy else f"{len(unhealthy)} problems, first: {unhealthy[:3]}"
        print(f"  health during the run : {health}")
        print(f"  endpoints after it    : {broken or 'all 200 ' + str(list(after))}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--drain", action="store_true",
                    help="buyers pay what they can afford, not the model price")
    ap.add_argument("--every", type=int, default=55, help="checkpoint interval")
    args = ap.parse_args()
    print(f"state dir (temp): {main.STATE_DIR}")
    run(args.drain, args.every)

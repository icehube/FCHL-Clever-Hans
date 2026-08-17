"""Read the spend curve a finished draft already recorded.

NOT a test — pytest ignores it. Named `measure_spend.py` for the same reason
`measure_layout.py` and `measure_ceiling.py` are: an instrument, not an
assertion. Unlike both of those it **never drives the app**. It reads a state
JSON and nothing else — no `main` import, no `STATE_DIR`, no `TestClient` — so
it structurally cannot touch a live draft, which is a stronger guarantee than
the temp-dir redirect those two rely on.

It exists because two `BACKLOG.md` engine findings were parked on "needs a real
draft's numbers" when the numbers were already being written down. Every
`/assign` logs a `TransactionRecord` carrying `salary`, `model_price` AND
`market_price`, captured before the player leaves the pool, and the log is
serialized with the state. Nothing read it back.

**The question this answers is sharper than `measure_ceiling.py`'s.** That
instrument counts a bind as `ceiling < MAX_SALARY`. But the ceiling only changes
a *planning price* when `market_price < model_price`, i.e. `ceiling <
model_price`. The two numbers are not interchangeable and this one is what "the
market layer did something" actually means.

Measured on the same drain run 2026-08-17: 133 by the first definition, **122**
by this one — and all 122 start at pick 43, when the ceiling reached the $0.5M
floor. The $7.3M and $4.5M steps before it capped nothing, because a top-down
draft has already sold the players they would have caught. Note the counts come
out close, which is NOT what the reasoning that motivated this file predicted:
the argument was that 563 of 705 players sit at the floor so most ceilings
change nothing, and that is true right up until the ceiling itself reaches the
floor and caps essentially everything. Right mechanism, wrong magnitude.

Usage:
    .venv/bin/python -m tests.measure_spend                       # live state
    .venv/bin/python -m tests.measure_spend path/to/state.json    # an archive
"""

import json
import statistics
import sys
from pathlib import Path

from state import TransactionRecord, _transaction_from_dict

DEFAULT_STATE = Path("data/state/auction_state.json")

# Allowlist, never denylist — the project rule, and it bites here specifically.
# A trade's `salary` is not a clearing price, a buyout's is not a purchase, and
# `/trade-between` writes f"{source}→{dest}" into `team_code`, so per-team
# aggregation over anything but a draft is meaningless.
PURCHASE = "draft"


def load_records(path: Path) -> list[TransactionRecord]:
    """Parse the transaction log out of a saved state.

    Uses `state._transaction_from_dict` rather than reading the nine keys here.
    Hand-copying them is the hazard CLAUDE.md names for the backfills — a wrong
    key would silently do nothing, which is worse than failing — and that helper
    already handles save files written before `nhl_team` existed.
    """
    data = json.loads(path.read_text())
    return [_transaction_from_dict(d) for d in data.get("transaction_log", [])]


def summarize(records: list[TransactionRecord]) -> dict:
    """Everything the report prints, computed once. Pure — no I/O.

    Split out from the CLI on purpose. `measure_layout.py` rotted silently for
    two days because nothing could test it, and `measure_ceiling.py`'s numbers
    now appear in four documents with nothing checking them. A pure function
    costs one test file and removes that whole class of problem for this one.
    """
    picks = [r for r in records if r.transaction_type == PURCHASE]
    if not picks:
        return {"picks": 0, "skipped_types": len(records)}

    # A model price of 0 is MISSING DATA, not a cheap player: `_log_transaction`
    # defaults both prices to 0. Counting those would invent binds wholesale,
    # since `market_price=0 < model_price=0` is False but `0 < 5` is True the
    # moment only one of them is missing. Excluded and reported, never dropped
    # quietly.
    priced = [r for r in picks if r.model_price > 0]
    bound = [r for r in priced if r.market_price < r.model_price]

    paid_vs_plan = [r.salary - r.market_price for r in priced]
    paid_vs_model = [r.salary - r.model_price for r in priced]

    by_team: dict[str, float] = {}
    for r in picks:
        by_team[r.team_code] = round(by_team.get(r.team_code, 0.0) + r.salary, 1)

    return {
        "picks": len(picks),
        "priced": len(priced),
        "skipped_types": len(records) - len(picks),
        "unpriced": len(picks) - len(priced),
        "span": (picks[0].timestamp[:16], picks[-1].timestamp[:16]),
        "teams": len(by_team),
        "spent": round(sum(r.salary for r in picks), 1),
        # The headline: how often the ceiling actually moved a planning price.
        "ceiling_changed_a_price": len(bound),
        # Indexed into `picks`, NOT `priced`: the report prints this as "pick
        # N", so it has to be a real draft ordinal. Off `priced` it silently
        # shifts by however many unpriced records came before it — zero on the
        # synthetic runs, which is exactly why that bug would have survived.
        "first_bind": next(
            (i for i, r in enumerate(picks)
             if r.model_price > 0 and r.market_price < r.model_price),
            None,
        ),
        "bind_gap_max": round(max((r.model_price - r.market_price for r in bound), default=0.0), 1),
        # 0.0 not 0: `sum` of an empty generator is an int, and the report
        # formats these with a bare {} so the type leaks to the screen.
        "bind_gap_total": round(float(sum(r.model_price - r.market_price for r in bound)), 1),
        "paid_over_plan": sum(1 for d in paid_vs_plan if d > 0),
        "paid_vs_plan_mean": round(statistics.fmean(paid_vs_plan), 2),
        "paid_vs_model_mean": round(statistics.fmean(paid_vs_model), 2),
        "paid_vs_model_abs": round(
            statistics.fmean(abs(d) for d in paid_vs_model), 2
        ),
        "by_team": dict(sorted(by_team.items(), key=lambda kv: -kv[1])),
        "curve": _spend_curve(picks),
    }


def _spend_curve(picks: list[TransactionRecord]) -> list[tuple[int, float]]:
    """Cumulative league spend at each tenth of the draft.

    Deciles rather than every pick: the shape is the question (does the money
    go early or late), and 165 rows of running total answers it worse than 10.
    """
    out, running = [], 0.0
    step = max(1, len(picks) // 10)
    for i, r in enumerate(picks, start=1):
        running += r.salary
        if i % step == 0 or i == len(picks):
            out.append((i, round(running, 1)))
    return out


def report(path: Path) -> None:
    if not path.exists():
        print(f"no state file at {path}")
        return

    s = summarize(load_records(path))
    print(f"\n{'=' * 74}\n{path}\n{'=' * 74}")

    if not s["picks"]:
        # Says so plainly rather than printing a table of zeros, which would
        # read as a finding. This is the state of a fresh install.
        extra = f" ({s['skipped_types']} non-draft records)" if s["skipped_types"] else ""
        print(f"  no draft picks in the transaction log{extra} — nothing to measure")
        return

    print(f"  picks                 : {s['picks']}  over {s['span'][0]} .. {s['span'][1]}")
    print(f"  teams that bought     : {s['teams']}")
    print(f"  league spend          : ${s['spent']}M")
    if s["skipped_types"]:
        print(f"  non-draft records     : {s['skipped_types']} (trades/buyouts, excluded)")
    if s["unpriced"]:
        print(f"  picks with no model $ : {s['unpriced']} (excluded from the ceiling stats)")

    print(f"\n  --- did Layer 2 change a planning price? ---")
    first = "never" if s["first_bind"] is None else f"pick {s['first_bind']}"
    print(f"  market < model        : {s['ceiling_changed_a_price']}/{s['priced']}"
          f"  (first: {first})")
    print(f"  largest single cut    : ${s['bind_gap_max']}M")
    print(f"  total cut from model  : ${s['bind_gap_total']}M")

    print(f"\n  --- what buyers actually paid ---")
    print(f"  above the plan        : {s['paid_over_plan']}/{s['priced']} picks")
    print(f"  salary - market price : {s['paid_vs_plan_mean']:+.2f}M mean")
    print(f"  salary - model price  : {s['paid_vs_model_mean']:+.2f}M mean,"
          f" {s['paid_vs_model_abs']:.2f}M mean absolute")

    print(f"\n  --- spend curve (cumulative $M by pick) ---")
    print("  " + "  ".join(f"{n}:{v}" for n, v in s["curve"]))

    print(f"\n  --- by team ---")
    print("  " + "  ".join(f"{k} ${v}M" for k, v in s["by_team"].items()))


if __name__ == "__main__":
    report(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_STATE)

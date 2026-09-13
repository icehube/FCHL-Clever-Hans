"""Replay a finished draft and reconstruct what the market ceiling was doing.

NOT a test — pytest ignores it, the same convention `measure_spend.py`,
`measure_ceiling.py`, `measure_drivers.py`, `measure_layout.py` and
`measure_marginal.py` follow: an instrument, not an assertion.

**Why this exists beside `measure_spend.py`, which already reads a draft log.**
That file answers "did the ceiling change the planning price of a player who
SOLD" — `market_price < model_price` over the transaction log. It is the only
thing a log can answer on its own, and it is NOT the question the MILP cares
about. The optimizer plans over the **whole remaining pool** every time it runs,
so the ceiling can be cutting the price of six unsold stars for fifty picks and
`measure_spend.py` will report nothing, correctly, because none of them sold at
a capped price. Nothing had ever measured the pool-wide quantity on real data;
`measure_ceiling.py` does it per pick but only over an auction it simulates
itself. This file is the missing corner: real draft, whole pool.

**How it gets there.** It rebuilds the starting state from the pool CSV and
drives it forward through the saved log, computing
`market.compute_market_ceiling` before each pick and counting how many of the
players still available are capped at that moment. What it costs, relative to
`measure_spend.py`, is that file's strongest property: this one has to load a
pool and import the engine (`data_loader`, `state`, `market`, `price_model`).
**It still never imports `main`** — no `STATE_DIR`, no `TestClient`, no
`_save_state` — so it cannot write over a live draft, but do not read that as
the same structural guarantee `measure_spend.py` has. It is narrower on purpose
and the difference is one import away.

**The replay is only worth anything if it lands where the draft landed**, so it
checks itself two ways and prints both:

  * every drafted player's model price recomputed from the pool must equal the
    price the app LOGGED at the time (exact on all 139 picks of the 2026-09-13
    draft) — this is what catches being pointed at the wrong CSV, and it catches
    it far more sharply than a pool-size count, which several wrong pools pass;
  * every team's final roster count, minors count and remaining budget must
    equal the saved end state.

A divergence there is the only way this instrument can lie, which is why the
fidelity block prints unconditionally rather than only on failure.

Usage:
    .venv/bin/python -m tests.measure_replay                       # live state
    .venv/bin/python -m tests.measure_replay path/to/state.json
    .venv/bin/python -m tests.measure_replay path/to/state.json --pool data/x.csv
"""

import argparse
import json
import statistics
from dataclasses import dataclass
from pathlib import Path

import data_loader
import market
from config import BUYOUT_PENALTY_RATE, MAX_SALARY, MY_TEAM
from price_model import load_model_params, predict_all_prices
from state import (
    AuctionState,
    ChangeRecord,
    PlayerOnRoster,
    TransactionRecord,
    _change_from_dict,
    _transaction_from_dict,
)

DEFAULT_STATE = Path("data/state/auction_state.json")

# Multipliers applied to every model price in the sensitivity sweep. 1.0 is the
# pool as it is; the rest ask "would the ceiling have bound on a pool that
# priced dearer at the top", which is not a hypothetical — `players-25.csv`
# carries no prior salary on any biddable, so `has_lag` is 0 pool-wide and the
# model's top compresses to $6.40M against the live pool's $11.36M (1.77x).
# A result measured on a compressed pool has to carry its own robustness check
# or the next reader has to take this file's prose on trust.
SCALES = (1.0, 1.25, 1.5, 1.77, 2.0, 2.5)

PURCHASE = "draft"


@dataclass(frozen=True)
class PickRow:
    """One pick, with the state of the ceiling at the moment before it landed."""

    pick: int  # 1-BASED ordinal, the convention measure_spend/measure_ceiling share
    ceiling: float
    pool: int
    dearest: float  # dearest model price still available
    model_price: float  # what the app LOGGED for the player who sold
    salary: float
    capped: tuple[int, ...]  # pool prices the ceiling cut, one count per SCALES entry


def pool_for(state_path: Path) -> Path:
    """Guess the pool CSV a saved state was drafted from.

    The state file does not record it, so this inverts `main._default_state_dir`
    — `data/state-<stem>/` came from `data/<stem>.csv`, and a bare `data/state/`
    from the default pool. Inverted here rather than imported because importing
    `main` is the one thing this file will not do (see the module docstring).

    A wrong guess is not silent: `replay` compares every recomputed model price
    against the logged one, and a different pool disagrees immediately.
    """
    stem = state_path.parent.name
    if stem.startswith("state-"):
        return Path("data") / f"{stem[len('state-'):]}.csv"
    return Path(data_loader.PLAYERS_CSV)


def load_events(path: Path) -> list[TransactionRecord | ChangeRecord]:
    """Both logs, interleaved by timestamp.

    **The change log is not optional and leaving it out fails silently.**
    `team-done` moves a team in and out of the set `compute_market_ceiling`
    reads, so a reconstruction without it holds a finished team's dead budget in
    the ceiling for the rest of the draft — and the 2026-09-13 draft flipped
    that switch 19 times, four of them back to still-drafting, which is why the
    reconstructed ceiling RISES at four points. `move-to-minors` changes
    `total_spots_remaining` and therefore `physical_max_bid`, which is the
    ceiling's only input.

    Parsed through `state`'s own `_from_dict` helpers rather than by reading the
    keys here: the same rule `measure_spend.py` records, and a hand-copied key
    that stopped matching would do nothing rather than raise.
    """
    data = json.loads(path.read_text())
    events: list[TransactionRecord | ChangeRecord] = [
        _transaction_from_dict(d) for d in data.get("transaction_log", [])
    ]
    events += [_change_from_dict(d) for d in data.get("change_log", [])]
    # Stable, so records sharing a timestamp keep their logged order — which
    # `/trade-execute` relies on, writing all twelve of its records at once.
    events.sort(key=lambda r: r.timestamp)
    return events


def change_subject(rec: ChangeRecord) -> str:
    """The player a ChangeRecord is about, parsed out of its description.

    **This is a coupling with `main._log_change`'s wording, and it is the
    fragile part of this file.** `ChangeRecord` carries only kind, team, a
    timestamp and a human sentence, so the name exists nowhere else. The five
    grammars `main.py` emits today:

        team-done        "{code} marked as done" | "... as still drafting"
        toggle-bench     "{name} → bench" | "{name} → active"
        move-to-minors   "{name} → minors"
        move-to-roster   "{name} → active"
        adjust-salary    "{name}: $1.0M → $2.0M"

    `adjust-salary` is the trap: it contains ` → ` like the others, so the
    obvious `rsplit(" → ")` returns the name WITH the old salary glued on and
    then silently fails to find that player. It is also the one kind the
    2026-09-13 draft contains none of, so the data cannot catch it —
    `tests/test_measure_replay.py` pins all five strings instead.
    """
    if rec.kind == "adjust-salary":
        return rec.description.split(": $", 1)[0]
    return rec.description.rsplit(" → ", 1)[0]


def _take(state: AuctionState, name: str) -> PlayerOnRoster | None:
    """Lift a player off whichever roster currently holds him."""
    for team in state.teams.values():
        if team.find_player(name):
            return team.remove_player(name)
    return None


def _apply(
    state: AuctionState,
    rec: TransactionRecord | ChangeRecord,
    pending: dict[str, PlayerOnRoster],
) -> None:
    """Move the state forward by one logged record. Raises on anything unknown."""
    if isinstance(rec, ChangeRecord):
        team = state.teams[rec.team_code]
        if rec.kind == "team-done":
            team.is_done = rec.description.endswith("marked as done")
        elif rec.kind == "toggle-bench":
            team.set_bench(change_subject(rec), rec.description.endswith("bench"))
        elif rec.kind == "move-to-minors":
            team.send_to_minors(change_subject(rec))
        elif rec.kind == "move-to-roster":
            team.recall_from_minors(change_subject(rec))
        elif rec.kind == "adjust-salary":
            new = float(rec.description.rsplit("$", 1)[1].rstrip("M"))
            team.adjust_salary(change_subject(rec), new)
        else:
            raise ValueError(f"unknown change kind {rec.kind!r}")
        return

    name = rec.player_name
    if rec.transaction_type == PURCHASE:
        player = state.available_players.pop(name)
        state.teams[rec.team_code].add_acquired_player(
            PlayerOnRoster.from_pool(player, rec.salary)
        )
    elif rec.transaction_type == "trade":
        # /trade-between writes f"{source}→{dest}" into team_code — the reason
        # the project rule says allowlist this field rather than treat it as a
        # team code (CLAUDE.md, the `_view_team` bullet).
        source, dest = rec.team_code.split("→")
        state.teams[dest].add_acquired_player(state.teams[source].remove_player(name))
    elif rec.transaction_type == "trade_out":
        held = state.teams[rec.team_code].find_player(name)
        if held:
            pending[name] = state.teams[rec.team_code].remove_player(name)
    elif rec.transaction_type == "trade_in":
        # /trade-execute logs an out and an in per player, all on one timestamp,
        # and NOT always out-first: the 2026-09-13 blockbuster logged BOT's
        # incoming Larkin ahead of JHN's outgoing one. So take him from wherever
        # he is, fall back to what a trade_out stashed, and only then rebuild
        # him from the record — which loses his projected points, harmless for a
        # ceiling that is pure budget arithmetic but worth not doing by default.
        player = _take(state, name) or pending.pop(name, None) or PlayerOnRoster(
            name=name,
            position=rec.position,
            group="3",
            salary=rec.salary,
            projected_points=0,
            nhl_team=rec.nhl_team,
        )
        state.teams[rec.team_code].add_acquired_player(player)
    elif rec.transaction_type == "buyout":
        team = state.teams[rec.team_code]
        team.remove_player(name)
        team.penalties += rec.salary * BUYOUT_PENALTY_RATE
    else:
        raise ValueError(f"unknown transaction type {rec.transaction_type!r}")


def replay(
    state: AuctionState,
    events: list[TransactionRecord | ChangeRecord],
    model: dict[str, float],
    scales: tuple[float, ...] = SCALES,
) -> tuple[list[PickRow], list[str]]:
    """Drive `state` through `events`, sampling the ceiling before every pick.

    Mutates the state it is given — hand it a freshly built one. Returns the
    per-pick rows and a list of records it could not apply, which the report
    prints: a swallowed failure here would show up only as a budget that drifts,
    and "the ceiling was higher than it really was" is exactly the wrong way for
    this instrument to be wrong.

    Model prices are computed ONCE, before the first pick, because they are
    static by construction: `pos_rank` is frozen against the draft-time pool and
    `team_probability` does not move, so a player's model price is the same at
    pick 1 and pick 139. Recomputing per pick would silently re-rank a shrinking
    pool, which is the thing `price_model` freezes `pos_rank` to prevent.
    """
    rows: list[PickRow] = []
    problems: list[str] = []
    pending: dict[str, PlayerOnRoster] = {}
    pick = 0

    for rec in events:
        if isinstance(rec, TransactionRecord) and rec.transaction_type == PURCHASE:
            pick += 1
            info = market.compute_market_ceiling(state.teams, MY_TEAM)
            # Through compute_market_price, never a hand-written min(): the
            # floor_demand branch answers MIN_SALARY when no opponent can bid at
            # all, and inlining the min would miss it. It did not fire in the
            # 2026-09-13 draft (the ceiling never left 1.3M), so the data cannot
            # catch a reimplementation either.
            capped = tuple(
                sum(
                    1
                    for name in state.available_players
                    if market.is_capped(
                        model[name] * k,
                        market.compute_market_price(model[name] * k, info),
                    )
                )
                for k in scales
            )
            rows.append(
                PickRow(
                    pick=pick,
                    ceiling=info.market_ceiling,
                    pool=len(state.available_players),
                    dearest=max((model[n] for n in state.available_players), default=0.0),
                    model_price=rec.model_price,
                    salary=rec.salary,
                    capped=capped,
                )
            )
        try:
            _apply(state, rec, pending)
        except (KeyError, ValueError) as exc:
            kind = getattr(rec, "transaction_type", None) or getattr(rec, "kind", "?")
            problems.append(f"{rec.timestamp} {kind}: {type(exc).__name__}: {exc}")

    return rows, problems


def ceiling_steps(rows: list[PickRow]) -> list[tuple[int, float]]:
    """Every distinct value the ceiling took, with the pick it took it on.

    Same shape and the same 1-based ordinals as `measure_ceiling.py`'s
    `ceiling steps` line, deliberately: quoting a checkpoint row instead of a
    step is how "by pick 60" got into the rules file when the answer was 44.
    """
    steps: list[tuple[int, float]] = []
    for r in rows:
        if not steps or abs(steps[-1][1] - r.ceiling) > 1e-9:
            steps.append((r.pick, r.ceiling))
    return steps


def price_mismatches(
    rows: list[PickRow], model: dict[str, float], picks: list[TransactionRecord]
) -> list[str]:
    """Picks whose recomputed model price disagrees with the logged one.

    The pool check that actually works. A pool-size count passes for any CSV of
    the right length; this one is per player and exact to a hundredth, and on
    the right pool it comes back empty.
    """
    out = []
    for rec in picks:
        got = model.get(rec.player_name)
        if got is None:
            out.append(f"{rec.player_name}: not in the pool at all")
        elif abs(got - rec.model_price) > 0.005:
            out.append(f"{rec.player_name}: pool ${got:.2f}M vs logged ${rec.model_price:.2f}M")
    return out


def fidelity(state: AuctionState, saved: AuctionState) -> list[tuple[str, tuple, tuple]]:
    """Replayed end state against the saved one, per team: (roster, minors, budget).

    Takes the saved state as a real `AuctionState` rather than the raw dict, so
    `remaining_budget` is compared through the same property the app computes it
    with. Reading the JSON lists by hand would give roster and minors counts and
    leave the budget uncheckable — and the budget is the only one of the three
    the ceiling actually depends on, so a column that merely printed it would be
    decoration where the check has to be.
    """
    out = []
    for code in sorted(state.teams):
        mine, theirs = state.teams[code], saved.teams[code]
        out.append((
            code,
            (len(mine.roster_players), len(mine.minor_players), round(mine.remaining_budget, 2)),
            (len(theirs.roster_players), len(theirs.minor_players), round(theirs.remaining_budget, 2)),
        ))
    return out


def summarize(rows: list[PickRow], scales: tuple[float, ...] = SCALES) -> dict:
    """Everything the report prints, computed once. Pure — no I/O."""
    if not rows:
        return {"picks": 0}

    headroom = [(r.ceiling / r.dearest, r.pick) for r in rows if r.dearest > 0]
    sweep = []
    for i, k in enumerate(scales):
        hits = [r.pick for r in rows if r.capped[i]]
        sweep.append(
            {
                "scale": k,
                "picks": len(hits),
                "first": hits[0] if hits else None,
                "worst": max(r.capped[i] for r in rows),
            }
        )
    return {
        "picks": len(rows),
        "steps": ceiling_steps(rows),
        "at_max": sum(1 for r in rows if r.ceiling >= MAX_SALARY - 1e-9),
        # The headline, and the thing measure_spend.py cannot see: scales[0] is
        # the pool as it is, so this counts picks at which the ceiling was
        # cutting SOMEBODY's planning price, sold or not.
        "changed_a_pool_price": sweep[0]["picks"],
        "first": sweep[0]["first"],
        "worst": sweep[0]["worst"],
        "headroom_median": round(statistics.median(h for h, _ in headroom), 2) if headroom else 0.0,
        "headroom_min": min(headroom) if headroom else (0.0, 0),
        "headroom_under_2x": sum(1 for h, _ in headroom if h < 2),
        "sweep": sweep,
    }


def report(path: Path, pool: Path | None = None, scales: tuple[float, ...] = SCALES) -> None:
    if not path.exists():
        print(f"no state file at {path}")
        return

    # The pool is checked first because it depends only on the PATH, and a
    # derived guess that missed is the likeliest way to be here — answering that
    # with a parse error about the state file would send the reader to the wrong
    # problem.
    csv = Path(pool) if pool else pool_for(path)
    if not csv.exists():
        print(f"no pool CSV at {csv} — pass --pool")
        return

    # Same degradation contract as `measure_spend.report`: a `.corrupt` state is
    # exactly what someone points this at — `lifespan` renames a file it cannot
    # parse rather than deleting it — so name the file and return rather than
    # raise. Wider than that file's tuple by two, because this one also runs the
    # JSON through `AuctionState.from_json`, which walks nested structures and
    # answers a wrong SHAPE with AttributeError or TypeError rather than
    # KeyError (a list where the teams dict should be, say).
    try:
        saved = AuctionState.from_json(path.read_text())
        events = load_events(path)
    except (json.JSONDecodeError, KeyError, OSError, AttributeError, TypeError) as exc:
        print(f"could not read {path}: {type(exc).__name__}: {exc}")
        return

    data_loader.PLAYERS_CSV = str(csv)
    state = data_loader.build_initial_state(players_path=str(csv))
    model = {
        name: pred.expected_price
        for name, pred in predict_all_prices(
            state.available_players, load_model_params()
        ).items()
    }
    started = len(state.available_players)

    picks = [
        r
        for r in events
        if isinstance(r, TransactionRecord) and r.transaction_type == PURCHASE
    ]
    rows, problems = replay(state, events, model, scales)

    print(f"\n{'=' * 74}\n{path}\n  pool: {csv}\n{'=' * 74}")
    if not rows:
        extra = f" ({len(events)} other records)" if events else ""
        print(f"  no draft picks in the transaction log{extra} — nothing to replay")
        return

    s = summarize(rows, scales)
    print(f"  picks                 : {s['picks']}  of a {started}-player pool")
    print(f"  idle ceiling at MAX   : {s['at_max']}/{s['picks']}"
          f"  (below the cap on {s['picks'] - s['at_max']})")
    print(f"  ceiling steps         : "
          + " -> ".join(f"{c:.1f}M@{p}" for p, c in s["steps"]))

    print("\n  --- did Layer 2 cut ANY pool price? (what measure_spend.py cannot see) ---")
    first = "never" if s["first"] is None else f"pick {s['first']}"
    print(f"  picks with >=1 capped : {s['changed_a_pool_price']}/{s['picks']}  (first: {first})")
    print(f"  most capped at once   : {s['worst']}")
    print(f"  headroom over dearest : {s['headroom_median']:.2f}x median,"
          f" {s['headroom_min'][0]:.2f}x worst (pick {s['headroom_min'][1]}),"
          f" under 2x on {s['headroom_under_2x']}")

    print("\n  --- sensitivity: a pool that priced dearer at the top ---")
    for row in s["sweep"]:
        first = "never" if row["first"] is None else f"pick {row['first']}"
        print(f"  x{row['scale']:<5} : {row['picks']:3}/{s['picks']} picks capped something"
              f"  (first: {first}, worst {row['worst']})")

    print("\n  --- replay fidelity (a divergence here invalidates everything above) ---")
    bad = price_mismatches(rows, model, picks)
    print(f"  model price vs logged : {len(bad)} mismatch(es) over {len(picks)} picks")
    for line in bad[:5]:
        print(f"      {line}")
    if problems:
        print(f"  records not applied   : {len(problems)}")
        for line in problems[:5]:
            print(f"      {line}")
    else:
        print(f"  records not applied   : 0 of {len(events)}")
    for code, got, want in fidelity(state, saved):
        flag = "" if got == want else "   <-- MISMATCH"
        print(f"      {code}  roster {got[0]:3} (saved {want[0]:3})"
              f"  minors {got[1]:3} (saved {want[1]:3})"
              f"  budget ${got[2]:6.2f}M (saved ${want[2]:6.2f}M){flag}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("state", nargs="?", default=str(DEFAULT_STATE))
    ap.add_argument("--pool", help="pool CSV (default: derived from the state directory)")
    args = ap.parse_args()
    report(Path(args.state), Path(args.pool) if args.pool else None)

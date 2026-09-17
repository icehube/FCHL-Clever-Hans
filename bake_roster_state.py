#!/usr/bin/env python3
"""Write the live roster placement back into the data files, so `/reset` keeps it.

Pre-draft prep — recalling a prospect, demoting a keeper, entering a cap penalty
the league handed down — is done in the UI, and the UI writes it to
`data/state/auction_state.json`. `POST /reset` does not read that file. It calls
`build_initial_state()`, which rebuilds all eleven teams from `data/players.csv`
plus `data/fchl_teams.json`, so every prep move made this session is discarded.
None of `main.py`'s three `_backfill_*` helpers saves it either: they fill a
blank NHL club, set `is_keeper`, and copy a logo. **Nothing moves a player
between the roster and the minors.**

That is correct behaviour — reset means reset — but it leaves no way to return to
a *prepared* baseline after testing. This script is that way: it reads the live
state and writes the two durable facts back into the data files, so the next
`/reset` rebuilds INTO your prep instead of away from it.

It carries exactly two things and refuses to invent a third:

    which list a player is in  ->  players.csv STATUS   (MINOR, else START)
    TeamState.penalties        ->  fchl_teams.json penalty

It never adds a row and never deletes one. Removing a player from the league —
the no-NHL-contract case — is a deliberate hand edit of `players.csv`, and it
stays that way so it cannot happen as a side effect of a bake.

Two things it CANNOT carry, reported on every run rather than dropped silently:

- **Bench flags.** There is no bench column in `CANONICAL_COLUMNS` and there is
  nowhere to put one. This costs nothing: `is_bench` reaches no engine module —
  `lineup_points` scores the best 12F/6D/2G from every roster player regardless,
  and none of `total_salary`, `roster_count`, `spendable_budget` or
  `physical_max_bid` reads it (see `TeamState.set_bench`'s docstring). It is a
  display flag plus the precondition `send_to_minors` enforces.
- **`is_done`.** A team that has stopped drafting is an auction fact, not a
  roster fact, and re-setting it is one click.

**Do not run `convert_fchl_online.py` against a file this has baked.** That
converter derives STATUS from the contract group (2/3 -> START, A-F -> MINOR)
and would overwrite every placement here. It builds a NEW season's pool from the
league export; this edits the season already in progress.

Run:
    python bake_roster_state.py              # dry run: print the diff
    python bake_roster_state.py --write      # apply it
"""

import argparse
import csv
import json
import re
import sys

import data_loader
from convert_legacy_players import CANONICAL_COLUMNS, _PLACEHOLDER_TEAMS

DEFAULT_STATE = "data/state/auction_state.json"
DEFAULT_PLAYERS = "data/players.csv"
DEFAULT_TEAMS = "data/fchl_teams.json"

# The two STATUS values this script writes. Anything else on a real team code is
# read as an ACTIVE keeper by `load_players` — `is_minor = status == "MINOR"` and
# nothing else — so a typo here is silent, which is the whole reason this is a
# script and not a hand edit.
MINORS = "MINOR"
ACTIVE = "START"


def load_state(path: str) -> dict:
    """The saved auction state, or a clear error naming what was found instead."""
    with open(path) as f:
        state = json.load(f)
    if not isinstance(state, dict) or "teams" not in state:
        raise ValueError(
            f"{path} is not an auction state file: expected a JSON object with a "
            f"'teams' key, found {type(state).__name__} "
            f"{sorted(state)[:6] if isinstance(state, dict) else ''}"
        )
    return state


def require_pre_draft(state: dict) -> None:
    """Refuse to bake anything but a pre-draft prep state.

    A drafted player sits in `acquired_players` and a buyout leaves a nameless
    float in `penalties`. Baking either would write the auction itself into the
    pool file: picks would come back as keepers with `is_keeper=True`, priced as
    though they had always been owned, and the penalty would be indistinguishable
    from one the league handed down. Both checks, because they fail
    independently — `/load-scenario` builds acquired players programmatically
    and logs no transaction at all.
    """
    txns = state.get("transaction_log") or []
    if txns:
        kinds = ", ".join(sorted({t.get("transaction_type", "?") for t in txns}))
        raise ValueError(
            f"state holds {len(txns)} transaction(s) ({kinds}) — this is a draft "
            "in progress, not pre-draft prep. Baking it would write draft picks "
            "into players.csv as keepers. Nothing was written."
        )
    bought = {
        code: len(t.get("acquired_players") or [])
        for code, t in state["teams"].items()
        if t.get("acquired_players")
    }
    if bought:
        detail = ", ".join(f"{c} {n}" for c, n in sorted(bought.items()))
        raise ValueError(
            f"state holds acquired players ({detail}) with an empty transaction "
            "log — a loaded scenario, not pre-draft prep. Nothing was written."
        )


def state_placement(state: dict) -> dict[str, tuple[str, str]]:
    """Every rostered player -> (team code, STATUS).

    Keyed on the name the STATE holds, which is the disambiguated one:
    `_disambiguated_names` renames every member of a colliding group, so a
    keeper called `Elias Pettersson (VAN D)` is stored under that string.
    `csv_names` below derives the same key from the CSV, so the two sides meet.
    """
    placement: dict[str, tuple[str, str]] = {}
    for code, team in state["teams"].items():
        for p in team.get("keeper_players") or []:
            placement[p["name"]] = (code, ACTIVE)
        for p in team.get("minor_players") or []:
            placement[p["name"]] = (code, MINORS)
    return placement


def read_players(path: str) -> tuple[list[dict], list[str]]:
    """The pool rows in file order, plus the disambiguated name for each.

    Returned together because the names are positional: `_disambiguated_names`
    decides the ordinal suffix from file order, so a name only means anything
    beside the row it was computed from.
    """
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    return rows, data_loader._disambiguated_names(rows)


def plan_changes(
    rows: list[dict], names: list[str], placement: dict[str, tuple[str, str]]
) -> list[tuple[int, str, str, str, str]]:
    """Rows whose STATUS the state disagrees with: (index, name, team, was, now).

    Raises rather than guessing when the two sides describe different worlds.
    A player the state holds and the pool does not means one of them has been
    edited since the other was built, and there is no safe reading of that.
    """
    seen: set[str] = set()
    changes = []
    for i, (row, name) in enumerate(zip(rows, names)):
        fchl = row["FCHL TEAM"].strip()
        # Biddables keep STATUS "" — `load_players` gates the pool branch on it,
        # and anything else there is dropped without a word.
        if fchl in _PLACEHOLDER_TEAMS or fchl == "":
            continue
        if name not in placement:
            raise ValueError(
                f"'{name}' is on {fchl} in the pool file but on no team in the "
                "state — the two files describe different leagues. Nothing was "
                "written."
            )
        seen.add(name)
        team, status = placement[name]
        if team != fchl:
            raise ValueError(
                f"'{name}' is on {fchl} in the pool file and {team} in the state. "
                "Only a trade moves a player between teams, and a trade logs a "
                "transaction, so this state should not exist. Nothing was written."
            )
        was = row["STATUS"].strip()
        if was != status:
            changes.append((i, name, team, was, status))

    missing = sorted(set(placement) - seen)
    if missing:
        raise ValueError(
            f"{len(missing)} player(s) are on a roster in the state but have no "
            f"row in the pool file: {', '.join(missing[:5])}"
            f"{' ...' if len(missing) > 5 else ''}. Nothing was written."
        )
    return changes


def plan_penalties(state: dict, teams_path: str) -> list[tuple[str, float, float]]:
    """Teams whose penalty the state disagrees with: (code, was, now)."""
    with open(teams_path) as f:
        meta = json.load(f)
    out = []
    for code, team in state["teams"].items():
        if code not in meta:
            continue
        was = float(meta[code].get("penalty", 0.0))
        now = round(float(team.get("penalties", 0.0)), 1)
        if abs(was - now) >= 0.05:
            out.append((code, was, now))
    return sorted(out)


def write_penalties(path: str, changes: list[tuple[str, float, float]]) -> None:
    """Rewrite only the `penalty` numbers, in place, line by line.

    `json.dump` would reformat the whole file — `colors` is hand-written on one
    line per team — turning a two-number edit into a 100-line diff that hides
    what actually changed. So the team block is located by its key and only the
    one line inside it is rewritten.
    """
    want = {code: now for code, _was, now in changes}
    if not want:
        return
    lines = open(path).read().splitlines(keepends=True)
    current = None
    done: set[str] = set()
    for i, line in enumerate(lines):
        header = re.match(r'^\s*"([A-Z]{2,4})":\s*\{', line)
        if header:
            current = header.group(1)
            continue
        if current in want:
            m = re.match(r'^(\s*"penalty":\s*)([-\d.]+)(,?\s*)$', line)
            if m:
                lines[i] = f"{m.group(1)}{want[current]}{m.group(3)}"
                done.add(current)
    stale = sorted(set(want) - done)
    if stale:
        raise ValueError(
            f"could not find a 'penalty' line for {', '.join(stale)} in {path}. "
            "Nothing was written."
        )
    with open(path, "w") as f:
        f.writelines(lines)


def line_terminator(path: str) -> str:
    """The terminator the file already uses.

    `data/players.csv` is CRLF -- it comes out of the league's web export -- and
    `csv.DictWriter` defaults to CRLF regardless, so this matched by luck rather
    than by design. A pool file saved LF would have been rewritten end to end,
    turning a three-line bake into a 1268-line diff with the real change buried
    in it. Measured the hard way on 2026-09-17, on a hand edit that did exactly
    that.
    """
    with open(path, "rb") as f:
        head = f.readline()
    return "\r\n" if head.endswith(b"\r\n") else "\n"


def write_players(path: str, rows: list[dict]) -> None:
    terminator = line_terminator(path)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=CANONICAL_COLUMNS, lineterminator=terminator
        )
        writer.writeheader()
        writer.writerows(rows)


def report_dropped(state: dict) -> None:
    """Name what this bake cannot carry, so the gap is never silent."""
    benched = sum(
        1
        for t in state["teams"].values()
        for p in (t.get("keeper_players") or []) + (t.get("acquired_players") or [])
        if p.get("is_bench")
    )
    done = sorted(c for c, t in state["teams"].items() if t.get("is_done"))
    if benched:
        print(
            f"\nnot carried: {benched} bench flag(s) — there is no bench column in "
            "the schema. They reach no engine module, so this costs no number; "
            "re-bench after a reset only to stage a demotion."
        )
    if done:
        print(f"not carried: is_done on {', '.join(done)} — re-toggle after a reset.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--players", default=DEFAULT_PLAYERS)
    ap.add_argument("--teams", default=DEFAULT_TEAMS)
    ap.add_argument(
        "--write",
        action="store_true",
        help="apply the changes; without it nothing is written",
    )
    args = ap.parse_args(argv)

    state = load_state(args.state)
    require_pre_draft(state)

    rows, names = read_players(args.players)
    changes = plan_changes(rows, names, state_placement(state))
    penalties = plan_penalties(state, args.teams)

    print(f"state:   {args.state}")
    print(f"players: {args.players}")
    print(f"teams:   {args.teams}")

    if changes:
        print(f"\n{len(changes)} roster placement(s) to bake:")
        for _i, name, team, was, now in changes:
            print(f"    {name:32} {team}  {was or '(blank)':6} -> {now}")
    else:
        print("\nno roster placements differ.")

    if penalties:
        print(f"\n{len(penalties)} penalty change(s):")
        for code, was, now in penalties:
            print(f"    {code}  ${was:.1f}M -> ${now:.1f}M")
    else:
        print("\nno penalties differ.")

    report_dropped(state)

    if not changes and not penalties:
        print("\nnothing to do.")
        return 0
    if not args.write:
        print("\ndry run — nothing written. Re-run with --write to apply.")
        return 0

    for i, _name, _team, _was, now in changes:
        rows[i]["STATUS"] = now
    if changes:
        write_players(args.players, rows)
    write_penalties(args.teams, penalties)
    print(
        f"\nwrote {len(changes)} placement(s) to {args.players} and "
        f"{len(penalties)} penalty change(s) to {args.teams}. "
        "Commit them, then POST /reset returns here."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

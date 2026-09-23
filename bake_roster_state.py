#!/usr/bin/env python3
"""Write the live roster placement back into the data files, so `/reset` keeps it.

Pre-draft prep — recalling a prospect, demoting a keeper — is done in the UI,
and the UI writes it to the saved state (`data/state/auction_state.json` for the
default pool; see `data_loader.default_state_dir`). `POST /reset` does not read that file. It calls
`build_initial_state()`, which rebuilds all eleven teams from `data/players.csv`
plus `data/fchl_teams.json`, so every prep move made this session is discarded.
None of `main.py`'s four `_backfill_*` helpers saves it either: they fill a
blank NHL club, set `is_keeper`, copy a logo, and repair a legacy snapshot's
model inputs. **Nothing moves a player between the roster and the minors.**

That is correct behaviour — reset means reset — but it leaves no way to return to
a *prepared* baseline after testing. This script is that way: it reads the live
state and writes the one durable fact back into the pool file, so the next
`/reset` rebuilds INTO your prep instead of away from it.

It carries exactly ONE thing:

    which list a player is in  ->  players.csv STATUS   (MINOR, else START)

**Penalties are not carried, and neither are salaries.** They were, until
2026-09-22 — the penalty half wrote `TeamState.penalties` into
`fchl_teams.json` — and it could only ever do harm. No UI action produces a
pre-draft penalty this script would accept: the one real writer is
`execute_buyout`, which logs a transaction, and `require_pre_draft` refuses any
state holding one. So a state that disagrees with the teams file about a penalty
is a state that PREDATES a hand edit of it, and writing the state's figure back
reverts the edit — measured, SHF's hand-entered $2.8M baked to $0.0M. Worse,
`fchl_teams.json` is shared by every pool while `players.csv` is not, so baking
an alternate pool's state wrote ITS stale penalties into the live draft's teams
file. A salary carried the same way has the same trap: `/adjust-salary` and a
hand edit of `SALARY` are indistinguishable from here. Both are hand edits of
the data files, and this script's job for them is to NOTICE: every disagreement
is printed with both figures, so a correction made in the UI is not lost
silently and a stale state is not trusted silently.

It never adds a row and never deletes one. Removing a player from the league —
the no-NHL-contract case — is a deliberate hand edit of `players.csv`, and it
stays that way so it cannot happen as a side effect of a bake.

Two more things it CANNOT carry, reported on every run rather than dropped
silently:

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
and would overwrite every placement here, so since 2026-09-22 it lists them (and
any hand-corrected salary it would revert) and refuses unless `--force`. It
builds a NEW season's pool from the league export;
this edits the season already in progress. Converting a fresh export means
re-baking after, from a state that still holds the prep.

Run:
    python bake_roster_state.py              # dry run: print the diff
    python bake_roster_state.py --write      # apply it
"""

import argparse
import csv
import io
import json
import os
import sys

import data_loader
from convert_legacy_players import _PLACEHOLDER_TEAMS

DEFAULT_TEAMS = "data/fchl_teams.json"


def default_paths() -> tuple[str, str]:
    """(state file, pool file) for the pool the SERVER would load.

    Read at call time rather than bound as argparse defaults at import, and
    through the same two variables the server reads, so `FCHL_PLAYERS_CSV` on
    its own points both halves at the alternate pool's files — never the live
    draft's state baked into another pool, or the reverse.
    """
    state_dir = os.environ.get("FCHL_STATE_DIR") or data_loader.default_state_dir()
    return os.path.join(state_dir, "auction_state.json"), data_loader.PLAYERS_CSV


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

    A drafted player sits in `acquired_players`, and baking one would write the
    auction itself into the pool file: picks would come back as keepers with
    `is_keeper=True`, priced as though they had always been owned. Both checks,
    because they fail independently — `/load-scenario` builds acquired players
    programmatically and logs no transaction at all.
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
    # A row with more fields than the header — a trailing comma is the usual
    # way — parses with a `None` key. The app tolerates it; a rewrite cannot
    # reproduce it, so refuse before planning anything.
    ragged = [i + 2 for i, row in enumerate(rows) if None in row]
    if ragged:
        raise ValueError(
            f"{path} has more fields than its header on line(s) "
            f"{', '.join(map(str, ragged[:5]))} — a trailing comma? Fix the file "
            "first. Nothing was written."
        )
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
                "transaction, so either the state was saved from a DIFFERENT "
                "pool file than this one, or the pool file was edited after the "
                "state was saved. Nothing was written."
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


def penalty_disagreements(
    state: dict, teams_path: str
) -> list[tuple[str, float, float]]:
    """Teams whose penalty the state disagrees with: (code, file, state).

    Reported, never written — see the module docstring for why a disagreement
    here means a stale state far more often than a new fact.

    Compared at two decimals, not the salary check's one: a penalty is HALF a
    salary, so it lives on a $0.05M grid. Rounding one side to one decimal put
    17 of the 245 legal penalties up to $12.25M a full 0.05 off themselves — a
    $0.75M buyout entered in both places reported "file $0.8M state $0.8M" as
    a disagreement, blaming a stale state.
    """
    with open(teams_path) as f:
        meta = json.load(f)
    out = []
    for code, team in state["teams"].items():
        if code not in meta:
            continue
        was = round(float(meta[code].get("penalty", 0.0)), 2)
        now = round(float(team.get("penalties", 0.0)), 2)
        if abs(was - now) >= 0.005:
            out.append((code, was, now))
    return sorted(out)


def salary_disagreements(
    rows: list[dict], names: list[str], state: dict
) -> list[tuple[str, str, float, float]]:
    """Rostered players whose salary the state disagrees with: (name, team, file, state).

    The pre-draft way to get one is `/adjust-salary` correcting an export error,
    and that correction is what a reset would otherwise throw away without a
    word — found 2026-09-22 by baking a state with one in it, which exited 0
    and printed nothing about salary at all. Reported rather than carried for
    the reason penalties are.
    """
    salary = {
        p["name"]: float(p.get("salary", 0.0))
        for team in state["teams"].values()
        for key in ("keeper_players", "minor_players")
        for p in team.get(key) or []
    }
    out = []
    for row, name in zip(rows, names):
        if name not in salary:
            continue
        was = float(row["SALARY"] or 0.0)
        if abs(was - salary[name]) >= 0.05:
            out.append((name, row["FCHL TEAM"].strip(), was, round(salary[name], 1)))
    return out


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


def render_players(path: str, rows: list[dict]) -> str:
    """The pool file's new text, in its own header order and line terminator.

    Rendered in memory, so a row `DictWriter` cannot write raises BEFORE the
    file is opened. Until 2026-09-22 this wrote straight into the pool file:
    it opened it `"w"` — truncating it — wrote the header, and only then met a
    bad row, so one trailing comma left the live pool at its header and five
    rows. An empty pool looks exactly like a finished draft on screen.
    """
    with open(path, newline="") as f:
        fieldnames = next(csv.reader(f))
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=fieldnames, lineterminator=line_terminator(path))
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def write_atomically(path: str, text: str) -> None:
    """`.tmp` then `os.replace`, the pattern `main._save_state` uses."""
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def report_dropped(
    state: dict,
    penalties: list[tuple[str, float, float]],
    salaries: list[tuple[str, str, float, float]],
    teams_path: str,
    players_path: str,
) -> None:
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
    if penalties:
        print(
            f"\nnot carried: {len(penalties)} penalty disagreement(s) with {teams_path}. "
            "Penalties are hand-edited there; no pre-draft UI action can produce "
            "one, so the likelier reading is that the STATE is older than the file:"
        )
        for code, was, now in penalties:
            print(f"    {code}  file ${was:.2f}M   state ${now:.2f}M")
    if salaries:
        print(
            f"\nnot carried: {len(salaries)} salary disagreement(s) with "
            f"{players_path}. If one is a correction made with /adjust-salary, "
            "hand-edit SALARY in the pool file or a reset will undo it:"
        )
        for name, team, was, now in salaries:
            print(f"    {name:32} {team}  file ${was:.1f}M   state ${now:.1f}M")


def run(argv: list[str] | None = None) -> int:
    """The bake itself. Raises ValueError on any refusal, having written nothing."""
    default_state, default_players = default_paths()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state", default=default_state)
    ap.add_argument("--players", default=default_players)
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
    penalties = penalty_disagreements(state, args.teams)
    salaries = salary_disagreements(rows, names, state)

    print(f"state:   {args.state}")
    print(f"players: {args.players}")
    print(f"teams:   {args.teams} (read only)")

    if changes:
        print(f"\n{len(changes)} roster placement(s) to bake:")
        for _i, name, team, was, now in changes:
            print(f"    {name:32} {team}  {was or '(blank)':6} -> {now}")
    else:
        print("\nno roster placements differ.")

    report_dropped(state, penalties, salaries, args.teams, args.players)

    if not changes:
        print("\nnothing to write.")
        return 0

    for i, _name, _team, _was, now in changes:
        rows[i]["STATUS"] = now
    # Rendered BEFORE anything is opened for writing, so a row the writer
    # cannot serialise refuses here with the file untouched.
    text = render_players(args.players, rows)
    if not args.write:
        print("\ndry run — nothing written. Re-run with --write to apply.")
        return 0

    write_atomically(args.players, text)
    print(
        f"\nwrote {len(changes)} placement(s) to {args.players}. "
        "Commit it, then POST /reset returns here."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """`run`, with a refusal printed as a sentence and exit status 1.

    Every refusal is a ValueError whose message already says what was wrong and
    that nothing was written; a traceback in front of it only buries that.
    """
    try:
        return run(argv)
    except ValueError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Build the canonical `players.csv` from an FCHL Online export + DobberHockey.

The league's roster export and the projection workbook each hold half of what
`data_loader` needs, so this is a JOIN rather than a column rename:

    FCHL Online   rosters, contract groups, salaries, NHL clubs, ages
    DobberHockey  projected points
    neither       STATUS, PRIOR FCHL TEAM

Run:
    python convert_fchl_online.py \\
        "data/Players – FCHL Online(1).csv" \\
        data/dobberhockeydraftlist202627.xlsx \\
        data/players.csv

Four things about the export are traps, and three of them fail silently.

**The contract group is glued onto the name.** Column 0 reads
``"Connor McDavid                                3"`` — the group is the last
whitespace-separated token. It is validated against `KNOWN_GROUPS` rather than
taken on faith, because a name whose last token is not a group means the column
layout moved and every row after it would be mis-parsed into a plausible-looking
file.

**21 of the 22 RFA rows spell their team `'RFA'`, with literal apostrophes** —
a quoting artifact from the source spreadsheet. `_PLACEHOLDER_TEAMS` does not
match that, so stripping has to happen BEFORE the known-team check; otherwise
those 21 are held back as an unknown team and the pool loses all but one RFA.

**There is no STATUS column**, and `data_loader` needs one to tell an active
keeper from a minor-leaguer. It is derived from the contract group
(`ACTIVE_GROUPS` -> START, everything else -> MINOR) by owner decision
2026-09-15. Measured against the file this replaces that rule is wrong on 22 of
248 rostered rows (8.9%) and understates league cap used by $20.7M, always in
the same direction — a group A/B/C player who is actually on the active roster
reads as a minor, and his salary comes off cap. The GROUP=3 errors cost nothing,
since group 3 counts against the cap in the minors too; only the A/B/C ones move
money. Quote that error rate when the file is refreshed, rather than rediscovering it.

**Its own GP/Pts/PPG columns are NOT the projections** — they are a blend, and
they disagree with DobberHockey by enough to matter (McDavid 138 against 131).
They are dropped; PTS comes from the workbook.

`ENT` (the entry-draft class, 885 rows) is held back through the same mechanism
`convert_legacy_players.convert` uses, and reported rather than filtered, so the
size of that decision is visible.
"""

import argparse
import collections
import csv
import json
import os
import sys

from convert_legacy_players import (
    CANONICAL_COLUMNS,
    _PLACEHOLDER_TEAMS,
    _require_biddables,
    league_team_codes,
    normalize_name,
    valid_nhl_teams,
)

# Column positions in the FCHL Online export. It ships with sentence-long
# headers (the team column's header is the list of every legal value), so
# indices are the only stable handle -- but `read_export` still checks the
# header count so a changed layout fails loudly instead of silently shifting.
COL_PLAYER, COL_POS, COL_FCHL, COL_NHL, COL_AGE, COL_CAP = 0, 1, 2, 3, 4, 5
EXPORT_WIDTH = 9

# Every contract group the league issues. `F` is undocumented but real (it
# appears on 4 rows, and `convert_state_json` records the same finding): it is
# in none of RFA_GROUPS/MINOR_CAP_GROUPS/BUYOUT_ELIGIBLE_GROUPS, so it already
# behaves like the A-E family. Passed through rather than remapped, because
# remapping it would invent a contract the league does not record.
KNOWN_GROUPS = {"2", "3", "A", "B", "C", "D", "E", "F", "RFA1", "RFA2"}

# Groups whose holders sit on the active roster. Everything else goes to the
# minors -- see the module docstring for what that rule costs.
ACTIVE_GROUPS = {"2", "3"}

SKATER_SHEET = "Points&Potential"
GOALIE_SHEET = "Goaltenders"
GOALIE_STATS_COLUMNS = ["league_year", "player_name", "proj_wins", "proj_so", "proj_gp"]


def _load_openpyxl():
    """Imported lazily so the rest of the module stays usable without it.

    openpyxl is in requirements-dev.txt, NOT requirements.txt: this script runs
    once a season on a dev machine, and the app has to install with the network
    down on draft day.
    """
    try:
        import openpyxl
    except ModuleNotFoundError:  # pragma: no cover - environment-dependent
        raise SystemExit(
            "convert_fchl_online.py needs openpyxl to read the .xlsx projections.\n"
            "    .venv/bin/pip install -r requirements-dev.txt"
        )
    return openpyxl


def _number(value) -> float | None:
    """A cell as a float, or None. The workbook carries `#N/A` as a string."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value) -> str:
    """Collapse the export's padding and strip the `'RFA'` quoting artifact."""
    return " ".join(str(value or "").split()).strip("'")


def split_name_and_group(cell: str) -> tuple[str, str]:
    """`"Connor McDavid    3"` -> `("Connor McDavid", "3")`.

    Raises on a trailing token that is not a contract group. That is the shape
    of "the column layout changed": without the check the group silently becomes
    part of the name and the name loses its last word, which reads as a pool of
    slightly-misspelled players rather than as an error.
    """
    parts = _text(cell).split()
    if len(parts) < 2 or parts[-1] not in KNOWN_GROUPS:
        raise ValueError(
            f"cannot read a contract group off {cell.strip()!r}; expected the "
            f"last token to be one of {sorted(KNOWN_GROUPS)}"
        )
    return " ".join(parts[:-1]), parts[-1]


def read_export(path: str) -> list[list[str]]:
    """The export's data rows, header validated by width."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    if not rows:
        raise ValueError(f"{path} is empty")
    if len(rows[0]) != EXPORT_WIDTH:
        raise ValueError(
            f"{path} has {len(rows[0])} columns, expected {EXPORT_WIDTH} "
            "(PLAYERS, POS, FCHL TEAM, NHL TEAM, Age, Cap, GP, Pts, PPG)"
        )
    return rows[1:]


def find_header_row(rows: list[tuple], required: list[str]) -> tuple[int, dict[str, int]]:
    """Locate the header by content, not position.

    The DobberHockey sheets open with several rows of `Quick Jumps:` navigation
    before the real header (row index 5 in the 2026-27 file). Scanning for the
    required column names is what `parse_projections.py` in the pricer repo
    does, and it survives the junk block changing height between seasons.
    """
    for index, row in enumerate(rows[:15]):
        labels = [" ".join(str(v).split()) if v is not None else "" for v in row]
        if all(col in labels for col in required):
            return index, {label: i for i, label in enumerate(labels) if label}
    raise ValueError(f"no header row containing {required} in the first 15 rows")


def read_projections(path: str) -> tuple[dict[str, int], dict[tuple, list]]:
    """Projected FCHL points by player, plus a first-initial+surname index.

    FCHL scoring is Goals + Assists for skaters and 2*Wins + 3*SO for goalies.
    Skaters use the sheet's own `Points` column, which equals G+A on 897 of 905
    rows (it differs only where this sheet rounds assists); G+A is the fallback
    when `Points` is `#N/A`.

    The loose index is returned UNRESOLVED -- a list per key, not a winner --
    because picking one is exactly the mistake `match_points` has to avoid.
    """
    openpyxl = _load_openpyxl()
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)

    exact: dict[str, int] = {}
    loose: dict[tuple, list] = collections.defaultdict(list)

    def add(name: str, club: str, points: int) -> None:
        exact[normalize_name(name)] = points
        parts = normalize_name(name).split()
        if len(parts) >= 2:
            loose[(parts[0][:1], parts[-1])].append((name, _text(club), points))

    sheet = book[SKATER_SHEET]
    rows = list(sheet.iter_rows(values_only=True))
    start, cols = find_header_row(rows, ["Player", "Goals", "Assists", "Points"])
    for row in rows[start + 1 :]:
        name = row[cols["Player"]]
        if not isinstance(name, str) or not name.strip():
            continue
        points = _number(row[cols["Points"]])
        if points is None:
            goals, assists = _number(row[cols["Goals"]]), _number(row[cols["Assists"]])
            if goals is None or assists is None:
                continue
            points = goals + assists
        add(name, row[cols["Team"]], round(points))

    sheet = book[GOALIE_SHEET]
    rows = list(sheet.iter_rows(values_only=True))
    start, cols = find_header_row(rows, ["Player", "Wins", "SO"])
    for row in rows[start + 1 :]:
        name = row[cols["Player"]]
        if not isinstance(name, str) or not name.strip():
            continue
        wins, shutouts = _number(row[cols["Wins"]]), _number(row[cols["SO"]])
        if wins is None or shutouts is None:
            continue
        add(name, row[cols["Team"]], round(2 * wins + 3 * shutouts))

    book.close()
    return exact, loose


def read_goalie_stats(path: str, season: str) -> list[dict]:
    """The Goaltenders sheet as `goalie_projection_stats.csv` rows.

    `proj_gp` is DobberHockey's *projected* games column, matching what
    `parse_projections.py` writes for prior seasons.
    """
    openpyxl = _load_openpyxl()
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheet = book[GOALIE_SHEET]
    rows = list(sheet.iter_rows(values_only=True))
    start, cols = find_header_row(rows, ["Player", "Wins", "SO"])
    gp_col = cols.get("Proj. Games")

    out = []
    for row in rows[start + 1 :]:
        name = row[cols["Player"]]
        if not isinstance(name, str) or not name.strip():
            continue
        wins, shutouts = _number(row[cols["Wins"]]), _number(row[cols["SO"]])
        if wins is None or shutouts is None:
            continue
        games = _number(row[gp_col]) if gp_col is not None else None
        out.append(
            {
                "league_year": season,
                "player_name": name.strip(),
                "proj_wins": wins,
                "proj_so": shutouts,
                "proj_gp": "" if games is None else games,
            }
        )
    book.close()
    return out


def match_points(
    name: str, club: str, exact: dict, loose: dict
) -> tuple[int | None, tuple | None]:
    """Projected points for one player: `(points, fallback_used_or_None)`.

    Exact normalized name first. Then first-initial + surname, **gated on the
    NHL club agreeing** — and that gate is the whole point. Measured on this
    file, an ungated fallback recovers 9 players of whom 5 are different people
    (`Jack Anderson` -> `Josh Anderson`, `Dryden Hunt` -> `Daemon Hunt`,
    `Colin White` -> `Colton White`, `Aku Raty` -> `Aatu Raty`, `Jack Smith` ->
    `Jackson Smith`). A false match prices a real player off a stranger's
    projection, which is strictly worse than a miss: a miss only drops him from
    the pool, where the zero-point rule makes his absence visible.

    With the club gate it accepts exactly the four real nickname cases
    (`Dan`/`Daniel Vladar`, `Jack`/`John St. Ivany`, `Sam`/`Samuel Poulin`,
    `Alex`/`Alexander Wennberg`) and rejects all five impostors.

    Note this gate is right HERE and wrong for `prior_team_index`, which joins
    across seasons — see that function.
    """
    key = normalize_name(name)
    if key in exact:
        return exact[key], None

    parts = key.split()
    if len(parts) < 2:
        return None, None
    candidates = loose.get((parts[0][:1], parts[-1]), [])
    agreeing = [c for c in candidates if c[1] == club]
    if len(agreeing) == 1:
        matched_name, _, points = agreeing[0]
        return points, (name, club, matched_name)
    return None, None


def prior_team_index(path: str) -> dict:
    """Every player's FCHL team in the previous pool, for the RFA prior column.

    `TestLiveDataInvariants::test_every_rfa_has_a_prior_team` requires one on
    every RFA, and the export has no such column, so it is recovered from the
    file this one replaces: an RFA now who sat on a team last season gives that
    team. A player who was ALREADY an RFA carries the answer in the old
    `PRIOR FCHL TEAM` instead, so that is the fallback.

    Keyed loosely on purpose, and **without the NHL-club gate `match_points`
    uses** — this join crosses seasons, and players get traded. It also needs a
    surname-only pass, which that function would never allow: the live case is
    `Egor Chinakhov`, who is `Yegor Chinakhov` in the old file, so he agrees on
    neither the full name NOR the first initial, and he moved CBJ -> PIT so a
    club gate would reject him too. Without the surname pass he has no prior
    team and `test_every_rfa_has_a_prior_team` fails.

    What keeps the looser rule honest is that **ambiguity is refused rather than
    broken** — each key holds a set, and a key naming two different teams
    resolves to nothing — and that only ~22 rows consult it, all of which `main`
    prints for review.
    """
    full: dict[str, set] = collections.defaultdict(set)
    initial: dict[tuple, set] = collections.defaultdict(set)
    surname: dict[str, set] = collections.defaultdict(set)
    with open(path) as f:
        for row in csv.DictReader(f):
            team = row["FCHL TEAM"].strip()
            if team in _PLACEHOLDER_TEAMS:
                team = row.get("PRIOR FCHL TEAM", "").strip()
            if not team:
                continue
            key = normalize_name(row["PLAYER"])
            full[key].add(team)
            parts = key.split()
            if len(parts) >= 2:
                initial[(parts[0][:1], parts[-1])].add(team)
                surname[parts[-1]].add(team)
    return {"full": full, "initial": initial, "surname": surname}


def lookup_prior_team(name: str, index: dict) -> str:
    """The team, or "" when no pass resolves it to exactly one."""
    if not index:
        return ""
    key = normalize_name(name)
    parts = key.split()
    candidates = [index["full"].get(key, set())]
    if len(parts) >= 2:
        candidates.append(index["initial"].get((parts[0][:1], parts[-1]), set()))
        candidates.append(index["surname"].get(parts[-1], set()))
    for teams in candidates:
        if len(teams) == 1:
            return next(iter(teams))
    return ""


def convert_row(row: list[str], allowed_nhl: set[str]) -> dict:
    """One export row onto the canonical schema. PTS and PRIOR are filled later."""
    name, group = split_name_and_group(row[COL_PLAYER])
    fchl_team = _text(row[COL_FCHL])
    nhl_team = _text(row[COL_NHL])
    salary = _text(row[COL_CAP]).lstrip("$")
    status = (
        ""
        if fchl_team in _PLACEHOLDER_TEAMS
        else ("START" if group in ACTIVE_GROUPS else "MINOR")
    )
    return {
        "PLAYER": name,
        "POS": _text(row[COL_POS]),
        "GROUP": group,
        "STATUS": status,
        # Blanked rather than invented when it is not a real club: 4 rows carry
        # the FCHL placeholder `UFA` here, which would render as an NHL team and,
        # once written into a pool file, look like data.
        "FCHL TEAM": fchl_team,
        "NHL TEAM": nhl_team if nhl_team in allowed_nhl else "",
        "AGE": _text(row[COL_AGE]),
        "SALARY": salary or "0",
        "BID": "0",
        "PTS": "0",
        "PRIOR FCHL TEAM": "",
    }


def convert(
    rows: list[list[str]], known_teams: set[str], allowed_nhl: set[str]
) -> tuple[list[dict], dict[str, list[str]]]:
    """Convert every row, holding back those on a team the league does not have.

    Same contract as `convert_legacy_players.convert`: `build_initial_state`
    drops an unknown FCHL TEAM without a word, so separating them here is what
    makes the decision reportable. This file's holdback is `ENT` at 885 rows —
    far larger than the 31 and 22 the other two converters see.
    """
    converted: list[dict] = []
    skipped: dict[str, list[str]] = {}
    for row in rows:
        out = convert_row(row, allowed_nhl)
        team = out["FCHL TEAM"]
        if team and team not in _PLACEHOLDER_TEAMS and team not in known_teams:
            skipped.setdefault(team, []).append(out["PLAYER"])
            continue
        converted.append(out)
    return converted, skipped


def fill_points(
    rows: list[dict], exact: dict, loose: dict
) -> tuple[list[tuple], list[str]]:
    """Fill PTS in place; return the fallback matches and the unmatched names."""
    fallbacks: list[tuple] = []
    unmatched: list[str] = []
    for row in rows:
        points, fallback = match_points(row["PLAYER"], row["NHL TEAM"], exact, loose)
        if points is None:
            unmatched.append(row["PLAYER"])
            continue
        row["PTS"] = str(points)
        if fallback:
            fallbacks.append(fallback)
    return fallbacks, unmatched


def fill_prior_teams(rows: list[dict], index: dict) -> list[tuple]:
    """Fill PRIOR FCHL TEAM for RFAs in place; return `(name, team)` for review."""
    resolved = []
    for row in rows:
        if row["FCHL TEAM"] != "RFA":
            continue
        row["PRIOR FCHL TEAM"] = lookup_prior_team(row["PLAYER"], index)
        resolved.append((row["PLAYER"], row["PRIOR FCHL TEAM"]))
    return resolved


def convert_all(
    rows: list[list[str]],
    known_teams: set[str],
    allowed_nhl: set[str],
    exact: dict,
    loose: dict,
    prior_index: dict,
) -> tuple[list[dict], dict, list[tuple], list[str], list[tuple]]:
    """The whole pipeline in one entry point, so `main` and the tests agree.

    `convert_legacy_players.convert_all` exists for the same reason and records
    why: the test fixture once called `convert` alone and produced rows the
    script would never write, so a guard comparing the committed file against a
    fresh conversion failed on its own fixture.
    """
    converted, skipped = convert(rows, known_teams, allowed_nhl)
    fallbacks, unmatched = fill_points(converted, exact, loose)
    priors = fill_prior_teams(converted, prior_index)
    return converted, skipped, fallbacks, unmatched, priors


def write_csv(path: str, rows: list[dict], columns: list[str]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def merge_goalie_stats(path: str, new_rows: list[dict], season: str) -> list[dict]:
    """Prior seasons, plus this one — replacing `season` if it is already there.

    Re-running the converter must be idempotent; appending blindly would give
    one season two blocks and let `load_goalie_wins` pick whichever the dict
    comprehension saw last.
    """
    kept: list[dict] = []
    if os.path.exists(path):
        with open(path) as f:
            kept = [r for r in csv.DictReader(f) if r["league_year"] != season]
    return kept + new_rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="FCHL Online roster export (CSV)")
    parser.add_argument("projections", help="DobberHockey draft list (XLSX)")
    parser.add_argument("dest", help="canonical players CSV to write")
    parser.add_argument("--teams", default="data/fchl_teams.json")
    parser.add_argument("--odds", default="data/team_odds.json")
    parser.add_argument(
        "--prior",
        default="data/players.csv",
        help="previous canonical CSV, for RFA prior teams",
    )
    parser.add_argument(
        "--season", default="2026-2027", help="league_year for the goalie stats block"
    )
    parser.add_argument(
        "--goalie-stats",
        default="data/goalie_projection_stats.csv",
        help="goalie projection CSV to update (blank to skip)",
    )
    args = parser.parse_args(argv)

    rows = read_export(args.source)
    known_teams = league_team_codes(args.teams)
    allowed_nhl = valid_nhl_teams(args.odds)
    exact, loose = read_projections(args.projections)
    prior_index = prior_team_index(args.prior) if os.path.exists(args.prior) else {}

    converted, skipped, fallbacks, unmatched, priors = convert_all(
        rows, known_teams, allowed_nhl, exact, loose, prior_index
    )

    # Before anything is written: an empty pool looks exactly like a finished
    # draft on screen, so it must never reach a file.
    biddable = _require_biddables(converted)
    scoring = sum(
        1
        for r in converted
        if r["FCHL TEAM"] in _PLACEHOLDER_TEAMS and int(r["PTS"]) > 0
    )

    for team, names in sorted(skipped.items()):
        shown = ", ".join(names[:5]) + (", ..." if len(names) > 5 else "")
        print(
            f"held back {len(names)} row(s) on {team!r}, which {args.teams} "
            f"does not list: {shown}"
        )

    if fallbacks:
        print(f"\nmatched by first-initial + surname, NHL club agreeing ({len(fallbacks)}):")
        for export_name, club, dobber_name in fallbacks:
            print(f"    {export_name} ({club}) -> {dobber_name}")

    missing_prior = [n for n, t in priors if not t]
    print(f"\nRFA prior teams ({len(priors)} RFAs):")
    for name, team in priors:
        print(f"    {name:28} -> {team or 'UNRESOLVED'}")
    if missing_prior:
        print(
            f"  WARNING: {len(missing_prior)} RFA(s) have no prior team; "
            "test_every_rfa_has_a_prior_team will fail: "
            + ", ".join(missing_prior)
        )

    write_csv(args.dest, converted, CANONICAL_COLUMNS)
    print(
        f"\nwrote {len(converted)} rows to {args.dest} "
        f"({biddable} biddable, {scoring} of them scoring; "
        f"{len(unmatched)} rows had no projection)"
    )

    if args.goalie_stats:
        stats = read_goalie_stats(args.projections, args.season)
        # Write the spelling that lands in players.csv: `load_players` joins
        # goalie wins on the RAW PLAYER string, so a row left as
        # `Daniel Vladar` would silently miss a pool row named `Dan Vladar` and
        # fall back to the pts/goalie_pts_per_win approximation.
        #
        # Driven off the fallback list rather than off normalized names,
        # because those are precisely the pairs whose normalized names DIFFER —
        # that is why they needed a fallback. Each one has already cleared the
        # NHL-club gate in `match_points`.
        rename = {dobber: export for export, _club, dobber in fallbacks}
        renamed = 0
        for row in stats:
            new_name = rename.get(row["player_name"])
            if new_name and new_name != row["player_name"]:
                row["player_name"] = new_name
                renamed += 1
        merged = merge_goalie_stats(args.goalie_stats, stats, args.season)
        write_csv(args.goalie_stats, merged, GOALIE_STATS_COLUMNS)
        print(
            f"wrote {len(stats)} {args.season} goalie rows to {args.goalie_stats} "
            f"({len(merged)} total, {renamed} renamed to the pool's spelling)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

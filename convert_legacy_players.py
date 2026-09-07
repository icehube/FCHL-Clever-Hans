#!/usr/bin/env python3
"""Convert a legacy-schema players CSV into the canonical `players.csv` schema.

`data/players-23.csv` is the 2023-season snapshot committed with the repo. It
predates the current column set and cannot be loaded by `data_loader`:

    legacy:    Player,Pos,Pts,Team,Status,Salary,Bid
    canonical: PLAYER,POS,GROUP,STATUS,FCHL TEAM,NHL TEAM,AGE,SALARY,BID,PTS,
               PRIOR FCHL TEAM

A column rename alone is NOT enough, and the way it fails is silent. The legacy
file encodes "no status" as the string "0" rather than blank, while
`data_loader.load_players` gates the biddable branch on `status == ""`. Since
UFA/RFA are in `_PLACEHOLDER_TEAMS`, a row with STATUS="0" matches *neither* the
biddable branch nor the on-a-team branch, so all 674 free agents vanish and the
app boots with an empty pool. `_require_biddables` below exists for that case
alone.

Run:
    python convert_legacy_players.py data/players-23.csv data/players-23-converted.csv
"""

import argparse
import csv
import json
import sys

# The schema `data_loader.load_players` reads, in order.
CANONICAL_COLUMNS = [
    "PLAYER",
    "POS",
    "GROUP",
    "STATUS",
    "FCHL TEAM",
    "NHL TEAM",
    "AGE",
    "SALARY",
    "BID",
    "PTS",
    "PRIOR FCHL TEAM",
]

LEGACY_COLUMNS = ["Player", "Pos", "Pts", "Team", "Status", "Salary", "Bid"]

# The legacy file writes "0" where the canonical one leaves STATUS empty.
LEGACY_BLANK_STATUS = "0"

_PLACEHOLDER_TEAMS = {"UFA", "RFA"}


def derive_group(fchl_team: str, status: str) -> str:
    """Synthesize the contract GROUP the legacy schema has no column for.

    The legacy file records only where a player sits, never what he is signed
    to, so GROUP is reconstructed from team + status:

    - UFA -> "3", RFA -> "RFA2". Only `is_rfa` is ever read downstream and
      `RFA1`/`RFA2` are equivalent for auction purposes, so the choice between
      them is arbitrary.
    - MINOR -> "A" (prospect). This decides real money: group A is outside both
      `MINOR_CAP_GROUPS` and `BUYOUT_ELIGIBLE_GROUPS`, so the salary stays off
      cap. It matches the shape of the current file, where 145 of 149 MINOR
      rows are A-E, and these legacy minors are almost all $0.5-0.7M.
    - anything else on a team is on the active roster -> "3", a real contract
      whose salary counts and who may be bought out.
    """
    if fchl_team == "UFA":
        return "3"
    if fchl_team == "RFA":
        return "RFA2"
    if status == "MINOR":
        return "A"
    return "3"


def convert_row(row: dict) -> dict:
    """Map one legacy row onto the canonical schema.

    NHL TEAM, AGE and PRIOR FCHL TEAM have no legacy source and are left blank.
    AGE is not a price-model feature, so it costs nothing. A blank NHL TEAM
    means `DEFAULT_TEAM_PROBABILITY` for every player -- that is the 32-team
    mean, so the feature goes uninformative rather than biased.
    """
    status = row["Status"].strip()
    if status == LEGACY_BLANK_STATUS:
        status = ""
    fchl_team = row["Team"].strip()
    return {
        "PLAYER": row["Player"].strip(),
        "POS": row["Pos"].strip(),
        "GROUP": derive_group(fchl_team, status),
        "STATUS": status,
        "FCHL TEAM": fchl_team,
        "NHL TEAM": "",
        "AGE": "",
        "SALARY": row["Salary"].strip(),
        "BID": "0",
        "PTS": row["Pts"].strip(),
        "PRIOR FCHL TEAM": "",
    }


def league_team_codes(teams_path: str = "data/fchl_teams.json") -> set[str]:
    """The team codes `build_initial_state` will actually build a TeamState for."""
    with open(teams_path) as f:
        metadata = json.load(f)
    return {
        code
        for code, info in metadata.items()
        if isinstance(info, dict) and "id" in info
    }


def convert(
    rows: list[dict], known_teams: set[str]
) -> tuple[list[dict], dict[str, list[str]]]:
    """Convert every row, holding back those on a team the league doesn't have.

    `build_initial_state` drops an unknown FCHL TEAM without a word -- it only
    builds rosters for codes in fchl_teams.json. The 2023 file carries 31 rows
    on "ENT" (that season's entry-draft class), and letting them disappear
    inside the loader would make a real data decision invisible. They are
    separated here so the caller can report them.
    """
    converted: list[dict] = []
    skipped: dict[str, list[str]] = {}
    for row in rows:
        out = convert_row(row)
        team = out["FCHL TEAM"]
        if team and team not in _PLACEHOLDER_TEAMS and team not in known_teams:
            skipped.setdefault(team, []).append(out["PLAYER"])
            continue
        converted.append(out)
    return converted, skipped


def _require_biddables(rows: list[dict]) -> int:
    """Fail loudly if the conversion produced no free agents.

    This is the STATUS="0" trap. An empty pool is indistinguishable from a
    finished draft on screen, so it must never reach a written file.
    """
    count = sum(
        1
        for r in rows
        if r["FCHL TEAM"] in _PLACEHOLDER_TEAMS and r["STATUS"] == ""
    )
    if count == 0:
        raise ValueError(
            "conversion produced 0 biddable players — every UFA/RFA row failed "
            "the `FCHL TEAM in {UFA, RFA} and STATUS == ''` test that "
            "data_loader.load_players applies. Check the STATUS translation."
        )
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="legacy CSV to read")
    parser.add_argument("dest", help="canonical CSV to write")
    parser.add_argument(
        "--teams", default="data/fchl_teams.json", help="league metadata JSON"
    )
    args = parser.parse_args(argv)

    with open(args.source) as f:
        reader = csv.DictReader(f)
        missing = [c for c in LEGACY_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            parser.error(
                f"{args.source} is not a legacy players CSV — missing "
                f"{', '.join(missing)}"
            )
        rows = list(reader)

    converted, skipped = convert(rows, league_team_codes(args.teams))
    biddable = _require_biddables(converted)

    for team, names in sorted(skipped.items()):
        print(
            f"skipped {len(names)} row(s) on team {team!r}, which is not in "
            f"{args.teams}:",
            file=sys.stderr,
        )
        for name in names:
            print(f"    {name}", file=sys.stderr)

    with open(args.dest, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CANONICAL_COLUMNS)
        writer.writeheader()
        writer.writerows(converted)

    rostered = len(converted) - biddable
    print(
        f"wrote {len(converted)} rows to {args.dest} "
        f"({biddable} biddable, {rostered} rostered, "
        f"{sum(len(v) for v in skipped.values())} skipped)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

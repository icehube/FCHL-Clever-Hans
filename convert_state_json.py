#!/usr/bin/env python3
"""Convert an old Streamlit auto-save JSON into the canonical `players.csv` schema.

`data/25-26 Starting Rosters.json` is an auto-save from the Streamlit tool this
app replaced, written 2025-09-05 — days before the 2025 draft the workbook
records. It is the only genuine PRE-DRAFT snapshot of that season we have:
rosters holding keepers only, everyone else still in the pool.

    state JSON: PLAYER, GROUP, POS, FCHL TEAM, NHL TEAM, AGE, STATUS, SALARY,
                Dobber, DtZ, PTS, Draftable, Z-score, BID
    canonical:  PLAYER, POS, GROUP, STATUS, FCHL TEAM, NHL TEAM, AGE, SALARY,
                BID, PTS, PRIOR FCHL TEAM

Most columns line up, which is what makes the one that does not dangerous.
**The state file writes "NO" where the canonical schema leaves STATUS blank**,
and `data_loader.load_players` gates the biddable branch on `status == ""`.
UFA/RFA are in `_PLACEHOLDER_TEAMS`, so an untranslated row matches *neither*
the biddable branch nor the on-a-team branch and is dropped without a word: all
651 free agents vanish and the app boots on an empty pool, which on screen is
indistinguishable from a finished draft. This is the same trap
`convert_legacy_players` documents for the 2023 file's STATUS="0", in a
different disguise — hence the shared `_require_biddables` guard.

Everything else this drops or blanks, it does on purpose:

- **`BID` is zeroed.** The file carries a *predicted* price on 145 players,
  computed by the old tool's Z-score model (`auction_results.dollar_per_z`) —
  the very model CLAUDE.md records as the reason this app exists. Canonical
  `BID` is 0 in source and populated during the auction, so importing those
  would preload the pool with the predictions of the thing we replaced.
- **`PRIOR FCHL TEAM` is blank.** The state file has no such column and it is
  not derivable: the workbook's `Intro` is the nominating team, not the holder.
  Display-only here — there is no ROFR logic in the tool (owner decision
  2026-07-05) — but it leaves the RFA badge empty for all 20.
- **`Dobber`, `DtZ`, `Z-score`, `Draftable` are dropped.** `PTS` is already the
  blend of the first two; the last two belong to the old tool's shortlist.
- **`GROUP` passes through untouched, including the two rows in group `F`.**
  It is not in the documented vocabulary (`2 3 C RFA1 RFA2 A B D E`), but it is
  in none of `RFA_GROUPS`, `MINOR_CAP_GROUPS` or `BUYOUT_ELIGIBLE_GROUPS`, so
  it behaves exactly like the A-E family: off cap in the minors, not buyout
  eligible, not an RFA. Remapping it to a letter we recognize would be
  inventing a contract that the source does not record.

One limitation is not fixable here and matters to pricing: **SALARY is 0.0 on
every biddable**, so the price model's reputation feature (`log_lag`/`has_lag`)
is flat pool-wide, exactly as it is for the 2023 pool. The top of the price
distribution compresses as a result.

Run:
    python convert_state_json.py "data/25-26 Starting Rosters.json" \\
        data/players-25.csv
"""

import argparse
import csv
import json
import sys

from convert_legacy_players import (
    CANONICAL_COLUMNS,
    _PLACEHOLDER_TEAMS,
    _require_biddables,
    league_team_codes,
    valid_nhl_teams,
)

# Every key `convert_row` reads. Checked up front so a file of the wrong shape
# fails by name rather than by KeyError on row 1.
STATE_COLUMNS = [
    "PLAYER",
    "GROUP",
    "POS",
    "FCHL TEAM",
    "NHL TEAM",
    "AGE",
    "STATUS",
    "SALARY",
    "PTS",
]

# The state file writes this where the canonical schema leaves STATUS empty.
# The whole reason this script exists — see the module docstring.
STATE_BLANK_STATUS = "NO"


def load_state(path: str) -> list[dict]:
    """The `players_data` array, or a clear error saying what was found instead.

    The auto-save is a whole application state — `teams_data`, `auction_results`,
    `bid_history`, `roster_changelog` — and only one key of it is a player pool.
    """
    with open(path) as f:
        state = json.load(f)
    if not isinstance(state, dict) or "players_data" not in state:
        found = ", ".join(sorted(state)) if isinstance(state, dict) else type(state).__name__
        raise ValueError(
            f"{path} has no 'players_data' key — found {found}. This converter "
            f"reads a Streamlit auto-save, not a players CSV."
        )
    rows = state["players_data"]
    if not rows:
        raise ValueError(f"{path} has an empty 'players_data'")
    return rows


def convert_row(row: dict, allowed_nhl: set[str]) -> dict:
    """One state row in the canonical column order.

    `str()` on the numeric fields rather than a format: AGE and PTS are already
    ints and SALARY is already a float rounded to a tenth, so re-formatting
    would be inventing precision the source does not have. `load_players`
    parses all three back out of the CSV anyway.
    """
    status = str(row.get("STATUS") or "").strip()
    nhl = str(row.get("NHL TEAM") or "").strip()
    return {
        "PLAYER": str(row["PLAYER"]).strip(),
        "POS": str(row["POS"]).strip(),
        "GROUP": str(row["GROUP"]).strip(),
        "STATUS": "" if status == STATE_BLANK_STATUS else status,
        "FCHL TEAM": str(row["FCHL TEAM"]).strip(),
        # Same placeholder contamination valid_nhl_teams was written for: three
        # rows carry the FCHL "UFA" in the column that means an NHL club.
        "NHL TEAM": nhl if nhl in allowed_nhl else "",
        "AGE": str(row.get("AGE") or ""),
        "SALARY": str(row.get("SALARY") or 0),
        "BID": "0",
        "PTS": str(row.get("PTS") or 0),
        "PRIOR FCHL TEAM": "",
    }


def convert(
    rows: list[dict], known_teams: set[str], allowed_nhl: set[str]
) -> tuple[list[dict], dict[str, list[str]]]:
    """Convert every row, holding back those on a team the league doesn't have.

    `build_initial_state` drops an unknown FCHL TEAM in silence — it only builds
    rosters for codes in fchl_teams.json. This file carries 22 rows on "ENT"
    (that season's entry-draft class), and letting them disappear inside the
    loader would make a real data decision invisible. Separated here so the
    caller can report them, exactly as `convert_legacy_players.convert` does.
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


def convert_all(
    rows: list[dict],
    known_teams: set[str],
    odds_path: str = "data/team_odds.json",
) -> tuple[list[dict], dict[str, list[str]]]:
    """The whole pipeline behind one call.

    One entry point so `main()` and the tests cannot diverge — they did in
    `convert_legacy_players`, where the fixture called the inner function and
    the script called a wrapper, and the guard comparing the committed file
    against a fresh conversion failed on its own fixture.
    """
    return convert(rows, known_teams, valid_nhl_teams(odds_path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="Streamlit auto-save JSON to read")
    parser.add_argument("dest", help="canonical CSV to write")
    parser.add_argument(
        "--teams", default="data/fchl_teams.json", help="league metadata JSON"
    )
    parser.add_argument(
        "--odds", default="data/team_odds.json", help="NHL club codes to validate against"
    )
    args = parser.parse_args(argv)

    rows = load_state(args.source)
    missing = [c for c in STATE_COLUMNS if c not in rows[0]]
    if missing:
        parser.error(
            f"{args.source} is not a Streamlit player pool — its rows are "
            f"missing {', '.join(missing)}"
        )

    converted, skipped = convert_all(rows, league_team_codes(args.teams), args.odds)
    biddable = _require_biddables(converted)

    with open(args.dest, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CANONICAL_COLUMNS)
        writer.writeheader()
        writer.writerows(converted)

    rostered = sum(1 for r in converted if r["STATUS"] == "START")
    minors = sum(1 for r in converted if r["STATUS"] == "MINOR")
    rfa = sum(1 for r in converted if r["FCHL TEAM"] == "RFA")
    blank_nhl = sum(1 for r in converted if not r["NHL TEAM"])
    print(
        f"{args.dest}: {len(converted)} rows — {biddable} biddable ({rfa} RFA), "
        f"{rostered} on active rosters, {minors} in the minors"
    )
    if blank_nhl:
        print(f"  {blank_nhl} row(s) have no NHL TEAM (not a real club code)")
    for team, names in sorted(skipped.items()):
        print(
            f"  held back {len(names)} row(s) on '{team}', which "
            f"{args.teams} does not list: {', '.join(names[:5])}"
            + (" ..." if len(names) > 5 else "")
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

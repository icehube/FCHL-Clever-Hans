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
import collections
import csv
import json
import os
import re
import sys
import unicodedata
from collections.abc import Sequence

from config import NHL_TEAM_ALIASES

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


def normalize_name(name: str) -> str:
    """A comparison key that survives how the two files spell the same player.

    `players-23.csv` and `players.csv` disagree in several mechanical ways, and
    two of them are damage in the current pool rather than era drift:

    - **backtick for apostrophe** — the legacy file writes ``O`Reilly`` (U+0060);
    - **`-` rendered as `0`** — `Oliver Ekman0Larsson`, from a find-and-replace
      in `players.csv` that hit the hyphen;
    - **`ari` rendered as `UTH`** — `Eetu LuostUTHnen`, `MUTHo Ferraro`, from the
      same file's case-insensitive Arizona -> Utah rename catching the substring
      inside names;
    - **a trailing parenthetical** — `Tony DeAngelo (NCM)`;
    - accents, punctuation and hyphen-vs-space.

    Undoing the two corruptions here rather than repairing `players.csv` keeps
    this a read-only join: the pool file is the operator's, and rewriting names
    in it is a separate decision with a separate blast radius.
    """
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("`", "'")
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s)
    s = re.sub(r"(?<=[A-Za-z])0(?=[A-Za-z])", "-", s)
    s = re.sub(r"UTH", "ari", s)
    s = s.casefold().replace("'", "").replace(".", "")
    return re.sub(r"[-\s]+", " ", s).strip()


def valid_nhl_teams(odds_path: str = "data/team_odds.json") -> set[str]:
    """The NHL club codes a team name is allowed to be.

    The 2024-25 `players.csv` put the FCHL placeholder `UFA` in the NHL TEAM
    column on 9 rows (`Tony DeAngelo (NCM)` among them); no pool carries it
    today, but the donor is replaced every season and an unfiltered join copies a
    league placeholder into a field that means an NHL club. It is invisible in
    the live app — `_get_team_probability` falls through to the default for an
    unknown code — but it would render as the player's NHL team and, once
    written into a pool file, look like real data.

    Aliases are included as keys: the 2024-25 `players.csv` spelled Utah `UTH`
    while `team_odds.json` uses `UTA`.
    """
    with open(odds_path) as f:
        codes = set(json.load(f)["odds"])
    return codes | set(NHL_TEAM_ALIASES)


def nhl_team_index(path: str, odds_path: str = "data/team_odds.json") -> tuple[dict, dict]:
    """Two lookups over a canonical CSV: by full name, and by initial+surname.

    Values are sets of `(position, team)` rather than a single team, because the
    pool genuinely contains distinct players who share a name. Keeping the set
    is what lets the caller REFUSE rather than pick one.
    """
    full: dict[str, set] = collections.defaultdict(set)
    loose: dict[tuple, set] = collections.defaultdict(set)
    allowed = valid_nhl_teams(odds_path)
    with open(path) as f:
        for row in csv.DictReader(f):
            team = row.get("NHL TEAM", "").strip()
            if team not in allowed:
                continue
            name, pos = normalize_name(row["PLAYER"]), row["POS"].strip()
            full[name].add((pos, team))
            parts = name.split()
            if len(parts) >= 2:
                loose[(parts[0][:1], parts[-1])].add((pos, team))
    return full, loose


def _teams_for(candidates: set, position: str) -> set:
    """Narrow by position first — that is what separates two same-named players."""
    same = {team for pos, team in candidates if pos == position}
    return same or {team for _, team in candidates}


def lookup_nhl_team(name: str, position: str, full: dict, loose: dict) -> str | None:
    """The player's NHL team, or None when it cannot be established uniquely.

    Three passes, each tried only if the previous left the answer ambiguous:
    exact normalized name; the legacy file's own habit of appending the position
    letter to break a tie (`Sebastian AhoD`); then first-initial + surname, which
    covers a nickname spelled out (`Mitchell` for `Mitch`).

    **A tie is never broken by picking one.** Two real players share a name in
    this pool — the collisions `data_loader._disambiguated_names` exists for —
    and a coin flip there would put a wrong team on a real player silently,
    which is worse than the blank it replaces.
    """
    key = normalize_name(name)
    teams = _teams_for(full.get(key, set()), position)
    if len(teams) == 1:
        return teams.pop()

    suffix = position.casefold()
    if key.endswith(suffix) and len(key) > len(suffix):
        teams = _teams_for(full.get(key[: -len(suffix)].strip(), set()), position)
        if len(teams) == 1:
            return teams.pop()

    parts = key.split()
    if len(parts) >= 2:
        teams = _teams_for(loose.get((parts[0][:1], parts[-1]), set()), position)
        if len(teams) == 1:
            return teams.pop()
    return None


# Chained, first answer wins: the current pool is the present-day truth and the
# older one only covers names it has dropped. See `fill_nhl_teams` for why one
# is no longer enough and why merging them would be worse.
DEFAULT_NHL_SOURCES = ("data/players.csv", "data/players-25.csv")


def fill_nhl_teams(
    rows: list[dict],
    source: str | Sequence[str],
    odds_path: str = "data/team_odds.json",
) -> list[str]:
    """Fill NHL TEAM in place; return the names that could not be resolved.

    The legacy schema has no NHL team, and without one every player gets
    `DEFAULT_TEAM_PROBABILITY` — one of the price model's ten features flat
    across the whole pool. The sources are the canonical pools in the repo, so
    these are **present-day** teams: a player who has since been traded gets the
    club he plays for now, not the one he played for in the legacy season. That
    is the coherent pairing rather than a compromise, since `team_odds.json`
    carries present-day Cup odds too.

    **Several sources, tried in order, and chaining is not merging.** Merging the
    indexes would hand `lookup_nhl_team` a player listed on two clubs by two
    pools and it would correctly REFUSE — turning every off-season trade into a
    blank, which is the opposite of what a second source is for. Chained, the
    first pool that can answer wins, so precedence is explicit: the current pool
    is the present-day truth and an older one only covers names it has dropped.

    The reason there is more than one is measured. The donor is whatever
    `data/players.csv` happens to hold, and the 2026-27 refresh cut it from 2158
    rows to 1268 by dropping 885 unprojected prospects (owner decision
    2026-09-15) — so coverage of the 2023 pool fell from **>95% to 73.4%**, and
    31% of the converted pool priced at `DEFAULT_TEAM_PROBABILITY`. Adding
    `players-25.csv` behind it recovers it to **81.4%**. It does not reach 95%
    again and cannot: a 2023 player who has left the league since is in no pool
    the repo carries, and the join has nowhere to look him up.
    """
    sources = [source] if isinstance(source, str) else list(source)
    indexes = [nhl_team_index(path, odds_path) for path in sources]
    unresolved = []
    for row in rows:
        for full, loose in indexes:
            team = lookup_nhl_team(row["PLAYER"], row["POS"], full, loose)
            if team:
                row["NHL TEAM"] = team
                break
        else:
            unresolved.append(row["PLAYER"])
    return unresolved


def convert_row(row: dict) -> dict:
    """Map one legacy row onto the canonical schema.

    AGE and PRIOR FCHL TEAM have no legacy source and are left blank; AGE is not
    a price-model feature, so it costs nothing. NHL TEAM is blank HERE and filled
    afterwards by `fill_nhl_teams`, which needs the whole file to resolve a name
    against -- a row cannot tell on its own whether its name is ambiguous.
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


def convert_all(
    rows: list[dict],
    known_teams: set[str],
    nhl_source: str | Sequence[str] | None = None,
    odds_path: str = "data/team_odds.json",
) -> tuple[list[dict], dict[str, list[str]], list[str]]:
    """The whole pipeline: convert, hold back unknown teams, join NHL teams.

    One entry point so `main()` and the tests cannot diverge. They did: the test
    fixture called `convert` alone, so it produced rows with a blank NHL TEAM
    while the script wrote rows with it filled, and the guard comparing the
    committed file against a fresh conversion failed on its own fixture.
    """
    converted, skipped = convert(rows, known_teams)
    unresolved: list[str] = []
    wanted = [nhl_source] if isinstance(nhl_source, str) else list(nhl_source or [])
    present = [path for path in wanted if os.path.exists(path)]
    if present:
        unresolved = fill_nhl_teams(converted, present, odds_path)
    return converted, skipped, unresolved


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
    parser.add_argument(
        "--nhl-teams",
        action="append",
        metavar="CSV",
        help=(
            "canonical CSV to source NHL TEAM from; repeat to chain fallbacks, "
            "first answer wins (default: %s)" % ", ".join(DEFAULT_NHL_SOURCES)
        ),
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

    sources = args.nhl_teams or list(DEFAULT_NHL_SOURCES)
    converted, skipped, unresolved = convert_all(
        rows, league_team_codes(args.teams), sources
    )
    biddable = _require_biddables(converted)

    # `--nhl-teams` is `action="append"`, so it arrives as a LIST. This passed
    # that list to `os.path.exists` until 2026-09-22 — a TypeError on any
    # explicit `--nhl-teams` — and gated the whole report on the flag, so the
    # DEFAULT run, which is the one anybody actually makes, filled the column
    # and printed nothing about it: 162 blank clubs on the 2023 pool, unnamed.
    present = [path for path in sources if os.path.exists(path)]
    for path in sources:
        if path not in present:
            print(f"NHL TEAM: {path} not found — skipped", file=sys.stderr)
    if present:
        filled = len(converted) - len(unresolved)
        print(
            f"NHL TEAM: filled {filled}/{len(converted)} from {', '.join(present)}",
            file=sys.stderr,
        )
        # Named, not just counted: each is either two real players sharing a name
        # -- where refusing is the correct answer -- or a spelling the normalizer
        # does not cover, which is a fixable gap. A bare count hides which.
        for name in unresolved:
            print(f"    unresolved, left blank: {name}", file=sys.stderr)
    else:
        print(
            "NHL TEAM: no source found — left blank, so every player will price "
            "at DEFAULT_TEAM_PROBABILITY",
            file=sys.stderr,
        )

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

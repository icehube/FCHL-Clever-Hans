"""The FCHL Online converter's report on players with no NHL club.

`convert_row` blanks any NHL club that is not a real code, which folds two
different things into one empty cell:

- a club the odds file does not know -- a data problem, and what the 162 blanks
  in `players-23-converted.csv` are (that pool's NHL join lost most of its
  donor, see CLAUDE.md);
- the league export's own `UFA` placeholder, which means **this player has no
  NHL contract**.

The second is a roster decision. A prospect in group A-F with no NHL contract is
normal and costs nothing -- he sits in the minors off cap. A player in a
cap-counting group on an active roster with no NHL contract is a $2.3M hole:
he counts against the cap all season and will not play a game. On 2026-09-17
there was exactly one, and he had to be found by hand because nothing said so.
"""

import csv
from pathlib import Path

import pytest

from convert_fchl_online import ACTIVE_GROUPS, no_nhl_club

REPO = Path(__file__).resolve().parent.parent
LIVE_POOL = REPO / "data/players.csv"


def _row(**kw):
    row = {
        "PLAYER": "Sample Player",
        "POS": "F",
        "GROUP": "3",
        "STATUS": "START",
        "FCHL TEAM": "BOT",
        "NHL TEAM": "EDM",
        "AGE": "27",
        "SALARY": "2.3",
        "BID": "0",
        "PTS": "10",
        "PRIOR FCHL TEAM": "",
    }
    row.update(kw)
    return row


class TestTheReporter:
    def test_it_finds_exactly_the_blank_clubs(self):
        rows = [
            _row(PLAYER="Sample With Club"),
            _row(PLAYER="Sample No Club", **{"NHL TEAM": ""}),
            _row(PLAYER="Sample Prospect", GROUP="B", STATUS="MINOR", **{"NHL TEAM": ""}),
        ]
        assert [r["PLAYER"] for r in no_nhl_club(rows)] == [
            "Sample No Club",
            "Sample Prospect",
        ]

    def test_a_populated_club_is_never_reported(self):
        assert no_nhl_club([_row(), _row(PLAYER="Sample Two")]) == []


class TestTheLivePoolHasNoRosteredPlayerWithoutAnNhlContract:
    """The invariant the 2026-09-17 hand edit established.

    Scoped to `data/players.csv` deliberately, not parametrized over every pool
    the way the logo and Cup-odds sweeps are. Those check a property of the
    DATA; this checks a property of the league's ROSTER decisions, and only the
    live pool is built from an export that carries the `UFA` signal at all.
    `players-23-converted.csv` has four rows that would fail this and none of
    them means what it looks like -- its NHL join simply lost its donor.
    """

    def test_no_cap_counting_roster_row_lacks_a_club(self):
        with LIVE_POOL.open(newline="") as f:
            rows = list(csv.DictReader(f))
        offenders = [
            r
            for r in no_nhl_club(rows)
            if r["STATUS"] == "START" and r["GROUP"] in ACTIVE_GROUPS
        ]
        assert not offenders, (
            "these players count against a team's cap but have no NHL club, so "
            "the league export flagged them as having no NHL contract: "
            + ", ".join(
                f"{r['PLAYER']} ({r['FCHL TEAM']} ${r['SALARY']}M grp {r['GROUP']})"
                for r in offenders
            )
            + ". Either the club is missing from the data, or the player should "
            "be dropped from the league — delete his row from players.csv, which "
            "is the only way to say 'gone this season'."
        )

    def test_the_prospects_that_remain_are_all_off_cap(self):
        """The three that are left are group A-F minors, which is the normal case."""
        with LIVE_POOL.open(newline="") as f:
            rows = list(csv.DictReader(f))
        blank = no_nhl_club(rows)
        assert blank, "no blank-club rows at all — has the pool schema changed?"
        assert all(r["STATUS"] == "MINOR" for r in blank)
        assert all(r["GROUP"] not in ACTIVE_GROUPS for r in blank)

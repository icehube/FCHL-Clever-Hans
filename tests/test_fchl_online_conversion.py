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

import collections
import csv
from pathlib import Path

import pytest

from convert_fchl_online import (
    ACTIVE_GROUPS,
    GOALIE_SHEET,
    PASTE_SHEET,
    SKATER_SHEET,
    match_points,
    no_nhl_club,
    read_projections,
)
from convert_legacy_players import normalize_name

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


class TestTwoPlayersSharingOneName:
    """The 2026-27 workbook carried two Elias Petterssons and the pool got 69 twice.

    Dobber lists the defenceman as `Name (d)`, `normalize_name` strips the
    suffix, and his display row is `#N/A` because the sheet's own lookup into
    its raw table fails on the suffixed name. The old exact index was a plain
    name -> points dict, so the `#N/A` row was skipped, the key held only the
    forward, and both export rows matched it — a 10-point defenceman priced as
    the pool's #2 D. Names here are synthetic: the pool must never be named in a
    test, and this has to hold for the NEXT pair, not for that one.
    """

    TWINS = {"sample twin": [("F", 60), ("D", 10)]}

    def test_a_shared_name_is_resolved_by_position(self):
        assert match_points("Sample Twin", "VAN", "F", self.TWINS, {}) == (60, None)
        assert match_points("Sample Twin", "VAN", "D", self.TWINS, {}) == (10, None)

    def test_a_shared_name_nobody_agrees_with_is_a_miss_not_a_guess(self):
        """A goalie called Sample Twin matches neither skater. Returning either
        one's points is the original bug with a different victim."""
        assert match_points("Sample Twin", "VAN", "G", self.TWINS, {}) == (None, None)

    def test_a_name_carried_once_ignores_position(self):
        """The export and the workbook disagree about position for the SAME
        player (one is F in the export and RD in the workbook), so gating a
        unique name on position would drop a real player. Only a shared name
        needs the tie-break."""
        once = {"sample single": [("D", 8)]}
        assert match_points("Sample Single", "ANA", "F", once, {}) == (8, None)


def _workbook(path, skaters, paste):
    """A minimal DobberHockey-shaped workbook: display sheet, goalies, raw table."""
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    book.remove(book.active)
    sheet = book.create_sheet(SKATER_SHEET)
    sheet.append(["Quick Jumps:"])  # the real file opens with navigation rows
    sheet.append(["Rank", "Player", "Pos", "Team", "Goals", "Assists", "Points"])
    for row in skaters:
        sheet.append(row)
    sheet = book.create_sheet(GOALIE_SHEET)
    sheet.append(["Rank", "Player", "Team", "Wins", "SO"])
    sheet = book.create_sheet(PASTE_SHEET)
    sheet.append([None])
    sheet.append(["player", "pr gp", "pr goals", "pr pts", "pr pim", "pos", "F=1, D=2", "team"])
    for row in paste:
        sheet.append(row)
    book.save(path)
    return str(path)


class TestAnUnprojectedDisplayRowReadsTheRawTable:
    """What `read_projections` does with the `#N/A` half of the pair."""

    FORWARD = [1, "Sample Twin", "C", "VAN", 20, 40, 60]
    DEFENCE = [2, "Sample Twin (d)", "#N/A", "#N/A", "#N/A", "#N/A", "#N/A"]
    PASTE = [
        ["Sample Twin", 76, 20, 60.2, 0, "C", None, "VAN"],
        ["Sample Twin", 72, 3, 10.4, 0, "LD", None, "VAN"],
    ]

    def _entries(self, tmp_path, skaters, paste=PASTE):
        exact, _loose = read_projections(_workbook(tmp_path / "d.xlsx", skaters, paste))
        return sorted(exact[normalize_name("Sample Twin")])

    def test_both_halves_carry_their_own_projection(self, tmp_path):
        assert self._entries(tmp_path, [self.FORWARD, self.DEFENCE]) == [("D", 10), ("F", 60)]

    def test_the_order_of_the_display_rows_does_not_matter(self, tmp_path):
        """The raw-table pass runs after the whole sheet is read. Run inline, a
        `#N/A` row met BEFORE its twin would see two unclaimed entries and
        refuse, so the answer would depend on Dobber's sort order."""
        assert self._entries(tmp_path, [self.DEFENCE, self.FORWARD]) == [("D", 10), ("F", 60)]

    def test_it_refuses_when_the_raw_table_leaves_more_than_one(self, tmp_path):
        paste = self.PASTE + [["Sample Twin", 70, 2, 12.0, 0, "RD", None, "BOS"]]
        assert self._entries(tmp_path, [self.FORWARD, self.DEFENCE], paste) == [("F", 60)]


class TestTheFallbackSpeaksTheLeaguesClubCodes:
    """Dobber spells Washington `WAS`, the export and odds file `WSH`, and the
    first-initial fallback gates on the two AGREEING — so until 2026-09-22 no
    Washington player could pass it. Synthetic names, per the pool rule."""

    def test_a_washington_nickname_still_matches(self, tmp_path):
        skaters = [[1, "Jonathan Sample", "C", "WAS", 10, 20, 30]]
        exact, loose = read_projections(_workbook(tmp_path / "d.xlsx", skaters, []))
        assert match_points("Jon Sample", "WSH", "F", exact, loose) == (
            30, ("Jon Sample", "WSH", "Jonathan Sample")
        )

    def test_the_gate_still_refuses_another_club(self, tmp_path):
        skaters = [[1, "Jonathan Sample", "C", "WAS", 10, 20, 30]]
        exact, loose = read_projections(_workbook(tmp_path / "d.xlsx", skaters, []))
        assert match_points("Jon Sample", "BOS", "F", exact, loose) == (None, None)


class TestTheLivePoolHasNoCopiedProjection:
    """The live-data tripwire for the same bug, in case the next workbook finds
    a new way to collapse two players onto one key."""

    def test_no_same_name_pair_across_positions_shares_a_projection(self):
        with LIVE_POOL.open(newline="") as f:
            rows = list(csv.DictReader(f))
        by_name = collections.defaultdict(list)
        for r in rows:
            by_name[normalize_name(r["PLAYER"])].append(r)
        copied = [
            name
            for name, group in by_name.items()
            if len({r["POS"] for r in group}) > 1
            and len({r["PTS"] for r in group}) == 1
            and group[0]["PTS"] != "0"
        ]
        assert not copied, (
            f"players sharing a name at different positions carry the same "
            f"projection, which is one player's points copied onto another: {copied}"
        )


class TestARerunRefusesToUndoPlacements:
    """`main` over a dest that was baked or hand-edited after it was converted.

    The derived STATUS is a starting point that `bake_roster_state.py` writes
    over, and dropping a player from the league is a deleted row. On 2026-09-22
    a re-run over the live pool would have reverted three recalls and brought a
    dropped player back, with only the last of the four mentioned anywhere.

    Driven through `main` with both readers patched, so it needs no workbook and
    no openpyxl; the teams and odds files are the real ones, read only.
    """

    # Group A derives MINOR and group 3 derives START.
    EXPORT = [
        ["Sample Prospect    A", "F", "BOT", "EDM", "21", "$0.5", "", "", ""],
        ["Sample Keeper    3", "D", "BOT", "EDM", "27", "$2.3", "", "", ""],
        ["Sample Signing    3", "F", "BOT", "EDM", "26", "$1.1", "", "", ""],
        ["Sample Free    3", "G", "UFA", "EDM", "30", "$0.0", "", "", ""],
        ["Sample Restricted    RFA1", "F", "RFA", "EDM", "24", "$0.9", "", "", ""],
    ]
    POINTS = {
        normalize_name(r[0].rsplit(None, 1)[0]): [(r[1], 20)] for r in EXPORT
    }

    def _run(self, tmp_path, monkeypatch, *extra, export=None, points=None, prior=None):
        import convert_fchl_online as cfo

        monkeypatch.setattr(cfo, "read_export", lambda _p: export or self.EXPORT)
        monkeypatch.setattr(cfo, "read_projections", lambda _p: (points or self.POINTS, {}))
        monkeypatch.setattr(cfo, "read_goalie_stats", lambda _p, _s: [])
        return cfo.main([
            "export.csv", "dobber.xlsx", str(tmp_path / "players.csv"),
            "--prior", str(prior or tmp_path / "no-prior.csv"),
            "--goalie-stats", str(self._goalies(tmp_path)),
            *extra,
        ])

    def _edit(self, tmp_path, name, **cells):
        rows = self._rows(tmp_path)
        for r in rows:
            if r["PLAYER"] == name:
                r.update(cells)
        self._rewrite(tmp_path, rows)

    @staticmethod
    def _goalies(tmp_path):
        path = tmp_path / "goalies.csv"
        if not path.exists():
            path.write_text("league_year,player_name,proj_wins,proj_so,proj_gp\n")
        return path

    def _rows(self, tmp_path):
        with (tmp_path / "players.csv").open(newline="") as f:
            return list(csv.DictReader(f))

    def _rewrite(self, tmp_path, rows):
        with (tmp_path / "players.csv").open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def _refused(self, tmp_path, monkeypatch, capsys):
        before = {p: p.read_bytes() for p in tmp_path.iterdir()}
        assert self._run(tmp_path, monkeypatch) == 1
        err = capsys.readouterr()
        assert "refused" in err.err
        assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before, (
            "a refused run wrote something — the goalie stats count too"
        )
        return err.out

    def test_a_first_conversion_writes(self, tmp_path, monkeypatch):
        assert self._run(tmp_path, monkeypatch) == 0
        assert {r["PLAYER"]: r["STATUS"] for r in self._rows(tmp_path)}[
            "Sample Prospect"
        ] == "MINOR"

    def test_a_rerun_over_its_own_output_is_not_refused(self, tmp_path, monkeypatch):
        assert self._run(tmp_path, monkeypatch) == 0
        assert self._run(tmp_path, monkeypatch) == 0

    def test_a_baked_recall_is_refused(self, tmp_path, monkeypatch, capsys):
        self._run(tmp_path, monkeypatch)
        rows = self._rows(tmp_path)
        for r in rows:
            if r["PLAYER"] == "Sample Prospect":
                r["STATUS"] = "START"
        self._rewrite(tmp_path, rows)
        capsys.readouterr()

        out = self._refused(tmp_path, monkeypatch, capsys)
        assert "Sample Prospect" in out and "START -> MINOR" in out

    def test_a_deleted_row_is_refused(self, tmp_path, monkeypatch, capsys):
        self._run(tmp_path, monkeypatch)
        self._rewrite(
            tmp_path, [r for r in self._rows(tmp_path) if r["PLAYER"] != "Sample Keeper"]
        )
        capsys.readouterr()

        out = self._refused(tmp_path, monkeypatch, capsys)
        assert "Sample Keeper" in out and "(no row) -> START" in out

    def test_force_discards_them(self, tmp_path, monkeypatch, capsys):
        self._run(tmp_path, monkeypatch)
        rows = [r for r in self._rows(tmp_path) if r["PLAYER"] != "Sample Keeper"]
        self._rewrite(tmp_path, rows)

        assert self._run(tmp_path, monkeypatch, "--force") == 0
        assert "Sample Keeper" in {r["PLAYER"] for r in self._rows(tmp_path)}

    def test_a_deleted_free_agent_is_refused_too(self, tmp_path, monkeypatch, capsys):
        """A player the league drops need not be on a roster; until 2026-09-22
        only rostered rows were checked and a deleted UFA came back with exit 0."""
        self._run(tmp_path, monkeypatch)
        self._rewrite(
            tmp_path, [r for r in self._rows(tmp_path) if r["PLAYER"] != "Sample Free"]
        )
        capsys.readouterr()

        out = self._refused(tmp_path, monkeypatch, capsys)
        assert "Sample Free" in out and "(no row) -> in the pool" in out

    def test_a_hand_corrected_salary_is_refused(self, tmp_path, monkeypatch, capsys):
        """The bake's salary report tells the operator to make exactly this
        edit, and a re-run reverted it with exit 0."""
        self._run(tmp_path, monkeypatch)
        self._edit(tmp_path, "Sample Keeper", SALARY="3.4")
        capsys.readouterr()

        out = self._refused(tmp_path, monkeypatch, capsys)
        assert "Sample Keeper" in out and "SALARY 3.4 -> 2.3" in out

    def test_a_hand_corrected_prior_team_is_refused(self, tmp_path, monkeypatch, capsys):
        self._run(tmp_path, monkeypatch)
        self._edit(tmp_path, "Sample Restricted", **{"PRIOR FCHL TEAM": "SRL"})
        capsys.readouterr()

        out = self._refused(tmp_path, monkeypatch, capsys)
        assert "Sample Restricted" in out and "PRIOR FCHL TEAM SRL -> (blank)" in out

    def test_the_default_prior_reads_a_corrected_prior_team_back(self, tmp_path, monkeypatch):
        """`--prior` defaults to the dest, and a placeholder row's team is read
        from its own PRIOR column, so the correction survives a re-run rather
        than being refused — the refusal is for a `--prior` pointed elsewhere."""
        self._run(tmp_path, monkeypatch)
        self._edit(tmp_path, "Sample Restricted", **{"PRIOR FCHL TEAM": "SRL"})

        assert self._run(tmp_path, monkeypatch, prior=tmp_path / "players.csv") == 0
        prior = {r["PLAYER"]: r["PRIOR FCHL TEAM"] for r in self._rows(tmp_path)}
        assert prior["Sample Restricted"] == "SRL"

    def test_a_salary_typed_differently_is_not_an_edit(self, tmp_path, monkeypatch):
        self._run(tmp_path, monkeypatch)
        self._edit(tmp_path, "Sample Keeper", SALARY="2.30")
        assert self._run(tmp_path, monkeypatch) == 0

    def test_new_projections_are_not_an_edit(self, tmp_path, monkeypatch):
        """A new workbook moves PTS legitimately, and re-running to pick it up
        is the one routine same-season re-run there is."""
        self._run(tmp_path, monkeypatch)
        moved = {k: [(pos, pts + 5) for pos, pts in v] for k, v in self.POINTS.items()}
        assert self._run(tmp_path, monkeypatch, points=moved) == 0
        assert {r["PTS"] for r in self._rows(tmp_path)} == {"25"}

    def test_a_name_twice_on_a_team_is_compared_as_a_multiset(
        self, tmp_path, monkeypatch, capsys
    ):
        """Three rows sharing (PLAYER, POS, FCHL TEAM): the dest's statuses are
        the export's with one START demoted. As SETS the two agree — both hold
        START and MINOR — so a set comparison would call this a clean re-run."""
        twins = [
            ["Sample Twin    3", "F", "BOT", "EDM", "25", "$1.0", "", "", ""],
            ["Sample Twin    3", "F", "BOT", "EDM", "25", "$1.0", "", "", ""],
            ["Sample Twin    A", "F", "BOT", "EDM", "25", "$1.0", "", "", ""],
        ]
        export = self.EXPORT + twins
        self._run(tmp_path, monkeypatch, export=export)
        rows = self._rows(tmp_path)
        next(r for r in rows if r["PLAYER"] == "Sample Twin" and r["STATUS"] == "START")[
            "STATUS"
        ] = "MINOR"
        self._rewrite(tmp_path, rows)
        capsys.readouterr()

        before = {p: p.read_bytes() for p in tmp_path.iterdir()}
        assert self._run(tmp_path, monkeypatch, export=export) == 1
        assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before
        assert "Sample Twin" in capsys.readouterr().out

    def test_a_signing_out_of_the_pool_is_not_a_deleted_row(self, tmp_path, monkeypatch):
        """The dest had him as a free agent; the export has him on a team. That
        is the league moving, not an edit being undone — matching on the team
        as well would call him deleted and refuse every refresh."""
        self._run(tmp_path, monkeypatch)
        rows = self._rows(tmp_path)
        for r in rows:
            if r["PLAYER"] == "Sample Signing":
                r["FCHL TEAM"], r["STATUS"] = "UFA", ""
        self._rewrite(tmp_path, rows)

        assert self._run(tmp_path, monkeypatch) == 0

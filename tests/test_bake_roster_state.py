"""Baking the live roster placement back into the data files.

`POST /reset` rebuilds every team from `data/players.csv` + `data/fchl_teams.json`
and reads the saved state not at all, so pre-draft prep -- recalls, demotions --
is discarded by the first reset after it. `bake_roster_state.py` closes that by
writing the one durable fact the UI produces, STATUS, back into the pool file,
so the next reset rebuilds INTO the prep. Penalties and salaries are REPORTED
when the state disagrees with the files, never written -- see the script's
docstring for the stale-state trap that carrying them walked into.

The load-bearing property is that it **refuses** rather than guesses. A bake is
an in-place rewrite of the pool file, and the two failure modes are both silent
on screen: a draft in progress baked as keepers looks like a league that always
owned those players, and a STATUS the loader does not recognise
(`is_minor = status == "MINOR"`, nothing else) is read as an active keeper with
no warning. So every refusal test asserts the files are **byte-identical
afterwards**, not merely that the exit status was 1.
"""

import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

import bake_roster_state as bake
from convert_legacy_players import CANONICAL_COLUMNS

REPO = Path(__file__).resolve().parent.parent

HEADER = ",".join(CANONICAL_COLUMNS)

# Synthetic names throughout, matching `tests/fixtures/players_sample.csv`. A
# real pool name here would go stale at the next refresh and is banned by
# `tests/test_no_literal_player_names.py`.
POOL = f"""{HEADER}
Sample Keeper Forward,F,2,START,BOT,EDM,27,8.0,0,100,
Sample Prospect Forward,F,B,MINOR,BOT,CHI,22,0.5,0,52,
Sample Rival Defence,D,3,START,SRL,COL,29,4.0,0,70,
Sample Free Agent,F,3,,UFA,TOR,25,0.0,0,60,
Sample Restricted,D,RFA1,,RFA,BOS,24,1.0,0,45,BOT
"""


def _player(name, group="2", salary=1.0, **kw):
    """One PlayerOnRoster as the state file serialises it."""
    row = {
        "name": name,
        "position": "F",
        "group": group,
        "salary": salary,
        "projected_points": 50,
        "nhl_team": "EDM",
        "is_minor": False,
        "is_bench": False,
        "is_keeper": True,
    }
    row.update(kw)
    return row


def _state(bot_keepers=(), bot_minors=(), srl_keepers=(), **kw):
    state = {
        "teams": {
            "BOT": {
                "code": "BOT",
                "keeper_players": list(bot_keepers),
                "minor_players": list(bot_minors),
                "acquired_players": [],
                "penalties": 0.0,
                "is_done": False,
            },
            "SRL": {
                "code": "SRL",
                "keeper_players": list(srl_keepers),
                "minor_players": [],
                "acquired_players": [],
                "penalties": 0.0,
                "is_done": False,
            },
        },
        "transaction_log": [],
        "change_log": [],
    }
    state.update(kw)
    return state


@pytest.fixture
def files(tmp_path):
    """A pool file and a teams file, plus their bytes as written."""
    players = tmp_path / "players.csv"
    players.write_text(POOL)
    teams = tmp_path / "fchl_teams.json"
    teams.write_text(
        '{\n'
        '  "BOT": {\n'
        '    "id": 1,\n'
        '    "is_my_team": true,\n'
        '    "name": "Bridlewood AI",\n'
        '    "penalty": 0.0,\n'
        '    "colors": { "primary": "#ea217b", "secondary": "#ff7e31" },\n'
        '    "logo": "BOT.png"\n'
        '  },\n'
        '  "SRL": {\n'
        '    "id": 2,\n'
        '    "is_my_team": false,\n'
        '    "name": "Searle Supremes",\n'
        '    "penalty": 0.0,\n'
        '    "colors": { "primary": "#000000", "secondary": "#464646" },\n'
        '    "logo": "SRL.png"\n'
        '  },\n'
        '  "nomination_order": ["BOT", "SRL"]\n'
        '}\n'
    )
    return players, teams


def _write_state(tmp_path, state):
    p = tmp_path / "auction_state.json"
    p.write_text(json.dumps(state))
    return p


def _run(state_path, players, teams, *extra):
    return bake.main(
        [
            "--state", str(state_path),
            "--players", str(players),
            "--teams", str(teams),
            *extra,
        ]
    )


def _status_of(players: Path) -> dict[str, str]:
    with players.open(newline="") as f:
        return {r["PLAYER"]: r["STATUS"] for r in csv.DictReader(f)}


# --------------------------------------------------------------------------
# What it carries
# --------------------------------------------------------------------------


class TestPlacementRoundTrips:
    def test_a_recall_bakes_to_start(self, tmp_path, files):
        """A prospect recalled in the UI comes back active after the next reset."""
        players, teams = files
        state = _state(
            bot_keepers=[
                _player("Sample Keeper Forward"),
                _player("Sample Prospect Forward", group="B", salary=0.5),
            ],
            srl_keepers=[_player("Sample Rival Defence", group="3")],
        )
        assert _run(_write_state(tmp_path, state), players, teams, "--write") == 0
        assert _status_of(players)["Sample Prospect Forward"] == "START"

    def test_a_demotion_bakes_to_minor(self, tmp_path, files):
        players, teams = files
        state = _state(
            bot_keepers=[],
            bot_minors=[
                _player("Sample Keeper Forward"),
                _player("Sample Prospect Forward", group="B", salary=0.5),
            ],
            srl_keepers=[_player("Sample Rival Defence", group="3")],
        )
        assert _run(_write_state(tmp_path, state), players, teams, "--write") == 0
        assert _status_of(players)["Sample Keeper Forward"] == "MINOR"

    def test_biddable_rows_keep_their_blank_status(self, tmp_path, files):
        """`load_players` gates the pool branch on STATUS == "" exactly.

        Anything else on a UFA/RFA row is dropped without a word, so a bake that
        stamped START on the pool would empty it -- which on screen is
        indistinguishable from a finished draft.
        """
        players, teams = files
        state = _state(
            bot_keepers=[_player("Sample Keeper Forward")],
            bot_minors=[_player("Sample Prospect Forward", group="B", salary=0.5)],
            srl_keepers=[_player("Sample Rival Defence", group="3")],
        )
        _run(_write_state(tmp_path, state), players, teams, "--write")
        status = _status_of(players)
        assert status["Sample Free Agent"] == ""
        assert status["Sample Restricted"] == ""

    def test_an_unchanged_state_writes_nothing(self, tmp_path, files):
        players, teams = files
        before = players.read_bytes(), teams.read_bytes()
        state = _state(
            bot_keepers=[_player("Sample Keeper Forward")],
            bot_minors=[_player("Sample Prospect Forward", group="B", salary=0.5)],
            srl_keepers=[_player("Sample Rival Defence", group="3")],
        )
        assert _run(_write_state(tmp_path, state), players, teams, "--write") == 0
        assert (players.read_bytes(), teams.read_bytes()) == before

    def test_a_dry_run_writes_nothing(self, tmp_path, files):
        """The default. This rewrites files in place, so writing is opt-in."""
        players, teams = files
        before = players.read_bytes(), teams.read_bytes()
        state = _state(
            bot_keepers=[
                _player("Sample Keeper Forward"),
                _player("Sample Prospect Forward", group="B", salary=0.5),
            ],
            srl_keepers=[_player("Sample Rival Defence", group="3")],
        )
        assert _run(_write_state(tmp_path, state), players, teams) == 0
        assert (players.read_bytes(), teams.read_bytes()) == before


class TestLineEndingsSurvive:
    """The pool file is CRLF, and a rewrite that normalises it is a 1268-line diff.

    `csv.DictWriter` writes CRLF by default, so the real file matched by
    accident; an LF pool would have been converted end to end with the three
    real changes buried inside it. Both directions are asserted because the
    accident covers only one of them.
    """

    @pytest.mark.parametrize("terminator", ["\r\n", "\n"])
    def test_the_terminator_is_preserved(self, tmp_path, files, terminator):
        players, teams = files
        players.write_bytes(POOL.replace("\n", terminator).encode())
        state = _state(
            bot_keepers=[
                _player("Sample Keeper Forward"),
                _player("Sample Prospect Forward", group="B", salary=0.5),
            ],
            srl_keepers=[_player("Sample Rival Defence", group="3")],
        )
        assert _run(_write_state(tmp_path, state), players, teams, "--write") == 0
        raw = players.read_bytes()
        assert raw.count(terminator.encode()) == len(POOL.strip().splitlines())
        if terminator == "\n":
            assert b"\r" not in raw

    def test_only_the_baked_rows_differ(self, tmp_path, files):
        """The property the terminator bug broke: a small change stays small."""
        players, teams = files
        players.write_bytes(POOL.replace("\n", "\r\n").encode())
        before = players.read_bytes().split(b"\r\n")
        state = _state(
            bot_keepers=[
                _player("Sample Keeper Forward"),
                _player("Sample Prospect Forward", group="B", salary=0.5),
            ],
            srl_keepers=[_player("Sample Rival Defence", group="3")],
        )
        _run(_write_state(tmp_path, state), players, teams, "--write")
        after = players.read_bytes().split(b"\r\n")
        assert len(before) == len(after)
        differing = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        assert len(differing) == 1
        assert b"Sample Prospect Forward" in after[differing[0]]


def _prep_state(**kw):
    """The fixture pool's own placement, so a test changes one thing only."""
    return _state(
        bot_keepers=[_player("Sample Keeper Forward", salary=8.0)],
        bot_minors=[_player("Sample Prospect Forward", group="B", salary=0.5)],
        srl_keepers=[_player("Sample Rival Defence", group="3", salary=4.0)],
        **kw,
    )


class TestPenaltiesAreReportedNeverWritten:
    """The penalty half could only ever revert a hand edit.

    No pre-draft UI action produces a penalty this script accepts — the one real
    writer is `execute_buyout`, which logs a transaction, and a state holding one
    is refused. So a disagreement means the state predates an edit of the teams
    file, and writing the state's figure back undid it: measured 2026-09-22,
    SHF's hand-entered $2.8M baked to $0.0M.
    """

    def test_a_stale_state_cannot_revert_a_hand_entered_penalty(
        self, tmp_path, files, capsys
    ):
        players, teams = files
        teams.write_text(teams.read_text().replace(
            '"name": "Searle Supremes",\n    "penalty": 0.0',
            '"name": "Searle Supremes",\n    "penalty": 2.8',
        ))
        before = teams.read_bytes()
        state = _prep_state()
        state["teams"]["BOT"]["keeper_players"].append(
            state["teams"]["BOT"]["minor_players"].pop()
        )  # and a real placement change, so --write has work to do
        assert _run(_write_state(tmp_path, state), players, teams, "--write") == 0
        assert teams.read_bytes() == before, "the hand-entered penalty was reverted"
        assert _status_of(players)["Sample Prospect Forward"] == "START"
        out = capsys.readouterr().out
        assert "penalty disagreement" in out
        assert "SRL  file $2.8M   state $0.0M" in out

    def test_a_penalty_only_in_the_state_is_reported_not_baked(
        self, tmp_path, files, capsys
    ):
        players, teams = files
        before = players.read_bytes(), teams.read_bytes()
        state = _prep_state()
        state["teams"]["SRL"]["penalties"] = 2.8
        assert _run(_write_state(tmp_path, state), players, teams, "--write") == 0
        assert (players.read_bytes(), teams.read_bytes()) == before
        assert "SRL  file $0.0M   state $2.8M" in capsys.readouterr().out


class TestSalaryCorrectionsAreReported:
    @pytest.mark.parametrize("roster, name, was", [
        ("keeper_players", "Sample Keeper Forward", 8.0),
        ("minor_players", "Sample Prospect Forward", 0.5),
    ])
    def test_an_adjusted_salary_is_named_with_both_figures(
        self, tmp_path, files, capsys, roster, name, was
    ):
        """`/adjust-salary` exited 0 and said nothing until 2026-09-22, so the
        correction vanished at the next reset. A minor's salary is fully on the
        cap, so both lists count."""
        players, teams = files
        before = players.read_bytes()
        state = _prep_state()
        state["teams"]["BOT"][roster][0]["salary"] = was + 1.0
        assert _run(_write_state(tmp_path, state), players, teams, "--write") == 0
        assert players.read_bytes() == before, "salary is reported, not written"
        out = capsys.readouterr().out
        assert "1 salary disagreement" in out
        assert name in out
        assert f"file ${was:.1f}M   state ${was + 1.0:.1f}M" in out

    def test_an_agreeing_state_reports_no_salary(self, tmp_path, files, capsys):
        players, teams = files
        assert _run(_write_state(tmp_path, _prep_state()), players, teams) == 0
        assert "disagreement" not in capsys.readouterr().out


class TestWhatItCannotCarryIsNamed:
    def test_bench_flags_and_done_teams_are_reported(self, tmp_path, files, capsys):
        players, teams = files
        state = _prep_state()
        state["teams"]["BOT"]["keeper_players"][0]["is_bench"] = True
        state["teams"]["SRL"]["is_done"] = True
        assert _run(_write_state(tmp_path, state), players, teams) == 0
        out = capsys.readouterr().out
        assert "not carried: 1 bench flag(s)" in out
        assert "not carried: is_done on SRL" in out


class TestTheWriteIsAtomic:
    def test_a_failure_mid_write_leaves_the_pool_file_whole(
        self, tmp_path, files, monkeypatch
    ):
        """The pool used to be opened "w" and written row by row, so any error
        after the header left it truncated — measured, 53313 bytes to 274."""
        players, teams = files
        before = players.read_bytes()
        state = _prep_state()
        state["teams"]["BOT"]["keeper_players"].append(
            state["teams"]["BOT"]["minor_players"].pop()
        )

        def boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(bake.os, "replace", boom)
        with pytest.raises(OSError, match="disk full"):
            _run(_write_state(tmp_path, state), players, teams, "--write")
        assert players.read_bytes() == before
        assert not (tmp_path / "players.csv.tmp").exists()


class TestDisambiguatedRosterNames:
    """The state holds `_disambiguated_names`' strings; the CSV holds raw ones."""

    def test_an_ordinal_pair_on_one_roster_maps_back_row_by_row(self, tmp_path, files):
        players, teams = files
        players.write_text(POOL + "Sample Twin,F,C,START,BOT,EDM,22,0.5,0,20,\n"
                                   "Sample Twin,F,C,START,BOT,EDM,23,0.5,0,21,\n")
        state = _prep_state()
        state["teams"]["BOT"]["keeper_players"].append(_player("Sample Twin (#1)", group="C"))
        state["teams"]["BOT"]["minor_players"].append(_player("Sample Twin (#2)", group="C"))
        assert _run(_write_state(tmp_path, state), players, teams, "--write") == 0
        with players.open(newline="") as f:
            twins = [r for r in csv.DictReader(f) if r["PLAYER"] == "Sample Twin"]
        assert [(r["AGE"], r["STATUS"]) for r in twins] == [("22", "START"), ("23", "MINOR")]

    def test_a_club_suffixed_pair_maps_back_too(self, tmp_path, files):
        players, teams = files
        players.write_text(POOL + "Sample Twin,F,C,START,BOT,EDM,22,0.5,0,20,\n"
                                   "Sample Twin,F,C,START,BOT,CHI,23,0.5,0,21,\n")
        state = _prep_state()
        state["teams"]["BOT"]["minor_players"].append(_player("Sample Twin (EDM)", group="C"))
        state["teams"]["BOT"]["keeper_players"].append(_player("Sample Twin (CHI)", group="C"))
        assert _run(_write_state(tmp_path, state), players, teams, "--write") == 0
        with players.open(newline="") as f:
            twins = {r["NHL TEAM"]: r["STATUS"] for r in csv.DictReader(f)
                     if r["PLAYER"] == "Sample Twin"}
        assert twins == {"EDM": "MINOR", "CHI": "START"}


class TestTheDefaultsFollowThePool:
    """Hardcoded to `data/state` + `data/players.csv` until 2026-09-22, so under
    `FCHL_PLAYERS_CSV` the bake read a different pool's files than the server."""

    def test_an_alternate_pool_brings_its_own_state(self, monkeypatch):
        import data_loader

        monkeypatch.delenv("FCHL_STATE_DIR", raising=False)
        monkeypatch.setattr(data_loader, "PLAYERS_CSV", "data/players-25.csv")
        assert bake.default_paths() == (
            "data/state-players-25/auction_state.json", "data/players-25.csv"
        )

    def test_the_state_dir_override_wins(self, monkeypatch):
        monkeypatch.setenv("FCHL_STATE_DIR", "/elsewhere")
        assert bake.default_paths()[0] == "/elsewhere/auction_state.json"


# --------------------------------------------------------------------------
# What it refuses -- and every one of these asserts the files did not move
# --------------------------------------------------------------------------


class TestItRefusesRatherThanGuesses:
    @pytest.fixture
    def untouched(self, files):
        players, teams = files
        return players, teams, (players.read_bytes(), teams.read_bytes())

    @pytest.fixture(autouse=True)
    def _capsys(self, capsys):
        self.capsys = capsys

    def _refuse(self, tmp_path, untouched, state, fragment):
        """Exit 1 with the reason on stderr, both files byte-identical."""
        players, teams, before = untouched
        assert _run(_write_state(tmp_path, state), players, teams, "--write") == 1
        err = self.capsys.readouterr().err
        assert err.startswith("refused: ") and fragment in err, err
        assert (players.read_bytes(), teams.read_bytes()) == before

    def test_a_draft_in_progress_is_refused(self, tmp_path, untouched):
        """Baking picks would write them into the pool file as keepers."""
        state = _state(
            bot_keepers=[_player("Sample Keeper Forward")],
            bot_minors=[_player("Sample Prospect Forward", group="B", salary=0.5)],
            srl_keepers=[_player("Sample Rival Defence", group="3")],
        )
        state["transaction_log"] = [
            {"transaction_type": "draft", "player_name": "Sample Free Agent"}
        ]
        self._refuse(tmp_path, untouched, state, "1 transaction")

    def test_a_loaded_scenario_is_refused(self, tmp_path, untouched):
        """`/load-scenario` builds acquired players and logs no transaction."""
        state = _state(
            bot_keepers=[_player("Sample Keeper Forward")],
            bot_minors=[_player("Sample Prospect Forward", group="B", salary=0.5)],
            srl_keepers=[_player("Sample Rival Defence", group="3")],
        )
        state["teams"]["BOT"]["acquired_players"] = [
            _player("Sample Free Agent", is_keeper=False)
        ]
        self._refuse(tmp_path, untouched, state, "acquired players")

    def test_a_pool_player_missing_from_the_state_is_refused_by_name(
        self, tmp_path, untouched
    ):
        state = _state(
            bot_keepers=[_player("Sample Keeper Forward")],
            bot_minors=[_player("Sample Prospect Forward", group="B", salary=0.5)],
            srl_keepers=[],
        )
        self._refuse(tmp_path, untouched, state, "Sample Rival Defence")

    def test_a_state_player_missing_from_the_pool_is_refused_by_name(
        self, tmp_path, untouched
    ):
        state = _state(
            bot_keepers=[
                _player("Sample Keeper Forward"),
                _player("Sample Ghost Winger"),
            ],
            bot_minors=[_player("Sample Prospect Forward", group="B", salary=0.5)],
            srl_keepers=[_player("Sample Rival Defence", group="3")],
        )
        self._refuse(tmp_path, untouched, state, "Sample Ghost Winger")

    def test_a_player_on_a_different_team_is_refused(self, tmp_path, untouched):
        state = _state(
            bot_keepers=[
                _player("Sample Keeper Forward"),
                _player("Sample Rival Defence", group="3"),
            ],
            bot_minors=[_player("Sample Prospect Forward", group="B", salary=0.5)],
            srl_keepers=[],
        )
        self._refuse(tmp_path, untouched, state, "Sample Rival Defence")

    def test_a_file_of_the_wrong_shape_is_refused(self, tmp_path, untouched):
        players, teams, before = untouched
        p = tmp_path / "auction_state.json"
        p.write_text(json.dumps({"players_data": []}))
        assert _run(p, players, teams, "--write") == 1
        assert "not an auction state file" in self.capsys.readouterr().err
        assert (players.read_bytes(), teams.read_bytes()) == before

    def test_a_ragged_row_is_refused_before_anything_is_written(
        self, tmp_path, files
    ):
        """A trailing comma parses with a `None` key and used to raise inside
        the writer AFTER the file had been truncated."""
        players, teams = files
        players.write_text(POOL.replace(
            "Sample Free Agent,F,3,,UFA,TOR,25,0.0,0,60,",
            "Sample Free Agent,F,3,,UFA,TOR,25,0.0,0,60,,",
        ))
        before = players.read_bytes()
        state = _prep_state()
        state["teams"]["BOT"]["keeper_players"].append(
            state["teams"]["BOT"]["minor_players"].pop()
        )
        assert _run(_write_state(tmp_path, state), players, teams, "--write") == 1
        assert "more fields than its header" in self.capsys.readouterr().err
        assert players.read_bytes() == before

    def test_a_blank_team_row_is_neither_refused_nor_changed(self, tmp_path, files):
        """`plan_changes` skips a row with no FCHL TEAM. Without the skip it
        reads as a player "on '' in the pool but on no team in the state"."""
        players, teams = files
        players.write_text(POOL + "Sample Nobody,F,3,,,TOR,25,0.0,0,0,\n")
        assert _run(_write_state(tmp_path, _prep_state()), players, teams, "--write") == 0
        assert _status_of(players)["Sample Nobody"] == ""


# --------------------------------------------------------------------------
# The real files
# --------------------------------------------------------------------------


class TestAgainstTheLiveData:
    def test_a_state_built_from_the_data_files_bakes_to_nothing(self, tmp_path):
        """End to end through the CLI, on the REAL pool, writing nothing.

        The state is built from the data files rather than read from
        `data/state/`, and that is the point twice over. It makes the assertion
        a **round trip** — `build_initial_state` reads `STATUS`, this writes it
        back, and the two must agree on all 1267 rows or one of them is wrong —
        which is a far stronger claim than "the CLI exits 0". And it removes a
        dependency on the operator's live state, which is gitignored, absent on
        a fresh checkout, and legitimately out of step with the pool file
        whenever a player has been removed from the league (the saved state
        still holds him, and the script is *supposed* to refuse that).
        """
        import data_loader

        state = data_loader.build_initial_state()
        state_path = tmp_path / "auction_state.json"
        state_path.write_text(state.to_json(include_snapshots=False))

        r = subprocess.run(
            [
                sys.executable, "bake_roster_state.py",
                "--state", str(state_path),
                "--players", str(REPO / "data/players.csv"),
                "--teams", str(REPO / "data/fchl_teams.json"),
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stderr
        assert "no roster placements differ." in r.stdout, r.stdout
        assert "disagreement" not in r.stdout, r.stdout

    def test_the_repo_pool_and_teams_file_parse(self):
        """The two files this writes are the two files it must be able to read."""
        rows, names = bake.read_players(str(REPO / "data/players.csv"))
        assert len(rows) == len(names) > 0
        assert set(rows[0]) == set(CANONICAL_COLUMNS)
        meta = json.loads((REPO / "data/fchl_teams.json").read_text())
        assert all("penalty" in v for k, v in meta.items() if k != "nomination_order")

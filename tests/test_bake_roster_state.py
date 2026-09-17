"""Baking the live roster placement back into the data files.

`POST /reset` rebuilds every team from `data/players.csv` + `data/fchl_teams.json`
and reads the saved state not at all, so pre-draft prep -- recalls, demotions, a
cap penalty the league handed down -- is discarded by the first reset after it.
`bake_roster_state.py` closes that by writing the two durable facts back into the
data files, so the next reset rebuilds INTO the prep.

The load-bearing property is that it **refuses** rather than guesses. A bake is
an in-place rewrite of the pool file, and the two failure modes are both silent
on screen: a draft in progress baked as keepers looks like a league that always
owned those players, and a STATUS the loader does not recognise
(`is_minor = status == "MINOR"`, nothing else) is read as an active keeper with
no warning. So every refusal test asserts the files are **byte-identical
afterwards**, not merely that an exception came back.
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


class TestPenalties:
    def test_a_penalty_reaches_the_json(self, tmp_path, files):
        players, teams = files
        state = _state(
            bot_keepers=[_player("Sample Keeper Forward")],
            bot_minors=[_player("Sample Prospect Forward", group="B", salary=0.5)],
            srl_keepers=[_player("Sample Rival Defence", group="3")],
        )
        state["teams"]["SRL"]["penalties"] = 2.8
        assert _run(_write_state(tmp_path, state), players, teams, "--write") == 0
        assert json.loads(teams.read_text())["SRL"]["penalty"] == 2.8

    def test_only_the_penalty_line_moves(self, tmp_path, files):
        """A json.dump round trip would reformat every hand-written `colors` line.

        A two-number edit showing up as a hundred-line diff hides what actually
        changed, which is the one thing a bake has to be legible about.
        """
        players, teams = files
        before = teams.read_text().splitlines()
        state = _state(
            bot_keepers=[_player("Sample Keeper Forward")],
            bot_minors=[_player("Sample Prospect Forward", group="B", salary=0.5)],
            srl_keepers=[_player("Sample Rival Defence", group="3")],
        )
        state["teams"]["SRL"]["penalties"] = 2.8
        _run(_write_state(tmp_path, state), players, teams, "--write")
        after = teams.read_text().splitlines()
        differing = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        assert len(before) == len(after)
        assert len(differing) == 1
        assert "penalty" in after[differing[0]]
        assert "2.8" in after[differing[0]]

    def test_an_unchanged_penalty_is_not_rewritten(self, tmp_path, files):
        players, teams = files
        before = teams.read_bytes()
        state = _state(
            bot_keepers=[
                _player("Sample Keeper Forward"),
                _player("Sample Prospect Forward", group="B", salary=0.5),
            ],
            srl_keepers=[_player("Sample Rival Defence", group="3")],
        )
        _run(_write_state(tmp_path, state), players, teams, "--write")
        assert teams.read_bytes() == before


# --------------------------------------------------------------------------
# What it refuses -- and every one of these asserts the files did not move
# --------------------------------------------------------------------------


class TestItRefusesRatherThanGuesses:
    @pytest.fixture
    def untouched(self, files):
        players, teams = files
        return players, teams, (players.read_bytes(), teams.read_bytes())

    def _refuse(self, tmp_path, untouched, state, fragment):
        players, teams, before = untouched
        with pytest.raises(ValueError, match=fragment):
            _run(_write_state(tmp_path, state), players, teams, "--write")
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

    def test_a_file_of_the_wrong_shape_is_refused(self, tmp_path, files):
        players, teams = files
        p = tmp_path / "auction_state.json"
        p.write_text(json.dumps({"players_data": []}))
        with pytest.raises(ValueError, match="not an auction state file"):
            _run(p, players, teams, "--write")

    def test_a_teams_file_with_no_penalty_line_is_refused(self, tmp_path, files):
        """The line rewrite must fail loudly rather than drop the change."""
        players, teams = files
        teams.write_text(
            '{\n  "BOT": {\n    "id": 1,\n    "logo": "BOT.png"\n  },\n'
            '  "SRL": {\n    "id": 2,\n    "logo": "SRL.png"\n  }\n}\n'
        )
        before = teams.read_bytes()
        state = _state(
            bot_keepers=[_player("Sample Keeper Forward")],
            bot_minors=[_player("Sample Prospect Forward", group="B", salary=0.5)],
            srl_keepers=[_player("Sample Rival Defence", group="3")],
        )
        state["teams"]["SRL"]["penalties"] = 2.8
        with pytest.raises(ValueError, match="could not find a 'penalty' line"):
            _run(_write_state(tmp_path, state), players, teams, "--write")
        assert teams.read_bytes() == before


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
        assert "no penalties differ." in r.stdout, r.stdout

    def test_the_repo_pool_and_teams_file_parse(self):
        """The two files this writes are the two files it must be able to read."""
        rows, names = bake.read_players(str(REPO / "data/players.csv"))
        assert len(rows) == len(names) > 0
        assert set(rows[0]) == set(CANONICAL_COLUMNS)
        meta = json.loads((REPO / "data/fchl_teams.json").read_text())
        assert all("penalty" in v for k, v in meta.items() if k != "nomination_order")

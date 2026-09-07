"""The legacy-schema converter and the alternate-pool override.

`data/players-23.csv` is the 2023 snapshot committed with the repo, in a schema
that predates the current one. `convert_legacy_players.py` translates it; these
tests cover the translation and the two environment overrides that let the app
run on the result without touching the real draft's saved state.

The load-bearing case is `STATUS`. The legacy file writes `"0"` where the
canonical schema leaves it blank, and `data_loader.load_players` gates the
biddable branch on `status == ""` while UFA/RFA sit in `_PLACEHOLDER_TEAMS` --
so an untranslated row matches *neither* branch and is dropped in silence. The
symptom is an empty pool, which looks exactly like a finished draft.
"""

import csv
import subprocess
import sys
from pathlib import Path

import pytest

import config
import data_loader
import main
from convert_legacy_players import (
    CANONICAL_COLUMNS,
    LEGACY_BLANK_STATUS,
    _require_biddables,
    convert,
    convert_row,
    league_team_codes,
)

REPO = Path(__file__).resolve().parent.parent
LEGACY_CSV = REPO / "data" / "players-23.csv"
CONVERTED_CSV = REPO / "data" / "players-23-converted.csv"


def _legacy_rows() -> list[dict]:
    with open(LEGACY_CSV) as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def converted() -> list[dict]:
    """A fresh conversion, so these test the script and not a stale artifact."""
    rows, _ = convert(_legacy_rows(), league_team_codes(REPO / "data" / "fchl_teams.json"))
    return rows


@pytest.fixture(scope="module")
def converted_csv(tmp_path_factory, converted) -> str:
    path = tmp_path_factory.mktemp("pool") / "converted.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CANONICAL_COLUMNS)
        w.writeheader()
        w.writerows(converted)
    return str(path)


class TestTheSchemaTranslation:
    def test_the_output_header_is_the_pool_schema_the_loader_reads(self):
        """Compared against the live file, not a second copy of the list.

        A hand-written expectation here would be a duplicate of
        CANONICAL_COLUMNS that agrees with it forever, including when both are
        wrong. `players.csv` is the schema `load_players` actually parses.
        """
        with open(REPO / "data" / "players.csv") as f:
            assert next(csv.reader(f)) == CANONICAL_COLUMNS

    def test_a_blank_legacy_status_becomes_an_empty_string(self):
        row = convert_row(
            {
                "Player": "X",
                "Pos": "F",
                "Pts": "50",
                "Team": "UFA",
                "Status": LEGACY_BLANK_STATUS,
                "Salary": "0",
                "Bid": "0",
            }
        )
        assert row["STATUS"] == ""

    def test_the_translation_is_what_fills_the_pool(self, converted_csv):
        """The mutation-killer for the STATUS rule.

        Loading the converted file gives a full pool; feeding the loader the
        same rows with the legacy `"0"` restored gives *nothing*. Both halves
        are asserted, because the first alone passes against a converter that
        never touched STATUS if some other rule happened to fill the pool.
        """
        _, biddable = data_loader.load_players(converted_csv)
        assert len(biddable) > 600

        untranslated = []
        with open(converted_csv) as f:
            for row in csv.DictReader(f):
                if row["STATUS"] == "":
                    row = {**row, "STATUS": LEGACY_BLANK_STATUS}
                untranslated.append(row)
        path = Path(converted_csv).with_name("untranslated.csv")
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CANONICAL_COLUMNS)
            w.writeheader()
            w.writerows(untranslated)

        _, none_at_all = data_loader.load_players(str(path))
        assert none_at_all == {}

    def test_writing_an_empty_pool_is_refused(self):
        """The guard for the failure above, so it can never reach a file."""
        with pytest.raises(ValueError, match="0 biddable"):
            _require_biddables(
                [{"FCHL TEAM": "UFA", "STATUS": LEGACY_BLANK_STATUS}]
            )


class TestTheSynthesizedContractGroups:
    def test_minor_keepers_are_off_cap(self, converted):
        minors = [r for r in converted if r["STATUS"] == "MINOR"]
        assert minors
        assert all(r["GROUP"] not in config.MINOR_CAP_GROUPS for r in minors)

    def test_minor_keepers_cannot_be_bought_out(self, converted):
        """Asserted separately from the cap check on purpose.

        `config.py` documents at length why MINOR_CAP_GROUPS and
        BUYOUT_ELIGIBLE_GROUPS must not be merged despite identical membership.
        One assertion over both would re-merge them here.
        """
        minors = [r for r in converted if r["STATUS"] == "MINOR"]
        assert all(r["GROUP"] not in config.BUYOUT_ELIGIBLE_GROUPS for r in minors)

    def test_active_keepers_carry_a_real_contract(self, converted):
        active = [
            r
            for r in converted
            if r["STATUS"] == "START" and r["FCHL TEAM"] not in {"UFA", "RFA"}
        ]
        assert active
        assert all(r["GROUP"] in config.MINOR_CAP_GROUPS for r in active)

    def test_rfas_price_as_rfas_and_ufas_do_not(self, converted_csv):
        _, biddable = data_loader.load_players(converted_csv)
        by_team = {}
        with open(converted_csv) as f:
            for row in csv.DictReader(f):
                by_team[row["PLAYER"]] = row["FCHL TEAM"]
        rfa = [p for p in biddable.values() if by_team[p.name] == "RFA"]
        ufa = [p for p in biddable.values() if by_team[p.name] == "UFA"]
        assert rfa and ufa
        assert all(p.is_rfa for p in rfa)
        assert not any(p.is_rfa for p in ufa)


class TestTeamsTheLeagueDoesNotHave:
    def test_every_converted_row_lands_on_a_real_team(self, converted):
        known = league_team_codes(REPO / "data" / "fchl_teams.json") | {"UFA", "RFA"}
        assert {r["FCHL TEAM"] for r in converted} <= known

    def test_unknown_teams_are_reported_rather_than_dropped(self):
        """`build_initial_state` ignores an unknown team code without a word.

        The 2023 file carries an entry-draft class on a code the league has no
        TeamState for. Letting the loader swallow them would hide a real data
        decision, so the converter holds them back and names them.
        """
        _, skipped = convert(
            _legacy_rows(), league_team_codes(REPO / "data" / "fchl_teams.json")
        )
        assert skipped
        assert all(names for names in skipped.values())

    def test_the_script_prints_what_it_skipped(self, tmp_path):
        dest = tmp_path / "out.csv"
        r = subprocess.run(
            [sys.executable, "convert_legacy_players.py", str(LEGACY_CSV), str(dest)],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stderr
        _, skipped = convert(
            _legacy_rows(), league_team_codes(REPO / "data" / "fchl_teams.json")
        )
        for team, names in skipped.items():
            assert team in r.stderr
            assert str(len(names)) in r.stderr


class TestTheConvertedPoolIsDraftable:
    def test_the_committed_file_matches_a_fresh_conversion(self, converted):
        """So the artifact cannot drift from the script that derives it."""
        with open(CONVERTED_CSV) as f:
            assert list(csv.DictReader(f)) == converted

    def test_every_team_can_still_fill_a_roster(self, converted_csv, monkeypatch):
        """The invariant the commissioner's reserve rule guarantees.

        Without it the MILP's `== spots` constraint is unsatisfiable and the
        pool is unusable for the thing it was converted for.
        """
        monkeypatch.setattr(data_loader, "PLAYERS_CSV", converted_csv)
        state = data_loader.build_initial_state()
        for team in state.teams.values():
            spots = config.ROSTER_SIZE - len(team.roster_players)
            assert team.remaining_budget >= spots * config.MIN_SALARY, (
                f"{team.code} cannot fill {spots} spots with "
                f"${team.remaining_budget:.1f}M"
            )

    def test_the_pool_has_enough_players_for_the_picks_it_needs(
        self, converted_csv, monkeypatch
    ):
        monkeypatch.setattr(data_loader, "PLAYERS_CSV", converted_csv)
        state = data_loader.build_initial_state()
        needed = sum(
            config.ROSTER_SIZE - len(t.roster_players) for t in state.teams.values()
        )
        assert 0 < needed <= len(state.available_players)


class TestThePoolOverride:
    def test_build_initial_state_follows_the_global(self, converted_csv, monkeypatch):
        default = data_loader.build_initial_state()
        monkeypatch.setattr(data_loader, "PLAYERS_CSV", converted_csv)
        alternate = data_loader.build_initial_state()
        assert alternate.available_players.keys() != default.available_players.keys()

    def test_an_explicit_path_still_wins(self, converted_csv, monkeypatch):
        """The argument is not dead now that a global exists."""
        monkeypatch.setattr(data_loader, "PLAYERS_CSV", converted_csv)
        _, biddable = data_loader.load_players(str(REPO / "data" / "players.csv"))
        _, alternate = data_loader.load_players()
        assert biddable.keys() != alternate.keys()


class TestTheStateDirFollowsThePool:
    """A draft record is not a cache.

    `lifespan` reads the saved state before it reads any CSV, so an alternate
    pool sharing `data/state/` would load the real draft's JSON and then save
    over it. The directory is derived rather than left to a second variable the
    operator has to remember.
    """

    def test_the_default_pool_keeps_the_default_directory(self, monkeypatch):
        monkeypatch.setattr(
            data_loader, "PLAYERS_CSV", data_loader.DEFAULT_PLAYERS_CSV
        )
        assert main._default_state_dir() == "data/state"

    def test_an_alternate_pool_gets_its_own(self, monkeypatch, converted_csv):
        monkeypatch.setattr(data_loader, "PLAYERS_CSV", converted_csv)
        assert main._default_state_dir() != "data/state"

    def test_the_directory_is_named_for_the_pool(self, monkeypatch):
        monkeypatch.setattr(data_loader, "PLAYERS_CSV", "data/somewhere/pool-x.csv")
        assert main._default_state_dir() == "data/state-pool-x"

    def test_startup_names_the_pool_and_the_directory(self):
        """On uvicorn's logger, which is the only reason it is visible.

        uvicorn configures its own loggers and leaves root at WARNING, so a
        plain `logging.info` here prints nothing in a real run while reading as
        correct in the source -- measured against a live server, which showed
        four uvicorn lines and none of ours. The logger NAME is therefore the
        assertion; the message alone passes on either.

        The handler is attached to that logger DIRECTLY rather than read off
        `caplog`, which reaches records only by propagation to root. Uvicorn's
        own config gives the parent `uvicorn` logger `propagate: False`, so once
        any test has started a real server -- `test_browser_ui.py` does -- these
        records stop reaching root and a caplog version of this test fails for a
        reason that has nothing to do with what it checks. It did, in the full
        suite, while passing on its own.
        """
        import logging

        from fastapi.testclient import TestClient

        logger = logging.getLogger("uvicorn.error")
        seen: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = seen.append
        previous = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            with TestClient(main.app):
                pass
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)

        startup = [r for r in seen if "player pool" in r.getMessage()]
        assert startup, "startup did not log the pool on uvicorn's logger"
        assert main.STATE_DIR in startup[-1].getMessage()

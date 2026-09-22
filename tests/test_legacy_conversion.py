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
    DEFAULT_NHL_SOURCES,
    LEGACY_BLANK_STATUS,
    _require_biddables,
    convert,
    convert_all,
    convert_row,
    fill_nhl_teams,
    league_team_codes,
    lookup_nhl_team,
    nhl_team_index,
    normalize_name,
    valid_nhl_teams,
)

REPO = Path(__file__).resolve().parent.parent
LEGACY_CSV = REPO / "data" / "players-23.csv"
CONVERTED_CSV = REPO / "data" / "players-23-converted.csv"


def _legacy_rows() -> list[dict]:
    with open(LEGACY_CSV) as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def converted() -> list[dict]:
    """A fresh conversion, so these test the script and not a stale artifact.

    Goes through `convert_all`, the same entry point `main()` uses. Calling
    `convert` alone here left the fixture without the NHL join while the script
    wrote it, so the fixture and the committed file disagreed and the guard
    comparing them failed on the fixture rather than on the artifact.

    The NHL sources come from `DEFAULT_NHL_SOURCES` for the same reason, not
    from a path spelled out here: `main()` reads that tuple, so a second donor
    added there and not here would reopen exactly the divergence above, one
    donor further along.
    """
    rows, _, _ = convert_all(
        _legacy_rows(),
        league_team_codes(REPO / "data" / "fchl_teams.json"),
        [str(REPO / path) for path in DEFAULT_NHL_SOURCES],
        str(REPO / "data" / "team_odds.json"),
    )
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


class TestTheNhlJoinIsReported:
    """`main()` over the real legacy file, in process, for its stderr report.

    `--nhl-teams` is `action="append"`, so the script receives a list, and it
    passed that list to `os.path.exists` until 2026-09-22 — every explicit
    `--nhl-teams` was a TypeError. The report was also gated on the flag, so the
    default run said nothing about the clubs it could not fill.
    """

    def _run(self, tmp_path, capsys, *extra):
        from convert_legacy_players import main as convert_main

        rc = convert_main([str(LEGACY_CSV), str(tmp_path / "out.csv"), *extra])
        return rc, capsys.readouterr().err

    def test_an_explicit_source_runs_and_reports(self, tmp_path, capsys):
        rc, err = self._run(tmp_path, capsys, "--nhl-teams", "data/players.csv")
        assert rc == 0
        assert "NHL TEAM: filled" in err and "from data/players.csv" in err

    def test_the_default_run_reports_and_names_what_it_left_blank(self, tmp_path, capsys):
        rc, err = self._run(tmp_path, capsys)
        assert rc == 0
        assert "NHL TEAM: filled" in err
        with open(tmp_path / "out.csv") as f:
            blank = [r["PLAYER"] for r in csv.DictReader(f) if not r["NHL TEAM"]]
        assert blank, "the legacy pool has players no donor pool knows"
        assert f"unresolved, left blank: {blank[0]}" in err

    def test_a_missing_source_is_named_and_the_rest_still_used(self, tmp_path, capsys):
        rc, err = self._run(
            tmp_path, capsys,
            "--nhl-teams", str(tmp_path / "gone.csv"), "--nhl-teams", "data/players.csv",
        )
        assert rc == 0
        assert "gone.csv not found" in err
        assert "from data/players.csv" in err

    def test_no_source_at_all_says_every_player_prices_at_the_default(
        self, tmp_path, capsys
    ):
        rc, err = self._run(tmp_path, capsys, "--nhl-teams", str(tmp_path / "gone.csv"))
        assert rc == 0
        assert "DEFAULT_TEAM_PROBABILITY" in err


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


class TestTheNhlTeamJoin:
    """The legacy schema has no NHL TEAM, so it is joined from the current pool.

    Without it every player prices at `DEFAULT_TEAM_PROBABILITY` and one of the
    price model's ten features is flat across the whole pool.
    """

    def test_normalization_undoes_how_the_two_files_spell_a_name(self):
        """Synthetic names on purpose — these assert the RULES, not the data.

        Each case is a real difference between the files: a backtick for an
        apostrophe, a hyphen that `players.csv` renders as `0`, an `ari` it
        renders as `UTH` (its Arizona -> Utah rename catching the substring), a
        trailing parenthetical, and hyphen-vs-space.
        """
        assert normalize_name("Foo`Bar") == normalize_name("Foo'Bar")
        assert normalize_name("Quux0Zed") == normalize_name("Quux-Zed")
        assert normalize_name("LuostUTHnen") == normalize_name("Luostarinen")
        assert normalize_name("Widget (NCM)") == normalize_name("Widget")
        assert normalize_name("Ekman-Larsson") == normalize_name("Ekman Larsson")

    def test_the_join_fills_most_rows(self, converted):
        """0.80, down from 0.95, and the drop is data rather than a weaker guard.

        The donor is whatever the canonical pools happen to hold, and the
        2026-27 refresh cut `players.csv` from 2158 rows to 1268 by dropping 885
        unprojected prospects (owner decision 2026-09-15). Measured: coverage of
        the 2023 pool fell to **73.4%** on `players.csv` alone and comes back to
        **81.5%** once `players-25.csv` is chained behind it (see
        `fill_nhl_teams`). It cannot reach 95% again — a 2023 player who has left
        the league since is in no pool this repo carries — so the choice is a
        threshold that states the achievable figure or one that is permanently
        red. The guard still has teeth at 0.80: a join that broke outright, or
        lost a whole donor, lands far below it.
        """
        filled = [r for r in converted if r["NHL TEAM"]]
        assert len(filled) / len(converted) > 0.80

    def test_only_real_nhl_clubs_are_written(self, converted):
        """`players.csv` carries the FCHL placeholder `UFA` in its NHL TEAM
        column on several rows. Copying that through would put a league
        placeholder in a field that means an NHL club — invisible to the price
        model, which falls through to the default for an unknown code, but shown
        on screen as the player's team and indistinguishable from real data once
        written to a pool file.
        """
        allowed = valid_nhl_teams(str(REPO / "data" / "team_odds.json"))
        written = {r["NHL TEAM"] for r in converted if r["NHL TEAM"]}
        assert written <= allowed

    def test_a_name_two_players_share_is_left_blank(self, tmp_path):
        """Refusing beats guessing: a coin flip puts a wrong club on a real
        player silently.

        **Synthetic, and it has to be.** This was derived from `players.csv` on
        the stated grounds that "today's collisions are not tomorrow's" — which
        was the right instinct aimed at the wrong half. The rule needs two
        players sharing a name AND a position on DIFFERENT clubs, and whether
        the live pool contains such a pair is not something the pool owes
        anybody: the 2026-27 refresh left exactly one duplicate group, both on
        the same club, so the fixture found nothing and the test failed for
        having no material rather than for a broken lookup. A rule test supplies
        its own material, like `test_normalization_undoes_how_the_two_files_spell_a_name`
        two tests up. `test_a_name_only_one_player_has_resolves` reads the live
        pool and is the half that should.
        """
        source = tmp_path / "collide.csv"
        source.write_text(
            "PLAYER,POS,NHL TEAM\n"
            "Widget Sprocket,F,TOR\n"
            "Widget Sprocket,F,BOS\n"
        )
        full, loose = nhl_team_index(str(source), str(REPO / "data" / "team_odds.json"))
        assert lookup_nhl_team("Widget Sprocket", "F", full, loose) is None

    def test_a_name_only_one_player_has_resolves(self):
        """The other half — so a lookup that returned None for everything, which
        would satisfy the test above, fails here.
        """
        source = str(REPO / "data" / "players.csv")
        full, loose = nhl_team_index(source)
        solo = [
            (n, sorted(c)[0][0])
            for n, c in full.items()
            if len({t for _, t in c}) == 1
        ]
        assert solo
        name, position = solo[0]
        assert lookup_nhl_team(name, position, full, loose) is not None

    def test_a_missing_source_leaves_the_column_blank(self, tmp_path):
        rows = [{"PLAYER": "Nobody Atall", "POS": "F", "NHL TEAM": ""}]
        unresolved = fill_nhl_teams(rows, str(REPO / "data" / "players.csv"))
        assert unresolved == ["Nobody Atall"]
        assert rows[0]["NHL TEAM"] == ""

    def test_the_join_reaches_the_price_model(self, converted_csv, monkeypatch):
        """End to end: a filled column has to change what the model sees, or the
        join is decoration. Before it, every player carried the same figure.
        """
        monkeypatch.setattr(data_loader, "PLAYERS_CSV", converted_csv)
        state = data_loader.build_initial_state()
        probabilities = {p.team_probability for p in state.available_players.values()}
        assert len(probabilities) > 5
        at_default = sum(
            1
            for p in state.available_players.values()
            if p.team_probability == config.DEFAULT_TEAM_PROBABILITY
        )
        # 0.25, and the same measurement moved it as moved the row threshold
        # above: 142 of 668 (21.3%) with both donors chained, against 206 of 668
        # before `players-25.csv` was added behind the shrunken `players.csv`.
        assert at_default < 0.25 * len(state.available_players)

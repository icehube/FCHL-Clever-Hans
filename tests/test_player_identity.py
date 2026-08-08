"""A player's NAME is this app's primary key, so it has to be unique.

`biddable`, `available_players`, `market_prices`, `find_player`, the
transaction log, ~20 endpoints' `player` form field and the buyout dots' DOM
ids are all keyed on the name string. `data/players.csv` does not guarantee it:
on 2026-08-07 it carried 2158 rows and 2155 distinct names, and the three
collisions failed in two different ways.

- **Two biddable rows** — `biddable[name] = ...` overwrites and one player
  vanishes from the pool. `Matt Murray` (DAL and TOR) made 705 eligible rows
  load as 704, and the DAL one could not be drafted at all.
- **A roster row and a biddable row** — they go to different dicts, so nothing
  overwrites; the same name is simply owned AND draftable. `Jack Hughes` and
  `Elias Pettersson` are each a keeper on HSM and a UFA row, and only the
  zero-point exclusion keeps them apart. A projection refresh removes that.

`data_loader._disambiguated_names` suffixes colliding names. The synthetic
cases below pin the rules against CSVs this file owns; the two at the bottom
run against the real file and are written as invariants, because a test that
pins live numbers is the thing the whole refresh effort is removing.
"""

import csv

import pytest

import data_loader

from data_loader import (
    _disambiguated_names,
    last_disambiguations,
    load_players,
)

HEADER = "PLAYER,POS,GROUP,STATUS,FCHL TEAM,NHL TEAM,AGE,SALARY,BID,PTS,PRIOR FCHL TEAM"


def _csv(tmp_path, *rows: str) -> str:
    path = tmp_path / "players.csv"
    path.write_text("\n".join([HEADER, *rows]) + "\n")
    return str(path)


def _names(path: str) -> list[str]:
    with open(path) as f:
        return _disambiguated_names(list(csv.DictReader(f)))


class TestDisambiguation:
    def test_a_clean_file_is_left_alone(self, tmp_path):
        """The no-op case. An escalation that fires on unique names would
        rename the entire pool and break every saved link and DOM id."""
        path = _csv(
            tmp_path,
            "Connor McDavid,F,3,,UFA,EDM,27,0,0,132,",
            "Cale Makar,D,3,,UFA,COL,26,0,0,95,",
            "Connor Hellebuyck,G,3,,UFA,WPG,31,0,0,60,",
        )
        assert _names(path) == ["Connor McDavid", "Cale Makar", "Connor Hellebuyck"]
        assert last_disambiguations == {}

    def test_two_biddable_rows_both_survive(self, tmp_path):
        """The Matt Murray case: the collision that silently loses a player."""
        path = _csv(
            tmp_path,
            "Matt Murray,G,3,,UFA,DAL,30,0,0,5,",
            "Matt Murray,G,3,,UFA,TOR,30,0,0,5,",
        )
        _, biddable = load_players(path)
        assert sorted(biddable) == ["Matt Murray (DAL)", "Matt Murray (TOR)"]
        assert biddable["Matt Murray (DAL)"].nhl_team == "DAL"
        assert biddable["Matt Murray (TOR)"].nhl_team == "TOR"

    def test_a_keeper_and_a_free_agent_stop_sharing_a_name(self, tmp_path):
        """The Jack Hughes case: nothing overwrites, but the same string is
        both owned and draftable, so `/assign` cannot tell them apart."""
        path = _csv(
            tmp_path,
            "Jack Hughes,F,C,START,HSM,NJD,23,7.0,0,74,",
            "Jack Hughes,F,3,,UFA,LAK,22,0,0,31,",
        )
        team_players, biddable = load_players(path)
        rostered = {p.name for p in team_players["HSM"]["keepers"]}
        assert rostered == {"Jack Hughes (NJD)"}
        assert set(biddable) == {"Jack Hughes (LAK)"}
        assert not rostered & set(biddable)

    def test_two_teams_cannot_hold_the_same_name(self, tmp_path):
        path = _csv(
            tmp_path,
            "Sebastian Aho,F,3,START,BOT,CAR,27,5.0,0,80,",
            "Sebastian Aho,D,3,START,SRL,NYI,27,0.5,0,12,",
        )
        team_players, _ = load_players(path)
        assert [p.name for p in team_players["BOT"]["keepers"]] == ["Sebastian Aho (CAR)"]
        assert [p.name for p in team_players["SRL"]["keepers"]] == ["Sebastian Aho (NYI)"]

    def test_the_same_nhl_team_escalates_to_position(self, tmp_path):
        """The Elias Pettersson case: both are VAN, so the team suffix alone
        does not separate them."""
        path = _csv(
            tmp_path,
            "Elias Pettersson,F,2,START,HSM,VAN,26,9.0,0,89,",
            "Elias Pettersson,D,3,,UFA,VAN,21,0,0,14,",
        )
        assert _names(path) == [
            "Elias Pettersson (VAN F)",
            "Elias Pettersson (VAN D)",
        ]

    def test_every_row_in_a_group_is_suffixed(self, tmp_path):
        """Renaming only the later row gives `X` beside `X (VAN D)`, which
        reads as one player listed twice rather than two players."""
        path = _csv(
            tmp_path,
            "Elias Pettersson,F,2,START,HSM,VAN,26,9.0,0,89,",
            "Elias Pettersson,D,3,,UFA,VAN,21,0,0,14,",
        )
        assert all(n != "Elias Pettersson" for n in _names(path))

    def test_identical_rows_fall_through_to_ordinals(self, tmp_path):
        """Same name, same team, same position — neither suffix separates
        them, and the loader must still not drop one."""
        path = _csv(
            tmp_path,
            "John Smith,F,3,,UFA,BOS,25,0,0,40,",
            "John Smith,F,3,,UFA,BOS,25,0,0,40,",
        )
        assert _names(path) == ["John Smith (#1)", "John Smith (#2)"]

    def test_a_generated_name_never_steals_a_real_one(self, tmp_path):
        """If the file already contains the name the first tier would produce,
        that tier is unusable — otherwise disambiguating creates a collision."""
        path = _csv(
            tmp_path,
            "Matt Murray,G,3,,UFA,DAL,30,0,0,5,",
            "Matt Murray,G,3,,UFA,TOR,30,0,0,5,",
            "Matt Murray (DAL),G,3,,UFA,SJS,30,0,0,5,",
        )
        names = _names(path)
        assert len(set(names)) == len(names), names
        assert names[2] == "Matt Murray (DAL)", "the real row keeps its own name"

    def test_the_renames_are_reported(self, tmp_path):
        """The operator has to be told, or a suffix on screen looks like a bug
        in the data rather than a decision the loader made."""
        path = _csv(
            tmp_path,
            "Matt Murray,G,3,,UFA,DAL,30,0,0,5,",
            "Matt Murray,G,3,,UFA,TOR,30,0,0,5,",
        )
        load_players(path)
        assert last_disambiguations == {
            "Matt Murray": ["Matt Murray (DAL)", "Matt Murray (TOR)"]
        }

    def test_the_report_describes_the_current_file(self, tmp_path):
        """Stale renames would name players the loaded pool does not contain."""
        dirty = _csv(
            tmp_path,
            "Matt Murray,G,3,,UFA,DAL,30,0,0,5,",
            "Matt Murray,G,3,,UFA,TOR,30,0,0,5,",
        )
        load_players(dirty)
        assert last_disambiguations

        clean = tmp_path / "clean.csv"
        clean.write_text(f"{HEADER}\nConnor McDavid,F,3,,UFA,EDM,27,0,0,132,\n")
        load_players(str(clean))
        assert last_disambiguations == {}

    def test_goalie_wins_survive_a_rename(self, tmp_path):
        """goalie_projection_stats.csv is keyed on the RAW name and cannot
        disambiguate either, so the join has to use the source name. Looking up
        the rename finds nothing and silently degrades every renamed goalie to
        the pts/win approximation."""
        path = _csv(
            tmp_path,
            "Matt Murray,G,3,,UFA,DAL,30,0,0,5,",
            "Matt Murray,G,3,,UFA,TOR,30,0,0,5,",
        )
        _, biddable = load_players(path, goalie_wins={"Matt Murray": 2.0})
        assert biddable["Matt Murray (DAL)"].proj_wins == 2.0
        assert biddable["Matt Murray (TOR)"].proj_wins == 2.0


class TestTheRenamesReachTheOperator:
    """A suffix on screen with nothing explaining it reads as bad data.

    Uses `client` from conftest, so it goes through the real `GET /`.
    """

    def test_the_banner_names_every_rename(self, client):
        page = client.get("/").text
        assert data_loader.loaded_disambiguations, (
            "players.csv has no duplicate names, so this test proves nothing — "
            "if that is now true on purpose, delete it"
        )
        assert 'id="data-warning"' in page
        for original, replacements in data_loader.loaded_disambiguations.items():
            assert original in page
            for name in replacements:
                assert name in page

    def test_it_is_not_the_startup_banner(self, client):
        """Two banners with opposite lifecycles. Folded into one, a duplicate
        name in the CSV would leave the degraded-boot alarm permanently lit and
        nobody would read it — see `test_the_happy_path_shows_no_banner`."""
        page = client.get("/").text
        assert 'id="data-warning"' in page
        assert 'id="startup-warning"' not in page

    def test_it_survives_a_reset(self, client):
        """/reset clears the startup warning because a fresh start answers it.
        It does not answer a duplicate name in players.csv, which is still
        there — and `/reset` re-runs the loader, so the note must come back."""
        client.post("/reset")
        assert 'id="data-warning"' in client.get("/").text

    def test_loading_another_csv_does_not_rewrite_the_banner(self, client, tmp_path):
        """`load_players` is called by tests and by the pre-auction runbook.
        Sharing one global with the app let any of them blank the banner for
        whatever ran next, which is why `build_initial_state` keeps its own."""
        before = dict(data_loader.loaded_disambiguations)
        clean = tmp_path / "clean.csv"
        clean.write_text(f"{HEADER}\nConnor McDavid,F,3,,UFA,EDM,27,0,0,132,\n")
        load_players(str(clean))

        assert data_loader.last_disambiguations == {}
        assert data_loader.loaded_disambiguations == before
        assert 'id="data-warning"' in client.get("/").text


class TestTheLiveFileLoadsWholeAndUnambiguous:
    """Invariants, not counts. Both of these fail on the pre-fix loader.

    Deliberately derived from `data/players.csv` rather than pinned to a
    number: the point of the refresh work is that a new dataset must not need
    an edit here to stay meaningful.
    """

    @pytest.fixture(scope="class")
    def loaded(self):
        from data_loader import load_team_odds

        return load_players(team_odds=load_team_odds())

    @pytest.fixture(scope="class")
    def rows(self):
        with open("data/players.csv") as f:
            return list(csv.DictReader(f))

    def test_no_biddable_row_is_lost(self, loaded, rows):
        """Every eligible row must reach the pool. A repeated name silently
        overwrote one on 2026-08-07 — 705 rows in, 704 players out."""
        _, biddable = loaded
        eligible = [
            r for r in rows
            if r["FCHL TEAM"].strip() in {"UFA", "RFA"}
            and r["STATUS"].strip() == ""
            and int(r["PTS"] or 0) > 0
        ]
        assert len(biddable) == len(eligible)

    def test_nobody_is_owned_and_draftable_at_once(self, loaded):
        """The `test_stress.py` ownership invariant, checked at load instead of
        only after 40 simulated picks — a name on a roster AND in the pool can
        be drafted by a second team."""
        team_players, biddable = loaded
        owned: dict[str, str] = {}
        for code, players in team_players.items():
            for p in players["keepers"] + players["minors"]:
                assert p.name not in owned, (
                    f"{p.name} is on both {owned.get(p.name)} and {code}"
                )
                owned[p.name] = code

        double_listed = sorted(set(owned) & set(biddable))
        assert not double_listed, (
            f"{double_listed} are on a roster AND still biddable"
        )

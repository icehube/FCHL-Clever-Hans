"""The Streamlit auto-save converter and the 2025 pre-draft pool it produces.

`data/25-26 Starting Rosters.json` is an auto-save from the tool this app
replaced, written 2025-09-05 — days before the draft `FCHL Free Agent Draft.xlsx`
records on its `2025` sheet. It is the only genuine PRE-DRAFT snapshot of a
season we have with that season's own projections, which is what makes it worth
a converter: the 2023 pool has no prior salaries either, but its `PTS` are three
years stale.

The load-bearing case is `STATUS`, and it is the same trap
`test_legacy_conversion` documents wearing a different hat. The state file
writes `"NO"` where the canonical schema leaves it blank; `load_players` gates
the biddable branch on `status == ""` and UFA/RFA sit in `_PLACEHOLDER_TEAMS`,
so an untranslated row matches *neither* branch and is dropped in silence. All
651 free agents vanish and the app boots on an empty pool, which on screen is
indistinguishable from a finished draft.
"""

import csv
import json
from pathlib import Path

import pytest

import data_loader
from config import MIN_SALARY
from convert_legacy_players import CANONICAL_COLUMNS, league_team_codes
from convert_state_json import (
    STATE_BLANK_STATUS,
    _require_biddables,
    convert,
    convert_all,
    convert_row,
    load_state,
    valid_nhl_teams,
)

REPO = Path(__file__).resolve().parent.parent
STATE_JSON = REPO / "data" / "25-26 Starting Rosters.json"
CONVERTED_CSV = REPO / "data" / "players-25.csv"

# Placeholder names, never real ones: these rows are built here, so a pool name
# would only couple the test to `players.csv` (test_no_literal_player_names).
A_ROW = {
    "PLAYER": "X",
    "POS": "F",
    "GROUP": "3",
    "FCHL TEAM": "UFA",
    "NHL TEAM": "TBL",
    "AGE": 30,
    "STATUS": STATE_BLANK_STATUS,
    "SALARY": 0.0,
    "PTS": 50,
    "BID": 8.3,
}


def _row(**overrides) -> dict:
    return {**A_ROW, **overrides}


@pytest.fixture(scope="module")
def source() -> list[dict]:
    return load_state(str(STATE_JSON))


@pytest.fixture(scope="module")
def converted(source) -> list[dict]:
    """A fresh conversion through the same entry point `main()` uses.

    `convert_all` rather than `convert`, for the reason the legacy suite
    records: when the fixture and the script called different functions, the
    guard comparing the committed file against a fresh conversion failed on the
    fixture instead of on the artifact.
    """
    rows, _ = convert_all(source, league_team_codes(str(REPO / "data" / "fchl_teams.json")))
    return rows


class TestTheSchemaTranslation:
    def test_the_blank_state_status_becomes_an_empty_string(self):
        """The whole reason this converter exists. See the module docstring."""
        assert convert_row(_row(), valid_nhl_teams())["STATUS"] == ""

    @pytest.mark.parametrize("status", ["START", "MINOR"])
    def test_a_real_status_survives_untouched(self, status):
        assert convert_row(_row(STATUS=status), valid_nhl_teams())["STATUS"] == status

    def test_the_written_header_is_the_schema_the_loader_reads(self):
        with open(CONVERTED_CSV) as f:
            assert next(csv.reader(f)) == CANONICAL_COLUMNS

    def test_the_old_tools_predicted_bid_is_not_imported(self):
        """`BID` is 0 in source and filled during the auction.

        The state file carries a price on 145 players from the Streamlit tool's
        Z-score model — the model CLAUDE.md names as the reason this app was
        written. Importing it would preload the pool with the predictions of the
        thing being replaced, and they would read as bids already placed.
        """
        assert convert_row(_row(BID=8.3), valid_nhl_teams())["BID"] == "0"

    def test_prior_fchl_team_is_blank_because_there_is_no_source_for_it(self):
        assert convert_row(_row(), valid_nhl_teams())["PRIOR FCHL TEAM"] == ""

    def test_an_unrecognized_contract_group_passes_through_unchanged(self):
        """Two rows are in group `F`, which the documented vocabulary
        (`2 3 C RFA1 RFA2 A B D E`) does not have.

        It is in none of RFA_GROUPS, MINOR_CAP_GROUPS or
        BUYOUT_ELIGIBLE_GROUPS, so it already behaves like the A-E family.
        Remapping it to a letter we recognize would invent a contract the source
        does not record — so the converter must not touch it.
        """
        assert convert_row(_row(GROUP="F"), valid_nhl_teams())["GROUP"] == "F"

    def test_a_league_placeholder_in_the_nhl_team_column_is_dropped(self):
        """Three rows carry the FCHL `UFA` where an NHL club belongs.

        Invisible to pricing (`_get_team_probability` falls through to the
        default) but it would render as a club and, once in a pool file, look
        like data. Same guard `convert_legacy_players` needed.
        """
        assert convert_row(_row(**{"NHL TEAM": "UFA"}), valid_nhl_teams())["NHL TEAM"] == ""

    def test_a_real_club_is_kept(self):
        out = convert_row(_row(**{"NHL TEAM": "TBL"}), valid_nhl_teams())
        assert out["NHL TEAM"] == "TBL"


class TestTheEmptyPoolTrap:
    def test_skipping_the_status_translation_refuses_to_produce_a_file(self):
        """The failure this converter exists to prevent, asserted directly.

        Rows that keep `STATUS="NO"` satisfy no branch of `load_players`, so the
        pool is empty — and an empty pool looks like a finished draft rather
        than like an error.
        """
        untranslated = [dict(_row(), STATUS=STATE_BLANK_STATUS) for _ in range(5)]
        # convert_row is what does the translation, so bypass it to reproduce
        # the naive "rename the columns" conversion.
        naive = [
            {**{c: "" for c in CANONICAL_COLUMNS}, "FCHL TEAM": "UFA", "STATUS": "NO"}
            for _ in untranslated
        ]
        with pytest.raises(ValueError, match="0 biddable"):
            _require_biddables(naive)

    def test_the_real_conversion_has_free_agents(self, converted):
        assert _require_biddables(converted) > 0


class TestReadingTheAutoSave:
    def test_a_json_without_a_player_pool_says_so_by_name(self, tmp_path):
        p = tmp_path / "x.json"
        p.write_text(json.dumps({"teams_data": {}, "bid_history": []}))
        with pytest.raises(ValueError, match="no 'players_data'"):
            load_state(str(p))

    def test_an_empty_pool_in_the_file_is_an_error_not_an_empty_result(self, tmp_path):
        p = tmp_path / "x.json"
        p.write_text(json.dumps({"players_data": []}))
        with pytest.raises(ValueError, match="empty 'players_data'"):
            load_state(str(p))


class TestTeamsTheLeagueDoesNotHave:
    def test_rows_on_an_unknown_team_are_held_back_and_named(self, source):
        """`build_initial_state` drops an unknown FCHL TEAM without a word.

        The file carries an entry-draft class on a code `fchl_teams.json` does
        not list. Letting the loader swallow it would make a real data decision
        invisible, so `convert` separates them for the caller to report.
        """
        known = league_team_codes(str(REPO / "data" / "fchl_teams.json"))
        rows, skipped = convert(source, known, valid_nhl_teams())
        assert skipped, (
            "nothing was held back — either the file stopped carrying rows on "
            "an unknown team, or the guard stopped separating them"
        )
        for team, names in skipped.items():
            assert team not in known
            assert names
        held = {n for names in skipped.values() for n in names}
        assert held.isdisjoint(r["PLAYER"] for r in rows)

    def test_every_row_that_survives_is_placeable(self, converted):
        known = league_team_codes(str(REPO / "data" / "fchl_teams.json"))
        for r in converted:
            assert r["FCHL TEAM"] in known | {"UFA", "RFA"}, r


class TestTheCommittedFileIsWhatTheConverterProduces:
    def test_it_matches_a_fresh_conversion(self, converted):
        """Otherwise the CSV is a stale artifact and every test above is
        checking code that no longer produced it."""
        with open(CONVERTED_CSV) as f:
            on_disk = list(csv.DictReader(f))
        assert len(on_disk) == len(converted)
        assert on_disk == [dict(r) for r in converted]


class TestTheConvertedPoolIsDraftable:
    """The point of the whole exercise: a state you can actually run a draft on.

    Invariants rather than pinned numbers, deliberately — the file may be
    reconverted, and `tests/test_data_loader.py` records why a refresh drill
    that drowned two real bugs in nineteen exact figures is the failure mode to
    avoid.
    """

    @pytest.fixture(scope="class")
    def state(self):
        return data_loader.build_initial_state(players_path=str(CONVERTED_CSV))

    def test_the_pool_is_not_empty(self, state):
        assert len(state.available_players) > 100

    def test_every_league_team_is_built(self, state):
        known = league_team_codes(str(REPO / "data" / "fchl_teams.json"))
        assert set(state.teams) == known

    def test_it_has_restricted_free_agents(self, state):
        """A pool with no RFAs would silently disable half the nomination rule
        (1 RFA + 1 UFA per turn)."""
        assert sum(1 for p in state.available_players.values() if p.is_rfa) > 0

    def test_every_team_can_legally_fill_its_roster(self, state):
        """The commissioner rule (owner decision 2026-08-06): a bid that would
        leave a team unable to fill 24 is refused, so `remaining_budget <
        spots * MIN_SALARY` is unreachable — and the MILP's `== spots`
        constraint would be Infeasible from the first solve if the pool started
        there. A converted file that produces it is not draftable.
        """
        broke = [
            (code, t.remaining_budget, t.total_spots_remaining)
            for code, t in state.teams.items()
            if t.remaining_budget < t.total_spots_remaining * MIN_SALARY
        ]
        assert not broke, f"teams that cannot fill a roster from the start: {broke}"

    def test_there_are_more_players_than_spots_to_fill(self, state):
        """An auction needs a surplus. With one player per open spot every pick
        is forced, the market ceiling means nothing and the drain logic has
        nothing to choose between — see the 2025-results-sheet approach this
        converter replaced, which would have given 139 players for 137 spots.
        """
        spots = sum(t.total_spots_remaining for t in state.teams.values())
        assert spots > 0
        assert len(state.available_players) > 2 * spots

    def test_no_duplicate_names_needed_renaming(self, state):
        """The old tool already disambiguated its own collisions, so the
        `#data-warning` banner should stay silent on this pool. If this starts
        failing the file grew a collision and the banner is the right answer —
        but it should be a deliberate change, not a surprise on draft night.
        """
        assert data_loader.loaded_disambiguations == {}

"""Tests for data_loader.py: loading all data files and building initial state.

Three jobs, deliberately kept apart, because they fail for different reasons:

1. **`TestLoaderRules`** — does the loader apply the rules correctly? Pinned to
   `tests/fixtures/players_sample.csv`, a file this suite owns and no data
   refresh ever touches, so these assertions can stay exact.
2. **`TestLiveData*`** — is the real dataset well formed? Invariants only. Every
   one is true of any dataset the app could legitimately run on.
3. **`TestDataFingerprint`** — is it the dataset we last looked at? One test,
   one readable diff.

This file used to do all three with one set of exact numbers against
`data/players.csv` (704 biddable, BOT at $30.3M, EDM at 11.04, 165 picks). A
drill on 2026-08-07 that perturbed the data files produced **25 failures, 19 of
them here**, and reading them was the problem: legitimate arithmetic drowned
out the two failures that were a real bug. Splitting the jobs means a refresh
now produces exactly one failure — the fingerprint — while anything genuinely
broken still fails loudly on its own terms.
"""

import csv
import json
import os
from pathlib import Path

import pytest

from config import MIN_SALARY, ROSTER_SIZE, SALARY_CAP
from data_loader import (
    build_initial_state,
    load_goalie_wins,
    load_players,
    load_team_metadata,
    load_team_odds,
)

SAMPLE_CSV = str(Path(__file__).parent / "fixtures" / "players_sample.csv")
FINGERPRINT = Path(__file__).parent / "fixtures" / "data_fingerprint.json"

# Enough for the fixture's aliases without reading the live odds file — a rules
# test that depends on refreshable data is the thing this split is removing.
SAMPLE_ODDS = {"EDM": 11.04, "UTA": 2.02, "NYR": 5.0}


class TestLoaderRules:
    """Every branch in `load_players`, against data we own."""

    @pytest.fixture(scope="class")
    def loaded(self):
        return load_players(
            SAMPLE_CSV,
            team_odds=SAMPLE_ODDS,
            goalie_wins={"Sample Ufa Goalie": 31.0, "Sample Keeper Goalie": 20.0},
        )

    def test_keepers_and_minors_are_separated_by_status(self, loaded):
        team_players, _ = loaded
        bot = team_players["BOT"]
        assert [p.name for p in bot["keepers"]] == [
            "Sample Keeper Forward",
            "Sample Keeper Defence",
            "Sample Keeper Goalie",
        ]
        assert [p.name for p in bot["minors"]] == [
            "Sample Minor On Cap",
            "Sample Minor Off Cap",
        ]
        assert all(p.is_minor for p in bot["minors"])
        assert not any(p.is_minor for p in bot["keepers"])

    def test_each_fchl_team_gets_its_own_bucket(self, loaded):
        team_players, _ = loaded
        assert sorted(team_players) == ["BOT", "SRL"]

    def test_placeholder_teams_are_biddable(self, loaded):
        _, biddable = loaded
        assert set(biddable) == {
            "Sample Ufa Forward",
            "Sample Ufa Rookie",
            "Sample Ufa Goalie",
            "Sample Ufa Blank Fields",
            "Sample Ufa Utah",
            "Sample Rfa One",
            "Sample Rfa Two",
        }

    def test_zero_point_players_are_excluded(self, loaded):
        _, biddable = loaded
        assert "Sample Ufa Zero Points" not in biddable

    def test_a_placeholder_row_with_a_status_is_neither(self, loaded):
        """UFA/RFA plus a non-blank STATUS matches no branch, so the row is
        dropped rather than becoming a keeper of a team called "UFA"."""
        team_players, biddable = loaded
        assert "Sample Placeholder With Status" not in biddable
        assert "UFA" not in team_players

    def test_a_row_with_no_fchl_team_is_dropped(self, loaded):
        team_players, biddable = loaded
        assert "Sample No Fchl Team" not in biddable
        assert "" not in team_players

    def test_rfa_groups_set_the_rfa_flag(self, loaded):
        _, biddable = loaded
        assert biddable["Sample Rfa One"].is_rfa is True
        assert biddable["Sample Rfa Two"].is_rfa is True
        assert biddable["Sample Ufa Forward"].is_rfa is False

    def test_rfas_carry_their_prior_team(self, loaded):
        _, biddable = loaded
        assert biddable["Sample Rfa One"].prior_fchl_team == "GVR"
        assert biddable["Sample Rfa Two"].prior_fchl_team == "SRL"
        assert biddable["Sample Ufa Forward"].prior_fchl_team == ""

    def test_last_seasons_salary_is_the_reputation_feature(self, loaded):
        """SALARY on a biddable row is last season's price, not a cap figure —
        it feeds log_lag/has_lag. Blank means new to the league."""
        _, biddable = loaded
        assert biddable["Sample Ufa Forward"].salary == pytest.approx(7.3)
        assert biddable["Sample Ufa Rookie"].salary == 0.0

    def test_blank_age_and_salary_parse_as_zero(self, loaded):
        _, biddable = loaded
        assert biddable["Sample Ufa Blank Fields"].age == 0
        assert biddable["Sample Ufa Blank Fields"].salary == 0.0

    def test_team_probability_comes_from_the_odds(self, loaded):
        _, biddable = loaded
        assert biddable["Sample Rfa Two"].team_probability == pytest.approx(11.04)

    def test_an_alias_resolves_to_the_canonical_odds(self, loaded):
        """UTH in players.csv, UTA in team_odds.json."""
        _, biddable = loaded
        assert biddable["Sample Ufa Utah"].team_probability == pytest.approx(2.02)

    def test_an_unlisted_nhl_team_falls_back_to_the_default(self, loaded):
        from config import DEFAULT_TEAM_PROBABILITY

        _, biddable = loaded
        assert biddable["Sample Ufa Goalie"].team_probability == pytest.approx(
            DEFAULT_TEAM_PROBABILITY
        )

    def test_goalie_wins_attach_to_goalies_only(self, loaded):
        _, biddable = loaded
        assert biddable["Sample Ufa Goalie"].proj_wins == 31.0
        assert all(
            p.proj_wins is None for p in biddable.values() if p.position != "G"
        )

    def test_pos_ranks_are_within_position(self, loaded):
        """Scarcity is ranked inside a position, not across the pool."""
        _, biddable = loaded
        by_pos: dict[str, list] = {}
        for p in biddable.values():
            by_pos.setdefault(p.position, []).append(p)
        for pos, players in by_pos.items():
            ranks = sorted(p.pos_rank for p in players)
            assert ranks == list(range(1, len(players) + 1)), pos
            leader = max(players, key=lambda p: p.projected_points)
            assert leader.pos_rank == 1, f"{pos} leader {leader.name} is not rank 1"


class TestLoadTeamMetadata:
    def test_loads_all_teams(self):
        metadata = load_team_metadata()
        team_codes = [k for k, v in metadata.items() if isinstance(v, dict) and "id" in v]
        assert len(team_codes) == 11

    def test_bot_is_my_team(self):
        metadata = load_team_metadata()
        assert metadata["BOT"]["is_my_team"] is True

    def test_exactly_one_team_is_mine(self):
        metadata = load_team_metadata()
        mine = [
            k for k, v in metadata.items()
            if isinstance(v, dict) and v.get("is_my_team")
        ]
        assert mine == ["BOT"]

    def test_every_team_has_a_name(self):
        metadata = load_team_metadata()
        for code, info in metadata.items():
            if isinstance(info, dict) and "id" in info:
                assert info.get("name"), f"{code} has no name"

    def test_nomination_order_names_real_teams(self):
        """Length and membership, not who goes first — the order is league
        config and changes between seasons. The fingerprint records the order
        itself."""
        metadata = load_team_metadata()
        codes = {k for k, v in metadata.items() if isinstance(v, dict) and "id" in v}
        order = metadata["nomination_order"]
        assert len(order) == len(codes)
        assert set(order) == codes
        assert len(set(order)) == len(order), "a team appears twice in the order"

    def test_penalties_are_non_negative(self):
        metadata = load_team_metadata()
        for code, info in metadata.items():
            if isinstance(info, dict) and "id" in info:
                assert info.get("penalty", 0.0) >= 0, f"{code} has a negative penalty"


class TestLoadTeamOdds:
    def test_odds_are_percent_not_fractions(self):
        """The price model was trained on percentages, so the league sums to
        ~100. Stored as fractions; the conversion is the whole point of the
        loader. Aliases duplicate entries, so sum over the canonical names."""
        odds = load_team_odds()
        with open("data/team_odds.json") as f:
            canonical = json.load(f)["odds"]
        total = sum(odds[team] for team in canonical)
        assert total == pytest.approx(100.0, abs=1.0)
        assert all(v > 0 for v in odds.values())

    def test_uth_alias(self):
        odds = load_team_odds()
        assert odds["UTH"] == pytest.approx(odds["UTA"])


class TestLiveDataInvariants:
    """True of any well-formed dataset. A refresh must not need an edit here."""

    @pytest.fixture(scope="class")
    def loaded(self):
        return load_players(team_odds=load_team_odds(), goalie_wins=load_goalie_wins())

    @pytest.fixture(scope="class")
    def state(self):
        return build_initial_state()

    def test_the_pool_is_not_empty(self, loaded):
        _, biddable = loaded
        assert len(biddable) > 100, "a pool this small cannot fill 11 rosters"

    def test_every_pool_key_is_its_own_players_name(self, state):
        """The dict key and `Player.name` are one identity, not two.

        The pool key is this app's primary key — `/assign` pops by it, the market
        and model price dicts are keyed on it, `_dom_id` hashes it. Anything that
        looks a player up by `player.name` instead of by the key it iterated is
        relying on this, and `optimizer._nomination_pick` does exactly that for
        both figures the nomination panel prints side by side. Before 2026-08-18
        nothing tested it, and `to_json`/`from_json` store the key verbatim, so a
        mismatch would round-trip faithfully rather than heal.
        """
        wrong = {k: p.name for k, p in state.available_players.items() if k != p.name}
        assert not wrong, f"pool keys disagree with their own Player.name: {wrong}"

    def test_the_rfa_flag_agrees_with_the_fchl_team_column(self, loaded):
        """Two independent columns have to tell the same story.

        `is_rfa` comes from GROUP (`RFA_GROUPS`); the row is biddable at all
        because FCHL TEAM is UFA or RFA. A refresh that introduces a group code
        `RFA_GROUPS` has never heard of — `RFA3` — loads those players as UFAs
        silently: they keep their points, they stay in the pool, and the only
        visible effect is the RFA/UFA split moving, which is expected to move on
        any refresh and would be waved through.

        This replaces `len(ufa) + len(rfa) == len(biddable)`, which was an
        identity — `is_rfa` is a bool, so the two lists partition the dict by
        construction and no change to any code could falsify it.
        """
        _, biddable = loaded
        with open("data/players.csv") as f:
            declared = {
                r["PLAYER"].strip(): r["FCHL TEAM"].strip() for r in csv.DictReader(f)
            }

        rfa = [p for p in biddable.values() if p.is_rfa]
        assert rfa, "no RFAs at all is a parsing failure, not a league state"

        for p in biddable.values():
            # Renamed duplicates carry a suffix the CSV does not; they are
            # covered by tests/test_player_identity.py and skipped here rather
            # than reverse-engineered back to a source row.
            if p.name not in declared:
                continue
            assert p.is_rfa == (declared[p.name] == "RFA"), (
                f"{p.name} is group {p.group} on an FCHL TEAM of "
                f"{declared[p.name]}, but loaded with is_rfa={p.is_rfa}"
            )

    def test_every_biddable_scores(self, loaded):
        _, biddable = loaded
        assert all(p.projected_points > 0 for p in biddable.values())

    def test_every_biddable_is_ranked(self, loaded):
        _, biddable = loaded
        assert all(p.pos_rank >= 1 for p in biddable.values())

    def test_every_biddable_has_a_position(self, loaded):
        _, biddable = loaded
        assert {p.position for p in biddable.values()} <= {"F", "D", "G"}
        for pos in ("F", "D", "G"):
            assert any(p.position == pos for p in biddable.values()), f"no {pos} in pool"

    def test_every_rfa_has_a_prior_team(self, loaded):
        _, biddable = loaded
        missing = [p.name for p in biddable.values() if p.is_rfa and not p.prior_fchl_team]
        assert not missing, f"RFAs with no prior FCHL team (no ROFR): {missing}"

    def test_every_biddable_has_a_probability(self, loaded):
        _, biddable = loaded
        assert all(p.team_probability > 0 for p in biddable.values())

    def test_goalie_wins_reach_the_pool(self, loaded):
        _, biddable = loaded
        goalies = [p for p in biddable.values() if p.position == "G"]
        assert any(p.proj_wins for p in goalies), (
            "no biddable goalie matched goalie_projection_stats.csv — every one "
            "would fall back to the pts/win approximation"
        )
        assert all(p.proj_wins is None for p in biddable.values() if p.position != "G")

    def test_all_teams_loaded(self, state):
        assert len(state.teams) == 11

    def test_no_team_starts_over_the_cap(self, state):
        over = {
            c: t.total_salary for c, t in state.teams.items() if t.total_salary > SALARY_CAP
        }
        assert not over, f"teams start over the ${SALARY_CAP}M cap: {over}"

    def test_spendable_budget_reserves_the_remaining_spots(self, state):
        for code, team in state.teams.items():
            assert team.spendable_budget == pytest.approx(
                team.remaining_budget - team.total_spots_remaining * MIN_SALARY
            ), code

    def test_roster_needs_fit_the_starting_lineup(self, state):
        limits = {"F": 12, "D": 6, "G": 2}
        for code, team in state.teams.items():
            for pos, need in team.roster_needs.items():
                assert 0 <= need <= limits[pos], f"{code} needs {need} {pos}"

    def test_nobody_can_be_drafted_twice(self, state):
        """The `test_stress.py` ownership invariant at pick zero: a name on a
        roster AND in the pool can be bought by a second team."""
        owned: dict[str, str] = {}
        for code, team in state.teams.items():
            for p in team.keeper_players + team.minor_players:
                assert p.name not in owned, (
                    f"{p.name} is on both {owned.get(p.name)} and {code}"
                )
                owned[p.name] = code
        overlap = sorted(set(owned) & set(state.available_players))
        assert not overlap, f"{overlap} are on a roster AND still biddable"

    def test_the_draft_fits_in_the_rosters(self, state):
        total = sum(t.total_spots_remaining for t in state.teams.values())
        assert 0 < total <= len(state.teams) * ROSTER_SIZE

    def test_the_pool_can_fill_the_rosters(self, state):
        needed = sum(t.total_spots_remaining for t in state.teams.values())
        assert len(state.available_players) >= needed, (
            "fewer biddable players than open roster spots — the MILP goes "
            "Infeasible for everyone"
        )


def _fingerprint() -> dict:
    """The handful of live-data numbers worth one deliberate failure.

    Kept short so the diff stays scannable. Invariants above cannot notice a
    refresh that silently drops half the file; this can, and says so once
    rather than nineteen times.
    """
    state = build_initial_state()
    odds = load_team_odds()
    with open("data/team_odds.json") as f:
        canonical = json.load(f)["odds"]
    pool = list(state.available_players.values())

    by_position: dict[str, int] = {}
    for p in pool:
        by_position[p.position] = by_position.get(p.position, 0) + 1

    return {
        "teams": len(state.teams),
        "nomination_order": state.nomination_order,
        "biddable_total": len(state.available_players),
        "biddable_ufa": sum(1 for p in pool if not p.is_rfa),
        "biddable_rfa": sum(1 for p in pool if p.is_rfa),
        "biddable_by_position": dict(sorted(by_position.items())),
        "roster_players": sum(
            len(t.keeper_players) + len(t.minor_players) for t in state.teams.values()
        ),
        "bot_total_salary": round(state.teams["BOT"].total_salary, 2),
        "total_picks_needed": sum(
            t.total_spots_remaining for t in state.teams.values()
        ),
        "odds_sum_percent": round(sum(odds[t] for t in canonical), 2),
    }


class TestDataFingerprint:
    """One failure per data refresh, with a readable diff.

    Regenerate after a deliberate refresh:

        FCHL_WRITE_FINGERPRINT=1 .venv/bin/pytest tests/test_data_loader.py -k fingerprint

    That rewrites the file and then FAILS on purpose. Green needs a second run
    without the variable, so the guard cannot be left permanently self-healing
    by someone who exported the variable and forgot.
    """

    def test_matches_the_recorded_dataset(self):
        actual = _fingerprint()

        if os.environ.get("FCHL_WRITE_FINGERPRINT"):
            FINGERPRINT.write_text(json.dumps(actual, indent=2) + "\n")
            pytest.fail(
                f"fingerprint rewritten to {FINGERPRINT} — review `git diff` on it, "
                "then re-run WITHOUT FCHL_WRITE_FINGERPRINT to go green"
            )

        assert FINGERPRINT.exists(), (
            f"{FINGERPRINT} is missing; regenerate it with "
            "FCHL_WRITE_FINGERPRINT=1 pytest tests/test_data_loader.py -k fingerprint"
        )
        expected = json.loads(FINGERPRINT.read_text())

        changed = {
            key: f"{expected.get(key)!r} -> {value!r}"
            for key, value in actual.items()
            if expected.get(key) != value
        }
        assert not changed, (
            "the live data files no longer match tests/fixtures/data_fingerprint.json:\n"
            + "\n".join(f"  {k}: {v}" for k, v in sorted(changed.items()))
            + "\n\nIf this is a deliberate refresh, regenerate with:\n"
            "  FCHL_WRITE_FINGERPRINT=1 .venv/bin/pytest tests/test_data_loader.py -k fingerprint"
        )

    def test_the_fingerprint_covers_what_it_claims_to(self):
        """A fingerprint that quietly lost its fields would pass forever.

        Fields are listed here rather than derived from `_fingerprint()`, so
        deleting one from the builder fails instead of shrinking the check.
        """
        assert set(_fingerprint()) == {
            "teams",
            "nomination_order",
            "biddable_total",
            "biddable_ufa",
            "biddable_rfa",
            "biddable_by_position",
            "roster_players",
            "bot_total_salary",
            "total_picks_needed",
            "odds_sum_percent",
        }

    def test_the_recorded_counts_add_up(self):
        """Cheap internal consistency, so a hand-edited fingerprint that makes
        the guard green cannot also be nonsense."""
        recorded = json.loads(FINGERPRINT.read_text())
        assert (
            recorded["biddable_ufa"] + recorded["biddable_rfa"]
            == recorded["biddable_total"]
        )
        assert (
            sum(recorded["biddable_by_position"].values()) == recorded["biddable_total"]
        )
        assert len(recorded["nomination_order"]) == recorded["teams"]


class TestTheSampleFixtureStaysUsable:
    """The rules above are only as good as the file they read.

    A fixture that lost its edge cases would keep every assertion green while
    testing nothing interesting — the same vacuity trap as an ast guard whose
    scan finds nothing.
    """

    def test_the_sample_covers_every_branch(self):
        with open(SAMPLE_CSV) as f:
            rows = list(csv.DictReader(f))

        def has(**cols) -> bool:
            return any(
                all(r[k].strip() == v for k, v in cols.items()) for r in rows
            )

        assert has(STATUS="START"), "no keeper"
        assert has(STATUS="MINOR", GROUP="3"), "no on-cap minor"
        assert has(STATUS="MINOR", GROUP="A"), "no off-cap minor"
        assert has(**{"FCHL TEAM": "UFA", "STATUS": ""}), "no UFA"
        assert has(**{"FCHL TEAM": "RFA", "GROUP": "RFA1"}), "no RFA1"
        assert has(**{"FCHL TEAM": "RFA", "GROUP": "RFA2"}), "no RFA2"
        assert has(**{"FCHL TEAM": "UFA", "PTS": "0"}), "no zero-point exclusion case"
        assert has(**{"FCHL TEAM": "UFA", "NHL TEAM": "UTH"}), "no alias case"
        assert has(**{"FCHL TEAM": "UFA", "STATUS": "MINOR"}), "no placeholder+status case"
        assert has(**{"FCHL TEAM": ""}), "no blank-team case"
        assert has(AGE="", SALARY=""), "no blank numeric case"
        assert len({r["FCHL TEAM"] for r in rows} - {"UFA", "RFA", ""}) >= 2, (
            "only one FCHL team, so per-team bucketing is untested"
        )

    def test_the_sample_has_no_duplicate_names(self):
        """Collisions belong in test_player_identity.py. One here would make
        every count above wrong for a reason that file already covers."""
        with open(SAMPLE_CSV) as f:
            names = [r["PLAYER"].strip() for r in csv.DictReader(f)]
        assert len(set(names)) == len(names)

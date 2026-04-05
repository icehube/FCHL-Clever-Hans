"""Tests for data_loader.py: loading all data files and building initial state."""

import pytest

from config import SALARY_CAP
from data_loader import (
    build_initial_state,
    load_players,
    load_team_metadata,
    load_team_odds,
)


class TestLoadTeamMetadata:
    def test_loads_all_teams(self):
        metadata = load_team_metadata()
        team_codes = [k for k, v in metadata.items() if isinstance(v, dict) and "id" in v]
        assert len(team_codes) == 11

    def test_bot_is_my_team(self):
        metadata = load_team_metadata()
        assert metadata["BOT"]["is_my_team"] is True

    def test_nomination_order(self):
        metadata = load_team_metadata()
        order = metadata["nomination_order"]
        assert len(order) == 11
        assert order[0] == "BOT"

    def test_penalties(self):
        metadata = load_team_metadata()
        assert metadata["JHN"]["penalty"] == 0.3
        assert metadata["LGN"]["penalty"] == 0.3
        assert metadata["BOT"]["penalty"] == 0.0


class TestLoadTeamOdds:
    def test_loads_odds(self):
        odds = load_team_odds()
        assert odds["EDM"] == pytest.approx(0.1104)
        assert odds["FLA"] == pytest.approx(0.0974)

    def test_uth_alias(self):
        odds = load_team_odds()
        # UTH should be resolvable via the alias
        assert "UTH" in odds or "UTA" in odds
        # UTA should have the value
        assert odds.get("UTA", odds.get("UTH")) == pytest.approx(0.0202)


class TestLoadPlayers:
    @pytest.fixture
    def loaded(self):
        odds = load_team_odds()
        team_players, biddable = load_players(team_odds=odds)
        return team_players, biddable

    def test_biddable_count(self, loaded):
        # Zero-point players excluded from biddable pool
        _, biddable = loaded
        assert len(biddable) == 704

    def test_biddable_ufa_count(self, loaded):
        _, biddable = loaded
        ufas = [p for p in biddable.values() if not p.is_rfa]
        assert len(ufas) == 682

    def test_biddable_rfa_count(self, loaded):
        _, biddable = loaded
        rfas = [p for p in biddable.values() if p.is_rfa]
        assert len(rfas) == 22

    def test_biddable_with_points(self, loaded):
        _, biddable = loaded
        with_pts = [p for p in biddable.values() if p.projected_points > 0]
        assert len(with_pts) == 704

    def test_mcdavid_is_rfa(self, loaded):
        _, biddable = loaded
        mcdavid = biddable["Connor McDavid"]
        assert mcdavid.is_rfa is True
        assert mcdavid.group == "RFA2"
        assert mcdavid.prior_fchl_team == "GVR"

    def test_panarin_is_ufa(self, loaded):
        _, biddable = loaded
        panarin = biddable["Artemi Panarin"]
        assert panarin.is_rfa is False
        assert panarin.group == "3"

    def test_team_probability_edm(self, loaded):
        _, biddable = loaded
        mcdavid = biddable["Connor McDavid"]
        assert mcdavid.team_probability == pytest.approx(0.1104)

    def test_team_probability_uth_alias(self, loaded):
        _, biddable = loaded
        # Find a UTH player
        uth_players = [p for p in biddable.values() if p.nhl_team == "UTH"]
        assert len(uth_players) > 0
        # Should have resolved to UTA odds
        assert uth_players[0].team_probability == pytest.approx(0.0202)

    def test_bot_keepers(self, loaded):
        team_players, _ = loaded
        bot = team_players["BOT"]
        assert len(bot["keepers"]) == 12

    def test_bot_minors(self, loaded):
        team_players, _ = loaded
        bot = team_players["BOT"]
        assert len(bot["minors"]) == 37

    def test_all_rfa_prior_teams(self, loaded):
        _, biddable = loaded
        rfas = [p for p in biddable.values() if p.is_rfa]
        for rfa in rfas:
            assert rfa.prior_fchl_team != "", f"{rfa.name} missing prior FCHL team"


class TestBuildInitialState:
    @pytest.fixture
    def state(self):
        return build_initial_state()

    def test_all_teams_loaded(self, state):
        assert len(state.teams) == 11

    def test_bot_salary(self, state):
        bot = state.teams["BOT"]
        # 12 keepers + cap-eligible minors (4 goalies group 3 at $0.5 each = $2.0)
        assert bot.total_salary == pytest.approx(30.3)

    def test_bot_remaining_budget(self, state):
        bot = state.teams["BOT"]
        assert bot.remaining_budget == pytest.approx(SALARY_CAP - 30.3)

    def test_bot_spendable_budget(self, state):
        bot = state.teams["BOT"]
        # remaining = 26.5, reserved = 12 * 0.5 = 6.0, spendable = 20.5
        assert bot.spendable_budget == pytest.approx(20.5)

    def test_bot_roster_needs(self, state):
        bot = state.teams["BOT"]
        needs = bot.roster_needs
        assert needs == {"F": 7, "D": 4, "G": 1}

    def test_jhn_penalty(self, state):
        assert state.teams["JHN"].penalties == pytest.approx(0.3)

    def test_lgn_penalty(self, state):
        assert state.teams["LGN"].penalties == pytest.approx(0.3)

    def test_nomination_order(self, state):
        assert state.nomination_order[0] == "BOT"
        assert len(state.nomination_order) == 11

    def test_snake_draft(self, state):
        assert state.snake_draft is True

    def test_available_players(self, state):
        assert len(state.available_players) == 704

    def test_total_picks_needed(self, state):
        total = sum(t.total_spots_remaining for t in state.teams.values())
        assert total == 165

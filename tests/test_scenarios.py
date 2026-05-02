"""Tests for scenarios.py."""

import pytest

import scenarios
from config import MY_TEAM


def test_goalie_asymmetry_non_bot_have_two_goalies():
    state = scenarios.load("goalie-asymmetry")
    for code, team in state.teams.items():
        if code == MY_TEAM:
            continue
        goalies = [p for p in team.roster_players if p.position == "G"]
        assert len(goalies) >= 2, f"{code} only has {len(goalies)} goalies"


def test_goalie_asymmetry_assigned_at_min_salary():
    state = scenarios.load("goalie-asymmetry")
    for code, team in state.teams.items():
        if code == MY_TEAM:
            continue
        for p in team.acquired_players:
            if p.position == "G":
                assert p.salary == 0.5


def test_unknown_scenario_raises():
    with pytest.raises(KeyError):
        scenarios.load("not-a-scenario")

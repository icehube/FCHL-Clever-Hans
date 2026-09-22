"""Tests for `tests/measure_replay.py`.

Same rationale as `tests/test_measure_spend.py`: an instrument whose logic lives
only inside a `__main__` is one nothing can check, and `measure_ceiling.py`'s
numbers reached four documents that way. The pure halves here are `replay`,
`change_subject`, `ceiling_steps` and `fidelity`.

States are hand-built from two teams and a two-player pool. Nothing reads
`players.csv` — the arithmetic under test is budgets and a `min()`, and a real
pool would add live-data coupling and buy nothing. Player names are deliberately
not real ones (`tests/test_no_literal_player_names.py` walks this file).
"""

import ast
import json
from pathlib import Path

import pytest

from config import MAX_SALARY, MIN_SALARY, MY_TEAM, ROSTER_SIZE, SALARY_CAP
from state import (
    AuctionState,
    ChangeRecord,
    Player,
    PlayerOnRoster,
    TeamState,
    TransactionRecord,
)
from tests.measure_replay import (
    PickRow,
    ceiling_steps,
    change_subject,
    fidelity,
    load_events,
    pool_for,
    pool_mismatches,
    replay,
    report,
    summarize,
    verdict,
)

MAIN = Path(__file__).resolve().parent.parent / "main.py"

# Every description grammar `main._log_change` emits, as the CONSTANT fragments
# its f-strings are built from, keyed by kind. `test_the_descriptions_main_emits_
# still_parse` asserts main.py still matches this, because `change_subject` has
# to recover a player's name from these sentences and the name exists nowhere
# else on a ChangeRecord.
GRAMMARS = {
    "team-done": [" marked as "],
    "toggle-bench": [" → "],
    "move-to-minors": [" → minors"],
    "move-to-roster": [" → active"],
    "adjust-salary": [": $", "M → $", "M"],
}


def _roster(n: int = ROSTER_SIZE, salary: float = MIN_SALARY) -> list[PlayerOnRoster]:
    return [
        PlayerOnRoster(
            name=f"Filler {i}", position="F", group="3", salary=salary, projected_points=1
        )
        for i in range(n)
    ]


def _team(code: str, physical_max: float) -> TeamState:
    """A full-roster team whose physical max bid is exactly `physical_max`.

    Full roster on purpose: with no spots left `physical_max_bid` IS
    `remaining_budget`, with no MIN_SALARY reservation to add back, so one
    number sets it. (A full roster is not a zero ceiling — teams draft past 24
    and the extra goes to the minors on cap.) The money is absorbed by
    `penalties`, which `total_salary` counts.
    """
    filler = _roster()
    spent = sum(p.salary for p in filler)
    return TeamState(
        code=code, name=code, keeper_players=filler, penalties=SALARY_CAP - spent - physical_max
    )


def _pool(**prices: float) -> dict[str, Player]:
    return {
        name: Player(
            name=name,
            position="F",
            group="3",
            nhl_team="TBL",
            age=25,
            projected_points=50,
            is_rfa=False,
            salary=0.0,
            team_probability=3.1,
        )
        for name in prices
    }


def _state(pool: dict[str, Player], *teams: TeamState) -> AuctionState:
    return AuctionState(
        teams={MY_TEAM: TeamState(code=MY_TEAM, name=MY_TEAM), **{t.code: t for t in teams}},
        available_players=pool,
    )


def _pick(name: str, *, salary: float = 1.0, model: float = 1.0, team: str = MY_TEAM):
    from state import TransactionRecord

    return TransactionRecord(
        player_name=name,
        position="F",
        team_code=team,
        salary=salary,
        model_price=model,
        market_price=model,
        timestamp="2026-09-13T10:00:00",
        transaction_type="draft",
    )


class TestItCountsTheWholePoolNotJustWhoSold:
    """The one thing this instrument exists for.

    `measure_spend.py` reads `market_price < model_price` off the log, so it can
    only ever speak for players who SOLD. The MILP plans over everything still
    available, so a ceiling cutting six unsold stars is invisible to that file
    and is exactly what this one has to see.
    """

    def test_an_unsold_player_over_the_ceiling_is_counted(self):
        prices = {"Cheap Sample": 0.5, "Dear Sample": 9.0}
        state = _state(_pool(**prices), _team("AAA", 5.0), _team("BBB", 3.0))
        # second-highest of the two opponents = 3.0
        rows, problems = replay(state, [_pick("Cheap Sample", model=0.5)], prices, (1.0,))

        assert not problems
        assert rows[0].ceiling == 3.0
        # The player who sold was under the ceiling; the one still in the pool
        # was not. measure_spend.py would report zero here.
        assert rows[0].capped == (1,)

    def test_a_pool_entirely_under_the_ceiling_counts_nothing(self):
        prices = {"Cheap Sample": 0.5, "Modest Sample": 2.0}
        state = _state(_pool(**prices), _team("AAA", 5.0), _team("BBB", 3.0))
        rows, _ = replay(state, [_pick("Cheap Sample", model=0.5)], prices, (1.0,))
        assert rows[0].capped == (0,)

    def test_the_cut_is_quantized_the_way_the_panels_print_it(self):
        """$0.01 over the ceiling is not a cut — `market.is_capped`'s rule.

        A raw `model > ceiling` marks a player whose two figures render
        identically at one decimal, which reads as a display bug rather than as
        Layer 2 binding. The drain tie-break puts recommendations a cent under
        the ceiling routinely, so this is the common case and not an edge.
        """
        prices = {"Hair Over Sample": 3.01}
        state = _state(_pool(**prices), _team("AAA", 5.0), _team("BBB", 3.0))
        rows, _ = replay(state, [_pick("Hair Over Sample", model=3.01)], prices, (1.0,))
        assert rows[0].ceiling == 3.0
        assert rows[0].capped == (0,)

    def test_the_scales_are_applied_to_the_model_price(self):
        """The sensitivity sweep, which is what makes a compressed pool quotable."""
        prices = {"Sample One": 2.0, "Sample Two": 0.5}
        state = _state(_pool(**prices), _team("AAA", 5.0), _team("BBB", 3.0))
        rows, _ = replay(state, [_pick("Sample Two", model=0.5)], prices, (1.0, 2.0))
        # 2.0 is under a 3.0 ceiling; doubled to 4.0 it is not.
        assert rows[0].capped == (0, 1)


class TestPickOrdinals:
    def test_a_pick_number_is_one_based(self):
        prices = {"Sample One": 0.5, "Sample Two": 0.5}
        state = _state(_pool(**prices), _team("AAA", 5.0), _team("BBB", 3.0))
        rows, _ = replay(
            state,
            [_pick("Sample One", model=0.5), _pick("Sample Two", model=0.5)],
            prices,
            (1.0,),
        )
        assert [r.pick for r in rows] == [1, 2]

    def test_a_change_record_between_picks_does_not_advance_it(self):
        """Ordinals count picks, matching `measure_spend.first_bind` and
        `measure_ceiling`'s step labels. Counting every record instead would
        print pick numbers no other instrument agrees with."""
        prices = {"Sample One": 0.5, "Sample Two": 0.5}
        state = _state(_pool(**prices), _team("AAA", 5.0), _team("BBB", 3.0))
        between = ChangeRecord(
            timestamp="2026-09-13T10:00:00",
            kind="team-done",
            team_code="AAA",
            description="AAA marked as done",
        )
        rows, problems = replay(
            state,
            [_pick("Sample One", model=0.5), between, _pick("Sample Two", model=0.5)],
            prices,
            (1.0,),
        )
        assert not problems
        assert [r.pick for r in rows] == [1, 2]


class TestItSamplesBeforeThePickLands:
    """The row describes the market the pick was MADE in.

    Sampled after `_apply` instead, every row describes the next pick's market:
    the ceiling one pick late and a pool one player short — the same off-by-one
    the 1-based ordinals exist to prevent, and the 2026-09-22 grill found that
    mutant passing every test here and the real draft's own fidelity check.
    """

    def _drains_the_second_highest(self):
        prices = {"Sold Sample": 2.0, "Left Sample": 0.5}
        # AAA 5.0 and BBB 4.0: the ceiling is BBB's 4.0. BBB buys at 2.0,
        # leaving it 2.0 — so a ceiling read after the pick says 2.0.
        state = _state(_pool(**prices), _team("AAA", 5.0), _team("BBB", 4.0))
        rows, problems = replay(
            state, [_pick("Sold Sample", salary=2.0, model=2.0, team="BBB")], prices, (1.0,)
        )
        assert not problems
        return rows[0]

    def test_the_ceiling_is_the_one_bidding_happened_under(self):
        assert self._drains_the_second_highest().ceiling == 4.0

    def test_the_pool_still_holds_the_player_being_sold(self):
        assert self._drains_the_second_highest().pool == 2


def _txn(name: str, kind: str, team: str, salary: float = 1.0) -> TransactionRecord:
    return TransactionRecord(
        player_name=name, position="F", team_code=team, salary=salary,
        model_price=salary, market_price=salary,
        timestamp="2026-09-13T10:00:00", transaction_type=kind,
    )


class TestEveryTransactionTypeItApplies:
    """`_apply`'s branches beyond a draft pick, none of which had a test.

    The 2026-09-13 draft exercises all of them, so a broken branch showed up
    only as a budget drifting in the fidelity block — which, until the INVALID
    banner, printed below a headline that had already been believed.
    """

    def _two_teams(self):
        aaa, bbb = _team("AAA", 5.0), _team("BBB", 3.0)
        aaa.keeper_players.append(
            PlayerOnRoster(name="Traded Sample", position="F", group="3", salary=2.0,
                           projected_points=40)
        )
        aaa._invalidate_cache()
        return _state(_pool(), aaa, bbb)

    def test_a_trade_between_teams_moves_him(self):
        state = self._two_teams()
        _, problems = replay(state, [_txn("Traded Sample", "trade", "AAA→BBB")], {}, (1.0,))
        assert not problems
        assert state.teams["AAA"].find_player("Traded Sample") is None
        assert state.teams["BBB"].find_player("Traded Sample") is not None

    @pytest.mark.parametrize("order", ["out-first", "in-first"])
    def test_an_out_and_an_in_land_him_once_in_either_order(self, order):
        """`/trade-execute` logs both on one timestamp and not always out-first."""
        state = self._two_teams()
        out = _txn("Traded Sample", "trade_out", "AAA")
        into = _txn("Traded Sample", "trade_in", "BBB")
        events = [out, into] if order == "out-first" else [into, out]
        _, problems = replay(state, events, {}, (1.0,))
        assert not problems
        assert state.teams["AAA"].find_player("Traded Sample") is None
        held = state.teams["BBB"].find_player("Traded Sample")
        assert held is not None and held.projected_points == 40, (
            "he was rebuilt from the record rather than moved"
        )

    def test_a_trade_out_nothing_places_is_reported(self):
        state = self._two_teams()
        _, problems = replay(state, [_txn("Traded Sample", "trade_out", "AAA")], {}, (1.0,))
        assert any("Traded Sample" in p and "trade_out" in p for p in problems), problems

    def test_a_buyout_leaves_half_his_salary_on_the_cap(self):
        state = self._two_teams()
        before = state.teams["AAA"].penalties
        _, problems = replay(
            state, [_txn("Traded Sample", "buyout", "AAA", salary=2.0)], {}, (1.0,)
        )
        assert not problems
        assert state.teams["AAA"].find_player("Traded Sample") is None
        assert state.teams["AAA"].penalties == pytest.approx(before + 1.0)

    def test_an_unknown_type_is_reported_rather_than_skipped(self):
        state = self._two_teams()
        _, problems = replay(state, [_txn("Traded Sample", "gift", "AAA")], {}, (1.0,))
        assert any("gift" in p for p in problems), problems


class TestTheUnsoldPoolIsChecked:
    def test_an_agreeing_pool_is_clean(self):
        assert pool_mismatches({"Sample One": 0.5}, {"Sample One": 0.504}) == []

    def test_a_moved_price_is_reported_at_a_precision_that_shows_it(self):
        (line,) = pool_mismatches({"Sample One": 0.506}, {"Sample One": 0.514})
        assert "$0.514M" in line and "$0.506M" in line

    def test_a_player_the_csv_lacks_is_reported(self):
        (line,) = pool_mismatches({"Sample One": 0.5}, {})
        assert "Sample One" in line and "not in this CSV" in line


_SAME = [("AAA", (24, 0, 1.0), (24, 0, 1.0))]


class TestTheVerdict:
    def test_a_faithful_replay_has_no_banner(self):
        assert verdict([], [], [], _SAME) == []

    @pytest.mark.parametrize("sold, unsold, problems, teams", [
        (["x"], [], [], _SAME),
        ([], ["x"], [], _SAME),
        ([], [], ["x"], _SAME),
        ([], [], [], [("AAA", (24, 0, 1.0), (24, 0, 0.7))]),
    ], ids=["sold", "unsold", "not-applied", "team"])
    def test_any_one_divergence_invalidates_it(self, sold, unsold, problems, teams):
        lines = verdict(sold, unsold, problems, teams)
        assert lines and "INVALID" in lines[0]


class TestTheChangeLogIsNotOptional:
    def test_marking_a_team_done_moves_the_ceiling(self):
        """Replaying transactions alone would hold a finished team's dead budget
        in the ceiling for the rest of the draft, silently. The 2026-09-13 draft
        flipped this switch 19 times."""
        prices = {"Sample One": 0.5, "Sample Two": 0.5}
        state = _state(
            _pool(**prices), _team("AAA", 5.0), _team("BBB", 3.0), _team("CCC", 1.0)
        )
        done = ChangeRecord(
            timestamp="2026-09-13T10:00:00",
            kind="team-done",
            team_code="BBB",
            description="BBB marked as done",
        )
        rows, _ = replay(
            state,
            [_pick("Sample One", model=0.5), done, _pick("Sample Two", model=0.5)],
            prices,
            (1.0,),
        )
        # Second-highest of {5.0, 3.0, 1.0} is 3.0; with BBB done it is 1.0.
        assert [r.ceiling for r in rows] == [3.0, 1.0]

    def test_marking_a_team_still_drafting_puts_it_back(self):
        """Four of the draft's 19 team-done records were un-dones, which is why
        the reconstructed ceiling rises at four points rather than only falling.
        A parser reading 'marked as' and stopping would miss the direction."""
        prices = {"Sample One": 0.5, "Sample Two": 0.5}
        state = _state(_pool(**prices), _team("AAA", 5.0), _team("BBB", 3.0))
        state.teams["BBB"].is_done = True
        back = ChangeRecord(
            timestamp="2026-09-13T10:00:00",
            kind="team-done",
            team_code="BBB",
            description="BBB marked as still drafting",
        )
        rows, _ = replay(
            state,
            [_pick("Sample One", model=0.5), back, _pick("Sample Two", model=0.5)],
            prices,
            (1.0,),
        )
        assert rows[0].ceiling == 5.0  # only AAA bidding: the highest is the price
        assert rows[1].ceiling == 3.0  # BBB back, so second-highest again


class TestTheDescriptionGrammarMainActuallyEmits:
    """`change_subject` parses a human sentence, so a reword in `main.py` breaks
    a ceiling reconstruction months later with nothing to say so."""

    def test_every_kind_main_logs_is_one_the_replay_handles(self):
        tree = ast.parse(MAIN.read_text())
        kinds = {
            node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_log_change"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        }
        assert kinds, "found no _log_change calls — did the helper get renamed?"
        assert kinds == set(GRAMMARS), (
            f"main.py logs {sorted(kinds)}; measure_replay handles {sorted(GRAMMARS)}"
        )

    def test_the_descriptions_main_emits_still_parse(self):
        """The literal text of each f-string, straight out of main.py's ast.

        Compared as the constant fragments rather than by rendering the
        f-string, because two of the five interpolate a conditional expression
        and one a format spec — and it is the literal separators
        (`" → "`, `": $"`) that `change_subject` splits on anyway.
        """
        tree = ast.parse(MAIN.read_text())
        found: dict[str, list[str]] = {}
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_log_change"
                and len(node.args) == 3
                and isinstance(node.args[0], ast.Constant)
            ):
                desc = node.args[2]
                parts = desc.values if isinstance(desc, ast.JoinedStr) else [desc]
                found[node.args[0].value] = [
                    p.value for p in parts if isinstance(p, ast.Constant)
                ]
        assert found == GRAMMARS, f"main.py's wording moved: {found}"

    @pytest.mark.parametrize(
        "kind,description",
        [
            ("toggle-bench", "Sample Player → bench"),
            ("toggle-bench", "Sample Player → active"),
            ("move-to-minors", "Sample Player → minors"),
            ("move-to-roster", "Sample Player → active"),
            ("adjust-salary", "Sample Player: $1.0M → $2.0M"),
        ],
    )
    def test_the_name_comes_back_out(self, kind, description):
        rec = ChangeRecord(
            timestamp="2026-09-13T10:00:00", kind=kind, team_code="AAA", description=description
        )
        assert change_subject(rec) == "Sample Player"

    def test_an_adjust_salary_does_not_keep_the_old_salary_in_the_name(self):
        """The specific trap: `adjust-salary` contains ` → ` like the other four,
        so an `rsplit(" → ")` returns `"Name: $1.0M"` and then finds no such
        player. The 2026-09-13 draft logged none of these, so only this test
        covers it."""
        rec = ChangeRecord(
            timestamp="2026-09-13T10:00:00",
            kind="adjust-salary",
            team_code="AAA",
            description="Sample Player: $1.0M → $2.0M",
        )
        assert "$" not in change_subject(rec)


class TestCeilingSteps:
    def test_only_changes_are_recorded_with_the_pick_they_happened_on(self):
        rows = [
            PickRow(pick=i, ceiling=c, pool=0, dearest=0.0, model_price=0.0, salary=0.0, capped=())
            for i, c in enumerate([11.4, 11.4, 7.3, 7.3, 0.5], start=1)
        ]
        assert ceiling_steps(rows) == [(1, 11.4), (3, 7.3), (5, 0.5)]

    def test_a_ceiling_that_rises_is_a_step_too(self):
        """Un-marking a team done raises it, which happened four times in the
        real draft. A steps list built on `<` would drop those."""
        rows = [
            PickRow(pick=i, ceiling=c, pool=0, dearest=0.0, model_price=0.0, salary=0.0, capped=())
            for i, c in enumerate([2.5, 2.7], start=1)
        ]
        assert ceiling_steps(rows) == [(1, 2.5), (2, 2.7)]


class TestFidelity:
    def test_a_matching_replay_reports_equal_tuples(self):
        state = _state(_pool(), _team("AAA", 5.0))
        saved = AuctionState.from_json(state.to_json())
        assert all(got == want for _, got, want in fidelity(state, saved))

    def test_a_divergence_is_reported_rather_than_raised(self):
        """The only way this instrument can lie is by landing somewhere the
        draft did not, so the check has to survive to be printed."""
        state = _state(_pool(), _team("AAA", 5.0))
        saved = AuctionState.from_json(state.to_json())
        saved.teams["AAA"].penalties += 1.0
        saved.teams["AAA"]._invalidate_cache()
        rows = fidelity(state, saved)
        assert any(got != want for _, got, want in rows)


class TestSummarize:
    def test_no_picks_says_so_rather_than_reporting_zeros(self):
        assert summarize([]) == {"picks": 0}

    def test_the_headline_counts_picks_at_the_first_scale(self):
        rows = [
            PickRow(pick=1, ceiling=3.0, pool=2, dearest=9.0, model_price=1.0, salary=1.0,
                    capped=(1, 2)),
            PickRow(pick=2, ceiling=3.0, pool=1, dearest=0.5, model_price=0.5, salary=0.5,
                    capped=(0, 1)),
        ]
        s = summarize(rows, (1.0, 2.0))
        assert s["changed_a_pool_price"] == 1
        assert s["first"] == 1
        assert s["worst"] == 1
        assert s["sweep"][1] == {"scale": 2.0, "picks": 2, "first": 1, "worst": 2}

    def test_never_binding_reports_none_rather_than_a_pick_number(self):
        rows = [
            PickRow(pick=1, ceiling=MAX_SALARY, pool=1, dearest=1.0, model_price=1.0,
                    salary=1.0, capped=(0,))
        ]
        s = summarize(rows, (1.0,))
        assert s["first"] is None
        assert s["at_max"] == 1


class TestPoolDerivation:
    def test_a_named_state_directory_names_its_pool(self):
        assert pool_for(Path("data/state-players-25/auction_state.json")) == Path(
            "data/players-25.csv"
        )

    def test_the_default_directory_falls_back_to_the_default_pool(self):
        import data_loader

        assert pool_for(Path("data/state/auction_state.json")) == Path(data_loader.PLAYERS_CSV)


class TestReportDegradesInsteadOfRaising:
    def test_a_missing_file_is_not_an_error_at_all(self, tmp_path, capsys):
        assert report(tmp_path / "nope.json") == 0
        assert "no state file" in capsys.readouterr().out

    def test_a_corrupt_state_names_the_file(self, tmp_path, capsys):
        """A `.corrupt` state is exactly what someone points this at — `lifespan`
        renames a file it cannot parse, and the reason to open one is to find out
        what the draft contained."""
        bad = tmp_path / "auction_state.json"
        bad.write_text("{not json")
        assert report(bad) == 1, "no measurement was made"
        assert str(bad) in capsys.readouterr().out

    def test_a_missing_pool_says_which_one_it_looked_for(self, tmp_path, capsys):
        state = tmp_path / "auction_state.json"
        state.write_text(json.dumps({"teams": {}, "available_players": {}}))
        assert report(state, pool=tmp_path / "nosuch.csv") == 1
        out = capsys.readouterr().out
        assert "nosuch.csv" in out and "--pool" in out

    def test_an_empty_log_says_so_instead_of_replaying_nothing(self, tmp_path, capsys):
        state = tmp_path / "auction_state.json"
        state.write_text(AuctionState(teams={}, available_players={}).to_json())
        assert report(state, pool=Path("data/players.csv")) == 0
        assert "nothing to replay" in capsys.readouterr().out


class TestAnInvalidReplaySaysSoFirst:
    """End to end over the live pool: one pick, logged faithfully or not.

    The live pool rather than `players_sample.csv` because `report` builds its
    state through `build_initial_state`, which rewrites
    `data_loader.loaded_disambiguations` in place — the data banner's source.
    The live pool rewrites it with what it already held.
    """

    def _one_pick(self, tmp_path, logged_off_by: float) -> Path:
        import data_loader
        from price_model import load_model_params, predict_all_prices

        state = data_loader.build_initial_state()
        name = next(iter(state.available_players))
        player = state.available_players.pop(name)
        price = predict_all_prices({name: player}, load_model_params())[name].expected_price
        state.teams[MY_TEAM].add_acquired_player(PlayerOnRoster.from_pool(player, 1.0))
        state.transaction_log.append(_pick(name, salary=1.0, model=price + logged_off_by))
        path = tmp_path / "auction_state.json"
        path.write_text(state.to_json())
        return path

    def test_a_faithful_replay_exits_zero_with_no_banner(self, tmp_path, capsys):
        assert report(self._one_pick(tmp_path, 0.0), pool=Path("data/players.csv")) == 0
        out = capsys.readouterr().out
        assert "INVALID" not in out
        assert "0 mismatch(es) over 1 picks" in out
        assert "unsold pool vs saved  : 0 mismatch(es)" in out

    def test_a_divergence_exits_one_and_is_named_above_the_headline(self, tmp_path, capsys):
        assert report(self._one_pick(tmp_path, 1.0), pool=Path("data/players.csv")) == 1
        out = capsys.readouterr().out
        assert "INVALID" in out
        assert out.index("INVALID") < out.index("picks with >=1 capped"), (
            "the banner printed below the headline it invalidates"
        )


class TestLoadEvents:
    def test_both_logs_come_back_interleaved_by_timestamp(self, tmp_path):
        state = tmp_path / "auction_state.json"
        state.write_text(
            json.dumps(
                {
                    "transaction_log": [
                        {
                            "player_name": "Sample Player",
                            "position": "F",
                            "team_code": MY_TEAM,
                            "salary": 1.0,
                            "model_price": 1.0,
                            "market_price": 1.0,
                            "timestamp": "2026-09-13T10:00:02",
                            "transaction_type": "draft",
                        }
                    ],
                    "change_log": [
                        {
                            "timestamp": "2026-09-13T10:00:01",
                            "kind": "team-done",
                            "team_code": "AAA",
                            "description": "AAA marked as done",
                        }
                    ],
                }
            )
        )
        events = load_events(state)
        assert [type(e).__name__ for e in events] == ["ChangeRecord", "TransactionRecord"]

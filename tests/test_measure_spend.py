"""Tests for `tests/measure_spend.py`'s pure summary.

An instrument gets a test here, which the other two do not, and that is the
point of splitting `summarize()` out. `measure_layout.py` could not see a stale
selector for two days (its own backlog entry) and `measure_ceiling.py`'s numbers
now appear in four documents unchecked — both because their logic only exists
inside a `__main__` that nothing runs.

Records are hand-built rather than derived from a loaded state: the whole file
is about arithmetic over a log, so a fixture would add live-data coupling and
buy nothing. Nothing here reads `players.csv`.
"""

import json
from pathlib import Path

from state import TransactionRecord
from tests.measure_spend import report, summarize


def _rec(
    name: str = "P",
    *,
    salary: float = 1.0,
    model: float = 2.0,
    market: float = 2.0,
    team: str = "BOT",
    kind: str = "draft",
) -> TransactionRecord:
    return TransactionRecord(
        player_name=name,
        position="F",
        team_code=team,
        salary=salary,
        model_price=model,
        market_price=market,
        timestamp="2026-08-17T10:00:00",
        transaction_type=kind,
    )


class TestSummarizeCountsTheRightRecords:
    def test_an_empty_log_reports_no_picks_rather_than_zeros(self):
        """A fresh install has an empty log — that is the normal case, not a finding.

        A zero-filled summary would print a full table of $0.0M and read as
        "the draft spent nothing", which is a different claim from "there is no
        draft here".
        """
        s = summarize([])
        assert s["picks"] == 0
        assert "spent" not in s, (
            "an empty log returned the full summary shape, so the report will "
            "print a table of zeros instead of saying there is nothing to read"
        )

    def test_only_draft_records_are_purchases(self):
        """Allowlist, never denylist — and here the denylist would be wrong twice.

        A trade's `salary` is the contract moving between teams, not a clearing
        price, and `/trade-between` writes "SRL→MAC" into `team_code`, so a
        per-team total that included it would attribute spend to a team that
        does not exist.
        """
        s = summarize([
            _rec("bought", salary=3.0),
            _rec("traded", salary=9.0, kind="trade_in"),
            _rec("swapped", salary=7.0, team="SRL→MAC", kind="trade"),
            _rec("cut", salary=5.0, kind="buyout"),
        ])
        assert s["picks"] == 1
        assert s["skipped_types"] == 3
        assert s["spent"] == 3.0, (
            f"non-draft salaries reached the league spend total ({s['spent']} "
            f"against 3.0 bought at auction)"
        )
        assert list(s["by_team"]) == ["BOT"], (
            f"a non-team code reached the per-team breakdown: {list(s['by_team'])}"
        )

    def test_a_missing_model_price_is_excluded_and_counted(self):
        """`_log_transaction` defaults both prices to 0, so 0 is missing data.

        Left in, it invents a bind: `market_price < model_price` is False when
        both are 0, but the moment only ONE is missing the comparison fires on
        nothing. The count has to be reported rather than silently dropped —
        two unpriced picks out of three is a different dataset from three
        priced ones.
        """
        s = summarize([
            _rec("priced", model=5.0, market=4.0),
            _rec("no model", model=0.0, market=0.0),
            _rec("half logged", model=0.0, market=3.0),
        ])
        assert s["picks"] == 3, "unpriced picks are still picks and still spend money"
        assert s["unpriced"] == 2
        assert s["ceiling_changed_a_price"] == 1, (
            "a pick with no model price was counted in the ceiling statistics"
        )

    def test_the_bind_test_is_market_strictly_below_model(self):
        """Equal prices mean the ceiling did NOT change anything.

        `market_price = min(model_price, ceiling)`, so equality is the normal
        case — it is what "the ceiling was above this player's model price"
        looks like in the log, i.e. Layer 2 doing nothing. Counting it would
        report the market layer as active on every pick of every draft.
        """
        s = summarize([
            _rec("untouched", model=4.0, market=4.0),
            _rec("capped", model=9.0, market=6.5),
        ])
        assert s["ceiling_changed_a_price"] == 1
        assert s["first_bind"] == 2, "the SECOND pick is the capped one"
        assert s["bind_gap_max"] == 2.5
        assert s["bind_gap_total"] == 2.5

    def test_first_bind_is_a_pick_ordinal_not_an_index_into_the_priced_subset(self):
        """The report prints this as "pick N", so it must count real picks.

        Indexed into `priced` it shifts down by however many unpriced records
        came before it. Both synthetic drafts have zero unpriced records, so the
        cross-check against `measure_ceiling.py` agreed either way — the bug
        would have shipped behind a measurement that looked like proof.
        """
        s = summarize([
            _rec("unpriced", model=0.0, market=0.0),
            _rec("unpriced too", model=0.0, market=0.0),
            _rec("capped", model=8.0, market=3.0),
        ])
        assert s["first_bind"] == 3, (
            f"first_bind is {s['first_bind']}: it must be a 1-based ordinal over "
            f"ALL picks. 2 means it indexed the priced subset, 1 or 2 means it is "
            f"0-based — and the report prints it as 'pick N' either way"
        )

    def test_no_bind_reports_none_rather_than_a_pick_number(self):
        """`first_bind` is what the report turns into "never"."""
        s = summarize([_rec(model=4.0, market=4.0)])
        assert s["ceiling_changed_a_price"] == 0
        assert s["first_bind"] is None
        assert isinstance(s["bind_gap_total"], float), (
            "sum() of an empty generator is an int, and the report formats this "
            "with a bare {} so the type reaches the screen as $0M"
        )


def _state_file(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "auction_state.json"
    path.write_text(body)
    return path


class TestTheReportSurvivesWhatPeopleActuallyOpen:
    """`report()` reads a file off disk, so its failures are file failures.

    Every case here is a file someone plausibly points this at. The one that
    motivated the guard: the app renames a state it cannot parse to `.corrupt`
    instead of deleting it, and the reason to open a `.corrupt` file is to find
    out what the draft contained — so the failure that brings people to this tool
    was the one failure it could not survive.

    These go through `report()` rather than `summarize()` deliberately. The
    summary is pure and covered above; what is being pinned here is that the
    process prints something usable and returns, which is a property of the CLI.
    """

    def test_an_empty_log_says_so_instead_of_printing_a_table_of_zeros(
        self, tmp_path, capsys
    ):
        """The behaviour `summarize`'s two-shape return exists to produce.

        The shape assertion above and this one catch different things: that one
        fails if the dict starts carrying zeroed keys, this one fails if the
        report stops branching on them. Neither implies the other — a refactor
        could zero-fill the dict and keep the guard, or keep the dict and drop
        the guard, and only one test notices each.
        """
        report(_state_file(tmp_path, '{"transaction_log": []}'))
        out = capsys.readouterr().out
        assert "nothing to measure" in out, (
            f"an empty log did not say so; the report printed:\n{out}"
        )
        assert "league spend" not in out, (
            f"the report printed its spend table for a log with no picks, so a "
            f"fresh install reads as a draft that spent $0.0M:\n{out}"
        )

    def test_a_corrupt_state_names_the_file_rather_than_raising(self, tmp_path, capsys):
        """A truncated write is what `.corrupt` files usually are."""
        path = _state_file(tmp_path, '{"transaction_log": [{"player_name": "X"')
        report(path)
        out = capsys.readouterr().out
        assert str(path) in out and "JSONDecodeError" in out, (
            f"a corrupt state must name the file and what went wrong, got:\n{out}"
        )

    def test_a_record_missing_a_price_key_is_reported_rather_than_raising(
        self, tmp_path, capsys
    ):
        """Not a JSON error — `_transaction_from_dict` reads nine keys positionally.

        A hand-edited or half-written log parses fine and then raises
        `KeyError: 'model_price'`, which is why the guard cannot be
        `json.JSONDecodeError` alone.
        """
        record = {
            "player_name": "X", "position": "F", "team_code": "BOT",
            "salary": 1.0, "timestamp": "t", "transaction_type": "draft",
        }
        path = _state_file(tmp_path, json.dumps({"transaction_log": [record]}))
        report(path)
        out = capsys.readouterr().out
        assert "KeyError" in out and "model_price" in out, (
            f"a log record missing a price must be reported, not raised:\n{out}"
        )

    def test_a_missing_file_is_not_an_error_at_all(self, tmp_path, capsys):
        """The default path is the operator's live state, which may not exist yet."""
        report(tmp_path / "nope.json")
        assert "no state file at" in capsys.readouterr().out

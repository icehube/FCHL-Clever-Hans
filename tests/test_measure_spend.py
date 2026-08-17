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

from state import TransactionRecord
from tests.measure_spend import summarize


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
        assert s["first_bind"] == 1, "the index should point at the capped pick"
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
        assert s["first_bind"] == 2, (
            f"first_bind is {s['first_bind']}, which is the index within the "
            f"priced subset — the report would name the wrong pick"
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

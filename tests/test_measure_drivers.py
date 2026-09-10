"""Tests for `tests/measure_drivers.py`.

An instrument gets a test here for the reason `test_measure_spend.py` states:
`measure_layout.py` could not see a stale selector for two days and
`measure_ceiling.py`'s numbers reached four documents unchecked, both because
their logic lived only inside a `__main__` nothing runs. The figures this one
produces are about to be quoted in `BACKLOG.md`, `CHANGELOG.md` and the pricing
rules file, so they need to be reproducible.
"""

import ast
from pathlib import Path

import pytest

from price_model import load_model_params, points_slopes
from tests.measure_drivers import (
    hinge_cost,
    load_pool,
    points_inversions,
    price_inversions,
    slope_table,
)

SOURCE = Path(__file__).parent / "measure_drivers.py"


@pytest.fixture(scope="module")
def loaded():
    return load_pool()


class TestItCannotTouchALiveDraft:
    def test_it_never_imports_main(self):
        """The structural guarantee, stronger than a STATE_DIR redirect.

        `measure_layout.py` imports `main` and redirects `STATE_DIR` before
        serving; this file reads the CSV and nothing else, so there is no state
        directory for it to get wrong. Asserted rather than trusted, because
        one convenient `import main` would silently remove the property.
        """
        tree = ast.parse(SOURCE.read_text())
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "main" not in imported, (
            "measure_drivers imports main, so it can now write the operator's "
            "state directory — read the CSV instead"
        )


class TestTheSlopeTable:
    def test_it_matches_coefficients_summed_independently(self):
        """The table is the instrument's central claim, so it is checked
        against the arithmetic rather than against itself."""
        params = load_model_params()
        rows = {(pos, bp): slope for pos, bp, slope, _ in slope_table(params)}
        for pos in ("F", "D", "G"):
            p = params[pos]
            base = p["coef_projected_points"]
            assert rows[(pos, 0.0)] == pytest.approx(base)
            assert rows[(pos, 60.0)] == pytest.approx(base + p["coef_pts_hinge_60"])
            assert rows[(pos, 80.0)] == pytest.approx(
                base + p["coef_pts_hinge_60"] + p["coef_pts_hinge_80"]
            )

    def test_it_flags_exactly_the_negative_segments(self):
        params = load_model_params()
        flagged = {(pos, bp) for pos, bp, _, bad in slope_table(params) if bad}
        expected = {
            (pos, bp)
            for pos in ("F", "D", "G")
            for bp, slope in points_slopes(params[pos])
            if slope < 0
        }
        assert flagged == expected


class TestTheTwoInversionMeasuresMeanDifferentThings:
    """The distinction the first draft of this instrument got wrong.

    It reported only "more points, cheaper overall" and called that the
    defect. D has 898 of those with a points slope that is positive at every
    segment — a lesser scorer on a better team with a bigger reputation
    SHOULD cost more. Isolating the Points driver is what turns the number
    into a diagnosis, and these tests are what keep the two apart.
    """

    def test_the_isolated_measure_finds_the_forward_defect(self, loaded):
        pool, params, refs = loaded
        found = points_inversions(pool, params, refs, "F")
        assert found, (
            "no F points-driver inversions — if the pricer notebook was "
            "refit, this instrument's reason for existing is gone and "
            "BACKLOG.md's entry should be closed"
        )
        better, worse, gap, factor_better, factor_worse = found[0]
        assert better.projected_points > worse.projected_points
        assert factor_worse > factor_better
        assert gap == pytest.approx(factor_worse - factor_better)

    @pytest.mark.parametrize("position", ["D", "G"])
    def test_the_isolated_measure_clears_the_healthy_positions(
        self, loaded, position
    ):
        """D and G have non-negative slopes everywhere, so the defect measure
        must report nothing — which is also what says it is not simply
        matching every pair it is handed."""
        pool, params, refs = loaded
        assert points_inversions(pool, params, refs, position) == []

    def test_the_overall_measure_fires_on_a_healthy_position_too(self, loaded):
        """The reason the two are reported separately.

        If this ever goes to zero the distinction has stopped being load-
        bearing and the instrument can be simplified — but while it holds,
        quoting the overall count as the defect overstates it.
        """
        pool, params, refs = loaded
        assert price_inversions(pool, params, refs, "D"), (
            "D no longer has price inversions from the non-points drivers, so "
            "the two measures no longer need separating"
        )

    @pytest.mark.parametrize("measure", [points_inversions, price_inversions])
    def test_both_are_sorted_by_the_size_of_the_gap(self, loaded, measure):
        pool, params, refs = loaded
        gaps = [row[2] for row in measure(pool, params, refs, "F")]
        assert gaps == sorted(gaps, reverse=True)


class TestHingeCost:
    def test_it_covers_exactly_the_forwards_past_the_knot(self, loaded):
        pool, params, refs = loaded
        rows, _ = hinge_cost(pool, params, refs)
        named = {p.name for p, _, _ in rows}
        expected = {
            p.name for p in pool.values()
            if p.position == "F" and p.projected_points > 80
        }
        assert named == expected

    def test_removing_the_hinge_raises_every_one_of_them(self, loaded):
        """The direction is the finding: the hinge is SUPPRESSING these prices."""
        pool, params, refs = loaded
        rows, total = hinge_cost(pool, params, refs)
        for player, live, without in rows:
            assert without >= live, (
                f"{player.name} got cheaper without the negative hinge, which "
                f"inverts the whole diagnosis"
            )
        assert total == pytest.approx(sum(w - l for _, l, w in rows))
        assert total > 0

    def test_it_does_not_mutate_the_params_it_was_given(self, loaded):
        """It deep-copies to zero the coefficient. Sharing the dict would leave
        every later prediction in the process running on a model that is not
        the deployed one."""
        pool, params, refs = loaded
        before = params["F"]["coef_pts_hinge_80"]
        hinge_cost(pool, params, refs)
        assert params["F"]["coef_pts_hinge_80"] == before


def test_the_report_runs_and_prints_every_section(loaded, capsys):
    from tests.measure_drivers import report

    report(position="F", top=3, pairs=2)
    out = capsys.readouterr().out
    for heading in (
        "reference", "points slope per segment", "price vs points",
        "driver mix", "by expected price", "SMALLER points",
        "costs less OVERALL", "cost of the negative F hinge",
    ):
        assert heading in out, f"the report is missing its {heading!r} section"

"""Startup recovery from a damaged state file.

The disaster this file exists to prevent: the app is four hours into a live
auction, `auction_state.json` gets truncated by a crash or a full disk, and
startup silently discards 150 picks in favour of a fresh draft — with the last
good copy sitting untouched next to it as `.backup`. It got worse on the next
click, because `_save_state` rotated the *corrupt* file over that backup.

`lifespan` now walks current -> backup -> fresh, and sets an unusable file
aside as `.corrupt` so the rotation cannot reach the backup. None of that path
had a single test before this file.

Each test drives `lifespan` for real by entering `TestClient` as a context
manager; a bare `TestClient(app)` never runs startup and would assert nothing.
"""

import json
import os

import pytest
from fastapi.testclient import TestClient


# Everything lifespan and _recompute own. Restoring `auction_state` alone is
# WORSE than restoring nothing: it pairs a restored roster with a MILP solution
# and market prices solved against a different one, which is the stale-derived-
# value hazard _recompute()'s own docstring is about. Unreachable today only
# because every other test file context-manages TestClient (re-running
# lifespan) — order-dependence that is invisible at the call site.
_APP_GLOBALS = (
    "STATE_DIR", "auction_state", "model_prices",
    "market_prices", "market_info", "milp_solution", "last_trade_eval",
)


@pytest.fixture
def state_dir(tmp_path):
    """Point the app at an empty temp STATE_DIR, restoring the globals after."""
    import main

    saved = {name: getattr(main, name) for name in _APP_GLOBALS}
    main.STATE_DIR = str(tmp_path)
    yield tmp_path
    for name, value in saved.items():
        setattr(main, name, value)
    # Cleared rather than snapshotted, for the reason _recompute gives: an empty
    # cache cannot serve a stale entry, and these are keyed by player name only.
    main._marginal_cache.clear()
    main._counterfactual_cache.clear()
    main.buyout_indicators.clear()
    main._startup_warning = None


def _draft(client, player: str, team: str, salary: float):
    """Draft `player` to `team`, asserting he actually landed.

    The status code alone proves nothing: `/assign` answers 200 with a warning
    toast for a name that is not in the biddable pool, and returns *before*
    `_save_state`. A typo'd name therefore writes nothing to disk while looking
    like a successful pick — which is precisely how the backup-rotation test
    below first passed against a build whose rename had been removed.
    """
    r = client.post("/assign", data={
        "player": player, "team": team, "salary": salary})
    assert r.status_code == 200, r.text
    # acquired OR minors: a team at 24 auto-routes the pick to the minors
    # (owner decision 2026-08-06), so acquired-only would fail on a full roster
    # for a pick that landed correctly. No team here is near 24 — this keeps the
    # helper reusable rather than fixing a reachable bug.
    import main
    t = main.auction_state.teams[team]
    landed = {p.name for p in t.acquired_players + t.minor_players}
    assert player in landed, r.headers.get("HX-Trigger", "no toast")
    return r


def _acquired(code: str) -> set[str]:
    """Names drafted by `code` this auction.

    Acquired only, not keepers or minors: a fresh state already carries those
    from the CSVs, so they cannot distinguish a recovered draft from a lost one.
    """
    import main
    return {p.name for p in main.auction_state.teams[code].acquired_players}


def _an_available_player() -> str:
    """Any name the current state will actually accept a bid on.

    Read off the live pool rather than hardcoded, because a hardcoded name that
    turns out to be somebody's keeper makes the draft a silent no-op.
    """
    import main
    return next(iter(main.auction_state.available_players))


def _good_state_with_pick(state_dir, player="Connor McDavid", team="BOT"):
    """A saved auction with one pick in it, as current + backup on disk.

    Written by the app itself rather than by hand: a fixture JSON blob would
    drift from `to_json` and the test would then be recovering a shape the app
    no longer writes.
    """
    import main

    with TestClient(main.app) as c:
        c.post("/reset")
        _draft(c, player, team, 8.0)
    current = state_dir / "auction_state.json"
    assert current.exists(), "the draft did not reach disk"
    # /reset saved once and /assign saved again, so a rotated backup already
    # exists; copy the post-pick file over it so both copies hold the pick.
    (state_dir / "auction_state.json.backup").write_text(current.read_text())
    return player, team


class TestRecoveryLadder:
    def test_corrupt_current_recovers_from_backup(self, state_dir):
        """The headline case: a truncated file must not cost the draft."""
        import main

        player, team = _good_state_with_pick(state_dir)
        (state_dir / "auction_state.json").write_text('{"teams": {"BOT"')

        with TestClient(main.app) as c:
            assert c.get("/").status_code == 200
            assert player in _acquired(team)

    def test_missing_current_recovers_from_backup(self, state_dir):
        """Absent is as recoverable as corrupt — the pick is still on disk."""
        import main

        player, team = _good_state_with_pick(state_dir)
        os.remove(state_dir / "auction_state.json")

        with TestClient(main.app) as c:
            assert c.get("/").status_code == 200
            assert player in _acquired(team)

    def test_wrong_shape_recovers(self, state_dir):
        """Valid JSON, wrong shape: `data["teams"].items()` on a list.

        This raises AttributeError, which the pre-fix `except
        (JSONDecodeError, KeyError, ValueError)` did not catch — so the app
        failed to *start* rather than degrading, the worst outcome for the one
        file whose job is to survive a crash.
        """
        import main

        player, team = _good_state_with_pick(state_dir)
        (state_dir / "auction_state.json").write_text('{"teams": []}')

        with TestClient(main.app) as c:
            assert c.get("/").status_code == 200
            assert player in _acquired(team)

    def test_both_unusable_starts_fresh_rather_than_failing(self, state_dir):
        """No recoverable copy is a bad day, not a dead app."""
        import main

        player, team = _good_state_with_pick(state_dir)
        (state_dir / "auction_state.json").write_text("not json at all")
        (state_dir / "auction_state.json.backup").write_text("{")

        with TestClient(main.app) as c:
            assert c.get("/").status_code == 200
            assert _acquired(team) == set(), "expected a fresh draft"
            assert player in main.auction_state.available_players

    def test_good_current_is_used_and_backup_untouched(self, state_dir):
        """The happy path must not regress: no rename, no fallback."""
        import main

        player, team = _good_state_with_pick(state_dir)
        backup = state_dir / "auction_state.json.backup"
        before = backup.read_text()

        with TestClient(main.app) as c:
            assert c.get("/").status_code == 200
            assert player in _acquired(team)
        assert backup.read_text() == before
        assert not (state_dir / "auction_state.json.corrupt").exists()


class TestABrokenBackfillDoesNotCostTheDraft:
    """A file that PARSES is usable, whatever the fixups afterwards do.

    Found by grilling the first version of this file, which wrapped the parse
    and all three backfills in one `except`. One raise from
    `_backfill_model_inputs` — plausible on exactly the legacy snapshots it
    exists to serve — renamed a byte-perfect draft `.corrupt`, fell through to a
    backup that failed identically, and started fresh returning 200. The draft
    was gone and the app looked normal.
    """

    @pytest.fixture
    def broken_backfill(self, monkeypatch):
        """Make the riskiest backfill raise, leaving the state file pristine."""
        import main

        def boom(state):
            raise TypeError("'<' not supported between 'NoneType' and 'float'")

        monkeypatch.setattr(main, "_backfill_model_inputs", boom)
        return boom

    def test_the_draft_survives(self, state_dir, broken_backfill):
        import main

        player, team = _good_state_with_pick(state_dir)

        with TestClient(main.app) as c:
            assert c.get("/").status_code == 200
            assert player in _acquired(team), "a good draft was thrown away"

    def test_the_good_file_is_not_renamed(self, state_dir, broken_backfill):
        """The rename must mean "unparseable", or the name is a lie."""
        import main

        _good_state_with_pick(state_dir)
        before = (state_dir / "auction_state.json").read_text()

        with TestClient(main.app) as c:
            assert c.get("/").status_code == 200
        assert (state_dir / "auction_state.json").read_text() == before
        assert not (state_dir / "auction_state.json.corrupt").exists()

    def test_it_says_so_on_screen(self, state_dir, broken_backfill):
        """Stale model inputs are not cosmetic — prices come off them.

        Asserts the operator-facing consequence, not the function name: the
        banner is read mid-auction by someone deciding whether to trust a price.
        """
        import main

        _good_state_with_pick(state_dir)

        with TestClient(main.app) as c:
            page = c.get("/").text
        assert "PRICES MAY BE WRONG" in page, "the banner does not say what broke"
        assert "stale" in page


class TestDegradedStartupIsVisible:
    """A lost or downgraded draft must not look like a normal start.

    The whole failure mode being guarded is silence: every one of these paths
    used to answer 200 with a clean-looking page, so the only evidence was a
    line in a terminal nobody watches mid-auction.
    """

    def test_recovering_from_the_backup_says_so(self, state_dir):
        import main

        _good_state_with_pick(state_dir)
        (state_dir / "auction_state.json").write_text("{ truncated")

        with TestClient(main.app) as c:
            page = c.get("/").text
        assert "backup copy" in page
        assert "one save behind" in page, "the operator is not told what it costs"

    def test_a_forced_fresh_start_says_so_and_names_the_file(self, state_dir):
        import main

        _good_state_with_pick(state_dir)
        (state_dir / "auction_state.json").write_text("not json at all")
        (state_dir / "auction_state.json.backup").write_text("{")

        with TestClient(main.app) as c:
            page = c.get("/").text
        assert "NEW auction" in page
        assert "auction_state.json.corrupt" in page, "cannot find the salvage"

    def test_the_happy_path_shows_no_banner(self, state_dir):
        """Without this the banner becomes wallpaper and stops being read."""
        import main

        _good_state_with_pick(state_dir)

        with TestClient(main.app) as c:
            page = c.get("/").text
        assert 'id="startup-warning"' not in page

    def test_reset_clears_it(self, state_dir):
        """A deliberate fresh start answers the warning; leaving it up lies."""
        import main

        _good_state_with_pick(state_dir)
        (state_dir / "auction_state.json").write_text("{ truncated")

        with TestClient(main.app) as c:
            assert 'id="startup-warning"' in c.get("/").text
            c.post("/reset")
            assert 'id="startup-warning"' not in c.get("/").text


class TestBackupSurvivesTheRestart:
    """The second half of the bug, and the half that made it unrecoverable.

    Recovering once is not enough: `_save_state` rotates `auction_state.json`
    into `.backup` on every save, so a startup that leaves a corrupt file in
    place destroys the good backup on the very next click.
    """

    def test_corrupt_file_is_set_aside(self, state_dir):
        import main

        _good_state_with_pick(state_dir)
        (state_dir / "auction_state.json").write_text("{ truncated")

        with TestClient(main.app) as c:
            assert c.get("/").status_code == 200
        assert (state_dir / "auction_state.json.corrupt").read_text() == "{ truncated"

    def test_backup_still_parses_after_a_further_save(self, state_dir):
        """Recover, then draft again — the backup must still be a real state.

        Without the `.corrupt` rename, this save rotates the corrupt file over
        the backup and `json.load` below raises.
        """
        import main

        player, team = _good_state_with_pick(state_dir)
        (state_dir / "auction_state.json").write_text("{ truncated")

        with TestClient(main.app) as c:
            assert c.get("/").status_code == 200
            _draft(c, _an_available_player(), "SRL", 9.0)

        with open(state_dir / "auction_state.json.backup") as f:
            data = json.load(f)
        assert player in json.dumps(data["teams"][team]), \
            "the backup no longer holds the recovered draft"

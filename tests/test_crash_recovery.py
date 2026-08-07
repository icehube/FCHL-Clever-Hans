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


@pytest.fixture
def state_dir(tmp_path):
    """Point the app at an empty temp STATE_DIR, restoring the globals after.

    Both `STATE_DIR` and `auction_state` are module globals that `lifespan`
    writes, so without the restore the first recovery test here would leave a
    hand-built state installed for every test that runs afterwards.
    """
    import main

    saved_dir, saved_state = main.STATE_DIR, main.auction_state
    main.STATE_DIR = str(tmp_path)
    yield tmp_path
    main.STATE_DIR, main.auction_state = saved_dir, saved_state


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
    assert player in _acquired(team), r.headers.get("HX-Trigger", "no toast")
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

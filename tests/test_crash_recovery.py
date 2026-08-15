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

from tests.helpers import section_of, toast_of


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


def _a_pre_auction_minor() -> tuple[str, str]:
    """Team code + name of somebody in the minors of the CURRENT state.

    Read off the loaded state rather than hardcoded, per CLAUDE.md: players.csv
    is replaced before every draft and a literal name stops matching silently.
    Every player in the minors of a *fresh* state got there from the CSV's
    `STATUS = MINOR`, so any of them is a keeper by provenance.
    """
    import main
    for code, team in main.auction_state.teams.items():
        if team.minor_players:
            return code, team.minor_players[0].name
    pytest.fail("no team has a minor-league player — the CSV cannot exercise this")


def _legacy_state_without_keeper_flags(
    state_dir, build=lambda c: None, key="is_keeper", scope=None
):
    """Save an auction through the app, then strip `key` from it.

    Defaults to `is_keeper`, which landed 2026-08-08, so this is precisely the
    shape of a file saved mid-auction by the previous build. Parameterised
    because the same shape recurs on every field this schema grows —
    `nhl_team` on TransactionRecord (2026-08-15) is the second.

    `scope` names a top-level list to restrict the strip to, and getting it
    right is what makes the fixture honest. `nhl_team` is old on Player and
    PlayerOnRoster and new only on TransactionRecord, so a whole-document strip
    simulates a file no build has ever written — and it fails at the PARSE
    (`_player_from_dict` reads its copy with a bare lookup), which is a
    different code path from the one under test and would have passed for the
    wrong reason had the assertion been weaker. Written by the app and edited afterwards
    rather than hand-authored, for the reason `_good_state_with_pick` gives: a
    fixture blob drifts from `to_json` and the test then covers a shape nothing
    writes. Both copies on disk get it, so the recovery ladder cannot quietly
    supply a newer file.
    """
    import main

    with TestClient(main.app) as c:
        c.post("/reset")
        built = build(c)

    def strip(obj):
        if isinstance(obj, dict):
            return {k: strip(v) for k, v in obj.items() if k != key}
        if isinstance(obj, list):
            return [strip(v) for v in obj]
        return obj

    current = state_dir / "auction_state.json"
    with open(current) as f:
        data = json.load(f)
    if scope:
        data[scope] = strip(data[scope])
    else:
        data = strip(data)
    # `AuctionState._snapshots` is a list of whole JSON DOCUMENTS, not of
    # dicts, so the walk above steps over each one as an opaque string
    # and left the field in the undo chain — which is how this helper first hung
    # pytest rather than failing: `assert "is_keeper" not in text` on 2MB sends
    # difflib quadratic. A file written by the old build has no `is_keeper`
    # anywhere, chain included.
    data["_snapshots"] = [
        json.dumps(_strip_snapshot(json.loads(s), strip, scope))
        for s in data.get("_snapshots", [])
    ]
    text = json.dumps(data[scope] if scope else data)
    leaked = text.count(f'"{key}"')
    assert leaked == 0, f"the strip missed {leaked} copies of the field"
    text = json.dumps(data)
    current.write_text(text)
    (state_dir / "auction_state.json.backup").write_text(text)
    return built


def _strip_snapshot(snapshot: dict, strip, scope: str | None) -> dict:
    """Apply the same strip to one snapshot document."""
    if not scope:
        return strip(snapshot)
    if scope in snapshot:
        snapshot[scope] = strip(snapshot[scope])
    return snapshot


def _send_down(client, team_code: str, player_name: str) -> None:
    """Bench a player then demote him, asserting each half landed."""
    for endpoint in ("/toggle-bench", "/move-to-minors"):
        r = client.post(
            endpoint, data={"team_code": team_code, "player_name": player_name})
        assert r.status_code == 200, r.text
        assert toast_of(r).get("type") != "error", f"{endpoint}: {toast_of(r)}"


class TestKeeperProvenanceSurvivesAnOldStateFile:
    """The minors are the one list whose provenance cannot be re-derived.

    `_team_from_dict` reads the two ACTIVE lists as authoritative — a player in
    `keeper_players` is a keeper by definition, whatever the file says. The
    minors hold demoted keepers and drafted players side by side, so a state
    written before `is_keeper` existed carries no way to tell them apart, and
    recalling a keeper out of it would file him under `acquired_players` and
    paint him green as somebody BOT had bought at auction.

    `_backfill_keeper_flags` re-reads that one bit from players.csv, which is
    the same pre-auction record `data_loader` derives keepers from. It is a
    fixup, so per the rule above it is never fatal — but it is the only thing
    standing between a legacy save and a wrong roster.
    """

    def test_the_flag_comes_back(self, state_dir):
        import main

        _legacy_state_without_keeper_flags(state_dir)

        with TestClient(main.app) as c:
            assert c.get("/").status_code == 200
            code, name = _a_pre_auction_minor()
            demoted = main.auction_state.teams[code].minor_players[0]
            assert demoted.name == name
            assert demoted.is_keeper, (
                f"{name} was on {code} before the auction, but loaded off a "
                f"legacy file as a player they bought"
            )

    def test_recalling_him_puts_him_back_with_the_keepers(self, state_dir):
        """The consequence, which is what the operator actually sees.

        Asserted through the endpoint rather than on the flag alone: the flag is
        only worth restoring because `recall_from_minors` routes on it.
        """
        import main

        _legacy_state_without_keeper_flags(state_dir)

        with TestClient(main.app) as c:
            assert c.get("/").status_code == 200
            code, name = _a_pre_auction_minor()
            r = c.post("/move-to-roster",
                       data={"team_code": code, "player_name": name})
            assert r.status_code == 200, r.text

            team = main.auction_state.teams[code]
            assert name in {p.name for p in team.keeper_players}, (
                f"{name} did not come back a keeper: {toast_of(r)}"
            )
            assert name not in {p.name for p in team.acquired_players}

    def test_a_player_drafted_into_the_minors_is_left_alone(self, state_dir):
        """The backfill must not simply flag everyone in the minors.

        A player bought at auction and sent down IS acquired, and green has to
        keep meaning that — a fixup that over-reaches kills the colour entirely
        instead of fixing it. His players.csv row says UFA/RFA, which is exactly
        how the two are told apart.
        """
        import main

        def build(c):
            name = _an_available_player()
            _draft(c, name, "BOT", 1.0)
            _send_down(c, "BOT", name)
            return name

        drafted = _legacy_state_without_keeper_flags(state_dir, build)

        with TestClient(main.app) as c:
            assert c.get("/").status_code == 200
            team = main.auction_state.teams["BOT"]
            player = next(p for p in team.minor_players if p.name == drafted)
            assert not player.is_keeper, (
                f"{drafted} was drafted this auction — the backfill claimed him "
                f"as a pre-auction keeper"
            )

            r = c.post("/move-to-roster",
                       data={"team_code": "BOT", "player_name": drafted})
            assert r.status_code == 200, r.text
            assert drafted in {p.name for p in team.acquired_players}, toast_of(r)

    def test_a_renamed_keeper_is_found_too(self, state_dir):
        """The lookup key is the name the STATE holds, not the CSV's.

        `_disambiguated_names` renames every member of a colliding group, so a
        keeper stored as `Jack Hughes (NJD)` is not in players.csv under that
        string at all. Matching on `row["PLAYER"]` finds nobody and leaves him
        mis-coloured with nothing on screen to say why — and the collisions are
        not hypothetical: the 2026-08-07 file has three, two of them keepers.
        """
        import data_loader
        import main

        def build(c):
            renamed = {n for names in data_loader.loaded_disambiguations.values()
                       for n in names}
            if not renamed:
                pytest.skip("players.csv has no duplicate names to disambiguate")
            for code, team in main.auction_state.teams.items():
                for p in team.keeper_players:
                    if p.name in renamed:
                        _send_down(c, code, p.name)
                        return code, p.name
            pytest.skip("no renamed player is a keeper in this players.csv")

        code, name = _legacy_state_without_keeper_flags(state_dir, build)

        with TestClient(main.app) as c:
            assert c.get("/").status_code == 200
            demoted = next(p for p in main.auction_state.teams[code].minor_players
                           if p.name == name)
            assert demoted.is_keeper, (
                f"{name} kept his provenance only if the backfill matched on "
                f"the disambiguated name"
            )


class TestTheLogsNhlClubSurvivesAnOldStateFile:
    """`TransactionRecord.nhl_team` landed 2026-08-15; older files lack it.

    Two halves, and both are needed. `_transaction_from_dict`'s `.get` keeps the
    file PARSING — without it `_load_saved_state` renames a good draft
    `.corrupt`, which `tests/test_state.py` pins. This class covers the other
    half: parsing to a blank club would leave every pre-upgrade pick with no
    badge in the Auction tab for the rest of the draft, so
    `_backfill_nhl_teams` re-reads it from the same players.csv it already uses
    for rostered players.
    """

    def _draft_one(self, client):
        """A real pick through /assign, returning (name, expected club)."""
        import main

        ranked = sorted(
            main.auction_state.available_players.values(),
            key=lambda p: -p.projected_points,
        )
        p = next(p for p in ranked if p.nhl_team)
        r = client.post("/assign", data={
            "player": p.name, "team": main.MY_TEAM, "salary": "2.0"})
        assert toast_of(r).get("type") != "error", toast_of(r)
        return p.name, p.nhl_team

    def test_the_club_comes_back(self, state_dir):
        import main

        drafted = _legacy_state_without_keeper_flags(
            state_dir, build=self._draft_one, key="nhl_team",
            scope="transaction_log")
        name, club = drafted

        with TestClient(main.app) as c:
            assert c.get("/").status_code == 200
            record = next(
                t for t in main.auction_state.transaction_log if t.player_name == name)
            assert record.nhl_team == club, (
                f"{name}'s club loaded as {record.nhl_team!r} from a legacy "
                f"file; expected {club!r}"
            )

    def test_the_badge_is_back_on_screen(self, state_dir):
        """The consequence, asserted where the operator would see it.

        The field alone is not the deliverable — `_log_nhl_logo.html` skips a
        blank one silently, so a backfill that ran but wrote the wrong thing
        looks identical to no badge at all.

        Scoped to the panel with `section_of`, and that is not a formality: the
        drafted player is on BOT's roster, whose table renders the same
        `/nhl_logos/<club>.svg`, so a whole-page assertion passes with the
        backfill deleted. It did, when this test was first written.
        """
        import main

        drafted = _legacy_state_without_keeper_flags(
            state_dir, build=self._draft_one, key="nhl_team",
            scope="transaction_log")
        _name, club = drafted

        with TestClient(main.app) as c:
            panel = section_of(c.get("/").text, "logs-panel")
        assert f'src="/nhl_logos/{club}.svg"' in panel

"""No test may name a player from `players.csv`.

CLAUDE.md's rule — *never hard-code a player name in a test* — existed for a
year with nothing behind it, and drifted the whole time. Measured 2026-08-19
before this file was written: **61 literal pool names across 7 test files**,
where `BACKLOG.md` recorded "four literal names … ~39 times". A rule nobody can
count is a rule nobody can keep.

**Why a stale name is worse than a broken one.** `data/players.csv` is replaced
before every draft, and `/assign` answers **200 with a toast** when it rejects.
So a test that posts a name the new CSV does not carry passes — against a pick
that never happened — and the damage surfaces somewhere else entirely. The
2026-08-07 refresh drill saw one missing name arrive as `assert 24 == 25` three
tests downstream, naming neither the player nor the reason. That is also why
`BACKLOG.md`'s deferral ("do it at the next refresh, when the failures are in
front of you") was backwards: the failure mode is a silent pass, so at refresh
time the failures are NOT in front of you.

The fix at every call site is to derive the name by the ROLE it plays —
`helpers.pool_top`, `a_roster_player`, `a_buyout_candidate` — and to draft
through `helpers.assign`, which fails AT the pick.

**Full names only, deliberately.** This checks exact equality against whole pool
names, so it has no false positives: those 2000-odd strings collide with no
English word. Also matching capitalised *tokens* would catch a bare-surname
assertion (`assert "Panarin" in message`, which existed until 2026-08-19 and
which this file cannot see), but it needs a **data-dependent** allowlist —
`"Charlie"` in `test_state.py`'s Alice/Bob/Charlie unit test collides with five
real players today — and such an allowlist rots in the noisy direction: the next
CSV could fail a synthetic-name unit test for no reason, at exactly the moment
you want the suite quiet. So the surname class stays a gap, and the way to close
it at a call site is to assert the *derived* name rather than a fragment of it.

**What this can and cannot prove.** It proves no test names a player the current
pool contains. It does not prove the suite would survive an arbitrary CSV — a
pool with no goalies, or with twenty players, breaks things no naming discipline
can fix. `TestDataFingerprint` and the refresh drill in CLAUDE.md cover that.
"""

import ast
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent

# Files allowed to name players, and why. An entry here is a claim that the
# names are the SUBJECT of the test rather than a convenient body for it.
NAMES_ARE_THE_POINT = {
    "test_player_identity.py": (
        "every name in it goes into a CSV the test writes itself in tmp_path — "
        "it reuses real ones (Matt Murray, Elias Pettersson) because those are "
        "the historical collision cases the disambiguation exists for, so the "
        "live pool is not what it reads"
    ),
}


def _pool_names() -> set[str]:
    """Every player the loaded state knows about, keepers included.

    Read off `auction_state` rather than parsed out of the CSV, so the loader's
    duplicate renames (`Matt Murray (DAL)`) count too — a test could hard-code
    one of those, and it is the raw file that would not contain it.
    """
    import main

    names = set(main.auction_state.available_players)
    for team in main.auction_state.teams.values():
        names.update(p.name for p in team.all_players)
    return names


def _string_constants(path: Path) -> list[tuple[int, str]]:
    return [
        (node.lineno, node.value.strip())
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


@pytest.mark.parametrize(
    "path", sorted(TESTS.glob("test_*.py")), ids=lambda p: p.name
)
def test_no_test_file_names_a_player(path, client):
    """`client` for the fresh state the pool is read from, not for requests."""
    if path.name in NAMES_ARE_THE_POINT:
        pytest.skip(f"allowlisted: {NAMES_ARE_THE_POINT[path.name]}")

    pool = _pool_names()
    offenders = [
        f"{path.name}:{line} names {value!r}"
        for line, value in _string_constants(path)
        if value in pool
    ]
    assert not offenders, (
        "a test hard-codes a player from players.csv:\n  "
        + "\n  ".join(offenders)
        + "\n\nplayers.csv is replaced before every draft and /assign answers 200 "
        "WITH A TOAST when it rejects, so the day this name is gone the test "
        "passes against a pick that never happened. Derive the name by the role "
        "it plays — helpers.pool_top / a_roster_player / a_buyout_candidate — and "
        "draft through helpers.assign, which fails at the pick. If the name IS "
        "the subject of the test, add the file to NAMES_ARE_THE_POINT with the "
        "reason."
    )


def test_the_allowlist_still_describes_real_files():
    """An entry for a file that no longer exists is a rule with a hole in it.

    The allowlist is the only way to opt out, so a stale entry is worth failing
    over: a renamed file would silently keep its exemption under the old name
    while the new one goes unchecked, and nothing else here would notice.
    """
    missing = [name for name in NAMES_ARE_THE_POINT if not (TESTS / name).exists()]
    assert not missing, (
        f"NAMES_ARE_THE_POINT exempts {missing}, which do not exist — delete the "
        f"entry, or fix the filename if the file was renamed"
    )

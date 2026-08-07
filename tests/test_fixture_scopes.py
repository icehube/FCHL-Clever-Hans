"""A module-scoped `client` fixture must be a decision, not an accident.

`conftest.py` provides a function-scoped `client` that resets the auction
before every test. A file can shadow it with a module-scoped one, which makes
`POST /reset` run ONCE for the whole file — and then any test that mutates
global state without putting it back changes what every later test in that
file sees.

Three files want exactly that: their tests are a sequence played forward, not
independent checks. Everyone else wants isolation.

**Which direction needs a guard.** Converting a sequential file by mistake
fails LOUDLY — `test_01` depends on `test_00`, so the suite goes red on the
next run. The silent direction is the other one: a NEW file declaring a
module-scoped client for tests that are not a sequence, re-introducing the
coupling with nothing to notice it. That coupling has twice let a test keep
passing against a real defect (see `conftest.client`), both caught by mutation
testing rather than by the suite. This is that failure mode with a check
behind it.

**What this can and cannot prove.** It reads the source for a fixture
declaration; it does not run anything, and it cannot tell whether a file's
tests are *genuinely* a sequence — only whether someone recorded a claim that
they are. Naming the file here is the claim. It also only looks for a fixture
named `client`; a differently-named module-scoped fixture holding a TestClient
would sail past.
"""

import ast
import re
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent

# Files whose tests are a deliberate sequence, and why. Adding an entry is a
# claim that resetting between these tests would make them meaningless — not
# that isolation happens to be inconvenient.
SEQUENTIAL_BY_DESIGN = {
    "test_auction_draft.py": "numbered tests play one draft forward",
    "test_dry_run.py": "one continuous 40-pick auction; the flow is the test",
    "test_trade_buyout_undo.py":
        "numbered trade/buyout/undo sequence; its independent classes already "
        "shadow with their own function-scoped fixture",
}


def _client_fixture_scope(path: Path) -> str | None:
    """Scope of a `client` fixture declared in `path`, or None if it declares none.

    Returns "function" for a bare `@pytest.fixture`, since that is what pytest
    defaults to — the thing being checked is the effective scope, not whether
    the argument was written out.
    """
    tree = ast.parse(path.read_text())
    scopes = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "client":
            continue
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if not (isinstance(target, ast.Attribute) and target.attr == "fixture"):
                continue
            scope = "function"
            if isinstance(dec, ast.Call):
                for kw in dec.keywords:
                    if kw.arg == "scope":
                        scope = kw.value.value
            scopes.append(scope)
    # A file with two `client` fixtures is a class-level shadow, which is fine
    # and deliberate; the module-level one is what this cares about, and it is
    # the widest scope declared.
    if not scopes:
        return None
    return "module" if "module" in scopes else scopes[0]


def _test_files() -> list[Path]:
    return sorted(p for p in TESTS.glob("test_*.py"))


def test_the_scan_finds_fixtures_at_all():
    """The guard below is vacuous if nothing parses.

    A rename of the fixture, or a move to a different helper, would empty
    every set here and turn the assertions green — the failure mode of any
    test that derives its own subject.
    """
    scoped = {p.name: _client_fixture_scope(p) for p in _test_files()}
    declared = {n: s for n, s in scoped.items() if s}
    assert len(declared) >= 6, f"only found {len(declared)} client fixtures: {declared}"
    assert scoped.get("test_dry_run.py") == "module", (
        "test_dry_run.py is the canonical module-scoped file; if the scan says "
        "otherwise the scan is broken, not the file"
    )
    assert scoped.get("test_endpoints.py") is None, (
        "test_endpoints.py should inherit conftest's client, not declare one"
    )


def test_every_module_scoped_client_is_declared_deliberate():
    offenders = sorted(
        p.name for p in _test_files()
        if _client_fixture_scope(p) == "module"
        and p.name not in SEQUENTIAL_BY_DESIGN
    )
    assert not offenders, (
        f"module-scoped `client` in {offenders}: POST /reset would run once for "
        f"the whole file, making test order load-bearing. Either drop the "
        f"fixture and inherit conftest's function-scoped one, or add the file "
        f"to SEQUENTIAL_BY_DESIGN with the reason its tests are a sequence."
    )


def test_the_allow_list_has_no_stale_entries():
    """A file that stops being sequential must leave the list.

    An exemption nobody removes reads as a decision that was made, and the
    next person adding a test file copies it.
    """
    missing = sorted(n for n in SEQUENTIAL_BY_DESIGN if not (TESTS / n).exists())
    assert not missing, f"SEQUENTIAL_BY_DESIGN names files that do not exist: {missing}"
    not_module = sorted(
        n for n in SEQUENTIAL_BY_DESIGN
        if _client_fixture_scope(TESTS / n) != "module"
    )
    assert not not_module, (
        f"these are exempted but no longer declare a module-scoped client; "
        f"drop them from the list: {not_module}"
    )


@pytest.mark.parametrize("name", sorted(SEQUENTIAL_BY_DESIGN))
def test_a_sequential_file_says_so_in_the_fixture(name):
    """The reason has to be readable where the fixture is, not only here.

    Someone deleting a module-scoped fixture is looking at that file, not at
    this list — so the docstring is what actually stops them.
    """
    source = (TESTS / name).read_text()
    body = re.search(r'def client\(\):\n(\s+""".*?""")', source, re.S)
    assert body, f"{name}'s client fixture has no docstring"
    assert "ON PURPOSE" in body.group(1), (
        f"{name}'s client fixture does not say the module scope is deliberate"
    )


class TestTheFixtureActuallyIsolates:
    """Source scanning proves a declaration; this proves the behaviour.

    An ordered pair: the first mutates the auction, the second asserts it is
    gone. Under a module-scoped `client` the second one FAILS.

    That makes this the only check in the change that can demonstrate it did
    anything. Reverting `conftest.client` to module scope fails nothing else,
    because no test in the suite currently DEPENDS on leaked state — every one
    was verified to pass in isolation before the conversion. The value of the
    conversion is preventive, so its regression test has to be written rather
    than discovered.
    """

    def test_1_mutates_the_auction(self, client):
        import main

        r = client.post("/team-done", data={"team_code": "SRL"})
        assert r.status_code == 200
        assert main.auction_state.teams["SRL"].is_done, (
            "precondition: the mutation has to land, or the next test proves nothing"
        )

    def test_2_does_not_see_the_mutation(self, client):
        import main

        assert not main.auction_state.teams["SRL"].is_done, (
            "state leaked from the previous test — `client` is not resetting "
            "per test, so order is load-bearing again"
        )

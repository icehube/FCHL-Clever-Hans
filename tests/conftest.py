"""Shared test configuration.

Redirects the app's state directory to a per-session temp dir so that:
- pytest never clobbers a real draft state in data/state/ (it used to
  write through on every mutating endpoint), and
- tests are hermetic: no test run inherits whatever mid-draft state
  happens to be on disk.
"""

import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient

from config import MY_TEAM


@pytest.fixture(scope="session", autouse=True)
def isolated_state_dir(tmp_path_factory):
    import main

    main.STATE_DIR = str(tmp_path_factory.mktemp("state"))
    yield


@pytest.fixture(scope="session")
def _app_client():
    """The HTTP transport, opened once for the run.

    Session-scoped because it holds no auction state of its own — entering the
    context manager runs the app's lifespan, and that is the expensive part
    (221ms with a fresh client per test against 107ms for a reset alone). The
    per-test isolation lives in `client` below.

    `isolated_state_dir` is session-scoped AND autouse, so pytest orders it
    ahead of this. That ordering is load-bearing: the lifespan writes state,
    and without it the first thing this fixture does is write into the real
    `data/state/`.
    """
    from main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def client(_app_client):
    """A fresh auction per test.

    This used to be `scope="module"` in each file that needed it, so `/reset`
    ran ONCE for 98 tests and any test that mutated global state without
    putting it back changed what every later test in the file saw. Nothing
    depended on that when it was fixed — every test passed in isolation — but
    the coupling is invisible at the call site and it hides failures in the
    tests whose job is to catch failures. It did, twice:

    - `TestPanelContextIsolation` passed against a reproduction of the
      2026-08-05 trade-panel leak, because the view had moved server-side and
      its cases no longer set up the state they assumed.
    - `test_undo_reverts_move_to_minors` passed against a `/move-to-minors`
      that had stopped snapshotting, because a shared SNAPSHOT CHAIN let
      `/undo` pop a snapshot taken by the test's own setup.

    Both were caught by mutation testing rather than by the suite.

    Files whose tests form a deliberate sequence — `test_dry_run.py`'s 40-pick
    auction and the numbered flows in `test_auction_draft.py` and
    `test_trade_buyout_undo.py` — shadow this with their own module-scoped
    fixture. `tests/test_fixture_scopes.py` holds the list of who may.
    """
    _app_client.post("/reset")
    return _app_client


@pytest.fixture(autouse=True)
def default_viewed_team():
    """Every test starts looking at BOT.

    `main._viewed_team` is a module global, so a test that opens an opponent's
    roster and does not put it back would silently change what every later test
    in the same file renders — the same order-dependence already open against
    `test_endpoints.py`'s module-scoped client. One assignment is cheaper than
    remembering, and it cannot mask a real leak: it runs between tests, never
    between requests inside one.
    """
    import main

    main._viewed_team = MY_TEAM
    yield


@pytest.fixture(scope="session")
def live_server():
    """The real app on a real port, in this process.

    `TestClient` answers "what did the endpoint return"; this exists for the
    two questions it cannot reach — what a browser does with the response, and
    how the server behaves under genuinely concurrent requests.

    In-process on purpose: `isolated_state_dir` above already redirects
    `main.STATE_DIR`, and tests can reach into `main.auction_state` to set up
    state exactly as the endpoint tests do. A subprocess would need a whole new
    setup vocabulary to say the same things.

    The bound socket is handed to uvicorn rather than a port number, so there
    is no close-then-rebind window for another process to take it.
    """
    import uvicorn

    import main

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    server = uvicorn.Server(uvicorn.Config(main.app, log_level="warning"))
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, daemon=True
    )
    thread.start()

    deadline = time.monotonic() + 20
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        assert thread.is_alive(), "uvicorn thread died during startup"
        time.sleep(0.05)

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=10)

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


@pytest.fixture(scope="session", autouse=True)
def isolated_state_dir(tmp_path_factory):
    import main

    main.STATE_DIR = str(tmp_path_factory.mktemp("state"))
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

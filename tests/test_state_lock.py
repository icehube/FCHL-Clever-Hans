"""The saved-draft folder belongs to one process at a time.

The disaster: the operator's server is hours into a live auction, and a second
process pointed at the same `data/state/` — a diagnostic `with
TestClient(main.app)` script, a second `uvicorn` started by mistake — saves over
it. `_save_state` rotates the previous file into `.backup`, so two saves from the
wrong process destroy both copies. On 2026-09-15 a script did exactly that with
nine picks. `main._hold_state_lock` makes the second process refuse to start.

The holders here are CHILD processes calling raw `flock`, not a second lifespan
in this process: Linux `flock` belongs to the open file, so an in-process holder
would be the registry's re-entrant path, which is the opposite of the case under
test. The children mimic the server by writing their pid into the lock file,
which is what the refusal message reads.
"""

import errno
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_crash_recovery import state_dir  # noqa: F401  (fixture)

fcntl = pytest.importorskip("fcntl")

REPO = Path(__file__).resolve().parent.parent

# Takes the lock, records its pid the way the server does, and holds it until
# its stdin closes.
_HOLDER = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
os.ftruncate(fd, 0)
os.write(fd, f"{os.getpid()}\\n".encode())
print("locked", flush=True)
sys.stdin.read()
"""

# Reports whether somebody else holds the lock, without waiting for it.
_PROBE = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("held")
else:
    print("free")
"""


def _lock_path(folder) -> str:
    import main
    return os.path.join(str(folder), main.STATE_LOCK_NAME)


@pytest.fixture
def holder(state_dir):  # noqa: F811
    """A separate process holding `state_dir`'s lock for the whole test."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, _lock_path(state_dir)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    assert proc.stdout.readline().strip() == "locked", "the holder never locked"
    yield proc
    proc.stdin.close()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _probe(folder) -> str:
    out = subprocess.run(
        [sys.executable, "-c", _PROBE, _lock_path(folder)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return out.stdout.strip()


class TestASecondProcessIsRefused:
    def test_a_second_process_cannot_boot_on_a_held_folder(self, state_dir, holder):  # noqa: F811
        import main

        with pytest.raises(RuntimeError) as refused:
            with TestClient(main.app):
                pass
        message = str(refused.value)
        assert str(state_dir) in message
        # The HOLDER's pid, not ours: a refusal that truncated the file before
        # it had the lock would print its own pid, or "unknown".
        assert f"pid {holder.pid}" in message, message

    def test_the_refusal_touches_nothing(self, client, state_dir, holder):  # noqa: F811
        """Nothing in the folder moves — the recovery ladder included.

        Garbage in the current file is the sharpest probe: an unlocked boot does
        not merely read it, it RENAMES it to `.corrupt`, which is a write into
        the other process's draft before a single request has arrived. So this
        is what fails if the lock is taken after `_load_saved_state`.
        """
        import main

        current = state_dir / "auction_state.json"
        backup = state_dir / "auction_state.json.backup"
        current.write_text("{ this is not json")
        backup.write_text(main.auction_state.to_json())
        before = {p.name: p.read_bytes() for p in (current, backup)}

        with pytest.raises(RuntimeError):
            with TestClient(main.app):
                pass

        assert {p.name: p.read_bytes() for p in (current, backup)} == before
        leftovers = {p.name for p in state_dir.iterdir()} - {
            current.name, backup.name, main.STATE_LOCK_NAME}
        assert not leftovers, f"the refused boot wrote {sorted(leftovers)}"

    def test_save_state_refuses_without_lifespan(self, client, state_dir, holder):  # noqa: F811
        """A script that assigns the state and saves never runs lifespan.

        `client` is here only so `main.auction_state` is a real state: with it
        None, a missing check would fail on `.to_json()` and read as a pass.
        """
        import main

        assert os.path.realpath(state_dir) not in main._state_locks
        with pytest.raises(RuntimeError, match=f"pid {holder.pid}"):
            main._save_state()
        written = {p.name for p in state_dir.iterdir()} - {main.STATE_LOCK_NAME}
        assert not written, f"_save_state wrote {sorted(written)} anyway"


class TestTheServerHoldsIt:
    def test_the_server_keeps_holding_it(self, state_dir):  # noqa: F811
        """Held for the life of the process, not for the length of a call.

        The likeliest real bug is an fd that goes out of scope — a `with open()`
        around the `flock`, or a lock taken and never stored — which releases
        the lock the moment `_hold_state_lock` returns and passes every test
        that only asks whether the boot succeeded.
        """
        import main

        with TestClient(main.app):
            assert _probe(state_dir) == "held"

    def test_the_same_process_may_boot_twice(self, state_dir):  # noqa: F811
        """Re-entrant, because pytest boots lifespan repeatedly on one folder.

        Without the registry the SECOND `os.open` + `flock` in this process
        conflicts with the first — `flock` belongs to the open file, not the
        process — and the session's `_app_client` plus `live_server` would
        refuse each other.
        """
        import main

        with TestClient(main.app) as outer:
            with TestClient(main.app) as inner:
                assert inner.get("/state").status_code == 200
            assert outer.get("/state").status_code == 200

    def test_a_dead_holder_leaves_no_lock(self, state_dir):  # noqa: F811
        """What separates this from a pidfile: a crash cannot strand the draft.

        The dead holder's pid is still in the file — nothing cleans it up — and
        the boot succeeds anyway, because the kernel dropped the lock with the
        process.
        """
        import main

        proc = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, _lock_path(state_dir)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        assert proc.stdout.readline().strip() == "locked"
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
        assert Path(_lock_path(state_dir)).read_text().strip() == str(proc.pid)

        with TestClient(main.app) as c:
            assert c.get("/state").status_code == 200
        assert Path(_lock_path(state_dir)).read_text().strip() == str(os.getpid())

    def test_a_filesystem_that_cannot_lock_still_boots(
            self, state_dir, monkeypatch, caplog):  # noqa: F811
        """Only `BlockingIOError` means "somebody else has it".

        Any other failure — ENOLCK on a network mount, a folder that cannot
        hold the lock file — means the guard is absent, not tripped, and
        startup degrades rather than refusing, like the rest of `lifespan`.
        Warned once, not on every save.
        """
        import main

        def no_locks(fd, op):
            raise OSError(errno.ENOLCK, "No locks available")

        monkeypatch.setattr(main.fcntl, "flock", no_locks)
        with caplog.at_level(logging.WARNING):
            with TestClient(main.app) as c:
                assert c.get("/state").status_code == 200
                main._save_state()
        warned = [r for r in caplog.records if "Could not lock" in r.getMessage()]
        assert len(warned) == 1, [r.getMessage() for r in warned]


def _collect(stream, lines: queue.Queue) -> None:
    for line in stream:
        lines.put(line)
    lines.put(None)


def test_a_second_uvicorn_exits(tmp_path):
    """What the operator actually sees: the second server exits, and says why.

    The only test here that runs `uvicorn` itself, because the in-process tests
    prove `lifespan` raises and nothing more. That the server then EXITS rather
    than serving with no lifespan is uvicorn's behaviour (starlette sends
    `lifespan.startup.failed`, uvicorn logs "Application startup failed.
    Exiting."), and this checks it rather than trusting it. About five seconds,
    most of it server A's boot.
    """
    env = {**os.environ, "FCHL_STATE_DIR": str(tmp_path)}
    env.pop("FCHL_PLAYERS_CSV", None)  # the default pool, whatever the shell has
    cmd = [sys.executable, "-m", "uvicorn", "main:app", "--port", "0"]

    first = subprocess.Popen(cmd, cwd=REPO, env=env, text=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    lines: queue.Queue = queue.Queue()
    threading.Thread(target=_collect, args=(first.stderr, lines), daemon=True).start()
    try:
        seen = []
        while True:
            line = lines.get(timeout=120)
            assert line is not None, f"server A exited during boot:\n{''.join(seen)}"
            seen.append(line)
            if "Application startup complete" in line:
                break

        # Planted AFTER A booted, so A never read it: an unlocked B would
        # rename it to `.corrupt`, which is the write this guard exists to stop.
        current = tmp_path / "auction_state.json"
        current.write_text("{ this is not json")

        second = subprocess.run(cmd, cwd=REPO, env=env, capture_output=True,
                                text=True, timeout=120)
        assert second.returncode != 0, second.stderr
        assert f"pid {first.pid}" in second.stderr, second.stderr
        assert current.read_text() == "{ this is not json"
        assert not (tmp_path / "auction_state.json.corrupt").exists()
        assert first.poll() is None, "server A died when B was refused"
    finally:
        first.terminate()
        try:
            first.wait(timeout=20)
        except subprocess.TimeoutExpired:
            first.kill()
            first.wait()

"""Shared test configuration.

Redirects the app's state directory to a per-session temp dir so that:
- pytest never clobbers a real draft state in data/state/ (it used to
  write through on every mutating endpoint), and
- tests are hermetic: no test run inherits whatever mid-draft state
  happens to be on disk.
"""

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolated_state_dir(tmp_path_factory):
    import main

    main.STATE_DIR = str(tmp_path_factory.mktemp("state"))
    yield

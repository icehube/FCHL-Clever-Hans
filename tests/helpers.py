"""Helpers shared across test modules.

Both of these were copy-pasted rather than imported — `squeeze` in three files
and `toast_of` in two — which is how a fix lands in one copy and silently misses
the others. `squeeze` in particular encodes a non-obvious sequence (zero the
penalties, invalidate, recompute against the *clean* total, invalidate again);
getting that wrong in one copy would produce a team that is near the cap by a
different amount than the test says, and the assertion would still pass.

Kept out of `conftest.py` on purpose: these are plain functions, not fixtures,
and the browser tests need `squeeze` from a module scope where no fixture is in
play.
"""

import json
from typing import Any

from config import SALARY_CAP


def toast_of(response: Any) -> dict:
    """The showToast payload an endpoint attached, or {} if it attached none."""
    header = response.headers.get("HX-Trigger")
    return json.loads(header)["showToast"] if header else {}


def squeeze(code: str, headroom: float) -> None:
    """Set `code`'s penalties so exactly `headroom` of cap space remains.

    Negative headroom puts the team that far OVER the cap, which is legal state
    the league permits (owner decision 2026-08-06) and several tests depend on.

    Imports `main` at call time, not module import time: `tests/conftest.py`
    redirects `main.STATE_DIR` and the browser harness runs the app in-process,
    so the module object must be resolved live rather than captured early.
    """
    import main

    team = main.auction_state.teams[code]
    # Zero first so `total_salary` reads the roster alone — reusing a total that
    # still carries the previous penalties would compound them on a second call,
    # and several tests squeeze two teams in a row.
    team.penalties = 0.0
    team._invalidate_cache()
    team.penalties = round(SALARY_CAP - team.total_salary - headroom, 1)
    team._invalidate_cache()

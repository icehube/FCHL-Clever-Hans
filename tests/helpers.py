"""Helpers shared across test modules.

`squeeze` and `toast_of` were copy-pasted rather than imported — three files and
two — which is how a fix lands in one copy and silently misses the others.
`squeeze` in particular encodes a non-obvious sequence (zero the penalties,
invalidate, recompute against the *clean* total, invalidate again); getting that
wrong in one copy would produce a team that is near the cap by a different
amount than the test says, and the assertion would still pass.

Kept out of `conftest.py` on purpose: these are plain functions, not fixtures,
and the browser tests need `squeeze` from a module scope where no fixture is in
play.
"""

import json
import re
from typing import Any

from config import SALARY_CAP


def section_of(html: str, element_id: str) -> str:
    """The `<section id="...">…</section>` block with that id.

    Mutation endpoints return the whole page, and most panels list every team —
    League State has all eleven codes in a table, the Trade panel has ten in a
    dropdown. So `"SRL" in response.text` is true no matter which team the team
    panel is showing, and an assertion written that way passes on the bug it is
    meant to catch. Slice the panel out first.

    Deliberately naive: no nested `<section>` exists inside a panel today, and a
    real parser would be a dependency the offline requirement.txt cannot carry.
    Raises rather than returning "" so a renamed id fails loudly.
    """
    m = re.search(rf'<section[^>]*\bid="{re.escape(element_id)}"', html)
    if m is None:
        raise AssertionError(f'no <section id="{element_id}"> in the response')
    end = html.index("</section>", m.start())
    return html[m.start():end]


def toast_of(response: Any) -> dict:
    """The showToast payload an endpoint attached, or {} if it attached none."""
    header = response.headers.get("HX-Trigger")
    return json.loads(header)["showToast"] if header else {}


def assign(client: Any, player: str, team: str, salary: float) -> Any:
    """POST /assign and fail HERE if the pick did not land.

    `/assign` answers 200 with a toast when it REJECTS — unknown player,
    unknown team — so `assert r.status_code == 200` passes on a pick that never
    happened. The 2026-08-07 refresh drill found this the expensive way: one
    hard-coded name left the pool and the suite reported `assert 24 == 25` in a
    later test, three picks downstream of the rejection, naming neither the
    player nor the reason.

    Checks the transaction log rather than the toast TYPE, because the
    unknown-player toast is "warning" and so is a perfectly successful assign
    that puts a team over the cap.
    """
    import main

    before = len(main.auction_state.transaction_log)
    response = client.post(
        "/assign", data={"player": player, "team": team, "salary": str(salary)}
    )
    assert response.status_code == 200
    assert len(main.auction_state.transaction_log) == before + 1, (
        f"/assign did not draft {player} to {team} at ${salary}M: "
        f"{toast_of(response).get('message', '(no toast)')}"
    )
    return response


def a_buyout_candidate(state=None):
    """BOT's worst money-per-point player that may legally be bought out.

    Tests used to name one ("Dougie Hamilton", $4.2M / 16pts) and assert against
    a hard-coded salary. `players.csv` is replaced before every draft, so a
    literal silently stops matching — and `/buyout-check/<gone>` still answers
    200, which is how the 2026-08-07 drill produced "Should recommend KEEP for
    top player" about a player who was not on the roster.

    Only groups 2 and 3 are eligible (`can_be_bought_out`), so filter first:
    picking the worst value overall lands on an A-E prospect the engine will
    correctly refuse.

    Defaults to the live `main.auction_state`; pass a state to work on a clone.
    """
    import main

    if state is None:
        state = main.auction_state
    team = state.teams[main.MY_TEAM]
    eligible = [
        p for p in team.keeper_players + team.acquired_players
        if p.can_be_bought_out
    ]
    assert eligible, "BOT has no buyout-eligible player — the fixture is wrong"
    return max(eligible, key=lambda p: p.salary / max(p.projected_points, 1))


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

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
from html import unescape
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


def pool_top(
    n: int = 1,
    position: str | None = None,
    group: str | None = None,
    skip: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    """The n highest-scoring available players, by name.

    Derived rather than named, per the CLAUDE.md rule: `players.csv` is replaced
    before every draft. The chart tests are the reason this matters more than
    usual — `/player-chart/<gone>` answers **200** with a ~250-byte empty state,
    so a stale literal does not fail, it silently turns
    `assert 'id="player-chart-container"' not in r.text` into an assertion that
    cannot fail, on the test guarding the two-mount invariant.

    Top by points on purpose: a floor-priced player is the degenerate end of the
    distribution, and a chart test wants a curve to look at.

    Lives here because `test_browser_ui.py` had grown its own `_pool_top` — the
    path `squeeze` took to three copies before it was folded in.

    `position` and `group` narrow the pool for a test whose assertion needs a
    ROLE rather than a body: the scripted auction in `test_auction_draft.py` has
    a pick asserting that a sale converts RFA2 to group 3, one described as the
    top D-man and one as a goalie, and those three are the only reason these
    filters exist. Both are exact matches on `Player.position` / `Player.group`
    — `"F"`/`"D"`/`"G"` and `"3"`/`"RFA1"`/`"RFA2"` are the whole vocabulary
    (measured 2026-08-19 on the live pool: 705 available, 234 D, 64 G, 9 RFA2).
    The assert below is what turns a filter that matches nothing into a failure
    naming the filter, rather than an IndexError at the call site.

    `skip` is for a caller assembling SEVERAL roles that have to be distinct
    people. The roles overlap by nature — 9 RFA2s in the pool today and they
    include a D and a G — so "top D" and "top RFA2" can be one player on a
    different CSV, and a caller that only checks afterwards can do nothing but
    fail. Excluding what is already claimed keeps `n=1`, which matters: asking
    for a couple of spares instead would break on a pool thin in that role.
    """
    import main

    ranked = sorted(
        (
            p for p in main.auction_state.available_players.values()
            if p.name not in skip
            and (position is None or p.position == position)
            and (group is None or p.group == group)
        ),
        key=lambda p: -p.projected_points,
    )
    assert len(ranked) >= n, (
        f"only {len(ranked)} players in the pool match position={position!r} "
        f"group={group!r} (skipping {len(skip)}), wanted {n}"
    )
    return [p.name for p in ranked[:n]]


def a_roster_player(code: str):
    """The first player on `code`'s ACTIVE roster — a target for a roster edit.

    Derived rather than named, per the CLAUDE.md rule: `players.csv` is replaced
    before every draft, so a literal silently stops matching. Lives here because
    two classes in `test_endpoints.py` had grown identical private copies of it
    (`_victim`) by 2026-08-11, and a second copy is how a fix lands in one and
    misses the other — the same path `squeeze` took to three copies.

    Active roster on purpose: benched and minor-league players are reachable
    through `all_players`, but the Bench / Adjust / ↓ Minors controls a roster
    edit test is aiming at render against this list.
    """
    import main

    players = main.auction_state.teams[code].roster_players
    assert players, f"{code} has an empty active roster — the fixture is wrong"
    return players[0]


def a_buyout_candidate(state=None):
    """BOT's worst money-per-point player that may legally be bought out.

    Tests used to name one ("Dougie Hamilton", $4.2M / 16pts) and assert against
    a hard-coded salary. `players.csv` is replaced before every draft, so a
    literal silently stops matching — and `/buyout-check?player_name=<gone>` answers
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


def buyout_options(html: str) -> list[str]:
    """The names the Buyout Analyzer offers, read off its picker.

    Three tests used to learn the offered set by string-matching each button's
    URL (`f"/buyout-check/{p.name}" in html`), which coupled them to the markup
    AND to the route shape — both changed on 2026-08-15 when the row of buttons
    became a `<select>` and the name moved to a query parameter. One reader, so
    the next change to either is a one-line edit rather than a three-file hunt.

    Scoped to `#buyout-panel`: the same names appear in the team panel's roster
    tables, so a whole-page match would report players the Analyzer never
    offered. The empty placeholder option is dropped — it is a prompt, not a
    candidate.

    **Unescaped, and that is not tidiness.** Jinja autoescapes the attribute, so
    `Ryan O'Reilly` comes back as `Ryan O&#39;Reilly` and compares unequal to the
    `Player.name` every caller checks it against. Two such names are in the pool
    today and one `/assign` makes one BOT's (drafted group 3, eligible), at which
    point `test_ineligible_players_are_not_offered` stops being able to fail: an
    illegally-offered apostrophe player is simply not found in the list, and the
    test reports clean. The app was never affected — the browser unescapes before
    htmx reads `select.value` — so this could only ever have shown up as a guard
    quietly going hollow.
    """
    panel = section_of(html, "buyout-panel")
    return [
        unescape(n) for n in re.findall(r'<option value="([^"]*)"', panel) if n
    ]


def trade_choices(html: str, aria_label: str) -> dict[str, str]:
    """One `.choice-list`'s offer, as {checkbox value: visible label}.

    Addressed by the group's `aria-label` rather than by an id or a position,
    because two of the four lists are rendered per-viewed-team and the other two
    are filled by JS — the name of the group is the only stable handle, and
    naming them is a requirement anyway (four of them had no accessible name at
    all until 2026-08-15).

    Unescaped, for the reason `buyout_options` documents: Jinja escapes the
    attribute, so `Ryan O'Reilly` would compare unequal to `Player.name` and a
    membership check against it silently stops being able to fail.

    Naive slicing to the next `</div>`, like `section_of` — a `.choice-list`
    holds only `<label>` rows, and a real parser is a dependency the offline
    requirements.txt cannot carry.
    """
    start = html.find(f'aria-label="{aria_label}"')
    assert start != -1, f'no group labelled "{aria_label}" in the response'
    block = html[start:html.index("</div>", start)]
    return {
        unescape(v): unescape(re.sub(r"\s+", " ", label)).strip()
        for v, label in re.findall(
            r'<input[^>]*\bvalue="([^"]*)"[^>]*>\s*<span>(.*?)</span>', block, re.S
        )
    }


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

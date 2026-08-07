"""Things only a real browser can answer.

The suite is good at "what did the endpoint return". It is blind to everything
that happens after: where an element actually sits on screen, whether a trigger
re-fires, whether a click lands on the thing it was aimed at. Six UI changes
shipped on 2026-08-06 and every claim about their *rendered* behaviour was
argued from HTML ordering or from reading minified htmx. This file checks them.

Scope discipline: a test belongs here only if `TestClient` genuinely cannot
answer it. Everything else stays in the endpoint tests, which are ~100x faster.

Runs against the installed Google Chrome (`channel="chrome"`), so there is no
`playwright install` step and no browser download. Skips cleanly when the dev
requirements are absent — that skip is what keeps playwright optional rather
than optional-in-name.
"""

import re
import time

import pytest

pytest.importorskip("playwright.sync_api", reason="pip install -r requirements-dev.txt")

from playwright.sync_api import sync_playwright  # noqa: E402

import main  # noqa: E402
from config import SALARY_CAP  # noqa: E402

pytestmark = pytest.mark.browser


# ---------------------------------------------------------------- harness


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        yield b
        b.close()


@pytest.fixture
def page(browser, live_server):
    """A fresh context per test, against a freshly reset auction."""
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    pg = context.new_page()
    pg.request.post(f"{live_server}/reset")
    yield pg
    context.close()


# ---------------------------------------------------------------- helpers


def _open(page, live_server):
    page.goto(live_server, wait_until="domcontentloaded")
    page.wait_for_selector("#bid-panel")


def _pool_top(n: int = 2) -> list[str]:
    """The n highest-scoring available players, by name."""
    ranked = sorted(
        main.auction_state.available_players.values(),
        key=lambda p: -p.projected_points,
    )
    return [p.name for p in ranked[:n]]


def _start_bid(page, player: str, price: str = "3.0", bidders: str = "BOT"):
    """Put a player under the hammer, the way the panel itself does.

    Submits the bid form rather than poking the DOM, so the panel ends up in
    exactly the state a real bid produces — including the Assign button, which
    only renders when one bidder is left standing.
    """
    page.fill("#bid-panel input[name='player']", player)
    page.fill("#bid-price", price)
    page.eval_on_selector(
        "#bid-panel input[name='bidders']", "(el, v) => el.value = v", bidders
    )
    with page.expect_response(re.compile(r"/bid-check")):
        page.click("#bid-panel button[type='submit']")
    page.wait_for_selector("#bid-counterfactual")


def _squeeze(code: str, headroom: float) -> None:
    """Set `code`'s penalties so exactly `headroom` of cap space remains."""
    team = main.auction_state.teams[code]
    team.penalties = 0.0
    team._invalidate_cache()
    team.penalties = round(SALARY_CAP - team.total_salary - headroom, 1)
    team._invalidate_cache()


# ---------------------------------------------------------------- phase 1


class TestCounterfactualDoesNotDisturbTheControls:
    """The reason this harness exists.

    `TestCounterfactualAutoLoads` asserts the mount comes after the Assign form
    *in the HTML*. That is a proxy for the thing that matters — whether the
    button moves on screen when a ~200ms-late fragment lands — and a proxy is
    exactly what a browser is for.
    """

    def test_assign_button_does_not_move_when_the_analysis_lands(
        self, page, live_server
    ):
        _open(page, live_server)
        player = _pool_top(1)[0]

        # Hold /explain so the pre-arrival layout can be measured. Without the
        # delay the fragment is already in place by the time the panel settles
        # and the test would compare a position against itself.
        def hold(route):
            time.sleep(0.4)
            route.continue_()

        page.route("**/explain/**", hold)
        _start_bid(page, player)

        before = page.locator("#assign-price").bounding_box()
        page.wait_for_selector("#bid-counterfactual .alert")
        after = page.locator("#assign-price").bounding_box()

        assert before is not None and after is not None
        assert before["y"] == after["y"], (
            f"Assign moved {after['y'] - before['y']:.0f}px when the "
            f"counterfactual landed — a pointer already travelling toward it "
            f"would miss, which is the blur race in a different costume"
        )

    def test_the_counterfactual_loads_exactly_once(self, page, live_server):
        """`hx-swap='innerHTML'` must not re-arm the `load` trigger.

        htmx processes the swapped-in children, not the container, so the
        trigger should not re-fire — reasoned from the minified source when the
        mount was written, never observed until now. A loop here would be a
        request every ~10ms for the whole auction.
        """
        _open(page, live_server)
        seen: list[str] = []
        page.on("request", lambda r: seen.append(r.url) if "/explain/" in r.url else None)

        _start_bid(page, _pool_top(1)[0])
        page.wait_for_selector("#bid-counterfactual .alert")
        page.wait_for_timeout(600)  # a loop would have fired many times by now

        assert len(seen) == 1, f"expected one /explain, got {len(seen)}: {seen}"


class TestTheAssignClickSurvives:
    """The blur race, at the speed that made it dangerous.

    `change` on a number input fires on BLUR, and clicking Assign is what blurs
    the price box — so that /bid-check is triggered by the very click it can
    destroy. The last manual confirmation of this predates the 114x speedup
    that took /bid-check from ~1000ms (landing safely after mouseup) to ~9ms
    (landing mid-click), so it confirmed a version of the app where the bug was
    masked.
    """

    def test_typing_a_price_then_clicking_assign_records_the_typed_price(
        self, page, live_server
    ):
        _open(page, live_server)
        player = _pool_top(1)[0]
        _start_bid(page, player, price="3.0")

        # Type a new price and click Assign with no Tab, no Enter — exactly the
        # sequence that used to be swallowed.
        page.fill("#bid-price", "5.7")
        with page.expect_response(re.compile(r"/assign")) as got:
            page.click("#bid-panel form[hx-vals] button[type='submit']")
        assert got.value.status == 200

        page.wait_for_selector("#toast-container .alert")
        roster = main.auction_state.teams["BOT"].acquired_players
        signed = [p for p in roster if p.name == player]
        assert signed, f"{player} was not recorded — the click was swallowed"
        assert signed[0].salary == 5.7, (
            f"recorded ${signed[0].salary}M, typed $5.7M — Assign read a stale "
            f"price instead of the live input"
        )


class TestBiddingSessionSurvives:
    """The session lives only in the DOM: player, price, bidder toggles.

    Nothing persists it server-side, so any response that replaces #bid-panel
    destroys it. The 2026-08-06 split exists to make that impossible; these
    check it in the browser where the keypress actually happens.
    """

    def test_pressing_n_mid_bid_keeps_the_bidding_session(self, page, live_server):
        _open(page, live_server)
        player = _pool_top(1)[0]
        _start_bid(page, player, price="4.2")

        # Focus a BUTTON, not an input: shortcuts.js guards on
        # INPUT/TEXTAREA/SELECT, so a button is where `n` stays live — and it
        # is where focus lands after clicking a bidder logo.
        page.focus("#bid-panel .bidder-logo-btn")
        with page.expect_response(re.compile(r"/nominate")):
            page.keyboard.press("n")
        page.wait_for_timeout(200)

        assert page.input_value("#bid-panel input[name='player']") == player
        assert page.input_value("#bid-price") == "4.2"

    def test_toggling_a_bidder_keeps_the_session(self, page, live_server):
        _open(page, live_server)
        player = _pool_top(1)[0]
        _start_bid(page, player, price="4.2")

        explains: list[str] = []
        page.on("request", lambda r: explains.append(r.url) if "/explain/" in r.url else None)

        with page.expect_response(re.compile(r"/bid-check")):
            page.click("#bid-panel .bidder-logo-btn[data-team='SRL']")
        page.wait_for_selector("#bid-counterfactual .alert")

        assert page.input_value("#bid-panel input[name='player']") == player
        assert page.input_value("#bid-price") == "4.2"
        assert len(explains) == 1, (
            f"one panel swap should reload the counterfactual once, got "
            f"{len(explains)}"
        )


class TestLayoutAndToasts:
    """The two things no assertion on HTML can reach: CSS and runtime JS."""

    def test_narrow_viewport_stacks_the_columns(self, page, live_server):
        """The 1-col breakpoint, shipped and never once looked at."""
        page.set_viewport_size({"width": 420, "height": 900})
        _open(page, live_server)

        areas = [
            page.locator(f".{cls}").first.bounding_box()
            for cls in ("area-auction", "area-players", "area-team")
        ]
        assert all(a is not None for a in areas)
        xs = {round(a["x"]) for a in areas}
        assert len(xs) == 1, f"columns did not stack at 420px wide: x positions {xs}"

    def test_an_over_cap_toast_renders_and_dismisses(self, page, live_server):
        """Pins the class name shortcuts.js builds at RUNTIME.

        `'alert-' + type` is invisible to any source scanner, which is the
        specific hazard recorded against the Tailwind-build backlog item. It
        survives today only because DaisyUI's prebuilt CSS carries every
        alert-* variant; this is what would catch a build that dropped them.
        """
        _open(page, live_server)
        team = main.auction_state.teams["BOT"]
        minor = max(
            (m for m in team.minor_players if not m.counts_on_cap),
            key=lambda m: m.salary,
        )
        _squeeze("BOT", headroom=minor.salary - 0.5)

        page.request.post(
            f"{live_server}/move-to-roster",
            form={"team_code": "BOT", "player_name": minor.name},
        )
        # Drive it through the UI so the HX-Trigger header reaches the JS
        # listener; a bare request would not exercise the toast at all.
        page.reload(wait_until="domcontentloaded")
        _squeeze("BOT", headroom=0.4)
        with page.expect_response(re.compile(r"/adjust-salary")):
            page.evaluate(
                """([name]) => htmx.ajax('POST', '/adjust-salary', {
                       target: '#app', swap: 'innerHTML',
                       values: {team_code: 'BOT', player_name: name, new_salary: '9.9'}})""",
                [team.roster_players[0].name],
            )

        toast = page.locator("#toast-container .alert-warning")
        toast.wait_for(state="visible", timeout=5000)
        assert "over cap" in toast.inner_text()
        toast.wait_for(state="detached", timeout=8000)

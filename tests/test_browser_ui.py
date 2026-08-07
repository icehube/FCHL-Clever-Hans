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
from tests.helpers import squeeze  # noqa: E402

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


class TestViewingAnotherTeam:
    """The reported symptom, at the level it was reported.

    The endpoint tests prove the returned HTML names SRL. They cannot see
    whether the swap lands: `/adjust-salary` targets `#app` with the whole page
    while `/team-view` targets `#team-panel` with `outerHTML`, so the panel is
    reached two different ways and only one of them is a full-page replace.
    """

    def test_editing_an_opponents_salary_keeps_the_panel_on_them(
        self, page, live_server
    ):
        _open(page, live_server)
        page.click("#league-state a[hx-get='/team-view/SRL']")
        page.wait_for_selector("#team-panel h2:has-text('(SRL)')")

        # The panel's own salary input, auto-submitting on change exactly as a
        # typed correction does — no Assign, no Tab.
        box = page.locator("#team-panel input[name='new_salary']").first
        with page.expect_response(re.compile(r"/adjust-salary")):
            box.fill("3.3")
            box.blur()

        page.wait_for_timeout(300)
        header = page.locator("#team-panel h2").inner_text()
        assert "(SRL)" in header, (
            f"panel snapped to {header!r} after editing SRL — the whole-page "
            f"swap discarded the view"
        )
        # And the forms in the panel that landed post back to SRL. Rendering
        # SRL's roster over BOT's hidden inputs would make the next Bench click
        # edit a player BOT doesn't have.
        posts_to = page.eval_on_selector_all(
            "#team-panel input[name='team_code']",
            "els => [...new Set(els.map(e => e.value))]",
        )
        assert posts_to == ["SRL"], f"panel shows SRL but posts to {posts_to}"


class TestLayoutAndToasts:
    """The two things no assertion on HTML can reach: CSS and runtime JS."""

    def _column_xs(self, page) -> set[int]:
        areas = [
            page.locator(f".{cls}").first.bounding_box()
            for cls in ("area-auction", "area-players", "area-team")
        ]
        assert all(a is not None for a in areas), "a grid area did not render"
        return {round(a["x"]) for a in areas}

    def test_the_layout_responds_to_width(self, page, live_server):
        """The breakpoints, shipped and never once looked at.

        BOTH ends are asserted on purpose. Checking only the narrow case passes
        even with every `@media` rule deleted — verified by mutation: removing
        both queries destroys the 3-column desktop layout the draft is actually
        run in, and a stacked-only assertion notices nothing.
        """
        page.set_viewport_size({"width": 420, "height": 900})
        _open(page, live_server)
        narrow = self._column_xs(page)
        assert len(narrow) == 1, f"columns did not stack at 420px: {narrow}"

        page.set_viewport_size({"width": 1280, "height": 900})
        page.wait_for_function(
            "() => document.querySelector('.area-players').getBoundingClientRect().x > 0"
        )
        wide = self._column_xs(page)
        assert len(wide) == 3, (
            f"columns did not spread at 1280px: {wide} — the media queries are "
            f"gone or the grid template changed, and the draft runs at this width"
        )

    def test_an_over_cap_adjust_salary_toast_renders_and_dismisses(
        self, page, live_server
    ):
        """Pins the class name shortcuts.js builds at RUNTIME.

        `'alert-' + type` is invisible to any source scanner, which is the
        specific hazard recorded against the Tailwind-build backlog item. It
        survives today only because DaisyUI's prebuilt CSS carries every
        alert-* variant; this is what would catch a build that dropped them.

        Fired through `htmx.ajax` rather than a clicked control: the toast is
        driven by the `HX-Trigger` response header, so the request has to come
        from the page. An out-of-band `page.request.post` mutates state and
        produces no toast at all — an earlier draft of this test did exactly
        that, and the assertion below was passing on a *different* endpoint's
        toast than the one it appeared to set up.
        """
        _open(page, live_server)
        team = main.auction_state.teams["BOT"]
        subject = min(team.roster_players, key=lambda p: p.salary).name
        squeeze("BOT", headroom=0.4)

        with page.expect_response(re.compile(r"/adjust-salary")):
            page.evaluate(
                """([name]) => htmx.ajax('POST', '/adjust-salary', {
                       target: '#app', swap: 'innerHTML',
                       values: {team_code: 'BOT', player_name: name, new_salary: '9.9'}})""",
                [subject],
            )

        toast = page.locator("#toast-container .alert-warning")
        toast.wait_for(state="visible", timeout=5000)
        text = toast.inner_text()
        assert "over cap" in text and "BOT" in text, text
        toast.wait_for(state="detached", timeout=8000)

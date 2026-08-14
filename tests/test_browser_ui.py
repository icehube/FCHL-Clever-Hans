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
from tests.helpers import pool_top, squeeze  # noqa: E402

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
        player = pool_top()[0]

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

        _start_bid(page, pool_top()[0])
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
        player = pool_top()[0]
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
        player = pool_top()[0]
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
        player = pool_top()[0]
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

    # Every width the 3-column layout can be run at: 1024 is the tightest
    # (329px tracks), 1280 is the draft laptop, 1600 the top of that range.
    CONTAINMENT_WIDTHS = (1024, 1280, 1600)

    CONTAINMENT_PROBE = """() => {
      const g = document.querySelector('.auction-grid');
      const gr = g.getBoundingClientRect();
      const clientLeft = gr.left + g.clientLeft;
      const spill = [];
      const cards = g.querySelectorAll('section.card');
      for (const c of cards)
        if (c.scrollWidth > c.clientWidth + 1 &&
            getComputedStyle(c).overflowX === 'visible')
          spill.push((c.id ? '#' + c.id : c.tagName) +
                     ' content ' + c.scrollWidth + ' in ' + c.clientWidth);
      const areas = {};
      for (const cls of ['area-auction', 'area-players', 'area-team']) {
        const r = document.querySelector('.' + cls).getBoundingClientRect();
        areas[cls] = {left: Math.round(r.left), right: Math.round(r.right)};
      }
      return {
        gridScrollW: g.scrollWidth, gridClientW: g.clientWidth,
        gridClientLeft: Math.round(clientLeft),
        gridClientRight: Math.round(clientLeft + g.clientWidth),
        tracks: getComputedStyle(g).gridTemplateColumns
                  .split(' ').map(t => parseFloat(t)),
        pageScrollW: document.scrollingElement.scrollWidth,
        innerWidth: window.innerWidth,
        cardsSeen: cards.length,
        areas, spill,
      };
    }"""

    def test_the_grid_never_overflows_its_own_width(self, browser, live_server):
        """#team-panel was entirely off-screen on the draft laptop until 2026-08-11.

        `.auction-grid` used bare `1fr` tracks, i.e. `minmax(AUTO, 1fr)`, so per
        css-grid §6.6 each item took a content-based automatic minimum and the
        widest panel set its own column's floor. Measured at 1280: tracks
        292/991/585, grid content 1904 against a 1280 client box, `.area-team`
        starting at x=1310. Cap Used, Remaining, Max Bid, the roster and the
        buyout dots were all off-screen while bidding.

        It was invisible because `all_panels.html`'s inline `overflow-y: auto`
        forces `overflow-x` to `auto` too, so the overflow went behind the GRID's
        scrollbar and no page scrollbar ever appeared — and because
        `test_the_layout_responds_to_width` above only counts DISTINCT column x
        values, which `{9, 311, 1310}` satisfies perfectly. Three columns is not
        the same claim as three columns you can see.
        """
        for width in self.CONTAINMENT_WIDTHS:
            context = browser.new_context(viewport={"width": width, "height": 900})
            pg = context.new_page()
            pg.request.post(f"{live_server}/reset")
            _open(pg, live_server)
            d = pg.evaluate(self.CONTAINMENT_PROBE)

            assert d["gridScrollW"] <= d["gridClientW"] + 1, (
                f"{width}px: the grid overflows itself by "
                f"{d['gridScrollW'] - d['gridClientW']}px "
                f"({d['gridScrollW']} content in {d['gridClientW']}) — panels "
                f"that cannot scroll to their own content: {d['spill'] or 'none'}"
            )
            for cls, r in d["areas"].items():
                assert r["left"] >= d["gridClientLeft"] - 1, (
                    f"{width}px: .{cls} starts at x={r['left']}, left of the "
                    f"grid's content box at {d['gridClientLeft']}"
                )
                assert r["right"] <= d["gridClientRight"] + 1, (
                    f"{width}px: .{cls} ends at x={r['right']}, past the grid's "
                    f"content box at {d['gridClientRight']} — it is off-screen"
                )
            # Not "let the body scroll instead", which is what deleting the
            # wrapper's inline overflow-y would silently turn this into.
            assert d["pageScrollW"] <= d["innerWidth"] + 1, (
                f"{width}px: the PAGE now scrolls horizontally "
                f"({d['pageScrollW']} > {d['innerWidth']})"
            )
            # One assertion, both failure directions: a hogged track (292/1885 =
            # 0.155 before the fix) and a future collapse to nothing. 0.25 not
            # 0.33 so a deliberate `1fr 1.4fr 1fr` later is not a false failure.
            tracks = d["tracks"]
            assert len(tracks) == 3, f"{width}px: expected 3 tracks, got {tracks}"
            assert min(tracks) >= 0.25 * sum(tracks), (
                f"{width}px: one column is hogging the row — tracks {tracks}"
            )
            # `spill` is built by iterating a selector, so an empty result means
            # EITHER nothing overflows or the selector matched nothing — and the
            # second reads exactly like the first. Nine `section.card` partials
            # render unconditionally inside the grid, so 8 leaves room to delete
            # one panel while still catching a rename to `div.card`. The `areas`
            # loop above needs no equivalent: `querySelector` returns null there
            # and the probe throws, which is loud.
            assert d["cardsSeen"] >= 8, (
                f"{width}px: only {d['cardsSeen']} section.card panels found in "
                f"the grid — the spill check below iterates that selector, so it "
                f"would pass while measuring almost nothing"
            )
            # The invariant that survives the next wide column someone adds to
            # either table: a panel whose content overflows must be able to
            # scroll to it. Fails if a .table-scroll-x wrapper is forgotten.
            assert not d["spill"], (
                f"{width}px: content overflows a panel that cannot scroll, so it "
                f"paints over its neighbour: {d['spill']}"
            )
            context.close()

    def test_the_layout_responds_to_width(self, page, live_server):
        """The breakpoints, shipped and never once looked at.

        BOTH ends are asserted on purpose. Checking only the narrow case passes
        even with every `@media` rule deleted — verified by mutation: removing
        both queries destroys the 3-column desktop layout the draft is actually
        run in, and a stacked-only assertion notices nothing.

        What this CANNOT see is containment: it compares only the count of
        distinct column x values, so it passed for months while `.area-team` sat
        at x=1310 with the viewport 1280 wide. That is
        `test_the_grid_never_overflows_its_own_width` above.
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


class TestTheChartLandsWhereYouClicked:
    """The duplicate id, at the only altitude where it is visible.

    `player_chart.html` used to carry `id="player-chart-container"` itself
    while `bid_limits.html` rendered an empty div with the same id as the
    table's swap target. No single server response contained both copies — the
    chart body reaches the page by a swap — so the whole suite passed while a
    chart link in the players table rendered its chart into the bid panel in
    the other column, `area-auction` being first in document order.
    """

    def _open_with_a_live_bid(self, page, live_server):
        """A DOM holding both mounts: the bid panel embeds its own chart."""
        _open(page, live_server)
        bid_player, other = pool_top(2)
        _start_bid(page, bid_player)
        page.wait_for_selector("#bid-panel .price-chart-card")
        return bid_player, other

    def _click_chart_link(self, page, player: str):
        link = page.locator(
            f'#bid-limits a[hx-get^="/player-chart/"]:text-is("{player}")'
        )
        with page.expect_response(re.compile(r"/player-chart/")):
            link.click()
        page.wait_for_selector("#player-chart-container .price-chart-card")

    def test_the_chart_opens_above_the_table_not_in_the_bid_panel(
        self, page, live_server
    ):
        bid_player, other = self._open_with_a_live_bid(page, live_server)
        self._click_chart_link(page, other)

        mounted = page.locator("#player-chart-container .price-chart-card")
        assert other in mounted.inner_text()

        # The screen-position claim, which is the whole user-visible bug and
        # the one thing TestClient cannot answer: the chart must appear in the
        # players column the click came from, not the auction column.
        chart_box = mounted.bounding_box()
        panel_box = page.locator("#bid-panel").bounding_box()
        assert chart_box["x"] >= panel_box["x"] + panel_box["width"], (
            f"chart opened at x={chart_box['x']:.0f}, inside/left of the bid "
            f"panel ending at x={panel_box['x'] + panel_box['width']:.0f} — it "
            f"rendered into the wrong column"
        )

        # And the bid panel's own chart is untouched, still showing its player.
        assert bid_player in page.locator("#bid-panel .price-chart-card").inner_text()

    def test_closing_the_bid_panel_chart_leaves_the_table_chart_open(
        self, page, live_server
    ):
        """× must close the chart it sits in, not the first one in the page.

        **This direction is the load-bearing one.** Closing the *table's* chart
        works under either implementation — with an id-free body,
        `getElementById('player-chart-container')` finds the table's mount,
        which is the chart being closed anyway — so a test aimed that way
        passes against the bug and proves nothing. Only the bid panel's ×
        exposes it: `getElementById` reaches across the screen and clears the
        table's chart while leaving the one you actually clicked on.
        """
        bid_player, other = self._open_with_a_live_bid(page, live_server)
        self._click_chart_link(page, other)

        page.click("#bid-panel .price-chart-card button")
        page.wait_for_selector("#bid-panel .price-chart-card", state="detached")
        assert page.locator("#player-chart-container .price-chart-card").count() == 1, (
            "closing the bid panel's chart also closed the table's, in the "
            "other column — × resolved to the wrong element"
        )


class TestShortcutsModalOpens:
    """`showModal()` and `<dialog>` are browser behaviour, not markup.

    The endpoint tests can prove the button and the dialog are on the page and
    that the documented keys match the handler. Whether clicking actually opens
    a top-layer dialog, and whether it closes again, only a browser knows.
    """

    # `.open`, never is_visible(): DaisyUI's .modal leaves a CLOSED <dialog>
    # laid out and merely transparent (opacity 0 + pointer-events none) rather
    # than display:none, so Playwright reports a shut dialog as "visible" and an
    # is_visible() assertion passes in both states. Found by writing it wrong.
    _OPEN = "() => document.getElementById('shortcuts-modal').open"

    def test_the_button_opens_and_closes_the_dialog(self, page, live_server):
        _open(page, live_server)
        assert not page.evaluate(self._OPEN), "the dialog starts open"

        page.click("button[title='Keyboard shortcuts']")
        page.wait_for_function(self._OPEN)

        text = page.locator("#shortcuts-modal").inner_text()
        assert "Ctrl" in text and "Z" in text and "N" in text

        page.keyboard.press("Escape")
        page.wait_for_function(f"() => !({self._OPEN})()")

    def test_it_survives_a_panel_swap_while_open(self, page, live_server):
        """The reason it is mounted outside #app.

        Every mutating endpoint swaps all_panels.html into #app. A dialog
        inside it would be torn out mid-read the moment a pick landed — and
        during a draft, picks land while you are reading.

        The swap is fired through htmx.ajax rather than by clicking Undo,
        because a top-layer modal correctly blocks clicks on the page behind
        it — an earlier draft of this test clicked Undo and timed out, which is
        the browser being right. A background swap is the reachable case
        anyway: an in-flight request landing while the list is open.
        """
        _open(page, live_server)
        page.click("button[title='Keyboard shortcuts']")
        page.wait_for_function(self._OPEN)

        with page.expect_response(re.compile(r"/undo")):
            page.evaluate(
                "() => htmx.ajax('POST', '/undo', {target: '#app', swap: 'innerHTML'})"
            )
        page.wait_for_selector("#bid-panel")

        assert page.evaluate(self._OPEN), "a panel swap closed the shortcuts dialog"


class TestTheViewSticksOnScreen:
    """The view surviving a full-page swap, where it is actually visible.

    The endpoint tests assert on the returned HTML. This asserts on what is on
    screen after htmx has swapped #app — the claim the operator experiences,
    and the one that reads as fixed only in a browser.
    """

    def _panel_team(self, page) -> str:
        return page.locator("#team-panel h2").inner_text()

    def test_marking_another_team_done_does_not_snap_the_panel_back(
        self, page, live_server
    ):
        _open(page, live_server)
        with page.expect_response(re.compile(r"/team-view/SRL")):
            page.click("[hx-get='/team-view/SRL']")
        page.wait_for_function(
            "() => document.querySelector('#team-panel h2').textContent.includes('(SRL)')"
        )

        # A third team, so the toggle has nothing to do with the roster on show.
        # It swaps all of #app, which is what used to take the view with it.
        with page.expect_response(re.compile(r"/team-done")):
            page.click("#league-state form:has(input[value='MAC']) button")
        page.wait_for_selector("#team-panel")

        assert "(SRL)" in self._panel_team(page), (
            "a League State toggle threw the panel back to your own team"
        )

    def test_the_scan_button_follows_the_view_both_ways(self, page, live_server):
        """A control that cannot work must not be offered — and must come back.

        Its OOB swaps target `bo-` dots that exist for BOT only, so on an
        opponent every swap missed and htmx logged htmx:oobErrorNoTarget. The
        return trip is the half a `TestClient` will not show you: `/team-view`
        swaps `#team-panel` only, so a button gated inside the *buyout* panel
        vanished on the way out and never came back, and every endpoint test
        read `GET /` afterwards — a fresh document, where it is always correct.
        """
        scan = "#buyout-panel [hx-get='/buyout-indicators']"
        _open(page, live_server)
        assert page.locator(scan).count() == 1

        with page.expect_response(re.compile(r"/team-view/SRL")):
            page.click("[hx-get='/team-view/SRL']")
        page.wait_for_function(
            "() => document.querySelector('#team-panel h2').textContent.includes('(SRL)')"
        )
        assert page.locator(scan).count() == 0, "the scan button survived on an opponent"

        with page.expect_response(re.compile(r"/team-view/BOT")):
            page.click("[hx-get='/team-view/BOT']")
        page.wait_for_function(
            "() => document.querySelector('#team-panel h2').textContent.includes('(BOT)')"
        )
        assert page.locator(scan).count() == 1, (
            "coming home left the scan button missing until some unrelated "
            "full-page swap restored it"
        )


class TestTheDataBannerOutlivesAPick:
    """The renamed-players note has to still be there after the first pick.

    `TestClient` can see that `GET /` puts the banner before `<div id="app">`,
    which is an argument about HTML ordering — the same proxy the counterfactual
    test at the top of this file exists to replace. What matters is whether the
    element is still in the document once htmx has replaced `#app`'s innerHTML,
    and only a browser has run that swap. Same failure the startup banner was
    moved out of `#app` to avoid: drafting one player silently clears the notice
    explaining why half the pool has parenthesised suffixes.
    """

    def test_the_banner_is_outside_app_and_survives_an_assign(
        self, page, live_server
    ):
        import data_loader

        if not data_loader.loaded_disambiguations:
            pytest.skip("players.csv has no duplicate names — nothing to report")

        _open(page, live_server)
        assert page.locator("#data-warning").count() == 1
        assert page.evaluate(
            "() => !document.querySelector('#app')"
            ".contains(document.querySelector('#data-warning'))"
        ), "the banner is inside #app, so the next panel swap deletes it"

        # A real pick through the real controls, not htmx.ajax: the Assign form
        # is what targets #app with the whole page, and it is the swap the
        # operator triggers first.
        player = pool_top()[0]
        _start_bid(page, player)
        with page.expect_response(re.compile(r"/assign")):
            page.click("#bid-panel form[hx-vals] button[type='submit']")
        page.wait_for_selector("#toast-container .alert")

        assert page.locator("#data-warning").count() == 1, (
            "the first pick of the draft wiped the note explaining the renamed "
            "players, and nothing brings it back until a full page load"
        )
        original = next(iter(data_loader.loaded_disambiguations))
        assert original in page.locator("#data-warning").inner_text()


class TestTheScanSurvivesAnAwkwardName:
    """One bad dot id abandons the whole scan, and only a browser says so.

    htmx resolves an out-of-band target by building a CSS SELECTOR from the id
    (`htmx-1.9.10.min.js`, `Ee`: `var t = "#" + ee(i,"id"); …
    re().querySelectorAll(t)`), and it calls that from a plain forEach with no
    try/catch. So an id that is not a valid CSS identifier does not merely miss
    its own target — `querySelectorAll` throws and every remaining OOB swap in
    the response is abandoned.

    `data/players.csv` carries names with backticks and parentheses, and
    `_disambiguated_names` adds `(TEAM)`, `(TEAM POS)` and `(#n)` suffixes on
    top. An endpoint test sees two matching id strings and cannot see the
    selector being rejected.
    """

    # Characters the id derivation has to survive. Spaces and hyphens are fine
    # in an identifier; everything else must be removed or encoded.
    _NEEDS_WORK = re.compile(r"[^A-Za-z0-9 -]")
    # The subset the template's historical `replace` chain never contemplated.
    # `.` and `'` it strips; backticks, parentheses and the `#` that
    # `_disambiguated_names` can emit it does not.
    _BEYOND_THE_OLD_STRIP = re.compile(r"[^A-Za-z0-9 .'-]")

    def _awkward_biddable(self) -> str | None:
        """The hardest-to-encode name in the pool, or None if it is all plain.

        Derived, never named — the pool is replaced before every draft. Sorted
        so a name the old strip could not fix wins over one it could: picking
        merely the first `_NEEDS_WORK` match found `J.T. Miller`, whose dots the
        strip already removed, and the test passed against the live bug.
        """
        candidates = [
            n for n in main.auction_state.available_players if self._NEEDS_WORK.search(n)
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda n: not self._BEYOND_THE_OLD_STRIP.search(n))

    def test_every_dot_resolves_with_an_awkward_name_on_the_roster(
        self, page, live_server
    ):
        victim = self._awkward_biddable()
        # Skip rather than settle for a benign name. A pool with nothing beyond
        # the old strip cannot break the scan this way, and running against
        # `J.T. Miller` — whose dots the strip already removed — is what let an
        # earlier draft of this test pass against the live bug. Silent
        # degradation into a no-op is worse than an honest skip, because the
        # test keeps reading as coverage.
        if victim is None or not self._BEYOND_THE_OLD_STRIP.search(victim):
            pytest.skip(
                f"no pool name needs escaping beyond `.` and `'` "
                f"(best candidate: {victim!r}) — nothing here can break the scan"
            )

        r = page.request.post(
            f"{live_server}/assign",
            form={"player": victim, "team": "BOT", "salary": "1.0"},
        )
        assert r.status == 200
        assert any(
            p.name == victim for p in main.auction_state.teams["BOT"].all_players
        ), f"{victim} was not drafted, so the scan has nothing awkward to trip on"

        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on(
            "console",
            lambda m: errors.append(f"{m.type}: {m.text}") if m.type == "error" else None,
        )
        _open(page, live_server)

        placeholders = page.eval_on_selector_all("[id^='bo-']", "els => els.map(e => e.id)")
        assert placeholders, "no dot placeholders rendered at all"

        # Asked of the browser's own selector parser rather than a regex
        # approximation of it — the consumer is Chrome, so Chrome is the oracle.
        unusable = page.evaluate(
            """() => [...document.querySelectorAll("[id^='bo-']")]
                 .filter(e => {
                     try { document.querySelectorAll('#' + e.id); return false }
                     catch (err) { return true }
                 }).map(e => e.id)"""
        )
        assert not unusable, (
            f"htmx builds its OOB target as '#' + id, and these are not valid "
            f"selectors: {unusable}"
        )

        # The Analyzer is a collapsed <details>, so scanning is two clicks for
        # real — open the disclosure the way the operator does, or the button
        # is present in the DOM and unclickable.
        page.click("#buyout-panel summary")
        scan = "#buyout-panel [hx-get='/buyout-indicators']"
        page.locator(scan).scroll_into_view_if_needed()
        with page.expect_response(re.compile(r"/buyout-indicators")) as got:
            page.click(scan)
        assert got.value.status == 200
        # One MILP solve per eligible player, so give the swaps room to land.
        page.wait_for_timeout(4000)

        unresolved = page.locator(".buyout-light.light-grey").count()
        assert not errors, f"the scan threw in the browser: {errors[:3]}"
        assert unresolved == 0, (
            f"{unresolved} of {len(placeholders)} dots never resolved — one "
            f"un-escapable id ({victim!r}) aborts every OOB swap after it"
        )


class TestATypoDoesNotVanish:
    """The Start Auction field is free text, and a name it can't find used to
    empty the box with nothing said.

    `/bid-check` answered the unknown-player case with the bare empty form, so
    the whole-panel swap replaced what you typed with a blank input: no toast,
    no message, no clue whether the app had crashed or simply not heard you.
    `TestClient` can see that the response now carries the text and the note; it
    cannot see whether either survives htmx replacing the panel, which is the
    entire failure — the old behaviour also answered 200 with a valid panel.
    """

    def test_an_unrecognized_name_leaves_a_message_and_the_text(
        self, page, live_server
    ):
        _open(page, live_server)
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        # Derived from the pool, so a data refresh cannot turn this into a
        # successful bid check that silently tests nothing.
        typo = f"{pool_top()[0]} Jr."
        assert typo not in main.auction_state.available_players

        page.fill("#bid-panel input[name='player']", typo)
        with page.expect_response(re.compile(r"/bid-check")):
            page.click("#bid-panel button[type='submit']")

        page.wait_for_selector("#bid-advice")
        assert page.locator("#bid-advice").is_visible(), (
            "the unknown-player note is in the DOM but not on screen"
        )
        assert typo in page.locator("#bid-advice").inner_text(), (
            "the message does not name the player, so it reads as a generic "
            "failure rather than a spelling correction"
        )
        assert page.input_value("#bid-panel input[name='player']") == typo, (
            "the typed name was wiped by the swap, so correcting a one-letter "
            "typo means retyping the whole thing mid-auction"
        )
        assert not errors, f"the console threw: {errors[:3]}"


class TestTooltipsStayInsideTheirPanel:
    """An explanation you cannot read is worse than none — it looks answered.

    DaisyUI centres a tooltip bubble on its trigger (`left: 50%` plus
    `translateX(-50%)`, vendor `.tooltip-bottom:before`) and has no flip logic,
    so a bubble on a trigger near a container's left edge renders off it. This
    is unreachable from `TestClient`: the HTML is identical either way and the
    position only exists once Chrome has laid the page out and resolved the
    pseudo-element's box.

    **Measured against each bubble's own nearest SCROLLING ANCESTOR**, which is
    what "can the operator read this" actually depends on. Until 2026-08-11 this
    measured everything against `.auction-grid` in grid-content coordinates,
    bounded by `g.scrollWidth` — written that way because the grid overflowed
    horizontally by 624px, so a panel could sit legitimately outside the
    viewport. That overflow was a real bug (the team panel was off-screen on the
    draft laptop) and fixing it made this test's old bound both too tight and
    wrong in kind:

    - Too tight: `scrollWidth` collapsed to `clientWidth`, removing ~665px of
      slack at 375px. The suite had been resting on the layout bug — a 15rem
      bubble in a ~342px panel passed only because the League State table
      inflated the bound.
    - Wrong in kind: the fix gives the League State and roster tables their own
      `.table-scroll-x` scrollers, so a `th` bubble 700px into a 415px scroller
      is perfectly reachable — a FALSE offender under any grid-anchored bound.

    So there are now several scrollers, and each bubble is judged inside the one
    it lives in. The extra bound `w <= clientWidth` is the honest version of
    what the old one approximated: a bubble wider than the box containing it can
    never be read whole, wherever it is anchored. Grid containment itself is no
    longer this test's job — `test_the_grid_never_overflows_its_own_width` owns
    it.

    Pseudo-elements have no `getBoundingClientRect`, but their box is exactly
    computable from the resolved `left`/`right`, the transform matrix and the
    trigger's own rect, which is what makes this an assertion rather than a
    screenshot.
    """

    # Every breakpoint in style.css (1-col, 2-col, 3-col) plus the edges either
    # side of the 768px switch, where the stat tiles narrow to ~191px and a
    # centred 15rem bubble no longer clears the panel edge. 800 is the width
    # that actually caught the team-panel case; 375 and 1280 did not.
    WIDTHS = (375, 640, 700, 800, 1024, 1280, 1920)

    # (width, scenario-or-None). A fresh reset cannot render 8 of the app's 20
    # `data-tip` tooltips, and the one that mattered most is `#bid-limits`' only
    # `tooltip-left`: it renders per CAPPED row, and on a fresh state the ceiling
    # IS the salary cap, so nothing is ever capped and this suite had never once
    # measured it (`BACKLOG.md`, bid_limits.html:41). `endgame-ceiling-binds`
    # produces ~83 capped rows.
    #
    # Three widths rather than all seven, because the extra cost is a page load
    # plus a live bid each and the risk does not vary smoothly: 375 is the 1-col
    # case where the panel is widest, 1024 the tightest 3-col track (~329px), and
    # 1280 the width the draft is actually run at. A left-anchored bubble in a
    # horizontally scrollable table is most at risk at the narrow end.
    STATES = tuple((w, None) for w in WIDTHS) + (
        (375, "endgame-ceiling-binds"),
        (1024, "endgame-ceiling-binds"),
        (1280, "endgame-ceiling-binds"),
    )

    PROBE = """() => {
      // The box a bubble must stay inside is the nearest ancestor that can
      // scroll, not the grid: since 2026-08-11 the league table and the roster
      // each have their own .table-scroll-x, and a bubble deep inside one is
      // reachable by scrolling it.
      const scrollerOf = el => {
        for (let p = el.parentElement; p; p = p.parentElement)
          if (getComputedStyle(p).overflowX !== 'visible') return p;
        return document.scrollingElement;
      };
      const name = el => el.id ? '#' + el.id
        : (el.className || '').toString().trim().split(/\\s+/)[0] || el.tagName.toLowerCase();
      // An absolutely positioned box is offset from the PADDING BOX of its
      // nearest positioned ancestor. Usually that is the trigger itself
      // (DaisyUI makes .tooltip position:relative), but .chart-meta hands the
      // containing block to the line instead, so this must be derived rather
      // than assumed — a probe that assumed the trigger reported a
      // correctly-placed bubble as 312px wide at x=244.
      const cbOf = el => {
        for (let p = el; p; p = p.parentElement)
          if (getComputedStyle(p).position !== 'static') return p;
        return document.documentElement;
      };
      const out = [];
      for (const el of document.querySelectorAll('.tooltip[data-tip]')) {
        el.classList.add('tooltip-open');      // vendor CSS keeps it opacity:0
        const cs = getComputedStyle(el, '::before');
        const w = parseFloat(cs.width);
        const cb = cbOf(el);
        const cbr = cb.getBoundingClientRect();
        const r = {left: cbr.left + cb.clientLeft, width: cb.clientWidth};
        // Anchored bubbles use `right: 0; left: auto` (the .team-stats rules in
        // style.css), so `left` resolves to `auto` for them and has to be
        // derived from `right` against the containing block.
        const cl = parseFloat(cs.left), cr = parseFloat(cs.right);
        const rel = !isNaN(cl) ? cl : (!isNaN(cr) ? r.width - cr - w : NaN);
        const m = new DOMMatrixReadOnly(cs.transform === 'none' ? '' : cs.transform);
        el.classList.remove('tooltip-open');
        const s = scrollerOf(el);
        const sr = s.getBoundingClientRect();
        const ox = -(sr.left + s.clientLeft) + s.scrollLeft;
        const left = r.left + rel + m.m41 + ox;
        out.push({widthAuto: isNaN(w) || isNaN(rel),
                  text: el.textContent.trim().replace(/\\s+/g, ' ').slice(0, 30),
                  tip: el.getAttribute('data-tip') || '',
                  left: Math.round(left), right: Math.round(left + w),
                  width: Math.round(w),
                  box: name(s), boxContent: s.scrollWidth, boxClient: s.clientWidth});
      }
      return {rows: out};
    }"""

    def test_no_tooltip_renders_outside_the_scrollable_content(
        self, browser, live_server
    ):
        offenders: list[str] = []
        counted = 0
        seen: set[str] = set()
        for width, state in self.STATES:
            context = browser.new_context(viewport={"width": width, "height": 900})
            pg = context.new_page()
            if state is None:
                pg.request.post(f"{live_server}/reset")
            else:
                pg.request.post(f"{live_server}/load-scenario", form={"name": state})
            _open(pg, live_server)
            # A live bid, so .bid-details and its four tooltips actually exist —
            # they render only inside a verdict block.
            _start_bid(pg, pool_top()[0], bidders="BOT,SRL,MAC")
            pg.wait_for_selector(".bid-details .tooltip")

            where = f"{width}px/{state or 'fresh'}"
            data = pg.evaluate(self.PROBE)
            assert data["rows"], f"no tooltips found at {where}"
            counted = max(counted, len(data["rows"]))
            seen.update(r["tip"] for r in data["rows"])
            for row in data["rows"]:
                # `auto` on both sides means the bubble was never laid out,
                # which would make every comparison below vacuously true.
                assert not row["widthAuto"], (
                    f"{where}: could not resolve a bubble box for "
                    f"{row['text']!r} — this measurement is not viable"
                )
                if row["left"] < 0 or row["right"] > row["boxContent"]:
                    offenders.append(
                        f"{where} {row['text']!r} spans "
                        f"[{row['left']},{row['right']}] of "
                        f"0..{row['boxContent']} in {row['box']}"
                    )
                # A bubble wider than the box it lives in cannot be read whole
                # at any anchoring, so no amount of `left`/`right` fixes it —
                # only a narrower max-width will.
                elif row["width"] > row["boxClient"]:
                    offenders.append(
                        f"{where} {row['text']!r} is {row['width']}px wide "
                        f"inside {row['box']}, which is only "
                        f"{row['boxClient']}px — unreadable at any anchor"
                    )
            context.close()

        # Named rather than counted. A count alone goes quiet-green the day a
        # template drops a tooltip: fewer bubbles trivially means fewer
        # offenders, so this test would keep passing while covering less. These
        # four are the ones the 2026-08-08 batch placed or wrote — one per
        # distinct container, so between them they exercise every rule in the
        # CSS block (left-anchored flex row, capped-width stat grid, the
        # league table header, and the chart meta line).
        #
        # Matched on `data-tip`, NOT on the trigger's label. Labels are not
        # unique: the first version of this required "Proj", which the team
        # panel's pre-existing "Proj PTS" tile satisfies — so deleting the
        # league-table Proj tooltip this batch added left the test green.
        # Caught by mutation, which is the only thing that would have caught it.
        required = {
            "Worth up to (bid panel)": "HARD LIMIT",
            "Marginal (bid panel)": "What he adds to YOUR optimal roster",
            "Sigma (price chart)": "How SPREAD OUT",
            "Proj (league table)": "Computed two ways",
            # The app's only `tooltip-left`, and the reason the endgame scenario
            # is in STATES. It renders per CAPPED row, so on a fresh state it
            # never renders at all and this suite measured 0 of them until
            # 2026-08-13. Requiring it by name means a scenario that stops
            # producing capped rows fails HERE rather than quietly reverting this
            # to a fresh-state-only check that still passes.
            "Capped model price (available players)": "no opponent can push bidding that high",
        }
        missing = [k for k, frag in required.items() if not any(frag in t for t in seen)]
        assert not missing, (
            f"{missing} never appeared as a tooltip — either the template "
            f"dropped it or the page never reached the state that renders it, "
            f"and this test silently stops covering it either way. "
            f"Measured tips: {sorted(t[:40] for t in seen)}"
        )
        assert counted >= 10, (
            f"only {counted} tooltips were ever measured — the page must render "
            f"the bid panel's four and the team panel's stat tiles, or this "
            f"passes while checking almost nothing"
        )
        assert not offenders, (
            f"{len(offenders)} tooltip bubbles render outside the panel area:\n  "
            + "\n  ".join(offenders[:8])
        )


class TestMidBidClutterCanBeDismissed:
    """Two things that compete with the bid advice at the highest-tempo moment.

    Both are DOM-removal properties, which is why they live here: TestClient can
    prove the close button and the marker class are RENDERED (and
    `TestExplain` does), but only a browser can answer whether a click removes
    the right element — or whether removing an element cancels the request that
    was meant to accompany it.
    """

    def test_closing_one_counterfactual_leaves_the_other(self, page, live_server):
        """The two-mount invariant, at the only altitude where it is visible.

        `counterfactual.html` is mounted twice — as the #explanation panel from
        the players table's "?" links, and inline under the live bid advice. A
        close button resolving by id, or by any document-wide query, removes
        whichever comes first in document order rather than the one clicked.

        **Only one of the two directions can detect that, and the first draft of
        this test picked the wrong one.** `all_panels.html` puts `.area-auction`
        before `.area-players`, so the BID PANEL's card is first: clicking its
        own close button hits the same element whether the code says `closest()`
        or `document.querySelector()`, and a mutation to the latter sailed
        through green. So this closes the **#explanation** card — the one that is
        not first — which is where the two implementations disagree.
        """
        _open(page, live_server)
        bid_player, other = pool_top(2)
        _start_bid(page, bid_player)
        page.wait_for_selector("#bid-panel .counterfactual-card")

        page.click(f'#bid-limits a[hx-get^="/explain/"]:right-of(:text-is("{other}"))')
        page.wait_for_selector("#explanation .counterfactual-card")
        assert page.locator(".counterfactual-card").count() == 2, (
            "the test needs both mounts in one document or it proves nothing"
        )
        assert page.locator(".counterfactual-card").first.evaluate(
            "el => !!el.closest('#bid-panel')"
        ), (
            "the bid panel's card is no longer first in document order, so "
            "closing #explanation's stops discriminating — swap the direction"
        )

        page.click("#explanation .counterfactual-card button")
        page.wait_for_selector("#explanation .counterfactual-card", state="detached")
        assert page.locator("#bid-panel .counterfactual-card").count() == 1, (
            "closing the panel's counterfactual removed the bid panel's instead "
            "— the close button is resolving document-wide, not with closest()"
        )
        # The MOUNT has to outlive its contents. `getElementById('explanation')
        # .remove()` passes every assertion above — it takes the card with it —
        # while destroying the target every future "?" link swaps into, so the
        # panel could never come back. Same rule as buyout_scan.html's
        # unconditional wrapper.
        assert page.locator("#explanation").count() == 1, (
            "the close button removed the #explanation mount, not just the card "
            "— the panel is the target of every '?' link and cannot come back"
        )
        assert page.locator("#bid-counterfactual").count() == 1, (
            "the inline mount is gone, so the bid panel's lazy load has nowhere "
            "to land on the next whole-panel swap"
        )

    def test_bidding_a_pick_dismisses_that_recommendation_only(self, page, live_server):
        """And the /bid-check it fired must still land.

        The reason this is removed on afterRequest rather than on click: htmx
        aborts an in-flight request whose triggering element leaves the DOM, so
        the naive version would cancel the very bid check the button exists to
        start. The bid-panel assertion at the end is what catches that — without
        it this passes against a build that dismisses the card and does nothing.
        """
        _open(page, live_server)
        page.keyboard.press("n")
        page.wait_for_selector(".nomination-pick")
        picks = page.locator(".nomination-pick")
        assert picks.count() == 2, (
            f"expected an RFA and a UFA recommendation, got {picks.count()} — "
            f"with one block there is no 'only' to prove"
        )
        rfa = page.locator(".nomination-pick", has_text="RFA Pick")
        player = rfa.locator(".font-bold").first.inner_text().strip()

        with page.expect_response(re.compile(r"/bid-check")):
            rfa.locator("button[type='submit']").click()

        page.wait_for_selector(".nomination-pick:has-text('RFA Pick')", state="detached")
        assert page.locator(".nomination-pick", has_text="UFA Pick").count() == 1, (
            "bidding the RFA half dismissed the UFA recommendation too — an RFA "
            "sale KEEPS the nomination turn, so that is the next thing needed"
        )
        panel = page.locator("#bid-panel").inner_text()
        assert player in panel, (
            f"the bid panel is not bidding on {player} — removing the card "
            f"aborted its own /bid-check"
        )

    def test_a_failed_bid_check_leaves_the_recommendation_on_screen(
        self, page, live_server
    ):
        """`/nominate` is the only way back, so a failure must not discard it.

        Pins the `event.detail.successful` guard, which is a documented rule in
        CLAUDE.md and was otherwise reasoned from the htmx contract rather than
        measured. Without the guard the operator loses the recommendation AND
        gets no bid — the worst of both, at the tempo where it matters most.
        """
        _open(page, live_server)
        page.keyboard.press("n")
        page.wait_for_selector(".nomination-pick")
        rfa = page.locator(".nomination-pick", has_text="RFA Pick")
        assert rfa.count() == 1

        page.route("**/bid-check", lambda route: route.fulfill(
            status=500, content_type="text/html", body="boom"))
        with page.expect_response(re.compile(r"/bid-check")):
            rfa.locator("button[type='submit']").click()
        page.wait_for_timeout(300)  # let any afterRequest handler run

        assert page.locator(".nomination-pick", has_text="RFA Pick").count() == 1, (
            "a 500 from /bid-check discarded the recommendation — nothing was "
            "bid and the only way back is re-running /nominate"
        )

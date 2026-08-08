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
        bid_player, other = _pool_top(2)
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
        player = _pool_top(1)[0]
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

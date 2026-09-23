"""Counterfactual cache: correctness first, speed second.

`generate_counterfactual` costs 2 MILP solves (~200ms) and is pure in
(roster, budget, pool, market prices) at the market price, so it is cached per
state epoch and cleared by `_recompute()` — the same terms as the marginal
value, for the same reasons. See `tests/test_bid_cache.py`.

A stale counterfactual is worse than a stale marginal. The marginal is one
number; the counterfactual names **specific alternative players**, so a missed
invalidation tells you to draft someone who has already been sold.
"""

import re
import tempfile
import time

import pytest
from fastapi.testclient import TestClient

import main
import optimizer
from config import MAX_SALARY, MIN_SALARY, MY_TEAM
from optimizer import generate_counterfactual


@pytest.fixture
def client():
    """Function-scoped: these tests mutate rosters and the cache deliberately."""
    main.STATE_DIR = tempfile.mkdtemp()
    with TestClient(main.app) as c:
        c.post("/reset")
        yield c
        c.post("/reset")


def _fresh(player):
    """Counterfactual computed from scratch against the CURRENT state.

    Price comes from `main._cf_price`, deliberately not re-derived here. It
    quantizes to the $0.1M increment, and a hand-rolled copy of the lookup
    would compare the cache against a solve at a *different* price — the first
    draft of this file did exactly that and reported a phantom cache bug.
    """
    return generate_counterfactual(
        player,
        main._cf_price(player.name),
        main.auction_state.teams[MY_TEAM],
        main.auction_state.available_players,
        main.market_prices,
    )


def _at_market(player):
    """`main._counterfactual` at the price the card loads itself at.

    `_counterfactual` takes the price explicitly since the Recompute button
    exists — the same player at two prices is two different answers, and a
    caller that did not say which one it wanted is exactly the bug the key
    change removed. The tests below that are about the CACHE rather than about
    pricing say "market" once, here, and `_cf_price` is used for the reason
    `_fresh` gives: a hand-rolled copy of the lookup compares against a solve
    at a different price.
    """
    return main._counterfactual(player, main._cf_price(player.name))


def _same(a, b) -> bool:
    """Compare on what the panel actually renders.

    CounterfactualResult holds MILPSolution objects, which don't compare by
    value — and the roster lists hold Player objects that do compare by
    identity. Comparing the rendered facts (deltas plus the named
    alternatives) is both sufficient and closer to the failure being guarded:
    the panel showing a wrong number or a sold player.
    """
    return (
        a.points_difference == b.points_difference
        and round(a.budget_difference, 6) == round(b.budget_difference, 6)
        and [p.name for p in a.alternative_players] == [p.name for p in b.alternative_players]
        and a.with_player.total_points == b.with_player.total_points
        and a.without_player.total_points == b.without_player.total_points
    )


@pytest.fixture
def count_solves(monkeypatch):
    """Count solves reached through the `optimizer` module attribute.

    `generate_counterfactual` resolves it at call time, so its two solves are
    visible here. `_recompute()`'s own solve is NOT: main.py did
    `from optimizer import solve_optimal_roster`, binding the name directly.
    Same instrument and same caveat as `test_bid_cache.count_marginal_solves` —
    counting rather than timing, because wall-clock assertions go flaky under
    load and the solve count is the actual cause.

    Module-level rather than a method of one class: the cache is keyed by
    (player, price) and both halves of that key have a class asserting it
    saves a solve.
    """
    calls = {"n": 0}
    real = optimizer.solve_optimal_roster

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(optimizer, "solve_optimal_roster", counting)
    return calls


def _a_player(skip: set[str] | None = None):
    skip = skip or set()
    return max(
        (p for p in main.auction_state.available_players.values() if p.name not in skip),
        key=lambda p: p.projected_points,
    )


def _quoted_price(html: str) -> float:
    """The dollar figure the verdict sentence leads with.

    The verdict is the only place the card states what it was conditioned on,
    which is exactly why it is the thing to assert against: a card solved at
    one price and quoting another is the failure both the quantization and the
    Recompute button have to avoid.
    """
    m = re.search(r"(?:Skip him|Worth having|Toss-up) at \$([\d.]+)M", html)
    assert m, html[:300]
    return float(m.group(1))


def _explanation_tag(html: str) -> str:
    """The opening `<section id="explanation">` tag alone.

    Asserted on the TAG rather than on the whole body, because the word
    "hidden" appears in unrelated markup and `"hidden" in r.text` would pass
    against a section that carries no such attribute.
    """
    m = re.search(r'<section[^>]*id="explanation"[^>]*>', html)
    assert m, "no #explanation section in the response"
    return m.group(0)


class TestCacheStaysTrue:
    """A cached counterfactual must never differ from a freshly computed one."""

    def test_cached_matches_fresh_after_every_mutation(self, client):
        """The real guard: a missed invalidation diverges here.

        Same ten-mutation walk as the marginal cache, and for the same reason —
        later mutations act on the state the earlier ones produced, and the
        cache is warmed before each so a stale entry has something to be stale
        *from*. Player names come from the live pool rather than being
        hardcoded (a hardcoded star turned out to be a keeper, so /assign
        no-opped with a toast and never called _recompute()).
        """
        mine = _a_player()
        rival = _a_player(skip={mine.name})
        subject = _a_player(skip={mine.name, rival.name})

        mutations = [
            ("assign to BOT", lambda c: c.post("/assign", data={
                "player": mine.name, "team": MY_TEAM, "salary": "5.0"})),
            ("assign to a rival", lambda c: c.post("/assign", data={
                "player": rival.name, "team": "SRL", "salary": "7.0"})),
            ("toggle-bench", lambda c: c.post("/toggle-bench", data={
                "team_code": MY_TEAM, "player_name": mine.name})),
            ("adjust-salary", lambda c: c.post("/adjust-salary", data={
                "team_code": MY_TEAM, "player_name": mine.name, "new_salary": "9.0"})),
            ("move-to-minors", lambda c: c.post("/move-to-minors", data={
                "team_code": MY_TEAM, "player_name": mine.name})),
            ("move-to-roster", lambda c: c.post("/move-to-roster", data={
                "team_code": MY_TEAM, "player_name": mine.name})),
            ("trade-between", lambda c: c.post("/trade-between", data={
                "team_a": MY_TEAM, "team_b": "SRL",
                "players_from_a": mine.name, "players_from_b": rival.name})),
            ("buyout", lambda c: c.post("/buyout", data={"player": rival.name})),
            ("team-done", lambda c: c.post("/team-done", data={"team_code": "SRL"})),
            ("undo", lambda c: c.post("/undo")),
        ]

        for label, mutate in mutations:
            _at_market(subject)                    # warm
            assert main._counterfactual_cache, "cache should be warm before mutating"

            r = mutate(client)
            assert r.status_code == 200, f"{label} failed: {r.status_code}"

            assert not main._counterfactual_cache, (
                f"{label} did not clear the cache — the next bid would show "
                f"alternatives computed against the pre-{label} pool, which can "
                f"name a player who has since been sold. "
                f"(toast: {r.headers.get('HX-Trigger')})"
            )
            assert _same(_at_market(subject), _fresh(subject)), (
                f"after {label}: cached counterfactual != fresh"
            )

    def test_a_sold_player_never_survives_as_an_alternative(self, client):
        """The concrete form of the failure above, asserted directly."""
        subject = _a_player()
        before = _at_market(subject)
        assert before.alternative_players, "need a suggestion to invalidate"
        sold = before.alternative_players[0].name

        assert client.post("/assign", data={
            "player": sold, "team": "SRL", "salary": "4.0"}).status_code == 200

        after = _at_market(subject)
        assert sold not in [p.name for p in after.alternative_players], (
            f"{sold} was drafted by SRL and is still being recommended"
        )

    def test_cache_is_per_player(self, client):
        first = _a_player()
        second = _a_player(skip={first.name})
        _at_market(first)
        _at_market(second)
        assert set(main._counterfactual_cache) == {
            (first.name, main._cf_price(first.name)),
            (second.name, main._cf_price(second.name)),
        }


class TestSolvedAtALegalPrice:
    """The counterfactual must be run at a price the auction can actually reach.

    Market prices come off a log-normal, so essentially none of them land on the
    $0.1M increment — 704 of 704 at reset. Forcing a player in at
    $9.5476934838794 plans the roster around a price no bid can produce, while
    the panel rounds it to "$9.5M" in the verdict sentence. Same class as the
    typed-salary quantization `_legal_salary` exists to fix; it survived here
    because the counterfactual used to sit behind a DROP-only link, and this is
    now on screen for every player under the hammer.
    """

    def test_every_market_price_quantizes(self, client):
        illegal = [
            name for name in main.market_prices
            if round(main._cf_price(name), 1) != main._cf_price(name)
        ]
        assert not illegal, f"{len(illegal)} counterfactual prices off the increment"

    def test_the_raw_market_price_really_is_illegal(self, client):
        """Guards the test above from passing vacuously.

        If market prices ever became pre-quantized upstream, the assertion
        would hold with `_cf_price` doing nothing — and someone could then
        delete the round() without a failure.
        """
        raw = [p for p in main.market_prices.values() if round(p, 1) != p]
        assert raw, (
            "market prices are already on the increment, so _cf_price's "
            "quantization is untested — re-check whether it is still needed"
        )

    def test_the_panel_quotes_the_price_it_solved_at(self, client):
        name = _a_player().name
        body = client.get(f"/explain/{name}?inline=1").text
        assert _quoted_price(body) == main._cf_price(name)


class TestCacheActuallySaves:
    """The point of the exercise: no MILP solves on a repeat load."""

    def test_first_load_solves_twice_then_never_again(self, client, count_solves):
        name = _a_player().name
        assert client.get(f"/explain/{name}?inline=1").status_code == 200
        assert count_solves["n"] == 2, (
            f"expected the with/without pair, got {count_solves['n']}"
        )

        count_solves["n"] = 0
        for _ in range(4):
            client.get(f"/explain/{name}?inline=1")
        assert count_solves["n"] == 0, (
            f"a repeat load re-solved {count_solves['n']} times; the "
            f"counterfactual does not change within a state epoch"
        )

    def test_bid_check_does_not_compute_it(self, client, count_solves):
        """The load-bearing negative.

        Folding this into /bid-check "to save a round trip" would take that
        endpoint from ~9ms to ~210ms warm, and 9ms is what keeps a response
        from landing between mousedown and mouseup on Assign (the blur race
        fixed 2026-08-06). The lazy mount exists to prevent exactly that, and
        nothing else in the suite would notice if someone undid it.
        """
        name = _a_player().name
        bid = {"player": name, "price": "3.0", "bidders": "BOT,SRL"}
        client.post("/bid-check", data=bid)          # warms the marginal

        count_solves["n"] = 0
        for price in ("3.1", "3.2", "3.3"):
            client.post("/bid-check", data={**bid, "price": price})
        assert count_solves["n"] == 0, (
            f"/bid-check performed {count_solves['n']} solves; the "
            f"counterfactual must stay on its own lazy request"
        )
        assert not main._counterfactual_cache, (
            "/bid-check populated the counterfactual cache — it should not "
            "compute one at all"
        )


class TestResponsesCannotOvertakeEachOther:
    """Why the stale-counterfactual race is unreachable — and a tripwire.

    The bid panel's mount targets a static `#bid-counterfactual`, and htmx
    1.9.10 does not abort an in-flight XHR when the issuing element is removed
    (verified in the vendored bundle: abort happens only through the
    `htmx:abort` listener `hx-sync` fires). So *if* two `/explain` responses
    could arrive out of order, a late one for player A would land inside the
    panel now showing player B.

    They cannot. `/explain` is `async def` but `_counterfactual` is a blocking
    MILP solve, so it holds the single event loop for its whole ~200ms and
    requests are serialised FIFO. Measured: firing a WARM request 20ms after a
    COLD one still finished it last (202.5ms vs 198.8ms).

    That is a property of how the endpoint is written, not a guarantee of the
    design — and the obvious optimisation for the 200ms cold path is to move
    the solve off the loop (`run_in_threadpool`, or simply dropping `async`),
    which would make responses concurrent and reintroduce the hazard silently.
    This test fails the moment that happens. The fix at that point is
    `hx-sync="#app:replace"` on the mount in `bid_panel.html` — htmx stores
    sync state on the resolved sync element, so `#app` survives panel swaps
    where `this` cannot.
    """

    def test_a_warm_request_cannot_overtake_a_cold_one(self, client, live_server):
        import threading
        import urllib.parse

        import httpx

        httpx.post(f"{live_server}/reset", timeout=60)
        ranked = sorted(
            main.auction_state.available_players.values(),
            key=lambda p: -p.projected_points,
        )
        cold, warm = ranked[0].name, ranked[1].name

        # Warm one and leave the other cold, so the second request is the one
        # with far less work to do — the only shape that could overtake.
        httpx.get(
            f"{live_server}/explain/{urllib.parse.quote(warm)}?inline=1", timeout=60
        )
        main._counterfactual_cache.pop(cold, None)

        sent: dict[str, float] = {}
        finished: dict[str, float] = {}
        origin = time.perf_counter()

        def fire(tag: str, name: str, delay: float) -> None:
            time.sleep(delay)
            sent[tag] = time.perf_counter() - origin
            httpx.get(
                f"{live_server}/explain/{urllib.parse.quote(name)}?inline=1",
                timeout=60,
            )
            finished[tag] = time.perf_counter() - origin

        threads = [
            threading.Thread(target=fire, args=("cold", cold, 0.0)),
            threading.Thread(target=fire, args=("warm", warm, 0.02)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Precondition, not the finding. If thread scheduling ever sends the
        # warm request first, the ordering assertion below is meaningless and
        # would fail with a message sending someone after a race that did not
        # happen. Measured 0 inversions in 5 trials under 20-core load, so this
        # is insurance rather than an observed problem.
        assert sent["cold"] < sent["warm"], (
            f"the cold request was not sent first (cold {sent['cold'] * 1000:.1f}ms, "
            f"warm {sent['warm'] * 1000:.1f}ms) — scheduling noise, not a finding; "
            f"re-run"
        )
        assert finished["warm"] > finished["cold"], (
            f"a /explain request that started 20ms later finished FIRST "
            f"(warm {finished['warm'] * 1000:.0f}ms vs cold "
            f"{finished['cold'] * 1000:.0f}ms), so responses can now arrive out "
            f"of order. The counterfactual mount targets a static id and htmx "
            f"will not abort the superseded request, so a stale analysis can "
            f"land under the wrong player. Add hx-sync=\"#app:replace\" to the "
            f"mount in templates/partials/bid_panel.html."
        )


class TestBothMountsRender:
    """One analysis, two mount points: the panel and the bid-panel body."""

    def test_inline_is_the_body_only(self, client):
        name = _a_player().name
        r = client.get(f"/explain/{name}?inline=1")
        assert r.status_code == 200
        assert 'id="explanation"' not in r.text, (
            "the inline fragment must not carry the panel's id — it mounts "
            "inside #bid-counterfactual while the panel is also on the page, "
            "and two elements with one id is what the split exists to avoid"
        )
        assert name in r.text

    def test_default_is_still_the_whole_panel(self, client):
        """The "?" links in the players table swap #explanation by outerHTML."""
        name = _a_player().name
        r = client.get(f"/explain/{name}")
        assert 'id="explanation"' in r.text
        assert name in r.text

    def test_unknown_player_inline_renders_nothing(self, client):
        """Reachable if a player leaves the pool between render and load.

        Empty, not the panel's "click ? on a player" prompt — there is no "?"
        in the bid panel, so that wording would be actively wrong there.
        """
        r = client.get("/explain/Nobody?inline=1")
        assert r.status_code == 200
        assert re.sub(r"\s+", "", r.text) == ""

    def test_unknown_player_standalone_hides_the_mount_but_keeps_it(self, client):
        """The panel's empty state is `hidden`, not a prompt.

        Keeping the SECTION is the load-bearing half: every "?" link is an
        outerHTML swap into `#explanation`, so a response that dropped it would
        leave the page with no target and the panel could never come back.

        It used to carry a "Click ?" prompt under a "Counterfactual" heading,
        which during an auction sat beside the bid panel's own inline card
        saying nothing.
        """
        r = client.get("/explain/Nobody")
        assert r.status_code == 200
        assert "counterfactual-card" not in r.text
        assert " hidden" in _explanation_tag(r.text)

    def test_a_populated_panel_is_not_hidden(self, client):
        """The other half. A template that hid the section unconditionally
        satisfies the test above while making the panel permanently invisible.
        """
        r = client.get(f"/explain/{_a_player().name}")
        assert "counterfactual-card" in r.text
        assert " hidden" not in _explanation_tag(r.text)


class TestRecomputingAtTheLiveBid:
    """The card loads at the market price and sharpens only on request.

    `_cf_price` is a forecast of the clearing price, so the auto-loaded card
    answers "what does letting him go cost me if he goes for about what he
    should". Mid-auction the price on the table is a fact and the forecast is
    not, which is the whole reason `?price=` exists — and why it is a button
    rather than a trigger: two MILP solves is ~200ms, and firing that per
    $0.1M increment puts a response back inside the window where it lands
    between mousedown and mouseup on Assign.
    """

    def test_the_card_re_solves_at_the_price_it_was_given(self, client):
        name = _a_player().name
        market = client.get(f"/explain/{name}?inline=1").text
        assert _quoted_price(market) == main._cf_price(name)

        asked = main._legal_salary(main._cf_price(name) - 2.0)
        assert asked != main._cf_price(name), "pick a price that differs"
        sharpened = client.get(f"/explain/{name}?inline=1&price={asked}").text
        assert _quoted_price(sharpened) == asked

    def test_a_higher_price_can_only_be_worse(self, client, monkeypatch):
        """The property that proves the price reaches the SOLVER.

        A card that quoted the asked price while solving at the market one
        passes the test above — the figure is rendered from `cf_price`, not
        read back out of the solution. Points gained over the best roster
        without him is non-increasing in what he costs, so cheap-vs-dear is a
        real answer changing rather than a string.

        Driven through `/explain`, and the solve read off a spy. It called
        `main._counterfactual` directly until 2026-09-22, so the two layers
        between the query string and the solver — the endpoint's parse and
        `_counterfactual_context` — could drop the price without failing it.
        """
        name = _a_player().name
        solved = []
        real = main._counterfactual

        def spy(player, price):
            result = real(player, price)
            solved.append(result.points_difference)
            return result

        monkeypatch.setattr(main, "_counterfactual", spy)
        for price in (MIN_SALARY, MAX_SALARY):
            assert client.get(f"/explain/{name}?inline=1&price={price}").status_code == 200
        cheap, dear = solved
        assert cheap > dear, (
            f"{name} is worth the same at ${MIN_SALARY}M and ${MAX_SALARY}M "
            f"({cheap} vs {dear}) — the price is not reaching the solve"
        )

    def test_the_asked_price_is_clamped_and_quantized(self, client):
        """Through `_legal_salary`, for the reason `_cf_price` rounds.

        The bid box auto-submits whatever was typed, so the button can be
        handed a fat-fingered 46 or a 2.5476. A verdict reading "Skip him at
        $46.0M" would be conditioned on a price the CBA has no room for, and
        one reading $2.5476M on a price no bid can match.
        """
        name = _a_player().name
        for asked, expected in [
            (9.5476, 9.5),
            (46, MAX_SALARY),
            (0.01, MIN_SALARY),
        ]:
            body = client.get(f"/explain/{name}?inline=1&price={asked}").text
            assert _quoted_price(body) == expected, f"{asked} quoted wrong"

    def test_the_cache_is_keyed_by_price(self, client, count_solves):
        """Same player, two prices, two answers — and neither re-solves twice.

        A name-only key hands the second price the first price's answer while
        the card quotes the second, which is a confident wrong number on the
        bidding path.
        """
        name = _a_player().name
        client.get(f"/explain/{name}?inline=1")
        assert count_solves["n"] == 2, count_solves["n"]

        count_solves["n"] = 0
        asked = main._legal_salary(main._cf_price(name) - 2.0)
        client.get(f"/explain/{name}?inline=1&price={asked}")
        assert count_solves["n"] == 2, (
            f"a new price re-used a cached answer ({count_solves['n']} solves)"
        )

        count_solves["n"] = 0
        client.get(f"/explain/{name}?inline=1&price={asked}")
        client.get(f"/explain/{name}?inline=1")
        assert count_solves["n"] == 0, (
            f"a repeat of either price re-solved {count_solves['n']} times"
        )

    def test_only_the_bid_panels_mount_offers_the_button(self, client):
        """`#bid-price` holds the price of the player under the hammer.

        The standalone panel is opened from the players table for an arbitrary
        player, so a Recompute there would re-solve him at a price belonging to
        somebody else and quote it as his.
        """
        name = _a_player().name
        assert ">Recompute</button>" in client.get(f"/explain/{name}?inline=1").text
        assert ">Recompute</button>" not in client.get(f"/explain/{name}").text

    def test_the_button_targets_the_card_and_not_the_mount(self, client):
        """`#bid-counterfactual` is the mount and must survive the swap.

        An outerHTML swap into the mount deletes it when the response is empty
        (the player left the pool mid-bid), and every future load targets it —
        the `#bid-advice` failure, which rendered a half-built panel with no
        console error. Targeting the card leaves the mount standing.
        """
        body = client.get(f"/explain/{_a_player().name}?inline=1").text
        assert 'hx-target="closest .counterfactual-card"' in body
        assert "bid-counterfactual" not in body

    def test_the_marker_says_which_price_is_on_screen(self, client):
        """Same job as `#proj-basis`, and a recompute names its figure.

        The verdict names the dollars either way, so without the basis the two
        cards are indistinguishable at a glance. The figure is there because
        typing a new bid swaps `#bid-advice` and never this card: a bare "at
        your bid" went on describing a bid that had left the box.
        """
        name = _a_player().name
        assert "at market" in client.get(f"/explain/{name}?inline=1").text
        sharpened = client.get(f"/explain/{name}?inline=1&price=3.0").text
        assert "at $3.0M bid" in sharpened
        assert "at market" not in sharpened
        assert "at your bid" not in sharpened

    @pytest.mark.parametrize("blank", ["", "%20%20"])
    def test_an_empty_box_answers_at_market_rather_than_422(self, client, blank):
        """Recompute sends the box as it stands, and it can be empty.

        A `float` query param turned `price=` into a 422 — "Request failed
        (422)" in a toast, for pressing a button before typing a bid.
        """
        name = _a_player().name
        r = client.get(f"/explain/{name}?inline=1&price={blank}")
        assert r.status_code == 200, r.text[:200]
        assert "at market" in r.text
        assert r.text == client.get(f"/explain/{name}?inline=1").text

    def test_a_price_that_is_not_a_number_is_still_refused(self, client):
        r = client.get(f"/explain/{_a_player().name}?inline=1&price=abc")
        assert r.status_code == 422

    def test_the_marker_names_the_price_it_was_SOLVED_at(self, client):
        """The legal price, not the typed one — the card solves at the former.

        `_legal_salary` quantizes to $0.1M and clamps to the league range, so a
        marker echoing the raw query would name a price nothing was solved at.
        """
        name = _a_player().name
        assert "at $3.0M bid" in client.get(f"/explain/{name}?inline=1&price=3.04").text
        assert f"at ${MAX_SALARY:.1f}M bid" in client.get(
            f"/explain/{name}?inline=1&price=99"
        ).text

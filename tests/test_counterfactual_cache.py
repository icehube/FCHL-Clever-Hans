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

import pytest
from fastapi.testclient import TestClient

import main
import optimizer
from config import MY_TEAM
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


def _a_player(skip: set[str] | None = None):
    skip = skip or set()
    return max(
        (p for p in main.auction_state.available_players.values() if p.name not in skip),
        key=lambda p: p.projected_points,
    )


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
            main._counterfactual(subject)          # warm
            assert main._counterfactual_cache, "cache should be warm before mutating"

            r = mutate(client)
            assert r.status_code == 200, f"{label} failed: {r.status_code}"

            assert not main._counterfactual_cache, (
                f"{label} did not clear the cache — the next bid would show "
                f"alternatives computed against the pre-{label} pool, which can "
                f"name a player who has since been sold. "
                f"(toast: {r.headers.get('HX-Trigger')})"
            )
            assert _same(main._counterfactual(subject), _fresh(subject)), (
                f"after {label}: cached counterfactual != fresh"
            )

    def test_a_sold_player_never_survives_as_an_alternative(self, client):
        """The concrete form of the failure above, asserted directly."""
        subject = _a_player()
        before = main._counterfactual(subject)
        assert before.alternative_players, "need a suggestion to invalidate"
        sold = before.alternative_players[0].name

        assert client.post("/assign", data={
            "player": sold, "team": "SRL", "salary": "4.0"}).status_code == 200

        after = main._counterfactual(subject)
        assert sold not in [p.name for p in after.alternative_players], (
            f"{sold} was drafted by SRL and is still being recommended"
        )

    def test_set_nominator_need_not_invalidate(self, client):
        """The one mutating endpoint that skips _recompute, deliberately.

        It moves `nomination_index` and nothing else; a counterfactual cannot
        depend on whose turn it is. Pinned so that if /set-nominator ever grows
        a real state change, this fails and forces the question.
        """
        player = _a_player()
        before = main._counterfactual(player)
        assert client.post("/set-nominator", data={"team_code": "LGN"}).status_code == 200
        assert _same(main._counterfactual(player), before)
        assert _same(main._counterfactual(player), _fresh(player))

    def test_cache_is_per_player(self, client):
        first = _a_player()
        second = _a_player(skip={first.name})
        main._counterfactual(first)
        main._counterfactual(second)
        assert set(main._counterfactual_cache) == {first.name, second.name}


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
        quoted = re.search(r"(?:Skip him|Worth having|Toss-up) at \$([\d.]+)M", body)
        assert quoted, body[:300]
        assert float(quoted.group(1)) == main._cf_price(name)


class TestCacheActuallySaves:
    """The point of the exercise: no MILP solves on a repeat load."""

    @pytest.fixture
    def count_solves(self, monkeypatch):
        """Count solves reached through the `optimizer` module attribute.

        `generate_counterfactual` resolves it at call time, so its two solves
        are visible here. `_recompute()`'s own solve is NOT: main.py did
        `from optimizer import solve_optimal_roster`, binding the name
        directly. Same instrument and same caveat as
        `test_bid_cache.count_marginal_solves` — counting rather than timing,
        because wall-clock assertions go flaky under load and the solve count
        is the actual cause.
        """
        calls = {"n": 0}
        real = optimizer.solve_optimal_roster

        def counting(*args, **kwargs):
            calls["n"] += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(optimizer, "solve_optimal_roster", counting)
        return calls

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

    def test_unknown_player_standalone_keeps_its_prompt(self, client):
        r = client.get("/explain/Nobody")
        assert r.status_code == 200
        assert 'id="explanation"' in r.text
        assert "Click" in r.text

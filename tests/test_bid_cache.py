"""Marginal-value cache: correctness first, speed second.

`compute_marginal_value` costs ~10 MILP solves and is pure in (roster, budget,
pool, market prices). `/bid-check` varies only the price and the bidder list
between calls, neither of which it reads, so the result is cached per state
epoch and cleared by `_recompute()`.

The risk this file exists to cover is NOT slowness. It is a stale number
presented as live bid advice during a draft — the one failure mode a cache can
introduce and the hardest to notice, because a plausible-looking figure is
indistinguishable from a correct one on screen.
"""

import tempfile

import pytest
from fastapi.testclient import TestClient

import main
import optimizer
from config import MY_TEAM
from optimizer import compute_marginal_value


@pytest.fixture
def client():
    """Function-scoped: these tests mutate rosters and the cache deliberately."""
    main.STATE_DIR = tempfile.mkdtemp()
    with TestClient(main.app) as c:
        c.post("/reset")
        yield c
        c.post("/reset")


def _fresh(player) -> float:
    """Marginal value computed from scratch against the CURRENT state."""
    return compute_marginal_value(
        player,
        main.auction_state.teams[MY_TEAM],
        main.auction_state.available_players,
        main.market_prices,
    )


def _a_player(skip: set[str] | None = None):
    skip = skip or set()
    return max(
        (p for p in main.auction_state.available_players.values() if p.name not in skip),
        key=lambda p: p.projected_points,
    )


@pytest.fixture
def count_solves(monkeypatch):
    """Count MILP solves. The deterministic proxy for 'did we recompute?'.

    Asserting on wall-clock time would go flaky the moment the suite runs under
    load; the solve count is the actual thing the cache removes.
    """
    calls = {"n": 0}
    real = optimizer.solve_optimal_roster

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(optimizer, "solve_optimal_roster", counting)
    return calls


class TestCacheStaysTrue:
    """A cached marginal must never differ from a freshly computed one."""

    def test_cached_matches_fresh_after_every_mutation(self, client):
        """The real guard: a missed invalidation diverges here.

        Runs the mutations in sequence — later ones act on the state the
        earlier ones produced — warming the cache before each so a stale entry
        has something to be stale *from*. Comparing against a fresh compute
        after every step means any path that forgets to clear shows up as a
        wrong number, which is exactly how it would show up in a draft.

        Player names are taken from the live pool rather than hardcoded: the
        first draft of this test named two stars, one of whom is a keeper and
        so not biddable, and `/assign` rejected him with a 200 and a toast.
        Note that the cache-cleared assertion catches that by itself — a
        mutation that silently no-ops leaves the cache warm and fails here —
        which is why it is checked before the value comparison.
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
            main._marginal_value(subject)          # warm
            assert main._marginal_cache, "cache should be warm before mutating"

            r = mutate(client)
            assert r.status_code == 200, f"{label} failed: {r.status_code}"

            assert not main._marginal_cache, (
                f"{label} did not clear the cache — the next bid check would "
                f"show a marginal computed against the pre-{label} world. "
                f"(toast: {r.headers.get('HX-Trigger')})"
            )
            cached, fresh = main._marginal_value(subject), _fresh(subject)
            assert cached == fresh, (
                f"after {label}: cached ${cached}M != fresh ${fresh}M"
            )

    def test_set_nominator_need_not_invalidate(self, client):
        """The one mutating endpoint that skips _recompute, deliberately.

        It moves `nomination_index` and nothing else; a marginal value cannot
        depend on whose turn it is. Pinned so that if /set-nominator ever grows
        a real state change, this fails and forces the question.
        """
        player = _a_player()
        before = main._marginal_value(player)
        assert client.post("/set-nominator", data={"team_code": "LGN"}).status_code == 200
        assert main._marginal_value(player) == before == _fresh(player)

    def test_cache_is_per_player(self, client):
        """Two players in one epoch must not share an entry."""
        first = _a_player()
        second = _a_player(skip={first.name})
        v1, v2 = main._marginal_value(first), main._marginal_value(second)
        assert main._marginal_cache[first.name] == v1
        assert main._marginal_cache[second.name] == v2
        assert set(main._marginal_cache) == {first.name, second.name}


class TestCacheActuallySaves:
    """The point of the exercise: no MILP solves on a repeat interaction."""

    def _bid(self, client, name, **over):
        data = {"player": name, "price": "3.0", "bidders": "BOT,SRL,MAC"}
        data.update(over)
        return client.post("/bid-check", data=data)

    def test_price_step_performs_no_solves(self, client, count_solves):
        name = _a_player().name
        self._bid(client, name, price="3.0")
        first = count_solves["n"]
        assert first > 0, "the first check must actually compute something"

        count_solves["n"] = 0
        for price in ("3.1", "3.2", "3.3", "3.4"):
            self._bid(client, name, price=price)
        assert count_solves["n"] == 0, (
            f"stepping the price re-solved {count_solves['n']} times; the "
            f"marginal does not depend on price"
        )

    def test_bidder_toggle_performs_no_solves(self, client, count_solves):
        """Bidders move the ceiling, never the marginal."""
        name = _a_player().name
        self._bid(client, name)
        count_solves["n"] = 0
        for bidders in ("BOT,SRL", "BOT,SRL,MAC,LPT", "BOT", "BOT,GVR"):
            self._bid(client, name, bidders=bidders)
        assert count_solves["n"] == 0

    def test_a_mutation_makes_it_recompute(self, client, count_solves):
        """Guards the counter itself: zero everywhere would also 'pass' above."""
        name = _a_player(skip={"Artemi Panarin"}).name
        self._bid(client, name)
        client.post("/assign", data={
            "player": "Artemi Panarin", "team": "BOT", "salary": "5.0"})
        count_solves["n"] = 0
        self._bid(client, name)
        assert count_solves["n"] > 0, (
            "after a roster change the marginal must be recomputed, not served"
        )


class TestNoBehaviouralDrift:
    """A pure speedup: same advice, fewer solves."""

    def test_advice_is_identical_cached_and_uncached(self, client):
        """Across prices spanning the forecast boundary, where the two caps swap."""
        player = _a_player()
        team = main.auction_state.teams[MY_TEAM]
        ceiling = main.market_info.market_ceiling

        for price in (0.5, 1.0, ceiling - 0.1, ceiling, ceiling + 0.1, ceiling + 5.0):
            uncached = optimizer.compute_bid_recommendation(
                player, team, main.auction_state.available_players,
                main.market_prices, main.market_info, price,
            )
            cached = optimizer.compute_bid_recommendation(
                player, team, main.auction_state.available_players,
                main.market_prices, main.market_info, price,
                marginal_value=main._marginal_value(player),
            )
            assert cached == uncached, f"advice drifted at ${price}M"

    def test_omitting_the_kwarg_still_computes(self, client):
        """The default path every existing test and caller uses."""
        player = _a_player()
        rec = optimizer.compute_bid_recommendation(
            player, main.auction_state.teams[MY_TEAM],
            main.auction_state.available_players, main.market_prices,
            main.market_info, 1.0,
        )
        assert rec.marginal_value == _fresh(player)

"""`AuctionState.locate_players` — the one thing that answers "where is he?".

Engine only; the endpoint and its markup are covered in `test_endpoints.py`.

Every subject is derived by the ROLE it has to play, never named: `players.csv`
is replaced before every draft, and `test_no_literal_player_names.py` fails any
test file carrying a real name as a string constant.
"""

import pytest

from config import MY_TEAM
from state import SEARCH_MIN_QUERY, Player, PlayerOnRoster, TransactionRecord


@pytest.fixture
def state(client):
    """The live auction state, reset by the shared `client` fixture."""
    import main

    return main.auction_state


def a_query_for(name: str) -> str:
    """A prefix of the player's SURNAME — what an operator actually types.

    The surname rather than the whole name because that is the query the tier
    ranking exists for, and because a full name would make every test a
    prefix-tier test by accident.

    Only ALPHABETIC tokens count. `_disambiguated_names` appends " (DAL)",
    " (VAN F)" and " (#2)", so the last token of a name is not reliably a
    surname — and a query of "(DAL" folds to "dal", which matches the suffix
    rather than the man and would keep passing while testing the wrong thing.
    """
    words = [w for w in name.split() if w.isalpha()] or [name]
    return words[-1][:4]


def hit_for(result, name: str):
    """The one hit naming this player, or None."""
    return next((h for h in result.hits if h.name == name), None)


def locate(state, name: str, limit: int = 200):
    """Search for a player by his own surname and return his hit.

    `limit` is generous on purpose: a common surname can push the subject past
    the default 10, and a test that failed for THAT reason would look like the
    location bug it is meant to catch.
    """
    return hit_for(state.locate_players(a_query_for(name), limit=limit), name)


class TestItFindsEveryPlaceAPlayerCanBe:
    """One test per location. These are the reason the feature exists."""

    def test_a_player_in_the_pool(self, state):
        from tests.helpers import pool_top

        name = pool_top(1)[0]
        found = locate(state, name)
        assert found is not None, f"{name} is in the pool and was not found"
        assert found.where == "pool"
        assert found.team_code is None, "a pool player has no holder"

    def test_a_player_on_an_active_roster(self, state):
        from tests.helpers import a_roster_player

        code = next(c for c in state.teams if c != MY_TEAM)
        player = a_roster_player(code)
        found = locate(state, player.name)
        assert found is not None, f"{player.name} is on {code} and was not found"
        assert found.where == "roster"
        assert found.team_code == code, (
            f"{player.name} is on {code} but the search says {found.team_code}"
        )

    def test_a_benched_player_reads_bench_and_not_roster(self, state, client):
        """Bench is a FLAG, not a list, so it is the half that can be dropped
        silently — a benched player would still be found, just mislabelled as
        a starter, which is the opposite of what the panel shows."""
        from tests.helpers import a_roster_player

        player = a_roster_player(MY_TEAM)
        assert locate(state, player.name).where == "roster", "precondition"

        client.post("/toggle-bench", data={
            "team_code": MY_TEAM, "player_name": player.name,
        })
        assert locate(state, player.name).where == "bench"

    def test_a_player_in_the_minors(self, state):
        """The 2026-08-07 class of bug: `roster_players` excludes the minors,
        so anything reading it instead of `all_players` reports a player who is
        plainly on the panel as missing entirely."""
        code, minor = next(
            (c, t.minor_players[0])
            for c, t in state.teams.items()
            if t.minor_players
        )
        found = locate(state, minor.name)
        assert found is not None, f"{minor.name} is in {code}'s minors and was not found"
        assert found.where == "minors"
        assert found.team_code == code

    def test_a_bought_out_player_is_still_findable(self, state, client):
        """He is removed from every list and survives only as a nameless float
        in `team.penalties`. His buyout record is the sole evidence."""
        from tests.helpers import a_buyout_candidate

        victim = a_buyout_candidate()
        client.post("/buyout", data={"player": victim.name})

        assert state.teams[MY_TEAM].find_player(victim.name) is None, "precondition"
        assert victim.name not in state.available_players, "precondition"

        found = locate(state, victim.name)
        assert found is not None, (
            f"{victim.name} was bought out and is now unfindable, which is the "
            f"one location that exists nowhere but the log"
        )
        assert found.where == "bought-out"
        assert found.salary == pytest.approx(victim.salary), (
            "the buyout row must carry what he was paid, since the penalty is "
            "derived from it"
        )
        assert found.counts_on_cap is False, (
            "he is gone; it is the PENALTY that remains on the cap, and that "
            "is a different number"
        )


class TestWhatEachHitReports:
    def test_a_minor_reports_whether_he_counts_on_cap(self, state):
        """The whole reason the minors are coloured in the team panel. Both
        values must be reachable, so the test supplies the group rather than
        hoping the pool carries one of each."""
        team = state.teams[MY_TEAM]
        shared = dict(position="F", salary=1.0, projected_points=10, is_minor=True)
        team.minor_players.append(
            PlayerOnRoster(name="Cap Counting Minor", group="3", **shared)
        )
        team.minor_players.append(
            PlayerOnRoster(name="Cap Exempt Minor", group="5", **shared)
        )
        team._invalidate_cache()

        assert locate(state, "Cap Counting Minor").counts_on_cap is True
        assert locate(state, "Cap Exempt Minor").counts_on_cap is False

    def test_a_pool_player_reports_no_salary(self, state):
        """`Player.salary` is LAST season's salary — the price model's lag
        feature, 0 meaning new to the league — not a cap hit. Rendering it
        beside a roster player's actual cap hit would be wrong on every row,
        and wrong in a way that reads as plausible money.
        """
        from tests.helpers import pool_top

        name = next(
            n for n in pool_top(60) if state.available_players[n].salary > 0
        )
        assert state.available_players[name].salary > 0, "precondition"
        assert locate(state, name).salary is None

    def test_an_owned_player_reports_his_cap_hit(self, state):
        from tests.helpers import a_roster_player

        player = a_roster_player(MY_TEAM)
        assert locate(state, player.name).salary == pytest.approx(player.salary)

    def test_a_drafted_player_reports_his_buyer_and_not_the_pool(self, state, client):
        """Live state beats the log. He has a `draft` row naming his team AND
        he has left `available_players`; reading the log first, or the pool
        first, would answer with a location he is no longer in."""
        from tests.helpers import assign, pool_top

        name = pool_top(1)[0]
        code = next(c for c in state.teams if c != MY_TEAM)
        assign(client, name, code, 1.0)

        found = locate(state, name)
        assert found.where == "roster"
        assert found.team_code == code

    def test_a_hit_carries_its_latest_transaction(self, state, client):
        """Provenance is how "moved by a trade" is answerable without
        inventing a sixth location for it."""
        from tests.helpers import assign, pool_top

        name = pool_top(1)[0]
        assert locate(state, name).last_txn is None, "precondition: never traded"

        code = next(c for c in state.teams if c != MY_TEAM)
        assign(client, name, code, 1.0)

        txn = locate(state, name).last_txn
        assert txn is not None and txn.transaction_type == "draft"
        assert txn.team_code == code


class TestRankingAndLimits:
    def test_a_word_prefix_outranks_a_bare_substring(self, state):
        """A surname prefix is what the operator types; a mid-word hit is not.

        Supplied rather than derived, for the reason the test below it states
        and one more. It ran `"son"` over the live pool on the measurement that
        it matched 1 name by word prefix and 82 by substring — and the 2026-27
        pool answers that query with **76 hits, none of them a word prefix**, so
        the two tiers were never both populated and the test reported that it
        could not fail. A query that happens to split the tiers today is the same
        bet placed again; `"berg"` would have worked this year and is no safer.

        The decoy scores HIGHER, so points alone would rank it first and only the
        tier can produce the asserted order — a collapsed `in` fails here rather
        than merely losing its material.
        """
        shared = dict(position="F", nhl_team="XXX", group="1", salary=1.0)
        team = state.teams[MY_TEAM]
        team.acquired_players.append(PlayerOnRoster(
            name="Yyyy Vuqaxis", projected_points=99, **shared,   # mid-word
        ))
        team.acquired_players.append(PlayerOnRoster(
            name="Yyyy Qaxley", projected_points=20, **shared,    # word prefix
        ))
        team._invalidate_cache()

        names = [h.name for h in state.locate_players("qax", limit=50).hits]
        assert names == ["Yyyy Qaxley", "Yyyy Vuqaxis"], (
            f"a substring match ranked above a surname match: {names}"
        )

    def test_a_first_name_prefix_does_not_outrank_a_better_surname(self, state):
        """A full-name prefix looks like the stronger match and is not.

        With no space in the query it means only "his FIRST name starts with
        this", which nobody types. Ranked as its own tier above surnames it
        filled all ten slots with the wrong people — measured over the live
        pool, "hu" led with three players whose first names begin Hu- and hid
        every Hughes. Supplied rather than derived because the pool is
        replaced before every draft and the pairing has to be guaranteed.
        """
        shared = dict(position="F", nhl_team="XXX", group="1", salary=1.0)
        team = state.teams[MY_TEAM]
        # The decoy's FIRST name starts with the query, so a full-name-prefix
        # tier would put him first; he scores less, so points put him second.
        team.acquired_players.append(PlayerOnRoster(
            name="Aaaasurnameberg Zzzz", projected_points=20, **shared,
        ))
        team.acquired_players.append(PlayerOnRoster(
            name="Zzzz Aaaasurname", projected_points=90, **shared,
        ))
        team._invalidate_cache()

        names = [h.name for h in state.locate_players("aaaasur", limit=50).hits]
        assert names[:1] == ["Zzzz Aaaasurname"], (
            f"the first-name match outranked the better surname match: {names}"
        )

    def test_the_best_match_is_not_merely_the_alphabetically_first(self, state):
        """Ten of a hundred matches get shown, so which ten is the question.

        Alphabetical order is arbitrary at that ratio. Both names below are
        surname matches in the same tier, and the alphabet puts the wrong one
        first, so this fails against a `sorted(tier)` that ignores points.
        """
        shared = dict(position="F", nhl_team="XXX", group="1", salary=1.0)
        team = state.teams[MY_TEAM]
        team.acquired_players.append(PlayerOnRoster(
            name="Alpha Qwertyfirst", projected_points=10, **shared,
        ))
        team.acquired_players.append(PlayerOnRoster(
            name="Beta Qwertysecond", projected_points=99, **shared,
        ))
        team._invalidate_cache()

        names = [h.name for h in state.locate_players("qwerty", limit=50).hits]
        assert names == ["Beta Qwertysecond", "Alpha Qwertyfirst"], (
            f"ranked by name rather than by points: {names}"
        )

    def test_it_counts_the_matches_it_did_not_return(self, state):
        """Without this the panel would silently truncate, which on a common
        surname reads as "he isn't in the league"."""
        result = state.locate_players("son", limit=3)
        assert len(result.hits) == 3
        assert result.total > 3, "total is the count BEFORE the limit"
        assert result.total == state.locate_players("son", limit=500).total

    def test_a_query_below_the_minimum_finds_nothing(self, state):
        short = "a" * (SEARCH_MIN_QUERY - 1)
        result = state.locate_players(short)
        assert result.hits == () and result.total == 0, (
            f"{short!r} matched {result.total} names; one character matches "
            f"hundreds and would make the box unusable"
        )

    def test_whitespace_alone_is_not_a_query(self, state):
        assert state.locate_players("   ").total == 0

    def test_a_name_in_two_places_yields_one_hit(self, state):
        """Nothing enforces that the three roster lists are disjoint —
        `add_acquired_player` and `add_minor_player` append with no name check
        — and `find_player` resolves keeper → acquired → minors. The search
        must agree with it rather than reporting the player twice."""
        team = state.teams[MY_TEAM]
        duplicated = dict(
            name="Doubled Up Skater", position="F", group="2",
            salary=3.0, projected_points=40,
        )
        team.acquired_players.append(PlayerOnRoster(**duplicated))
        team.minor_players.append(PlayerOnRoster(is_minor=True, **duplicated))
        team._invalidate_cache()

        result = state.locate_players("Doubled", limit=50)
        matching = [h for h in result.hits if h.name == "Doubled Up Skater"]
        assert len(matching) == 1, f"one player, {len(matching)} hits"
        assert matching[0].where == "roster", (
            "resolved in a different order than TeamState.find_player, so the "
            "search and the roster edit would act on different rows"
        )


class TestMatchingRules:
    def test_it_ignores_case(self, state):
        from tests.helpers import pool_top

        name = pool_top(1)[0]
        q = a_query_for(name)
        assert hit_for(state.locate_players(q.upper(), limit=200), name)
        assert hit_for(state.locate_players(q.lower(), limit=200), name)

    def test_a_diacritic_name_is_found_typed_plainly(self, state):
        """SUPPLIED data, not the live pool. Measured 2026-09-10, the pool has
        zero non-ASCII names, so folding is a no-op on it and a test written
        against it could not fail. `players.csv` is replaced before every
        draft and NHL rosters carry diacritics routinely.
        """
        state.available_players["Tomas Accented Hertlova".replace("Tomas", "Tomáš")] = (
            Player(
                name="Tomáš Accented Hertlova", position="F", group="3",
                nhl_team="SJS", age=30, projected_points=50, is_rfa=False,
                salary=0.0, team_probability=5.0,
            )
        )
        found = hit_for(state.locate_players("tomas", limit=200), "Tomáš Accented Hertlova")
        assert found is not None, (
            "an accented name is unfindable unless you can reproduce the "
            "accent, which is the opposite of what a search box is for"
        )
        assert hit_for(state.locate_players("Tomáš", limit=200), "Tomáš Accented Hertlova")

    def test_a_punctuated_name_is_found_typed_plainly(self, state):
        """Against the LIVE pool, because this one can fail on it today.

        Measured 2026-09-10, 24 pool names carry a character that is not a
        letter, a space or a period: apostrophes, hyphens, the parentheses
        `_disambiguated_names` adds — and four names where `players.csv`
        encodes the hyphen as the digit `0`. Before the letters-only fold,
        every one of those was unfindable unless you reproduced the file's
        punctuation exactly, a data-entry bug included.

        Asserts the subjects EXIST before searching, so a future CSV with
        clean names skips loudly rather than passing hollow.
        """
        import re

        punctuated = [
            n for n in state.available_players
            if re.search(r"[^A-Za-z .]", n) and " " in n
        ]
        assert punctuated, "no pool name carries punctuation; this cannot fail"

        for name in punctuated:
            plain = re.sub(r"[^a-z]", "", name.split(" ", 1)[1].lower())
            found = hit_for(state.locate_players(plain, limit=500), name)
            assert found is not None, (
                f"{name!r} is in the pool and typing {plain!r} did not find "
                f"him; the operator cannot be asked to guess the file's "
                f"punctuation"
            )

    def test_punctuation_typed_a_different_way_still_matches(self, state):
        """SUPPLIED, because the digit-for-hyphen form is a CSV bug a refresh
        may fix, and the intent has to survive the fix. Both spellings of the
        same surname must answer to a plainly typed query and to a hyphen.

        The RANK assertion is the half that pins the offsets. Folding to
        letters alone already makes these findable; what it does not do is
        keep them out of the substring tier, because the fold concatenates
        and `"alexbarreboulet".split()` is one token, so matching a split
        list would rank a punctuated surname below anyone who merely contains
        the query. The decoy is here to make that fail — it outscores both
        subjects, so only tiering can put it last.
        """
        shared = dict(position="F", nhl_team="XXX", group="1", salary=1.0)
        team = state.teams[MY_TEAM]
        team.acquired_players.append(PlayerOnRoster(
            name="Real Hyphen-Surname", projected_points=40, **shared,
        ))
        team.acquired_players.append(PlayerOnRoster(
            name="Digit Hyphen0Surname", projected_points=41, **shared,
        ))
        team.acquired_players.append(PlayerOnRoster(
            name="Decoy Prefixhyphensurname", projected_points=99, **shared,
        ))
        team._invalidate_cache()

        subjects = {"Real Hyphen-Surname", "Digit Hyphen0Surname"}
        for query in ("hyphensurname", "hyphen-surname", "hyphen surname"):
            names = [h.name for h in state.locate_players(query, limit=50).hits]
            assert set(names[:2]) == subjects, (
                f"{query!r} ranked {names}; a surname at a word boundary must "
                f"outrank a bare substring even when the substring scores more"
            )


class TestTheLogDoesNotOutrankReality:
    def test_a_buyout_row_never_outranks_a_live_roster(self, state):
        """The ordering inside `_searchable`, pinned with SUPPLIED state.

        Nothing the app can do reaches "on a roster AND carrying a buyout
        row": the buyout removes him for good, and `/undo` restores a snapshot
        from before it, taking the row with it. So indexing the log first
        survives every reachable test — measured 2026-09-10, it survived all
        20 — while breaking the rule the docstring states. The log is
        append-only history; only the collections say where a player IS.
        """
        from tests.helpers import a_roster_player

        player = a_roster_player(MY_TEAM)
        state.transaction_log.append(
            TransactionRecord(
                player_name=player.name, position=player.position,
                team_code=MY_TEAM, salary=player.salary,
                model_price=1.0, market_price=1.0, timestamp="now",
                transaction_type="buyout",
            )
        )
        found = locate(state, player.name)
        assert found.where == "roster", (
            f"a history row made a player standing on the roster read as "
            f"{found.where!r}"
        )
        assert found.salary == pytest.approx(player.salary)

    def test_an_undone_buyout_returns_him_to_his_roster(self, state, client):
        """Undo pops the buyout row along with the buyout, so he stops being
        findable as gone and starts being findable on the roster again."""
        from tests.helpers import a_buyout_candidate

        victim = a_buyout_candidate()
        client.post("/buyout", data={"player": victim.name})
        assert locate(state, victim.name).where == "bought-out", "precondition"

        client.post("/undo")
        found = locate(state, victim.name)
        assert found.where in ("roster", "bench", "minors"), (
            f"after the undo he is back on BOT's roster but the search still "
            f"says {found.where!r}"
        )
        assert found.team_code == MY_TEAM

    def test_a_trade_between_two_teams_does_not_break_the_hit(self, state):
        """`/trade-between` logs `team_code` as f"{source}→{dest}", which is
        NOT a team code. It reaches a hit through `last_txn`, so anything
        rendering that field has to guard — see `_log_team_link.html`."""
        from tests.helpers import a_roster_player

        code = next(c for c in state.teams if c != MY_TEAM)
        player = a_roster_player(code)
        state.transaction_log.append(
            TransactionRecord(
                player_name=player.name, position=player.position,
                team_code=f"{code}→{MY_TEAM}", salary=player.salary,
                model_price=1.0, market_price=1.0, timestamp="now",
                transaction_type="trade",
            )
        )
        found = locate(state, player.name)
        assert found.team_code == code, "the LOCATION is still a real team code"
        assert found.last_txn.team_code == f"{code}→{MY_TEAM}", (
            "the provenance keeps the arrow form, so its renderer must guard"
        )

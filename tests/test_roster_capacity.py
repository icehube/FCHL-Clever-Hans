"""The 24-man active roster is a hard cap; extras belong in the minors.

CLAUDE.md has carried the rule since the first commit — "24 active … Teams can
draft beyond 24, extras go to minors with salary fully on cap" — but nothing
enforced it: `ROSTER_SIZE` appeared once, in `total_spots_remaining`, and was
only ever subtracted from.

The damage is not to bid advice (at 24 the MILP already says "no room, don't
bid", and at 25 it says the same thing for a different reason). It is to
`lineup_points`, which picks the best 12F/6D/2G off the ACTIVE roster: a 25th
active player competes for a starting slot he cannot legally hold. Measured, one
120-point forward on an otherwise-full roster added 70 phantom points — and that
number is what `evaluate_trade` accepts or declines on.
"""

import pytest
from fastapi.testclient import TestClient

from config import BENCH_SIZE, MY_TEAM, ROSTER_SIZE, SALARY_CAP
from state import PlayerOnRoster


@pytest.fixture
def client():
    """Function-scoped, unlike test_endpoints.py's module-scoped fixture.

    Every test here deliberately mutates a roster to capacity; sharing one
    reset across the file would leak a 24-man roster into whatever ran next.
    """
    from main import app

    with TestClient(app) as c:
        c.post("/reset")
        yield c
        c.post("/reset")


def _fill_active_roster(team, count: int = ROSTER_SIZE, points: int = 50) -> None:
    """Seat `count` players on the ACTIVE roster, bypassing add_acquired_player.

    Assigning keeper_players directly is deliberate: add_acquired_player is the
    code under test in most of this file, so building the fixture with it would
    make the tests agree with themselves. Same trick as
    tests/test_nomination.py::TestBiddingOpponents.
    """
    roster = []
    for i in range(count):
        # 12F/6D/2G first so position minimums are met, then bench-grade filler.
        pos = "F" if i < 12 else "D" if i < 18 else "G" if i < 20 else "F"
        roster.append(PlayerOnRoster(
            name=f"FILL{i}", position=pos, group="3",
            salary=0.5, projected_points=points,
        ))
    team.keeper_players = roster
    team.acquired_players = []
    team.minor_players = []
    team.penalties = 0.0
    team._invalidate_cache()


def _some_available(state) -> str:
    return next(iter(state.available_players))


class TestAssignAtCapacity:
    def test_assign_to_full_roster_lands_in_minors(self, client):
        import main

        team = main.auction_state.teams["SRL"]
        _fill_active_roster(team)
        name = _some_available(main.auction_state)

        r = client.post("/assign", data={"player": name, "team": "SRL", "salary": "2.0"})

        assert r.status_code == 200
        assert team.roster_count == ROSTER_SIZE, "active roster must not grow past 24"
        assert any(p.name == name for p in team.minor_players), (
            f"{name} should have been routed to the minors"
        )
        assert name not in main.auction_state.available_players, "the sale still happened"

    def test_operator_is_told_the_player_went_down(self, client):
        """Silent routing during a live draft is how you end up with a roster
        you did not intend."""
        import main

        team = main.auction_state.teams["SRL"]
        _fill_active_roster(team)
        name = _some_available(main.auction_state)

        r = client.post("/assign", data={"player": name, "team": "SRL", "salary": "2.0"})

        assert "minors" in r.headers.get("HX-Trigger", "").lower(), (
            f"toast must say where the player went, got: {r.headers.get('HX-Trigger')}"
        )

    def test_full_roster_salary_still_counts_on_cap(self, client):
        """The owner decision rests on this: every biddable player is group 3,
        so a minors salary is a full cap hit. If group handling ever changes,
        auto-routing silently starts freeing cap and this fails loudly."""
        import main

        team = main.auction_state.teams["SRL"]
        _fill_active_roster(team)
        before = team.total_salary
        name = _some_available(main.auction_state)

        client.post("/assign", data={"player": name, "team": "SRL", "salary": "2.0"})

        assert team.total_salary == pytest.approx(before + 2.0), (
            "a minors player drafted in the auction is a full cap hit"
        )

    def test_illegal_25th_cannot_inflate_lineup_points(self, client):
        """The reason this work exists.

        lineup_points takes the best 12F/6D/2G off the ACTIVE roster. Before the
        guard, a 25th active player displaced a starter he was not eligible to
        displace — measured at +70 points for one 120-point forward — and that
        number is what evaluate_trade decides on.
        """
        import main

        team = main.auction_state.teams["SRL"]
        _fill_active_roster(team, points=50)
        legal_points = team.current_roster_points

        stud = max(main.auction_state.available_players.values(),
                   key=lambda p: p.projected_points if p.position == "F" else -1)
        assert stud.projected_points > 50, "fixture needs a player who WOULD start"

        client.post("/assign", data={"player": stud.name, "team": "SRL", "salary": "2.0"})

        assert team.current_roster_points == legal_points, (
            f"{stud.name} ({stud.projected_points}pts) entered the starting lineup "
            f"from a full roster: {legal_points} -> {team.current_roster_points}"
        )

    def test_milp_stays_optimal_at_capacity(self, client):
        """spots never goes negative through an endpoint, so the Infeasible
        branch (optimizer.py) and its warning badge stop firing on a legal draft."""
        import main
        from optimizer import solve_optimal_roster

        bot = main.auction_state.teams[MY_TEAM]
        _fill_active_roster(bot)
        name = _some_available(main.auction_state)

        client.post("/assign", data={"player": name, "team": MY_TEAM, "salary": "2.0"})

        assert bot.total_spots_remaining == 0, "must be 0, never negative"
        sol = solve_optimal_roster(
            bot, main.auction_state.available_players, main.market_prices,
        )
        assert sol.status == "Optimal"


class TestRecallRefusedAtCapacity:
    """Recall is the one move that cannot auto-route — recalling INTO a full
    roster is the illegal act itself, so it has to be refused."""

    def test_recall_into_full_roster_raises(self):
        from data_loader import build_initial_state

        state = build_initial_state()
        team = state.teams["SRL"]
        _fill_active_roster(team)
        team.minor_players = [PlayerOnRoster(
            name="DOWNSTAIRS", position="F", group="3",
            salary=1.0, projected_points=60, is_minor=True, is_bench=True,
        )]
        team._invalidate_cache()

        with pytest.raises(ValueError, match="full"):
            team.recall_from_minors("DOWNSTAIRS")

        assert team.roster_count == ROSTER_SIZE
        assert [p.name for p in team.minor_players] == ["DOWNSTAIRS"], (
            "a refused recall must leave the player exactly where they were"
        )

    def test_endpoint_reports_the_capacity_reason(self, client):
        """/move-to-roster hardcoded "not in minors" for every ValueError, which
        would now be an actively wrong explanation for a real capacity refusal."""
        import main

        team = main.auction_state.teams["SRL"]
        _fill_active_roster(team)
        team.minor_players = [PlayerOnRoster(
            name="DOWNSTAIRS", position="F", group="3",
            salary=1.0, projected_points=60, is_minor=True, is_bench=True,
        )]
        team._invalidate_cache()

        r = client.post("/move-to-roster",
                        data={"team_code": "SRL", "player_name": "DOWNSTAIRS"})

        trigger = r.headers.get("HX-Trigger", "")
        assert "not in minors" not in trigger, (
            f"misleading reason — the player IS in the minors. Got: {trigger}"
        )
        assert "full" in trigger.lower(), f"should name the real reason, got: {trigger}"


class TestTradeOrdering:
    """Auto-routing makes trade ordering load-bearing: if a full team GAINS
    before it LOSES, the incoming player hits a 24-man roster and is wrongly
    sent to the minors, even though the trade nets to no change in size."""

    def test_one_for_one_with_a_full_team_stays_on_the_active_roster(self, client):
        import main

        ta = main.auction_state.teams["SRL"]
        tb = main.auction_state.teams["MAC"]
        _fill_active_roster(ta)
        _fill_active_roster(tb)
        out_a, out_b = "FILL0", "FILL1"

        r = client.post("/trade-between", data={
            "team_a": "SRL", "team_b": "MAC",
            "players_from_a": out_a, "players_from_b": out_b,
        })
        assert r.status_code == 200

        for team, incoming in ((ta, out_b), (tb, out_a)):
            assert team.roster_count == ROSTER_SIZE, f"{team.code} changed size"
            assert any(p.name == incoming for p in team.acquired_players), (
                f"{incoming} was sent to {team.code}'s minors by a 1-for-1 trade "
                f"that left the roster the same size"
            )
            assert not any(p.name == incoming for p in team.minor_players)


class TestFullRosterTrades:
    """Routing to the minors changed what a trade is worth, and nothing covered it.

    A player received onto a full roster now lands in the minors and scores
    nothing, so a pure acquisition that used to look like an upgrade is correctly
    a decline. The guarantee worth pinning is that the PREVIEW cannot promise
    points the EXECUTION won't deliver — before this change the two could
    disagree, because evaluate seated the player where execute would not.
    """

    def _full_bot(self):
        from data_loader import build_initial_state
        from market import compute_market_ceiling, compute_market_price
        from price_model import load_model_params, predict_all_prices

        state = build_initial_state()
        _fill_active_roster(state.teams[MY_TEAM])
        preds = predict_all_prices(state.available_players, load_model_params())
        model = {n: p.expected_price for n, p in preds.items()}
        info = compute_market_ceiling(state.teams)
        mp = {n: compute_market_price(model[n], info) for n in model}
        return state, mp

    def _best_forward(self, state):
        return max((p for p in state.available_players.values() if p.position == "F"),
                   key=lambda p: p.projected_points)

    def test_pure_acquisition_onto_a_full_roster_is_declined(self):
        from trade import PlayerTrade, evaluate_trade

        state, mp = self._full_bot()
        stud = self._best_forward(state)
        assert stud.projected_points > 50, "must out-score every filler starter"

        result = evaluate_trade(
            state, give=[],
            receive=[PlayerTrade(name=stud.name, position=stud.position,
                                 salary=2.0, projected_points=stud.projected_points)],
            market_prices=mp,
        )

        assert result.recommendation == "decline", (
            f"{stud.name} ({stud.projected_points}pts) cannot start from the minors, "
            "so acquiring him onto a full roster buys nothing but cap"
        )

    def test_preview_points_match_what_execution_delivers(self):
        from copy import deepcopy

        from trade import PlayerTrade, evaluate_trade, execute_trade

        state, mp = self._full_bot()
        stud = self._best_forward(state)
        recv = [PlayerTrade(name=stud.name, position=stud.position,
                            salary=2.0, projected_points=stud.projected_points)]

        # The KEEP-ALL scenario, which is what execute_trade performs. Not
        # best_scenario: since 2026-09-25 the trade side tries buying out every
        # eligible contract, so its best can be a buyout of one of the fillers --
        # a move execution never makes, and a comparison that would fail for a
        # reason unrelated to the minors routing this test exists to pin.
        previewed = evaluate_trade(state, give=[], receive=recv, market_prices=mp,
                                   auto_check_buyouts=False).scenarios[0].total_points

        live = deepcopy(state)
        execute_trade(live, give=[], receive=[PlayerTrade(
            name=stud.name, position=stud.position,
            salary=2.0, projected_points=stud.projected_points)])
        bot = live.teams[MY_TEAM]

        assert bot.roster_count == ROSTER_SIZE
        assert any(p.name == stud.name for p in bot.minor_players)
        # Expected points on both sides -- the figure every verdict compares
        # since 2026-09-25. current_roster_points is the starters alone, and
        # comparing it with a preview that counts the bench always fails.
        assert bot.expected_roster_points == pytest.approx(previewed), (
            f"preview promised {previewed} points, execution delivered "
            f"{bot.expected_roster_points}"
        )

    def test_received_player_still_costs_full_cap(self):
        from copy import deepcopy

        from trade import PlayerTrade, execute_trade

        state, mp = self._full_bot()
        stud = self._best_forward(state)
        before = state.teams[MY_TEAM].total_salary

        live = deepcopy(state)
        execute_trade(live, give=[], receive=[PlayerTrade(
            name=stud.name, position=stud.position,
            salary=2.0, projected_points=stud.projected_points)])

        assert live.teams[MY_TEAM].total_salary == pytest.approx(before + 2.0), (
            "a group-3 player in the minors is a full cap hit — the trade costs "
            "real money for zero lineup points, which is why it declines"
        )


class TestSpotsDisplay:
    def test_spots_never_renders_negative(self, client):
        """A 25-man roster is no longer reachable through an endpoint, but a
        state file or fchl_teams.json written before this guard can still hold
        one. "-1" reads as a bug to the operator and there is no action it
        implies, so the display clamps."""
        import main

        team = main.auction_state.teams["SRL"]
        _fill_active_roster(team, count=ROSTER_SIZE + 1)
        assert team.total_spots_remaining == -1, "property stays signed for the MILP"

        r = client.get("/team-view/SRL")

        assert r.status_code == 200
        assert ">-1<" not in r.text.replace(" ", ""), "Spots must not render negative"


class TestBenchCapacity:
    """The bench holds BENCH_SIZE and nothing enforced that until 2026-09-10.

    `is_bench` reaches NO engine module — the MILP selects its own starters
    (`s <= x`, capped 12/6/2) and `lineup_points` reads every roster player
    regardless — so a 5th benched player changed no number anywhere. Measured:
    benching a 76-point starter left `current_roster_points` at 583, unmoved.
    That is exactly why it needs a guard rather than a calculation fix: the tool
    would record an illegal roster shape and never notice it had.
    """

    def _bot(self, client):
        import main

        return main.auction_state.teams[MY_TEAM]

    def _bench(self, client, team_code, name):
        return client.post(
            "/toggle-bench", data={"team_code": team_code, "player_name": name}
        )

    def test_the_cap_is_the_leftover_roster_spots(self):
        """Derived, not the literal 4. The bench's COMPOSITION is the solver's
        choice (config.BENCH_DEPTH_WEIGHTS) and sets no count of its own."""
        from config import STARTING_LINEUP

        assert BENCH_SIZE == ROSTER_SIZE - sum(STARTING_LINEUP.values())
        assert BENCH_SIZE == 4, "if the lineup shape changed, so did this"

    def test_bench_count_ignores_the_minors(self, client):
        """send_to_minors forces is_bench on the way down so a later recall
        lands on the bench. Counting all_players would put a team with 37
        minors permanently over the cap."""
        bot = self._bot(client)
        victim = bot.roster_players[0]
        bot.set_bench(victim.name, True)
        bot.send_to_minors(victim.name)

        assert bot.minor_players[-1].is_bench, "the travel marker is still set"
        assert bot.bench_count == 0, "a minor occupies no bench slot"

    def test_a_fifth_bench_is_refused(self, client):
        bot = self._bot(client)
        for p in bot.roster_players[:BENCH_SIZE]:
            bot.set_bench(p.name, True)
        fifth = bot.roster_players[BENCH_SIZE]

        with pytest.raises(ValueError, match="bench is full"):
            bot.set_bench(fifth.name, True)

        assert not fifth.is_bench, "a refusal must not have mutated the flag"
        assert bot.bench_count == BENCH_SIZE

    def test_activating_is_never_refused(self, client):
        """The way out of a full bench. Gating it would deadlock the workflow
        this cap makes harder: demoting a starter needs a free slot to stage
        him in."""
        bot = self._bot(client)
        for p in bot.roster_players[:BENCH_SIZE]:
            bot.set_bench(p.name, True)

        bot.set_bench(bot.roster_players[0].name, False)

        assert bot.bench_count == BENCH_SIZE - 1
        bot.set_bench(bot.roster_players[BENCH_SIZE].name, True)
        assert bot.bench_count == BENCH_SIZE

    def test_rebenching_someone_already_benched_is_not_a_fifth(self, client):
        """The cap counts bench SLOTS, not toggle calls — a no-op set_bench on
        somebody already down must not trip it."""
        bot = self._bot(client)
        for p in bot.roster_players[:BENCH_SIZE]:
            bot.set_bench(p.name, True)

        bot.set_bench(bot.roster_players[0].name, True)  # already benched

        assert bot.bench_count == BENCH_SIZE

    def test_the_endpoint_refuses_and_says_why(self, client):
        """200 with an error toast, like every other refusal here — so the
        status code proves nothing and the bench count is what to read."""
        from tests.helpers import toast_of

        bot = self._bot(client)
        names = [p.name for p in bot.roster_players[:BENCH_SIZE + 1]]
        for name in names[:BENCH_SIZE]:
            r = self._bench(client, MY_TEAM, name)
            assert toast_of(r).get("type") != "error", f"{name} should have benched"

        r = self._bench(client, MY_TEAM, names[BENCH_SIZE])

        assert r.status_code == 200
        toast = toast_of(r)
        assert toast.get("type") == "error"
        assert "bench is full" in toast.get("message", "")
        assert self._bot(client).bench_count == BENCH_SIZE

    def test_a_refused_toggle_costs_no_undo_depth(self, client):
        """save_snapshot() captures AND commits, so snapshotting before the
        outcome is known evicts the oldest chain entry at MAX_SNAPSHOTS while
        the error path pops from the other end — a refusal that reads as a
        no-op while quietly destroying a real undo step."""
        import main

        bot = self._bot(client)
        for p in bot.roster_players[:BENCH_SIZE]:
            self._bench(client, MY_TEAM, p.name)
        depth = len(main.auction_state._snapshots)

        self._bench(client, MY_TEAM, bot.roster_players[BENCH_SIZE].name)

        assert len(main.auction_state._snapshots) == depth, (
            "a rejected request must not spend a snapshot"
        )

    def test_undo_still_reverts_the_last_successful_bench(self, client):
        """The other half of the same mutant: _undoable must still COMMIT on
        success. Reads the flag, not a count — pre-bench and post-bench have
        identical roster and minors counts."""
        import main

        bot = self._bot(client)
        victim = bot.roster_players[0]
        self._bench(client, MY_TEAM, victim.name)
        assert self._bot(client).find_player(victim.name).is_bench

        client.post("/undo")

        assert not self._bot(client).find_player(victim.name).is_bench

    def test_the_button_is_greyed_at_the_cap(self, client):
        """The affordance, not the rule. Only the BENCH direction — an
        Activate button must never be disabled."""
        from tests.helpers import section_of

        bot = self._bot(client)
        for p in bot.roster_players[:BENCH_SIZE]:
            self._bench(client, MY_TEAM, p.name)

        panel = section_of(client.get(f"/team-view/{MY_TEAM}").text, "team-panel")

        assert panel.count("Activate") >= BENCH_SIZE
        for row in panel.split("<tr")[1:]:
            if ">Bench<" in row:
                assert "disabled" in row, "Bench must be greyed at the cap"
            if ">Activate<" in row:
                assert "disabled" not in row, "Activate is the way out"


class TestALegacyStateOverTheCapStillWorks:
    """The cap gates TRANSITIONS, not existing state, and it has to.

    `is_bench` has been serialized since long before the cap, so a state file
    written by any earlier build can hold more than BENCH_SIZE benched — and a
    tool that refuses to render four hours into a live auction is worse than
    one showing an illegal roster. Verified by hand 2026-09-10 and pinned here
    because "state files on disk must still load" is a standing rule and
    nothing else in the suite covers this shape.
    """

    @staticmethod
    def _over_the_cap(bot) -> None:
        """Bench `BENCH_SIZE + 3` of BOT's actives, recalling depth if it is short.

        Both tests need more actives than the bench holds, and how many BOT
        starts with is a property of the POOL: the 2026-27 file seats **6** and
        stashes 21 in the minors, so a bare `roster_players[:BENCH_SIZE + 3]`
        silently benched everyone BOT had and asserted 6 == 7. Recalling clears
        `is_bench` explicitly because `send_to_minors` forces it True on the way
        down and `recall_from_minors` does not reset it — see
        `TestRecallRespectsTheBench` — so a recalled player would arrive already
        counted and the setup would never reach the over-cap state it is naming.
        """
        while len(bot.roster_players) < BENCH_SIZE + 3 and bot.minor_players:
            name = bot.minor_players[0].name
            bot.recall_from_minors(name)
            bot.find_player(name).is_bench = False
        assert len(bot.roster_players) >= BENCH_SIZE + 3, (
            f"BOT has {len(bot.roster_players)} actives and "
            f"{len(bot.minor_players)} in the minors — not enough to get over a "
            f"{BENCH_SIZE}-man bench"
        )
        for p in bot.roster_players[:BENCH_SIZE + 3]:
            p.is_bench = True                      # bypass set_bench, as a load does

    def test_it_renders_and_can_be_recovered(self, client):
        import main

        bot = main.auction_state.teams[MY_TEAM]
        self._over_the_cap(bot)
        assert bot.bench_count == BENCH_SIZE + 3

        r = client.get(f"/team-view/{MY_TEAM}")
        assert r.status_code == 200

        # Activating is the way back down, and must not be gated by a state
        # that is already over.
        client.post("/toggle-bench", data={
            "team_code": MY_TEAM, "player_name": bot.roster_players[0].name,
        })
        assert main.auction_state.teams[MY_TEAM].bench_count == BENCH_SIZE + 2

    def test_the_flag_survives_a_json_round_trip(self, client):
        """Otherwise the recovery above is undone by the next save."""
        import main
        from state import AuctionState

        bot = main.auction_state.teams[MY_TEAM]
        self._over_the_cap(bot)

        back = AuctionState.from_json(main.auction_state.to_json())

        assert back.teams[MY_TEAM].bench_count == BENCH_SIZE + 3


class TestRecallRespectsTheBench:
    """The second bench-adding path, and the one a cap on /toggle-bench alone
    leaves wide open: recall_from_minors never resets is_bench, and
    send_to_minors forces it True on the way down."""

    def test_a_demoted_player_comes_back_benched(self, client):
        import main

        bot = main.auction_state.teams[MY_TEAM]
        victim = bot.roster_players[0]
        bot.set_bench(victim.name, True)
        bot.send_to_minors(victim.name)
        bot.set_bench(bot.roster_players[0].name, False)  # clear the bench

        bot.recall_from_minors(victim.name)

        assert bot.find_player(victim.name).is_bench, (
            "deliberate — a recall must not displace a starter"
        )
        assert bot.bench_count == 1

    def test_recall_is_refused_when_the_bench_is_full(self, client):
        import main

        bot = main.auction_state.teams[MY_TEAM]
        victim = bot.roster_players[0]
        bot.set_bench(victim.name, True)
        bot.send_to_minors(victim.name)
        for p in bot.roster_players[:BENCH_SIZE]:
            bot.set_bench(p.name, True)

        with pytest.raises(ValueError, match="bench is full"):
            bot.recall_from_minors(victim.name)

        assert any(p.name == victim.name for p in bot.minor_players), (
            "validated before mutating — a refused recall leaves him down"
        )

    def test_a_csv_minor_recalls_without_a_bench_slot(self, client):
        """Minors loaded from the CSV carry is_bench=False (measured: 0 of 149
        at reset), so recalling one lands him ACTIVE and the cap does not apply.
        Guarding recall unconditionally would refuse a legal move."""
        import main

        bot = main.auction_state.teams[MY_TEAM]
        fresh = next(p for p in bot.minor_players if not p.is_bench)
        for p in bot.roster_players[:BENCH_SIZE]:
            bot.set_bench(p.name, True)

        bot.recall_from_minors(fresh.name)

        assert not bot.find_player(fresh.name).is_bench
        assert bot.bench_count == BENCH_SIZE

    def test_the_endpoint_reports_the_bench_reason(self, client):
        """/move-to-roster already surfaces str(e) — the message has to read
        well there, which is why it names the team and the cap."""
        from tests.helpers import toast_of

        import main

        bot = main.auction_state.teams[MY_TEAM]
        victim = bot.roster_players[0]
        bot.set_bench(victim.name, True)
        bot.send_to_minors(victim.name)
        for p in bot.roster_players[:BENCH_SIZE]:
            bot.set_bench(p.name, True)

        r = client.post(
            "/move-to-roster",
            data={"team_code": MY_TEAM, "player_name": victim.name},
        )

        assert r.status_code == 200
        assert "bench is full" in toast_of(r).get("message", "")

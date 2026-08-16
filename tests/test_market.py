"""Tests for market.py: market ceilings and adjusted prices."""

import pytest

from config import MAX_SALARY, MIN_SALARY, MY_TEAM, ROSTER_SIZE, SALARY_CAP
from market import (
    MarketInfo,
    compute_all_market_prices,
    compute_live_ceiling,
    compute_market_ceiling,
    compute_market_price,
    bid_winner,
    compute_opponent_ceiling,
    live_opponents,
)
from state import PlayerOnRoster, TeamState


def _make_team(
    code: str,
    keeper_salary: float = 0.0,
    num_keepers: int = 0,
    penalties: float = 0.0,
    is_done: bool = False,
    keeper_positions: dict[str, int] | None = None,
) -> TeamState:
    """Helper to create a team with specified budget characteristics."""
    keepers = []
    if keeper_positions:
        for pos, count in keeper_positions.items():
            for i in range(count):
                keepers.append(PlayerOnRoster(
                    name=f"{code}_{pos}{i}",
                    position=pos,
                    group="3",
                    salary=keeper_salary / max(num_keepers, 1) if num_keepers else 0,
                    projected_points=50,
                ))
    else:
        per_salary = keeper_salary / max(num_keepers, 1) if num_keepers else 0
        for i in range(num_keepers):
            keepers.append(PlayerOnRoster(
                name=f"{code}_P{i}",
                position="F",
                group="3",
                salary=per_salary,
                projected_points=50,
            ))
    return TeamState(
        code=code,
        name=f"Team {code}",
        keeper_players=keepers,
        penalties=penalties,
        is_done=is_done,
    )


class TestComputeOpponentCeiling:
    def test_basic_ceiling(self):
        """Active team with budget should return physical_max."""
        team = _make_team("OPP", keeper_salary=30.0, num_keepers=12,
                          keeper_positions={"F": 7, "D": 3, "G": 2})
        ceiling = compute_opponent_ceiling(team)
        assert ceiling is not None
        assert ceiling == team.physical_max_bid

    def test_done_team_returns_none(self):
        """Done teams are excluded."""
        team = _make_team("OPP", keeper_salary=30.0, num_keepers=12,
                          is_done=True,
                          keeper_positions={"F": 7, "D": 3, "G": 2})
        assert compute_opponent_ceiling(team) is None

    def test_team_can_bid_any_position(self):
        """Team with 14F can still bid on forwards (extras go to bench/minors)."""
        team = _make_team("OPP", keeper_salary=14.0, num_keepers=14,
                          keeper_positions={"F": 14})
        ceiling = compute_opponent_ceiling(team)
        assert ceiling is not None  # Can still bid

    def test_ceiling_capped_at_max_salary(self):
        """Physical max should be capped at MAX_SALARY."""
        team = _make_team("OPP", keeper_salary=5.0, num_keepers=1,
                          keeper_positions={"F": 1})
        ceiling = compute_opponent_ceiling(team)
        assert ceiling is not None
        assert ceiling == MAX_SALARY

    def test_tight_budget_ceiling(self):
        """Team with tight budget has low ceiling."""
        # 22 keepers totaling $55.0, remaining=$1.8
        # spots=2, physical_max = remaining - (spots-1)*MIN = 1.8 - 0.5 = 1.3
        team = _make_team("OPP", keeper_salary=55.0, num_keepers=22,
                          keeper_positions={"F": 14, "D": 5, "G": 3})
        ceiling = compute_opponent_ceiling(team)
        assert ceiling is not None
        assert ceiling == pytest.approx(1.3)


class TestComputeMarketCeiling:
    def _make_league(self, **overrides) -> dict[str, TeamState]:
        """Create a basic league with 3 opponent teams + BOT."""
        teams = {
            MY_TEAM: _make_team(MY_TEAM, keeper_salary=28.0, num_keepers=12,
                                keeper_positions={"F": 7, "D": 3, "G": 2}),
            "OPP1": _make_team("OPP1", keeper_salary=20.0, num_keepers=10,
                               keeper_positions={"F": 5, "D": 3, "G": 2}),
            "OPP2": _make_team("OPP2", keeper_salary=30.0, num_keepers=10,
                               keeper_positions={"F": 5, "D": 3, "G": 2}),
            "OPP3": _make_team("OPP3", keeper_salary=40.0, num_keepers=10,
                               keeper_positions={"F": 5, "D": 3, "G": 2}),
        }
        teams.update(overrides)
        return teams

    def test_second_highest_is_ceiling(self):
        """Market ceiling should be the second-highest physical_max.

        The keeper salaries are deliberately much heavier than `_make_league`'s.
        With the default budgets, OPP1 and OPP2 both clamp to MAX_SALARY, so
        this test's two assertions read `11.4 == 11.4` twice and pass just as
        happily against a `compute_market_ceiling` that returns the HIGHEST —
        the one rule it is named for. Keep all three maxes below the cap and
        distinct; the assert below is what stops that from rotting again.
        """
        teams = self._make_league(
            OPP1=_make_team("OPP1", keeper_salary=39.0, num_keepers=10,
                            keeper_positions={"F": 5, "D": 3, "G": 2}),
            OPP2=_make_team("OPP2", keeper_salary=41.0, num_keepers=10,
                            keeper_positions={"F": 5, "D": 3, "G": 2}),
            OPP3=_make_team("OPP3", keeper_salary=45.0, num_keepers=10,
                            keeper_positions={"F": 5, "D": 3, "G": 2}),
        )
        maxes = {c: teams[c].physical_max_bid for c in ("OPP1", "OPP2", "OPP3")}
        assert len(set(maxes.values())) == 3 and max(maxes.values()) < MAX_SALARY, (
            f"budgets no longer separate below the cap ({maxes}); this test "
            f"cannot tell highest from second-highest until they do"
        )

        info = compute_market_ceiling(teams)
        # OPP1 has most budget, OPP2 second, OPP3 least
        assert info.market_ceiling == maxes["OPP2"]
        assert info.highest_bid == maxes["OPP1"]

    def test_excludes_my_team(self):
        """BOT should be excluded from market ceiling calculation."""
        teams = self._make_league()
        info = compute_market_ceiling(teams)
        assert info.highest_bidder != MY_TEAM
        if info.second_bidder:
            assert info.second_bidder != MY_TEAM

    def test_done_team_excluded(self):
        """Teams marked as done should be excluded."""
        teams = self._make_league()
        teams["OPP1"].is_done = True  # Richest opponent done
        info = compute_market_ceiling(teams)
        assert info.highest_bidder != "OPP1"

    def test_all_done_gives_floor(self):
        """If all opponents are done, floor_demand is True."""
        teams = self._make_league()
        for code in ["OPP1", "OPP2", "OPP3"]:
            teams[code].is_done = True
        info = compute_market_ceiling(teams)
        assert info.floor_demand is True
        assert info.market_ceiling == MIN_SALARY
        assert info.demand_count == 0

    def test_single_opponent_is_ceiling(self):
        """With only one active opponent, they set the ceiling."""
        teams = self._make_league()
        teams["OPP2"].is_done = True
        teams["OPP3"].is_done = True
        info = compute_market_ceiling(teams)
        assert info.demand_count == 1
        assert info.market_ceiling == teams["OPP1"].physical_max_bid

    def test_demand_count_accurate(self):
        """Demand count should reflect all active opponents."""
        teams = self._make_league()
        info = compute_market_ceiling(teams)
        assert info.demand_count == 3  # All 3 opponents active


class TestWhenTheCeilingLeavesTheCap:
    """Where the idle ceiling stops being MAX_SALARY, against the real league.

    `market_price = min(model_price, market_ceiling)`, so while the ceiling sits
    at MAX_SALARY the MILP is planning on raw model prices — the second half of
    that `min` does nothing. Whether that ever changes during a draft is a
    property of how fast budgets drain, and `tests/measure_ceiling.py` measures
    it end to end: at model prices it never binds (0 of 165 picks, 18% of the
    league cap unspent), and with the money actually spent it first binds at
    pick 32 and binds on 133 of 165.

    This pins the *mechanism* underneath both of those numbers, which is the
    part a future change could move silently. The ceiling is the SECOND-highest
    opponent max, so it holds at the cap until all but one opponent is priced
    out — not until the league is broke. Nobody had written that threshold down.
    """

    # A team reaches MAX_SALARY when `spendable_budget + MIN_SALARY` does, so
    # anything under this line cannot bid the league maximum. Squeezing to the
    # line itself is not enough: `spendable_budget` subtracts the reserve for
    # every remaining spot, which on a fresh roster is several million.
    PIN_LINE = MAX_SALARY - MIN_SALARY

    def test_the_cap_holds_until_all_but_one_opponent_is_priced_out(self, client):
        import main

        from tests.helpers import squeeze

        teams = main.auction_state.teams
        opponents = [c for c in teams if c != MY_TEAM]
        assert len(opponents) > 2, "the threshold below is meaningless with fewer"
        assert all(teams[c].physical_max_bid == MAX_SALARY for c in opponents), (
            "a fresh league starts with every opponent able to bid the maximum — "
            "that starting point is what makes the ceiling inert early"
        )

        at_cap = []
        for n, code in enumerate(opponents, start=1):
            squeeze(code, self.PIN_LINE)
            assert teams[code].physical_max_bid < MAX_SALARY, (
                f"{code} still reaches the cap after being squeezed; the pin "
                f"line moved and this test is measuring nothing"
            )
            if compute_market_ceiling(teams).market_ceiling == MAX_SALARY:
                at_cap.append(n)

        # Two rich opponents are enough to pin it, so it survives every squeeze
        # but the last two. Reading `highest` instead of `second` here would
        # extend this by exactly one entry.
        assert at_cap == list(range(1, len(opponents) - 1))

    def test_the_last_rich_opponent_does_not_hold_the_cap_alone(self, client):
        """The single case the walk above turns on, stated on its own.

        The walk asserts a whole sequence, so a mutant that shifts the
        transition fails it without saying which end moved. This one names the
        state: one opponent at the cap, every other priced out, and the ceiling
        is the richest of the *poor* teams — because the rich one would have to
        drop out for anyone to win, and the price stops where the second bidder
        does.
        """
        import main

        from tests.helpers import squeeze

        teams = main.auction_state.teams
        rich, *poor = [c for c in teams if c != MY_TEAM]
        for code in poor:
            squeeze(code, self.PIN_LINE)

        info = compute_market_ceiling(teams)
        assert teams[rich].physical_max_bid == MAX_SALARY
        assert info.highest_bidder == rich
        assert info.market_ceiling < MAX_SALARY
        assert info.market_ceiling == max(teams[c].physical_max_bid for c in poor)


class TestComputeMarketPrice:
    def test_model_below_ceiling(self):
        """When model price < ceiling, market price = model price."""
        info = MarketInfo(
            market_ceiling=8.0, highest_bidder="A", highest_bid=10.0,
            second_bidder="B", demand_count=3, floor_demand=False,
        )
        assert compute_market_price(5.0, info) == 5.0

    def test_model_above_ceiling(self):
        """When model price > ceiling, market price = ceiling."""
        info = MarketInfo(
            market_ceiling=3.0, highest_bidder="A", highest_bid=5.0,
            second_bidder="B", demand_count=2, floor_demand=False,
        )
        assert compute_market_price(5.0, info) == 3.0

    def test_floor_demand_gives_min(self):
        """When no demand, market price = MIN_SALARY."""
        info = MarketInfo(
            market_ceiling=MIN_SALARY, highest_bidder=None, highest_bid=0.0,
            second_bidder=None, demand_count=0, floor_demand=True,
        )
        assert compute_market_price(5.0, info) == MIN_SALARY

    def test_model_equals_ceiling(self):
        """When model price == ceiling, market price = ceiling."""
        info = MarketInfo(
            market_ceiling=5.0, highest_bidder="A", highest_bid=7.0,
            second_bidder="B", demand_count=2, floor_demand=False,
        )
        assert compute_market_price(5.0, info) == 5.0


class TestComputeLiveCeiling:
    def test_second_highest_active_bidder(self):
        """Live ceiling uses second-highest of active bidders only."""
        teams = {
            "A": _make_team("A", keeper_salary=10.0, num_keepers=5,
                            keeper_positions={"F": 5}),
            "B": _make_team("B", keeper_salary=30.0, num_keepers=5,
                            keeper_positions={"F": 5}),
            "C": _make_team("C", keeper_salary=20.0, num_keepers=5,
                            keeper_positions={"F": 5}),
        }
        ceiling = compute_live_ceiling(["A", "B", "C"], teams)
        # A has most budget, C second, B least
        assert ceiling == teams["C"].physical_max_bid

    def test_single_active_bidder(self):
        """With one bidder, their max is the ceiling."""
        teams = {
            "A": _make_team("A", keeper_salary=10.0, num_keepers=5,
                            keeper_positions={"F": 5}),
        }
        ceiling = compute_live_ceiling(["A"], teams)
        assert ceiling == teams["A"].physical_max_bid

    def test_no_active_bidders(self):
        """With no valid bidders, ceiling is MIN_SALARY."""
        ceiling = compute_live_ceiling([], {})
        assert ceiling == MIN_SALARY

    def test_done_bidder_excluded(self):
        """Done teams in active bidder list are excluded."""
        teams = {
            "A": _make_team("A", keeper_salary=10.0, num_keepers=5, is_done=True,
                            keeper_positions={"F": 5}),
            "B": _make_team("B", keeper_salary=20.0, num_keepers=5,
                            keeper_positions={"F": 5}),
        }
        ceiling = compute_live_ceiling(["A", "B"], teams)
        assert ceiling == teams["B"].physical_max_bid


class TestLiveOpponents:
    """The predicate behind both the live ceiling and uncontested detection."""

    def test_excludes_bot_done_and_unknown(self):
        teams = {
            MY_TEAM: _make_team(MY_TEAM, keeper_salary=10.0, num_keepers=5,
                                keeper_positions={"F": 5}),
            "A": _make_team("A", keeper_salary=10.0, num_keepers=5, is_done=True,
                            keeper_positions={"F": 5}),
            "B": _make_team("B", keeper_salary=20.0, num_keepers=5,
                            keeper_positions={"F": 5}),
        }
        opponents = live_opponents([MY_TEAM, "A", "B", "GHOST"], teams)
        assert opponents == ["B"]

    def test_excludes_teams_that_cannot_reach_the_floor(self):
        """A team with no spendable room isn't a live bidder."""
        teams = {
            "A": _make_team("A", keeper_salary=0.0, num_keepers=0,
                            penalties=SALARY_CAP),
        }
        assert teams["A"].physical_max_bid < MIN_SALARY
        assert live_opponents(["A"], teams) == []

    def test_full_roster_with_money_is_still_a_live_bidder(self):
        """The bug this pins: a 24-man team with cap space used to report
        physical_max_bid 0.0, so it vanished from live_opponents and from
        every ceiling — late-draft ceilings read too low and BOT under-bid.
        The CBA lets teams draft past 24 (extra goes to minors at full cap).
        """
        teams = {
            "FULL": _make_team("FULL", keeper_salary=24.0, num_keepers=ROSTER_SIZE),
        }
        assert teams["FULL"].total_spots_remaining == 0
        assert teams["FULL"].physical_max_bid >= MIN_SALARY
        assert live_opponents(["FULL"], teams) == ["FULL"]

    def test_full_roster_with_money_lifts_the_ceiling(self):
        """Asserted at the layer where the bug did harm: the ceiling itself."""
        broke = _make_team("BROKE", penalties=SALARY_CAP)
        rich_and_full = _make_team("FULL", keeper_salary=24.0, num_keepers=ROSTER_SIZE)
        teams = {"BROKE": broke, "FULL": rich_and_full}
        # Highest opponent max, since BOT is among the bidders and must outbid
        # the strongest of them to win.
        ceiling = compute_live_ceiling([MY_TEAM, "BROKE", "FULL"], teams)
        assert ceiling == rich_and_full.physical_max_bid
        assert ceiling > MIN_SALARY, "a full team's budget must reach the ceiling"

    def test_bot_alone_has_no_opponents(self):
        """The uncontested case: BOT is the only bidder left."""
        teams = {
            MY_TEAM: _make_team(MY_TEAM, keeper_salary=10.0, num_keepers=5,
                                keeper_positions={"F": 5}),
        }
        assert live_opponents([MY_TEAM], teams) == []


class TestBidWinner:
    """One definition of "last bidder standing", shared by the advisor and the UI.

    Regression: the Assign button used to gate on len(active_bidders) == 1 while
    the advisor gated on live_opponents being empty. A cap-full team toggled on
    alongside BOT satisfied the advisor but not the button, so the panel said
    "You've won -- take it" with no way to take it.
    """

    def _teams(self):
        return {
            MY_TEAM: _make_team(MY_TEAM, keeper_salary=10.0, num_keepers=5,
                                keeper_positions={"F": 5}),
            "LIVE": _make_team("LIVE", keeper_salary=10.0, num_keepers=5,
                               keeper_positions={"F": 5}),
            "LIVE2": _make_team("LIVE2", keeper_salary=12.0, num_keepers=5,
                                keeper_positions={"F": 5}),
            "DONE": _make_team("DONE", keeper_salary=10.0, num_keepers=5,
                               is_done=True, keeper_positions={"F": 5}),
            "BROKE": _make_team("BROKE", keeper_salary=0.0, num_keepers=0,
                                penalties=SALARY_CAP),
        }

    def test_bot_alone_wins(self):
        assert bid_winner([MY_TEAM], self._teams()) == MY_TEAM

    def test_bot_plus_capped_out_team_still_wins(self):
        """The reported case: the other 'bidder' cannot legally raise the price."""
        teams = self._teams()
        assert teams["BROKE"].physical_max_bid < MIN_SALARY
        assert bid_winner([MY_TEAM, "BROKE"], teams) == MY_TEAM

    def test_bot_plus_done_team_still_wins(self):
        assert bid_winner([MY_TEAM, "DONE"], self._teams()) == MY_TEAM

    def test_live_opponent_means_no_winner_yet(self):
        assert bid_winner([MY_TEAM, "LIVE"], self._teams()) is None

    def test_two_live_opponents_means_no_winner_yet(self):
        assert bid_winner(["LIVE", "LIVE2"], self._teams()) is None

    def test_sole_opponent_wins_when_bot_is_out(self):
        assert bid_winner(["LIVE"], self._teams()) == "LIVE"

    def test_no_bidders_no_winner(self):
        assert bid_winner([], self._teams()) is None


class TestComputeAllMarketPrices:
    def test_returns_all_players(self):
        """Should return a result for every player."""
        from data_loader import build_initial_state
        from price_model import load_model_params, predict_all_prices

        state = build_initial_state()
        params = load_model_params()
        model_prices = predict_all_prices(state.available_players, params)
        market_prices = compute_all_market_prices(
            state.available_players, model_prices, state.teams,
        )
        assert len(market_prices) == len(state.available_players)

    def test_market_price_never_exceeds_ceiling(self):
        """Market price should never exceed the market ceiling."""
        from data_loader import build_initial_state
        from price_model import load_model_params, predict_all_prices

        state = build_initial_state()
        params = load_model_params()
        model_prices = predict_all_prices(state.available_players, params)
        market_prices = compute_all_market_prices(
            state.available_players, model_prices, state.teams,
        )
        for name, (price, info) in market_prices.items():
            if not info.floor_demand:
                assert price <= info.market_ceiling + 0.001, \
                    f"{name}: market price {price} > ceiling {info.market_ceiling}"

    def test_market_price_at_least_min(self):
        """Market price should always be at least MIN_SALARY."""
        from data_loader import build_initial_state
        from price_model import load_model_params, predict_all_prices

        state = build_initial_state()
        params = load_model_params()
        model_prices = predict_all_prices(state.available_players, params)
        market_prices = compute_all_market_prices(
            state.available_players, model_prices, state.teams,
        )
        for name, (price, _) in market_prices.items():
            assert price >= MIN_SALARY, f"{name}: market price {price} < MIN_SALARY"

    def test_done_team_changes_ceiling(self):
        """Marking a team as done should change market ceilings."""
        # Use synthetic teams where budgets differ enough to matter
        teams = {
            MY_TEAM: _make_team(MY_TEAM, keeper_salary=28.0, num_keepers=12,
                                keeper_positions={"F": 7, "D": 3, "G": 2}),
            "RICH": _make_team("RICH", keeper_salary=10.0, num_keepers=5,
                               keeper_positions={"F": 3, "D": 1, "G": 1}),
            "MID": _make_team("MID", keeper_salary=40.0, num_keepers=15,
                              keeper_positions={"F": 8, "D": 4, "G": 3}),
            "POOR": _make_team("POOR", keeper_salary=50.0, num_keepers=20,
                               keeper_positions={"F": 12, "D": 5, "G": 3}),
        }
        info_before = compute_market_ceiling(teams)
        assert info_before.highest_bidder == "RICH"

        # Mark RICH as done
        teams["RICH"].is_done = True
        info_after = compute_market_ceiling(teams)

        # Ceiling should drop since RICH is gone
        assert info_after.market_ceiling < info_before.market_ceiling or \
               info_after.highest_bidder != "RICH"

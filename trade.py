"""Trade evaluator and buyout analyzer."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field

from config import (
    BUYOUT_ELIGIBLE_GROUPS,
    BUYOUT_PENALTY_RATE,
    DEFAULT_TEAM_PROBABILITY,
    MAX_SALARY,
    MIN_SALARY,
    MY_TEAM,
    OVERPAY_STRESS,
)
from market import compute_market_ceiling, compute_market_price
from optimizer import MILPSolution, buyout_relief, solve_optimal_roster
from price_model import load_model_params, predict_all_prices
from state import AuctionState, Player, PlayerOnRoster, TeamState


def _pool_rank(pool: dict[str, Player], position: str, projected_points: float) -> int:
    """Rank a player re-entering the pool against the remaining pool (ties=min)."""
    return 1 + sum(
        1 for q in pool.values()
        if q.position == position and q.projected_points > projected_points
    )


@dataclass
class PlayerTrade:
    """A player involved in a trade."""

    name: str
    position: str
    salary: float
    projected_points: int


@dataclass
class BuyoutEvaluation:
    """Result of evaluating a potential buyout."""

    player_name: str
    salary_freed: float
    penalty_added: float
    net_cap_freed: float
    current_points: float
    buyout_points: float
    delta_points: float
    recommendation: str  # "buyout" or "keep"
    current_roster: MILPSolution
    buyout_roster: MILPSolution


@dataclass
class TradeScenario:
    """One possible outcome of a trade (keep all, buyout one, etc.)."""

    description: str
    total_points: float
    cap_remaining: float
    roster: MILPSolution
    buyouts: list[str] = field(default_factory=list)

    @property
    def left_after_plan(self) -> float:
        """Cap still unspent once the MILP has filled every open spot.

        THE figure to break a points tie on, never `cap_remaining`. Two
        scenarios can differ in how many spots they leave open -- a trade that
        sends out two players and buys the incoming one out opens one more than
        a single buyout -- and the extra cap is then already spoken for.
        Measured 2026-09-25: Dobson + Gustavsson for Kyrou-then-buyout "freed
        $1.6M" over buying Gustavsson out alone, and the solver spent exactly
        that money replacing Dobson (Hronek, D 48pts at $1.8M). Both plans
        spent every dollar; the tie-break called it a win.
        """
        return self.cap_remaining - self.roster.total_cost

    @property
    def spots_to_fill(self) -> int:
        return len(self.roster.roster)


@dataclass
class TradeEvaluation:
    """Complete evaluation of a proposed trade."""

    trade_id: str
    give: list[PlayerTrade]
    receive: list[PlayerTrade]
    current_scenario: TradeScenario
    scenarios: list[TradeScenario]
    best_scenario: TradeScenario
    recommendation: str  # "accept", "even" or "decline"
    reasoning: str
    source_team_code: str | None = None  # The team BOT is trading with, if any
    # The no-trade side of the comparison: BOT buying one of its OWN players
    # out without trading at all. The verdict is measured against the best of
    # these (or the current roster, if none beats it), never against the bare
    # current roster -- see evaluate_trade.
    baseline_scenarios: list[TradeScenario] = field(default_factory=list)
    baseline_best: TradeScenario | None = None
    # The same two winners re-solved with every auction price marked up by
    # `stress_rate`. None when there was no legal trade outcome to stress.
    stress_rate: float = OVERPAY_STRESS
    stress_trade_points: float | None = None
    stress_baseline_points: float | None = None

    @property
    def overpay_outcome(self) -> str | None:
        """"ahead", "level" or "behind" at stressed prices; None when unknown.

        Three-way, not a bool: EVEN is the verdict most likely to tie again at
        stressed prices, and a strict `>` printed "Falls behind: 1276 vs 1276".
        """
        if self.stress_trade_points is None or self.stress_baseline_points is None:
            return None
        if self.stress_trade_points > self.stress_baseline_points:
            return "ahead"
        if self.stress_trade_points < self.stress_baseline_points:
            return "behind"
        return "level"


# One MILP to run: (label, team to solve, pool it may buy from, the names it
# may buy out -- None for a plan with no buyouts at all).
_Job = tuple[str, TeamState, dict[str, Player], "set[str] | None"]


def _buyout_menu(team: TeamState) -> set[str]:
    """Every contract on `team` the league would let it buy out.

    Handed to the solver as `buyout_candidates`, which then chooses ANY SET of
    them in one solve. Until 2026-09-25 this built one clone and one solve per
    single buyout, so two dead contracts received in one trade could never be
    bought out together -- see `solve_optimal_roster`. Materialised on the
    caller's thread: `all_players` reads `roster_players`, which lazily writes
    `_roster_cache`, and the same team is solved by two jobs at once.
    """
    return {p.name for p in team.all_players if p.can_be_bought_out}


def _solve_jobs(
    jobs: list[_Job], market_prices: dict[str, float], workers: int,
) -> list[TradeScenario]:
    """Solve every job, `workers` at a time, returning scenarios in job order.

    ONE batch for the whole evaluation rather than one per side. `map` yields
    in input order, which is what keeps the scenario list deterministic;
    concurrent CBC solves are safe because PuLP gives each one its own scratch
    files (see TestTwoSolvesAtOnceAgreeWithTwoSolvesInARow). Two jobs share a
    team (the current roster, with and without buyouts), which is read-only
    here because `_buyout_menu` has already built its roster cache.
    """
    def solve(job: _Job) -> TradeScenario:
        label, team, pool, menu = job
        sol = solve_optimal_roster(team, pool, market_prices, buyout_candidates=menu)
        cut = [team.find_player(n) for n in sol.buyouts]
        description = label
        if cut:
            penalty = sum(p.salary * BUYOUT_PENALTY_RATE for p in cut)
            description = (f"{label} buy out {', '.join(p.name for p in cut)} "
                           f"(penalty ${penalty:.1f}M)")
        return TradeScenario(
            description=description,
            total_points=sol.total_points,
            cap_remaining=team.remaining_budget + sum(buyout_relief(p.salary) for p in cut),
            roster=sol,
            buyouts=list(sol.buyouts),
        )

    if workers <= 1 or len(jobs) <= 1:
        return [solve(j) for j in jobs]
    with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as ex:
        return list(ex.map(solve, jobs))


def _points(n: int) -> str:
    return f"{n} point" if n == 1 else f"{n} points"


def evaluate_trade(
    state: AuctionState,
    give: list[PlayerTrade],
    receive: list[PlayerTrade],
    market_prices: dict[str, float],
    auto_check_buyouts: bool = True,
    source_team_code: str | None = None,
    workers: int = 1,
    model_params: dict | None = None,
    overpay: float = OVERPAY_STRESS,
) -> TradeEvaluation:
    """
    Evaluate a proposed trade by comparing MILP solutions.

    1. Solve current state → baseline
    2. Clone state, apply trade, solve → "keep all" scenario
    3. If auto_check_buyouts: let the solver buy out ANY SET of legal
       contracts on BOTH sides -- the post-trade roster, and the current
       roster with no trade at all
    4. Recommend accept iff the best trade outcome beats the best no-trade one

    Step 3 has to be symmetric. Until 2026-09-25 only the trade side got
    buyouts, and only of RECEIVED players, while the trade was compared against
    the bare current roster -- so a trade that merely moved one of BOT's own bad
    contracts scored the salary relief as a trade gain. Measured on the live
    draft: Gustavsson ($3.2M, drafted at a $1.76M model price) for Kyrou, then
    buying Kyrou out, read ACCEPT at +7 (1348); buying Gustavsson out with no
    trade scores 1351. The trade was worth -3, and the tool said take it.

    Step 3 also has to allow more than one buyout. Until the same day it
    tried them one at a time, so Kyrou for Thrun + Perunovich (1 point and 0,
    both cheap and dead) read DECLINE: the best SINGLE buyout scored 1444
    against 1450 for buying Kyrou out yourself, while buying out both scored
    1461 -- and the solver, choosing freely, finds 1462 by adding Coyle.

    Four solves plus two stress solves, however many contracts are eligible
    (it was ~2 per eligible contract under the one-at-a-time menu). `workers`
    fans them out (main passes SCAN_WORKERS). It still runs on the event loop:
    trades happen in auction breaks, with no bid in flight to stall.

    The two winners are then re-solved with every auction price marked up by
    `overpay`. Every plan here assumes BOT buys its open spots at the model's
    EXPECTED price, and a give-for-nothing trade is a bet on exactly that --
    Larkin + Dobson for nothing measured +2 (1333 -> 1335) by refilling their
    spots at expected prices, in a league whose one replayed draft paid 27%
    over the model. The stress solves say whether the gain survives that.

    `model_params` prices players the free-agent flow returns to the pool;
    main passes its loaded copy, and it is read from disk when omitted.
    """
    team = state.teams[MY_TEAM]
    trade_id = str(uuid.uuid4())[:8]

    # Apply trade to cloned state
    trade_state = deepcopy(state)
    trade_team = trade_state.teams[MY_TEAM]

    # Two-team trades: take salary/points from the source team's roster,
    # not the client-supplied form JSON (stale if adjusted after the
    # dropdown loaded). Mutating the PlayerTrade DTOs here also corrects
    # the values execute_trade will apply later.
    # A traded player keeps his real contract group, which decides buyout
    # eligibility and minors cap treatment. execute_trade already carries it
    # over; this preview used to hardcode "3", so a group A-E prospect looked
    # like a signed contract and the scenario builder below offered an illegal
    # buyout on him. Evaluate and execute must describe the same player.
    receive_groups: dict[str, str] = {}
    if source_team_code and source_team_code in trade_state.teams:
        src_team = trade_state.teams[source_team_code]
        for p in receive:
            actual = src_team.find_player(p.name)
            if actual is not None:
                p.salary = actual.salary
                p.projected_points = actual.projected_points
                receive_groups[p.name] = actual.group

    # Remove players BOT gives away
    for p in give:
        try:
            trade_team.remove_player(p.name)
        except ValueError:
            pass  # Player might not be on roster (shouldn't happen but be safe)

    # Add players BOT receives
    for p in receive:
        trade_team.add_acquired_player(PlayerOnRoster(
            name=p.name,
            position=p.position,
            # Falls back to "3" for the free-agent flow, where a received player
            # is a fresh draftee — the same group /assign gives them.
            group=receive_groups.get(p.name, "3"),
            salary=p.salary,
            projected_points=p.projected_points,
            nhl_team=getattr(p, "nhl_team", ""),
        ))

    # Remove received players from available pool (they're now on BOT)
    trade_available = dict(trade_state.available_players)
    for p in receive:
        trade_available.pop(p.name, None)

    # Free-agent flow only: given players return to the auction pool so the
    # post-trade MILP can model re-acquiring them. In a two-team trade they
    # live on the source team's roster and are not re-acquireable.
    if source_team_code is None:
        for p in give:
            trade_available[p.name] = Player(
                name=p.name,
                position=p.position,
                group="3",
                nhl_team="",
                age=0,
                projected_points=p.projected_points,
                is_rfa=False,
                salary=p.salary,
                # League-average odds, not 0.0 — 0% Cup probability is
                # out-of-distribution for the price model
                team_probability=DEFAULT_TEAM_PROBABILITY,
                pos_rank=_pool_rank(trade_available, p.position, p.projected_points),
            )

    # ...and they need PRICES. `market_prices` was computed before they were
    # in the pool, and the MILP prices a missing name at MIN_SALARY, so without
    # this every released player could be "bought back" for $0.5M: Larkin +
    # Dobson for nothing measured a bogus +35 that way (2026-09-25). Priced the
    # way /trade-execute's recompute will price them once the trade is real:
    # the model, capped by the post-trade market ceiling. Adding names to a
    # copy is safe for the no-trade solves, which only iterate their own pool.
    prices = dict(market_prices)
    returned = {n: q for n, q in trade_available.items() if n not in market_prices}
    if returned:
        preds = predict_all_prices(returned, model_params or load_model_params())
        ceiling = compute_market_ceiling(trade_state.teams)
        for n, pred in preds.items():
            prices[n] = compute_market_price(pred.expected_price, ceiling)

    # Buyouts on BOTH sides of the comparison -- see the docstring for why
    # the no-trade side is not optional -- and any number of them per side,
    # chosen by the solver. The plain plans stay alongside for the table.
    trade_jobs: list[_Job] = [
        ("Keep all received players", trade_team, trade_available, None),
    ]
    baseline_jobs: list[_Job] = []
    if auto_check_buyouts:
        trade_jobs.append(("Trade +", trade_team, trade_available, _buyout_menu(trade_team)))
        baseline_jobs.append(("No trade,", team, state.available_players, _buyout_menu(team)))

    all_jobs = (
        [("Current roster (no trade)", team, state.available_players, None)]
        + trade_jobs + baseline_jobs
    )
    solved = _solve_jobs(all_jobs, prices, workers)
    current_scenario = solved[0]
    # A buyout job that bought nobody out solved the plain plan's problem over
    # again, and is dropped rather than kept as a duplicate. Kept, it could WIN:
    # the solver may return a different roster of equal value that costs less,
    # ties break on money left over, and the verdict then read "Best line:
    # Trade +" -- the bare job label -- with a highlighted winner the table
    # never shows (found in the /grill of b1dbffe).
    scenarios = [solved[1]] + [s for s in solved[2:1 + len(trade_jobs)] if s.buyouts]
    baseline_scenarios = [s for s in solved[1 + len(trade_jobs):] if s.buyouts]

    # A scenario is only acceptable if it leaves a legal team: cap space
    # non-negative and a solvable roster. Comparing raw total_points let
    # cap-violating trades win (Infeasible solves still report baseline
    # lineup points).
    def _is_legal(s: TradeScenario) -> bool:
        return s.cap_remaining >= 0 and s.roster.status == "Optimal"

    # The bar the trade has to clear. The current roster is always a candidate,
    # legal or not: the league tolerates a temporary over-cap state, and "do
    # nothing" is always available.
    baseline_best = max(
        [current_scenario] + [s for s in baseline_scenarios if _is_legal(s)],
        key=lambda s: (s.total_points, s.left_after_plan),
    )

    legal = [s for s in scenarios if _is_legal(s)]
    if not legal:
        worst_cap = min(s.cap_remaining for s in scenarios)
        return TradeEvaluation(
            trade_id=trade_id,
            give=give,
            receive=receive,
            current_scenario=current_scenario,
            scenarios=scenarios,
            best_scenario=max(scenarios, key=lambda s: s.total_points),
            recommendation="decline",
            reasoning=(
                f"No legal outcome: trade leaves the roster over the cap "
                f"(${worst_cap:.1f}M remaining) or unsolvable"
            ),
            source_team_code=source_team_code,
            baseline_scenarios=baseline_scenarios,
            baseline_best=baseline_best,
        )

    # Find best legal scenario
    best = max(legal, key=lambda s: (s.total_points, s.left_after_plan))

    # Re-solve the two winners at stressed prices, in one batch. Found by
    # identity: solved[i] came from all_jobs[i].
    #
    # Only the part of a price ABOVE the league minimum is marked up. Nobody
    # can be made to pay more than the floor for a floor player, and marking
    # the floor itself up can make a tight roster unfillable -- 10 open spots
    # on $5.1M is feasible at $0.5M each and not at $0.625M. (Exempting only
    # prices of exactly MIN_SALARY is not enough: expected prices near the
    # floor are 0.51, 0.52..., and those are what a tight roster fills with.)
    # An Infeasible solve still reports the bare roster's points, which is not
    # a plan, so any non-Optimal stress solve leaves the figures unset.
    stressed_prices = {
        n: min(MIN_SALARY + max(v - MIN_SALARY, 0.0) * overpay, MAX_SALARY)
        for n, v in prices.items()
    }
    job_of = {id(sc): job for sc, job in zip(solved, all_jobs)}
    stress_trade, stress_bar = _solve_jobs(
        [job_of[id(best)], job_of[id(baseline_best)]], stressed_prices, workers,
    )
    stress_ok = stress_trade.roster.status == "Optimal" == stress_bar.roster.status

    # Compare the best trade outcome to the best NO-trade outcome, and name the
    # latter: "the trade loses 3" is baffling beside a table where it plainly
    # beats the current roster, unless the reasoning says what it lost to.
    bar = baseline_best
    # Whole points, truncated -- the figure the table, the team panel and the
    # Proj column all print (`int()`). Since 2026-09-25 totals are EXPECTED
    # points and fractional, so comparing raw floats made a 0.3-point edge an
    # ACCEPT reading "+0 points", made EVEN (an exact tie) all but unreachable,
    # and let the verdict disagree with the table beneath it: the verify pass
    # caught "1446" in the trade table beside "1445" on the team panel.
    best_pts, bar_pts = int(best.total_points), int(bar.total_points)
    bar_label = (
        f"buying out {', '.join(bar.buyouts)} yourself ({bar_pts})"
        if bar.buyouts else f"standing pat ({bar_pts})"
    )
    # Buying out everything you receive means the trade is a salary dump: the
    # partner absorbs your contracts and you keep nothing. Legitimate, but the
    # owner should not have to reverse-engineer that from a scenario name.
    received = {p.name for p in receive}
    dump = bool(received) and received <= set(best.buyouts)
    lead = "Salary dump — you keep nothing you receive. " if dump else ""
    unspent = round(best.left_after_plan - bar.left_after_plan, 1)

    if best_pts > bar_pts:
        recommendation = "accept"
        reasoning = (
            f"{lead}+{_points(best_pts - bar_pts)} over "
            f"{bar_label}. Best line: {best.description}"
        )
    elif best_pts < bar_pts:
        recommendation = "decline"
        reasoning = (
            f"{lead}{_points(bar_pts - best_pts)} worse than "
            f"{bar_label}. Best line: {best.description}"
        )
    elif unspent < 0:
        # Level on points and dearer: paying for nothing.
        recommendation = "decline"
        reasoning = (
            f"{lead}Level on points with {bar_label}, and leaves "
            f"${-unspent:.1f}M less unspent once the roster is filled"
        )
    else:
        # A tie is not an ACCEPT. Cap the solver could not turn into points is
        # a weak reason to trade, and a green verdict reads as "+10".
        recommendation = "even"
        extra = (
            f"leaves ${unspent:.1f}M more unspent once the roster is filled"
            if unspent > 0 else "spends the same once the roster is filled"
        )
        reasoning = f"{lead}No point gain: level with {bar_label}, and {extra}"

    return TradeEvaluation(
        trade_id=trade_id,
        give=give,
        receive=receive,
        current_scenario=current_scenario,
        scenarios=scenarios,
        best_scenario=best,
        recommendation=recommendation,
        reasoning=reasoning,
        source_team_code=source_team_code,
        baseline_scenarios=baseline_scenarios,
        baseline_best=baseline_best,
        stress_rate=overpay,
        stress_trade_points=stress_trade.total_points if stress_ok else None,
        stress_baseline_points=stress_bar.total_points if stress_ok else None,
    )


def _require_buyout_eligible(player: PlayerOnRoster) -> None:
    """Reject a buyout the league doesn't allow (CBA: group 2/3 only).

    Names the alternative, because mid-draft the operator needs to know what to
    do instead, not just that the button didn't work. A prospect on the active
    roster is the case worth catching: their salary DOES count on the cap, so
    the evaluation looked entirely plausible with nothing to flag it.
    """
    if not player.can_be_bought_out:
        raise ValueError(
            f"'{player.name}' is group {player.group} and cannot be bought out — "
            f"only groups {'/'.join(sorted(BUYOUT_ELIGIBLE_GROUPS))} are eligible. "
            f"Send him to the minors instead, where his cap hit is $0."
        )


def evaluate_buyout(
    state: AuctionState,
    player_name: str,
    market_prices: dict[str, float],
) -> BuyoutEvaluation:
    """
    Evaluate buying out a player on BOT's roster.

    Buyout removes the player but adds a penalty of 50% salary to cap.
    """
    team = state.teams[MY_TEAM]

    # Current optimal
    current_sol = solve_optimal_roster(team, state.available_players, market_prices)

    # Find the player
    player = team.find_player(player_name)
    if player is None:
        raise ValueError(f"Player '{player_name}' not found on {MY_TEAM}")
    _require_buyout_eligible(player)

    # Clone and apply buyout
    buyout_state = deepcopy(state)
    buyout_team = buyout_state.teams[MY_TEAM]
    buyout_team.remove_player(player_name)
    penalty = player.salary * BUYOUT_PENALTY_RATE
    buyout_team.penalties += penalty

    buyout_sol = solve_optimal_roster(buyout_team, state.available_players, market_prices)

    delta = buyout_sol.total_points - current_sol.total_points
    recommendation = "buyout" if delta > 0 else "keep"

    return BuyoutEvaluation(
        player_name=player_name,
        salary_freed=player.salary,
        penalty_added=penalty,
        net_cap_freed=player.salary * (1 - BUYOUT_PENALTY_RATE),
        current_points=current_sol.total_points,
        buyout_points=buyout_sol.total_points,
        delta_points=delta,
        recommendation=recommendation,
        current_roster=current_sol,
        buyout_roster=buyout_sol,
    )


def execute_trade(
    state: AuctionState,
    give: list[PlayerTrade],
    receive: list[PlayerTrade],
    source_team_code: str | None = None,
) -> None:
    """
    Execute a trade on the live state.

    With source_team_code set: a real two-team trade -- give players move to
    that team's roster, receive players come from that team's roster.
    Without it: legacy free-agent flow -- give players return to the available
    pool, receive players are pulled from it.
    """
    bot = state.teams[MY_TEAM]

    if source_team_code:
        if source_team_code == MY_TEAM:
            raise ValueError("Cannot trade with self")
        if source_team_code not in state.teams:
            raise ValueError(f"Unknown team {source_team_code}")
        other = state.teams[source_team_code]

        # Remove from BOTH teams before adding to either. add_acquired_player
        # routes to the minors at 24, so a full team that gains before it loses
        # would send the incoming player down on a trade that leaves its roster
        # exactly the same size.
        out_of_bot = [bot.remove_player(p.name) for p in give]
        out_of_other = [other.remove_player(p.name) for p in receive]

        for removed in out_of_bot:
            other.add_acquired_player(PlayerOnRoster(
                name=removed.name,
                position=removed.position,
                group=removed.group,
                salary=removed.salary,
                projected_points=removed.projected_points,
                nhl_team=removed.nhl_team,
            ))

        for removed in out_of_other:
            # Carry the authoritative roster object's identity: group drives
            # minors cap semantics (A-E don't count), salary is unchanged by
            # a trade, and a fresh PlayerOnRoster resets is_bench/is_minor.
            bot.add_acquired_player(PlayerOnRoster(
                name=removed.name,
                position=removed.position,
                group=removed.group,
                salary=removed.salary,
                projected_points=removed.projected_points,
                nhl_team=removed.nhl_team,
            ))
        return

    # Legacy free-agent flow
    for p in give:
        removed = bot.remove_player(p.name)
        state.available_players[p.name] = Player(
            name=removed.name,
            position=removed.position,
            group=removed.group,
            nhl_team=removed.nhl_team,
            age=0,
            projected_points=removed.projected_points,
            is_rfa=False,
            salary=removed.salary,
            # League-average odds, not 0.0 — 0% Cup probability is
            # out-of-distribution for the price model
            team_probability=DEFAULT_TEAM_PROBABILITY,
            # Re-entering the pool: rank against the remaining pool so the
            # price model's scarcity feature doesn't treat them as rank 1
            pos_rank=_pool_rank(state.available_players, removed.position, removed.projected_points),
        )

    for p in receive:
        state.available_players.pop(p.name, None)
        bot.add_acquired_player(PlayerOnRoster(
            name=p.name,
            position=p.position,
            group="3",
            salary=p.salary,
            projected_points=p.projected_points,
            nhl_team=getattr(p, "nhl_team", ""),
        ))


def execute_buyout(
    state: AuctionState,
    player_name: str,
    team_code: str = MY_TEAM,
) -> None:
    """Execute a buyout on a player on `team_code`'s roster.

    ANY team may buy anyone out (CBA 11.4) and this tool is the league's record
    of all eleven rosters, so a rival's buyout has to be recordable — it was
    not until 2026-09-12, and an unrecorded one leaves that team's cap wrong in
    every calculation the app makes about them, the market ceiling included.

    `evaluate_buyout` deliberately does NOT follow: it scores a hypothetical
    against BOT's MILP total, so run on SRL's roster it would answer a question
    about the wrong team. Recording an opponent's buyout and advising on your
    own are different jobs.

    Eligibility is unchanged and is not a function of who owns the contract:
    group 2/3 only, wherever the player sits.
    """
    team = state.teams.get(team_code)
    if team is None:
        raise ValueError(f"Unknown team '{team_code}'")
    # Checked here as well as in evaluate_buyout: /buyout posts a bare player
    # name, so guarding only the advisory path leaves the illegal move one
    # hand-made request away — and this one mutates the cap.
    target = team.find_player(player_name)
    if target is None:
        # Names the team, because with eleven rosters reachable "not found" on
        # its own reads as "not in the league" when the real answer is usually
        # "he is on somebody else's".
        raise ValueError(f"Player '{player_name}' not found on {team_code}")
    _require_buyout_eligible(target)

    player = team.remove_player(player_name)
    team.penalties += player.salary * BUYOUT_PENALTY_RATE

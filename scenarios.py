"""Pre-baked auction-state scenarios for live testing.

Each scenario takes a freshly initialized AuctionState and mutates it in
place. Used by POST /load-scenario to drop the user into a specific
draft state without manually drafting players.
"""

from __future__ import annotations

from config import (
    BACKUP_TARGETS,
    MIN_SALARY,
    MY_TEAM,
    POSITION_MINIMUMS,
    ROSTER_SIZE,
    SALARY_CAP,
)
from data_loader import build_initial_state
from price_model import load_model_params, predict_all_prices
from state import AuctionState, PlayerOnRoster, TeamState


def _scenario_goalie_asymmetry(state: AuctionState) -> None:
    """Every non-BOT team has 2 goalies; BOT keeps however many it started with.

    Pulls the cheapest available goalies (by projected points ascending) and
    assigns them as acquired players at $0.5M each. Stops when each non-BOT
    team has exactly 2 G on the active roster.
    """
    target = 2

    # Collect available goalies cheapest-first (low projected points = back-end).
    pool = sorted(
        (p for p in state.available_players.values() if p.position == "G"),
        key=lambda p: p.projected_points,
    )
    pool_iter = iter(pool)

    for code, team in state.teams.items():
        if code == MY_TEAM:
            continue
        have = sum(1 for p in team.roster_players if p.position == "G")
        while have < target:
            try:
                src = next(pool_iter)
            except StopIteration:
                return
            _seat(team, state, src.name, MIN_SALARY)
            have += 1


def _model_price(state: AuctionState) -> dict[str, float]:
    """Model price per available player, quantized like every other money value."""
    predictions = predict_all_prices(state.available_players, load_model_params())
    return {
        name: max(MIN_SALARY, round(p.expected_price, 1))
        for name, p in predictions.items()
    }


def _seat(
    team: TeamState,
    state: AuctionState,
    name: str,
    salary: float,
    minors: bool = False,
) -> None:
    """Sell a pool player to a team at `salary`, the way POST /assign would.

    Goes through `PlayerOnRoster.from_pool`, so the RFA group conversion happens
    here too — which is the difference between a stashed purchase costing a team
    its salary and costing it nothing (see `counts_on_cap`).

    `minors=True` puts him straight down rather than on the active roster. That
    is not the same as letting `add_acquired_player` overflow: overflow only
    happens at 24, and depth gets stashed on teams with spots to spare.
    """
    player = state.available_players.pop(name)
    on_roster = PlayerOnRoster.from_pool(player, salary)
    if minors:
        team.add_minor_player(on_roster)
    else:
        team.add_acquired_player(on_roster)


def _fill(
    team: TeamState,
    state: AuctionState,
    price: dict[str, float],
    reserved: set[str],
    up_to: int = ROSTER_SIZE,
    positions: tuple[str, ...] = ("F", "D", "G"),
) -> None:
    """Fill spots with the cheapest players available, positions still needed first.

    The counterpart to `_drain`, and deliberately not the same function: that one
    buys the DEAREST player under a headroom bound to walk a budget down onto a
    target, this one buys the CHEAPEST to fill spots while leaving the budget
    where it is. A floor purchase moves `spendable_budget` by zero ($0.5M of
    budget out, $0.5M of reserve freed), which is what makes the two composable —
    drain to a target, then fill without disturbing it.

    `positions` restricts what may be bought. Needed because the needs-first rule
    fights `reserved`: a team needing only a goalie, with every goalie reserved,
    has no legal candidate and stops early. That is not hypothetical — it is how
    the first build of `endgame-last-goalie` left BOT at 19 of 23 players, and
    the only sign was a physical max that came out right anyway.

    `room` keeps the commissioner's reserve intact (a bid may never leave a team
    unable to fill its roster at MIN_SALARY), the same guard `_drain` uses. It
    cannot bind on either scenario and is not meant to: a cheapest-first fill
    picks a floor-priced player, the reserve rule guarantees `room >= MIN_SALARY`,
    and deleting the condition entirely fails no test (measured). It earns its
    place at the other end — when `positions` narrows the choice to a position
    whose cheap end has been sold, it turns "seat him anyway and break the
    reserve" into the RuntimeError below.

    Raises RuntimeError rather than returning short: a scenario that quietly
    builds a smaller roster than it says produces test failures three assertions
    away from the cause.
    """
    while team.roster_count < up_to:
        room = team.remaining_budget - (team.total_spots_remaining - 1) * MIN_SALARY
        needs = team.roster_needs
        wanted = [pos for pos in positions if needs.get(pos, 0) > 0] or list(positions)
        candidates = [
            (price[name], name) for name, p in state.available_players.items()
            if name not in reserved
            and price[name] <= room
            and p.position in wanted
        ]
        if not candidates:
            raise RuntimeError(
                f"{team.code} stalled at {team.roster_count} of {up_to}: no "
                f"{'/'.join(wanted)} in the pool at or under ${room:.1f}M"
            )
        # Ties on name, so two loads of the same scenario are identical.
        _, name = min(candidates)
        _seat(team, state, name, price[name])


def _drain(
    team: TeamState,
    state: AuctionState,
    price: dict[str, float],
    reserved: set[str],
    target_spendable: float,
) -> None:
    """Buy real players at model price until `spendable_budget` drops to target.

    Converges on the target from above rather than blowing past it. Buying the
    most expensive affordable player each time undershoots wildly — the last
    purchase can be worth millions — and the first version of this landed both
    live opponents on the SAME $0.9M max from targets of $3.0M and $2.2M, which
    makes `second_bidder` meaningless and is exactly what the differing targets
    exist to avoid.

    The arithmetic that makes it converge: seating a player at price P costs P of
    budget but also frees one reserved spot, so `spendable` moves by
    `-(P - MIN_SALARY)`. Hence the two bounds below.

    * `P <= headroom + MIN_SALARY` — do not cross the target.
    * `P > MIN_SALARY` — must make progress. A min-salary player leaves
      `spendable` UNCHANGED (0.5 of budget out, 0.5 of reserve freed), so
      without this the loop never terminates while floor-priced players remain,
      and 534 of 705 of them are floor-priced.

    Never picks a player the team could not legally seat either: the
    commissioner refuses a bid that would leave a roster unfillable at
    MIN_SALARY, so `room` caps the choice and a scenario that ignored it would
    be modelling an auction the league forbids.

    Ties break on name so two loads of the same scenario are identical; the tests
    assert that, and `dict` order alone would tie it to CSV row order.
    """
    while team.spendable_budget > target_spendable and team.roster_count < ROSTER_SIZE:
        room = team.remaining_budget - (team.total_spots_remaining - 1) * MIN_SALARY
        headroom = team.spendable_budget - target_spendable
        ceiling = min(room, headroom + MIN_SALARY)
        needs = team.roster_needs
        wanted = [pos for pos in ("F", "D", "G") if needs.get(pos, 0) > 0]
        candidates = [
            (name, p) for name, p in state.available_players.items()
            if name not in reserved
            and MIN_SALARY < price[name] <= ceiling
            and (p.position in wanted if wanted else True)
        ]
        if not candidates:
            return
        name, _ = max(candidates, key=lambda kv: (price[kv[0]], kv[0]))
        _seat(team, state, name, price[name])


def _scenario_endgame_ceiling_binds(state: AuctionState) -> None:
    """Late draft: most teams have stopped, the rest are broke, the stars are unsold.

    Exists because a fresh state cannot reach the market ceiling's interesting
    half. Measured on `/reset`: all 11 teams sit at `physical_max_bid` = 11.4, so
    `compute_market_ceiling` returns MAX_SALARY, every bid reports
    `stop_status = at_cap` with no forecast, and **not one** row in Available
    Players is `capped` — which is why the app's only `tooltip-left` had never
    been placement-checked.

    Three ingredients, and all three are load-bearing. Two plausible-sounding
    scenarios were built first and both produced ZERO capped rows:

    * Draining budgets by buying the best available got the ceiling to $5.2M and
      capped nothing — buying top-down removes exactly the players whose model
      price the ceiling would have capped.
    * Reserving the top 40 and draining with mid-tier depth put the ceiling back
      at $11.4M. The price distribution is far steeper than it looks: 20 players
      above $4M, 36 above $3M, and **534 of 705 at the $0.5M floor** — floor
      meaning `round(expected_price, 1) == 0.5`, which has to be stated because
      the count swings from 0 to 604 across plausible definitions (no player's
      expected price is exactly MIN_SALARY; 604 are under $1M). Reserving 40
      reserves everything above $3.0M, so teams filled all 24 spots with
      floor-priced depth and still had ~$20M spare.

      These three numbers read 19 / 40 / 563 until 2026-08-17. Re-measured
      against `data/players.csv`, unchanged since 2026-07-05 — so they were
      wrong when written here on 2026-08-13, not stale. 563 reproduces under no
      definition at all.

    The distribution is also why this state does not arise by accident — pool
    value is ~$632M against ~$338M of league money for ~165 open spots, so a real
    draft consumes the whole expensive tier. What produces it instead is a rule
    the league already has: a team marked done is excluded from the ceiling
    (`.claude/rules/pricing-pipeline.md`), and the design notes put 3+ early
    finishers in every draft. So: most teams done, the two still bidding drained,
    and the top of the pool held back.

    Note what is NOT done here: `pos_rank` is frozen against the draft-time pool
    by `build_initial_state` and must never be recomputed against the shrinking
    one. This only deletes from the pool, so that holds by construction.
    """
    price = _model_price(state)

    # The last two bidders are the two richest opponents — the teams that would
    # plausibly still be live this late. Derived, not hard-coded: team codes come
    # from fchl_teams.json and the same rule that forbids literal player names in
    # tests applies to a scenario that has to survive a league edit.
    opponents = sorted(
        (code for code in state.teams if code != MY_TEAM),
        key=lambda code: (-state.teams[code].remaining_budget, code),
    )
    live, done = opponents[:2], opponents[2:]
    for code in done:
        state.teams[code].is_done = True

    # Hold back the top of the pool so the stars are still unsold once nobody can
    # afford them. 25 rather than 40: see the distribution note above — 40 takes
    # everything over $3.0M and leaves nothing worth draining a budget on.
    reserved = set(sorted(price, key=lambda n: (-price[n], n))[:25])

    # Different targets on purpose: the ceiling is the SECOND-highest opponent
    # max, so two teams at the same number would hide which one sets it.
    for code, target in zip(live, (3.0, 2.2)):
        _drain(state.teams[code], state, price, reserved, target)

    # BOT keeps real money and real needs, so the panel gives live advice rather
    # than DROP on everything — the point is to exercise the advisor, not to
    # bankrupt it.
    _drain(state.teams[MY_TEAM], state, price, reserved, 7.0)


def _scenario_endgame_last_goalie(state: AuctionState) -> None:
    """Goaltending is picked clean: one spot, one goalie BOT can afford, no plan B.

    The state behind two documented engine semantics that until now existed only
    against 23 synthetic players called F0..G2 (`tests/test_edge_cases.py`):

    * **A must-have is worth the physical max.** With one spot, a goalie need and
      no affordable alternative, the roster is UNSOLVABLE without him — so
      `compute_marginal_value` skips the binary search and returns everything BOT
      can pay. Measured here: a $0.5M, 2-point backup carrying a $3.0M value cap,
      six times his model price.
    * **Forced players exactly filling the roster = Optimal, not Infeasible.**
      Forcing him leaves `spots == 0`, which `solve_optimal_roster` answers from
      its own branch rather than the MILP — returning `roster == []` and
      `total_cost` equal to the forced salary. That branch used to return
      Infeasible, which floor-priced EVERY player once one spot remained.

    Three ingredients:

    1. **BOT needs a goalie at all.** Spares go down to the minors until one is
       left in the crease — a real move the app offers (`POST /move-to-minors`),
       and where this league already keeps its spares: BOT's own keeper data
       carries ten goalies down there. Measured on today's roster: one demotion,
       John Gibson, 26 points, group 3, so his $0.5M stays fully on the cap and
       the demotion frees nothing.

       A loop rather than a single demotion, because the count is keeper data and
       moves every season. Measured against a doctored roster: with three keeper
       goalies the old single demotion left BOT needing NONE, so the state was not
       a must-have one at all and the scenario quietly stopped testing its own
       subject; with one it demoted the last goalie, leaving a team that cannot
       field a legal lineup at all. Both now come out at exactly one goalie short.
    2. **Exactly one spot, and a budget below the salary cap.** `_drain` to $2.5M
       spendable then `_fill` to 23 leaves BOT one seat and $3.0M — deliberately
       under MAX_SALARY, because a physical max that IS the league maximum cannot
       be told apart from the clamp inside `physical_max_bid`, and the whole claim
       here is which number the marginal came from. Filling with floor-priced
       depth does not disturb the drain's target: see `_fill`.
    3. **No affordable alternative.** Every OTHER goalie the pool offers at or
       under BOT's remaining budget has to go, or excluding the target leaves a
       legal roster and the marginal drops back to a binary search. What leaves is
       measured, not chosen: 53 of 64 goalies, 20 filling out opponents' creases
       to the classic three and 33 stashed in their minors. Eleven stay on the
       board — the target at $0.5M and ten priced $3.2M-$7.7M, every one of them
       past BOT's whole budget. So the pool still SHOWS goalies, which is the
       honest version of this state: late-draft goaltending is not gone, it is out
       of reach.

    What this scenario deliberately does NOT do is drain the opponents. They stay
    rich, the ceiling holds at MAX_SALARY and every `stop_status` reads `at_cap`
    — `endgame-ceiling-binds` owns that half, and it needed three ingredients of
    its own to get there.

    Why the goalies go onto opponents' rosters rather than all into their minors:
    it is about the state being READABLE, not about solvability. A first draft of
    this docstring claimed an opponent left needing goalies would solve Infeasible
    and lose its exact League State projection — that is false here and the
    mutation proves it. Stripping the crease-filling leaves every opponent Optimal,
    because they are rich and ten goalies priced $3.2M-$7.7M are still on the
    board: unaffordable to BOT, pocket change to a team with $20M. What the
    filling buys is a league that looks like a league on screen — three in the
    crease each, the classic shape — rather than ten teams carrying one goalie and
    six in the minors.
    """
    price = _model_price(state)
    bot = state.teams[MY_TEAM]
    goalies = {n for n, p in state.available_players.items() if p.position == "G"}

    # One short of the position minimum, so exactly one goalie is needed. Weakest
    # first, and the tie-break on name keeps two loads identical.
    crease = sorted(
        (p for p in bot.roster_players if p.position == "G"),
        key=lambda p: (p.projected_points, p.name),
    )
    for spare in crease[:max(0, len(crease) - (POSITION_MINIMUMS["G"] - 1))]:
        spare.is_bench = True  # send_to_minors' precondition
        bot.send_to_minors(spare.name)

    # Goalies are reserved from BOT's own buying: the point is the hole in the
    # crease, and `_drain` would happily fill it with the best one in the pool.
    _drain(bot, state, price, goalies, 2.5)
    _fill(bot, state, price, goalies, up_to=ROSTER_SIZE - 1, positions=("F", "D"))

    # The MILP's own budget constraint is what decides "affordable": filling the
    # last spot with a goalie priced P needs P <= remaining_budget. With one spot
    # that is also exactly physical_max_bid.
    affordable = bot.remaining_budget
    ranked = sorted(
        goalies & set(state.available_players),
        # Cheapest, then the least useful of those, then name — so the must-have
        # is the least defensible player in the pool and the tie-break is stable.
        key=lambda n: (price[n], state.available_players[n].projected_points, n),
    )
    # ranked[0] is the must-have and stays; everything cheap enough to replace
    # him is what leaves.
    sold = [n for n in ranked[1:] if price[n] <= affordable]

    opponents = [code for code in state.teams if code != MY_TEAM]
    per_team = POSITION_MINIMUMS["G"] + BACKUP_TARGETS["G"]  # the 14/7/3 shape
    surplus = iter(sold)
    for code in opponents:
        team = state.teams[code]
        while sum(1 for p in team.roster_players if p.position == "G") < per_team:
            name = next(surplus, None)
            if name is None:
                break
            # Model price, capped by what the commissioner would let them bid.
            room = team.remaining_budget - (team.total_spots_remaining - 1) * MIN_SALARY
            _seat(team, state, name, min(price[name], room))
    # The rest are depth, at the floor, round-robin. They must leave the pool
    # whatever happens to them: any one of them left behind is a legal
    # alternative to the target and there is no must-have any more.
    for i, name in enumerate(surplus):
        _seat(state.teams[opponents[i % len(opponents)]], state, name, MIN_SALARY, minors=True)


def _scenario_endgame_sole_bidder(state: AuctionState) -> None:
    """The league is out of money: ten full rosters, nobody done, BOT alone able to bid.

    The 2026-08-05 report, as a state you can load: every rival drops out, the
    live ceiling collapses to the floor, and an advisor that still capped on
    `ceiling + increment` said DROP at $2.5M on a player worth $4.2M. It is
    pinned by unit tests on a synthetic team; this reaches it on the real pool,
    where the number the collapse would have produced is measured at **$0.6M**
    against a $7.5M value cap.

    **A team with a roster spot open can always bid the floor.** The commissioner
    software refuses any bid that would leave a team unable to fill 24 at
    MIN_SALARY, so `spendable_budget` can never go negative and
    `physical_max_bid` never drops below MIN_SALARY while a spot remains. The only
    legal way to price a team out completely is therefore a FULL roster with less
    than one increment of cap left — which is what `_drain` to zero followed by
    `_fill` to 24 produces (measured: all ten land at $0.0-0.1M). A floor purchase
    moves `spendable_budget` by nothing, so the fill cannot undo the drain.

    **Nobody is marked done, and that is the whole point.** `bid_panel.html`
    filters the bidder grid on `is_done` alone, so these teams stay clickable
    while unable to raise a bid — exactly the case `market.bid_winner` exists to
    get right, and the one that used to render "You've won" with no Assign button.
    Marking them done instead would produce the same WIN verdict for the wrong
    reason and cover none of it.

    BOT is drained FIRST, so it buys from the top of the pool rather than the
    leftovers, and to a target that leaves its physical max under MAX_SALARY
    (measured $7.5M) — a value cap sitting exactly at the league maximum cannot
    be told apart from `physical_max_bid`'s clamp.

    Side effect worth knowing: with no opponent able to bid, `demand_count` is 0,
    `floor_demand` is True and EVERY market price in the pool is MIN_SALARY. That
    is the documented zero-demand rule, and this is the only loadable state that
    shows it.
    """
    price = _model_price(state)

    # BOT first: after ten teams have spent $300M the pool is depth only, and a
    # drain against it would need most of BOT's 12 spots to move the budget.
    _drain(state.teams[MY_TEAM], state, price, set(), 7.0)

    for code, team in state.teams.items():
        if code == MY_TEAM:
            continue
        _drain(team, state, price, set(), 0.0)
        _fill(team, state, price, set())


def _squeeze(team: TeamState, target_max: float) -> None:
    """Set `penalties` so `physical_max_bid` lands exactly on `target_max`.

    The lever purchases cannot pull. `_drain` stops at `ROSTER_SIZE`, so a team
    cannot spend its way to "no money, spots still open" — measured 2026-08-18
    with the top 25 held back, **7 of 10 opponents hit 24 players with $8.2M to
    $22.6M still spendable**, the ceiling stayed at MAX_SALARY and not one of 570
    pool prices was capped. Dead cap is what removes money without adding players,
    and the league already has it: CBA 11.4 leaves 50% of a bought-out salary on
    the cap. So a squeezed team reads as one that bought contracts out, which is
    exactly the mid-draft state these scenarios are about.

    Inverted rather than searched, the same trick as `tests/helpers.squeeze` and
    `test_nomination._drain_state`: zero the penalties first so `total_salary`
    reads the roster alone, then solve for the remaining budget that produces
    `target_max`.

    **Two branches, and the second one is load-bearing.** With spots open,
    `physical_max_bid = (remaining - spots * MIN_SALARY) + MIN_SALARY`, so the
    reserve has to be added back. At `spots == 0` there is no reserve and
    `physical_max_bid` IS `remaining_budget` — adding the term there lands the
    figure $0.5M off, which is the whole claim of `full-roster-still-bidding`.

    Call it AFTER every purchase for that team. A squeezed team has no `room`
    left, so `_fill` raises and `_drain` returns having bought nothing.
    """
    team.penalties = 0.0
    team._invalidate_cache()
    spots = team.total_spots_remaining
    wanted_remaining = (
        target_max if spots == 0
        else target_max - MIN_SALARY + spots * MIN_SALARY
    )
    team.penalties = round(
        max(0.0, SALARY_CAP - team.total_salary - wanted_remaining), 1
    )
    team._invalidate_cache()


def _late_draft_shape(
    state: AuctionState,
    price: dict[str, float],
    reserved: set[str],
    codes: list[str],
    spread: tuple[float, float],
    fill_to: tuple[int, int] = (17, 21),
) -> None:
    """Give each of `codes` a real roster, real holes, and no money left.

    Three steps per team, in this order: `_drain` to $12.0M spendable (money
    actually spent on players, so the rosters look drafted rather than filled),
    `_fill` to a staggered size with depth, then `_squeeze` onto a staggered
    physical max. Spending first is not decoration — it is what keeps the
    penalties plausible: measured, no drain at all needs $13.5M to $28.4M of dead
    cap per team, draining to $12.0M needs **$9.0M to $11.0M**, and draining
    deeper does not help (at $8.0M and $5.0M some teams reach 24 players, which
    destroys the premise, while the penalty spread widens to $5.4-15.9M and
    $2.9-19.5M).

    **The stagger is load-bearing, not cosmetic.** The market ceiling is the
    SECOND-highest opponent max; ten teams on the same number make "second"
    indistinguishable from "highest" or "any of them", so a test asserting the
    ceiling is the second-highest could not fail. Same reason
    `_scenario_endgame_ceiling_binds` drains its two live teams to different
    targets. Codes are taken in the caller's order, so the assignment is
    deterministic and two loads of a scenario are identical.
    """
    lo, hi = spread
    fill_lo, fill_hi = fill_to
    last = len(codes) - 1
    for i, code in enumerate(codes):
        team = state.teams[code]
        _drain(team, state, price, reserved, 12.0)
        _fill(team, state, price, reserved,
              up_to=fill_lo + (i * (fill_hi - fill_lo)) // last)
        _squeeze(team, round(lo + i * (hi - lo) / last, 1))


def _leave_bot_planning(
    state: AuctionState, price: dict[str, float], reserved: set[str]
) -> None:
    """BOT: money spent, holes left, and still able to plan. Both scenarios.

    Tuned against the real call order rather than guessed, and the order matters:
    the opponents are shaped first and thin the mid tier, so the same drain target
    buys BOT MORE players here than the same call does against a fresh pool. Every
    figure below is measured after the ten opponents have been through.

    * $14.0M — 21 players, 3 spots, and `roster_needs` all zero. Cheapest penalty
      ($7.0M) and the worst state to load: nothing left to plan.
    * $16.0M — **19 players, 5 spots, $9.5M remaining, $7.5M physical max, needs
      {D: 1}, penalty $9.0M**, and the optimal lineup at 1196 points, the best of
      the four targets tried. This one.
    * $18.0M / $20.0M — 18 players and 6 spots, but $11.0M and $13.0M of dead cap,
      which reads as absurd on your own roster.

    $7.5M is deliberately under MAX_SALARY: a physical max sitting at the league
    maximum cannot be told apart from `physical_max_bid`'s clamp, which is the
    mistake `endgame-last-goalie` was built to avoid. The penalty lands in the
    same $9-11M band as the opponents', so the state reads as one league rather
    than as BOT being special.
    """
    bot = state.teams[MY_TEAM]
    _drain(bot, state, price, reserved, 16.0)
    if bot.roster_count < 18:
        _fill(bot, state, price, reserved, up_to=18)
    _squeeze(bot, 7.5)


def _reserved_top(price: dict[str, float], count: int = 25) -> set[str]:
    """The priciest `count` players, held back so the ceiling has something to cap.

    `_scenario_endgame_ceiling_binds` learned this the hard way twice: a top-down
    drain removes exactly the players whose model price the ceiling would have
    cut, and the state then reports zero capped rows. 25 rather than 40 for the
    reason recorded there — the distribution is steep, and reserving 40 takes
    everything over $3.0M and leaves nothing worth draining a budget on.
    """
    return set(sorted(price, key=lambda n: (-price[n], n))[:count])


def _scenario_drained_late_draft(state: AuctionState) -> None:
    """Sixty picks in: everyone has holes, nobody can pay for the stars left.

    The state between a fresh reset and the two endgames, and the one a real
    draft spends most of its second half in. `endgame-ceiling-binds` reaches a
    binding ceiling by marking eight teams DONE, which is a different situation:
    there, demand has collapsed and two teams are bidding. Here all ten opponents
    are live, every one of them still needs players, and the ceiling binds anyway
    — because the money is gone.

    Measured 2026-08-18: ceiling **$3.3M**, strictly inside the floor/cap range
    and the second of ten distinct maxes ($3.5M / $3.3M / $3.1M / ...);
    `demand_count` 10 with `floor_demand` False; rosters 17-21 with 3-7 spots
    each and nobody done; **25 of 597** pool prices capped; every team's MILP
    Optimal. Build 16ms.

    What it is for: this is the only loadable state where the bid panel's
    forecast half says something about a player rather than reporting `at_cap`.
    A bid check on the priciest RFA against the two richest rivals reads BID,
    worth $4.0M, "Should win it" $3.6M — and the nomination panel shows him at a
    $3.3M market price against a $9.5M model price, which is the struck-through
    figure the two-price line was built for, outside an endgame.
    """
    price = _model_price(state)
    reserved = _reserved_top(price)
    _late_draft_shape(
        state, price, reserved,
        sorted(code for code in state.teams if code != MY_TEAM),
        spread=(1.5, 3.5),
    )
    _leave_bot_planning(state, price, reserved)


def _scenario_full_roster_still_bidding(state: AuctionState) -> None:
    """A rival filled its 24 early, still has $8M of cap, and sets the whole market.

    The other half of the 2026-08-05 report. `4dc59da` made a full roster with cap
    space a LIVE bidder — extras go to the minors with their salary fully on the
    cap, so a team at 24 can still raise a bid and someone has to outbid it — and
    that fix has only ever been exercised against synthetic teams.
    `endgame-sole-bidder` is the opposite case (full AND broke) and cannot be
    folded in: there the ceiling collapses to the floor, here a team that cannot
    roster anybody sets the league's clearing price.

    Measured 2026-08-18. **MAC: 24 players, 0 spots, `roster_needs` all zero,
    `physical_max_bid` $8.0M** on $8.0M of remaining budget — and
    `market_ceiling` is $8.0M with `second_bidder` MAC. GVR is highest at $10.0M
    with 7 spots still open, the other eight opponents run $3.0M down to $1.5M at
    17-21 players, nobody is done, every team's MILP is Optimal. Build 13ms.

    **The counterfactual is the point, and it is a number**: the second-highest
    max among opponents WITH SPOTS is $3.0M, so a ceiling rule that gated on
    roster space would price the entire pool **$5.0M** too low. Downstream,
    `live_opponents([BOT, MAC])` returns MAC and `compute_live_ceiling` gives
    $8.0M, so a bid check on the priciest player in the pool against MAC alone
    reads BID, worth $6.2M, "Should win it" $8.1M — advice that exists only
    because a full team counts.

    Three things are load-bearing:

    * **The full team must be SECOND-highest.** The market ceiling IS the
      second-highest opponent max, so as the highest it would set nothing and the
      scenario would prove nothing. Hence $8.0M for it against $10.0M for the
      richest rival, and swapping the two is a mutation the tests catch.
    * **Both figures sit under MAX_SALARY.** A physical max at the league maximum
      cannot be told apart from `physical_max_bid`'s clamp — the mistake
      `endgame-last-goalie` was built to avoid — and the claim here is precisely
      which number the ceiling came from.
    * **The two rich teams are shaped FIRST, while the pool is still rich.** Not
      cosmetic: `_drain` buys the dearest player it can and stops at
      `ROSTER_SIZE`, so against the leftovers of eight shaped teams it needs many
      cheap purchases to move a big budget — measured, shaping these two last put
      the RIVAL at 24 as well. Two full teams read fine on screen and quietly
      destroy the test: with the highest and the second both full, "the ceiling is
      set by a team with no roster space" can no longer fail.

    Only **2 of 594** pool prices are capped here — exactly the two players whose
    model price exceeds $8.0M. That is expected at this ceiling and this scenario
    is not the one for the capped marker; `drained-late-draft` is.
    """
    price = _model_price(state)
    reserved = _reserved_top(price)
    by_wealth = sorted(
        (code for code in state.teams if code != MY_TEAM),
        key=lambda code: (-state.teams[code].remaining_budget, code),
    )
    full, rival = by_wealth[0], by_wealth[1]
    for code, up_to, target_max in ((full, ROSTER_SIZE, 8.0), (rival, 17, 10.0)):
        team = state.teams[code]
        _drain(team, state, price, reserved, 12.0)
        _fill(team, state, price, reserved, up_to=up_to)
        _squeeze(team, target_max)
    _late_draft_shape(state, price, reserved, by_wealth[2:], spread=(1.5, 3.0))
    _leave_bot_planning(state, price, reserved)


SCENARIOS = {
    "goalie-asymmetry": _scenario_goalie_asymmetry,
    "endgame-ceiling-binds": _scenario_endgame_ceiling_binds,
    "endgame-last-goalie": _scenario_endgame_last_goalie,
    "endgame-sole-bidder": _scenario_endgame_sole_bidder,
    "drained-late-draft": _scenario_drained_late_draft,
    "full-roster-still-bidding": _scenario_full_roster_still_bidding,
}


def load(name: str) -> AuctionState:
    """Build a fresh state and apply the named scenario. Raises KeyError if unknown."""
    if name not in SCENARIOS:
        raise KeyError(f"Unknown scenario: {name}")
    state = build_initial_state()
    SCENARIOS[name](state)
    return state

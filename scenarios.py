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
    unable to fill its roster at MIN_SALARY), the same guard `_drain` uses.

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

    1. **BOT needs a goalie at all.** Its keepers include two, so the spare goes
       down to the minors — a real move the app offers (`POST /move-to-minors`),
       and where this league already keeps its spares: BOT's own keeper data
       carries ten goalies down there. Measured: John Gibson, 26 points, group 3,
       so his $0.5M stays fully on the cap and the demotion frees nothing.
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
    its own to get there. Their creases do get filled, which is not decoration:
    an opponent still needing goalies with none in reach solves Infeasible, and
    the League State projection would silently fall back to its estimate for it.
    """
    price = _model_price(state)
    bot = state.teams[MY_TEAM]
    goalies = {n for n, p in state.available_players.items() if p.position == "G"}

    spare = min(
        (p for p in bot.roster_players if p.position == "G"),
        key=lambda p: (p.projected_points, p.name),
    )
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


SCENARIOS = {
    "goalie-asymmetry": _scenario_goalie_asymmetry,
    "endgame-ceiling-binds": _scenario_endgame_ceiling_binds,
    "endgame-last-goalie": _scenario_endgame_last_goalie,
    "endgame-sole-bidder": _scenario_endgame_sole_bidder,
}


def load(name: str) -> AuctionState:
    """Build a fresh state and apply the named scenario. Raises KeyError if unknown."""
    if name not in SCENARIOS:
        raise KeyError(f"Unknown scenario: {name}")
    state = build_initial_state()
    SCENARIOS[name](state)
    return state

"""Pre-baked auction-state scenarios for live testing.

Each scenario takes a freshly initialized AuctionState and mutates it in
place. Used by POST /load-scenario to drop the user into a specific
draft state without manually drafting players.
"""

from __future__ import annotations

from config import MIN_SALARY, MY_TEAM, ROSTER_SIZE
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
            team.add_acquired_player(PlayerOnRoster(
                name=src.name,
                position=src.position,
                group=src.group,
                salary=MIN_SALARY,
                projected_points=src.projected_points,
                nhl_team=src.nhl_team,
            ))
            del state.available_players[src.name]
            have += 1


def _model_price(state: AuctionState) -> dict[str, float]:
    """Model price per available player, quantized like every other money value."""
    predictions = predict_all_prices(state.available_players, load_model_params())
    return {
        name: max(MIN_SALARY, round(p.expected_price, 1))
        for name, p in predictions.items()
    }


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
        name, player = max(candidates, key=lambda kv: (price[kv[0]], kv[0]))
        team.add_acquired_player(PlayerOnRoster(
            name=player.name,
            position=player.position,
            group=player.group,
            salary=price[name],
            projected_points=player.projected_points,
            nhl_team=player.nhl_team,
        ))
        del state.available_players[name]


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


SCENARIOS = {
    "goalie-asymmetry": _scenario_goalie_asymmetry,
    "endgame-ceiling-binds": _scenario_endgame_ceiling_binds,
}


def load(name: str) -> AuctionState:
    """Build a fresh state and apply the named scenario. Raises KeyError if unknown."""
    if name not in SCENARIOS:
        raise KeyError(f"Unknown scenario: {name}")
    state = build_initial_state()
    SCENARIOS[name](state)
    return state

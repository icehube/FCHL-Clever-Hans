"""FastAPI app: all HTTP endpoints for the auction simulator."""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime

from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from config import MAX_SALARY, MIN_SALARY, MY_TEAM, SALARY_CAP
from data_loader import build_initial_state, load_goalie_wins
from market import (
    MarketInfo,
    compute_all_market_prices,
    bid_winner,
    compute_live_ceiling,
    compute_market_ceiling,
    live_opponents,
)
from optimizer import (
    CounterfactualResult,
    compute_bid_recommendation,
    compute_marginal_value,
    generate_counterfactual,
    recommend_nomination,
    solve_optimal_roster,
)
from price_model import (
    PricePrediction,
    compute_pos_ranks,
    load_model_params,
    predict_all_prices,
)
from state import AuctionState, ChangeRecord, Player, PlayerOnRoster, TransactionRecord
from trade import (
    PlayerTrade,
    evaluate_buyout,
    evaluate_trade,
    execute_buyout,
    execute_trade,
)

STATE_DIR = "data/state"

# -- Global state --
auction_state: AuctionState | None = None
model_params: dict | None = None
model_prices: dict[str, PricePrediction] | None = None
market_prices: dict[str, float] | None = None
market_info: MarketInfo | None = None
milp_solution = None
last_trade_eval = None
# Set by lifespan when startup degraded (backup used, fresh start despite a
# saved file, a backfill skipped). In memory rather than on disk because the
# claim is about THIS boot: a clean restart should not re-raise a fixed alarm.
_startup_warning: str | None = None


# The backfills take the state explicitly rather than reading the global: a
# candidate loaded off disk has to be validated BEFORE it is installed, or a
# half-backfilled corrupt state is already the live one by the time it raises.
def _backfill_nhl_teams(state: AuctionState, csv_path: str = "data/players.csv") -> None:
    """Fill in nhl_team for roster players loaded from old state files."""
    import csv
    nhl_lookup: dict[str, str] = {}
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            nhl_lookup[row["PLAYER"].strip()] = row["NHL TEAM"].strip()
    for team in state.teams.values():
        for p in team.keeper_players + team.acquired_players + team.minor_players:
            if not p.nhl_team:
                p.nhl_team = nhl_lookup.get(p.name, "")


def _backfill_team_metadata(
    state: AuctionState, teams_path: str = "data/fchl_teams.json"
) -> None:
    """Refresh team metadata (logos, colors) from fchl_teams.json for old state files."""
    with open(teams_path) as f:
        meta = json.load(f)
    for code, team in state.teams.items():
        if code in meta:
            team.logo = meta[code].get("logo", team.logo)


def _backfill_model_inputs(state: AuctionState) -> None:
    """Fill model inputs missing from pre-round-2 state files.

    pos_rank is recomputed over the remaining pool (approximate for
    mid-draft snapshots — drafted players no longer count — but only
    legacy snapshots ever hit this path). Goalie wins come from the
    projection stats file; unmatched goalies use the points fallback.
    """
    pool = state.available_players
    if any(p.pos_rank <= 0 for p in pool.values()):
        for name, rank in compute_pos_ranks(pool).items():
            pool[name].pos_rank = rank
    # Old snapshots stored fraction odds; the model expects percent. No real
    # pool has every team's Cup probability under 1%, so max < 1 = fractions.
    if pool and max(p.team_probability for p in pool.values()) < 1.0:
        for p in pool.values():
            p.team_probability *= 100.0
    goalie_wins = None
    for p in pool.values():
        if p.position == "G" and p.proj_wins is None:
            if goalie_wins is None:
                goalie_wins = load_goalie_wins()
            p.proj_wins = goalie_wins.get(p.name)


def _load_saved_state(path: str) -> AuctionState | None:
    """A usable state from `path`, or None with the reason logged.

    Broad `except` on purpose, and it is handling rather than swallowing: this
    runs at startup of a tool that may be four hours into a live auction, and
    every alternative to degrading is worse than degrading. The narrow net it
    replaces (JSONDecodeError/KeyError/ValueError) let an AttributeError from a
    shape mismatch stop the app booting outright. A genuinely missing data file
    still fails loudly — build_initial_state() reads the same CSVs and raises.

    Only the PARSE decides whether the file is usable. The backfills each get
    their own net below, because folding them in here made a broken fixup
    indistinguishable from a broken file: one raise from `_backfill_model_inputs`
    on a legacy snapshot renamed a byte-perfect draft `.corrupt` and started
    fresh, returning 200 with nothing on screen to say 150 picks had gone.
    """
    if not os.path.exists(path):
        return None  # absent is not an error worth logging
    try:
        with open(path) as f:
            state = AuctionState.from_json(f.read())
    except Exception as e:
        logging.warning("Cannot use %s: %s: %s", path, type(e).__name__, e)
        return None
    # Labelled by what the operator loses, not by function name: the banner is
    # read mid-auction by someone deciding whether to trust a number on screen,
    # and "_backfill_model_inputs raised" does not answer that. The log keeps
    # the identifier and the exception for whoever debugs it afterwards.
    for label, backfill in (
        ("NHL teams on rostered players", _backfill_nhl_teams),
        ("team logos", _backfill_team_metadata),
        ("price model inputs, so PRICES MAY BE WRONG", _backfill_model_inputs),
    ):
        try:
            backfill(state)
        except Exception as e:
            # Never fatal: not one of the three is load-bearing for the draft
            # record — they fill logos, nhl_team and legacy model inputs. A
            # missing logo must not cost the auction. ERROR rather than WARNING
            # because _backfill_model_inputs failing is not cosmetic: prices are
            # then computed off legacy pos_rank / team_probability / proj_wins.
            logging.error("Skipped %s on %s: %s: %s — the draft is intact, but "
                          "anything it fills is stale",
                          backfill.__name__, path, type(e).__name__, e)
            _warn_at_startup(
                f"Could not refresh {label} — those values are stale. "
                f"The draft itself loaded normally."
            )
    return state


def _warn_at_startup(message: str) -> None:
    """Record something the operator must see about how this boot went.

    Startup is not a request, so there is no toast to fire; `_context` hands
    this to base.html, which renders it as a banner until dismissed or reloaded.
    Accumulates rather than overwrites — a boot that both skipped a backfill and
    fell back to the backup has two things worth saying, and the second is not
    more important than the first.
    """
    global _startup_warning
    _startup_warning = f"{_startup_warning} {message}" if _startup_warning else message


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load data, compute prices, solve initial MILP on startup."""
    global auction_state, model_params, model_prices, _startup_warning
    os.makedirs(STATE_DIR, exist_ok=True)
    model_params = load_model_params()
    _startup_warning = None  # this boot's story, not the previous one's
    # Recovery ladder: current -> backup -> fresh. A fresh state is 150 picks
    # thrown away, so it is the last resort rather than the first fallback.
    saved_path = os.path.join(STATE_DIR, "auction_state.json")
    auction_state = _load_saved_state(saved_path)
    if auction_state is None and os.path.exists(saved_path):
        # Move the unusable file aside rather than leaving it for _save_state
        # to rotate over the good backup — one restart plus one click would
        # otherwise destroy both copies. Renaming rather than an in-memory
        # "don't trust the current file" flag because the outcome is then
        # visible on disk: you can see what happened without reading the log.
        try:
            os.replace(saved_path, saved_path + ".corrupt")
        except OSError as e:
            logging.warning("Could not set aside %s: %s", saved_path, e)
    if auction_state is None:
        auction_state = _load_saved_state(saved_path + ".backup")
        if auction_state is not None:
            logging.error("Recovered the draft from the backup file")
            _warn_at_startup(
                "Could not read the saved draft, so this is the backup copy — "
                "one save behind. Check the last pick is here and re-enter it "
                "if it is not."
            )
    if auction_state is None:
        if os.path.exists(saved_path + ".corrupt"):
            # The loud case: a state file existed and nothing could be salvaged.
            # Without this the app comes up looking like a normal fresh start.
            logging.error("No usable saved draft; starting fresh")
            _warn_at_startup(
                "Could not read the saved draft or its backup, so this is a "
                f"NEW auction. The unreadable file is at {saved_path}.corrupt — "
                "nothing has overwritten it."
            )
        auction_state = build_initial_state()
    model_prices = predict_all_prices(auction_state.available_players, model_params)
    _recompute()
    yield


app = FastAPI(title="FCHL Auction Manager", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/fchl_logos", StaticFiles(directory="fchl_logos"), name="logos")
app.mount("/nhl_logos", StaticFiles(directory="nhl_logos"), name="nhl_logos")
templates = Jinja2Templates(directory="templates")


buyout_indicators: dict[str, str] = {}  # player_name -> "buyout" or "keep"


# Marginal values for the current state epoch, keyed by player name. Cleared
# wholesale by _recompute() rather than versioned: a version counter has the
# same failure mode (forget to bump, serve a stale number as live bid advice)
# plus unbounded growth. An empty dict cannot serve a stale entry.
_marginal_cache: dict[str, float] = {}


def _marginal_value(player: Player) -> float:
    """Marginal value of `player` to BOT, cached for this state epoch.

    compute_marginal_value costs ~10 MILP solves (~780ms on a 704-player pool)
    and is pure in (roster, budget, pool, market prices). None of those move
    when the price or the bidder list changes — which is all /bid-check varies
    between calls — so a live auction otherwise spends a second per $0.1M
    increment re-deriving an identical number.
    """
    if player.name not in _marginal_cache:
        _marginal_cache[player.name] = compute_marginal_value(
            player,
            auction_state.teams[MY_TEAM],
            auction_state.available_players,
            market_prices,
        )
    return _marginal_cache[player.name]


# Counterfactuals for the current state epoch, on the same terms as
# _marginal_cache. Holds objects rather than floats, but an epoch ends at every
# assign, so in practice this is the one or two players bid on since the last
# sale — not worth a bound.
_counterfactual_cache: dict[str, CounterfactualResult] = {}


def _cf_price(player_name: str) -> float:
    """The price a counterfactual is conditioned on: the expected clearing price.

    Quantized to the $0.1M auction increment, for the same reason
    `_legal_salary` quantizes a typed one: market prices come off a log-normal
    and are essentially never legal prices — all 704 of them at reset — so
    forcing a player in at $9.5476934838794 plans the roster around a price the
    auction cannot produce, while the panel quotes "$9.5M". Small in dollars,
    but it is a number on screen that no bid can ever match.

    One definition, because the analysis and the sentence describing it are
    computed in different places and a drift between them would show a verdict
    ("Skip him at $9.5M") derived from a different number than the one quoted.
    """
    return round(market_prices.get(player_name, MIN_SALARY), 1)


def _counterfactual(player: Player) -> CounterfactualResult:
    """Roster with vs without `player` at the market price, cached this epoch.

    Two MILP solves (~200ms), pure in (roster, budget, pool, market prices) —
    the same inputs _recompute() replaces. Keyed on the MARKET price and not
    the live bid on purpose: that is what makes it epoch-stable, and re-solving
    per $0.1M increment would put a 200ms response back inside the window where
    it can land between mousedown and mouseup on Assign.
    """
    if player.name not in _counterfactual_cache:
        _counterfactual_cache[player.name] = generate_counterfactual(
            player,
            _cf_price(player.name),
            auction_state.teams[MY_TEAM],
            auction_state.available_players,
            market_prices,
        )
    return _counterfactual_cache[player.name]


def _counterfactual_context(player_name: str) -> dict | None:
    """Template variables needed by counterfactual.html.

    Returns None if the player isn't in the pool. Used by both /explain and the
    bid panel's lazy mount, mirroring _chart_context.
    """
    p = auction_state.available_players.get(player_name)
    if p is None:
        return None
    return {
        "counterfactual": _counterfactual(p),
        "cf_player": p,
        # The whole verdict is conditioned on this price — without it the panel
        # shows a points delta the reader can't judge. Already quantized, so
        # what is quoted is exactly what was solved.
        "cf_price": _cf_price(player_name),
    }


def _recompute():
    """After any state change: recompute market prices + re-solve MILP.

    Also invalidates any pending trade evaluation — a trade evaluated
    against the old world must not be executable against the new one. The
    cached marginal values and counterfactuals go for exactly the same reason:
    they are derived from the roster, budget and market prices this function is
    replacing. A stale counterfactual is the worse of the two, because it names
    specific alternative players — it would recommend drafting someone who has
    already been sold.
    """
    global market_prices, market_info, milp_solution, last_trade_eval
    last_trade_eval = None
    _marginal_cache.clear()
    _counterfactual_cache.clear()
    market_info = compute_market_ceiling(auction_state.teams)
    all_market = compute_all_market_prices(
        auction_state.available_players, model_prices, auction_state.teams,
    )
    market_prices = {name: price for name, (price, _) in all_market.items()}
    team = auction_state.teams[MY_TEAM]
    milp_solution = solve_optimal_roster(team, auction_state.available_players, market_prices)


def _log_transaction(
    player_name: str,
    position: str,
    team_code: str,
    salary: float,
    txn_type: str,
    *,
    model_price: float = 0,
    market_price: float = 0,
    timestamp: str | None = None,
) -> None:
    """Append a TransactionRecord to the auction state's transaction log."""
    auction_state.transaction_log.append(TransactionRecord(
        player_name=player_name,
        position=position,
        team_code=team_code,
        salary=salary,
        model_price=model_price,
        market_price=market_price,
        timestamp=timestamp or datetime.now().isoformat(),
        transaction_type=txn_type,
    ))


def _log_change(kind: str, team_code: str, description: str) -> None:
    """Append a ChangeRecord for a non-transaction state edit."""
    auction_state.change_log.append(ChangeRecord(
        timestamp=datetime.now().isoformat(),
        kind=kind,
        team_code=team_code,
        description=description,
    ))


def _recompute_buyout_indicators():
    """Recompute buyout indicators for BOT's roster. Called on-demand from /buyout-indicators."""
    global buyout_indicators
    from copy import deepcopy
    from config import BUYOUT_PENALTY_RATE

    team = auction_state.teams[MY_TEAM]
    current_pts = milp_solution.total_points if milp_solution and milp_solution.status == "Optimal" else 0
    buyout_indicators = {}
    # Active roster only, and only what's eligible. Roster because the dots
    # render into placeholders in the team table, which lists no minors — every
    # solve for a minors player would be thrown away. Eligible because a dot
    # beside a player who can't be bought out is worse than no dot: it reads as
    # a verdict on a decision that isn't available.
    for p in (q for q in team.roster_players if q.can_be_bought_out):
        try:
            clone = deepcopy(auction_state)
            bt = clone.teams[MY_TEAM]
            bt.remove_player(p.name)
            bt.penalties += p.salary * BUYOUT_PENALTY_RATE
            bo_sol = solve_optimal_roster(bt, auction_state.available_players, market_prices)
            buyout_indicators[p.name] = "buyout" if bo_sol.total_points > current_pts else "keep"
        except Exception:
            buyout_indicators[p.name] = "keep"


def _save_state():
    """Save auction state to disk atomically with backup rotation."""
    path = os.path.join(STATE_DIR, "auction_state.json")
    backup_path = path + ".backup"
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        f.write(auction_state.to_json())
    if os.path.exists(path):
        os.replace(path, backup_path)
    os.replace(tmp_path, path)


def _legal_salary(value: float) -> float:
    """Clamp to the legal range and quantize to the $0.1M auction increment.

    Every salary field auto-submits whatever was typed, and neither can stop a
    bad value on its own: the bid box's step= only drives the spinner, and it
    sits in a different form from Assign so its validity is never checked on
    submit. A typo'd 46 recorded as $46M corrupts the draft record loudly; a
    typo'd 2.55 corrupts it quietly — the CBA has no such price, and it strands
    $0.05M of the team's cap below the increment remaining_budget floors to.

    Round rather than floor: _floor_to_increment guards budget headroom, where
    the safe direction is down. A typo'd price has no safe direction, so take
    the nearest and let the caller's toast make the change visible.
    """
    return round(max(MIN_SALARY, min(value, MAX_SALARY)), 1)


def _cap_overages(*team_codes: str | None) -> list[str]:
    """Teams currently over the cap, worst first, as '<CODE> $X.XM over cap'.

    Trades are allowed to leave a team over — the league resolves those with
    buyouts (owner decision 2026-08-06) — so this reports rather than blocks.
    Without it an accidental over-cap trade returned the same green toast as a
    legal one, which is the whole gap being closed.

    An unknown, empty or None code is skipped by design: `TradeEvaluation.
    source_team_code` is `str | None` and /trade-execute passes it straight
    through, so the signature admits None rather than making every call site
    guard.
    """
    over: list[tuple[float, str]] = []
    for code in team_codes:
        team = auction_state.teams.get(code)
        if team is None:
            continue
        # Round before testing: total_salary sums many $0.1M values, so float
        # noise would otherwise report "$0.0M over cap" on an exactly-legal team.
        overage = round(team.total_salary - SALARY_CAP, 1)
        if overage > 0:
            over.append((overage, f"{code} ${overage:.1f}M over cap"))
    return [msg for _, msg in sorted(over, reverse=True)]


def _toast(response: HTMLResponse, message: str, toast_type: str = "info") -> HTMLResponse:
    """Attach a toast notification to an HTMX response via HX-Trigger header."""
    response.headers["HX-Trigger"] = json.dumps(
        {"showToast": {"message": message, "type": toast_type}}
    )
    return response


def _render(request: Request, template: str, extra: dict | None = None) -> HTMLResponse:
    """Render a template with the standard context plus any extras."""
    if extra and "request" in extra:
        # Caller already built a full context — use it directly
        return templates.TemplateResponse(request, template, extra)
    ctx = _context(request)
    if extra:
        ctx.update(extra)
    return templates.TemplateResponse(request, template, ctx)


def _context_viewing(request: Request, team_code: str) -> dict:
    """Standard context with the team panel pointed at `team_code`.

    Only `viewed_team` moves. `team` stays BOT deliberately: it also drives the
    Trade "I Give" list and the Buyout Analyzer, and moving it is exactly what
    leaked an opponent's players into BOT's trade form (fixed 2026-08-05,
    pinned by TestPanelContextIsolation).

    An unresolvable code leaves the default. That branch is reachable only
    through `/team-view/{code}` — the five editing endpoints all validate and
    return early — which is why the lookup lives here rather than being written
    out at both call sites: one copy, and `test_team_view_nonexistent` covers it.
    """
    ctx = _context(request)
    t = auction_state.teams.get(team_code)
    if t is not None:
        ctx["viewed_team"] = t
    return ctx


def _panels_viewing(request: Request, team_code: str) -> HTMLResponse:
    """all_panels.html with the team panel left on `team_code`.

    Roster edits are posted from whichever panel is open, so returning the
    default context snapped the view back to BOT after every Bench, salary fix
    or recall — auditing another team meant re-opening it each time.
    """
    return _render(
        request, "partials/all_panels.html", _context_viewing(request, team_code)
    )


def _context(request: Request) -> dict:
    """Build template context with all current state."""
    team = auction_state.teams[MY_TEAM]
    wanted = {p.name for p in milp_solution.roster} if milp_solution and milp_solution.status == "Optimal" else set()

    # Build bid limits for available players with points
    bid_limits = []
    for name, player in sorted(
        auction_state.available_players.items(),
        key=lambda x: -x[1].projected_points,
    ):
        mp = market_prices.get(name, MIN_SALARY)
        model_p = model_prices[name].expected_price if name in model_prices else MIN_SALARY
        bid_limits.append({
            "name": name,
            "position": player.position,
            "nhl_team": player.nhl_team,
            "projected_points": player.projected_points,
            "model_price": round(model_p, 1),
            "market_price": round(mp, 1),
            # market_price = min(model_price, ceiling), so the two are equal on
            # every row until opponent budgets drain. Flag the rows where the
            # ceiling actually cuts the price — the table shows one column and
            # only marks it when the market is doing something.
            "capped": round(mp, 1) < round(model_p, 1),
            "is_rfa": player.is_rfa,
            "in_optimal": name in wanted,
            "prior_fchl_team": player.prior_fchl_team,
        })

    # Compute projected standings (lightweight, no extra MILP solves)
    available_pool = sorted(
        auction_state.available_players.values(),
        key=lambda p: -p.projected_points,
    )
    projections = {}
    for code, t in auction_state.teams.items():
        current = t.current_roster_points
        if code == MY_TEAM and milp_solution and milp_solution.status == "Optimal":
            projected = int(milp_solution.total_points)
        else:
            # Only unfilled STARTER slots add points — bench scores nothing
            starter_slots = sum(t.roster_needs.values())
            if starter_slots > 0 and available_pool:
                affordable = [
                    p for p in available_pool
                    if market_prices.get(p.name, MIN_SALARY) <= t.physical_max_bid
                ]
                sample = min(len(affordable), starter_slots * 3)
                if sample > 0:
                    avg_pts = sum(p.projected_points for p in affordable[:sample]) / sample
                    projected = current + int(starter_slots * avg_pts)
                else:
                    projected = current
            else:
                projected = current
        projections[code] = {"current": current, "projected": projected}

    # Add rank (sorted by projected descending)
    for rank, (code, _) in enumerate(
        sorted(projections.items(), key=lambda x: -x[1]["projected"]), 1
    ):
        projections[code]["rank"] = rank

    default_bidders = ",".join(
        c for c in auction_state.nomination_order if not auction_state.teams[c].is_done
    )

    return {
        "request": request,
        "team": team,
        # The team whose roster is ON SCREEN, which is BOT until someone opens
        # another one. Split from `team` because `team` is also what the Trade
        # "I Give" list and the Buyout Analyzer act on: pointing that at the
        # team being viewed put an opponent's players in BOT's trade form
        # (fixed 2026-08-05). Only team_panel.html reads this.
        "viewed_team": team,
        "teams": auction_state.teams,
        "available_players": auction_state.available_players,
        "transaction_log": auction_state.transaction_log,
        "change_log": auction_state.change_log,
        "milp": milp_solution,
        "market_info": market_info,
        "bid_limits": bid_limits,
        "nomination_order": auction_state.nomination_order,
        "current_nominator": auction_state.current_nominator(),
        "my_team": MY_TEAM,
        "buyout_indicators": buyout_indicators,
        "market_prices": market_prices,
        "projections": projections,
        "default_bidders": default_bidders,
        # Read by base.html only, so it reaches the screen on a full page load
        # and not on htmx partial swaps — which is what keeps it on screen: a
        # panel swap replaces panels, never the banner above them.
        "startup_warning": _startup_warning,
    }


# -- Endpoints --

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Main page with all panels."""
    return _render(request, "index.html")


@app.post("/assign", response_class=HTMLResponse)
async def assign_player(
    request: Request,
    player: str = Form(...),
    team: str = Form(...),
    salary: float = Form(...),
):
    """Player drafted: assign to team at salary."""
    # Validate team
    if team not in auction_state.teams:
        return _toast(
            _render(request, "partials/all_panels.html"),
            f"Unknown team: {team}", "error",
        )

    # Validate BEFORE snapshotting — a failed assign must not leave a no-op
    # snapshot that eats the next undo
    if player not in auction_state.available_players:
        return _toast(
            _render(request, "partials/all_panels.html"),
            f"Player not found: {player}", "warning",
        )

    raw_salary = salary
    salary = _legal_salary(salary)
    clamp_note = (
        f" (salary adjusted from ${raw_salary:g}M)" if salary != raw_salary else ""
    )

    auction_state.save_snapshot()
    p = auction_state.available_players.pop(player)

    # RFA group conversion: RFA1→GROUP 2, RFA2→GROUP 3
    group = p.group
    if group == "RFA1":
        group = "2"
    elif group == "RFA2":
        group = "3"

    roster_player = PlayerOnRoster(
        name=p.name,
        position=p.position,
        group=group,
        salary=salary,
        projected_points=p.projected_points,
        nhl_team=p.nhl_team,
    )
    to_minors = auction_state.teams[team].add_acquired_player(roster_player)
    # The sale still succeeds — refusing it mid-auction would cost clicks at the
    # worst moment — but the operator has to be told the player went down.
    minors_note = " — roster full, sent to minors" if to_minors else ""

    # Capture prices before removing from dicts
    model_price_val = model_prices[player].expected_price if player in model_prices else 0.0
    market_price_val = market_prices.get(player, 0.0)
    model_prices.pop(player, None)

    # Log transaction
    _log_transaction(
        p.name, p.position, team, salary, "draft",
        model_price=model_price_val, market_price=market_price_val,
    )

    # A nomination turn is a combo: 1 RFA (silent bid) then 1 UFA (open
    # bid). The turn passes to the next team only when the UFA half sells —
    # advancing on the RFA too skipped every other team in the order.
    # Late-draft states with no RFAs left advance on every (UFA) sale.
    if not p.is_rfa:
        auction_state.advance_nomination()
    _recompute()
    _save_state()
    # Should essentially never fire: the league's commissioner software refuses
    # bids a team cannot afford. Kept because it costs one pass over a roster
    # next to a MILP solve, and leaving the busiest endpoint the silent one
    # would make the same warning elsewhere hard to trust.
    over = _cap_overages(team)
    return _toast(
        _render(request, "partials/all_panels.html"),
        f"{p.name} → {team} at ${salary}M{clamp_note}{minors_note}"
        + (f" — {'; '.join(over)}" if over else ""),
        "warning" if (to_minors or over) else "success",
    )


@app.post("/bid-check", response_class=HTMLResponse)
async def bid_check(
    request: Request,
    player: str = Form(...),
    bidders: str = Form(""),
    price: float = Form(0.5),
    highest_bidder: str = Form(""),
):
    """Live bidding: get bid recommendation."""
    p = auction_state.available_players.get(player)
    if p is None:
        ctx = _context(request)
        ctx["bid_advice"] = None
        return _render(request, "partials/bid_panel.html", ctx)

    # Use live ceiling from active bidders if provided
    bidder_list = [b.strip() for b in bidders.split(",") if b.strip()]
    winner = None
    if bidder_list:
        opponents = live_opponents(bidder_list, auction_state.teams)
        # One shared notion of "last bidder standing": the advisor's WIN verdict
        # and the template's Assign button both derive from `winner`. An empty
        # bidder list means no auction is running, so there is no winner and a
        # WIN verdict there would be nonsense.
        winner = bid_winner(bidder_list, auction_state.teams)
        live_ceil = compute_live_ceiling(bidder_list, auction_state.teams)
        live_info = MarketInfo(
            market_ceiling=live_ceil,
            highest_bidder=highest_bidder or None,
            highest_bid=live_ceil,
            second_bidder=None,
            demand_count=len(opponents),
            # Keep compute_market_ceiling's invariant: no demand means the
            # player goes for the floor. Nothing reads it off this path today,
            # but an inconsistent MarketInfo is a trap for whoever does next.
            floor_demand=not opponents,
        )
    else:
        live_info = market_info

    team = auction_state.teams[MY_TEAM]
    rec = compute_bid_recommendation(
        p, team, auction_state.available_players, market_prices, live_info, price,
        bot_uncontested=(winner == MY_TEAM),
        # Cached across price steps and bidder toggles — neither can change it.
        marginal_value=_marginal_value(p),
    )

    ctx = _context(request)
    ctx["bid_advice"] = rec
    ctx["bid_player"] = p
    ctx["bid_price"] = price
    ctx["active_bidders"] = bidder_list
    ctx["bid_winner"] = winner
    ctx["highest_bidder"] = highest_bidder
    chart = _chart_context(player)
    if chart is not None:
        ctx.update(chart)
    # Bid half only — returning the whole panel would replace the nomination
    # recommendations on every price change and bidder toggle.
    return _render(request, "partials/bid_panel.html", ctx)


@app.get("/nominate", response_class=HTMLResponse)
async def nominate(request: Request):
    """It's BOT's turn: get nomination recommendation."""
    model_expected = {name: pred.expected_price for name, pred in model_prices.items()}
    rfa_pick, ufa_pick = recommend_nomination(
        auction_state, market_prices, model_expected,
    )
    ctx = _context(request)
    ctx["rfa_pick"] = rfa_pick
    ctx["ufa_pick"] = ufa_pick
    # Nomination half only: this fires on a bare `n` keypress, and returning
    # the whole panel destroyed any in-flight bidding session.
    return _render(request, "partials/nomination_panel.html", ctx)


@app.get("/explain/{player_name}", response_class=HTMLResponse)
async def explain(request: Request, player_name: str, inline: bool = False):
    """Why not bid: counterfactual explanation.

    `inline=1` returns the body alone, for the bid panel's lazy mount; the
    default returns the standalone `#explanation` section the Available Players
    table's "?" links swap. Same analysis, two mount points — a query param
    rather than a second route, since only the wrapper differs.
    """
    template = (
        "partials/counterfactual.html" if inline else "partials/explanation.html"
    )
    ctx = _context(request)
    ctx["counterfactual"] = None
    cf = _counterfactual_context(player_name)
    if cf is not None:
        ctx.update(cf)
    return _render(request, template, ctx)


@app.post("/trade-evaluate", response_class=HTMLResponse)
async def trade_evaluate(request: Request):
    """Evaluate a proposed trade."""
    global last_trade_eval
    form = await request.form()

    give_names = form.getlist("give_player")
    receive_json = form.getlist("receive_player")
    source_team = (form.get("source_team") or "").strip() or None

    give = []
    for name in give_names:
        p = auction_state.teams[MY_TEAM].find_player(name)
        if p:
            give.append(PlayerTrade(p.name, p.position, p.salary, p.projected_points))

    receive = []
    for raw in receive_json:
        if raw.strip():
            try:
                data = json.loads(raw)
                receive.append(PlayerTrade(
                    name=data["name"],
                    position=data["position"],
                    salary=float(data["salary"]),
                    projected_points=int(data["projected_points"]),
                ))
            except (json.JSONDecodeError, KeyError):
                pass

    if give or receive:
        result = evaluate_trade(
            auction_state, give, receive, market_prices, source_team_code=source_team,
        )
        last_trade_eval = result
    else:
        result = None

    ctx = _context(request)
    ctx["trade_result"] = result
    return _render(request, "partials/trade_panel.html", ctx)


@app.post("/trade-execute", response_class=HTMLResponse)
async def trade_execute(request: Request, trade_id: str = Form("")):
    """Execute a previously evaluated trade."""
    global last_trade_eval
    if last_trade_eval is None:
        return _toast(
            _render(request, "partials/all_panels.html"),
            "No current trade evaluation — state changed since it was "
            "evaluated. Re-evaluate the trade.", "warning",
        )
    if trade_id != last_trade_eval.trade_id:
        return _toast(
            _render(request, "partials/all_panels.html"),
            "Stale trade — re-evaluate before executing.", "warning",
        )

    # Capture trade details before clearing
    trade_give = last_trade_eval.give
    trade_receive = last_trade_eval.receive
    source_team = last_trade_eval.source_team_code

    auction_state.save_snapshot()
    try:
        execute_trade(auction_state, trade_give, trade_receive, source_team_code=source_team)
    except ValueError as e:
        auction_state.restore_snapshot()
        last_trade_eval = None
        return _toast(
            _render(request, "partials/all_panels.html"),
            f"Trade failed: {e}", "error",
        )
    last_trade_eval = None

    # Log trade transactions for both teams (when source_team is known)
    now = datetime.now().isoformat()
    for p in trade_give:
        _log_transaction(p.name, p.position, MY_TEAM, p.salary, "trade_out", timestamp=now)
        if source_team:
            _log_transaction(p.name, p.position, source_team, p.salary, "trade_in", timestamp=now)
    for p in trade_receive:
        _log_transaction(p.name, p.position, MY_TEAM, p.salary, "trade_in", timestamp=now)
        if source_team:
            _log_transaction(p.name, p.position, source_team, p.salary, "trade_out", timestamp=now)

    # Recompute model prices for any newly available players
    global model_prices
    model_prices = predict_all_prices(auction_state.available_players, model_params)
    _recompute()
    _save_state()
    over = _cap_overages(MY_TEAM, source_team)
    return _toast(
        _render(request, "partials/all_panels.html"),
        "Trade executed" + (f" — {'; '.join(over)}" if over else ""),
        "warning" if over else "success",
    )


@app.get("/buyout-check/{player_name}", response_class=HTMLResponse)
async def buyout_check(request: Request, player_name: str):
    """Preview buyout impact."""
    try:
        result = evaluate_buyout(auction_state, player_name, market_prices)
    except ValueError as e:
        # Rendering an empty panel told the operator nothing. An ineligible
        # group is the common case now, and the reason IS the useful answer.
        ctx = _context(request)
        ctx["buyout_result"] = None
        return _toast(
            _render(request, "partials/buyout_panel.html", ctx),
            str(e), "error",
        )

    ctx = _context(request)
    ctx["buyout_result"] = result
    return _render(request, "partials/buyout_panel.html", ctx)


@app.post("/buyout", response_class=HTMLResponse)
async def buyout(request: Request, player: str = Form(...)):
    """Execute a buyout."""
    # Capture player info before execute_buyout removes them
    team = auction_state.teams[MY_TEAM]
    p = team.find_player(player)
    if p:
        bo_position = p.position
        bo_salary = p.salary

    auction_state.save_snapshot()
    try:
        execute_buyout(auction_state, player)
    except ValueError as e:
        auction_state.restore_snapshot()
        # Report the actual reason: this used to say "not found" for every
        # failure, so an ineligible-group refusal named the wrong problem.
        return _toast(
            _render(request, "partials/all_panels.html"),
            f"Buyout failed: {e}", "error",
        )

    # Log buyout transaction
    if p:
        _log_transaction(player, bo_position, MY_TEAM, bo_salary, "buyout")

    _recompute()
    _save_state()
    return _toast(
        _render(request, "partials/all_panels.html"),
        f"Bought out {player}", "success",
    )


@app.post("/team-done", response_class=HTMLResponse)
async def team_done(request: Request, team_code: str = Form(...)):
    """Toggle team as finished drafting."""
    t = auction_state.teams.get(team_code)
    if t is None:
        return _toast(
            _render(request, "partials/all_panels.html"),
            f"Unknown team: {team_code}", "error",
        )
    auction_state.save_snapshot()
    t.is_done = not t.is_done
    _log_change(
        "team-done",
        team_code,
        f"{team_code} marked as {'done' if t.is_done else 'still drafting'}",
    )
    _recompute()
    _save_state()
    return _render(request, "partials/all_panels.html")


@app.post("/undo", response_class=HTMLResponse)
async def undo(request: Request):
    """Restore previous snapshot, telling the user WHAT was undone."""
    pre_txn = list(auction_state.transaction_log)
    pre_chg = list(auction_state.change_log)

    if not auction_state.restore_snapshot():
        return _toast(
            _render(request, "partials/all_panels.html"),
            "Nothing to undo", "warning",
        )

    # Whichever log shrank names the reverted action; a silent undo made
    # the Ctrl+Z-while-typing footgun invisible
    message = "Undid last action"
    if len(auction_state.transaction_log) < len(pre_txn):
        t = pre_txn[-1]
        message = (
            f"Undid {t.transaction_type}: {t.player_name} → "
            f"{t.team_code} (${t.salary:.1f}M)"
        )
    elif len(auction_state.change_log) < len(pre_chg):
        message = f"Undid: {pre_chg[-1].description}"

    global model_prices
    model_prices = predict_all_prices(auction_state.available_players, model_params)
    _recompute()
    _save_state()
    return _toast(
        _render(request, "partials/all_panels.html"), message, "info",
    )


@app.get("/buyout-indicators", response_class=HTMLResponse)
async def buyout_indicators_endpoint(request: Request):
    """Compute buyout indicators lazily, loaded via HTMX after page render."""
    _recompute_buyout_indicators()
    ctx = _context(request)
    return _render(request, "partials/buyout_dots.html", ctx)


@app.post("/reset", response_class=HTMLResponse)
async def reset(request: Request):
    """Reset to fresh state from CSV data."""
    global auction_state, model_prices, _startup_warning
    # A deliberate fresh start answers whatever the banner was warning about —
    # leaving it up would have it read as a live alarm against a state the
    # operator just chose. Nothing here can re-degrade: build_initial_state
    # raises rather than half-loading.
    _startup_warning = None
    auction_state = build_initial_state()
    model_prices = predict_all_prices(auction_state.available_players, model_params)
    _recompute()
    _save_state()
    return _render(request, "partials/all_panels.html")


@app.post("/load-scenario", response_class=HTMLResponse)
async def load_scenario(request: Request, name: str = Form(...)):
    """Load a pre-baked test scenario."""
    import scenarios as _scenarios

    global auction_state, model_prices
    # Capture prior state so /undo can roll back the scenario load. Snapshots
    # live on AuctionState._snapshots, which gets wiped when we replace the
    # global — copy it onto the new state explicitly.
    prior = auction_state.to_json(include_snapshots=False)
    try:
        new_state = _scenarios.load(name)
    except KeyError:
        return _toast(
            _render(request, "partials/all_panels.html"),
            f"Unknown scenario: {name}", "error",
        )
    new_state._snapshots.append(prior)
    auction_state = new_state
    model_prices = predict_all_prices(auction_state.available_players, model_params)
    _recompute()
    _save_state()
    return _toast(
        _render(request, "partials/all_panels.html"),
        f"Loaded scenario: {name}", "success",
    )


_TWO_PI_SQRT = math.sqrt(2.0 * math.pi)


def _lognormal_pdf_path(
    log_mu: float,
    sigma: float,
    p_floor: float,
    scale_max: float,
    min_salary: float,
    n_points: int = 60,
    x_off: float = 10.0,
    chart_width: float = 380.0,
    y_axis: float = 75.0,
    max_height: float = 55.0,
) -> tuple[str, tuple[float, float, float, float] | None]:
    """Build SVG path for log-normal PDF curve and optional floor spike."""
    if sigma <= 0:
        return "", None

    scale = chart_width / scale_max
    x_start = max(min_salary, 0.01)
    x_end = scale_max
    step = (x_end - x_start) / n_points

    # Sample PDF values
    points: list[tuple[float, float]] = []
    peak_pdf = 0.0
    for i in range(n_points + 1):
        x = x_start + i * step
        ln_x = math.log(x)
        exponent = -((ln_x - log_mu) ** 2) / (2.0 * sigma * sigma)
        pdf = (1.0 / (x * sigma * _TWO_PI_SQRT)) * math.exp(exponent)
        pdf *= 1.0 - p_floor
        svg_x = x_off + x * scale
        points.append((svg_x, pdf))
        peak_pdf = max(peak_pdf, pdf)

    if not points or peak_pdf == 0:
        return "", None

    # Scale to pixel height
    h_scale = max_height / peak_pdf
    parts = [f"M {points[0][0]:.1f} {y_axis:.1f}"]
    for svg_x, pdf in points:
        parts.append(f"L {svg_x:.1f} {y_axis - pdf * h_scale:.1f}")
    parts.append(f"L {points[-1][0]:.1f} {y_axis:.1f} Z")
    curve_d = " ".join(parts)

    # Floor spike when p_floor is meaningful
    floor_bar = None
    if p_floor > 0.05:
        bar_x = x_off + min_salary * scale
        bar_h = p_floor * max_height
        bar_w = max(2.0, 0.1 * scale)
        floor_bar = (bar_x - bar_w / 2, y_axis - bar_h, bar_w, bar_h)

    return curve_d, floor_bar


def _chart_context(player_name: str) -> dict | None:
    """Build the template variables needed by player_chart.html.

    Returns None if the player isn't priceable (unknown or missing model
    prediction). Used by both /player-chart/{name} and /bid-check (which
    embeds the chart inline during an active auction).
    """
    p = auction_state.available_players.get(player_name)
    if p is None:
        return None
    pred = model_prices.get(player_name)
    if pred is None:
        return None
    mp = market_prices.get(player_name, MIN_SALARY)
    curve_d, floor_bar = _lognormal_pdf_path(
        log_mu=pred.log_mu,
        sigma=pred.sigma,
        p_floor=pred.p_floor,
        scale_max=MAX_SALARY,
        min_salary=MIN_SALARY,
    )
    return {
        "chart_player": p,
        "chart_data": pred,
        "chart_market_price": mp,
        "chart_scale_max": MAX_SALARY,
        "chart_curve_d": curve_d,
        "chart_floor_bar": floor_bar,
    }


@app.get("/player-chart/{player_name}", response_class=HTMLResponse)
async def player_chart(request: Request, player_name: str):
    """Show price model visualization for a player."""
    ctx = _context(request)
    chart = _chart_context(player_name)
    if chart is None:
        # The chart partial's own empty state, NOT explanation.html — that
        # rendered the entire Counterfactual panel, second id="explanation" and
        # all, into the chart slot. Reachable when a player leaves the pool
        # between the table rendering and the click (second tab, stale panel).
        ctx["chart_missing_name"] = player_name
    else:
        ctx.update(chart)
    return _render(request, "partials/player_chart.html", ctx)


@app.post("/set-nominator", response_class=HTMLResponse)
async def set_nominator(request: Request, team_code: str = Form(...)):
    """Override which team nominates next."""
    order = auction_state._effective_order()
    # Nomination half only, both paths — the nominator badge lives there, and
    # returning the whole panel wiped any in-flight bidding session.
    if team_code not in order:
        return _render(request, "partials/nomination_panel.html")
    auction_state.save_snapshot()
    auction_state.nomination_index = order.index(team_code)
    _save_state()
    return _render(request, "partials/nomination_panel.html")


@app.get("/team-view/{team_code}", response_class=HTMLResponse)
async def team_view(request: Request, team_code: str):
    """Render the team panel for the given team. Falls back to default
    (BOT) when the code is unknown so HTMX swaps still produce a valid panel."""
    return _render(
        request, "partials/team_panel.html", _context_viewing(request, team_code)
    )


@app.get("/team-players/{team_code}")
async def team_players(team_code: str):
    """Return JSON list of players on a team (for trade dropdown)."""
    t = auction_state.teams.get(team_code)
    if t is None:
        return []
    return [
        {
            "name": p.name,
            "position": p.position,
            "salary": p.salary,
            "projected_points": p.projected_points,
        }
        for p in t.roster_players
    ]


@app.post("/toggle-bench", response_class=HTMLResponse)
async def toggle_bench(
    request: Request,
    team_code: str = Form(...),
    player_name: str = Form(...),
):
    """Toggle a player between active and bench."""
    t = auction_state.teams.get(team_code)
    if t is None:
        return _toast(
            _render(request, "partials/all_panels.html"),
            f"Unknown team: {team_code}", "error",
        )
    p = t.find_player(player_name)
    if p is None:
        return _toast(
            _render(request, "partials/all_panels.html"),
            f"{player_name} not found on {team_code}", "warning",
        )
    # Snapshot like every other mutation — without it, undo after a bench
    # toggle silently reverts the PREVIOUS action instead
    auction_state.save_snapshot()
    p.is_bench = not p.is_bench
    _log_change(
        "toggle-bench",
        team_code,
        f"{player_name} → {'bench' if p.is_bench else 'active'}",
    )
    _recompute()
    _save_state()
    return _panels_viewing(request, team_code)


@app.post("/adjust-salary", response_class=HTMLResponse)
async def adjust_salary(
    request: Request,
    team_code: str = Form(...),
    player_name: str = Form(...),
    new_salary: float = Form(...),
):
    """Correct a player's salary (typo fix)."""
    t = auction_state.teams.get(team_code)
    if t is None:
        return _render(request, "partials/all_panels.html")
    # Validate before snapshotting: the input auto-submits on change, and a
    # player traded/bought out since render used to 500 here
    p = t.find_player(player_name)
    if p is None:
        return _toast(
            _render(request, "partials/all_panels.html"),
            f"{player_name} is no longer on {team_code}", "warning",
        )
    clamped = _legal_salary(new_salary)
    auction_state.save_snapshot()
    old_salary = p.salary
    t.adjust_salary(player_name, clamped)
    _log_change(
        "adjust-salary",
        team_code,
        f"{player_name}: ${old_salary:.1f}M → ${clamped:.1f}M",
    )
    _recompute()
    _save_state()
    response = _panels_viewing(request, team_code)
    # Two independent warnings can fire here, so collect rather than return on
    # the first: a fat-fingered figure is often both off-increment AND too big.
    notes: list[str] = []
    # Say so when the typed value wasn't recorded verbatim. This is the
    # typo-fix endpoint, so it sees the most fat-fingered input of any — and
    # silently storing something other than what was typed is the failure it
    # exists to correct. _log_change already formats to .1f, so before the
    # quantization the change log read "$2.5M" while the roster held 2.55.
    if clamped != new_salary:
        notes.append(
            f"{player_name} set to ${clamped}M (adjusted from ${new_salary:g}M)"
        )
    # _legal_salary clamps to MIN/MAX/increment but knows nothing about the cap,
    # so a typo'd correction could put a team over and still read as a success.
    over = _cap_overages(team_code)
    if over:
        # The cap note needs a subject. On its own it reads as a bare fact about
        # the team, with nothing tying it to the edit that just caused it.
        if not notes:
            notes.append(f"{player_name} set to ${clamped}M")
        notes.extend(over)
    if notes:
        return _toast(response, " — ".join(notes), "warning")
    return response


@app.post("/move-to-minors", response_class=HTMLResponse)
async def move_to_minors(
    request: Request,
    team_code: str = Form(...),
    player_name: str = Form(...),
):
    """Move a player from active roster to minors."""
    t = auction_state.teams.get(team_code)
    if t is None:
        return _render(request, "partials/all_panels.html")
    auction_state.save_snapshot()
    try:
        t.send_to_minors(player_name)
    except ValueError as e:
        auction_state.restore_snapshot()
        return _toast(
            _render(request, "partials/all_panels.html"),
            str(e), "error",
        )
    _log_change("move-to-minors", team_code, f"{player_name} → minors")
    _recompute()
    _save_state()
    return _panels_viewing(request, team_code)


@app.post("/move-to-roster", response_class=HTMLResponse)
async def move_to_roster(
    request: Request,
    team_code: str = Form(...),
    player_name: str = Form(...),
):
    """Recall a player from minors to the active roster."""
    t = auction_state.teams.get(team_code)
    if t is None:
        return _render(request, "partials/all_panels.html")
    auction_state.save_snapshot()
    try:
        t.recall_from_minors(player_name)
    except ValueError as e:
        auction_state.restore_snapshot()
        # Surface the real reason: this used to hardcode "not in minors", which
        # is an actively wrong explanation for a roster-capacity refusal.
        return _toast(
            _render(request, "partials/all_panels.html"), str(e), "error",
        )
    _log_change("move-to-roster", team_code, f"{player_name} → active")
    _recompute()
    _save_state()
    response = _panels_viewing(request, team_code)
    # A group A-E minor is cap-free while down and cap-counted the moment it is
    # recalled (PlayerOnRoster.counts_on_cap), and 145 of the 149 minors at reset
    # are group A-E — so this is the ordinary recall, not an edge case. It went
    # unreported because the endpoint had no toast at all on success.
    over = _cap_overages(team_code)
    if over:
        return _toast(
            response, f"{player_name} recalled — {'; '.join(over)}", "warning",
        )
    # Still silent on a legal recall: the panel re-render already shows the move,
    # and the warning is the only new information here.
    return response


@app.post("/trade-between", response_class=HTMLResponse)
async def trade_between(
    request: Request,
    team_a: str = Form(...),
    team_b: str = Form(...),
    players_from_a: str = Form(""),
    players_from_b: str = Form(""),
):
    """Execute a trade between two teams. Atomic: all names must resolve."""
    names_a = [n.strip() for n in players_from_a.split(",") if n.strip()]
    names_b = [n.strip() for n in players_from_b.split(",") if n.strip()]
    if not names_a and not names_b:
        return _toast(
            _render(request, "partials/all_panels.html"),
            "No players selected for trade", "warning",
        )
    if team_a == team_b:
        return _toast(
            _render(request, "partials/all_panels.html"),
            "Cannot trade a team with itself", "error",
        )
    ta = auction_state.teams.get(team_a)
    tb = auction_state.teams.get(team_b)
    if not ta or not tb:
        return _toast(
            _render(request, "partials/all_panels.html"),
            f"Unknown team: {team_a if not ta else team_b}", "error",
        )
    # Validate every name BEFORE mutating — a typo or stale roster must not
    # produce a silent one-sided trade
    missing = [n for n in names_a if ta.find_player(n) is None]
    missing += [n for n in names_b if tb.find_player(n) is None]
    if missing:
        return _toast(
            _render(request, "partials/all_panels.html"),
            f"Trade aborted — not on roster: {', '.join(missing)}", "error",
        )
    auction_state.save_snapshot()
    now = datetime.now().isoformat()
    # Remove from both rosters before adding to either: add_acquired_player
    # routes to the minors at 24, so a full team that gains before it loses
    # would send the incoming player down on an even swap.
    out_of_a = [ta.remove_player(name) for name in names_a]
    out_of_b = [tb.remove_player(name) for name in names_b]

    demoted: list[str] = []
    # Carry the destination TEAM, not just its code — recovering the object by
    # comparing codes would be correct only because team_a == team_b is rejected
    # 40 lines up, and that is the duplicate-predicate trap this repo keeps
    # finding.
    for source, dest, target, p in (
        [(team_a, team_b, tb, p) for p in out_of_a]
        + [(team_b, team_a, ta, p) for p in out_of_b]
    ):
        p.is_minor = False
        p.is_bench = False
        if target.add_acquired_player(p):
            demoted.append(f"{p.name} → {dest} minors")
        _log_transaction(p.name, p.position, f"{source}→{dest}", p.salary, "trade", timestamp=now)
    _recompute()
    _save_state()
    note = f" ({'; '.join(demoted)} — roster full)" if demoted else ""
    over = _cap_overages(team_a, team_b)
    if over:
        note += f" — {'; '.join(over)}"
    return _toast(
        # team_a, because this form only ever posts from team_a's own panel —
        # its hidden team_a field is that panel's code.
        _panels_viewing(request, team_a),
        f"Trade executed: {team_a} ↔ {team_b}{note}",
        # A demotion alone stays a success: that note is informational and
        # pre-dates this. Only going over the cap lifts the tier.
        "warning" if over else "success",
    )


@app.get("/state")
async def get_state():
    """JSON state dump for debugging."""
    return json.loads(auction_state.to_json(include_snapshots=False))

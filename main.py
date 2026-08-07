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

from config import MAX_SALARY, MIN_SALARY, MY_TEAM
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


def _backfill_nhl_teams(csv_path: str = "data/players.csv"):
    """Fill in nhl_team for roster players loaded from old state files."""
    import csv
    nhl_lookup: dict[str, str] = {}
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            nhl_lookup[row["PLAYER"].strip()] = row["NHL TEAM"].strip()
    for team in auction_state.teams.values():
        for p in team.keeper_players + team.acquired_players + team.minor_players:
            if not p.nhl_team:
                p.nhl_team = nhl_lookup.get(p.name, "")


def _backfill_team_metadata(teams_path: str = "data/fchl_teams.json"):
    """Refresh team metadata (logos, colors) from fchl_teams.json for old state files."""
    with open(teams_path) as f:
        meta = json.load(f)
    for code, team in auction_state.teams.items():
        if code in meta:
            team.logo = meta[code].get("logo", team.logo)


def _backfill_model_inputs():
    """Fill model inputs missing from pre-round-2 state files.

    pos_rank is recomputed over the remaining pool (approximate for
    mid-draft snapshots — drafted players no longer count — but only
    legacy snapshots ever hit this path). Goalie wins come from the
    projection stats file; unmatched goalies use the points fallback.
    """
    pool = auction_state.available_players
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load data, compute prices, solve initial MILP on startup."""
    global auction_state, model_params, model_prices
    os.makedirs(STATE_DIR, exist_ok=True)
    model_params = load_model_params()
    saved_path = os.path.join(STATE_DIR, "auction_state.json")
    if os.path.exists(saved_path):
        try:
            with open(saved_path) as f:
                auction_state = AuctionState.from_json(f.read())
            _backfill_nhl_teams()
            _backfill_team_metadata()
            _backfill_model_inputs()
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logging.warning("Corrupt state file, starting fresh: %s", e)
            auction_state = build_initial_state()
    else:
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


def _recompute():
    """After any state change: recompute market prices + re-solve MILP.

    Also invalidates any pending trade evaluation — a trade evaluated
    against the old world must not be executable against the new one. The
    cached marginal values go for exactly the same reason: they are derived
    from the roster, budget and market prices this function is replacing.
    """
    global market_prices, market_info, milp_solution, last_trade_eval
    last_trade_eval = None
    _marginal_cache.clear()
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
    return _toast(
        _render(request, "partials/all_panels.html"),
        f"{p.name} → {team} at ${salary}M{clamp_note}{minors_note}",
        "warning" if to_minors else "success",
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
async def explain(request: Request, player_name: str):
    """Why not bid: counterfactual explanation."""
    p = auction_state.available_players.get(player_name)
    if p is None:
        ctx = _context(request)
        ctx["counterfactual"] = None
        return _render(request, "partials/explanation.html", ctx)

    team = auction_state.teams[MY_TEAM]
    price = market_prices.get(player_name, MIN_SALARY)
    cf = generate_counterfactual(p, price, team, auction_state.available_players, market_prices)

    ctx = _context(request)
    ctx["counterfactual"] = cf
    ctx["cf_player"] = p
    # The whole verdict is conditioned on this price — without it the panel
    # shows a points delta the reader can't judge.
    ctx["cf_price"] = round(price, 1)
    return _render(request, "partials/explanation.html", ctx)


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
    return _toast(
        _render(request, "partials/all_panels.html"),
        "Trade executed", "success",
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
    global auction_state, model_prices
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
    chart = _chart_context(player_name)
    if chart is None:
        return _render(request, "partials/explanation.html")
    ctx = _context(request)
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
    ctx = _context(request)
    t = auction_state.teams.get(team_code)
    if t is not None:
        ctx["team"] = t
    return _render(request, "partials/team_panel.html", ctx)


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
    # Render with the default context. Overriding ctx["team"] to the edited
    # team leaked that team into every panel — `team` also drives the Trade
    # "I Give" dropdown and the buyout controls, so editing an opponent put
    # THEIR players in BOT's trade form. Consistent with /assign,
    # /move-to-minors and every other mutation, which already render as BOT.
    return _render(request, "partials/all_panels.html")


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
    # Default context — see the note in /toggle-bench on the ctx["team"] leak.
    response = _render(request, "partials/all_panels.html")
    # Say so when the typed value wasn't recorded verbatim. This is the
    # typo-fix endpoint, so it sees the most fat-fingered input of any — and
    # silently storing something other than what was typed is the failure it
    # exists to correct. _log_change already formats to .1f, so before the
    # quantization the change log read "$2.5M" while the roster held 2.55.
    if clamped != new_salary:
        return _toast(
            response,
            f"{player_name} set to ${clamped}M (adjusted from ${new_salary:g}M)",
            "warning",
        )
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
    return _render(request, "partials/all_panels.html")


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
    return _render(request, "partials/all_panels.html")


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
    return _toast(
        _render(request, "partials/all_panels.html"),
        f"Trade executed: {team_a} ↔ {team_b}{note}", "success",
    )


@app.get("/state")
async def get_state():
    """JSON state dump for debugging."""
    return json.loads(auction_state.to_json(include_snapshots=False))

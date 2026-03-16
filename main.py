"""FastAPI app: all HTTP endpoints for the auction simulator."""

from __future__ import annotations

import json
import os
from datetime import datetime

from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from config import MIN_SALARY, MY_TEAM
from data_loader import build_initial_state
from market import (
    MarketInfo,
    compute_all_market_prices,
    compute_live_ceiling,
    compute_market_ceiling,
)
from optimizer import (
    compute_bid_recommendation,
    generate_counterfactual,
    recommend_nomination,
    solve_optimal_roster,
)
from price_model import PricePrediction, load_model_params, predict_all_prices
from state import AuctionState, PlayerOnRoster, TransactionRecord
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load data, compute prices, solve initial MILP on startup."""
    global auction_state, model_params, model_prices
    os.makedirs(STATE_DIR, exist_ok=True)
    model_params = load_model_params()
    auction_state = build_initial_state()
    model_prices = predict_all_prices(auction_state.available_players, model_params)
    _recompute()
    yield


app = FastAPI(title="FCHL Auction Simulator", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/fchl_logos", StaticFiles(directory="fchl_logos"), name="logos")
templates = Jinja2Templates(directory="templates")


def _recompute():
    """After any state change: recompute market prices + re-solve MILP."""
    global market_prices, market_info, milp_solution
    market_info = compute_market_ceiling(auction_state.teams)
    all_market = compute_all_market_prices(
        auction_state.available_players, model_prices, auction_state.teams,
    )
    market_prices = {name: price for name, (price, _) in all_market.items()}
    team = auction_state.teams[MY_TEAM]
    milp_solution = solve_optimal_roster(team, auction_state.available_players, market_prices)


def _save_state():
    """Save auction state to disk."""
    path = os.path.join(STATE_DIR, "auction_state.json")
    with open(path, "w") as f:
        f.write(auction_state.to_json())


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
        if player.projected_points <= 0:
            continue
        mp = market_prices.get(name, MIN_SALARY)
        model_p = model_prices[name].expected_price if name in model_prices else MIN_SALARY
        bid_limits.append({
            "name": name,
            "position": player.position,
            "nhl_team": player.nhl_team,
            "projected_points": player.projected_points,
            "model_price": round(model_p, 1),
            "market_price": round(mp, 1),
            "is_rfa": player.is_rfa,
            "in_optimal": name in wanted,
            "prior_fchl_team": player.prior_fchl_team,
        })

    return {
        "request": request,
        "team": team,
        "teams": auction_state.teams,
        "available_players": auction_state.available_players,
        "transaction_log": auction_state.transaction_log,
        "milp": milp_solution,
        "market_info": market_info,
        "bid_limits": bid_limits,
        "nomination_order": auction_state.nomination_order,
        "current_nominator": auction_state.current_nominator(),
        "my_team": MY_TEAM,
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
    auction_state.save_snapshot()

    p = auction_state.available_players.pop(player, None)
    if p is None:
        return _render(request, "index.html")

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
    )
    auction_state.teams[team].add_acquired_player(roster_player)

    # Capture prices before removing from dicts
    model_price_val = model_prices[player].expected_price if player in model_prices else 0.0
    market_price_val = market_prices.get(player, 0.0)
    model_prices.pop(player, None)

    # Log transaction
    auction_state.transaction_log.append(TransactionRecord(
        player_name=p.name,
        position=p.position,
        team_code=team,
        salary=salary,
        model_price=model_price_val,
        market_price=market_price_val,
        timestamp=datetime.now().isoformat(),
        transaction_type="draft",
    ))

    _recompute()
    _save_state()
    return _render(request, "index.html")


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
        return _render(request, "partials/auction_control.html", ctx)

    # Use live ceiling from active bidders if provided
    bidder_list = [b.strip() for b in bidders.split(",") if b.strip()]
    if bidder_list:
        live_ceil = compute_live_ceiling(bidder_list, auction_state.teams, p.position)
        live_info = MarketInfo(
            market_ceiling=live_ceil,
            highest_bidder=highest_bidder or None,
            highest_bid=live_ceil,
            second_bidder=None,
            demand_count=len(bidder_list),
            floor_demand=False,
        )
    else:
        live_info = market_info

    team = auction_state.teams[MY_TEAM]
    rec = compute_bid_recommendation(
        p, team, auction_state.available_players, market_prices, live_info, price,
    )

    ctx = _context(request)
    ctx["bid_advice"] = rec
    ctx["bid_player"] = p
    ctx["bid_price"] = price
    return _render(request, "partials/auction_control.html", ctx)


@app.get("/nominate", response_class=HTMLResponse)
async def nominate(request: Request):
    """It's BOT's turn: get nomination recommendation."""
    model_expected = {name: pred.expected_price for name, pred in model_prices.items()}
    rfa_pick, ufa_pick = recommend_nomination(
        auction_state, market_prices, model_expected, market_info,
    )
    ctx = _context(request)
    ctx["rfa_pick"] = rfa_pick
    ctx["ufa_pick"] = ufa_pick
    return _render(request, "partials/nomination.html", ctx)


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
    return _render(request, "partials/explanation.html", ctx)


@app.post("/trade-evaluate", response_class=HTMLResponse)
async def trade_evaluate(request: Request):
    """Evaluate a proposed trade."""
    global last_trade_eval
    form = await request.form()

    give_names = form.getlist("give_player")
    receive_names = form.getlist("receive_name")
    receive_positions = form.getlist("receive_position")
    receive_salaries = form.getlist("receive_salary")
    receive_points = form.getlist("receive_points")

    give = []
    for name in give_names:
        p = auction_state.teams[MY_TEAM].find_player(name)
        if p:
            give.append(PlayerTrade(p.name, p.position, p.salary, p.projected_points))

    receive = []
    for i in range(len(receive_names)):
        if receive_names[i].strip():
            receive.append(PlayerTrade(
                name=receive_names[i].strip(),
                position=receive_positions[i].strip() if i < len(receive_positions) else "F",
                salary=float(receive_salaries[i]) if i < len(receive_salaries) else 0.5,
                projected_points=int(receive_points[i]) if i < len(receive_points) else 0,
            ))

    if give or receive:
        result = evaluate_trade(auction_state, give, receive, market_prices)
        last_trade_eval = result
    else:
        result = None

    ctx = _context(request)
    ctx["trade_result"] = result
    return _render(request, "partials/trade_panel.html", ctx)


@app.post("/trade-execute", response_class=HTMLResponse)
async def trade_execute(request: Request):
    """Execute a previously evaluated trade."""
    global last_trade_eval
    if last_trade_eval is None:
        return _render(request, "index.html")

    form = await request.form()
    buyout_names = form.getlist("buyout_player")

    auction_state.save_snapshot()
    execute_trade(auction_state, last_trade_eval.give, last_trade_eval.receive, buyout_names)
    last_trade_eval = None

    # Recompute model prices for any newly available players
    global model_prices
    model_prices = predict_all_prices(auction_state.available_players, model_params)
    _recompute()
    _save_state()
    return _render(request, "index.html")


@app.get("/buyout-check/{player_name}", response_class=HTMLResponse)
async def buyout_check(request: Request, player_name: str):
    """Preview buyout impact."""
    try:
        result = evaluate_buyout(auction_state, player_name, market_prices)
    except ValueError:
        result = None

    ctx = _context(request)
    ctx["buyout_result"] = result
    return _render(request, "partials/buyout_panel.html", ctx)


@app.post("/buyout", response_class=HTMLResponse)
async def buyout(request: Request, player: str = Form(...)):
    """Execute a buyout."""
    auction_state.save_snapshot()
    try:
        execute_buyout(auction_state, player)
    except ValueError:
        pass

    _recompute()
    _save_state()
    return _render(request, "index.html")


@app.post("/team-done", response_class=HTMLResponse)
async def team_done(request: Request, team_code: str = Form(...)):
    """Toggle team as finished drafting."""
    auction_state.save_snapshot()
    t = auction_state.teams.get(team_code)
    if t:
        t.is_done = not t.is_done
    _recompute()
    _save_state()
    return _render(request, "index.html")


@app.post("/undo", response_class=HTMLResponse)
async def undo(request: Request):
    """Restore previous snapshot."""
    auction_state.restore_snapshot()
    global model_prices
    model_prices = predict_all_prices(auction_state.available_players, model_params)
    _recompute()
    _save_state()
    return _render(request, "index.html")


@app.get("/state")
async def get_state():
    """JSON state dump for debugging."""
    return json.loads(auction_state.to_json(include_snapshots=False))


@app.post("/save")
async def save():
    """Force save current state."""
    _save_state()
    return {"status": "saved"}

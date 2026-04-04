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
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logging.warning("Corrupt state file, starting fresh: %s", e)
            auction_state = build_initial_state()
    else:
        auction_state = build_initial_state()
    model_prices = predict_all_prices(auction_state.available_players, model_params)
    _recompute()
    _recompute_buyout_indicators()
    yield


app = FastAPI(title="FCHL Auction Manager", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/fchl_logos", StaticFiles(directory="fchl_logos"), name="logos")
app.mount("/nhl_logos", StaticFiles(directory="nhl_logos"), name="nhl_logos")
templates = Jinja2Templates(directory="templates")


buyout_indicators: dict[str, str] = {}  # player_name -> "buyout" or "keep"


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


def _recompute_buyout_indicators():
    """Recompute buyout indicators for BOT's roster. Called after player assignment."""
    global buyout_indicators
    from copy import deepcopy
    from config import BUYOUT_PENALTY_RATE

    team = auction_state.teams[MY_TEAM]
    current_pts = milp_solution.total_points if milp_solution and milp_solution.status == "Optimal" else 0
    buyout_indicators = {}
    for p in team.roster_players:
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
        "buyout_indicators": buyout_indicators,
        "market_prices": market_prices,
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
        return _render(request, "partials/all_panels.html")

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
    _recompute_buyout_indicators()
    _save_state()
    return _render(request, "partials/all_panels.html")


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
    ctx["bid_highest"] = highest_bidder
    ctx["active_bidders"] = bidder_list
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
    receive_json = form.getlist("receive_player")

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
        return _render(request, "partials/all_panels.html")

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
    return _render(request, "partials/all_panels.html")


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
    return _render(request, "partials/all_panels.html")


@app.post("/team-done", response_class=HTMLResponse)
async def team_done(request: Request, team_code: str = Form(...)):
    """Toggle team as finished drafting."""
    auction_state.save_snapshot()
    t = auction_state.teams.get(team_code)
    if t:
        t.is_done = not t.is_done
    _recompute()
    _save_state()
    return _render(request, "partials/all_panels.html")


@app.post("/undo", response_class=HTMLResponse)
async def undo(request: Request):
    """Restore previous snapshot."""
    auction_state.restore_snapshot()
    global model_prices
    model_prices = predict_all_prices(auction_state.available_players, model_params)
    _recompute()
    _save_state()
    return _render(request, "partials/all_panels.html")


_TWO_PI_SQRT = math.sqrt(2.0 * math.pi)


def _lognormal_pdf_path(
    log_mu: float,
    sigma: float,
    p_floor: float,
    scale_max: float,
    min_salary: float,
    n_points: int = 60,
    x_off: float = 20.0,
    chart_width: float = 360.0,
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


@app.get("/player-chart/{player_name}", response_class=HTMLResponse)
async def player_chart(request: Request, player_name: str):
    """Show price model visualization for a player."""
    p = auction_state.available_players.get(player_name)
    if p is None:
        return _render(request, "partials/explanation.html")
    pred = model_prices.get(player_name)
    if pred is None:
        return _render(request, "partials/explanation.html")
    mp = market_prices.get(player_name, MIN_SALARY)
    scale_max = max(pred.ci_high, mp, pred.expected_price) * 1.2
    curve_d, floor_bar = _lognormal_pdf_path(
        log_mu=pred.log_mu,
        sigma=pred.sigma,
        p_floor=pred.p_floor,
        scale_max=max(scale_max, 1.0),
        min_salary=MIN_SALARY,
    )
    ctx = _context(request)
    ctx["chart_player"] = p
    ctx["chart_data"] = pred
    ctx["chart_market_price"] = mp
    ctx["chart_scale_max"] = max(scale_max, 1.0)
    ctx["chart_curve_d"] = curve_d
    ctx["chart_floor_bar"] = floor_bar
    return _render(request, "partials/player_chart.html", ctx)


@app.post("/set-nominator", response_class=HTMLResponse)
async def set_nominator(request: Request, team_code: str = Form(...)):
    """Override which team nominates next."""
    order = auction_state._effective_order()
    if team_code not in order:
        return _render(request, "partials/nomination.html")
    auction_state.save_snapshot()
    auction_state.nomination_index = order.index(team_code)
    _save_state()
    return _render(request, "partials/nomination.html")


@app.get("/team-view/{team_code}", response_class=HTMLResponse)
async def team_view(request: Request, team_code: str):
    """View another team's roster details."""
    t = auction_state.teams.get(team_code)
    if t is None:
        return _render(request, "partials/roster_panel.html")
    ctx = _context(request)
    ctx["view_team"] = t
    return _render(request, "partials/team_detail.html", ctx)


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
        return _render(request, "partials/all_panels.html")
    p = t.find_player(player_name)
    if p:
        p.is_bench = not p.is_bench
    _save_state()
    ctx = _context(request)
    ctx["view_team"] = t
    return _render(request, "partials/team_detail.html", ctx)


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
    clamped = max(MIN_SALARY, min(new_salary, MAX_SALARY))
    auction_state.save_snapshot()
    t.adjust_salary(player_name, clamped)
    _recompute()
    _recompute_buyout_indicators()
    _save_state()
    ctx = _context(request)
    ctx["view_team"] = t
    return _render(request, "partials/team_detail.html", ctx)


@app.post("/trade-between", response_class=HTMLResponse)
async def trade_between(
    request: Request,
    team_a: str = Form(...),
    team_b: str = Form(...),
    players_from_a: str = Form(""),
    players_from_b: str = Form(""),
):
    """Execute a trade between two non-BOT teams."""
    names_a = [n.strip() for n in players_from_a.split(",") if n.strip()]
    names_b = [n.strip() for n in players_from_b.split(",") if n.strip()]
    if not names_a and not names_b:
        return _render(request, "partials/all_panels.html")
    ta = auction_state.teams.get(team_a)
    tb = auction_state.teams.get(team_b)
    if not ta or not tb:
        return _render(request, "partials/all_panels.html")
    auction_state.save_snapshot()
    for name in names_a:
        try:
            p = ta.remove_player(name)
            p.is_minor = False
            tb.add_acquired_player(p)
        except ValueError:
            pass
    for name in names_b:
        try:
            p = tb.remove_player(name)
            p.is_minor = False
            ta.add_acquired_player(p)
        except ValueError:
            pass
    _recompute()
    _recompute_buyout_indicators()
    _save_state()
    return _render(request, "partials/all_panels.html")


@app.get("/state")
async def get_state():
    """JSON state dump for debugging."""
    return json.loads(auction_state.to_json(include_snapshots=False))

# FCHL Auction Simulator

A live auction draft tool for an 11-team fantasy hockey league. During a multi-hour, 150+ pick auction, the simulator tracks all teams, computes market-adjusted bid limits, recommends nominations, provides real-time bidding advice, evaluates trades and buyouts on the fly, and recalculates the ideal roster after every transaction.

## Stack

**FastAPI** + **HTMX** + **Jinja2** + **PuLP** (MILP solver)

## Quick Start

```bash
pip install -r requirements.txt
uvicorn main:app --reload
# Opens at http://localhost:8000
```

## How It Works

### Three-Layer Pricing Pipeline

```
price_model.py  ──→  market.py  ──→  optimizer.py
(Layer 1)            (Layer 2)       (Layer 3)
Historical           Market          Decision
prediction           reality         engine
```

1. **Model price** — Historical two-stage log-normal model trained on 8 seasons of auction data
2. **Market price** — Adjusts predictions using real-time budget visibility across all 11 teams
3. **Bid recommendation** — MILP optimizer plans the optimal roster and computes marginal value per player

### Draft-Day Modes

| Mode | Trigger | Output |
|------|---------|--------|
| Player drafted | Someone wins a pick | All panels update with new market state |
| My nomination | It's BOT's turn | RFA + UFA recommendation with reasoning |
| Active bidding | Bidding is live | BID / CAUTION / DROP OUT advice |
| Trade offer | Someone proposes a trade | Point impact, cap impact, buyout options |
| Buyout check | Considering a buyout | Cap freed vs penalty, new optimal roster |

## Architecture

```
Browser (HTMX)              FastAPI Server                    Engine
┌─────────────────┐     ┌─────────────────────┐    ┌──────────────────────┐
│ Auction control  │───>│ POST /assign        │───>│ price_model.py       │
│ Bidding advisor  │<───│ POST /bid-check     │    │       ↓              │
│ Nomination helper│    │ GET  /nominate      │    │ market.py            │
│ Trade evaluator  │    │ POST /trade-eval    │    │       ↓              │
│ My team view     │    │ POST /buyout-check  │    │ optimizer.py         │
│ League dashboard │    │ POST /team-done     │    │       ↓              │
└─────────────────┘     │ POST /undo          │    │ trade.py             │
        ▲               └─────────────────────┘    └──────────────────────┘
        │                        │
        │                        ▼
   HTMX partial              AuctionState
   HTML swaps                (JSON on disk)
```

## Data Files

| File | Location | Purpose |
|------|----------|---------|
| `fchl_teams.json` | Repo root | Team metadata, nomination order, penalties |
| `team_odds.json` | Repo root | Stanley Cup odds by NHL team |
| `keepers.json` | `data/` | All 11 teams' keeper rosters + salaries |
| `biddable_players.csv` | `data/` | All players available at auction |
| `model_params.json` | `data/` | Price model coefficients (from pricer repo) |

## Development

```bash
# Run tests
pytest tests/ -v

# Run specific module tests
pytest tests/test_market.py -v
```

See [CLAUDE.md](CLAUDE.md) for full project spec, module docs, and coding conventions.

## League Rules (Summary)

- **Salary cap**: $56.8M per team
- **Roster**: 24 players (14F + 7D + 3G minimum)
- **Bidding**: $0.1M increments, $0.5M floor, $11.4M ceiling
- **UFA**: Open circular bidding, drop out is permanent
- **RFA**: Secret bids, prior team has right of first refusal
- **Nominations**: Snake draft order, 1 RFA + 1 UFA per turn
- **Buyouts**: Player removed, 50% salary penalty on cap
- **Trades**: Allowed during auction breaks

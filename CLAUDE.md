# FCHL Auction Manager

## Project overview

A live auction draft tool for an 11-team fantasy hockey league. During a multi-hour, 150+ pick auction, the simulator tracks all teams, computes market-adjusted bid limits, recommends nominations, provides real-time bidding advice, evaluates trades and buyouts on the fly, and recalculates the ideal roster after every transaction.

**Stack**: FastAPI + HTMX + Jinja2 + PuLP (MILP solver)

## Quick start

```bash
pip install -r requirements.txt
uvicorn main:app --reload
# Opens at http://localhost:8000
```

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

### Pricing pipeline (critical concept)

Three layers, each adding real-world context:

```
price_model.py  ──→  market.py  ──→  optimizer.py
(Layer 1)            (Layer 2)       (Layer 3)
Historical           Market          Decision
prediction           reality         engine
```

**Layer 1 — Model price** (`price_model.py`): What the historical model says a player typically sells for. Two-stage per-position log-normal model trained on 8 seasons of data. A starting point — a prediction in a vacuum.

**Layer 2 — Market price** (`market.py`): Adjusts model prices using real-time auction state. Computes market ceilings from each opponent's exact remaining budget, roster needs, and minimum reserve requirements. We have perfect budget visibility during the draft, so these calculations are precise. Teams marked as "done" are excluded from market calculations.

**Layer 3 — Bid recommendation** (`optimizer.py`): Uses market-adjusted prices in the MILP to plan the optimal roster. Computes BOT's max bid as the marginal value of each player. Final bid recommendation:

```
recommended_bid = min(marginal_value, market_ceiling + 0.1, spendable_budget)
```

**Critical rule**: The bid recommendation must NEVER exceed the market ceiling. If no opponent can bid above $5.5M, BOT's max recommendation is $5.6M — regardless of what the model or marginal value says.

## File structure

```
fchl-auction-simulator/
├── main.py                      # FastAPI app, all HTTP endpoints
├── optimizer.py                 # MILP solver, bid calculator, nomination engine
├── market.py                    # Market ceiling + adjusted price calculations
├── price_model.py               # Two-stage log-normal price predictions
├── trade.py                     # Trade evaluator + buyout analyzer
├── state.py                     # AuctionState dataclass, serialization, undo
├── data_loader.py               # Startup: load CSVs/JSONs, build initial state
├── config.py                    # League constants (cap, roster sizes, etc.)
├── templates/
│   ├── base.html                # Page shell: HTMX script, CSS, layout grid
│   ├── index.html               # Main layout
│   ├── partials/
│   │   ├── auction_control.html # Assign form + live bidding advisor
│   │   ├── nomination.html      # "It's my turn" recommendation panel
│   │   ├── roster_panel.html    # BOT's current roster + target roster
│   │   ├── bid_limits.html      # All available players with max bids
│   │   ├── league_state.html    # All 11 teams: budget, roster, needs
│   │   ├── explanation.html     # "Why not bid" counterfactual display
│   │   ├── trade_panel.html     # Trade input + evaluation display
│   │   ├── buyout_panel.html    # Buyout impact analysis
│   │   └── transaction_log.html # Recent picks with deviation tracking
├── static/
│   ├── style.css                # Layout, dark mode, auction-day optimized
│   └── shortcuts.js             # Keyboard shortcuts (Ctrl+Z undo, etc.)
├── fchl_logos/                   # Team logo GIFs (0.gif=league, 1-11=teams)
├── data/
│   ├── players.csv              # All players: keepers, biddable, and minors
│   ├── fchl_teams.json          # Team metadata: order, penalties, names
│   ├── team_odds.json           # Stanley Cup odds by NHL team
│   ├── model_params.json        # Price model coefficients (from pricer repo)
│   └── state/                   # Auto-saved auction state snapshots
├── tests/
│   ├── test_price_model.py
│   ├── test_market.py
│   ├── test_optimizer.py
│   ├── test_bid_calculator.py
│   ├── test_nomination.py
│   ├── test_trade.py
│   ├── test_state.py
│   └── test_endpoints.py
├── requirements.txt
├── CLAUDE.md                    # This file
└── README.md
```

## Core modules

### config.py

League constants. Change these if league rules change.

```python
SALARY_CAP = 56.8          # $56.8M upper limit per team
MIN_SALARY = 0.5           # $0.5M minimum player salary
MAX_SALARY = 11.4          # $11.4M maximum (20% of cap)
SALARY_INCREMENT = 0.1     # $100K bidding increments
ROSTER_SIZE = 24           # Total active roster
PLAYING_ROSTER = 20        # 12F + 6D + 2G
BENCH_SIZE = 4             # Any position
MIN_FORWARDS = 14          # Minimum F on active roster
MIN_DEFENSE = 7            # Minimum D on active roster
MIN_GOALIES = 3            # Minimum G on active roster
STARTING_FORWARDS = 12     # F on playing roster
STARTING_DEFENSE = 6       # D on playing roster
STARTING_GOALIES = 2       # G on playing roster
NUM_TEAMS = 11
MY_TEAM = "BOT"            # The team we're optimizing for
BUYOUT_PENALTY_RATE = 0.5  # 50% of salary stays as cap penalty
```

### price_model.py

Layer 1. Historical price prediction.

Two-stage per-position model:

- **Stage 1**: Logistic regression → P(player sells at $0.5M floor)
- **Stage 2**: OLS on log(salary) → price distribution for above-floor players

Features: projected_points, projected_points², team_probability, is_rfa
Output: expected price, median, P(floor), sigma, confidence intervals

Parameters from `data/model_params.json`.

**Known limitations** (addressed by Layer 2):

- Static: doesn't adjust for budget depletion
- No positional scarcity awareness
- Goalie CIs are wide (R²=0.61)

**Potential improvements to explore**:

- Goalie features: games played, save pct, team defense
- Auction position effect: early picks sell higher
- Non-linear points × team_probability interaction

### market.py

Layer 2. Market-adjusted prices using exact real-time budget data.

**Core concepts**:

1. **Opponent physical max**: absolute ceiling any team can bid.

   ```
   physical_max = remaining_budget - (remaining_spots_after × MIN_SALARY)
   capped at MAX_SALARY
   ```

   Teams marked as `is_done` are excluded entirely.

2. **Market ceiling**: highest the bidding can realistically reach for a player.

   ```
   ceiling = second-highest physical_max among all active (non-done) opponents
   ```

   Position-agnostic — any team can bid on any player (extras go to bench or minors). Second-highest because auction price is set when second-to-last bidder drops out.

3. **Market-adjusted price**: what the MILP should use for roster planning.

   ```
   market_price = min(model_price, market_ceiling)
   ```

4. **Demand count**: active (non-done) teams that can afford to bid. Zero demand (all opponents done) = floor price.

**Key functions**:

```python
def compute_opponent_ceiling(team: TeamState) -> float | None
def compute_market_ceiling(all_teams: dict[str, TeamState], exclude_team: str = MY_TEAM) -> MarketInfo
def compute_market_price(model_price: float, market_info: MarketInfo) -> float
def compute_all_market_prices(players, model_prices, all_teams) -> dict
def compute_live_ceiling(active_bidders: list[str], teams: dict[str, TeamState], position: str) -> float
```

During live bidding (Mode 3), the market layer uses the specific active bidders reported by the user to narrow the ceiling further.

### optimizer.py

Layer 3. Uses market-adjusted prices for all decisions.

**1. Team Builder (MILP)**

```
Maximize:  Σ (projected_points[i] × x[i])
Subject to:
  Σ (market_price[i] × x[i]) ≤ spendable_budget    ← market prices, not model
  Σ x[i] where pos=F  = F_spots_needed
  Σ x[i] where pos=D  = D_spots_needed
  Σ x[i] where pos=G  = G_spots_needed
  x[i] ∈ {0, 1}
```

**2. Bid Calculator**
Binary search: find salary where "roster with P" = "roster without P" in points. That's marginal value.

```
max_bid = min(marginal_value, market_ceiling + SALARY_INCREMENT, spendable_budget)
```

**3. Nomination Engine**
Strategies: target, drain, depth. Considers RFA+UFA combo pairs per league rules.

**4. Counterfactual Generator**
When system says "don't bid", shows side-by-side: roster WITH player vs WITHOUT, highlighting the alternatives and budget freed up.

### trade.py

Trade evaluator and buyout analyzer. Both work by solving the MILP on hypothetical states.

**Trade evaluation**:

```python
def evaluate_trade(
    state: AuctionState,
    give: list[PlayerTrade],       # Players BOT sends away
    receive: list[PlayerTrade],    # Players BOT receives
    auto_check_buyouts: bool = True
) -> TradeEvaluation:
    """
    Compare current optimal roster vs optimal roster after trade.
    If auto_check_buyouts is True, also test buying out each received player
    and report whichever option (keep vs buyout) produces a better roster.
    """
```

The trade evaluator produces:

- Current MILP solution (total projected points, roster, cap)
- Post-trade MILP solution (same)
- Delta: points gained/lost, cap space gained/lost
- For each received player: keep vs buyout comparison
- Recommendation: accept or decline, with reasoning

**Buyout analysis**:

```python
def evaluate_buyout(
    state: AuctionState,
    player_name: str
) -> BuyoutEvaluation:
    """
    Compare current optimal roster vs roster with player bought out.
    Buyout removes the player but adds a penalty of 50% of their salary.
    """
```

A buyout removes the player and their salary, but adds a penalty equal to 50% of salary (BUYOUT_PENALTY_RATE × salary) to the team's cap. The net cap space freed is salary × (1 - BUYOUT_PENALTY_RATE) = 50% of salary. The MILP then re-solves: can it find a better roster with that freed cap space than the original roster had with the bought-out player?

**Combined trade + buyout**:
When evaluating a trade with `auto_check_buyouts=True`, for each received player, the evaluator tests three scenarios:

1. Keep all received players
2. Buy out each received player individually (keep the rest)
3. Buy out combinations if multiple received players

It reports the best scenario. Example output:

```
Trade: Give Shea Theodore ($3.2M), Receive Star ($6M) + Dud ($3M)
Option A: Keep both → +42 pts, -$5.8M cap
Option B: Keep Star, buyout Dud → +42 pts, -$4.3M cap (Dud penalty: $1.5M)
Recommendation: Option B — same points, saves $1.5M
```

### state.py

`AuctionState` dataclass. Serializes to JSON after every action. Snapshot stack for undo (last 50).

```python
@dataclass
class TeamState:
    code: str
    name: str
    keeper_players: list[PlayerOnRoster]
    acquired_players: list[PlayerOnRoster]
    penalties: float                          # Includes buyout penalties
    is_done: bool = False                     # Marked as finished drafting

    @property
    def total_salary(self) -> float: ...
    @property
    def remaining_budget(self) -> float: ...      # SALARY_CAP - total_salary - penalties
    @property
    def roster_needs(self) -> dict[str, int]: ... # F/D/G spots to fill
    @property
    def total_spots_remaining(self) -> int: ...
    @property
    def min_budget_reserved(self) -> float: ...   # spots × MIN_SALARY
    @property
    def spendable_budget(self) -> float: ...      # remaining - reserved
    @property
    def physical_max_bid(self) -> float: ...      # most they can bid on one player
```

When `is_done = True`:

- Team is excluded from market ceiling calculations (their budget doesn't count)
- Team's roster needs are excluded from demand counts
- Team is removed from nomination order
- Team's remaining budget shows as "inactive" on dashboard

### data_loader.py

Startup pipeline:

1. Load `data/fchl_teams.json` → team metadata, nomination order, penalties
2. Load `data/players.csv` → all players (keepers, biddable, minors), derive rosters
3. Load `data/model_params.json` → price model coefficients
4. Load `data/team_odds.json` → Stanley Cup odds
5. Layer 1: compute model prices for biddable players
6. Layer 2: compute initial market prices (full budgets)
7. Build initial `AuctionState`

### main.py

| Endpoint                 | Method | Purpose                                                |
| ------------------------ | ------ | ------------------------------------------------------ |
| `/`                      | GET    | Main page (full HTML)                                  |
| `/assign`                | POST   | Player drafted: {player, team, salary}                 |
| `/bid-check`             | POST   | Live bidding: {player, bidders, price, highest_bidder} |
| `/nominate`              | GET    | "It's my turn" → recommendation                        |
| `/explain/{player}`      | GET    | "Why not bid" counterfactual                           |
| `/trade-evaluate`        | POST   | Evaluate a proposed trade                              |
| `/trade-execute`         | POST   | Execute a previously evaluated trade                   |
| `/buyout-check/{player}` | GET    | Preview buyout impact                                  |
| `/buyout`                | POST   | Execute a buyout                                       |
| `/team-done`             | POST   | Toggle team as finished drafting                       |
| `/undo`                  | POST   | Restore previous snapshot                              |
| `/state`                 | GET    | JSON state dump (debug)                                |
| `/save`                  | POST   | Force save                                             |

All state-modifying endpoints trigger: update state → recompute market prices → re-solve MILP → save snapshot → return HTML partials.

## Draft-day modes

### Mode 1: "Player X was just drafted"

Input: player, team, salary. Output: all panels update.

### Mode 2: "It's my turn to nominate"

Input: click button. Output: RFA pick (optional) + UFA pick with reasoning.
Per league rules: 1 RFA (secret bid) + 1 UFA (open bid) per turn.

### Mode 3: "Active bidding on Player X"

Input: player, active bidders, current price, highest bidder.
Output: BID / CAUTION / DROP OUT + reasoning. If DROP: counterfactual shown.

### Mode 4: "Someone offered me a trade"

Input: players BOT gives, players BOT receives.
Output: point impact, cap impact, buyout options, accept/decline recommendation.

### Mode 5: "Should I buy out Player X?"

Input: player name (must be on BOT's roster).
Output: cap space freed, penalty incurred, new optimal roster comparison.

## Data file formats

### players.csv

Single source for all players in the system: keepers, auction-eligible, and minor leaguers.

```csv
PLAYER,POS,GROUP,STATUS,FCHL TEAM,NHL TEAM,AGE,SALARY,BID,PTS
Nikita Kucherov,F,3,START,LGN,TBL,31,8.5,0,144
Connor McDavid,F,RFA2,,RFA,EDM,27,11.4,0,132
Artemi Panarin,F,3,,UFA,NYR,32,7.3,0,120
Connor Ingram,G,3,MINOR,BOT,UTA,27,0.5,0,30
Brandt Clarke,D,A,MINOR,SRL,LAK,21,0.5,0,25
```

**Column meanings**:

| Column | Description |
|--------|-------------|
| `PLAYER` | Player name |
| `POS` | Position: F, D, or G |
| `GROUP` | Contract group: 2, 3, C, RFA1, RFA2, A, B, D, E |
| `STATUS` | `START` = keeper on active roster, `MINOR` = minor league, blank = auction-eligible |
| `FCHL TEAM` | Team code if on a team, `RFA` if restricted free agent, `UFA` if unrestricted |
| `NHL TEAM` | NHL team |
| `AGE` | Player age |
| `SALARY` | Current salary in millions. Ignore for `FCHL TEAM = UFA` (stale from last year) |
| `BID` | Always 0 in the source file (populated during auction) |
| `PTS` | Projected fantasy points |
| `PRIOR FCHL TEAM` | For RFAs only: which FCHL team previously held this player (for ROFR). Blank for non-RFAs |

**Deriving player categories from the CSV**:

- **Keepers** (on a team's active roster): `STATUS = START` and `FCHL TEAM` is a team code (not UFA/RFA)
- **Biddable at auction**: `FCHL TEAM = UFA` or `FCHL TEAM = RFA` (no STATUS, or STATUS is blank)
- **Minor league**: `STATUS = MINOR`

**RFA detection** (for price model `is_rfa` feature):
- Only applies to biddable players. In practice, only `RFA1` and `RFA2` appear in the biddable pool.
- `GROUP` in (`RFA1`, `RFA2`) → RFA (`is_rfa=1`). They are equivalent for auction purposes.
- `GROUP = 3` → UFA (`is_rfa=0`)
- `GROUP` 2 and C are keeper/minor contract types — they never appear in the biddable pool.

**RFA group conversion on signing**: When an RFA is signed at auction, their group changes:
- `RFA1` → becomes `GROUP 2`
- `RFA2` → becomes `GROUP 3`

This matters for salary cap rules if they are later sent to minors.

**Keeper/minor contract groups and salary rules**:

| GROUP | Appears in biddable pool? | Salary on cap (START)? | Salary on cap (MINOR)? |
|-------|--------------------------|------------------------|------------------------|
| `2`   | No                       | Yes                    | Yes                    |
| `3`   | Yes (as UFA)             | Yes                    | Yes                    |
| `RFA1`, `RFA2` | Yes (as RFA)    | N/A                   | N/A                    |
| `A`, `B`, `C`, `D`, `E` | No     | Yes                   | No                     |

**Minor league rules**:
- Minors do NOT count toward the team's roster size or bench
- Salary on cap depends on GROUP (see table above)

**UFA salary**: When `FCHL TEAM = UFA`, the SALARY column is last year's value and should be ignored entirely. These players have no current FCHL salary.

### fchl_teams.json

```json
{
  "BOT": {
    "id": 1,
    "is_my_team": true,
    "name": "Bridlewood AI",
    "penalty": 0.0,
    "colors": { "primary": "#ea217b", "secondary": "#ff7e31" },
    "logo": "1.gif"
  },
  "SRL": {
    "id": 2,
    "is_my_team": false,
    "name": "Searle Supremes",
    "penalty": 0.0,
    "colors": { "primary": "#000000", "secondary": "#464646" },
    "logo": "2.gif"
  },
  "nomination_order": ["BOT", "LPT", "GVR", "ZSK", "JHN", "LGN", "SRL", "VPP", "MAC", "SHF", "HSM"],
  "snake_draft": true
}
```

### model_params.json

From FCHL Auction Pricer repo. Do not edit manually.

### team_odds.json

```json
{ "season": "2025-2026", "odds": { "EDM": 0.12, "FLA": 0.09 } }
```

Vig-removed probabilities. Missing teams → 0.031 default.

## Auction rules (from CBA)

- UFA: circular bidding, $0.1M increments, drop out = permanent for that player
- RFA: secret bids, prior team can match (ROFR)
- Combo: 1 RFA + 1 UFA per nomination turn
- Min salary $0.5M, max $11.4M
- Roster: 24 active (playing: 12F + 6D + 2G, bench: 4 any position). Teams can draft beyond 24 — extras go to minors with salary fully on cap. Teams can also finish with fewer than 24.
- Snake draft for nominations
- Trades allowed during auction breaks
- Buyouts: player removed, 50% salary penalty remains on team's cap
- Teams can voluntarily stop drafting before filling all 24 spots

## Key design decisions

| Decision                          | Why                                                                                              |
| --------------------------------- | ------------------------------------------------------------------------------------------------ |
| Three-layer pricing               | Model alone ignores budget constraints. Market layer ensures bids reflect reality.               |
| Market ceiling from exact budgets | Perfect visibility during draft. Use it.                                                         |
| "Team done" toggle                | 3+ teams finish early per draft. Their dead budget distorts market calculations if not excluded. |
| Trade eval via hypothetical MILP  | Same optimizer, just run on a cloned state. No new algorithm needed.                             |
| Buyout as penalty math            | CBA rule: 50% stays on cap. Simple to model: remove salary, add penalty.                         |
| PuLP + CBC                        | Fast enough for ~200 binary vars. CBC bundled.                                                   |
| FastAPI + HTMX                    | Partial updates, no full-page re-runs. Single-page layout — no tab switching.                    |
| JSON snapshots for undo           | Simple, crash-safe, human-readable.                                                              |
| Term not tracked                  | Nobody caps out. Irrelevant.                                                                     |

## Development workflow

Verification loop for every change:

1. Make changes
2. Run tests: `pytest tests/ -v`
3. Fix any failures before moving on
4. Before committing: run full test suite

```bash
# Verification commands
pytest tests/ -v              # Run all tests
pytest tests/test_market.py   # Run specific module tests
git status                    # Check current state
git diff                      # Review changes before commit
```

## Testing

TDD. Key validations:

- Price predictions match Colab notebook
- Market ceiling ≤ opponents' physical max; bid rec ≤ market ceiling
- "Done" teams excluded from market calculations
- MILP produces valid rosters (positions, cap compliance)
- Trade evaluator: accept trade iff post-trade points > pre-trade points
- Buyout: penalty correctly computed, freed cap space = 50% of salary
- State serialization round-trips cleanly
- Endpoints update state correctly

`pytest tests/ -v`

## Nord theme color system

The UI uses the [Nord](https://www.nordtheme.com/) color palette. All colors are CSS custom properties in `static/style.css`. When adding or modifying UI elements, use the correct variable — never hardcode hex values.

### CSS variables and when to use them

| Variable | Nord token | Hex | Use for |
|---|---|---|---|
| `--bg` | nord0 | `#2e3440` | Page background, recessed input fields |
| `--bg-panel` | nord1 | `#3b4252` | Panel/card backgrounds, sticky headers |
| `--bg-hover` | nord2 | `#434c5e` | Hover states, active selections |
| `--text` | nord6 | `#eceff4` | Primary body text (brightest Snow Storm) |
| `--text-muted` | nord4 | `#d8dee9` | Secondary text, labels, timestamps |
| `--accent` | nord8 | `#88c0d0` | Headings, brand highlights, primary buttons |
| `--accent-secondary` | nord12 | `#d08770` | Warm emphasis (sparingly) |
| `--green` | nord14 | `#a3be8c` | Success: BID, trade accept, optimal roster |
| `--red` | nord11 | `#bf616a` | Danger: DROP, trade decline, errors |
| `--yellow` | nord13 | `#ebcb8b` | Warning: CAUTION, RFA markers, buyouts |
| `--blue` | nord9 | `#81a1c1` | Links, secondary buttons, informational |
| `--border` | nord3 | `#4c566a` | Panel borders, table dividers, separators |
| `--input-bg` | nord0 | `#2e3440` | Form input backgrounds (same as base) |

### Button text contrast rules

Nord's Frost and Aurora colors are pastel — **white text fails WCAG AA on most of them**. Follow this rule:

- Buttons on `--accent`, `--blue`, `--green`, `--yellow`, `--text-muted`: use `color: var(--bg)` (dark text)
- Buttons on `--red` only: use `color: #fff` (white text)
- Primary button hover: `#8fbcbb` (nord7, a sister Frost color)

### Tinted backgrounds for semantic states

For colored row/card backgrounds (bid results, trade outcomes), use the aurora color at low opacity:

```css
/* Pattern: rgba(<nord-color-rgb>, <opacity>) */
background: rgba(163, 190, 140, 0.1);  /* green tint — success */
background: rgba(191, 97, 106, 0.15);  /* red tint — danger */
background: rgba(235, 203, 139, 0.1);  /* yellow tint — warning */
background: rgba(129, 161, 193, 0.08); /* blue tint — informational */
background: rgba(136, 192, 208, 0.1);  /* accent tint — highlight */
```

### Adding a new UI element — checklist

1. Use CSS variables, not hex codes
2. Check text contrast: light backgrounds (`--accent`, `--green`, etc.) need dark text (`var(--bg)`)
3. Tinted backgrounds: use rgba at 0.08–0.15 opacity, not solid colors
4. Borders: use `var(--border)` consistently
5. Sticky headers: set `background: var(--bg-panel)` so content doesn't show through

## Code conventions

- Python 3.12, type hints on signatures
- Comments explain WHY not WHAT
- No over-engineering
- All money in millions (4.6 = $4.6M)
- Market-adjusted prices everywhere in optimizer — never raw model prices
- Flat module layout, no nested packages
- Keep functions small and focused
- Handle errors explicitly, don't swallow them

## Things Claude should NOT do

- Don't skip error handling
- Don't commit without running tests first
- Don't make breaking API changes without discussion
- Don't edit `data/model_params.json` manually (generated by pricer repo)

## Self-improvement

After every correction or mistake, update this CLAUDE.md with a rule to prevent repeating it. End corrections with: "Now update CLAUDE.md so you don't make that mistake again."

## Working with plan mode

- Start every complex task in plan mode
- Pour energy into the plan so implementation can be done in one shot
- When something goes sideways, switch back to plan mode and re-plan — don't keep pushing
- Use plan mode for verification steps too, not just for the build

## Slash commands

| Command           | Description                                             |
| ----------------- | ------------------------------------------------------- |
| `/commit-push-pr` | Commit, push, and open a PR                             |
| `/quick-commit`   | Stage all changes and commit with a descriptive message |
| `/test-and-fix`   | Run tests and fix any failures                          |
| `/review-changes` | Review uncommitted changes and suggest improvements     |
| `/worktree`       | Create a git worktree for parallel Claude sessions      |
| `/grill`          | Adversarial code review — don't ship until it passes    |
| `/techdebt`       | End-of-session sweep for duplicated and dead code       |

## Subagents

| Agent             | Purpose                                                      |
| ----------------- | ------------------------------------------------------------ |
| `code-simplifier` | Simplify code after Claude is done working                   |
| `code-architect`  | Design reviews and architectural decisions                   |
| `verify-app`      | Thoroughly test the application works correctly              |
| `build-validator` | Ensure project builds correctly for deployment               |
| `oncall-guide`    | Help diagnose and resolve production issues                  |
| `staff-reviewer`  | Review plans and architectures as a skeptical staff engineer |
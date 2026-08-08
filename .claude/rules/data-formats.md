---
paths:
  - "data/**"
  - "data_loader.py"
---

# Data File Formats

## players.csv

Single source for all players: keepers, auction-eligible, and minor leaguers.

```csv
PLAYER,POS,GROUP,STATUS,FCHL TEAM,NHL TEAM,AGE,SALARY,BID,PTS,PRIOR FCHL TEAM
Nikita Kucherov,F,3,START,LGN,TBL,31,8.5,0,144,
Connor McDavid,F,RFA2,,RFA,EDM,27,11.4,0,132,SRL
Artemi Panarin,F,3,,UFA,NYR,32,7.3,0,120,
Connor Ingram,G,3,MINOR,BOT,UTH,27,0.5,0,30,
```

### Column meanings

| Column | Description |
|--------|-------------|
| `PLAYER` | Player name |
| `POS` | Position: F, D, or G |
| `GROUP` | Contract group: 2, 3, C, RFA1, RFA2, A, B, D, E |
| `STATUS` | `START` = keeper on active roster, `MINOR` = minor league, blank = auction-eligible |
| `FCHL TEAM` | Team code if on a team, `RFA` if restricted free agent, `UFA` if unrestricted |
| `NHL TEAM` | NHL team |
| `AGE` | Player age |
| `SALARY` | Current salary in millions. For biddable players (UFA/RFA) this is **last season's salary** (0/blank = new to league) -- it feeds the price model's reputation feature (`log_lag`/`has_lag`), not the cap |
| `BID` | Always 0 in source (populated during auction) |
| `PTS` | Projected fantasy points |
| `PRIOR FCHL TEAM` | For RFAs only: which FCHL team previously held this player (for ROFR) |

### Duplicate PLAYER names

**The player name is the app's primary key** — `available_players`,
`market_prices`, `find_player`, every endpoint's `player` form field, the
transaction log, and the `bo-<name>` DOM ids. `players.csv` does not guarantee
uniqueness: as of 2026-08-07 it had 2158 rows and 2155 distinct names.

`data_loader._disambiguated_names` suffixes every row of a colliding group,
escalating only as far as it must: `Name (TEAM)`, then `Name (TEAM POS)` when
two share an NHL team, then `Name (#n)`. **Every row in the group is suffixed**,
never just the later ones — `X` beside `X (VAN D)` reads as one player listed
twice. The renames are logged and shown in an `#data-warning` banner (separate
from `#startup-warning`, which `/reset` clears).

Two ways a collision breaks things, and both are live in the current file:

- **two biddable rows** — `biddable[name] = ...` overwrote one, so `Matt Murray`
  (DAL and TOR) made 705 eligible rows load as 704 and the DAL one could not be
  drafted at all;
- **a roster row and a biddable row** — different dicts, nothing overwrites, so
  the same name is owned *and* draftable (`Jack Hughes`, `Elias Pettersson`).
  Only the zero-point exclusion hides those today; a projection refresh removes
  it.

The goalie-wins join uses the **raw** CSV name, because
`goalie_projection_stats.csv` carries that and cannot disambiguate either —
two goalies sharing a name share a wins figure. Looking the rename up there
would silently degrade every renamed goalie to the pts/win fallback.

### Deriving player categories

- **Keepers**: `STATUS = START` and `FCHL TEAM` is a team code (not UFA/RFA)
- **Biddable at auction**: `FCHL TEAM = UFA` or `FCHL TEAM = RFA` (STATUS blank)
- **Minor league**: `STATUS = MINOR`

### RFA detection (for price model `is_rfa` feature)

- `GROUP` in (`RFA1`, `RFA2`) -> RFA (`is_rfa=1`). Equivalent for auction purposes.
- `GROUP = 3` -> UFA (`is_rfa=0`)
- `GROUP` 2 and C are keeper/minor types -- never in biddable pool.

### RFA group conversion on signing

- `RFA1` -> becomes `GROUP 2`
- `RFA2` -> becomes `GROUP 3`

This matters for salary cap rules if later sent to minors.

### Keeper/minor salary rules

| GROUP | In biddable pool? | Salary on cap (START)? | Salary on cap (MINOR)? |
|-------|-------------------|------------------------|------------------------|
| `2` | No | Yes | Yes |
| `3` | Yes (as UFA) | Yes | Yes |
| `RFA1`, `RFA2` | Yes (as RFA) | N/A | N/A |
| `A`, `B`, `C`, `D`, `E` | No | Yes | No |

**Minor league rules**: Minors do NOT count toward roster size or bench. Salary on cap depends on GROUP (see table).

## fchl_teams.json

Team metadata, nomination order, penalties, colors, logos. Key fields: `id`, `is_my_team`, `name`, `penalty`, `colors`, `logo`, `nomination_order`, `snake_draft`.

## team_odds.json

Vig-removed Stanley Cup probabilities by NHL team, stored as **fractions** (0.1104). `load_team_odds` converts to **percent** (11.04) because the price model was trained on percentages. Missing teams default to 3.1 (percent, `DEFAULT_TEAM_PROBABILITY`).

## goalie_projection_stats.csv

Raw Dobber goalie projections (`league_year, player_name, proj_wins, proj_so, proj_gp`), copied from the FCHL-auction-pricer repo (written by its `parse_projections.py`). The loader uses only the **latest season's** rows to attach `proj_wins` to biddable goalies -- the price model prices goalies on wins, not the 2W+3SO composite. Goalies missing here fall back to `pts / goalie_pts_per_win`. Refresh this file together with `players.csv` before each draft.

## model_params.json

Exported by the FCHL-auction-pricer notebook (`auction_model_params.json`) -- never edit by hand. When refreshing it, also copy the notebook's `auction_predictions_current.csv` to `tests/fixtures/` so the golden test validates the new coefficients.

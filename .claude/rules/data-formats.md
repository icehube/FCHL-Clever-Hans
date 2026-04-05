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
| `SALARY` | Current salary in millions. **Ignore for `FCHL TEAM = UFA`** (stale from last year) |
| `BID` | Always 0 in source (populated during auction) |
| `PTS` | Projected fantasy points |
| `PRIOR FCHL TEAM` | For RFAs only: which FCHL team previously held this player (for ROFR) |

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

Vig-removed Stanley Cup probabilities by NHL team. Missing teams default to 0.031.

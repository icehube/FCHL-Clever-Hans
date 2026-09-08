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

## Legacy schema (`players-23.csv`) and the alternate-pool override

`data/players-23.csv` is the 2023-season snapshot committed with the repo. It
uses an **older, narrower schema** and cannot be loaded directly:

```csv
Player,Pos,Pts,Team,Status,Salary,Bid
Connor McDavid,F,145,GVR,START,11.4,0
Nikita Kucherov,F,110,UFA,0,0,0
```

Four canonical columns have no legacy source — `GROUP`, `NHL TEAM`, `AGE`,
`PRIOR FCHL TEAM` — and two conventions differ:

| | legacy | canonical |
|---|---|---|
| blank `STATUS` | the string `0` | `""` |
| team column | also carries `ENT` (entry-draft class) | only a code, `UFA`, or `RFA` |

**The `STATUS` difference is the one that bites, because it fails silently.**
`load_players` gates the biddable branch on `status == ""` and `UFA`/`RFA` are in
`_PLACEHOLDER_TEAMS`, so an untranslated row matches *neither* branch and is
dropped without a word. A column rename alone therefore loads **zero available
players**, which on screen is indistinguishable from a finished draft.

`convert_legacy_players.py` does the translation and refuses to write a file with
no biddables. It synthesizes `GROUP` from team + status — `UFA -> 3`,
`RFA -> RFA2`, `MINOR -> A`, otherwise `3`. `MINOR -> A` is the consequential
one: it keeps those salaries **off cap** and out of buyout eligibility, matching
the current file, where 145 of 149 MINOR rows are `A`-`E`. Rows on a team code
`fchl_teams.json` does not have are held back and printed, rather than being
swallowed by `build_initial_state` (which ignores unknown codes in silence).

**`NHL TEAM` is joined from the current `players.csv`** by normalized name
(`--nhl-teams`, on by default), covering 869 of 876 rows. Three normalizations
matter, and two of them undo damage in the pool file rather than era drift: the
legacy file writes a **backtick** for an apostrophe; `players.csv` renders `-`
as `0` (`Oliver Ekman0Larsson`) and `ari` as `UTH` (`Eetu LuostUTHnen`), from
two find-and-replaces — see the open finding in `BACKLOG.md`. Matching also
strips a trailing parenthetical (`Tony DeAngelo (NCM)`) and falls back to
first-initial + surname for a spelled-out nickname.

Two guards make the join safe to trust:

- **A name two players share resolves to nothing.** The set of candidate clubs
  is kept, not collapsed, so a tie is refused rather than broken — a coin flip
  would put a wrong club on a real player silently.
- **Only real NHL clubs are written.** `players.csv` carries the FCHL
  placeholder `UFA` in its NHL TEAM column on 9 rows, which is invisible to
  pricing but would render as a club and, once in a pool file, look like data.

These are **present-day** clubs: a player traded since the legacy season gets
the team he plays for now. That is coherent rather than a compromise, because
`team_odds.json` carries present-day Cup odds too.

`SALARY`-as-reputation is still **not recoverable** — no legacy biddable has a
prior salary, so `has_lag = 0` pool-wide and that one feature stays flat. `AGE`
costs nothing; it is not a model feature.

### Selecting a pool

`data_loader.PLAYERS_CSV` holds the pool path, defaulting to `data/players.csv`
and overridable with **`FCHL_PLAYERS_CSV`**. It is a module global rather than a
default argument, because a default binds at import and could not be
monkeypatched; `load_players` and `build_initial_state` resolve a `None`
sentinel against it, so an explicit path argument still wins.

**The state directory follows the pool, and must.** `lifespan` reads the saved
state *before* it reads any CSV, so an alternate pool sharing `data/state/`
would load the real draft's JSON and then save over it. `main._default_state_dir`
derives `data/state-<stem>` for any non-default pool; `FCHL_STATE_DIR` overrides
it. Both are logged at startup, since a mismatch is otherwise invisible.

```bash
.venv/bin/python convert_legacy_players.py \
    data/players-23.csv data/players-23-converted.csv
FCHL_PLAYERS_CSV=data/players-23-converted.csv .venv/bin/uvicorn main:app --reload
```

Do **not** export `FCHL_PLAYERS_CSV` while running pytest: `TestDataFingerprint`
reads the global and will fail, correctly.

## fchl_teams.json

Team metadata, nomination order, penalties, colors, logos. Key fields: `id`, `is_my_team`, `name`, `penalty`, `colors`, `logo`, `nomination_order`, `snake_draft`.

## team_odds.json

Vig-removed Stanley Cup probabilities by NHL team, stored as **fractions** (0.1104). `load_team_odds` converts to **percent** (11.04) because the price model was trained on percentages. Missing teams default to 3.1 (percent, `DEFAULT_TEAM_PROBABILITY`).

## goalie_projection_stats.csv

Raw Dobber goalie projections (`league_year, player_name, proj_wins, proj_so, proj_gp`), copied from the FCHL-auction-pricer repo (written by its `parse_projections.py`). The loader uses only the **latest season's** rows to attach `proj_wins` to biddable goalies -- the price model prices goalies on wins, not the 2W+3SO composite. Goalies missing here fall back to `pts / goalie_pts_per_win`. Refresh this file together with `players.csv` before each draft.

## model_params.json

Exported by the FCHL-auction-pricer notebook (`auction_model_params.json`) -- never edit by hand. When refreshing it, also copy the notebook's `auction_predictions_current.csv` to `tests/fixtures/` so the golden test validates the new coefficients.

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
Connor McDavid,F,3,START,GVR,EDM,30,11.4,0,131,
Nikita Kucherov,F,3,,UFA,TBL,34,10.0,0,128,
Gabriel Vilardi,F,RFA2,,RFA,WPG,28,0.9,0,72,ZSK
Dylan Holloway,F,C,MINOR,BOT,STL,25,1.6,0,67,
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
| `PRIOR FCHL TEAM` | For RFAs only: which FCHL team previously held this player (for ROFR). **Stale in the 2026-27 file**, which took it from the 2024-25 pool; a proposed correction changes 10 of its 22 RFAs — see "Which season each pool is" below |

### Duplicate PLAYER names

**The player name is the app's primary key** — `available_players`,
`market_prices`, `find_player`, every endpoint's `player` form field, the
transaction log, and the `bo-<name>` DOM ids. `players.csv` does not guarantee
uniqueness: as of 2026-09-17 it has 1267 rows and 1266 distinct names.

`data_loader._disambiguated_names` suffixes every row of a colliding group,
escalating only as far as it must: `Name (TEAM)`, then `Name (TEAM POS)` when
two share an NHL team, then `Name (#n)`. **Every row in the group is suffixed**,
never just the later ones — `X` beside `X (VAN D)` reads as one player listed
twice. The renames are logged and shown in an `#data-warning` banner (separate
from `#startup-warning`, which `/reset` clears).

Two ways a collision breaks things. Both were live in the 2024-25 file; the
2026-27 refresh left **one** colliding group and it is the first kind:

- **two biddable rows** — `biddable[name] = ...` overwrote one, so `Matt Murray`
  (DAL and TOR) made 705 eligible rows load as 704 and the DAL one could not be
  drafted at all. The live case today is `Elias Pettersson`, two Vancouver
  players who share a club as well as a name, so he escalates to the `(TEAM
  POS)` tier: `Elias Pettersson (VAN F)` and `Elias Pettersson (VAN D)`. Both
  are biddable, both carry points, and both are therefore draftable — which is
  the rename doing its job. **The rename only keeps the NAMES apart; it says
  nothing about whether each row carries its own projection**, and until
  2026-09-22 the defenceman carried the forward's: 69 points against the 10
  Dobber projects, making him the pool's #2 D in every team's plan. The
  converter's exact join had collapsed the two onto one key — see
  `convert_fchl_online.read_projections` — and this sentence called the result
  correct because both rows *had* points, without asking whose. A same-name
  pair is now resolved by position at conversion, and
  `tests/test_fchl_online_conversion.py` fails a live pool where two such
  players share a projection.
- **a roster row and a biddable row** — different dicts, nothing overwrites, so
  the same name is owned *and* draftable. `Jack Hughes` and `Elias Pettersson`
  were both this shape in the 2024-25 file, hidden only by the zero-point
  exclusion dropping the biddable half, and `BACKLOG.md` recorded that the next
  projection refresh would remove the cover. Re-checked on the 2026-27 pool
  (2026-09-15): **this shape no longer occurs at all** — the one remaining group
  is biddable-vs-biddable. That is the pool changing, not the hazard being
  fixed, so the entry stays.

The goalie-wins join uses the **raw** CSV name, because
`goalie_projection_stats.csv` carries that and cannot disambiguate either —
two goalies sharing a name share a wins figure. Looking the rename up there
would silently degrade every renamed goalie to the pts/win fallback.

### Deriving player categories

- **Keepers**: `STATUS = START` and `FCHL TEAM` is a team code (not UFA/RFA)
- **Biddable at auction**: `FCHL TEAM = UFA` or `FCHL TEAM = RFA` (STATUS blank)
- **Minor league**: `STATUS = MINOR`

**Only `MINOR` is compared against anything.** `load_players` reads
`is_minor = status == "MINOR"` and nothing else, so on a real team code every
other string — `START`, a blank, `MINR`, `Minor`, a stray space — produces an
**active keeper**, silently. `START` is a convention the writers keep, not a
value the loader checks. That is why moving a player between the roster and the
minors in a pool file goes through `bake_roster_state.py` rather than by hand:
a typo there is not an error, it is a wrong roster that looks right.

**These three are the whole durable roster vocabulary**, and `/reset` rebuilds
from them. A bench assignment is not among them — `is_bench` has no column and
reaches no engine module — and a player removed from the league is expressed by
the **absence** of his row, which is the only way to say "gone this season": a
blank `FCHL TEAM` is dropped at load but leaves a misleading row behind, and
`UFA` would put him in the auction. See the prep loop in CLAUDE.md.

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
one: it keeps those salaries **off cap** and out of buyout eligibility, which is
what a minor almost always is in the canonical files. The 2026-27 file as
converted (`4a08a6f`) had **166 of 170** MINOR rows in `A`-`E` and the other 4
in `F`, which behaves identically — it is in none of `RFA_GROUPS`,
`MINOR_CAP_GROUPS` or `BUYOUT_ELIGIBLE_GROUPS`. The hand-maintained 2024-25
file had 145 of 149, and its other 4 were group `3`: on-cap minors, the one
shape `MINOR -> A` gets wrong. (Until 2026-09-22 this attributed the 166 of 170
to the hand-maintained file, which reproduces on no file but the export.) On
the 2026-27 file STATUS is **derived** from the contract group instead (owner
decision 2026-09-15: `2`/`3` -> `START`, `A`-`F` -> `MINOR`), so **as
converted** no group-2/3 player is a minor and no `A`-`F` player is a starter.
**The bake then moves players between the lists, so only the first half still
holds on the live file.** Re-measured 2026-09-22: 167 MINOR rows, 163 `A`-`E`
and 4 `F`, none in group 2/3 — and **three** `A`-`F` starters, the BOT
prospects recalled in the `c27ed02` bake. Two consequences worth knowing: the
league's cap-used figure is understated — by $20.7M on the hand-maintained
2024-25 file (22 of 248 rostered rows wrong, 8.9%) and by **$35.4M** on
`players-25.csv`, the 2025-26 snapshot (36 of 255, 14.1%), each measured
against that file's own `GROUP` and `STATUS`, so neither figure is season
drift; and a buyout-INELIGIBLE
contract can sit on either list — in the minors by construction, on the active
roster once baked there — which is why `test_trade_buyout_undo.py::_ineligible`
searches `all_players` rather than `roster_players`. Rows on a team code
`fchl_teams.json` does not have are held back and printed, rather than being
swallowed by `build_initial_state` (which ignores unknown codes in silence).

**`NHL TEAM` is joined from the canonical pools** by normalized name
(`--nhl-teams`, repeatable; `DEFAULT_NHL_SOURCES` is `players.csv` then
`players-25.csv`, tried in that order with the first answer winning). Coverage
is **714 of 876** as of 2026-09-15, down from 869 of 876: the donor is whatever
`players.csv` holds, and the 2026-27 refresh cut it from 2158 rows to 1268 by
dropping 885 unprojected prospects, which took the single-donor figure to 644.
Chaining `players-25.csv` behind it recovers 70 of those. It will not reach 869
again — a 2023 player who has left the league since is in no pool this repo
carries — and chaining is deliberately not merging, because a player listed on
two clubs by two pools would be refused by the tie guard below.

Three normalizations matter, and two of them undo damage in the *2024-25* pool
file rather than era drift: the legacy file writes a **backtick** for an
apostrophe; that `players.csv` rendered `-` as `0` (`Oliver Ekman0Larsson`) and
`ari` as `UTH` (`Eetu LuostUTHnen`), from two find-and-replaces. **Neither
artefact survives the 2026-27 refresh** — measured 2026-09-15, no pool file in
the repo now contains a digit-zero hyphen, and none spells Utah `UTH`. The
normalizations stay, and `test_normalization_undoes_how_the_two_files_spell_a_name`
supplies its own synthetic cases rather than relying on the pool: the file is
replaced before every draft and the next export may reintroduce either. Matching
also strips a trailing parenthetical (`Tony DeAngelo (NCM)`) and falls back to
first-initial + surname for a spelled-out nickname.

Two guards make the join safe to trust:

- **A name two players share resolves to nothing.** The set of candidate clubs
  is kept, not collapsed, so a tie is refused rather than broken — a coin flip
  would put a wrong club on a real player silently.
- **Only real NHL clubs are written.** The 2024-25 `players.csv` carried the
  FCHL placeholder `UFA` in its NHL TEAM column on 9 rows, which is invisible to
  pricing but would render as a club and, once in a pool file, look like data.
  `convert_fchl_online.py` now blanks the same placeholder at the source (3 rows
  on the 2026-27 file, after the fourth was removed from the league — see
  `no_nhl_club` below), so no pool in the repo carries `UFA` any more and the
  named exception in `test_every_nhl_club_in_every_pool_has_cup_odds` currently
  matches nothing. **A blank club means two different things and
  `no_nhl_club` reports both rather than letting them collapse**: a club the odds
  file does not know (a data problem — the 162 blanks in
  `players-23-converted.csv` are all this) and the `UFA` placeholder, which is
  the league's own flag for *no NHL contract*. Only the second is a roster
  decision, and only when the row is a cap-counting one: a group A-F prospect
  without an NHL contract is the normal case and costs nothing, while a `START`
  row in group 2/3 is a salary against the cap for a player who will not play.
  `tests/test_fchl_online_conversion.py` fails on the latter.
  Both stay: the guard is about what a future export may write,
  not about what today's happens to contain.

These are **present-day** clubs: a player traded since the legacy season gets
the team he plays for now. That is coherent rather than a compromise, because
`team_odds.json` carries present-day Cup odds too.

`SALARY`-as-reputation is still **not recoverable** — no legacy biddable has a
prior salary, so `has_lag = 0` pool-wide and that one feature stays flat. `AGE`
costs nothing; it is not a model feature.

## Streamlit auto-save schema (`25-26 Starting Rosters.json`)

`data/25-26 Starting Rosters.json` is an auto-save from the Streamlit tool this
app replaced, written 2025-09-05 — days before the draft the league workbook
records on its `2025` sheet. It is the only genuine **pre-draft** snapshot of a
season we have that also carries **that season's own projections**, which is why
it earns a converter of its own rather than being folded into the legacy one.

```json
{"timestamp": "...", "players_data": [
  {"PLAYER": "...", "GROUP": "3", "POS": "F", "FCHL TEAM": "UFA",
   "NHL TEAM": "TBL", "AGE": 32, "STATUS": "NO", "SALARY": 0.0,
   "Dobber": 124.0, "DtZ": 114.0, "PTS": 119, "Draftable": "YES",
   "Z-score": 5.29, "BID": 8.3}],
 "teams_data": {...}, "auction_results": {...}, "bid_history": [],
 "roster_changelog": []}
```

Most columns line up with the canonical schema, which is what makes the one that
does not dangerous. **`STATUS` is `"NO"` where canonical is blank** — the same
silent-drop trap as the legacy file's `"0"`, in a different disguise, with the
same symptom: 651 free agents vanish and the app boots on a pool that looks like
a finished draft. `convert_state_json.py` translates it and shares
`_require_biddables` with the legacy converter so neither can write that file.

What it drops or blanks, and why:

| field | treatment | reason |
|---|---|---|
| `BID` | zeroed | carries the **old tool's Z-score predictions** on 145 players — the model this app exists to replace. Canonical `BID` is 0 in source. |
| `PRIOR FCHL TEAM` | blank | no column, and not derivable: the workbook's `Intro` is the nominating team, not the holder |
| `Dobber`, `DtZ` | dropped | `PTS` is already the blend of the two |
| `Draftable`, `Z-score` | dropped | the old tool's shortlist (145 rows) and its pricing intermediate |
| `GROUP` | passed through | including the two rows in group **`F`**, which the documented vocabulary lacks. It is in none of `RFA_GROUPS`, `MINOR_CAP_GROUPS` or `BUYOUT_ELIGIBLE_GROUPS`, so it already behaves like the A-E family; remapping it would invent a contract the source does not record. |
| `NHL TEAM` | validated | 3 rows carry the FCHL placeholder `UFA`, the same contamination `valid_nhl_teams` was written for |
| `ENT` rows (22) | held back, reported | `build_initial_state` drops an unknown FCHL TEAM in silence |

Unlike the 2023 file, **no NHL-team join is needed** (the column is populated)
and **no name disambiguation fires** — the old tool had already resolved its own
collisions, so `Sebastian Aho (F)` and `Sebastian Aho (D)` arrive distinct and
the `#data-warning` banner stays silent. `tests/test_state_json_conversion.py`
pins that, so a future collision is a deliberate change rather than a draft-night
surprise.

`SALARY` is **0.0 on every biddable**, so `has_lag = 0` pool-wide and the price
model's reputation feature is flat — the same limitation as the 2023 pool, and
the reason the top of the price distribution compresses. Penalties come from
`fchl_teams.json` (JHN and LGN at $0.3M today), not from the file's own
`teams_data`, which recorded 0.0 for all eleven in 2025.

The converted result is a real auction: 649 available players for **137** open
roster spots against the workbook's 139 actual picks, all 11 teams able to fill
a roster, 20 RFAs matching the workbook's `2025` sheet exactly.

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
# 2023 pool (legacy CSV schema)
.venv/bin/python convert_legacy_players.py \
    data/players-23.csv data/players-23-converted.csv
FCHL_PLAYERS_CSV=data/players-23-converted.csv .venv/bin/uvicorn main:app --reload

# 2025 pool (Streamlit auto-save) — the pre-draft state with that season's
# projections, and the one the workbook has a known outcome for
.venv/bin/python convert_state_json.py \
    "data/25-26 Starting Rosters.json" data/players-25.csv
FCHL_PLAYERS_CSV=data/players-25.csv .venv/bin/uvicorn main:app --port 8001
```

Do **not** export `FCHL_PLAYERS_CSV` while running pytest: `TestDataFingerprint`
reads the global and will fail, correctly.

**Which season each pool is.** `players-23.csv` is the 2023 snapshot,
`players-25.csv` the 2025-26 pre-draft state (written 2025-09-05), and
`players.csv` the 2026-27 pool. The file `players.csv` held until the
2026-09-15 refresh — `git show 4a08a6f^:data/players.csv`, the source of every
"UTH on 78 rows" and "705-player pool" figure in these docs — is the
**2024-25** pool, and this repo called it 2025-26 until 2026-09-22. Measured
that day: its ages run exactly one below `players-25.csv` on 823 of 824 shared
players, and it lists Luukkonen and Vilardi as RFAs, both signed at the 2024
auction. It was also the converter's `--prior` default when the 2026-27 file was
built, which is why that file's `PRIOR FCHL TEAM` column names who held each
RFA two seasons back rather than in the season just ended (`BACKLOG.md`,
`prior_team_index`).

## fchl_teams.json

Team metadata, team order, penalties, colors, logos. Key fields: `id`, `is_my_team`, `name`, `penalty`, `colors`, `logo`, `nomination_order`.

`nomination_order` keeps its name but is now purely a **display** order — the League State rows, the `/solve-standings` OOB cells, the bidder-toggle grid, the trade-partner dropdown and `default_bidders` all iterate it so the league appears in one stable order. The turn pointer that used to index it was removed 2026-09-11. `snake_draft` went with it; it was never in the data fingerprint, so dropping the key costs no refresh dance.

## team_odds.json

Vig-removed Stanley Cup probabilities by NHL team, stored as **fractions** (0.1104). `load_team_odds` converts to **percent** (11.04) because the price model was trained on percentages. Missing teams default to 3.1 (percent, `DEFAULT_TEAM_PROBABILITY`).

## goalie_projection_stats.csv

Raw Dobber goalie projections (`league_year, player_name, proj_wins, proj_so, proj_gp`), copied from the FCHL-auction-pricer repo (written by its `parse_projections.py`). The loader uses only the **latest season's** rows to attach `proj_wins` to biddable goalies -- the price model prices goalies on wins, not the 2W+3SO composite. Goalies missing here fall back to `pts / goalie_pts_per_win`. Refresh this file together with `players.csv` before each draft.

## model_params.json

Exported by the FCHL-auction-pricer notebook (`auction_model_params.json`) -- never edit by hand. When refreshing it, also copy the notebook's `auction_predictions_current.csv` to `tests/fixtures/` so the golden test validates the new coefficients.

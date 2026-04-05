# Pre-Auction Checklist

You are running a pre-auction readiness check for the FCHL Auction Manager at the current working directory. Your job is to verify everything works before a live multi-hour auction draft.

Run each check below in order. Collect all issues into a list. At the end, print a clear verdict.

## Checks to run

### 1. Test suite
Run `pytest tests/ -v`. ALL tests must pass. If any fail, report the failure names and stop — nothing else matters until tests pass.

### 2. App startup
Run this Python snippet to verify the app loads and responds:
```bash
python3 -c "
from fastapi.testclient import TestClient
from main import app
import main

with TestClient(app) as c:
    r = c.get('/')
    print(f'Index: {r.status_code} ({len(r.text)} bytes)')
    r = c.get('/state')
    print(f'State: {r.status_code}')
    print(f'Teams: {len(r.json()[\"teams\"])}')
    print(f'Available: {len(r.json()[\"available_players\"])}')
    print(f'MILP status: {main.milp_solution.status}')
    print(f'MILP points: {main.milp_solution.total_points}')
    print(f'Market ceiling: {main.market_info.market_ceiling}')
    print(f'Floor demand: {main.market_info.floor_demand}')
    print(f'Nominator: {main.auction_state.current_nominator()}')
"
```
Verify: index returns 200, state returns 200 with 11 teams, MILP is Optimal, market ceiling > MIN_SALARY, floor_demand is False.

### 3. Data quality — duplicate names
Run:
```bash
python3 -c "
import csv
from collections import Counter
names = []
with open('data/players.csv') as f:
    for row in csv.DictReader(f):
        if row['FCHL TEAM'].strip() in ('UFA', 'RFA') and row['STATUS'].strip() == '':
            names.append(row['PLAYER'].strip())
dupes = {k: v for k, v in Counter(names).items() if v > 1}
if dupes:
    print(f'WARNING: {len(dupes)} duplicate names in biddable pool:')
    for name, count in dupes.items():
        print(f'  {name} appears {count} times')
else:
    print('OK: No duplicate biddable player names')
"
```
Duplicates cause player lookup failures during the auction. Report any found.

### 4. Data quality — missing fields
Run:
```bash
python3 -c "
import csv
issues = []
with open('data/players.csv') as f:
    for i, row in enumerate(csv.DictReader(f), 2):
        name = row['PLAYER'].strip()
        if not row['POS'].strip():
            issues.append(f'Line {i}: {name} missing POS')
        if not row['NHL TEAM'].strip():
            issues.append(f'Line {i}: {name} missing NHL TEAM')
if issues:
    print(f'WARNING: {len(issues)} data issues:')
    for issue in issues[:10]:
        print(f'  {issue}')
else:
    print('OK: All players have POS and NHL TEAM')
"
```

### 5. Logo coverage
Run:
```bash
python3 -c "
import csv, os
teams_in_csv = set()
with open('data/players.csv') as f:
    for row in csv.DictReader(f):
        t = row['NHL TEAM'].strip()
        if t:
            teams_in_csv.add(t)
logos = set(f.replace('.svg', '') for f in os.listdir('nhl_logos') if f.endswith('.svg'))
missing = teams_in_csv - logos
if missing:
    print(f'WARNING: {len(missing)} teams have no logo: {missing}')
else:
    print(f'OK: All {len(teams_in_csv)} NHL teams have SVG logos')
"
```

### 6. State file health
Run:
```bash
python3 -c "
import json, os
state_path = 'data/state/auction_state.json'
backup_path = state_path + '.backup'
for path, label in [(state_path, 'State'), (backup_path, 'Backup')]:
    if os.path.exists(path):
        try:
            with open(path) as f:
                data = json.load(f)
            print(f'{label}: OK ({os.path.getsize(path)//1024}KB, {len(data.get(\"teams\", {}))} teams)')
        except Exception as e:
            print(f'{label}: CORRUPT — {e}')
    else:
        print(f'{label}: not found (will start fresh)')
"
```

### 7. BOT roster sanity
Run:
```bash
python3 -c "
from fastapi.testclient import TestClient
from main import app
import main

with TestClient(app) as c:
    c.post('/reset')  # Fresh state
    bot = main.auction_state.teams['BOT']
    print(f'Keepers: {len(bot.keeper_players)}')
    print(f'Salary: \${bot.total_salary:.1f}M')
    print(f'Remaining: \${bot.remaining_budget:.1f}M')
    print(f'Spots: {bot.total_spots_remaining}')
    print(f'Max bid: \${bot.physical_max_bid:.1f}M')
    print(f'Position needs: {dict(bot.roster_needs)}')
    if bot.total_salary > 56.8:
        print('ERROR: BOT over salary cap!')
    elif bot.total_spots_remaining <= 0:
        print('ERROR: BOT has no spots remaining!')
    elif len(bot.keeper_players) == 0:
        print('ERROR: BOT has no keepers!')
    else:
        print('OK: BOT roster looks healthy')
"
```

### 8. FCHL team logos
Run:
```bash
python3 -c "
import os
from data_loader import load_team_metadata
meta = load_team_metadata()
missing = []
for code, team in meta.items():
    if isinstance(team, dict) and 'logo' in team:
        logo_path = os.path.join('fchl_logos', team['logo'])
        if not os.path.exists(logo_path):
            missing.append(f'{code}: {team[\"logo\"]}')
if missing:
    print(f'WARNING: {len(missing)} FCHL logos missing:')
    for m in missing:
        print(f'  {m}')
else:
    print('OK: All FCHL team logos present')
"
```

## Final verdict

After all checks, print a summary like:

```
═══════════════════════════════════════
  PRE-AUCTION CHECKLIST RESULTS
═══════════════════════════════════════
  Tests:        PASS (283 passed)
  App startup:  PASS
  Data quality: PASS (1 warning: duplicate Matt Murray)
  Logo coverage: PASS
  State file:   PASS
  BOT roster:   PASS (12 keepers, $35.3M, 12 spots)
  FCHL logos:   PASS
═══════════════════════════════════════
  VERDICT: READY FOR AUCTION
═══════════════════════════════════════
```

Or if issues are found:

```
═══════════════════════════════════════
  VERDICT: NOT READY — 2 issues found
═══════════════════════════════════════
  1. [CRITICAL] 3 test failures — fix before auction
  2. [WARNING] Missing logo for UTH — players will show broken image
═══════════════════════════════════════
```

Classify issues as CRITICAL (blocks auction) or WARNING (cosmetic/minor).

# TODO — Low Priority

## fchl_teams.json: `is_my_team` field consistency
Only BOT has `is_my_team: true`. Other teams omit the field entirely. `data_loader.py` should use `.get("is_my_team", False)` when reading this.

## fchl_logos/0.gif — unknown logo
There's a `0.gif` in `fchl_logos/` with no matching team (team IDs are 1–11). Confirm if this is a league logo or can be removed.

## CLAUDE.md: keepers.json penalty field
`keepers.json` format in CLAUDE.md shows `penalties` at the team level, but `fchl_teams.json` already has a `penalty` field per team. Clarify which file is the source of truth for pre-auction penalties, or whether both contribute (keepers.json for roster, fchl_teams.json for penalties).

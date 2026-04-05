"""League constants and configuration."""

# Salary constraints (in millions)
SALARY_CAP = 56.8
MIN_SALARY = 0.5
MAX_SALARY = 11.4
SALARY_INCREMENT = 0.1

# Roster sizes
ROSTER_SIZE = 24

# Position minimums (active roster)
MIN_FORWARDS = 14
MIN_DEFENSE = 7
MIN_GOALIES = 3

# League
MY_TEAM = "BOT"

# Buyout
BUYOUT_PENALTY_RATE = 0.5

# NHL team alias mapping (players.csv uses UTH, team_odds.json uses UTA)
NHL_TEAM_ALIASES = {"UTH": "UTA"}

# Default Stanley Cup probability for teams not in team_odds.json
DEFAULT_TEAM_PROBABILITY = 0.031

# Groups whose minor-league salary counts toward the cap
MINOR_CAP_GROUPS = {"2", "3"}

# Groups that indicate RFA status
RFA_GROUPS = {"RFA1", "RFA2"}

# Position minimum lookup
POSITION_MINIMUMS = {"F": MIN_FORWARDS, "D": MIN_DEFENSE, "G": MIN_GOALIES}

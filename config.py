"""League constants and configuration."""

# Salary constraints (in millions)
SALARY_CAP = 56.8
MIN_SALARY = 0.5
MAX_SALARY = 11.4
SALARY_INCREMENT = 0.1

# How close the price can get to a player's value before the advisor stops
# saying BID and starts saying CAUTION.
CAUTION_BAND = 0.3

# Roster sizes
ROSTER_SIZE = 24

# Starting lineup — only these players score points each week. The 4 bench
# spots are position-agnostic insurance and contribute nothing to the total.
STARTING_LINEUP = {"F": 12, "D": 6, "G": 2}

# Position minimums (active roster) = must be able to field the lineup
MIN_FORWARDS = 12
MIN_DEFENSE = 6
MIN_GOALIES = 2

# Bench composition preference (2F/1D/1G -> the classic 14F/7D/3G roster).
# Soft, not a constraint: BACKUP_BONUS points of objective credit per filled
# backup slot, so the optimizer gives up the balanced bench only when a
# different shape wins more than ~BACKUP_BONUS starter points. BENCH_WEIGHT
# values bench players' projected points at 10% so backups are good players,
# not warm bodies, without letting bench depth outbid starter upgrades.
BACKUP_TARGETS = {"F": 2, "D": 1, "G": 1}
BACKUP_BONUS = 5.0
BENCH_WEIGHT = 0.1

# League
MY_TEAM = "BOT"

# Buyout
BUYOUT_PENALTY_RATE = 0.5

# NHL team alias mapping (players.csv uses UTH, team_odds.json uses UTA)
NHL_TEAM_ALIASES = {"UTH": "UTA"}

# Default Stanley Cup probability (percent) for teams not in team_odds.json —
# the price model was trained on percentages (league sums to 100)
DEFAULT_TEAM_PROBABILITY = 3.1

# Groups whose minor-league salary counts toward the cap
MINOR_CAP_GROUPS = {"2", "3"}

# Groups that may be bought out (CBA Article 11.4). A-E are prospects: they can
# be parked in the minors for a $0 cap hit, so a buyout would cost the 50%
# penalty while freeing nothing — the league disallows it outright.
#
# Deliberately NOT reusing MINOR_CAP_GROUPS despite the identical membership.
# The two answer different questions ("may this player be bought out?" vs "does
# this minors salary count on cap?") and coincide only because both descend from
# real-contract-vs-prospect. This repo already has one open finding from two
# predicates that agreed until one moved (the drain filter) — don't merge these.
BUYOUT_ELIGIBLE_GROUPS = {"2", "3"}

# Groups that indicate RFA status
RFA_GROUPS = {"RFA1", "RFA2"}

# Position minimum lookup
POSITION_MINIMUMS = {"F": MIN_FORWARDS, "D": MIN_DEFENSE, "G": MIN_GOALIES}

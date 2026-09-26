"""League constants and configuration."""

# Salary constraints (in millions)
SALARY_CAP = 56.8
MIN_SALARY = 0.5
MAX_SALARY = 11.4
SALARY_INCREMENT = 0.1

# Stress rate for the trade evaluator's "does it survive an expensive auction"
# re-solve: every auction price is marked up this much. The one real draft
# replayed (tests/measure_replay.py, 2026-09-13) paid $308.0M against a $242.9M
# model total -- 27% over -- so a trade whose gain comes from buying players
# back at the model's EXPECTED price is exactly the gain that draft falsified.
# Rounded down to 25% so the test is not stricter than the evidence, and
# applied only to the part of a price above MIN_SALARY (see evaluate_trade).
OVERPAY_STRESS = 1.25

# How close the price can get to a player's value before the advisor stops
# saying BID and starts saying CAUTION.
CAUTION_BAND = 0.3

# Roster sizes
ROSTER_SIZE = 24

# Starting lineup — only these players score points each week. The 4 bench
# spots are position-agnostic insurance and contribute nothing to the total.
STARTING_LINEUP = {"F": 12, "D": 6, "G": 2}

# How many roster spots are left over once the lineup is fielded. Derived from
# the two structural numbers rather than written as 4. A hard legality rule:
# 24 spots, 20 of them starting, so at most 4 on the bench. How the bench is
# MADE UP is the solver's choice, priced by BENCH_DEPTH_WEIGHTS below.
BENCH_SIZE = ROSTER_SIZE - sum(STARTING_LINEUP.values())

# Position minimums (active roster) = must be able to field the lineup
MIN_FORWARDS = 12
MIN_DEFENSE = 6
MIN_GOALIES = 2

# Bench value: a backup scores when a starter at his position is out -- injured,
# or swapped out for poor form at a monthly adjustment, the only two times the
# league lets a lineup change. OUT_RATE is the fraction of the season each
# starter is out, independently. So the k-th backup at a position plays when at
# least k of its n starters are out, which is P(Binomial(n, m) >= k): a season
# fraction that falls fast with depth and depends on how many starters the
# position has. At m = 0.15 the first backup F plays 0.86 of the season, the
# first D 0.62, the first G 0.28, and a 3rd backup D 0.05.
#
# A GUESS to tune, not a measurement: ~10% games lost to injury plus some bust
# cover. The league DB archives carry no games-played or weekly data to fit it
# from (checked 2026-09-25).
#
# Replaced, on 2026-09-25, BACKUP_TARGETS / BACKUP_BONUS / BENCH_WEIGHT: a flat
# 5 points of objective credit per filled 2F/1D/1G backup slot, whoever filled
# it, plus 10% of a backup's points -- but only for players the MILP BOUGHT, and
# in no figure any decision compared. A 1-point defenceman earned the D backup
# credit exactly as well as a 40-point one.
OUT_RATE = {"F": 0.15, "D": 0.15, "G": 0.15}


def _depth_weights(starters: int, out_rate: float, depth: int) -> tuple[float, ...]:
    """P(Binomial(starters, out_rate) >= k) for k = 1..depth."""
    from math import comb
    pmf = [comb(starters, i) * out_rate**i * (1 - out_rate) ** (starters - i)
           for i in range(starters + 1)]
    return tuple(sum(pmf[k:]) for k in range(1, depth + 1))


# A depth slot worth less than this share of the season is not modelled. Each
# slot costs the MILP a variable per candidate, and the ones dropped at 0.15 --
# a 3rd backup D (0.047), a 4th (0.006), a 2nd backup G (0.022) -- are worth at
# most ~2 points between them. Dropped from the model itself, not just the
# solver, so `expected_points` and the MILP value the same bench.
MIN_DEPTH_WEIGHT = 0.05

# Season fraction the k-th backup plays, per position: F 0.86/0.56/0.26/0.09,
# D 0.62/0.22, G 0.28 at the default OUT_RATE.
BENCH_DEPTH_WEIGHTS = {
    pos: tuple(
        w for w in _depth_weights(n, OUT_RATE[pos], min(BENCH_SIZE, n))
        if w >= MIN_DEPTH_WEIGHT
    )
    for pos, n in STARTING_LINEUP.items()
}

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

# Below this clearing price a drain nomination isn't worth the turn: burning
# ~$2M of one opponent's cap costs BOT a nomination it could have spent on a
# player it wants. Compared against the MARKET price (what the player will
# actually fetch), not the model price — a $9M star behind a $0.5M ceiling
# drains $0.5M, and gating on his model price would call that a drain.
MIN_DRAIN_PRICE = 2.0

# Groups that indicate RFA status
RFA_GROUPS = {"RFA1", "RFA2"}

# Position minimum lookup
POSITION_MINIMUMS = {"F": MIN_FORWARDS, "D": MIN_DEFENSE, "G": MIN_GOALIES}

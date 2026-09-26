"""Auction state: players, teams, serialization, undo."""

from __future__ import annotations

import json
import re
import unicodedata
from copy import deepcopy
from dataclasses import dataclass, field, fields
from functools import lru_cache

from config import (
    BENCH_DEPTH_WEIGHTS,
    BENCH_SIZE,
    BUYOUT_ELIGIBLE_GROUPS,
    MAX_SALARY,
    MIN_SALARY,
    MINOR_CAP_GROUPS,
    POSITION_MINIMUMS,
    ROSTER_SIZE,
    SALARY_CAP,
    STARTING_LINEUP,
)

MAX_SNAPSHOTS = 50


def lineup_points(players) -> int:
    """Points from the best legal starting lineup (12F/6D/2G).

    League scoring counts starters only — bench players contribute nothing.
    Greedy top-k per position is exact for this subproblem.
    """
    by_pos: dict[str, list[int]] = {"F": [], "D": [], "G": []}
    for p in players:
        if p.position in by_pos:
            by_pos[p.position].append(p.projected_points)
    total = 0
    for pos, slots in STARTING_LINEUP.items():
        total += sum(sorted(by_pos[pos], reverse=True)[:slots])
    return total


def expected_points(players) -> float:
    """Starting-lineup points plus the expected contribution of the bench.

    THE figure every decision compares -- trade and buyout verdicts, max bids,
    the Proj column -- because a backup is worth real points: he plays when a
    starter at his position is out (see config.OUT_RATE). The best 12F/6D/2G
    start and score in full; then up to BENCH_SIZE of the rest fill the bench,
    the k-th backup at a position scoring BENCH_DEPTH_WEIGHTS[pos][k-1] of his
    points. Beyond BENCH_SIZE a player adds nothing (the minors never play).

    Greedy is exact: within a position both the weights and the sorted points
    fall, so each position's marginal values are already descending, and the
    best BENCH_SIZE of all of them respect every position's depth order.
    `solve_optimal_roster` optimizes this same quantity, and
    tests/test_bench_value.py pins the two agreeing.
    """
    by_pos: dict[str, list[int]] = {"F": [], "D": [], "G": []}
    for p in players:
        if p.position in by_pos:
            by_pos[p.position].append(p.projected_points)
    total = 0.0
    bench: list[float] = []
    for pos, slots in STARTING_LINEUP.items():
        ranked = sorted(by_pos[pos], reverse=True)
        total += sum(ranked[:slots])
        bench += [w * pts for w, pts in zip(BENCH_DEPTH_WEIGHTS[pos], ranked[slots:])]
    return total + sum(sorted(bench, reverse=True)[:BENCH_SIZE])


@dataclass
class Player:
    """A biddable player available at auction."""

    name: str
    position: str  # "F", "D", "G"
    group: str  # "2", "3", "RFA1", "RFA2", etc.
    nhl_team: str
    age: int
    projected_points: int
    is_rfa: bool
    salary: float  # Last season's FCHL salary (0 = new to league); lag feature
    team_probability: float  # Stanley Cup odds for their NHL team
    prior_fchl_team: str = ""  # For RFAs: which FCHL team previously held them
    pos_rank: int = 0  # Rank by points within position at draft start (0 = unset)
    proj_wins: float | None = None  # Goalies: projected wins (model input)


@dataclass
class PlayerOnRoster:
    """A player on a team's roster (keeper, minor, or acquired)."""

    name: str
    position: str
    group: str
    salary: float
    projected_points: int
    nhl_team: str = ""
    is_minor: bool = False
    is_bench: bool = False
    # PROVENANCE: was this player on an FCHL team before the auction? Same
    # meaning "keeper" carries in send_to_minors — not a league rule, but two
    # things DO read it: recall_from_minors routes on it, and team_panel.html
    # colours a row green for anyone in acquired_players.
    #
    # It exists because provenance was previously encoded ONLY by which list a
    # player sat in, and `minor_players` is a third list that holds neither. So
    # a keeper sent down and recalled came back into `acquired_players` and
    # `team_panel.html` coloured him green, i.e. "I bought him at auction",
    # permanently. Default False is the right one: a drafted or traded-for
    # player IS acquired, and green should say so.
    is_keeper: bool = False

    @property
    def counts_on_cap(self) -> bool:
        """Whether this player's salary counts toward the team's cap."""
        if not self.is_minor:
            return True
        return self.group in MINOR_CAP_GROUPS

    @property
    def can_be_bought_out(self) -> bool:
        """Whether this player may legally be bought out.

        Location is irrelevant — active, bench and minors all buy out the same
        way, for the same 50% penalty. Only the contract group decides.

        Asked from the template as well as the engine, so the buttons on offer
        and the moves the engine will accept cannot drift apart.
        """
        return self.group in BUYOUT_ELIGIBLE_GROUPS

    @classmethod
    def from_pool(cls, player: Player, salary: float) -> "PlayerOnRoster":
        """A pool player, bought at `salary`. The one door into a roster.

        Carries the RFA group conversion the CBA requires on a sale: an RFA1
        signs to group 2 and an RFA2 to group 3. That is not cosmetic —
        `counts_on_cap` reads the group, and MINOR_CAP_GROUPS is {"2", "3"},
        so a player left on his pool group would sit in the minors costing
        nothing against the cap. /assign did this inline and scenario setup did
        not do it at all, which was harmless only while scenarios never put a
        purchase in the minors.

        is_keeper stays False by construction: a purchase is not provenance.
        """
        group = player.group
        if group == "RFA1":
            group = "2"
        elif group == "RFA2":
            group = "3"
        return cls(
            name=player.name,
            position=player.position,
            group=group,
            salary=salary,
            projected_points=player.projected_points,
            nhl_team=player.nhl_team,
        )


def _floor_to_increment(amount: float) -> float:
    """Largest committable amount not exceeding `amount`.

    Money moves in $0.1M steps, so cap space below one increment can never
    actually be spent. Sub-increment residue is NOT always float noise:
    a buyout penalty is 50% of salary, so buying out a $2.1M player leaves a
    genuine $1.05M on the cap. Rounding that to nearest invents $0.05M of
    space (10 of 100 reachable penalty values) — enough for the MILP to plan
    a roster over the cap and for physical_max_bid to report a bid the team
    cannot make. Floor, so the error is always in the safe direction.

    Scaling to integer hundredths first absorbs float error, so a genuine
    4.2 arriving as 4.199999999999996 still floors to 4.2 rather than 4.1.
    """
    return (round(amount * 100) // 10) / 10.0


def _index_of(players: list[PlayerOnRoster], name: str) -> int | None:
    """Index of the named player in a list, or None if absent.

    Returns the index rather than the player so callers can validate before
    mutating — send_to_minors must reject an un-benched player while leaving
    them exactly where they were.
    """
    for i, p in enumerate(players):
        if p.name == name:
            return i
    return None


@dataclass
class TeamState:
    """State of one FCHL team during the auction."""

    code: str
    name: str
    keeper_players: list[PlayerOnRoster] = field(default_factory=list)
    minor_players: list[PlayerOnRoster] = field(default_factory=list)
    acquired_players: list[PlayerOnRoster] = field(default_factory=list)
    penalties: float = 0.0
    is_done: bool = False
    colors: dict[str, str] = field(default_factory=dict)
    logo: str = ""
    is_my_team: bool = False
    _roster_cache: list[PlayerOnRoster] | None = field(default=None, repr=False)

    def _invalidate_cache(self) -> None:
        self._roster_cache = None

    @property
    def roster_players(self) -> list[PlayerOnRoster]:
        """All players on active roster (keepers + acquired, NOT minors)."""
        if self._roster_cache is None:
            self._roster_cache = self.keeper_players + self.acquired_players
        return self._roster_cache

    @property
    def all_players(self) -> list[PlayerOnRoster]:
        """All players including minors."""
        return self.roster_players + self.minor_players

    @property
    def total_salary(self) -> float:
        """Cap-counted salary: roster salaries + cap-eligible minor salaries + penalties."""
        roster_sal = sum(p.salary for p in self.roster_players)
        minor_sal = sum(p.salary for p in self.minor_players if p.counts_on_cap)
        return roster_sal + minor_sal + self.penalties

    @property
    def remaining_budget(self) -> float:
        """How much cap space is left, floored to what can actually be spent.

        Left raw this returned values like 4.199999999999996, and the MILP
        budget constraint then rejected a bid at exactly 4.2 — costing a team
        $0.1M of real headroom at its own physical max. Floored rather than
        rounded so a buyout's half-increment penalty can never inflate it;
        see _floor_to_increment.
        """
        return _floor_to_increment(SALARY_CAP - self.total_salary)

    @property
    def roster_count(self) -> int:
        """Active roster size (keepers + acquired, NOT minors)."""
        return len(self.roster_players)

    @property
    def bench_count(self) -> int:
        """How many active-roster players are benched.

        Over `roster_players`, so minors are excluded by construction — and that
        is the point, not an oversight. `send_to_minors` and `add_minor_player`
        force `is_bench = True` on the way down so a later recall lands on the
        bench instead of displacing a starter, which makes a minor's flag a
        travel marker rather than a roster slot. Counting `all_players` here
        would put a team with 37 minors permanently over the cap.
        """
        return sum(1 for p in self.roster_players if p.is_bench)

    @property
    def total_spots_remaining(self) -> int:
        """How many more players can be added to active roster."""
        return ROSTER_SIZE - self.roster_count

    @property
    def position_counts(self) -> dict[str, int]:
        """F/D/G counts on active roster (not minors)."""
        counts = {"F": 0, "D": 0, "G": 0}
        for p in self.roster_players:
            counts[p.position] = counts.get(p.position, 0) + 1
        return counts

    @property
    def roster_needs(self) -> dict[str, int]:
        """How many more F/D/G needed to meet position minimums."""
        counts = self.position_counts
        return {
            pos: max(0, minimum - counts.get(pos, 0))
            for pos, minimum in POSITION_MINIMUMS.items()
        }

    @property
    def min_budget_reserved(self) -> float:
        """Budget that must be reserved for remaining roster spots at MIN_SALARY.

        Clamped at zero: total_spots_remaining goes NEGATIVE past 24, and a
        negative reserve would *inflate* spendable_budget above the team's
        actual remaining budget — a number the league table shows.
        """
        return max(0, self.total_spots_remaining) * MIN_SALARY

    @property
    def spendable_budget(self) -> float:
        """Budget available for the next pick (remaining minus reserved)."""
        return self.remaining_budget - self.min_budget_reserved

    @property
    def physical_max_bid(self) -> float:
        """Maximum this team can bid on any single player.

        A full roster is NOT a zero ceiling. The CBA lets teams draft past 24;
        the extra goes to minors, and since every biddable player ends up in
        group 2 or 3 (/assign converts RFA1->2, RFA2->3, and both are in
        MINOR_CAP_GROUPS) that salary counts fully on the cap. Owner confirmed
        2026-08-05 that teams in this league do it. Returning 0.0 here made a
        cap-rich full team invisible to market.py, so every late-draft ceiling
        read too low and BOT under-bid exactly when the money was on the table.

        Both branches say the same thing. With spots left, the bid FILLS a
        reserved spot, so its MIN_SALARY reservation is replaced by the actual
        amount — hence spendable + MIN_SALARY. With no spots left there is no
        reservation to replace, so the whole remaining budget is biddable.
        """
        ceiling = (
            self.remaining_budget
            if self.total_spots_remaining <= 0
            else self.spendable_budget + MIN_SALARY
        )
        # Clamp at 0 so over-committed teams read as "can't bid", not a
        # nonsense negative ceiling in templates and projections.
        return _floor_to_increment(max(0.0, min(ceiling, MAX_SALARY)))

    @property
    def current_roster_points(self) -> int:
        """Projected points from the best starting lineup alone -- the
        "starters" figure for display. Decisions use expected_roster_points."""
        return lineup_points(self.roster_players)

    @property
    def expected_roster_points(self) -> float:
        """Starters plus expected bench contribution (see `expected_points`)."""
        return expected_points(self.roster_players)

    def find_player(self, name: str) -> PlayerOnRoster | None:
        """Find a player by name across all lists."""
        # Bind once: all_players concatenates a fresh list on every access.
        players = self.all_players
        i = _index_of(players, name)
        return players[i] if i is not None else None

    def remove_player(self, name: str) -> PlayerOnRoster:
        """Remove and return a player by name. Raises ValueError if not found."""
        for player_list in [self.keeper_players, self.acquired_players, self.minor_players]:
            i = _index_of(player_list, name)
            if i is not None:
                self._invalidate_cache()
                return player_list.pop(i)
        raise ValueError(f"Player '{name}' not found on team {self.code}")

    def add_acquired_player(self, player: PlayerOnRoster) -> bool:
        """Add a drafted or traded player. Returns True if routed to the minors.

        CBA: 24 active, extras go to the minors with salary fully on cap.

        The check lives here because this method is the one door every path
        takes to seat a player — drafts, both trade directions, evaluation
        clones, scenario setup. A guard at the endpoints instead would have to
        be repeated at each, and the one that got missed would put roster_count
        at 25, where `lineup_points` lets the extra player compete for a
        starting slot he cannot legally hold. Measured, one 120-point forward on
        an otherwise full roster was worth 70 phantom points — and that is the
        number evaluate_trade accepts or declines on.

        (Deliberately not listing the call sites: the first draft of this
        docstring counted them, and a refactor in the same commit made the count
        wrong before it was ever read.)

        Callers that need to tell the operator where the player went read the
        return value; the rest can ignore it.
        """
        if self.roster_count >= ROSTER_SIZE:
            self.add_minor_player(player)
            return True

        self.acquired_players.append(player)
        self._invalidate_cache()
        return False

    def add_minor_player(self, player: PlayerOnRoster) -> None:
        """Seat a player straight in the minors, bypassing the active roster.

        The overflow branch of add_acquired_player is one caller; scenario setup
        is the other, and it needs this door because it stashes depth on teams
        that still have active spots — where add_acquired_player would seat them
        on the roster instead. Without it a caller has to reach through
        `_invalidate_cache`, and the flags below are exactly the sort of thing
        that gets forgotten one copy at a time.

        Benched on arrival, mirroring send_to_minors' precondition, so a later
        recall lands on the bench instead of displacing a starter.
        """
        player.is_minor = True
        player.is_bench = True
        self.minor_players.append(player)
        self._invalidate_cache()

    def set_bench(self, player_name: str, bench: bool) -> None:
        """Bench or activate a roster player, capped at BENCH_SIZE benched.

        The CBA roster is 24 with a 20-man starting lineup, so at most 4 sit on
        the bench. Nothing enforced that until now: `is_bench` reaches no engine
        module at all — the MILP picks its own starters and `lineup_points`
        reads every roster player regardless — so a 5th benched player was a
        bookkeeping lie the tool would happily record and never notice.

        Validated before mutating, so a refusal leaves the flag exactly as it
        was; `main.py` can therefore use `_undoable(rollback=False)` and a
        rejected click costs no undo depth.

        Activating is never refused. It is the way out of a full bench, and
        gating it would deadlock the one workflow this cap makes harder:
        demoting a starter needs a free bench slot to stage him in.
        """
        p = self.find_player(player_name)
        if p is None:
            raise ValueError(f"Player '{player_name}' not on {self.code}")
        if bench and not p.is_bench and self.bench_count >= BENCH_SIZE:
            raise ValueError(
                f"{self.code}'s bench is full ({BENCH_SIZE}) — activate someone "
                f"before benching '{player_name}'"
            )
        p.is_bench = bench

    def send_to_minors(self, player_name: str) -> None:
        """Move an active-roster player to minors. Player must be benched first.

        Keepers may go down too. "Keeper" only records that a player was on an
        FCHL team before the auction — provenance, not a league rule. Refusing
        them used to strand the one legal move a group A-E player has: those
        can't be bought out, and the minors is where their cap hit goes to zero.

        Provenance survives the trip: `is_keeper` is set on the player, so
        recall_from_minors puts a keeper back where he came from.

        This docstring used to add "and nothing else in the app branches on it
        (every other reader just concatenates keepers + acquired)". That was
        false when written — `team_panel.html` had already coloured rows
        `text-success` off `acquired_players` alone since f440053 — and it is
        the sentence that justified recalling every player into
        `acquired_players`, which relabelled demoted keepers as purchases for
        three months. Provenance IS read, in two places. Don't put it back.
        """
        for source in (self.acquired_players, self.keeper_players):
            i = _index_of(source, player_name)
            if i is None:
                continue
            # Validate before mutating: a rejected send must leave the player
            # exactly where they were.
            if not source[i].is_bench:
                raise ValueError(
                    f"'{player_name}' must be benched before being sent to minors"
                )
            source[i].is_minor = True
            self.minor_players.append(source.pop(i))
            self._invalidate_cache()
            return
        raise ValueError(f"Player '{player_name}' not on active roster of {self.code}")

    def recall_from_minors(self, player_name: str) -> None:
        """Move a player from minors back onto the active roster.

        Back to the list he came from: `keeper_players` for someone who was on
        an FCHL team before the auction, `acquired_players` for a draftee. The
        two lists are the only record of that distinction, so recalling
        everybody into `acquired_players` relabelled a keeper as a player BOT
        had bought — and `team_panel.html` colours rows green on exactly that,
        so the change was visible and permanent.

        The one move that cannot auto-route to the minors when the roster is
        full — recalling INTO a full roster is the illegal act itself, and the
        player is already where the overflow would go. Refused, and validated
        before mutating so a rejected recall leaves the player untouched.
        """
        i = _index_of(self.minor_players, player_name)
        if i is not None:
            if self.roster_count >= ROSTER_SIZE:
                raise ValueError(
                    f"{self.code}'s active roster is full ({ROSTER_SIZE}) — "
                    f"bench a player and send them down before recalling "
                    f"'{player_name}'"
                )
            # The second bench-adding path, and the one a cap on /toggle-bench
            # alone leaves wide open: a player demoted through send_to_minors
            # keeps is_bench, so recalling him lands him ON the bench. Guarded on
            # the flag rather than unconditionally, because minors loaded from
            # the CSV carry is_bench=False (measured: 0 of 149 at reset) and
            # recalling one of those lands him active, costing no bench slot.
            if self.minor_players[i].is_bench and self.bench_count >= BENCH_SIZE:
                raise ValueError(
                    f"{self.code}'s bench is full ({BENCH_SIZE}) and "
                    f"'{player_name}' comes back benched — activate someone first"
                )
            self.minor_players[i].is_minor = False
            recalled = self.minor_players.pop(i)
            destination = (
                self.keeper_players if recalled.is_keeper else self.acquired_players
            )
            destination.append(recalled)
            self._invalidate_cache()
            return
        raise ValueError(f"Player '{player_name}' not in minors on {self.code}")

    def adjust_salary(self, player_name: str, new_salary: float) -> None:
        """Correct a player's salary (typo fix)."""
        p = self.find_player(player_name)
        if p is None:
            raise ValueError(f"Player '{player_name}' not found on team {self.code}")
        p.salary = new_salary
        self._invalidate_cache()


@dataclass
class ChangeRecord:
    """A non-transaction state change (salary edits, bench toggles, etc.).

    Separate from TransactionRecord because these don't move players between
    teams — they're audit trail for hand-corrections during the draft.
    """

    timestamp: str
    kind: str  # "adjust-salary" | "toggle-bench" | "move-to-minors" | "move-to-roster" | "team-done"
    team_code: str
    description: str


@dataclass
class TransactionRecord:
    """Record of a single auction transaction."""

    player_name: str
    position: str
    team_code: str
    salary: float
    model_price: float
    market_price: float
    timestamp: str
    # The real vocabulary, verified against every _log_transaction call site:
    # "draft" | "trade_out" | "trade_in" | "trade" | "buyout". This said
    # "trade_give"/"trade_receive" until 2026-08-11 — two values the code has
    # never emitted — and a first draft of /undo's view mirror was reasoned
    # against it. Those are the LOCAL VARIABLE names in /trade-execute
    # (main.py, `trade_give = last_trade_eval.give`), which is presumably where
    # the wrong pair came from: the writer read the code that builds the trade
    # rather than the strings it logs. Anything branching on this must
    # ALLOWLIST, because
    # /trade-between (the "trade" writer) puts f"{source}→{dest}" in team_code,
    # so that field is not always a team code — `_log_team_link.html` guards on
    # `in teams` for exactly this, and `logs_panel.html` splits the log on it.
    # Note that split is a TOTAL partition (`draft` vs everything else) rather
    # than an allowlist, on purpose: there a mis-classified record would vanish
    # from the log instead of merely landing in the wrong tab.
    transaction_type: str
    # The NHL club AT THE TIME OF THE TRANSACTION. Denormalised on purpose: the
    # log outlives the roster. A bought-out player is on no roster and gone from
    # the pool, so resolving the club by name at render time draws nothing on
    # exactly the rows the Transaction tab exists for.
    nhl_team: str = ""


# ---------------------------------------------------------------------------
# "Where is this player?" — the one global finder.
# ---------------------------------------------------------------------------
#
# Nothing else in the app answers this. `TeamState.find_player` searches ONE
# team; `main._nhl_team_of` is the only function that scans every team plus the
# pool and it throws the answer away, returning the NHL club and discarding
# where it found him. So a rostered player was findable only by opening the
# right panel out of eleven, a minor only inside a second table in that panel,
# and a bought-out player nowhere at all.

# What an index entry points at. Three unrelated shapes that happen to share
# `position` and `nhl_team`; `_locate` branches on `where` rather than on the
# type, because `where` is also what the template branches on.
Searchable = Player | PlayerOnRoster | TransactionRecord

SEARCH_MIN_QUERY = 2  # one character matches hundreds; see the tier table below
SEARCH_LIMIT = 10


# Matching folds a name to LETTERS ONLY, plus the offset each word starts at.
# Punctuation is not cosmetic here: `players.csv` today carries an apostrophe
# (`Ryan O'Reilly`, `K'Andre Miller`), thirteen real hyphens
# (`Ryan Nugent-Hopkins`), the parentheses `_disambiguated_names` adds
# (`Matt Murray (DAL)`), and — measured 2026-09-10 — FOUR names where the file
# encodes the hyphen as the digit `0`: `Oliver Ekman0Larsson`,
# `Nicolas Aube0Kubel`, `Alex Barre0Boulet`, `Trey Fix0Wolansky`. CLAUDE.md
# already records U+0060 backticks in the same file.
#
# So a fold that keeps punctuation makes the operator reproduce the file's
# punctuation exactly, INCLUDING a data-entry bug they have no way of knowing
# about. Measured against the shipped fold: `oreilly`, `kandre`,
# `ekman-larsson`, `ekman larsson`, `barre-boulet`, `fix-wolansky`,
# `aube-kubel` and `nugent hopkins` ALL returned zero hits for players who are
# in the pool right now. Letters-only, all eight return exactly the right man.
#
# The offsets are what makes that work without demoting him. A word prefix has
# to be matched at a BOUNDARY inside the concatenation, not against a split
# list: `barreboulet` is no `.split()` token of `alexbarreboulet`, but it does
# start at offset 4, which is where `barre` begins. Matching the split list
# instead put every one of those names in the substring tier, below anyone
# whose surname simply starts with the query.
#
# Memoized because the folding, not the searching, is the cost, and the names
# are stable strings re-folded on every keystroke. Measured 2026-09-10 over
# the live 953 names, one full query: **0.21ms** warm against **0.80ms** cold.
# The sweep alone is 0.455ms unmemoized — cheaper than the 0.770ms the old
# NFKD-everything fold cost, because `isascii()` skips the normalize for all
# 953 of today's names. Bounded rather than unbounded: the keys are pool names
# plus every prefix the operator types, and `players.csv` is replaced between
# drafts.
_NON_LETTER = re.compile(r"[^a-z]+")


@lru_cache(maxsize=4096)
def _fold(text: str) -> tuple[str, tuple[int, ...]]:
    """Letters of `text` lower-cased and de-accented, and where its words start.

    NFKD splits an accented character into a base letter plus a combining
    mark, which dropping category "Mn" then removes — so "Tomáš" folds to
    "tomas" and a name typed off a broadcast graphic still finds him. Guarded
    by `isascii()` because, measured 2026-09-10, the live pool has ZERO
    non-ASCII names: the normalize is pure cost on today's data. It is not
    dead code — `players.csv` is replaced before every draft and NHL rosters
    carry diacritics routinely — but a test asserting it against the live pool
    could not fail, so the test SUPPLIES a name that needs folding.

    Offsets are cumulative over the LETTERS, not indices into `text`: they are
    only ever used as the `start` argument of `str.startswith`, and there is
    no caller that needs to map one back to the original string.
    """
    low = text.lower()
    if not low.isascii():
        low = "".join(
            ch for ch in unicodedata.normalize("NFKD", low)
            if unicodedata.category(ch) != "Mn"
        )
    words, starts, at = [], [], 0
    for word in _NON_LETTER.split(low):
        if word:
            starts.append(at)
            words.append(word)
            at += len(word)
    return "".join(words), tuple(starts)


@dataclass(frozen=True)
class PlayerLocation:
    """Where one player is right now, and what he costs there."""

    name: str
    # "pool" | "roster" | "bench" | "minors" | "bought-out". An allowlist, and
    # a template branching on it must treat an unknown value as "say nothing"
    # rather than borrow another branch's wording.
    where: str
    team_code: str | None  # None only for "pool"
    position: str
    nhl_team: str
    # 0 for a bought-out player: the log does not record points, and guessing
    # them from a name that is no longer in any collection is not possible.
    projected_points: int
    # The player's CAP HIT, or None when he has none. None in the pool is not
    # missing data: `Player.salary` is LAST season's salary — the price model's
    # lag feature, where 0 means "new to the league" — while
    # `PlayerOnRoster.salary` is the actual cap hit. They are different
    # quantities, and rendering the first beside the second would be wrong on
    # every pool row.
    salary: float | None
    # Whether THIS PLAYER's salary is on his team's cap right now. False for a
    # bought-out player: he is gone and it is the penalty that remains, which
    # is a different number (50% of `salary`) and is named separately.
    counts_on_cap: bool
    last_txn: TransactionRecord | None  # provenance: how he got where he is


@dataclass(frozen=True)
class PlayerSearch:
    """The answer to one query."""

    query: str
    hits: tuple[PlayerLocation, ...]
    # Matches BEFORE the limit was applied, so the caller can say "+N more"
    # rather than silently truncating.
    total: int


def _placement(player: PlayerOnRoster, in_minors: bool) -> str:
    """Which of the three roster locations a player is in.

    `in_minors` comes from WHICH LIST he was found in, not from `is_minor`,
    because the list is what `all_players` and `total_salary` read. The bench
    has no list of its own — it is a flag — so that half does read `is_bench`.
    Order matters: `send_to_minors` forces `is_bench = True` on the way down,
    so a flag-first reading would report every minor as benched.
    """
    if in_minors:
        return "minors"
    return "bench" if player.is_bench else "roster"


@dataclass
class AuctionState:
    """Complete state of the auction at any point in time."""

    teams: dict[str, TeamState] = field(default_factory=dict)
    available_players: dict[str, Player] = field(default_factory=dict)
    transaction_log: list[TransactionRecord] = field(default_factory=list)
    change_log: list[ChangeRecord] = field(default_factory=list)
    # Purely a DISPLAY order now: the standings rows, the bidder-toggle grid,
    # the trade-partner dropdown and `default_bidders` all iterate it so the
    # league appears in one stable order. The turn pointer that used to index
    # it was removed 2026-09-11 -- it gated nothing the tool computes.
    nomination_order: list[str] = field(default_factory=list)
    # The per-position "typical player" the price-driver breakdown measures
    # against, frozen at draft time for exactly the reason `Player.pos_rank`
    # is: the pool shrinks. Recomputed against a drafted-down pool the
    # reference log_mu moves F -0.194, D -0.139, G -0.794, so an explanation
    # given at pick 10 would silently disagree with the same one at pick 170.
    #
    # On the state rather than a `main.py` global because `available_players`
    # is what a mid-draft reload restores: a global rebuilt at startup would be
    # computed from the surviving pool and be wrong for the rest of the draft.
    price_reference: dict[str, dict[str, float]] = field(default_factory=dict)
    _snapshots: list[str] = field(default_factory=list, repr=False)

    def capture_snapshot(self) -> str:
        """Serialize the current state WITHOUT putting it on the undo chain.

        Split out from `save_snapshot` so an endpoint that might reject can
        capture the pre-state, attempt the operation, and only commit on
        success. `save_snapshot` is not free to call speculatively: it evicts
        the oldest entry the moment the chain passes MAX_SNAPSHOTS, so
        snapshot-then-restore is not the no-op it reads as — it destroys a real
        undo step. Measured 2026-08-07 on a full chain: a rejected
        /move-to-minors took the depth from 50 to 49 and the oldest snapshot
        was gone.
        """
        return self.to_json(include_snapshots=False)

    def commit_snapshot(self, snapshot: str) -> None:
        """Put an already-captured snapshot on the chain. Keeps last MAX_SNAPSHOTS.

        The eviction lives here rather than in `capture_snapshot` on purpose:
        it is the act of committing that costs chain depth, so a captured-but-
        never-committed snapshot costs nothing at all.
        """
        self._snapshots.append(snapshot)
        if len(self._snapshots) > MAX_SNAPSHOTS:
            self._snapshots.pop(0)

    def save_snapshot(self) -> None:
        """Save current state for undo. Keeps last MAX_SNAPSHOTS.

        Right for the endpoints that cannot reject after this point. One that
        can should capture/commit instead, so a refusal leaves the chain
        untouched.
        """
        self.commit_snapshot(self.capture_snapshot())

    def rollback_to(self, snapshot: str) -> None:
        """Restore a captured snapshot WITHOUT touching the undo chain.

        For an endpoint undoing its own failed attempt. `restore_snapshot`
        spends a chain entry on purpose because that is what Ctrl+Z means; a
        rejected request must not, or a mis-click erodes the operator's undo
        history at the one moment there is nothing behind it.

        Enumerates the dataclass fields rather than listing them, because a
        hand-written list fails open: add a field to AuctionState and undo
        silently stops restoring it. `tests/test_state.py::TestSnapshotFieldsCannotDrift`
        covers the other half — a field that never reaches the JSON at all.
        """
        restored = AuctionState.from_json(snapshot)
        for f in fields(self):
            # The undo CHAIN is not part of what undo restores. Snapshots are
            # written with include_snapshots=False, so restored._snapshots is
            # always the empty default — copying it would wipe the chain and
            # make the second Ctrl+Z do nothing. Skipped by name and not by a
            # leading-underscore rule, so a future private field is restored by
            # default; the round-trip guards are what make that the safe way to
            # be wrong.
            if f.name == "_snapshots":
                continue
            setattr(self, f.name, getattr(restored, f.name))

    def restore_snapshot(self) -> bool:
        """Restore the most recent snapshot. Returns False if no snapshots.

        This is Ctrl+Z: it SPENDS a chain entry. An endpoint rolling back its
        own rejected attempt wants `rollback_to`.
        """
        if not self._snapshots:
            return False
        self.rollback_to(self._snapshots.pop())
        return True

    def _searchable(self) -> dict[str, tuple[str, str | None, Searchable]]:
        """Every findable name, resolved to ONE location per name.

        Per NAME, not per player, which is safe because a name IS a player
        here: `_disambiguated_names` runs over EVERY row of players.csv,
        rostered and biddable alike, so a keeper and a free agent sharing a
        string are both renamed at load, and `test_player_identity.py` pins
        the rule and the live data. This docstring said until 2026-09-22 that
        only the biddable pool was renamed, so two people could share a key
        here. That was never true of the loader (CHANGELOG.md, Investigated),
        and `test_stress.py`'s ownership check holds it through picks and undo.

        Resolution order mirrors `TeamState.find_player` and `remove_player`:
        keeper, then acquired, then minors, then the pool, then the log. The
        three roster lists are *intended* to be disjoint but nothing enforces
        it — `add_acquired_player` and `add_minor_player` append with no name
        check — so using their order here is what keeps a search hit and a
        `find_player` hit from naming different rows.

        Live state beats the log, always, and `setdefault` is what enforces
        it. `execute_buyout` removes a player from every list and leaves only
        a nameless float in that team's `penalties`, so his `buyout` record is the
        sole evidence he existed — but the log is append-only HISTORY, and a
        name appearing in it is not evidence of where he is now. Only the
        collections are.

        Reversing that order is not caught by anything the app can reach on
        its own, which is why `test_a_buyout_row_never_outranks_a_live_roster`
        supplies the state directly. `/undo` does not produce it: it restores
        a snapshot taken BEFORE the buyout, so the row goes with it. (An
        earlier draft of this comment cited undo as the reason and was simply
        wrong — the mutant survived the undo test, which is how it was found.)
        """
        index: dict[str, tuple[str, str | None, Searchable]] = {}
        for code, team in self.teams.items():
            for in_minors, group in (
                (False, team.keeper_players),
                (False, team.acquired_players),
                (True, team.minor_players),
            ):
                for player in group:
                    if player.name not in index:
                        index[player.name] = (
                            _placement(player, in_minors), code, player,
                        )
        for name, player in self.available_players.items():
            index.setdefault(name, ("pool", None, player))
        for txn in self.transaction_log:
            if txn.transaction_type == "buyout":
                index.setdefault(txn.player_name, ("bought-out", txn.team_code, txn))
        return index

    def locate_players(self, query: str, limit: int = SEARCH_LIMIT) -> PlayerSearch:
        """Find a player anywhere — pool, roster, bench, minors, or bought out.

        Named `locate_players` and not `find_player` deliberately:
        `TeamState.find_player` is an exact-match lookup on one team, and
        mistaking a fuzzy multi-hit search for it is the confusion the name
        exists to prevent.

        TWO tiers — a prefix of the whole name or of any word in it, then a
        bare substring — ranked within each by projected points, then name. A
        bare substring alone is unusable at this pool size; measured over the
        live 953 names, `an` matches 229 of them and `er` 216.

        The two halves of that are separate decisions and each has a measured
        reason.

        **Why prefix-anywhere is ONE tier.** A full-name prefix looks like the
        stronger match and is not: with no space in the query it means only
        "his FIRST name starts with this", which is not what anyone types.
        Ranked above surnames it fills all ten slots with the wrong people —
        measured, `ma` returned MacKenzie Entwistle, MacKenzie Weegar and
        Mackenzie Blackwood while hiding Nathan MacKinnon, Auston Matthews and
        Cale Makar behind "+127 more", and `hu` led with Hudson Fasching while
        burying every Hughes. A query that does contain a space still matches
        as a full prefix (`connor mcd`), so collapsing loses nothing.

        **Why points, not the alphabet.** Ten of 100 matches have to be
        chosen, and alphabetical order is arbitrary at that ratio — `smi` led
        with Brendan Smith over Reilly Smith. The subject object is already in
        the index, so this costs nothing. Consequence, deliberate: a
        bought-out player's subject is a `TransactionRecord`, which carries no
        points, so he sorts to the BOTTOM of his tier. Acceptable because a
        buyout search is a near-exact surname with one or two hits — but it is
        a decision, not an accident, and `getattr` is where it lives.
        """
        folded_query, _ = _fold(query.strip())
        if len(folded_query) < SEARCH_MIN_QUERY:
            return PlayerSearch(query=query, hits=(), total=0)

        index = self._searchable()
        prefixes: list[str] = []
        substrings: list[str] = []
        for name in index:
            letters, starts = _fold(name)
            if any(letters.startswith(folded_query, at) for at in starts):
                prefixes.append(name)
            elif folded_query in letters:
                substrings.append(name)

        def rank(name: str) -> tuple[int, str]:
            return (-getattr(index[name][2], "projected_points", 0), name)

        ranked = sorted(prefixes, key=rank) + sorted(substrings, key=rank)
        # Rank on NAMES and materialize only the survivors: the index is ~950
        # entries and all but `limit` of them would be thrown away.
        hits = tuple(self._locate(name, *index[name]) for name in ranked[:limit])
        return PlayerSearch(query=query, hits=hits, total=len(ranked))

    def _locate(
        self, name: str, where: str, team_code: str | None, subject: Searchable
    ) -> PlayerLocation:
        """One index entry as a PlayerLocation, plus its provenance."""
        last_txn = next(
            (t for t in reversed(self.transaction_log) if t.player_name == name),
            None,
        )
        if where == "pool":
            return PlayerLocation(
                name=name, where=where, team_code=None,
                position=subject.position, nhl_team=subject.nhl_team,
                projected_points=subject.projected_points,
                salary=None, counts_on_cap=False, last_txn=last_txn,
            )
        if where == "bought-out":
            # Everything here comes off the TransactionRecord, which is all
            # that survives him. It denormalises `nhl_team` for exactly this
            # reason; it does not carry group or points, so those stay empty
            # rather than being guessed.
            return PlayerLocation(
                name=name, where=where, team_code=team_code,
                position=subject.position, nhl_team=subject.nhl_team,
                projected_points=0,
                salary=subject.salary, counts_on_cap=False, last_txn=last_txn,
            )
        return PlayerLocation(
            name=name, where=where, team_code=team_code,
            position=subject.position, nhl_team=subject.nhl_team,
            projected_points=subject.projected_points,
            salary=subject.salary, counts_on_cap=subject.counts_on_cap,
            last_txn=last_txn,
        )

    def to_json(self, include_snapshots: bool = True) -> str:
        """Serialize state to JSON string."""
        data = {
            "teams": {
                code: _team_to_dict(team) for code, team in self.teams.items()
            },
            "available_players": {
                name: _player_to_dict(p) for name, p in self.available_players.items()
            },
            "transaction_log": [_transaction_to_dict(t) for t in self.transaction_log],
            "change_log": [_change_to_dict(c) for c in self.change_log],
            "nomination_order": self.nomination_order,
            "price_reference": self.price_reference,
        }
        if include_snapshots:
            data["_snapshots"] = self._snapshots
        return json.dumps(data)

    @classmethod
    def from_json(cls, json_str: str) -> AuctionState:
        """Deserialize state from JSON string."""
        data = json.loads(json_str)
        state = cls()
        state.teams = {
            code: _team_from_dict(d) for code, d in data["teams"].items()
        }
        state.available_players = {
            name: _player_from_dict(d)
            for name, d in data["available_players"].items()
        }
        state.transaction_log = [
            _transaction_from_dict(d) for d in data["transaction_log"]
        ]
        state.change_log = [
            _change_from_dict(d) for d in data.get("change_log", [])
        ]
        state.nomination_order = data["nomination_order"]
        state.price_reference = data.get("price_reference", {})
        state._snapshots = data.get("_snapshots", [])
        return state


# -- Serialization helpers --

def _player_on_roster_to_dict(p: PlayerOnRoster) -> dict:
    return {
        "name": p.name,
        "position": p.position,
        "group": p.group,
        "salary": p.salary,
        "projected_points": p.projected_points,
        "nhl_team": p.nhl_team,
        "is_minor": p.is_minor,
        "is_bench": p.is_bench,
        "is_keeper": p.is_keeper,
    }


def _player_on_roster_from_dict(d: dict) -> PlayerOnRoster:
    return PlayerOnRoster(
        name=d["name"],
        position=d["position"],
        group=d["group"],
        salary=d["salary"],
        projected_points=d["projected_points"],
        nhl_team=d.get("nhl_team", ""),
        is_minor=d.get("is_minor", False),
        is_bench=d.get("is_bench", False),
        # .get so a state file written before this field existed still loads —
        # a draft four hours in must not fail to parse over a colour. For the
        # two active lists _team_from_dict then overwrites this from the list
        # itself, which is authoritative; only minors rely on the stored value.
        is_keeper=d.get("is_keeper", False),
    )


def _team_to_dict(t: TeamState) -> dict:
    return {
        "code": t.code,
        "name": t.name,
        "keeper_players": [_player_on_roster_to_dict(p) for p in t.keeper_players],
        "minor_players": [_player_on_roster_to_dict(p) for p in t.minor_players],
        "acquired_players": [_player_on_roster_to_dict(p) for p in t.acquired_players],
        "penalties": t.penalties,
        "is_done": t.is_done,
        "colors": t.colors,
        "logo": t.logo,
        "is_my_team": t.is_my_team,
    }


def _team_from_dict(d: dict) -> TeamState:
    team = TeamState(
        code=d["code"],
        name=d["name"],
        keeper_players=[_player_on_roster_from_dict(p) for p in d["keeper_players"]],
        minor_players=[_player_on_roster_from_dict(p) for p in d["minor_players"]],
        acquired_players=[_player_on_roster_from_dict(p) for p in d["acquired_players"]],
        penalties=d["penalties"],
        is_done=d.get("is_done", False),
        colors=d.get("colors", {}),
        logo=d.get("logo", ""),
        is_my_team=d.get("is_my_team", False),
    )
    # The list a player is in decides his provenance, not the stored flag: for
    # the two active lists, position IS the record and has been since long
    # before `is_keeper` existed. Overwriting rather than trusting the file
    # means a state saved before this field self-heals on load instead of
    # colouring every keeper as a draftee.
    #
    # `minor_players` is deliberately absent: it is the one list that cannot
    # re-derive provenance from position, which is the whole reason the field
    # exists. Legacy saves fall back to `_backfill_keeper_flags` in main.py.
    for p in team.keeper_players:
        p.is_keeper = True
    for p in team.acquired_players:
        p.is_keeper = False
    team._invalidate_cache()
    return team


def _player_to_dict(p: Player) -> dict:
    return {
        "name": p.name,
        "position": p.position,
        "group": p.group,
        "nhl_team": p.nhl_team,
        "age": p.age,
        "projected_points": p.projected_points,
        "is_rfa": p.is_rfa,
        "salary": p.salary,
        "team_probability": p.team_probability,
        "prior_fchl_team": p.prior_fchl_team,
        "pos_rank": p.pos_rank,
        "proj_wins": p.proj_wins,
    }


def _player_from_dict(d: dict) -> Player:
    return Player(
        name=d["name"],
        position=d["position"],
        group=d["group"],
        nhl_team=d["nhl_team"],
        age=d["age"],
        projected_points=d["projected_points"],
        is_rfa=d["is_rfa"],
        salary=d["salary"],
        team_probability=d["team_probability"],
        prior_fchl_team=d.get("prior_fchl_team", ""),
        pos_rank=d.get("pos_rank", 0),
        proj_wins=d.get("proj_wins"),
    )


def _transaction_to_dict(t: TransactionRecord) -> dict:
    return {
        "player_name": t.player_name,
        "position": t.position,
        "team_code": t.team_code,
        "salary": t.salary,
        "model_price": t.model_price,
        "market_price": t.market_price,
        "timestamp": t.timestamp,
        "transaction_type": t.transaction_type,
        "nhl_team": t.nhl_team,
    }


def _change_to_dict(c: ChangeRecord) -> dict:
    return {
        "timestamp": c.timestamp,
        "kind": c.kind,
        "team_code": c.team_code,
        "description": c.description,
    }


def _change_from_dict(d: dict) -> ChangeRecord:
    return ChangeRecord(
        timestamp=d["timestamp"],
        kind=d["kind"],
        team_code=d["team_code"],
        description=d["description"],
    )


def _transaction_from_dict(d: dict) -> TransactionRecord:
    return TransactionRecord(
        player_name=d["player_name"],
        position=d["position"],
        team_code=d["team_code"],
        salary=d["salary"],
        model_price=d["model_price"],
        market_price=d["market_price"],
        timestamp=d["timestamp"],
        transaction_type=d["transaction_type"],
        # .get, not d["nhl_team"]. Every save file written before this field
        # exists lacks the key, which makes the legacy case the NORMAL case on
        # the first boot after this change. A KeyError here fails the PARSE, and
        # per `_load_saved_state` only the parse decides usability — so a bare
        # lookup would rename a byte-perfect draft `.corrupt` and start fresh,
        # four hours in, over a logo. Same reason `_roster_player_from_dict`
        # reads its own `nhl_team` with `.get` (above); `_player_from_dict` uses
        # a bare lookup because a POOL player has carried the field since the
        # first release, so a file missing it there is unreadable anyway.
        nhl_team=d.get("nhl_team", ""),
    )

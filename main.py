"""FastAPI app: all HTTP endpoints for the auction simulator."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from datetime import datetime

from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from functools import lru_cache, partial
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
# The two manual scans hand their MILP loops to this rather than running them on
# the event loop. See `_publish_if_current` for the whole rule.
from starlette.concurrency import run_in_threadpool

from config import (
    BENCH_SIZE,
    BUYOUT_PENALTY_RATE,
    DEFAULT_TEAM_PROBABILITY,
    MAX_SALARY,
    MIN_SALARY,
    MY_TEAM,
    NHL_TEAM_ALIASES,
    SALARY_CAP,
)
# Imported as a module so `data_loader.loaded_disambiguations` is read live. It
# is mutated in place, so a from-import would happen to work today — but it
# would also survive data_loader switching to a rebind, silently and wrongly.
import data_loader
from data_loader import build_initial_state, load_goalie_wins
from market import (
    MarketInfo,
    compute_all_market_prices,
    bid_winner,
    compute_live_ceiling,
    compute_market_ceiling,
    is_capped,
    live_opponents,
)
from optimizer import (
    CounterfactualResult,
    compute_bid_recommendation,
    compute_marginal_value,
    generate_counterfactual,
    recommend_nomination,
    solve_optimal_roster,
)
from price_model import (
    PriceBreakdown,
    PricePrediction,
    compute_pos_ranks,
    compute_reference_features,
    decompose_player,
    load_model_params,
    predict_all_prices,
)
from state import AuctionState, ChangeRecord, Player, PlayerOnRoster, TransactionRecord
from trade import (
    PlayerTrade,
    evaluate_buyout,
    evaluate_trade,
    execute_buyout,
    execute_trade,
)

def _default_state_dir() -> str:
    """Where this pool's saved draft lives.

    Derived from the pool rather than left to a second environment variable the
    operator has to remember, because forgetting it destroys a draft record:
    `lifespan` reads the saved state BEFORE it ever reads a CSV, so booting an
    alternate pool against `data/state/` would load the real draft's JSON,
    backfill it from the wrong CSV, and then save over it. pytest did exactly
    that until `conftest.py` started redirecting the directory.
    """
    if data_loader.PLAYERS_CSV == data_loader.DEFAULT_PLAYERS_CSV:
        return "data/state"
    stem = os.path.splitext(os.path.basename(data_loader.PLAYERS_CSV))[0]
    return f"data/state-{stem}"


STATE_DIR = os.environ.get("FCHL_STATE_DIR") or _default_state_dir()

# -- Global state --
auction_state: AuctionState | None = None
model_params: dict | None = None
model_prices: dict[str, PricePrediction] | None = None
market_prices: dict[str, float] | None = None
market_info: MarketInfo | None = None
milp_solution = None
last_trade_eval = None
# Set by lifespan when startup degraded (backup used, fresh start despite a
# saved file, a backfill skipped, an unreadable file that could not be set
# aside). In memory rather than on disk because the claim is about THIS boot: a
# clean restart should not re-raise a fixed alarm.
#
# A LIST, not an accumulated string. Three of these can be true at once — the
# current file will not parse, setting it aside fails, and the backup parses but
# a backfill raises — and concatenation ran them together as one unpunctuated
# strip. One sentence renders as a sentence; two or more render as a list, which
# is the only way the second one reads as a separate claim.
_startup_warnings: list[str] = []

# True when the current state file could not be read AND could not be renamed out
# of the way, so it is still sitting where _save_state's rotation would find it.
# Consulted there: rotating it into .backup is precisely the destruction the
# rename exists to prevent, and a failed rename must not be the thing that
# performs it.
_untrusted_current_file: bool = False

# Which team's roster is on screen. Held centrally so that no endpoint has to
# remember to carry it: the previous design passed a team code through every
# handler that rendered all_panels.html, and the ones that forgot — /team-done,
# /trade-execute, and the ERROR branch of all five roster-edit endpoints — threw
# you back to your own team mid-audit.
#
# A module global and NOT a field on AuctionState: on the state it would
# serialize into the save file and /undo would restore a *view*, which is not a
# draft action. Shared across browser tabs, which is fine for a tool one person
# runs on localhost.
_viewed_team: str = MY_TEAM


# The backfills take the state explicitly rather than reading the global: a
# candidate loaded off disk has to be validated BEFORE it is installed, or a
# half-backfilled corrupt state is already the live one by the time it raises.
def _backfill_nhl_teams(state: AuctionState, csv_path: str | None = None) -> None:
    """Fill in nhl_team for roster players and log records from old state files."""
    import csv

    # The pool the app is actually running on, not the default -- a state saved
    # from an alternate pool must be backfilled from that same file.
    csv_path = csv_path or data_loader.PLAYERS_CSV
    nhl_lookup: dict[str, str] = {}
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            nhl_lookup[row["PLAYER"].strip()] = row["NHL TEAM"].strip()
    for team in state.teams.values():
        for p in team.keeper_players + team.acquired_players + team.minor_players:
            if not p.nhl_team:
                p.nhl_team = nhl_lookup.get(p.name, "")
    # The log carries its own copy (see TransactionRecord.nhl_team), so records
    # written before that field existed need the same fill. `.get("", ...)`
    # rather than a skip: a player who has since left the CSV keeps the blank,
    # and the template draws nothing rather than a broken image.
    for t in state.transaction_log:
        if not t.nhl_team:
            t.nhl_team = nhl_lookup.get(t.player_name, "")


def _backfill_keeper_flags(
    state: AuctionState, csv_path: str | None = None
) -> None:
    """Restore `is_keeper` on MINOR-LEAGUE players from old state files.

    Provenance for the two active lists is re-derived from position by
    `_team_from_dict` — a player in `keeper_players` is a keeper by definition.
    The minors are the one list where that is impossible, which is exactly why
    the flag exists, so a state written before it did would recall a demoted
    keeper into `acquired_players` and colour him as a player BOT had bought.

    `players.csv` is the same pre-auction record `data_loader` derives keepers
    from, read with the same rule and the same constant
    (`data_loader._PLACEHOLDER_TEAMS`, deliberately not a second copy of
    `{"UFA", "RFA"}` here): a real FCHL TEAM means the player was rostered
    before the auction. Anyone drafted INTO the minors during this auction was
    biddable, so his row carries UFA/RFA and he is correctly left alone.

    Idempotent, and only ever sets the flag — never clears one, so re-running
    it cannot undo what a newer save already recorded.

    The undo chain is NOT repaired: `AuctionState._snapshots` is a list of whole
    JSON documents, so fixing them here would mean this module reaching
    into serialization keys that belong to state.py. Consequence, filed in
    BACKLOG.md: after booting a legacy file, undoing back past everything done
    this session restores minors without the flag. Narrow, cosmetic, and cheaper
    to record than to hard-code a second copy of the state's JSON shape.

    Matched on the DISAMBIGUATED name, not `row["PLAYER"]`, because that is the
    name the state file holds: `_disambiguated_names` renames every member of a
    colliding group, so the raw string would miss a minor-league keeper called
    `Matt Murray (DAL)` and leave him mis-coloured with no way to tell. (The
    older `_backfill_nhl_teams` above has the same gap; it costs a blank NHL
    team, not a wrong provenance.) The call resets `last_disambiguations` as a
    side effect, which is harmless here — the banner reads
    `loaded_disambiguations`, written only by `build_initial_state`, and this
    runs on the path where that was never called.
    """
    import csv

    # Same reason as _backfill_nhl_teams: follow the pool actually loaded.
    csv_path = csv_path or data_loader.PLAYERS_CSV
    with open(csv_path) as f:
        rows = list(csv.DictReader(f))
    pre_auction = {
        name
        for row, name in zip(rows, data_loader._disambiguated_names(rows))
        if (fchl := row.get("FCHL TEAM", "").strip())
        and fchl not in data_loader._PLACEHOLDER_TEAMS
    }
    for team in state.teams.values():
        for p in team.minor_players:
            if not p.is_keeper and p.name in pre_auction:
                p.is_keeper = True


def _backfill_team_metadata(
    state: AuctionState, teams_path: str = "data/fchl_teams.json"
) -> None:
    """Refresh team metadata (logos, colors) from fchl_teams.json for old state files."""
    with open(teams_path) as f:
        meta = json.load(f)
    for code, team in state.teams.items():
        if code in meta:
            team.logo = meta[code].get("logo", team.logo)


def _backfill_model_inputs(state: AuctionState) -> None:
    """Fill model inputs missing from pre-round-2 state files.

    pos_rank is recomputed over the remaining pool (approximate for
    mid-draft snapshots — drafted players no longer count — but only
    legacy snapshots ever hit this path). Goalie wins come from the
    projection stats file; unmatched goalies use the points fallback.
    `price_reference` is approximate in exactly the same way and for the same
    reason: it belongs to the draft-time pool, and a legacy snapshot no longer
    has one, so the surviving pool is the best available stand-in. It is only
    ever read by the price-driver breakdown, which is an explanation rather
    than a draft record, so an approximate baseline degrades the wording of a
    card and nothing else.
    """
    pool = state.available_players
    if any(p.pos_rank <= 0 for p in pool.values()):
        for name, rank in compute_pos_ranks(pool).items():
            pool[name].pos_rank = rank
    # Old snapshots stored fraction odds; the model expects percent. No real
    # pool has every team's Cup probability under 1%, so max < 1 = fractions.
    if pool and max(p.team_probability for p in pool.values()) < 1.0:
        for p in pool.values():
            p.team_probability *= 100.0
    goalie_wins = None
    for p in pool.values():
        if p.position == "G" and p.proj_wins is None:
            if goalie_wins is None:
                goalie_wins = load_goalie_wins()
            p.proj_wins = goalie_wins.get(p.name)
    # Last, because it reads the three fields repaired above.
    if not state.price_reference and pool:
        state.price_reference = compute_reference_features(pool, load_model_params())


def _load_saved_state(path: str) -> AuctionState | None:
    """A usable state from `path`, or None with the reason logged.

    Broad `except` on purpose, and it is handling rather than swallowing: this
    runs at startup of a tool that may be four hours into a live auction, and
    every alternative to degrading is worse than degrading. The narrow net it
    replaces (JSONDecodeError/KeyError/ValueError) let an AttributeError from a
    shape mismatch stop the app booting outright. A genuinely missing data file
    still fails loudly — build_initial_state() reads the same CSVs and raises.

    Only the PARSE decides whether the file is usable. The backfills each get
    their own net below, because folding them in here made a broken fixup
    indistinguishable from a broken file: one raise from `_backfill_model_inputs`
    on a legacy snapshot renamed a byte-perfect draft `.corrupt` and started
    fresh, returning 200 with nothing on screen to say 150 picks had gone.
    """
    if not os.path.exists(path):
        return None  # absent is not an error worth logging
    try:
        with open(path) as f:
            state = AuctionState.from_json(f.read())
    except Exception as e:
        logging.warning("Cannot use %s: %s: %s", path, type(e).__name__, e)
        return None
    # Labelled by what the operator loses, not by function name: the banner is
    # read mid-auction by someone deciding whether to trust a number on screen,
    # and "_backfill_model_inputs raised" does not answer that. The log keeps
    # the identifier and the exception for whoever debugs it afterwards.
    for label, backfill in (
        ("NHL teams on rostered players", _backfill_nhl_teams),
        ("keeper labels on minor-league players", _backfill_keeper_flags),
        ("team logos", _backfill_team_metadata),
        ("price model inputs, so PRICES MAY BE WRONG", _backfill_model_inputs),
    ):
        try:
            backfill(state)
        except Exception as e:
            # Never fatal: not one of the three is load-bearing for the draft
            # record — they fill logos, nhl_team and legacy model inputs. A
            # missing logo must not cost the auction. ERROR rather than WARNING
            # because _backfill_model_inputs failing is not cosmetic: prices are
            # then computed off legacy pos_rank / team_probability / proj_wins.
            logging.error("Skipped %s on %s: %s: %s — the draft is intact, but "
                          "anything it fills is stale",
                          backfill.__name__, path, type(e).__name__, e)
            _warn_at_startup(
                f"Could not refresh {label} — those values are stale. "
                f"The draft itself loaded normally."
            )
    return state


def _warn_at_startup(message: str) -> None:
    """Record something the operator must see about how this boot went.

    Startup is not a request, so there is no toast to fire; `_context` hands
    these to base.html, which renders them as a banner until dismissed or
    reloaded. Accumulates rather than overwrites — a boot that both skipped a
    backfill and fell back to the backup has two things worth saying, and the
    second is not more important than the first.

    Appends to a list rather than concatenating into one string. Three messages
    are reachable (see `_startup_warnings`), and run together in one strip the
    second and third read as continuations of the first rather than as separate
    things that went wrong.
    """
    _startup_warnings.append(message)


def _data_warning() -> str | None:
    """What the loader had to change about players.csv to make it usable.

    Kept OUT of `_startup_warnings` and rendered as its own banner, because the
    two have opposite lifecycles and merging them breaks both. The startup
    warning is about how this BOOT went and `POST /reset` clears it — a
    deliberate fresh start answers it. The renames are about the DATA and are
    still true after a reset, so folding them in would either make the startup
    alarm permanent wallpaper (which is what
    `test_the_happy_path_shows_no_banner` exists to prevent) or make the rename
    note vanish while the renames were still in force.

    Composed at render time rather than pushed through `_warn_at_startup` so it
    tracks the loaded CSV: `/reset` re-runs `build_initial_state()`, which
    repopulates `data_loader.loaded_disambiguations`.

    Booting onto a SAVED state leaves it empty and says nothing, which is
    right: that file's names were fixed when it was built, and describing
    today's CSV would name a pool the draft is not using.
    """
    if not data_loader.loaded_disambiguations:
        return None
    renames = "; ".join(
        f"{original} → {' / '.join(replacements)}"
        for original, replacements in data_loader.loaded_disambiguations.items()
    )
    return (
        f"players.csv repeats {len(data_loader.loaded_disambiguations)} player "
        f"name(s). They were renamed so each one can be drafted separately: {renames}."
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load data, compute prices, solve initial MILP on startup."""
    global auction_state, model_params, model_prices, _untrusted_current_file
    # Named on every boot because the two travel together and a mismatch is
    # silent: an alternate pool with the default state dir means loading one
    # season's draft and saving it over another's.
    # Through uvicorn's logger, not the root one: uvicorn configures only its
    # own loggers and leaves root at WARNING, so `logging.info` here printed
    # nothing at all in a real run while looking perfectly correct in the source.
    logging.getLogger("uvicorn.error").info(
        "player pool: %s | state dir: %s", data_loader.PLAYERS_CSV, STATE_DIR
    )
    os.makedirs(STATE_DIR, exist_ok=True)
    model_params = load_model_params()
    _load_nhl_odds()
    _startup_warnings.clear()  # this boot's story, not the previous one's
    _untrusted_current_file = False
    # Recovery ladder: current -> backup -> fresh. A fresh state is 150 picks
    # thrown away, so it is the last resort rather than the first fallback.
    saved_path = os.path.join(STATE_DIR, "auction_state.json")
    auction_state = _load_saved_state(saved_path)
    # Whether there was a draft on disk to lose, recorded BEFORE the rename that
    # may move it. Every branch below asks this rather than probing for
    # `.corrupt`: that file is an artifact of our own rename, so testing for it
    # answered "did the rename work" while reading as "was there a draft" — two
    # different questions that came apart in both directions. A rename that
    # failed left the fresh-start banner silent on a boot that had just lost 150
    # picks, and a `.corrupt` left over from an earlier incident (nothing ever
    # deletes one, `/reset` included) made that same banner claim a draft could
    # not be read when there had been none.
    had_saved_file = os.path.exists(saved_path)
    set_aside = False
    if auction_state is None and had_saved_file:
        # Move the unusable file aside rather than leaving it for _save_state
        # to rotate over the good backup — one restart plus one click would
        # otherwise destroy both copies. Renaming rather than only an in-memory
        # flag because the outcome is then visible on disk: you can see what
        # happened without reading the log.
        try:
            os.replace(saved_path, saved_path + ".corrupt")
            set_aside = True
        except OSError as e:
            # The rename is the whole defence, so losing it is not a footnote.
            # `_save_state` now refuses the rotation while this is set, which
            # keeps the backup — but saving into this directory is evidently
            # broken, and that is worth knowing before another 20 picks are
            # entered against it.
            _untrusted_current_file = True
            logging.error("Could not set aside %s: %s: %s — the backup is being "
                          "protected in software instead", saved_path,
                          type(e).__name__, e)
            _warn_at_startup(
                "Could not read the saved draft, and could not move the "
                f"unreadable file aside either ({type(e).__name__}). The backup "
                "is being protected in memory, but SAVING MAY BE BROKEN — check "
                "that a pick you enter survives a reload before drafting on."
            )
    if auction_state is None:
        auction_state = _load_saved_state(saved_path + ".backup")
        if auction_state is not None:
            logging.error("Recovered the draft from the backup file")
            _warn_at_startup(
                "Could not read the saved draft, so this is the backup copy — "
                "one save behind. Check the last pick is here and re-enter it "
                "if it is not."
            )
    if auction_state is None:
        if had_saved_file:
            # The loud case: a state file existed and nothing could be salvaged.
            # Without this the app comes up looking like a normal fresh start.
            logging.error("No usable saved draft; starting fresh")
            # Only name the salvage when the rename actually produced one.
            # Pointing at a `.corrupt` that does not exist sends the operator
            # looking for their draft in a file that was never written.
            where = (f" The unreadable file is at {saved_path}.corrupt — "
                     "nothing has overwritten it." if set_aside else
                     f" The unreadable file is still at {saved_path}.")
            _warn_at_startup(
                "Could not read the saved draft or its backup, so this is a "
                f"NEW auction.{where}"
            )
        auction_state = build_initial_state()
    model_prices = predict_all_prices(auction_state.available_players, model_params)
    _recompute()
    yield


app = FastAPI(title="FCHL Auction Manager", lifespan=lifespan)
# Absolute, so _asset_version stats the right file whatever the process
# cwd is — the mount below resolves relative to it and pytest does not.
STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/fchl_logos", StaticFiles(directory="fchl_logos"), name="logos")
app.mount("/nhl_logos", StaticFiles(directory="nhl_logos"), name="nhl_logos")
templates = Jinja2Templates(directory="templates")


def _dom_id(name: str) -> str:
    """A player name as a DOM id htmx can build a CSS selector from.

    htmx resolves an out-of-band target by SELECTOR, not by getElementById —
    `htmx-1.9.10.min.js`, function `Ee`: `var t = "#" + ee(i,"id"); …
    re().querySelectorAll(t)` — and it calls that from a plain forEach with no
    try/catch. So a character that is illegal in a CSS identifier does not just
    make one swap miss: `querySelectorAll` throws and every remaining swap in
    the response is abandoned. Measured in Chrome 2026-08-07 with
    `Matt Murray (DAL)` on BOT's roster: **12 dot placeholders, 0 resolved**,
    `htmx:swapError` in the console and a Scan button that looks like it simply
    never finished.

    `players.csv` really does carry such names — backticks (`Drew O`Connor`)
    and parentheses (`Tony DeAngelo (NCM)`) — and `_disambiguated_names` adds
    ` (TEAM)`, ` (TEAM POS)` and ` (#n)` suffixes on top, so the pool holds two
    of them today. The old inline expression stripped `'` (U+0027) and `.`,
    which reads as covering apostrophes; the data uses U+0060, so it never
    matched one.

    The digest is what makes this injective. A slug alone is lossy, and two
    players colliding on a derived key is precisely the failure the 2026-08-07
    disambiguation work removed — it must not return one layer down as two
    dots fighting over a single id.
    """
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()
    return f"{slug}-{hashlib.sha1(name.encode()).hexdigest()[:8]}"


# A filter rather than a macro: the placeholder and the out-of-band swap that
# has to find it live in different templates, and they are only guaranteed to
# agree if there is one definition rather than two copies of an expression.
templates.env.filters["dom_id"] = _dom_id


def _nhl_canonical(code: str) -> str:
    """The one spelling of an NHL club this app shows anywhere.

    The pool CSVs disagree with each other: data/players.csv spells Utah `UTH`
    on 78 rows and data/players-25.csv spells it `UTA` on 30. `UTH` is not a
    tricode any NHL club has -- it is the FCHL's own spelling, and
    NHL_TEAM_ALIASES declares `UTA` canonical, matching team_odds.json and the
    league itself.

    Until 2026-09-11 only the IMAGE path resolved the alias while the badge's
    `alt`/`title` and the player search's meta line printed the raw column
    value, so which file the operator loaded decided whether the same club read
    `UTH` or `UTA` on screen. A label is a fact about the club, not about the
    CSV, so it does not get to vary with the pool.

    An unknown code passes through unchanged -- see `_nhl_logo_src`, which is
    built on this and needs the same property.
    """
    return NHL_TEAM_ALIASES.get(code, code)


templates.env.filters["nhl_canonical"] = _nhl_canonical


def _nhl_logo_src(code: str) -> str:
    """The URL for one NHL club badge, resolving the alias BEFORE naming a file.

    The assets are named by the CANONICAL tricode -- the value side of
    NHL_TEAM_ALIASES, which is also what team_odds.json and the NHL itself use.
    The pool CSVs are not consistent about it: data/players.csv spells Utah
    `UTH` on 78 rows while data/players-25.csv spells it `UTA` on 30, and before
    this existed every template pasted the raw column value into the path, so
    the 2025 pool rendered 30 broken images and 404'd on every page load.

    One function for the same reason `_dom_id` is one function: the six render
    sites and the tests that assert on them can only agree if there is a single
    definition. Never build this path by hand.

    An unknown code passes through unchanged so a missing logo stays VISIBLY
    missing -- that is what the data invariant in tests/test_data_loader.py and
    the pre-auction runbook's logo sweep are there to catch, and a silent
    fallback would hide both.
    """
    return f"/nhl_logos/{_nhl_canonical(code)}.svg"


templates.env.filters["nhl_logo_src"] = _nhl_logo_src


@lru_cache(maxsize=None)
def _asset_version(path: str) -> str:
    """A cache-busting token for one file under `static/`, from its mtime.

    StaticFiles sends `last-modified` and `etag` and NO `Cache-Control`, and per
    RFC 9111 §4.2.2 a response with no explicit freshness may be served from
    cache under *heuristic* freshness — the browser never revalidates, so it
    never learns the file changed. A conditional GET does return 304 correctly;
    the problem is that the browser has no reason to make one.

    That is not theoretical here. Two fixes shipped on 2026-09-09 (the
    search-box `focusout` dismissal, and `#pool-rows` so the position filters
    survive an open price chart) were both re-reported as still broken the next
    day, by an operator running the previous `shortcuts.js`. Both were verified
    working in a fresh browser profile within minutes.

    `static/vendor/*` is deliberately NOT routed through this: those filenames
    already carry their version (`htmx-1.9.10.min.js`), so they are correctly
    cacheable forever and a query string would only defeat that.

    Memoised, so the whole app pays one stat() per asset. That means a file
    edited while the server is running keeps its old token until restart — which
    is what `--reload` is for, and the alternative is a stat() on every page
    render for a number that cannot change in a production run.
    """
    try:
        return str(int((STATIC_DIR / path).stat().st_mtime))
    except OSError:
        # A missing asset is the template's bug, not this function's, and a
        # cache-busting token is no reason to fail a page load mid-draft.
        return "0"


# A global rather than a `_context` key: base.html must not depend on `_context`,
# which `_render` deliberately short-circuits on the hot path (/find-player).
templates.env.globals["asset_version"] = _asset_version


def _roster_slots(rows: list[dict]) -> list[dict]:
    """Label each team-panel roster row with its lineup slot, in display order.

    `F1`..`F12`, `D1`..`D6`, `G1`..`G2` for players who will start, `B1`..`B4`
    for benched ones, and an EMPTY label for a MILP suggested-buy row. The last
    number rendered in a position group is therefore literally how many of that
    position the team holds, which is the question this column exists to answer
    and which nothing on the panel answered before — `roster_needs` gives the
    aggregate (`Needs: 3F · 1D`), never the running count beside the player.

    Order is the caller's, not this function's: it must be applied AFTER
    `team_panel.html`'s `pos_rank` + points sort, because the number means "the
    Nth row of this group as drawn". Sorting here would let the label and the
    table disagree.

    Three things this deliberately does not do.

    A target row is skipped rather than numbered because it is not owned — the
    MILP suggests it, nobody bought it — so letting it take `F3` would report a
    roster the team does not have, which is the one thing the column must not
    do. It consumes no counter at all, so removing a suggestion never renumbers
    the players around it.

    A position is NOT clamped at its starting-lineup size. Fourteen forwards
    with none benched really does render `F13`, `F14`: that is the true holding
    and it is exactly the signal that two of them belong on the bench. Clamping
    would hide the only state the operator needs to act on, and auto-benching
    the overflow would overwrite a decision that is theirs — `is_bench` is set
    through `/toggle-bench` and `TeamState.set_bench` already caps the bench at
    `BENCH_SIZE`. On a correctly-benched full roster the sequence lands exactly
    on F1-12, D1-6, G1-2, B1-4 without any clamp.

    A bench label carries its position too — `BF1`, `BD2` — while the NUMBER
    stays one running sequence across every position rather than restarting per
    position. The number is therefore "how full is the bench", against the hard
    `BENCH_SIZE` of 4, which is the constraint that actually binds; bench
    COMPOSITION against `BACKUP_TARGETS` (2F/1D/1G) is a soft objective
    preference the MILP is free to ignore, so it would be the wrong thing to
    count. The letter went in on 2026-09-20, when dropping the `Pos` column left
    `B1` as the only row on the panel whose position nothing stated.

    Bench membership is read off the `is_bench` FLAG, not derived from points.
    That is the flag the row dimming, the Bench button and `send_to_minors`'
    precondition all read, so deriving it differently here would put two answers
    on one row. It does mean this column can disagree with the "Lineup PTS"
    tile: `lineup_points` scores the best 12F/6D/2G from every roster player and
    ignores `is_bench` entirely (see `TeamState.set_bench`), so benching your
    top forward shows him as `BF1` while the tile still counts him as a starter.
    That disagreement is older than this column and is merely made visible by
    it; it is not this function's to resolve.
    """
    counts: dict[str, int] = {}
    bench = 0
    for row in rows:
        pos = row.get("position", "")
        if row.get("is_target"):
            row["slot"] = ""
        elif row.get("is_bench"):
            bench += 1
            row["slot"] = f"B{pos}{bench}"
        else:
            counts[pos] = counts.get(pos, 0) + 1
            row["slot"] = f"{pos}{counts[pos]}"
    return rows


# A filter rather than six lines of Jinja `namespace` counters, for the reason
# `_dom_id` records: the rule is then one pure function a unit test can call
# directly, instead of arithmetic that only a rendered page can check.
templates.env.filters["roster_slots"] = _roster_slots


buyout_indicators: dict[str, str] = {}  # player_name -> "buyout" or "keep"

# Stanley Cup odds per NHL club, as PERCENT, for the navbar's odds view. Loaded
# in `lifespan` INDEPENDENTLY of the state, because `build_initial_state` — the
# only other caller — runs on a fresh boot only, so a restored draft would
# otherwise have no odds at all. Carries the alias keys `load_team_odds` adds
# (`UTH` alongside `UTA`), so anything iterating it for display has to drop them.
nhl_odds: dict[str, float] = {}

# Exact projected points per LIVE OPPONENT, from a real MILP solve, filled only
# by GET /solve-standings and cleared by _recompute(). Empty means "nobody has
# asked", which is the normal state and the reason the heuristic in _context
# stays: 10 solves cost ~1.3s and no draft action can afford that.
#
# Keyed by team code, and absence is meaningful — a team missing from here falls
# back to the estimate rather than to zero, which is what makes an Infeasible
# opponent solve harmless.
exact_projections: dict[str, int] = {}

# Bumped by `_recompute()` on every state change. Read by the two threaded scans
# to decide whether the state they solved against is still the state on screen;
# `_publish_if_current` is the whole rule.
_state_version = 0

# How many MILPs the two manual scans solve at once inside their worker thread.
#
# Not in `config.py`: that file is the league — cap, roster shape, CBA rules —
# and this is machine tuning. Each concurrent solve is a **CBC subprocess**, so
# this is a core budget, not a thread count, and the cap matters much more than
# the formula. The dev box has 20 cores and the draft runs on a laptop;
# oversubscribing a 4-core machine with 15 CBC processes would be slower than
# solving them one at a time, and most of the win is already there at 4.
# `os.cpu_count()` rather than `os.process_cpu_count()` because CLAUDE.md's
# floor is Python 3.12.
SCAN_WORKERS = min(8, max(1, os.cpu_count() or 2))


# Marginal values for the current state epoch, keyed by player name. Cleared
# wholesale by _recompute() rather than versioned: a version counter has the
# same failure mode (forget to bump, serve a stale number as live bid advice)
# plus unbounded growth. An empty dict cannot serve a stale entry.
_marginal_cache: dict[str, float] = {}


def _marginal_value(player: Player) -> float:
    """Marginal value of `player` to BOT, cached for this state epoch.

    compute_marginal_value costs ~10 MILP solves (~780ms on a 704-player pool)
    and is pure in (roster, budget, pool, market prices). None of those move
    when the price or the bidder list changes — which is all /bid-check varies
    between calls — so a live auction otherwise spends a second per $0.1M
    increment re-deriving an identical number.
    """
    if player.name not in _marginal_cache:
        _marginal_cache[player.name] = compute_marginal_value(
            player,
            auction_state.teams[MY_TEAM],
            auction_state.available_players,
            market_prices,
        )
    return _marginal_cache[player.name]


# Counterfactuals for the current state epoch, on the same terms as
# _marginal_cache. Holds objects rather than floats, but an epoch ends at every
# assign, so in practice this is the one or two players bid on since the last
# sale — not worth a bound.
#
# Keyed on (name, price) rather than name alone, because the Recompute button
# asks for the same player at a DIFFERENT price and a name-only key would hand
# back the market-price answer while the card quoted the live bid. That widens
# the bound from "players bid on since the last sale" to "players × deliberate
# clicks", which is still tiny: every extra entry costs someone a click and two
# MILP solves, so the operator cannot generate them faster than ~5/second even
# trying.
_counterfactual_cache: dict[tuple[str, float], CounterfactualResult] = {}


def _cf_price(player_name: str) -> float:
    """The price a counterfactual is conditioned on: the expected clearing price.

    Quantized to the $0.1M auction increment, for the same reason
    `_legal_salary` quantizes a typed one: market prices come off a log-normal
    and are essentially never legal prices — all 704 of them at reset — so
    forcing a player in at $9.5476934838794 plans the roster around a price the
    auction cannot produce, while the panel quotes "$9.5M". Small in dollars,
    but it is a number on screen that no bid can ever match.

    One definition, because the analysis and the sentence describing it are
    computed in different places and a drift between them would show a verdict
    ("Skip him at $9.5M") derived from a different number than the one quoted.
    """
    return round(market_prices.get(player_name, MIN_SALARY), 1)


def _counterfactual(player: Player, price: float) -> CounterfactualResult:
    """Roster with vs without `player` at `price`, cached this epoch.

    Two MILP solves (~200ms), pure in (roster, budget, pool, market prices,
    price) — the first four are the inputs _recompute() replaces, and the fifth
    is in the key.

    The price the callers pass by DEFAULT is still the market price and not the
    live bid, which is what makes the auto-loaded card epoch-stable: re-solving
    per $0.1M increment would put a 200ms response back inside the window where
    it can land between mousedown and mouseup on Assign. Sharpening it to the
    live bid is a button the operator presses, never a trigger — see
    `_counterfactual_context`.
    """
    key = (player.name, price)
    if key not in _counterfactual_cache:
        _counterfactual_cache[key] = generate_counterfactual(
            player,
            price,
            auction_state.teams[MY_TEAM],
            auction_state.available_players,
            market_prices,
        )
    return _counterfactual_cache[key]


def _counterfactual_context(player_name: str, price: float | None = None) -> dict | None:
    """Template variables needed by counterfactual.html.

    Returns None if the player isn't in the pool. Used by both /explain and the
    bid panel's lazy mount, mirroring _chart_context.

    `price` is the Recompute button asking for this player at the price on the
    table instead of the expected clearing price. Through `_legal_salary` for
    the same reason `_cf_price` rounds: a bid box auto-submits whatever was
    typed, and a verdict reading "Skip him at $46.0M" would be conditioned on a
    price the CBA has no room for. That quantization is also what keeps the
    cache key dense — a raw float would miss on every keystroke.
    """
    p = auction_state.available_players.get(player_name)
    if p is None:
        return None
    at_bid = price is not None
    cf_price = _legal_salary(price) if at_bid else _cf_price(player_name)
    return {
        "counterfactual": _counterfactual(p, cf_price),
        "cf_player": p,
        # The whole verdict is conditioned on this price — without it the panel
        # shows a points delta the reader can't judge. Already quantized, so
        # what is quoted is exactly what was solved.
        "cf_price": cf_price,
        # Which of the two prices that is. The card names the figure in its
        # verdict either way; this says whether it is a forecast of the
        # clearing price or the number currently on the table, which is the
        # whole difference between the auto-load and a Recompute.
        "cf_at_bid": at_bid,
    }


def _recompute():
    """After any state change: recompute market prices + re-solve MILP.

    Also invalidates any pending trade evaluation — a trade evaluated
    against the old world must not be executable against the new one. The
    cached marginal values and counterfactuals go for exactly the same reason:
    they are derived from the roster, budget and market prices this function is
    replacing. A stale counterfactual is the worse of the two, because it names
    specific alternative players — it would recommend drafting someone who has
    already been sold.
    """
    global market_prices, market_info, milp_solution, last_trade_eval
    global _state_version
    # Every state change passes through here, so one bump covers all twelve
    # mutating endpoints. The two threaded scans compare it before and after
    # solving — see `_publish_if_current`.
    _state_version += 1
    last_trade_eval = None
    _marginal_cache.clear()
    _counterfactual_cache.clear()
    # Same sentence as the two caches above, and it is the whole reason the
    # exact standings are safe to cache at all: they are per-team MILP optima
    # over this roster, this budget and these market prices. A stale one is the
    # worst of the three to read, because the Proj column carries a rank badge —
    # a number that looks authoritative and describes a state that is gone.
    exact_projections.clear()
    market_info = compute_market_ceiling(auction_state.teams)
    all_market = compute_all_market_prices(
        auction_state.available_players, model_prices, auction_state.teams,
    )
    market_prices = {name: price for name, (price, _) in all_market.items()}
    team = auction_state.teams[MY_TEAM]
    milp_solution = solve_optimal_roster(team, auction_state.available_players, market_prices)


def _nhl_team_of(name: str) -> str:
    """A player's NHL club, from wherever he currently is, or "" if nowhere.

    Only for /trade-execute, whose `PlayerTrade` DTO carries no NHL club — the
    receive side is assembled from client-submitted JSON, and threading it
    through there would mean trusting the browser for a field the log keeps
    forever. A lookup is sound HERE and nowhere else: a trade always leaves
    every player somewhere (a roster, or the pool when there is no source
    team), so this cannot come up empty the way it would after a buyout.
    """
    for team in auction_state.teams.values():
        p = team.find_player(name)
        if p:
            return p.nhl_team
    pooled = auction_state.available_players.get(name)
    return pooled.nhl_team if pooled else ""


def _log_transaction(
    player_name: str,
    position: str,
    team_code: str,
    salary: float,
    txn_type: str,
    *,
    model_price: float = 0,
    market_price: float = 0,
    timestamp: str | None = None,
    nhl_team: str = "",
) -> None:
    """Append a TransactionRecord to the auction state's transaction log."""
    auction_state.transaction_log.append(TransactionRecord(
        player_name=player_name,
        position=position,
        team_code=team_code,
        salary=salary,
        model_price=model_price,
        market_price=market_price,
        timestamp=timestamp or datetime.now().isoformat(),
        transaction_type=txn_type,
        nhl_team=nhl_team,
    ))


def _log_change(kind: str, team_code: str, description: str) -> None:
    """Append a ChangeRecord for a non-transaction state edit."""
    auction_state.change_log.append(ChangeRecord(
        timestamp=datetime.now().isoformat(),
        kind=kind,
        team_code=team_code,
        description=description,
    ))


def _solve_exact_projections(
    state: AuctionState, prices: dict[str, float]
) -> dict[str, int]:
    """Solve every LIVE OPPONENT's optimal roster. Called only from /solve-standings.

    **Pure, and that is the point**: it reads the state and prices it is handed
    and returns a dict, touching no global. That is what makes it safe to run in
    a worker thread while the event loop stays free to answer a bid check — see
    `_publish_if_current`. It used to read `auction_state`/`market_prices` and
    write `exact_projections` directly, which could only ever run on the loop.

    The heuristic in `_context` exists because 11 MILPs per action is
    unaffordable; this is the same answer computed properly, on request, the way
    the Buyout Analyzer's Scan button already trades ~15 solves for a click.

    Two teams are skipped and neither is an optimization:

    * a **done** team's roster is FINAL, so its projection is what it has — the
      same rule `_context` applies first, and solving it would invent purchases
      it has sworn off (the 2026-08-13 bug, worth +673 to +1101 points a team);
    * **BOT's** figure is already `milp_solution.total_points`, the identical
      solve over the identical inputs.

    So the cost falls as the draft progresses — measured 2026-08-17, 10 solves
    (~1265ms) on a fresh league against 2 (~250ms) in the endgame scenario,
    because by then eight teams are done.

    A non-Optimal solve leaves the team OUT of the dict rather than storing a
    zero, so it keeps showing the estimate. Reporting a team as 0 points because
    a solver failed is worse than reporting it approximately.

    **Solved `SCAN_WORKERS` at a time**, which is safe for the reason
    `TestTwoSolvesAtOnceAgreeWithTwoSolvesInARow` pins: PuLP names its scratch
    files per solve and CBC is a subprocess, so the GIL is released across it.
    `pool.map` yields in INPUT order, so the dict is built in the same order the
    serial loop built it — there is no completion-order nondeterminism to reason
    about, and `TestAScanInParallelAgreesWithItselfInSeries` asserts that order
    as well as the values.
    """
    codes = [
        code for code, t in state.teams.items()
        if not t.is_done and code != MY_TEAM
    ]
    if not codes:
        # `max_workers=0` raises, and an all-done league is a real state.
        return {}
    with ThreadPoolExecutor(max_workers=min(SCAN_WORKERS, len(codes))) as pool:
        results = pool.map(partial(_solve_one_opponent, state, prices), codes)
        return {code: points for code, points in results if points is not None}


def _solve_one_opponent(
    state: AuctionState, prices: dict[str, float], code: str
) -> tuple[str, int | None]:
    """One opponent's optimum, or `None` if it could not be had.

    Its own `except`, and that placement is required rather than tidy: this runs
    in a pool worker, so an exception escaping here surfaces when the RESULT is
    consumed — taking down the whole scan instead of one team, and doing it
    slowly, because the pool's `shutdown(wait=True)` first waits for every other
    solve in flight.

    **The whole body is inside it, which the first version got wrong**: the try
    wrapped only the solve, leaving `sol.status` and the `int()` conversion
    outside a net whose docstring claimed to cover them. Nothing reachable makes
    `total_points` non-numeric today — it is a `float` on the dataclass — so this
    is the guarantee being made true rather than a live bug, and the asymmetry
    with `_solve_one_buyout` (whose body always was inside its try) is what gave
    it away.

    Broad on purpose — a solver blowing up on one opponent must not cost the
    other nine — but never silent. The basis marker reveals that a cell is still
    an estimate (`exact 9/10`) and cannot say which team or why, so this log is
    the only place that failure is diagnosable.
    """
    try:
        sol = solve_optimal_roster(state.teams[code], state.available_players, prices)
        return code, int(sol.total_points) if sol.status == "Optimal" else None
    except Exception as e:
        logging.warning(
            "No exact projection for %s: %s: %s — that team keeps its "
            "estimate and the Proj column says so", code, type(e).__name__, e,
        )
        return code, None


def _solve_buyout_indicators(
    state: AuctionState, prices: dict[str, float], current_points: float
) -> dict[str, str]:
    """Would buying each eligible player out improve BOT's optimal lineup?

    Pure for the same reason as `_solve_exact_projections`, and it needs one more
    thing passed in: `current_points`, the figure every hypothetical is compared
    against, read off `milp_solution` on the loop before this is called. The
    per-player clones come off `state` — the caller's private snapshot — so
    nothing here can see a roster change half-applied by a pick that lands while
    it is solving.

    **Solved `SCAN_WORKERS` at a time**, in input order — see
    `_solve_exact_projections` for why that is safe and why the order matters.
    This is the more expensive of the two scans and the endgame is its WORST
    case, not its best: measured 2026-08-19, 15 eligible players on a fresh
    league (1630ms) against 23 in the endgame scenario (2174ms), because a
    late-draft BOT owns more group 2/3 contracts. Standings runs the other way.
    """
    # Only what's eligible: a dot beside a player who can't be bought out is
    # worse than no dot, because it reads as a verdict on a decision that isn't
    # available.
    #
    # `all_players`, matching buyout_panel.html and buyout_dots.html. This was
    # roster-only until 2026-08-07, which silently under-reported: eligibility
    # is a property of the contract group alone, so BOT's 4 group-3 players in
    # the minors ($2.0M, fully on cap) are legal buyouts the Analyzer already
    # offered while the scan said nothing about them. Costs 4 more solves.
    #
    # Materialised here, on the caller's thread, and the mechanism is narrower
    # than "before the fan-out": `roster_players` LAZILY ASSIGNS `_roster_cache`
    # (state.py), a plain dataclass field, so computing this list inside a worker
    # would have one thread writing BOT's `__dict__` while another `deepcopy`s
    # it. That is benign today only by luck — the field already exists, so the
    # dict does not resize and `deepcopy` cannot raise "changed size during
    # iteration"; the clone would just carry a torn cache. Moving this line into
    # the pool would look free and would not be.
    candidates = [q for q in state.teams[MY_TEAM].all_players if q.can_be_bought_out]
    if not candidates:
        return {}
    with ThreadPoolExecutor(max_workers=min(SCAN_WORKERS, len(candidates))) as pool:
        verdicts = pool.map(
            partial(_solve_one_buyout, state, prices, current_points), candidates
        )
        return dict(verdicts)


def _solve_one_buyout(
    state: AuctionState,
    prices: dict[str, float],
    current_points: float,
    player: PlayerOnRoster,
) -> tuple[str, str]:
    """Would buying this one player out beat `current_points`?

    Own `except` for the same reason as `_solve_one_opponent`: raising in a pool
    worker would cost the whole scan rather than one dot. `keep` on failure is
    the conservative answer — it says "no help here", not "buy him out".

    **And it logs**, which it did not until 2026-08-20. A silent fallback here is
    the 2026-08-07 failure with the evidence removed: every dot green reads as
    "no buyout helps", and if the cause were a solver blowing up on all fifteen
    there was nothing anywhere to say so. The dots cannot express "unknown" — the
    template has two colours — so the log is the only place this is visible.

    The `deepcopy` is per player and always was: the clone is mutated (the player
    removed, the penalty added), so it cannot be shared. It reads `state` while
    other workers read the same `state`, which is fine — nothing here writes to
    it, and it is the caller's private snapshot in the first place.
    """
    try:
        clone = deepcopy(state)
        bt = clone.teams[MY_TEAM]
        bt.remove_player(player.name)
        bt.penalties += player.salary * BUYOUT_PENALTY_RATE
        sol = solve_optimal_roster(bt, state.available_players, prices)
        return player.name, "buyout" if sol.total_points > current_points else "keep"
    except Exception as e:
        logging.warning(
            "No buyout verdict for %s: %s: %s — his dot stays green, which reads "
            "as 'keep him'", player.name, type(e).__name__, e,
        )
        return player.name, "keep"


def _publish_if_current[V](
    version: int, target: dict[str, V], solved: dict[str, V], what: str
) -> bool:
    """Copy a thread's result into its module dict — unless the state moved on.

    The two manual scans (`/solve-standings`, `/buyout-indicators`) are seconds of
    synchronous CBC. Run on the event loop, as they were until 2026-08-19, they
    block EVERY other request for their whole duration — measured a warm
    `/bid-check` at **10ms alone against 1682ms behind a roster scan**, and even
    `/state`, which solves nothing at all, at 1564ms. The operator types a bid
    price while a scan runs, so that is the one stall a live auction cannot
    afford. `hx-disabled-elt` greys the button and says nothing about the panel.

    So the solving moved to a worker thread, and this is the price of that: the
    loop can now run an `/assign` while a scan is mid-flight, and a result
    computed against the roster from before that pick must not be published.
    Both dicts are read by templates that present them as authoritative — the
    Proj column carries a rank badge, the dots carry a verdict — so a stale one
    is worse than none. Discarding leaves the Proj column on its estimate with
    `#proj-basis` saying so; the dots need one extra step at the call site,
    because their template defaults a missing verdict to "keep" and would paint
    a discarded scan all-green.

    **A version counter rather than "is the dict still empty".** `_recompute()`
    already clears `exact_projections`, and empty is ALSO the normal state before
    anybody scans, so emptiness cannot tell "nobody asked" from "a pick landed
    while I was solving". The counter can.

    Cleared and updated in place rather than rebound: `_recompute()` clears the
    same object, and one dict with two owners is easier to reason about than two
    bindings.
    """
    if _state_version != version:
        logging.info(
            "Discarded %s solved against v%d — the state moved to v%d while the "
            "solver ran, so the panel keeps what it had", what, version, _state_version,
        )
        return False
    target.clear()
    target.update(solved)
    return True


def _save_state():
    """Save auction state to disk atomically with backup rotation."""
    global _untrusted_current_file
    path = os.path.join(STATE_DIR, "auction_state.json")
    backup_path = path + ".backup"
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        f.write(auction_state.to_json())
    if os.path.exists(path) and not _untrusted_current_file:
        os.replace(path, backup_path)
    os.replace(tmp_path, path)
    # Cleared only after a save has landed, and only here: from this point `path`
    # is a file we just wrote, so the next rotation is rotating a good state and
    # the backup is safe again. Skipping the rotation is what saves the backup
    # when startup could not rename an unreadable file out of the way — without
    # it, one save put the corrupt file on top of the last good copy, which is
    # exactly the disaster the rename exists to prevent.
    _untrusted_current_file = False


def _legal_salary(value: float) -> float:
    """Clamp to the legal range and quantize to the $0.1M auction increment.

    Every salary field auto-submits whatever was typed, and neither can stop a
    bad value on its own: the bid box's step= only drives the spinner, and it
    sits in a different form from Assign so its validity is never checked on
    submit. A typo'd 46 recorded as $46M corrupts the draft record loudly; a
    typo'd 2.55 corrupts it quietly — the CBA has no such price, and it strands
    $0.05M of the team's cap below the increment remaining_budget floors to.

    Round rather than floor: _floor_to_increment guards budget headroom, where
    the safe direction is down. A typo'd price has no safe direction, so take
    the nearest and let the caller's toast make the change visible.
    """
    return round(max(MIN_SALARY, min(value, MAX_SALARY)), 1)


def _cap_overages(*team_codes: str | None) -> list[str]:
    """Teams currently over the cap, worst first, as '<CODE> $X.XM over cap'.

    Trades are allowed to leave a team over — the league resolves those with
    buyouts (owner decision 2026-08-06) — so this reports rather than blocks.
    Without it an accidental over-cap trade returned the same green toast as a
    legal one, which is the whole gap being closed.

    An unknown, empty or None code is skipped by design: `TradeEvaluation.
    source_team_code` is `str | None` and /trade-execute passes it straight
    through, so the signature admits None rather than making every call site
    guard.
    """
    over: list[tuple[float, str]] = []
    for code in team_codes:
        team = auction_state.teams.get(code)
        if team is None:
            continue
        # Round before testing: total_salary sums many $0.1M values, so float
        # noise would otherwise report "$0.0M over cap" on an exactly-legal team.
        overage = round(team.total_salary - SALARY_CAP, 1)
        if overage > 0:
            over.append((overage, f"{code} ${overage:.1f}M over cap"))
    return [msg for _, msg in sorted(over, reverse=True)]


# The event POST /assign fires to make the standings re-solve themselves. A
# constant because it is a contract across two files with no import between
# them — tests/test_endpoints.py asserts this exact string appears in
# static/shortcuts.js, since a typo on either side is completely silent.
SOLVE_STANDINGS_EVENT = "solveStandings"


def _toast(
    response: HTMLResponse,
    message: str,
    toast_type: str = "info",
    *,
    after_settle: dict | None = None,
) -> HTMLResponse:
    """Attach a toast notification to an HTMX response via HX-Trigger header.

    `after_settle` rides a SECOND header, `HX-Trigger-After-Settle`. The
    ordering difference is real and verifiable in the vendored htmx 1.9.10:
    plain `HX-Trigger` is dispatched in the onload handler immediately after
    `htmx:beforeOnLoad` and BEFORE the swap, while after-settle fires inside
    the settle callback once the new elements are in place.

    Be precise about what that buys, because the obvious claim is wrong: it is
    NOT that plain `HX-Trigger` would fire `/solve-standings` at the doomed
    `proj-<CODE>` spans. htmx resolves an out-of-band target when the RESPONSE
    arrives, and this scan takes ~250-400ms against a swap that is synchronous
    in the same tick — so the targets are always the fresh ones either way, and
    the mutation swapping the two headers survives both browser tests. What
    after-settle buys is a guarantee instead of a timing margin, and a panel
    that has finished painting before up to 8 CBC subprocesses start competing
    with the renderer for cores.
    """
    response.headers["HX-Trigger"] = json.dumps(
        {"showToast": {"message": message, "type": toast_type}}
    )
    if after_settle:
        response.headers["HX-Trigger-After-Settle"] = json.dumps(after_settle)
    return response


def _render(request: Request, template: str, extra: dict | None = None) -> HTMLResponse:
    """Render a template with the standard context plus any extras."""
    if extra and "request" in extra:
        # Caller already built a full context — use it directly
        return templates.TemplateResponse(request, template, extra)
    ctx = _context(request)
    if extra:
        ctx.update(extra)
    return templates.TemplateResponse(request, template, ctx)


def _view_team(code: str) -> None:
    """Point the team panel at `code`. An unknown code changes nothing.

    Owner decision 2026-08-08, amending 2026-08-07: the view follows whichever
    roster the action changed. /assign passes the BUYER — on your own pick that
    is still BOT, which is the only case the original reasoning ("reading an
    opponent's Cap Used as your own right after a pick lands") was ever about.
    On an opponent's pick nothing of yours moved and the panel that just went
    stale is theirs. /buyout passes the team whose player was bought out — BOT
    for your own, the rival for theirs, since 2026-09-12 made execute_buyout
    take a team at all; /reset and /load-scenario because they replace the
    world; /undo passes the team named by the record it reverted.

    It VALIDATES rather than leaning on _context's `teams.get(_viewed_team,
    team)` fallback, so `_viewed_team` is always a live team code. That fallback
    is silent, and it renders BOT's roster *and* BOT's Scan gate from the same
    object — so a dead code would look completely normal on screen while every
    later /team-view no-op'd on top of the garbage. It also gives both writers
    of the global one contract, the same "an unknown code changes nothing" rule
    GET /team-view/FAKE follows. The transaction log is a real source of
    non-team-code strings: /trade-between logs team_code as f"{source}→{dest}".
    """
    global _viewed_team
    if code in auction_state.teams:
        _viewed_team = code


def _context(request: Request) -> dict:
    """Build template context with all current state."""
    team = auction_state.teams[MY_TEAM]
    wanted = {p.name for p in milp_solution.roster} if milp_solution and milp_solution.status == "Optimal" else set()

    # Build bid limits for available players with points
    bid_limits = []
    for name, player in sorted(
        auction_state.available_players.items(),
        key=lambda x: -x[1].projected_points,
    ):
        mp = market_prices.get(name, MIN_SALARY)
        model_p = model_prices[name].expected_price if name in model_prices else MIN_SALARY
        bid_limits.append({
            "name": name,
            "position": player.position,
            "nhl_team": player.nhl_team,
            "projected_points": player.projected_points,
            "model_price": round(model_p, 1),
            "market_price": round(mp, 1),
            # market_price = min(model_price, ceiling), so the two are equal on
            # every row until opponent budgets drain. Flag the rows where the
            # ceiling actually cuts the price — the table shows one column and
            # only marks it when the market is doing something. Shared with the
            # nomination panel's two figures, hence market.is_capped rather than
            # the comparison inline.
            "capped": is_capped(model_p, mp),
            "is_rfa": player.is_rfa,
            "in_optimal": name in wanted,
            "prior_fchl_team": player.prior_fchl_team,
        })

    # Compute projected standings (lightweight, no extra MILP solves)
    available_pool = sorted(
        auction_state.available_players.values(),
        key=lambda p: -p.projected_points,
    )
    projections = {}
    for code, t in auction_state.teams.items():
        current = t.current_roster_points
        if t.is_done:
            # A done team has STOPPED drafting, so its roster is final and its
            # projection is simply what it has. Projecting its unfilled slots is
            # not a small overstatement: measured 2026-08-13 on the
            # endgame-ceiling-binds scenario, the eight done teams read +673 to
            # **+1101** points above their real finals (SRL: 390 actual, 1491
            # shown) because a team that never spent still has
            # physical_max_bid = MAX_SALARY, so the affordability filter below
            # hands it the best players in the pool.
            #
            # It corrupts the RANK BADGE, which is the number you read to know
            # where you stand: BOT's real 1311 sat behind five phantom teams, so
            # the panel said #6 when BOT was first by a mile. Reachable in every
            # draft — the design notes put 3+ early finishers in each one — and
            # it gets worse the further a done team is from a full roster.
            #
            # This branch is first on purpose, so it also covers BOT. Marking
            # your own team done is a legal move in the League State table, and a
            # MILP that keeps planning purchases you have sworn off is the same
            # lie pointed at yourself. Done teams are already excluded from
            # market ceilings, demand counts and nomination order; this is the
            # same rule reaching the one place it had not.
            projected = current
        elif code == MY_TEAM and milp_solution and milp_solution.status == "Optimal":
            projected = int(milp_solution.total_points)
        elif code in exact_projections:
            # Somebody clicked Solve Standings and nothing has changed since —
            # `_recompute()` empties this dict on every mutation, so a hit here
            # is always a solve against the world currently on screen.
            #
            # Ordered AFTER the two branches above rather than first, because
            # neither can be improved on: a done team's roster is final, and
            # BOT's figure is already the same solve. Only live opponents are in
            # here, and only they read the estimate below.
            projected = exact_projections[code]
        else:
            # Only unfilled STARTER slots add points — bench scores nothing
            starter_slots = sum(t.roster_needs.values())
            if starter_slots > 0 and available_pool:
                affordable = [
                    p for p in available_pool
                    if market_prices.get(p.name, MIN_SALARY) <= t.physical_max_bid
                ]
                sample = min(len(affordable), starter_slots * 3)
                if sample > 0:
                    avg_pts = sum(p.projected_points for p in affordable[:sample]) / sample
                    projected = current + int(starter_slots * avg_pts)
                else:
                    projected = current
            else:
                projected = current
        projections[code] = {"current": current, "projected": projected}

    # What BASIS the figures above were computed on. `total` is the live
    # opponents — the only teams a solve can say anything new about — and
    # `estimated` is the count the template actually branches on.
    #
    # It branches on the ESTIMATES, not on the exact ones, and the difference is
    # a measured bug rather than a preference. With every opponent done,
    # `exact` is 0 and there is nothing to solve, yet every figure on screen is
    # exact: a done team projects its final roster and BOT projects its MILP
    # optimum (both verified 2026-08-18). Reading `exact` as the flag labelled
    # that column "estimated" and left a Solve Standings button that performed
    # zero solves and changed nothing — broken-looking, in the one state where
    # the operator most wants the final table.
    #
    # `exact` is still reported because a count is what covers the partial case:
    # an Infeasible opponent keeps its estimate, and a column labelled solved
    # while one cell is not is the silent-staleness problem the label exists to
    # prevent.
    live_opponents = sum(
        1 for c, t in auction_state.teams.items() if not t.is_done and c != MY_TEAM
    )
    standings_basis = {
        "exact": len(exact_projections),
        "total": live_opponents,
        "estimated": live_opponents - len(exact_projections),
    }

    # Add rank (sorted by projected descending)
    for rank, (code, _) in enumerate(
        sorted(projections.items(), key=lambda x: -x[1]["projected"]), 1
    ):
        projections[code]["rank"] = rank

    default_bidders = ",".join(
        c for c in auction_state.nomination_order if not auction_state.teams[c].is_done
    )

    # Resolved once and read twice below. Written out at both keys, the two
    # could be edited apart, and `buyout_dots_on_screen` disagreeing with
    # `viewed_team` IS the bug the boolean exists to prevent — the Scan button
    # offered against a panel that renders no dots for it to fill.
    #
    # Falls back to BOT rather than KeyError-ing, so a stored code that no
    # longer resolves renders your own roster instead of a panel for nobody.
    on_screen = auction_state.teams.get(_viewed_team, team)

    return {
        "request": request,
        "team": team,
        # The team whose roster is ON SCREEN, which is BOT until someone opens
        # another one. Split from `team` because `team` is also what the Trade
        # "I Give" list and the Buyout Analyzer act on: pointing that at the
        # team being viewed put an opponent's players in BOT's trade form
        # (fixed 2026-08-05). Only team_panel.html reads this.
        "viewed_team": on_screen,
        # Whether the `bo-` dot placeholders the buyout scan swaps into are
        # actually in the document — team_panel.html renders them for BOT only.
        # A DOM fact, deliberately not `viewed_team` itself: CLAUDE.md allows no
        # panel but team_panel.html to read that, and the rule exists to stop a
        # panel acting on the wrong roster. A boolean carries no roster.
        "buyout_dots_on_screen": on_screen.is_my_team,
        "teams": auction_state.teams,
        "available_players": auction_state.available_players,
        "transaction_log": auction_state.transaction_log,
        "change_log": auction_state.change_log,
        # The Logs panel's Auction/Transaction split. Deliberately a TOTAL
        # partition — `draft` and everything-else — rather than the allowlist
        # CLAUDE.md mandates for transaction_type elsewhere. That rule exists
        # because a mis-routed value points _viewed_team at "SRL→MAC"; here the
        # failure inverts, and a record matching no tab disappears from the log
        # entirely. A new transaction type must be visible in the wrong tab
        # rather than invisible in none, so the two lists always sum to
        # len(transaction_log).
        "auction_txns": [
            t for t in auction_state.transaction_log if t.transaction_type == "draft"
        ],
        "other_txns": [
            t for t in auction_state.transaction_log if t.transaction_type != "draft"
        ],
        "milp": milp_solution,
        "bid_limits": bid_limits,
        "nomination_order": auction_state.nomination_order,
        "my_team": MY_TEAM,
        # The league's salary cap, so a template quoting it in prose reads the
        # config rather than carrying its own copy of "11.4" to drift.
        "max_salary": MAX_SALARY,
        # Same reason: team_panel.html greys the Bench button at the cap and has
        # to name the number in the tooltip.
        "bench_size": BENCH_SIZE,
        "buyout_indicators": buyout_indicators,
        "market_prices": market_prices,
        "projections": projections,
        "standings_basis": standings_basis,
        "default_bidders": default_bidders,
        # Both read by base.html only, so they reach the screen on a full page
        # load and not on htmx partial swaps — which is what keeps them on
        # screen: a panel swap replaces panels, never the banners above them.
        "startup_warnings": _startup_warnings,
        "data_warning": _data_warning(),
    }


# -- Endpoints --

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Main page with all panels."""
    return _render(request, "index.html")


@app.post("/assign", response_class=HTMLResponse)
async def assign_player(
    request: Request,
    player: str = Form(...),
    team: str = Form(...),
    salary: float = Form(...),
):
    """Player drafted: assign to team at salary."""
    # Validate team
    if team not in auction_state.teams:
        return _toast(
            _render(request, "partials/all_panels.html"),
            f"Unknown team: {team}", "error",
        )

    # Validate BEFORE snapshotting — a failed assign must not leave a no-op
    # snapshot that eats the next undo
    if player not in auction_state.available_players:
        return _toast(
            _render(request, "partials/all_panels.html"),
            f"Player not found: {player}", "warning",
        )

    raw_salary = salary
    salary = _legal_salary(salary)
    clamp_note = (
        f" (salary adjusted from ${raw_salary:g}M)" if salary != raw_salary else ""
    )

    auction_state.save_snapshot()
    p = auction_state.available_players.pop(player)

    # Including the RFA group conversion the sale requires — see
    # PlayerOnRoster.from_pool, which is where that rule lives now.
    roster_player = PlayerOnRoster.from_pool(p, salary)
    to_minors = auction_state.teams[team].add_acquired_player(roster_player)
    # The sale still succeeds — refusing it mid-auction would cost clicks at the
    # worst moment — but the operator has to be told the player went down.
    minors_note = " — roster full, sent to minors" if to_minors else ""

    # Capture prices before removing from dicts
    model_price_val = model_prices[player].expected_price if player in model_prices else 0.0
    market_price_val = market_prices.get(player, 0.0)
    model_prices.pop(player, None)

    # Log transaction
    _log_transaction(
        p.name, p.position, team, salary, "draft",
        model_price=model_price_val, market_price=market_price_val,
        nhl_team=p.nhl_team,
    )

    _recompute()
    _save_state()
    # The view follows the sale. On your own pick that is BOT, unchanged since
    # 2026-08-07; on an opponent's it is theirs, because nothing of yours moved
    # and the roster that just went stale is the one worth looking at (owner
    # decision 2026-08-08). Success path only — a rejected assign is not a draft
    # action, and the error branches above return without touching the view, so
    # a typo'd team code does not cost you the roster you were auditing.
    _view_team(team)
    # Should essentially never fire: the league's commissioner software refuses
    # bids a team cannot afford. Kept because it costs one pass over a roster
    # next to a MILP solve, and leaving the busiest endpoint the silent one
    # would make the same warning elsewhere hard to trust.
    over = _cap_overages(team)
    return _toast(
        _render(request, "partials/all_panels.html"),
        f"{p.name} → {team} at ${salary}M{clamp_note}{minors_note}"
        + (f" — {'; '.join(over)}" if over else ""),
        "warning" if (to_minors or over) else "success",
        # `_recompute()` above just emptied `exact_projections`, so every
        # opponent's Proj went back to an estimate that runs +68 mean / +193
        # worst and moves 9 of 10 teams in rank order. Owner decision
        # 2026-09-10: re-solve after every pick rather than leave the column
        # degraded until someone notices the marker. SUCCESS PATH ONLY — the
        # rejection branches above all return before this, and a refused pick
        # must not spend ten MILP solves.
        after_settle={SOLVE_STANDINGS_EVENT: True},
    )


@app.post("/bid-check", response_class=HTMLResponse)
async def bid_check(
    request: Request,
    player: str = Form(...),
    bidders: str = Form(""),
    price: float = Form(0.5),
):
    """Live bidding: get bid recommendation."""
    p = auction_state.available_players.get(player)
    if p is None:
        # Say so, and hand back what was typed. This used to return the bare
        # empty form, which fails silently in BOTH directions the panel is
        # driven from. The "Start Auction" field is free text (`required` plus a
        # datalist, not readonly), so a typo lands here and the box simply
        # emptied — 1178 bytes, no toast, nothing to read. And the price input
        # carries hx-select="#bid-advice", which found no such id in that
        # response, so htmx swapped *nothing at all*: a player who left the pool
        # mid-bid left the panel frozen on stale advice.
        ctx = _context(request)
        ctx["bid_advice"] = None
        ctx["bid_error"] = (
            f"No player named “{player}” in the pool — check the spelling, "
            f"or he may already be drafted."
        )
        ctx["bid_player_text"] = player
        return _render(request, "partials/bid_panel.html", ctx)

    # Use live ceiling from active bidders if provided
    bidder_list = [b.strip() for b in bidders.split(",") if b.strip()]
    winner = None
    if bidder_list:
        opponents = live_opponents(bidder_list, auction_state.teams)
        # One shared notion of "last bidder standing": the advisor's WIN verdict
        # and the template's Assign button both derive from `winner`. An empty
        # bidder list means no auction is running, so there is no winner and a
        # WIN verdict there would be nonsense.
        winner = bid_winner(bidder_list, auction_state.teams)
        live_ceil = compute_live_ceiling(bidder_list, auction_state.teams)
        # The bidder identities are DERIVED here, not taken from the request.
        # `highest_bidder` used to arrive as a form field that no JavaScript
        # ever wrote, so it was permanently "" -> None while `demand_count`
        # right below said there were bidders — and `highest_bid` carried the
        # ceiling, which is the SECOND-highest max whenever BOT is only
        # observing, so it did not describe `highest_bidder` either.
        #
        # Nothing outside tests reads any of the three, which is exactly why
        # they have to be right: an inconsistent MarketInfo is a trap for
        # whoever reads one next. Same reasoning `floor_demand` carries, and
        # the same convention compute_market_ceiling uses — sort the eligible
        # opponents by physical max, descending, and take the first two.
        ranked = sorted(
            ((code, auction_state.teams[code].physical_max_bid) for code in opponents),
            key=lambda x: -x[1],
        )
        live_info = MarketInfo(
            market_ceiling=live_ceil,
            highest_bidder=ranked[0][0] if ranked else None,
            highest_bid=ranked[0][1] if ranked else 0.0,
            second_bidder=ranked[1][0] if len(ranked) > 1 else None,
            demand_count=len(opponents),
            # Keep compute_market_ceiling's invariant: no demand means the
            # player goes for the floor.
            floor_demand=not opponents,
        )
    else:
        live_info = market_info

    team = auction_state.teams[MY_TEAM]
    rec = compute_bid_recommendation(
        p, team, auction_state.available_players, market_prices, live_info, price,
        bot_uncontested=(winner == MY_TEAM),
        # Cached across price steps and bidder toggles — neither can change it.
        marginal_value=_marginal_value(p),
    )

    ctx = _context(request)
    ctx["bid_advice"] = rec
    ctx["bid_player"] = p
    ctx["bid_price"] = price
    ctx["active_bidders"] = bidder_list
    ctx["bid_winner"] = winner
    chart = _chart_context(player)
    if chart is not None:
        ctx.update(chart)
    # Bid half only — returning the whole panel would replace the nomination
    # recommendations on every price change and bidder toggle.
    return _render(request, "partials/bid_panel.html", ctx)


@app.get("/nominate", response_class=HTMLResponse)
async def nominate(request: Request):
    """Nomination recommendations for BOT — 1 RFA and 1 UFA.

    Available at any time, in or out of turn: `recommend_nomination` is
    hard-coded to MY_TEAM and reads no turn pointer, and knowing who you would
    put up is useful while somebody else is nominating.
    """
    model_expected = {name: pred.expected_price for name, pred in model_prices.items()}
    rfa_pick, ufa_pick = recommend_nomination(
        auction_state, market_prices, model_expected,
    )
    ctx = _context(request)
    ctx["rfa_pick"] = rfa_pick
    ctx["ufa_pick"] = ufa_pick
    # Nomination half only: this fires on a bare `n` keypress, and returning
    # the whole panel destroyed any in-flight bidding session.
    return _render(request, "partials/nomination_panel.html", ctx)


# The five locations, as the words that go on screen. An allowlist: an
# unrecognised `where` renders no label rather than borrowing another one's
# wording, the same rule `bid_panel.html` follows for `stop_status`.
_SEARCH_LABELS = {
    "pool": "available",
    "roster": "roster",
    "bench": "bench",
    "minors": "minors",
    "bought-out": "bought out",
}


def _provenance(txn: TransactionRecord | None) -> str:
    """How he got where he is, in one phrase — or nothing.

    A `trade` record's `team_code` is `f"{source}→{dest}"` and NOT a team
    code, which is why every branch here produces TEXT and none produces a
    link. The arrow form actually reads well as prose, so it is quoted rather
    than parsed; parsing it would be inventing a direction the log does not
    promise.

    Nothing for a buyout: the row is already labelled "bought out" and names
    the penalty, so a second line saying it again is noise.
    """
    if txn is None:
        return ""
    if txn.transaction_type == "draft":
        return f"drafted by {txn.team_code} for ${txn.salary:.1f}M"
    if txn.transaction_type == "trade":
        return f"traded {txn.team_code}"
    if txn.transaction_type in ("trade_in", "trade_out"):
        return f"{txn.transaction_type.replace('_', ' ')} ({txn.team_code})"
    return ""


def _search_rows(query: str) -> dict:
    """Template-ready rows for the header search.

    Every figure and every branch decision lives here rather than in Jinja —
    the shape `_driver_rows` established for the price breakdown. The template
    prints and formats; it decides nothing.

    Pool rows carry model and market price and nothing else, deliberately.
    A max bid is a binary search over MILP solves, and this endpoint fires on
    every keystroke; `tests/test_event_loop.py` exists because one solve on
    that path is a visible stall. The Available Players table and the bid
    panel are where that number lives.
    """
    result = auction_state.locate_players(query)
    # Copied verbatim from _context: reading `.roster` off an Infeasible
    # solution would mark players as wanted on the strength of a plan that
    # does not exist.
    wanted = (
        {p.name for p in milp_solution.roster}
        if milp_solution and milp_solution.status == "Optimal"
        else set()
    )

    rows = []
    for hit in result.hits:
        model_p = market_p = penalty = None
        if hit.where == "pool":
            model_p = round(
                model_prices[hit.name].expected_price
                if hit.name in model_prices else MIN_SALARY,
                1,
            )
            market_p = round(market_prices.get(hit.name, MIN_SALARY), 1)
        elif hit.where == "bought-out" and hit.salary is not None:
            penalty = hit.salary * BUYOUT_PENALTY_RATE

        rows.append({
            "name": hit.name,
            "where": hit.where,
            "label": _SEARCH_LABELS.get(hit.where, ""),
            "position": hit.position,
            "nhl_team": hit.nhl_team,
            "salary": hit.salary,
            "counts_on_cap": hit.counts_on_cap,
            "model_price": model_p,
            "market_price": market_p,
            "capped": is_capped(model_p, market_p) if model_p is not None else False,
            "in_optimal": hit.name in wanted,
            "penalty": penalty,
            "provenance": "" if hit.where == "bought-out" else _provenance(hit.last_txn),
            # DISPLAY and NAVIGATION are two fields, and a buyout is why.
            # `team_code` names whose cap carries the row — for a bought-out
            # player that is whichever team bought him out, and the penalty
            # sits in that team's panel header, so the code is worth showing
            # (and is the only place it appears, since he is on no roster to
            # read it off). But he is on no roster, and the
            # other four rows all promise "click this and see the player", so
            # linking there lands you somewhere he demonstrably is not. Only
            # the live locations navigate.
            "team_code": hit.team_code if hit.team_code in auction_state.teams else None,
            "link_team": (
                hit.team_code
                if hit.where != "bought-out" and hit.team_code in auction_state.teams
                else None
            ),
        })

    return {
        "search_query": result.query,
        "search_rows": rows,
        "search_total": result.total,
        "search_more": result.total - len(rows),
    }


def _load_nhl_odds() -> None:
    """Read the Cup odds for the navbar view, degrading to none.

    Not fatal, deliberately, and it is the one data file that can afford that:
    a saved state boots today without `team_odds.json` ever being opened (the
    odds are already baked into each `Player.team_probability`), so raising here
    would make startup stricter than it was for a reference table. A fresh boot
    still fails loudly — `build_initial_state` reads the same file and raises.
    """
    global nhl_odds
    try:
        nhl_odds = data_loader.load_team_odds()
    except (OSError, ValueError, KeyError) as e:
        nhl_odds = {}
        logging.getLogger("uvicorn.error").warning(
            "No NHL odds (%s: %s) — the odds view will say so", type(e).__name__, e
        )


def _club_counts() -> tuple[Counter, Counter]:
    """Players per NHL club — still in the pool, and already rostered.

    **Folded through `_nhl_canonical`, which is the whole subtlety.**
    `players.csv` spells Utah `UTH` on 78 rows and `team_odds.json` spells it
    `UTA`; a raw `Counter` on `p.nhl_team` splits one club across two rows, one
    of which then finds no odds. Same fold the badge and the label already go
    through — see `_nhl_logo_src`.

    One function rather than a copy in each caller, because the table and the
    "no odds entry" footer have to agree about what a club IS: a drifted second
    fold would list the same club in both at once.

    `rostered` reads `all_players`, not `roster_players` — a minor is drafted
    and off the board, so counting him as still available would say a contender
    has players left when it does not.
    """
    pool = Counter(
        _nhl_canonical(p.nhl_team)
        for p in auction_state.available_players.values() if p.nhl_team
    )
    rostered = Counter(
        _nhl_canonical(p.nhl_team)
        for team in auction_state.teams.values()
        for p in team.all_players if p.nhl_team
    )
    return pool, rostered


def _odds_rows() -> list[dict]:
    """One row per NHL club: odds, players left in the pool, players rostered.

    Alias keys are dropped from the odds dict for the reason `_club_counts`
    folds the pool: `load_team_odds` carries `UTH` AND `UTA` pointing at one
    number, so iterating it raw prints Utah twice with the pool split between
    them.
    """
    pool, rostered = _club_counts()
    rows = [
        {
            "code": code,
            "odds": odds,
            "in_pool": pool.get(code, 0),
            "rostered": rostered.get(code, 0),
        }
        for code, odds in nhl_odds.items()
        if _nhl_canonical(code) == code
    ]
    rows.sort(key=lambda r: (-r["odds"], r["code"]))
    return rows


def _odds_unlisted() -> list[dict]:
    """Clubs the pool uses that the odds file does not name.

    `players.csv` puts the FCHL placeholder `UFA` in the NHL TEAM column on 9
    rows, and `_get_team_probability` answers for it with
    `DEFAULT_TEAM_PROBABILITY` — silently. Showing them is the point: a club
    RESPELLED by a data refresh lands here too, and it would otherwise price a
    whole roster at the default with nothing on screen to say so.
    """
    pool, rostered = _club_counts()
    seen = pool + rostered
    return sorted(
        ({"code": code, "count": n} for code, n in seen.items() if code not in nhl_odds),
        key=lambda r: r["code"],
    )


@app.get("/nhl-odds", response_class=HTMLResponse)
async def nhl_odds_view(request: Request):
    """Stanley Cup odds per club, with what is left of each on the board.

    The odds are a price-model input — `team_probability`, one of the five
    drivers `decompose_price` reports — and nothing else in the app shows them.
    The counts are why this is a request rather than markup baked into
    `base.html`: they move with every pick, and a table rendered at page load
    would be wrong by the second nomination while looking authoritative.

    Builds its own context, taking `_render`'s short-circuit for the reason
    `/find-player` documents: `_context` costs ~8.5ms and a 705-row `bid_limits`
    for a fragment that reads neither.
    """
    return _render(request, "partials/nhl_odds.html", {
        "request": request,
        "odds_rows": _odds_rows(),
        "odds_unlisted": _odds_unlisted(),
        # Read off the module, never from-imported: `last_odds_season` is a
        # REBIND, which is the exact hazard the `import data_loader` comment at
        # the top of this file names.
        "odds_season": data_loader.last_odds_season,
        "odds_default": DEFAULT_TEAM_PROBABILITY,
    })


@app.get("/find-player", response_class=HTMLResponse)
async def find_player(request: Request, q: str = ""):
    """Where is this player? — the header search.

    A QUERY parameter for the reason `/buyout-check` documents: a `#` in a
    path truncates at the fragment and never reaches the server, and
    `_disambiguated_names`' last-resort tier is ` (#n)`.

    **It deliberately does not build `_context`.** This fires on every
    keystroke, and `_context` costs ~8.5ms and assembles a 705-row
    `bid_limits` list regardless of what is rendered, against 0.33ms of
    actual work. Passing a dict already carrying `"request"` takes `_render`'s
    existing short-circuit. That narrows ONE endpoint; the BACKLOG entry
    asking for a per-panel context builder across all of them is untouched.

    `teams` is passed because the row template names the holding club, and it
    is a dict reference rather than a computation.
    """
    return _render(request, "partials/search_results.html", {
        "request": request,
        "teams": auction_state.teams,
        "my_team": MY_TEAM,
        **_search_rows(q),
    })


@app.get("/explain/{player_name}", response_class=HTMLResponse)
async def explain(
    request: Request,
    player_name: str,
    inline: bool = False,
    price: float | None = None,
):
    """Why not bid: counterfactual explanation.

    `inline=1` returns the body alone, for the bid panel's lazy mount; the
    default returns the standalone `#explanation` section the Available Players
    table's "?" links swap. Same analysis, two mount points — a query param
    rather than a second route, since only the wrapper differs.

    `price` is the same idea one level down: the Recompute button re-asks for
    the card at the bid on the table rather than at the expected clearing
    price. Optional, because omitting it has to keep meaning "the market
    price" — that is what the lazy mount fires on every panel swap, and making
    it mandatory would put a 200ms solve on the trigger instead of the click.
    """
    template = (
        "partials/counterfactual.html" if inline else "partials/explanation.html"
    )
    ctx = _context(request)
    ctx["counterfactual"] = None
    # Only the bid panel's mount owns a live price, so only it gets the button
    # that reads one — see counterfactual.html.
    ctx["cf_inline"] = inline
    cf = _counterfactual_context(player_name, price)
    if cf is not None:
        ctx.update(cf)
    return _render(request, template, ctx)


@app.post("/trade-evaluate", response_class=HTMLResponse)
async def trade_evaluate(request: Request):
    """Evaluate a proposed trade."""
    global last_trade_eval
    form = await request.form()

    give_names = form.getlist("give_player")
    receive_json = form.getlist("receive_player")
    source_team = (form.get("source_team") or "").strip() or None

    give = []
    for name in give_names:
        p = auction_state.teams[MY_TEAM].find_player(name)
        if p:
            give.append(PlayerTrade(p.name, p.position, p.salary, p.projected_points))

    receive = []
    for raw in receive_json:
        if raw.strip():
            try:
                data = json.loads(raw)
                receive.append(PlayerTrade(
                    name=data["name"],
                    position=data["position"],
                    salary=float(data["salary"]),
                    projected_points=int(data["projected_points"]),
                ))
            except (json.JSONDecodeError, KeyError):
                pass

    if give or receive:
        result = evaluate_trade(
            auction_state, give, receive, market_prices, source_team_code=source_team,
        )
        last_trade_eval = result
    else:
        result = None

    ctx = _context(request)
    ctx["trade_result"] = result
    # The VERDICT alone, not the panel. The ticked checkboxes, the partner
    # select and the JS-built "I Receive" list live only in the DOM, exactly
    # like the bidding session in #bid-panel -- returning the whole panel here
    # meant every evaluate handed back a stateless copy of the form you had just
    # filled in, so modifying a trade meant re-entering it. Also 1.9KB instead
    # of 27KB, since the 49-row give list stops being serialised per evaluate.
    return _render(request, "partials/trade_verdict.html", ctx)


@contextmanager
def _undoable(*, rollback: bool) -> Iterator[None]:
    """capture -> attempt -> commit, so a REJECTED request costs no undo depth.

    save_snapshot() captures and commits in one call, so calling it before the
    operation is known to succeed evicts the oldest chain entry at MAX_SNAPSHOTS
    while the restore_snapshot() on the error path pops from the other end — a
    refusal reads as a no-op while quietly destroying a real undo step.

    `rollback` is not a style choice. Pass False only when the operation
    validates before it mutates, so a refusal has changed nothing; pass True when
    it can mutate and then raise, which is the only case rollback_to has to
    undo. Never restore_snapshot() here: that is Ctrl+Z, and spending a chain
    entry is what it means.

    The contract is ValueError and nothing wider, which is what the four
    hand-rolled sites did before this existed: another exception mid-mutation
    neither rolls back nor commits, so the partial mutation survives into a 500.
    Widening to Exception would change error semantics on the undo path, so it is
    a deliberate limit rather than an oversight.
    """
    before = auction_state.capture_snapshot()
    try:
        yield
    except ValueError:
        if rollback:
            auction_state.rollback_to(before)
        raise
    auction_state.commit_snapshot(before)


@app.post("/trade-execute", response_class=HTMLResponse)
async def trade_execute(request: Request, trade_id: str = Form("")):
    """Execute a previously evaluated trade."""
    global last_trade_eval
    if last_trade_eval is None:
        return _toast(
            _render(request, "partials/all_panels.html"),
            "No current trade evaluation — state changed since it was "
            "evaluated. Re-evaluate the trade.", "warning",
        )
    if trade_id != last_trade_eval.trade_id:
        return _toast(
            _render(request, "partials/all_panels.html"),
            "Stale trade — re-evaluate before executing.", "warning",
        )

    # Capture trade details before clearing
    trade_give = last_trade_eval.give
    trade_receive = last_trade_eval.receive
    source_team = last_trade_eval.source_team_code

    # rollback=True: execute_trade removes from one team before adding to the
    # other and can raise partway.
    try:
        with _undoable(rollback=True):
            execute_trade(auction_state, trade_give, trade_receive,
                          source_team_code=source_team)
    except ValueError as e:
        last_trade_eval = None
        return _toast(
            _render(request, "partials/all_panels.html"),
            f"Trade failed: {e}", "error",
        )
    last_trade_eval = None

    # Log trade transactions for both teams (when source_team is known)
    now = datetime.now().isoformat()
    for p in trade_give:
        club = _nhl_team_of(p.name)
        _log_transaction(p.name, p.position, MY_TEAM, p.salary, "trade_out",
                         timestamp=now, nhl_team=club)
        if source_team:
            _log_transaction(p.name, p.position, source_team, p.salary, "trade_in",
                             timestamp=now, nhl_team=club)
    for p in trade_receive:
        club = _nhl_team_of(p.name)
        _log_transaction(p.name, p.position, MY_TEAM, p.salary, "trade_in",
                         timestamp=now, nhl_team=club)
        if source_team:
            _log_transaction(p.name, p.position, source_team, p.salary, "trade_out",
                             timestamp=now, nhl_team=club)

    # Recompute model prices for any newly available players
    global model_prices
    model_prices = predict_all_prices(auction_state.available_players, model_params)
    _recompute()
    _save_state()
    over = _cap_overages(MY_TEAM, source_team)
    return _toast(
        _render(request, "partials/all_panels.html"),
        "Trade executed" + (f" — {'; '.join(over)}" if over else ""),
        "warning" if over else "success",
    )


@app.get("/buyout-check", response_class=HTMLResponse)
async def buyout_check(request: Request, player_name: str):
    """Preview buyout impact.

    The name is a QUERY parameter, not a path segment, and that is what lets the
    Analyzer's picker be a bare `<select name="player_name">` with no wrapper,
    no submit button and no JS — htmx sends a triggering select's own value on a
    GET. It also removes an encoding hazard the path form carried: this was the
    one place in the app a raw player name went into a URL unencoded (every
    other name-in-path call site uses `|urlencode`). No name in the current pool
    trips it, but `_disambiguated_names`' last-resort tier is ` (#n)`, and a `#`
    never reaches the server at all — the request would truncate at the fragment
    and the panel would answer "not found" for a player on the roster in front
    of you. A data refresh is exactly what turns that tier on.
    """
    try:
        result = evaluate_buyout(auction_state, player_name, market_prices)
    except ValueError as e:
        # Rendering an empty panel told the operator nothing. An ineligible
        # group is the common case now, and the reason IS the useful answer.
        ctx = _context(request)
        ctx["buyout_result"] = None
        return _toast(
            _render(request, "partials/buyout_panel.html", ctx),
            str(e), "error",
        )

    ctx = _context(request)
    ctx["buyout_result"] = result
    return _render(request, "partials/buyout_panel.html", ctx)


@app.post("/buyout", response_class=HTMLResponse)
async def buyout(
    request: Request,
    player: str = Form(...),
    team_code: str = Form(MY_TEAM),
):
    """Execute a buyout on any team's roster.

    `team_code` defaults to BOT because the Buyout Analyzer's Execute button is
    the older caller and acts on `team`, which is always BOT. The team panel's
    picker posts it explicitly for whichever roster is on screen — the league
    permits any team to buy anyone out, and an opponent's buyout that cannot be
    recorded leaves their cap wrong in every calculation the tool makes about
    them, the market ceiling included.
    """
    # Capture player info before execute_buyout removes them. `.get`, because
    # an unknown code has to reach execute_buyout's own refusal rather than
    # KeyError-ing here — one definition of "which teams exist".
    team = auction_state.teams.get(team_code)
    p = team.find_player(player) if team else None
    if p:
        bo_position = p.position
        bo_salary = p.salary
        bo_nhl_team = p.nhl_team

    # rollback=True: execute_buyout can raise after mutating. Ineligible players
    # are refused routinely (the Analyzer only offers group 2/3, but /buyout takes
    # any name), so this path is walked.
    try:
        with _undoable(rollback=True):
            execute_buyout(auction_state, player, team_code)
    except ValueError as e:
        # Report the actual reason: this used to say "not found" for every
        # failure, so an ineligible-group refusal named the wrong problem.
        return _toast(
            _render(request, "partials/all_panels.html"),
            f"Buyout failed: {e}", "error",
        )

    # Log buyout transaction
    if p:
        _log_transaction(player, bo_position, team_code, bo_salary, "buyout",
                         nhl_team=bo_nhl_team)

    _recompute()
    _save_state()
    # The view follows the roster that changed, the 2026-08-08 policy — which
    # is BOT for your own buyout and the rival for theirs. `/undo` mirrors this
    # off the logged record and needed no change: it already reads t.team_code.
    _view_team(team_code)
    return _toast(
        _render(request, "partials/all_panels.html"),
        # Names the team and the dead cap it just took on. "Bought out X" alone
        # was unambiguous while only one roster could be touched.
        f"Bought out {player} ({team_code}) — "
        f"${(bo_salary * BUYOUT_PENALTY_RATE if p else 0.0):.1f}M penalty "
        f"stays on {team_code}'s cap",
        "success",
    )


@app.post("/team-done", response_class=HTMLResponse)
async def team_done(request: Request, team_code: str = Form(...)):
    """Toggle team as finished drafting."""
    t = auction_state.teams.get(team_code)
    if t is None:
        return _toast(
            _render(request, "partials/all_panels.html"),
            f"Unknown team: {team_code}", "error",
        )
    auction_state.save_snapshot()
    t.is_done = not t.is_done
    _log_change(
        "team-done",
        team_code,
        f"{team_code} marked as {'done' if t.is_done else 'still drafting'}",
    )
    _recompute()
    _save_state()
    return _render(request, "partials/all_panels.html")


@app.post("/undo", response_class=HTMLResponse)
async def undo(request: Request):
    """Restore previous snapshot, telling the user WHAT was undone."""
    pre_txn = list(auction_state.transaction_log)
    pre_chg = list(auction_state.change_log)

    if not auction_state.restore_snapshot():
        return _toast(
            _render(request, "partials/all_panels.html"),
            "Nothing to undo", "warning",
        )

    # Whichever log shrank names the reverted action; a silent undo made
    # the Ctrl+Z-while-typing footgun invisible
    message = "Undid last action"
    if len(auction_state.transaction_log) < len(pre_txn):
        t = pre_txn[-1]
        message = (
            f"Undid {t.transaction_type}: {t.player_name} → "
            f"{t.team_code} (${t.salary:.1f}M)"
        )
        # Mirror the view policy of the action reverted, off the record the
        # message above already read — no state-layer change, which is what
        # kept this deferred. ALLOWLIST, never a denylist: the real types are
        # draft / trade_out / trade_in / trade / buyout, and /trade-between logs
        # team_code as "SRL→MAC", so a denylist that missed one string would
        # point the view at something that is not a team code at all. One
        # branch covers draft and buyout because both log the team whose roster
        # moved — which for a buyout was always MY_TEAM until 2026-09-12 and is
        # now whichever team the panel's picker named. The code did not change;
        # it read t.team_code all along.
        if t.transaction_type in ("draft", "buyout"):
            _view_team(t.team_code)
    elif len(auction_state.change_log) < len(pre_chg):
        message = f"Undid: {pre_chg[-1].description}"
        # View untouched on purpose, matching the roster-edit endpoints, which
        # do not touch it either. Every one of those controls renders inside
        # team_panel.html, so you cannot click Bench for a roster that is not on
        # screen — staying put IS showing the team whose edit just came back.
        # Undoing an opponent's roster edit no longer throws you home.
        #
        # Not mirrored off pre_chg[-1].team_code even though ChangeRecord has
        # one: "team-done" is a ChangeRecord kind, so mirroring would swap the
        # panel to an uninvolved third team — exactly what the 2026-08-07 fix
        # removed from the forward path. The gap that accepts: edit an opponent,
        # navigate away, then Ctrl+Z, and the view stays where you last put it.
        # Your /team-view click is newer information than the log.

    global model_prices
    model_prices = predict_all_prices(auction_state.available_players, model_params)
    _recompute()
    _save_state()
    return _toast(
        _render(request, "partials/all_panels.html"), message, "info",
    )


@app.get("/buyout-indicators", response_class=HTMLResponse)
async def buyout_indicators_endpoint(request: Request):
    """Compute buyout indicators lazily, loaded via HTMX after page render.

    ~15 MILP solves, off the event loop. Everything this handler reads from the
    live state — the snapshot, the prices, the figure to beat — is read HERE, on
    the loop, and handed to the solver; everything it writes goes through
    `_publish_if_current`, also on the loop. So no worker thread ever touches a
    module global, and the only thing the threading buys is that the other
    requests keep being answered.

    The `deepcopy` is safety, not speed — 3ms against a 78ms solve. The solver
    only reads, but it reads `available_players` and every roster while an
    `/assign` can now run alongside it, and a dict that changes size during
    iteration raises.
    """
    version = _state_version
    snapshot = deepcopy(auction_state)
    current = (
        milp_solution.total_points
        if milp_solution and milp_solution.status == "Optimal" else 0
    )
    solved = await run_in_threadpool(
        _solve_buyout_indicators, snapshot, market_prices, current
    )
    if not _publish_if_current(version, buyout_indicators, solved, "buyout indicators"):
        # An EMPTY body, not the dots template. `buyout_dots.html` paints a
        # verdict on every eligible player and defaults a missing one to "keep",
        # so rendering it after a discard would turn all 15 dots green — which
        # reads as "no buyout helps" and is exactly the silent failure the
        # 2026-08-07 minors bug produced. `hx-swap="none"` plus no body leaves
        # the placeholders grey, which is the truth: nobody has scanned this
        # state yet. The toast is the only thing that says so.
        return _toast(
            HTMLResponse(""),
            "Roster changed while scanning — nothing to show, scan again",
            "warning",
        )
    ctx = _context(request)
    return _render(request, "partials/buyout_dots.html", ctx)


@app.get("/solve-standings", response_class=HTMLResponse)
async def solve_standings(request: Request):
    """Replace the Proj column's estimates with real per-team MILP optima.

    Manual, and out-of-band only, for the same reason the buyout Scan button is:
    the work costs ~1.3s on a fresh league and every draft action would pay it.
    `hx-swap="none"` on the trigger, so the ONLY thing this response does is swap
    the `proj-<CODE>` spans and the basis marker — nothing else in League State
    moves, and nothing outside it is touched.

    A GET that mutates only a derived cache: no snapshot, nothing saved to disk,
    and `/undo` has nothing to revert, because no draft record changed. That is
    also why it does not go through `_recompute()` — which would empty the dict
    it just filled.

    Solved off the event loop, published only if the state has not moved
    underneath — `_publish_if_current` carries the reasoning for both scans, and
    `/buyout-indicators` the note on why the snapshot is taken here rather than
    in the thread.
    """
    version = _state_version
    snapshot = deepcopy(auction_state)
    solved = await run_in_threadpool(
        _solve_exact_projections, snapshot, market_prices
    )
    _publish_if_current(version, exact_projections, solved, "exact projections")
    return _render(request, "partials/standings_cells.html", _context(request))


@app.post("/reset", response_class=HTMLResponse)
async def reset(request: Request):
    """Reset to fresh state from CSV data."""
    global auction_state, model_prices
    # A deliberate fresh start answers whatever the banner was warning about —
    # leaving it up would have it read as a live alarm against a state the
    # operator just chose. Nothing here can re-degrade: build_initial_state
    # raises rather than half-loading.
    _startup_warnings.clear()
    auction_state = build_initial_state()
    model_prices = predict_all_prices(auction_state.available_players, model_params)
    _recompute()
    _save_state()
    _view_team(MY_TEAM)  # new world; a view into the old one means nothing
    return _render(request, "partials/all_panels.html")


@app.post("/load-scenario", response_class=HTMLResponse)
async def load_scenario(request: Request, name: str = Form(...)):
    """Load a pre-baked test scenario."""
    import scenarios as _scenarios

    global auction_state, model_prices
    # Capture prior state so /undo can roll back the scenario load. Snapshots
    # live on AuctionState._snapshots, which gets wiped when we replace the
    # global — copy it onto the new state explicitly.
    prior = auction_state.to_json(include_snapshots=False)
    try:
        new_state = _scenarios.load(name)
    except KeyError:
        return _toast(
            _render(request, "partials/all_panels.html"),
            f"Unknown scenario: {name}", "error",
        )
    except Exception as e:
        # A scenario that cannot BUILD is a different failure from one that does
        # not exist, and it needs its own answer: `_fill` raises when the pool
        # cannot supply what a construction asks for, which is exactly what a
        # refreshed `players.csv` could cause. Unhandled it is a 500, and htmx
        # swaps nothing on a 500 — so the operator gets a click that silently did
        # nothing, the same class of bug as the no-feedback scan button.
        #
        # Safe to report and carry on precisely because nothing has moved yet:
        # every mutation below happens after `load` returns, so the live draft is
        # untouched. Logged as well as toasted, since a toast auto-dismisses and
        # the traceback is what says which construction step gave up.
        logging.exception("Scenario %s failed to build", name)
        return _toast(
            _render(request, "partials/all_panels.html"),
            f"Scenario {name} failed to build: {type(e).__name__}: {e}", "error",
        )
    new_state._snapshots.append(prior)
    auction_state = new_state
    model_prices = predict_all_prices(auction_state.available_players, model_params)
    _recompute()
    _save_state()
    _view_team(MY_TEAM)  # ditto — the unknown-scenario branch leaves it alone
    return _toast(
        _render(request, "partials/all_panels.html"),
        f"Loaded scenario: {name}", "success",
    )


_TWO_PI_SQRT = math.sqrt(2.0 * math.pi)


def _lognormal_pdf_path(
    log_mu: float,
    sigma: float,
    p_floor: float,
    scale_max: float,
    min_salary: float,
    n_points: int = 60,
    x_off: float = 10.0,
    chart_width: float = 380.0,
    y_axis: float = 75.0,
    max_height: float = 55.0,
) -> tuple[str, tuple[float, float, float, float] | None]:
    """Build SVG path for log-normal PDF curve and optional floor spike."""
    if sigma <= 0:
        return "", None

    scale = chart_width / scale_max
    x_start = max(min_salary, 0.01)
    x_end = scale_max
    step = (x_end - x_start) / n_points

    # Sample PDF values
    points: list[tuple[float, float]] = []
    peak_pdf = 0.0
    for i in range(n_points + 1):
        x = x_start + i * step
        ln_x = math.log(x)
        exponent = -((ln_x - log_mu) ** 2) / (2.0 * sigma * sigma)
        pdf = (1.0 / (x * sigma * _TWO_PI_SQRT)) * math.exp(exponent)
        pdf *= 1.0 - p_floor
        svg_x = x_off + x * scale
        points.append((svg_x, pdf))
        peak_pdf = max(peak_pdf, pdf)

    if not points or peak_pdf == 0:
        return "", None

    # Scale to pixel height
    h_scale = max_height / peak_pdf
    parts = [f"M {points[0][0]:.1f} {y_axis:.1f}"]
    for svg_x, pdf in points:
        parts.append(f"L {svg_x:.1f} {y_axis - pdf * h_scale:.1f}")
    parts.append(f"L {points[-1][0]:.1f} {y_axis:.1f} Z")
    curve_d = " ".join(parts)

    # Floor spike when p_floor is meaningful
    floor_bar = None
    if p_floor > 0.05:
        bar_x = x_off + min_salary * scale
        bar_h = p_floor * max_height
        bar_w = max(2.0, 0.1 * scale)
        floor_bar = (bar_x - bar_w / 2, y_axis - bar_h, bar_w, bar_h)

    return curve_d, floor_bar


def _is_unit(x: float, places: int = 2) -> bool:
    """Does this multiplier print as 1 on the card, at the precision shown?

    The card's own precision is what decides whether a row says anything, so
    the hiding rule reads the RENDERED figure rather than the float behind it.
    `places` exists because the card prints the same quantity two ways: two
    decimals in the table, one in the collapsed <summary>. Both have to match
    their own template formatter or the rule hides the wrong things.
    """
    return f"{x:.{places}f}" == f"{1.0:.{places}f}"


# Where the odds-ratio label stops printing digits, and the only reason the
# exponential below is safe. `math.exp` UNDERFLOWS to 0.0 below about -745 and
# raises OverflowError above +709; `_odds_label` then divides by its argument.
# The pool has 729 of headroom today (floor_logit_delta runs -16.27 to +3.94),
# so this is not reachable with current data -- but `data/model_params.json` is
# regenerated by another repo, and the card is embedded in `/bid-check`, so the
# failure would be a 500 on the bidding path rather than a wrong number.
_ODDS_CAP = 1000.0
_ODDS_CAP_LOG = math.log(_ODDS_CAP)


def _odds_ratio(log_delta: float) -> float:
    """One driver's odds ratio on a floor sale, clamped where the label caps.

    Clamped in LOG space, before the exponential, so no coefficient can make
    exp() overflow or return a zero that `_odds_label` would divide by. The
    clamp is invisible: it only binds past a thousandfold, which is exactly
    where the label stops printing digits anyway.
    """
    return math.exp(max(-_ODDS_CAP_LOG, min(_ODDS_CAP_LOG, log_delta)))


def _odds_label(ratio: float) -> str:
    """One driver's effect on the ODDS of a floor sale, as it appears on screen.

    Not `%.2f`. Measured over the fresh 705-player pool the stage-1 odds ratios
    span 8.6e-08 to 51.6, so a fixed two decimals prints `x0.00` on 66 rows —
    a column saying "this driver did nothing" about the single largest effect
    on the card. The reciprocal is shown as a division instead, which is the
    same order-invariant quantity read the other way round, with precision that
    follows the magnitude: p50 is 1.99, p90 14.2, p99 3392.

    Above a thousandfold the digits stop meaning anything an operator can use —
    "he does not go at the minimum" is the whole content — so the label caps and
    the bar carries the magnitude. ~1% of rows reach it.

    A non-positive ratio is not a number this can describe, so it reports the
    cap rather than dividing by it. `_odds_ratio` already makes that
    unreachable from the card; this is here because the reciprocal is a
    division and a crash on the bidding path is not worth leaving to one
    caller's discipline.
    """
    if ratio <= 0.0:
        return "÷1000+"
    r, sym = (ratio, "×") if ratio >= 1.0 else (1.0 / ratio, "÷")
    # On the ROUNDED value, so the cap agrees with the digits underneath it.
    # `1 / exp(-log(1000))` is 999.9999999999998, which a bare `r >= _ODDS_CAP`
    # lets through to print a bare "1000" -- the capped figure without the "+",
    # claiming precision the label exists to refuse.
    if round(r) >= _ODDS_CAP:
        return f"{sym}1000+"
    if r >= 100:
        return f"{sym}{r:.0f}"
    if r >= 10:
        return f"{sym}{r:.1f}"
    return f"{sym}{r:.2f}"


def _driver_rows(b: PriceBreakdown) -> dict:
    """Template-ready rows for the price-driver breakdown.

    Every bit of arithmetic and every bit of prose lives here rather than in
    Jinja: the bar needs the largest |log_delta| across rows, and "rank 203"
    needs exp() back out of `log_rank`.

    **Two chains, and both are multiplicative.** Stage 2 gives a price factor
    `exp(log_delta)`; stage 1 gives an odds ratio `exp(floor_logit_delta)` on
    P(floor). Both are order-invariant, which is the whole reason either can be
    shown per row. Measured, the stage-1 chain reconstructs exactly —
    base_odds x PROD(odds ratios) lands on the player's own odds to a maximum
    relative error of 6.8e-13 over the pool.

    **Factors, never per-row dollar steps — and never per-row PERCENTAGE POINTS
    either.** A dollar step is exp(running + delta) - exp(running), so it moves
    with the row's position in the list; measured across all 120 orderings of
    the five groups, McDavid's Points step runs $1.60M to $13.24M. The same
    trap wearing a different costume is a P(floor) column in percentage points:
    the sigmoid is nonlinear, so "+12pp" depends on what the running odds were
    when the row was applied. Only the two ENDS of each chain are quoted in
    dollars and percent, and both ends are order-free.
    """
    ref, feats = b.reference, b.features

    def _describe(group: str) -> str:
        """This player's value for the group, against the reference's."""
        if group == "Points":
            if b.position == "G":
                return (f"{feats['proj_wins']:.0f} projected wins "
                        f"(typical {ref['proj_wins']:.0f})")
            return (f"{feats['projected_points']:.0f} pts "
                    f"(typical {ref['projected_points']:.0f})")
        if group == "NHL team":
            return (f"{feats['team_probability']:.1f}% Cup odds "
                    f"(typical {ref['team_probability']:.1f}%)")
        if group == "Reputation":
            mine = (f"${math.exp(feats['log_lag']):.1f}M last season"
                    if feats["has_lag"] else "new to the league")
            theirs = (f"${math.exp(ref['log_lag']):.1f}M"
                      if ref["has_lag"] else "none")
            return f"{mine} (typical: {theirs})"
        if group == "Scarcity":
            return (f"rank {math.exp(feats['log_rank']):.0f} "
                    f"(typical {math.exp(ref['log_rank']):.0f})")
        return ("RFA" if feats["is_rfa"] else "UFA") + (
            " (typical: RFA)" if ref["is_rfa"] else " (typical: UFA)"
        )

    # A driver that moves the price by nothing is hidden, not printed as
    # x1.00. Two of the five are structurally dead in states the owner is
    # actually in: `coef_log_rank` is exactly 0.0 for G, so Scarcity cannot
    # apply to a goalie, and on a pool where nobody carries a prior FCHL
    # salary every player AND the reference sit on ln(MIN_SALARY) with
    # has_lag 0, so Reputation reads x1.000 on every card. Both were read off
    # the card as "the model ignores this" — true of a goalie's rank, false of
    # reputation, and the row was answering neither question.
    #
    # A row is hidden only when BOTH its columns print 1.00, and reading only
    # the price column would be a real bug rather than a nicety: measured over
    # the fresh 705-player pool, 11 of the 12 rows that print x1.00 on price
    # move the floor odds by something worth seeing, against exactly 1 row
    # inert in both. Hiding on the price column alone would drop eleven rows
    # whose whole content is in the other one.
    #
    # Rounded, not exactly zero. Until 2026-09-11 this tested `log_delta !=
    # 0.0` and the comment justified it: a dropped x1.004 row "would leave a
    # gap between the base and the median that nothing on screen explains".
    # Measured, that gap is already there and is 70x larger — printed base x
    # printed factors misses the printed unclamped median by more than $0.01M
    # on 312 of 705 players (max $0.19M, McDavid $16.04M against $15.85M),
    # purely from rounding each factor to 2dp. Dropping every x1.00 row changes
    # it by $0.0000M. Rounding is what the reader sees, so rounding is what
    # decides whether a row said anything.
    # One exponential per driver, reused by the filter and the label. Three
    # separate `math.exp(d.floor_logit_delta)` calls is not a speed problem on
    # five drivers; it is that the safe form has to be the only form, and a
    # second copy is where the raw one grows back.
    odds = [(d, _odds_ratio(d.floor_logit_delta)) for d in b.drivers]
    live = [(d, o) for d, o in odds if not (_is_unit(d.factor) and _is_unit(o))]
    widest = max((abs(d.log_delta) for d, _ in live), default=0.0)
    floor_widest = max((abs(d.floor_logit_delta) for d, _ in live), default=0.0)
    rows = [
        {
            "group": d.group,
            "factor": d.factor,
            "detail": _describe(d.group),
            "up": d.log_delta >= 0,
            # Sized on the LOG delta, which is the order-invariant quantity.
            "bar_px": round(60.0 * abs(d.log_delta) / widest) if widest else 0,
            # The LABEL, not the ratio. A raw `floor_factor` shipped alongside
            # it for one commit, rendered by nothing — which is the exact defect
            # the finding this feature closed described one level up (now in
            # CHANGELOG.md — it left BACKLOG.md when it was resolved):
            # an unrendered number beside a price factor is an invitation to
            # render it as one.
            "floor_label": _odds_label(o),
            # Its OWN direction, never the price column's. Measured, the two
            # disagree about what they do to the price on 195 of 2361 rows —
            # dearer if he clears the floor AND likelier to sell at it — and
            # colouring both by one "pushes the price up" reading would paint
            # over exactly the rows worth looking at.
            "floor_up": d.floor_logit_delta >= 0,
            "floor_bar_px": (round(60.0 * abs(d.floor_logit_delta) / floor_widest)
                             if floor_widest else 0),
        }
        for d, o in live
    ]

    base_bits = [f"{ref['projected_points']:.0f} pts",
                 f"rank {math.exp(ref['log_rank']):.0f}"]
    base_bits.append("an FCHL salary last season" if ref["has_lag"]
                     else "no FCHL history")
    base_bits.append(f"{ref['team_probability']:.1f}% Cup odds")

    clamp_note = None
    if b.clamped == "min":
        clamp_note = (f"raised from ${b.unclamped_price:.2f}M to the "
                      f"${b.min_bid:.1f}M minimum bid")
    elif b.clamped == "max":
        clamp_note = (f"cut from ${b.unclamped_price:.2f}M to the "
                      f"${b.max_bid:.1f}M {b.position} maximum")

    return {
        "base_price": b.base_price,
        "base_label": f"Typical {b.position}",
        "base_detail": " · ".join(base_bits),
        "base_p_floor": b.base_p_floor,
        "rows": rows,
        # Top two by effect, for the collapsed <summary> — the card has to
        # answer the question without being opened.
        #
        # Filtered on ONE decimal, because that is what the summary prints
        # (`%.1f` in player_chart.html) while the table prints two. The table
        # learned on 2026-09-11 to hide a row that says nothing and the summary
        # did not, which made it the worse offender of the two: measured,
        # **135 of 705** players had a closed summary naming a driver and
        # printing `×1.0`, against 12 such rows inside — and for **82** of them
        # that same driver moved the floor odds by 1.5x or more, so the summary
        # had something to say and said nothing instead. Dropping the entry is
        # right rather than lossy: the floor percentage beside it is already
        # the answer for those players (Kiefer Sherwood, 99% floor, reads
        # "Points ×1.2 · 99% floor" instead of adding "Scarcity ×1.0").
        "headline": [
            r for r in sorted(rows, key=lambda r: -abs(math.log(r["factor"])))
            if not _is_unit(r["factor"], places=1)
        ][:2],
        "median_price": b.prediction.median_price,
        "clamp_note": clamp_note,
        "p_floor": b.prediction.p_floor,
        "expected_price": b.prediction.expected_price,
    }


def _chart_context(player_name: str) -> dict | None:
    """Build the template variables needed by player_chart.html.

    Returns None if the player isn't priceable (unknown or missing model
    prediction). Used by both /player-chart/{name} and /bid-check (which
    embeds the chart inline during an active auction).
    """
    p = auction_state.available_players.get(player_name)
    if p is None:
        return None
    pred = model_prices.get(player_name)
    if pred is None:
        return None
    mp = market_prices.get(player_name, MIN_SALARY)
    curve_d, floor_bar = _lognormal_pdf_path(
        log_mu=pred.log_mu,
        sigma=pred.sigma,
        p_floor=pred.p_floor,
        scale_max=MAX_SALARY,
        min_salary=MIN_SALARY,
    )
    # A pool with no player at this position, or a legacy snapshot whose
    # backfill was skipped, leaves no reference — the card drops the breakdown
    # and keeps the chart rather than 500ing.
    reference = auction_state.price_reference.get(p.position)
    drivers = (
        _driver_rows(decompose_player(p, model_params, reference))
        if reference
        else None
    )
    return {
        "chart_player": p,
        "chart_data": pred,
        "chart_market_price": mp,
        "chart_scale_max": MAX_SALARY,
        "chart_curve_d": curve_d,
        "chart_floor_bar": floor_bar,
        "chart_drivers": drivers,
    }


@app.get("/player-chart/{player_name}", response_class=HTMLResponse)
async def player_chart(request: Request, player_name: str):
    """Show price model visualization for a player."""
    ctx = _context(request)
    chart = _chart_context(player_name)
    if chart is None:
        # The chart partial's own empty state, NOT explanation.html — that
        # rendered the entire Counterfactual panel, second id="explanation" and
        # all, into the chart slot. Reachable when a player leaves the pool
        # between the table rendering and the click (second tab, stale panel).
        ctx["chart_missing_name"] = player_name
    else:
        ctx.update(chart)
    return _render(request, "partials/player_chart.html", ctx)


@app.get("/team-view/{team_code}", response_class=HTMLResponse)
async def team_view(request: Request, team_code: str):
    """Open a team's roster in the team panel — the ONE place the view moves.

    An unknown code changes nothing and re-renders the team you were already
    looking at, so a stale link cannot move your view out from under you. When
    that is the default it renders BOT, which is what this used to do
    unconditionally.
    """
    global _viewed_team
    if team_code in auction_state.teams:
        _viewed_team = team_code
    return _render(request, "partials/team_view_response.html")


@app.get("/team-players/{team_code}")
async def team_players(team_code: str):
    """Return JSON list of players on a team (for trade dropdown).

    `all_players`, not `roster_players`: a minor-league player is tradeable —
    `execute_trade`, `remove_player` and `find_player` have always handled him —
    and for group 2/3 his salary is fully on cap, so leaving him out of the
    dropdown made a legal, cap-relevant trade impossible to even propose. Same
    reasoning and the same expression as the buyout panel's list.

    `is_minor` rides along because the label is built in JS here rather than by
    the `player_label` macro, and a dropdown that silently mixes the two would
    be worse than one that omits them: a trade reads differently when the player
    arrives on the active roster.
    """
    t = auction_state.teams.get(team_code)
    if t is None:
        return []
    return [
        {
            "name": p.name,
            "position": p.position,
            "salary": p.salary,
            "projected_points": p.projected_points,
            "is_minor": p.is_minor,
        }
        for p in t.all_players
    ]


@app.post("/toggle-bench", response_class=HTMLResponse)
async def toggle_bench(
    request: Request,
    team_code: str = Form(...),
    player_name: str = Form(...),
):
    """Toggle a player between active and bench."""
    t = auction_state.teams.get(team_code)
    if t is None:
        return _toast(
            _render(request, "partials/all_panels.html"),
            f"Unknown team: {team_code}", "error",
        )
    p = t.find_player(player_name)
    if p is None:
        return _toast(
            _render(request, "partials/all_panels.html"),
            f"{player_name} not found on {team_code}", "warning",
        )
    # _undoable, not a bare save_snapshot(): this endpoint can now REJECT (the
    # bench is capped at BENCH_SIZE), and save_snapshot() captures AND commits,
    # so snapshotting before the outcome is known spends a chain entry on a
    # request that changed nothing. rollback=False because set_bench validates
    # before it mutates.
    try:
        with _undoable(rollback=False):
            t.set_bench(player_name, not p.is_bench)
    except ValueError as e:
        return _toast(
            _render(request, "partials/all_panels.html"), str(e), "error",
        )
    _log_change(
        "toggle-bench",
        team_code,
        f"{player_name} → {'bench' if p.is_bench else 'active'}",
    )
    _recompute()
    _save_state()
    return _render(request, "partials/all_panels.html")


@app.post("/adjust-salary", response_class=HTMLResponse)
async def adjust_salary(
    request: Request,
    team_code: str = Form(...),
    player_name: str = Form(...),
    new_salary: float = Form(...),
):
    """Correct a player's salary (typo fix)."""
    t = auction_state.teams.get(team_code)
    if t is None:
        return _render(request, "partials/all_panels.html")
    # Validate before snapshotting: the input auto-submits on change, and a
    # player traded/bought out since render used to 500 here
    p = t.find_player(player_name)
    if p is None:
        return _toast(
            _render(request, "partials/all_panels.html"),
            f"{player_name} is no longer on {team_code}", "warning",
        )
    clamped = _legal_salary(new_salary)
    auction_state.save_snapshot()
    old_salary = p.salary
    t.adjust_salary(player_name, clamped)
    _log_change(
        "adjust-salary",
        team_code,
        f"{player_name}: ${old_salary:.1f}M → ${clamped:.1f}M",
    )
    _recompute()
    _save_state()
    response = _render(request, "partials/all_panels.html")
    # Two independent warnings can fire here, so collect rather than return on
    # the first: a fat-fingered figure is often both off-increment AND too big.
    notes: list[str] = []
    # Say so when the typed value wasn't recorded verbatim. This is the
    # typo-fix endpoint, so it sees the most fat-fingered input of any — and
    # silently storing something other than what was typed is the failure it
    # exists to correct. _log_change already formats to .1f, so before the
    # quantization the change log read "$2.5M" while the roster held 2.55.
    if clamped != new_salary:
        notes.append(
            f"{player_name} set to ${clamped}M (adjusted from ${new_salary:g}M)"
        )
    # _legal_salary clamps to MIN/MAX/increment but knows nothing about the cap,
    # so a typo'd correction could put a team over and still read as a success.
    over = _cap_overages(team_code)
    if over:
        # The cap note needs a subject. On its own it reads as a bare fact about
        # the team, with nothing tying it to the edit that just caused it.
        if not notes:
            notes.append(f"{player_name} set to ${clamped}M")
        notes.extend(over)
    if notes:
        return _toast(response, " — ".join(notes), "warning")
    return response


@app.post("/move-to-minors", response_class=HTMLResponse)
async def move_to_minors(
    request: Request,
    team_code: str = Form(...),
    player_name: str = Form(...),
):
    """Move a player from active roster to minors."""
    t = auction_state.teams.get(team_code)
    if t is None:
        return _render(request, "partials/all_panels.html")
    # rollback=False: send_to_minors validates before mutating. "Send down" sits
    # next to every roster row and refuses an unbenched player, so this is the
    # most-clicked rejection in the app — it must cost no undo depth.
    try:
        with _undoable(rollback=False):
            t.send_to_minors(player_name)
    except ValueError as e:
        return _toast(
            _render(request, "partials/all_panels.html"),
            str(e), "error",
        )
    _log_change("move-to-minors", team_code, f"{player_name} → minors")
    _recompute()
    _save_state()
    return _render(request, "partials/all_panels.html")


@app.post("/move-to-roster", response_class=HTMLResponse)
async def move_to_roster(
    request: Request,
    team_code: str = Form(...),
    player_name: str = Form(...),
):
    """Recall a player from minors to the active roster."""
    t = auction_state.teams.get(team_code)
    if t is None:
        return _render(request, "partials/all_panels.html")
    # rollback=False: recall_from_minors validates before mutating.
    try:
        with _undoable(rollback=False):
            t.recall_from_minors(player_name)
    except ValueError as e:
        # Surface the real reason: this used to hardcode "not in minors", which
        # is an actively wrong explanation for a roster-capacity refusal.
        return _toast(
            _render(request, "partials/all_panels.html"), str(e), "error",
        )
    _log_change("move-to-roster", team_code, f"{player_name} → active")
    _recompute()
    _save_state()
    response = _render(request, "partials/all_panels.html")
    # A group A-E minor is cap-free while down and cap-counted the moment it is
    # recalled (PlayerOnRoster.counts_on_cap), and 145 of the 149 minors at reset
    # are group A-E — so this is the ordinary recall, not an edge case. It went
    # unreported because the endpoint had no toast at all on success.
    over = _cap_overages(team_code)
    if over:
        return _toast(
            response, f"{player_name} recalled — {'; '.join(over)}", "warning",
        )
    # Still silent on a legal recall: the panel re-render already shows the move,
    # and the warning is the only new information here.
    return response


@app.post("/trade-between", response_class=HTMLResponse)
async def trade_between(
    request: Request,
    team_a: str = Form(...),
    team_b: str = Form(...),
    players_from_a: list[str] = Form([]),
    players_from_b: list[str] = Form([]),
):
    """Execute a trade between two teams. Atomic: all names must resolve.

    Repeated form values, one per ticked checkbox, since the form's two
    `<select multiple>`s became `.choice-list`s on 2026-08-15. That removed a
    comma-joined hidden field and the `updateTradeHidden` that wrote it — which
    was never a live bug (no name in the pool has a comma, and
    `_disambiguated_names` cannot add one), just a hand-rolled encoding where
    the form already had one.
    """
    names_a = [n.strip() for n in players_from_a if n.strip()]
    names_b = [n.strip() for n in players_from_b if n.strip()]
    if not names_a and not names_b:
        return _toast(
            _render(request, "partials/all_panels.html"),
            "No players selected for trade", "warning",
        )
    if team_a == team_b:
        return _toast(
            _render(request, "partials/all_panels.html"),
            "Cannot trade a team with itself", "error",
        )
    ta = auction_state.teams.get(team_a)
    tb = auction_state.teams.get(team_b)
    if not ta or not tb:
        return _toast(
            _render(request, "partials/all_panels.html"),
            f"Unknown team: {team_a if not ta else team_b}", "error",
        )
    # Validate every name BEFORE mutating — a typo or stale roster must not
    # produce a silent one-sided trade
    missing = [n for n in names_a if ta.find_player(n) is None]
    missing += [n for n in names_b if tb.find_player(n) is None]
    if missing:
        return _toast(
            _render(request, "partials/all_panels.html"),
            f"Trade aborted — not on roster: {', '.join(missing)}", "error",
        )
    auction_state.save_snapshot()
    now = datetime.now().isoformat()
    # Remove from both rosters before adding to either: add_acquired_player
    # routes to the minors at 24, so a full team that gains before it loses
    # would send the incoming player down on an even swap.
    out_of_a = [ta.remove_player(name) for name in names_a]
    out_of_b = [tb.remove_player(name) for name in names_b]

    demoted: list[str] = []
    # Carry the destination TEAM, not just its code — recovering the object by
    # comparing codes would be correct only because team_a == team_b is rejected
    # 40 lines up, and that is the duplicate-predicate trap this repo keeps
    # finding.
    for source, dest, target, p in (
        [(team_a, team_b, tb, p) for p in out_of_a]
        + [(team_b, team_a, ta, p) for p in out_of_b]
    ):
        p.is_minor = False
        p.is_bench = False
        # He was somebody else's keeper; on `target` he is a player they
        # acquired. `trade.execute_trade` gets this free by constructing a fresh
        # PlayerOnRoster — this path REUSES the roster object, so the flag has
        # to be reset explicitly or the two trade paths disagree. Left set, a
        # bench → minors → recall on the new team would file him under
        # `keeper_players`, which is the 2026-08-08 colouring bug pointing the
        # other way. It self-heals on the next reload (`_team_from_dict` derives
        # the flag from the list) and so would never reproduce after a restart.
        p.is_keeper = False
        if target.add_acquired_player(p):
            demoted.append(f"{p.name} → {dest} minors")
        _log_transaction(p.name, p.position, f"{source}→{dest}", p.salary, "trade",
                         timestamp=now, nhl_team=p.nhl_team)
    _recompute()
    _save_state()
    note = f" ({'; '.join(demoted)} — roster full)" if demoted else ""
    over = _cap_overages(team_a, team_b)
    if over:
        note += f" — {'; '.join(over)}"
    return _toast(
        # team_a, because this form only ever posts from team_a's own panel —
        # its hidden team_a field is that panel's code.
        _render(request, "partials/all_panels.html"),
        f"Trade executed: {team_a} ↔ {team_b}{note}",
        # A demotion alone stays a success: that note is informational and
        # pre-dates this. Only going over the cap lifts the tier.
        "warning" if over else "success",
    )


@app.get("/state")
async def get_state():
    """JSON state dump for debugging."""
    return json.loads(auction_state.to_json(include_snapshots=False))

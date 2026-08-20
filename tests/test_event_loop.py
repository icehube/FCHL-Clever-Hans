"""The two manual scans must solve OFF the event loop, and publish only if the
state they solved against is still on screen.

Every handler in `main.py` is `async def`, so FastAPI dispatches it to the event
loop rather than to a threadpool. That is correct for the mutating endpoints —
serialised writes are free — and it was a real defect for the two scans, which
are seconds of synchronous CBC. Measured 2026-08-19 before the fix, against the
live server: a warm `/bid-check` cost **10ms alone and 1682ms behind a roster
scan**, `/nominate` 1582ms, and `/state` — which solves nothing whatsoever —
1564ms. The draft-day shape is one click: Scan Roster, then type a price.

Three kinds of claim, because none of them covers the others:

* structural — the solving reaches a worker thread at all, and nothing else in
  the module solves on the loop;
* ordering — a cheap request really does overtake a scan, end to end;
* discard — a result computed against a roster that has since changed is thrown
  away instead of published.

There is deliberately **no wall-clock assertion**. CLAUDE.md's rule is that
timing assertions go flaky under load and the cause is a solve count anyway; the
ordering test below asks which response finished first, which holds by
construction once the solve is off the loop (a JSON dump against 15 MILPs).
"""

import ast
import re
import threading
import time
from pathlib import Path

import pytest

import main

# The handlers whose bodies are dominated by MILP solves. A third one joins this
# list or explains itself here, in the same commit — the failure mode is a new
# multi-solve endpoint quietly re-blocking the loop, which nothing else notices
# until a draft-day stall.
THREADED_SCANS = {
    "buyout_indicators_endpoint": "~15 solves, one per eligible player",
    "solve_standings": "up to 10 solves, one per live opponent",
}

# Pure solvers, by convention `_solve_*`. Calling one on the event loop is the
# bug this module exists to prevent.
SOLVER_PREFIX = "_solve_"

# The one place in main.py allowed to solve on the loop, with the reason. This is
# what closes the guard: `THREADED_SCANS` above is a hand-maintained list and
# cannot notice a THIRD multi-solve endpoint being written, so the check that
# matters runs the other way round — nothing may reach `solve_optimal_roster`
# except from here or from a `_solve_*` handed to a thread.
SOLVES_ON_THE_LOOP = {
    "_recompute": (
        "exactly one solve, for BOT, after a state change — ~78ms, and every "
        "panel in the response needs its result, so there is nothing to overlap "
        "it with"
    ),
}


def _handlers() -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every `@app.get`/`@app.post` handler in main.py, by function name."""
    tree = ast.parse((Path(__file__).resolve().parent.parent / "main.py").read_text())
    out = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(
            isinstance(d, ast.Call)
            and isinstance(d.func, ast.Attribute)
            and d.func.attr in ("get", "post")
            for d in node.decorator_list
        ):
            out[node.name] = node
    return out


class TestTheSolvingHappensInAThread:
    """Structural, over main.py's ast — the same idiom as
    `test_state.py::TestEveryMutatingPostTakesASnapshot`.

    What it can prove: that each scan hands a `_solve_*` function to
    `run_in_threadpool` and never calls one directly. What it cannot: that the
    thread is actually reached at runtime, or that the reads around it happen on
    the loop. The ordering test below is what covers the behaviour; this one
    catches the edit that quietly reverts the shape.
    """

    def test_both_scans_hand_their_solver_to_the_threadpool(self):
        handlers = _handlers()
        for name, why in THREADED_SCANS.items():
            assert name in handlers, f"{name} is no longer an endpoint — {why}"
            threaded = [
                call.args[0].id
                for call in ast.walk(handlers[name])
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "run_in_threadpool"
                and call.args
                and isinstance(call.args[0], ast.Name)
            ]
            assert any(s.startswith(SOLVER_PREFIX) for s in threaded), (
                f"{name} ({why}) does not pass a {SOLVER_PREFIX}* function to "
                f"run_in_threadpool, so its MILP loop runs on the event loop and "
                f"every other request queues behind it — measured 1682ms for a "
                f"10ms bid check"
            )

    def test_nothing_calls_a_solver_except_through_a_thread(self):
        """A `_solve_*` invoked anywhere is on the loop — and not only in a handler.

        Scoped to handler bodies at first, which was the same open shape as the
        list above: `_context` is called by every endpoint, so a `_solve_*` added
        THERE would have been on the loop for all 25 of them and passed. Walks the
        whole module now, and `run_in_threadpool(_solve_x, ...)` passes the
        function as a name rather than calling it, so the legitimate route does
        not register as a call.
        """
        tree = ast.parse((Path(__file__).resolve().parent.parent / "main.py").read_text())
        offenders = [
            f"{call.func.id}() called at main.py:{call.lineno}"
            for call in ast.walk(tree)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id.startswith(SOLVER_PREFIX)
        ]
        assert not offenders, (
            "a pure solver is being called on the event loop: "
            + "; ".join(offenders)
            + " — hand it to run_in_threadpool instead"
        )

    def test_only_recompute_may_solve_on_the_loop(self):
        """The closed half of this module, and the one that catches a NEW endpoint.

        `THREADED_SCANS` is maintained by hand, so on its own it says nothing
        about a third multi-solve endpoint — the failure it cannot see is exactly
        the one that happened: an endpoint written the obvious way, `async def`
        around a loop of solves, blocking every other request. This asks the
        question from the other side. Any new function that calls
        `solve_optimal_roster` has to be a `_solve_*` (which the test above forces
        into a thread) or name itself here with a reason.

        Same shape as `test_state.py::TestEveryMutatingPostTakesASnapshot`: a set
        equality over a list that is otherwise kept by memory.
        """
        tree = ast.parse((Path(__file__).resolve().parent.parent / "main.py").read_text())
        callers = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                isinstance(c, ast.Call)
                and isinstance(c.func, ast.Name)
                and c.func.id == "solve_optimal_roster"
                for c in ast.walk(node)
            )
        }
        threaded = {n for n in callers if n.startswith(SOLVER_PREFIX)}
        assert callers - threaded == set(SOLVES_ON_THE_LOOP), (
            f"main.py solves the MILP from {sorted(callers - threaded)}, but the "
            f"list of functions allowed to do that on the event loop is "
            f"{sorted(SOLVES_ON_THE_LOOP)}. A multi-solve endpoint written the "
            f"obvious way holds the loop for seconds — measured 1682ms for a 10ms "
            f"bid check. Make it a {SOLVER_PREFIX}* function and hand it to "
            f"run_in_threadpool, or add it here with the reason."
        )

    def test_every_state_change_bumps_the_version(self):
        """`_publish_if_current` is inert unless `_recompute` moves the counter.

        The discard test below proves the mechanism end to end; this proves the
        bump lives in the one function all twelve mutating endpoints call, rather
        than in whichever endpoint happened to be written first.
        """
        tree = ast.parse((Path(__file__).resolve().parent.parent / "main.py").read_text())
        recompute = next(
            n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "_recompute"
        )
        bumps = [
            n for n in ast.walk(recompute)
            if isinstance(n, ast.AugAssign)
            and isinstance(n.target, ast.Name)
            and n.target.id == "_state_version"
        ]
        assert bumps, (
            "_recompute() no longer bumps _state_version, so a scan can never "
            "notice that the state moved while it was solving and will publish "
            "figures for a roster that is gone"
        )


class TestACheapRequestOvertakesAScan:
    """End to end, on a real server: `/state` beats a scan that started first.

    Modelled on `test_counterfactual_cache.py::TestResponsesCannotOvertakeEachOther`,
    which asserts the OPPOSITE for `/explain` and is the reason this change was
    scoped to the two scans — that test documents the stale-mount hazard which
    moving `/explain` off the loop would reintroduce.

    Ordering, not duration. `/state` builds a JSON dump of the state and does not
    solve; the scan is ~15 MILPs. So once the solving is off the loop, the only
    way `/state` finishes last is if it was blocked.
    """

    def test_state_answers_while_a_roster_scan_is_still_solving(self, client, live_server):
        import httpx

        httpx.post(f"{live_server}/reset", timeout=60)

        sent: dict[str, float] = {}
        done: dict[str, float] = {}
        origin = time.perf_counter()

        def fire(tag: str, url: str, delay: float) -> None:
            time.sleep(delay)
            sent[tag] = time.perf_counter() - origin
            httpx.get(url, timeout=120)
            done[tag] = time.perf_counter() - origin

        threads = [
            threading.Thread(target=fire, args=("scan", f"{live_server}/buyout-indicators", 0.0)),
            threading.Thread(target=fire, args=("state", f"{live_server}/state", 0.05)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Precondition, not the finding — if scheduling sent /state first the
        # ordering assertion below would be meaningless.
        assert sent["scan"] < sent["state"], (
            f"the scan was not sent first (scan {sent['scan'] * 1000:.0f}ms, state "
            f"{sent['state'] * 1000:.0f}ms) — scheduling noise, not a finding; re-run"
        )
        assert done["state"] < done["scan"], (
            f"/state finished AFTER a roster scan that started 50ms earlier "
            f"(state {done['state'] * 1000:.0f}ms vs scan {done['scan'] * 1000:.0f}ms), "
            f"so the scan's MILP loop is holding the event loop again. Every "
            f"request queues behind it, including the /bid-check that fires when "
            f"the operator types a price — measured 1682ms against 10ms warm."
        )


class TestAScanDiscardsWhatTheStateOutran:
    """A pick landing mid-scan must beat the scan, not the other way round.

    This is the cost of threading the solve, and the whole reason
    `_publish_if_current` exists: the loop can now run an `/assign` while a scan
    is in flight, so the result can describe a roster that no longer exists. Both
    dicts are rendered as authoritative — the Proj column carries a rank badge,
    the dots carry a verdict — so publishing a stale one is worse than publishing
    nothing.

    The stub calls the real `_recompute()` rather than poking the counter, so
    these two also cover the link between them: monkeypatch the bump away and
    they fail.
    """

    def test_the_dots_send_nothing_rather_than_a_page_of_keeps(self, client, monkeypatch):
        def solve_then_a_pick_lands(state, prices, current_points):
            main._recompute()          # what an /assign on the loop would do
            return {"Nobody At All": "buyout"}

        monkeypatch.setattr(main, "_solve_buyout_indicators", solve_then_a_pick_lands)
        before = dict(main.buyout_indicators)
        r = client.get("/buyout-indicators")

        assert r.status_code == 200
        assert dict(main.buyout_indicators) == before, (
            f"a scan solved against a state that then changed published anyway: "
            f"{main.buyout_indicators}"
        )
        assert "buyout-light" not in r.text, (
            "the response still carries dot spans — buyout_dots.html defaults a "
            "missing verdict to 'keep', so every eligible player would go green "
            "and read as 'no buyout helps'"
        )
        assert "showToast" in r.headers.get("HX-Trigger", ""), (
            "nothing on screen would say why the dots did not change"
        )

    def test_the_proj_column_falls_back_to_its_estimate(self, client, monkeypatch):
        def solve_then_a_pick_lands(state, prices):
            main._recompute()
            return {code: 9999 for code in state.teams}

        monkeypatch.setattr(main, "_solve_exact_projections", solve_then_a_pick_lands)
        r = client.get("/solve-standings")

        assert r.status_code == 200
        assert main.exact_projections == {}, (
            f"exact figures for a superseded state were published: "
            f"{main.exact_projections}"
        )
        assert "9999" not in r.text, "a fabricated figure reached the Proj column"
        assert "estimated" in r.text, (
            "the basis marker does not say the column is back on estimates, so "
            "the figures on screen look exact and are not"
        )


class TestTwoSolvesAtOnceAgreeWithTwoSolvesInARow:
    """CBC being safe to call from two threads at once is now load-bearing.

    Nothing before this change could run two solves concurrently: every handler
    was `async def`, so the loop serialised them. Now the loop can answer an
    `/assign` — which calls `_recompute()`, which solves for BOT — while a scan
    is mid-flight in a worker thread. If PuLP's temp files collided, the symptom
    would be an exception (visible, and the broad `except` per team would hide it
    behind an estimate) or, far worse, a WRONG objective from a half-written
    model file: a Proj column or a buyout dot that is authoritative and quietly
    incorrect.

    Verified 2026-08-19 rather than assumed. PuLP names its scratch files
    `uuid4().hex` per solve when `keepFiles` is false (`pulp/apis/core.py`,
    `LpSolver.create_tmp_files`), so two solvers cannot pick the same path, and
    CBC is a subprocess — the GIL is released across it. 11 concurrent solves ×
    3 rounds: zero errors, zero disagreements with the serial answers, 401ms
    against 1134ms serial.

    This test is the regression net for a PuLP upgrade that changes that naming
    — `keepFiles=True` alone would put every solve on the same filename, since
    that switch makes the prefix the model NAME instead. It asserts the ANSWERS,
    not the timing, and the reason is measured: adding `keepFiles=True` to
    `optimizer.py`'s one `PULP_CBC_CMD` does **not** raise. SRL came back
    `status='Optimal'` with **950** points against **1355** solved alone — a
    figure that would have rendered as a rank badge with nothing wrong on screen.
    A collision that somehow produced identical answers would be harmless; one
    that produces different answers is what this has to fail on.
    """

    def test_three_teams_solved_in_parallel_give_the_serial_answers(self, client):
        from optimizer import solve_optimal_roster

        state = main.auction_state
        prices = main.market_prices
        # Three live opponents, whichever they are — same rule as the scan.
        codes = [
            c for c, t in state.teams.items()
            if not t.is_done and c != main.MY_TEAM and t.total_spots_remaining > 0
        ][:3]
        assert len(codes) == 3, f"need three solvable opponents, got {codes}"

        def solve(code: str):
            return solve_optimal_roster(
                state.teams[code], state.available_players, prices
            )

        serial = {c: solve(c) for c in codes}

        results: dict[str, object] = {}
        errors: list[str] = []
        start = threading.Barrier(len(codes))

        def race(code: str) -> None:
            try:
                start.wait(timeout=30)   # overlap the solves, don't just queue them
                results[code] = solve(code)
            except Exception as e:                      # noqa: BLE001 - reported below
                errors.append(f"{code}: {type(e).__name__}: {e}")

        threads = [threading.Thread(target=race, args=(c,)) for c in codes]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert not errors, (
            "solving from several threads at once raised — CBC or PuLP is no "
            f"longer safe to call concurrently: {errors}. The scans hand their "
            "MILP loop to a worker thread while the event loop keeps solving for "
            "BOT on every pick, so this has to hold."
        )
        for code in codes:
            assert results[code].status == serial[code].status, (
                f"{code} solved to {results[code].status} in parallel and "
                f"{serial[code].status} serially"
            )
            assert results[code].total_points == serial[code].total_points, (
                f"{code}'s optimum changed when solved alongside others: "
                f"{results[code].total_points} parallel vs "
                f"{serial[code].total_points} serial — a concurrent solve is "
                f"reading another one's model or solution file"
            )


class TestAScanInParallelAgreesWithItselfInSeries:
    """The scans fan their solves out; the answers must not depend on that.

    Both scans solved one team (or one hypothetical) at a time until 2026-08-19b
    put them in a worker thread, and one at a time inside it until this. The
    speedup is real — measured on a fresh league, `/solve-standings` **1294ms ->
    384ms** and Scan Roster **1630ms -> 454ms**, and in the endgame scenario
    **2174ms -> 569ms** for the roster scan, which is its worst case rather than
    its best (a late-draft BOT owns 23 eligible contracts against 15 fresh).

    So the risk is not speed, it is a WRONG answer arriving faster. Both dicts
    are rendered as authoritative — the Proj column carries a rank badge, the
    dots carry a verdict — so these compare the parallel result against the same
    per-item solvers called one at a time, which is what the loops used to do.

    **Key order is asserted, not incidental.** `pool.map` yields in input order,
    which is the only reason the result is deterministic; a refactor to
    `as_completed` would still pass a values-only comparison while making the
    dict's order depend on which CBC subprocess finished first. It costs ~4s of
    suite time to run each solve twice, which is worth it here in a way that the
    29 repeats of an ast walk removed from the names guard were not: this is the
    correctness proof for the change.
    """

    def test_the_proj_column_is_the_same_solved_ten_at_once(self, client):
        state, prices = main.auction_state, main.market_prices
        codes = [
            code for code, t in state.teams.items()
            if not t.is_done and code != main.MY_TEAM
        ]
        assert len(codes) > 1, f"need several live opponents to fan out, got {codes}"

        parallel = main._solve_exact_projections(state, prices)
        serial = {}
        for code in codes:
            _, points = main._solve_one_opponent(state, prices, code)
            if points is not None:
                serial[code] = points

        assert list(parallel.items()) == list(serial.items()), (
            f"solving the opponents concurrently changed the Proj column:\n"
            f"  parallel {parallel}\n  serial   {serial}\n"
            f"Every one of these renders as an exact figure and BOT's carries a "
            f"rank badge."
        )

    def test_the_dots_are_the_same_solved_many_at_once(self, client):
        state, prices = main.auction_state, main.market_prices
        current = (
            main.milp_solution.total_points
            if main.milp_solution and main.milp_solution.status == "Optimal" else 0
        )
        candidates = [
            p for p in state.teams[main.MY_TEAM].all_players if p.can_be_bought_out
        ]
        assert len(candidates) > 1, (
            f"need several eligible players to fan out, got {len(candidates)}"
        )

        parallel = main._solve_buyout_indicators(state, prices, current)
        serial = dict(
            main._solve_one_buyout(state, prices, current, p) for p in candidates
        )

        assert list(parallel.items()) == list(serial.items()), (
            f"solving the buyout hypotheticals concurrently changed the "
            f"verdicts:\n  parallel {parallel}\n  serial   {serial}\n"
            f"A wrong 'keep' here is the 2026-08-07 'no buyout helps' failure."
        )

    def test_the_worker_budget_is_capped_below_the_core_count(self):
        """A count, not a thread pool the machine picks.

        Each concurrent solve is a CBC **subprocess**, so this is a core budget:
        uncapped on the 20-core dev box it would put 15 processes on a draft-day
        laptop with 4. The floor matters as much — `os.cpu_count()` returns None
        on some platforms, and `max_workers=0` raises.
        """
        assert 1 <= main.SCAN_WORKERS <= 8, (
            f"SCAN_WORKERS is {main.SCAN_WORKERS}; it has to stay a small cap "
            f"rather than this machine's core count, because the draft runs on a "
            f"laptop and each solve is a CBC subprocess"
        )


class TestAScanWithNothingToSolve:
    """An empty work list must answer, not raise.

    Both scans return early on one, and that guard is not cosmetic:
    `ThreadPoolExecutor(max_workers=0)` raises `ValueError: max_workers must be
    greater than 0`, so without it the endpoint 500s. Neither branch was
    exercised when the parallel fan-out landed — the existing standings coverage
    marks at most ONE team done — which is how a guard preventing a live 500
    shipped untested.

    Both states are legal rather than contrived. **Every opponent done** is the
    end of a real draft: the CBA lets teams stop before filling 24, CLAUDE.md
    records that 3+ do every draft, and `test_auction_draft.py` already walks all
    ten to done. **BOT with no eligible contracts** is rarer but is the same
    branch, and the Analyzer offers nothing in that state either — the scan has
    to agree with it rather than fall over.
    """

    def test_standings_answers_when_every_opponent_is_done(self, client):
        for code in [c for c in main.auction_state.teams if c != main.MY_TEAM]:
            client.post("/team-done", data={"team_code": code})
        assert not [
            c for c, t in main.auction_state.teams.items()
            if not t.is_done and c != main.MY_TEAM
        ], "the fixture failed to retire every opponent"

        r = client.get("/solve-standings")

        assert r.status_code == 200, (
            f"/solve-standings raised with no opponent left to solve — the "
            f"empty-list guard is what stops ThreadPoolExecutor(max_workers=0). "
            f"This is the end of a real draft, not an edge case."
        )
        assert main.exact_projections == {}, (
            f"a done team's roster is final, so the scan must not invent figures "
            f"for one: {main.exact_projections}"
        )
        # And the marker reads a plain "exact", which looks wrong for a scan that
        # solved nothing and is not: with every opponent done there is nothing
        # left to guess, because a done team's figure is its FINAL and BOT's is
        # its own MILP optimum. `standings_basis.html` branches on the count of
        # ESTIMATES for exactly this state (measured 2026-08-18) — asserting
        # "estimated" here is the mistake its comment predicts, and this test
        # made it before making it this way.
        marker = re.search(r'id="proj-basis"[^>]*>(.*?)</span>', r.text, re.S)
        assert marker and marker.group(1).strip() == "exact", (
            f"nothing on screen is an estimate — every opponent is done, so their "
            f"figures are finals and BOT's is its own optimum — but the marker "
            f"reads {marker.group(1).strip()!r} if marker else '(absent)'"
        )

    def test_the_roster_scan_answers_when_nothing_is_eligible(self, client):
        team = main.auction_state.teams[main.MY_TEAM]
        # Contract group is the whole rule (`can_be_bought_out`), so moving every
        # player out of the eligible groups is the only way to empty the list.
        for p in team.all_players:
            p.group = "RFA1"
        team._invalidate_cache()
        assert not [p for p in team.all_players if p.can_be_bought_out], (
            "the fixture failed to make BOT's roster ineligible"
        )

        r = client.get("/buyout-indicators")

        assert r.status_code == 200, (
            "/buyout-indicators raised with nothing eligible to solve — same "
            "empty-list guard as the standings scan"
        )
        assert "buyout-light" not in r.text, (
            "no player is eligible, so there is no dot to paint — and a dot here "
            "would be a verdict on a decision that is not available"
        )

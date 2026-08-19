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

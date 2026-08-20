"""Measure the auction grid's real width chain. NOT a test — pytest ignores it.

Named `measure_layout.py` rather than `test_layout.py` on purpose: this is an
instrument, not an assertion. It answers "which element is forcing the column
wide, and by how much" so a layout fix targets the right thing. The 2026-08-08
diagnosis of the off-screen team panel named `#bid-limits` as the forcer and
proposed a fix for it; running this is what showed the forcer is somewhere else.

State safety: `main.py` hardcodes `STATE_DIR = "data/state"` with no env
override, so importing and serving the app writes the OPERATOR'S state. This
redirects `main.STATE_DIR` to a temp dir before the app is imported anywhere
else, exactly as `tests/conftest.py::isolated_state_dir` does — which is what
makes the `POST /reset` below safe. Never remove that redirect to "just point it
at the running dev server": a measurement run must not be able to touch a draft.

Usage:
    .venv/bin/python -m tests.measure_layout                    # default widths
    .venv/bin/python -m tests.measure_layout --widths 1280
    .venv/bin/python -m tests.measure_layout --whatif           # + reproduce the bug
    .venv/bin/python -m tests.measure_layout --selftest         # + prove the MISS kinds
"""

import argparse
import json
import socket
import sys
import tempfile
import threading
import time

# Redirect state BEFORE the app can load or save anything real.
import main

main.STATE_DIR = tempfile.mkdtemp(prefix="measure-layout-state-")

from playwright.sync_api import sync_playwright  # noqa: E402

# Everything whose width could plausibly force a column. Order matters only for
# reading the report; the attribution pass sorts by min-content.
#
# DESCENDANT selectors for the tables, never `>`. This list carried
# `#league-state > table` until 2026-08-13, and the .table-scroll-x wrapper added
# by the very fix this instrument was written to investigate broke that match —
# so the report printed "(absent)" for the 955px table that caused the bug, and
# min_contents() dropped it silently. A wrapper is exactly the kind of thing a
# layout fix adds, so a child combinator here is a selector designed to rot.
TARGETS = [
    ".auction-grid",
    ".area-auction",
    ".area-players",
    ".area-team",
    "#bid-limits",
    "#bid-limits .flex.items-center.gap-4",
    "#bid-limits .scroll-container",
    "#bid-limits .scroll-container table",
    "#league-state",
    "#league-state .table-scroll-x",
    "#league-state table",
    "#team-panel",
    "#team-panel .team-stats",
    "#team-panel .table-scroll-x",
    "#team-panel table",
    "#bid-panel",
    # The Auction tab specifically, not "#logs-panel table": the panel holds
    # three, only one is displayed, and querySelector would take whichever came
    # first in source order — a display:none table measures 0, which reads as a
    # real number rather than as "not the one you meant".
    #
    # Expect `(no match)` on a default run and do NOT go hunting for a rotted
    # selector: main_measure() POSTs /reset first, and the Auction tab renders no
    # table at all with an empty transaction log. Measured 2026-08-20 — the
    # selector matches, rendered, from the first pick onward. This is the one
    # TARGET whose miss is about the STATE rather than the DOM, which is worth
    # knowing precisely because the three-way report below now says "(no match)"
    # where it used to say "(absent)" for everything.
    "#logs-panel div[role=tabpanel]:first-of-type table",
]

# One target of each MISS kind, appended by --selftest. Deliberately not
# fixtures inside a test: like --whatif, the point is to inject the failure into
# a REAL run and read the real output. All three reporters have to be exercised —
# report(), min_contents() AND attribution() — because each collapsed the three
# differently, and a branch nobody has watched print is the thing this file's own
# history is about.
SELFTEST = [
    "#bid-limits ::",     # invalid CSS         -> (BAD SELECTOR)
    "#no-such-element",   # a rotted target     -> (no match)
    "#player-list",       # a <datalist>        -> (not rendered)
]

# Three ways a target can fail to produce a number, and they mean different
# things. `(absent)` used to cover all three, so the 2026-08-11 wrapper that
# broke `#league-state > table` was indistinguishable from a panel that
# legitimately does not render — and the instrument existed to find exactly the
# 955px element that selector had stopped matching.
MISS = {
    "bad-selector": "(BAD SELECTOR)",   # invalid CSS — querySelector throws
    "no-match":     "(no match)",       # valid, matches nothing: a rotted TARGET
    "not-rendered": "(not rendered)",   # matched, but display:none — 0 is not a width
}

PROBE = """
(selectors) => {
  const out = {};
  for (const sel of selectors) {
    let el;
    // querySelector THROWS on a syntactically invalid selector, which used to
    // abandon the whole probe and report nothing about any target.
    try { el = document.querySelector(sel); }
    catch (e) { out[sel] = {miss: 'bad-selector'}; continue; }
    if (!el) { out[sel] = {miss: 'no-match'}; continue; }
    // getClientRects() is empty for display:none and non-empty for a rendered
    // box of any size — the distinction a zero from getBoundingClientRect()
    // cannot make, and the reason a hidden #logs-panel table read as 0px wide.
    if (el.getClientRects().length === 0) { out[sel] = {miss: 'not-rendered'}; continue; }
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    out[sel] = {
      left: r.left, right: r.right, width: r.width,
      clientWidth: el.clientWidth, scrollWidth: el.scrollWidth,
      overflowX: cs.overflowX, overflowY: cs.overflowY,
    };
  }
  const g = document.querySelector('.auction-grid');
  out.__meta = {
    // A grid container's computed grid-template-columns is the USED track
    // sizes in px, so this is the whole track argument in one read.
    tracks: getComputedStyle(g).gridTemplateColumns,
    gridClientLeft: g.getBoundingClientRect().left + g.clientLeft,
    gridClientWidth: g.clientWidth,
    gridScrollWidth: g.scrollWidth,
    pageScrollWidth: document.scrollingElement.scrollWidth,
    innerWidth: window.innerWidth,
  };
  return out;
}
"""

# One element at a time, restored immediately: a specified `width` beats
# justify-self/align-self stretch, so this reads the box's own min-content in
# situ (inheritance and percentages intact). Sizing one item reflows the
# tracks, so never hold two at min-content at once.
MIN_CONTENT = """
(sel) => {
  let el;
  try { el = document.querySelector(sel); }
  catch (e) { return {miss: 'bad-selector'}; }
  if (!el) return {miss: 'no-match'};
  if (el.getClientRects().length === 0) return {miss: 'not-rendered'};
  const w0 = el.style.width, m0 = el.style.minWidth;
  el.style.minWidth = '0';
  el.style.width = 'min-content';
  const w = el.getBoundingClientRect().width;
  el.style.width = w0;
  el.style.minWidth = m0;
  return {width: w};
}
"""

# For each grid area, the deepest descendant whose min-content is within 2px of
# the area's — i.e. the element actually setting the floor.
ATTRIBUTE = """
(areaSel) => {
  let area;
  try { area = document.querySelector(areaSel); }
  catch (e) { return {miss: 'bad-selector'}; }
  if (!area) return {miss: 'no-match'};
  if (area.getClientRects().length === 0) return {miss: 'not-rendered'};
  const mc = el => {
    const w0 = el.style.width, m0 = el.style.minWidth;
    el.style.minWidth = '0'; el.style.width = 'min-content';
    const w = el.getBoundingClientRect().width;
    el.style.width = w0; el.style.minWidth = m0;
    return w;
  };
  const areaMin = mc(area);
  const rows = [];
  for (const el of area.querySelectorAll('*')) {
    if (el.offsetParent === null && el.tagName !== 'TABLE') continue;
    const w = mc(el);
    if (w >= areaMin - 2) {
      const id = el.id ? '#' + el.id : '';
      const cls = (el.className || '').toString().split(/\\s+/).slice(0, 2).join('.');
      rows.push({tag: el.tagName.toLowerCase(), id, cls, min: Math.round(w),
                 depth: (() => { let d = 0, p = el; while (p !== area) { d++; p = p.parentElement; } return d; })()});
    }
  }
  rows.sort((a, b) => b.depth - a.depth);
  return {areaMin: Math.round(areaMin), forcers: rows.slice(0, 6)};
}
"""


def start_server() -> str:
    import uvicorn

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(main.app, log_level="error"))
    threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True).start()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return f"http://127.0.0.1:{port}"
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("server never came up")


def report(page, width: int, label: str) -> None:
    data = page.evaluate(PROBE, TARGETS)
    m = data.pop("__meta")
    print(f"\n{'=' * 78}\n{label} @ {width}px\n{'=' * 78}")
    print(f"  tracks (used px)   : {m['tracks']}")
    print(f"  grid client / scroll: {m['gridClientWidth']} / {m['gridScrollWidth']}"
          f"   overflow = {m['gridScrollWidth'] - m['gridClientWidth']:+d}")
    print(f"  page scrollWidth    : {m['pageScrollWidth']}  (innerWidth {m['innerWidth']})")
    print(f"\n  {'element':<44} {'left':>6} {'right':>7} {'client':>7} {'scroll':>7}  ovf-x")
    for sel, v in data.items():
        if "miss" in v:
            print(f"  {sel:<44}   {MISS[v['miss']]}")
            continue
        flag = "  <-- OVERFLOWS" if v["scrollWidth"] > v["clientWidth"] + 1 else ""
        print(f"  {sel:<44} {v['left']:>6.0f} {v['right']:>7.0f} "
              f"{v['clientWidth']:>7} {v['scrollWidth']:>7}  {v['overflowX']:<8}{flag}")


def min_contents(page) -> None:
    """A line per target, always. A silently skipped one is the number a layout
    investigation turns on — see MISS."""
    print(f"\n  {'element':<44} {'min-content':>11}")
    for sel in TARGETS:
        res = page.evaluate(MIN_CONTENT, sel)
        if "miss" in res:
            print(f"  {sel:<44} {MISS[res['miss']]:>11}")
        else:
            print(f"  {sel:<44} {res['width']:>11.0f}")


AREAS = (".area-auction", ".area-players", ".area-team")


def attribution(page, areas=AREAS) -> None:
    print("\n  ATTRIBUTION — deepest descendant setting each area's floor")
    for area in areas:
        res = page.evaluate(ATTRIBUTE, area)
        if "miss" in res:
            print(f"    {area}  {MISS[res['miss']]}")
            continue
        print(f"    {area}  min-content = {res['areaMin']}")
        for f in res["forcers"]:
            print(f"        depth {f['depth']:>2}  {f['tag']}{f['id']}"
                  f"{('.' + f['cls']) if f['cls'] else ''}  -> {f['min']}")


def main_measure() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--widths", default="1024,1280,1600,1920")
    ap.add_argument("--whatif", action="store_true",
                    help="also re-measure with the bare `1fr` bug injected")
    ap.add_argument("--selftest", action="store_true",
                    help="append one target of each MISS kind, to prove the "
                         "three are told apart rather than collapsed")
    args = ap.parse_args()
    if args.selftest:
        # Deliberately not fixtures inside a test: like --whatif, the point is to
        # inject the failure into a REAL run and read the real output.
        TARGETS.extend(SELFTEST)
    widths = [int(w) for w in args.widths.split(",")]

    url = start_server()
    print(f"state dir (temp): {main.STATE_DIR}")

    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        for width in widths:
            ctx = b.new_context(viewport={"width": width, "height": 900})
            page = ctx.new_page()
            page.request.post(f"{url}/reset")   # safe: STATE_DIR is a temp dir
            page.goto(url)
            page.wait_for_selector("#team-panel")

            report(page, width, "BASELINE")
            if width == 1280:
                min_contents(page)
                attribution(page, AREAS + tuple(SELFTEST) if args.selftest else AREAS)
                sc = page.evaluate(
                    "() => getComputedStyle(document.querySelector("
                    "'#bid-limits .scroll-container')).overflowX")
                print(f"\n  DOUBT 1 — .scroll-container computed overflow-x = {sc!r}"
                      f"  ({'no-op confirmed' if sc == 'auto' else 'declaring it WOULD change something'})")

            if args.whatif:
                # Injects the BUG, not the fix. Until 2026-08-13 this injected
                # minmax(0,1fr) — which shipped on 2026-08-11, so it re-measured
                # the baseline and labelled it a hypothesis. Reverting to a bare
                # `1fr` is the useful direction now: it reproduces the failure on
                # demand, which is what you want when checking whether a new wide
                # element would have been caught.
                page.add_style_tag(content="""
                    .auction-grid { grid-template-columns: repeat(3, 1fr) !important; }
                    @media (max-width: 1023px) { .auction-grid { grid-template-columns: 1fr !important; } }
                """)
                page.wait_for_timeout(150)
                report(page, width, "WHAT-IF bare 1fr (the 2026-08-11 bug)")
            ctx.close()
        b.close()


if __name__ == "__main__":
    main_measure()

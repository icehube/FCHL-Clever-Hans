# Changelog

Everything that has been fixed, added or changed in the FCHL Auction Manager,
newest first. Open work — anything still to do — lives in [BACKLOG.md](BACKLOG.md).

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
There are no version numbers: this project ships straight to `main` and has
never cut a release, so entries are grouped by the **date the work landed**.
A `## [1.2.0]` here would be fiction.

Entries are long on purpose. Each one records what was actually wrong, what was
measured, and — often — **what the original bug report got wrong**, because
several findings here sat deferred for weeks on a diagnosis that turned out to
be incorrect. That is the part worth keeping.

`### Investigated` is not a Keep a Changelog section. It holds work that closed
**without a code change** — a reported bug that reproduced as correct
behaviour, or a race that turned out to be unreachable. Filing those under
*Fixed* would misrepresent them, and deleting them would invite someone to
rediscover the same non-problem.


## [2026-09-12]

### Added

- **Any team's buyout can now be recorded, from that team's panel.** CBA 11.4
  lets any team buy anyone out, and this tool is the league's record of all
  eleven rosters — but `trade.execute_buyout` read `state.teams[MY_TEAM]`, so a
  rival's buyout could not be entered at any layer. An unrecorded one leaves
  that team's cap wrong in *everything* the app computes about them, the market
  ceiling included, which is the bid advisor's own input.

  `execute_buyout(state, player_name, team_code=MY_TEAM)` looks the team up
  first, runs `find_player` against **that** roster, and puts the penalty there.
  `POST /buyout` takes a `team_code` form field, logs the transaction against
  it, and points the view at it — the 2026-08-08 "the view follows the roster
  that changed" policy. `/undo` needed no change at all: it already mirrored the
  view off `t.team_code`, which was simply always `MY_TEAM` before.

  **A second, smaller gap closed with it.** `buyout_panel.html` renders Execute
  Buyout only when the recommendation is `buyout`, so even for BOT there was no
  way to execute one the MILP rates `keep` — legal, and sometimes right for cap
  reasons the MILP does not model. The new picker renders for BOT too.

  **The two controls are advice and record, and the split is deliberate.**
  `evaluate_buyout` did NOT follow `execute_buyout` in taking a team: it scores
  the hypothetical against BOT's MILP total, so pointed at SRL's roster it would
  answer a question about the wrong team. Same reason the scan and the dots stay
  BOT-only.

  The picker is a `<select>` rather than a button per row, for the reason the
  Analyzer became one on 2026-08-15: eligible counts run 4–15 per team on a
  fresh pool and grow with every pick (everyone drafted is group 3), so by the
  endgame most rows would carry a button — and a `<select>` is sized by its
  column rather than its content, so it cannot widen the panel. No confirm
  dialog, per the owner decision the same day: the buyout is a logged
  transaction, it shows in the Logs panel and the header search, and `Ctrl+Z`
  reverts it. Its field is `player`, matching `/buyout` and deliberately not
  `player_name`, which is `/buyout-check`'s query param on the Analyzer's
  picker — two pickers sharing a name is a reader that cannot tell them apart.

  **The guard that makes the team argument mean something** is that
  `find_player` runs against the named team only. A lookup that searched the
  league would take the right player off the wrong cap and toast success: two
  teams corrupted, nothing on screen to say so. Pinned from both directions —
  `test_a_player_on_another_roster_is_refused` at the engine and at the
  endpoint. The set-equality test for the picker is asserted on an **opponent**
  on purpose: a picker reading `team` instead of `viewed_team` renders BOT's
  candidates under SRL's heading, which is the 2026-08-05 panel leak in a new
  control, and on BOT's own panel the two expressions agree so it would look
  perfect. Eight mutants run, eight died — including penalty-on-BOT,
  log-against-BOT, view-goes-home, picker-reads-`team`, and the pre-selected
  first option, which here is a one-click buyout of whoever sorts first rather
  than the Analyzer's silent no-op.

  Six comments asserting BOT-only moved with the code (`_view_team`'s docstring
  and call site, `/undo`'s allowlist note, `_search_rows`' display/navigation
  split, `state.py`'s `_searchable` docstring, CLAUDE.md's view-policy and
  buyout bullets). The toast now names the team and the dead cap: "Bought out X"
  was unambiguous only while one roster could be touched, and with no confirm
  dialog it is the only confirmation there is.

- **A Recompute button on the bid panel's counterfactual, so the card can be
  re-solved at the price actually on the table.** Filed 2026-08-06 as an owner
  finding and deferred "pending draft-day experience"; the owner asked for the
  button on 2026-09-12, which is the trigger the entry named.

  The card auto-loads at `_cf_price` — the market price, a *forecast* of the
  clearing price — and that is what makes it cacheable per state epoch. It is
  also what makes it go quietly out of date: bidding climbs, the forecast does
  not move, and the verdict keeps answering a question about a price nobody is
  offering any more. Re-solving it automatically is the one thing that must not
  happen. Two MILP solves is ~200ms, and `bid_panel.html`'s own comment
  records why that number is dangerous on this panel: a response landing
  between mousedown and mouseup reflows the Assign button out from under the
  pointer.

  So `GET /explain/{player}` takes an optional `price=`, and the button asks
  for it. Five things are load-bearing and each carries its comment:

  - **The price goes through `_legal_salary`**, the same clamp-and-quantize the
    typed salary fields use. The bid box auto-submits whatever was typed, so
    the button can be handed a fat-fingered `46` or a `2.5476`; a verdict
    reading "Skip him at $46.0M" would be conditioned on a price the CBA has no
    room for. That quantization also keeps the cache key dense.
  - **`_counterfactual_cache` is keyed on `(name, price)`**, not on the name. A
    name-only key hands the second price the first price's answer while the
    card quotes the second — a confident wrong number on the bidding path.
    `_recompute()` still clears the whole dict, so the epoch semantics are
    unchanged; the bound widens from "players bid on since the last sale" to
    "players × deliberate clicks", which is still tiny because every entry
    costs a click and two solves.
  - **The button exists only in the bid panel's mount** (`cf_inline`). The
    standalone `#explanation` mount is opened from the players table for an
    arbitrary player, where `#bid-price` holds the price of somebody else
    entirely. An undefined `cf_inline` is falsy in Jinja, so the fail-safe
    direction is "no button" rather than one reading the wrong input.
  - **`hx-target="closest .counterfactual-card"`, never `#bid-counterfactual`.**
    The body is mounted twice and carries no id of its own — the rule the close
    button already follows — and targeting the card means a response with no
    card removes the card and leaves the mount standing. An `outerHTML` swap
    into the mount consumes it, which is the `#bid-advice` failure: a target
    that vanishes with its contents can be swapped exactly once, and nothing on
    screen says so.
  - **The price is read out of `#bid-price` when the request is built**, not
    captured at render time. `change` on a number input fires on blur, so
    pressing Recompute right after typing a price starts a `/bid-check`
    re-render; a value snapshotted at the last render would re-solve at the
    PREVIOUS price and quote it confidently.

  The card also carries a one-word basis marker — `at market` / `at your bid` —
  with a native `title`, never a DaisyUI `data-tip`: this body is mounted inside
  panels that scroll, and a bubble in a horizontal scroller is clipped with no
  flip logic to save it. Same job as `#proj-basis` on the Proj column, for the
  same reason: the verdict names the dollars either way, so without the marker
  the two cards are indistinguishable at a glance, and mid-auction the thing
  worth knowing is whether you are reading a forecast or the table.

  **What it deliberately does not do.** It does not re-sharpen on its own, and
  the next whole-panel swap (a bidder toggle) reloads the card at the market
  price. That is honest rather than stale, because the verdict sentence names
  the price it was solved at and the marker names the basis — the operator can
  always see which question was answered.

  Seven endpoint tests in `tests/test_counterfactual_cache.py`
  (`TestRecomputingAtTheLiveBid`) and one browser test
  (`TestRecomputeUsesThePriceOnTheTable`). Nine mutants were run and all nine
  died, including the two that matter most: quoting the asked price while
  solving at the market one — caught by `test_a_higher_price_can_only_be_worse`,
  which asserts the property rather than the string, since the figure on screen
  comes from `cf_price` and not from the solution — and swapping the mount
  instead of the card, which only the browser can see. One mutant did not apply
  on its first run (`{% if cf_inline %}` matches two sites now, the gate and the
  marker's tooltip) and was re-run with a unique anchor; a mutant that applies
  to nothing prints green.

### Changed

- **`TestExplain::test_both_mounts_carry_a_close_button` now bans
  `getElementById` in the click handlers rather than anywhere in the body.** The
  blanket ban was correct when the card's only script was its close button;
  the Recompute button reads `#bid-price` by id when it builds its request, so
  the guard as written would have had to be deleted to let the feature through
  — taking the close-by-id check with it. Verified by mutation that the
  narrowed version still catches a close button resolving by id.

### Investigated

- **The draft-day backlog items were put to the owner instead of waiting for
  the draft.** Seven entries were parked on "revisit if it actually bites on
  the day" — a deferral reason that expires only during a live auction, which
  is the worst moment to discover the answer is yes. Asked directly on
  2026-09-12, four of the seven resolved on a sentence each. That is worth
  recording as a method: a trigger phrased as draft-day *feel* is usually a
  question for the operator, not a measurement waiting on an event.

- **The cold `/bid-check` stall is not felt, so the 2026-08-19 entry is
  closed.** Owner: "It's not slow at all, didn't notice the stall." The entry's
  trigger was explicitly subjective — a stall on the FIRST bid of a player
  actually being felt — and the operator is the only instrument for it. The
  measurements stand and are not in dispute: a cold `/bid-check` is 935ms (a
  binary search over MILP solves), 89.5–89.8% of it inside CBC in aggregate and
  65–92% per subject; the warm path is 9ms from the marginal cache, which is
  what the operator's own next keystroke hits. **The costed fix is not lost by
  closing this.** Three candidates were measured 2026-08-21 with
  `tests/measure_marginal.py` (which stays, and re-runs with `--sweep`): model
  reuse 1.06x, reuse plus `warmStart=True` 1.14x, and the probe search's
  single-solve dual 1.55–1.60x on the big-pool states (1.45x overall, worst
  subject 1511ms → 772ms), all three reproducing the reference marginal
  byte-for-byte over 168 subjects. It was not shipped because 1.6x on the slow
  cases does not buy a second MILP formulation plus a confirm loop plus two
  float-epsilon subtleties on the hottest path in the app — and that reasoning
  is unchanged. Note this is a report from use ahead of the auction, not from
  the auction: if the day feels different, the work is sitting there.

- **Opponent roster edits get a log entry and an undo, not a confirm dialog.**
  Owner: "Don't put an edit confirmation button. Just log it and allow an
  undo." The 2026-08-11 entry worried that `/assign` pointing the view at the
  buyer auto-presents a rival's EDITABLE panel at the highest-tempo moment of
  the draft, where the controls used to need a deliberate click. Verified that
  the behaviour the decision asks for already ships, rather than assuming it:
  all five edit kinds — `adjust-salary`, `toggle-bench`, `move-to-minors`,
  `move-to-roster`, `team-done` — append a `ChangeRecord` through
  `main._log_change`, which the Logs panel renders in its **Change** tab with
  timestamp, team, kind and detail; and every one of them snapshots (through
  `_undoable` or `save_snapshot`, enforced by
  `TestEveryMutatingPostTakesASnapshot`), so `Ctrl+Z` reverts it. So an
  accidental edit is visible and reversible, which is what was asked. No code
  change. Gating the forms on `is_my_team` was never on the table — auditing a
  rival is the feature the 2026-08-07 view work exists to provide.

- **Only `/assign` re-solves the exact standings, and that is now a decision
  rather than a deferral.** Owner: "keep the button for all the other edits, no
  need to recompute after them." Since 2026-09-10 a pick fires
  `HX-Trigger-After-Settle: {"solveStandings": true}` and the column re-solves
  itself; every other mutation still calls `_recompute()`, still clears
  `exact_projections`, and still drops the column back to estimates, with
  `#proj-basis` saying so and the manual button the way back. The open question
  was whether to extend the trigger to the other mutations. Closed as no: the
  parallel scan is 384ms on a fresh league, a bench toggle or a salary edit is
  not a moment that needs a rank badge to be exact, and the marker already
  makes the state honest. **The two measured warnings the entry carried still
  apply to any future attempt**: a per-team cache invalidated only when that
  team's roster or budget changes would have served 28 of 45 stale rows (62%)
  over five picks on a fresh league, with single-pick swings reaching −26
  points; and a synchronous solve on the other action paths is exactly what the
  out-of-band after-settle shape exists to avoid.

## [2026-09-11]

### Removed

- **`/bid-check`'s `highest_bidder` form field, which was dead and looked
  load-bearing.** Filed 2026-09-10 while answering "does it matter whose turn
  it is to bid", deferred then as out of scope, and closed now. The field was a
  closed loop: `bid_panel.html` rendered a hidden input from
  `ctx["highest_bidder"]`, `/bid-check` read the form value straight back into
  that same context key, and **no JavaScript anywhere wrote it** — `shortcuts.js`
  never names it — so it arrived as `""` on every request the app has ever
  made. It then became `MarketInfo.highest_bidder`, which `optimizer.py` does
  not contain the string for: `compute_bid_recommendation` never read it.

  **Why it was worth removing rather than leaving inert.** A `highest_bidder`
  parameter on the bidding endpoint reads as though the advisor tracks who is
  currently winning, which is exactly the trap the nomination turn had been —
  a feature that looked load-bearing, gated nothing, and took a full audit to
  disprove. `MarketInfo.highest_bidder` itself stays: `compute_market_ceiling`
  populates it honestly and `tests/test_market.py` asserts on it. This was the
  form field and its hidden input, four sites plus nine test payloads.

### Fixed

- **The live `MarketInfo` built by `/bid-check` now describes the bidders it
  was given.** Removing the form field forced the question of what the
  constructor should take instead, and the honest answer was not `None`.
  Three of its fields were wrong on that path and had been all along:
  `highest_bidder` was permanently `None` while `demand_count` in the same
  struct said there were bidders; `highest_bid` carried the *ceiling*, which is
  the SECOND-highest opponent max whenever BOT is only observing, so it did not
  describe `highest_bidder` even when that was set; and `second_bidder` was a
  flat `None`. They are now derived at the call site with
  `compute_market_ceiling`'s own convention — sort the eligible opponents by
  physical max descending, take the first two, and fall back to
  `None`/`None`/`0.0` when there are none.

  **Nothing outside tests reads any of the three, which is the reason to fix
  them rather than the reason not to.** That is the reasoning the neighbouring
  `floor_demand` comment already carried — an inconsistent `MarketInfo` is a
  trap for whoever reads one next — and it was being applied to one field of
  six. No behaviour changes: `compute_bid_recommendation` takes
  `market_ceiling` and `floor_demand`, neither of which moved.

  **The first version of the guard was equivalent-mutant green, and the data is
  why.** Every team sits at `physical_max_bid == MAX_SALARY` on a fresh league,
  so "richest named bidder" and "poorest named bidder" are the same team:
  reversing the sort order passed all three tests. The suite now gives each of
  three opponents one max-salary pick to separate their budgets, asserts the
  three maxes are **distinct** as an explicit precondition (a refreshed
  `players.csv` moves the keeper salaries this relies on, and the failure says
  so), and reads the expected ordering off live state rather than hard-coding
  it. Three mutants die: reverting to always-`None`, sorting ascending, and
  putting `highest_bid` back to the ceiling. The fields never reach the
  response, so the tests capture the struct by patching
  `main.compute_bid_recommendation`.

### Added

- **A Clear control on the nomination panel.** `/nominate` returned an RFA and
  a UFA card and nothing dismissed them except bidding one. `shortcuts.js` has
  removed the card you acted on since the panel split, but the commoner case —
  reading a recommendation and deciding against it — left it on screen until
  the next `/nominate`. An × now sits beside "Auction", in the slot the removed
  Override dropdown vacated this morning, and clears both cards at once (owner
  decision; the alternative offered was one × per card).

  **It is gated on there being something to clear, and taken away in both
  directions.** An × beside a heading with nothing under it is a control that
  does nothing, which mid-draft reads as a broken button. The Jinja gate is the
  same `is defined and` pair the cards use — but that only re-evaluates when the
  server renders, so bidding the RFA half and then the UFA half would strand it:
  both cards go client-side and the × would be left alone. `shortcuts.js`'s
  existing `htmx:afterRequest` handler now removes it when the last
  `.nomination-pick` goes. Two paths, two tests.

  Mutation-checked three ways, and the useful one is the first: `querySelector`
  in place of `querySelectorAll` — the obvious slip — satisfies every "the card
  went away" reading, which is why the browser test asserts the count at
  **zero** rather than "fewer". The other two: the Jinja gate removed (the
  fresh-league absence assertion fires) and the `shortcuts.js` tail removed
  (the bid-both-halves test fires).

### Fixed

- **The Proj column tooltip was 375 characters in a box that could not be read,
  and neither half of that was what the browser suite was measuring.** The
  owner's report was "this tooltip is too big". It was: the longest hover text
  in the app, **3.2× the median `data-tip`**, rendering **270×324px** — a third
  of the panel's height hanging off a table header.

  **The length was the smaller problem.** `Proj` is a `th` inside
  `.table-scroll-x`, and DaisyUI centres a bubble on its trigger with no flip
  logic. Re-measured 2026-09-11 closed-form over the whole scroll range rather
  than sampled (the geometry is linear in `scrollLeft`, so the answer is a
  ratio of interval lengths and a sampled sweep just reports its own step):
  the shipped 270px bubble is outside the visible box at **100%** of the scroll
  positions where the header is fully visible.

  **The figures do not depend on the viewport width — that was the error in the
  first write-up of this entry.** Measured at 1600 / 1280 / 1024 / 928, the
  clipped fractions are *identical* at all four, because the binding constraint
  is the trigger's clearance from the **table's right edge** (82px), which is
  content geometry. `Proj` is second from last of eleven columns, so scrolling
  fully right still leaves only 82px to the box edge for a bubble wanting 135px
  of it. The superseded sampled figures — 5 of 6 positions at 1280, 4 of 5 at
  1024, 6 of 7 at 928, "only the fully-scrolled-right position works" — read as
  width-dependent because each width happened to admit a different number of
  samples, and the "works" position is clipped by **under a pixel**, which the
  sweep's tolerance swallowed.

  **The bubble was also widening the widest table in the app.** An absolutely
  positioned descendant contributes to its ancestor's scrollable overflow, so
  merely carrying the `data-tip` pushed `.table-scroll-x`'s `scrollWidth` from
  **776 to 815px** (+39, identical at all four widths). The bubble partly made
  room for itself: those 39px of extra scroll range are why the pre-removal
  page had 121px of right clearance against 82px now. This does **not** explain
  the unreconciled 815px min-content figure in `league_state.html`'s note —
  checked, `tests/measure_layout.py` reads `getBoundingClientRect()` under
  `width: min-content`, a border box, which excludes that overflow. The
  coincidence is exact and is still a coincidence.

  **No bubble can live there, and that is geometry rather than tuning.** The
  plan was to shorten the text and add a right-anchor rule in the shape
  `.team-stats` already uses. Measured, that fixes nothing: re-anchoring moves
  the failure to the opposite edge, and narrowing does not rescue it either —
  over the shipped geometry a 200px bubble is clipped at **100%** of hover
  positions, 120px at **57%**, and even an 80px one, far too narrow for a
  sentence, at **32%**. (The first write-up said ~50 / ~30 / ~20%, which was
  both sampled and quoted from one width; the real figures are worse and do not
  vary.) A 27px trigger with 82px of clearance has no width that works. So the
  header explanation is a native `title` now, which has no box to clip — the
  conclusion `.price-drivers` reached in `style.css` for the same reason, and
  the one the `#proj-basis` marker in the *same* `<th>` had already reached.

  **Why `TestTooltipsStayInsideTheirPanel` was green throughout.** It measures
  at `scrollLeft: 0`, where the Proj header is not on screen at all, and bounds
  containment against the scroller's `scrollWidth` rather than against what is
  visible. A 270px bubble in a 379px panel passes both of its checks while being
  unreadable at every scroll position an operator would hover from. That entry
  is removed from its `required` inventory with the numbers written down, not
  quietly dropped — and nothing replaces it, because after this change **no**
  `data-tip` lives inside a horizontally scrolling container, so the container
  class it stood for has nothing left to measure. A cheap static endpoint test
  (`test_the_header_explanation_is_not_a_daisyui_bubble`) guards the regression
  the browser suite structurally cannot see.

  **The text lost the half that was already being said better.** The
  `#proj-basis` marker rendered into the same `<th>` carries a state-aware
  `title` in three branches; the header tip restated it at greater length and
  **less accurately**, saying "estimates" unconditionally — including in the
  state where every figure on screen is a real solve. So the basis story stays
  with the marker and the header says what the column *measures*, which nothing
  else in the app does and which is genuinely non-obvious: only the starting
  12F/6D/2G scores. 375 chars → **143**.
  `test_the_header_and_the_marker_do_not_say_the_same_thing` pins the split in
  both directions.

### Investigated

- **The backlog is current.** Asked directly: does `BACKLOG.md` still describe
  what is left to do? Audited end to end — every entry re-read, all 15
  `file:line (symbol)` references resolved against the working tree, each
  finding's mechanism re-checked in the code, and `CHANGELOG.md` plus 40 commits
  grepped for anything that had closed one. **Nothing in the file has already
  been fixed**, and no reference has drifted. Today's nomination-turn removal
  stranded nothing: no entry mentions `current_nominator`, `/set-nominator`,
  `snake_draft` or `nomination_index`, and `GET /nominate` still exists.

  Four corrections landed, all in prose rather than in the findings themselves.
  The triage note said "all **nineteen** findings reproduce" — true when it was
  written that morning, and four were closed by the work that followed the same
  day (`d89e4ba`, `8ca89f4`, `5b8aacc`), so it now says fifteen and names them.
  The `_context` entry said that endpoint builds **16** keys; it builds **23**
  (its 705-row `bid_limits` figure is right). Four references were re-anchored:
  each passed `tests/test_backlog_refs.py` only because the enclosing symbol's
  span absorbed it, while pointing at a docstring line, a bare `)` or an
  unrelated line rather than at the code the entry describes. And the four
  newest and highest-priority findings sat above the first `###` with three
  paragraphs of guidance wedged below them, so a reader scanning headings missed
  them — they have a `### recently filed` heading now.

  Separately, six comments **pointing at** `BACKLOG.md` from code and tests had
  rotted, which `tests/test_backlog_refs.py` structurally cannot see (it
  validates references *inside* the two docs only): a stale 704-row figure the
  backlog itself had already corrected to 705, two citations of a per-column
  decomposition that moved to this file, a citation of `bid_limits.html` by a
  line the backlog no longer references at all,
  and two comments citing entries that were deleted when they were resolved.
  All now point where the content actually is.

- **"Goalie features" removed from the Ideas list** at the owner's request. It
  was the only place in the repo recording that the July 2026 round-2 rebuild
  moved goalies onto projected wins and that **the old accuracy numbers
  therefore no longer apply** — `CHANGELOG.md` has nothing on goalie accuracy
  and the nearest code comment documents a different goalie problem. That
  caveat moved into `.claude/rules/pricing-pipeline.md` beside the existing
  `proj_wins` documentation, where it is a fact about the model rather than a
  want. The idea itself is gone.

### Removed

- **The nomination-turn tracker — badge, Override dropdown, `POST
  /set-nominator`, three state fields and the `/assign` advance.** The owner
  asked whether it earned its place: *"Even when it's my turn, I can just get
  recommendations and then put that player into the auction system to be bid
  on. Do I need that dropdown/intro order?"* The audit answer is sharper than
  "it is unused": **the turn's only effect on the running tool was to HIDE a
  button.**

  `AuctionState` carried `nomination_index`, `nomination_round` and
  `snake_draft`; `current_nominator()` walked `_effective_order()`; `/assign`
  advanced the pointer on every UFA sale; the panel drew an `X's turn` badge
  and an Override `<select>` posting to `/set-nominator`; and the button read
  `{% if current_nominator == my_team %}`. That gate is the whole of what the
  feature did. `current_nominator()` had **exactly one** non-test caller —
  `main._context`, putting it in the template namespace. It was never compared,
  branched on or passed to anything in Python, and `grep` found it nowhere in
  `market.py`, `optimizer.py`, `trade.py`, `/bid-check` or the MILP.
  `recommend_nomination` is hard-coded to `MY_TEAM` and turn-blind; `/nominate`
  never checked a pointer (its docstring asserted "It's BOT's turn" against
  code that checked nothing); `/assign` validates only that the team and the
  player exist, so any team could always be assigned any player at any time.

  **The inconsistency is what made it worth removing rather than merely
  harmless.** `shortcuts.js` binds `n` to `GET /nominate` unconditionally, so
  the recommendations were *already* available out of turn — by keyboard, while
  the button documenting them was hidden. Nothing was lost; a keyboard-only
  path became a visible one.

  **Nothing asserted the gate.** No test anywhere read the badge text, the
  badge classes, or the button's presence or absence — which is also why the
  removal needed new tests rather than deleted ones to stay honest.

  Two latent bugs went out with it. `/team-done` flipped `is_done` without
  adjusting `nomination_index`, so `_effective_order()` shrank and the same
  index silently resolved to a **different** team. And the Override dropdown
  listed `nomination_order` forward while the pointer indexed a list *reversed*
  on odd rounds, so on half the rounds the dropdown and the badge disagreed
  about what a selection meant.

  **`nomination_order` stays, under that name** (owner decision — no
  data-format churn before a draft). It is the app's canonical team display
  order: `league_state.html`, `standings_cells.html`, `bid_panel.html`,
  `team_panel.html` and `main`'s `default_bidders` all iterate it, it is in the
  data fingerprint, and `standings_cells.html` reading the same **unfiltered**
  list is what makes its OOB cells match League State's rows by construction
  rather than by coincidence. `snake_draft` did go, from both `data_loader.py`
  and `data/fchl_teams.json`; it was never in the fingerprint, so that cost no
  refresh dance. Both league rules stay documented in CLAUDE.md's CBA section —
  what changed is that the tool no longer claims to model them.

  The button is **unconditional and still on demand**, never
  `hx-trigger="load"`: each `/nominate` is a MILP solve and the fragment ships
  inside `all_panels.html`, which answers `GET /` and a dozen other things, so
  a load trigger would solve on every page load and every panel swap — the
  hazard `/solve-standings` already documents.

  Mutation-checked five ways, and **one mutant exposed a weak test rather than
  weak code**. The first version of the always-visible-button test took a
  single checkpoint after eleven picks; a faithfully restored round-robin gate
  over an eleven-team order lands back on BOT after exactly eleven picks, so
  that mutant **survived** while looking like coverage. The test now walks one
  lap **plus one** and asserts the button after every pick, and the mutant dies.
  The other four: `/set-nominator` restored (caught by a 404/405 assertion,
  because a silent 200 is what a stale bookmark would look like), a dead
  context key named in a template (caught by a static grep guard — a template
  naming a key that no longer exists renders **empty** rather than raising),
  and `from_json` / `to_json` reading or writing a removed key.

  **Backward compatibility was the one thing that could have bitten on draft
  day.** `from_json` read all three keys with **bracket** access, so dropping
  the reads is what lets a state file written before today load unchanged — and
  the operator's live `data/state/auction_state.json` is exactly that file. A
  `KeyError` there is a tool that will not boot four hours into an auction, and
  the failure would be *silent*: `lifespan` catches broad `Exception` by design,
  so it would have renamed a byte-perfect draft record `.corrupt` and started
  fresh. `TestALegacySaveStillLoads` supplies the three keys and asserts they
  are ignored.

  Docs corrected in passing: `.claude/rules/pricing-pipeline.md` said
  `nomination_order` was read by "four templates" when it was five — the claim
  was written 2026-09-10 and was already stale, since `standings_cells.html`
  joined the list on 2026-08-17. Removing the dropdown made the stale number
  accidentally true, which is recorded rather than quietly inherited. And
  `standings_cells.html` explained its unfiltered loop by saying done teams
  "are dropped by `next_nominator`" — a function that has never existed
  anywhere in this repo.

### Investigated

- **Tailwind's Play CDN stays; a real build is not worth its cost here.** Open
  since 2026-08-06 on the reasoning that the bundle JITs utility classes in the
  browser on every page load and a real build would ship a fraction of the CSS.
  That reasoning was never wrong, only uncosted — and `static/vendor/README.md`
  already argued the case qualitatively, which is precisely why the entry kept
  getting re-opened.

  Measured 2026-09-11 in Chrome at 1280px against the local server, median of
  five loads: **613ms with the bundle, 362ms with it blocked — ~250ms, paid
  once per session.** Every interaction after the first `GET /` is an htmx swap
  with no reload, so 250ms is the entire cost for a four-hour draft. node and
  npm turned out to be installed (v22.22.1 / 9.2.0), so the toolchain was
  available and still is not the deciding factor: a build needs a rebuild on
  every template edit, and its one failure mode — `shortcuts.js` composes
  `'alert-' + type` at runtime, which a source-scanning build cannot see, and
  which survives today only because DaisyUI's prebuilt CSS carries every
  `alert-*` variant — would surface on draft night rather than in a test.

  Owner decision 2026-09-11: closed, no code change. The number is now in the
  README's "Why the Tailwind *Play* CDN and not a real build" section, so the
  next person re-litigating it has something to argue against.

### Fixed

- **The collapsed summary was the worse offender, and the first pass missed
  it.** The table learned to hide a row that says nothing; the `<summary>` — the
  closed state, the thing the card exists to answer without being opened — went
  on naming a driver and printing `×1.0`. Measured, on **135 of 705** players
  against the 12 rows inside, and for **82** of them that same driver moved the
  floor odds by 1.5x or more: the summary had something to say and said nothing
  instead. Kiefer Sherwood read "Points ×1.2 · Scarcity ×1.0" while Scarcity
  multiplied his floor odds by 2.01 and he sits at 99% floor; he now reads
  "Points ×1.2 · 99% floor". Dropping the entry is not lossy, which is the half
  worth asserting: the floor percentage beside it is the answer for exactly
  those players. 27 players now carry no price driver in the summary at all,
  correctly — every one of theirs rounds to 1.0.

  The filter runs at **one** decimal, not the table's two, because that is what
  `player_chart.html` prints there. `_is_unit` grew a `places` argument rather
  than the summary borrowing the table's rule, and a mutant that makes it ignore
  that argument dies.

- **Three test defects the first commit introduced or left behind, all in the
  code that zips engine facts onto rendered rows.**
  `test_the_bars_are_scaled_in_log_space_not_on_factor_minus_one` carried an
  INLINE copy of the hiding predicate — and a comment warning that a predicate
  disagreeing with the template "silently labelled every width with the wrong
  group". The rule changed on 2026-09-11 and the copy did not, which is exactly
  the mislabelling its own comment describes.
  `test_it_renders_the_numbers_the_engine_computed` compared the printed factors
  against ALL five drivers rather than the live ones: it passed only because its
  subject has every driver in play, and it quietly asserted the *opposite* of
  the hiding rule. Both now go through one `_live_drivers` helper.

  And the helper itself was unpinned, which mutation testing caught: reverting
  it to the stale predicate broke nothing, because only ONE player in the pool
  separates the two rules and no test happened to select him. A new sweep
  asserts the helper picks the same rows as the card for all 705 players, so a
  second statement of the rule can no longer drift in silence. Cost check on the
  bidding path, where this card is embedded: `_driver_rows` is 0.013ms median /
  0.046ms max, `GET /player-chart` 10.2ms median.

- **`_odds_label` divided by its own argument.** `math.exp` underflows to `0.0`
  below about -745 rather than raising, so a `floor_logit_delta` past that would
  have produced a `ZeroDivisionError` — and this card is embedded in
  `/bid-check`, so the failure would be a 500 on the bidding path rather than a
  wrong number. Not reachable with current data (the pool runs -16.27 to +3.94,
  729 of headroom), but `data/model_params.json` is regenerated by another repo.
  `_odds_ratio` now clamps in LOG space, before the exponential, so the
  pathological values never reach `math.exp` at all; the clamp binds only past a
  thousandfold, which is where the label stops printing digits anyway, and a
  test asserts it changes no printed figure on any real row.

  While fixing it: the cap compares the **rounded** value, because
  `1 / exp(-log(1000))` is 999.9999999999998 and a bare `>=` let it through to
  print a naked `1000` — the capped figure without the `+`, claiming precision
  the label exists to refuse.

- **A dead field shipped in the first commit.** `_driver_rows` returned
  `floor_factor` beside `floor_label` and no template read it — which is exactly
  the defect the `floor_logit_delta` backlog entry described one level up, "an
  unrendered number beside a price factor is an invitation to render it as one",
  reintroduced in the commit that closed it. Removed, along with the two
  redundant `math.exp` calls it came with: the odds ratio is now computed once
  per driver and reused by the filter and the label, so the safe form is the
  only form.

- **An NHL badge now labels the club it actually drew.** `src` resolved the
  alias and `alt`/`title` did not, so a badge pointing at `UTA.svg` announced
  itself as `UTH` — a tricode no NHL club has — on `players.csv`, the pool the
  draft is run from, while the same club read `UTA` on `players-25.csv`. The
  player search's meta line printed the raw code for the same reason, and it is
  the only place in the app a club code reaches the screen as text rather than
  as an image.

  Owner decision 2026-09-11: the canonical tricode everywhere. A label is a
  fact about the club, not about which CSV is loaded. `main._nhl_canonical` is
  the single resolver and `_nhl_logo_src` is now built on it, so the image and
  its label cannot drift — the `_dom_id` rule, applied to the second half of a
  string that already had it applied to the first.

  The guard is the invariant rather than a spelling check on one club: swept
  over every `data/players*.csv`, no rendered club label is an alias KEY. A
  refresh that introduces a new FCHL spelling fails at pytest instead of on
  draft night. All four mutants die, including the one that makes
  `_nhl_canonical` the identity function.

### Added

- **The price-driver card grew a second column: what each driver does to the
  ODDS of a $0.5M floor sale.** `DriverContribution.floor_logit_delta` had been
  computed for every driver of every decomposition since the card shipped and
  read by nothing but the test asserting it reconstructs. That mattered more
  than "an unused field": **490 of 705 pool players clamp up to their position's
  `min_bid`**, so for most of the pool P(floor) *is* the price story and the
  median waterfall was explaining a figure nobody pays. Measured before
  building it, **556 of 705** sit at P(floor) >= 50%, and the mean |stage-1
  log-odds delta| runs Points 1.589 / Scarcity 1.486 / NHL team 0.421 /
  Reputation 0.372 / Contract 0.067 — so the column that was missing was
  routinely the larger of the two.

  **It is an odds ratio, and that is not a presentation preference.** Stage 1 is
  a logistic, so the intuitive display — "this driver adds 12 percentage points
  to P(floor)" — is the per-row dollar step in a new costume: the sigmoid is
  nonlinear, so the step depends on what the running odds were when the row was
  applied. `exp(floor_logit_delta)` does not move, and the chain reconstructs
  exactly — `base_odds × PROD(ratios) = player_odds`, maximum relative error
  **6.8e-13** across all 705 players.
  `test_the_floor_odds_do_not_depend_on_their_order_either` measures the
  percentage-point step and the odds ratio through the *same* permutation loop,
  so the two are compared like for like rather than one being computed from the
  formula it is meant to be tested against, and a second guard fails if any
  driver row's effect cell ever prints a `%`.

  **`main._odds_label` is not a `%.2f`, and the reason is measured.** The pool's
  ratios span **8.6e-08 to 51.6**, so two fixed decimals print `×0.00` on **66
  rows** — a column reporting "this driver did nothing" about the single largest
  effect on the card. It shows the reciprocal as `÷`, with precision following
  magnitude (p50 1.99, p90 14.2, p99 3392) and a `÷1000+` cap, because the
  measured maximum is 11,691,617 and those digits are not a number an operator
  uses; the bar carries the magnitude.

  **The two columns get different hues on purpose.** Their coefficients often
  carry opposite signs — F's `floor_coef_log_rank` is +3.006 against
  `coef_log_rank` −0.196 — which is not a contradiction (a deep rank makes a
  player both likelier to be a floor sale *and* cheaper if he is not) but does
  mean they cannot share green/red, which already mean "dearer" and "cheaper"
  one column to the left. Measured, the two disagree about what they do to the
  price on **195 of 2361** rows, and those rows are the reason the column
  exists. The floor bar is coloured on its own sign.

  Also: the table gained a `<thead>` — it had none, so nothing on screen said
  what `×6.07` meant — the terminal row is now `This player` rather than `Model
  median` since it ends both chains, and the collapsed `<summary>` carries the
  floor percentage, because a closed card reading "Points ×6.07" misleads on the
  556 players whose sale probably happens at the minimum.

### Fixed

- **Driver rows are hidden on what they PRINT, not on an exactly-zero delta —
  and the reason the old rule was kept turned out to be false.** `_driver_rows`
  tested `log_delta != 0.0`, leaving 12 rows across the pool on screen printing
  `×1.00`, which is the same "the model ignores this" misreading the original
  hiding change removed for the structural cases. The deferral reason, in the
  code comment and in `BACKLOG.md`: a dropped `×1.004` row "would leave a gap
  between the base and the median that nothing on screen explains".

  Measured, **the card already did not reconcile, and by 70x more**. Printed
  base × printed factors misses the printed unclamped median by more than $0.01M
  on **312 of 705** players — mean $0.008M, max $0.19M (McDavid: $0.32M ×
  [6.07, 1.37, 1.79, 2.83, 1.19] = $16.04M against a printed $15.85M) — purely
  from rounding each factor to 2dp. Dropping every `×1.00` row changes that by
  **$0.0000M** on every player. The gap is a property of printing rounded
  factors at all, and no residual row was added: that is the running-dollar
  column the order-invariance test exists to forbid, wearing a third hat.

  **The rule reads both columns, and shipping it alone would have been a
  regression.** Measured, **11 of the 12** `×1.00` price rows move the floor
  odds by something worth seeing, against exactly **1** row inert in both — so
  hiding on the price column alone would have dropped eleven rows whose entire
  content is in the column that landed beside it. That is why the two changes
  are one commit. The honest size of this half, once the column exists: **one
  row across 705 players.**

  Two tests had to be rebuilt rather than adjusted. `_live_groups` computed the
  old rule, so `test_it_names_exactly_the_drivers_that_are_in_play` would have
  gone on asserting set equality against the wrong set. And
  `test_a_row_that_says_nothing_in_either_column_is_hidden` was first written
  against a player decomposed against his OWN features, where every delta is
  *exactly* zero and the old and new rules agree — mutation testing showed the
  old rule surviving it. Its subject is now NUDGED by an epsilon on one input,
  which is the only shape that separates the two rules.

- **A bar test that passed while measuring the wrong thing.**
  `test_the_bars_are_scaled_to_the_effects_they_show` matched
  `driver-bar[^"]*" ... .*?&times;` across a row. With a second bar in each row
  that pattern began pairing every FLOOR bar with the NEXT row's price factor —
  five matches, every assertion green, every pairing wrong. Found by running the
  suite against the new template, not by reading it. The pattern now anchors on
  the price bar's exact class and takes the factor from the cell immediately
  after it: a regex that spans cells is not reading a row. Same commit, a second
  regex bug in the new test beside it — `<th[^>]*>` also matches `<thead>`, and
  swallowed the whole header row into one bogus "cell".

### Changed

- **`BACKLOG.md` re-triaged and cut from 322 lines to 229.** Every one of the
  nineteen open findings was re-checked against the code rather than re-read —
  all nineteen still reproduce, so nothing closed on the walk — and four claims
  that had drifted were corrected: `.table-scroll-x` is four regions now and the
  keyboard entry said three; `bid_limits` is 705 rows and the `_context` entry
  said 704; the previous triage note ended "No entries are waiting on a manual
  check", which stopped being true when the draft-day items were filed.

  **The fourth was the one worth finding.** The idea *"make the exact standings
  automatic rather than a button"* still read "the operator has to remember to
  press it … revisit if draft-day use shows the button being forgotten" — a
  description of the state of things before **2026-09-10**, when `POST /assign`
  started firing `HX-Trigger-After-Settle: {"solveStandings": true}` and the
  column began re-solving itself after every pick. A backlog entry asking for
  something that shipped the day before is worse than no entry: it invites the
  work to be done twice. Rewritten to the honest residue — only `/assign` asks,
  so every other mutation still drops the column to estimates and the manual
  button is still the only way back from those — and carrying forward the two
  measurements any follow-up has to respect (the per-team cache would have
  served **28 of 45 rows stale** over five picks; the parallel scan is 384ms
  fresh, which is why the one trigger that exists is out-of-band).

### Notes kept from closed wants

The 2026-08-07 testing pass has fully landed, and its entries had accumulated a
hundred lines of *what the want got wrong* on top of `BACKLOG.md`. That is
finished work, so it moves here under the documented split. Each of these is a
correction the next want in the same area will hit.

- **Nomination panel** (landed 2026-08-18). The entry called it an additional
  **column**; the panel is two cards, not a table, so it is a second labelled
  figure on the existing line (`Expected: ~$2.8M ▼ · Model $9.5M`), which is what
  let it reuse the struck-model grammar from the Available Players price column
  rather than invent one. It also assumed the two figures would routinely differ.
  Measured, they do not: the UFA drain ranking breaks ties toward **least
  surplus**, so it actively selects the candidate whose model and market prices
  agree — $2.51M against $2.50M is that half's normal case, and the divergence
  the want was about shows up on the **RFA half and on target picks**. That is
  also why `is_capped` is quantized to one decimal; a cent of gap would strike
  through a figure identical to the one beside it.

- **Buyout Analyzer** (landed 2026-08-15, after a 2026-08-06 entry closed it as
  "already built" and a 2026-08-08 decision reopened the dropdown half the other
  way). Two corrections. The entry said the buyout **dots** would "need somewhere
  to live" if the list collapsed to a `<select>` — they never lived in the
  Analyzer at all; they are in `team_panel.html`'s two roster tables, and
  duplicating them into a picker would collide on `_dom_id`. And it did not
  mention the only real defect in there: `hx-get="/buyout-check/{{ p.name }}"`
  was the one place in the app a raw player name went into a URL unencoded.

- **Logs** (landed 2026-08-15). "NHL team logos in **both** logs" was not
  buildable as written. `ChangeRecord` holds `timestamp`/`kind`/`team_code`/
  `description` and no player at all, so an NHL club badge has nothing to resolve
  from there. Both logs carry an FCHL team logo; only the transaction side can
  carry an NHL one.

- **League State table** (landed 2026-08-13). Shorter column headers are the only
  lever left on that table's width, and the premise under the original want was
  wrong in a way that would sink the follow-up. Removing the full team name and
  the "Stopped Drafting" label took min-content from **955px to 868px** — only
  87px, because min-content is each column's longest *word*, not its longest
  string, so a two-word name never cost more than "Johannesburg". Measured
  afterwards, **all 12 columns were floored by their own header text** and summed
  to exactly the 868: Remaining 102, Spendable 100, Cap Used 92, Max Bid 83,
  Penalty 81, Roster 73, Needs 72, Team 66, Proj 57, Done 53, Pts 51, logo 38.
  **That decomposition no longer predicts the total** — the Spendable column came
  out on 2026-09-07 and min-content went to 815px, not the ~768 that subtracting
  its 100px implies, so read the per-column figures as an explanation of *why*
  the headers are the floor, not as an additive model; re-measure with
  `tests/measure_layout.py`. Nothing in the table *body* can narrow it further.
  `Rem`/`Spend`/`Cap`/`Max`/`Pen` would, by roughly 200px, and that is
  abbreviating the labels on a dense grid of money figures that all look alike —
  not built, because it was not asked for and the legibility cost is real. The
  table still overflows its column at every width including 1920, so
  `.table-scroll-x` stays load-bearing either way.

- **Available Players** (landed 2026-08-16). The RFA filter was built; **both
  bid-panel tooltips already existed** — sigma on the chart's meta line, where
  `TestTooltipsStayInsideTheirPanel` already required it by name, and marginal
  value in the `.bid-details` row. Recorded rather than deleted because that was
  the second time a want in this list turned out to be built already (the Buyout
  Analyzer, 2026-08-06) and the cost each time is a re-investigation. **Check the
  template before filing a tooltip want.**

- **Trade form** (landed 2026-08-15). "Cramped" understated it: measured at
  1280, the four controls rendered 120–183px against labels wanting 229–316px, so
  the salary and points were off the edge on every row. Both forms are stacked
  one-column `.choice-list` checkbox blocks now.

- **Interaction budget: every UI interaction < 500ms.** Met, and closed on
  measurement rather than on more work. Measured 2026-08-06 on a fresh state:
  warm `/bid-check` **9ms**, `/assign` **150ms**, `/nominate` **130ms**, `/undo`
  **127ms**, `GET /` **20ms**, `/explain` **215ms** cold and **9ms** warm.
  `/trade-evaluate` was the one hole — it needed a built-up trade form — and the
  2026-09-10 verdict-fragment split made it trivial to assemble: **345ms**
  (measured 2026-09-11, median of 5 warm, one give plus one receive), dominated
  by the scenario MILP solves, with the `_context` the response no longer uses
  accounting for ~8.5ms of it, 2.5%. Two things stay true and are not defects:
  the *first* bid check on a new player is still ~1000ms, and the lever there is
  a cheaper solve rather than fewer solves — **not** pool pruning, which was
  measured unsafe on 2026-08-20. Where a regression would actually hurt, the
  guard is a solve count rather than wall-clock (`tests/test_bid_cache.py`,
  `tests/test_counterfactual_cache.py`): timing assertions go flaky under load
  and the solve count is the cause anyway.


## [2026-09-10]

### Removed

- **17MB of third-party reference material was committed by accident and is
  now untracked.** `git add -A` on the bench-cap commit swept in four files
  that had been sitting untracked in `data/` since before the session and that
  nobody asked to version: two DobberHockey PDFs (9.4MB and 6.0MB), the
  2026-27 draft-list workbook (1.3MB) and the free-agent draft workbook
  (128KB). Nothing in the app or the tests opens any of them — the one mention
  anywhere is a docstring in `tests/test_state_json_conversion.py` naming a
  file by date, not reading it.

  `git rm --cached` untracks them; they stay on disk. `.gitignore` now covers
  `data/*.pdf` and `data/*.xlsx` by **extension rather than by name**, because
  the next guide will have a different filename and the same sweep will happen
  again; verified that no currently-tracked file in `data/` matches the new
  rules (all nine are `.csv` or `.json`).

  **The blobs are still in the history of those commits**, which are unpushed —
  so dropping them is a rewrite of unpublished work rather than of anything
  shared, and it is the owner's call rather than something to do quietly. Until
  then `.git` carries ~17MB it does not need.

### Added

- **Solve Standings now runs itself after every pick.** Asked for directly: "is
  there a way to autorun Solve Standings once a player is assigned to a team?"
  Owner decision on the shape: always, after every pick — not a toggle, not
  only on a turn change.

  `_recompute()` empties `exact_projections` on every mutation, so one pick
  returned all ten opponents' Proj figures to an estimate that runs +68 mean /
  +193 worst (+14.2%) and moves 9 of 10 teams in rank order, BOT's own badge
  included. The column degraded after every sale and stayed degraded until
  somebody noticed the `estimated` marker and clicked.

  `POST /assign`'s **success path** returns
  `HX-Trigger-After-Settle: {"solveStandings": true}` and `shortcuts.js`
  answers with `htmx.ajax('GET', '/solve-standings', {swap: 'none'})`. A
  rejected assign fires nothing, and neither does any other mutation — all of
  them still degrade the column, and the manual button is still the only way
  back from those.

  Three decisions about the shape, two of which were nearly wrong:

  - **Server-driven, not an `hx-trigger="load"` mount.** The obvious placement
    is a zero-height div in `league_state.html`, and it is wrong: that template
    ships inside `all_panels.html`, which answers thirteen different things
    **including `GET /`**. A load trigger would fire a ten-solve scan on
    initial page load and after every bench toggle and salary edit.
  - **`HX-Trigger-After-Settle`, and the obvious reason for it is false.** htmx
    1.9.10 does dispatch plain `HX-Trigger` before the swap — verifiable in the
    vendored source, where it runs in the onload handler right after
    `htmx:beforeOnLoad` while after-settle runs inside the settle callback. But
    the tempting conclusion, that plain `HX-Trigger` would aim the scan at the
    `proj-<CODE>` spans the `#app` swap is about to destroy, does **not**
    follow: htmx resolves an out-of-band target when the RESPONSE arrives, and
    this scan takes 250–400ms against a swap that is synchronous in the same
    tick. The mutation swapping the two headers survives both browser tests,
    and the comment in `_toast` says so. After-settle is kept because it is a
    guarantee rather than a timing margin, and it lets the panel finish
    painting before up to 8 CBC subprocesses compete with the renderer.
  - **Coalesced, not stacked.** A second pick landing mid-scan makes
    `_publish_if_current` discard the first scan silently, so two quick picks
    would otherwise run 16 CBC subprocesses for one usable answer. The listener
    holds at most one in flight and re-fires once on completion if a pick
    arrived while it ran — a latch with no queue would leave the column on
    estimates exactly when drafting fast, which is the case this feature exists
    for. The latch is released in `finally`, so one network hiccup cannot
    silently retire the feature for the rest of the draft.

  **The cost, stated plainly.** ~384ms of up to `SCAN_WORKERS` parallel CBC
  subprocesses, after every one of ~165 picks. A warm `/bid-check` measures
  43ms during a scan against 3ms idle — well inside the 500ms budget, but it
  scales with the cap. If typing feels sticky on draft day, lower
  `SCAN_WORKERS` before looking anywhere else.

  `CLAUDE.md`'s "**Never put this on an action path**" was rewritten in the
  same commit rather than left to contradict the code. `tests/test_event_loop.py`
  needed no change and that is not an accident: its three guards constrain
  *where* a solve runs, not who triggers it, and `/solve-standings` was already
  in `THREADED_SCANS`.

  Six mutants, five killed: `/assign` silent (endpoint and browser), a listener
  name typo (the cross-file contract test), the listener removed, and the latch
  never released. The survivor is the header swap described above.

- **A `+` on the Price Model card, so a chart you opened can start the
  auction.** Asked for directly: "can we add a '+' to the Price Model window".
  The pool table has had one since 2026-09-09; the chart you open *from* that
  table did not, so reading the price model and then bidding meant scrolling
  back and finding the row again.

  No JavaScript was needed. `.btn-add-bid` is delegated on `document` and reads
  `data-player`, deliberately, so the class and the attribute are the whole
  contract and a button arriving by htmx swap is covered for free.

  **Gated off the inline mount.** `player_chart.html` is rendered twice — into
  `#player-chart-container` from the players table, and inside
  `bid_panel.html` during a live auction — and in the second one the button is
  worse than useless: you are already bidding on him, `.bid-form` (Start
  Auction) does not exist while an auction is live, and the handler's only
  possible answer is the "finish the current auction first" toast. The include
  in `bid_panel.html` is wrapped in `{% with chart_inline = true %}`; the flag
  is undefined everywhere else, including the standalone
  `GET /player-chart/{name}` response, which is the mount that needs it.

  Four mutants killed by the endpoint tests (gate removed, button removed,
  `with`-flag dropped, `data-player` misnamed) and three more by the browser
  tests — the browser pair is what proves the delegation actually reaches a
  swapped-in button, which `TestClient` cannot see. One existing assertion was
  tightened in the same commit: `TestTheChartLandsWhereYouClicked` clicked
  `.price-chart-card button` unqualified, which would have hit the new `+`
  first if the gate ever broke, and timed out instead of failing clearly.

- **A platform-wide player search in the header, because "where is he?" had no
  answer.** Mid-auction a name gets called and there was no way to find out
  where that player is without hunting: the Available Players table lists only
  the undrafted, a rostered player is visible only by opening the right one of
  eleven team panels, a player in the minors sits in a second table inside that
  panel, and a bought-out player was visible **nowhere at all** — `execute_buyout`
  removes him from every list and leaves a nameless float in `team.penalties`,
  so his `buyout` `TransactionRecord` is the only evidence he ever existed.
  Nothing in the app scanned across teams: `main._nhl_team_of` was the one
  function that walked every roster plus the pool, and it threw the answer away,
  returning `p.nhl_team` and discarding *where* it found him.

  `AuctionState.locate_players` is the engine. It indexes keeper → acquired →
  minors → pool → the log, `setdefault` so live state always beats history, and
  returns a frozen `PlayerLocation` per hit carrying the location, the team, the
  cap hit, whether that salary counts on cap, and the latest `TransactionRecord`
  as provenance. **Traded is not a sixth location** — a traded player is on the
  destination roster and is found live; the trade is provenance, which is what
  makes "find someone a trade moved" answerable without inventing a place for
  him to be. `change_log` is deliberately not searched: `ChangeRecord` has no
  `player_name` field at all, only a free-text `description`, and substring-
  matching prose to find a player is a different and much worse thing.

  `main._search_rows` does every figure and every string so the template only
  branches — the shape `_driver_rows` established. Pool rows carry model price,
  market price, `market.is_capped` and `in_optimal`, all four of which `_context`
  already computes for the pool table at **no MILP cost**; `in_optimal` copies
  `_context`'s guard verbatim, because reading `.roster` off an Infeasible
  solution would star players on the strength of a plan that does not exist.
  **No max bid, deliberately**: nothing in `bid_limits` carries one, and a max
  bid is a binary search over MILP solves — on a keyup-triggered endpoint that is
  exactly the stall `tests/test_event_loop.py` exists to prevent.

  Three properties of `GET /find-player?q=` that are easy to regress and are each
  pinned. It **does not call `_context`**, passing a dict already carrying
  `"request"` through `_render`'s short-circuit: `_context` costs ~8.5ms and
  builds a 704-row `bid_limits` list regardless of what renders, against 0.21ms
  of actual search. `q` is a **query param, not a path segment**, for the reason
  `/buyout-check` already documents — `_disambiguated_names`' last-resort tier is
  ` (#n)` and a `#` in a path truncates at the fragment and never reaches the
  server. And a blank query answers **200 with an empty body, never 204**, which
  htmx reads as "do not swap" and which would strand the previous results on
  screen in the one state where they are guaranteed wrong.

  `team_code` and `link_team` are two fields on purpose. A bought-out player's
  50% penalty sits on BOT's cap and is worth naming, but he is on no roster, so
  his row names the team and navigates nowhere; conflating them sent the click to
  a panel he is demonstrably not in.

  The results partial mints **no ids at all** — `main._dom_id` owns exactly one
  id per player for the buyout dots, and a second copy is a duplicate the roster
  scan swaps twice. The mount `#player-search-results` carries the only id
  involved and lives in the navbar, outside `#app`, so a panel swap cannot
  destroy an open list.

  `/` focuses the box (`preventDefault`, because Firefox binds it to Quick Find),
  `Escape` closes the results, and the shortcuts modal grew a row — the guard's
  regex widened from `([a-z])` to `([a-z/])` in the same commit, as its own
  contract requires.

  **Three things about dismissal were found by driving it, not by reading it.**
  (1) A dismissal has to outlast the request still in the air: the input
  debounces 200ms and also fires on `focus`, so clicking a hit promptly after
  typing routinely leaves a response coming, and htmx swaps on `b.onload`
  regardless of what the page did meanwhile — the list reappeared a fraction of
  a second after being dismissed. Fixed by suppressing the *swap*, with a flag
  every close sets and the next `input` or `focus` on the box clears.
  (2) `Escape` has to clear the query as well as the list, and has to tell htmx
  it did: `changed` compares against a value htmx cached at trigger time, not
  against the DOM, so assigning `.value = ''` silently left it believing the box
  still said "hu" and re-typing the same surname fired nothing. The version
  without the dispatched event looked correct and was not.
  (3) `focus` is a second trigger for the same reason — clicking away leaves the
  query in the box, and without it coming back and re-typing does nothing.

### Changed

- **The price-drivers card no longer prints a row that does nothing.** The
  other two items from the same review were both the card saying `×1.00` and
  the reader concluding something false.

  "Does Scarcity not apply to Goalies?" — correct, it does not: `coef_log_rank`
  is exactly `0.0` for G, because ~20 goalies a season is too coarse a field to
  fit rank against. All **64** goalies in the pool drop the row.

  "What is Reputation? I didn't think that was in the model" — it is
  (`log_lag` + `has_lag`, last season's FCHL salary), but a player new to the
  league sits on *exactly* the reference's encoding, so the row has nothing to
  say. On `data/players-25.csv`, the pool this was reported against, **nobody**
  carries a prior salary, so it read `×1.000` on every card — indistinguishable
  from "not in the model". It stays live where there is a reputation: **194 of
  407** forwards on `players.csv` (91 of 234 D, 63 of 64 G), Panarin ×1.645,
  McDavid ×1.793.

  Filtered in `main._driver_rows`, not the template, and on an **exactly zero**
  `log_delta` rather than a factor that rounds to 1.00. A ×1.004 row dropped
  from a card that prints both a base and a median would leave a gap between
  them that nothing on screen explains; exact equality on a continuous feature
  only happens structurally, which is the case worth hiding. `widest` and
  `headline` are computed after the filter, or a hidden row would set the bar
  scale and the collapsed summary could advertise a driver the table does not
  show.

  The goalie row keeps the label **"Points"** (owner's call) — the detail line
  already reads "N projected wins", which is where the distinction belongs.

  `test_it_names_every_driver` asserted all five groups appear, which stopped
  being true (the reference is a UFA, so Contract is dead for every UFA in the
  pool). It is now a set equality against the engine in both directions — and
  it picks a player with a **mixed** profile deliberately: the top forward has
  all five drivers live, so against him the equality cannot fail in the hiding
  direction, and the first version of it stayed green with the filter removed.

- **The Proj basis marker is back, with different words.** Removed on
  2026-09-09 as one of four requested changes, and restored the next day once
  it was clear what it did: the owner's words were *"I didn't realize it was
  functional. I didn't understand what it did."* That is a wording bug, not a
  feature bug, so the mechanism came back unchanged — `standings_basis.html`,
  both includes, the `standings_basis` dict in `_context`, the unconditional
  span with only `hx-swap-oob` conditional, and the branch on the count of
  ESTIMATES rather than of exact figures (the 2026-08-18 bug, and two tests
  catch that mutant today: `test_an_unsolvable_opponent_keeps_its_estimate_rather_than_reading_zero`
  and `test_standings_answers_when_every_opponent_is_done`).

  What changed is the label. It read a bare `exact` / `exact 9/10` /
  `estimated`. **`exact` appeared nowhere else in the feature** — the button
  says Solve, the header tooltip says estimate — so the one word that told you
  the column's state shared no vocabulary with the control that changes it. It
  now reads `solved` / `9/10 solved` / `estimated`. And it carries its own
  native `title` in every state, which is the half that was missing: a bare
  adjective under a header does not say what it is about, and this one is
  about a *subset* of its column, since BOT's own Proj is a real MILP optimum
  in every state and the marker never covered it. The estimated state's
  tooltip names Solve Standings, because knowing the figures are guesses is no
  use without knowing what to press.

  **The label stays one word, and that is measured rather than taste.**
  `opponents estimated` was drafted first, on the reasoning that `opponents`
  and `estimated` are both 9 characters so the panel could not get wider. That
  reasoning was wrong twice. DaisyUI sets
  `.table :where(thead,tfoot){white-space:nowrap}`, so a header cell's
  min-content is its **whole string**, not its longest word — the
  longest-word rule holds for the body, where cells wrap, and `league_state.html`
  had stated it for the table as a whole. Measured 2026-09-10 by
  `tests/measure_layout.py`, three variants in one run: **768px** with the
  marker hidden, **776px** with it, **829px** with the two-word label. A second
  word cost **53px** of the widest table in the app — nearly seven times the
  marker itself — for something the tooltip says better.

### Fixed

- **A test added the day before was flaky by construction, and this batch
  tripped it.** `TestAppAssetsAreCacheBusted::test_the_token_tracks_the_file`
  asserted that `style.css` and `shortcuts.js` get different cache-busting
  tokens, on the stated grounds that comparing two real files avoids the mtime
  resolution problem a touch-and-compare has. That reasoning is backwards: two
  real files are not guaranteed to differ, and the first commit to edit both
  inside the same second — the trade-panel fix, which changed the JS and the
  CSS together — gave them the identical token `1789100528` and turned a
  correct implementation red.

  Rewritten to stamp two temp files with `os.utime` and assert the exact tokens,
  with `cache_clear()` on both sides because `_asset_version` is `lru_cache`d.
  A second test keeps the live half — both real assets exist and neither took
  the `"0"` fallback — while saying nothing about the two differing, because
  they legitimately do not. Both die under a constant-returning buster and under
  one keyed on file size.

- **Evaluating a trade wiped the form you had just filled in.** Reported as
  "when I evaluate a trade, the players get deselected, so I have to readd all
  of them to modify the trade to recheck things".

  `trade_panel.html`'s evaluate form posted with `hx-target="#trade-panel"` and
  `hx-swap="outerHTML"`, against a `<section id="trade-panel">` that contains
  both checkbox lists — so the answer and the question were swapped together,
  and the replacement is stateless by construction: the template renders no
  `checked` anywhere, no `selected` on any partner `<option>`, and
  `#trade-receive-list` back at its literal "Select a team first" placeholder.
  Nothing re-fires `loadTradeChoices` (its only trigger is the select's
  `onchange`), so the fetched half did not merely come back unticked, it came
  back **gone**, taking the partner selection with it. Both running
  `N selected · $X.XM` counters reset, and the 180px scrolling lists returned to
  the top.

  Fixed by narrowing the swap instead of rebuilding the state: the verdict moved
  into `templates/partials/trade_verdict.html`, the form targets `#trade-result`,
  and `/trade-evaluate` answers with that fragment. Same reasoning as
  `#bid-advice` inside `#bid-panel` — when the state lives only in the DOM, swap
  the answer and not the room it is standing in — and one step cleaner, because
  `/trade-evaluate` has a single consumer and can return the fragment directly
  rather than returning the panel and narrowing with `hx-select`, which carries
  the documented trap that an unmatched `hx-select` on an `outerHTML` swap
  **deletes** its target.

  **Both branches of the fragment carry `id="trade-result"`**, and the empty one
  is load-bearing for the reason `bid_panel.html` records: a swap target that
  disappears with its contents can only be swapped once. An evaluate with
  nothing ticked sets `result = None` and renders `<div id="trade-result"></div>`,
  33 bytes, so the next evaluate still has somewhere to land.
  `test_an_empty_evaluate_leaves_the_swap_target_standing` is that assertion;
  deleting the `{% else %}` branch fails it *and*
  `test_the_page_holds_exactly_one_swap_target`.

  The `.trade-verdict` **class** stays on the populated branch alone — it is the
  documented hook for "there is an answer on screen", and
  `wait_for_selector(".trade-verdict")` has to keep meaning that.

  Side effect worth recording: the response went from **26,934 bytes** carrying
  all **49** `give_player` checkboxes to **1,747**, because the give list stops
  being serialised once per evaluate.

  **The fix creates one hazard, and it ships closed in the same commit.**
  `/trade-execute` posts the SERVER's `last_trade_eval`, not the form, and only
  refuses a `trade_id` that no longer matches — so once the ticks survive,
  unticking a player and hitting Execute would execute the trade you
  *evaluated* rather than the one on screen, with nothing saying so. It was
  reachable before only by ticking boxes into an empty form; afterwards it is
  the natural next click. `markTradeEvalStale` in `shortcuts.js` now adds
  `is-stale` to the verdict and disables every submit inside it on any change to
  either list, scoped with `closest('#trade-panel')` so the team panel's
  `/trade-between` form — which shares `updateTradeSummary` and has no verdict —
  is untouched. The verdict itself is left readable rather than removed, because
  "to modify the trade to recheck things" means comparing against the previous
  answer.

  **The stale marker is called FIRST in `updateTradeSummary`, ahead of its early
  returns, and that is not tidiness.** Unticking the last box takes the
  `!picked.length` branch and returns; with the call at the bottom the marker
  fired on every change *except* the one that empties a list — which is the most
  likely way a trade gets narrowed. Caught by the browser test on its first run,
  not by reading the diff.

  No opacity dim on `.trade-verdict.is-stale`: CSS `opacity` creates a group, so
  a child can never be brighter than its parent and the warning would be the
  faintest thing in the block it is warning about. The note is hidden and shown
  by one class instead, so `shortcuts.js` owns no copy of the sentence.

  Two smaller things went with it. `<details {% if trade_result %}open{% endif %}>`
  is **deleted**: after the split nothing renders `trade_panel.html` with a
  `trade_result`, and the element is open by construction — you clicked Evaluate
  inside it. And one of the new endpoint tests had to be rewritten before it
  could fail: slicing the response from `class="trade-verdict` to the end of the
  string passes just as happily with the Execute form moved OUT to a sibling,
  which is precisely the arrangement that would leave the button live. It now
  depth-counts `<div>` to find the element's real extent, and the mutant dies.

  Nothing clears `is-stale` in JS — the next evaluate brings a fresh verdict
  div — so the browser test re-evaluates and requires the note gone and the
  button live again. That half is not decoration either: putting the marker on
  `#trade-panel` instead of on the verdict (and scoping the CSS to match) makes
  it survive the swap, so a freshly evaluated verdict keeps a warning saying it
  is out of date. Measured — that mutant fails on the note, not on the button:
  the disable still runs against the verdict, so the new button comes back
  enabled and only the warning is wrong. A permanent false warning on the one
  control that commits a trade is the failure worth catching.

  Measured against the unfixed build to confirm the browser tests are the proof
  and not decoration: restoring the old target + handler pair fails
  `test_the_selection_survives_an_evaluate`, and removing the marker call fails
  `test_changing_the_selection_marks_the_verdict_stale`.

  Out of scope and still true: an `/assign` returns `all_panels.html` and wipes
  the trade form with everything else. Trades happen during auction breaks per
  the CBA, so that is not the reported problem.

- **Utah's logo was missing on the 2025 pool, because the asset is named after
  the alias.** Reported as "the UTA Logo is missing". `config.NHL_TEAM_ALIASES`
  declares `{"UTH": "UTA"}` — so `UTA` is the canonical tricode, matching
  `team_odds.json` and the NHL itself — but the file on disk was
  `nhl_logos/UTH.svg`, named after the *alias*, and six templates pasted the raw
  CSV value straight into `/nhl_logos/{{ code }}.svg` with nothing resolving
  between the two.

  So which pool you load decided whether Utah had a badge. Measured across the
  three pools carrying an `NHL TEAM` column: `data/players.csv` spells it `UTH`
  on **78** rows and worked by accident; `data/players-25.csv` spells it `UTA`
  on **30** rows and 404'd on every one; `data/players-23-converted.csv` spells
  it `UTH` on 27 and worked. Nothing was wrong with the image — the app was
  asking for a filename that did not exist.

  Fixed by making the canonical spelling the one on disk (`UTH.svg` → `UTA.svg`)
  and putting the resolution in one function, `main._nhl_logo_src`, registered
  as the `nhl_logo_src` Jinja filter and wrapped by a new
  `templates/macros/nhl.html`. That is the `_dom_id` rule applied to a second
  derived string: **no template and no test builds this path any more**, and
  `TestOneLogoPathForTheWholeApp::test_no_template_builds_the_path_itself`
  keeps it that way, because the regression is one template at a time.

  The macro also generalises a guard only `_log_nhl_logo.html` had. A blank
  `NHL TEAM` is real — **3** rows of `players-25.csv` and **7** of
  `players-23-converted.csv`, plus any log record written before the field
  existed — and the other five sites rendered `/nhl_logos/.svg` for it: a broken
  image and one more 404. They now draw nothing, which is what the log fragment
  had always documented as the right answer.

  Two things that look like cruft and are not: `UFA.svg` is the FCHL placeholder
  `players.csv` puts in the NHL TEAM column on 9 rows, and `ARI.svg` is a retired
  club (`convert_legacy_players.py` already renames `ARI`→`UTH` upstream, so
  nothing reaches it) kept because deleting an asset buys nothing.

  The guard that should have caught this could not: `.claude/agents/pre-auction-check.md`'s
  logo sweep hardcoded `data/players.csv`, so it was structurally unable to see
  an alternate pool, and it compared the CSV's spelling to the filenames
  directly, which is the same mistake the templates were making. It now reads
  `data_loader.PLAYERS_CSV` (honouring `FCHL_PLAYERS_CSV`), resolves through
  `NHL_TEAM_ALIASES`, and skips the legacy schema cleanly. Run against all three
  pools after the change: `OK` for 33, `OK` for 32, `SKIP` for `players-23.csv`.

  The real enforcement is a data invariant rather than the runbook:
  `TestLiveDataInvariants::test_every_nhl_club_in_every_pool_has_a_logo` is
  parametrized over **every** `data/players*.csv` with an `NHL TEAM` column, so
  a refresh that changes a club's spelling fails at `pytest` rather than on
  screen. Verified it can fail by moving `UTA.svg` aside: three pools red,
  naming the file and the code.

- **Two grill findings on the same day's batch.** The MILP headline rendered
  two consecutive parentheticals — "Optimal Projected Points: 1230 (your Proj
  in League State) (Cost: $26.5M)" — because the new cross-reference was
  inserted before the cost rather than after it. Moved, and it now reads
  "... 1230 (Cost: $26.5M) — your Proj in League State".

  And the bench cap had no test for the shape it deliberately does not
  enforce: `is_bench` has been serialized since long before the cap, so a state
  file from any earlier build can hold more than `BENCH_SIZE` benched. The cap
  gates transitions only — a tool that refuses to render four hours into a live
  auction is worse than one showing an illegal roster — so an over-cap state
  loads, renders, greys only the Bench buttons, and recovers by activating.
  Verified by hand and now pinned, including the JSON round trip, without which
  the recovery would be undone by the next save.

- **App assets were unversioned, so two shipped fixes were re-reported as
  broken the next day.** "When I move away from the search box, the drop menu
  still displays" and "the Position Filters don't work when the price model is
  open" — both had landed on 2026-09-09, and both reproduced as **working** in
  a fresh browser profile within minutes of being reported. The operator was
  running the previous `shortcuts.js`.

  `StaticFiles` sends `last-modified` and `etag` and **no `Cache-Control`**,
  and RFC 9111 §4.2.2 lets a cache serve such a response under *heuristic*
  freshness without revalidating. The conditional GET works perfectly — `curl`
  with an `If-Modified-Since` returns 304 correctly. The browser simply has no
  reason to make one.

  `_asset_version()` is a Jinja global returning the file's integer mtime, and
  `base.html` appends `?v=` to `style.css` and `shortcuts.js`. A global rather
  than a `_context` key, because `base.html` must not depend on `_context`,
  which `_render` deliberately short-circuits on the hot path (`/find-player`).
  Memoised, so the app pays one `stat()` per asset and a file edited while the
  server runs keeps its old token until restart — which is what `--reload` is
  for, and the alternative is a `stat()` on every render for a number that
  cannot change in a production run.

  **`static/vendor/*` is deliberately excluded.** Those filenames already carry
  their version (`htmx-1.9.10.min.js`), so they are correctly cacheable forever
  and a query string would only defeat that; a test asserts no vendored
  reference grows one.

  The wider lesson, recorded because it cost a day: a bug report that does not
  reproduce in a fresh browser profile is a **cache** report until proven
  otherwise, and the first thing to check is whether the asset carrying the fix
  can be revalidated at all.

- **The bench had no capacity limit, so a team could hold any number of benched
  players.** Reported as "you shouldn't be able to add more than 4 people to a
  bench — the system should make you move someone to active first." There was
  no constant, no counter, no validation and no display: `is_bench` was a bare
  flag on `PlayerOnRoster` and `ROSTER_SIZE` was the only roster number anything
  checked.

  **What this is not.** `is_bench` reaches no engine module at all. The MILP
  selects its own starters (binary `s <= x`, slots capped 12/6/2) and
  `lineup_points` takes the greedy top-k over every roster player regardless of
  the flag — measured, benching a 76-point starter left `current_roster_points`
  at 583, unmoved. So this is a bookkeeping and legality rule for the operator,
  not a fix to a wrong number. The tool was recording an illegal roster shape
  and had no way to notice.

  `config.BENCH_SIZE` is `ROSTER_SIZE - sum(STARTING_LINEUP.values())` = 4,
  derived from the two structural numbers and deliberately **not** from
  `sum(BACKUP_TARGETS)`, which is also 4 but is a soft objective preference the
  MILP is free to deviate from. `TeamState.set_bench()` validates before
  mutating and raises; `TeamState.bench_count` counts `roster_players` only.

  **Two endpoints add to the bench and a cap on the obvious one is bypassable in
  two clicks.** `send_to_minors` and `add_minor_player` force `is_bench = True`
  on the way down — deliberately, so a later recall lands on the bench instead
  of displacing a starter — and `recall_from_minors` never resets it. Measured:
  demote a BOT player and recall him and he is back in `roster_players` with
  `is_bench=True`, bench count 0 → 1. So `recall_from_minors` carries the same
  check, guarded on the flag rather than applied unconditionally: minors loaded
  from the CSV carry `is_bench=False` (0 of 149 at reset), and recalling one of
  those lands him active and must stay legal.

  `POST /toggle-bench` moved from a bare `save_snapshot()` to
  `with _undoable(rollback=False)`. That is forced rather than cosmetic: the
  endpoint can now reject, `save_snapshot()` captures *and* commits, and the
  error path pops from the other end of the chain — so a refusal would have read
  as a no-op while destroying a real undo step. `rollback=False` is correct
  because `set_bench` validates first.

  The Bench button is greyed at the cap with a tooltip naming it; **Activate is
  never greyed**, because it is the way out of a full bench and gating it would
  deadlock the one workflow the cap makes harder — demoting a starter needs a
  free bench slot to stage him in. There is no deadlock: send one of the four
  benched down first.

  Seven mutants checked, each killed by a different test: the cap removed from
  `set_bench`; the recall guard removed; the recall guard made unconditional
  (which is what would break CSV minors); `bench_count` over `all_players`;
  `/toggle-bench` back to a pre-outcome `save_snapshot()`; Activate greyed too;
  and the button never greyed.

- **The forward price curve peaked at 80 points and fell, so the league's best
  forwards were priced below mid-tier ones.** Reported as "Kucherov is still
  priced lower than Panarin, which doesn't make sense" — and he was: on
  `data/players-25.csv`, Kucherov (119pts, rank 1) came out at $4.86M against
  Panarin (94pts, rank 2) at $5.50M. On `players.csv` the same inversion put
  Panarin (120pts) at $5.46M against Marner (85pts) at $8.02M.

  `coef_pts_hinge_80` was −0.0504 against a 60–80 slope of +0.0310, giving an
  effective stage-2 points slope of **−0.0194** above the knot. At rank 10 with
  a $6M lag the curve ran 40pts → $2.51M, 80pts → $6.60M (peak), 132pts →
  $2.44M. Every bid limit and every nomination ranking for the top forwards was
  derived from a price that was too low, and Layer 2 could not correct it
  because the market ceiling only ever caps *downward*.

  Fixed in the pricer repo (`FCHL-auction-pricer` 7becd27) and copied in.
  `constrained_stage2` bounds every points **segment** slope at `>= 0`. The
  constraint cannot be stated in the exported basis — those coefficients are
  slope *increments*, so monotonicity there is a constraint on partial sums and
  no bounded solver takes it — so the fit reparametrises the points columns as
  `min(p,60)` / `clip(p-60,0,20)` / `max(p-80,0)`, which spans the same column
  space (the unconstrained fits are identical), bounds each one, and translates
  back. `projected_points_sq` is now dropped from stage 2 unconditionally
  rather than only when it fits positive: with it left in, the quadratic simply
  absorbed the same downward bend (−0.000077 × 119² is enough), which is the
  first attempt at this fix and why it failed.

  The bound is active **exactly once**, on F's >80 segment, at the −0.019406
  the old fit produced. D and G are byte-identical, as is every `floor_coef_*`
  for all three positions — the check that stage 1 was left alone. Only 12 F
  stage-2 fields move.

  | | before | after |
  |---|---|---|
  | F slope <60 / 60–80 / >80 | +0.0179 / +0.0310 / **−0.0194** | +0.0240 / +0.0422 / **+0.0000** |
  | LOSO R² (above-floor) | 0.848 | 0.843 |
  | LOSO MAE | $0.577M | **$0.574M** |
  | isolated F points-inversions in the pool | **122** | **0** |
  | Kucherov / Panarin (players-25) | $4.86M / $5.50M | **$6.08M / $4.89M** |
  | Panarin / Marner (players.csv) | $5.46M / $8.02M | **$8.09M / $7.73M** |

  **Note the exported `coef_pts_hinge_80` is still negative (−0.0422), and that
  is correct** — it is the increment that takes a +0.0422 segment down to flat.
  It is not a defect to re-fix on the sign.

  **Three things the `BACKLOG.md` entry got wrong**, all of which cost time.
  (1) It called the hinge "over-fitting a thin tail — 8 of 407 forwards clear
  it". That counted the *pool*; the training data has **87 of 605** above-floor
  forwards past the knot, so the tail is not thin and the diagnosis pointed the
  wrong way. (2) It proposed **dropping** the hinge, which was measured and is
  worse: LOSO MAE $0.577M → $0.646M. The hinge is right; only its sign was
  wrong. (3) It deferred on "the fix is not in this repo" — true of
  `price_model.py`, which applies the coefficients faithfully, and false of the
  problem: the pricer repo is on the same machine with the full training data,
  and the owner said so ("you can fix the model"). "Can't" was doing work that
  "haven't" should have been doing.

  The drop rule was **triplicated** across the fit, the LOSO CV and the
  feature-set comparison; all three now call one estimator. Cell 20's comment
  claimed it fit "exactly as deployed", so leaving the copies alone would have
  started reporting cross-validation for a model nobody ships.

  Both `xfail(strict=True)` markers XPASSed and are gone, as their own reasons
  instructed. `tests/measure_drivers.py` now reports zero inversions for all
  three positions, which means a correct instrument and `return []` look
  identical against the live data — so
  `test_the_isolated_measure_finds_the_forward_defect` is replaced by
  `test_the_isolated_measure_fires_on_a_negative_segment`, which restores the
  July −0.0504 hinge on a copy, and F joins D and G in the clean list. Verified
  by mutation.

  Follow-on figures re-measured rather than left to rot: `coef_log_rank`
  roughly halved (−0.391 → −0.196) as signal moved back into Points, so
  `.claude/rules/pricing-pipeline.md`'s driver-mix paragraph is rewritten —
  Points is now the largest driver for **89%** of forwards and for **all 40** of
  the top 40, where the old text said Scarcity "takes over at the top". Clamps
  went 496 → **490 of 705** below `min_bid`, and **one** player now sits *above*
  `max_bid` where none did, making the `"max"` clamp note reachable on a real
  card.

- **The pool filters stopped working whenever a price-drivers card was open,
  and the card's own rows vanished.** Two reports, one bug: "the FDG and
  RFA/UFA don't work when the Price Model window is open" and "sometimes when I
  use the filters and then click the price drivers, the table doesn't show up".

  `#player-chart-container` is *inside* `#bid-limits` and ahead of the pool
  table, so with a chart open the drivers `<tbody>` is the first match for
  `#bid-limits tbody` — and `document.querySelector` returns first in tree
  order. Measured on a live server: filtered to D, 233 of 649 rows visible;
  opening a card left the pool at 233 and hid **all 7 driver rows**, with the
  footer reading "No defencemen left in the pool"; clicking F then changed
  nothing but the footer's wording. The footer is the part that actually
  misleads — on draft day that sentence reads as a fact about the draft.
  `applyPlayerFilters` also ends in `renumberRows`, which overwrote each driver
  group **label** with `1,2,3`.

  "Sometimes" was exact: with both filters on *All* the caller early-returns,
  and any pick re-renders `#app` and heals it.

  The pool `<tbody>` now carries `id="pool-rows"` and all three selectors
  address it by name — `applyPlayerFilters` via `getElementById`,
  `renumberRows` by identity instead of `closest('#bid-limits')`, and the
  `htmx:afterSwap` guard by presence.

  **A third symptom was written up and turned out not to exist.** The plan
  claimed sorting with a card open also wiped the driver labels. It does not:
  `sortTable` hands `renumberRows` the tbody it just sorted, so the sort path
  never sees the drivers table. A browser test driven through it passed against
  the *unfixed* build, so the test was deleted rather than shipped as coverage,
  and the comment in `shortcuts.js` now names the path that actually reaches it.

  Fixing this broke `tests/test_endpoints.py`'s sortable-column scraper, which
  matched a literal `<tbody>`: the new id silently dropped the pool table from
  its results, and with it the only column on the page that relies on the
  `img[alt]` sort fallback — exactly what
  `test_the_alt_fallback_is_what_rescues_the_logo_columns` exists to notice.

- **Eight plainly-typed surnames found nobody, because the name fold kept
  punctuation.** Shipped in the first cut of the search engine and caught by a
  design review the same day. The fold lower-cased and stripped diacritics but
  left punctuation in place, so the operator had to reproduce `players.csv`'s
  spelling exactly. 24 pool names carry a non-letter — apostrophes, thirteen
  hyphens, the parentheses `_disambiguated_names` adds — and, measured, **four
  names encode the hyphen as the digit `0`**: `Oliver Ekman0Larsson`,
  `Nicolas Aube0Kubel`, `Alex Barre0Boulet`, `Trey Fix0Wolansky`. So `oreilly`,
  `kandre`, `ekman-larsson`, `ekman larsson`, `barre-boulet`, `fix-wolansky`,
  `aube-kubel` and `nugent hopkins` all returned **zero hits** for players who
  are in the pool right now, and one of them asked the operator to guess a
  data-entry bug.

  `_fold` now returns letters only plus the offset each word starts at, and a
  prefix is matched at any of those offsets. **The offsets are the load-bearing
  half**, not the letters: the fold concatenates, so `"alexbarreboulet".split()`
  is a single token and matching a split list would find him only in the
  substring tier, below anyone who merely contains the query. `isascii()` now
  guards the NFKD sweep, which is pure cost on today's pool (zero non-ASCII
  names) — the whole fold is 0.455ms unmemoized against the old 0.770ms, and a
  warm query is 0.21ms.

- **The search buried the surname you typed.** A full-name prefix was its own
  top tier, which looks like the stronger match and is not: with no space in the
  query it only means "his FIRST name starts with this". Measured over the live
  pool, `ma` returned MacKenzie Entwistle, MacKenzie Weegar and Mackenzie
  Blackwood while hiding Nathan MacKinnon, Auston Matthews and Cale Makar behind
  "+127 more"; `hu` led with Hudson Fasching and buried every Hughes. Two tiers
  now — prefix anywhere, then substring — ranked within each by projected points
  then name. A query containing a space still matches as a full prefix, so
  collapsing loses nothing. Points rather than the alphabet because ten of a
  hundred matches get shown and alphabetical order is arbitrary at that ratio
  (`smi` led with Brendan over Reilly Smith). A bought-out subject is a
  `TransactionRecord` with no points and therefore sorts last in its tier — a
  decision, not an accident, and `getattr` is where it lives.

- **The navbar pushed its last control off-screen at 1024px once the search box
  was added.** The title's flex item could not shrink below its own content
  (css-flexbox §4.5), so the 216px input's cost landed on the `flex-none` group:
  measured, the Reset button left the viewport and "FCHL Auction Manager" wrapped
  to three lines inside a 45px bar. Fixed with `min-w-0` on the title group and
  `truncate` on the title itself — **both halves are needed**, because shrinking
  the box does nothing while the text still wraps — plus a narrower input.
  `TestTheHeaderSearchLandsWhereItPoints` pins it at 1024 and 1280 by geometry
  rather than a pixel constant.

### Investigated

- **"Does it matter whose turn it is to bid? All that I think matters is that
  if it's mine or if it's someone else's. Is that right?" — yes, and no code
  changed.** The answer is written into
  `.claude/rules/pricing-pipeline.md`'s Critical rule section, where the two
  ceiling contexts already live, because it is a domain fact that keeps being
  re-derived.

  `current_nominator()` is read in exactly one place — `nomination_panel.html`,
  where it draws a badge and gates the "It's My Turn" button. It appears
  nowhere in `market.py`, `optimizer.py`, `/bid-check` or the MILP. `/assign`
  only *advances* it, and `/set-nominator` is the single mutating POST that
  deliberately skips `_recompute()`, on the grounds `tests/test_bid_cache.py`
  states outright: a marginal value cannot depend on whose turn it is.

  What the engine reduces a bidder set to is the boolean "is BOT one of them"
  and the **sorted multiset** of the other bidders' `physical_max_bid` —
  `compute_live_ceiling` projects codes to numbers and sorts, destroying both
  order and identity. So opponent identity matters only as a way to look up a
  budget, permuting the same opponents changes nothing, and opponent roster
  *needs* never gate a bid at all (`_bidding_opponents` gates on
  `physical_max_bid`, not on spots remaining — a 24-man team with cap space is
  still a bidder). `nomination_order` *is* read by four templates, but purely
  as a stable display order.

  One thing the question surfaced went to `BACKLOG.md` rather than being
  fixed here: the `highest_bidder` form field on `/bid-check` is dead — no JS
  writes it, and `compute_bid_recommendation` never reads the `MarketInfo`
  field it feeds.

- **"When I move away from the search box, the drop menu still displays" —
  no code change.** The `focusout` handler at `static/shortcuts.js` already
  closes the results, and does it correctly: it ignores a focus move to another
  element *inside* `#player-search`, so tabbing from the input to a result does
  not dismiss what you are reaching for. Verified in a fresh browser — the
  results container went from 1 child to 0 on clicking away. See the
  cache-busting entry above for why it looked broken.

- **"The Position Filters don't work when the price model is open" — no code
  change.** Fixed on 2026-09-09 by giving the pool `<tbody>` the id
  `pool-rows`, because `#player-chart-container` sits inside `#bid-limits` and
  before the pool table, so `document.querySelector('#bid-limits tbody')`
  resolved to the price-drivers table once a chart was open. Verified in a
  fresh browser at 1280px: with a drivers card open, filtering to D took the
  pool from 614 rows to 227 and left all 6 driver rows on screen with their
  labels intact. Same cause as above.

- **The team panel's "Proj PTS" was a raw sum including bench players, so it
  was not a legal lineup.** Reported as "in League State it says Proj.
  Est/Solved. But then in the Team Panel it also shows Proj PTS. These values
  are different." They were — and the panel's was the wrong one.

  Three figures read as one and only one was mislabelled *and* miscomputed:

  | Where | Quantity | Was |
  |---|---|---|
  | League State **Pts** | `current_roster_points` — best 12F/6D/2G right now | correct |
  | League State **Proj** | optimal points once the roster is filled | correct |
  | Team panel **Proj PTS** | `roster_players\|sum('projected_points')` | **wrong** |

  Bench players score nothing under league scoring — `lineup_points` takes the
  greedy top-k per position for exactly that reason — so summing every roster
  player counts phantom points from anyone past 12F/6D/2G. It agreed with the
  truth while rosters were small enough that everyone starts, which is why it
  shipped unnoticed and bit only at the end of a draft, when the number is read
  hardest: measured on `full-roster-still-bidding`, MAC showed **901** against
  a real **896** and HSM **1140** against **1121**.

  The tile now shows `current_roster_points` and is labelled **Lineup PTS**.
  The rename is half the fix: "Proj" already means the optimal FILLED roster,
  both in League State's column and in the headline two lines below the tile,
  and a third meaning on the same screen is what the report was about.

  League State's own headers deliberately did **not** change. Its `<th>`s sit
  in the widest table in the app and DaisyUI applies `white-space: nowrap` to
  header cells, so a header's min-content is its whole string — measured on
  this same table, one extra word in the basis marker cost 53px. The team panel
  has no such constraint, so that is where the vocabulary got fixed.

  The MILP headline also gained "(your *Proj* in League State)" and switched
  from `"%.0f"` to `|int`, matching `_context` — **consistency, not a bug**:
  `MILPSolution.total_points` is declared `float` but is always fed from
  `state.lineup_points`, which sums ints, and measured across all six scenarios
  the two formats never disagree. The mutation swapping them back survives, and
  the template comment says so rather than implying coverage it does not have.
  What is pinned is the cross-panel equality, compared as **rendered** — going
  through `_context` instead would have passed on a build where one panel
  showed something else entirely.

- **htmx does NOT abort an in-flight request whose triggering element leaves the
  DOM.** This claim has been in `CLAUDE.md`, in two comments in `shortcuts.js`
  and in a browser-test docstring since 2026-08-14, always as the reason
  dismiss-on-interaction runs on `htmx:afterRequest` rather than on click. It was
  reasoned from the minified source and never run. Measured 2026-09-10 in Chrome
  against the vendored htmx 1.9.10: removing a trigger element during
  `htmx:beforeSend` fires no `htmx:abort` and **the swap lands anyway**. Reading
  `b.onload` in the bundle says why — it calls the swap `M(n,I)` unconditionally
  and only then fires events, re-firing them on the nearest surviving ancestor
  via the `if(!se(n))` branch (which is a real constraint: such a handler must be
  idempotent). It surfaced because a `click`-time clear was run as a mutant
  against the new player-search browser tests and **all three passed**.

  No code changed. `afterRequest` is still right, for a reason that was always
  the real one and was standing beside the wrong one: `event.detail.successful`
  is only knowable after the response, and a failed request must leave the card
  on screen. All four sites were rewritten to say that instead, and
  `TestMidBidClutterCanBeDismissed`'s docstring no longer claims its bid-panel
  assertion "catches the abort" — it separates "the card went away" from "the
  card went away and the request still landed", which is a weaker property and
  the one it actually has.

- **`league_state.html`'s min-content note was right about the conclusion and
  wrong about the reason**, corrected in place. "Only shorter headers can
  narrow this" is true; "because min-content is each column's longest word" is
  not, and the two come apart exactly when a header holds more than one word.
  Also recorded there: the 768/776/829 run does not reproduce the 815px figure
  measured 2026-09-07, and no attempt is made to reconcile them — the pool
  changed that same day and figures from different runs are not comparable.
  Compare variants inside one run.

## [2026-09-09]

### Added

- **The Price Model card now says WHY, not just what.** `BACKLOG.md`'s
  "decompose Model $ into its drivers — how much comes from projected points vs
  NHL team quality" is closed. `price_model.decompose_price` splits the stage-2
  median against a per-position reference player:

      log_mu(player) = log_mu(reference) + SUM_groups SUM_keys coef_k * (x_k - r_k)

  exactly, because both sides are the same linear form. Verified over all 705
  pool players: worst reconstruction error 6.7e-16.

  **The entry's open question was answered by neither of its two options.** It
  asked whether to show the contributions "in log space or as % of predicted
  price". Measured: *any* per-row dollar or percent attribution is
  **order-dependent** in a multiplicative model, because a row's dollar step is
  `exp(running + delta) - exp(running)` and moves with where the row sits.
  Across all 120 orderings of the five groups, McDavid's Points step runs
  $0.11M to $2.72M and Scarcity's $2.06M to $8.50M — every one arithmetically
  correct, which is what makes displaying one a trap, since the figure gets
  quoted. Only `exp(log_delta)` is invariant, so the card shows a base price,
  per-row multipliers, and a final price, with no intermediate dollar column.
  `test_the_factors_do_not_depend_on_their_order` asserts the hazard is real
  before asserting the factors are immune, because the dollar column is the
  obvious "improvement" someone will reach for.

  **Five drivers, not the three the entry named.** `log_rank` and `is_rfa` are
  far too large to bucket as "other" — Scarcity is F's second-largest mean
  effect (0.268 against Points' 0.324) and dominates at the top of the pool
  (McDavid x7.97 against Points x1.39). Note `pos_rank` is derived FROM
  projected points, so Points and Scarcity are collinear by construction and
  the card's Scarcity tooltip says to read them together. `log_lag` and
  `has_lag` are one group: split apart, every player new to the league shows a
  spurious negative "reputation", because `log_lag` is ln(MIN_SALARY) for him
  and not 0.

  The breakdown explains the **median** and says so. `expected_price` is not
  decomposable the same way — P(floor) is a separate logistic whose
  coefficients frequently point the other way (F's `floor_coef_log_rank` is
  +3.006 against `coef_log_rank` −0.391), sigma is a nonlinear function of
  `log_mu`, and the clip bounds are per-position (`max_bid` 11.4 F / 8.5 D /
  10.5 G, **not** `config.MAX_SALARY`). The card states the E[$] arithmetic
  instead of attributing it. The clamp is the common case rather than an edge:
  496 of 705 pool players have an unclamped median below their position's
  `min_bid`, 0 above `max_bid`.

  Supporting changes: `build_features`, `_score` and `_player_inputs` extracted
  from `predict_price` so the formula exists once (the golden test's 139
  predictions still reproduce, which is the proof); `AuctionState.price_reference`
  freezes the reference at draft time, like `pos_rank` and for the same reason —
  recomputed against a drafted-down pool it moves F −0.194, D −0.139, G −0.794
  in log_mu; and `tests/measure_drivers.py` answers the same question in bulk.

- **`tests/measure_drivers.py`** — the pool-wide view: slope per segment, the
  price/points curve, driver mix per position, the top N by price with their
  factors, inversions, and what the negative F hinge costs. Imports
  `data_loader` and `price_model` only, so like `measure_spend.py` it
  structurally cannot touch a live draft.

  **It reports two inversion measures and keeping them apart is the point.**
  The first draft reported only "more points, cheaper overall" and called that
  the defect. It is not — with five drivers, a lesser scorer on a better team
  with a bigger reputation *should* cost more, and D has 898 of those with a
  points slope positive at every segment. Isolating the Points driver reads 259
  for F and **zero for D and G**. Both measures also rank on the input the
  position is actually priced on: ranking goalies by their 2W+3SO composite
  while reading a wins-driven factor manufactured 355 goalie "inversions" out
  of nothing. Both bugs were caught by the companion test, which is the
  argument for `test_measure_spend.py`'s convention of giving an instrument
  one.

### Changed

- **The League State panel drops two readouts.** The "Market ceiling: $X.XM (N
  active bidders)" alert below the table was the only place `market_info`
  reached the screen, so the context key went with it; `compute_market_ceiling`,
  the `MarketInfo` dataclass, the `main.market_info` global and every engine
  consumer stay untouched, as does the bid panel's own ceiling line, which comes
  from `bid_advice.market_ceiling` — a different source. And the small grey
  `estimated` / `exact n/m` basis line under the **Proj** header is gone, along
  with `standings_basis.html`, both its includes and the `standings_basis` dict.
  `GET /solve-standings`, `exact_projections` and the Solve Standings button all
  remain.

  Removing the marker meant re-pointing three tests, and two came back stronger
  than the string they had been reading. The OOB fragment count loses its `+ 1`
  (the marker was the twelfth fragment) rather than being loosened, since the
  count is what stops a fragment silently ceasing to be emitted.
  `test_the_proj_column_falls_back_to_its_estimate` grepped for the literal word
  "estimated"; it now compares the swapped figures against a freshly rendered
  page, which closes a vacuous pass — a response carrying no figures at all
  satisfied its old check.

- **Minors that count against the cap are coloured.** A group 2/3 minor's salary
  is fully on cap and the table said so only in the On Cap cell's Yes/No, which
  is unscannable in a table that is mostly the other kind — at reset the whole
  league has 4 cap-counting minors against 145 that are free. The row now takes
  `text-warning` and drops the `opacity-70` every other row carries. Keyed on
  `counts_on_cap`, the same property the On Cap cell reads, so the colour and
  the word cannot disagree — and that covers group 2, which a literal
  `group == "3"` test would miss. Two colours were unavailable for reasons that
  are not taste: `italic` is asserted absent from an opponent's whole panel by
  the roster-key test, and opponents render this table; `text-success` means
  "bought at auction" and is asserted as such across a bench/minors/recall round
  trip. The opponent test supplies its own group-2 minor rather than finding
  one, because all four of the league's cap-counting minors are currently BOT's
  — with the data supplied, four mutants each die on a different test; without
  it, two survive.

### Investigated

- **The forward price curve peaks at 80 projected points and falls — and it is
  not a bug in this repo.** Chased because the owner did not trust the baseline
  Model $ figures on the panel. He was right. F's stage-2 points slope is
  +0.0179 below 60, +0.0310 from 60, and **−0.0194 above 80**, because
  `coef_pts_hinge_80` is −0.0504. Past the knot every additional point *lowers*
  the predicted price: at rank 10 with a $6M lag, 40pts → $2.51M, 80pts →
  $6.60M, 132pts → $2.44M. On the live pool that puts Panarin (120pts) at
  $5.46M against Marner (85pts) at $8.02M, and Forsberg (94pts, $5.10M) $4.17M
  below Barkov, who sits exactly on the peak at 80pts.

  Closed here with **no code change to the model** because `price_model.py`
  applies the coefficients faithfully — the golden fixture reproduces the same
  inversion, so the defect is in the fit the pricer notebook exported, and
  `data/model_params.json` may not be hand-edited. Filed as an open
  `BACKLOG.md` finding against a refit, with the measurement attached so nobody
  re-derives it: 259 isolated forward inversions, zero for D and G, and $27.8M
  of model price suppressed across the 8 forwards past the knot.

  **Why the suite was green on it for two months.**
  `test_forward_hinges_flatten_slope_past_80` asserted only that the slope past
  80 is *damped* relative to the slope below 60 — which a negative slope
  satisfies — and `test_higher_points_generally_higher_price` compared 40pts
  against 90pts, straddling the peak, where the curve is still net-up. The word
  "generally" was load-bearing. Both are now backed by `xfail(strict=True)`
  guards asserting the real property, so a corrected export fails the suite as
  XPASS and forces the markers and the backlog entry to be deleted together;
  verified by simulating a refit. xfail rather than asserting the current
  reality, because asserting it would lock the defect in and make the fix read
  as a regression.

## [2026-09-07c]

### Added

- **`convert_state_json.py` — the 2025 pre-draft pool**, from
  `data/25-26 Starting Rosters.json`, an auto-save of the Streamlit tool this
  app replaced, written 2025-09-05. It is the only genuine pre-draft snapshot we
  have that carries **that season's own projections**, which is what makes it
  worth a second converter: the 2023 pool is a true pre-draft state too, but its
  `PTS` are three years stale, so its prices cannot be compared to anything that
  actually happened.

  Converted it gives 649 available players for **137** open roster spots — the
  league workbook records **139** actual picks that year — with all 11 teams able
  to fill a roster and a spread from LGN (3 spots, $6.3M, physical max $5.3M) to
  MAC (18 spots, $41.7M). The 20 RFAs match the workbook's `2025` sheet exactly,
  which is the strongest available check that the file is what it claims to be.

  **`STATUS` is the trap again, wearing a different hat.** The state file writes
  `"NO"` where canonical is blank; `load_players` gates the biddable branch on
  `status == ""` and UFA/RFA are in `_PLACEHOLDER_TEAMS`, so an untranslated row
  matches neither branch and is dropped in silence — all 651 free agents gone,
  and an empty pool looks exactly like a finished draft. Identical in kind to the
  2023 file's `"0"`, which is why the new script imports `_require_biddables`,
  `valid_nhl_teams`, `league_team_codes` and `CANONICAL_COLUMNS` from
  `convert_legacy_players` rather than growing a second copy of each.

  Four things it deliberately does not import. `BID` is zeroed — the file carries
  a *predicted* price on 145 players from the old tool's Z-score model, the model
  CLAUDE.md names as the reason this app was written, and importing it would
  preload the pool with bids from the thing being replaced. `PRIOR FCHL TEAM`
  stays blank, because there is no column and it is not derivable (the workbook's
  `Intro` is the nominating team, not the holder). `Dobber`/`DtZ`/`Z-score`/
  `Draftable` are dropped. And `GROUP` passes through **untouched, including two
  rows in group `F`** — absent from the documented vocabulary but in none of
  `RFA_GROUPS`, `MINOR_CAP_GROUPS` or `BUYOUT_ELIGIBLE_GROUPS`, so it already
  behaves like the A-E family; remapping it to a letter we recognize would invent
  a contract the source does not record.

  Unlike 2023 this needs no NHL-team join (the column is populated) and triggers
  no name disambiguation — the old tool had already split `Sebastian Aho (F)`
  from `Sebastian Aho (D)`, so the `#data-warning` banner stays silent. Pinned,
  so a future collision is a deliberate change rather than a draft-night
  surprise. Three rows carry the FCHL placeholder `UFA` in the NHL TEAM column
  and are blanked by the same guard the legacy converter needed.

  All six converter mutants (status translation, NHL validation, BID import,
  unknown-team hold-back, GROUP remap, PRIOR fill) are killed by their own
  targeted test rather than only by the artifact-drift guard, which is the
  distinction that makes the drift guard not a false comfort.

### Fixed

- **An RFA with no prior team now still gets a badge.** The Available Players
  table rendered `prior_fchl_team` bare inside a `badge-warning`, and that field
  is filled for 22 of 2158 rows in `players.csv` and for **none at all** in
  either alternate pool — neither the 2023 legacy schema nor the 2025 auto-save
  has a column for it. So both rehearsal pools painted an empty orange box on
  every RFA row, which reads as a broken template rather than as absent data, and
  left an RFA indistinguishable from a UFA in the one column that distinguishes
  them. That is not only cosmetic: a nomination turn is 1 RFA + 1 UFA. Falls back
  to the literal `RFA`, keeping the job that must not degrade.

### Investigated

- **A screenshot of the workbook's `2025` sheet could not be used to rebuild the
  2025 pool, and the reason is worth keeping.** The sheet is a record of a
  *completed* auction — every one of its 139 rows has a winner and a price — so
  its 20 RFAs are all under contract today (Rantanen on GVR, Matthews on HSM),
  and applying it would have marked rostered keepers as free agents while wiping
  the 22 real RFAs, each of which carries a `PRIOR FCHL TEAM` for ROFR. The
  decisive point is narrower than "it is last year's data": the sheet lists the
  players who **were drafted**, not the pool they were drafted **from**. Making
  only those available would leave 139 players for 137+ open spots, so the MILP's
  `== spots` constraint returns Infeasible league-wide and the tool has nothing
  to say. A pool needs a surplus — the converted file gives 4.7:1, and
  `test_there_are_more_players_than_spots_to_fill` now pins that as an invariant.

## [2026-09-07b]

Four findings from draft-day testing. Two were questions with answers rather
than defects, and the answers are the deliverable; two were real.

### Changed

- **The empty Counterfactual panel now disappears instead of showing a bare
  heading.** There are two mounts and only one carried the `<h2>`: the bid
  panel's inline card (`#bid-counterfactual`, `hx-trigger="load"`) has none,
  while `#explanation` is in the left column on every page load with the heading
  rendered unconditionally. So during an auction the auto-opened card sat beside
  a second panel showing nothing but the word "Counterfactual" and a "click ?"
  prompt. A populated card already names the player in its own `<h3>`, so the
  generic title was a second heading saying less.

  `hidden`, never removed — every "?" link is `hx-target="#explanation"
  hx-swap="outerHTML"`, and an outerHTML swap into a missing target is a dead
  swap the panel could not come back from. Same rule as `buyout_scan.html`: only
  the visibility is conditional, never the element.

  This also fixes a **close-button orphan**. The prompt was behind `{% if not
  counterfactual %}`, evaluated server-side, so it was absent from a populated
  response and closing the card left a visible empty box under a heading. The
  close button now re-hides the section, guarded with `if(s)` because the same
  body is mounted inside the bid panel where there is no `#explanation`
  ancestor. Still `closest()`, never `getElementById`.

  **A `[hidden]{display:none!important}` was added to `style.css` for this and
  then removed, because measuring said it did nothing.** The reasoning that
  motivated it is sound and wrong: `.card` sets `display: flex`, and an author
  rule beats the UA stylesheet whatever the order. But the vendored Tailwind
  preflight ships its own `[hidden]{display:none}` and is injected after the
  DaisyUI link, so it wins the equal-specificity tie on source order. Measured
  in Chrome — the section computes to `display: none` with or without the rule.
  Caught by mutation testing: deleting the rule killed no test, which is the
  signal that the rule was carrying nothing. The comment shipped with it
  asserted the opposite as fact and would have been believed.

- **"Marginal" is shown only when it differs from "Worth up to".** They were the
  same number in every normal state: `value_cap = round(min(marginal,
  physical_max_bid), 1)` and `compute_marginal_value` is itself bounded by
  `physical_max_bid` at four of its five exits. So the panel printed one figure
  under two labels with two tooltips describing different concepts.

  The one reachable divergence is an over-committed team, where
  `physical_max_bid` floors to $0.0–0.4M while the marginal falls back to
  `MIN_SALARY` — max gap $0.5M, and the panel already reads "can't bid". That
  regime had **no test at all**; it does now, both halves. Compared as
  DISPLAYED (`"%.1f"|format` on both) rather than as stored, since `value_cap`
  is pre-rounded and `marginal_value` is raw — the same rule as
  `market.is_capped`. The figure is now formatted too; it printed a raw float.

  Making it conditional **failed the tooltip-containment suite**, which is what
  that suite is for: `TestTooltipsStayInsideTheirPanel` holds a `required` map of
  tooltips it must actually observe, so a template that stops rendering one fails
  loudly instead of quietly measuring less. The cheap response — delete the
  entry — would have dropped the bubble from coverage on the very day it became
  the harder one to reach. Instead the class gained an `OVER_COMMITTED` state: a
  `helpers.squeeze` on BOT rather than a scenario, because no scenario reaches
  that regime. One width (1024, the tightest 3-col track), and it asserts
  `physical_max_bid < MIN_SALARY` first so a squeeze that missed cannot be
  reported as a missing tooltip.

- **The Spendable column is gone from League State.** Display only — the
  `spendable_budget` property stays, since `TeamState.physical_max_bid` is built
  on it. The team-panel *tile* stays as well.

  Renumbering `data-sort-col` was the risk, not the deletion: `sortTable`
  indexes `a.cells[col]` directly, so a stale index does not fail — it silently
  sorts a **different** column while the arrow appears over the one clicked. A
  new guard asserts every `data-sort-col` equals its header's own position, in
  both `any_penalties` states, which is the only thing that could catch it;
  `test_every_row_has_a_cell_for_every_header` pins counts and stays correct
  under an offset.

  **The width note was re-measured, not recalculated**, and it is a good thing:
  the per-column decomposition in `BACKLOG.md` puts Spendable at 100px, so
  subtracting predicted ~768px, and the browser said **815px** (fresh state, no
  penalty column, `tests/measure_layout.py`). Both records now say the
  decomposition explains why headers are the floor and is not an additive model.

- **The Penalty tooltip no longer asserts a buyout.** `team_panel.html` said
  "Buyout penalty: 50% of bought-out player's salary stays on the cap" — untrue
  in the two squeezed scenarios, and untrue for JHN and LGN, whose $0.3M comes
  from `fchl_teams.json`. It now says what the number is (dead cap, no player
  attached) and names buyouts as the usual source rather than the only one. The
  League State column needed no change: its header is the bare word "Penalty"
  with no tooltip, so it claims nothing.

### Investigated

- **Why scenario penalties are $9–11M on every team: by design, not a bug.**
  `scenarios._squeeze` *assigns* `penalties` as a dead-cap lever so
  `physical_max_bid` lands on a target, and it replaces rather than adds — it
  discards JHN's and LGN's real $0.3M. Only `drained-late-draft` and
  `full-roster-still-bidding` call it, and both squeeze all 11 teams including
  BOT; the other four scenarios leave penalties untouched. The band is the
  measured one `scenarios.py` documents: draining to $12.0M needs $9.0–11.0M of
  dead cap per team. No buyout path runs during scenario construction — three of
  the four `penalties +=` sites operate on deep copies — and the $M formatting is
  correct. Confirmed against a fresh state, which reads $0.0M everywhere. The
  only thing wrong was the tooltip, fixed above.

## [2026-09-07]

### Added

- **An alternate player pool, selectable by environment variable, with its own
  saved state.** `data/players-23.csv` is the 2023-season snapshot committed in
  the very first commit (`98e9908`) and referenced by no code, test or doc since.
  Loading it needed three things.

  **A converter, because it is a different schema.** The legacy header is
  `Player,Pos,Pts,Team,Status,Salary,Bid` against the canonical
  `PLAYER,POS,GROUP,STATUS,FCHL TEAM,NHL TEAM,AGE,SALARY,BID,PTS,PRIOR FCHL TEAM`.
  A column rename alone is not enough and fails **silently**: the legacy file
  writes `"0"` where the canonical one leaves `STATUS` blank, while
  `load_players` gates the biddable branch on `status == ""` and `UFA`/`RFA` sit
  in `_PLACEHOLDER_TEAMS` — so all 674 free agents match *neither* branch and are
  dropped without a word. An empty pool is indistinguishable from a finished
  draft on screen. `convert_legacy_players.py:295 (_require_biddables)` refuses
  to write that file at all, and `tests/test_legacy_conversion.py` asserts both
  halves: the converted file loads 668 biddables, and the same rows with `"0"`
  restored load none.

  **Synthesized contract groups, because the legacy schema has no `GROUP`
  column.** `UFA -> 3`, `RFA -> RFA2` (only `is_rfa` is read downstream, and the
  two RFA groups are equivalent for auction purposes), active keepers `-> 3`, and
  minor-leaguers `-> A`. That last one is the decision that moves money: group A
  is outside both `MINOR_CAP_GROUPS` and `BUYOUT_ELIGIBLE_GROUPS`, so those
  salaries stay off cap. It matches the shape of the current file, where 145 of
  149 MINOR rows are `A`-`E`, and these legacy minors are almost all $0.5-0.7M.
  Asserted against the two config sets **separately**, since `config.py`
  documents at length why they must not be merged.

  **31 rows on team code `ENT`** — that season's entry-draft class — are held
  back and named on stderr. `build_initial_state` already ignored them, because
  it only builds a `TeamState` for codes in `fchl_teams.json`, but it did so in
  silence; a real data decision should not be invisible.

  Measured on the converted pool against today's: 668 biddable against 705, 164
  picks needed against 165, mean model price $0.83M against $0.90M. All 11 teams
  clear the commissioner reserve rule (`remaining >= spots * MIN_SALARY`); BOT
  sits at $45.5M for 17 spots. **Max model price is $6.12M against $9.55M**, and
  that gap is expected rather than a bug: no biddable in the legacy file carries
  a prior salary (0 of 668 against 324 of 705), so `has_lag` is 0 pool-wide, and
  there is no `NHL TEAM` column, so every player gets `DEFAULT_TEAM_PROBABILITY`.
  Two of the price model's ten features go flat. Neither is recoverable from any
  file in the repo. `AGE` is absent too and costs nothing — it is not a model
  feature, confirmed against `model_params.json`'s coefficient keys.

- **`FCHL_PLAYERS_CSV` and `FCHL_STATE_DIR`.** `data_loader.PLAYERS_CSV` reads
  the pool path into a module global rather than a default argument — a default
  binds at import and could not be monkeypatched — and `load_players` /
  `build_initial_state` resolve a `None` sentinel against it, so an explicit path
  argument still wins.

  **The state directory follows the pool by construction**, which is the
  safety-critical half. `lifespan` reads the saved state *before* it ever reads a
  CSV, so booting an alternate pool against `data/state/` would load the real
  draft's JSON, backfill it from the wrong CSV, and then save over it — the same
  write-through that `tests/conftest.py` was written to stop pytest doing. So
  `main.py:77 (_default_state_dir)` derives `data/state-<stem>` for any
  non-default pool, rather than leaving it to a second variable the operator has
  to remember; `FCHL_STATE_DIR` overrides it explicitly.
  `main.py:138 (_backfill_nhl_teams)` and
  `main.py:162 (_backfill_keeper_flags)` follow the
  same global instead of hardcoding `data/players.csv`, and startup logs the pool
  and the directory together, because a mismatch between them is otherwise
  silent. `.gitignore` widened from `data/state/` to `data/state*/` to cover the
  derived directories.

  `TestDataFingerprint` is unaffected: the default is unchanged. It reads the
  global, so running pytest with `FCHL_PLAYERS_CSV` exported fails it — correct,
  and loud.

- **NHL teams joined into the converted legacy pool.** The legacy schema has no
  `NHL TEAM` column, so every player was pricing at `DEFAULT_TEAM_PROBABILITY` —
  one of the price model's ten features flat across the whole pool, and a blank
  column on screen. The only source in the repo is the current `players.csv`
  (the draft-results xlsx has pick/winner/salary/term and no club), so the
  converter joins it by normalized name: **869 of 876 rows**, 32 clubs, 25
  distinct probabilities where there was 1.

  Matching needed three normalizations, and two undo damage in `players.csv`
  rather than era drift — the legacy file writes a **backtick** for an
  apostrophe, while the pool file renders `-` as `0` and `ari` as `UTH`. Those
  are filed as an open finding; the converter works around them for matching and
  deliberately does not rewrite the operator's pool file.

  Two guards, both mutation-tested. **A name two players share resolves to
  nothing** — the candidate set is kept rather than collapsed, so the known
  collisions are refused instead of coin-flipped onto a real player. **Only real
  NHL club codes are written**: `players.csv` carries the FCHL placeholder `UFA`
  in its NHL TEAM column on 9 rows, invisible to pricing (an unknown code falls
  through to the default) but rendered as a club and indistinguishable from data
  once written to a pool file. That guard is why coverage is 869 and not 873 —
  four rows whose only source is a `UFA` row now stay blank, correctly.

  These are **present-day** clubs; a player traded since 2023 gets his current
  team. Coherent rather than a compromise, since `team_odds.json` is present-day
  too. Max model price moves $6.12M -> $7.03M.

  `convert_all` is now the single entry point for the pipeline. The test fixture
  called `convert` alone and so produced rows without the join while the script
  wrote rows with it — the guard comparing the committed file against a fresh
  conversion failed on its own fixture rather than on the artifact.

### Fixed

- **The startup line naming the pool and the state dir printed nothing.** It was
  logged on the root logger, which uvicorn leaves at WARNING while configuring
  only its own — so the call read as correct in the source and emitted nothing in
  a real run. Measured against a live server: four uvicorn startup lines, and
  none of ours. It now goes through `uvicorn.error`. Caught only because the
  claim "logged at startup" was checked against a running server rather than
  against the source, which is the only place that difference is visible.

  Its test attaches a handler to that logger **directly** instead of reading
  `caplog`, which sees records only by propagation to root. Uvicorn's own config
  marks the parent `uvicorn` logger `propagate: False`, so once any test starts a
  real server — `test_browser_ui.py` does — those records stop reaching root. The
  caplog version passed alone and failed in the full suite, for a reason that had
  nothing to do with what it checks.

## [2026-08-21]

### Changed

- **A filter combination with no matches now says so.** Available Players
  rendered its headers and an empty body, which reads as a broken panel rather
  than an empty result — and nothing in the code knew: `applyPlayerFilters`
  wrote `display` on every row and counted nothing. Reachable in a real draft,
  which is how the test reaches it: **G + RFA once the last restricted goalie
  sells** (measured on a fresh pool — 4 restricted goalies, 2 restricted
  defencemen, 16 restricted forwards).

  Not a Jinja `{% else %}`: the filtering is entirely client-side and
  `bid_limits` is never empty server-side. The row is filled and toggled by JS,
  and it **names the combination** ("No RFA defencemen left in the pool.")
  rather than saying "no matches" — the filter buttons already show which
  filters are on, so what the operator needs is the table confirming it agrees.

  It lives in **`<tfoot>`**, not as a `<tr>` in `<tbody>`, because everything
  that walks the body treats a row as a player: `sortTable` would read
  `cells[col]` off a single colspan cell and shuffle the message in among the
  players, and `renumberRows` would overwrite it with "1". A row that is not data
  does not belong in the body, and tfoot means none of the three needs an
  exception for it — moving it back into `<tbody>` kills **three** tests,
  including the pre-existing partition test that counts rows.

  A `renumberRows` selector narrowed to `tr[data-position]` was written as a
  second line of defence and then **reverted**: with the row in `<tfoot>` the
  mutant survived every test, because `tbody.querySelectorAll('tr')` cannot see
  it. Same rule as the grid fix's `min-width: 0` — one mechanism, not two, and
  the placement is the mechanism. Six tests in
  `TestAvailablePlayerFilters`, five mutants killed.

- **The roster panel now says what an italic blue row means.** The panel's most
  valuable output — "these are the players to buy" — was conveyed entirely by
  `text-info opacity-50 italic` plus a missing Actions cell, with no legend, no
  header and no per-row marker. An operator who did not already know the
  convention had no way to tell a MILP target from a player they own.

  Three states are rendered, not two, which is the correction to the original
  finding: a target, a player *drafted this auction* (`text-success`), and a
  keeper carried over (no class at all). Naming only the blue rows would have
  left green-vs-plain as a second undocumented convention, so the key names all
  three.

  Each label **wears the class it describes** rather than sitting beside a colour
  swatch — which is where this departs from `bid_limits.html`'s legend grammar
  deliberately. Those are text colours, so a filled square would advertise a
  background the rows do not have; here the label is the sample.

  The entry was deferred because every obvious fix cost something, and a legend
  line's cost was permanent chrome in a panel already tight at 1280px. That is
  answered by making it **conditional on `milp_targets`** — already computed in
  the template, so no second derivation. With a full roster, a failed MILP, or an
  opponent on screen there are no target rows, and the only distinction left
  (green vs plain) is "when did I get him", which is information rather than the
  "is this even mine?" ambiguity the key exists to remove. Measured after the
  change: at 1280px `#team-panel` spans 856–1271 with grid overflow +0, so the
  added height changed no horizontal containment.

  Three tests, all three mutants killed. The load-bearing one is
  `test_it_is_absent_on_an_opponent` — the only one that can fail against a key
  rendered unconditionally.

- `scenarios.py` no longer carries an inline copy of `_reserved_top`.
  `_scenario_endgame_ceiling_binds` had
  `set(sorted(price, key=lambda n: (-price[n], n))[:25])` written out, character
  for character what `_reserved_top(price)` returns — and `_reserved_top`'s
  docstring already cites that scenario by name for the 25-rather-than-40
  reasoning, so the two were documenting each other while duplicating each other.
  Verified by digesting all six scenarios' keepers, minors, acquired, penalties,
  done flags and pool before and after: **byte-identical**, all six.

- **`tests/helpers.squeeze` split in two.** The by-code form reaches into
  `main.auction_state`, which is useless to an instrument that deliberately never
  imports `main`, so the inversion moved to `set_headroom(team, headroom)` and
  `squeeze` became the wrapper. No behaviour change. The docstring records why
  `scenarios._squeeze` is not a fourth copy of the same trick: it inverts for
  `physical_max_bid`, which adds the min-salary reserve back when spots are open,
  so the two deliberately land on different numbers.

### Fixed

- **A failed `.corrupt` rename could still destroy the good backup, and could
  make the app go quiet about having lost the draft.** `lifespan` renames an
  unreadable `auction_state.json` to `.corrupt` precisely so `_save_state`'s
  `path → .backup` rotation cannot carry it over the last good copy. The filed
  entry knew that rename could fail and be logged-and-ignored; reading the branch
  there were **three** defects, and the two nobody had filed were worse than the
  one that was.

  **(a)** `_save_state` rotated unconditionally, so one save after a failed
  rename put the unreadable file on top of the backup — the exact disaster
  `tests/test_crash_recovery.py`'s docstring opens with, reachable by a different
  route. There is now a `_untrusted_current_file` flag: set when the rename
  fails, it skips the rotation, and it clears after the first successful save
  because from then on the current file is one we wrote. **One-shot on purpose** —
  a permanent skip would freeze `.backup` at the recovered state and stop it
  following the draft, which is why there is a test for the guard *lifting* as
  well as for it holding.

  **(b)** The loud "this is a NEW auction" banner was gated on
  `os.path.exists(saved_path + ".corrupt")` — an artifact of the app's own rename
  standing in for the question it actually meant, "was there a draft here". When
  the rename failed there was no `.corrupt`, so a boot that had just lost 150
  picks came up looking like a normal fresh start. That is the *silence* the
  2026-08-07 work exists to end, reintroduced one branch down and undetected for
  two weeks. Both branches now read a `had_saved_file` local captured **before**
  the rename, and the message only names `.corrupt` when the rename actually
  produced one — pointing an operator at a salvage file that was never written is
  its own failure.

  **(c)** The same gate ran the other way too: nothing ever deletes `.corrupt`
  (`/reset` included), so one left over from an earlier incident made that banner
  claim a draft could not be read when there had never been one. The app's
  loudest notice asserting something false.

  A failed rename now also warns on screen in its own right — the backup is safe
  in software, but a directory that will not accept a rename probably will not
  accept a save either, and that is worth knowing before another twenty picks go
  in against it. Six new tests in
  `TestAFailedSetAsideDoesNotCostTheBackup` / `TestAStaleCorruptFileDoesNotInventAnAlarm`,
  driven by a fixture that fails `os.replace` **only** for a `.corrupt`
  destination: a read-only `STATE_DIR` is the realistic cause but the wrong
  instrument, because it breaks the `.tmp` write too and the test could then never
  observe whether the rotation would have eaten the backup — which is the entire
  question. All seven mutants killed, each by the test that claims it.

  **A seventh test was written and deleted.** "A good state file beside a stale
  `.corrupt` is quiet" read as coverage and could not fail: a state file that
  loads never enters the `auction_state is None` block, so the branch is
  unreachable and the assertion held against the pre-fix code too. Found by
  mutation, not by reading.

- **The startup banner is a list, because this change made a third message
  reachable.** `main.py:315 (_warn_at_startup)` concatenated into one string, and
  its own backlog entry said the fix was worth doing *"when a third warning source
  is added, not before"*. (a) above adds one, and three are now simultaneously
  true: the current file will not parse, setting it aside fails, and the backup
  parses but a backfill raises. Run together in one strip the second and third
  read as continuations of the first rather than as separate things that went
  wrong. `_startup_warnings` is a `list[str]`; `base.html` renders one message as
  a sentence and two or more as a `<ul>` — a single bulleted item reads as a list
  with entries missing, which is why the branch exists rather than always
  bulleting. `#data-warning` is untouched and stays a separate banner with its own
  lifecycle.

### Investigated

- **How much cheaper can a cold `/bid-check` be? About 1.6x, and it was not worth
  taking.** The backlog entry had done the hard negative work already — pool
  pruning is byte-identical on every pinned scenario and silently wrong once
  budget-per-spot nears the reserve floor — and closed by naming three surviving
  candidates with the note that **none was measured**. They are now, by
  `tests/measure_marginal.py`, which stays as the harness so the next attempt
  starts from a working instrument rather than from scratch.

  **Two of the entry's own numbers were wrong.** The wall time reproduces
  (956-1511ms per cold marginal) but the split does not: the aggregate is
  **89.5-89.8% inside CBC** over two full runs, not the 98-99% claimed, and the
  remaining ~10% is a flat ~9.2ms per solve of model build and extraction — paid
  ten times for ten models that differ in exactly one number.

  **And that aggregate hides the fact that decided the outcome.** Per subject the
  CBC share runs **65.3% to 92.1%**, because the ~9.2ms is charged per *solve*
  and so its share tracks how expensive each solve is: a fresh 705-player pool is
  92%, and `endgame-sole-bidder`, whose three solves are over a nearly-full
  roster, is 65%. That is C2's regression on that state seen from the other side —
  a candidate that removes solves cannot help where a third of the cost is not in
  the solves. "The solve is the whole cost" is true of the states that cost a
  second and false of the rest, and the first pass at this quoted the aggregate as
  if it were uniform.

  The solve count is not "~10" either, and calling it *bimodal* (an earlier draft
  of this entry did, while naming three values) was also wrong. Over the 28
  scenario subjects it lands on **2, 3, 9 or 10** — ×4 / ×8 / ×8 / ×8. Two is a
  floor-priced player short-circuiting on `with_at_min <= without`; three is a
  must-have, where excluding him is Infeasible; nine and ten are the full search,
  differing by where `physical_max_bid` puts the bracket. A mean over a mixed set
  hides the case that costs the second.

  **The best candidate is one the entry did not name.** Reading the build, the ~8
  probe solves differ only in `forced_cost`, which feeds the budget RHS and
  nothing else — so the search is a sequence over one right-hand side, and that
  has a single-solve dual: minimise roster cost subject to beating the
  without-him total, then `P* = remaining_budget - min_cost`. Sound because the
  starter variables are free and slot-capped, so `starter_pts >= t` says "some
  lineup reaches t" and `lineup_points` is the max over lineups. The strict `>`
  needs no tolerance at all: `projected_points` is an `int` and `lineup_points`
  returns an `int`, so it is exactly `>= B + 1`.

  Measured, all three candidates reproduce the reference's marginal
  **byte-for-byte on 168 subjects** — 28 across the fresh pool and six scenarios,
  then 140 with BOT squeezed to $1.90 / $1.50 / $1.00 / $0.70 / $0.60M per open
  spot, which is the regime that caught pool pruning. The payoff:

  | candidate | big-pool states | overall | worst subject |
  |---|---|---|---|
  | model reuse only | 1.07x | 1.06x | — |
  | reuse + `warmStart=True` | 1.19x | 1.14x | 1511 → 1117ms |
  | **min-cost single solve** | **1.55-1.60x** | **1.45x** | **1511 → 772ms** |

  and min-cost is **~0.8x on `endgame-sole-bidder`** (0.79x and 0.84x on two
  runs — one figure to two decimals would be false precision), a real regression,
  because the reference already short-circuits there in three solves.

  **The candidate numbers rest on the model copy costing what `optimizer`'s
  costs, and that was collected and thrown away.** `--faithful` drives the copy
  through the *production* search, so its total is directly comparable — but
  `copy_ms` was measured and never printed, leaving the copy validated for
  correctness and unvalidated for cost. If the copy's build were materially
  dearer, every speedup above would be understated by that difference and the
  1.06x would be unreadable. Measured 2026-08-21: **1.00x** (3384ms against 3400),
  and it is a reported column now.

  **The marginal is the only thing compared, and the plan asked for more than
  that on reasoning that turned out not to apply.** The plan's acceptance
  criterion was agreement on what reaches the screen — `value_cap`, `max_bid`,
  `expected_stop`, `stop_status` and the BID/CAUTION/DROP verdict — on the
  pool-pruning precedent that agreeing on one number proves nothing. But
  `compute_bid_recommendation` takes `marginal_value` as an argument and
  `main.bid_check` passes it in from `_marginal_value`, so a candidate's only
  channel to any of those five fields is that one float: equal float in, equal
  recommendation out, at every price. Building the comparison would have been a
  check that cannot fail, which this project treats as worse than none, so it is
  documented in `compare` instead — including what the argument rests on, since a
  second dependency on the roster the marginal came from would make it real work.

  **A 1.06x claim needs a null candidate, and this one did not have it at first.**
  A grill pass asked what the harness's noise floor was, and nothing had
  established it — so 1.06x and 1.14x rested on nothing, and could have been
  measurement order (the reference always runs first for each subject) or
  run-to-run variance. Measured: 6 repetitions per subject give a 1.01-1.02x
  spread and a stdev of 6-8ms on ~1100ms, and the reference entered AS a
  candidate scores **1.00x** (0.99x on a 4-subject run — a band, not a number).
  So the small numbers are signal. `--null` is now a flag rather than a one-off
  script, because the next claim under ~1.1x needs the same check — and it
  implies `--compare`, because a candidate is only ever run by a comparison and
  `--null` on its own printed a profile with no null figure in it.

  **Not shipped.** 1.6x on the slow cases does not buy a second MILP formulation,
  a confirm loop and two float-epsilon subtleties on the hottest path in the app.
  Both subtleties took a wrong draft to find and **both were invisible on the
  scenario set**: smoothing with `round(v, 6)` reads a genuinely-2.9 answer as
  3.0, because market prices are themselves off-grid (`0.5000000106310717`) and a
  $6.1M roster costs `6.100000041203467`; dropping the epsilon entirely then
  reads an exact 1.9 as 1.8, because `1.9 / 0.1` is `18.999999999999996`. The two
  error scales are ~4e-8 and ~1e-16 and the epsilon has to sit between them. A
  wrong marginal is a confident number on screen — the `keepFiles=True` failure
  class — and the entry's trigger (a draft-day stall on the first bid actually
  felt) has not fired. The entry stays open, with the cost now measured instead
  of open.

  One thing cleared along the way: **`warmStart=True` is safe under the scans'
  8-way concurrency**, which the `keepFiles=True` disaster makes it natural to
  assume it is not. PuLP writes the start file through the same
  `create_tmp_files` call as the `.lp` and `.sol`, so it carries the per-solve
  `uuid4().hex` prefix; `keepFiles` is the one flag that collapses every solve
  onto a shared name. Worth only 1.14x on its own, but no longer an unknown.

  Also verified rather than assumed: comparing the marginal float is sufficient,
  because it is the **only** solver-derived input to
  `compute_bid_recommendation` — everything after it is pure arithmetic, so
  identical marginals give identical `value_cap`, `max_bid`, `expected_stop`,
  `stop_status` and verdict. A second comparison over those five would have read
  as extra rigour while proving nothing new.

## [2026-08-20]

Adversarial review of the backlog-clearing batch below (`b01f303..6d5b4f0`),
then the fixes. Six findings, all resolved; nothing was a live defect in the
running app — no endpoint contract, state format or MILP path moved. What was
wrong is that **two of the batch's own claims were untrue**, one of them in the
file that overrides everything else.

Then one more pass, on a theme the day kept turning up: a **guard that reads as
coverage and is not**. That is the first two entries below — and the second one
started by measuring whether the test it was asked for could fail at all, which
turned out to be the interesting part.

### Added

- **Tests for the two `max_workers=0` guards**, which nothing reached.
  `ThreadPoolExecutor(max_workers=0)` raises `ValueError`, so each scan's
  `if not <work>: return {}` is load-bearing rather than tidy: without it,
  `/solve-standings` 500s in a league where **every** opponent is done — a legal
  end-of-draft state the CBA allows and that CLAUDE.md records happening to 3+
  teams every draft — and `/buyout-indicators` 500s on a BOT with no group 2/3
  contracts. Existing coverage marked at most one team done. Mutation-checked by
  deleting each guard in turn, one site each: both mutants raise `max_workers
  must be greater than 0` at the `ThreadPoolExecutor` line its own test drives.

  The standings test asserts `#proj-basis` reads exactly **`exact`**, not
  `estimated`, and a first draft had that backwards. With every opponent done
  nothing on screen is an estimate — their figures are finals and BOT's is its
  own optimum — which is why `standings_basis.html` branches on the count of
  *estimates* rather than of exact figures. The code was right and the assertion
  was wrong; recorded because reasoning from "the dict is empty" to "the marker
  says estimated" is the mistake, and it was made here.

### Changed

- **The bid-panel naming rule scans `<select>` and `<textarea>` too**, for the
  same reason the trade-form test it models itself on does: the control added
  later is the one a narrower scan waves through. Both branches carry zero
  selects today, so one added tomorrow sailed through a test whose docstring
  claims to state the general rule. Pinned by adding an unnamed `<select>` to the
  panel and watching it redden.
- **And each arm of that rule now guards itself.** `assert suspects` fired only
  when **both** came back empty — measured, the pre-auction branch is 2 fields +
  2 glyph buttons and the live one 2 + 3, so losing the whole field arm to an
  attribute-style change left the glyph buttons holding the assertion up while
  the input coverage silently vanished. Separate floors, separate messages naming
  which scan rotted; checked by breaking each regex in turn and reading the
  actual assertion rather than the traceback's source listing, which quotes both.
- **`_undoable` is annotated `-> Iterator[None]`** — CLAUDE.md asks for hints on
  signatures, 27 of `main.py`'s 30 private defs comply, and a `@contextmanager`
  generator is where the annotation is least guessable from the body. Its
  docstring now also states the contract it always had: `ValueError` and nothing
  wider, so another exception mid-mutation neither rolls back nor commits —
  unchanged from the four hand-rolled sites, and a deliberate limit rather than
  an oversight.
- **Two comments that had stopped matching their own code.** The guard's block
  still opened *"Two ways … and both count"* above a one-element set, and the
  same edit had left a 108-char docstring line.

- **The RFA and UFA nomination cards are one `pick_card` macro.** 43 lines
  duplicated for 43 lines, beside the `pick_prices` macro that had already
  collapsed their price line — filed 2026-08-18 and deferred out of that commit.
  Normalised, the two blocks differ in exactly two places: the `<h3>` text and
  the RFA-only prior-team line. Nothing else, to the character, including the
  position/logo line the backlog entry warned might diverge.
- **`show_prior` is semantic and defaults off.** Only an RFA has a prior team
  holding rights over him, so the line is absent from the UFA card because there
  is nothing to print, not because it was forgotten. A caller that wants it must
  ask.
- **Verified by byte-diffing the rendered output**, not by the suite alone:
  `GET /` is byte-identical, and `GET /nominate` differs only by two runs of
  insignificant whitespace inside the RFA card's own text node, collapsed by the
  `{%- if %}` that lets one macro serve both cards.
- **The refactor's mutation check found a real hole and it is now closed.**
  Deleting the whole `show_prior` block passed **all 892 tests** — nothing read
  the prior-team line. The heading parameter was already covered (by
  `TestMidBidClutterCanBeDismissed`, which finds a card by its heading text),
  this half was not. Two tests added. The second one exists because the obvious
  one is not enough: `players.csv` fills PRIOR FCHL TEAM for RFA1/RFA2 rows and
  nothing else — 22 of 2158 — so `{% if show_prior and ... %}`'s data guard
  masks the flag, and flipping the default to `True` or passing
  `show_prior=True` on the UFA call *both* survived the whole suite as
  equivalent mutants. Stamping a prior team onto the pool's UFAs kills them.
  That matters because the CSV is replaced before every draft, so an export that
  starts filling the column for group 3 is a live possibility and the answer is
  still no. All four mutants now die, one site per patch.
- **The four endpoints that can reject a request share one `_undoable`.**
  `BACKLOG.md`, 2026-08-07: they each repeated `capture_snapshot → try → except
  ValueError → _toast → commit_snapshot`. Attempted, judged, and landed — the
  entry asked for a shared helper and the honest answer turned out to be yes, but
  not for the reason it gave. It is **not** a line saving: `main.py` grows 13
  lines, because the helper's docstring carries the protocol that four
  paraphrases of it used to carry between them (and one of the four,
  `move_to_roster`, had quietly stopped carrying it at all). What it buys is that
  `capture_snapshot`, `commit_snapshot` and `rollback_to` now have exactly **one**
  caller each, inside `_undoable`, so the pairing cannot come apart at an
  endpoint — and `rollback=True/False` states the per-site decision as an
  argument instead of as the presence or absence of a line. Each site keeps its
  own one-line reason for the flag; only the invariant moved.
- **That made `TestEveryMutatingPostTakesASnapshot` stronger, not weaker.**
  `commit_snapshot` left `SNAPSHOTTING_CALLS`, because an endpoint that calls
  `capture_snapshot` and forgets to commit used to satisfy the guard — capturing
  without committing snapshots nothing. `_undoable` is a bare-name call rather
  than `auction_state.method()`, so the ast walk had to learn a second shape;
  getting that wrong is not a subtle false negative, it reports all four
  endpoints as taking no snapshot at all, which is how this was noticed.
  Mutation-checked, one site per patch: dropping `/buyout`'s `with` block reddens
  the guard *and* an undo test; flipping the trade to `rollback=False` reddens
  `test_a_failed_trade_still_rolls_back`; committing on the failure path inside
  `_undoable` reddens all four `TestARejectedEditCostsNoUndoDepth` cases.

Adversarial review of yesterday's parallel-scan batch. Both scans were
re-measured on both states afterwards, against the pre-review commit run in a
worktree side by side rather than against the published numbers: 398/431/179/561ms
against 436/460/182/573ms, so the fixes are neutral to slightly better and
yesterday's 384/454/182/569ms stand within noise. All 11 `proj-<CODE>` figures
and both dot sets (15/9 fresh, 23/15 endgame) are **identical** — the error
handling below changed no answer, which is what it should do on a run where
nothing failed.

- **`BUYOUT_PENALTY_RATE` hoisted to `main.py`'s top-level config import.** The
  function-local `from config import` ran once per scan when it sat at the top of
  a serial loop; after the fan-out it ran once per **player** — up to 23 times,
  from 8 threads, each taking the import lock. `config.py` imports nothing, so
  there was never a cycle to avoid.

- **The buyout scan's thread-safety comment now names the mechanism it rests
  on.** It said the candidate list is materialised on the caller's thread so "no
  worker reads `team.all_players` while another is deepcopying". True but too
  broad to protect anything: the actual hazard is that `roster_players` **lazily
  assigns** `_roster_cache`, a plain dataclass field, so computing that list
  inside a worker would have one thread writing BOT's `__dict__` while another
  `deepcopy`s it. It is benign today only by luck — the field already exists, so
  the dict cannot resize and `deepcopy` cannot raise *changed size during
  iteration*; the clone would just carry a torn cache. Stated the old way, moving
  that comprehension into the pool looks free.

### Fixed

- **Scenario determinism was only ever checked within one process, and the
  finding that said so named the wrong three functions.** `scenarios.py` makes
  six ordering decisions with a `name` tie-break; the deferred entry claimed the
  ones in `_fill`, `_drain` and `_reserved_top` "matter **across** processes,
  where hash and set order vary". Measured 2026-08-20 before writing anything —
  all six scenarios digested under `PYTHONHASHSEED` 0/1/12345/999, with each
  tie-break deleted in turn — **none of those three is seed-dependent.** All
  three iterate `available_players`, which is a dict, insertion-ordered from the
  CSV; `min`/`max` return the FIRST extreme and `sorted` is stable, so removing
  their tie-break picks a different player and picks the same different player
  on every machine. **Three of the six** do not even change the loaded state at
  all — `_reserved_top`, the character-for-character copy of it in
  `_scenario_endgame_ceiling_binds`, and the crease sort in
  `_scenario_endgame_last_goalie` — because their ties never reach a decision.
  (An earlier draft of this entry, and `e226a38`'s commit message, said two of
  them; the table in the test's own docstring has always shown three, which is
  the shape of mistake this project keeps catching — a doc sentence contradicting
  code committed beside it.) The one real exposure is elsewhere in that same
  scenario: `ranked` sorts `goalies & set(state.available_players)`, and a
  **set**'s iteration order is a function of the seed, so without the trailing
  `n` the scenario loads four different ways under four seeds. So the test that
  is now in place is aimed at what is actually exposed rather than at what the
  entry guessed — `test_a_scenario_loads_the_same_under_any_hash_seed`, one
  scenario, three child interpreters, ~45ms each, at module level rather than in
  either scenario's class because the subject is determinism and the scenario is
  only the vehicle. **The measurement also changed how the test is built, twice.**
  The entry proposed comparing a digest at seeds 0 and 1; measured, the mutant at
  those two seeds moves the team rosters but leaves `available_players` key order
  **identical**, so the obvious cheap digest — the pool's keys — would have passed
  against the very mutation the test exists for. It digests the rosters, and runs
  a third seed so it survives someone narrowing that later. Verified by deleting
  the trailing `n`: exactly one test in the file reddens, and it is this one.
  Filed on the way past: `_scenario_endgame_ceiling_binds` holds a
  character-for-character inline copy of `_reserved_top`, found because both sites
  had to be mutated separately.

- **Every template reference in these two files was checked for existence and
  nothing else, and six of the nine had drifted.** `test_reference_resolves`
  verified that a `.py` reference sits inside the function it names, then — for
  any non-`.py` path — asserted only that the file resolves and the line is in
  range, and returned. The documented reason was real (an HTML file has no
  enclosing symbol) but the cost had never been counted. Counted 2026-08-20:
  the `league_state.html` one was **35 lines** off the `.table-scroll-x` it
  claimed, two `bid_limits.html` ones were 15 off, and three more — two in
  `team_panel.html`, one in this file — were 9 to 15 off. The suite was green on
  every one of them. (Those six are named here without their old line numbers on
  purpose. A **historical** line number points nowhere you should go, so writing
  it in `file:line` form would make this guard flag the changelog forever — and
  the guard would be right to. Same reasoning as the rule itself: a reference
  that does not resolve is worse than no reference. Found the honest way, by
  writing this entry with the numbers in and watching four cases redden.) The
  deferred entry knew about exactly one drift, of 17 lines, and set "if it bites
  again" as its trigger; the trigger had
  already fired four more times unnoticed, which is the general lesson: a
  latent-cost deferral needs the cost **measured at defer time**, because
  nothing will measure it later. A template reference now carries a literal
  **anchor** in its parenthetical instead of a symbol — `(table-scroll-x)`,
  `(hx-trigger="change")`, `(milp.status != "Optimal")` — and the anchor must be
  ON the cited line. Stricter than the Python rule on purpose: there is no
  symbol span to absorb a few lines of drift, so exact is the only thing left
  that means anything. Two details make the stricter rule cheap rather than a
  chore: comparison is **whitespace-normalized** with backticks stripped, which
  answers the objection that killed this idea for three days (a re-indent or a
  re-wrap no longer breaks an anchor), and a failure **greps the file and names
  the line the anchor is actually on**, so the message *is* the re-anchor. An
  anchor need not be unique — `table-scroll-x` is on three lines of
  `team_panel.html` — because the assertion is "on the cited line". The missing
  parenthetical is now a loud failure too, matching what the Python side already
  did via `n.isidentifier()`, so the next entry cannot opt out. All nine
  references re-anchored in the same commit, no template edited. `_REF` itself
  is unchanged: it already accepted `\(([^)]+)\)`, so a quoted-attribute anchor
  parses without a regex change, which `test_reference_pattern_still_matches`
  now states with a sample. And the module gained the coverage it was itself
  missing — `test_the_anchor_rule_can_actually_fail` derives a true
  (path, line, anchor) triple **from the template at run time** rather than
  writing one down (a hardcoded line here would rot exactly like the ones being
  fixed) and asserts three things that die to different mutants: the true
  reference passes, a citation one line off fails *and names the real line*, and
  a missing anchor fails. Without it, restoring the old bare `return` left the
  whole suite green, since the live references are all correct once a
  re-anchoring pass lands.

- **CLAUDE.md was instructing a pattern that fails the suite.** The `_undoable`
  commit changed `TestEveryMutatingPostTakesASnapshot` and left both bullets that
  document it untouched: one still said *"`save_snapshot()` and
  `commit_snapshot()` both count"* — `commit_snapshot` had left
  `SNAPSHOTTING_CALLS` — and the other still told you to write
  `capture_snapshot() → attempt → commit_snapshot(before)`, which is now the
  **body** of `_undoable`. An endpoint written from that bullet is reported as
  taking no snapshot at all. Both now name `_undoable`, say why
  `commit_snapshot` stopped counting, and carry the escape hatch the guard's own
  comment already anticipated.
- **`measure_layout.py`'s `__meta` block was exempt from the three-way it sits
  beside.** The commit that added the three-way claimed it removed the case where
  a throwing selector abandons the whole probe. It did — for `TARGETS`. `__meta`
  still read `.auction-grid` bare, and `getComputedStyle(null)` throws. Measured
  by renaming the class in the live DOM: `page.evaluate(PROBE, TARGETS)` raised
  `TypeError`, `report()` printed **nothing**, and the exception came out of
  `main_measure()`'s width loop — while `min_contents()` answered `(no match)`
  for the identical selector, so the instrument's two halves disagreed.
  `.auction-grid` is the one hand-written grid in the app: what a layout refactor
  renames, which is also why you would be running this. `report()` now prints the
  marker and keeps going, because losing the grid summary is not a reason to lose
  17 measurements — and on the reproduction the table is the useful part, showing
  all three `.area-*` boxes at full width, which is what the rename did.
- **Then the fix itself was a finding.** Guarding `__meta` gave `PROBE` two
  implementations of the same three-way, and grepping found a third in
  `MIN_CONTENT` and a fourth in `ATTRIBUTE` — the rule existed three times and
  the one place that needed it most had none. `PROBE_FN` holds it once and is
  injected into each payload as a string (each is its own `page.evaluate` and has
  to be a single arrow-function expression). Verified against a worktree at the
  previous commit: the whole `--selftest` run across four widths is
  byte-identical, 10300 bytes either way.

- **Six controls in the bid panel had no usable accessible name.** `BACKLOG.md`
  filed this on 2026-08-15 as *"the bid panel's price input has no accessible
  name … a fix here should name both"* — two attributes on one form. The file has
  **two** forms, mutually exclusive, and the real inventory was six: both player
  inputs (the active one had nothing at all; the Start Auction one was named only
  by Chrome's fallback to its `placeholder`, which is the anti-pattern rather
  than the fix), both price inputs, and the four `-`/`+` steppers. The steppers
  are the `&times;` close-button class from 2026-08-14 — a name exists and names
  nothing — so they are labelled for what they do (`Lower the bid by $0.1M`), not
  for the glyph. The `placeholder` stays on the Start Auction field, because that
  field is free text and the hint is what stops a typo; it is simply no longer
  what names it.
- **Guarded as a rule, not as six labels**, by
  `tests/test_endpoints.py::TestNoBidControlIsUnnamed` — the same shape and the
  same reason as `test_no_control_in_either_trade_form_is_unnamed`, which the
  entry itself cites. Both branches of the template are covered, from both mounts
  (`GET /`'s section slice and `/bid-check`'s bare fragment), because a
  per-branch fix would otherwise pass a single-page assertion while leaving the
  other half silent. A `<button>` is named by its own text, so the rule is
  *"visible text with no letter or digit in it is not a name"* rather than
  "everything carries `aria-label`" — that is what makes a glyph stepper a
  finding and `Assign to BOT ($0.5M)`, `Start Auction` and the bidder logos not
  ones. Mutation-checked: each of the eight labels stripped in turn, one site per
  patch, every one reddening the new tests (the Start Auction player field kills
  two, as designed) and, on the widest mutant, **nothing else** across
  `test_endpoints`, `test_htmx_interactions`, `test_browser_ui` and
  `test_offline_assets`.
- **`measure_layout.py` tells three failures apart, where it had one.** Filed
  2026-08-13: `report` printed `(absent)`, `min_contents` skipped a `None` with
  no line at all, and `attribution` did `if not res: continue` — so a rotted
  selector in `TARGETS` was indistinguishable from an element that legitimately
  does not render. That is not hypothetical; `#league-state > table` went stale
  the moment the 2026-08-11 fix wrapped that table, and the instrument silently
  could not see the 955px element it was written to find. Now `(BAD SELECTOR)`
  (invalid CSS — `querySelector` **throws**, which used to abandon the whole
  probe), `(no match)` (valid, matches nothing: the stale-`TARGETS` case) and
  `(not rendered)` (matched but `display:none`, the case that made a hidden
  `#logs-panel` table measure 0 and read as a real number). `min_contents` and
  `attribution` print a line for **every** target rather than skipping, since a
  silent skip on min-content is the number a layout investigation turns on.
  Rendered-ness is `getClientRects().length`, not a zero width — a zero from
  `getBoundingClientRect()` is exactly the ambiguity being removed.
- **Verified by running the instrument, not by pytest** — it is not collected and
  has no test. `--selftest` appends one target of each kind and is the same
  idiom as `--whatif`: inject the failure into a real run and read the real
  output. All three reporters are exercised, because each collapsed the three
  differently and a branch nobody has watched print is what this entry was
  about.
- **One thing it immediately surfaced was NOT a bug, and the first diagnosis of
  it was wrong.** `#logs-panel div[role=tabpanel]:first-of-type table` came back
  `(no match)`, which read as a rotted selector, and the reasoning that it must
  be one — `:first-of-type` is per element name, so the first `<div>` child is
  the `role=tablist` — was written into the file before being checked. Measured:
  the tabpanels are *inside* the tablist, so the selector matches, rendered, from
  the first pick onward. The miss is about the STATE: `main_measure()` POSTs
  `/reset` first, and the Auction tab renders no table with an empty transaction
  log. The selector is unchanged and now carries that note, so the next reader
  does not repeat the hunt.

- **The standings worker's error net did not cover its own body.** `try` wrapped
  only the `solve_optimal_roster` call; `sol.status` and the `int()` conversion
  sat outside it, under a docstring that claimed to cover them. The asymmetry
  with `_solve_one_buyout`, whose body always was inside its try, is what gave it
  away. Nothing reachable makes `total_points` non-numeric today — it is a
  `float` on the dataclass — so this is a guarantee made true rather than a live
  bug, but the *shape* of the failure is worth recording, because parallelising
  the scan made it worse than it had been in the serial loop: an exception
  escaping a pool worker surfaces when the **result** is consumed, inside the
  `with`, so `shutdown(wait=True)` waits for the other seven solves to finish
  before raising. A serial loop raised at once; the pool would hang for about a
  second and then 500. Forced with a mutant (patch asserted to hit exactly one
  site) returning `total_points=None` from an `"Optimal"` solve: pre-fix,
  `TypeError: int() argument must be … not 'NoneType'` killed the whole scan;
  post-fix that one team falls back to its estimate and `#proj-basis` counts it.

- **The buyout worker swallowed every failure without a word.** `except
  Exception: return player.name, "keep"` — pre-existing, but this batch rewrote
  the block and gave its standings twin a log line, which is what made the gap
  visible. The consequence is precise: `buyout_dots.html` has two colours and no
  way to say *unknown*, so a solver blowing up on all fifteen contracts paints
  fifteen green dots, which reads as "no buyout helps". That is the 2026-08-07
  bug's exact appearance with the evidence removed. Now logs the player and the
  exception type, and still answers `"keep"` — the conservative verdict is right,
  the silence was not.

### Investigated

- **One import line shifted every `main.py` reference in both docs**, and three
  of the six were pointing at a plain `def`, so they landed one line above their
  own function and `test_backlog_refs` failed — as designed. All six re-anchored
  in the same commit, including the three the guard did **not** catch:
  `_symbol_ranges` starts a span at the first **decorator**, so a reference to an
  `@app.post` line stays in range while being exactly as stale. Worth knowing
  before trusting a green run as proof that no reference drifted.

Clearing the backlog's cheap tail — items that are small, self-contained and
not blocked on draft-day experience, so that what is left in `BACKLOG.md` is the
work that genuinely needs a draft to settle.

- **Pool pruning — the cheaper-solve lever `BACKLOG.md` has named since
  2026-08-06 — is measured unsafe, and the way it fails is silent.** A cold
  `/bid-check` is **988–1030ms** with **98–99% of it inside CBC** across 10
  solves, so the entry was right that the solve is the entire cost. Keeping the
  top 50 by projected points per position (705 players → 150) gives a
  **byte-identical answer on all seven pinned scenarios** and a 2–3.7x faster
  solve. That result is the trap rather than the finding: every scenario sits at
  **$1.9M or more of BOT budget per open roster spot**, and pruning by points is
  only safe while the budget is loose enough that the cheap filler never matters.

  Squeezing BOT's budget toward the reserve floor breaks it, in the worst
  possible way:

  | budget per open spot | full pool | top-50-by-points |
  |---|---|---|
  | $2.00M | 1233 Optimal | 1233 Optimal |
  | $1.00M | 1076 Optimal | **1069 Optimal** — 7 points low, no signal |
  | $0.70M | 999 Optimal | **Infeasible** |
  | $0.60M | 961 Optimal | **Infeasible** |

  Adding "and the K cheapest per position" does **not** rescue it — 912 against
  999 at $0.70M — because which players matter depends on the budget
  *interaction*, not on points or price along either axis alone. A wrong
  `Optimal` wearing a confident number is precisely the `keepFiles=True` failure
  class (950 against 1355) that `TestTwoSolvesAtOnceAgreeWithTwoSolvesInARow`
  exists to catch, and this one would reach the bid advisor rather than a scan.
  The tight-budget regime is not hypothetical either: it is reachable **through
  play**, since buyout penalties, `/trade-between` and `/adjust-salary` all warn
  rather than refuse, and `BACKLOG.md` already records $20.5M of penalties on a
  fresh BOT getting there.

  No code changed. Three `BACKLOG.md` entries did: the `main.py (bid_check)`
  finding now strikes pruning by name and carries the table, the closed
  interaction-budget entry no longer propagates the dead lever, and the
  `optimizer.py (solve_optimal_roster)` short-roster entry — which "already
  frames" pointed at — now says a short-roster path has to be exact rather than a
  heuristic over "the N players you CAN buy", because that heuristic is the thing
  just measured wrong. Surviving candidates, none of them measured: a warm-start
  basis, fewer binary variables via position aggregation, or fewer binary-search
  steps.

- **Audited all 31 `/grill` rounds to see whether the findings were actually
  fixed. They were, with two exceptions — and the weak link is not the fixing,
  it is the promise to file.** Two independent axes, because they catch different
  failures. **The paper trail** (mechanical): every entry ever added to
  `BACKLOG.md` across its 109 revisions — **104 distinct findings**, 20 open
  today, 84 closed, sum reconciled — each classified by what the commit that
  *removed* it did, since CLAUDE.md requires the write-up in the same commit as
  the deletion. That trail has **no holes**: of 84 closures, 21 of the initially
  unexplained ones were a single deliberate event (the 2026-07-05 owner-decision
  triage, which predates `/grill` entirely) and all 4 unexplained `[grill]`
  entries traced to real resolutions — the flags were artifacts of a fix and its
  entry-removal landing in different commits, or of the fix landing in a
  different file than the entry named. **The reports** (read): all 55 verdict
  messages, ~158KB, since only these can show a finding flagged and never
  written down anywhere.

  **What the audit found.** Two findings were flagged and never filed and are
  still true — now filed as `[2026-08-20] [audit]`: `_warn_at_startup`'s banner
  concatenation (2026-08-07) and the pool table's empty filter state
  (2026-08-16). Three more were named in a sentence promising *"these go to
  `BACKLOG.md`"* and never arrived, surviving only because later work happened to
  fix them anyway — the hardcoded `CAUTION_BAND`, the live `MarketInfo`'s
  `floor_demand` inconsistency (now consistent, with a comment at
  `main.py:1465 (bid_check)` naming that exact trap), and the negative `Spots` display
  (clamped). **So a report saying "this goes to the backlog" is not evidence that
  it did** — three of the four items named in that sentence in the very first
  grill round never appeared in the file. Every dropped item was in a *closing
  narrative* rather than in a numbered finding; the numbered findings were
  without exception fixed or filed. A further 11 prose asides were raised and
  dismissed *with a stated reason*, which is a judged non-finding rather than a
  deferred one and correctly absent from `BACKLOG.md`.

  Also closed: **round 20 — today's parallel-scan grill — had never been
  re-reviewed.** It reported NEEDS WORK with five findings, all five were fixed
  earlier today, but the protocol's step-5 re-review never ran so no verdict
  closed it. Re-established the diff (`fe4edde..HEAD`) and reviewed it fresh:
  **SHIP IT**, nothing new. Round 13's missing verdict is *correct* — a plan-mode
  grill puts its remediation in the plan file — and its span ends with all five
  of its findings resolved across three commits.

  Two instrument notes worth keeping, both of which would have produced a wrong
  answer. **`rtk` truncates `git log`**: the unproxied form returned 50 of 316
  commits, so anything needing full history goes through `rtk proxy` or reads the
  file in Python (it rewrites `grep`/`wc` too, so counts printed through them are
  its own summaries). And **the transcript is self-contaminating** — this
  session's own audit text matched both `NEEDS WORK` and `SHIP IT` and made round
  20 look closed, so the scan is cut at the turn that requested the audit, pinned
  by string search rather than a line number that drifts as the file grows. A
  first pass at the backlog join also keyed entries on their
  `file:line (symbol)` header and silently **merged two distinct `optimizer.py`
  findings**; keying on the finding's body prose instead is what makes the 104
  count trustworthy.

## [2026-08-19f]

### Changed

- **Both manual scans now solve their MILPs in parallel inside the worker
  thread.** They were moved off the event loop earlier today, which stopped them
  blocking the cockpit but left them a serial loop. Concurrent CBC was already
  measured safe and pinned, so this is the payoff:

  | state | endpoint | before | after | |
  |---|---|---|---|---|
  | fresh | `/solve-standings` | 1294ms | **384ms** | 3.4x |
  | fresh | `/buyout-indicators` | 1630ms | **454ms** | 3.6x |
  | endgame | `/solve-standings` | 264ms | **182ms** | 1.5x |
  | endgame | `/buyout-indicators` | 2174ms | **569ms** | 3.8x |

  Every published figure is **byte-identical** before and after on both states —
  all 11 `proj-<CODE>` spans and all 15/23 dot verdicts diffed, not eyeballed.
  The endgame standings case gains least, as expected: eight done teams are not
  solved, so there are only two MILPs to overlap.

  **Learned while measuring, and it inverts what CLAUDE.md said:** the endgame is
  the ROSTER scan's *worst* case, not its best — 23 eligible contracts against 15
  on a fresh league, because a late-draft BOT owns more group 2/3 players. Only
  the standings direction (cheaper late, because done teams are final) had been
  written down, and it is easy to assume both scans behave the same way.

  Three shape notes, all forced rather than chosen. Each loop body became a
  per-item `_solve_one_*` keeping **its own** `try`/`except`, because an exception
  raised in a pool worker surfaces when the *result* is consumed — a shared net
  would cost the whole scan instead of one team or one dot. `functools.partial`
  rather than a lambda, because `lambda c: _solve_one_opponent(...)` is a call in
  the ast and fails `test_nothing_calls_a_solver_except_through_a_thread`, while
  the workers must carry the `_solve_` prefix to satisfy
  `test_only_recompute_may_solve_on_the_loop` — both guards were written to force
  exactly this shape and needed no amendment. And `pool.map` yields in **input
  order**, which is the only reason the result is deterministic, so the new test
  asserts key order as well as values: a refactor to `as_completed` would
  otherwise pass a values-only comparison while making the dict depend on which
  CBC subprocess finished first.

  `SCAN_WORKERS` lives in `main.py`, not `config.py` — that file is the league
  (cap, roster shape, CBA rules) and a worker count is machine tuning. Capped at
  8 rather than taken from `os.cpu_count()`: each concurrent solve is a CBC
  **subprocess**, so it is a core budget, and uncapped on this 20-core box it
  would put 15 processes on a 4-core draft-day laptop.

- **The per-team cache for automatic exact standings is dead, and the
  `BACKLOG.md` entry now says so with the numbers.** It proposed invalidating a
  team's cached optimum only when *that team's* roster or budget changed, because
  "the pool losing one player rarely moves an opponent's optimum". Measured over
  five picks on a fresh league: **28 of 45 cached rows (62%) would have been
  stale**, single-pick swings reached **−26 points**, and one pick moved **9 of 9**
  other opponents twice in five. Every one of those renders as exact and BOT's
  carries a rank badge, so it is the same class of error as the done-team
  projection bug. The whole-column invalidation `_recompute()` already does is
  correct. The entry was **corrected rather than deleted** — the want is real, and
  an idea that simply vanishes invites the next person to re-propose the cache.

- **The trade-off, measured rather than assumed: a concurrent request got
  slower.** A warm `/bid-check` fired 30ms into a scan costs **43ms**, against
  the **3ms** the same measurement gave this morning when the scan was a serial
  loop in one thread — because the scan now occupies up to 8 CBC subprocesses
  instead of one, and the loop competes with them for cores. That is the right
  trade at these magnitudes (43ms is 12x inside the 500ms interaction budget, and
  still 39x better than the 1682ms this all started from), but it is a real cost
  and it scales with `SCAN_WORKERS` — which is the second reason that cap is not
  `os.cpu_count()`. If a draft-day laptop ever makes typing feel sticky during a
  scan, lower the cap before looking anywhere else.

### Investigated

- **Parallelism does not help anything on the request path**, so nothing there
  changed. `_recompute`'s single solve for BOT has nothing to overlap it with,
  and `/bid-check`'s cold ~935ms is a *sequential* binary search over solves, not
  a fan-out — its lever is still a cheaper solve, as `main.py:1465 (bid_check)`
  says. Even at 384ms the standings scan is far too expensive for an action path:
  on top of `/assign`'s 150ms it would blow the 500ms interaction budget, so
  "never put this on an action path" stands.

## [2026-08-19e]

Grill of the hard-coded-names sweep. Four findings, all in the new code, all
about it being *narrower or more brittle than it read*.

### Changed

- **The guard read `test_*.py`, which excluded the file where a literal would do
  the most damage.** `helpers.py` is imported by every test module, so one stale
  name there goes stale for the whole suite at once; `conftest.py` and the three
  `measure_*.py` instruments were out too. Now every `tests/*.py`. Zero hits
  there today, so widening was free — and it is safe for a reason worth writing
  down: the guard matches a name only when it **is the entire string constant**,
  which is the only form that gets used as *data*. A name inside a longer string
  is invisible, so the docstrings that discuss real players do not trip it
  (`helpers.a_buyout_candidate` names one who is still in the pool). Prose naming
  a player goes stale; it cannot make a test silently stop testing.
- **It also paid 29 `client` resets — a MILP solve each — to do it.** Per-file
  parametrisation measured 0.11s of setup per case, **3.48s** total, for a check
  that runs in microseconds; one test over all files is **0.40s** and reports
  *more*, because the message names every offender by file and line instead of
  failing on the first file. The reset itself stays and is not optional: a buyout
  removes a player from the roster **and** the pool, so on a state another test
  left behind, `available + rosters` would be missing him and a test naming him
  would pass.
- **The role derivation could itself break on a new `players.csv`** — in the one
  file whose point is surviving one. `test_auction_draft._script` derived the top
  D, the top G and the top RFA2 independently and then asserted they came out
  distinct, so a CSV where the top D *is* the top RFA2 would kill the whole file
  at `test_01`. Not hypothetical: the pool holds 9 RFA2s today and they include a
  D (Miro Heiskanen) and a G (Igor Shesterkin), so it takes only one of them
  being best at their position. Reproduced by forcing that shape — the old
  derivation returned 2 distinct players of 3; the new one returns 11 of 11 with
  the roles still on picks 5/6/7. `pool_top` gained `skip` for it, which keeps
  `n=1` — asking for spare candidates instead would break on a pool thin in that
  role.
- **`test_15` and `test_16` hand-copied the script table**, as `== 4` and
  `5.0 + 4.5 + 3.0 + 3.0`, while a comment written in the same commit claimed
  they "read straight off" it. They now do. The cost of the copy was not
  tidiness: edit a row and the copy goes stale, so the assertion fails describing
  the wrong reason — or, if two edits cancel, passes while checking nothing.
  Proved by moving MAC's pick to BOT: derived, the file passes at 5 picks /
  $19.0M; with the hand-copied figures restored on top of the same edit, both
  tests fail.

### Investigated

- **Synthetic unit-test names cannot start failing the guard on a refresh.**
  `test_state.py` names players `Alice` / `Bob` / `Minor1`, and the worry was
  that a future CSV would collide with one. Measured: **0 of 2155** pool names is
  a single token — every one is `First Last` — so a one-word synthetic name is
  structurally safe. This is the concern that ruled out matching capitalised
  tokens (`"Charlie"` collides with five real players *today*), and it is why the
  whole-name rule is the one that stays quiet.

## [2026-08-19d]

### Changed

- **No test names a player from `players.csv` any more, and a guard keeps it
  that way.** CLAUDE.md has carried the rule *never hard-code a player name in a
  test* for a year with nothing behind it, and it drifted the whole time.
  Counted properly with an ast walk against all 2155 `PLAYER` values:
  **61 literal names across 7 files**, where `BACKLOG.md` recorded "four literal
  names … ~39 times" — an undercount of more than half. Seven of the 61 are
  legitimate (`test_player_identity.py` writes its own CSV in `tmp_path` and
  reuses the real collision cases on purpose), so **54 sites across 6 files**
  were swept, each name replaced by the ROLE it plays via `helpers.pool_top` /
  `a_roster_player`, and `_draft_to` — the symbol the backlog entry named — now
  drafts through `helpers.assign`, which fails AT the pick.

  **The deferral reason was backwards, which is why this had sat since
  2026-08-18.** The entry said to do it at the next `players.csv` refresh, "when
  the failures are in front of you". `/assign` answers **200 with a toast** when
  it rejects, so the failure mode is a silent pass: at refresh time some tests
  would simply have stopped testing anything, and nothing would have said so.
  The 2026-08-07 drill is the precedent the entry itself cites — one missing name
  arrived as `assert 24 == 25` three tests downstream, naming neither the player
  nor the reason.

  Two things the entry did not mention, both found by measuring rather than
  reading. **A bare surname**: `test_htmx_interactions.py` asserted
  `"Panarin" in trigger["showToast"]["message"]`, which no full-name guard can
  see; it now asserts the derived name. And **`test_auction_draft.py` was never
  silent** — its `_assign_and_verify` already checked that the pool shrank and
  the log grew, naming the player, so that file would have failed loudly. The
  five files that would have failed *quietly* are the ones that mattered.

  `tests/test_no_literal_player_names.py` is the new guard: full-name equality
  against the names the loaded state knows (available plus every team's
  `all_players`, so the loader's `Matt Murray (DAL)` renames count too), with a
  one-entry allowlist carrying its reason. **Full names only, deliberately** —
  also matching capitalised tokens would catch the surname class, but it needs a
  *data-dependent* allowlist (`"Charlie"`, in `test_state.py`'s Alice/Bob/Charlie
  unit test, collides with five real players today) and that rots in the noisy
  direction: the next CSV could fail a synthetic-name unit test for no reason, at
  exactly the moment you want the suite quiet.

- **The scripted ten-pick auction is derived, not listed.** `test_auction_draft.py`'s
  `PICKS` table turned out to be almost exactly the top ten by projected points —
  the tell that the names were never the point. Three of them were, and the
  derivation keeps them: pick 5 is the top D-man, pick 6 must be an **RFA2**
  because `test_08` asserts the sale converts him to group 3, and pick 7 is a
  goalie. Verified faithful — 11 distinct players, the roles on picks 5/6/7, and
  BOT still buying exactly 4 for exactly $15.5M, the two figures `test_15` and
  `test_16` read straight off the table. `pool_top` gained `position` / `group`
  filters for those three lookups, and `_script()` runs from `test_01` because
  `main.auction_state` does not exist until the client starts. Ten test methods
  were renamed off their players (`..._marner_to_hsm` → `..._an_rfa2_to_hsm`): a
  method named for a player it no longer drafts is the same rot one level up, and
  no guard can read a function name.

### Investigated

- **What this does and does not buy.** The guard proves no literal pool name
  remains; `helpers.assign` proves a rejected pick fails at the pick. Neither
  proves the suite would survive an *arbitrary* CSV — a pool with no goalies, or
  with twenty players, breaks things no naming discipline can fix. That is what
  `TestDataFingerprint` and the refresh drill are for.
- **Sweeping all 15 files' direct `/assign` posts through `helpers.assign` was
  considered and dropped.** 59 sites, and with names derived its unique value is
  close to zero — the helper's documented purpose was stale names specifically.
  Not filed as an idea either: there is no remaining failure it would catch.

## [2026-08-19c]

Grill of the event-loop change. Three findings, none of them a defect in the
shipped behaviour — the measurements all held up on re-check — and all three
about the change being *undefended* rather than wrong.

### Changed

- **The structural guard now asks the question from the side that catches a new
  endpoint.** `THREADED_SCANS` is a hand-maintained pair, so on its own it says
  nothing about a **third** multi-solve endpoint — which is exactly the failure
  it exists to prevent, and exactly how this bug arrived in the first place: an
  endpoint written the obvious way, `async def` around a loop of solves. Two
  additions close it. First, the "no direct solver call" check moved from handler
  bodies to the whole module: `_context` is called by all 25 endpoints, so a
  `_solve_*` added *there* would have been on the loop for every one of them and
  passed the handler-only walk. Second, every caller of `solve_optimal_roster`
  must now be a `_solve_*` (which the first check forces into a thread) or name
  itself in `SOLVES_ON_THE_LOOP` with a reason — today `_recompute` alone, one
  78ms solve whose result every panel in the response needs. Same set-equality
  idiom as `TestEveryMutatingPostTakesASnapshot`. Both mutants checked, each
  asserted to have patched exactly one site.
- **CBC being safe to call from two threads at once is recorded and pinned.** The
  change made concurrent solves reachable for the first time — the loop solves
  for BOT on every pick while a scan is mid-flight in a worker thread — and
  nothing said so. It holds because PuLP names its scratch files `uuid4().hex`
  per solve (`pulp/apis/core.py`, `LpSolver.create_tmp_files`) and CBC is a
  subprocess, so the GIL is released: measured 11 concurrent solves × 3 rounds,
  zero errors, zero disagreements with the serial answers, **401ms against
  1134ms**. The new test races three opponents against their serial answers
  (0.88s, six solves) and asserts the **answers**, not the timing, because the
  collision regime is silent — adding `keepFiles=True` to `optimizer.py`'s one
  `PULP_CBC_CMD`, the one-line change that reintroduces a shared filename, does
  **not** raise: SRL came back `status="Optimal"` with **950** points against
  **1355** solved alone, a wrong figure that would have rendered wearing a rank
  badge.
- `_publish_if_current` is generic over its value type (`[V]`, `dict[str, V]`)
  instead of taking two bare `dict`s, so handing the buyout dict's string
  verdicts an int-valued projections solve is a type error rather than a runtime
  surprise. Also corrected the documented Python version: `CLAUDE.md` and
  `verify-app.md` said 3.12 and the venv runs **3.14.4**, with nothing in the
  repo pinning either — so 3.12 is the floor, not the target.

### Investigated

- **The `deepcopy` per scan is not a cost worth optimising.** 2.7–2.8ms on a
  fresh state, mid-draft, and with a full 50-entry snapshot chain — the chain is
  a list of JSON *strings*, which `deepcopy` returns by reference rather than
  copying. A roster scan pays 23 copies, ~63ms of ~1600ms, all of it inside the
  worker thread.
- **The discard path was re-checked in Chrome and has no defect.** A pick landing
  mid-scan leaves all 15 dot placeholders untouched, re-enables the Scan button,
  logs no console error, and does show the warning toast: present at t+319ms
  through t+3009ms, gone at t+4613ms, which is `shortcuts.js`'s 4000ms
  auto-dismiss. A first probe reported no toast — that was a bad query on a
  document-wide `.alert` join, not a finding.
- **A ~3x speedup is available and was deliberately not taken** — see
  `BACKLOG.md` → Ideas → Performance. The scans' own loops are embarrassingly
  parallel now that concurrent CBC is measured safe, which would take a ~1.6s
  roster scan to ~550ms. The loop fix already removed the *blocking*, which is
  the part that hurt on draft day; a slow scan beside a responsive cockpit is a
  different problem from a frozen one.

## [2026-08-19b]

### Fixed

- **The two manual scans no longer stall the whole cockpit.** `BACKLOG.md` had this filed on 2026-08-18 and deferred "with a draft coming"; re-measured against the live server it was worse than the entry said, because **every** request queued behind a scan and not just other solves:

  | request | alone | during `/solve-standings` | during Scan Roster | after |
  |---|---|---|---|---|
  | `/bid-check` (warm) | 10ms | 1234ms | **1682ms** | **3–14ms** |
  | `/state` (solves nothing) | ~3ms | 1208ms | 1564ms | **12ms** |
  | `/nominate` | 79ms | 1214ms | 1582ms | **87–90ms** |

  All 25 handlers in `main.py` are `async def`, so FastAPI dispatches them to the event loop and ~1.2–1.6s of synchronous CBC held it. The draft-day shape is one click: Scan Roster, then type a bid price — `#bid-price` is `hx-trigger="change"` — and the advisor took 1.7s to answer a request that costs 10ms. `hx-disabled-elt` greys the button and says nothing about the rest of the panel. The scans themselves are unchanged (1296ms / 1635ms) and still land their results (15 dots, 10 exact figures). Confirmed in Chrome at 1280px, which is the claim that matters: with a roster scan in flight, a price typed into the bid panel 150ms after the click got its verdict in **116ms** — one htmx round trip including the counterfactual that rides along — where the same interaction used to wait out the whole scan. 15 dots painted, no console errors.

  **Not the one-word version.** `_recompute_exact_projections` and `_recompute_buyout_indicators` read module globals and wrote module globals, so simply dropping `async` would have run the state reads *and* the global writes in a worker thread — the objection the backlog entry itself raised. Instead each is now a pure `_solve_*` function that takes a state and prices and returns a dict: the endpoint reads what it needs on the loop, hands the pure function to `run_in_threadpool`, and publishes on the loop. No worker thread touches a global.

  The `deepcopy` per scan is **safety, not speed** — 3ms against a 78ms solve. The solvers only read, but they iterate `available_players` and every roster while an `/assign` can now run alongside them, and a dict that changes size during iteration raises.

- **A pick landing mid-scan now beats the scan.** That hazard is created by this change and is the reason `_publish_if_current` exists: the loop can run an `/assign` while a scan is in flight, and a result describing the roster from before it must not be published, because both dicts are rendered as authoritative — the Proj column carries a rank badge, the dots carry a verdict. `_recompute()` bumps a `_state_version` (one bump covers all twelve mutating endpoints); the scans compare it before and after.

  **A counter rather than "is the dict still empty"**: `_recompute()` already clears `exact_projections`, and empty is *also* the normal state before anyone scans, so emptiness cannot tell "nobody asked" from "a pick landed while I was solving".

  **The two discards are not symmetrical**, which is the part worth remembering. Dropping a standings solve is enough on its own — the column falls back to `_context`'s estimate and `#proj-basis` says `estimated`. Dropping a buyout scan is not: `buyout_dots.html` paints a verdict on every eligible player and defaults a missing one to `keep`, so re-rendering it would turn all 15 dots **green** — indistinguishable from "no buyout helps", which is precisely the 2026-08-07 minors failure. So that path returns an empty body (the placeholders stay grey) plus a warning toast saying to scan again.

- New `tests/test_event_loop.py`, three kinds of claim because none covers the others: **structural** (ast — each scan hands a `_solve_*` to `run_in_threadpool`, no handler calls one directly, and `_recompute` still bumps the counter), **ordering** on the live server (`/state` fired 50ms *after* a scan must finish first — ordering rather than wall-clock, since CLAUDE.md's rule is that timing assertions go flaky under load), and **discard** (a stub solver that calls the real `_recompute()` mid-solve, so the test covers the link between the bump and the guard). Proven able to fail three ways: against the pre-fix build 5 of the 6 fail including the ordering test; publishing unconditionally kills both discard tests; removing the bump kills those two *and* the structural one.

### Investigated

- **`/trade-evaluate` measured at last: 179ms** for one-for-one and 171ms for three-for-one, inside the 500ms interaction budget. It was the one endpoint the closed interaction-budget entry left unmeasured, for the stated reason that it needs a built-up trade form — which is now three lines of `form.getlist` payload. No change; the figure is the point.

## [2026-08-19]

### Changed

- **The purchases-cannot-do-it measurement is republished as a sweep, because the single construction did not reproduce.** The `_squeeze` docstring and this file both said "7 of 10 opponents hit 24 players with $8.2M–$22.6M still spendable … not one of 570 pool prices was capped". Re-measured under the construction that sentence describes — drain every opponent to $0.0M spendable, top 25 reserved, no fill — it is **6 of 10, $9.8M–$23.0M, 0 of 575**, and no variant reproduces the trio: the closest, top 40 reserved, gives 7 of 10 but $0.0–24.5M and 577. This is the same class of error as the 19/40/563 trio recorded under `endgame-ceiling-binds`, and the fix is the same shape — publish what was actually established, which is stronger: swept over **16 constructions** (targets $0/$5/$8/$12M spendable × with and without a fill to 24 × top-25 and top-40 reserved), **the ceiling stayed at MAX_SALARY and zero prices were capped in every one**. Also `_late_draft_shape`'s "no drain at all needs $13.5M–$28.4M" re-measures at **$15.2M–$28.3M**; its other three figures reproduce exactly.

- **Two test docstrings claimed coverage that mutation disproved.** Six targeted mutations against the five claims the original batch never proved could fail. Three landed — a shaped opponent marked done kills the live-and-shopping sweep; BOT's drain target moved 16.0 → 14.0 kills the position-needs claim and its squeeze moved 7.5 → 11.4 kills the under-the-clamp claim; the reserve term leaving `_squeeze`'s open branch kills eight tests. Two exposed docstrings rather than bugs:

  **The determinism tests do not pin the name tie-breaks.** Removing `_fill`'s tie-break leaves all 58 tests green — dict iteration is insertion-ordered, so two loads in one process give the same answer either way. The tie-breaks matter *across* processes, where hash and set order vary, and nothing in this file can see that. What the test does catch is state leaking between loads (a cached price dict, a mutated `POSITION_MINIMUMS`), which is worth its 20ms; the docstring now says so instead of the opposite, and the two new copies point at it.

  **`test_every_team_still_solves` has no teeth on a full team.** `solve_optimal_roster` answers `spots == 0` from its own branch (`optimizer.py:162 (solve_optimal_roster)`) and returns Optimal without running the MILP, so filling that team with skaters only left everything green. Its position legality is pinned by the `roster_needs` assertion in the exactly-one-full test instead.

  The first attempt at the done-team mutation matched **two** sites and was not applied — the fifth anchor miss in this repo, caught by asserting exactly one replacement rather than by noticing a suspiciously green run.

### Fixed

- **The grill on the two new scenarios: a helper that could miss quietly, a test counting the wrong definition, and published numbers that did not reproduce.** The code was correct and both suites were green — every finding here is about a claim being weaker or wronger than it read.

  **`_squeeze` returned success while missing its target.** `max(0.0, SALARY_CAP - salary - wanted)` yields 0 when the ask is impossible — penalties only take money AWAY, so no dead cap can *raise* a team's max — and the helper then left the team parked somewhere else while reporting nothing. That is the failure `_fill`'s own docstring argues against ("a scenario that quietly builds something other than what it says produces test failures three assertions from the cause"), in a helper whose entire contract is one figure. It now raises, naming the team, the target and the shortfall, and restores the penalties it found first so a failed build leaves no half-squeezed team behind. Measured: the branch is reached by neither shipped scenario (11 squeezes each, all exact), so this is a guard for the next one.

  **The endgame's capped-row count asserted a definition the panel does not use.** `test_a_substantial_share_of_the_pool_is_capped` counted a raw `live < model - 1e-9` while the new late-draft test counted `market.is_capped`, which quantizes to the one decimal both panels print — two definitions of one rule in one file. They disagree by 3×: **83 raw against 28 quantized** of 677 on that state, because 55 of the 83 differ by less than a cent and render as two identical figures. The old floor of 40 sat *between* the two numbers, so it passed only by counting rows that show nothing, while its stated rationale is "the tooltip-left renders per capped row". Same one-definition problem `2176a56` fixed in production code the day before, back test-side.

  **`_late_draft_shape` divided by zero on a single team.** `last = len(codes) - 1` is the spread's divisor. Unreachable from both callers (ten and eight) but the helper exists to be reused; one code now lands on the low end.

### Investigated

- **The two new picker labels widen the navbar `<select>` by 67px and overflow nothing.** They are the longest options in the list and DaisyUI's `.select` carries no width constraint, so it is content-sized. Measured in Chrome: **402px → 469px**, with `scrollWidth == clientWidth` on both the navbar and the document at 1024 / 1280 / 1600, and the select's right edge unchanged at every width (940 / 1196 / 1516) — it grew leftward into free space. No change made.

## [2026-08-18e]

### Added

- **Two scenarios finish the pre-baked set: `drained-late-draft` and `full-roster-still-bidding`.** These were the last two items on `BACKLOG.md`'s *More scenarios*, and both are states `POST /reset` cannot reach and the operator has never seen. The set is now five: `goalie-asymmetry`, `endgame-ceiling-binds` (most teams done, ceiling binding, stars unsold), `endgame-last-goalie` (one spot, one affordable goalie), `endgame-sole-bidder` (ten full rosters, nobody able to raise a bid), and these two.

  **`drained-late-draft`** — sixty picks in: all ten opponents live, every one still needing players, and nobody able to pay for the stars left. Measured: ceiling **$3.3M**, the second of ten distinct maxes ($3.5M / $3.3M / $3.1M / …), `demand_count` 10 with `floor_demand` False, rosters 17–21 with 3–7 spots, **25 of 597** pool prices capped, every team's MILP Optimal, build 16ms. It fills the gap between a fresh reset (ceiling *is* the cap, every `stop_status` reads `at_cap`) and `endgame-sole-bidder` (ceiling at the floor, every price floored): the only loadable state where the ceiling binds **mid-range**, so the bid panel's "Should win it" figure says something about a particular player — BID, worth $4.0M, stop $3.6M on the priciest RFA — and the nomination panel's new two-price line shows a $3.3M market price against a $9.5M model price with the model struck through.

  **`full-roster-still-bidding`** — the other half of the 2026-08-05 report. `4dc59da` made a full roster with cap space a live bidder (extras go to the minors with salary fully on cap), and that fix had only ever been exercised against synthetic teams. Measured: one opponent at **24 players, 0 spots, `roster_needs` all zero, `physical_max_bid` $8.0M** — and `market_ceiling` is $8.0M with `second_bidder` that team. **The counterfactual is the point and it is a number**: the second-highest max among opponents *with spots* is $3.0M, so a ceiling gated on roster space would price the whole pool **$5.0M** too low. Downstream, `live_opponents([BOT, full])` returns it and a bid check against it alone reads BID, worth $6.2M, stop $8.1M. Only 2 of 594 prices are capped at that ceiling — exactly the two players priced above it — so this is not the scenario for the capped marker, and the docstring says so.

- **The finding that shaped both: a "no money, spots still open" state cannot be built by buying players.** `_drain` stops at `ROSTER_SIZE`, so spending is bounded by roster space. Swept over **16 constructions** — drain targets $0 / $5 / $8 / $12M spendable × with and without a fill to 24 × the top 25 and the top 40 reserved — and in **every one** the ceiling stayed at `MAX_SALARY` with **zero** pool prices capped, the exact opposite of the intended state. Sharpest single case, top 25 reserved and drained to $0.0M with no fill: **6 of 10 opponents stop at 24 players with $9.8M–$23.0M still spendable**. The lever that removes money without adding players is one the league already has: CBA 11.4 leaves 50% of a bought-out salary on the cap, so `_squeeze` sets `penalties` to land a team on a named `physical_max_bid` and the team reads as one that bought contracts out. Real spending *plus* dead cap beats either alone — drain to $12.0M spendable first and the penalties come out at a tidy $9.0M–$11.0M, where no drain at all needs $15.2M–$28.3M. Draining **deeper** does not help: at $8.0M and $5.0M targets some teams reach 24 (premise gone) and the spread widens to $5.4–15.9M and $2.9–19.5M.

### Changed

- **Ordering inside a scenario is load-bearing twice, and both were measured rather than reasoned.**

  In `full-roster-still-bidding` the two rich teams are shaped **first, while the pool is still rich**. `_drain` buys the dearest player it can, so against the leftovers of eight already-shaped teams it needs many cheap purchases to move a big budget — measured, shaping these two last put the *rival* at 24 as well. Two full teams look fine on screen and quietly destroy the test: with the highest and the second both full, "the ceiling is set by a team with no roster space" can no longer fail, and swapping the two squeeze targets stops being detectable.

  In both scenarios BOT is shaped **last**, which is why its drain target had to be tuned against the real call order: the opponents thin the mid tier first, so the same target buys BOT more players than it does against a fresh pool. Four targets measured in place — $14.0M (21 players, 3 spots, **no position needs left**), $16.0M (19 / 5, $9.5M remaining, $7.5M max, needs {D: 1}, 1196 lineup points), $18.0M and $20.0M (18 / 6 but $11.0M–$13.0M of dead cap on your own roster). $16.0M ships. The first draft of that docstring carried the fresh-pool figures (18 players, 6 spots, a $7.0M penalty) which the real ordering never produces.

- **`scenarios._squeeze` is unit-tested directly — the only test in `test_scenarios.py` that reaches for a private.** The helper needs two branches: with spots open the commissioner's reserve is added back, at `spots == 0` there is none. Get the second wrong and the team lands $0.5M off its named target while **every scenario-level assertion still passes**, because the ceiling still equals the full team's max, just half a million lower. So the branch is pinned on the helper, at both ends, rather than through a scenario constant asserted in two places. An earlier draft of the `_squeeze` docstring called that branch "the whole claim of `full-roster-still-bidding`", which overstated it in the direction that matters: the error is invisible from outside.

  Six mutations, all killed, each in the test that names its claim: dropping `_squeeze` from the shape (ceiling back to `MAX_SALARY`, nothing capped — 7 tests), one flat squeeze target instead of the stagger (the distinct-maxes guard alone), the full team left one short of 24, the two squeeze targets swapped (`second_bidder` becomes the rival), the reserve added at zero spots, and the rich teams shaped last.

## [2026-08-18d]

### Changed

- **One definition of the capped rule: `market.is_capped`.** `main.py`'s `bid_limits` row and `NominationPick.capped` each carried the same quantized comparison, written in opposite directions — two copies of one rule, the second added the same day. That is the trap this file already records twice (the stale drain filter; `compute_marginal_value` carrying its own drifting copy of `physical_max_bid`'s formula). Sited beside `compute_market_price` because it is the observation that that function's `min()` bit, and `optimizer.py` already imported from `market.py`, so no new cycle. The two **test-side** copies stay hand-written on purpose, and the docstring says so — they are the independent equivalence, and importing the helper there would turn both into tautologies.

- **The pool key and `Player.name` are now pinned as one identity**, in two tests: the live-data invariant (`test_data_loader.py`) and across a JSON round trip (`test_state.py`). `_nomination_pick` looks both of the panel's figures up by `player.name` while every branch that calls it iterates the pool by key — before this batch each branch used the key it was holding, so the two could not disagree. The invariant holds everywhere today and is load-bearing well beyond this change (`/assign` pops by key, both price dicts are keyed on it, `_dom_id` hashes it), but nothing tested it, and `to_json`/`from_json` store the key verbatim, so a mismatch would round-trip faithfully rather than heal. Both proven able to fail by keying a pool one character off — in the loader for the first, in `from_json` for the second.

- `.claude/rules/pricing-pipeline.md` now records the drain tie-break's **display** consequence, which runs opposite to the intuition: breaking ties toward least surplus makes the UFA half systematically pick the candidate whose two figures *agree*, so a large gap on a UFA drain recommendation means the ranking is not doing what the rule says.

### Fixed

- **The grill on the nomination-price batch killed two of its own tests and found a third that had never worked.** Recorded because none of the three was visible by reading — each was proven by a mutant that survived.

  **The factory guard missed any construction outside a function body.** `test_every_pick_is_built_by_the_factory` walked `ast.FunctionDef` nodes, which is a *list of the places a call can hide* rather than a rule; a module-level `NominationPick(...)` left all 47 selected tests green while the docstring claimed "one construction site". Now checked by line span against `_nomination_pick`, which covers module level, `async def`, comprehensions and lambdas at once.

  **The hover sentence could quote one figure twice.** The test asserted only that `"the market ceiling caps it at"` appeared *somewhere* in the response, so a title reading `Model says $2.8M … caps it at $2.8M` — self-contradicting, on screen — passed. It now has to name both figures, each derived from the price dicts for that card's player. The regex is anchored to the price line's own opening tag: the card carries an earlier `title` on the NHL logo and a bare search picked that one up ("EDM").

  **`TestPriceColumn` never read the rendered `capped` flag.** Its `_capped` helper *recomputes* the rule from the two price dicts — deliberately, that is what makes its assertions an equivalence rather than a tautology — but the consequence is that every assertion in the class holds identically against a flag that is wrong for every row. Measured: a `capped` predicate returning False left the class green with the Price column's marker gone. It now also reads the markup in the squeezed state, one capped row and one uncapped. Pre-existing, and surfaced only because the rule became shared (below). The row reader matches the **unescaped** row rather than escaping the name: Jinja writes an apostrophe as `&#39;` and `html.escape` gives `&#x27;`, so escaping matched **zero** rows for `Ryan O'Reilly` and `K'Andre Miller`, and which name the test picks depends on the data.

## [2026-08-18c]

### Added

- **The model price renders beside the market price in the nomination panel.** The last unbuilt want from the 2026-08-07 owner testing pass. The panel's "Expected" figure is `market_prices` — deliberately, and that is not being undone: until 2026-08-06 the drain path put the raw **model** price under that label and advertised `Expected: ~$7.7M` for a player who could only fetch $2.5M. But with one figure on screen, "cheap because the market is thin" and "cheap because the model rates him low" look identical, and telling those apart is the whole point of a nomination. So each pick now carries both figures, each labelled: `Expected: ~$2.8M ▼ · Model $9.5M`, the model struck through when the ceiling cuts the price. Same visual grammar as `bid_limits.html`'s Price column, so the two panels explain the same phenomenon the same way; the order differs because this line is labelled and that column is not.

  **Both figures always render, even when they agree.** A second figure that appears only on divergence cannot be told apart from "this panel doesn't show that", and a fixed position is what lets the RFA and UFA halves be compared at a glance.

  **`capped` is quantized to one decimal**, matching `main.py`'s `bid_limits` flag, and that is not a nicety: the drain tie-break breaks toward **least surplus**, so on the UFA half the recommended player is routinely a cent under the ceiling. Measured in a $2.5M-ceiling state — the UFA pick is **$2.51M model against a $2.50M market**, which prints as two identical figures. A raw float comparison would strike one of them through and read as a display bug. The consequence worth knowing: the divergence this want is about shows up mostly on the **RFA half and on target picks**, because the UFA drain ranking actively selects toward agreement.

  Structural, because the failure mode is invisible on screen: all **six** `NominationPick` construction sites (2 RFA, 4 UFA) now go through `_nomination_pick`, which keys both dicts off `player.name`. Two figures side by side describing *different* players is not something a reader could catch, and six copies of `market_prices.get(name, MIN_SALARY)` is where a lookup keyed on the wrong branch's name would hide. An ast guard asserts `NominationPick(...)` is constructed in exactly one place — which is also what covers the two branches (UFA depth, UFA fallback) that no buildable state reaches, rather than contriving a state and pretending. Four of the six are exercised for real: RFA drain + UFA target on a fresh state, RFA drain + UFA drain at a $2.5M ceiling, RFA target + UFA target in `endgame-ceiling-binds`.

  Measured in Chrome at 375 / 1280 / 1600px, both states: the line stays **one line at every width** — 209px of text uncapped, 226px capped, inside a box 303px wide even in the 1-col layout — and no card reaches past the panel's right edge. The planning estimate was ~190px, which was low but not by enough to matter.

  A native `title`, **not** DaisyUI's `data-tip`: `TestTooltipsStayInsideTheirPanel` loads `GET /` and starts a bid, but never fires `/nominate`, so a bubble here would be one nobody has ever placement-checked — the debt `bid_limits`' own `tooltip-left` carried until 2026-08-13, and which `BACKLOG.md` still tracks for eight of the twenty.

  Nine mutations, all killed. Two are worth recording. Swapping the model lookup to the market dict dies **only** in the ceiling-bound state — at full budgets the two dicts agree on every pick, so the fresh case cannot see it, which is why both states are parametrized rather than one. And deleting the price line from **either** card fails, in both directions: the two blocks were hand-maintained copies of that line until this change made them one macro call, the same duplication `loadTradeChoices` removed from the trade forms.

## [2026-08-18b]

### Added

- **Two endgame scenarios behind `POST /load-scenario`: `endgame-last-goalie` and `endgame-sole-bidder`.** Both are documented engine semantics that existed only against 23 synthetic players called `F0..G2`, and neither had ever been reached on the real pool or looked at on screen. Same argument `endgame-ceiling-binds` made on 2026-08-13, which paid for itself twice — it placement-checked the app's only `tooltip-left`, and it surfaced the done-team projection bug.

  **`endgame-last-goalie`** covers two rules at once: a player whose exclusion makes the roster unsolvable is worth the physical max, and forced players exactly filling the roster answer **Optimal, not Infeasible** (the second used to floor-price every player in the pool the moment one spot remained). Three ingredients, all measured. BOT's spare goalie goes down to the minors — a real move the app offers, and where this league already keeps ten of them; measured, he is group 3, so his $0.5M stays fully on the cap and the demotion frees nothing. `_drain` then `_fill` leave BOT 23 players and **$3.0M**, deliberately under `MAX_SALARY`: a physical max sitting at the league maximum cannot be told apart from the clamp inside `physical_max_bid`, and which number the marginal came from is the entire claim. Then every goalie the pool offered at or under that budget is sold — **53 of 64**, twenty filling opponents' creases out to the classic three and 33 stashed as their minor-league depth. **Eleven stay on the board, priced $3.2M–$7.7M.** So the state is not "goaltending is gone" but "goaltending is out of reach", which is what a real endgame looks like: solving without the last affordable goalie is Infeasible, forcing him in returns Optimal from the `spots == 0` branch with an empty roster and $0.5M of cost, and his marginal is **$3.0M against a $0.5M model price**. The opponents stay rich on purpose — the ceiling holds at the cap and every `stop_status` reads `at_cap`, because `endgame-ceiling-binds` owns that half and needed three ingredients of its own to get there. Their creases are filled for a reason that is not cosmetic: an opponent still needing goalies with none in reach solves **Infeasible**, and the League State Proj column would silently keep its estimate for that team.

  **`endgame-sole-bidder`** reproduces the 2026-08-05 report — DROP on a bargain when every rival has dropped out — on real data for the first time: a 62-point forward worth **$7.5M** where an advisor capping on `ceiling + increment` would have refused anything over **$0.6M**. The construction rests on a rule worth stating plainly: **a team with a roster spot open can always bid the floor**, because the commissioner refuses any bid that would leave it unable to fill 24 at the minimum, so `physical_max_bid` cannot fall below `MIN_SALARY` while a spot remains. The only legal way to price a team out completely is therefore a full 24 with less than one increment of cap left — `_drain` to zero spendable then `_fill` to 24, which works because a floor purchase moves `spendable_budget` by exactly nothing ($0.5M of budget out, $0.5M of reserve freed). Measured: all ten opponents land at **$0.0–0.1M**, `live_opponents` over the whole grid is empty, `bid_winner` returns BOT, and with zero demand every market price in the pool collapses to the floor — the documented zero-demand rule, and the only loadable state that shows it. **Nobody is marked done, and that is the point**: the bidder grid filters on `is_done` alone, so a spent-out team stays clickable, which is the exact case `market.bid_winner` exists to get right and the one that once rendered "You've won" with no Assign button. Marking them done instead produces the same WIN verdict for the wrong reason, which is why the class asserts the premise directly.

  Also added: `test_no_scenario_builds_a_state_the_league_forbids`, parametrized over every scenario — the reserve rule, the cap, 24 on the active roster, and BOT never marked done. It passed against the two existing scenarios before being written, so it is a guard on future construction rather than a bug found.

  Fifteen mutations run across the batch. Four from the first pass are worth recording because each exposed a real gap: leaving the surplus goalies in the pool (an affordable alternative, so nothing is a must-have), filling BOT to 24 instead of 23 (no spot, and the marginal collapses to 0.0), marking the opponents done instead of spending them out, and hiding broke teams from the bidder grid. One "passed" against an anchor that matched nothing and printed a green suite for the unmutated file — the same trap recorded on 2026-08-17, and the reason each patch now asserts it replaced exactly one site.

  **The grill on this batch then killed three of its own claims, and they are the part worth keeping.**

  **The crease-filling rationale was false.** Deleting it entirely failed nothing, all eight tests green. The docstring said an opponent left needing goalies would solve Infeasible and lose its exact League State projection; measured, every opponent still solves, because they are rich and ten goalies sit on the board at $3.2M–$7.7M — unaffordable to BOT, pocket change to a team with $20M. The real reason the creases get filled is that the state has to be **readable**: three in the crease each rather than ten teams carrying one goalie and six in the minors. The shape is now asserted directly (no filling, and filling only three of ten, both die), and the solvability test keeps an honest docstring saying it holds for a boring reason today and what it actually guards is a future construction that strips a position out of the pool while teams still need it.

  **The scenario assumed BOT keeps exactly two goalies.** True this season and invisible either way, because two is precisely the count at which one demotion and demote-until-one agree. Measured against a doctored crease: with three keeper goalies BOT ended up needing **no** goalie, so the state was not a must-have one and the scenario silently stopped testing its own subject; with one it demoted the last goalie and left a team unable to field a legal lineup. It now demotes to one short of the position minimum, and the test doctors the count so both mutants die.

  **A scenario that cannot build had no path to the operator.** `_fill` raises when the pool cannot supply what a construction asks for — a refreshed `players.csv` is the realistic cause — and `/load-scenario` caught only `KeyError`. Unhandled that is a 500, and htmx swaps nothing on a 500, so the click appears to do nothing: the same class of bug as the no-feedback scan button fixed the day before. Now logged with a traceback and toasted with the reason, which is safe to do because every mutation in the endpoint happens after `scenarios.load` returns — the test proves the live draft is byte-identical afterwards by comparing the whole `/state` dump.

  One more thing measured and left alone: `_fill`'s `room` guard cannot bind on either scenario, because a cheapest-first fill always picks a floor-priced player and the reserve rule guarantees `room >= MIN_SALARY`. Deleting it fails no test. It stays for the narrowed-`positions` case, where it converts breaking the reserve into a loud error — and its docstring now says that rather than implying the guard is live.

### Changed

- **One door from the pool onto a roster, and one into the minors.** `PlayerOnRoster.from_pool(player, salary)` now owns the RFA group conversion a sale requires (RFA1 → 2, RFA2 → 3), which lived inline in `/assign` and did not exist in scenario setup at all. That was harmless only while no scenario put a purchase in the minors: `counts_on_cap` reads the group and `MINOR_CAP_GROUPS` is `{"2", "3"}`, so a player left on his pool group sits in the minors costing his team nothing against the cap — and `endgame-last-goalie` stashes 33 of them. Note the knock-on: `_drain` now converts too, so `endgame-ceiling-binds`' purchases carry groups 2 and 3 instead of RFA1 and RFA2. That is a fix, not a regression — `/assign` converts, so no real draft can produce an RFA-grouped roster player — and it makes those purchases buyout-eligible in that scenario, as they would be in a real one. `TeamState.add_minor_player` is the other half, because `add_acquired_player` only routes to the minors when the roster is **full**, so stashing depth on a team with open spots meant reaching through `_invalidate_cache` from outside. Its overflow branch delegates rather than repeating the flags, and both paths are pinned on `is_minor` and `is_bench` **together** — a copy that forgot `is_bench` still satisfies "he is in the minors" and then displaces a starter on recall.

## [2026-08-18]

### Added

- **Tests for the rank badge, which is the reason the scan exists and had none.** Measured: deleting the badge from the macro passed all twelve tests in the class. Three tests now, and the third exists because the second was not enough — a mutant that emitted the badge inline but omitted it from the out-of-band payload passed both of the others, because they re-fetch `GET /` and get a fresh inline render. **A browser does not**: htmx replaces the span with what the response contains, so the badge would vanish from the live DOM until some later full-panel swap restored it. Reading the swap payload is different evidence from reading the page rendered after it, and only one of them is what htmx applies. The first two are invariants over what is rendered (BOT's badge is BOT's position among the figures in the table) rather than expected numbers, so they hold in every state; the endgame one asserts the badge moves **up**, not that it reads `#1`, which would be a `players.csv` fingerprint in the wrong file.

  Also recorded in `BACKLOG.md` rather than fixed: both multi-solve endpoints are `async def` around seconds of synchronous CBC work, so they run on the event loop and every other request queues behind them — including the `/bid-check` that fires while the operator types a price. Pre-existing and file-wide (`/bid-check` is itself `async def` around ~1000ms of cold solving). The fix is deleting a keyword, which is why it is not casual: it moves the handlers onto a threadpool, and the module globals they mutate would then be written off the loop.

### Changed

- **One definition of the Proj figure instead of two.** The id, the number and the rank badge existed inline in `league_state.html` and again in `standings_cells.html`, and htmx matches the out-of-band swap **by id**, so the two had to agree character for character. CLAUDE.md names this hazard for the buyout dots, where the fix shared only the id through the `dom_id` filter and left the markup duplicated across two files. `templates/macros/standings.html` now exports `proj_figure(...)` and both callers use it — the better tool was already in the project (`macros/player.html`, imported by three partials). The existing out-of-band tests are the proof and were not modified.

### Fixed

- **The Proj column's basis marker said "estimated" when every figure on it was exact.** Found by grilling the standings batch from the day before. With every opponent done — reachable late in any draft, and `endgame-ceiling-binds` already has 8 of 10 — `exact_projections` is empty because there is nobody to solve, yet a done team projects its final roster and BOT projects its MILP optimum, so the whole column is exact by construction. Both halves verified rather than argued. The label said the opposite, and pressing Solve Standings performed **zero solves and changed nothing** — a broken-looking button in the one state where the operator most wants the final table.

  The wording was fine; the predicate was wrong. The template branched on how many figures are **exact** (falsy at zero) when the question is whether any figure is still a **guess**. `standings_basis` now carries `estimated` and the template tests that, giving three states: nothing left to estimate → `exact`, a mixed column → `exact 9/10`, nothing solved → `estimated`. A bare `exact` rather than `exact N/N`, because the count only carries information when the column is mixed and `exact 0/0` is worse than saying nothing. What it still does not distinguish, deliberately: a scan in which every solve failed reads the same as never having scanned — honest about the figures, which is what the label is for, where a fourth state would describe history instead.

- **A solver that blew up on one opponent said nothing at all.** `except Exception: continue` dropped the team silently. The marker reveals *that* a cell is still an estimate and can never say which team or why, so nothing was diagnosable. The broad catch stays — one opponent's failure must not cost the other nine, the same stance as `_load_saved_state` — with a `logging.warning` naming the team, the exception type and its message. The path now has a test, which it did not before: the existing unsolvable-opponent test forces a non-Optimal *result*, not an exception.

- **A 1.26s click that looked identical to no click.** Measured: the app styles neither `.htmx-request` nor an `htmx-indicator` anywhere, so Solve Standings (1262ms, 10 solves) and the buyout Scan (~15 solves) gave no feedback at all, and a second click started the whole run again. `hx-disabled-elt="this"` on both buys the visible state and the double-click guard together — confirmed in the vendored bundle rather than assumed (`htmx-1.9.10.min.js`, function `sr`, sets `disabled=""` for the request's duration). Both buttons, stated in the tests as a rule over the pair: one greying while the other sits inert is a worse inconsistency than neither.

## [2026-08-17d]

### Added

- **`GET /solve-standings` — the League State Proj column, answered exactly, on a click.** The column is two rules: BOT's figure is `milp_solution.total_points`, a real MILP optimum under BOT's real budget; every opponent's is an estimate in `_context` that costs no solve, because 11 MILPs per action is what it avoids. A manual scan now replaces the opponents' figures with real per-team optima and swaps them in out-of-band — the buyout Scan button's idiom exactly, for the same reason. Measured through the endpoint: **1262ms** on a fresh league (10 solves) and **259ms** in the `endgame-ceiling-binds` scenario (2), because done teams are final and BOT is already solved and neither is asked. The cost therefore *falls* as the draft progresses.

  **The 2026-08-08 decision to label rather than fix rested on a measurement of the wrong thing, and it stood for nine days.** That entry read "measured on a fresh state they were 5 points apart and the rank was identical either way" — computed by comparing the two rules **on BOT**, whose figure never uses the estimate. It could not see the error it was quoted to bound. Re-measured 2026-08-17 against a real per-team MILP for every opponent:

  | | fresh league | endgame scenario |
  |---|---|---|
  | estimate vs MILP optimum | **+68 mean, +193 worst (+14.2%)** | +146 / −72 |
  | overstates the *achievable* optimum | **6 of 10 opponents** | 1 of 2 |
  | teams whose rank order it moves | **9 of 10** (GVR 3rd → 8th) | — |
  | BOT's own rank badge | #10 → **#11** exact | **#2 → #1** exact |

  The endgame row is the one that matters, and it is not a new class of bug: the badge said **#2 when BOT was #1**, which is what the done-team projection fix (2026-08-13) removed in its own form — *"the panel said #6 while BOT was first by a distance"*. Note also that an estimate above the MILP optimum is not merely imprecise. The optimum is the most points that roster can reach at those prices; a figure above it describes a team that cannot exist.

  **The entry's own suggested fix is dead, and that is worth keeping.** It read *"the affordability filter still tests players one at a time and never asks whether the team can afford the whole set"*, which reads as a recipe: make the fill budget-aware. Three cheap estimators measured on a fresh league, mean |error| against the per-team optimum — **current 94**, per-slot average 147, points-greedy fill (budget- and position-aware, reserving `MIN_SALARY` per remaining slot) 176, points-per-dollar greedy 401. Every replacement is worse, because a greedy spends the budget on one star and fills the rest at the floor while the current rule's average-of-the-top-3×slots happens to approximate a budget-constrained optimum. Right criticism, wrong conclusion: the affordability filter *is* structurally wrong and there is still no cheap rule that beats it. The only thing better than the estimate is the solve.

  **Staleness is the load-bearing part, not the solve.** `exact_projections.clear()` joins `_recompute()`'s invalidation list, whose existing sentence about values *"derived from the roster, budget and market prices this function is replacing"* covers it verbatim — so one pick returns the whole column to estimates. `#proj-basis` reports which basis is on screen as a **count** (`exact 9/10`), never a boolean: an Infeasible opponent solve leaves that team *absent* from the dict and it keeps its estimate, so "exact" alone would mislabel the one cell that is still a guess. Absence rather than a stored zero is also what keeps a solver failure from putting a plausible last place on the board. The marker is unconditional with only its `hx-swap-oob` attribute conditional, following `buyout_scan.html` — a target that disappears with its contents can only be swapped one way, which is how the Scan button once vanished and never came back.

  Two smaller decisions worth recording. The figures are wrapped in **spans**, not swapped as `<td>`s: a bare cell at the top level of a response has no table context to be parsed in. And the id is the **raw team code** with no `dom_id` filter — three uppercase letters are already a legal CSS identifier, and the filter exists for player names carrying backticks, parentheses and disambiguation suffixes. Unlike the buyout dots the button needs no gating, because all 11 spans render unconditionally, so no swap can miss.

  Seven tests, eight mutations verified dead — and **the mutation that renames the OOB ids survived the first version of the test written to catch it.** That test collected fragments with `id="(proj-[\w-]+)"`, so renaming them to `projection-<CODE>` made all eleven invisible to the very regex looking for them; it saw only the basis marker, whose target was still fine, and passed against 11 dead swaps. It now collects every id carrying `hx-swap-oob` with no assumption about the name, and counts them, because a fragment that is never emitted resolves vacuously. Two existing tests needed re-anchoring, both because they were working: `TestTooltipsStayInsideTheirPanel` requires each tooltip by a text fragment and the Proj tooltip's "Computed two ways" is gone with the rewrite (re-anchored on "Solve Standings" — the mechanism, not a turn of phrase), and `BACKLOG.md`'s `_context` line reference in `main.py` had to move down 53 lines. (Written first as the literal `path:line` pair, which `tests/test_backlog_refs.py` collects out of this file too — so quoting a stale reference in prose *creates* a live one and fails the suite. The same slip is already recorded under 2026-08-17.)

## [2026-08-17c]

### Fixed

- **One report called the same pick two different numbers.** `measure_spend.py`'s `first_bind` was a **0-based index** into the pick list, printed as `pick {n}`; `_spend_curve` in the same function labels its rows with `enumerate(picks, start=1)`. Demonstrated on three picks with only the third capped: `first_bind` said `2` while the curve called that same pick `3`. `measure_ceiling.py` shared the 0-based basis (`ceiling steps ... 0.5M@43`, `first pick it bound (32, 7.3)`), so the cross-check between the two instruments was internally valid — but **every figure either one had published was an index labelled as an ordinal**, and those figures had already reached `.claude/rules/pricing-pipeline.md` and two entries here.

  Fixed as one convention across both instruments: **1-based ordinals**, because "pick N" means the Nth pick to every reader of these documents. `measure_ceiling.py` now derives a `pick = picks + 1` at the top of its loop and uses it for the step sequence, the first bind, the health lines and the checkpoint rows — the checkpoint test is `(pick - 1) % every` rather than `pick % every`, deliberately, so the rows are the same *states* as before the renumbering and the first one is still the fresh league, which is where the ceiling's starting value is read from.

  It buys more than tidiness. Re-run both regimes 2026-08-17 and read each saved state back with the reader: the drain run's `first_bind` and the instrument's `0.5M@` step now print **the same number, 44**, so "the two measures are close" became exact agreement on the pick where the ceiling started mattering — two independent paths, one over live `market_info` per pick, one over the serialized transaction log. Everything not a pick label reproduced unchanged (133 of 165, 122 of 165, `$337.7M` of `$337.7M`, the baseline's 18% unspent and JHN $19.8M / GVR $14.1M / VPP $12.0M), which is the evidence that only the labelling moved.

  Restated in `.claude/rules/pricing-pipeline.md` — a live rules document has to carry the current figures — with an explicit note that pre-2026-08-17 quotes were 0-based, so an old number sitting one below a fresh run reads as a renumbering rather than a behaviour change. The `## [2026-08-16]` entry below keeps the numbers it published, with a one-clause pointer instead of a rewrite: **the numbers there are the record of what the run said.** Same-day figures in `## [2026-08-17b]` are corrected in place, since two entries from the same day disagreeing is just an error.

  Two mutants, each verified to die and each killed by `test_first_bind_is_a_pick_ordinal_not_an_index_into_the_priced_subset` specifically: back to 0-based, and a 1-based ordinal over the *priced* subset rather than all picks. The second is the one a cross-check cannot catch — both synthetic drafts have zero unpriced records, so it agrees with the instrument either way.

- **`measure_spend.py` gave a raw traceback on exactly the file it exists to open.** `report()` read the state unguarded, and all four failure modes were reproduced rather than assumed: a truncated write and a non-JSON file raise `json.JSONDecodeError`; a hand-edited or half-written log parses fine and then raises **`KeyError: 'model_price'`**, because `state._transaction_from_dict` reads nine keys positionally; and a directory argument raises `IsADirectoryError`. That last one and an unreadable file are both `OSError`, so one guard covers them.

  Why it matters more than a tidy-up: the app **renames a state it cannot parse to `.corrupt` instead of deleting it**, and the reason anyone opens a `.corrupt` file is to find out what the draft contained. The failure that brings people to this reader was the one failure the reader could not survive. Fixed the way `_load_saved_state` already handles the same class of problem — degrade, don't die — printing the path and the exception type, since the whole point of the CLI argument is that the file may not be the live state.

  Four tests over `report()` with `capsys` (`TestTheReportSurvivesWhatPeopleActuallyOpen`), and five mutants verified dead, each at the test that claims it: guard narrowed to `ValueError` → the missing-key test alone; guard deleted → the corrupt and missing-key tests; message no longer naming the file or cause → the same two; the empty-log branch deleted from `report()` → the new empty-log test alone; the `path.exists()` check deleted → the missing-file test. That last mutant is the interesting one — with the new guard in place its failure mode *degrades* from a traceback to a less useful message (`FileNotFoundError` is an `OSError`), which is exactly the kind of quiet regression a guard invites, and the test still catches it.

  The empty-log case now has **two** tests that look redundant and are not: `test_an_empty_log_reports_no_picks_rather_than_zeros` fails if `summarize` starts returning zero-filled keys, the new `capsys` one fails if `report` stops branching on them. A refactor can do either without the other, and only one test notices each. Also dropped the `f` prefix from four placeholder-less f-strings (F541).

## [2026-08-17b]

### Added

- **`tests/measure_spend.py` — a reader for the spend curve the app has been recording all along.** Two `engine/market` findings were parked on "needs a real draft's numbers". They were not blocked on collection: every `/assign` logs a `TransactionRecord` carrying `salary`, `model_price` **and** `market_price`, captured before the player leaves the pool, and the log is serialized with the state. Nothing read it back. Both entries stay open — what changed is their blocker, from "collect the numbers" to "run the reader".

  **It measures a sharper quantity than `measure_ceiling.py`, and the difference is the finding.** That instrument counts a bind as `ceiling < MAX_SALARY`; this one counts `market_price < model_price`, i.e. the ceiling moved *past a player's model price* and changed what the MILP planned on. Cross-checked on the same synthetic drafts 2026-08-17:

  | buyers pay | ceiling below `MAX` | changed a price |
  |---|---|---|
  | the tool's own market price | 0 of 165 | 0 of 165, never |
  | what the reserve rule allows | 133 of 165 | **122 of 165, from pick 44** |

  The baseline agreeing at zero is the correctness evidence — two independent paths, one over live `market_info` per pick and one over logged records, and a ceiling pinned at `MAX_SALARY` can never sit below a model price. The drain run is where they diverge: **the ceiling changed nothing until it reached the $0.5M floor at pick 44**, because the intermediate steps ($7.3M at pick 33, $4.5M at pick 41) were above every remaining model price — a top-down draft has already sold the players those ceilings would have capped. So `.claude/rules/pricing-pipeline.md`'s "133 of 165" overstated when Layer 2 started mattering by 11 picks, and it now carries both columns with what each one means.

  **The prediction that motivated this was wrong about the magnitude.** The plan argued the price-changing count would be *much* smaller than 133 because most of the pool is floor-priced. It is 122 — barely smaller — because once the ceiling itself reaches the floor it caps essentially everything. The mechanism was real, the conclusion drawn from it was not, and the counts converge for a reason the original reasoning missed.

  **And it was quoting a number that reproduces under no definition.** The floor count was cited as "563 of 705" — copied from `scenarios.py`, which has said it since 2026-08-13 against `data/players.csv` unchanged since 2026-07-05, so it was wrong when written rather than stale. Re-measured 2026-08-17: **534 of 705**, where floor means `round(expected_price, 1) == 0.5`. The definition now travels with the figure everywhere it appears, because the count runs from 0 (no player's expected price is exactly `MIN_SALARY`) to 604 (under $1M) depending on which one you pick. Corrected in `scenarios.py` (both comment blocks, along with "19 players above $4M" → 20 and "40 above $3M" → 36), `tests/test_scenarios.py`, `.claude/rules/pricing-pipeline.md` and this entry. The 2026-08-13 entry below still carries the old figures as written — it is the historical record of what was believed, and this paragraph is the correction.

  Three deliberate constraints, each load-bearing: it **never imports `main`**, so unlike the other two instruments it cannot touch a live draft even in principle rather than relying on a temp-dir redirect; it parses with `state._transaction_from_dict` instead of hand-copying nine key names (the hazard CLAUDE.md names for the backfills); and it **allowlists `transaction_type == "draft"`**, because a trade's `salary` is not a clearing price and `/trade-between` writes `"SRL→MAC"` into `team_code`, which would attribute spend to a team that does not exist. Records with `model_price <= 0` are excluded from the ceiling statistics and the count reported — `_log_transaction` defaults both prices to `0`, so a zero is missing data, and `market_price=0 < model_price=5` would otherwise read as a bind.

  Unlike `measure_layout.py` and `measure_ceiling.py`, the logic is a pure `summarize()` with **a real pytest test** (`tests/test_measure_spend.py` — 6 tests over `summarize()` with 6 mutations verified; this entry said 5, miscounted, and the file is at 10 after the `report()` guard above). That split is deliberate: `measure_layout.py` could not see a stale selector for two days, and `measure_ceiling.py`'s numbers now appear in four documents unchecked, both because their logic only exists inside a `__main__` nothing runs.

## [2026-08-17]

### Changed

- **Re-anchored the opponent-editable-panel backlog entry**, whose two line references had drifted: the entry moved from line 120 to 152, and the salary-box line it cites in prose moved to 179. The second was **already stale before this change**, pointing 17 lines off, and `tests/test_backlog_refs.py` was green on it the whole time — because for a non-`.py` path that test only asserts the file exists and the line is in range (`line <= total`), then returns. There are no symbols to anchor a template to, so **every** template reference in `BACKLOG.md` is checked for existence and nothing else; drift inside one is invisible. Filed as a test-infrastructure finding rather than fixed here.

  A first version of this entry blamed the missing `templates/partials/` prefix instead. That was wrong — the bare form is collected and does resolve — and it mattered, because it would have sent the next person to add a prefix and believe the reference was then verified.

### Fixed

- **A failed optimizer took BOT's whole buy list off screen without saying so.** `team_panel.html` gates both the Optimal Projected Points headline and every MILP target row on `milp.status == "Optimal"`, so an Infeasible solve removed them — **12 of 63 rows and 5.4KB**, with the word "Optimizer" appearing nowhere inside `#team-panel`. (Re-measured 2026-08-17 rather than quoted: the original finding said 12 rows and ~5.6KB on 2026-08-13, and the row count is unchanged while the panel has grown enough to move the byte figure.) The badge that explains it renders in `#bid-panel`, which is `.area-auction`: **grid column 1 against the team panel's column 3** at 1024px+, and a long scroll away in the 1-col mobile layout. So your own roster lost its plan while the reason sat at the other end of the screen.

  Reachable through ordinary draft-day play, which is what moved this up the list. Not through *bidding* — the commissioner refuses any bid that would leave a team unable to fill 24 (owner decision 2026-08-06) — but buyout penalties, `/trade-between` and `/adjust-salary` all raise cap load and **warn rather than refuse**; $20.5M of penalties on a fresh BOT reaches it, confirmed again 2026-08-17 by walking the figure up until the solve broke. A buyout is a draft-day action, and this is what the panel did immediately afterwards.

  **The original entry framed the fix as "a second warning that duplicates a message already on screen", and that framing was the reason it sat deferred for four days. It is not a second warning.** The two panels are answering different questions — `#bid-panel` says the *numbers* are floor fallbacks, `#team-panel` says the *content* is missing — so the fix is a quiet empty state (`text-xs opacity-60`, the idiom `buyout_panel.html` and `trade_panel.html` already use for "nothing here, and why") where the headline was. Two yellow alerts side by side saying nearly the same thing is the wallpaper failure `test_a_solvable_optimizer_shows_no_warning` exists to prevent from the other direction. The note is self-sufficient rather than pointing at the Bid Advisor, because that panel has no visible heading to send anyone to.

  Gated on `status`, never on "no targets": a *finished* BOT has empty targets too, since every player in `milp.roster` is already owned, and telling a team that just filled its roster that the optimizer failed would be worse than the original bug. Gated on `is_my_team` in both arms — the MILP is BOT-only, same reasoning as the buyout dots, so an opponent's panel says nothing rather than reporting BOT's optimizer as though it described them.

  Three tests, and **the mutation map is not the one the plan predicted** — recorded because the plan's table was wrong and the docstrings were corrected against measurement, not reasoning. `test_the_team_panel_says_why_the_buy_list_is_gone` uniquely kills deleting the `elif`. `test_a_solvable_optimizer_leaves_the_team_panel_quiet` uniquely kills splitting the note out into a separate `if viewed_team.is_my_team` — the obvious "simplification", which stays BOT-only but stops being exclusive with the healthy headline. `test_an_opponents_panel_stays_silent_about_bots_optimizer` uniquely kills both `elif` → `{% else %}` and dropping `is_my_team`; neither is visible while the view is on BOT, which is why it has to `GET /team-view` first.

## [2026-08-16]

### Added

- **`tests/test_market.py::TestWhenTheCeilingLeavesTheCap`** — where the idle ceiling stops being `MAX_SALARY`. It is the second-highest of ten, so it holds at the cap until **all but one** opponent is priced out, not until the league is broke. That threshold is what makes the `min` inert early and nobody had written it down, so a change to `physical_max_bid` or to the second-highest rule would have moved it silently. Two tests: a walk that squeezes the real 11-team league one opponent at a time and pins the whole transition, and the single state it turns on stated alone, so a mutant that shifts the threshold says which end moved.

### Fixed

- **`test_second_highest_is_ceiling` could not fail.** Found while pinning the threshold above, in the test named for the rule the whole layer rests on. `_make_league`'s OPP1 and OPP2 both come out at `physical_max_bid = 11.4` — clamped at `MAX_SALARY` — so its two assertions read `11.4 == 11.4` twice and passed just as happily against a `compute_market_ceiling` returning the **highest**. Verified by mutation: the whole of `tests/test_market.py` was green against that change except the two tests added the same day. Heavier keeper salaries put all three opponents below the cap and distinct, plus an explicit guard on that separation so the next budget tweak fails loudly instead of quietly disarming it.

### Investigated

- **Does the market layer's ceiling ever change a planning price?** Closed with no engine change; the answer is "it depends entirely on how fast the league spends", and the two ends of that range are far enough apart that neither can be quoted as *the* behaviour.

  `market_price = min(model_price, market_ceiling)` is what the MILP plans on. Nothing in the suite could say whether the ceiling half of that `min` ever fires, because it is a property of a whole auction rather than of any one state. `tests/measure_ceiling.py` (new — an instrument, named like `measure_layout.py` so pytest ignores it) runs a full 165-pick auction and reports it per pick, under two spending models:

  | buyers pay | ceiling binds | first bind | distinct ceilings | league cap unspent |
  |---|---|---|---|---|
  | the tool's own market price | 0 of 165 | never | `[11.4]` | 18% ($59.4M of $337.7M) |
  | what the reserve rule allows (`--drain`) | **133 of 165** | pick 32, $7.3M | `[0.5, 4.5, 7.3, 11.4]` | 0% |

  *(Pick numbers in this entry are the 0-based indices the instrument printed at the time. It was renumbered to 1-based ordinals on 2026-08-17 — a fresh run of the same regime reports 33 and `@1 / @33 / @41 / @44`. Left as published rather than restated, because the numbers are the record of what the run said; the clause is here so nobody reads the one-pick difference as a behaviour change.)*

  **The triage that opened this reported only the first row, and concluded Layer 2 contributes nothing to planning. The `--drain` run killed that.** The ceiling binds readily, stepping `11.4M@0 -> 7.3M@32 -> 4.5M@40 -> 0.5M@43` — at the floor in a quarter of a draft. What produces the pinned run is not an inert layer, it is that paying exactly the model price is the one behaviour the model cannot be wrong about: it leaves 18% of the cap unspent, and three teams (JHN $19.8M, GVR $14.1M, VPP $12.0M) finish above the line, one more than the second-highest rule needs. A real draft sits somewhere between the rows, and where it sits is now a measurable question rather than an argued one.

  Supporting evidence read the same way from the other side: `scenarios.py` already needed `_scenario_endgame_ceiling_binds`, whose `_drain()` helper buys players purely to force spendable budgets down — the repo had to construct the binding case by hand once before.

  **A second claim from the same triage, also killed by measurement.** It reported that `stop_status` would therefore read `at_cap` for the entire draft and the panel's "Should win it" figure would never show a number. Wrong: `/bid-check` builds its own `MarketInfo` from `compute_live_ceiling` over the *named bidders only* (`main.bid_check`), not the idle ceiling. Over every matchup rather than a sample — 10 single-rival and 45 two-rival, since an average would hide that the same rich teams pin it — the live ceiling is below `MAX_SALARY` in **7/10** and **21/45** by mid-draft, against 0/10 and 0/45 on a fresh state. The advisor's forecast fires routinely. Reasoning from one ceiling to the other is the specific mistake, it was made twice here, and `.claude/rules/pricing-pipeline.md` now says so under the Critical rule.

  Left behind: the numbers in `.claude/rules/pricing-pipeline.md` and one clause on CLAUDE.md's three-layer row; the open design question in `BACKLOG.md` (should a planning price be demand-aware rather than a second-highest-of-ten nobody reaches?), deferred deliberately because changing it moves every bid recommendation in the tool and there is a draft coming; and a note in `test_dry_run.py` that its 40 picks are a stopping point, since full length is now covered on demand.

## [2026-08-15f]

### Changed

- **Both trade forms use checkbox lists instead of `<select multiple>`.** The last of the 2026-08-07 wants — *"both sides are cramped and the multi-select affordance is not obvious; this is the panel most likely to be used under time pressure during a break"* — and "cramped" understated it. Measured at the 1280px width the draft is run at, with both `<details>` forced open:

  | Control | Rendered | Shows | Widest option needs | Clipped by |
  |---|---|---|---|---|
  | Evaluator / I Give | 183px | 5 of 49 | 316px | **157px** |
  | Between / sends | 120px | 3 of 49 | 229px | **133px** |
  | Between / to team | 120px | — | 198px | **102px** |

  `Tony DeAngelo (NCM) (D, $3.0M, 11pts) (M)` rendered as about `Tony DeAngelo (NCM) (D…` — the salary and the points, the entire reason the label exists, past the edge. Still clipped by 104/97/66px at 1600, so not a narrow-viewport artifact. After: every list 379px at 1280 and 485px at 1600, content fitting inside with grid overflow 0 at 1024/1280/1600.

  **Stacking to one column is what buys the width**, not the checkboxes: the panels are 415px at 1280, so `grid-cols-2` and `grid-cols-3` were dividing that into 183px and 120px. Unconditional rather than a breakpoint, because two columns of the 521px panel at 1600 is still only 236px against 316px.

  The affordance half was the other half of the want and is what the checkboxes answer. A plain click in a `<select multiple>` **silently discards every prior selection**, and in a 3-row window onto 49 options there is nothing on screen to notice it by. Each block now carries a running `N selected · $X.XM`, so the state is visible without scrolling the list.

  **Checkboxes sharing a `name` serialize exactly like a multi-select**, so `/trade-evaluate` — already `form.getlist("give_player")` — was not touched at all, and its existing tests passing unmodified is the equivalence proof rather than a new assertion. `/trade-between` moved from a comma-joined hidden field to `list[str] = Form([])`, deleting `updateTradeHidden` and two hidden inputs. That was never a live bug — no name in the pool has a comma and `_disambiguated_names` cannot add one — just a hand-rolled encoding where the form already had one.

  **The two JS-built halves became one builder**, which closes the tracked "(M)" finding at the cause rather than by testing two copies. (No `file:line` for it here on purpose — a resolved write-up describes code that has moved, and the line it cited now points at something unrelated while still resolving, which is exactly the rot `test_backlog_refs.py` cannot catch.) `loadTeamPlayers` (inline in the template) and `loadTradePartner` (`shortcuts.js`) differed only in the value and whether the label carried points, and the duplicated `(M)` suffix in them was the tracked finding — deleting either left the suite green. `trade_panel.html` now has no inline `<script>` at all.

  Four of the six unnamed controls filed earlier the same day are named here, and the guard is written as the general rule — every `.choice-list` and every `<select>` in either form must carry an `aria-label` — so a fifth list added later is caught. Each `<label class="choice-row">` names its own checkbox by wrapping it. That leaves one control outstanding, the bid panel's price input.

  One thing the tests found rather than reasoning: after stacking, the widest row still wanted **395px against a 377px list**, because the rows inherited larger text than the `select-sm` they replaced. `.choice-row` is `text-xs`. And one accepted rough edge, measured rather than assumed: at **1024px** the widest Give row wants 305px against a 293px list and scrolls 12px inside its own container — the draft runs at 1280–1600, and scrolling 12px is a different class of thing from clipping 157px in silence.

  `white-space: nowrap` on the rows is safe only because of the scroll container: `overflow-y: auto` forces `overflow-x` to compute to `auto` (CSS Overflow §3.2), so a long row scrolls inside the list and can never set its grid column's min-content. Without it this would be the 2026-08-11 off-screen-panel bug again.

## [2026-08-15e]

### Changed

- **The Buyout Analyzer's candidate list is a picker, not a row of ~15 buttons.** Owner decision 2026-08-08, which superseded the 2026-08-06 call closing this as already-built on the grounds that buttons beat a dropdown. What changed is the count, and it is now measured at the width the draft is actually run at rather than argued: at **1280px each label is too wide to share the 415px column**, so the fifteen buttons sat on fifteen rows — 468px of list inside a **587px** panel. Worse at the draft width than at 1600px (398px, nine rows), and growing, since the candidate set is `all_players|selectattr('can_be_bought_out')` and BOT drafts group 2/3 players all day.

  After: **149px at both 1280 and 1600**, a 27px picker. The number that matters is not the 75% cut but that it is now *the same at both widths and independent of the candidate count* — the panel no longer grows with the roster. `test_the_open_panel_stays_short` pins it at under 250px, generously, and goes red against a revert to the buttons.

  The name moved from a path segment to a query parameter — `GET /buyout-check?player_name=` — and that is what lets the picker be a bare `<select name="player_name">` with **no wrapper, no submit button and no JS**: htmx sends a triggering select's own value on a GET. That last claim is about htmx's runtime rather than about markup, so it was confirmed in Chrome and not reasoned: renaming the select's `name` attribute is one of the mutations the browser test dies on. (`hx-trigger="change"` turns out to be redundant — it is already htmx's default for a `<select>` — and is kept only for symmetry with the salary input beside it, which is explicit for the same non-reason.)

  Three details each fix a specific way this breaks, and each has its own test. The **empty first option is load-bearing**: without it the top candidate is pre-selected and choosing him fires no `change`, so the first click on the most likely buyout would do nothing. That one is asserted in the endpoint suite rather than the browser on purpose — Playwright's `select_option` dispatches `change` unconditionally, so the harness that looks like the right place to catch it physically cannot. The **checked player is re-selected** on render, because the response replaces the whole panel including the select, and without it the box snaps back to the placeholder while a verdict for someone else sits underneath it. And the **empty case says so in words**, because an empty `<select>` is a control that looks broken, where the old flex div correctly rendered nothing at all.

  Two corrections to the backlog entry, both of which changed the work:

  **The dots are not in the Analyzer, and moving them in would be the defect.** The entry said that if the list collapsed to a `<select>` the dots would "need somewhere to live". They already have somewhere: the `bo-` placeholders are in `team_panel.html`'s active-roster and minors tables, and the Analyzer's buttons never carried one — so collapsing it touches the scan not at all. Duplicating them into the picker would collide, since `_dom_id` mints exactly one id per player and htmx resolves an out-of-band target with `querySelectorAll("#"+id)`. `test_the_picker_carries_no_buyout_dots` now pins the decision rather than leaving the next reader to re-derive it.

  **The path form carried an encoding hazard the entry did not mention.** `hx-get="/buyout-check/{{ p.name }}"` was the one place in the app a raw player name went into a URL unencoded — the two other name-in-path call sites both use `|urlencode`. Not currently exploitable: none of the 953 loaded names carries `#?%&+`. But `_disambiguated_names`' last-resort tier is ` (#n)`, a `#` never reaches the server at all, and the panel would then answer "not found" about a player sitting on the roster in front of you. A data refresh is exactly what turns that tier on. The query parameter removes it structurally rather than needing a `|urlencode` patch that the next template could forget.

- **The offered set has one reader.** Three tests learned which players the Analyzer offers by string-matching each button's URL (`f"/buyout-check/{p.name}" in html`), which coupled them to the markup *and* to the route shape — and this change moved both. `tests/helpers.buyout_options` replaces them, scoped to `#buyout-panel` because the same names appear in the roster tables beside it.

- **The "one expression" invariant is finally stated as one equality.** CLAUDE.md has said since 2026-08-07 that the scan, the dots and this list deliberately read the same `all_players|selectattr('can_be_bought_out')`, but the tests only covered ineligible-are-absent and minors-are-present — neither notices an eligible *active* player going missing, which is the direction the 2026-08-07 bug failed in (11 of BOT's 15 hidden, indistinguishable on screen from "no buyout helps"). `test_it_offers_exactly_the_eligible_set` asserts set equality and dies against a `roster_players` copy.

- **The previous batch was filed under `## [2026-08-16]`.** Every commit in it landed on the 15th; corrected to `2026-08-15d`, matching the a/b/c convention the rest of the file uses for same-day batches.

## [2026-08-15d]

### Added

- **An RFA/UFA filter in Available Players, and filters now survive a pick.** Three states rather than an "RFA only" toggle, because per the CBA a nomination turn is 1 RFA + 1 UFA — "show me the UFAs" is exactly as real a need as the other half. Rows needed no new attribute: `is-rfa` is already on the `<tr>` and already load-bearing for the yellow left border, so reading the class beats restating the same fact in a `data-rfa`.

  Both selections funnel through one `applyPlayerFilters()`, which is the only thing that writes `row.style.display`. Two filters each writing it directly would fight — whichever ran last would win and silently discard the other, so picking F after RFA would quietly show non-RFA forwards. That is the mutation `test_position_and_rfa_compose` exists for.

### Fixed

- **Every Available Players filter was wiped by every pick, and had been since the position filter was written.** `/assign` returns `all_panels.html` into `#app`, which re-renders `bid_limits.html` from the template: inline row styles gone, buttons back to All. Over a 150+ pick draft that is constant, and it would have hit the new RFA filter hardest, since hunting the RFA half of a nomination turn is exactly what you are doing when picks land. The filter state now lives in a JS variable re-applied on `htmx:afterSwap`, guarded twice — an early-out when nothing is filtered, so the common case costs nothing, and a check that the swapped subtree actually contains the table, since `afterSwap` also fires for every `#bid-panel` and `#team-panel` swap and re-styling 705 rows on each would be waste on the request path.

  Deliberately unlike the Logs tabs, which reset on purpose so a pick lands you back on Auction. A tab is a place you are looking; a filter is a search you are in the middle of.

  Button state is restored alongside visibility. Restoring the rows while the buttons still read "All" is its own bug — you would believe you were seeing the whole pool — and `test_a_pick_does_not_clear_the_filter` asserts both halves.

- **The filter bar got `flex-wrap`, and it made the panel *less* likely to force its column.** Measured: min-content **283px → 139px**, because a wrapping flex container is bounded by its widest item rather than by the sum of them. Grid overflow stayed 0 at 375/1024/1280. Without the wrap, three more buttons on a `flex items-center gap-4` with no wrapping would have pushed the legend past the panel edge at the narrow end.

### Investigated

- **A grill pass added group labels and corrected a tooltip that misdescribed its own control.** The filter bar announced "All, F, D, G, All, RFA, UFA" — two groups, both starting with a button reading "All", and nothing to tell them apart; each group now carries `role="group"` and an `aria-label`, the same gap the `&times;` close buttons and the League State done glyph had. The guard is written as the general rule (every `.flex.gap-1` group in the bar must be named), because a third unlabelled group is invisible on screen and the two specific assertions would not see it. A third test pins the *premise* — if the duplicate "All" ever goes away, the labelling tests stop protecting anything and should be reconsidered rather than left as decoration. The salary box's new `title` read "saves when you leave the box", which this session's own measurement contradicts: Enter and the spinner arrows save too, so it now names Enter as well.

- **Three interactions were measured rather than reasoned about, and all three were already correct.** `closest .salary-edit` picks the right row when the panel holds twelve of them — the earlier probe had only ever edited the first, which cannot distinguish a correct scope from a broken one. Sorting composes with a filter (22 RFA rows before and after a PTS sort, still all RFA, still renumbered 1..N). And `/undo` and `/reset` both preserve the filter, which is consistent with the project's existing rule that a reset clears what describes *this boot* and keeps what describes the data or the view — the buttons visibly show the state either way.

- **Two of the three wants picked up for this batch were already built**, which is the second time that has happened (the Buyout Analyzer closed the same way on 2026-08-06). The bid panel's marginal-value tooltip matches its want almost word for word, down to "A big gap either way is the tool working, not a mispricing", and the Sigma tooltip lives on the price chart's meta line and is *already placement-checked by name* in `TestTooltipsStayInsideTheirPanel` — a live bid auto-loads the chart into the bid panel's own mount, which is why a suite that never clicks a player name still measures it. `BACKLOG.md` now records where both live rather than just deleting the bullets, so a third re-investigation is not needed.

- **Nothing had ever exercised a filter.** The pre-existing coverage asserts that `data-position` attributes exist (`test_htmx_interactions.py`, `test_dry_run.py`) and stops there — the filtering is JavaScript, so `TestClient` cannot reach it. That is how the reset survived unnoticed. `TestAvailablePlayerFilters` is browser-only for the same reason, and all four of its tests go red against the matching mutation.

## [2026-08-15c]

### Fixed

- **One salary correction was two edits, and `Ctrl+Z` looked broken.** The salary box posts itself on `change` and also sat inside a `<form>` that posted, so pressing Enter fired implicit form submission *and* the change event, while the `$` button blurred the input (change) then submitted (form). Measured in Chrome: **one Enter press produced 2 POSTs, 2 change-log rows — the second a no-op reading `"$4.6M → $4.6M"` — and 2 snapshots**, so the first `Ctrl+Z` reverted the no-op and the salary did not move. On the one control whose entire job is fixing a mistake mid-draft, an undo that appears to do nothing is the worst available failure: you conclude undo is broken and stop trusting it.

  The wrapper is now a `<div class="salary-edit">` with no `hx-post`, so there is no implicit submission and Enter can only fire `change`; `hx-include` targets the wrapper by class rather than `closest form`, since htmx gathers the hidden inputs from any matched element. Re-measured after: 1 POST, 1 log row, 1 snapshot, and one `Ctrl+Z` restores the old salary.

- **The `$` submit button is gone**, closing the 2026-08-07 want — and its stated precondition is now measured rather than assumed. All five input paths auto-submit exactly once: type+Tab, type+Enter, the native spinner arrows, type-then-click-elsewhere, and paste+blur. The button was not a harmless fallback; it was one of the two double-submit paths. The input gained a `title` explaining that it saves on leaving the box, since removing a visible control removes the affordance that said so.

### Investigated

- **Every existing `/adjust-salary` test posts the endpoint directly, which is why this shipped unnoticed.** A duplicate request originates in the *browser*; `TestClient` cannot produce one, so eleven endpoint tests and two browser tests all passed against it — the two browser ones drive the control via `fill()`+`blur()` and `htmx.ajax`, neither of which is a doubling path. `TestASalaryCorrectionIsOneEdit` covers the three that matter (one POST, one undo, one log row) and all three go red against the old markup.

## [2026-08-15b]

### Fixed

- **Every sortable column in the app now actually sorts; one of them never has.** The Available Players NHL header carried `data-sort-col`, an `onclick` and `cursor-pointer` since the column was added, but `sortTable` compared `cell.textContent` and the cell holds only an `<img>` — measured **0 of 705 rows with any text**, so every key tied, the stable sort was a no-op, and clicking left the order byte-identical. A control that looks live and is inert is worse than no control: mid-draft you assume the sort took and read the wrong row.

  Fixed in shared JS rather than by removing the affordance, because sorting a 705-player pool by club is plausibly useful: `cellSortText()` falls back to the cell's `img[alt]`, which is already the club code and already what a screen reader announces, so the sort order matches what the column communicates. Verified in Chrome — ascending puts ANA first and descending WSH, all 705 rows preserved, no console errors. The two NHL headers in `logs_panel.html`, made deliberately non-sortable a day earlier *because* the mechanism did not exist, are sortable again.

  **An empty cell is not an inert column, and the distinction is load-bearing.** The RFA column is blank for 683 of 705 players and carries a prior team for the other 22; grouping those 22 is exactly what clicking it is for. A first pass at the guard sampled only the first body row and would have condemned it.

### Investigated

- **The guard that was supposed to catch this had been scoped to the wrong panel.** `test_no_header_offers_a_sort_that_cannot_work` shipped 2026-08-15 inside `TestTheLogsPanel`, written as "the general rule rather than the instance" — but scoped to one panel it could never see `bid_limits.html`, where the live bug was, in the table scanned most during a draft. Replaced by `TestEverySortableColumnCanActuallySort`, which walks every `<table>` in `GET /` and every body row. Three tests, because the HTML-level ones cannot see JavaScript: two assert no column is inert and that at least one genuinely depends on the fallback, and a third reads `static/shortcuts.js` to assert `sortTable` still routes through `cellSortText` and that the `img[alt]` branch is still there — without it, reverting the fix left both HTML tests green, since they simulate the fallback rather than run it.

## [2026-08-15]

### Added

- **The three log wants, in one panel: Auction / Transaction / Change tabs, clickable teams, and logos.** `transaction_log.html` and `change_log.html` are gone, replaced by `logs_panel.html` plus two shared row fragments. The tabs are CSS-only — three radio inputs sharing a name, with `.tab-content` siblings — because every row is already in the response and a per-tab round trip would rebuild the whole ~8.5ms context (including the 704-row `bid_limits` list) to filter rows the browser is already holding. Nothing was vendored: `.tabs`, `.tab`, `.tab-active` and `.tabs-boxed` are all in the trimmed DaisyUI build, so the no-CDN rule is untouched. The selected tab lives only in the DOM and therefore resets on every `all_panels.html` swap, i.e. on every `/assign` — Auction is `checked` so that reset lands on the tab you want mid-draft. Verified in Chrome: three tabs, exactly one panel visible at a time, correct row counts per tab, and no console errors.

- **`TransactionRecord.nhl_team`**, so the log can show an NHL club badge. Denormalised deliberately: **the log outlives the roster.** A bought-out player is on no roster *and* gone from the pool, so resolving the club by name at render time draws nothing on precisely the rows the Transaction tab exists for — which is what `test_the_nhl_club_outlives_the_roster` pins, and it is the only test that distinguishes the stored field from a lookup.

### Investigated

- **A grill pass found two things in the new panel and one pre-existing bug behind them.** Fixed here: the NHL column's header was clickable and inert (`sortTable` reads `textContent`, the cell holds only an `<img>`, so every row tied and the order never moved — measured in Chrome), now non-sortable and pinned by `test_no_header_offers_a_sort_that_cannot_work`, which states the general rule so a future icon column is caught too; and the two row fragments had drifted into two contracts, `_log_team_link.html` taking an explicit `row` while `_log_nhl_logo.html` read the loop variable `t` by name — both now take `row`. The pre-existing half is `bid_limits.html:37 (data-sort-col="3")`, which has the same dud sort on its own NHL column, filed rather than fixed because the right answer there is an `img[alt]` fallback in shared JS, not removing the affordance.

- **Two claims in the panel were checked in Chrome rather than read off the CSS.** The trimmed DaisyUI build does carry `.tab:is(input[type=radio]):after{content:attr(aria-label)}`, so the labels render as real text — confirmed as "Auction (6)" / "Transaction (2)" / "Change (0)" at 27px tall, clearing WCAG 2.5.8's 24px. And the grid stays contained at 1280 on **all three** tabs (overflow 0 each), not just the default one, which the browser suite alone would not have shown.

- **Tailwind's Play JIT costs nothing per htmx swap, so the vendored bundle stays.** The open backlog entry deferred a real build on tooling risk; the reason to revisit was a hypothesis that its MutationObserver re-scans on every panel swap, which across a 150-pick auction would be a genuine draft-day cost. Measured in Chrome instead of reasoned: injected CSS grew **72 characters on the first `/bid-check` swap and 0 on every swap after**, with one longtask on that first swap and none subsequently — the JIT only compiles classes it has not seen, and after the first render every class in the app is already compiled. The load cost is real (398KB transferred, ~560ms of longtasks, DCL 868ms) but it is paid once, and the app is opened once on draft day. The entry stands, on stronger grounds than it was originally written with.

- **Three tests were written, watched pass, and then found unable to fail** — recorded because the failures were all in the *test*, not the code. (1) A backfill test asserted `/nhl_logos/<club>.svg` against the whole page; the drafted player is on BOT's roster, whose table renders the same URL, so it passed with the backfill deleted — fixed by scoping through `section_of`. (2) Counting `<tbody>` blocks to get per-tab row counts silently renumbered the tabs whenever one rendered its empty state instead of a table; fixed by slicing on the three radio inputs. (3) Nothing covered a *live* pick's badge — deleting `nhl_team=p.nhl_team` from `/assign` left the buyout test and both legacy-file tests green, because the backfill refills the field on the way in and so hides a writer that stopped passing it.

### Fixed / decided

- **"NHL team logos in both logs" was not buildable as written, and the plan said so before any code moved.** `ChangeRecord` is `timestamp`/`kind`/`team_code`/`description` — no player at all — so there is nothing there to resolve an NHL club from. Settled as: FCHL team logos in **both** logs (from `teams[code].logo`, the same expression `league_state.html` uses), NHL club logos in the transaction log only.

- **The Auction/Transaction split is a TOTAL partition (`draft` vs everything else), deliberately against CLAUDE.md's allowlist rule for `transaction_type`.** That rule exists because a mis-routed value points `_viewed_team` at the string `"SRL→MAC"`. Here the failure inverts: a record matching no branch **disappears from the draft record entirely**, which is worse than one landing in the wrong tab. So the two lists always sum to `len(transaction_log)`, a new transaction type is visible by default, and a test asserts the sum across three different writers. The `"SRL→MAC"` hazard is handled where it actually bites — `_log_team_link.html` renders a `/team-view` link only when `row.team_code in teams`, and plain text otherwise. CLAUDE.md now records this as the one deliberate exception rather than leaving the two rules to look contradictory.

- **The plan's own call-site table was wrong about `/trade-execute`.** It assumed `PlayerOnRoster`; the endpoint actually works with `PlayerTrade`, a DTO with no `nhl_team` — and its *receive* side is assembled from **client-submitted JSON**, so threading the field through there would mean trusting the browser for a value the log keeps forever. Resolved with `_nhl_team_of()`, a lookup used at that one site and nowhere else, on the grounds that a trade always leaves every player somewhere (a roster, or the pool when there is no source team) and so cannot come up empty the way a buyout can. The distinction is written into the helper's docstring, because "why is this a lookup here and a stored field there" is exactly the question a later tidy-up would get wrong.

- **Legacy save files still load.** `_transaction_from_dict` reads the new key with `.get`, and that is load-bearing rather than defensive: `_load_saved_state` treats *any* parse exception as an unusable file and renames it `.corrupt`, so a bare lookup would discard a byte-perfect draft on the first boot after this change — at the exact moment every real save file is a legacy one. `_backfill_nhl_teams` then refills historical records from the same `players.csv` map it already builds for rostered players. Both halves are pinned, and they needed *separate* tests: the `.get` keeps the file parsing, the backfill keeps pre-upgrade picks from showing a blank badge for the rest of the draft.

## [2026-08-14]

### Added

- **Two things that competed with the bid advice mid-auction can now be dismissed.** Both from the 2026-08-07 testing pass. The counterfactual gained a close button — it auto-loads under the live bid advice on every whole-panel swap and previously could not be got rid of. It closes with `this.closest('.counterfactual-card')`, never an id, because that body is mounted twice (the `#explanation` panel from the players table's "?" links, and inline under the bid panel). And a nomination recommendation now disappears once you have acted on it.

  **Only the half you acted on goes**, which the original request did not distinguish: per the CBA a nomination turn is 1 RFA + 1 UFA and *an RFA sale keeps the turn*, so hiding both would delete the next thing the operator needs. Removal happens on `htmx:afterRequest`, not on click, because **htmx aborts an in-flight request whose triggering element leaves the DOM** — the naive version would cancel the very `/bid-check` the button exists to start. It is gated on `event.detail.successful`, so a failed request leaves the recommendation on screen; `/nominate` is the only way back. Implemented as a listener in `shortcuts.js` rather than a server-side out-of-band swap: `/bid-check` deliberately does not touch the nomination panel (that panel split is what stopped price changes wiping the recommendations), and re-coupling them for this would undo it.

  **Three of the four mutation checks on the browser tests initially survived, and each one exposed a real gap.** (1) Closing the *bid panel's* counterfactual cannot distinguish `closest()` from `document.querySelector()` — `all_panels.html` puts `.area-auction` before `.area-players`, so the bid panel's card is already first in document order and both implementations return the same element. The test now closes the `#explanation` card, the one that is not first, which is where they disagree. (2) With that fixed, `getElementById('explanation').remove()` still passed: it takes the card with it. So the test now also asserts the **mount survives its contents** — destroying `#explanation` would remove the target every future "?" link swaps into, the same failure `buyout_scan.html`'s unconditional wrapper exists to prevent. (3) The abort hazard is pinned by asserting the bid panel is actually bidding on the player afterwards; without it the test passes against a build that dismisses the card and fires nothing.

### Fixed

- **Both glyph close buttons had no accessible name — including the one added the same day, one commit after arguing that they need one.** A grill of the change above found `&times;` with no `aria-label`, `aria-labelledby` or `title`, so the button's accessible name computes to `×` (U+00D7) and is announced as "times". Not a WCAG 4.1.2 failure — a name exists — but a useless one, and directly inconsistent with the League State done toggle shipped a day earlier, whose commit message says in as many words that replacing text with a glyph strips the accessible name. `player_chart.html` had the identical gap and is the template the counterfactual's button was copied from, so fixing only the new one would have left the next copy wrong; both are labelled together, with a `title` for hover as well. Pinned in both templates, each mutation-checked by stripping the attribute.

- **The claim that a failed request keeps the recommendation on screen is now measured rather than reasoned.** The `event.detail.successful` guard was written into CLAUDE.md as a rule on the strength of the htmx contract alone. It is now pinned by intercepting `/bid-check` with a 500 and asserting the RFA block survives — dropping the guard reddens it. This is the case that matters most: without it a failure loses the recommendation *and* produces no bid, and `/nominate` is the only way back.

- **`TestExplain` had a silent trap and an assert-nothing test, in the code path being changed.** `test_explain_player` fetched a hard-coded `Sidney Crosby` and asserted `"Counterfactual" in r.text` — but "Counterfactual" is the *panel heading*, present in the 267-byte empty state too, so once that name left `players.csv` the test would have passed against a page holding no counterfactual at all. It now derives the name from the pool and asserts on `.counterfactual-card`, and pointing the derivation at a missing player reddens it where the old form stayed green. `test_explain_invalid` asserted only `status_code == 200`, which is true of every response the endpoint can produce; it now pins the empty state by size and by the absence of the card. Two new cases cover what the close button depends on: both mounts render it, and the inline mount carries no `id="explanation"` — previously only the mount's `inline=1` *attribute* was checked, never the response's id-freedom.

## [2026-08-13]

### Added

- **`endgame-ceiling-binds`, a scenario for the half of the market ceiling a fresh reset cannot reach.** On `/reset` all 11 teams sit at `physical_max_bid` = 11.4, so the ceiling *is* the salary cap, every bid reports `stop_status = at_cap` with no forecast, and **not one** row in Available Players is `capped` — which is why the app's only `tooltip-left` had never been placement-checked in a browser. Three backlog entries were parked on states this reaches. **Two plausible designs were built and measured first, and both produced zero capped rows**, which is the part worth recording: draining budgets by buying the best available got the ceiling to $5.2M and capped nothing, because buying top-down removes exactly the players whose model price the ceiling would have capped; and reserving the top 40 while draining with mid-tier depth put the ceiling back at $11.4M, because the price distribution is far steeper than it looks — 19 players above $4M, 40 above $3M, and **563 of 705 at the $0.5M floor** — so reserving 40 reserves everything over $3.0M and teams fill all 24 spots with floor-priced depth while staying rich. The distribution is also why this state never arises by accident: pool value is ~$632M against ~$338M of league money for ~165 open spots, so a real draft consumes the entire expensive tier and drained budgets never coexist with unsold stars. What produces it is a rule the league already has — **a done team is excluded from the ceiling** — so the scenario marks most teams done, drains the two still bidding, and holds back the top of the pool. Result: ceiling **$2.8M**, **83 capped rows**, McDavid $9.5M → $2.8M, BOT's MILP **Optimal** at 1311 points, and the advisor reporting `stop_status = live` with a real `expected_stop` instead of a dash. `_drain` converges rather than overshooting, and the arithmetic is the interesting bit: seating a player at price P costs P of budget but frees one reserved spot, so `spendable` moves by `-(P - MIN_SALARY)` — hence a `P <= headroom + MIN_SALARY` bound to avoid crossing the target and a `P > MIN_SALARY` bound to make progress, because a min-salary buy leaves `spendable` **unchanged** and without it the loop never terminates while floor-priced players remain. The first version lacked both and landed **both** live opponents on the same $0.9M max from targets of $3.0M and $2.2M, which makes `second_bidder` meaningless. Mutation-checked four ways — no team done (5 tests), nothing reserved (1), the overshooting drain restored (1), the `<option>` removed from `base.html` (2) — and the nothing-reserved mutant initially **survived all 13 tests**, because the star-check asked whether `max(available)` was expensive, which is very nearly a tautology: whatever is left is by definition the richest thing left. It measures against the draft-time pool now and names the stars that got sold. Also added a two-way guard that `SCENARIOS` and the navbar picker cannot drift apart, mirroring `TestShortcutsModal` — a registered scenario with no `<option>` is invisible, and an `<option>` with no scenario looks available and answers with an error toast.

- **The `tooltip-left` in Available Players is placement-checked at last**, and required by name so the coverage cannot quietly lapse. `TestTooltipsStayInsideTheirPanel` runs the endgame scenario at 375/1024/1280 on top of its seven fresh widths — three rather than seven because each costs a page load plus a live bid and the risk does not vary smoothly (375 is the 1-col case, 1024 the tightest 3-col track, 1280 the draft width). It passes. The **vertical** exposure `BACKLOG.md` predicted for it is real but bounded, and measured rather than asserted: the bubble is 99px tall against ~64px rows, so on the last row visible inside the 405px `.scroll-container` it overhangs by **~25px**, and scrolling one row cures it. No assertion was added deliberately — a naive vertical check flags every row below the fold, since an unscrolled row is trivially outside the client box, which is the same mistake as the original probe measuring against `innerWidth`.

### Changed

- **League State answers by code and by glyph.** Two owner-testing items from 2026-08-07: the Team cell dropped the full team name (up to 23 characters — "Johannesburg Israeleafs") in favour of the three-letter code alone, and the Done toggle dropped "Stopped Drafting" / "Drafting" for `✕` / `○`. The name moved to a native `title` on the link, following the bidder toggle in `bid_panel.html` — deliberately **not** a DaisyUI `data-tip`, since that inventory already has an open entry about 8 of 20 tips never being placement-checked and the densest table in the app is the wrong place to add a ninth. The button kept its form, its colour encoding and its action `title`, and gained an `aria-label` carrying the *state*, which is what stops a glyph stripping the control's accessible name. `○` rather than a blank for the drafting case: this button excludes a team from every market ceiling, and that is the wrong click target to shrink to nothing.

  **The width premise behind both items was wrong, and measuring said so.** min-content went 955px → **868px** — 87px, against the ~200px the plan predicted — because a table's min-content is each column's longest **word**, not its longest string. "Johannesburg Israeleafs" wraps, so that column's floor was only ever "Johannesburg"; the button wrapped too, so its floor was "Drafting". A per-column pass afterwards found the more useful fact: **all 12 columns are now floored by their own header text**, summing to exactly the 868 (Remaining 102, Spendable 100, Cap Used 92, Max Bid 83, Penalty 81, …). Nothing in the table body can narrow it further — only shorter headers can, which is a legibility trade on a dense grid of money figures, so it went to `BACKLOG.md` as a measurement rather than being decided here. The table still overflows its column at every width measured including 1920, so `.table-scroll-x` remains load-bearing and the CLAUDE.md rule behind it is unchanged. Confirmed while planning that the Penalty column renders on a **fresh** state (JHN and LGN each carry $0.3M), so these are 12-column worst-case numbers rather than a best case. Three tests, each killed by a different mutation; the first needed a second draft, because "the name is not in the panel" is false by construction once the name is a `title` attribute — it counts occurrences against `title="…"` instead, so restoring the span fires it.

### Fixed

- **This table had been recorded as having 13 columns, in four separate documents, since 2026-08-11. It has 12.** Found by a grill of the change above, whose new comment managed to say "13 columns" on one line and "every one of the 12 columns" eight lines later. The wrong figure had propagated to `CLAUDE.md`, to two `CHANGELOG.md` entries and to the template — none of it load-bearing for behaviour, but it is the number quoted whenever anyone reasons about this table's width, and it was quoted in an argument about *per-column* minimums where being one out matters. The origin is almost certainly `grep -c '<th'`, which also matches `<thead>`; the first draft of the regression test written to prevent it reproduced the identical off-by-one, which is the best argument for the test existing. Rather than correct a fifth copy of an uncheckable number, the shape is now asserted: `test_every_row_has_a_cell_for_every_header` runs with and without penalties and requires the header count to equal every row's cell count. That pins something a comment cannot — the Penalty column is conditional in **both** `<th>` and `<td>`, two `{% if any_penalties %}` guards forty lines apart that nothing checked agreed, and dropping either silently offsets every column to its right. Deliberately relative rather than a hard-coded 12, so adding a column stays a one-line template change.

- **`/load-scenario`'s and `/buyout`'s view resets are tested, in both directions.** Both call `_view_team(MY_TEAM)` and deleting either left the whole suite green — a gap the 2026-08-11 move of the view onto a global inherited rather than opened. Four tests in `TestTheViewSticks`, reusing the `viewing_srl` fixture that encodes the rule that a view test must open the panel first. The forward buyout case is worth having on its own: `test_undoing_a_buyout_shows_my_team` reopens SRL after the buyout, which *implies* the forward reset without asserting it, and that test passes with the forward call deleted. The error-branch pair is what pins the placement rather than just the presence — `/load-scenario` resets the view **after** the `except KeyError` return, so an unknown scenario changes nothing, and mutation-checking proves each direction separately: dropping `/buyout`'s call reddens only the buyout case, dropping `/load-scenario`'s reddens only the scenario case, and hoisting it above the `try` reddens only the unknown-scenario case. The scenario name is derived from `scenarios.SCENARIOS` rather than written out, so registering a new one cannot silently 400 this test.

- **The "bid advice is degraded" warning had no test, and the two reasons it stayed that way were both wrong.** When the roster MILP cannot solve, every number in the bid panel is a floor fallback rather than analysis, and a warning says so. It had been untested since it was added. The backlog gave two grounds. The first was a plain factual error: it recorded the badge as living in `auction_control.html`, and `grep -rn Optimizer templates/` returns exactly one hit, in `partials/bid_panel.html` — so anyone going to write the test would have started in a file that has never contained it. The second was that the trigger is unreachable, which is half true and mattered: `remaining_budget < spots × MIN_SALARY` is unreachable through **bidding**, because the commissioner refuses any bid that would leave a team unable to fill 24 (owner decision 2026-08-06), but buyout penalties, `/trade-between` and `/adjust-salary` all raise cap load and **warn rather than refuse**, so the state is legal. Measured on a fresh BOT (12 spots, $26.5M): $7.0M of headroom still solves, **$6.0M and below is `Infeasible`** — the boundary sits just above 12 × the $0.5M floor because market prices are *expected* prices, so the cheapest player in the pool costs slightly more than the floor. Three tests now pin it, and the pair that matters is the second and third: the warning renders on `GET /` **and** on the `/bid-check` fragment, which is the one that replaces `#bid-panel` on every bidder toggle — rendered only on the full page it would disappear at exactly the moment the operator starts leaning on the numbers. Mutation-checked three ways, killing different sets: deleting the `{% if %}` reddens the two positive tests, inverting it to `== "Optimal"` reddens all three, and dropping the `_recompute()` from the helper reddens the two positive ones (`GET /` renders the context, it does not re-solve). The precondition assert in the helper is deliberate — with a nearly-full BOT roster $1.0M *is* a solvable budget, and all three tests would then quietly assert against an Optimal MILP. **Corrected an hour later by a grill of this same commit:** the class was badge-only, and closing a backlog item called "MILP-infeasible rendering" on that basis was an over-claim — an audit of every `milp` reference in `templates/` found a second consumer. `team_panel.html` gates both its Optimal Projected Points headline and its entire MILP *target* list on the same status, so a failed solve strips the buy list out of BOT's own panel: **12 of 63 rows and ~5.6KB**, with the word "Optimizer" appearing nowhere inside `#team-panel` — the warning that explains it is in `#bid-panel`, a different grid column. A fourth test pins that, asserting on the target player **names** rather than a row count or the styling class that marks them, and narrowed to the names actually visible while Optimal so the after-check cannot pass for the wrong reason. One mutant deliberately survives it and is documented in the test: dropping the status check from `show_milp` changes nothing, because every non-Optimal return in `solve_optimal_roster` sets `roster=[]` and the `and milp.roster` term already gates the list — belt-and-braces, not a second source of truth. The headline alert has no such backstop and its check *is* pinned. Whether the operator should get a second warning inside the panel they are actually looking at is a UX judgment call, so it went to `BACKLOG.md` rather than being decided inside a test-coverage commit.

- **Two more silent traps of the same shape, found by asking whether the class was closed rather than the instance.** With `TestPlayerChart` fixed, a sweep of every hard-coded player name left in `tests/` classified them: `test_crash_recovery.py` and `test_auction_draft.py` are **loud** rot (`_draft` asserts the pick landed, and says in its docstring exactly why), `test_htmx_interactions.py`'s ~20 assign literals are loud (they assert on toast type, or dereference `find_player(...).salary` and would raise) — but two `/bid-check` sites were not. `/bid-check` on a name that is not in the pool answers with the **not-found branch**, and measured 2026-08-13 that branch still carries `id="bid-advice"` and `id="bid-price"`. So `test_active_form_has_bid_price_id` asserted only `'id="bid-price"' in r.text`, which the not-found branch satisfies — with a stale name it silently became a second copy of the `test_inactive_form_has_bid_price_id` sitting directly above it. The assertion was weak *independently of the literal*, so both halves were fixed: the name is derived, and the test now anchors on a verdict (`BID`/`CAUTION`/`DROP`/`WIN`), which is what actually separates the branches. `TestSwapTargetsResolve._rendered_states` had the same exposure — its "active bid" half would have quietly shrunk to the two ids that survive both branches, narrowing an inventory the whole test class is built on — and it gained the same guard. That guard immediately earned itself by rejecting the anchor first tried: `hx-post="/assign"` is absent from a perfectly good three-bidder render, because the Assign button appears only when one bidder is left standing.

- **The test guarding the chart's two-mount invariant could not fail, and the loud test next to it was what hid that.** `TestPlayerChart` fetched a hard-coded `Steven Stamkos` twice, against the CLAUDE.md rule. The reason it mattered more than the usual stale-literal case: `/player-chart/<gone>` answers **200** with a ~250-byte empty state carrying no `<svg>` and, crucially, no `id="player-chart-container"` — so once the name left the pool, `assert 'id="player-chart-container"' not in r.text` was asserting a property of a page with no chart in it. Demonstrated rather than reasoned: pointing the derivation at `"Nobody At All"` reddens `test_player_chart_valid` and leaves `test_the_chart_body_carries_no_mount_id` **green**. Both names now come from `pool_top()`, so the loud test guards the silent one by construction. `pool_top` went into `tests/helpers.py` rather than being written a second time — `test_browser_ui.py` had already grown a private `_pool_top` with the same body, which is the path `squeeze` took to three copies, so that one was deleted and its nine call sites repointed at the shared helper. Top by projected points on purpose: a floor-priced player is the degenerate end of the price distribution and a chart test wants a curve.

- **A team that had stopped drafting was still projected as though it hadn't, by up to +1101 points, and it corrupted the rank badge.** Found by building the scenario below and then doing the thing the backlog entry asked for — re-measuring the Proj heuristic against a real per-team MILP — which turned up a different and much larger bug sitting in the same block. `_context` estimates every opponent's finished total as `current + unfilled_starter_slots × mean(points of the affordable top)`. For a **done** team there are no more picks, so every one of those points is invented; and the error is enormous rather than marginal because a team that stopped early never spent its budget, so its `physical_max_bid` is still `MAX_SALARY` and the affordability filter hands it the best players in the pool. Measured on `endgame-ceiling-binds`: the eight done teams read **+673 to +1101** above their real finals — SRL 390 actual against **1491** shown. The damage lands on the **rank badge**, which is the one figure that answers "am I winning": BOT's real 1311 sat behind five phantom teams and the panel said **#6** while BOT was first by a distance. Not an edge case — the design notes put 3+ early finishers in every draft, so this was wrong on draft day, in the second half, every time. Fixed with one branch, `projected = current` when `is_done`, ordered **ahead** of the MILP branch so it also covers BOT: marking your own team done is a legal move in that table, and a MILP that keeps planning purchases you have sworn off is the same lie aimed at yourself. This is not a new rule so much as an existing one reaching the last place it had not — done teams were already excluded from market ceilings, demand counts and nomination order. Three tests, mutation-checked two ways that kill **different** sets: deleting the branch reddens the two opponent cases (naming "1158 invented points" and "the rank badge reads backwards"), while merely reordering it after the MILP branch reddens only the BOT case, which is what that third test exists to pin. The heuristic's *original* complaint stays open and is now measured rather than assumed: it is **not** systematically optimistic (+62 mean on a fresh state, but +146 and −72 on the two still-drafting endgame teams), so a budget-aware greedy fill would not just shave off a known bias, and 11 extra MILPs per action still looks like a bad trade.

- **`measure_layout.py` could not see the table that caused the bug it exists to diagnose.** `TARGETS` carried `#league-state > table`, and the `.table-scroll-x` wrapper added by the 2026-08-11 grid fix broke that child match — so for two days the report printed `(absent)` for the 955px element that forced the column, and `min_contents()` dropped it with no line at all, on the one number a layout investigation turns on. `(absent)` is also what a legitimately-unrendered element prints, so nothing distinguished "your selector is wrong" from "there is nothing there". Tables are matched by descendant selector now and both `.table-scroll-x` wrappers joined the list. `--whatif` was worse than useless: it injected `minmax(0, 1fr)`, which had shipped, so it re-measured the baseline and labelled it a hypothesis — it now injects the bare `1fr` **bug**, which makes the failure reproducible on demand. The numbers in `CLAUDE.md` are consequently checkable rather than trusted: baseline 414.7/414.7/414.7 with +0 overflow, `--whatif` 292.5/990.8/584.6 with +624 and `.area-team` at x=1310.

## [2026-08-12]

### Fixed

- **The 2026-08-11 layout fix's own containment guard could not fail, and two of its comments were wrong.** Found by grilling the commit rather than by anything breaking. The layout fix itself held up under review — state isolation, selector safety, pytest collection and the tooltip test's guards all re-verified — but the change as *authored* carried three defects.

  **`assert not d["spill"]` asserted nothing if its selector died.** It is the assertion whose own docstring calls it *"the invariant that survives the next wide column someone adds"*, and it is built by iterating `g.querySelectorAll('section.card')` — so an empty result means **either** nothing overflows **or** the selector matched nothing, and the second reads exactly like the first. Measured, not reasoned: with the selector mutated to `section.cardx` and no guard, the test **passes**. A template renaming those panels to `div.card` would have retired the check in silence, which is the failure mode CLAUDE.md describes as reading like coverage. The probe now returns the count and the test asserts `>= 8` — nine `section.card` partials render unconditionally inside the grid, so that survives deleting a panel while still catching a rename. Verified in both directions: the guard reddens under the mutant, and the `spill` assertion alone stays green under it. The sibling `areas` loop needed nothing, and only by luck — `querySelector` returns null there and the probe throws, which is loud. The model for the fix was sitting in the same file the whole time: `TestTooltipsStayInsideTheirPanel` carries three guards of exactly this kind (`assert data["rows"]`, `counted >= 10`, and a required-by-`data-tip` set), and the new test had none.

  **A CSS comment cited a precedent that does not exist.** The `.chart-meta` block justified dropping the tooltip arrow with *"`.bid-details` already sets the precedent that an anchored bubble keeps its text and loses its aim."* `.bid-details` does no such thing: it overrides `::before` only, there is no `.bid-details` `::after` rule anywhere in the file, and the comment eleven lines above it says the opposite outright — the tail is *deliberately* left centred, because the arrow must still point at the thing it explains. Dropping it for `.chart-meta` remains correct for a reason the comment never gave: the trigger is `position: static` there, so the arrow resolves against the **line** rather than the word and would land mid-sentence on whatever text sits at that offset, which is worse than absent. The comment now names itself as the single exception and says why, because as written it invited the next reader to "restore consistency" by deleting the arrow in `.bid-details` too and undoing a measured 2026-08-08 fix. A wrong WHY is worse than no comment: it is believed.

  **And a dead rule, in the same commit that argued against exactly that.** `.team-stats > .tooltip[data-tip]:first-child::before` was fully covered the moment the four `:nth-child` bands landed — they partition every width (`<=639`, `640–767`, `768–1023`, `>=1024`) and each anchors `:nth-child(Nn+1)`, which always includes child 1. So it could never produce an effect the bands do not, while standing as a second source of truth for "tile 1 is left-anchored" — the redundancy the commit's own grid comment rejects for `min-width: 0` on the `.area-*` children. Deleted, with its one surviving measurement (at the 768px breakpoint the tiles narrow to ~191px, too narrow for a centred bubble to clear the edge even at the 15rem cap) folded into the band block, since it explains why anchoring is needed rather than just a cap. Deleting a redundant rule cannot move a bubble, and the tooltip test staying green at all seven widths is what makes that a checked claim rather than an assumed one — if the bands were not the partition claimed, it would have gone red.

## [2026-08-11]

### Fixed

- **The team panel was entirely off-screen at the width the draft is run at, and the bid panel's column was 29% narrower than its share.** The owner confirmed the draft runs on a **1280–1600px laptop**, which turns the 2026-08-08 finding from a portability note into a live-auction bug: at 1280px `#team-panel` — Cap Used, Remaining, Spendable, Max Bid, the roster, the buyout dots — began at **x=1310** and you would be bidding blind to your own cap. Nothing looked wrong because `all_panels.html`'s inline `overflow-y: auto` forces `overflow-x` to compute to `auto` (CSS Overflow §3.2), so **624px of content hid behind the *grid's* scrollbar and no *page* scrollbar ever appeared**. Fixed by replacing all three bare `1fr` templates in `.auction-grid` with `minmax(0, 1fr)` and giving every table wide enough to overflow its column its own `.table-scroll-x` wrapper. Contained at **375, 1024, 1280, 1600 and 1920** afterwards, exactly, with the page never scrolling sideways.

  **The original diagnosis was wrong in three ways — the fourth wrong deferral premise on this backlog in a row**, after the two the view work corrected earlier the same day. (1) **It blamed the wrong panel.** It named `#bid-limits`, min-content "990px". `#bid-limits`' table already sits in a `.scroll-container`, and its real min-content is **544px**; the forcer was `#league-state` — a 12-column table sitting directly in its `<section>` with no wrapper at all, min-content **955px**, which is where the 990 figure came from with the section's padding folded in. `team_panel.html`'s two tables were unwrapped too. (2) **Half the proposed fix was a literal no-op.** It said to add `overflow-x: auto` to that `.scroll-container`, which already sets `overflow-y: auto` — so per the same §3.2 rule that hid the whole bug, its `overflow-x` **already computed to `auto`**. It had simply never had anything to scroll, because it inherited a 990px track. `.scroll-container` is untouched by this change; it just starts *using* its x-axis now that its column is 409px. (3) **`min-width: 0` "on the tracks" conflated two specs.** The lever is `minmax(0, 1fr)` and it works through css-grid §6.6: an item takes a content-based automatic minimum only if it spans a track whose **min sizing function** is `auto`, and a bare `1fr` *is* `minmax(auto, 1fr)`. Zeroing the track's min zeroes the item's automatic minimum on its own; `min-width: 0` on the `.area-*` children would be a redundant second source of truth, and is the *flexbox* rule (§4.5), which keys on the item instead. Every Tailwind grid in this app already complies — `grid-cols-3` expands to `repeat(3, minmax(0, 1fr))`. `.auction-grid` was the one hand-written grid that did not.

  **Measurement replaced arithmetic, and it moved the plan twice.** `tests/measure_layout.py` (an instrument, not a test — the name keeps pytest from collecting it) attaches Playwright to an in-process server whose `STATE_DIR` is redirected to a temp dir first, and reports each candidate's rect, client/scroll widths, computed overflow, and min-content measured *in situ* one element at a time. Two one-line readings settled the argument that a month of prose had not: `getComputedStyle(grid).gridTemplateColumns` prints the **used** track sizes — **292 / 991 / 585 at 1280px**, against 409 each for an equal third — and the `.scroll-container` overflow reading proved the no-op. That first number is the second problem nobody had named: **`.area-auction`, the column holding the bid panel you work in all night, was being squeezed to 292px**. That track measures **415px** now, so the column carrying the bid panel is **42% wider** as a side effect of fixing the team panel — the change nobody asked for and everybody at the keyboard gets. The plan's own arithmetic was wrong twice and the instrument caught both: `.area-team`'s min-content is 585, not the predicted 602, and `bid_limits.html`'s filter row measures **283–293px against a 329px track**, not the "~350px" that would have needed `flex-wrap` — so **that edit was dropped rather than shipped**, since a wrapping filter row that never needed to wrap is a permanent layout change bought for nothing.

  **Zeroing the track floor is necessary but not sufficient, and the intermediate state is worse than the bug.** `minmax(0, 1fr)` shrinks the item box and does nothing to its *descendants*, which keep their min-content and `overflow: visible`. Measured with the override injected and no wrappers: still **+143px**, with the league table painting out to **x=1405 straight across `#team-panel` at 856–1271** — later tree order wins for non-positioned backgrounds, so the columns would have vanished under an opaque card with **no** scrollbar offering them, strictly worse than today where you can at least scroll to the panel. So the wrapper and the `minmax` are one change, not a fix plus a polish. `.table-scroll-x { overflow-x: auto }` is deliberately its own class rather than a reuse of `.scroll-container`: these tables are short and want neither its `min(500px, 45vh)` height cap nor its sticky `thead`.

  **The tooltip suite had been resting on the bug**, which is why the tooltip work is in this commit rather than a later one. `TestTooltipsStayInsideTheirPanel` bounded offenders by `contentW = grid.scrollWidth` — written in grid-content coordinates *because of* this overflow — so removing the overflow collapses `contentW` to `clientWidth` and tightens the bound at **all seven** of its widths, most starkly at 375px, where a 270px bubble in a ~342px panel passed only because the league table inflated `contentW` by ~665px. It then turned out the tooltips were not merely a consequence but a **prerequisite**: with pseudo-elements suppressed, `.team-stats` measured 379/379, and with them 379/**446** — so the last of `.area-team`'s overflow was *entirely* bubble geometry, and assertion 1 of the new test cannot pass without capping them. Two independent bugs surfaced there. `.team-stats` overflowed its panel by **13px before any of this change**, a pre-existing bug that the `md:grid-cols-7 lg:grid-cols-3` edit also fixes (at a 409px column, 7 tiles across are 46px wide and their labels paint outside them; 7-across now survives only at 768–1023, where the panel spans the full width). And the probe grew a bug of my own making: after `.chart-meta`'s triggers were given `position: static` so their bubbles anchor to the *line* rather than the word, the probe was still adding the resolved `left` to the **trigger's** rect and reported a correctly-placed bubble as 312px wide at x=244. It now walks to the nearest positioned ancestor for the containing block and the nearest *scrolling* ancestor for the frame, offsets by that box, and carries an honest new bound — **a bubble wider than its own container can never be read whole** — with a `right`-anchor fallback so an edge-anchored bubble still resolves. Edge tiles are anchored per column count (`:nth-child(3n+1)` left, `:nth-child(3n)` right, and the same for 4 and 7) rather than only `:first-child`, because 7 tiles in 3 columns leaves `:last-child` at the **left** edge of row 3, where a right-anchor pushes it off the other side.

  **The old layout test could not see containment and passed against the bug.** `test_the_layout_responds_to_width` asserted only that the three areas have three *distinct* rounded x values, and today's set was `{9, 311, 1310}` — three distinct, one off-screen. It is kept, with a comment saying it guards the media queries existing and nothing more, and a sibling added over `(1024, 1280, 1600)` — the tightest 3-column case, the draft width, and the top of the laptop range — asserting five things: the grid does not overflow its own client box (fails by ~624 before the fix, naming the widest offending card); every `.area-*` sits inside that box (client box, not `innerWidth`, so the assertion is scrollbar-agnostic); `document.scrollingElement` does not scroll sideways, so the fix can never become "let the body scroll instead"; three tracks with `min >= 0.25 * sum` (0.156 before; 0.25 rather than 0.33 leaves room for a deliberate `1fr 1.4fr 1fr` later); and — the one that survives the next wide column somebody adds — **no `section.card` in the grid may overflow while its computed `overflow-x` is `visible`**. Mutation-checked in three ways, each shown red: reverting to a bare `1fr` fails assertions 1, 2 and 4 at all three widths; deleting one `.table-scroll-x` wrapper fails 1 and 5 but **not** 2, which is exactly why 5 exists; and removing the stat-tile `max-width` fails the tooltip test at 375 and 1280. Full suite **691 passed**. Still owner-side: confirming it on the actual draft laptop at its real resolution.

- **The team panel now follows whichever roster the action changed, and `/undo` no longer throws you home off an opponent's edit.** Two `BACKLOG.md` entries that were really one change: the owner decision of 2026-08-08 (*on an opponent's pick, swap the panel to that team; on your own, keep returning to BOT*) and the open `main.py (undo)` finding of 2026-08-07 (`/undo` called `_view_my_team()` unconditionally, so viewing GVR, benching one of their players and pressing `Ctrl+Z` left you looking at BOT). `_view_my_team()` is gone; `_view_team(code)` replaces it at all five call sites. `/assign` passes the buyer, `/buyout` `/reset` `/load-scenario` pass `MY_TEAM`, and `/undo` mirrors the policy of the action it reverted. The 2026-08-07 rule was not wrong, it was over-general: "reading an opponent's Cap Used as yours right after a pick lands" is a hazard about **your own** pick, the moment you actually glance at the header, and that case is untouched. On an opponent's pick nothing of yours moved, the roster that just went stale is theirs, and the cap-overage toast (`"SRL $1.0M over cap"`) finally arrives next to the numbers it describes instead of next to BOT's.

  **What the original entries got wrong, which is why this sat deferred and why it turned out to be small.** The testing-pass item asserted that *"`/assign` must still return the panel OOB the way `/team-view` does (`team_view_response.html`), never `all_panels.html`, or the swap destroys the bidding session."* `/assign` has always returned `all_panels.html` into `#app` and has always replaced `#bid-panel` — deliberately, because the sale *ends* that bidding session. The rule it borrowed belongs to `/team-view` and `/nominate`, which must not. So no OOB plumbing was needed at all: `all_panels.html` re-renders `team_panel.html` **and** the non-OOB `buyout_scan.html` from one context in one response, so the panel and the Scan-button gate cannot disagree, and the whole `/assign` change is one line. Separately, the `main.py (undo)` finding said the fix *"means `restore_snapshot` reporting what it undid, and that is a state-layer change to serve a view concern."* It is not: `/undo` already diffs both logs to build its toast and was holding `t.transaction_type` and `t.team_code` eight lines above the `_view_my_team()` call. `state.py` needed nothing. Two entries, two deferral rationales, both reasoned rather than checked — `BACKLOG.md`'s triage note now says so at the top of **Open findings**.

  **`transaction_type`'s documented vocabulary was fiction, and the first draft of the `/undo` rule was reasoned against it.** `state.py` said `"draft", "trade_give", "trade_receive", "buyout"`. Two of those four have never been emitted by any code path: the real set, verified against every `_log_transaction` call site, is `draft` (`/assign`), `trade_out`/`trade_in` (`/trade-execute`), `trade` (`/trade-between`) and `buyout`. `transaction_log.html` had carried the correct list all along. The wrong pair has a traceable origin: `trade_give` and `trade_receive` are the **local variable names** inside `/trade-execute` (`trade_give = last_trade_eval.give`), so whoever wrote the comment read the code that assembles a trade rather than the strings it logs four lines later. A denylist written from the comment — "point the view at `t.team_code` unless it's a trade" — would have fired on all three trade types, and `/trade-between` logs `team_code` as `f"{source}→{dest}"`, so the view would have been set to the string `"SRL→MAC"`. Hence two independent guards: the branch **allowlists** `("draft", "buyout")`, and `_view_team` ignores any code that is not in `auction_state.teams`. Both comments now name the trap, and the stale comment is fixed.

  A collapse worth recording: `trade.execute_buyout` is `state.teams[MY_TEAM]`-only and `/buyout` logs the literal `MY_TEAM`, so a buyout record's `team_code` **is** BOT. "A reverted draft points at the buyer" and "a reverted buyout points at BOT" are therefore the same expression, and the rule is one branch rather than two. A buyout earns inclusion on its own merits anyway: the Penalties tile is conditional on `viewed_team.penalties > 0`, so undoing one makes a tile appear or vanish on your panel, and watching nothing change on SRL while half a salary moves on your own cap is the action/undo mismatch the finding was about, pointed the other way.

  Change-log undos deliberately leave the view alone, even though `ChangeRecord` has a `team_code` that would make mirroring look free. `team-done` is a `ChangeRecord` kind, so mirroring would swap the panel to an uninvolved third team — precisely what the 2026-08-07 fix removed from the forward path and what `test_marking_another_team_done_does_not_snap_the_panel_back` pins. It also costs nothing in the common case: every roster-edit control renders inside `team_panel.html`, so you cannot bench a player on a roster that is not on screen, which makes "leave alone" and "point at the edited team" the same answer. The accepted gap is edit-an-opponent → navigate-away → `Ctrl+Z`, where the view stays where you last put it; your `/team-view` click is newer information than the log.

  **Test integrity.** One test flipped as the backlog predicted (`test_assign_returns_to_my_team` → `test_assign_swaps_to_the_buyer`), and one was found **vacuous**: `test_undo_returns_to_my_team` never opened SRL, so with the view never moved off BOT, "the endpoint left it alone" and "the endpoint reset it to BOT" were the same assertion — it passed against both the bug and the fix, testing only the autouse `default_viewed_team` fixture. It now opens the panel first, per the CLAUDE.md rule, and goes red against the old code. Eight cases were added and every one was mutation-checked by breaking the behaviour and watching it fail: reverting `/assign` reddens 3, restoring the unconditional `/undo` reset reddens 4, widening the allowlist reddens 1, dropping `_view_team`'s guard reddens 1. That last pair is why there are two trade cases rather than one — and the first draft of the `/trade-between` docstring **overclaimed**, saying it pinned the allowlist. It does not: `_view_team` rejects `"SRL→MAC"`, so the view stays put and the test passes on a widened allowlist. Only `/trade-execute`, whose logged codes are real team codes, discriminates. Measured, not assumed, and both docstrings now say which mutant they catch.

## [2026-08-08]

### Added

- **Tooltips on the three numbers the owner asked about**: League State's **Proj**, the price chart's **Sigma**, and the bid panel's **Marginal**. Each was reported as "what does this mean / why is it different", and the answer belongs on the element rather than in a doc nobody reads mid-auction. *Proj* is the interesting one, because the honest answer is that the column is computed **two different ways** — yours is the optimizer's best roster inside your real budget, everyone else's is a quick estimate that assumes they can afford their best available players — and read as one sortable column beside a rank badge those look comparable. Measurement decided the response: BOT reads 1257 by MILP against 1262 by the opponents' rule on a fresh state, **5 points apart with the same rank either way**, so the asymmetry is not distorting the table and labelling it beats spending 11 extra MILP solves per action to reconcile it. The tooltip says opponents read slightly optimistic and that the gap widens as budgets tighten; the finding stays open in `BACKLOG.md` with the reason it will drift (the heuristic checks per-player affordability and never whether the team can afford the whole set). *Sigma* now says it is the **spread** of the predicted price, not a measure of how expensive the player is — a high value means the model is unsure, which is the opposite of the natural reading. *Marginal* previously said "ignoring market context", which invited the question it did not answer; it now explains why the figure disagrees with the Price column — marginal is what he adds to **your** roster, so it sits below his price when you already have that position covered and above it when he fills a hole, and a wide gap is the tool working. All three were placement-checked in Chrome at seven widths before shipping (12 tooltips, 0 outside their panel), which is why they landed in the same batch as the overflow fix: the League State header is the second-to-last column and a naive `tooltip-bottom` there would have overflowed right the way the bid panel's overflowed left. `TestTooltipsStayInsideTheirPanel` requires all four touched tooltips by name, so a template that drops one fails instead of quietly measuring less — verified by deleting the Sigma wrapper.

### Fixed

- **The trade form could not express a legal trade involving a minor-league player.** Reported from the 2026-08-07 testing pass. Both dropdowns read `roster_players`, which excludes the minors — `trade_panel.html` for "I Give" and `/team-players` for "I Receive" — so a group 2/3 player in the minors, whose salary is **fully on cap**, could not be named in a proposal at all. That is the same fact that made the buyout dots wrong the day before: eligibility and cap impact are properties of the contract group, and location is irrelevant to both. **The engine was never the problem, which is what makes this a display fix**: `remove_player` walks all three lists, `evaluate_trade` resolves the incoming player with `find_player`, which searches the minors, and `/trade-between` has always accepted one. **There are three lists, not the two the finding named, and the first pass at this shipped having fixed two of them** — caught by grilling the commit, and worth recording because the miss followed from a wrong assumption rather than an oversight: `/team-players` was assumed to have one consumer (the Trade Evaluator's "I Receive"), and it has two. The second is `shortcuts.js`'s `loadTradePartner`, which fills the **"Trade Between Teams"** form in `team_panel.html` — the one used to record a trade between two *other* teams during a break. So widening the endpoint alone made that form *worse*: measured in Chrome, its "Receives" side listed 18 of SRL's minors **with no marker**, while its "Sends" side still read `roster_players` and offered 12 of BOT's 49 players. One form, one side listing minors the other could not name, and the listed ones indistinguishable from active players — precisely the case `/team-players`' own docstring argues is worse than omitting them. All three lists now read `all_players` and mark minors `(M)` inline, sorted with everyone else — the convention `buyout_panel.html` already uses. The marker is built four times — once in each of the two Jinja "gives" lists and once in each of the two JS label builders, because both "receives" halves are assembled client-side; `/team-players` grew an `is_minor` field to feed them, and the existing key-set assertion in `test_edge_cases.py` caught the contract change rather than being loosened for it. Owner decision (2026-08-08): a player traded out of the minors **lands on the active roster**, which is what the engine already did — no change, and the cap jump is covered by the `_cap_overages()` warning `/trade-execute` already fires. Mutation-checked three ways, each killing a different test: either list reverted to `roster_players`, and the `(M)` made unconditional — that last one matters because a marker on every row is noise, not information. Verified in Chrome with the real pool: BOT's "I Give" shows 49 options with 37 marked (12 active + 37 minors), SRL's "I Receive" shows 23 with 18 marked, and proposing a trade that gives a minor returns a verdict with zero console errors. The JS marker itself has no automated test and that gap is now in `BACKLOG.md` rather than implied.

- **A keeper who round-tripped through the minors turned green and stayed green.** Reported from the 2026-08-07 testing pass: Active → Bench → Minors → Recall on a keeper leaves him rendered `text-success`, which `team_panel.html` uses to mean "BOT bought him at auction". Green is how the roster is read at a glance, so this quietly falsified it — permanently, since nothing ever moved him back. **The original entry diagnosed it as a choice and it was really a missing field.** Provenance was encoded *only* by which list a player sat in, and `minor_players` is a third list holding neither, so there was nothing for `recall_from_minors` to route on; it appended to `acquired_players` unconditionally, documented in its own docstring as "losing only that label". `PlayerOnRoster.is_keeper` now carries it, `recall_from_minors` sends him back where he came from, and the default of `False` is the correct one — a player you drafted or traded for *is* acquired. **The finding also missed half the bug**: `data_loader` builds a roster player for everyone on a real FCHL team and routes on `STATUS == "MINOR"`, so a player already in the minors *before* the auction is a keeper too, and recalling one of those went green with no round trip required. Both branches now set `is_keeper=True`. **The test that pinned the old behaviour was wrong when it was written, not merely made stale**, which is why it was inverted rather than deleted: `test_demoted_keeper_recalls_into_acquired` justified itself with "Nothing in the app branches on keeper-vs-acquired — every other reader concatenates the two", but `team_panel.html` had read `acquired_players` on its own to colour rows since **f440053 (2026-05-02)** and that test landed in **c8d7fc6 (2026-08-06)**, three months later. Serialization is backward compatible in both directions: `_player_on_roster_from_dict` reads `d.get("is_keeper", False)` so an old file still parses, and `_team_from_dict` then *overwrites* the flag from list membership for the two active lists, which are authoritative and always were — so an old save self-heals on load rather than colouring every keeper as a draftee. `minor_players` is the one list where position cannot re-derive it, so `_backfill_keeper_flags` re-reads that bit from players.csv at startup, as a fourth entry in the existing backfill registry with its own net (never fatal, per the rule that no backfill is load-bearing for the draft record). It matches on the **disambiguated** name, not `row["PLAYER"]` — `_disambiguated_names` renames every member of a colliding group, and the current file has three collisions of which two are keepers, so a raw-string lookup would find nobody for a demoted `Jack Hughes (NJD)` and leave him mis-coloured with nothing on screen to say why. What it does *not* repair is the undo chain, and that limit is now in `BACKLOG.md` rather than implied. **The grill then found the same bug pointing the other way**, in the sibling trade path: `/trade-between` **reuses** the roster object where `trade.execute_trade` **constructs a fresh one**, and it reset `is_minor` and `is_bench` on arrival but not `is_keeper` — so another team's keeper landed in the receiving team's `acquired_players` still flagged, and a later bench → minors → recall filed him under *their* keepers. It self-heals on reload, since `_team_from_dict` derives the flag from the list, which is precisely why it would never have reproduced after a restart. Reproduced by mutation before fixing, and the test asserts the round-trip consequence rather than the flag alone. The two paths disagreeing at all is the smell: one gets provenance right by construction and the other has to remember. Mutation-checked six ways, each killing a different set: the routing branch in `recall_from_minors` (4 tests, including the endpoint one that renders the row); the list-authoritative overwrite in `_team_from_dict` (1); `is_keeper=True` in `data_loader` (2); the backfill made a no-op (2); the backfill made to flag *everyone* in the minors, which would kill the colour instead of fixing it (1); and the backfill reverted to raw CSV names (1). One test hung pytest instead of failing, which is worth recording: `assert "is_keeper" not in text` against a 2MB state file sends difflib quadratic on failure, so a genuine assertion failure looked like an infinite loop for 400s — it compares counts now. The failure it was hiding was real, and the fix is in the helper: `_snapshots` holds whole JSON *documents* rather than dicts, so a recursive key-strip walks straight over them and the simulated "legacy" file was not legacy in its undo chain.

- **Tooltip bubbles rendered outside the panel they explain.** Reported from live testing as "the 'Worth up to' tooltip slides off screen". DaisyUI centres a bubble on its trigger (`left: 50%` plus `translateX(-50%)`, vendor `.tooltip-bottom:before`) and has no flip logic, so one on a trigger near a container's left edge simply renders off it. Measured in Chrome at 375/640/700/800/1024/1280/1920 with a live bid running: **4 of the 10 tooltips on screen were outside the scrollable content box, the worst 94px out** — and an explanation you cannot read is worse than none, because the element looks answered. The original entry described this as one tooltip and blamed `.bid-details`' `flex-wrap`; the wrap is real but it was three of the four bid-panel tips, plus one team-panel stat tile the entry did not mention. **Two causes, so two rules.** `.bid-details` items are short spans hugging the panel's left edge in a *wrapping* row, so which item leads a row changes with the breakpoint — at 1280 items 1 and 3 both sat at x=35 — and CSS cannot select "first in a wrapped row"; they are left-anchored instead, so a bubble extends rightward from its trigger and can never cross the left edge. The team panel's stat tiles are evenly spread across a 3/4/7-column grid where left-anchoring would push the *last* tile's bubble off the right instead (measured: +15px at 375), so those stay centred and the bubble is capped at 15rem, which is what actually failed there — at the 768px breakpoint the tiles narrow to ~191px and a 20rem bubble cannot fit however it is placed. The grid's first tile, the one that leads a row in all three column counts, is additionally left-anchored. The `:after` tail stays centred on its trigger throughout: the arrow must keep pointing at the thing it explains. Overriding `transform` is free here because the vendor CSS drives visibility with `opacity`, not a scale. **The measurement frame was the hard part and is worth recording.** Judged against `innerWidth`, six *correctly placed* tooltips also looked broken — the panels live inside `.auction-grid`, which is `overflow-y: auto`, and a non-`visible` `overflow-y` forces `overflow-x` to compute as `auto`, so the grid scrolls horizontally and a panel can sit legitimately outside the viewport. Filling the bid input scrolls that grid, which is why triggers first read at negative coordinates. The test therefore measures in grid-content coordinates. That detour surfaced a genuine separate finding, now in `BACKLOG.md`: at 1280px the grid's content is 1903px and `#team-panel` is entirely off-screen, forced by `#bid-limits`' 990px min-content. Pinned by `tests/test_browser_ui.py::TestTooltipsStayInsideTheirPanel`, which computes each bubble's box from the resolved `left`, the transform matrix and the trigger's rect (pseudo-elements have no `getBoundingClientRect`), asserts the box resolved at all so the comparison cannot be vacuous, and requires at least 8 tooltips to have been measured. Mutation-checked three ways — each of the three CSS rules deleted in turn, each turning the test red.

- **The bid panel advertised $11.5M — an illegal bid — on every player in the pool.** Reported from the 2026-08-07 live testing pass as two complaints that turned out to be one bug: the "Should win it" figure was wrong, *and* it was identical for everybody. `expected_stop` is `round(ceiling + SALARY_INCREMENT, 1)` with no clamp, and every ceiling reaching `compute_bid_recommendation` is already clamped at `MAX_SALARY` by `physical_max_bid` — so the sum overshoots the legal maximum exactly when the ceiling *is* the maximum. Measured on a fresh state: **all 11 teams sit at `physical_max_bid` = 11.4**, so `compute_market_ceiling` returns 11.4 and all 704 players showed $11.5M at once. The identical-for-everyone half is the same fact seen from the other side — while nobody is budget-constrained the forecast says nothing about any particular player. The live path was no different, since `compute_live_ceiling` with BOT bidding returns the *highest* opponent max, also 11.4. **A clamp to $11.4M would have been the wrong fix**, and this is the part the original backlog entry got right: `.claude/rules/pricing-pipeline.md` defines `ceiling + 0.1` as *"by construction the price that outbids the strongest opponent"*, and that construction breaks precisely at the cap — there is no legal price above $11.4M, so a rival can match and the winner is decided by who bids it rather than by budget. Showing $11.4M would have kept the promise "bid this and you should win", which the engine cannot keep there. So the forecast is reported absent, with a fifth `stop_status` (`at_cap`) explaining *why*: it did not retire the way `passed` and `uncontested` describe, it never started. Reusing either would have been a specific false claim — `passed` says a real price falsified it, `uncontested` says there are no rivals, which is the opposite of the situation. The panel now reads `Should win it: — (rivals can reach the max)`. **It was a display bug only**, and that claim is now a test rather than a commit-message assertion: `value_cap` is `min(marginal, physical_max_bid)` and `physical_max_bid` is itself clamped at `MAX_SALARY`, so `value_cap <= MAX_SALARY < expected_stop` and the old `min(value_cap, expected_stop)` already returned `value_cap` — no advice has ever been wrong because of this. `TestTheForecastAtTheCap::test_max_bid_and_verdict_are_untouched_by_the_new_status` checks the new code against an independent restatement of the pre-change rule across all 110 legal ceilings × 5 prices, rather than against the new code's own arithmetic, which would be a tautology. The new arm is ordered ahead of the `passed` check because `current_price >= expected_stop` is unreachable through legal bidding when `expected_stop > MAX_SALARY`; the two can only meet on an illegally typed price, where "the ceiling is the league max" is the more honest answer. **One existing test had to be re-stated, not weakened**: `test_bidding_the_shown_figure_always_retires_the_forecast` swept every legal ceiling asserting the status ends up `passed`, which now fails at 11.4 — where the panel is telling the truth by showing nothing. It reads the *figure* now ("if one is shown, bidding it must retire it"), which covers both cases and still catches the float-quantization bug it was written for, plus a guard that the sweep exercises the live path at all (>100 of the 110 ceilings) so it cannot go quiet-green. Mutation-checked three ways: the `at_cap` arm deleted (kills 2 tests), `max_bid` in that arm set to `expected_stop` (kills 2), and the template branch removed so the case falls through to the neutral dash (kills the parametrized render test). Verified against the running app in both directions — fresh state shows the dash and no `11.5` anywhere in the panel; after draining two opponents to `physical_max_bid` 1.2 and 0.0, the same panel shows a live `$1.3M`. `max_salary` joined the template context so the tooltip quotes `config.MAX_SALARY` instead of carrying its own copy of "11.4" to drift.

## [2026-08-07]

### Added

- `/undo` cannot drift. Ten POST endpoints take a snapshot; **four had an undo test** (`/assign`, `/trade-execute`, `/buyout`, `/team-done`). The other six — `/adjust-salary`, `/toggle-bench`, `/move-to-minors`, `/move-to-roster`, `/set-nominator`, `/trade-between` — had none, on the one operation with nothing behind it: mid-draft there is no second Ctrl+Z, so an undo that restores less than it should is unrecoverable and invisible until much later. All six were probed first and **all six already worked**, so this is insurance rather than a fix — the work was removing the fail-open lists that make a future failure likely. There were three, the same shape as the `_panels_viewing()` design deleted earlier the same day. (1) `restore_snapshot` hand-assigned eight fields; it now enumerates `fields(self)`, so a new field on `AuctionState` is restored automatically instead of being forgotten. `_snapshots` is skipped by name, and that skip is load-bearing: snapshots are written with `include_snapshots=False`, so the restored object's chain is always the empty default and copying it would make Ctrl+Z work exactly once per session. (2) The enumeration does not help if `to_json` never writes a field — then `from_json` supplies the *default* and undo restores a zero, which is worse than restoring nothing. Two guards cover that, and they are not redundant: dropping a `to_json` key fails `test_every_field_reaches_the_json`, while making `from_json` ignore a key it still writes fails **only** `test_every_field_survives_a_round_trip` — verified by running both mutants. (3) A new mutating POST that forgets `save_snapshot()` is invisible until someone hits Ctrl+Z mid-draft, so `TestEveryMutatingPostTakesASnapshot` walks `main.py`'s ast and requires every `@app.post` handler to snapshot or to be named in `NO_SNAPSHOT_NEEDED` with its reason; it carries its own not-vacuous check, since a decorator rename would otherwise turn every assertion in it green. **One of the six tests could not fail when first written, and the cause is worth recording**: `/move-to-minors` needs the player benched first, `/toggle-bench` takes a snapshot of its own, and with a counts-only reading `/undo` popped the *bench* snapshot — pre-bench has the same roster and minors counts as post-bench, so the test passed against a build where `/move-to-minors` had stopped snapshotting entirely. This is precisely the shared-chain hazard the function-scoped `client` shadow exists to avoid, arriving through the test's own precondition rather than through another test. The reading now carries `(is_minor, is_bench)`, which separates the two states. Mutation-checked seven ways: `save_snapshot()` deleted from each of the six endpoints in turn (each kills only its own test), the restore loop made to skip `teams`, the `_snapshots` skip dropped, a `to_json` key removed, `from_json` made to ignore one, a plausible new un-snapshotting endpoint added, and the ast walk broken. Browser-verified: three real edits on an opponent's roster driven by clicking the controls, three `Ctrl+Z`, exact walk-back each time, zero console errors — which also surfaced the new open finding above, that undo returns the panel to BOT even when what it undid was an opponent's roster edit.

### Changed

- the view moved to the server, closing the two open entries against it — the dead Scan button in `buyout_panel.html` and the view reset in `main.py (team_done)`, both dropped from Open findings, deliberately without their line numbers since a closed entry's line only rots — and an unflagged third. **Supersedes the `_panels_viewing` entry above** — that helper is deleted. Carrying the view per-endpoint failed open: every handler rendering `all_panels.html` had to remember to pass a team code, and the ones that didn't threw you back to BOT. Five carried it *on their success paths only*, so the **error branches of all five** were the worst of the set and were in no backlog entry: auditing SRL, you fix a salary, the player was traded away a second earlier → warning toast *and* you lose the roster you were auditing at the moment you most need to look at it. `main._viewed_team` now holds it and `_context` reads it, so there is nothing left to forget; `/team-view/{code}` is the sole setter, `/assign`, `/undo`, `/buyout`, `/reset` and `/load-scenario` call `_view_my_team()` (the 2026-08-07 owner decision, now pinned rather than assumed), and `/team-done` and `/trade-execute` started preserving it **without being touched** — which is the argument for the design. A module global and **not** a field on `AuctionState`: on the state it would serialize into the save file and `/undo` would restore a *view*, which is not a draft action; `test_the_view_never_reaches_the_state_file` asserts both halves. A deliberate semantic change rode along: `/team-view/FAKE` used to render BOT and now changes nothing, so a bad link cannot move your view — `test_edge_cases.py::test_team_view_nonexistent_changes_nothing` was rewritten to that stronger contract. The Scan Roster button is gated on a derived `buyout_dots_on_screen` boolean rather than on `viewed_team`, because CLAUDE.md lets no panel but `team_panel.html` read that key and the rule is about a panel *acting* on the wrong roster — a boolean describing whether the swap targets exist in the DOM carries no roster and cannot leak one. **The mutation checks earned their keep on the fourth one.** Pointing `trade_panel.html` at `viewed_team` — the 2026-08-05 leak, reproduced — left `TestPanelContextIsolation` **passing**: both its cases posted an edit for an opponent without opening that opponent, which the old design made sufficient (the code travelled with the request) and the new one does not, so `viewed_team` was still BOT and the leak leaked BOT into BOT. The refactor had silently disarmed the guard on the exact defect it was written for. Both cases now open the panel first, which is also the only way the edit happens for real — every one of those controls renders inside `team_panel.html` — and the mutant kills them. **The manual browser pass then caught what neither the endpoint tests nor the automated browser cases did**, which is the argument for still doing one. Gating the button inside `buyout_panel.html` fixed only the outbound direction: `/team-view` swaps `#team-panel` alone — it *cannot* return `all_panels.html`, which would replace `#bid-panel` and destroy the bidding session — so switching **back** to your own team restored the dots but not the button, and it stayed missing until some unrelated full-page swap happened to bring it back. Every endpoint test read `GET /` afterwards, a fresh document where the answer is always right; both browser cases only ever went one way. The button is now `buyout_scan.html`, returned out-of-band by `team_view_response.html`, with the wrapper div unconditional and only the `hx-swap-oob` attribute conditional — a target that disappears with its contents is one-way by construction, which is the bug restated. Verified end to end in Chrome: load → SRL → a `/team-done` toggle → back to BOT, then Scan, and all 11 dots resolved with **zero console errors** (the `htmx:oobErrorNoTarget` noise is gone in both directions). Pinned by `TestTheViewSticks` (8 cases through the HTTP surface), `TestBuyoutScanIsOfferedOnlyWhereItWorks` (5, including both swap directions and a guard that `GET /` carries no `hx-swap-oob`, or the fragment would swap itself over itself on every pick), and two browser cases for the claims that only read as fixed on screen. Mutation-checked all four ways from the plan, plus both directions of the OOB fix.

- test order stopped being load-bearing. The `client` fixture was `scope="module"`, so `POST /reset` ran **once for 98 tests** and anything a test mutated without putting back changed what every later test in the file saw. **The entry named one file; it was four** — `test_endpoints.py`, `test_edge_cases.py`, `test_htmx_interactions.py`, `test_stress.py`, 172 tests — with three more (`test_auction_draft.py`, `test_dry_run.py`, `test_trade_buyout_undo.py`, 74 tests) that are sequential *by design* and must stay module-scoped, since `test_dry_run.py` is one continuous 40-pick auction where the flow **is** the test. **This changed no test outcome**, and that was established before touching anything: all 98 `test_endpoints.py` tests were run individually and every class in the other three alone — zero depended on state left by another. The value is preventive, and the entry's own framing ("eat the per-test `/reset` cost") had made it look expensive when it is not: `/reset` is **109ms**, not seconds. Two measurements shaped the design. A naive `scope="function"` rebuilds `TestClient` and re-runs the lifespan every test (**221ms**, 38s across the suite); a session-scoped transport with a per-test reset costs **107ms** (18.5s). So `conftest.py` gained `_app_client` (session, holds no auction state) and `client` (function, resets), and the four local copies were deleted. Measured end to end the suite went 593 tests / 301s → 600 / 307s, i.e. +6s, better than the +18.5s predicted because the deleted fixtures were themselves rebuilding clients. **Two class-level shadows in `test_endpoints.py` turned out to be workarounds for this very problem** — their docstrings said "the module fixture resets once for the whole file" — and both also repointed `main.STATE_DIR` to a fresh temp dir with no restore, silently overriding the session-wide `isolated_state_dir` for everything that ran after. Deleting them took two now-unused imports with them. The guard is `tests/test_fixture_scopes.py`, aimed at the direction that fails **silently**: converting a sequential file fails loudly on the next run (`test_01` needs `test_00`), but a *new* file declaring a module-scoped client re-introduces the coupling with nothing to notice. It ast-scans for `client` fixtures, requires every module-scoped one to be in `SEQUENTIAL_BY_DESIGN` with a reason, requires the allow-list to have no stale entries, requires each listed file to say "ON PURPOSE" in the fixture docstring where a future reader will actually be standing, and carries a not-vacuous check so a rename cannot turn it green. It also states what it cannot prove: it reads a declaration, not behaviour, and cannot tell whether a file's tests are genuinely a sequence. Because nothing depended on leaked state, reverting the scope fails **no existing test** — so `TestTheFixtureActuallyIsolates` is a written-on-purpose ordered pair (mutate, then assert it is gone) and is the only thing in the change that can demonstrate it did anything. Mutation-checked five ways: conftest's `client` back to module scope (kills only that pair), a module-scoped client added to a non-listed file, an allow-list entry for a file that is not module-scoped, the ast scan broken, and an "ON PURPOSE" docstring removed.

### Fixed

- tests that asserted nothing: all three are fixed and each fix was mutation-checked. `tests/test_stress.py` invariant 6 was a `pass`-body loop — the stub's premise ("minor/keeper players might share names with available") is false, measured at reset as zero overlap. It moved to a module-level `_check_ownership` and is now also called after every undo cycle, which is where it can actually fire: `/assign` pops from the pool and appends to a roster in one step and cannot desync them, while `restore_snapshot` copies teams and the pool as separate statements. Deleting `self.teams = restored.teams` leaves the pool count correct and is caught only by this check. `test_counterfactual_shows_alternatives` was `len(...) >= 0`; it now pins `alternatives == without.roster - with.roster`, and the direction is the load-bearing half — both differences are non-empty, so subtracting the wrong way round still names real players, just the ones you get by signing him under a heading promising the opposite. `test_team_players_nonexistent` accepted a 500 as a pass; it now pins `200` + `[]`, with a known-team companion so `[] == []` cannot pass forever. Also folded `_squeeze`/`_toast_of` (three and two copies) into `tests/helpers.py`.

- the team panel stays on the team you are looking at. Opening an opponent and fixing a salary snapped the panel back to BOT — every roster edit did, so an audit meant re-opening the team after each one. This was the half-fix left by the 2026-08-05 leak repair, not an oversight: those endpoints used to override `ctx["team"]`, and `team` *also* feeds BOT's Trade "I Give" list and the Buyout Analyzer, so editing an opponent put THEIR players in BOT's trade form. The one overloaded key is now two — `viewed_team` for the panel on screen, `team` staying BOT for everything that acts on your own roster — with a `_panels_viewing` helper carrying the view through /toggle-bench, /adjust-salary, /move-to-minors, /move-to-roster and /trade-between. That the split is load-bearing rather than cosmetic is mutation-checked: pointing trade_panel.html at `viewed_team` re-opens the original leak and `TestPanelContextIsolation` fails. **Two owner decisions.** (1) Draft actions reset the view: /assign and /undo return you to your own team, because reading SRL's Cap Used as yours right after a pick lands is the failure mode a sticky view invites. (2) No buyout dots on an opponent — `_recompute_buyout_indicators` scores every hypothetical against BOT's MILP total, so the scan is BOT-only by construction and an opponent's dots could only sit grey, reading as "not analyzed" when the answer is "this analysis isn't about you".

- surviving a corrupt state file: startup now walks current → `.backup` → fresh instead of "try one file, else start a brand new draft". `_save_state` has rotated a `.backup` on every save since the beginning and **nothing had ever read it** — 150 picks and several hours could vanish with the last good copy sitting untouched two inches away on disk. **The second half is what made it unrecoverable**: `_save_state` rotates unconditionally, so the first click after that restart moved the *corrupt* file over the good backup. One restart plus one click and both copies were gone. The fix renames an unusable file to `.corrupt`, which needs no change to `_save_state` — with `auction_state.json` absent, its `if os.path.exists(path)` guard already skips the rotation — and leaves the failure visible on disk rather than only in a log. Two further defects rode along. The `except (JSONDecodeError, KeyError, ValueError)` net was too narrow for the shape it guards: `from_json` does `data["teams"].items()`, so `{"teams": []}` raises `AttributeError` and the app **failed to start at all** — the worst outcome for the one file whose job is to survive a crash; it is `except Exception` now, handling rather than swallowing, since every alternative to degrading is worse. And the three `_backfill_*` helpers mutated the module global, so a candidate could not be validated before being installed as live state; they take `state` explicitly now. Pinned by `tests/test_crash_recovery.py`, all seven cases mutation-checked against removing the fallback, the rename, the broad except, and the rotation guard. Writing it turned up the trap the CLAUDE.md rule warns about: the backup-preservation test first passed against a build with the rename deleted, because it drafted "Nathan MacKinnon" — not in the biddable pool, so `/assign` returned 200 with a *warning toast* and never saved. `_draft` now asserts the player actually landed. Also fixed `test_dry_run.py::test_12_atomic_save_backup`, which read the real `data/state/` in defiance of the conftest isolation and passed only because a live 213 KB draft happened to be on this machine — verified by running that file with `data/state/` moved aside. Confirmed end-to-end against a real uvicorn server too: three picks, `truncate -s 120000` on the state file, restart — log named the backup, the panel came back with the draft, `.corrupt` was on disk, and the backup still parsed after a further pick. That run also priced the recovery honestly: the backup is **one save behind** by construction (`_save_state` rotates the *previous* current), so recovery costs the most recent transaction — BOT came back with picks 1 and 2, not 3. Inherent to one-deep rotation, not introduced here; noted in CLAUDE.md as a draft-day check rather than fixed, since a deeper chain is a different design. **Grilled the same day, and the grill found the change half-implemented — see below.**

- the grill of the above: a state file that PARSES could still be discarded silently. `_load_saved_state` wrapped the parse and all three backfills in one `except Exception`, so a backfill raising was indistinguishable from a corrupt file: the draft was renamed `.corrupt`, the ladder fell to a backup that failed identically, and the app started fresh answering **200** — the original disaster recreated one layer down, which is the specific thing the fix above set out to end. Probed against a byte-perfect state file: `good state still on disk under its own name? False`, `.corrupt is byte-identical to the GOOD state: True`. The realistic trigger is `_backfill_model_inputs` (`compute_pos_ranks`, or `max(p.team_probability …)` on one odd field) — i.e. exactly the legacy snapshots it exists to serve. Each backfill now gets its own net and is **never fatal**: not one of the three is load-bearing for the draft record, and a missing logo must not cost 150 picks. That also makes `.corrupt` mean "genuinely unparseable" again instead of being a name that lies. Owner decision: **degraded startups get a banner**, because the real defect was silence — `_startup_warning` is set by `lifespan` (backup used / fresh start despite a saved file / backfill skipped), passed through `_context`, and rendered by `base.html` *outside* `#app`, since every panel swap replaces `#app` and drafting one player would otherwise clear the notice that your draft is gone. `POST /reset` clears it. The banner names the consequence, not the function — "Could not refresh price model inputs, so PRICES MAY BE WRONG" — because it is read mid-auction by someone deciding whether to trust a number; the identifier and the exception stay in the ERROR log. Two more from the same pass: the `state_dir` fixture restored `auction_state` but not the five other globals `lifespan`/`_recompute` own, which is worse than restoring nothing (a restored roster paired with a MILP solution solved against a different one — unreachable today only because every other test file context-manages `TestClient`); and `_draft` asserted on acquired-only, so it would fail spuriously against a full roster where `/assign` auto-routes to the minors. Pinned by seven more cases, mutation-checked against folding the backfills back into the parse net, dropping the banner from `_context`, and dropping the `/reset` clear.

- the player-chart duplicate id: `player_chart.html` was both the chart body and the mount, carrying `id="player-chart-container"` while `bid_limits.html` rendered an empty div with the same id as the players table's swap target. htmx resolves a target by id and takes the first match, and `all_panels.html` puts `area-auction` before `area-players`, so **during a live bid a chart link in the table rendered its chart into the bid panel in the other column**. Confirmed in Chrome. **Two things the old entry got wrong.** It said both copies are in the document only "during an active auction" — in fact `hx-swap="innerHTML"` nests the response's own copy of the id *inside* the mount, so one click duplicated it on a quiet page too. And it scoped the fix as "body partial + two named mounts, re-pointing the swap targets": the bid panel's chart is server-rendered inline and nothing targets it, so it needs no id at all — the body simply drops its id, `bid_limits.html` and every link are untouched, and the close button becomes `this.closest('.price-chart-card').remove()`, which needs no id to know which chart it sits in. Two more defects rode along: `/player-chart` rendered **`explanation.html`** on its no-player path, injecting the whole Counterfactual panel and a second `id="explanation"` into the chart slot; and that survived because both tests covering it (`test_endpoints.py`, `test_edge_cases.py` — the same request twice) asserted only `status_code == 200`, so they could not fail. Now one copy with assertions that can. Pinned by five endpoint cases, an id-uniqueness guard over `GET /`, and two browser cases — browser because the duplicate existed only in the **assembled** DOM (page plus the `/bid-check` fragment htmx swaps in), never in one server response. The load-bearing browser case closes the **bid panel's** chart, not the table's: with an id-free body, closing the table's chart works under either implementation, so a test aimed that way passes against the bug. Mutation-checked all four ways.

- surviving a data refresh. `players.csv`, `model_params.json` and `team_odds.json` are all replaced from the pricer repo before a draft, and that had never been done with the suite watching. A drill — a copy of the repo with points perturbed, rows dropped, names changed and odds nudged — produced **25 failures**, and reading them was the problem: 19 were legitimate arithmetic in `test_data_loader.py`, 4 reported a failure several steps downstream of its cause, and **2 were a real bug in the app**. The bug: the player NAME is the primary key everywhere, and `players.csv` had 2158 rows against 2155 distinct names. It fails two ways, both live. `Matt Murray` (DAL and TOR) are both biddable, so `biddable[name] = ...` overwrote one — **705 eligible rows loaded as 704 and the DAL one could not be drafted at all**, today, on the current file. `Jack Hughes` and `Elias Pettersson` are each a keeper on HSM *and* a UFA row: different dicts, nothing overwrites, so the same name is owned and draftable at once — hidden only by the zero-point exclusion the next projection refresh removes, which is exactly what the two `test_stress.py` failures were. Owner decision: **disambiguate at load** rather than introduce a real player id (backlogged above with the inventory). `_disambiguated_names` suffixes **every** row of a colliding group — `Name (TEAM)`, then `Name (TEAM POS)` because both Petterssons are VAN, then `Name (#n)` — because renaming only the later row leaves `X` beside `X (VAN D)`, which reads as one player listed twice. The goalie-wins join deliberately keys on the RAW name: `goalie_projection_stats.csv` carries that and cannot disambiguate either, so looking the rename up there would silently drop `proj_wins` for every renamed goalie. Saved state files load their own pool verbatim, so an in-progress draft is untouched. **The banner had to split in two**, which the crash-recovery tests caught: `#startup-warning` describes this boot and `/reset` clears it, while the renames are a property of the CSV and survive a reset, so merging them either turns the degraded-boot alarm into permanent wallpaper — the thing `test_the_happy_path_shows_no_banner` exists to prevent — or makes the rename note vanish while the renames are still in force. On the test side the loader file now does its three jobs separately: **rules** against `tests/fixtures/players_sample.csv` (ours, never refreshed, with a guard that it still covers every branch), live data checked only by **invariants**, and one `data_fingerprint.json` that turns a refresh into a single failure with a field-by-field diff. Its regeneration mode fails on purpose, so an exported `FCHL_WRITE_FINGERPRINT` cannot leave the guard permanently self-healing. Flows stopped naming players: the dry run derives its 40 picks from the pool against a kept-fixed (team, salary) shape, the trade/buyout tests pick targets by role (worst $/point, best player, cheapest spare), and every draft pick goes through `helpers.assign`, which reads the transaction log — `/assign` answers **200 with a toast** when it rejects, which is how one missing name became `assert 24 == 25` three tests later. `_drain_state` now **solves for** its market ceiling instead of hoping a cap-space figure lands somewhere useful; the drill had put it at exactly `MIN_DRAIN_PRICE`, the one value that fails `ceiling < MIN_DRAIN_PRICE` with nothing wrong. That change exposed a real latent bug it had been masking: `assert "0 can afford" not in reasoning` also matches **"10 can afford"**, and passed only while the fixture happened to leave a single-digit count. Two data-coupled assertions closed with it, and the first turned out to hide a third bug. `TestPriceColumn` now asserts the *rule* — capped iff the model price exceeds the ceiling — rather than today's outcome, but at full budgets the ceiling ($11.4M) sits over every model price, so both sides are false for every row and the rule **cannot fail**; mutation-checking it (cap a dollar low, do not cap at all) showed both mutants surviving. It only has content where something IS capped, which sent the check into `test_capped_flips_once_the_ceiling_bites` — where the same two mutants **also** survived. That test set `penalties = 54.0` with a comment claiming "~$2.8M of cap", ignoring the salary already on the roster: every opponent's physical max fell under MIN_SALARY, they all left the demand count, and `compute_market_price` returned `MIN_SALARY` from its `floor_demand` branch **without ever reaching the ceiling line**. The test named after the ceiling had been exercising the floor. It now solves for a $3M ceiling the same way `_drain_state` does, asserts `floor_demand` is False, and both mutants die. The dry run's dead `overlap` variable — computed and never asserted on — became the ownership check it looked like. Four more hard-coded roster names the drill happened not to trip (`test_edge_cases.py` x2, `test_endpoints.py`, `test_htmx_interactions.py`) went through a new `helpers.a_buyout_candidate`, which filters on `can_be_bought_out` first — picking the worst value overall lands on an A-E prospect the engine correctly refuses. **The drill was then re-run against the finished work and went 25 failures → 2**: the fingerprint, which is the whole point, and one more latent bug of the same family. `TestCounterfactualVerdict::test_verdict_follows_the_engine_both_ways` was a two-way `if gain > 0 / else` against a panel that has **three** branches — its sibling `test_break_even_is_a_toss_up_not_a_skip` pins zero as "Toss-up", and the two-way loop demanded "Skip him at $" for it. Nothing in the live pool scores exactly 0, so the test had never met the case; the perturbed points produced one and the suite failed on correct behaviour. Now a sign-keyed `_VERDICTS` table with all three entries, walking further down the pool only until both recommending branches are seen (every hit is a MILP solve), asserting on the *unconditional* half of each sentence since the clauses naming an alternative are all `{% if alt %}`. Renamed `..._every_way` so the count is in the name. The remaining verification: 631 tests green including browser, both Matt Murrays independently draftable through `/assign`, and one new browser case — `TestTheDataBannerOutlivesAPick` — for the half a `TestClient` cannot answer, that the rename note is still in the document after htmx has replaced `#app`. Mutation-checked four more ways: the buy branch given the skip wording, the break-even branch widened to swallow the negatives, and the banner block moved inside `#app` (which the browser case names in its failure message)

- the buyout scan covers the minors, and the dot ids stopped being unusable. The requested work was the under-report: `_recompute_buyout_indicators` and `buyout_dots.html` read `roster_players` while `buyout_panel.html` reads `all_players`, so **Scan reported on BOT's 11 active players and silently said nothing about 4 group-3 players in the minors holding $2.0M of cap** — eligibility is a property of the contract group alone, so those are legal buyouts the Analyzer had been offering the whole time. "No buyout helps" and "I didn't look" render identically. Now all three read `all_players`, the Minors table has a dot column, and the scan costs 15 solves instead of 11 (~1.4s → ~1.9s) on a manual button. The test that had pinned the gap (`test_scan_does_not_solve_for_players_with_no_dot`) asserted the opposite and is inverted; its premise — "minors have no row in the team table" — expired when that table was added and nobody revisited it. **Investigating it found a worse defect underneath, and the browser is the only place it is visible.** htmx resolves an out-of-band target by SELECTOR, not `getElementById` — `htmx-1.9.10.min.js`, `Ee`: `var t = "#" + ee(i,"id"); … re().querySelectorAll(t)` — called from a plain forEach with **no try/catch**, so an id that is not a legal CSS identifier does not merely miss its own target: `querySelectorAll` throws and every remaining swap in the response is abandoned. Measured in Chrome with `Matt Murray (DAL)` drafted to BOT: **12 placeholders, 0 resolved, 12 still grey**, console `htmx:swapError` → `SyntaxError … at Ee ← oe ← Ce ← je`, and a Scan button that looks like it simply never finished. The old inline expression stripped `'` (U+0027) and `.`, which reads as covering apostrophes — `players.csv` uses U+0060, so it matched none of `Drew O`Connor` and friends, and it never contemplated the parentheses in `Tony DeAngelo (NCM)` (a raw CSV name, already on BOT's minors) or the ` (TEAM)` / ` (TEAM POS)` / ` (#n)` suffixes `_disambiguated_names` emits. **Two things I got wrong while scoping it, both caught by checking rather than by reasoning.** The seven backtick names are all 0-point, so the loader excludes them and they are *not* draftable — the reachable trigger today is the two disambiguated goalies, not them. And the first version of the browser test picked its victim by "name contains a non-alphanumeric", which selected `J.T. Miller` — whose dots the old strip already removed — and **passed against the live bug**; it now prefers a name the old strip could not fix and asks Chrome's own parser whether each rendered id is a valid selector, rather than approximating CSS with a regex. Fixed by one `dom_id` Jinja filter (`main.py`), used by all three call sites, slug plus an 8-char sha1: the slug alone is lossy and two players colliding on a derived key is exactly what the disambiguation work removed, so it must not return one layer down as two dots fighting over one target. A third hand-rolled copy of the id lived in `test_ineligible_player_gets_no_buyout_dot`, whose `assert dot_id not in html` would have passed forever once the real id changed. Pinned by `TestNamesSurviveBecomingDomIds` (every pool + roster name yields a legal, unique id — the check that would have caught the backtick and survives a data refresh), four endpoint cases for the minors dots including the BOT-only gate, and one browser case

- a sweep of three long-deferred "real but won't bite" entries. Probing each against the running app before touching anything turned out to be the whole value of the exercise: **two were worse than their entries said and one was smaller**, and in all three cases the entry's stated mechanism was wrong. (1) **`/bid-check` swallowed a name it could not find.** The entry rated reachability low because "the player field is `readonly`" — true of the *live-auction* form, but the **Start Auction** field is free text (`required` plus a datalist), so any typo lands there, and the response swapped `#bid-panel` back to a blank form: 1178 bytes, no toast, no message, the name you typed simply gone. The path the entry *did* describe is the rarer one, and **the entry was wrong about what it does**, which is why it stayed deferred for a month: it said htmx "swaps nothing at all" when `hx-select` misses, and reasoned that silence beat destroying the bidding session. Driven in Chrome against the pre-fix template, the price input's swap is `outerHTML` on `#bid-advice`, so an unmatched selector swaps the EMPTY selection in and **deletes the target** — `#bid-advice` count 0, while `#bid-panel`, `#bid-form` and `#bid-price` all survived, no console error. The verdict block vanished mid-bid and left a half-built panel, which is neither "nothing" nor "resets the panel". Another entry reasoned about htmx instead of measuring it; that is now three. One fix covers both: the else branch renders an inline note carrying `id="bid-advice"`, deliberately the same id the verdict block uses (the branches are mutually exclusive, so the document still holds one), and the typed text comes back in the input's `value`. Pinned by four endpoint cases plus a browser case, because "the box empties and nothing is said" also answers 200 with a valid panel — mutation-checked by reverting the template, which times the browser case out on `#bid-advice`. (2) **Rejected roster edits were spending undo history**, not the "full JSON round-trip" the entry priced at 2.6ms. `save_snapshot()` evicts index 0 once the chain passes `MAX_SNAPSHOTS`; `restore_snapshot()` pops from the **other end** — so snapshot-then-restore reads as a no-op and destroys the oldest entry. Measured on a full chain: depth 50 → 49, oldest gone. **Four** endpoints, not the three listed: `/adjust-salary` had already been fixed, while `/trade-execute` and `/buyout` were never named. `AuctionState` now splits `capture_snapshot` / `commit_snapshot` / `rollback_to`, with `save_snapshot`/`restore_snapshot` re-expressed in terms of them so `/undo` is untouched; endpoints capture, attempt, and commit on success. `send_to_minors` and `recall_from_minors` validate before mutating and need no rollback; `execute_trade` strips both rosters before adding to either and does. **Two of the seven tests could not fail as first written** — the chain assertions needed to read the *oldest* entry as well as the depth (evict-then-pop ends at the same length), and the trade rollback case needed *two* give players, since with one the removal raises before anything moved and a build with no `rollback_to` passed. `/buyout`'s rollback survives as insurance for its two-step mutation but is currently unreachable, which the test docstring says outright rather than implying an assertion covers it. (3) **`compute_marginal_value` re-deriving `physical_max_bid`** was the smallest, and ships with **no test on purpose**: the two expressions agree on every reachable state, so any behavioural test would pass against both and read as coverage it hasn't got. They do diverge in principle — an over-committed team gives the hand-rolled version a negative ceiling where the property clamps to 0.0 — but the entry credited the wrong guard for that being unreachable. It named the `total_spots_remaining <= 0` early return, which only picks the branch; what protects the clamp is the `with_at_min` Infeasible return, since a legal roster with the player forced at the floor requires `spendable_budget >= 0`. Verified identical before and after on an over-cap BOT: marginal 0.5, value_cap 0.0, DROP

## [2026-08-06]

### Added

- counterfactual auto-shows during bidding: the "what do I lose if I let him go?" analysis now arrives by itself for the player under the hammer. It existed and was good — `generate_counterfactual` returns both rosters, the points delta and the alternatives you'd draft instead — but reaching it cost a click, and **the link only rendered on a DROP verdict**, so on BID or CAUTION there was no route to it from the auction panel at all. Three decisions carry the change. (1) **Lazy, not inline**: two MILP solves is ~200ms, and `/bid-check` fires on every price change and bidder toggle — folding it in would have taken that endpoint from 9ms to ~210ms and reopened the Assign blur race. It loads on its own request instead, pinned by `test_bid_check_does_not_compute_it`. (2) **Cached per epoch** in `main._counterfactual`, cleared by `_recompute()` alongside the marginal — a stale counterfactual is the worse of the two because it names *specific alternative players*, so a missed clear recommends someone already sold; `test_a_sold_player_never_survives_as_an_alternative` asserts exactly that. Warm load 9ms. (3) **Placement is load-bearing twice**: outside `#bid-advice` (which a price change replaces via `hx-select`, so `load` would re-fire per keystroke) and *after* the Assign form, so a fragment arriving 200ms late cannot push the button out from under a pointer already moving toward it. Both are invisible at the call site and a tidy-up would undo them, so `TestCounterfactualAutoLoads` asserts them on index position. `explanation.html` was split into a body partial plus two mounts to avoid a duplicate `id="explanation"`; `/explain` takes `?inline=1` to pick between them. Each mount owns its own empty state — the panel's "click ?" prompt would be wrong in a bid panel that has no "?".

- browser test harness, and the stale-counterfactual race **closed as not reachable**. Six UI changes shipped that day and none had been opened in a browser; the two manual confirmations on file both predated them, and one (`d57d344`, Assign reads the live price) was taken against the *pre-9ms* code, i.e. a version where latency masked the very race later fixed. `tests/test_browser_ui.py` drives the installed Chrome through Playwright (`channel="chrome"`, so no `playwright install` and no browser download; `requirements-dev.txt` keeps it out of the runtime set, and `importorskip` keeps the suite green without it). Seven checks, each for something `TestClient` cannot see. Two paid for the harness immediately, by mutation: moving the counterfactual mount above the Assign form makes the button **jump 322px** as the analysis lands — the placement rule had only ever been defended by an assertion on HTML ordering — and reverting `hx-select="#bid-advice"` on the price input swallows the Assign click outright, no toast, no pick recorded, which is the blur race reproduced live at the speed that made it dangerous. Also pinned: exactly one `/explain` per panel render (the `hx-swap="innerHTML"` no-loop claim, previously reasoned from minified htmx), `n` mid-bid, bidder toggles, the 1-col breakpoint, and the runtime-built `'alert-' + type` toast class that a source-scanning Tailwind build would silently drop. **The race itself turned out to be unreachable, and the earlier finding was wrong about that.** htmx 1.9.10 really does leave a superseded XHR running and really does resolve the target by id — but `/explain` is `async def` wrapping a *blocking* MILP solve, so it holds the single event loop and requests serialise FIFO. Measured: a WARM request fired 20ms after a COLD one still finished last (202.5ms vs 198.8ms). No `hx-sync` was added, because shipping an untestable attribute against a race that cannot occur is worse than not shipping it. Instead `TestResponsesCannotOvertakeEachOther` asserts the precondition, so the obvious future optimisation — moving the solve off the loop to kill the 200ms cold path — fails loudly with the fix to apply written in the assertion message rather than reintroducing the hazard in silence.

### Changed

- offline: htmx, DaisyUI and the Tailwind Play CDN are vendored under `static/vendor/` and pinned by `tests/test_offline_assets.py` — **manually confirmed working offline 2026-08-06**

- CSS size: vendored DaisyUI trimmed 2.93 MB → 468 KB (84%) by `trim_daisyui.py`, which drops unused opacity-suffixed colour utilities and copies everything else through byte-for-byte. **The old entry here was wrong twice.** It blamed the ~30 stock themes (those are 53 KB of the 2.93 MB; the real bulk is 21,588 generated colour-utility rules, 84%) and recommended `dist/styled.min.css`, which defines 609 classes to full's 24,940 and is missing 19 the app uses — `btn-sm`, `badge-xs`, `table-xs`, `tooltip`, `text-warning`, `bg-base-200` among them. Following that advice would have caused exactly the mid-draft visual regression the entry warned about. Guarded by two tests in `tests/test_offline_assets.py`: one drives the running app and checks every colour class it emits is defined, the other fails on an undeclared `bg-{{ … }}/NN` in a template

- Max-bid display: the bid panel now shows the two numbers `max_bid` was always made of — `Worth up to` (value cap, hard) and `Should win it` (expected stop, forecast) — instead of the blend that doubled from $4.1M to $8.5M when the price rose one increment past the forecast. `BidRecommendation` gained `value_cap`/`expected_stop`/`stop_status`; `max_bid` is untouched, so the ~15 assertions resting on it needed no edits. `stop_status` distinguishes the two reasons the forecast retires — "no rivals left" vs "bidding passed it" — because a bare dash for both told the operator nothing. Bonus: when `value_cap < expected_stop` the panel now reads "worth $8.5M, will take $11.5M", which is *why* the verdict is DROP — information the single figure never carried. Pinned by `tests/test_bid_calculator.py::TestBidPanelNumbers`, whose load-bearing test is that `value_cap` never moves with price

- bid advisor latency: `/bid-check` went from **~1000ms to ~9ms** on repeat interactions (114×), meeting the long-carried <500ms interaction budget. 97% of the request was `compute_bid_recommendation` and 80% was the ten MILP solves inside `compute_marginal_value` — which takes neither a price nor a bidder list, so it is pure in (roster, budget, pool, market prices) and *cannot* change between two bid checks on the same player. A live auction was spending a second per $0.1M increment re-deriving an identical number. Now cached per state epoch in `main._marginal_value`, cleared wholesale by `_recompute()` for the same reason it drops `last_trade_eval`; `compute_bid_recommendation` gained an optional `marginal_value` kwarg so the optimizer stays pure and the ~15 tests that call it with synthetic states are untouched. Clearing rather than versioning because an empty dict cannot serve a stale entry. The first check on a new player is still ~1000ms and correctly so. Pinned by `tests/test_bid_cache.py`, whose load-bearing test walks ten mutating endpoints and compares cached against freshly-computed after each — the failure mode being guarded is not slowness but a stale number shown as live advice. That file costs ~40s of suite time, 20.5s of it in the mutation walk, because each mutation runs a real `_recompute()` solve — inherent to what it proves, recorded here so it doesn't read as waste to whoever next looks at suite time

### Fixed

- roster capacity: the 24-man active roster is enforced at last — `add_acquired_player` routes extras to the minors and returns whether it did, `recall_from_minors` refuses when the roster is full, and both trade paths remove from every roster before adding to any (auto-routing made ordering load-bearing: a full team that gained before it lost sent the incoming player down on an even 1-for-1 swap). Pinned by `tests/test_roster_capacity.py`. **The old entry's stated harm was wrong.** It said a 25-man roster makes `solve_optimal_roster` infeasible and would "degrade every recommendation if it ever happened to BOT"; measured, bid advice at 24 and 25 active is *identical* (`max_bid $0.0M, DROP`) because at 24 the MILP already correctly says "no room". The real harm is `lineup_points`, which picks the best 12F/6D/2G off the ACTIVE roster — a 25th active player competes for a starting slot he cannot legally hold, worth **+70 phantom points** for one 120-point forward at identical cap, and that is the number `evaluate_trade` accepts or declines on. Owner decision (2026-08-06): auto-route rather than refuse, so a live sale never gets blocked. Closes the `team_panel.html` negative-Spots entry too — the display clamps at 0 while the property stays signed for the MILP

- drain nominations: ranked on market price, tie-broken on least surplus gifted (`_best_drain_candidate`), pinned by `tests/test_nomination.py::TestDrainStrategy`. **The old entry's stated mechanism did not reproduce.** It claimed dividing by `can_afford` made the tool favour players few opponents could afford; measured over 291 randomised budget spreads and two simulated drafts, the old formula's pick matched the drain-maximising pick *every time*, zero divergences — the clearing price is monotone in model price, so both rules collapse to "nominate the priciest unwanted player". Its conclusion ("gifting the rich opponent a bargain") was right, one level down: once the ceiling binds, every player above it drains *exactly* the ceiling (35 of 636 UFAs tied at $2.5M in the test state), and ranking that tied set by model price picked the biggest bargain for the buyer — $5.2M of surplus vs $0.0M for the same $2.5M drained. Two further defects rode along: drain was the only optimizer path reading raw model prices, so the panel showed `Expected: ~$7.7M` for a player who could fetch $2.5M; and `max(can_afford, 1)` scored the unaffordable case highest, printing `0 can afford` as a reason to nominate. The `needing_position` multiplier also cost real drain dollars where the ceiling did *not* bind — it took Aho at $7.5M over Vasilevskiy at $7.7M, leaving $0.3M unburned (pinned by `test_position_need_never_outranks_dollars`). Fix reuses `compute_market_price`, verified over 4,000 random league states to be exactly the second-highest-willingness clearing price. Measured over a 200-pick simulated draft: the new rule drains strictly less in 0 of 16 sampled decisions (total $33.1M → $33.4M) while surplus gifted falls from $9.6M to $0.1M

- over-cap trades warn: `/trade-between` and `/trade-execute` checked team codes, name resolution and self-trades but never the cap, so either side could finish over `SALARY_CAP` and get the same green "Trade executed" as a legal deal. Owner decision: **warn, don't refuse** — the league permits temporary over-cap states and resolves them with buyouts, so blocking would stop a legal manoeuvre. `_cap_overages()` now names every team over the cap, worst first, and lifts the toast to `warning`: *"Trade executed: BOT ↔ SRL — SRL $7.5M over cap"*. It rounds before testing, because `total_salary` sums many $0.1M values and raw float noise reports "$0.0M over cap" on an exactly-legal team. A demotion-to-minors note deliberately stays a success — that note pre-dates this and folding it into the warning tier was not asked for. Pinned by `tests/test_trade_buyout_undo.py::TestOverCapTradesWarn`, which asserts the owner decision first (the trade still executes) and carries its own function-scoped client, since squeezing a team's `penalties` under the file's module-scoped fixture would leak a mangled cap into every later test. Writing it turned up something worth keeping: in a two-team trade the salary deltas are equal and opposite, so a trade *cannot* push both sides over on its own — both being over means both already were

- Assign-click swallowing (the price-input blur race): closed, and it is worth recording that **the fix immediately above is what made it urgent**. `change` on a number input fires on BLUR, and clicking Assign is what blurs the price box — so that /bid-check is triggered by the very click it can destroy. If the response replaces the region Assign sits in before mouseup, the browser fires no `click` at all (mousedown and mouseup landed on different elements) and the pick is silently not recorded. The entry sat open for a day reading "the response almost always lands after mouseup", which was true *because* /bid-check took ~1000ms — latency was masking it, and taking it to ~9ms put the swap squarely inside a 70–150ms click. Fixed with `hx-select="#bid-advice"`, narrowing the swap to the verdict block; the form, bidder grid and Assign are never replaced. Safe because Assign's presence comes from `bid_winner`, which reads the bidder list and never the price, and its label is already kept in step by `syncAssignPrice()` on `input`. Pinned by `tests/test_endpoints.py::TestAssignSurvivesAPriceChange`, mutation-verified against reverting the target

- bidding session survives nomination: `#auction-control` split into `#nomination-panel` and `#bid-panel`, each swapped by its own endpoints, so `/nominate` and `/set-nominator` physically cannot return the region the bidding session lives in. That session — player, price, bidder toggles, highest bidder — is never persisted server-side, so before the split both endpoints re-rendered the whole panel from base context and wiped it; `Ctrl+Z` was no help, because `/undo` restores auction state and not DOM session. **The severity in the old entry was understated.** It read as a stray-click hazard; in fact `/nominate` fires on a bare `n` keypress and the shortcut's guard (`typing`) only covers INPUT/TEXTAREA/SELECT, so focus on a `<button>` — where it lands after clicking a bidder logo to drop a team out — left it live. The template already encoded the intent not to nominate mid-bid (the "It's My Turn" button was hidden while `bid_advice` was set); only the global keydown handler bypassed it. That guard is now dropped, since nominating is safe. Bonus: the 704-option player datalist moved to the shell, cutting the `/bid-check` payload from ~47KB to 13KB (72%) on the endpoint that fires on every price change and every bidder toggle. Pinned by `tests/test_endpoints.py::TestBidSessionSurvives`, which asserts the structural property — an endpoint cannot clobber a region it does not return — rather than any rendered value

- buyouts: eligibility restricted to groups 2/3 (`can_be_bought_out`), the panel now lists eligible players wherever they sit, trade scenarios stop proposing illegal buyouts, keepers can be sent to the minors, and both buyout endpoints report the real reason. **The old entry here asked for "minors-aware buyout math" — that premise was wrong.** Under the owner's rules (2026-08-06) a legal buyout only ever targets a group 2/3 player, whose salary is fully on the cap wherever they sit, so `salary_freed`/`net_cap_freed` needed no branch and got none. The actual defects were a missing legality guard in both directions: the panel offered A-E prospects on the active roster (illegal, and the numbers looked plausible because those *do* count on cap) while hiding group 2/3 players in the minors (legal, and where everyone drafted past 24 lands). Pinned by `tests/test_buyout_eligibility.py`

- over-cap warnings on the remaining three endpoints: a full grill of the trade-warning change found `/move-to-roster` had the same gap, worse. `PlayerOnRoster.counts_on_cap` is `group in MINOR_CAP_GROUPS` (`{"2","3"}`) for a minor, so a group A-E prospect is cap-free while down and cap-counted the instant it is recalled — and **145 of the 149 players in the minors at reset are group A-E, all with a real salary** (up to $3.0M). Only the 4 auto-routed draftees are cap-neutral to recall, so the safe case was the rare one. The endpoint also ended with a bare `_render()`, i.e. no toast at all on success, so an over-cap recall was silent rather than merely mis-coloured. Closed alongside the two remaining members of the class: `/adjust-salary` (backlogged the same day) and `/assign` (deferred as commissioner-protected, included because a free check on the busiest endpoint is what makes the same warning elsewhere trustworthy). An audit of all 13 mutating endpoints found no others — `/move-to-minors`, `/buyout` and `/toggle-bench` cannot raise cap load. `/adjust-salary` composes its two independent warnings into one toast instead of returning on the first, and supplies a subject when only the cap note fires: *"John Gibson set to $2.0M — BOT $0.5M over cap"* rather than a bare fact about the team. Pinned by `tests/test_endpoints.py::TestOverCapRosterEdits`; the load-bearing test is the group-3 recall, which proves the warning tracks `counts_on_cap` rather than firing on any near-cap move

### Investigated

- short-roster MILP mode — **closed as not-a-bug.** I reproduced `Infeasible` + `$0.0M/DROP` whenever `remaining_budget < spots_remaining * MIN_SALARY` and proposed relaxing the MILP's `== spots` constraint. Owner: **the league commissioner software refuses any bid that would leave a team unable to fill a full roster**, so that state is unreachable through legal bidding and the constraint is correct, not over-strict. Verified our advice already agrees with the commissioner — `value_cap` caps on `physical_max_bid`, which reserves `MIN_SALARY` per unfilled spot, and across six budget/roster states `max_bid` never exceeded the commissioner's limit. Do not "fix" this later; recorded in memory as `feedback_commissioner_enforces_roster_reserve`. The related `solve_optimal_roster` entry above stays open — it covers a different trigger (positive-point pool smaller than remaining spots)

## [2026-08-05]

### Fixed

- bid advisor: `8b928eb` (WIN not DROP when last bidder standing), `a3de737` (verdict ladder runs on value, not the blended max_bid), `ce20814` (one definition of "last bidder standing")

- money handling: `fa60955` (floor budgets to the $0.1M step), `d9e4fd2` + `4d77c21` (quantize salary on /assign and /adjust-salary via `_legal_salary`)

- market: `4dc59da` (a full roster is capacity, not a zero ceiling), `0bb86c0` (demand counts use "can bid", not "has spots")

- UI: `d57d344` (Assign reads the live price — **manually confirmed in a browser 2026-08-06**: typing a new price and clicking Assign with no Tab/Enter records the typed price), `713f029` (zero counterfactual delta is a toss-up), `a779ec2` (one price column), `fcd9647` (chart label size)

## [Earlier]

Before the backlog carried write-ups. Fix commits only.

### Fixed

- `200e80d`, `e4a5871`, `c30a636`, `8622f74`, `6dd17e6`, `9aece0d`


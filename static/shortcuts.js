/* Toast notifications triggered by HTMX HX-Trigger response header */
document.body.addEventListener('showToast', function(e) {
    var container = document.getElementById('toast-container');
    if (!container) return;
    var div = document.createElement('div');
    div.className = 'alert alert-' + (e.detail.type || 'info') + ' text-sm py-2 px-3 shadow-lg';
    div.textContent = e.detail.message;
    container.appendChild(div);
    setTimeout(function() { div.remove(); }, 4000);
});

/* Auto-solve the League State Proj column after a pick.

   `_recompute()` empties `exact_projections` on every mutation, so one pick
   returns all ten opponents' Proj figures to an estimate that runs +68 mean /
   +193 worst and moves 9 of 10 teams in rank order — BOT's own rank badge
   included. Owner decision 2026-09-10: re-solve after every pick rather than
   leave the column degraded until someone reads the `estimated` marker.

   Server-driven, via HX-Trigger-After-Settle on POST /assign, and NOT an
   `hx-trigger="load"` mount inside league_state.html. That template ships
   inside all_panels.html, which answers thirteen different things including
   GET / — a load trigger would fire a ten-solve scan on initial page load and
   after every bench toggle and salary edit. /assign is what the owner asked
   for and what the server can name.

   Coalesced, not stacked. A second pick landing mid-scan makes
   `_publish_if_current` discard the first scan's work silently (by design —
   `_state_version` moved), so without a guard two quick picks would run 16 CBC
   subprocesses for one usable answer. With a guard but no queue, the column
   would stay on estimates exactly when drafting fast, which is the failure
   this feature exists to remove. So: at most one in flight, and re-fire once
   on completion if a pick arrived while it ran.

   `swap: 'none'` because the response is out-of-band fragments only — the same
   reason the manual Solve Standings button uses it. */
var standingsScanInFlight = false;
var standingsScanQueued = false;

function autoSolveStandings() {
    if (standingsScanInFlight) {
        standingsScanQueued = true;
        return;
    }
    standingsScanInFlight = true;
    // `finally`, not `then`: a failed scan must still release the latch, or one
    // network hiccup silently retires the feature for the rest of the draft.
    htmx.ajax('GET', '/solve-standings', {swap: 'none'}).finally(function() {
        standingsScanInFlight = false;
        if (standingsScanQueued) {
            standingsScanQueued = false;
            autoSolveStandings();
        }
    });
}

document.body.addEventListener('solveStandings', autoSolveStandings);

/* Keyboard shortcuts for auction day */

document.addEventListener('keydown', function(e) {
    var tag = e.target.tagName;
    var typing = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT'
        || e.target.isContentEditable;

    // Ctrl/Cmd+Z: state-level undo — never while editing a field (the user
    // is undoing their typing, not the last draft pick), never on key-repeat
    // (holding Z must not unwind multiple picks).
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') {
        if (typing) return;
        e.preventDefault();
        if (e.repeat) return;
        htmx.ajax('POST', '/undo', {target: '#app', swap: 'innerHTML'});
    }

    // N: nomination recommendations. (Ctrl+N is reserved by Chrome/Firefox
    // and cannot be intercepted — plain letter keys outside inputs work.)
    //
    // Targets #nomination-panel, NOT the whole #auction-control: `typing` is
    // false whenever focus is on a button, which is where it lands after
    // clicking a bidder logo, so a stray `n` mid-auction used to replace the
    // bid panel and take the player, price and every bidder toggle with it.
    if (!typing && !e.ctrlKey && !e.metaKey && !e.altKey && e.key.toLowerCase() === 'n') {
        e.preventDefault();
        htmx.ajax('GET', '/nominate', {target: '#nomination-panel', swap: 'outerHTML'});
    }

    // /: focus the header player search. preventDefault is required — Firefox
    // binds / to Quick Find, which would swallow the keystroke and open its
    // own search bar over the app.
    //
    // Written `e.key.toLowerCase() === '/'` rather than `e.key === '/'`
    // because TestShortcutsModal reads the bound keys out of this file with
    // that exact pattern; a shortcut it cannot see is one the modal can drift
    // away from silently.
    if (!typing && !e.ctrlKey && !e.metaKey && !e.altKey && e.key.toLowerCase() === '/') {
        var search = document.querySelector('#player-search input');
        if (search) {
            e.preventDefault();
            search.focus();
            search.select();
        }
    }

    // Escape closes the result list. Deliberately NOT global: the shortcuts
    // modal is a <dialog>, which closes itself on Escape, and preventing the
    // default anywhere else would break it. `typing` is true here by
    // construction — focus is in the search box — which is why this sits
    // outside that gate.
    if (e.key === 'Escape') {
        var box = document.getElementById('player-search');
        if (box && box.contains(e.target)) {
            closePlayerSearch();
            // Clear the QUERY too, not just the list, and tell htmx about
            // it. The input carries the `changed` modifier, and htmx compares
            // against a value it cached at trigger time — not against the
            // DOM — so assigning .value alone leaves it believing the box
            // still says "hu": re-typing the same surname then fires no
            // request and the results never come back. Measured; the
            // silent-assignment version looked correct and was not. The
            // dispatched event costs one request for an empty query, which
            // answers 200 with an empty body.
            var field = box.querySelector('input');
            if (field && field.value !== '') {
                field.value = '';
                field.dispatchEvent(new Event('input', {bubbles: true}));
            }
            e.target.blur();
        }
    }
});

/* Surface failed HTMX requests — without these listeners a failed POST
   swaps nothing and the user believes the action was recorded. */
document.body.addEventListener('htmx:responseError', function(e) {
    document.body.dispatchEvent(new CustomEvent('showToast', {detail: {
        type: 'error',
        message: 'Request failed (' + e.detail.xhr.status + '): '
            + (e.detail.requestConfig ? e.detail.requestConfig.path : ''),
    }}));
});
document.body.addEventListener('htmx:sendError', function() {
    document.body.dispatchEvent(new CustomEvent('showToast', {detail: {
        type: 'error',
        message: 'Network error — the request did not reach the server',
    }}));
});

/* Dismiss a nomination recommendation once you have acted on it.

   The recommendation is stale the moment bidding starts, and it competes with
   the bid panel for attention at the highest-tempo moment of the draft. Only
   the half that was acted on goes: per the CBA a nomination turn is 1 RFA + 1
   UFA and an RFA sale KEEPS the turn, so the other half is the next thing the
   operator needs.

   On afterRequest, not on click, and the reason is the GATE. This comment
   used to say htmx aborts an in-flight request whose trigger leaves the DOM;
   measured 2026-09-10 in Chrome, it does not — removing the element during
   `htmx:beforeSend` fires no `htmx:abort` and the swap lands anyway, because
   `b.onload` in htmx-1.9.10.min.js swaps before it fires anything. The claim
   was reasoned off the minified source rather than run, and it was repeated
   into two other files before anyone tried it.

   What is true: `successful` is only knowable after the response, and a
   failed request must leave the recommendation on screen — /nominate is the
   only way back. On click you would discard it either way. */
document.body.addEventListener('htmx:afterRequest', function(e) {
    if (!e.detail.successful) return;
    var pick = e.target.closest && e.target.closest('.nomination-pick');
    if (!pick) return;
    var panel = pick.closest('#nomination-panel');
    pick.remove();
    /* Take the Clear button with the LAST card. Its Jinja gate only re-runs
       when the server renders, so bidding both halves in turn would leave an x
       beside "Auction" with nothing under it to clear. */
    if (panel && !panel.querySelector('.nomination-pick')) {
        var clear = panel.querySelector('#nomination-clear');
        if (clear) clear.remove();
    }
});

/* ── Platform-wide player search ─────────────────────────────────────────
   Four ways the result list goes away, and none of them is a click handler on
   the rows.

   **Path 1 — `htmx:afterRequest`, gated on `successful`**: you acted on a hit.
   NOT because removing the row aborts anything. Measured 2026-09-10 in Chrome,
   removing a trigger element during `htmx:beforeSend` neither fires
   `htmx:abort` nor stops the swap, and the vendored source says why —
   `b.onload` calls the swap `M(n,I)` unconditionally before firing any event.
   (The `.nomination-pick` comment above and CLAUDE.md both asserted the
   opposite for a month; it was reasoned from the minified source, not run.)
   The real reason is the gate: a request that FAILED should leave the list up,
   and on click you do not yet know. htmx does re-fire `afterRequest` on the
   nearest surviving ancestor when a handler removes its own trigger (the
   `if(!se(n))` branch), so this must be idempotent — clearing an already-empty
   mount twice is.

   **Path 2 — `focusout` leaving the widget**: you went back to bidding. Works
   only because a clickable row is focusable, so mousedown moves focus INTO the
   widget and path 2 cannot fire before path 1 gets its chance.

   **Path 3 — Escape**, in the keydown handler above.

   **Path 4 — the suppression flag**, which is what makes the other three
   stick. The input debounces 200ms and also fires on `focus`, so at the moment
   a result is clicked there is routinely a /find-player response still coming;
   clearing the mount does not stop it, and the list reappeared a fraction of a
   second after being dismissed. Measured, reproducibly, by clicking a hit
   promptly after typing. Suppress the SWAP rather than trying to cancel the
   request: every close sets the flag, and the next `input` or `focus` on the
   box clears it — those two events being the only things that can legitimately
   want the list back.

   None of the four clears on a plain successful request from elsewhere:
   /explain auto-fires on `load` from the bid panel, and gating on "any POST
   succeeded" would close the list mid-read. An open list can therefore go
   stale behind a pick — accepted, because every figure in it was live as of
   the keystroke that drew it and the next keystroke corrects it. */
var playerSearchSuppressed = false;

function closePlayerSearch() {
    playerSearchSuppressed = true;
    var mount = document.getElementById('player-search-results');
    if (mount) mount.innerHTML = '';
}

document.body.addEventListener('htmx:beforeSwap', function(e) {
    var target = e.detail.target;
    if (!target || target.id !== 'player-search-results') return;
    if (playerSearchSuppressed) e.detail.shouldSwap = false;
});

['input', 'focus'].forEach(function(kind) {
    document.addEventListener(kind, function(e) {
        var box = document.getElementById('player-search');
        if (box && box.contains(e.target)) playerSearchSuppressed = false;
    }, true);
});

document.body.addEventListener('htmx:afterRequest', function(e) {
    if (!e.detail.successful) return;
    if (!e.target.closest || !e.target.closest('#player-search-results')) return;
    closePlayerSearch();
    // The chart mount lives inside .auction-grid, which scrolls. Landing a
    // chart below the fold looks exactly like a click that did nothing.
    var path = e.detail.requestConfig ? e.detail.requestConfig.path : '';
    if (path.indexOf('/player-chart/') !== 0) return;
    var chart = document.getElementById('player-chart-container');
    if (chart) chart.scrollIntoView({block: 'nearest'});
});

document.addEventListener('focusout', function(e) {
    var box = document.getElementById('player-search');
    if (!box || !box.contains(e.target)) return;
    if (e.relatedTarget && box.contains(e.relatedTarget)) return;
    closePlayerSearch();
});

/* What a cell sorts on, falling back to an image's alt text.

   A column of bare <img> has no textContent, so every row produced the same
   key, the stable sort was a no-op and the header was inert while still
   looking clickable — measured 2026-08-15 at 0 of 705 rows in the Available
   Players NHL column. That is worse than no control: mid-draft you assume the
   sort took and read the wrong row.

   `alt` rather than `title` because alt is what the column already announces
   to a screen reader, so the sort order matches what the cell communicates.
   Note an EMPTY cell is different from an inert column and must stay empty:
   the RFA column is blank for the 683 non-RFA players and sorts correctly on
   the 22 that aren't — grouping them is the point of clicking it. */
function cellSortText(cell) {
    if (!cell) return '';
    var text = cell.textContent.trim();
    if (text) return text;
    var img = cell.querySelector('img[alt]');
    return img ? img.getAttribute('alt').trim() : '';
}

/* Sort table by clicking column headers */
function sortTable(th) {
    var table = th.closest('table');
    var tbody = table.querySelector('tbody');
    var rows = Array.from(tbody.querySelectorAll('tr'));
    var col = parseInt(th.dataset.sortCol);
    var type = th.dataset.sortType || 'text';
    var asc = th.dataset.sortDir !== 'asc';

    // Clear sort indicators from all th in this table
    table.querySelectorAll('th[data-sort-col]').forEach(function(h) {
        h.dataset.sortDir = '';
        h.classList.remove('sort-asc', 'sort-desc');
    });
    th.dataset.sortDir = asc ? 'asc' : 'desc';
    th.classList.add(asc ? 'sort-asc' : 'sort-desc');

    rows.sort(function(a, b) {
        var aText = cellSortText(a.cells[col]);
        var bText = cellSortText(b.cells[col]);
        var aVal, bVal;

        if (type === 'currency') {
            aVal = parseFloat(aText.replace(/[$M,+]/g, '')) || 0;
            bVal = parseFloat(bText.replace(/[$M,+]/g, '')) || 0;
        } else if (type === 'number') {
            aVal = parseFloat(aText) || 0;
            bVal = parseFloat(bText) || 0;
        } else {
            aVal = aText.toLowerCase();
            bVal = bText.toLowerCase();
        }

        if (aVal < bVal) return asc ? -1 : 1;
        if (aVal > bVal) return asc ? 1 : -1;
        return 0;
    });

    rows.forEach(function(row) { tbody.appendChild(row); });
    renumberRows(tbody);
}

/* Renumber the leading # cell of every visible row 1..N, for the Available
   Players table alone — which the tbody id below, not the column, is what
   decides. The team panel's roster table grew a `#` column of its own on
   2026-09-20 holding lineup slots (F1/D1/BF1, from main._roster_slots); those
   are labels rather than an ordinal sequence and renumbering them 1..N would
   destroy them. It is out of reach because its tbody carries no id, so do not
   relax the guard to "has a leading # cell".

   Identity, not containment. This read `tbody.closest('#bid-limits')` until
   2026-09-10, which the price-drivers table — mounted INSIDE #bid-limits —
   also satisfies. The way in is applyPlayerFilters, which passes whatever
   `#bid-limits tbody` resolved to: with a card open that was the drivers
   table, and every group LABEL got overwritten with 1,2,3. Note it is NOT
   reachable through sortTable, which hands over the tbody it just sorted —
   measured 2026-09-10, a browser test driven through the sort path passed
   against the unfixed build. There are two tables in that panel now. */
function renumberRows(tbody) {
    if (!tbody || tbody.id !== 'pool-rows') return;
    var i = 1;
    tbody.querySelectorAll('tr').forEach(function(row) {
        if (row.style.display === 'none') return;
        if (row.cells.length > 0) row.cells[0].textContent = i++;
    });
}

/* Add player to live bidding form (delegated to avoid inline JS with player names) */
document.addEventListener('click', function(e) {
    var btn = e.target.closest('.btn-add-bid');
    if (!btn) return;
    var name = btn.dataset.player;
    var bidForm = document.querySelector('.bid-form');
    if (!bidForm) {
        document.body.dispatchEvent(new CustomEvent('showToast', {
            detail: {
                type: 'warning',
                message: 'Finish the current auction before starting another.',
            },
        }));
        return;
    }
    var input = bidForm.querySelector('input[name="player"]');
    if (input) {
        input.value = name;
        input.focus();
    }
});

/* Available Players filters — position and RFA status, composed.

   Both selections funnel through applyPlayerFilters(), which is the ONLY thing
   that writes row.style.display. Two filters each writing it directly would
   fight: whichever ran last would win and silently discard the other, so
   picking F after RFA would quietly show non-RFA forwards.

   The state lives here rather than in the DOM because the DOM does not keep it.
   /assign returns all_panels.html into #app, which re-renders bid_limits.html
   from the template — inline styles gone, buttons back to All — so before
   2026-08-16 every filter was wiped by every sale. Over 150+ picks that is
   constant, and it hits the RFA filter hardest, since hunting the RFA half of
   a nomination turn is exactly what you are doing when picks land.

   Deliberately unlike the Logs tabs, which DO reset on a swap: a tab is a place
   you are looking, a filter is a search you are in the middle of. */
var playerFilters = {pos: 'all', rfa: 'all'};

var POS_LABELS = {F: 'forwards', D: 'defencemen', G: 'goalies'};

function applyPlayerFilters() {
    // #pool-rows, never '#bid-limits tbody': querySelector takes the FIRST
    // match in tree order, and an open price chart puts its drivers <tbody>
    // ahead of the pool's. That hid all 7 driver rows and left the pool
    // unfiltered — reported as "the filters don't work when the Price Model
    // window is open".
    var tbody = document.getElementById('pool-rows');
    if (!tbody) return;
    var visible = 0;
    tbody.querySelectorAll('tr').forEach(function(row) {
        // `is-rfa` is already on the row and already load-bearing for CSS (the
        // yellow left border, style.css). Reading it beats adding a data-rfa
        // attribute that restates the same fact in a second place.
        var isRfa = row.classList.contains('is-rfa');
        var okPos = playerFilters.pos === 'all' || row.dataset.position === playerFilters.pos;
        var okRfa = playerFilters.rfa === 'all'
            || (playerFilters.rfa === 'rfa' ? isRfa : !isRfa);
        row.style.display = (okPos && okRfa) ? '' : 'none';
        if (okPos && okRfa) visible++;
    });
    syncFilterButtons('data-pos', playerFilters.pos);
    syncFilterButtons('data-rfa', playerFilters.rfa);
    showPoolEmptyState(visible);
    // Last, and not optional: the # column is rendered 1..N by Jinja, so a
    // filtered table shows the original numbering with gaps until this runs.
    renumberRows(tbody);
}

/* Say so when a filter combination matches nothing.

   Reachable in a real draft — G + RFA, once the last restricted goalie sells —
   and until 2026-08-21 it rendered the headers and nothing else, which reads as
   a broken panel rather than an empty result. Nothing knew the table was empty:
   applyPlayerFilters() wrote display and counted nothing.

   Names the combination rather than saying "no matches". The filter buttons
   already show WHICH filters are on, so repeating that adds nothing; what the
   operator needs is the table confirming it agrees. */
function showPoolEmptyState(visible) {
    var row = document.getElementById('pool-no-matches');
    if (!row) return;
    row.style.display = visible ? 'none' : '';
    if (visible) return;
    var pos = playerFilters.pos === 'all' ? 'players' : POS_LABELS[playerFilters.pos];
    // Status BEFORE the noun: "No RFA defencemen left", not "No defencemen are
    // RFA left", which is what putting it after produced.
    var status = playerFilters.rfa === 'all' ? '' : playerFilters.rfa.toUpperCase() + ' ';
    row.cells[0].textContent = 'No ' + status + pos + ' left in the pool.';
}

function syncFilterButtons(attr, active) {
    document.querySelectorAll('[' + attr + ']').forEach(function(b) {
        var on = b.getAttribute(attr) === active;
        b.classList.toggle('btn-primary', on);
        b.classList.toggle('btn-outline', !on);
    });
}

function filterPosition(pos) {
    playerFilters.pos = pos;
    applyPlayerFilters();
}

function filterRfa(rfa) {
    playerFilters.rfa = rfa;
    applyPlayerFilters();
}

/* Re-apply the filters to a freshly swapped table.

   Two guards, both load-bearing. The early-out keeps the common case free —
   with no filter set there is nothing to restore, and 705 style writes per
   swap would be pure waste on the request path. The target check is because
   htmx:afterSwap fires for EVERY swap, including #bid-panel on each bid-check
   and #team-panel on each /team-view, neither of which touches this table.

   Keying on the swapped subtree is safe: bid_limits.html is included only by
   all_panels.html, and nothing in the app targets #bid-limits directly. It
   asks for #pool-rows rather than '#bid-limits tbody' because a price-chart
   swap lands inside #bid-limits too, and re-running the filters off the
   drivers table is exactly the 2026-09-10 bug. */
document.body.addEventListener('htmx:afterSwap', function(e) {
    if (playerFilters.pos === 'all' && playerFilters.rfa === 'all') return;
    var swapped = e.detail && e.detail.target;
    if (!swapped || !swapped.querySelector) return;
    if (!swapped.querySelector('#pool-rows')) return;
    applyPlayerFilters();
});

/* Keep the Assign button's price label in step with the live price input.
   The button posts #bid-price's value at submit time, so a label left at the
   last render's price would promise one number and record another. */
function syncAssignPrice() {
    var input = document.getElementById('bid-price');
    var label = document.getElementById('assign-price');
    if (!input || !label) return;
    var val = parseFloat(input.value);
    // An empty or unparseable box will 422 on submit, not assign at 0.5 —
    // leave the last good price up rather than advertise one that won't post.
    if (isNaN(val)) return;
    label.textContent = '$' + val.toFixed(1) + 'M';
}

/* Delegated off document: the auction panel is swapped on every bid-check, so
   a listener bound to the input itself dies on the first re-render. */
document.addEventListener('input', function(e) {
    if (e.target.id === 'bid-price') syncAssignPrice();
});

/* Adjust bid price by increment and auto-submit */
function adjustPrice(delta) {
    var input = document.getElementById('bid-price');
    if (!input) return;
    var val = parseFloat(input.value) || 0.5;
    input.value = Math.max(0.5, (val + delta)).toFixed(1);
    // Setting .value programmatically fires no input event, so sync by hand —
    // the label would otherwise stay stale until the re-render lands.
    syncAssignPrice();
    var form = input.closest('form');
    if (form) htmx.trigger(form, 'submit');
}

/* Toggle a bidder logo on/off and auto-resubmit the bid-check form so
   /bid-check re-renders advice + the conditional Assign block. */
function auctionTeamClick(btn) {
    btn.classList.toggle('active');
    var codes = [];
    document.querySelectorAll('#bidder-logos .bidder-logo-btn.active').forEach(function(b) {
        codes.push(b.dataset.team);
    });
    document.getElementById('bidders-hidden').value = codes.join(',');
    htmx.trigger(document.getElementById('bid-form'), 'submit');
}

/* "N selected · $X.XM" beneath a .choice-list.

   Half the answer to "the multi-select affordance is not obvious": a
   `<select multiple>` scrolls your picks out of sight, so you could not tell
   what you had chosen — or that a plain click had just discarded it. */
function updateTradeSummary(input) {
    /* FIRST, ahead of every early return below. Unticking the last box takes
       the `!picked.length` branch, and that is the most likely way a trade gets
       narrowed — a stale marker placed at the bottom fires on every change
       EXCEPT the one that empties a list. Caught by the browser test, not by
       reading. */
    markTradeEvalStale(input);
    var block = input.closest('.choice-block');
    if (!block) return;
    var out = block.querySelector('.choice-summary');
    if (!out) return;
    var picked = block.querySelectorAll('.choice-list input:checked');
    if (!picked.length) {
        out.textContent = 'None selected';
        return;
    }
    var total = 0;
    picked.forEach(function(el) { total += parseFloat(el.dataset.salary || 0); });
    out.textContent = picked.length + ' selected · $' + total.toFixed(1) + 'M';
}

/* A verdict stops describing the form the moment either list changes.

   Only reachable since 2026-09-10, and it is the cost of the fix that made it
   reachable: /trade-evaluate now swaps #trade-result alone, so the ticked boxes
   survive an evaluate — and /trade-execute posts the SERVER's last_trade_eval,
   not this form. Unticking a player and hitting Execute would therefore execute
   the trade you evaluated, not the one on screen, with nothing saying so.

   Scoped to #trade-panel because updateTradeSummary also serves the team
   panel's /trade-between form, which has no verdict beside it.

   The verdict is left readable rather than removed: the owner's words were "to
   modify the trade to recheck things", so comparing against the previous answer
   is the use case. Disabling the submit is what removes the hazard. */
function markTradeEvalStale(el) {
    var panel = el.closest && el.closest('#trade-panel');
    if (!panel) return;
    var verdict = panel.querySelector('.trade-verdict');
    if (!verdict || verdict.classList.contains('is-stale')) return;
    verdict.classList.add('is-stale');
    verdict.querySelectorAll('button[type="submit"]').forEach(function(b) {
        b.disabled = true;
    });
}

/* Fill a .choice-list with a team's roster as checkboxes.

   ONE builder for both fetched lists. The Trade Evaluator's "I Receive" and the
   Trade Between Teams form's "Receives" were two copies of this that differed
   only in the checkbox value and whether the label carried points — and the
   duplicated "(M)" suffix in them was a tracked finding, because deleting
   either copy left the suite green. Two copies of a label rule is how the two
   halves of one app come to describe the same player differently.

   opts.json: the Evaluator posts the whole player as JSON, because a received
   player is not on any roster of ours to look up by name. The Between form
   posts a bare name, which its own endpoint resolves against the partner's
   roster. */
function loadTradeChoices(teamCode, listId, opts) {
    var list = document.getElementById(listId);
    if (!list) return;
    if (!teamCode) {
        list.innerHTML = '<p class="text-xs opacity-60 p-1">Select a team first</p>';
        updateTradeSummaryFor(list);
        return;
    }
    fetch('/team-players/' + teamCode)
        .then(function(r) { return r.json(); })
        .then(function(players) {
            players.sort(function(a, b) { return b.projected_points - a.projected_points; });
            list.innerHTML = '';
            players.forEach(function(p) {
                var row = document.createElement('label');
                row.className = 'choice-row';
                var box = document.createElement('input');
                box.type = 'checkbox';
                box.className = 'checkbox checkbox-xs';
                box.name = opts.field;
                box.value = opts.json ? JSON.stringify(p) : p.name;
                box.dataset.salary = p.salary;
                box.addEventListener('change', function() { updateTradeSummary(box); });
                var text = document.createElement('span');
                /* "(M)" as everywhere else. /team-players returns all_players,
                   so minors arrive here whether or not this label says so — and
                   an unmarked one misdescribes the trade, because a group A-E
                   minor costs the receiving team his full salary the moment he
                   lands on the active roster. */
                text.textContent = p.name + ' (' + p.position + ', $' + p.salary.toFixed(1) + 'M'
                    + (opts.withPoints ? ', ' + p.projected_points + 'pts' : '') + ')'
                    + (p.is_minor ? ' (M)' : '');
                row.appendChild(box);
                row.appendChild(text);
                list.appendChild(row);
            });
            updateTradeSummaryFor(list);
        });
}

/* Reset a list's summary when its contents are replaced wholesale — the
   checkboxes that were counted no longer exist, so the old count would be a
   claim about players who are not on screen. */
function updateTradeSummaryFor(list) {
    markTradeEvalStale(list);
    var block = list.closest('.choice-block');
    var out = block && block.querySelector('.choice-summary');
    if (out) out.textContent = 'None selected';
}


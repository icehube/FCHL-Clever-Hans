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

   On afterRequest, not on click: htmx aborts an in-flight request whose
   triggering element is removed from the DOM, so removing the block on click
   would cancel the very /bid-check it is meant to accompany. Gated on
   `successful` so a failed request leaves the recommendation on screen rather
   than silently discarding it — /nominate is the only way back. */
document.body.addEventListener('htmx:afterRequest', function(e) {
    if (!e.detail.successful) return;
    var pick = e.target.closest && e.target.closest('.nomination-pick');
    if (pick) pick.remove();
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

/* Renumber the leading # cell of every visible row 1..N. Only the Available
   Players table has this column, so other tables are skipped. */
function renumberRows(tbody) {
    if (!tbody || !tbody.closest('#bid-limits')) return;
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

function applyPlayerFilters() {
    var tbody = document.querySelector('#bid-limits tbody');
    if (!tbody) return;
    tbody.querySelectorAll('tr').forEach(function(row) {
        // `is-rfa` is already on the row and already load-bearing for CSS (the
        // yellow left border, style.css). Reading it beats adding a data-rfa
        // attribute that restates the same fact in a second place.
        var isRfa = row.classList.contains('is-rfa');
        var okPos = playerFilters.pos === 'all' || row.dataset.position === playerFilters.pos;
        var okRfa = playerFilters.rfa === 'all'
            || (playerFilters.rfa === 'rfa' ? isRfa : !isRfa);
        row.style.display = (okPos && okRfa) ? '' : 'none';
    });
    syncFilterButtons('data-pos', playerFilters.pos);
    syncFilterButtons('data-rfa', playerFilters.rfa);
    // Last, and not optional: the # column is rendered 1..N by Jinja, so a
    // filtered table shows the original numbering with gaps until this runs.
    renumberRows(tbody);
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
   all_panels.html, and nothing in the app targets #bid-limits directly. */
document.body.addEventListener('htmx:afterSwap', function(e) {
    if (playerFilters.pos === 'all' && playerFilters.rfa === 'all') return;
    var swapped = e.detail && e.detail.target;
    if (!swapped || !swapped.querySelector) return;
    if (!swapped.querySelector('#bid-limits tbody')) return;
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
    var block = list.closest('.choice-block');
    var out = block && block.querySelector('.choice-summary');
    if (out) out.textContent = 'None selected';
}


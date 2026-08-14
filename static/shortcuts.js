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
        var aText = a.cells[col].textContent.trim();
        var bText = b.cells[col].textContent.trim();
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

/* Filter available players by position */
function filterPosition(pos) {
    var tbody = document.querySelector('#bid-limits tbody');
    if (!tbody) return;
    tbody.querySelectorAll('tr').forEach(function(row) {
        row.style.display = (pos === 'all' || row.dataset.position === pos) ? '' : 'none';
    });
    document.querySelectorAll('[data-pos]').forEach(function(b) {
        b.classList.remove('btn-primary');
        b.classList.add('btn-outline');
    });
    var active = document.querySelector('[data-pos="' + pos + '"]');
    if (active) {
        active.classList.add('btn-primary');
        active.classList.remove('btn-outline');
    }
    renumberRows(tbody);
}

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

/* Sync a multi-select's selected values into a hidden CSV input
   (used by the Trade Between Teams form). */
function updateTradeHidden(sel, hiddenId) {
    var vals = [];
    for (var i = 0; i < sel.options.length; i++) {
        if (sel.options[i].selected) vals.push(sel.options[i].value);
    }
    document.getElementById(hiddenId).value = vals.join(',');
}

/* Load the partner team's roster into the trade-receive multi-select. */
function loadTradePartner(teamCode, viewTeamCode) {
    if (!teamCode) return;
    fetch('/team-players/' + teamCode)
        .then(function(r) { return r.json(); })
        .then(function(players) {
            var sel = document.getElementById('trade-partner-players-' + viewTeamCode);
            sel.innerHTML = '';
            players.forEach(function(p) {
                var opt = document.createElement('option');
                opt.value = p.name;
                /* "(M)" as everywhere else. /team-players returns all_players,
                   so minors arrive here whether or not this label says so —
                   and an unmarked one misdescribes the trade, because a group
                   A-E minor costs the receiving team his full salary the
                   moment he lands on the active roster. */
                opt.textContent = p.name + ' (' + p.position + ', $' + p.salary.toFixed(1) + 'M)'
                    + (p.is_minor ? ' (M)' : '');
                sel.appendChild(opt);
            });
        });
}


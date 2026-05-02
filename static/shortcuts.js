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
    // Ctrl+Z: Undo
    if (e.ctrlKey && e.key === 'z') {
        e.preventDefault();
        htmx.ajax('POST', '/undo', {target: '#app', swap: 'innerHTML'});
    }

    // Ctrl+N: Nominate
    if (e.ctrlKey && e.key === 'n') {
        e.preventDefault();
        htmx.ajax('GET', '/nominate', {target: '#auction-control', swap: 'outerHTML'});
    }
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

/* Adjust bid price by increment and auto-submit */
function adjustPrice(delta) {
    var input = document.getElementById('bid-price');
    if (!input) return;
    var val = parseFloat(input.value) || 0.5;
    input.value = Math.max(0.5, (val + delta)).toFixed(1);
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
                opt.textContent = p.name + ' (' + p.position + ', $' + p.salary.toFixed(1) + 'M)';
                sel.appendChild(opt);
            });
        });
}


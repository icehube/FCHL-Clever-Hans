/* Keyboard shortcuts for auction day */

document.addEventListener('keydown', function(e) {
    // Ctrl+Z: Undo
    if (e.ctrlKey && e.key === 'z') {
        e.preventDefault();
        htmx.ajax('POST', '/undo', {target: '#app', swap: 'innerHTML'});
    }

    // Ctrl+S: Save
    if (e.ctrlKey && e.key === 's') {
        e.preventDefault();
        htmx.ajax('POST', '/save', {swap: 'none'});
    }

    // Ctrl+N: Nominate
    if (e.ctrlKey && e.key === 'n') {
        e.preventDefault();
        htmx.ajax('GET', '/nominate', {target: '#nomination', swap: 'outerHTML'});
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
}

/* Add player to live bidding form */
function setBidPlayer(name) {
    var bidForm = document.querySelector('.bid-form');
    if (bidForm) {
        var input = bidForm.querySelector('input[name="player"]');
        if (input) {
            input.value = name;
            input.focus();
        }
    }
}

/* Select team for assign form (single-select logo grid) */
function selectAssignTeam(btn) {
    btn.closest('.assign-logos').querySelectorAll('.bidder-logo-btn').forEach(function(b) {
        b.classList.remove('active');
    });
    btn.classList.add('active');
    document.getElementById('assign-team-hidden').value = btn.dataset.team;
}

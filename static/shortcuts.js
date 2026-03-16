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

/* Persist each administrative list's ordering without touching financial actions. */
(() => {
    'use strict';
    const script = document.currentScript;
    const table = document.querySelector('[data-list-sort-table]');
    if (!table) return;
    const allowed = new Set(Array.from(table.querySelectorAll('[data-list-sort]'), a => a.dataset.listSort));
    allowed.add(table.dataset.defaultSort || 'id');
    const sortParam = script.dataset.sortParam || 'list_sort';
    const dirParam = script.dataset.dirParam || 'list_dir';
    const storageKey = 'max-list-sort-v1:' + script.dataset.owner + ':' + table.dataset.listSortTable;
    const url = new URL(location.href);
    let state = {key: table.dataset.sortKey, direction: table.dataset.sortDir};
    const valid = s => s && allowed.has(s.key) && ['asc','desc'].includes(s.direction);
    try {
        if (!url.searchParams.has(sortParam) && !url.searchParams.has(dirParam)) {
            const saved = JSON.parse(localStorage.getItem(storageKey));
            if (valid(saved) && (saved.key !== state.key || saved.direction !== state.direction)) {
                url.searchParams.set(sortParam, saved.key);
                url.searchParams.set(dirParam, saved.direction);
                url.searchParams.set('page', '1');
                location.replace(url.href);
                return;
            }
        }
        if (valid(state)) localStorage.setItem(storageKey, JSON.stringify(state));
    } catch (_) { /* URL ordering remains available when storage is blocked. */ }
    // Keep the ordering through filters, status pills and server pagination.
    document.querySelectorAll('a[href]').forEach(a => {
        const target = new URL(a.href, location.href);
        if (target.origin === location.origin && target.pathname === location.pathname && !target.hash && !a.dataset.listSort) {
            target.searchParams.set(sortParam, state.key);
            target.searchParams.set(dirParam, state.direction);
            a.href = target.href;
        }
    });
    document.querySelectorAll('form').forEach(form => {
        if ((form.method || 'get').toLowerCase() !== 'get') return;
        const target = new URL(form.action || location.href, location.href);
        if (target.pathname !== location.pathname || target.origin !== location.origin) return;
        [[sortParam,state.key],[dirParam,state.direction]].forEach(([name,value]) => {
            let input = form.querySelector('input[name="' + name + '"]');
            if (!input) { input = document.createElement('input'); input.type = 'hidden'; input.name = name; form.append(input); }
            input.value = value;
        });
    });
})();

// Execute the template's actual UI handlers with local DOM/fetch stubs only.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2] || path.join(__dirname, '../web/templates/database_maintenance.html'), 'utf8');
const start = source.indexOf('let cascadeDeleteBusy = false;');
const end = source.indexOf('function executeCascadeDelete(', start);
assert(start >= 0 && end > start);
const handlers = source.slice(start, end);
const cancelled = {job_id: 'fixture', state: 'cancelled', parameters: {cancel_requested: 1}, result: {
    total: 120, processed: 20, deleted_vouchers: 20, remaining: 100, message: 'أُلغيت العملية؛ حُذف 20 كرتًا.'
}};
function fixture(job = cancelled) {
    const elements = new Map(), timers = new Map(), alerts = [], stored = new Map();
    let response = job, loaded, refreshes = 0, timerId = 0;
    const element = id => {
        if (!elements.has(id)) elements.set(id, {hidden: true, disabled: false, style: {},
            setAttribute(name, value) { this[name] = value; }});
        return elements.get(id);
    };
    const context = vm.createContext({document: {getElementById: element,
        addEventListener(event, fn) { assert.equal(event, 'DOMContentLoaded'); loaded = fn; }},
        fetch: async () => ({ok: true, json: async () => ({success: true, job: response})}),
        clearTimeout: id => timers.delete(id),
        setTimeout: (fn, delay) => { timers.set(++timerId, {fn, delay}); return timerId; },
        sessionStorage: {getItem: key => stored.get(key), setItem: (key, value) => stored.set(key, value)},
        Swal: {fire: data => alerts.push(data)}, refreshStats: () => refreshes++});
    vm.runInContext(handlers, context);
    return {context, element, timers, alerts, load: () => loaded(), respond: job => { response = job; },
        refreshes: () => refreshes, busy: () => vm.runInContext('cascadeDeleteBusy', context)};
}
const flush = async () => { for (let i = 0; i < 4; i++) await new Promise(setImmediate); };
(async () => {
    const f = fixture();
    for (const state of ['queued', 'running']) {
        assert.equal(f.context.showCleanupProgress({...cancelled, state, parameters: {}}), true);
        assert.equal(f.element('cleanupJobProgress').hidden, false);
        assert.equal(f.busy(), true);
    }
    f.context.showCleanupProgress({...cancelled, state: 'running'});
    assert.equal(f.element('cleanupJobProgress').hidden, false, 'Cancellation request must wait for worker acknowledgement');
    assert.equal(f.element('cleanupJobCancel').disabled, true);
    assert.equal(f.element('cleanupJobState').textContent, 'جارٍ الإلغاء');
    for (const state of ['cancelled', 'completed', 'partial', 'failed', 'interrupted']) {
        f.context.showCleanupProgress({...cancelled, state: 'running'});
        assert.equal(f.context.showCleanupProgress({...cancelled, state}), false);
        assert.equal(f.element('cleanupJobProgress').hidden, true);
        assert.equal(f.busy(), false);
    }
    const poll = fixture();
    poll.context.showCleanupProgress({...cancelled, state: 'running'});
    poll.context.pollCleanupJob(); await flush();
    assert.equal(poll.element('cleanupJobProgress').hidden, true);
    assert.equal(poll.timers.size, 0);
    assert.equal(poll.refreshes(), 1);
    assert.equal(poll.alerts.length, 1);
    assert.equal(poll.alerts[0].icon, 'info');
    assert.equal(poll.alerts[0].text, cancelled.result.message);
    poll.context.pollCleanupJob(); await flush();
    assert.equal(poll.alerts.length, 1, 'Completion notice is not repeated');
    for (let reload = 0; reload < 3; reload++) {
        const page = fixture(); page.load(); await flush();
        assert.equal(page.element('cleanupJobProgress').hidden, true, 'Historical cancelled job must stay hidden on reload');
        assert.equal(page.timers.size, 0);
        assert.equal(page.alerts.length, 0);
    }
    const active = fixture({...cancelled, state: 'running'}); active.load(); await flush();
    assert.equal(active.element('cleanupJobProgress').hidden, false);
    assert.equal(active.timers.size, 1);
    assert.equal([...active.timers.values()][0].delay, 1500);
    console.log(JSON.stringify({accepted: true, active_states_visible: 2, terminal_states_hidden: 5,
        cancellation_waits_for_ack: true, notification_preserved_once: true, reloads_hidden: 3,
        active_reload_keeps_polling: true, real_api_calls: 0}));
})().catch(err => { console.error(err); process.exitCode = 1; });

'use strict';
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../web/templates/base.html'), 'utf8');
const code = html.match(/<script>\s*([\s\S]*?)<\/script>/)[1];
const calls = [];
const window = {
    location: {href: 'https://fixture.invalid/dashboard', origin: 'https://fixture.invalid'},
    fetch: (input, options) => calls.push({input, options})
};
vm.runInNewContext(code, {window, URL, Request, Headers,
    document: {querySelector: () => ({content: 'fixture-csrf'})}});
for (const route of ['/api/tools/nas-diagnostics/test-coa-port',
    '/api/tools/nas-diagnostics/send-coa-disconnect',
    '/api/automation-rules/toggle', '/api/automation-rules/run',
    '/api/backups/create', '/api/system-services/action', '/api/whatsapp/settings/save']) {
    window.fetch(route, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
    const options = calls.at(-1).options;
    assert.equal(options.headers.get('X-CSRF-Token'), 'fixture-csrf');
    assert.equal(options.headers.get('Content-Type'), 'application/json');
    assert.equal(options.body, '{}');
}
window.fetch('https://external.invalid/api/automation-rules/run', {method: 'POST'});
assert.equal(calls.at(-1).options.headers, undefined);
window.fetch('/api/automation-rules/run', {method: 'GET'});
assert.equal(calls.at(-1).options.headers, undefined);
for (const method of ['PUT', 'PATCH', 'DELETE']) {
    window.fetch('/api/future-admin-route', {method});
    assert.equal(calls.at(-1).options.headers.get('X-CSRF-Token'), 'fixture-csrf');
}
console.log('PASS: administrative writes retain payloads; no token on GET or external origin');

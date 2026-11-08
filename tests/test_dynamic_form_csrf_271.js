// Isolated DOM contracts for programmatic form submissions; no network or app.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../web/templates/base.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
class Form {
    constructor(method = 'post', action = '/local-action') {
        this.method = method; this.action = action; this.attributes = {method, action}; this.children = []; this.submits = 0;
    }
    getAttribute(name) { return this.attributes[name] ?? null; }
    querySelector() { return this.children.find(e => e.name === 'csrf_token') || null; }
    appendChild(e) { this.children.push(e); }
    submit() { this.submits += 1; }
}
const listeners = {};
const document = {
    querySelector: () => ({content: 'LOCAL-CSRF-TOKEN'}),
    createElement: () => ({}),
    addEventListener: (name, callback, capture) => { assert.equal(capture, true); listeners[name] = callback; },
};
const window = {location: {origin: 'https://local.invalid', href: 'https://local.invalid/nas'}, fetch: () => {}};
vm.runInNewContext(script, {window, document, HTMLFormElement: Form, Request, Headers, URL});
let checked = 0;
if (process.argv.includes('--extended-only')) {
    const clobbered = new Form('POST', 'https://other.invalid/action');
    clobbered.action = '/local-looking-field'; clobbered.method = {name: 'method'};
    clobbered.submit(); assert.equal(clobbered.children.length, 0); checked++;
    const localClobbered = new Form(); localClobbered.action = {name: 'action'};
    localClobbered.method = {name: 'method'}; localClobbered.querySelector = {};
    localClobbered.appendChild = {}; localClobbered.submit();
    assert.equal(localClobbered.children[0].value, 'LOCAL-CSRF-TOKEN'); checked++;
    const externalButton = new Form();
    listeners.submit({target: externalButton, submitter: {getAttribute: name => name === 'formaction' ? 'https://other.invalid/action' : null}});
    assert.equal(externalButton.children.length, 0); checked++;
    const getButton = new Form();
    listeners.submit({target: getButton, submitter: {getAttribute: name => name === 'formmethod' ? 'get' : null}});
    assert.equal(getButton.children.length, 0); checked++;
    console.log(JSON.stringify({passed: checked, actual_network: false, scope: 'new named-control and submitter-origin cases only'}));
    process.exit(0);
}
const dynamic = new Form(); dynamic.submit();
assert.equal(dynamic.children.length, 1); assert.equal(dynamic.children[0].type, 'hidden');
assert.equal(dynamic.children[0].value, 'LOCAL-CSRF-TOKEN'); assert.equal(dynamic.submits, 1); checked++;
dynamic.children[0].value = 'stale'; dynamic.submit();
assert.equal(dynamic.children.length, 1); assert.equal(dynamic.children[0].value, 'LOCAL-CSRF-TOKEN'); checked++;
for (const action of ['https://other.invalid/action', '//other.invalid/action', 'javascript:void(0)']) {
    const foreign = new Form('POST', action); foreign.submit();
    assert.equal(foreign.children.length, 0); assert.equal(foreign.submits, 1); checked++;
}
const get = new Form('get'); get.submit(); assert.equal(get.children.length, 0); checked++;
const ordinary = new Form(); listeners.submit({target: ordinary});
assert.equal(ordinary.children[0].value, 'LOCAL-CSRF-TOKEN'); checked++;
listeners.submit({target: {}}); checked++;
console.log(JSON.stringify({passed: checked, actual_network: false, scope: 'new form-token behavior only'}));

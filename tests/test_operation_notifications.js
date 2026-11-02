/* Actual bridge/helper and page function; fake clock/transport, no server actions. */
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const helper = fs.readFileSync('web/static/operation-notifications.js', 'utf8');
function fixture() {
    let now = 0, serial = 0;
    const timers = new Map(), notices = [], confirmations = [];
    const later = (fn, ms) => { const id = ++serial; timers.set(id, {fn, at:now + ms}); return id; };
    function advance(ms) {
        const end = now + ms;
        while (true) {
            const next = [...timers].filter(([,t]) => t.at <= end).sort((a,b) => a[1].at-b[1].at)[0];
            if (!next) break;
            timers.delete(next[0]); now = next[1].at; next[1].fn();
        }
        now = end;
    }
    const original = async opts => { confirmations.push(opts); return {isConfirmed:true}; };
    const ctx = {window:{Swal:{fire:original, close(){}, showLoading(){}}}, Promise,
        setTimeout:later, clearTimeout:id=>timers.delete(id), sessionStorage:{setItem(){}}};
    ctx.Swal = ctx.window.Swal;
    ctx.window.showToastNotification = opts => {
        const notice = {opts, closed:false, close(){ this.closed=true; }};
        notices.push(notice);
        if (opts.duration > 0) later(()=>notice.close(), opts.duration);
        return notice;
    };
    vm.createContext(ctx);
    const source = fs.readFileSync('web/templates/base.html','utf8');
    const bridge = source.slice(source.indexOf('        // Bridge SweetAlert2 seamlessly'), source.indexOf('        // Global Toast Utility Shortcuts'));
    vm.runInContext(bridge, ctx);
    vm.runInContext(helper, ctx);
    return {ctx, notices, confirmations, advance, operation:()=>ctx.window.createOperationNotification()};
}
const loading = title => ({title, didOpen:()=>Swal.showLoading()});
const flush = async () => { for(let i=0;i<12;i++) await Promise.resolve(); };
async function run() {
    const templates = ['subscribers.html','active_card_users.html','active_card_user_details.html',
        'subscriber_details.html','card_inspect.html','backups.html','database_maintenance.html',
        'tools/accounting_archiver.html','system_services.html','autoheal.html','nas.html',
        'nas_details.html','notifications_center.html','packages.html','automation_rules.html','vouchers.html'];
    let scoped = 0;
    for (const file of templates) {
        const source=fs.readFileSync('web/templates/'+file,'utf8');
        const functions=/^(?<indent>[ \t]*)(?:(?:async\s+)?function\s+\w+\([^\n]*\)|window\.\w+\s*=\s*function\([^\n]*\))\s*\{/gm;
        for (const match of source.matchAll(functions)) {
            const tail=source.slice(match.index+match[0].length);
            const end=new RegExp('^'+match.groups.indent+'};?\\s*$','m').exec(tail);
            if (!end) continue;
            const body=match[0]+tail.slice(0,end.index+end[0].length);
            if (!body.includes('Swal.showLoading(')) continue;
            assert.equal((body.match(/const operationSwal = window.createOperationNotification\(\);/g)||[]).length,1,file);
            assert(!body.replaceAll('operationSwal.fire(', '').includes('Swal.fire('),file);
            assert(!body.replaceAll('operationSwal.close(', '').includes('Swal.close('),file);
            new vm.Script(body.replace(/{{[\s\S]*?}}/g,'0').replace(/{%[\s\S]*?%}/g,''));
            scoped++;
        }
    }
    assert.equal(scoped,51);
    const watcher=fs.readFileSync('web/static/session-operations.js','utf8');
    assert(watcher.includes('const operationSwal = window.createOperationNotification();'));
    assert(!watcher.replaceAll('operationSwal.fire(', '').includes('Swal.fire('));
    const f = fixture(), a=f.operation(), b=f.operation();
    a.fire(loading('A')); b.fire(loading('B'));
    assert.equal(f.notices.length,2);
    for (const n of f.notices) { assert.equal(n.opts.duration,0); assert.equal(n.opts.dismissible,false); }
    f.advance(100000); assert(f.notices.every(n=>!n.closed));
    f.ctx.Swal.fire('Unrelated','message','info'); f.ctx.Swal.close();
    assert(!f.notices[0].closed && !f.notices[1].closed);
    a.fire('Completed','<img src=x onerror=bad()>','success');
    assert(f.notices[0].closed); assert(!f.notices[1].closed);
    assert(f.notices.at(-1).opts.html.includes('&lt;img'));
    f.advance(6000); assert(f.notices.at(-1).closed); assert(!f.notices[1].closed);
    b.uncertain('Transport','Connection lost','error');
    assert(f.notices[1].closed); assert.equal(f.notices.at(-1).opts.icon,'warning');
    assert(f.notices.at(-1).opts.html.includes('قد تستمر العملية'));
    const c=f.operation(); c.fire(loading('C'));
    c.fire({title:'Confirmation',showCancelButton:true});
    assert(!f.notices.at(-1).closed); assert.equal(f.confirmations.length,1);
    let followed=0; c.follow(()=>followed++); assert.equal(followed,1); assert(f.notices.at(-1).closed);
    const d=f.operation(); d.fire(loading('D')); d.fire({icon:'error',title:'Rejected',text:'Server rejected'});
    assert.equal(f.notices.at(-1).opts.icon,'error');
    // DOM result bodies remain HTML, not [object HTMLDivElement].
    d.fire({html:{nodeType:1,outerHTML:'<p>safe constructed result</p>'},icon:'info'});
    assert.equal(f.notices.at(-1).opts.html,'<p>safe constructed result</p>');

    // Two real createBackup handlers finish out of order, with no request replay.
    const g=fixture(), requests=[]; let reloads=0;
    g.ctx.fetch=(url,opts)=>new Promise((resolve,reject)=>requests.push({url,opts,resolve,reject}));
    g.ctx.location={reload(){reloads++;}};
    const page=fs.readFileSync('web/templates/backups.html','utf8');
    const body=page.match(/^    function createBackup\(\) \{[\s\S]*?^    }/m)[0];
    vm.runInContext(body,g.ctx);
    g.ctx.createBackup(); g.ctx.createBackup(); await flush();
    assert.equal(requests.length,2); assert.equal(g.notices.length,2);
    assert(requests.every(r=>r.url==='/api/backups/create' && r.opts.method==='POST'));
    g.advance(20000); assert(g.notices.every(n=>!n.closed));
    requests[1].resolve({json:async()=>({success:false,message:'Synthetic rejection'})}); await flush();
    assert(!g.notices[0].closed); assert(g.notices[1].closed);
    requests[0].reject(new Error('Synthetic lost connection')); await flush();
    assert(g.notices[0].closed); assert.equal(g.notices.at(-1).opts.icon,'warning');
    assert.equal(requests.length,2); assert.equal(reloads,0);
    g.ctx.createBackup(); await flush();
    requests[2].resolve({json:async()=>({success:true,message:'Synthetic completed'})}); await flush();
    assert.equal(g.notices.at(-1).opts.icon,'success'); assert.equal(reloads,0);
    g.advance(1300); await flush(); assert.equal(reloads,1);
    // Reload from one finished request must wait for a second request AND its result.
    const h=fixture();let navigations=0,firstResolved=false;
    h.ctx.window.location={reload(){navigations++;}};
    const first=h.operation(),second=h.operation();
    first.fire(loading('First'));second.fire(loading('Second'));
    first.fire('Done','first result','success').then(()=>{firstResolved=true;});
    h.advance(1300);await flush();assert.equal(firstResolved,false);
    first.reloadWhenIdle(1200);h.advance(1200);await flush();assert.equal(navigations,0);
    second.fire('Rejected','second result','error');
    h.advance(1200);await flush();assert.equal(navigations,0);
    h.advance(100);await flush();assert.equal(firstResolved,true);assert.equal(navigations,1);
    console.log('PASS: 52 bound waiting sites and syntax; slow/concurrent/out-of-order operations, unrelated toast/close, own terminal result, uncertainty, confirmation, follow-up, HTML/text, real backup callbacks and no replay');
}
run().catch(e=>{console.error(e);process.exitCode=1;});

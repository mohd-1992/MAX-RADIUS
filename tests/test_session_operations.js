/* Isolated browser contracts: no app server, SQL or NAS. */
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
async function run(fail=false){
    const values=new Map(),calls=new Map(),dialogs=[];let active=0,peak=0;
    const ids=Array.from({length:10},(_,i)=>i.toString(16).padStart(32,'0'));
    const ctx={window:{showToastNotification:()=>({close(){}})},document:{addEventListener(){}},crypto:{randomUUID:()=> 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'},
        sessionStorage:{setItem:(k,v)=>values.set(k,v),getItem:k=>values.get(k),removeItem:k=>values.delete(k)},
        Swal:{showLoading(){},fire:async data=>{dialogs.push(data);}},Date,Promise,Map,Set,encodeURIComponent,
        setTimeout:fn=>setImmediate(fn),fetch:async(url,opts)=>{
            assert.equal(opts.cache,'no-store');assert.equal(opts.method,undefined);
            const id=url.split('/').pop(),n=(calls.get(id)||0)+1;calls.set(id,n);active++;peak=Math.max(peak,active);
            await new Promise(r=>setImmediate(r));active--;
            return {ok:!fail,status:fail?403:200,json:async()=>fail?{success:false,message:'Forbidden'}:
                {success:true,job_id:id,pending:id===ids[0]&&n===1,state:id===ids[0]&&n===1?'running':id===ids[1]?'partial':'completed',
                 confirmed:1,preserved:0,unknown:id===ids[1]?1:0,message:'Synthetic'}};
        }};
    vm.createContext(ctx);ctx.window.Swal=ctx.Swal;
    vm.runInContext(fs.readFileSync('web/static/operation-notifications.js','utf8'),ctx);
    vm.runInContext(fs.readFileSync('web/static/session-operations.js','utf8'),ctx);
    await ctx.window.watchSessionOperations({jobs:ids.map(job_id=>({job_id})),message:'Synthetic'});
    assert(peak<=4);assert.equal(values.has('max.pendingSessions'),false);
    assert.equal(dialogs.at(-1).icon,'warning');
    if(!fail){assert.equal(calls.get(ids[0]),2);for(const id of ids.slice(1))assert.equal(calls.get(id),1);}
    else assert(!(dialogs.at(-1).text || dialogs.at(-1).html).includes('الطلب محفوظ'));
}
(async()=>{await run();await run(true);console.log('2 browser behavior scenarios passed: bounded polling, no completed reread, partial and denied results');})().catch(e=>{console.error(e);process.exitCode=1;});

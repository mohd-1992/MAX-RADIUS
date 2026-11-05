// Real inline handlers, isolated DOM/network stubs; no application/database.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../web/templates/database_maintenance.html'),'utf8');
const start=html.indexOf('let archivePlanSequence = 0;'),end=html.indexOf('// 3. Confirm & Execute Historical Log Pruning',start);
assert(start>=0 && end>start);const source=html.slice(start,end);
const terminal={job_id:'one',state:'cancelled',parameters:{cancel_requested:1},result:{processed:2,total:10,message:'ألغيت بعد دفعتين',archived_sessions:2}};
function fixture(customFetch) {
    const elements=new Map(),timers=new Map(),alerts=[],storage=new Map();let loaded,refreshes=0;
    const element=id=>{
        if (!elements.has(id)) elements.set(id,{hidden:true,disabled:false,value:id==='archiveDaysSelect'?'90':id==='archiveModeSelect'?'archive':'',style:{},addEventListener(){},setAttribute(k,v){this[k]=v;}});
        return elements.get(id);
    };
    const fetch=customFetch|| (async url=>({ok:true,json:async()=>url.includes('?')?{success:true,plan:{count:91}}:{success:true,job:terminal}}));
    const context=vm.createContext({document:{getElementById:element,addEventListener:(name,fn)=>{loaded=fn;}},fetch,URLSearchParams,
        clearTimeout:id=>timers.delete(id),setTimeout:(fn,delay)=>{const id=Symbol();timers.set(id,{fn,delay});return id;},
        sessionStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},
        refreshStats:()=>refreshes++,Swal:{fire:opts=>alerts.push(opts)},
        window:{createOperationNotification:()=>({fire:async()=>({isConfirmed:true}),close(){},uncertain(){}})}});
    vm.runInContext(source,context);return {context,element,timers,alerts,load:()=>loaded(),refreshes:()=>refreshes};
}
const flush=async()=>{for(let i=0;i<5;i++)await new Promise(setImmediate);};
const response=body=>({ok:true,json:async()=>body});
(async()=>{
    const requests=[],race=fixture(url=>new Promise(resolve=>requests.push({url,resolve})));
    const old=race.context.refreshArchiveEligibility();race.element('archiveDaysSelect').value='30';
    const current=race.context.refreshArchiveEligibility();
    requests[1].resolve(response({success:true,plan:{count:1677}}));await current;
    requests[0].resolve(response({success:true,plan:{count:91}}));await old;
    assert.equal(race.element('arch_older_90d').textContent,Number(1677).toLocaleString('ar'));
    assert(race.element('archiveEligibilityTitle').textContent.includes('30'));
    const failed=fixture(async()=>({ok:false,json:async()=>({success:false,message:'unavailable'})}));
    await failed.context.refreshArchiveEligibility();assert.equal(failed.element('arch_older_90d').textContent,'—');
    for(const state of ['queued','running']){
        const page=fixture();assert.equal(page.context.showArchiveProgress({...terminal,state}),true);
        assert.equal(page.element('archiveJobProgress').hidden,false);
        assert.equal(page.element('archiveJobCancel').disabled,true);
    }
    for(const state of ['completed','cancelled','partial','failed','interrupted']){
        const page=fixture();page.context.showArchiveProgress({...terminal,state:'running'});
        assert.equal(page.context.showArchiveProgress({...terminal,state}),false);
        assert.equal(page.element('archiveJobProgress').hidden,true);
        assert.equal(page.element('btnStartArchiving').disabled,false);
    }
    for(let i=0;i<3;i++){
        const page=fixture();page.load();await flush();
        assert.equal(page.element('archiveJobProgress').hidden,true);
        assert.equal(page.timers.size,0);assert.equal(page.alerts.length,0);
    }
    const poll=fixture();poll.context.showArchiveProgress({...terminal,state:'running'});
    await poll.context.pollArchiveJob();await flush();await poll.context.pollArchiveJob();await flush();
    assert.equal(poll.alerts.length,1);assert.equal(poll.alerts[0].text,terminal.result.message);
    assert.equal(poll.timers.size,0);
    let posts=0;
    const unknown=fixture(async(url,options)=>{
        if(options?.method==='POST'){posts++;throw new Error('lost response');}
        return response({success:true,job:null});
    });
    unknown.context.triggerArchivingFromMaintenance();await flush();
    assert.equal(posts,1);assert.equal(unknown.element('btnStartArchiving').disabled,false);
    console.log(JSON.stringify({accepted:true,age_race_guard:true,unknown_not_zero:true,active_states:2,terminal_states:5,
        reloads_hidden:3,result_notice_once:true,uncertain_start_not_replayed:true,real_api_calls:0}));
})().catch(error=>{console.error(error);process.exitCode=1;});

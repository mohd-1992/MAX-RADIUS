/* Isolated browser state fixture: no network or browser dependency. */
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync('web/static/userman-progress.js', 'utf8');
async function scenario(state) {
    const id='a'.repeat(32), calls=[], nodes=new Map();
    let saved=JSON.stringify({id,kind:'import'}), onReady;
    const document={addEventListener:(_,cb)=>{onReady=cb;},querySelector:()=>({content:'fixture-csrf'}),
        getElementById:key=>{if(!nodes.has(key))nodes.set(key,{removeAttribute(){}});return nodes.get(key);}};
    const Swal={fire:async value=>value.title==='متابعة عملية سابقة'?{isDenied:true}:{},close(){},
        getCancelButton:()=>({addEventListener(){}})};
    const context={window:{},document,Swal,sessionStorage:{getItem:()=>saved,setItem:(_,v)=>{saved=v;},removeItem:()=>{saved=null;}},
        fetch:async(url,opts)=>{calls.push([url,opts]);return {ok:true,json:async()=>({success:true,job:{id,state,stats:{},message:'fixture result'}})};},
        setTimeout,console};
    vm.runInNewContext(source,context);await onReady();
    assert(calls.some(([url,opts])=>url.endsWith('/cancel')&&opts.method==='POST'));
    assert(!calls.some(([url])=>url.endsWith('/import')));
    assert(calls[0][1].headers['X-CSRF-Token']==='fixture-csrf');
    assert((saved!==null)===(state!=='cancelled'));
}
(async()=>{for(const state of ['cancelled','cancelled_partial','interrupted'])await scenario(state);
    console.log(JSON.stringify({passed:3,network:false,replayed_import:false}));})();

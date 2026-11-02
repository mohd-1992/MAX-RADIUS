/* Isolated async export contracts: no actual files, renderer, server or NAS. */
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
function fixture(hasEngine=true){
 let click,resolveExport,resolveCancel,downloads=0;const notices=[],requests=[];
 const ctx={window:{},location:{href:'http://fixture.local/account',origin:'http://fixture.local'},URL:class extends URL {
  static createObjectURL(){return 'blob:synthetic';}static revokeObjectURL(){}
 },crypto:{randomUUID:()=> 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'},setTimeout(){},
 document:{addEventListener:(k,fn)=>click=fn,querySelector:()=>({content:'csrf-synthetic'}),
  createElement:()=>({click(){downloads++;},remove(){}}),body:{appendChild(){}}},
 fetch:async(url,opts)=>{requests.push({url:String(url),opts});return new Promise(resolve=>{if(String(url).includes('/cancel'))resolveCancel=resolve;else resolveExport=resolve;});}};
 if(hasEngine)ctx.window.showToastNotification=opts=>{
  const msg={textContent:opts.text},notice={opts,msg,closed:false,element:{querySelector:()=>msg},close(){this.closed=true;}};
  notices.push(notice);return notice;
 };
 vm.createContext(ctx);vm.runInContext(fs.readFileSync('web/static/heavy-export.js','utf8'),ctx);
 const event=()=>({button:0,target:{closest:()=>({href:'http://fixture.local/account/sessions.pdf'})},preventDefault(){this.prevented=true;}});
 return {ctx,notices,requests,event,click:e=>click(e),export:r=>resolveExport(r),cancel:r=>resolveCancel(r),downloads:()=>downloads};
}
const response=(ok,status,message)=>({ok,status,json:async()=>({message}),blob:async()=>({}),headers:{get:()=>''}});
async function main(){
 let f=fixture(),e=f.event(),p=f.click(e);
 assert(e.prevented);assert.equal(f.notices[0].opts.duration,0);assert.equal(f.notices[0].opts.dismissible,false);
 await f.click(f.event());assert.equal(f.requests.length,1);
 f.export(response(true,200));await p;
 assert.equal(f.downloads(),1);assert(f.notices[0].closed);assert.equal(f.notices[1].opts.icon,'success');
 f=fixture();p=f.click(f.event());const button={disabled:false};const cancel=f.notices[0].opts.action.onClick(button);
 assert(button.disabled);assert.equal(f.requests[1].opts.headers['X-CSRF-Token'],'csrf-synthetic');
 await f.notices[0].opts.action.onClick(button);assert.equal(f.requests.length,2);
 f.cancel(response(true,202,'جارٍ إلغاء التصدير'));await cancel;
 assert.equal(f.notices[0].msg.textContent,'جارٍ إلغاء التصدير');assert(!f.notices[0].closed);
 f.export(response(false,409,'أُلغي التصدير'));await p;assert.equal(f.downloads(),0);assert.equal(f.notices[1].opts.icon,'info');
 f=fixture();p=f.click(f.event());const failedButton={disabled:false};const failed=f.notices[0].opts.action.onClick(failedButton);
 f.cancel(response(false,404,'تعذر تأكيد الإلغاء'));await failed;assert.equal(failedButton.disabled,false);
 f.export(response(false,503,'<img src=x onerror=alert(1)>'));await p;
 assert.equal(f.notices[1].opts.icon,'error');assert.equal(f.notices[1].opts.text,' ');assert.equal(f.notices[1].msg.textContent,'<img src=x onerror=alert(1)>');
 f=fixture();p=f.click(f.event());const late=f.notices[0].opts.action.onClick({disabled:false});
 f.export(response(true,200));await p;f.cancel(response(true,202,'جارٍ إلغاء التصدير'));await late;
 assert.equal(f.notices[1].opts.icon,'success');
 f=fixture(false);e=f.event();await f.click(e);assert(!e.prevented);assert.equal(f.requests.length,0);
 console.log('5 export scenarios passed: complete, cancel, rejection/failure/plain text, late cancel, standalone native fallback');
}
main().catch(e=>{console.error(e);process.exitCode=1;});

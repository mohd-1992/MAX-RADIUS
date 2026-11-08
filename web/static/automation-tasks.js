'use strict';
// Server-owned schedules and durable maintenance runs; no client-side execution.
const $ = id => document.getElementById(id);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon = (name, cls='') => `<i class="fa-solid fa-${name} ${cls}" aria-hidden="true"></i>`;
const automationTimezone = document.getElementById('topLiveClockTz')?.textContent?.trim() || 'Asia/Aden';
function wallDate(value,zone=automationTimezone){
 const parts=Object.fromEntries(new Intl.DateTimeFormat('en-CA',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).formatToParts(value).filter(p=>p.type!=='literal').map(p=>[p.type,Number(p.value)]));
 return new Date(Date.UTC(parts.year,parts.month-1,parts.day,parts.hour,parts.minute,parts.second));
}
function runStamp(value){const s=String(value||'').replace(' ','T');return new Date(/(?:Z|[+-]\d{2}:\d{2})$/.test(s)?s:s+'Z');}
function runDateText(value){const d=runStamp(value);return Number.isFinite(+d)?new Intl.DateTimeFormat('ar-YE',{timeZone:automationTimezone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(d)+' ('+automationTimezone+')':'—';}
const reference = wallDate(new Date()); // Wall coordinates are for editor previews only; scheduling remains server-owned.
const dateText = (d, opts={}) => new Intl.DateTimeFormat('ar-YE',{timeZone:'UTC',day:'numeric',month:'short',...opts}).format(d);
const clockText = d => new Intl.DateTimeFormat('en-GB',{timeZone:'UTC',hour:'2-digit',minute:'2-digit',hour12:false}).format(d);
const catalog = [
 {key:'summary',name:'تلخيص الجلسات القديمة',icon:'box-archive',color:'green',description:'تلخيص التاريخ القديم المحسوم مع إبقاء الكروت والحصص والمال وبصمات منع التكرار.',impact:'مهمة ثقيلة',defaults:'الجلسات الأقدم من 90 يومًا • التاريخ المحسوم فقط'},
 {key:'delete',name:'حذف الكروت المنتهية',icon:'ticket',color:'red',description:'تنظيف الكروت المؤهلة وفق العمر وسبب الانتهاء، مع إبقاء السجل المالي والاستهلاك.',impact:'حذف نهائي',defaults:'عمر الانتهاء 60 يومًا • حد أقصى 5000 كرت للجولة'},
 {key:'audit',name:'فحص سلامة قاعدة البيانات',icon:'shield-halved',color:'green',description:'تقرير بالملاحظات وتعارضات البيانات، دون إصلاح تلقائي.',impact:'فحص فقط',defaults:'فحص شامل • تقرير داخل اللوحة'},
 {key:'temp',name:'تنظيف الملفات المؤقتة',icon:'broom',color:'',description:'إزالة ملفات التصدير المنتهية ومخلفات المهام، مع حماية الملفات المستخدمة.',impact:'تنظيف ملفات',defaults:'أقدم من 7 أيام • ملفات التصدير فقط'},
 {key:'logs',name:'تنظيف السجلات التشغيلية',icon:'file-lines',color:'orange',description:'مدة احتفاظ مستقلة لسجل المهام والإشعارات. لا تشمل المال أو المحاسبة.',impact:'حذف سجلات محددة',defaults:'سجل المهام فقط • أقدم من 90 يومًا'},
 {key:'alert',name:'تنبيهات المساحة',icon:'bell',color:'orange',description:'فحص الحدود المحددة، مع تنبيه عند تغير الحالة ومنع تكرار الإشعارات.',impact:'فحص وتنبيه',defaults:'مساحة متاحة أقل من 15%'},
 {key:'report',name:'تقارير دورية',icon:'chart-pie',color:'',description:'ملخص المبيعات للفترة المطلوبة، مع عرض النتيجة وإرسالها لقناة تلجرام المهيأة.',impact:'قراءة وتقرير',defaults:'ملخص يومي • العرض داخل اللوحة'},
 {key:'optimize',name:'تحسين جداول مختارة',icon:'database',color:'orange',description:'استعادة المساحة عند ملاءمة الحالة، داخل نافذة صيانة، دون ضمان حجم محدد.',impact:'قد ينتظر أقفالًا',defaults:'اختيار الجداول يدويًا • معطل افتراضيًا'}
];
const kinds = Object.fromEntries(catalog.map(c => [c.key,c]));
const baseSchedule = (mode='daily',times=['03:00']) => ({mode,times,days:[0,4],monthDay:1,fallback:'last',interval:6,unit:'hours',start:reference.toISOString().slice(0,10),startTime:'03:00',endMode:'never',endDate:'',maxRuns:10,timezone:automationTimezone});
let tasks=[];let history=[];let catalogs={packages:[],batches:[]};let terminalSeen=new Set();let firstLoad=true;
const timers = new Map(); let editingId=null; let chosenDays=new Set([0,4]);

function notifyAutomation(message,type='success'){ return window.showToast(esc(message),type); }
function nextRuns(s,limit=5,now=reference){
 const persisted=tasks.find(t=>t.schedule===s);if(persisted)return(persisted.next_dates||[]).slice(0,limit).map(x=>wallDate(new Date(x),s.timezone||automationTimezone));
 if(!s.start || !/^\d{4}-\d{2}-\d{2}$/.test(s.start))return [];
 const start=new Date(s.start+'T00:00:00Z');if(!Number.isFinite(+start))return [];
 const end=s.endMode==='date'&&s.endDate?new Date(s.endDate+'T23:59:59Z'):null;
 const cap=s.endMode==='count'?Math.max(0,Number(s.maxRuns)||0):Infinity;
 limit=Math.min(limit,cap);if(!limit)return [];
 if(s.mode==='interval'){
  const step=Number(s.interval)*({minutes:60000,hours:3600000,days:86400000}[s.unit]||0);
  if(!Number.isFinite(step)||step<60000)return [];
  const anchor=new Date(s.start+'T'+s.startTime+':00Z');if(!Number.isFinite(+anchor))return [];
  const skipped=Math.max(0,Math.floor((now-anchor)/step)+1);const left=cap===Infinity?limit:Math.min(limit,Math.max(0,cap-skipped));
  const a=[];for(let i=0;i<left;i++){const d=new Date(+anchor+(skipped+i)*step);if(end&&d>end)break;a.push(d);}return a;
 }
 const result=[];let ordinal=0;
 for(let k=0;k<1462 && result.length<limit;k++){
  const day=new Date(+start+k*86400000);if(end&&day>end)break;
  if(s.mode==='once'&&k>0)break;
  if(s.mode==='weekly'&&!s.days.includes(day.getUTCDay()))continue;
  if(s.mode==='monthly'){
   const last=new Date(Date.UTC(day.getUTCFullYear(),day.getUTCMonth()+1,0)).getUTCDate();
   const requested=Number(s.monthDay);if(requested>last&&s.fallback==='skip')continue;
   if(day.getUTCDate()!==Math.min(requested,last))continue;
  }
  for(const t of [...new Set(s.times)].sort()){
   if(!/^\d{2}:\d{2}$/.test(t))continue;
   const [h,m]=t.split(':').map(Number);if(h>23||m>59)continue;
   const d=new Date(+day+h*3600000+m*60000);ordinal++;
   if(ordinal>cap)return result;
   if(d>now && (!end||d<=end))result.push(d);
   if(result.length>=limit)break;
  }
 }return result;
}
const dayNames=['الأحد','الإثنين','الثلاثاء','الأربعاء','الخميس','الجمعة','السبت'];
function scheduleLabel(s){
 const ts=s.times.join('، ');
 if(s.mode==='once')return 'مرة واحدة • '+s.start+' • '+ts;
 if(s.mode==='interval')return `كل ${s.interval} ${ {hours:'ساعات',minutes:'دقائق',days:'أيام'}[s.unit] }`;
 if(s.mode==='weekly')return s.days.map(d=>dayNames[d]).join(' و')+' • '+ts;
 if(s.mode==='monthly')return 'يوم '+s.monthDay+' من كل شهر • '+ts;
 return (s.times.length===1?'يوميًا':'يوميًا، '+s.times.length+' مواعيد')+' • '+ts;
}
function paramsLabel(t){const p=t.params||{};if(t.kind==='summary')return `أقدم من ${p.age||90} يومًا • تاريخ محسوم فقط`;if(t.kind==='delete')return `منتهية منذ ${p.age??60} يومًا • حتى ${p.batch??5000} كرت للجولة`;if(t.kind==='alert')return `تنبيه إذا انخفضت المساحة المتاحة عن ${p.threshold||15}%`;if(t.kind==='report')return 'ملخص '+({day:'يومي',week:'أسبوعي',month:'شهري'}[p.period]||'أسبوعي');return kinds[t.kind].defaults;}
function stateTag(state){const label={success:'نجاح',failed:'فشل',partial:'جزئي',cancelled:'ملغى',queued:'بانتظار التنفيذ',running:'قيد التشغيل',skipped:'متخطى',interrupted:'توقف غير مكتمل'}[state];return `<span class="tag ${{success:'green',failed:'red',partial:'orange',cancelled:'neutral'}[state]||'neutral'}">${esc(label||state)}</span>`;}
function render(){
 const active=tasks.filter(t=>t.enabled).length;
 const success=history.filter(h=>h.state==='success').length;const settled=history.filter(h=>h.state!=='cancelled').length;
 const metrics=[['المهام المفعلة',active+' <span style="font-size:13px;color:var(--muted)">/ '+tasks.length+'</span>','calendar-check','مهام مفعلة حسب الجدول المحدد'],['نسبة النجاح',Math.round(success/Math.max(1,settled)*100)+'<span style="font-size:17px">%</span>','circle-check',`${success} نجاح من ${settled} نتائج غير ملغاة`],['تحتاج مراجعة',history.filter(h=>['failed','partial'].includes(h.state)).length,'triangle-exclamation','فشل أو تنفيذ جزئي في السجل'],['المواعيد القادمة',tasks.filter(t=>t.enabled&&nextRuns(t.schedule,1).length).length,'clock','بحسب مواعيد المهام المفعلة']];
 $('metrics').innerHTML=metrics.map(([label,value,i,sub])=>`<article class="metric bg-[#0b0f19] border-[#1c2840] shadow-sm hover:shadow-md transition-all duration-300"><div class="metric-top">${label}<span class="metric-icon">${icon(i)}</span></div><strong>${value}</strong><small>${sub}</small></article>`).join('');
 $('taskCount').textContent=tasks.length;
 const q=$('search').value.trim();const filter=$('statusFilter').value;const type=$('typeFilter').value;
 const visible=tasks.filter(t=>(!q||(t.name+kinds[t.kind].name).includes(q))&&(type==='all'||t.kind===type)&&(filter==='all'||filter==='running'&&t.progress!=null||filter==='active'&&t.enabled||filter==='paused'&&!t.enabled));
 $('visibleCount').textContent=`عرض ${visible.length} من ${tasks.length} مهام`;
 $('taskList').innerHTML=visible.map(t=>{
  const c=kinds[t.kind],next=nextRuns(t.schedule,1)[0];
  return `<article class="task-card bg-[#0b0f19] border-[#1c2840] shadow-sm hover:shadow-md transition-all duration-300 ${t.progress!=null?'running':''}"><div class="task-top"><span class="task-icon ${c.color}">${icon(c.icon)}</span><div class="task-heading"><div class="task-title"><h3>${esc(t.name)}</h3>${t.progress!=null?'<span class="tag violet">قيد التشغيل</span>':!t.enabled?'<span class="tag neutral">متوقفة</span>':''}${t.kind==='delete'?'<span class="tag red">حذف نهائي</span>':''}</div><p class="task-description">${esc(paramsLabel(t))}</p></div><button class="toggle ${t.enabled?'on':''}" role="switch" aria-checked="${t.enabled}" aria-label="تفعيل جدولة ${esc(t.name)}" data-action="toggle" data-id="${esc(t.id)}"></button></div><div class="task-meta"><span>${icon('calendar-days')}${esc(scheduleLabel(t.schedule))}</span><span>${icon('rotate')}${t.runs} محاولة</span><span>${icon('circle-check')}آخر نتيجة: ${esc(t.last)}</span></div>${t.progress!=null?`<div class="progress-strip"><span>جارٍ المعالجة</span><div class="progress-track"><span style="width:${t.progress}%"></span></div><strong>${t.progress}%</strong></div>`:''}<div class="task-bottom"><span class="next-run">الموعد القادم<strong>${t.enabled&&next?esc(dateText(next)+'، '+clockText(next)):t.enabled?'انتهت المواعيد المحددة':'الجدولة متوقفة'}</strong></span><div class="task-actions"><button class="icon-button" title="تفاصيل المهمة" aria-label="تفاصيل ${esc(t.name)}" data-action="details" data-id="${esc(t.id)}">${icon('circle-info')}</button><button class="icon-button" title="نسخ المهمة" aria-label="نسخ ${esc(t.name)}" data-action="duplicate" data-id="${esc(t.id)}">${icon('copy')}</button><button class="icon-button" title="تعديل المهمة" aria-label="تعديل ${esc(t.name)}" data-action="edit" data-id="${esc(t.id)}">${icon('pen')}</button><button class="icon-button delete-task" title="${t.progress!=null?'ألغِ التشغيل قبل حذف المهمة':'حذف المهمة'}" aria-label="حذف ${esc(t.name)}" data-action="delete" data-id="${esc(t.id)}" ${t.progress!=null?'disabled':''}>${icon('trash-can')}</button>${t.progress!=null?`<button class="button danger-button" data-action="cancel" data-id="${esc(t.id)}">${icon('stop')} إلغاء التشغيل</button>`:`<button class="button" data-action="run" data-id="${esc(t.id)}">${icon('play')} تشغيل الآن</button>`}</div></div></article>`;
 }).join('')||'<div class="empty-state">'+icon('magnifying-glass')+'لا توجد مهام تطابق البحث.</div>';
 const agenda=tasks.filter(t=>t.enabled).flatMap(t=>nextRuns(t.schedule,2).map(d=>({t,d}))).sort((a,b)=>a.d-b.d).slice(0,5);
 $('agendaList').innerHTML=agenda.map(({t,d})=>`<div class="agenda-item"><small>${esc(dateText(d,{weekday:'long'}))}</small><span class="time" dir="ltr">${clockText(d)}</span><strong>${esc(t.name)}</strong></div>`).join('')||'<p class="muted small">لا توجد مواعيد قادمة.</p>';
 renderHistory();
}
function renderHistory(){const f=$('historyFilter').value;const rows=history.filter(h=>f==='all'||h.state===f);$('historyBody').innerHTML=rows.map(h=>`<tr><td><strong>${esc(h.name)}</strong><small>${esc(kinds[h.kind].name)}</small></td><td>${stateTag(h.state)}</td><td>${esc(h.at)}</td><td>${esc(h.duration)}</td><td>${esc(h.result)}</td><td><button class="text-button" data-history="${esc(h.id)}">التفاصيل ${icon('angle-left')}</button></td></tr>`).join('')||'<tr><td colspan="6" class="empty-state">لا توجد نتائج لهذا الفلتر.</td></tr>';}
function showTab(name){for(const t of document.querySelectorAll('.tab')){const yes=t.dataset.tab===name;t.classList.toggle('active',yes);t.setAttribute('aria-selected',String(yes));}for(const n of ['tasks','catalog','history','settings'])$(n+'Panel').classList.toggle('hidden',n!==name);}
function showDetails(t){$('detailTitle').textContent=t.name;$('detailContent').innerHTML=`<span class="tag violet">${esc(kinds[t.kind].name)}</span><div class="detail-grid"><div class="detail-cell bg-[#0b0f19] border-[#1c2840] shadow-sm hover:shadow-md transition-all duration-300"><small>الجدولة</small>${esc(scheduleLabel(t.schedule))}</div><div class="detail-cell bg-[#0b0f19] border-[#1c2840] shadow-sm hover:shadow-md transition-all duration-300"><small>النطاق</small>${esc(paramsLabel(t))}</div><div class="detail-cell bg-[#0b0f19] border-[#1c2840] shadow-sm hover:shadow-md transition-all duration-300"><small>المحاولات / آخر نتيجة</small>${t.runs} • ${esc(t.last)}</div><div class="detail-cell bg-[#0b0f19] border-[#1c2840] shadow-sm hover:shadow-md transition-all duration-300"><small>المدة السابقة</small>${esc(t.duration)}</div></div><h3>المواعيد القادمة</h3>${datesHTML(nextRuns(t.schedule))}<div class="hint">تفعيل الجدولة: ${t.enabled?'مفعلة':'متوقفة'}.</div>`;$('detailDialog').showModal();}
function datesHTML(dates){return dates.map(d=>`<div class="date-item"><span>${esc(dateText(d,{weekday:'short'}))}</span><strong dir="ltr">${clockText(d)}</strong></div>`).join('')||'<p class="hint">لا توجد مواعيد ضمن الاختيارات الحالية، أو انتهى حد التشغيل. عدّل وقت البداية أو التكرار.</p>';}
function logDetails(id){const h=history.find(v=>v.id===id);if(!h)return;$('detailTitle').textContent=h.name;$('detailContent').innerHTML=stateTag(h.state)+`<p style="margin-top:16px;font-size:13px">${esc(h.details)}</p><div class="detail-grid"><div class="detail-cell bg-[#0b0f19] border-[#1c2840] shadow-sm hover:shadow-md transition-all duration-300"><small>البداية</small>${esc(h.at)}</div><div class="detail-cell bg-[#0b0f19] border-[#1c2840] shadow-sm hover:shadow-md transition-all duration-300"><small>المدة</small>${esc(h.duration)}</div></div>${h.stages.map(([k,v])=>`<div class="date-item"><span>${esc(k)}</span><strong>${esc(v)}</strong></div>`).join('')}${h.payload?.metrics?Object.entries(h.payload.metrics).map(([kind,m])=>`<div class="detail-cell"><strong>${kind==='voucher'?'مبيعات وتجديدات الكروت':'فواتير المشتركين المدفوعة'}</strong><p>عدد العمليات: ${esc(m.sold_count)} • الإجمالي: ${esc(m.total_gross_revenue)} • حصة المنظومة: ${esc(m.total_isp_revenue)} • عمولة الموزعين: ${esc(m.total_reseller_profit)}</p></div>`).join(''):''}<div class="hint">يمكن مراجعة النتيجة ومراحل التنفيذ من هذا السجل.</div>`;$('detailDialog').showModal();}
function ask(title,text,action,label='بدء التشغيل',danger=false){ return window.confirmAction({title:esc(title),text:esc(text),confirmText:label,cancelText:'رجوع',icon:danger?'warning':'question',isDanger:danger,onConfirm:action}); }
function requestTaskDelete(t){
 if(t.progress!=null)return notifyAutomation('ألغِ التشغيل قبل حذف المهمة.','warning');
 ask('حذف المهمة',`هل تريد حذف «${t.name}» وإلغاء مواعيدها القادمة؟ سيبقى سجل التنفيذ محفوظًا.`,()=>guard(async()=>{await api('delete',{id:t.id,revision:t.revision});await refresh();notifyAutomation('حُذفت المهمة؛ سجلها محفوظ.');}),'حذف المهمة',true);
}
function finishRun(t){ return guard(async()=>{await api('cancel',{run_id:t.run_id});notifyAutomation('طُلب الإلغاء؛ انتظر انتهاء الدفعة الحالية.','info');await refresh();}); }
function run(t){ const key='automation-request-'+t.id;const request_id=sessionStorage.getItem(key)||crypto.randomUUID().replaceAll('-','');sessionStorage.setItem(key,request_id);return guard(async()=>{await api('run',{id:t.id,request_id});sessionStorage.removeItem(key);await refresh();notifyAutomation('قُبل التشغيل؛ تابع النتيجة في سجل التنفيذ.','info');}); }
function field(label,id,html,full=false){return `<label class="${full?'full':''}">${label}${html.replace('<input',`<input id="${id}"`).replace('<select',`<select id="${id}"`)}</label>`;}
function parameterFields(kind,p={}){
 let html='';const num=(label,id,value,min=1,max=3650)=>field(label,id,`<input type="number" value="${esc(value)}" min="${min}" max="${max}" required>`);
 const scope=()=>field('نطاق التطبيق','pScope','<select><option value="all">جميع الباقات المسموح بها</option><option value="selected">باقات محددة</option><option value="batch">حزمة محددة</option></select>',true);
 if(['summary','delete','temp','logs'].includes(kind))html+=num(kind==='delete'?'مضى على الانتهاء (يوم)':kind==='summary'?'عمر الجلسات (يوم)':'أقدم من (يوم)','pAge',p.age??({summary:90,delete:60,temp:7,logs:90}[kind]),kind==='delete'?0:kind==='summary'?2:1);
 if(kind==='delete'){html+=field('سبب الانتهاء','pReason','<select><option value="all">الأسبق: الرصيد أو الأيام</option><option value="time">انتهاء الأيام فقط</option><option value="quota">نفاد الرصيد فقط</option></select>');html+=scope();html+=field('الباقات','pPackages','<select multiple>'+catalogs.packages.map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join('')+'</select>',true);html+=field('الحزمة','pBatchScope','<select><option value="">اختر الحزمة</option>'+catalogs.batches.map(x=>`<option value="${x.id}">${esc(x.name)} (#${x.id})</option>`).join('')+'</select>',true);html+=num('الحد الأقصى للكروت في الجولة','pBatch',p.batch||5000,1,5000);html+='<div class="hint full">عمر 0 يلغي فلتر العمر ويشمل مجهول تاريخ الانتهاء. الجلسات غير المحسومة تنتظر التسوية الآمنة. السجل المالي والاستهلاك محفوظان.</div>';}
 if(kind==='summary'){html+=scope();html+=field('الباقات','pPackages','<select multiple>'+catalogs.packages.map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join('')+'</select>',true);html+=field('الحزمة','pBatchScope','<select><option value="">اختر الحزمة</option>'+catalogs.batches.map(x=>`<option value="${x.id}">${esc(x.name)} (#${x.id})</option>`).join('')+'</select>',true);html+=num('الحد الأقصى للعناصر في الجولة','pBatch',p.batch||500,1,50000);html+='<div class="hint full">تبقى الكروت وأرصدتها. تُلخص الجلسات القديمة المؤهلة فقط، دون حذف حسابات.</div>';}
 if(kind==='audit')html+='<div class="hint full">فحص وتقرير فقط. لا إصلاح تلقائي.</div>';
 if(kind==='temp')html+=field('نوع الملفات','pFiles','<select><option>ملفات PDF المؤقتة المنتهية فقط</option></select>');
 if(kind==='logs')html+=field('السجل المستهدف','pLogs','<select><option>سجل المهام والإشعارات</option></select>')+'<div class="hint full">لا يشمل السجلات المالية أو تفاصيل المحاسبة.</div>';
 if(kind==='alert'){html+=num('تنبيه إذا كانت المساحة أقل من (%)','pThreshold',p.threshold||15,1,99);html+='<div class="hint full">يُكرر الإشعار عند تغير الحالة فقط، دون إغراق بالتنبيهات.</div>';}
 if(kind==='report'){html+=field('الفترة','pPeriod','<select><option value="day">اليوم السابق</option><option value="week">الأسبوع السابق</option><option value="month">الشهر السابق</option></select>');html+='<div class="hint full">تعرض النتيجة داخل النظام وترسل لقناة تلجرام المهيأة إن توفرت.</div>';}
 if(kind==='optimize')html+='<label class="full">الجداول<select id="pTables" multiple required>'+['radacct','radacct_archive','wisp_imported_session_history','radpostauth','radcheck','radusergroup','wisp_vouchers','wisp_voucher_sales','wisp_subscribers','wisp_audit_logs'].map(x=>`<option value="${x}">${x}</option>`).join('')+'</select></label><div class="warning-box full">داخل نافذة صيانة فقط؛ أمر تحسين الجدول الجاري لا يمكن إلغاؤه بأمان.</div>';

 $('parameters').innerHTML=html;
 for(const [id,v] of [['pScope',p.scope],['pReason',p.reason],['pPeriod',p.period]])if($(id)&&v)$(id).value=v;
 $('deleteConfirmation').classList.toggle('hidden',!['delete','logs','optimize'].includes(kind));$('destructiveConsent').checked=false;$('destructiveConsent').required=['delete','logs','optimize'].includes(kind);
 for(const o of $('pPackages')?.options||[])o.selected=(p.package_ids||[]).includes(Number(o.value));if($('pBatchScope'))$('pBatchScope').value=p.batch_id||'';for(const o of $('pTables')?.options||[])o.selected=(p.tables||[]).includes(o.value);
 $('typeDescription').textContent=kinds[kind].description;
}
function readParams(){const p={};for(const [id,key] of [['pAge','age'],['pScope','scope'],['pReason','reason'],['pBatch','batch'],['pThreshold','threshold'],['pPeriod','period']])if($(id))p[key]=$(id).value;if($('pPackages'))p.package_ids=[...$('pPackages').selectedOptions].map(o=>Number(o.value));if($('pBatchScope'))p.batch_id=$('pBatchScope').value;if($('pTables'))p.tables=[...$('pTables').selectedOptions].map(o=>o.value);return p;}
function addTime(value='15:00'){const row=document.createElement('div');row.className='time-item';row.innerHTML=`<input type="time" required value="${esc(value)}" aria-label="وقت التشغيل"><button type="button" class="icon-button remove-time" aria-label="حذف وقت التشغيل">${icon('xmark')}</button>`;$('timeList').append(row);updateSummary();}
function readSchedule(){return {mode:$('scheduleMode').value,times:[...$('timeList').querySelectorAll('input')].map(x=>x.value).filter(Boolean),days:[...chosenDays].sort(),monthDay:Number($('monthDay').value),fallback:$('monthFallback').value,interval:Number($('intervalValue').value),unit:$('intervalUnit').value,start:$('startDate').value,startTime:$('intervalStart').value,endMode:$('endMode').value,endDate:$('endDate').value,maxRuns:Number($('maxRuns').value),timezone:tasks.find(t=>t.id===editingId)?.schedule?.timezone||automationTimezone};}
function updateScheduleFields(){const m=$('scheduleMode').value;for(const [id,show] of [['weeklyFields',m==='weekly'],['monthlyFields',m==='monthly'],['intervalFields',m==='interval'],['intervalStartLabel',m==='interval'],['timesFields',m!=='interval'],['endModeLabel',m!=='once']])$(id).classList.toggle('hidden',!show);for(const input of $('timeList').querySelectorAll('input'))input.required=m!=='interval';$('addTime').disabled=m==='once'||$('timeList').children.length>=6;const end=m==='once'?'never':$('endMode').value;$('endDateLabel').classList.toggle('hidden',end!=='date');$('maxRunsLabel').classList.toggle('hidden',end!=='count');$('countHint').classList.toggle('hidden',end!=='count');$('endDate').required=end==='date';$('endDate').min=$('startDate').value;$('maxRuns').required=end==='count';updateSummary();}
function updateSummary(){const s=readSchedule();if(s.mode==='once'){s.times=s.times.slice(0,1);s.endMode='never';}$('frequencyLabel').textContent=s.mode==='once'?'موعد واحد فقط':`عدد الأوقات المختارة: ${s.times.length} • حتى 6 أوقات في اليوم`;$('scheduleSummary').innerHTML=`<div class="summary-line">${icon(kinds[$('taskType').value]?.icon||'clock')}<div>${esc($('taskName').value||'مهمة جديدة')}<span>${esc(kinds[$('taskType').value]?.name||'')}</span></div></div><div class="summary-line">${icon('calendar-days')}<div>${esc(scheduleLabel(s))}<span>${esc(s.timezone||automationTimezone)} • يبدأ ${esc(s.start||'—')}</span></div></div><div class="summary-line">${icon('flag-checkered')}<div>${s.endMode==='count'?'حتى '+esc(s.maxRuns)+' تشغيلات مجدولة':s.endMode==='date'?'حتى '+esc(s.endDate||'اختيار التاريخ'):'دون حد إضافي'}<span>${$('enabled').checked?'ستكون الجدولة مفعلة':'ستبقى الجدولة متوقفة'}</span></div></div>`;$('nextDates').innerHTML=datesHTML(nextRuns(s));clearTimeout(scheduleTimer);const seq=++scheduleSequence;scheduleTimer=setTimeout(async()=>{try{const result=await api('schedule',{...s,timezone:s.timezone||automationTimezone});if(seq===scheduleSequence)$('nextDates').innerHTML=datesHTML(result.dates.map(x=>wallDate(new Date(x),s.timezone||automationTimezone)));}catch{if(seq===scheduleSequence)$('nextDates').textContent='راجع خيارات الجدولة؛ تعذر تأكيد المواعيد.';}},250);}
function openEditor(task=null,duplicate=false,kind='summary'){
 $('taskForm').reset();editingId=task&&!duplicate?task.id:null;$('editorTitle').textContent=editingId?'تعديل المهمة':duplicate?'نسخ المهمة':'إنشاء مهمة جديدة';
 const c=task?.kind||kind;const s=task?.schedule||baseSchedule();$('taskName').value=task?(task.name+(duplicate?' — نسخة':'')):kinds[c].name;$('taskType').value=c;parameterFields(c,task?.params||{});
 $('scheduleMode').value=s.mode;$('startDate').value=s.start;$('intervalStart').value=s.startTime;$('intervalValue').value=s.interval;$('intervalUnit').value=s.unit;$('monthDay').value=s.monthDay;$('monthFallback').value=s.fallback;$('endMode').value=s.endMode;$('endDate').value=s.endDate;$('maxRuns').value=s.maxRuns;
 $('enabled').checked=duplicate?false:task?task.enabled:!['delete','logs','optimize'].includes(c);$('notify').checked=task?.options?.notify??true;$('resourcePause').checked=task?.options?.resourcePause??true;chosenDays=new Set(s.days);renderDays();$('timeList').innerHTML='';for(const t of s.times)addTime(t);updateScheduleFields();$('editorDialog').showModal();
}
function renderDays(){$('weekdayChoices').innerHTML=dayNames.map((n,i)=>`<button type="button" data-day="${i}" aria-pressed="${chosenDays.has(i)}" class="day-choice ${chosenDays.has(i)?'chosen':''}">${n}</button>`).join('');}
function formSubmit(event){event.preventDefault();return guard(async()=>{
 const old=tasks.find(t=>t.id===editingId);const s=readSchedule();s.timezone='Asia/Aden';if(s.mode==='once'){s.times=s.times.slice(0,1);s.endMode='never';}
 s.timezone=old?.schedule?.timezone||automationTimezone;
 const value={name:$('taskName').value.trim(),kind:$('taskType').value,enabled:$('enabled').checked,schedule:s,params:readParams(),options:{notify:$('notify').checked,resourcePause:$('resourcePause').checked},consent:$('destructiveConsent').checked};
 if(old){value.id=old.id;value.revision=old.revision;}
 await api('',value);$('editorDialog').close();showTab('tasks');await refresh();notifyAutomation('تم حفظ المهمة وجدولتها.');
 });}
function init(){
 $('taskType').innerHTML=catalog.map(c=>`<option value="${c.key}">${c.name}</option>`).join('');$('typeFilter').insertAdjacentHTML('beforeend',catalog.map(c=>`<option value="${c.key}">${c.name}</option>`).join(''));
 $('catalogGrid').innerHTML=catalog.map(c=>`<article class="catalog-card bg-[#0b0f19] border-[#1c2840] shadow-sm hover:shadow-md transition-all duration-300"><span class="task-icon ${c.color}">${icon(c.icon)}</span><h3>${c.name}</h3><p>${c.description}</p><span class="tag ${c.key==='delete'?'red':'neutral'}">${c.impact}</span><button class="button secondary" data-create-kind="${c.key}">${icon('plus')} تخصيص مهمة</button></article>`).join('');
 $('newTask').addEventListener('click',()=>openEditor());$('taskForm').addEventListener('submit',formSubmit);$('policyForm').addEventListener('submit',e=>{e.preventDefault();guard(async()=>{await api('policy',{missed:$('policyMissed').value,retention:Number($('policyRetention').value),resourcePause:$('policyResource').checked,notify:$('policyNotify').checked});notifyAutomation('تم حفظ سياسة التشغيل.');});});
 for(const id of ['search','statusFilter','typeFilter'])$(id).addEventListener(id==='search'?'input':'change',render);$('historyFilter').addEventListener('change',renderHistory);
 $('taskType').addEventListener('change',()=>{$('taskName').value=kinds[$('taskType').value].name;parameterFields($('taskType').value);$('enabled').checked=!['delete','logs','optimize'].includes($('taskType').value);updateSummary();});
 $('taskForm').addEventListener('input',updateSummary);$('taskForm').addEventListener('change',updateScheduleFields);
 $('scheduleMode').addEventListener('change',()=>{if($('scheduleMode').value==='once')while($('timeList').children.length>1)$('timeList').lastElementChild.remove();updateScheduleFields();});
 $('addTime').addEventListener('click',()=>{if($('timeList').children.length<6)addTime();updateScheduleFields();});
 document.addEventListener('click',e=>{
  const close=e.target.closest('[data-close]');if(close)$(close.dataset.close).close();
  const tab=e.target.closest('[data-tab]');if(tab)showTab(tab.dataset.tab);
  const create=e.target.closest('[data-create-kind]');if(create)openEditor(null,false,create.dataset.createKind);
  const hist=e.target.closest('[data-history]');if(hist)logDetails(hist.dataset.history);
  const day=e.target.closest('[data-day]');if(day){const d=Number(day.dataset.day);chosenDays.has(d)?chosenDays.delete(d):chosenDays.add(d);renderDays();updateSummary();}
  const remove=e.target.closest('.remove-time');if(remove){if($('timeList').children.length>1){remove.parentElement.remove();updateScheduleFields();}else notifyAutomation('يجب الإبقاء على وقت تشغيل واحد على الأقل.');}
  const btn=e.target.closest('[data-action]');if(!btn)return;const t=tasks.find(t=>t.id===btn.dataset.id);if(!t)return;
  switch(btn.dataset.action){case'toggle':guard(async()=>{await api('',{...t,enabled:!t.enabled});await refresh();});break;case'details':showDetails(t);break;case'edit':openEditor(t);break;case'duplicate':openEditor(t,true);break;case'delete':requestTaskDelete(t);break;case'run':ask('تشغيل المهمة',`هل تريد بدء تشغيل «${t.name}» الآن؟`,()=>run(t));break;case'cancel':ask('إلغاء التشغيل الجاري','سيُوقف التشغيل وتُحفظ النتيجة في السجل. الإلغاء لا يسترجع الدفعات المنفذة.',()=>finishRun(t,true),'إلغاء التشغيل');break;}
 });
 for(const dialog of document.querySelectorAll('dialog'))dialog.addEventListener('click',e=>{if(e.target===dialog){const b=dialog.getBoundingClientRect();if(e.clientX<b.left||e.clientX>b.right||e.clientY<b.top||e.clientY>b.bottom)dialog.close();}});
 render();refresh().catch(reportError);setInterval(()=>{if(!document.hidden&&(tasks.some(t=>t.progress!=null)||Date.now()-lastRefresh>30000))refresh().catch(reportError);},3000);
}

let scheduleSequence=0;let scheduleTimer;
let refreshing=false;let lastRefresh=0;let lastError=0;
function reportError(e){if(Date.now()-lastError<15000)return;lastError=Date.now();notifyAutomation(e.message||'تعذر قراءة النتيجة؛ لا تعِد العملية قبل التحقق.','error');}
async function guard(fn){try{return await fn();}catch(e){reportError(e);}}
async function api(action='',data){const r=await fetch('/api/automation-tasks'+(action?'/'+action:''),{method:data?'POST':'GET',credentials:'same-origin',headers:data?{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content}:{},...(data?{body:JSON.stringify(data)}:{})});let v;try{v=await r.json();}catch{throw Error('تعذر قراءة رد النظام؛ تحقق من السجل قبل إعادة العملية.');}if(!r.ok||!v.success)throw Error(v.message||'تعذر تنفيذ الطلب');return v;}
async function refresh(){
 if(refreshing)return;refreshing=true;
 try{const data=await api();catalogs={packages:data.packages,batches:data.batches};
 tasks=data.tasks.map(t=>{const runs=data.runs.filter(r=>r.task_id===t.id),active=runs.find(r=>['queued','running'].includes(r.state));return {...t,runs:t.run_count,last:data.notices?.some(n=>n.task_id===t.id&&n.state==='review')?'تحتاج مراجعة الجدولة':runs[0]?({success:'نجاح',partial:'جزئي',failed:'فشل',cancelled:'ملغى',skipped:'متخطى',interrupted:'متوقف',running:'قيد التشغيل',queued:'بانتظار التنفيذ'}[runs[0].state]||runs[0].state):t.run_count?'خارج مدة الاحتفاظ':'لم تنفذ',duration:'—',...(active?{progress:active.result.total?Math.min(99,Math.floor(100*(active.result.processed||0)/active.result.total)):0,run_id:active.id}:{})};});
 history=data.runs.map(r=>({id:r.id,name:r.definition.name,kind:r.definition.kind,state:r.state,at:runDateText(r.created_at),duration:['queued','running'].includes(r.state)?'—':Math.max(0,((+runStamp(r.updated_at))-(+runStamp(r.created_at)))/1000)+' ث (المحاولة)',result:r.result.message||r.state,details:r.result.message||r.state,stages:Object.entries(r.result).filter(([k,v])=>typeof v!=='object'&&k!=='message').map(([k,v])=>[k,String(v)]),payload:r.result}));
 for(const r of data.runs){if(['queued','running'].includes(r.state))continue;if(!firstLoad&&!terminalSeen.has(r.id)&&(['success','cancelled'].includes(r.state)||r.definition.options?.notify||data.policy.notify))notifyAutomation(r.definition.name+': '+(r.result.message||r.state),r.state==='success'?'success':['partial','failed','interrupted'].includes(r.state)?'warning':'info');terminalSeen.add(r.id);}
 firstLoad=false;lastRefresh=Date.now();
 if(!document.querySelector('#policyForm :focus')){$('policyMissed').value=data.policy.missed;$('policyRetention').value=data.policy.retention;$('policyResource').checked=data.policy.resourcePause;$('policyNotify').checked=data.policy.notify;}
 $('legacyRules').innerHTML=data.legacy.map(r=>`<div class="date-item"><span>${esc(r.rule_name)}</span><button class="button" data-legacy="${r.id}" data-enabled="${r.is_active&&r.schedule_enabled?0:1}">${r.is_active&&r.schedule_enabled?'إيقاف الجدولة':'تفعيل الجدولة'}</button></div>`).join('');
 render();
 }finally{refreshing=false;}
}
document.addEventListener('click',e=>{const b=e.target.closest('[data-legacy]');if(b)guard(async()=>{const body=new URLSearchParams({rule_id:b.dataset.legacy,is_active:b.dataset.enabled});const r=await fetch('/api/automation-rules/toggle',{method:'POST',credentials:'same-origin',headers:{'X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content},body});const v=await r.json();if(!r.ok||!v.success)throw Error(v.message||'تعذر تعديل القاعدة');await refresh();});});

init();
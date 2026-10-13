/* Progress is reported by the server; cancelling never replays committed batches. */
(function () {
    'use strict';
    let active = false;
    const base = '/api/tools/mikrotik-import/jobs/';
    const stages = {connecting:'الاتصال بالراوتر', profiles:'قراءة البروفايلات وحدود الباقات',
        users:'سحب الكروت من User Manager', preparing_preview:'تجهيز البروفايلات والعينات وتقرير المعاينة',
        preparing:'تجهيز الاستيراد', importing:'استيراد الدفعات وحفظ بياناتها', completed:'اكتملت العملية'};
    async function request(url, method, data) {
        const headers = {'Content-Type':'application/json'};
        const token = document.querySelector('meta[name="csrf-token"]')?.content || document.querySelector('input[name="csrf_token"]')?.value;
        if (token) headers['X-CSRF-Token'] = token;
        const response = await fetch(url, {method:method || 'GET', headers, credentials:'same-origin',
            body:data === undefined ? undefined : JSON.stringify(data)});
        let value;
        try { value = await response.json(); } catch (_) { throw new Error('تعذر قراءة حالة العملية. لا تعِد تنفيذ الاستيراد قبل مراجعة نتيجته.'); }
        if (!response.ok || !value.success) throw new Error(value.message || value.error || 'تعذر متابعة العملية.');
        return value;
    }
    async function run(kind, payload, resumeId) {
        if (active) throw new Error('توجد عملية قيد المتابعة.');
        active = true;
        let id = resumeId || null, cancelWanted = false, cancelSent = false, terminal = false;
        const numeric = value => Number(value || 0).toLocaleString('ar');
        const popup = Swal.fire({title:kind === 'fetch' ? 'سحب بيانات User Manager' : 'تنفيذ الاستيراد',
            customClass:{popup:'um-dialog um-'+kind, title:'um-title', htmlContainer:'um-content', actions:'um-actions', cancelButton:'um-cancel'},
            buttonsStyling:false,
            html:'<div class="um-body" dir="rtl"><div class="um-phase">'+(kind === 'fetch' ? 'الخطوة الأولى · الاتصال والمعاينة' : 'الخطوة الثانية · استيراد البيانات')+'</div>'+
                '<p id="um-stage" role="status" aria-live="polite">جاري بدء العملية…</p>' +
                '<div class="um-meter-heading"><span>تقدم العملية</span><strong id="um-percent">جارٍ التجهيز</strong></div>'+
                '<progress id="um-progress" max="100" aria-label="تقدم العملية"></progress>' +
                '<p id="um-count"></p><div id="um-stats" class="um-stats"'+(kind==='fetch'?' hidden':'')+'>'+[
                    ['created_users','أُنشئ'],['updated_users','حُدّث'],['skipped_users','تُخطي'],['ignored_expired_users','استُبعد منتهي']
                ].map(([key,label])=>'<div class="um-stat"><span>'+label+'</span><strong id="um-stat-'+key+'">0</strong></div>').join('')+'</div>'+
                '<div class="um-notice"><span class="um-info" aria-hidden="true">i</span><span id="um-note">'+(kind === 'fetch' ? 'السحب للمعاينة فقط؛ لا تُزرع الكروت في هذه المرحلة.' : 'الإلغاء يوقف ما لم يُنفذ؛ الدفعات المحفوظة تبقى في النظام.')+'</span></div></div>',
            showConfirmButton:false, showCancelButton:true, cancelButtonText:'إلغاء العملية',
            allowOutsideClick:false, allowEscapeKey:false,
            didOpen:() => Swal.getCancelButton().addEventListener('click',async event => {
                event.preventDefault();event.stopImmediatePropagation();
                cancelWanted = true;
                const button = Swal.getCancelButton(); if (button) {button.disabled = true; button.textContent = 'جارٍ الإلغاء…';}
                document.getElementById('um-note').textContent = 'أُرسل طلب الإلغاء؛ ننتظر توقف القراءة أو تراجع الدفعة الحالية بأمان.';
                try {
                    if (id && !cancelSent) {await request(base+id+'/cancel','POST',{}); cancelSent = true;}
                } catch (error) {
                    document.getElementById('um-note').textContent = 'تعذر إرسال الإلغاء؛ قد تستمر المهمة. '+error.message;
                    if (button) {button.disabled = false;button.textContent = 'إعادة طلب الإلغاء';}
                }
            },true)});
        try {
            const started = resumeId ? {job:{id:resumeId}} : await request(base+kind,'POST',payload);
            id = started.job.id;
            sessionStorage.setItem('userman-active-job',JSON.stringify({id,kind}));
            if (cancelWanted && !cancelSent) {await request(base+id+'/cancel','POST',{});cancelSent = true;}
            while (true) {
                const {job} = await request(base+id);
                document.getElementById('um-stage').textContent = job.message || stages[job.stage] || 'متابعة العملية';
                const bar = document.getElementById('um-progress');
                if (job.percent == null) bar.removeAttribute('value'); else bar.value = job.percent;
                document.getElementById('um-percent').textContent = job.percent == null ? 'جارٍ القراءة' : numeric(job.percent)+'٪';
                document.getElementById('um-count').textContent = job.total == null ? 'بانتظار عدد سجلات المصدر' :
                    numeric(job.processed)+' من '+numeric(job.total)+(job.percent == null ? '' : ' — '+numeric(job.percent)+'٪');
                const stats = job.stats || {};
                if (kind === 'import') for (const key of ['created_users','updated_users','skipped_users','ignored_expired_users']) {
                    document.getElementById('um-stat-'+key).textContent = numeric(stats[key]);
                }
                if (job.state === 'completed') {
                    terminal = true;
                    const value = await request(base+id+'/result');
                    Swal.close(); return value.data;
                }
                if (['cancelled','failed','interrupted'].includes(job.state)) {
                    terminal = true;
                    const error = new Error(job.message || 'لم تكتمل العملية.');
                    error.cancelled = job.state === 'cancelled';error.stats = stats;
                    Swal.close();throw error;
                }
                await new Promise(resolve => setTimeout(resolve,1000));
            }
        } catch (error) {
            Swal.close();
            if (!terminal && id) error.message += ' مرجع المتابعة: '+id+'؛ قد تستمر المهمة على الخادم، لا تعِدها تلقائيًا.';
            throw error;
        } finally {
            active = false;
            if (terminal) sessionStorage.removeItem('userman-active-job');
        }
    }
    window.UsermanProgress = {
        fetch:payload => run('fetch',payload),
        import:payload => {payload.request_key = Array.from(crypto.getRandomValues(new Uint8Array(16)),x=>x.toString(16).padStart(2,'0')).join('');return run('import',payload);},
        resume:(id,kind) => run(kind,{},id)
    };
    document.addEventListener('DOMContentLoaded',async () => {
        let pending;
        try {pending=JSON.parse(sessionStorage.getItem('userman-active-job'));} catch (_) {return;}
        if (!pending || !/^[a-f0-9]{32}$/.test(pending.id) || !['fetch','import'].includes(pending.kind)) return;
        const choice=await Swal.fire({title:'متابعة عملية سابقة',text:'يمكن متابعة حالتها أو إلغاؤها؛ لن تُعاد عملية السحب أو الاستيراد.',
            showCancelButton:true,confirmButtonText:'متابعة الحالة',cancelButtonText:'لاحقًا'});
        if (!choice.isConfirmed) return;
        try {
            const data=await run(pending.kind,{},pending.id);
            if (pending.kind==='fetch') window.renderMikrotikPreview(data,'سحب مكتمل من الراوتر');
            else {
                const stats=data.stats || {};
                await Swal.fire({title:'ملخص الاستيراد',icon:'success',text:'أُنشئ '+Number(stats.created_users || 0).toLocaleString()+'، وحُدث '+Number(stats.updated_users || 0).toLocaleString()+'، وتُخطي '+Number(stats.skipped_users || 0).toLocaleString()+'.',
                    showCancelButton:true,confirmButtonText:'عرض النتائج',cancelButtonText:'البقاء هنا'}).then(v=>{
                    if (v.isConfirmed) window.location.href=data.target_type==='subscribers'?'/subscribers':'/vouchers';
                });
            }
        } catch (error) {Swal.fire(error.cancelled?'أُلغيت العملية':'تعذر المتابعة',error.message,error.cancelled?'info':'error');}
    });
})();

/* Durable operation results. Queue admission and NAS confirmation are separate. */
(() => {
    'use strict';
    window.newSessionOperationId = () => crypto.randomUUID().replaceAll('-', '');
    window.watchSessionOperations = async value => {
        const operationSwal = window.createOperationNotification();
        const jobs = value.jobs || [value.application || value];
        const ids = [...new Set(jobs.map(j => j.job_id).filter(Boolean))];
        if (!ids.length) return;
        try { sessionStorage.setItem('max.pendingSessions',JSON.stringify({at:Date.now(),ids})); } catch (_) {}
        const until = Date.now() + 200000;
        let latest = jobs;
        const results = new Map();
        operationSwal.fire({title: 'جارٍ التحقق من الجلسات', text: value.message,
            allowOutsideClick: false, showConfirmButton: false,
            didOpen: () => Swal.showLoading()});
        try {
            while (Date.now() < until) {
                const pending = ids.filter(id => !results.has(id) || results.get(id).pending);
                // Bound concurrent status requests; completed jobs need no more reads.
                for (let offset=0; offset<pending.length; offset+=4) {
                    await Promise.all(pending.slice(offset,offset+4).map(async id => {
                        const response = await fetch('/api/actions/session-operation/' + encodeURIComponent(id), {cache: 'no-store'});
                        const data = await response.json();
                        if (!response.ok || !data.success) {
                            if ([403,404].includes(response.status)) {
                                try { sessionStorage.removeItem('max.pendingSessions'); } catch (_) {}
                            }
                            throw new Error(data.message || 'تعذر قراءة النتيجة.');
                        }
                        results.set(id,data);
                    }));
                }
                latest = ids.map(id => results.get(id));
                if (latest.every(j => !j.pending)) break;
                await new Promise(resolve => setTimeout(resolve, 1500));
            }
            const confirmed = latest.reduce((n,j) => n + (j.confirmed || 0),0);
            const preserved = latest.reduce((n,j) => n + (j.preserved || 0),0);
            const unknown = latest.reduce((n,j) => n + (j.unknown || 0),0);
            const complete = latest.every(j => ['completed','no_targets','allowed_only'].includes(j.state));
            if (latest.every(j => !j.pending)) {
                try { sessionStorage.removeItem('max.pendingSessions'); } catch (_) {}
            }
            await operationSwal.fire({icon: complete ? 'success' : 'warning',
                title: complete ? 'اكتمل التحقق' : 'التحقق غير مكتمل',
                text: `تأكد الفصل: ${confirmed}، أبقيت مسموحة: ${preserved}، غير مؤكدة: ${unknown}. ` + latest.map(j => j.message).join(' ')});
        } catch (error) {
            await operationSwal.fire({icon:'warning',title:'تعذر تأكيد النتيجة',text:error.message + ' لا تعِد الفصل عشوائيًا.'});
        }
    };
    document.addEventListener('DOMContentLoaded', () => {
        try {
            const value=JSON.parse(sessionStorage.getItem('max.pendingSessions') || 'null');
            if (value && Date.now()-value.at<360000 && Array.isArray(value.ids)) {
                const ids=value.ids.filter(id => /^[a-f0-9]{32}$/.test(id));
                if(ids.length) window.watchSessionOperations({jobs:ids.map(job_id => ({job_id})),message:'متابعة نتيجة طلب سابق؛ لن يُرسل فصل جديد.'});
            }
        } catch (_) {}
    });
})();

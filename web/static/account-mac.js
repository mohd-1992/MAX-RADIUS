/* Shared MAC allow-list editor. Account/session limits remain server-owned. */
(() => {
    'use strict';
    let opening = false;
    window.openAccountMacPolicy = async (kind, id) => {
        if (opening || !['subscriber', 'voucher'].includes(kind) || !/^\d+$/.test(String(id))) return;
        opening = true;
        const url = `/api/${kind === 'subscriber' ? 'subscribers' : 'vouchers'}/${id}/mac-policy`;
        const token = document.querySelector('meta[name="csrf-token"]')?.content;
        const operationId = Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join('');
        try {
            const response = await fetch(url);
            const data = await response.json();
            if (!response.ok || !data.success) throw new Error(data.message || 'تعذر تحميل قائمة الماكات.');
            const policy = data.policy;
            const content = document.createElement('div');
            content.className = 'text-right space-y-4';
            const style = document.createElement('style');
            style.textContent = `
                .wisp-modal-popup.wisp-mac-popup { width:780px!important;max-width:calc(100vw - 2rem)!important;padding:1.5rem!important; }
                .wisp-mac-popup .mac-address-row { display:grid!important;grid-template-columns:210px minmax(0,1fr) 48px;gap:.5rem;align-items:center; }
                .wisp-mac-popup .mac-address-row input {height:44px!important;min-height:44px!important;min-width:0;box-sizing:border-box;}
                .wisp-mac-popup .mac-address-row button {white-space:nowrap;}
                .wisp-mac-popup .mac-address-list {max-height:42vh;overflow-y:auto;scrollbar-width:thin;}
                @media(max-width:600px) {
                    .wisp-modal-popup.wisp-mac-popup {padding:1rem!important;}
                    .wisp-mac-popup .mac-address-row {grid-template-columns:minmax(0,1fr) 48px;}
                    .wisp-mac-popup .mac-note {grid-column:1/-1;grid-row:2;}
                }`;
            content.append(style);
            const label = document.createElement('label');
            label.className = 'flex items-center gap-3 rounded-xl border border-slate-700 bg-[#070b14] p-4 text-sm text-white cursor-pointer';
            const enabled = document.createElement('input');
            enabled.type = 'checkbox'; enabled.checked = policy.enabled;
            enabled.className = 'w-5 h-5 accent-blue-600';
            label.append(enabled, document.createTextNode('تقييد الدخول بالماكات المسجلة فقط'));
            const hint = document.createElement('p');
            hint.className = 'text-xs text-slate-400 leading-relaxed';
            hint.textContent = 'عند إيقاف التقييد يسمح بأي ماك. عند تفعيله يرفض الماك غير المسجل. حد الجلسات مستقل ويطبق دائمًا، حتى على الماكات المسجلة.';
            const rows = document.createElement('div');
            rows.className = 'mac-address-list space-y-3 p-1';
            const add = document.createElement('button');
            add.type = 'button'; add.className = 'rounded-xl border border-blue-500/30 bg-blue-600/20 px-4 py-2 text-sm text-blue-400 hover:bg-blue-600/30';
            add.textContent = 'إضافة عنوان MAC';
            const counter = document.createElement('p');
            counter.className = 'text-xs text-slate-400';
            const updateCount = () => { counter.textContent = `مسجل ${rows.children.length} عنوانًا، الحد الأقصى ${policy.max_addresses}`; add.disabled = rows.children.length >= policy.max_addresses; };
            function addRow(item = {}) {
                if (rows.children.length >= policy.max_addresses) return;
                const row = document.createElement('div');
                row.className = 'mac-address-row rounded-xl border border-slate-700 bg-[#070b14] p-3';
                const mac = document.createElement('input');
                mac.type = 'text'; mac.dir = 'ltr'; mac.value = item.mac || ''; mac.maxLength = 17;
                mac.placeholder = 'AA:BB:CC:DD:EE:FF'; mac.setAttribute('aria-label', 'عنوان MAC');
                mac.className = 'w-full min-w-0 rounded-lg border border-slate-700 bg-[#070b14] px-3 py-2 text-sm text-white font-mono focus:border-blue-500';
                const note = document.createElement('input');
                note.type = 'text'; note.value = item.note || ''; note.maxLength = 250;
                note.placeholder = 'ملاحظة عن الجهاز (اختياري)'; note.setAttribute('aria-label', 'ملاحظة الماك');
                note.className = 'mac-note w-full min-w-0 rounded-lg border border-slate-700 bg-[#070b14] px-3 py-2 text-sm text-white focus:border-blue-500';
                const remove = document.createElement('button');
                remove.type = 'button'; remove.className = 'rounded-lg text-rose-400 hover:text-rose-300 p-2';
                remove.textContent = 'حذف'; remove.setAttribute('aria-label', 'إزالة عنوان الماك');
                remove.onclick = () => { row.remove(); updateCount(); };
                row.append(mac, note, remove); rows.append(row); updateCount();
            }
            add.onclick = () => addRow();
            content.append(label, hint, counter, rows, add);
            policy.addresses.forEach(addRow); updateCount();
            const result = await Swal.fire({
                title: 'الماكات المسموحة', html: content, width: 780,
                customClass: {popup: 'wisp-modal-popup wisp-mac-popup'},
                showCancelButton: true, confirmButtonText: 'حفظ القائمة', cancelButtonText: 'إلغاء',
                showLoaderOnConfirm: true, allowEscapeKey: () => !Swal.isLoading(), allowOutsideClick: () => !Swal.isLoading(),
                preConfirm: async () => {
                    const addresses = Array.from(rows.children, row => {
                        const inputs = row.querySelectorAll('input');
                        return {mac: inputs[0].value.trim(), note: inputs[1].value.trim()};
                    });
                    const normalized = addresses.map(row => row.mac.toUpperCase().replaceAll('-', ':'));
                    if ((enabled.checked && !addresses.length) || normalized.some(mac => !/^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$/.test(mac)) || new Set(normalized).size !== normalized.length) {
                        Swal.showValidationMessage('أضف عناوين MAC صحيحة وغير مكررة. التقييد يتطلب عنوانًا واحدًا على الأقل.'); return false;
                    }
                    content.querySelectorAll('input,button').forEach(el => { el.disabled = true; });
                    try {
                        const saved = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': token || ''}, body: JSON.stringify({enabled: enabled.checked, addresses, revision: policy.revision, operation_id: operationId})});
                        const value = await saved.json();
                        if (!saved.ok || !value.success) throw new Error(value.message || 'تعذر تأكيد الحفظ.');
                        return value;
                    } catch (error) {
                        Swal.showValidationMessage(error.message || 'تعذر تأكيد الحفظ؛ راجع القائمة قبل إعادة المحاولة.'); return false;
                    } finally { content.querySelectorAll('input,button').forEach(el => { el.disabled = false; }); updateCount(); }
                }
            });
            if (result.isConfirmed) {
                if (result.value.pending) await window.watchSessionOperations(result.value);
                else await Swal.fire({icon: result.value.application_uncertain ? 'warning' : 'success', title: 'تم الحفظ', text: result.value.message});
                window.location.reload();
            }
        } catch (error) {
            await Swal.fire({icon: 'error', title: 'تعذر فتح القائمة', text: error.message});
        } finally { opening = false; }
    };
})();

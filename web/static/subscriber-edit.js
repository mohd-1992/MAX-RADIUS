/* Both entry points keep one request ID until a confirmed result. */
(() => {
    'use strict';
    let pageBusy = false;
    const submitButtons = () => document.querySelectorAll('form[data-subscriber-edit] button[type="submit"], button[form="editSubForm"]');
    const packageSelect = document.getElementById('editSubscriberPackage');
    if (packageSelect) {
        packageSelect.addEventListener('change', () => {
            const option = packageSelect.selectedOptions[0];
            const quota = Number(option.dataset.quota);
            const units = {days: 'يوم', hours: 'ساعة', minutes: 'دقيقة', months: 'شهر'};
            const validity = Number(option.dataset.validity);
            document.getElementById('editPackagePreview').textContent = packageSelect.value
                ? `حصة الدورة الجديدة: ${quota === 0 ? 'تحميل مفتوح' : `${quota} ميجابايت`}. الصلاحية: ${validity > 0 ? `${validity} ${units[option.dataset.unit] || ''}` : 'غير محددة'}.`
                : '';
        });
    }
    document.querySelectorAll('form[data-subscriber-edit]').forEach(form => {
        let busy = false;
        form.addEventListener('submit', async event => {
            event.preventDefault();
            if (busy || pageBusy || !form.reportValidity()) return;
            pageBusy = true;
            const isPackage = form.dataset.subscriberEdit === 'package';
            const selected = isPackage ? packageSelect.selectedOptions[0].textContent : '';
            const confirmation = await Swal.fire({
                title: isPackage ? 'تغيير الباقة وبدء دورة جديدة؟' : 'حفظ تعديل المشترك؟',
                text: isPackage
                    ? `الباقة: ${selected}. ${form.elements.enable_rollover.checked ? 'مع ترحيل المتبقي.' : 'دون ترحيل المتبقي.'} لا يشمل هذا الزر تعديلات النموذج الآخر.`
                    : 'يحفظ الاستهلاك وتاريخ الحساب. قد يتطلب تغيير هوية الدخول أو نوع الخدمة أو الربط إنهاء اتصالات هذا المشترك قبل تطبيقه.',
                icon: 'question', showCancelButton: true, confirmButtonText: 'تأكيد الحفظ', cancelButtonText: 'إلغاء'
            });
            if (!confirmation.isConfirmed) { pageBusy = false; return; }
            busy = true;
            submitButtons().forEach(button => { button.disabled = true; });
            try {
                const response = await fetch(form.action, {method: 'POST', body: new FormData(form)});
                const result = await response.json();
                if (!response.ok || !result.success) throw new Error(result.message || 'تعذر تأكيد نتيجة العملية.');
                await Swal.fire({icon: 'success', title: 'تم الحفظ', text: result.message});
                window.location.reload();
            } catch (error) {
                await Swal.fire({icon: 'error', title: 'لم يتأكد الحفظ', text: error.message || 'راجع حالة المشترك قبل إعادة الطلب.'});
            } finally {
                busy = false;
                pageBusy = false;
                submitButtons().forEach(button => { button.disabled = false; });
            }
        });
    });
})();

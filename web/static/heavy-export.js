/* PDF exports use the shared themed notification engine and remain cancellable. */
(() => {
    let active = false;
    document.addEventListener('click', async event => {
        const link = event.target.closest('a[href]');
        if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey) return;
        const url = new URL(link.href, location.href);
        if (url.origin !== location.origin ||
            !(url.pathname.endsWith('/sessions.pdf') ||
              (url.pathname.startsWith('/vouchers/print/') && url.searchParams.get('export') === 'pdf'))) return;
        // Standalone print pages without the application shell retain native download.
        if (typeof window.showToastNotification !== 'function') return;
        event.preventDefault();
        if (active) return;
        active = true;
        const releaseOperation = window.registerPendingOperation?.();
        const token = crypto.randomUUID().replaceAll('-', '');
        let finished = false;
        let cancelling = false;
        const notice = window.showToastNotification({
            title: 'تصدير PDF',
            text: 'جارٍ تجهيز ملف PDF كاملًا؛ يمكنك متابعة الصفحة أو إلغاء التصدير.',
            isLoading: true, duration: 0, dismissible: false,
            action: {label: 'إلغاء التصدير', onClick: async button => {
                if (finished || button.disabled) return;
                button.disabled = true;
                const message = notice.element.querySelector('.wisp-msg');
                try {
                    const result = await fetch(`/api/exports/${token}/cancel`, {
                        method: 'POST', headers: {'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content}
                    });
                    const data = await result.json();
                    if (finished) return;
                    message.textContent = data.message || 'تعذر إرسال الإلغاء؛ أعد المحاولة.';
                    cancelling = result.ok;
                    button.disabled = result.ok;
                } catch (_) {
                    if (!finished) {
                        message.textContent = 'تعذر إرسال الإلغاء؛ أعد المحاولة.';
                        button.disabled = false;
                    }
                }
            }}
        });
        let outcome;
        try {
            const response = await fetch(url, {headers: {'X-Max-Export': token}});
            if (!response.ok) {
                const data = await response.json().catch(() => ({}));
                const error = new Error(data.message || data.error || 'تعذر تجهيز الملف.');
                error.cancelled = cancelling && response.status === 409;
                throw error;
            }
            const blob = await response.blob();
            const disposition = response.headers.get('Content-Disposition') || '';
            const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i);
            const plain = disposition.match(/filename="?([^";]+)/i);
            const download = document.createElement('a');
            download.download = encoded ? decodeURIComponent(encoded[1]) : (plain ? plain[1] : 'export.pdf');
            download.href = URL.createObjectURL(blob);
            document.body.appendChild(download); download.click(); download.remove();
            setTimeout(() => URL.revokeObjectURL(download.href), 10000);
            outcome = {icon: 'success', title: 'تصدير PDF', text: 'اكتمل تجهيز الملف وبدأ تنزيله.'};
        } catch (error) {
            outcome = {icon: error.cancelled ? 'info' : 'error', title: 'تصدير PDF', text: error.message};
        } finally {
            finished = true; active = false;
            notice.close();
            if (releaseOperation) releaseOperation();
        }
        // Server errors are plain text, never notification HTML.
        const status = window.showToastNotification({icon: outcome.icon, title: outcome.title, text: ' ', duration: 5500});
        status.element.querySelector('.wisp-msg').textContent = outcome.text;
    });
})();

/* One notifier per invocation; no global ownership and no request replay. */
(() => {
    'use strict';
    const pending = new Set(), idleWaiters = new Set();
    window.registerPendingOperation = () => {
        const token = {};
        pending.add(token);
        return () => {
            pending.delete(token);
            if (!pending.size) {
                const ready = [...idleWaiters]; idleWaiters.clear();
                ready.forEach(resolve => resolve());
            }
        };
    };
    const whenIdle = async () => {
        // Let a synchronous handoff register its next-stage watcher first.
        await Promise.resolve();
        while (pending.size) await new Promise(resolve => idleWaiters.add(resolve));
    };
    window.createOperationNotification = () => {
        let waiting = null, release = null;
        const close = () => {
            if (waiting) waiting.close();
            waiting = null;
            if (release) release();
            release = null;
        };
        const escape = value => String(value ?? '').replace(/[&<>"']/g,
            c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
        const normalize = args => typeof args[0] === 'string'
            ? {title: args[0], text: args[1] || '', icon: args[2] || 'info'}
            : {...args[0]};
        const fire = (...args) => {
            const opts = normalize(args);
            if (opts.html && opts.html.nodeType === 1) opts.html = opts.html.outerHTML;
            const interactive = opts.showCancelButton || opts.input || opts.showDenyButton || opts.asModal;
            const loading = !interactive && (opts.isLoading ||
                (opts.didOpen && opts.didOpen.toString().includes('showLoading')));
            if (loading) {
                close();
                release = window.registerPendingOperation();
                waiting = window.showToastNotification({
                    title: escape(opts.title || ''),
                    html: opts.html || escape(opts.text || ''),
                    isLoading: true, duration: 0, dismissible: false
                });
                return Promise.resolve({isConfirmed: true});
            }
            // Confirmation is not a terminal result for an operation already running.
            if (interactive) return window.Swal.fire(opts);
            // Hold navigation until this result has had its normal display
            // delay, as well as all other pending operations/results.
            const finish = release;
            release = null;
            if (waiting) waiting.close();
            waiting = null;
            if (!interactive) {
                opts.title = escape(opts.title || '');
                if (!opts.html && opts.text) { opts.html = escape(opts.text); delete opts.text; }
            }
            return Promise.resolve(window.Swal.fire(opts)).then(async result => {
                if (finish) finish();
                await whenIdle();
                return result;
            }, error => { if (finish) finish(); throw error; });
        };
        return {
            fire, close,
            reloadWhenIdle: (delay=0) => setTimeout(async () => {
                await whenIdle(); window.location.reload();
            }, delay),
            follow: callback => { close(); return callback(); },
            uncertain: (...args) => {
                return fire({icon: 'warning', title: 'تعذر تأكيد النتيجة',
                    text: 'تعذر الاتصال بالخادم أو قراءة رده.' +
                        ' قد تستمر العملية في الخادم؛ تحقق من حالتها قبل إعادة تنفيذها.'});
            }
        };
    };
})();

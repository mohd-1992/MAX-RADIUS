/* Normalize existing UI components without submitting forms or calling APIs. */
(() => {
    let sequence = 0;
    const open = new Map();
    let previousOverflow;
    let lastTrigger;
    document.addEventListener('click', event => {
        lastTrigger = event.target.closest('button,a');
    }, true);
    const visible = element => element.getClientRects().length > 0 && getComputedStyle(element).visibility !== 'hidden';
    const focusable = modal => [...modal.querySelectorAll('button,a[href],input,select,textarea,[tabindex]')]
        .filter(element => !element.disabled && element.tabIndex >= 0 && visible(element));
    function enhance(root) {
        root.querySelectorAll('button[title],a[title]').forEach(element => {
            if (!element.hasAttribute('aria-label')) element.setAttribute('aria-label', element.title);
        });
        root.querySelectorAll('label:not([for])').forEach(label => {
            if (label.querySelector('input,select,textarea')) return;
            const fields = label.parentElement.querySelectorAll('input:not([type=hidden]),select,textarea');
            if (fields.length !== 1) return;
            const field = fields[0];
            if (!field.id) field.id = `max-ui-field-${++sequence}`;
            label.htmlFor = field.id;
        });
        root.querySelectorAll('div.fixed.inset-0').forEach(modal => {
            const close = [...modal.querySelectorAll('button[onclick]')].find(button => /(?:close|hide)\w*\(/i.test(button.getAttribute('onclick')));
            // Backdrops, job overlays and the sidebar are not editable dialogs.
            if (!close || !modal.querySelector('h2,h3,h4')) return;
            modal.classList.add('max-ui-modal');
            modal.setAttribute('role', 'dialog');
            modal.setAttribute('aria-modal', 'true');
            modal.tabIndex = -1;
            const heading = modal.querySelector('h2,h3,h4');
            if (!heading.id) heading.id = `max-ui-dialog-title-${++sequence}`;
            modal.setAttribute('aria-labelledby', heading.id);
            if (!close.textContent.trim() || close.querySelector('.fa-xmark,.fa-times')) close.setAttribute('aria-label', 'إغلاق النافذة');
        });
    }
    function sync() {
        document.querySelectorAll('.max-ui-modal').forEach(modal => {
            if (visible(modal) && !open.has(modal)) {
                if (!open.size) {
                    previousOverflow = document.body.style.overflow;
                    document.body.style.overflow = 'hidden';
                }
                let trigger = document.activeElement;
                if (trigger === document.body || !visible(trigger)) trigger = lastTrigger;
                if (trigger && !visible(trigger)) trigger = trigger.closest('#profileDropdownContainer')?.querySelector('button');
                open.set(modal, trigger);
                (focusable(modal)[0] || modal).focus({preventScroll: true});
            } else if (!visible(modal) && open.has(modal)) {
                const trigger = open.get(modal);
                open.delete(modal);
                if (!open.size) document.body.style.overflow = previousOverflow || '';
                if (trigger?.isConnected && visible(trigger)) trigger.focus({preventScroll: true});
            }
        });
    }
    document.addEventListener('keydown', event => {
        if (window.Swal?.isVisible?.()) return;
        const modal = [...open.keys()].filter(visible).at(-1);
        if (!modal) return;
        if (event.key === 'Escape') {
            const close = [...modal.querySelectorAll('button[onclick]')].find(button => /(?:close|hide)\w*\(/i.test(button.getAttribute('onclick')));
            if (close) { event.preventDefault(); event.stopPropagation(); close.click(); }
        } else if (event.key === 'Tab') {
            const targets = focusable(modal);
            const first = targets[0], last = targets.at(-1);
            if (!first) { event.preventDefault(); modal.focus(); }
            else if (event.shiftKey && (document.activeElement === first || !modal.contains(document.activeElement))) {
                event.preventDefault(); last.focus();
            } else if (!event.shiftKey && (document.activeElement === last || !modal.contains(document.activeElement))) {
                event.preventDefault(); first.focus();
            }
        }
    }, true);
    document.addEventListener('DOMContentLoaded', () => {
        enhance(document); sync();
        const observer = new MutationObserver(records => {
            records.filter(record => record.attributeName === 'title' && /^(BUTTON|A)$/.test(record.target.tagName))
                .forEach(record => record.target.setAttribute('aria-label', record.target.title));
            records.filter(record => record.type === 'childList').forEach(record => {
                record.addedNodes.forEach(node => { if (node.nodeType === 1) enhance(node); });
            });
            if (records.some(record => record.type === 'childList' || record.target.classList?.contains('max-ui-modal'))) sync();
        });
        observer.observe(document.body, {subtree: true, childList: true, attributes: true, attributeFilter: ['class','style','title']});
    });
})();

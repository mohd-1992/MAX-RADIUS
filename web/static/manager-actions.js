(() => {
    const token = () => document.querySelector('meta[name="csrf-token"]').content;
    window.managerRequestId = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), n=>n.toString(16).padStart(2,'0')).join('');
    const add = (form, name, value) => {
        let input = form.querySelector(`input[name="${name}"]`);
        if (!input) { input = document.createElement('input'); input.type='hidden'; input.name=name; form.appendChild(input); }
        input.value = value;
    };
    window.prepareManagerForm = form => {
        add(form, 'csrf_token', token());
        if (!form.querySelector('input[name="request_id"]')) add(form, 'request_id', managerRequestId());
    };
    document.addEventListener('submit', event => {
        const form=event.target;
        if (form.method.toLowerCase()==='post') {
            prepareManagerForm(form);
            if (form.dataset.sending==='1') {event.preventDefault();return;}
            if (!event.defaultPrevented) { form.dataset.sending='1'; form.querySelectorAll('[type="submit"]').forEach(b=>b.disabled=true); }
        }
    });
})();

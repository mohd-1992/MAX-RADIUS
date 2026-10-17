/* Server epoch anchor; the browser wall clock is never authoritative. */
(function (scope) {
    'use strict';
    function create(timestampMs, timezone, elapsedNow = () => performance.now()) {
        if (!Number.isFinite(timestampMs)) throw new Error('Invalid server time');
        if (typeof timezone !== 'string' || !timezone.trim()) throw new Error('Missing server timezone');
        const anchor = elapsedNow();
        const fixed = /^([+-])(\d{2}):?(\d{2})$/.exec(timezone);
        let format;
        if (fixed) {
            const minutes = Number(fixed[2]) * 60 + Number(fixed[3]);
            if (Number(fixed[2]) > 23 || Number(fixed[3]) > 59) throw new Error('Invalid timezone');
            const offset = minutes * 60000 * (fixed[1] === '-' ? -1 : 1);
            format = epoch => new Date(epoch + offset).toISOString().slice(0, 19).replace('T', ' ');
        } else {
            const formatter = new Intl.DateTimeFormat('en-CA', {
                timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit',
                hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'
            });
            format = epoch => {
                const parts = Object.fromEntries(formatter.formatToParts(new Date(epoch)).map(p => [p.type, p.value]));
                return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
            };
        }
        return {text() {
            const delta = elapsedNow() - anchor;
            if (!Number.isFinite(delta) || delta < 0 || delta > 120000) throw new Error('Stale server time');
            return format(timestampMs + delta);
        }};
    }
    scope.MaxServerClock = {create};
    if (typeof module !== 'undefined') module.exports = {create};
})(globalThis);

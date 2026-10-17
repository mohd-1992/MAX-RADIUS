"""Pure source evidence validation. Never infer an original date from uptime."""
import datetime
from services.import_state_service import parse_import_datetime


def cycle_accounting(user, actual):
    cycle = parse_import_datetime(user.get('last_renewed_at') or user.get('first_used_at'))
    if cycle is None:
        return actual
    return [a for a in actual if parse_import_datetime(a.get('acctstarttime')) >= cycle
            or not a.get('acctstoptime') or parse_import_datetime(a['acctstoptime']) > cycle]


def historical_evidence(user, actual, nas_aliases=None):
    """Return residual source totals, or a review reason without guessing a cut.

    Detailed sessions must cover the supplied current-cycle aggregate exactly.
    A shared session is replaced by its actual NAS row, never added to it.
    """
    aliases = nas_aliases or {}
    actual = cycle_accounting(user, actual)
    totals = tuple(int(user.get(k) or 0) for k in
                   ('upload_used_bytes', 'download_used_bytes', 'uptime_used_seconds'))
    if user.get('uptime_used_seconds') is None:
        totals = totals[:2] + (int(user.get('uptime_used_mins') or 0) * 60,)
    if min(totals) < 0:
        raise ValueError('Negative source accounting')
    first = parse_import_datetime(user.get('first_used_at'))
    expiry = parse_import_datetime(user.get('expires_at') or user.get('till_time'))
    cycle = parse_import_datetime(user.get('last_renewed_at')) or first
    if not any(totals) and not first and not expiry and not actual:
        return dict(review=None, residual=totals)
    if not first or not cycle or (expiry and expiry < first):
        return dict(review='missing_original_dates', residual=totals)
    if cycle < first or (expiry and cycle > expiry):
        return dict(review='invalid_source_cycle', residual=totals)
    sessions = user.get('source_sessions')
    if not isinstance(sessions, list) or not sessions:
        return dict(review='missing_session_evidence', residual=totals)
    seen, matched, sums, overlap = set(), set(), [0, 0, 0], [0, 0, 0]
    for s in sessions:
        start = parse_import_datetime(s.get('start'))
        stop = parse_import_datetime(s.get('stop'))
        nas = aliases.get(s.get('nas'), s.get('nas'))
        identity = (str(s.get('session_id') or ''), nas, start)
        values = tuple(int(s.get(k) or 0) for k in ('upload', 'download', 'uptime'))
        if (not all(identity) or not s.get('mac') or not s.get('ip') or min(values) < 0
                or identity in seen or not start or start < cycle - datetime.timedelta(seconds=1)
                or (stop and stop < start)
                or (s.get('username') and str(s['username']).casefold() != str(user.get('username', '')).casefold())):
            return dict(review='invalid_session_evidence', residual=totals)
        seen.add(identity)
        sums = [a + b for a, b in zip(sums, values)]
        candidates = [a for a in actual if str(a.get('acctsessionid')) == identity[0]
                      and a.get('nasipaddress') == nas
                      and str(a.get('callingstationid') or '').casefold() == str(s['mac']).casefold()
                      and a.get('framedipaddress') == s['ip']
                      and abs((parse_import_datetime(a.get('acctstarttime')) - start).total_seconds()) <= 1]
        if len(candidates) > 1:
            return dict(review='ambiguous_session_match', residual=totals)
        if candidates:
            a = candidates[0]
            counters = tuple(int(a.get(k) or 0) for k in
                             ('acctinputoctets', 'acctoutputoctets', 'acctsessiontime'))
            # Preserve the actual accounting row byte-for-byte. A crossing row
            # needs a baseline; this importer never manufactures one.
            if (a['radacctid'] in matched or any(x < y for x, y in zip(counters, values))
                    or parse_import_datetime(a['acctstarttime']) < cycle
                    or not a.get('acctstoptime')):
                return dict(review='unsettled_or_crossing_session', residual=totals)
            matched.add(a['radacctid'])
            overlap = [a + b for a, b in zip(overlap, values)]
        elif not stop:
            return dict(review='unsettled_source_session', residual=totals)
        else:
            # A closed source row alone does not prove that delayed target
            # Start/Stop packets cannot arrive later. Without an established
            # cutover fence, hold it for review instead of adding a positive
            # aggregate which a later real row could count again.
            return dict(review='unconfirmed_target_cutover', residual=totals)
    if tuple(sums) != totals:
        return dict(review='incomplete_cycle_history', residual=totals)
    if len(matched) != len(actual):
        return dict(review='unmatched_target_accounting', residual=totals)
    return dict(review=None, residual=tuple(a - b for a, b in zip(totals, overlap)))

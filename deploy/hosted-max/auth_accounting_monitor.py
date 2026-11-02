"""Read-only evidence evaluator for the explicitly selected hosted tenants.

No MAX application imports, SQL writes, RADIUS packets, repairs or scheduling.
Unknown/incomplete evidence never clears an existing incident.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path

TENANTS = frozenset(('max1', 'm'))
SYNTHETIC = frozenset(('Stale-Session-Timeout', 'Watchdog-Autoheal-Timeout',
    'Backup-Restored-Closed', 'Admin-Reset-CoA', 'NAS-Reboot'))
SCHEMA = 1


def number(value):
    if value is None:
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError, OverflowError):
        return None


def subject(value):
    """Stable incident identity without publishing the account name."""
    return hashlib.sha256(str(value).encode()).hexdigest()[:16]


def finding(code, area, key, message, severity='warning', **evidence):
    return dict(code=code, area=area, subject=subject(key), severity=severity,
                message=message, evidence=evidence)


def identity(row):
    return (row.get('user'), row.get('nas'), row.get('sid'), row.get('class'))


def same_nas_entry(row, live):
    if row.get('user') != live.get('user') or row.get('nas') != live.get('nas'):
        return False
    sid = str(live.get('sid') or '').lower().removeprefix('0x')
    if sid:
        return str(row.get('sid') or '').lower().removeprefix('0x') == sid
    # Hotspot active/print does not always expose Acct-Session-Id.
    mac = lambda x: str(x or '').upper().replace('-', ':')
    return bool(live.get('ip') and live.get('mac') and row.get('ip') == live['ip']
                and mac(row.get('mac')) == mac(live['mac']))


def assess(snapshot, previous=None):
    """Return evidence-backed anomalies, not a promise that all operations passed."""
    tenant = snapshot.get('tenant')
    if tenant not in TENANTS or snapshot.get('schema') != SCHEMA:
        raise ValueError('Unsupported target or snapshot schema')
    if previous and (previous.get('tenant') != tenant or
                     previous.get('instance') != snapshot.get('instance')):
        raise ValueError('Previous snapshot belongs to another installation')
    now = number(snapshot.get('epoch'))
    if now is None or not snapshot.get('instance'):
        raise ValueError('Missing observation time or installation identity')
    if previous and now <= previous.get('epoch', 0):
        raise ValueError('Replayed/out-of-order snapshot')
    coverage = snapshot.get('coverage', {})
    complete = lambda area: coverage.get(area, {}).get('complete') is True
    out = []
    if previous and now-previous['epoch']>900:
        out.append(finding('observation_gap','auth','gap',
            'توجد فجوة بين القراءات؛ لا يمكن تأكيد سلامة العمليات خلالها.',
            seconds=int(now-previous['epoch'])))
    for area in ('auth', 'sessions', 'accounts', 'operations', 'nas'):
        if coverage.get(area,{}).get('reason') == 'deferred_until_deep_pass':
            continue
        if not complete(area):
            out.append(finding('observation_unavailable', area, area,
                'تعذرت تغطية جزء من الفحص؛ لا يثبت ذلك عطل الشبكة أو سلامتها.',
                reason=coverage.get(area, {}).get('reason', 'not_collected')))
    old_rows = {str(r['id']): r for r in (previous or {}).get('sessions', [])}
    sessions = snapshot.get('sessions', [])
    accounts = {r['user']: r for r in snapshot.get('accounts', [])}
    auth = snapshot.get('auth', [])
    ops = snapshot.get('operations', [])
    recent_ops = [r for r in ops if number(r.get('at')) is not None and
                  r['at'] >= (previous or {}).get('epoch', now-600)-120]
    affected = {r.get('user') for r in recent_ops if r.get('user')}
    interval = max(60, number(snapshot.get('interim_seconds')) or 900)

    if complete('sessions'):
        grouped = {}
        for r in sessions:
            rid = str(r['id'])
            old = old_rows.get(rid)
            if old:
                if any(r.get(k) != old.get(k) for k in ('user', 'nas', 'sid', 'uid', 'start')):
                    out.append(finding('accounting_identity_changed', 'sessions', rid,
                        'تغيرت هوية سجل محاسبة محفوظ.', 'critical', session_id=r['id']))
                for field in ('input', 'output', 'seconds'):
                    a, b = number(old.get(field)), number(r.get(field))
                    if a is not None and b is not None and b < a:
                        out.append(finding('counter_regressed', 'sessions', rid+field,
                            'انخفض عداد جلسة محفوظة؛ يحتاج مراجعة سبب الانخفاض.',
                            'critical', session_id=r['id'], field=field, before=a, after=b))
            start, stop = number(r.get('start')), number(r.get('stop'))
            if start is not None and stop is not None and stop < start:
                out.append(finding('invalid_session_time', 'sessions', rid,
                    'وقت نهاية الجلسة يسبق بدايتها.', session_id=r['id']))
            if start is not None and start > now + 120:
                out.append(finding('future_accounting_time', 'sessions', rid,
                    'وقت جلسة المحاسبة يتقدم على وقت القراءة.', session_id=r['id']))
            cycle = number(r.get('cycle'))
            if (cycle is not None and start is not None and start < cycle and
                    (stop is None or stop > cycle) and not r.get('has_baseline')):
                out.append(finding('missing_cycle_baseline', 'sessions', rid,
                    'جلسة تعبر التجديد دون خط أساس؛ الاستهلاك الحالي غير محسوم.',
                    session_id=r['id']))
            if stop is None:
                last = number(r.get('updated')) or start
                if last is not None and now-last > 2*interval+120:
                    out.append(finding('accounting_update_stale', 'sessions', rid,
                        'تأخرت تحديثات جلسة مفتوحة عن الفترة المتوقعة.',
                        session_id=r['id'], age_seconds=int(now-last)))
                if r.get('class') and r.get('sid'):
                    grouped.setdefault(identity(r), []).append(r['id'])
            if stop is not None and r.get('cause') in SYNTHETIC and now-stop > interval:
                out.append(finding('nonfinal_session_close', 'sessions', rid,
                    'إغلاق مهلة/إداري لا يثبت وصول Accounting-Stop نهائي.',
                    session_id=r['id'], cause=r['cause']))
        for key, ids in grouped.items():
            if len(ids) > 1:
                out.append(finding('possible_split_session', 'sessions', key,
                    'عدة سجلات مفتوحة تتشارك هوية الجلسة وClass؛ احتمال انقسام المحاسبة.',
                    session_ids=ids))
        # Only compare retained IDs when the collector explicitly checked them.
        checked = set(map(str, snapshot.get('checked_session_ids', [])))
        present = {str(r['id']) for r in sessions}
        for rid in checked & set(old_rows) - present:
            out.append(finding('accounting_row_missing', 'sessions', rid,
                'اختفى سجل محاسبة كان محفوظًا؛ قد يكون صيانة ويحتاج تفسيرًا.',
                session_id=rid))

    if complete('auth'):
        recent = [r for r in auth if number(r.get('at')) is not None and now-r['at'] <= 600]
        accepts = [r for r in recent if r.get('reply') == 'Access-Accept']
        rejects = [r for r in recent if r.get('reply') == 'Access-Reject']
        # Reasons are not guessed from a Reject packet or from today's account status.
        if len(recent) >= 20 and len(rejects)/len(recent) >= .8:
            out.append(finding('authentication_reject_surge', 'auth', 'ratio',
                'ارتفعت نسبة الرفض؛ قد تكون أسبابًا مشروعة وتحتاج تصنيفًا.',
                accepts=len(accepts), rejects=len(rejects), total=len(recent)))
        if complete('sessions'):
            for event in accepts:
                age = now-event['at']
                if age < 180:
                    continue
                matched = [r for r in sessions if r.get('user') == event.get('user')
                    and (not event.get('nas') or r.get('nas') == event['nas'])
                    and (not event.get('mac') or str(r.get('mac', '')).upper().replace('-', ':') ==
                         str(event['mac']).upper().replace('-', ':'))
                    and max(number(r.get('updated')) or 0,number(r.get('stop')) or 0,
                            number(r.get('start')) or 0) >= event['at']-120]
                if not matched:
                    out.append(finding('accept_without_accounting_evidence', 'auth', event['id'],
                        'قبول دخول بلا محاسبة مطابقة ضمن الأدلة المتاحة؛ لا يثبت اتصالًا فعليًا.',
                        auth_id=event['id']))
        if complete('accounts') and previous and complete('operations'):
            old_accounts = {r['user']: r for r in previous.get('accounts', [])}
            for event in accepts:
                account = accounts.get(event.get('user'))
                before = old_accounts.get(event.get('user'))
                if (not account or not before or event.get('user') in affected or
                        account.get('status') != before.get('status') or
                        event['at'] <= previous.get('epoch', 0)):
                    continue
                if account.get('status') in ('suspended', 'disabled', 'recharged'):
                    out.append(finding('accept_for_blocked_account', 'auth', event['id'],
                        'قبول لحساب بقي موقوفًا في القراءتين؛ يلزم تتبع توقيت العملية.',
                        'critical', auth_id=event['id'], status=account['status']))

    if complete('accounts'):
        for a in accounts.values():
            if a.get('uncertain'):
                out.append(finding('accounting_unknown', 'accounts', a['user'],
                    'حساب محاسبة دورته غير محسومة؛ لا تعتبر استهلاكه صفرًا.'))
            if number(a.get('loan')) is not None and a['loan'] < 0:
                out.append(finding('negative_loan', 'accounts', a['user'],
                    'رصيد السلفة سالب بصورة تحتاج مراجعة.'))
            # Negative extra quota/balance can be an authorized deduction or debt.
            opens = [r for r in sessions if r.get('user') == a['user'] and r.get('stop') is None]
            maximum = number(a.get('simultaneous'))
            if maximum is not None and maximum > 0 and len(opens) > maximum:
                out.append(finding('session_limit_candidate', 'accounts', a['user'],
                    'السجلات المفتوحة تتجاوز حد الحساب؛ لا يثبت ذلك تجاوزًا فعليًا على الراوتر.',
                    open_records=len(opens), allowed=maximum))
            if a.get('policy_pending_age', 0) > 300:
                out.append(finding('router_policy_pending', 'operations', a['user'],
                    'تحديث حدود الراوتر بعد عملية محفوظة ما زال معلقًا.'))
        # Discover silent inconsistencies from state changes, even with empty logs.
        if previous and complete('operations'):
            before_accounts={a['user']:a for a in previous.get('accounts',[])}
            migration=any(o.get('action') in ('MIGRATION_EXECUTED','FACTORY_RESET') for o in recent_ops)
            for a in accounts.values():
                b=before_accounts.get(a['user'])
                if not b:continue
                if a.get('cycle') is not None and b.get('cycle') is not None and a['cycle'] < b['cycle']:
                    out.append(finding('cycle_moved_backwards','accounts',a['user'],
                        'رجع تاريخ الدورة للخلف؛ يحتاج تفسيرًا حتى لو لم يظهر خطأ بالسجل.'))
                if a.get('cycle') == b.get('cycle'):
                    x,y=number(b.get('used_bytes')),number(a.get('used_bytes'))
                    if x is not None and y is not None and y < x:
                        out.append(finding('cycle_consumption_regressed','accounts',a['user'],
                            'انخفض استهلاك الدورة دون بدء دورة جديدة؛ يحتاج مراجعة.',
                            'critical',before_bytes=x,after_bytes=y))
                changed=[k for k in ('balance_cents','quota_mb','extra_mb','loan','cycle','package_id')
                         if a.get(k)!=b.get(k)]
                if changed and a['user'] not in affected and not migration:
                    out.append(finding('unexplained_entitlement_change','accounts',a['user'],
                        'تغير المال أو استحقاق العقد دون أثر عملية مرتبط ضمن الأدلة المتاحة؛ اشتباه للمراجعة.',
                        fields=changed))
                for op in recent_ops:
                    if (op.get('kind')=='audit' and op.get('user')==a['user'] and
                            op.get('action') in ('RENEW_PACKAGE','CHANGE_PACKAGE') and
                            now-op['at']>180 and op['at']>previous['epoch'] and
                            a.get('cycle')==b.get('cycle')):
                        out.append(finding('operation_without_cycle_transition','operations',op['id'],
                            'يوجد أثر تجديد/تغيير باقة دون انتقال دورة العقد في القراءتين؛ يحتاج تتبع النتيجة.',
                            operation_id=op['id']))
    if complete('operations'):
        invoice_keys, first_activations = {}, {}
        for op in ops:
            if op.get('kind') == 'invoice' and op.get('number'):
                invoice_keys.setdefault(op['number'], set()).add(op['id'])
            if op.get('kind') == 'sale' and op.get('first_activation') is not None:
                first_activations.setdefault(op['first_activation'], set()).add(op['id'])
        for key, ids in invoice_keys.items():
            if len(ids) > 1:
                out.append(finding('duplicate_invoice_number', 'operations', key,
                    'رقم فاتورة مكرر في سجلات مختلفة.', 'critical', invoice_ids=sorted(ids)))
        for key, ids in first_activations.items():
            if len(ids) > 1:
                out.append(finding('duplicate_first_activation_sale', 'operations', key,
                    'تكرر بيع التفعيل الأول للكرت؛ التجديدات العادية مستثناة.',
                    'critical', sale_ids=sorted(ids)))

    if complete('nas') and complete('sessions'):
        live_rows = [r for r in snapshot.get('nas_sessions', []) if r.get('radius') is True]
        for live in live_rows:
            matches = [r for r in sessions if r.get('stop') is None and same_nas_entry(r, live)]
            key = (live.get('nas'), live.get('kind'), live.get('user'), live.get('ip'), live.get('sid'))
            if not matches and (number(live.get('uptime')) or 0) > 180:
                out.append(finding('live_without_open_accounting', 'nas', key,
                    'اتصال RADIUS فعلي لا يقابله سجل محاسبة مفتوح مطابِق.'))
            if len(matches) > 1:
                out.append(finding('ambiguous_nas_accounting_match', 'nas', key,
                    'اتصال الراوتر يطابق أكثر من سجل؛ لا تدمج السجلات تلقائيًا.'))
            account = accounts.get(live.get('user'))
            if account is None and complete('accounts'):
                out.append(finding('live_without_contract','nas',key,
                    'اتصال RADIUS فعلي دون عقد مطابق ضمن الحسابات المقروءة؛ يحتاج تفسيرًا.'))
            if account and account.get('status') in ('suspended','disabled','recharged'):
                out.append(finding('blocked_account_still_online', 'nas', key,
                    'حساب موقوف ما زال متصلًا؛ يحتاج مراجعة طلب الفصل وتوقيته.'))
            if account and number(account.get('expires')) is not None and now-account['expires']>180:
                out.append(finding('expired_account_still_online','nas',key,
                    'اتصال RADIUS ما زال قائمًا بعد انتهاء صلاحية الحساب؛ يحتاج مراجعة التنفيذ.'))
            if account and number(account.get('used_bytes')) is not None:
                cap=number(account.get('quota_mb'));extra=number(account.get('extra_mb')) or 0
                if cap is not None and cap>0 and account['used_bytes']>(cap+extra)*1048576:
                    out.append(finding('quota_exhausted_still_online','nas',key,
                        'اتصال قائم مع استهلاك دورة محسوم تجاوز الاستحقاق؛ يحتاج مراجعة توقيت الإنفاذ.'))
            for match in matches:
                for field in ('input','output'):
                    sql_value,live_value=number(match.get(field)),number(live.get(field))
                    if sql_value is not None and live_value is not None and sql_value>live_value:
                        out.append(finding('nas_sql_counter_disagreement','nas',str(match['id'])+field,
                            'عداد SQL أعلى من عداد الاتصال المطابق في الراوتر؛ قد تكون هوية الجلسة أو العدادات غير متسقة.',
                            session_id=match['id'],field=field))
        for r in sessions:
            last = number(r.get('updated')) or number(r.get('start')) or now
            if r.get('stop') is None and now-last > 2*interval+120 and not any(
                    same_nas_entry(r, live) for live in live_rows):
                out.append(finding('open_accounting_absent_on_nas', 'nas', r['id'],
                    'سجل محاسبة متأخر ومفتوح لكن الاتصال غير موجود في قراءة الراوتر.'))
    return out


def advance(snapshot, state=None):
    """Deduplicate alerts; require two complete, separate observations to clear."""
    state = state or {'schema': SCHEMA, 'tenant': snapshot['tenant'], 'incidents': {}}
    if state.get('schema') != SCHEMA or state.get('tenant') != snapshot.get('tenant'):
        raise ValueError('State belongs to another target or schema')
    previous = state.get('previous')
    found = assess(snapshot, previous)
    current = {}
    notifications = []
    old = state.get('incidents', {})
    for item in found:
        key = item['code']+':'+item['subject']
        before = old.get(key)
        incident = dict(item, first_seen=before['first_seen'] if before else snapshot['epoch'],
                        last_seen=snapshot['epoch'], observations=(before or {}).get('observations',0)+1,
                        clear_observations=0)
        current[key] = incident
        if not before or before['severity'] != incident['severity']:
            notifications.append(dict(incident, event='changed' if before else 'opened',
                                      baseline=previous is None))
    coverage = snapshot.get('coverage', {})
    # A failed/deferred NAS pass, query failure, truncation or blind interval cannot recover an incident.
    continuous = previous is not None and snapshot['epoch']-previous['epoch'] <= 900
    for key, item in old.items():
        if key in current:
            continue
        historical = item['code'] in ('counter_regressed','accounting_identity_changed',
            'accounting_row_missing','invalid_session_time','duplicate_invoice_number',
            'duplicate_first_activation_sale','accept_for_blocked_account',
            'cycle_moved_backwards','cycle_consumption_regressed',
            'unexplained_entitlement_change','operation_without_cycle_transition')
        if historical:
            current[key]=dict(item,requires_review=True)
            continue
        good = continuous and coverage.get(item['area'], {}).get('complete') is True
        if item['code'] != 'observation_unavailable':
            # Cross-source conditions require all sources to remain visible.
            good = good and all(coverage.get(a,{}).get('complete') is True
                               for a in ('auth','sessions','accounts','operations','nas'))
        deferred = coverage.get('nas',{}).get('reason') == 'deferred_until_deep_pass'
        clear = item.get('clear_observations',0)+1 if good else item.get('clear_observations',0) if deferred and continuous else 0
        if clear >= 2:
            notifications.append(dict(item,event='recovered',recovered_at=snapshot['epoch']))
        else:
            current[key] = dict(item,clear_observations=clear)
    saved_snapshot=json.loads(json.dumps(snapshot))
    if previous:
        for area,field in (('sessions','sessions'),('accounts','accounts')):
            if coverage.get(area,{}).get('complete') is not True:
                saved_snapshot[field]=json.loads(json.dumps(previous.get(field,[])))
    new_state = dict(schema=SCHEMA, tenant=snapshot['tenant'], previous=saved_snapshot,
                     incidents=current)
    report = dict(schema=SCHEMA, tenant=snapshot['tenant'], epoch=snapshot['epoch'],
                  baseline=previous is None, scope='authentication-accounting-operations-only',
                  coverage=coverage, findings=found, notifications=notifications,
                  open_incidents=len(current),
                  counts={name:len(snapshot.get(name,[])) for name in
                          ('auth','sessions','accounts','operations','nas_sessions')})
    return new_state, report


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+'.tmp')
    fd = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(value,out,ensure_ascii=False,indent=2)
            out.flush();os.fsync(out.fileno())
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--snapshot', required=True)
    p.add_argument('--state', required=True)
    p.add_argument('--report', required=True)
    args=p.parse_args()
    path=Path(args.state)
    state=json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
    new_state,report=advance(json.loads(Path(args.snapshot).read_text(encoding='utf-8')),state)
    atomic_json(args.report,report)
    atomic_json(path,new_state)
    print(json.dumps({k:report[k] for k in ('tenant','epoch','baseline','counts','open_incidents')},ensure_ascii=True))


if __name__=='__main__':
    main()

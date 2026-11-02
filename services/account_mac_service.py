"""Explicit optional MAC lists, independently of session limits and accounting."""
import hashlib
import json
import re
from contextlib import nullcontext

from database.db import db_session, adapt_query, is_mysql_conn

TABLES = {'subscriber': 'wisp_subscribers', 'voucher': 'wisp_vouchers'}
from core.mac_policy import MAX_ADDRESSES, normalize_mac, validate_policy






def _query(conn, sql, params=(), many=False):
    with conn.cursor() as cur:
        cur.execute(adapt_query(sql, conn), params)
        return cur.fetchall() if many else cur.fetchone()


def initialize_mac_policy(conn, kind, ident, username, explicit_mac=None):
    """New accounts are unrestricted; a traffic-observed bound_mac is not a rule."""
    if _query(conn, 'SELECT 1 FROM wisp_account_mac_policies WHERE account_type=? AND account_id=?', (kind, ident)):
        return
    checks = _query(conn, "SELECT value,op FROM radcheck WHERE username=? AND attribute='Calling-Station-Id'", (username,), many=True)
    if len(checks) > 1 or any(v['op'] != '==' for v in checks):
        raise ValueError('قيد MAC قديم غير قياسي يحتاج مراجعة قبل المزامنة.')
    address = normalize_mac(explicit_mac) if explicit_mac else normalize_mac(checks[0]['value']) if checks else None
    with conn.cursor() as cur:
        cur.execute(adapt_query('INSERT INTO wisp_account_mac_policies(account_type,account_id,enabled) VALUES(?,?,?)', conn), (kind, ident, int(address is not None)))
        if address:
            cur.execute(adapt_query("INSERT INTO wisp_account_mac_addresses(account_type,account_id,mac,note) VALUES(?,?,?,'')", conn), (kind, ident, address))


def reject_legacy_mac_change(kind, row, proposed, conn=None):
    field = 'mac_binding' if kind == 'subscriber' else 'bound_mac'
    norm = lambda value: str(value or '').strip().upper().replace('-', ':')
    if norm(proposed) == norm(row.get(field)):
        return
    if conn is None:
        with db_session() as connection:
            return reject_legacy_mac_change(kind, row, proposed, connection)
    if _query(conn, 'SELECT 1 FROM wisp_account_mac_policies WHERE account_type=? AND account_id=?', (kind, row['id'])):
        raise ValueError('استخدم نافذة الماكات المسموحة لتعديل القيد؛ لم تتغير القائمة.')


def _account(conn, kind, ident, lock=False):
    if kind not in TABLES:
        raise ValueError('نوع الحساب غير صالح.')
    suffix = ' FOR UPDATE' if lock and is_mysql_conn(conn) else ''
    row = _query(conn, f'SELECT * FROM {TABLES[kind]} WHERE id=?' + suffix, (ident,))
    if not row:
        raise ValueError('الحساب غير موجود.')
    # Repeat ownership inside the locked transaction, not just at the HTTP boundary.
    from core.ownership import owner_id
    owner = owner_id()
    if owner is not None and (kind != 'voucher' or int(row.get('reseller_id') or 0) != owner):
        raise PermissionError('لا تملك صلاحية الوصول إلى هذا الحساب.')
    return row


def _policy(conn, kind, ident, row):
    stored = _query(conn, 'SELECT enabled FROM wisp_account_mac_policies WHERE account_type=? AND account_id=?', (kind, ident))
    if stored:
        rows = _query(conn, 'SELECT mac,note FROM wisp_account_mac_addresses WHERE account_type=? AND account_id=? ORDER BY mac', (kind, ident), many=True)
        state = {'enabled': bool(stored['enabled']), 'addresses': [dict(v) for v in rows]}
    else:
        # Only an existing RADIUS check proves a legacy restriction. bound_mac can
        # also hold a first-seen traffic address, which must not enable a new rule.
        checks = _query(conn, "SELECT value,op FROM radcheck WHERE username=? AND attribute='Calling-Station-Id' ORDER BY id", (row['username'],), many=True)
        if any(v['op'] != '==' for v in checks) or len(checks) > 1:
            raise ValueError('يوجد قيد MAC قديم غير قياسي؛ يحتاج مراجعة قبل استبداله.')
        rows = [{'mac': normalize_mac(v['value']), 'note': ''} for v in checks]
        state = {'enabled': bool(rows), 'addresses': rows}
    fingerprint = json.dumps({'state': state, 'username': row['username'], 'legacy': row.get('mac_binding' if kind == 'subscriber' else 'bound_mac')}, sort_keys=True, ensure_ascii=False)
    state['revision'] = hashlib.sha256(fingerprint.encode()).hexdigest()
    return state


def get_mac_policy(kind, ident):
    with db_session() as conn:
        row = _account(conn, kind, ident)
        return dict(_policy(conn, kind, ident, row), max_addresses=MAX_ADDRESSES)


def _receipt(conn, op, kind, ident, actor, fingerprint):
    previous = _query(conn, 'SELECT * FROM wisp_account_mac_receipts WHERE operation_id=?', (op,))
    if previous:
        if (previous['account_type'], int(previous['account_id']), previous['actor'], previous['fingerprint']) != (kind, int(ident), actor, fingerprint):
            raise ValueError('استُخدم هذا الطلب لعملية أخرى؛ أعد فتح النافذة.')
        return json.loads(previous['result'])


def save_mac_policy(kind, ident, data, actor='admin'):
    desired = validate_policy(data)
    op = str(data.get('operation_id') or '')
    if not re.fullmatch('[a-f0-9]{32}', op):
        raise ValueError('طلب الحفظ غير صالح؛ أعد فتح النافذة.')
    fingerprint = hashlib.sha256(json.dumps({'desired': desired, 'revision': data.get('revision')}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with db_session() as conn:
        row = _account(conn, kind, ident)
        previous = _receipt(conn, op, kind, ident, actor, fingerprint)
        if previous:
            return previous
        current = _policy(conn, kind, ident, row)
    if current['revision'] != data.get('revision'):
        raise ValueError('تغيرت بيانات الحساب أو قائمة الماكات؛ أعد فتح النافذة.')
    changed = current['enabled'] != desired['enabled'] or current['addresses'] != desired['addresses']
    # Notes alone never disconnect sessions. Changes in the effective allow-list
    # use the account login barrier; saving policy never needs a counter seal.
    effective = lambda v: [a['mac'] for a in v['addresses']] if v['enabled'] else None
    sensitive = effective(current) != effective(desired)
    from services.coa_queue_service import _disconnect_account_guard
    from services.session_control_service import prepare, policy_key, start_worker
    with (_disconnect_account_guard(row['username']) if sensitive else nullcontext()):
        with db_session() as conn:
            locked = _account(conn, kind, ident, lock=True)
            previous = _receipt(conn, op, kind, ident, actor, fingerprint)
            if previous:
                return previous
            before = _policy(conn, kind, ident, locked)
            if before['revision'] != current['revision']:
                raise ValueError('تغيرت البيانات أثناء العملية؛ أعد فتح النافذة.')
            with conn.cursor() as cur:
                if changed:
                    cur.execute(adapt_query('DELETE FROM wisp_account_mac_addresses WHERE account_type=? AND account_id=?', conn), (kind, ident))
                    cur.execute(adapt_query('DELETE FROM wisp_account_mac_policies WHERE account_type=? AND account_id=?', conn), (kind, ident))
                    cur.execute(adapt_query('INSERT INTO wisp_account_mac_policies(account_type,account_id,enabled) VALUES(?,?,?)', conn), (kind, ident, int(desired['enabled'])))
                    for item in desired['addresses']:
                        cur.execute(adapt_query('INSERT INTO wisp_account_mac_addresses(account_type,account_id,mac,note) VALUES(?,?,?,?)', conn), (kind, ident, item['mac'], item['note']))
                    cur.execute(adapt_query("DELETE FROM radcheck WHERE username=? AND attribute='Calling-Station-Id'", conn), (locked['username'],))
                    details = json.dumps({'mac_policy': {'before': {k: before[k] for k in desired}, 'after': desired}}, ensure_ascii=False)
                    cur.execute(adapt_query("INSERT INTO user_audit_logs(user_type,user_id,username,admin_name,action,change_details) VALUES(?,?,?,?,'EDIT_MAC_POLICY',?)", conn), (kind, ident, locked['username'], actor, details))
                result = {'success': True, 'message': 'تم حفظ قائمة الماكات دون تغيير حد الجلسات أو الاستهلاك.' if changed else 'لا توجد تغييرات جديدة لحفظها.'}
                if sensitive and desired['enabled']:
                    try:
                        result['application'] = prepare(kind, ident, locked['username'], actor, op, policy_key(desired))
                        result['job_id'] = result['application']['job_id']
                        result['pending'] = True
                        result['message'] = 'تم حفظ سياسة الماكات؛ جارٍ التحقق من فصل الجلسات المخالفة فقط.'
                    except (ValueError, RuntimeError, OSError):
                        result['application_uncertain'] = True
                        result['message'] = 'تم حفظ سياسة الماكات ومنع الدخول المخالف الجديد؛ تعذر تأكيد جدولة فصل الجلسات الحالية.'
                cur.execute(adapt_query('INSERT INTO wisp_account_mac_receipts(operation_id,account_type,account_id,actor,fingerprint,result) VALUES(?,?,?,?,?,?)', conn), (op, kind, ident, actor, fingerprint, json.dumps(result, ensure_ascii=False)))
    if result.get('job_id'):
        start_worker()
    return result

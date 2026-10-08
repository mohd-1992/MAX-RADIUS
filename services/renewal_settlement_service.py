"""Settle every session before a paid cycle transition; ACK is not final accounting."""
import hashlib
import inspect
import time
import uuid
from contextlib import contextmanager
from functools import wraps

from database.db import get_connection, is_mysql_conn, query_all, query_one

NON_FINAL_CAUSES = ('Stale-Session-Timeout', 'Watchdog-Autoheal-Timeout',
                    'Backup-Restored-Closed', 'Admin-Reset-CoA', 'NAS-Reboot')
WAIT_SECONDS = 12


class SettlementPending(RuntimeError):
    pass


def pending_sessions(username):
    marks = ','.join(['%s'] * len(NON_FINAL_CAUSES))
    return query_all(f"""SELECT radacctid,nasipaddress,acctsessionid,
        framedipaddress,callingstationid FROM radacct WHERE username=%s
        AND (acctstoptime IS NULL OR (acctterminatecause IN ({marks})
          AND acctstoptime>=COALESCE(
            (SELECT COALESCE(last_renewed_at,first_used_at,created_at) FROM wisp_subscribers WHERE username=%s LIMIT 1),
            (SELECT COALESCE(last_renewed_at,first_used_at,created_at) FROM wisp_vouchers WHERE username=%s LIMIT 1),
            '1970-01-01')))""",
        (username,) + NON_FINAL_CAUSES + (username, username))


def has_unaccounted_accept(username):
    # Access-Accept without final accounting must not vanish merely because it is old.
    marks = ','.join(['%s'] * len(NON_FINAL_CAUSES))
    return bool(query_one(f"""SELECT 1 AS pending FROM (
        SELECT nasipaddress,callingstationid,MAX(authdate) AS authdate FROM radpostauth
        WHERE username=%s AND reply='Access-Accept'
        AND authdate>=COALESCE(
            (SELECT created_at FROM wisp_subscribers WHERE username=%s LIMIT 1),
            (SELECT created_at FROM wisp_vouchers WHERE username=%s LIMIT 1), '1970-01-01')
        GROUP BY nasipaddress,callingstationid) p
        WHERE NOT EXISTS (SELECT 1 FROM radacct a WHERE a.username=%s
          AND (a.nasipaddress=p.nasipaddress OR COALESCE(p.nasipaddress,'')='')
          AND (COALESCE(a.callingstationid,'')=p.callingstationid OR COALESCE(p.callingstationid,'')='')
          AND a.acctstoptime>=p.authdate
          AND COALESCE(a.acctterminatecause,'') NOT IN ({marks})) LIMIT 1""",
        (username, username, username, username) + NON_FINAL_CAUSES))


def _disconnect(row, username):
    from services.coa_queue_service import _disconnect_single_session
    return _disconnect_single_session(username, row['nasipaddress'], row['acctsessionid'],
        row.get('framedipaddress'), row.get('callingstationid'), row['radacctid'],
        require_accounting_stop=True)


def wait_for_final_accounting(username):
    deadline = time.monotonic() + WAIT_SECONDS
    requested = set()
    while True:
        rows = pending_sessions(username)
        for row in rows:
            if row['radacctid'] not in requested:
                requested.add(row['radacctid'])
                _disconnect(row, username)
        rows = pending_sessions(username)
        reservation = query_one("""SELECT 1 AS pending FROM wisp_session_reservations
            WHERE username=%s AND radacctid IS NULL AND expires_at>NOW() LIMIT 1""", (username,))
        if not rows and not reservation and not has_unaccounted_accept(username):
            return
        if time.monotonic() >= deadline:
            raise SettlementPending('لم يكتمل وصول المحاسبة النهائية لجميع الجلسات؛ لم يتم التجديد أو الخصم. أعد المحاولة بعد وصول Accounting-Stop.')
        time.sleep(0.15)


@contextmanager
def settled_account(username):
    """A named lock serializes operations; a leased SQL guard blocks fresh logins."""
    conn = get_connection()
    token = uuid.uuid4().hex
    lock = 'max-cycle:' + hashlib.sha256(username.casefold().encode()).hexdigest()[:48]
    locked = False
    guarded = False
    try:
        if not is_mysql_conn(conn):
            raise SettlementPending('تسوية الجلسات تتطلب قاعدة MariaDB/MySQL.')
        with conn.cursor() as cur:
            cur.execute('SELECT GET_LOCK(%s,0) AS acquired', (lock,))
            locked = cur.fetchone()['acquired'] == 1
            if not locked:
                raise SettlementPending('يوجد تجديد أو شحن باقة جارٍ للحساب نفسه؛ انتظر اكتماله.')
            # Serialize with the existing atomic post-auth reservation function.
            cur.execute('UPDATE wisp_license_runtime_state SET id=id WHERE id=1')
            cur.execute("""INSERT INTO wisp_renewal_guards(username,token,expires_at)
                VALUES(%s,%s,NOW()+INTERVAL 5 MINUTE)
                ON DUPLICATE KEY UPDATE token=VALUES(token),expires_at=VALUES(expires_at)""", (username, token))
        conn.commit()
        guarded = True
        wait_for_final_accounting(username)
        yield
    finally:
        try:
            conn.rollback()
            with conn.cursor() as cur:
                if guarded:
                    cur.execute('DELETE FROM wisp_renewal_guards WHERE username=%s AND token=%s', (username, token))
                if locked:
                    cur.execute('SELECT RELEASE_LOCK(%s)', (lock,))
            conn.commit()
        finally:
            conn.close()


def settle_cycle_operation(fn):
    """Apply the same barrier to admin renewal/change and both portal package paths."""
    signature = inspect.signature(fn)

    @wraps(fn)
    def wrapped(*args, **kwargs):
        args_map = signature.bind(*args, **kwargs)
        args_map.apply_defaults()
        values = args_map.arguments
        if fn.__name__ == 'recharge_user_wallet_by_card' and str(values['recharge_type']).strip().lower() != 'package':
            return fn(*args, **kwargs)
        if 'entity_type' in values:
            from services.quick_action_service import get_target_entity
            entity, _ = get_target_entity(values['entity_type'], values['entity_id'])
        else:
            username = str(values['username'] or '').strip()
            entity = query_one('SELECT * FROM wisp_subscribers WHERE username=%s', (username,))
            if not entity and fn.__name__ == 'recharge_user_wallet_by_card':
                entity = query_one('SELECT * FROM wisp_vouchers WHERE username=%s', (username,))
        if not entity:
            return fn(*args, **kwargs)
        if fn.__name__ == 'renew_or_change_package' and entity.get('status') in ('disabled', 'suspended'):
            return fn(*args, **kwargs)
        if fn.__name__ == 'recharge_user_wallet_by_card':
            code = str(values['card_code'] or '').strip()
            card = query_one('SELECT username,status FROM wisp_vouchers WHERE username=%s OR pin_code=%s OR serial_number=%s LIMIT 1', (code, code, code))
            if (not card or card['status'] != 'unused' or
                    str(card['username']).casefold() == str(entity['username']).casefold() or
                    entity.get('status') in ('disabled', 'suspended', 'recharged')):
                return fn(*args, **kwargs)
        # Do not disconnect for an invalid or unaffordable requested package.
        new_id = values.get('new_package_id', values.get('new_pkg_id'))
        if new_id is not None:
            package = query_one('SELECT * FROM wisp_packages WHERE id=%s', (new_id,))
            if not package or (fn.__name__ == 'renew_or_change_package' and
                (not package.get('is_active') or float(entity.get('balance') or 0) < float(package.get('price') or 0))):
                return fn(*args, **kwargs)
        try:
            with settled_account(entity['username']):
                return fn(*args, **kwargs)
        except SettlementPending as exc:
            return False, str(exc)
    return wrapped

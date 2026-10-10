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
        from services.online_policy_service import checkpoint, preflight, view, intent, refresh_or_pending
        from services.autoheal_service import docker_api_request
        fleet, error = docker_api_request('GET', '/containers/json?all=1')
        import os
        own = next((r for r in (fleet or []) if r.get('Id','').startswith(os.environ.get('HOSTNAME','!'))), None)
        project = (own or {}).get('Labels',{}).get('com.docker.compose.project')
        core = next((r for r in (fleet or []) if project and
            r.get('Labels',{}).get('com.docker.compose.project') == project and
            r.get('Labels',{}).get('com.docker.compose.service') in ('radius_core','freeradius','core')), None)
        if error or not core or core.get('State') != 'running' or '(unhealthy)' in core.get('Status',''):
            from services.online_policy_service import fail
            fail()
        if query_one("SELECT 1 AS pending FROM wisp_session_reservations WHERE username=%s AND radacctid IS NULL AND expires_at>NOW() LIMIT 1",(username,)):
            from services.online_policy_service import fail
            fail()
        snapshot = checkpoint(username)
        preflight(snapshot)
        # A cycle timestamp has second precision; never overwrite a same-second cut.
        latest = query_one("SELECT last_renewed_at FROM wisp_subscribers WHERE username=%s UNION ALL SELECT last_renewed_at FROM wisp_vouchers WHERE username=%s LIMIT 1", (username, username))
        if latest and latest.get('last_renewed_at'):
            now = query_one('SELECT NOW() AS now')['now']
            if latest['last_renewed_at'] >= now:
                raise SettlementPending('\u0627\u0646\u062a\u0638\u0631 \u062b\u0627\u0646\u064a\u0629 \u0642\u0628\u0644 \u062a\u0643\u0631\u0627\u0631 \u0627\u0644\u0639\u0645\u0644\u064a\u0629.')
        intent(username)
        with view(snapshot):
            from services.online_policy_service import local
            local.operation_succeeded = False
            try:
                yield
            except Exception:
                from database.db import execute_write
                execute_write("UPDATE wisp_online_policy_outbox SET status='skipped',last_error='operation_failed' WHERE username=%s",(username,))
                raise
            if local.operation_succeeded:
                refresh_or_pending(username)
            else:
                from database.db import execute_write
                execute_write("UPDATE wisp_online_policy_outbox SET status='skipped',last_error='operation_rejected' WHERE username=%s",(username,))
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
            if not entity:
                entity = query_one('SELECT * FROM wisp_vouchers WHERE username=%s', (username,))
        if not entity:
            return fn(*args, **kwargs)
        if fn.__name__ == 'redeem_reward':
            reward = query_one('SELECT reward_type FROM wisp_loyalty_rewards WHERE id=%s', (values['reward_id'],))
            if not reward or reward['reward_type'] not in ('data_bonus_mb', 'validity_days'):
                return fn(*args, **kwargs)
        profile = values.get('voucher_profile')
        protected_profile_change = profile and (
            profile.get('username') != entity['username'] or
            profile.get('password', entity.get('password')) != entity.get('password') or
            str(profile.get('bound_mac') or '').upper() != str(entity.get('bound_mac') or '').upper() or
            profile.get('status') in ('suspended', 'disabled', 'recharged', 'unused'))
        if protected_profile_change and query_one('SELECT 1 AS live FROM radacct WHERE username=%s AND acctstoptime IS NULL LIMIT 1',(entity['username'],)):
            return False, '\u0644\u0627 \u064a\u0645\u0643\u0646 \u062a\u063a\u064a\u064a\u0631 \u0647\u0648\u064a\u0629 \u062d\u0633\u0627\u0628 \u0628\u062c\u0644\u0633\u0629 \u0645\u0641\u062a\u0648\u062d\u0629.'
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
                result = fn(*args, **kwargs)
                from services.online_policy_service import local
                local.operation_succeeded = bool(result[0])
            if result[0]:
                policy = query_one('SELECT status FROM wisp_online_policy_outbox WHERE username=%s',(entity['username'],))
                if policy and policy['status'] == 'pending':
                    return True, str(result[1]) + ' \u062a\u062d\u062f\u064a\u062b \u062d\u062f\u0648\u062f \u0627\u0644\u0631\u0627\u0648\u062a\u0631 \u0642\u064a\u062f \u0627\u0644\u0625\u0639\u0627\u062f\u0629\u061b \u0644\u0627 \u062a\u0643\u0631\u0631 \u0627\u0644\u062f\u0641\u0639.'
            return result
        except SettlementPending as exc:
            return False, str(exc)
    return wrapped

"""Small transactional lifecycle operations shared by UI and background jobs."""
import time
from contextlib import contextmanager
from decimal import Decimal
from database.db import get_connection, is_mysql_conn, adapt_query

ACCOUNTING_UNCERTAIN_REASON = 'تعذر تسوية حدود دورة المحاسبة'


def run_transaction(operation, connection=None):
    for attempt in range(3):
        conn = connection if connection is not None else get_connection()
        try:
            result = operation(conn)
            conn.commit()
            return result
        except Exception as exc:
            conn.rollback()
            code = exc.args[0] if exc.args else None
            if code not in (1205, 1213) or attempt == 2:
                raise
            time.sleep(.15 * (attempt + 1))
        finally:
            if connection is None:
                conn.close()


def sql(conn, statement, params=(), fetch=None):
    with conn.cursor() as cur:
        cur.execute(adapt_query(statement, conn), params)
        if fetch == 'one':
            return cur.fetchone()
        if fetch == 'all':
            return list(cur.fetchall())
        return cur.rowcount


@contextmanager
def job_lock(name, timeout=0):
    conn = get_connection()
    acquired = False
    try:
        if not is_mysql_conn(conn):
            raise RuntimeError('Lifecycle maintenance requires MySQL/MariaDB')
        row = sql(conn, 'SELECT GET_LOCK(CONCAT(DATABASE(), ?), ?) AS acquired', (':' + name, timeout), 'one')
        acquired = bool(row and row['acquired'] == 1)
        conn.commit()
        yield acquired
    finally:
        if acquired:
            sql(conn, 'SELECT RELEASE_LOCK(CONCAT(DATABASE(), ?))', (':' + name,), 'one')
        conn.close()


def lock_account(conn, kind, entity_id):
    table = 'wisp_subscribers' if kind == 'subscriber' else 'wisp_vouchers'
    row = sql(conn, f'SELECT *, CURRENT_TIMESTAMP AS checked_at FROM {table} WHERE id=? FOR UPDATE', (entity_id,), 'one')
    return table, row


def expiry_reason(conn, kind, row):
    if not row or row['status'] not in ('active', 'used'):
        return None
    if row.get('expires_at') and row['expires_at'] <= row['checked_at']:
        return 'تم استهلاك مدة الصلاحية'
    pkg = sql(conn, 'SELECT * FROM wisp_packages WHERE id=?', (row['package_id'],), 'one') or {}
    base = row.get('snap_volume_quota_mb')
    base = pkg.get('volume_quota_mb') if base is None else base
    uptime = row.get('snap_uptime_limit_mins') if kind == 'voucher' else pkg.get('uptime_limit_mins')
    uptime = pkg.get('uptime_limit_mins') if uptime is None else uptime
    if not (base or uptime):
        return None
    cycle = row.get('last_renewed_at')
    if cycle and sql(conn, '''SELECT a.radacctid FROM radacct a
        LEFT JOIN wisp_session_baselines b ON b.radacctid=a.radacctid AND b.renewed_at=?
        WHERE a.username=? AND a.acctstarttime<?
        AND (a.acctstoptime IS NULL OR a.acctstoptime>?)
        AND b.radacctid IS NULL LIMIT 1''', (cycle,row['username'],cycle,cycle),'one'):
        return ACCOUNTING_UNCERTAIN_REASON
    totals = sql(conn, '''SELECT
        COALESCE(SUM(CASE WHEN b.radacctid IS NOT NULL THEN
            GREATEST(0,CAST(COALESCE(a.acctinputoctets,0) AS SIGNED)-CAST(b.baseline_input_bytes AS SIGNED))+
            GREATEST(0,CAST(COALESCE(a.acctoutputoctets,0) AS SIGNED)-CAST(b.baseline_output_bytes AS SIGNED))
            ELSE COALESCE(a.acctinputoctets,0)+COALESCE(a.acctoutputoctets,0) END),0) bytes,
        COALESCE(SUM(CASE WHEN b.radacctid IS NOT NULL THEN
            GREATEST(0,CAST(COALESCE(a.acctsessiontime,0) AS SIGNED)-CAST(b.baseline_seconds AS SIGNED))
            ELSE COALESCE(a.acctsessiontime,0) END),0) seconds
        FROM radacct a LEFT JOIN wisp_session_baselines b ON b.radacctid=a.radacctid AND b.renewed_at=?
        WHERE a.username=? AND (? IS NULL OR a.acctstarttime>=? OR b.radacctid IS NOT NULL)''',
        (cycle, row['username'], cycle, cycle), 'one')
    if base and int(totals['bytes']) >= (Decimal(str(base)) + Decimal(str(row.get('extra_quota_mb') or 0))) * 1048576:
        return 'تم استهلاك رصيد البيانات بالكامل'
    if uptime and int(totals['seconds']) >= int(uptime) * 60:
        return 'تم استهلاك رصيد الوقت المسموح للباقة'
    return None


def sweep_expired_accounts():
    from database.db import query_all
    from services.coa_queue_service import enqueue_disconnect
    from services.factory_reset_service import factory_reset_active
    if factory_reset_active():
        return False
    with job_lock('expiry-sweep') as acquired:
        if not acquired or factory_reset_active():
            return False
        for kind, table in [('voucher','wisp_vouchers'), ('subscriber','wisp_subscribers')]:
            if factory_reset_active():
                return False
            # Read-only shortlist: only per-account transactions hold row locks.
            states = "('expired','disabled','suspended','recharged')" if kind=='voucher' else "('expired','disabled','suspended')"
            rows = query_all(f"SELECT id FROM {table} t WHERE status IN ('active','used') OR (status IN {states} AND (EXISTS (SELECT 1 FROM radcheck c WHERE c.username=t.username AND c.attribute='Cleartext-Password') OR EXISTS (SELECT 1 FROM radacct a WHERE a.username=t.username AND a.acctstoptime IS NULL))) ORDER BY id")
            for candidate in rows:
                # Finish the previous account transaction and its disconnect
                # enqueue before yielding to the accepted reset request.
                if factory_reset_active():
                    return False
                def process(conn):
                    _, row = lock_account(conn, kind, candidate['id'])
                    if not row:
                        return None
                    reason = expiry_reason(conn, kind, row)
                    if reason:
                        next_status = 'suspended' if reason == ACCOUNTING_UNCERTAIN_REASON else 'expired'
                        if kind == 'voucher':
                            sql(conn, f"UPDATE {table} SET status=?,expire_reason=? WHERE id=?", (next_status,reason,row['id']))
                        else:
                            sql(conn, f"UPDATE {table} SET status=? WHERE id=?", (next_status,row['id']))
                        if next_status == 'suspended':
                            sql(conn, f"UPDATE {table} SET pause_reason=? WHERE id=?", (reason,row['id']))
                        row['status'] = next_status
                    if row['status'] in ('expired','disabled','suspended','recharged'):
                        sql(conn, "DELETE FROM radcheck WHERE username=? AND attribute='Cleartext-Password'", (row['username'],))
                        return row
                    return None
                expired = run_transaction(process)
                if expired:
                    sessions = query_all('SELECT radacctid,acctsessionid,nasipaddress FROM radacct WHERE username=? AND acctstoptime IS NULL', (expired['username'],))
                    for session in sessions:
                        ok, msg = enqueue_disconnect(expired['username'], nas_ip=session['nasipaddress'], session_id=session['acctsessionid'], radacctid=session['radacctid'], reason='Account Expired / Quota Depleted', lifecycle_kind=kind, lifecycle_id=expired['id'], lifecycle_cycle=str(expired.get('last_renewed_at') or ''))
                        if not ok:
                            raise RuntimeError(msg)
        return True


def lifecycle_disconnect_still_required(task):
    kind = task.get('lifecycle_kind')
    if not kind:
        return True  # An explicit manual disconnect remains an explicit disconnect.
    table = 'wisp_subscribers' if kind == 'subscriber' else 'wisp_vouchers'
    from database.db import query_one
    row = query_one(f'SELECT status,last_renewed_at FROM {table} WHERE id=? AND username=?', (task['lifecycle_id'],task['username']))
    return bool(row and str(row.get('last_renewed_at') or '') == task.get('lifecycle_cycle','') and row['status'] in ('expired','disabled','suspended','recharged'))


def delete_account(kind, entity_id, expected_expired=False, delete_acct=False, min_age_days=0, expected_cycle=None, expected_reason=None, connection=None):
    """Preserve money/history. Open sessions defer deletion and prevent new auth."""
    def operation(conn):
        table, row = lock_account(conn, kind, entity_id)
        if not row:
            return {'deleted':False,'missing':True}
        if expected_expired:
            if (row['status'] != 'expired' or (expected_cycle is not None and str(row.get('last_renewed_at') or '') != expected_cycle) or row.get('expire_reason') != expected_reason):
                return {'deleted':False,'skipped':True}
            if min_age_days:
                old = sql(conn, f'SELECT 1 ok FROM {table} WHERE id=? AND expires_at < NOW()-INTERVAL ? DAY', (entity_id,min_age_days),'one')
                if not old:
                    return {'deleted':False,'skipped':True}
        sessions = sql(conn, "SELECT radacctid,acctsessionid,nasipaddress FROM radacct WHERE username=? AND (acctstoptime IS NULL OR acctterminatecause IN ('Stale-Session-Timeout','Watchdog-Autoheal-Timeout','Backup-Restored-Closed')) ORDER BY radacctid FOR UPDATE", (row['username'],),'all')
        reserved = sql(conn, 'SELECT id FROM wisp_session_reservations WHERE username=? AND expires_at>NOW() FOR UPDATE', (row['username'],), 'all')
        if sessions or reserved:
            if not expected_expired:
                sql(conn, f"UPDATE {table} SET status='suspended',pause_reason=? WHERE id=?",
                    ('إيقاف لحين إكمال حذف الحساب', entity_id))
            removed = sql(conn,"DELETE FROM radcheck WHERE username=? AND attribute='Cleartext-Password'",(row['username'],))
            return {'deleted':False,'pending':True,'username':row['username'],'sessions':sessions,'deleted_radcheck':removed,'cycle':str(row.get('last_renewed_at') or '')}
        removed = {}
        for radius_table in ('radcheck','radreply','radusergroup'):
            removed['deleted_' + radius_table] = sql(conn,f'DELETE FROM {radius_table} WHERE username=?',(row['username'],))
        if kind=='subscriber':
            sql(conn,'UPDATE wisp_invoices SET subscriber_id=NULL WHERE subscriber_id=?',(entity_id,))
        # Historical ledgers retain the original identity and are deliberately not deleted.
        sql(conn,f'DELETE FROM {table} WHERE id=?',(entity_id,))
        return dict(deleted=True, username=row['username'], **removed)
    result = run_transaction(operation, connection=connection)
    if result.get('pending'):
        from services.coa_queue_service import enqueue_disconnect
        failures = 0
        for session in result['sessions']:
            ok,_ = enqueue_disconnect(result['username'],nas_ip=session['nasipaddress'],session_id=session['acctsessionid'],radacctid=session['radacctid'],reason='Account deletion pending',lifecycle_kind=kind,lifecycle_id=entity_id,lifecycle_cycle=result['cycle'])
            failures += not ok
        result['queue_failures'] = failures
    if delete_acct and result.get('deleted'):
        # Only unreferenced closed history can be pruned by the protected history cleaner.
        result['history_preserved'] = True
    return result


def delete_packages(ids):
    ids = sorted(set(int(i) for i in ids))
    def operation(conn):
        pkgs=[]
        for i in ids:
            row=sql(conn,'SELECT id,name FROM wisp_packages WHERE id=? FOR UPDATE',(i,),'one')
            if not row:continue
            for table in ('wisp_subscribers','wisp_vouchers','wisp_voucher_batches'):
                if sql(conn,f'SELECT 1 ok FROM {table} WHERE package_id=? LIMIT 1',(i,),'one'):
                    raise ValueError('لا يمكن حذف باقة مرتبطة بحسابات أو دفعات؛ يمكن تعطيلها.')
            pkgs.append(row)
        for row in pkgs:
            for table in ('radgroupreply','radgroupcheck'):
                sql(conn,f'DELETE FROM {table} WHERE groupname=?',(row['name'],))
            sql(conn,'DELETE FROM wisp_packages WHERE id=?',(row['id'],))
        return len(pkgs)
    return run_transaction(operation)


def delete_voucher_batch(batch_id, admin_username):
    from database.db import query_all
    # Do not refund or delete a batch while a card may still be online.
    online = query_all("SELECT DISTINCT v.id FROM wisp_vouchers v JOIN radacct a ON a.username=v.username WHERE v.batch_id=? AND (a.acctstoptime IS NULL OR a.acctterminatecause IN ('Stale-Session-Timeout','Watchdog-Autoheal-Timeout','Backup-Restored-Closed'))",(batch_id,))
    if online:
        for row in online:delete_account('voucher',row['id'])
        raise ValueError('تم طلب فصل الجلسات ومنع دخولها؛ أعد الحذف بعد تأكيد إغلاقها. لم يُسترد أي مبلغ.')
    def operation(conn):
        batch=sql(conn,'SELECT * FROM wisp_voucher_batches WHERE id=? FOR UPDATE',(batch_id,),'one')
        if not batch:return False
        cards=sql(conn,'SELECT * FROM wisp_vouchers WHERE batch_id=? ORDER BY id FOR UPDATE',(batch_id,),'all')
        for row in cards:
            if sql(conn,"SELECT 1 ok FROM radacct WHERE username=? AND (acctstoptime IS NULL OR acctterminatecause IN ('Stale-Session-Timeout','Watchdog-Autoheal-Timeout','Backup-Restored-Closed')) LIMIT 1 FOR UPDATE",(row['username'],),'one') or sql(conn,'SELECT 1 ok FROM wisp_session_reservations WHERE username=? AND expires_at>NOW() LIMIT 1 FOR UPDATE',(row['username'],),'one'):
                raise ValueError('ظهرت جلسة جديدة؛ لم يتم حذف الدفعة أو استرداد قيمتها.')
        unused=sum(row['status']=='unused' for row in cards)
        reseller=batch.get('reseller_id')
        amount=Decimal(str(batch.get('cost') or Decimal(str(batch.get('price') or 0)) * Decimal('.8'))) * unused
        amount=amount.quantize(Decimal('.01'))
        if reseller and amount>0:
            manager=sql(conn,'SELECT * FROM wisp_managers WHERE id=? FOR UPDATE',(reseller,),'one')
            if manager:
                before=Decimal(str(manager.get('wallet_balance') or 0));after=before+amount
                sql(conn,'UPDATE wisp_managers SET wallet_balance=? WHERE id=?',(after,reseller))
                sql(conn,"INSERT INTO wisp_manager_invoices (invoice_number,manager_id,transaction_type,amount,payment_type,balance_before,balance_after,notes,is_voided,created_by) VALUES (?,?,'refund',?,'cash',?,?,?,0,?)",(f'BATCH-REFUND-{batch_id}',reseller,amount,before,after,f'استرداد حذف الدفعة {batch_id}',admin_username))
            else:
                old=sql(conn,'SELECT balance FROM wisp_resellers WHERE id=? FOR UPDATE',(reseller,),'one')
                if old:
                    after=Decimal(str(old['balance'] or 0))+amount
                    sql(conn,'UPDATE wisp_resellers SET balance=? WHERE id=?',(after,reseller))
                    sql(conn,"INSERT INTO wisp_reseller_transactions (reseller_id,type,amount,balance_after,description,reference_id) VALUES (?,'refund',?,?,?,?)",(reseller,amount,after,'استرداد حذف الدفعة',str(batch_id)))
        for row in cards:
            for table in ('radcheck','radreply','radusergroup'):
                sql(conn,f'DELETE FROM {table} WHERE username=?',(row['username'],))
        sql(conn,'DELETE FROM wisp_vouchers WHERE batch_id=?',(batch_id,))
        sql(conn,'DELETE FROM wisp_voucher_batches WHERE id=?',(batch_id,))
        return True
    return run_transaction(operation)

HISTORY_ELIGIBLE = "\n        a.acctstoptime IS NOT NULL\n        AND a.acctstoptime < NOW() - INTERVAL %s DAY\n        AND COALESCE(a.acctterminatecause, '') NOT IN\n            ('Stale-Session-Timeout', 'Watchdog-Autoheal-Timeout', 'Backup-Restored-Closed')\n        AND NOT EXISTS (\n            SELECT 1 FROM wisp_subscribers s WHERE s.username = a.username\n            AND (s.last_renewed_at IS NULL OR a.acctstarttime >= s.last_renewed_at\n                 OR EXISTS (SELECT 1 FROM wisp_session_baselines b WHERE b.radacctid = a.radacctid AND b.renewed_at = s.last_renewed_at))\n        )\n        AND NOT EXISTS (\n            SELECT 1 FROM wisp_vouchers v WHERE v.username = a.username\n            AND (v.last_renewed_at IS NULL OR a.acctstarttime >= v.last_renewed_at\n                 OR EXISTS (SELECT 1 FROM wisp_session_baselines b WHERE b.radacctid = a.radacctid AND b.renewed_at = v.last_renewed_at))\n        )\n    "

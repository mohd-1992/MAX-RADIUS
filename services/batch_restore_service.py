"""Atomic import of MAX batch workbooks; refuses retained accounting conflicts."""
import datetime
import secrets
from decimal import Decimal
from services.batch_export_service import SNAPSHOTS, validate_export_row


def validate_workbook(rows, import_type, default_reseller_id=None):
    from database.db import query_one
    if import_type != 'vouchers':
        return False, ['هذا الملف خاص بالكروت.'], []
    cleaned, seen = [], set()
    try:
        for raw in rows:
            row = validate_export_row(raw)
            username = str(row.get('username') or '').strip()
            if not username or username.lower() in seen:
                raise ValueError('اسم مستخدم فارغ أو مكرر في الملف.')
            seen.add(username.lower())
            pkg = query_one('SELECT * FROM wisp_packages WHERE name=?', (row['package'],))
            if not pkg:
                raise ValueError('إحدى باقات الملف غير موجودة؛ أضف الباقة المطابقة أولًا.')
            row.update(username=username, package_id=pkg['id'], reseller_id=default_reseller_id)
            cleaned.append(row)
        return True, [], cleaned
    except ValueError as exc:
        return False, [str(exc)], []


def restore_workbook(rows, batch_name=None, admin_username='admin'):
    from database.db import get_connection, is_mysql_conn, adapt_query
    from services.import_state_service import import_now, imported_radius_checks, historical_accounting_values
    conn = get_connection()
    cur = conn.cursor()
    mysql = is_mysql_conn(conn)
    try:
        if mysql:
            cur.execute('SET TRANSACTION ISOLATION LEVEL SERIALIZABLE')
            conn.begin()
            # Avoid first-activation financial triggers for historical summaries.
            cur.execute('SET @max_radius_import=1')
        else:
            cur.execute('BEGIN IMMEDIATE')
        def run(sql, args=()):
            cur.execute(adapt_query(sql, conn), args)
            return cur
        now = import_now(cur)
        validated = [validate_export_row(row) for row in rows]
        for row in validated:
            # Lock/range-lock all conflict sources before any insert. Never merge
            # retained NAS usage with a stale file or overwrite an existing card.
            for table in ('wisp_vouchers', 'wisp_subscribers', 'radcheck', 'radreply', 'radusergroup', 'radacct'):
                suffix = ' FOR UPDATE' if mysql else ''
                if run(f'SELECT username FROM {table} WHERE username=? LIMIT 1' + suffix,
                       (row['username'],)).fetchone():
                    raise ValueError('يوجد حساب أو سجل محاسبة محفوظ لأحد الكروت؛ أُلغي الاستيراد كاملًا لحماية الاستخدام الحالي.')
            if any(row[k] and row[k] > now for k in ('first_used_at', 'last_renewed_at', 'policy_frozen_at')):
                raise ValueError('تاريخ استخدام في المستقبل؛ أُلغي الاستيراد.')
        name = str(batch_name or validated[0]['batch_name'] or 'حزمة Excel').strip()
        number = 'XLS-' + secrets.token_hex(8)
        batch_fields = ('batch_number','name','package_id','reseller_id','card_count','prefix','pin_only',
                        'char_type','code_length','created_by','created_at', *(key[5:] for key in SNAPSHOTS))
        run('INSERT INTO wisp_voucher_batches (' + ','.join(batch_fields) + ') VALUES (' + ','.join('?' for _ in batch_fields) + ')',
            (number, name, rows[0]['package_id'], rows[0].get('reseller_id'), len(rows), '', 0, 'mixed', 8, admin_username, now,
             *(validated[0][key] for key in SNAPSHOTS)))
        batch_id = cur.lastrowid
        for row, source in zip(validated, rows):
            status = row['status']
            quota = Decimal(row['snap_volume_quota_mb'])
            uptime = Decimal(row['snap_uptime_limit_mins'])
            depleted = ((row['expires_at'] and row['expires_at'] <= now) or
                        (quota > 0 and Decimal(row['download_bytes']) + Decimal(row['upload_bytes']) >=
                         max(Decimal(0), quota + Decimal(row['extra_quota_mb'])) * 1048576) or
                        (uptime > 0 and Decimal(row['uptime_seconds']) >= uptime * 60))
            if status == 'active' and depleted:
                status = 'expired'
            fields = ('batch_id', 'package_id', 'reseller_id', 'username', 'password', 'pin_code', 'serial_number',
                      'status', 'bound_mac', 'pause_reason', 'first_used_at', 'last_renewed_at', 'expires_at',
                      'extra_quota_mb', 'policy_frozen_at', *SNAPSHOTS)
            values = (batch_id, source['package_id'], source.get('reseller_id'), row['username'], row['password'],
                      row['voucher_pin'], row['serial_number'], status, row['mac_binding'], row['pause_reason'],
                      row['first_used_at'], row['last_renewed_at'], row['expires_at'], row['extra_quota_mb'], row['policy_frozen_at'],
                      *(row[k] for k in SNAPSHOTS))
            run('INSERT INTO wisp_vouchers (' + ','.join(fields) + ') VALUES (' + ','.join('?' for _ in fields) + ')', values)
            state = dict(status=status, first_used_at=row['first_used_at'], last_renewed_at=row['last_renewed_at'],
                         expires_at=row['expires_at'], upload_bytes=int(row['upload_bytes']),
                         download_bytes=int(row['download_bytes']), uptime_seconds=int(row['uptime_seconds']))
            if any(state[k] for k in ('upload_bytes', 'download_bytes', 'uptime_seconds')):
                acct_fields = ('acctsessionid','acctuniqueid','username','realm','nasipaddress','nasportid','nasporttype',
                               'acctstarttime','acctupdatetime','acctstoptime','acctinterval','acctsessiontime','acctauthentic',
                               'connectinfo_start','connectinfo_stop','acctinputoctets','acctoutputoctets','acctinputgigawords',
                               'acctoutputgigawords','calledstationid','callingstationid','acctterminatecause','servicetype','framedprotocol','framedipaddress')
                history = list(historical_accounting_values(row['username'], state, now, row['mac_binding']))
                history[7] = state['last_renewed_at'] or state['first_used_at']
                run('INSERT INTO radacct (' + ','.join(acct_fields) + ') VALUES (' + ','.join('?' for _ in acct_fields) + ')', history)
            checks = imported_radius_checks(row['username'], row['password'], state)
            if status == 'recharged':
                checks = [(row['username'], 'Auth-Type', ':=', 'Reject')]
            if row['mac_binding'] and status not in ('recharged', 'expired', 'disabled', 'suspended'):
                checks.append((row['username'], 'Calling-Station-Id', '==', row['mac_binding'].upper()))
            for check in checks:
                run('INSERT INTO radcheck (username,attribute,op,value) VALUES (?,?,?,?)', check)
            run('INSERT INTO radusergroup (username,groupname,priority) VALUES (?,?,?)', (row['username'], row['package'], 1))
        conn.commit()
        return True, 'تم استيراد ملف الكروت مع حفظ الحالة والعقد والاستهلاك.', dict(count=len(rows), batch_id=batch_id,
                    batch_name=name, batch_number=number, imported_consumption_count=sum(bool(r['first_used_at']) for r in validated))
    except Exception as exc:
        conn.rollback()
        if isinstance(exc, ValueError):
            return False, str(exc), None
        return False, 'تعذر استيراد الملف؛ لم تُحفظ تغييرات.', None
    finally:
        if mysql:
            try:
                cur.execute('SET @max_radius_import=NULL')
            except Exception:
                pass
        cur.close()
        conn.close()

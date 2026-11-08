"""SQL import identity admission; existing cycles and accounting stay intact."""
from database.db import adapt_query


def prepare_identity(conn, username, kind, password, strategy, stats,
                     full_name=None, phone=None):
    """True admits a new account; existing accounts only get credential metadata.

    Caller holds the username writer lock for its whole transaction. A source
    snapshot cannot renew an existing target, replace its history, or move its
    identity between voucher and subscriber ownership.
    """
    if strategy not in ('skip', 'overwrite') or kind not in ('subscriber', 'voucher'):
        raise ValueError('Unsupported SQL import identity policy')
    table = 'wisp_subscribers' if kind == 'subscriber' else 'wisp_vouchers'
    other = 'wisp_vouchers' if kind == 'subscriber' else 'wisp_subscribers'
    lock = ' FOR UPDATE' if strategy == 'overwrite' else ''
    with conn.cursor() as cursor:
        cursor.execute(adapt_query('SELECT * FROM '+table+' WHERE username=?'+lock, conn), (username,))
        row = cursor.fetchone()
        cursor.execute(adapt_query('SELECT id FROM '+other+' WHERE username=?'+lock, conn), (username,))
        other_row = cursor.fetchone()
        if other_row or (row and strategy == 'skip'):
            stats['skipped_duplicates'] += 1
            return False
        if not row:
            # Old credentials, accounting, or a reserved historical name are
            # evidence of an owner, not permission to allocate a fresh identity.
            sources = ('radcheck', 'radreply', 'radusergroup', 'radacct',
                       'wisp_subscriber_names', 'wisp_voucher_names')
            statement = ' UNION ALL '.join('SELECT 1 AS occupied FROM '+name+
                                           ' WHERE username=?' for name in sources) + ' LIMIT 1'
            cursor.execute(adapt_query(statement, conn), tuple(username for _ in sources))
            if cursor.fetchone():
                stats['skipped_duplicates'] += 1
                return False
            return True
    from services.userman_import_journal import account_image
    # Reuse the account/credential/accounting row locks of User Manager imports.
    account_image(conn, row['username'])
    with conn.cursor() as cursor:
        if kind == 'subscriber':
            cursor.execute(adapt_query('UPDATE wisp_subscribers SET password=?, '
                'full_name=COALESCE(?,full_name), phone=COALESCE(?,phone) WHERE id=?', conn),
                (password, full_name or None, phone or None, row['id']))
        else:
            cursor.execute(adapt_query('UPDATE wisp_vouchers SET password=?,pin_code=? WHERE id=?', conn),
                           (password, password, row['id']))
        cursor.execute(adapt_query("SELECT id FROM radcheck WHERE username=? AND attribute='Cleartext-Password' FOR UPDATE", conn),
                       (row['username'],))
        if cursor.fetchone():
            cursor.execute(adapt_query("UPDATE radcheck SET value=? WHERE username=? AND attribute='Cleartext-Password'", conn),
                           (password, row['username']))
        else:
            cursor.execute(adapt_query("INSERT INTO radcheck(username,attribute,op,value) VALUES(?,'Cleartext-Password',':=',?)", conn),
                           (row['username'], password))
    stats['updated_existing_accounts'] = stats.get('updated_existing_accounts', 0) + 1
    stats['subscribers_imported' if kind == 'subscriber' else 'cards_imported'] += 1
    return False


def validate_session_owners(conn, rows, history=False):
    """An imported UID cannot silently collide with another account's live data."""
    owners = {}
    for row in rows:
        uid, username = str(row[1] or ''), str(row[2] or '').casefold()
        if not uid or not username or (uid in owners and owners[uid] != username):
            raise ValueError('تعارض UID في محاسبة المصدر؛ لم يتم تغيير البيانات.')
        owners[uid] = username
    ids = sorted(owners)
    with conn.cursor() as cursor:
        for offset in range(0, len(ids), 300):
            chunk = ids[offset:offset+300]
            tables = ('radacct', 'wisp_imported_session_history') if history else ('radacct',)
            for table in tables:
                sql = 'SELECT acctuniqueid,username FROM '+table+' WHERE acctuniqueid IN (' + ','.join('?' for _ in chunk) + ')'
                cursor.execute(adapt_query(sql, conn), tuple(chunk))
                for row in cursor.fetchall():
                    if owners[str(row['acctuniqueid'])] != str(row['username']).casefold():
                        raise ValueError('UID مستورد محجوز لحساب آخر؛ لم يتم تغيير البيانات.')

"""Transactional before/after receipts and conditional cancellation.

No accounting, financial, reservation or contract evidence is repaired here.
An account with new evidence is retained for review, not partially reverted.
"""
import hashlib
import json
import re


def encode(value):
    return json.dumps(value, default=str, sort_keys=True, separators=(',', ':'))


def query(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def lock_writers(conn):
    query(conn, 'SELECT id FROM wisp_username_write_lock WHERE id=1 FOR UPDATE')


def account_image(conn, username):
    image = {}
    for table in ('wisp_vouchers', 'wisp_subscribers', 'radcheck', 'radreply', 'radusergroup', 'radacct', 'radpostauth', 'wisp_session_reservations', 'wisp_session_baselines', 'wisp_renewal_guards'):
        image[table] = query(conn, f'SELECT * FROM {table} WHERE username=%s ORDER BY 1 FOR UPDATE', (username,))
    vouchers = image['wisp_vouchers']
    image['wisp_voucher_sales'] = []
    for v in vouchers:
        image['wisp_voucher_sales'].extend(query(conn, 'SELECT * FROM wisp_voucher_sales WHERE voucher_id=%s ORDER BY id FOR UPDATE', (v['id'],)))
    return image


def resource_image(conn, kind, key):
    if kind == 'package':
        return {'wisp_packages': query(conn, 'SELECT * FROM wisp_packages WHERE id=%s FOR UPDATE', (int(key),))}
    if kind == 'batch':
        rows = query(conn, 'SELECT * FROM wisp_voucher_batches WHERE id=%s FOR UPDATE', (int(key),))
        # Derived count can legitimately change as this job commits more chunks.
        return {'wisp_voucher_batches': [{k: v for k, v in r.items() if k != 'card_count'} for r in rows]}
    if kind == 'group':
        return {t: query(conn, f'SELECT * FROM {t} WHERE groupname=%s ORDER BY id FOR UPDATE', (key,))
                for t in ('radgroupcheck', 'radgroupreply')}
    raise ValueError('Unknown journal resource')


def record(conn, job, kind, key, before, after):
    if encode(before) == encode(after):
        return
    previous = query(conn, '''SELECT after_json FROM wisp_userman_import_journal
        WHERE job_id=%s AND kind=%s AND identity_key=%s FOR UPDATE''', (job,kind,str(key)))
    if previous and encode(json.loads(previous[0]['after_json'])) != encode(before):
        # A later chunk must not adopt another writer's new usage/contract as
        # its own after-image, which could make cancellation erase newer work.
        raise ValueError('Import target changed between batches; review required')
    with conn.cursor() as cur:
        cur.execute('''INSERT INTO wisp_userman_import_journal
            (job_id,kind,identity_key,before_json,after_json) VALUES(%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE after_json=VALUES(after_json)''',
                    (job, kind, str(key), encode(before), encode(after)))


def batch_receipt(conn, job, batch, data):
    if not re.fullmatch('[a-f0-9]{32}', job):
        raise ValueError('Invalid import operation')
    digest = hashlib.sha256(encode(data).encode()).hexdigest()
    with conn.cursor() as cur:
        # The receipt is committed in the SAME transaction as all data changes.
        # A duplicate (even after an uncertain commit) fails before any mutation.
        cur.execute('INSERT INTO wisp_userman_import_batches VALUES(%s,%s,%s)', (job, batch, digest))


def refresh_owned_resources(conn, job):
    """A later chunk may recount or remove this job's empty derived batch."""
    rows = query(conn, "SELECT kind,identity_key FROM wisp_userman_import_journal WHERE job_id=%s AND kind='batch' AND state='applied' FOR UPDATE", (job,))
    with conn.cursor() as cur:
        for row in rows:
            after = resource_image(conn, row['kind'], row['identity_key'])
            if after['wisp_voucher_batches']:
                continue  # Never adopt another writer's metadata as our own.
            cur.execute('UPDATE wisp_userman_import_journal SET after_json=%s WHERE job_id=%s AND kind=%s AND identity_key=%s',
                        (encode(after), job, row['kind'], row['identity_key']))


def restore_rows(conn, before, after):
    """Only owned model/RADIUS/derived import rows; actual evidence is immutable."""
    allowed = {'wisp_vouchers', 'wisp_subscribers', 'radcheck', 'radreply', 'radusergroup',
               'radacct', 'radgroupcheck', 'radgroupreply', 'wisp_packages', 'wisp_voucher_batches'}
    # Delete children before the new model; restore existing rows by primary key.
    order = ('radcheck', 'radreply', 'radusergroup', 'radacct', 'wisp_vouchers', 'wisp_subscribers',
             'radgroupcheck', 'radgroupreply', 'wisp_voucher_batches', 'wisp_packages')
    with conn.cursor() as cur:
        for table in order:
            if table not in allowed or table not in after:
                continue
            pk = 'radacctid' if table == 'radacct' else 'id'
            original = {str(r[pk]): r for r in before.get(table, [])}
            for row in after[table]:
                old = original.get(str(row[pk]))
                if old == row:
                    continue
                if table == 'radacct' and (old is not None or row.get('acctterminatecause') != 'Consolidated-Historical-Import'):
                    raise ValueError('Actual accounting cannot be reverted')
                if old is None:
                    cur.execute(f'DELETE FROM {table} WHERE {pk}=%s', (row[pk],))
                else:
                    fields = [k for k in old if k != pk]
                    cur.execute(f'UPDATE {table} SET '+','.join('`'+k+'`=%s' for k in fields)+f' WHERE {pk}=%s',
                                tuple(old[k] for k in fields)+(old[pk],))
            # Import currently only inserts models and updates credentials.
            if set(original) - {str(r[pk]) for r in after[table]}:
                raise ValueError('Unexpected import deletion requires review')


def rollback_import(job, connection_factory=None):
    if connection_factory is None:
        from database.db import get_connection
        connection_factory = get_connection
    conn = connection_factory()
    try:
        query(conn, 'SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ')
        query(conn, 'SET SESSION innodb_lock_wait_timeout=3')
        entries = query(conn, 'SELECT kind,identity_key FROM wisp_userman_import_journal WHERE job_id=%s', (job,))
        conn.rollback()
        # Accounts first, then unreferenced owned groups/batches/packages.
        priority = {'account': 0, 'group': 1, 'batch': 2, 'package': 3}
        reverted = retained = 0
        for entry in sorted(entries, key=lambda e: priority[e['kind']]):
            try:
                lock_writers(conn)
                row = query(conn, '''SELECT * FROM wisp_userman_import_journal
                    WHERE job_id=%s AND kind=%s AND identity_key=%s FOR UPDATE''',
                            (job, entry['kind'], entry['identity_key']))[0]
                if row['state'] == 'reverted':
                    conn.rollback()
                    continue
                before, after = json.loads(row['before_json']), json.loads(row['after_json'])
                kind, key = row['kind'], row['identity_key']
                current = account_image(conn, key) if kind == 'account' else resource_image(conn, kind, key)
                safe = encode(current) == encode(after)
                if kind == 'batch':
                    safe = safe and not query(conn, 'SELECT id FROM wisp_vouchers WHERE batch_id=%s LIMIT 1 FOR UPDATE', (int(key),))
                if kind == 'package':
                    for table in ('wisp_vouchers', 'wisp_subscribers', 'wisp_voucher_batches'):
                        safe = safe and not query(conn, f'SELECT id FROM {table} WHERE package_id=%s LIMIT 1 FOR UPDATE', (int(key),))
                if kind == 'group':
                    safe = safe and not query(conn, 'SELECT id FROM radusergroup WHERE groupname=%s LIMIT 1 FOR UPDATE', (key,))
                    # A retained/new account can reference the package without a group row.
                    safe = safe and not query(conn, '''SELECT v.id FROM wisp_vouchers v JOIN wisp_packages p ON p.id=v.package_id WHERE p.name=%s
                        UNION ALL SELECT s.id FROM wisp_subscribers s JOIN wisp_packages p ON p.id=s.package_id WHERE p.name=%s''', (key, key))
                if safe:
                    restore_rows(conn, before, after)
                    if kind == 'account':
                        # Keep shared batches truthful after removing only our cards.
                        batch_ids = {v['batch_id'] for v in after.get('wisp_vouchers', [])}
                        with conn.cursor() as cur:
                            for batch_id in sorted(i for i in batch_ids if i is not None):
                                cur.execute('''UPDATE wisp_voucher_batches SET card_count=
                                    (SELECT COUNT(*) FROM wisp_vouchers WHERE batch_id=%s) WHERE id=%s''', (batch_id,batch_id))
                    state = 'reverted'
                    reverted += 1
                else:
                    state = 'review_required'
                    retained += 1
                with conn.cursor() as cur:
                    cur.execute('UPDATE wisp_userman_import_journal SET state=%s WHERE job_id=%s AND kind=%s AND identity_key=%s',
                                (state, job, kind, key))
                conn.commit()
            except Exception:
                conn.rollback()
                # Unknown commit is never replayed in this invocation. Durable
                # journal state determines what a later explicit resume can do.
                raise
        return dict(state='cancelled_partial' if retained else 'cancelled', reverted_entries=reverted,
                    retained_entries=retained, rollback_complete=not retained)
    finally:
        conn.close()

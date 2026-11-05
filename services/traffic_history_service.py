"""Copy/verify consumption before destructive maintenance, in its transaction."""
from database.traffic_history_schema import session_key_sql, DETAIL_COLUMNS

SOURCES=('radacct','radacct_archive','wisp_imported_session_history')
FIELDS=('radacctid','acctuniqueid','acctsessionid','username','nasipaddress','calledstationid',
        'nasportid','framedprotocol','servicetype','acctstarttime','acctstoptime','acctupdatetime',
        'acctinputoctets','acctoutputoctets','acctterminatecause') + tuple(k for k in DETAIL_COLUMNS if k != 'payload_version')


def preserve_sessions(conn, source, where, params=()):
    """Internal fixed predicates only. Caller holds account/selected session locks."""
    if source not in SOURCES:raise ValueError('Invalid history source')
    from database.db import adapt_query
    key=session_key_sql('a',source)
    with conn.cursor() as cur:
        # Lock the selected rows until the caller commits preservation/deletion.
        cur.execute(adapt_query(f'SELECT a.radacctid FROM {source} a WHERE {where} FOR UPDATE',conn),params)
        expected=len(cur.fetchall())
        if not expected:return 0
        cur.execute(adapt_query(f'''SELECT COUNT(*) n FROM {source} a
            LEFT JOIN wisp_traffic_history h ON h.session_key={key} WHERE ({where}) AND (
                (a.account_subscriber_id IS NOT NULL AND a.account_voucher_id IS NOT NULL)
                OR (h.account_subscriber_id IS NOT NULL AND h.account_voucher_id IS NOT NULL)
                OR (a.account_subscriber_id IS NOT NULL AND h.account_voucher_id IS NOT NULL)
                OR (a.account_voucher_id IS NOT NULL AND h.account_subscriber_id IS NOT NULL)
                OR (a.account_subscriber_id IS NOT NULL AND h.account_subscriber_id IS NOT NULL AND a.account_subscriber_id<>h.account_subscriber_id)
                OR (a.account_voucher_id IS NOT NULL AND h.account_voucher_id IS NOT NULL AND a.account_voucher_id<>h.account_voucher_id))''',conn),params)
        if int(cur.fetchone()['n']):
            raise RuntimeError('ملكية نسخة الجلسة متعارضة؛ لم يُحذف أي سجل من الدفعة.')
        columns=','.join(FIELDS)
        def value(name):
            if name=='groupname' and source!='radacct_archive':return 'NULL'
            if name=='origin_archived_at':return 'a.archived_at' if source=='radacct_archive' else 'NULL'
            return 'a.`'+name+'`'
        values=','.join(value(name) for name in FIELDS)
        optional=tuple(k for k in DETAIL_COLUMNS if k not in ('payload_version','acctsessiontime','acctinputgigawords','acctoutputgigawords'))
        merges=','.join(f'`{name}`=COALESCE(wisp_traffic_history.`{name}`,VALUES(`{name}`))' for name in optional)
        maxima=','.join(f'`{name}`=CASE WHEN VALUES(`{name}`) IS NULL THEN wisp_traffic_history.`{name}` WHEN wisp_traffic_history.`{name}` IS NULL THEN VALUES(`{name}`) ELSE GREATEST(wisp_traffic_history.`{name}`,VALUES(`{name}`)) END' for name in ('acctsessiontime','acctinputgigawords','acctoutputgigawords'))
        fallback=f"CASE WHEN COALESCE(a.acctuniqueid,'')='' AND COALESCE(a.acctsessionid,'')='' AND a.acctstarttime IS NULL THEN CONCAT('{source}_owned:',a.radacctid) ELSE '' END"
        joins='''LEFT JOIN wisp_subscribers s ON s.id=COALESCE(a.account_subscriber_id,
            (SELECT MIN(id) FROM wisp_subscribers WHERE username=CONVERT(COALESCE(a.account_username,a.username) USING utf8mb4) COLLATE utf8mb4_unicode_ci AND BINARY username=BINARY COALESCE(a.account_username,a.username) HAVING COUNT(*)=1))
            AND a.account_voucher_id IS NULL
            LEFT JOIN wisp_vouchers v ON v.id=COALESCE(a.account_voucher_id,
            (SELECT MIN(id) FROM wisp_vouchers WHERE username=CONVERT(COALESCE(a.account_username,a.username) USING utf8mb4) COLLATE utf8mb4_unicode_ci AND BINARY username=BINARY COALESCE(a.account_username,a.username) HAVING COUNT(*)=1))
            AND a.account_subscriber_id IS NULL'''
        cur.execute(adapt_query(f'SELECT COUNT(*) AS n FROM {source} a {joins} WHERE ({where}) AND s.id IS NOT NULL AND v.id IS NOT NULL',conn),params)
        if int(cur.fetchone()['n']):
            raise RuntimeError('هوية حساب الجلسة متعارضة؛ تم الاحتفاظ بسجلاتها الأصلية.')
        cur.execute(adapt_query(f'''INSERT INTO wisp_traffic_history
            (session_key,origin_table,source_fallback,{columns},account_username,account_subscriber_id,
             account_voucher_id,reseller_id,account_kind,display_name,payload_version)
            SELECT {key},'{source}',{fallback},{values},COALESCE(a.account_username,s.username,v.username,a.username),
                CASE WHEN v.id IS NULL THEN COALESCE(a.account_subscriber_id,s.id) END,
                CASE WHEN s.id IS NULL THEN COALESCE(a.account_voucher_id,v.id) END,
                CASE WHEN s.id IS NULL THEN v.reseller_id END,
                CASE WHEN s.id IS NOT NULL AND v.id IS NULL THEN 'subscriber'
                     WHEN v.id IS NOT NULL AND s.id IS NULL THEN 'voucher' ELSE 'unknown' END,
                CASE WHEN v.id IS NULL THEN s.full_name END,1
            FROM {source} a {joins} WHERE {where}
            ON DUPLICATE KEY UPDATE
                {merges},{maxima},payload_version=1,
                acctinputoctets=CASE WHEN VALUES(acctinputoctets) IS NULL THEN wisp_traffic_history.acctinputoctets WHEN wisp_traffic_history.acctinputoctets IS NULL THEN VALUES(acctinputoctets) ELSE GREATEST(wisp_traffic_history.acctinputoctets,VALUES(acctinputoctets)) END,
                acctoutputoctets=CASE WHEN VALUES(acctoutputoctets) IS NULL THEN wisp_traffic_history.acctoutputoctets WHEN wisp_traffic_history.acctoutputoctets IS NULL THEN VALUES(acctoutputoctets) ELSE GREATEST(wisp_traffic_history.acctoutputoctets,VALUES(acctoutputoctets)) END,
                acctupdatetime=COALESCE(GREATEST(wisp_traffic_history.acctupdatetime,VALUES(acctupdatetime)),wisp_traffic_history.acctupdatetime,VALUES(acctupdatetime)),
                acctstoptime=COALESCE(GREATEST(wisp_traffic_history.acctstoptime,VALUES(acctstoptime)),wisp_traffic_history.acctstoptime,VALUES(acctstoptime))''',conn),params)
        identity=' AND '.join(f'(BINARY h.{name}<=>BINARY a.{name})' for name in
            ('acctuniqueid','acctsessionid','username','nasipaddress','calledstationid','nasportid','framedprotocol','servicetype','acctstarttime','acctterminatecause'))
        details=' AND '.join(
            f'({value(name)} IS NULL OR (BINARY h.`{name}` <=> BINARY {value(name)}))'
            for name in optional)
        counters=' AND '.join(f'(a.{name} IS NULL OR h.{name}>=a.{name})' for name in
            ('acctinputoctets','acctoutputoctets','acctupdatetime','acctstoptime','acctsessiontime','acctinputgigawords','acctoutputgigawords'))
        cur.execute(adapt_query(f'SELECT COUNT(*) AS n FROM {source} a STRAIGHT_JOIN wisp_traffic_history h ON h.session_key={key} WHERE ({where}) AND {identity} AND {counters} AND {details} AND h.payload_version=1',conn),params)
        if int(cur.fetchone()['n'])!=expected:
            raise RuntimeError('تعذر تأكيد حفظ تاريخ الاستهلاك؛ لم تُحذف الجلسات الأصلية.')
        if source == 'radacct':
            # A card can be deleted between Stop and the next sampler poll. Settle
            # its final report delta atomically, before removing the source row.
            cur.execute(adapt_query(f'''SELECT h.* FROM {source} a
                STRAIGHT_JOIN wisp_traffic_history h ON h.session_key={key}
                WHERE ({where}) AND a.acctstoptime>=NOW()-INTERVAL 48 HOUR''',conn),params)
            recent=list(cur.fetchall())
            if recent:
                cur.execute('''SELECT s.started_at,s.epoch,k.`value` marker
                    FROM wisp_traffic_rollup_state s LEFT JOIN wisp_system_settings k
                    ON k.`key`='traffic_rollup_epoch' WHERE s.id=1 FOR UPDATE''')
                state=cur.fetchone()
                if not state or state['epoch']!=state['marker']:
                    raise RuntimeError('تعذر تثبيت آخر قراءة للاستهلاك؛ لم تُحذف الجلسة الأصلية.')
                from services.traffic_sample_service import capture_rows
                for row in recent:row['owner_account_exists']=row['account_kind'] in ('subscriber','voucher')
                capture_rows(conn,recent,state['started_at'])
    return expected


def preserve_account(conn, kind, account):
    preserve_accounts(conn,kind,[account])


def preserve_accounts(conn, kind, accounts):
    from database.db import adapt_query
    field='account_subscriber_id' if kind=='subscriber' else 'account_voucher_id'
    other='account_voucher_id' if kind=='subscriber' else 'account_subscriber_id'
    for offset in range(0,len(accounts),250):
        batch=accounts[offset:offset+250]
        marks=','.join(['?']*len(batch))
        params=tuple(row['id'] for row in batch)+tuple(row['username'] for row in batch)
        for source in SOURCES:
            where=f'(a.{field} IN ({marks}) OR (a.{field} IS NULL AND a.{other} IS NULL AND BINARY a.account_read_username IN ({marks})))'
            preserve_sessions(conn,source,where,params)
            # Account is locked and about to be deleted. Never remove open, uncertain
            # or baseline-referenced rows; every removed payload is verified above.
            safe="a.acctstoptime<NOW()-INTERVAL 48 HOUR AND COALESCE(a.acctterminatecause,'') NOT IN ('Stale-Session-Timeout','Watchdog-Autoheal-Timeout','Backup-Restored-Closed') AND NOT EXISTS (SELECT 1 FROM wisp_session_baselines b WHERE b.radacctid=a.radacctid)"
            with conn.cursor() as cur:
                cur.execute(adapt_query(f'DELETE a FROM {source} a JOIN wisp_traffic_history h ON h.session_key={session_key_sql("a",source)} WHERE ({where}) AND {safe} AND h.payload_version=1',conn),params)


def preserve_before_replacement(conn):
    """Keep prior consumption inside the replacement import's own transaction."""
    from database.db import adapt_query
    for source in SOURCES:
        last=None
        while True:
            with conn.cursor() as cur:
                predicate='' if last is None else 'WHERE radacctid>?'
                cur.execute(adapt_query(f'SELECT radacctid FROM {source} {predicate} ORDER BY radacctid LIMIT 1000 FOR UPDATE',conn),() if last is None else (last,))
                ids=[row['radacctid'] for row in cur.fetchall()]
            if not ids:break
            marks=','.join(['?']*len(ids))
            preserve_sessions(conn,source,f'a.radacctid IN ({marks})',tuple(ids))
            last=max(ids)


def compact_duplicate_history(max_pages=1000, page_size=250, cancel_callback=None):
    """Resume a bounded scan; remove only verified, eligible existing copies.

    No unique imported history is archived here. The persisted per-source cursor
    advances in the same transaction as a successful transfer, never on failure.
    """
    from services.account_lifecycle_service import job_lock,run_transaction,sql,HISTORY_ELIGIBLE
    page_size=max(1,min(int(page_size),250))
    result=dict(scanned=0,removed=0,pending=False)
    with job_lock('history-compaction') as acquired:
        if not acquired:return dict(result,pending=True,busy=True)
        from database.db import query_one
        position=query_one("SELECT value FROM wisp_system_settings WHERE `key`='history_compact_source'")
        start=int((position or {}).get('value') or 0)%len(SOURCES)
        pages=0
        for index in range(start,len(SOURCES)):
            source=SOURCES[index]
            marker='history_compact_cursor_'+source
            def step(conn):
                state=sql(conn,'SELECT value FROM wisp_system_settings WHERE `key`=?',(marker,),'one')
                last=max(0,int((state or {}).get('value') or 0))
                rows=sql(conn,f'SELECT radacctid FROM {source} WHERE radacctid>? ORDER BY radacctid LIMIT ?', (last,page_size),'all')
                ids=sorted({r['radacctid'] for r in rows})
                removed=0
                if ids:
                    marks=','.join(['?']*len(ids))
                    key=session_key_sql('a',source)
                    # Materialize the small set first. A nested EXISTS against the
                    # INSERT target can otherwise become a quadratic semijoin.
                    copies=sql(conn,f'SELECT a.radacctid,{key} session_key FROM {source} a STRAIGHT_JOIN wisp_traffic_history h ON h.session_key={key} WHERE a.radacctid IN ({marks}) AND '+HISTORY_ELIGIBLE+' FOR UPDATE',tuple(ids)+(0,),'all')
                    if copies:
                        keys=tuple(sorted({row['session_key'] for row in copies}))
                        saved_ids=tuple(sorted({row['radacctid'] for row in copies}))
                        # Keep each variable-length bind list explicit. The runtime
                        # compiler can fold repeated literal lists inside a chained
                        # string expression to a single placeholder.
                        saved_marks=','.join('?' for _ in saved_ids)
                        key_marks=','.join('?' for _ in keys)
                        where=f'a.radacctid IN ({saved_marks}) AND {key} IN ({key_marks}) AND '+HISTORY_ELIGIBLE
                        params=saved_ids+keys+(0,)
                        expected=preserve_sessions(conn,source,where,params)
                        removed=sql(conn,f'DELETE a FROM {source} a JOIN wisp_traffic_history h ON h.session_key={key} WHERE ({where}) AND h.payload_version=1',params)
                        if removed!=expected:raise RuntimeError('تغيرت أهلية نسخة التاريخ؛ ألغيت معالجة الدفعة.')
                next_id=max(ids) if len(rows)==page_size else 0
                sql(conn,"INSERT INTO wisp_system_settings (`key`,value) VALUES (?,?) ON DUPLICATE KEY UPDATE value=VALUES(value)",(marker,str(next_id)))
                if not next_id:
                    sql(conn,"INSERT INTO wisp_system_settings (`key`,value) VALUES ('history_compact_source',?) ON DUPLICATE KEY UPDATE value=VALUES(value)",(str((index+1)%len(SOURCES)),))
                return len(rows),removed,next_id
            while pages<max(1,int(max_pages)):
                if cancel_callback and cancel_callback():
                    return dict(result,pending=True,cancelled=True)
                scanned,removed,next_id=run_transaction(step)
                pages+=1;result['scanned']+=scanned;result['removed']+=removed
                if not next_id:break
            else:
                result['pending']=True
                break
    return result

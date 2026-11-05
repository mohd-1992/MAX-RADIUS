# -*- coding: utf-8 -*-
"""
services/accounting_archiver_service.py
---------------------------------------
Complete, transactional session retention; physical compaction is explicit.
"""

from core.license_feature_policy import guarded as _license_guarded
from database.db import get_connection

_get_db = get_connection

def ensure_archive_tables():
    """Create or upgrade radacct_archive table with full 64-bit gigawords support."""
    db = _get_db()
    try:
        cur = db.cursor()
        try:
            cur.execute("SELECT GET_LOCK(CONCAT(DATABASE(), ':archive-schema'), 10) AS acquired")
            if (cur.fetchone() or {}).get('acquired') != 1:
                raise RuntimeError('تعذر حجز ترقية مخطط الأرشيف؛ أعد المحاولة لاحقًا.')
            cur.execute('SET SESSION lock_wait_timeout=10')
            # 1. Create table if it does not exist
            cur.execute("""
                CREATE TABLE IF NOT EXISTS radacct_archive (
                    radacctid BIGINT NOT NULL,
                    acctsessionid VARCHAR(64) NOT NULL,
                    acctuniqueid VARCHAR(32) NOT NULL,
                    username VARCHAR(64) NOT NULL,
                    groupname VARCHAR(64) NOT NULL DEFAULT '',
                    realm VARCHAR(64) DEFAULT '',
                    nasipaddress VARCHAR(15) NOT NULL,
                    nasportid VARCHAR(32) DEFAULT NULL,
                    nasporttype VARCHAR(32) DEFAULT NULL,
                    acctstarttime DATETIME NULL DEFAULT NULL,
                    acctupdatetime DATETIME NULL DEFAULT NULL,
                    acctstoptime DATETIME NULL DEFAULT NULL,
                    acctinterval INT NULL DEFAULT NULL,
                    acctsessiontime INT UNSIGNED NULL DEFAULT NULL,
                    acctauthentic VARCHAR(32) DEFAULT NULL,
                    connectinfo_start VARCHAR(128) DEFAULT NULL,
                    connectinfo_stop VARCHAR(128) DEFAULT NULL,
                    acctinputoctets BIGINT NULL DEFAULT NULL,
                    acctoutputoctets BIGINT NULL DEFAULT NULL,
                    acctinputgigawords BIGINT UNSIGNED DEFAULT 0,
                    acctoutputgigawords BIGINT UNSIGNED DEFAULT 0,
                    class VARCHAR(64) DEFAULT NULL,
                    calledstationid VARCHAR(50) NOT NULL DEFAULT '',
                    callingstationid VARCHAR(50) NOT NULL DEFAULT '',
                    acctterminatecause VARCHAR(32) NOT NULL DEFAULT '',
                    servicetype VARCHAR(32) DEFAULT NULL,
                    framedprotocol VARCHAR(32) DEFAULT NULL,
                    framedipaddress VARCHAR(15) NOT NULL DEFAULT '',
                    archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (radacctid, acctuniqueid),
                    INDEX idx_arch_user (username),
                    INDEX idx_arch_start (acctstarttime),
                    INDEX idx_arch_stop (acctstoptime)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)

            # 2. Check and add missing gigawords/class columns on existing radacct_archive tables
            cur.execute("""
                SELECT column_name
                FROM information_schema.COLUMNS
                WHERE table_schema = DATABASE() AND table_name = 'radacct_archive'
            """)
            existing_cols = {row['column_name'].lower() for row in cur.fetchall()}

            if 'acctinputgigawords' not in existing_cols:
                try:
                    cur.execute("ALTER TABLE radacct_archive ADD COLUMN acctinputgigawords BIGINT UNSIGNED DEFAULT 0")
                except Exception:
                    pass
            if 'acctoutputgigawords' not in existing_cols:
                try:
                    cur.execute("ALTER TABLE radacct_archive ADD COLUMN acctoutputgigawords BIGINT UNSIGNED DEFAULT 0")
                except Exception:
                    pass
            if 'class' not in existing_cols:
                try:
                    cur.execute("ALTER TABLE radacct_archive ADD COLUMN class VARCHAR(64) DEFAULT NULL")
                except Exception:
                    pass

            cur.execute("SHOW INDEX FROM radacct_archive WHERE Key_name='PRIMARY'")
            primary = [r['Column_name'] for r in sorted(cur.fetchall(),key=lambda r:r['Seq_in_index'])]
            if primary == ['radacctid']:
                # Retain source IDs and all historical rows. Different sessions can reuse a source ID.
                cur.execute('ALTER TABLE radacct_archive DROP PRIMARY KEY, ADD PRIMARY KEY (radacctid,acctuniqueid)')
            elif primary != ['radacctid','acctuniqueid']:
                raise RuntimeError('مفتاح أرشيف غير متوقع؛ لم يتم تغيير السجلات.')
            # Old archive payloads must be readable by the complete retention
            # verifier too. No row copying/deletion happens during schema setup.
            from database.traffic_history_schema import install_archive_payload_schema
            install_archive_payload_schema(db)
            db.commit()
        finally:
            cur.execute("SELECT RELEASE_LOCK(CONCAT(DATABASE(), ':archive-schema'))")
            cur.close()
    finally:
        db.close()

@_license_guarded('accounting_archiver')
def get_archiver_status():
    """Eligible counts use exactly the same predicate as the transfer worker."""
    from services.account_lifecycle_service import HISTORY_ELIGIBLE
    from database.traffic_history_schema import session_key_sql
    db=_get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT COUNT(*) live_count,MIN(acctstarttime) oldest_session,MAX(acctstarttime) newest_session FROM radacct")
            live=cur.fetchone()
            eligible={};ages={}
            for days in (90,180,365):
                cur.execute('SELECT COUNT(*) n FROM radacct a WHERE '+HISTORY_ELIGIBLE,(days,))
                eligible[days]=int(cur.fetchone()['n'])
                cur.execute('SELECT COUNT(*) n FROM radacct WHERE acctstoptime<NOW()-INTERVAL %s DAY',(days,))
                ages[days]=int(cur.fetchone()['n'])
            key=session_key_sql('a','radacct_archive')
            cur.execute(f"""SELECT COUNT(*) archive_count,MIN(saved_at) oldest_archive,
                SUM(down_bytes+up_bytes) total_bytes,SUM(down_bytes IS NULL OR up_bytes IS NULL) unknown_counters
                FROM (SELECT session_key,MAX(down_bytes) down_bytes,MAX(up_bytes) up_bytes,MIN(saved_at) saved_at
                    FROM (SELECT session_key,acctoutputoctets down_bytes,acctinputoctets up_bytes,COALESCE(origin_archived_at,captured_at) saved_at
                          FROM wisp_traffic_history WHERE acctstoptime IS NOT NULL
                          UNION ALL SELECT {key},a.acctoutputoctets,a.acctinputoctets,a.archived_at
                          FROM radacct_archive a WHERE a.acctstoptime IS NOT NULL) copies GROUP BY session_key) retained""")
            archived=cur.fetchone()
            cur.execute("SELECT table_name,ROUND((data_length+index_length)/1024/1024,2) size_mb FROM information_schema.TABLES WHERE table_schema=DATABASE() AND table_name IN ('radacct','radacct_archive','radpostauth','wisp_traffic_history')")
            sizes={r['table_name']:float(r['size_mb'] or 0) for r in cur.fetchall()}
        return dict(live_sessions_count=int(live['live_count']),archived_sessions_count=int(archived['archive_count']),
            total_archived_traffic_gb=round(float(archived['total_bytes'] or 0)/1024**3,2),
            archive_unknown_counters=int(archived['unknown_counters'] or 0),
            oldest_session=str(live['oldest_session']) if live['oldest_session'] else None,
            newest_session=str(live['newest_session']) if live['newest_session'] else None,
            **{f'older_{d}d_count':eligible[d] for d in eligible},
            **{f'age_older_{d}d_count':ages[d] for d in ages},
            radacct_size_mb=sizes.get('radacct',0),
            archive_size_mb=round(sizes.get('radacct_archive',0)+sizes.get('wisp_traffic_history',0),2),
            postauth_size_mb=sizes.get('radpostauth',0))
    finally:db.close()

@_license_guarded('accounting_archiver')
def archive_old_sessions(days_threshold=90, chunk_size=5000):
    from services.account_lifecycle_service import job_lock
    try:
        if int(days_threshold) < 1:
            return False, 'مدة الاحتفاظ يجب أن تكون يومًا واحدًا على الأقل.'
        with job_lock('accounting-archive') as acquired, job_lock('factory-reset') as reset_idle:
            if not acquired or not reset_idle:
                return False, 'الأرشفة أو إعادة المصنع قيد التنفيذ؛ أعد المحاولة بعد اكتمالها.'
            return _archive_old_sessions_locked(days_threshold,chunk_size)
    except Exception as exc:
        return False, 'تعذر تنفيذ الأرشفة: '+str(exc)


def _archive_old_sessions_locked(days_threshold=90, chunk_size=5000, progress_callback=None,
                                 cancel_callback=None, return_receipt=False):
    """Transfer eligible sessions once to complete canonical history, atomically."""
    from services.account_lifecycle_service import run_transaction,sql,HISTORY_ELIGIBLE
    from services.traffic_history_service import preserve_sessions,compact_duplicate_history
    ensure_archive_tables()
    days=max(1,int(days_threshold));chunk=max(1,min(int(chunk_size or 500),1000));total=0
    from database.db import query_one
    initial=query_one('SELECT COUNT(*) n,MAX(a.radacctid) last_id FROM radacct a WHERE '+HISTORY_ELIGIBLE,(days,))
    highwater=int(initial['last_id'] or 0)
    receipt=dict(success=True,total=int(initial['n']),processed=0,archived_sessions=0,
                 summarized_sessions=0,protected_cards=0,stage='archive')
    def report():
        if progress_callback:progress_callback(dict(receipt))
    def finish():
        receipt['processed']=total;receipt['archived_sessions']=total
        receipt['complete']=bool(receipt['success'] and not receipt.get('cancelled'))
        return receipt if return_receipt else (receipt['success'],receipt['message'])
    try:
        report()
        while True:
            if cancel_callback and cancel_callback():
                receipt.update(cancelled=True,message=f'ألغيت الأرشفة بعد حفظ {total} جلسة.');return finish()
            def transfer(db):
                rows=sql(db,'SELECT a.radacctid FROM radacct a WHERE a.radacctid<=? AND '+HISTORY_ELIGIBLE+' ORDER BY a.radacctid LIMIT ? FOR UPDATE',(highwater,days,chunk),'all')
                ids=[r['radacctid'] for r in rows]
                if not ids:return 0
                marks=','.join(['?']*len(ids))
                identity=' AND '.join(f'(BINARY h.{f}<=>BINARY a.{f})' for f in ('acctuniqueid','username','nasipaddress','acctsessionid','acctstarttime','callingstationid','framedipaddress'))
                conflict=sql(db,f'SELECT COUNT(*) n FROM radacct a JOIN radacct_archive h ON h.radacctid=a.radacctid AND BINARY h.acctuniqueid=BINARY a.acctuniqueid WHERE a.radacctid IN ({marks}) AND NOT ({identity})',tuple(ids),'one')
                if int(conflict['n']):raise RuntimeError('هوية الجلسة متعارضة؛ لم تنقل الدفعة.')
                where=f'a.radacctid IN ({marks})'
                preserve_sessions(db,'radacct',where,tuple(ids))
                count=sql(db,f'DELETE a FROM radacct a WHERE {where} AND '+HISTORY_ELIGIBLE,tuple(ids)+(days,))
                if count!=len(ids):raise RuntimeError('تغيرت أهلية جلسة؛ ألغيت الدفعة.')
                return count
            count=run_transaction(transfer);total+=count
            receipt.update(processed=total,archived_sessions=total);report()
            if count<chunk:break
        receipt['stage']='duplicates';report()
        compact=compact_duplicate_history(cancel_callback=cancel_callback) if cancel_callback else compact_duplicate_history()
        if compact.get('cancelled'):receipt['cancelled']=True
        receipt['duplicate_copies_removed']=compact['removed']
        suffix='؛ تستكمل معالجة التكرار في دورة لاحقة' if compact['pending'] else ''
        receipt['message']=f"حُفظت {total} جلسة في مخزن التاريخ الموحد، وعولجت {compact['removed']} نسخة مكررة{suffix}. لا يعني النقل تحرير مساحة القرص فعليًا."
        if receipt.get('cancelled'):receipt['message']='ألغيت المتابعة بعد إكمال الدفعة الحالية. '+receipt['message']
        return finish()
    except Exception as exc:
        receipt.update(success=False,message=f'لم تكتمل الأرشفة؛ نُقلت {total} جلسة في دفعات سابقة والدفعة المتعثرة محفوظة. {exc}')
        return finish()


def _validate_archive_options(days, mode):
    days=int(days)
    if not 1<=days<=3650 or mode not in ('archive','summary'):
        raise ValueError('مدة أو نوع معالجة غير صالح')
    return days


def _summary_candidates():
    """Indexed, disjoint owner branches; not an assertion of final eligibility."""
    from services.expired_voucher_purge import STORES
    parts=[]
    for source in STORES:
        parts.append(f"SELECT v.id FROM wisp_vouchers v JOIN {source} a ON a.account_voucher_id=v.id WHERE v.status='expired' AND a.account_subscriber_id IS NULL AND a.acctstoptime<?")
        names=('account_read_username',) if source!='wisp_traffic_history' else ('account_username','username')
        for name in names:
            extra=' AND a.account_username IS NULL' if source=='wisp_traffic_history' and name=='username' else ''
            parts.append(f"SELECT v.id FROM wisp_vouchers v JOIN {source} a ON v.username=CONVERT(a.{name} USING utf8mb4) COLLATE utf8mb4_unicode_ci AND BINARY v.username=BINARY a.{name} WHERE v.status='expired' AND a.account_voucher_id IS NULL AND a.account_subscriber_id IS NULL{extra} AND a.acctstoptime<?")
    return ' UNION '.join(parts),len(parts)


@_license_guarded('accounting_archiver')
def get_archive_plan(days=90, mode='archive'):
    """Age-specific read-only preview. Summary candidates are rechecked locked."""
    from services.account_lifecycle_service import HISTORY_ELIGIBLE
    from database.db import query_one
    days=_validate_archive_options(days,mode)
    if mode=='archive':
        row=query_one('SELECT COUNT(*) n FROM radacct a WHERE '+HISTORY_ELIGIBLE,(days,))
        return dict(days=days,mode=mode,count=int(row['n']),unit='sessions',exact_at_read=True)
    import datetime
    cutoff=query_one('SELECT NOW()-INTERVAL ? DAY cutoff',(max(2,days),))['cutoff']
    statement,branches=_summary_candidates()
    row=query_one('SELECT COUNT(*) n FROM ('+statement+') candidates',(cutoff,)*branches)
    return dict(days=days,mode=mode,count=int(row['n']),unit='cards',exact_at_read=False)


def _summarize_old_expired(days, progress_callback=None, cancel_callback=None):
    """Use the existing purge summary engine, preserving accounts and credentials."""
    from services.account_lifecycle_service import sql,run_transaction,lock_account
    from services.expired_voucher_purge import purge_details
    from database.db import query_all,query_one
    cutoff=query_one('SELECT NOW()-INTERVAL ? DAY cutoff',(max(2,days),))['cutoff']
    statement,branches=_summary_candidates()
    # Bounded control list, never unbounded session payloads. A later manual job
    # may cover the remaining candidates; no automatic replay after restart.
    ids=query_all('SELECT id FROM ('+statement+') candidates ORDER BY id LIMIT 50001',(cutoff,)*branches)
    capped=len(ids)>50000;ids=ids[:50000]
    r=dict(success=True,total=len(ids),processed=0,summarized_sessions=0,summary_groups=0,
           archived_sessions=0,protected_cards=0,skipped_cards=0,pending_reasons={},stage='summary',capped=capped)
    if progress_callback:progress_callback(dict(r))
    for selected in ids:
        if cancel_callback and cancel_callback():r['cancelled']=True;break
        def summarize(conn):
            _,account=lock_account(conn,'voucher',selected['id'])
            if not account or account['status']!='expired':return dict(skipped=True)
            aliases=sql(conn,'SELECT username FROM wisp_voucher_names WHERE voucher_id=?',(account['id'],),'all')
            names=tuple({account['username'],*(row['username'] for row in aliases)})
            marks=','.join('?' for _ in names)
            for table in ('wisp_renewal_guards','wisp_session_reservations'):
                if sql(conn,'SELECT 1 n FROM '+table+' WHERE username IN ('+marks+') AND expires_at>NOW() LIMIT 1 FOR UPDATE',names,'one'):
                    return dict(deferred=True,reason='active_account_operation')
            return purge_details(conn,account,history_only=True,cutoff=cutoff)
        result=run_transaction(summarize)
        r['processed']+=1
        if result.get('deferred'):
            r['protected_cards']+=1;reason=result['reason'];r['pending_reasons'][reason]=r['pending_reasons'].get(reason,0)+1
        elif result.get('skipped'):r['skipped_cards']+=1
        else:
            r['summarized_sessions']+=result['summarized_sessions'];r['summary_groups']+=result['summary_groups']
        if progress_callback:progress_callback(dict(r))
    r['complete']=not r.get('cancelled') and not capped
    r['message']=f"لُخّصت {r['summarized_sessions']} جلسة؛ {r['protected_cards']} كرتًا مؤجلًا للحماية. الكروت وبيانات الدخول والسجل المالي محفوظة. تفاصيل الجلسات الملخصة لا تعود كصفوف منفردة."
    if r.get('cancelled'):r['message']='ألغيت المتابعة بعد إكمال الحساب الجاري. '+r['message']
    if capped:r['message']+=' بلغت الدفعة حد الحسابات؛ راجع النتيجة قبل متابعة المتبقي.'
    return r


@_license_guarded('accounting_archiver')
def run_archive_maintenance(days=90, mode='archive', chunk_size=500,
                            progress_callback=None, cancel_callback=None):
    from services.account_lifecycle_service import job_lock
    days=_validate_archive_options(days,mode)
    with job_lock('accounting-archive') as own,job_lock('factory-reset') as factory,job_lock('expired-card-delete') as deletion:
        if not (own and factory and deletion):
            raise ValueError('توجد عملية صيانة أخرى؛ أعد المحاولة بعد اكتمالها.')
        if mode=='summary':return _summarize_old_expired(days,progress_callback,cancel_callback)
        return _archive_old_sessions_locked(days,chunk_size,progress_callback,cancel_callback,return_receipt=True)

@_license_guarded('accounting_archiver')
def optimize_accounting_tables():
    from services.db_maintenance_service import optimize_database_tables
    result=optimize_database_tables(['radacct','radacct_archive'])
    return result['success'],result['message']

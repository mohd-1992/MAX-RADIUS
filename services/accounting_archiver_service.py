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


def _archive_old_sessions_locked(days_threshold=90, chunk_size=5000):
    """Transfer eligible sessions once to complete canonical history, atomically."""
    from services.account_lifecycle_service import run_transaction,sql,HISTORY_ELIGIBLE
    from services.traffic_history_service import preserve_sessions,compact_duplicate_history
    ensure_archive_tables()
    days=max(1,int(days_threshold));chunk=max(1,min(int(chunk_size or 500),1000));total=0
    try:
        while True:
            def transfer(db):
                rows=sql(db,'SELECT a.radacctid FROM radacct a WHERE '+HISTORY_ELIGIBLE+' ORDER BY a.radacctid LIMIT ? FOR UPDATE',(days,chunk),'all')
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
            if count<chunk:break
        compact=compact_duplicate_history()
        suffix='؛ تستكمل معالجة التكرار في دورة لاحقة' if compact['pending'] else ''
        return True,f"حُفظت {total} جلسة في مخزن التاريخ الموحد، وعولجت {compact['removed']} نسخة مكررة{suffix}. لا يعني النقل تحرير مساحة القرص فعليًا."
    except Exception as exc:
        return False,f'لم تكتمل الأرشفة؛ نُقلت {total} جلسة في دفعات سابقة والدفعة المتعثرة محفوظة. {exc}'

@_license_guarded('accounting_archiver')
def optimize_accounting_tables():
    from services.db_maintenance_service import optimize_database_tables
    result=optimize_database_tables(['radacct','radacct_archive'])
    return result['success'],result['message']

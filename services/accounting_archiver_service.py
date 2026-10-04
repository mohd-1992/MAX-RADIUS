# -*- coding: utf-8 -*-
"""
services/accounting_archiver_service.py
---------------------------------------
Accounting Logs Archiver & DB Compactor Service for MAX RADIUS (Core Architecture Pillar 2).
Features:
- Full 64-bit Gigawords & Class support in radacct_archive.
- Zero-downtime chunked archiving (avoids long table locks on active production MariaDB).
- Safe table compaction & index defragmentation (OPTIMIZE / ANALYZE TABLE).
- Detailed telemetry on table sizes, storage reclaimed, and session distributions.
"""

from datetime import datetime, timedelta
from database.db import get_connection
from core.config import DB_NAME

_get_db = get_connection

def ensure_archive_tables():
    """Create or upgrade radacct_archive table with full 64-bit gigawords support."""
    db = _get_db()
    try:
        cur = db.cursor()
        try:
            # 1. Create table if it does not exist
            cur.execute("""
                CREATE TABLE IF NOT EXISTS radacct_archive (
                    radacctid BIGINT NOT NULL PRIMARY KEY,
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

            db.commit()
        finally:
            cur.close()
    finally:
        db.close()

def get_archiver_status():
    """Return status of live vs archived accounting records and estimated reclaimable space."""
    ensure_archive_tables()
    db = _get_db()
    try:
        cur = db.cursor()
        try:
            # Live radacct count and session timestamps
            cur.execute("""
                SELECT 
                    COUNT(*) as live_count,
                    MIN(acctstarttime) as oldest_session,
                    MAX(acctstarttime) as newest_session,
                    SUM(CASE WHEN acctstoptime IS NOT NULL AND acctstoptime < NOW() - INTERVAL 90 DAY THEN 1 ELSE 0 END) as older_90d,
                    SUM(CASE WHEN acctstoptime IS NOT NULL AND acctstoptime < NOW() - INTERVAL 180 DAY THEN 1 ELSE 0 END) as older_180d,
                    SUM(CASE WHEN acctstoptime IS NOT NULL AND acctstoptime < NOW() - INTERVAL 365 DAY THEN 1 ELSE 0 END) as older_365d
                FROM radacct
            """)
            live_stats = cur.fetchone() or {}

            # Archive count & oldest archive record
            cur.execute("""
                SELECT 
                    COUNT(*) as archive_count, 
                    MIN(archived_at) as oldest_archive,
                    COALESCE(SUM((CAST(COALESCE(acctinputgigawords, 0) AS UNSIGNED) + CAST(COALESCE(acctoutputgigawords, 0) AS UNSIGNED)) * 4294967296 + 
                                 CAST(COALESCE(acctinputoctets, 0) AS UNSIGNED) + CAST(COALESCE(acctoutputoctets, 0) AS UNSIGNED)), 0) as total_archived_bytes
                FROM radacct_archive
            """)
            arch_stats = cur.fetchone() or {}

            # Table sizes in MB
            db_name = DB_NAME or 'radius_wisp'
            cur.execute("""
                SELECT 
                    table_name,
                    ROUND(((data_length + index_length) / 1024 / 1024), 2) AS size_mb
                FROM information_schema.TABLES
                WHERE table_schema = %s AND table_name IN ('radacct', 'radacct_archive', 'radpostauth')
            """, (db_name,))
            table_sizes = {r['table_name']: r['size_mb'] for r in cur.fetchall()}

            archived_gb = round(float(arch_stats.get('total_archived_bytes') or 0) / (1024 ** 3), 2)
            
            oldest_s = live_stats.get('oldest_session')
            if oldest_s and hasattr(oldest_s, 'strftime'):
                oldest_s = oldest_s.strftime('%Y-%m-%d %H:%M')
            elif oldest_s:
                oldest_s = str(oldest_s)
                
            newest_s = live_stats.get('newest_session')
            if newest_s and hasattr(newest_s, 'strftime'):
                newest_s = newest_s.strftime('%Y-%m-%d %H:%M')
            elif newest_s:
                newest_s = str(newest_s)

            return {
                'live_sessions_count': int(live_stats.get('live_count') or 0),
                'archived_sessions_count': int(arch_stats.get('archive_count') or 0),
                'total_archived_traffic_gb': archived_gb,
                'oldest_session': oldest_s,
                'newest_session': newest_s,
                'older_90d_count': int(live_stats.get('older_90d') or 0),
                'older_180d_count': int(live_stats.get('older_180d') or 0),
                'older_365d_count': int(live_stats.get('older_365d') or 0),
                'radacct_size_mb': float(table_sizes.get('radacct') or 0.0),
                'archive_size_mb': float(table_sizes.get('radacct_archive') or 0.0),
                'postauth_size_mb': float(table_sizes.get('radpostauth') or 0.0)
            }
        finally:
            cur.close()
    finally:
        db.close()

def archive_old_sessions(days_threshold=90, chunk_size=5000):
    """
    Move closed accounting sessions older than days_threshold into radacct_archive
    in controlled chunks to avoid database locking.
    Includes full gigawords and session metadata.
    """
    ensure_archive_tables()
    db = _get_db()
    days = int(days_threshold)
    chunk = max(100, min(int(chunk_size or 5000), 20000))
    
    # Keep every session still used by an account's current-cycle quota or baseline.
    eligible = """
        a.acctstoptime IS NOT NULL
        AND a.acctstoptime < NOW() - INTERVAL %s DAY
        AND COALESCE(a.acctterminatecause, '') NOT IN
            ('Stale-Session-Timeout', 'Watchdog-Autoheal-Timeout', 'Backup-Restored-Closed')
        AND NOT EXISTS (
            SELECT 1 FROM wisp_subscribers s WHERE s.username = a.username
            AND (s.last_renewed_at IS NULL OR a.acctstarttime >= s.last_renewed_at
                 OR EXISTS (SELECT 1 FROM wisp_session_baselines b WHERE b.radacctid = a.radacctid AND b.renewed_at = s.last_renewed_at))
        )
        AND NOT EXISTS (
            SELECT 1 FROM wisp_vouchers v WHERE v.username = a.username
            AND (v.last_renewed_at IS NULL OR a.acctstarttime >= v.last_renewed_at
                 OR EXISTS (SELECT 1 FROM wisp_session_baselines b WHERE b.radacctid = a.radacctid AND b.renewed_at = v.last_renewed_at))
        )
    """
    total_archived = 0
    total_deleted = 0

    try:
        # Loop chunk by chunk until no more eligible rows
        while True:
            chunk_success = False
            for attempt in range(4):
                try:
                    cur = db.cursor()
                    try:
                        # 1. Fetch batch of radacctids to archive
                        cur.execute(f"""
                            SELECT a.radacctid FROM radacct a
                            WHERE {eligible}
                            ORDER BY a.radacctid ASC
                            LIMIT %s FOR UPDATE
                        """, (days, chunk))
                        rows = cur.fetchall()
                        if not rows:
                            chunk_success = True
                            ids = []
                            break

                        ids = [r['radacctid'] for r in rows]
                        placeholders = ','.join(['%s'] * len(ids))

                        # 2. Insert batch into radacct_archive with gigawords
                        cur.execute(f"""
                            INSERT IGNORE INTO radacct_archive (
                                radacctid, acctsessionid, acctuniqueid, username, groupname, realm,
                                nasipaddress, nasportid, nasporttype, acctstarttime, acctupdatetime,
                                acctstoptime, acctinterval, acctsessiontime, acctauthentic, connectinfo_start,
                                connectinfo_stop, acctinputoctets, acctoutputoctets, acctinputgigawords,
                                acctoutputgigawords, class, calledstationid, callingstationid,
                                acctterminatecause, servicetype, framedprotocol, framedipaddress
                            )
                            SELECT 
                                radacctid, acctsessionid, acctuniqueid, username, '' as groupname, realm,
                                nasipaddress, nasportid, nasporttype, acctstarttime, acctupdatetime,
                                acctstoptime, acctinterval, acctsessiontime, acctauthentic, connectinfo_start,
                                connectinfo_stop, acctinputoctets, acctoutputoctets,
                                COALESCE(acctinputgigawords, 0), COALESCE(acctoutputgigawords, 0),
                                class, calledstationid, callingstationid,
                                acctterminatecause, servicetype, framedprotocol, framedipaddress
                            FROM radacct
                            WHERE radacctid IN ({placeholders})
                        """, tuple(ids))
                        archived_count = cur.rowcount

                        # 3. Delete batch from radacct
                        cur.execute(f"DELETE a FROM radacct a WHERE a.radacctid IN ({placeholders}) AND {eligible}", tuple(ids) + (days,))
                        deleted_count = cur.rowcount

                        db.commit()

                        total_archived += archived_count
                        total_deleted += deleted_count
                        chunk_success = True
                        break
                    finally:
                        cur.close()
                except Exception as chunk_err:
                    db.rollback()
                    err_str = str(chunk_err)
                    if ('1213' in err_str or 'Deadlock' in err_str or 'lock' in err_str.lower()) and attempt < 3:
                        import time
                        time.sleep(0.2 * (attempt + 1))
                        continue
                    else:
                        raise chunk_err

            if not chunk_success:
                raise RuntimeError("فشلت معالجة الدفعة بعد عدة محاولات.")

            if not ids or len(ids) < chunk:
                break

        # Analyze table to update indexes and table stats
        try:
            cur = db.cursor()
            cur.execute("ANALYZE TABLE radacct")
            cur.close()
            db.commit()
        except Exception:
            pass

        return True, f"تم بنجاح أرشفة {total_archived} جلسة قديمة وتحرير مساحة جدول المحاسبة."
    except Exception as e:
        db.rollback()
        return False, f"خطأ أثناء أرشفة الجلسات: {str(e)}"
    finally:
        db.close()

def optimize_accounting_tables():
    """
    Defragments table storage and rebuilds indexes for radacct and radacct_archive.
    Uses ANALYZE TABLE and OPTIMIZE TABLE.
    """
    ensure_archive_tables()
    db = _get_db()
    try:
        cur = db.cursor()
        try:
            cur.execute("ANALYZE TABLE radacct")
            cur.execute("OPTIMIZE TABLE radacct_archive")
            db.commit()
            return True, "تم بنجاح تحسين وضغط جداول المحاسبة وإعادة بناء الفهارس."
        finally:
            cur.close()
    except Exception as e:
        return False, f"خطأ أثناء ضغط الجداول: {str(e)}"
    finally:
        db.close()

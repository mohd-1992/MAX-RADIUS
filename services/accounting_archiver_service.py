"""
services/accounting_archiver_service.py
---------------------------------------
Accounting Logs Archiver & DB Compactor Service for MAX RADIUS.
Archives historical radacct sessions to compressed archive tables and frees DB space.
"""

from datetime import datetime, timedelta
from database.db import get_connection
from core.config import DB_NAME

_get_db = get_connection

def ensure_archive_tables():
    """Create radacct_archive table if not exists."""
    db = _get_db()
    try:
        with db.cursor() as cur:
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
            db.commit()
    finally:
        db.close()

def get_archiver_status():
    """Return status of live vs archived accounting records and estimated reclaimable space."""
    ensure_archive_tables()
    db = _get_db()
    try:
        with db.cursor() as cur:
            # Live radacct count and oldest record
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

            # Archive count
            cur.execute("SELECT COUNT(*) as archive_count, MIN(archived_at) as oldest_archive FROM radacct_archive")
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

        return {
            'live_sessions_count': live_stats.get('live_count', 0),
            'archived_sessions_count': arch_stats.get('archive_count', 0),
            'oldest_session': live_stats.get('oldest_session'),
            'newest_session': live_stats.get('newest_session'),
            'older_90d_count': live_stats.get('older_90d') or 0,
            'older_180d_count': live_stats.get('older_180d') or 0,
            'older_365d_count': live_stats.get('older_365d') or 0,
            'radacct_size_mb': table_sizes.get('radacct', 0.0),
            'archive_size_mb': table_sizes.get('radacct_archive', 0.0),
            'postauth_size_mb': table_sizes.get('radpostauth', 0.0)
        }
    finally:
        db.close()

def archive_old_sessions(days_threshold=90):
    """
    Move closed accounting sessions older than days_threshold into radacct_archive
    and delete them from radacct.
    """
    ensure_archive_tables()
    db = _get_db()
    days = int(days_threshold)
    try:
        with db.cursor() as cur:
            cur.execute(f"""
                INSERT IGNORE INTO radacct_archive (
                    radacctid, acctsessionid, acctuniqueid, username, groupname, realm,
                    nasipaddress, nasportid, nasporttype, acctstarttime, acctupdatetime,
                    acctstoptime, acctinterval, acctsessiontime, acctauthentic, connectinfo_start,
                    connectinfo_stop, acctinputoctets, acctoutputoctets, calledstationid,
                    callingstationid, acctterminatecause, servicetype, framedprotocol, framedipaddress
                )
                SELECT 
                    radacctid, acctsessionid, acctuniqueid, username, groupname, realm,
                    nasipaddress, nasportid, nasporttype, acctstarttime, acctupdatetime,
                    acctstoptime, acctinterval, acctsessiontime, acctauthentic, connectinfo_start,
                    connectinfo_stop, acctinputoctets, acctoutputoctets, calledstationid,
                    callingstationid, acctterminatecause, servicetype, framedprotocol, framedipaddress
                FROM radacct
                WHERE acctstoptime IS NOT NULL 
                  AND acctstoptime < NOW() - INTERVAL %s DAY
            """, (days,))
            archived_rows = cur.rowcount

            cur.execute(f"""
                DELETE FROM radacct 
                WHERE acctstoptime IS NOT NULL 
                  AND acctstoptime < NOW() - INTERVAL %s DAY
            """, (days,))
            deleted_rows = cur.rowcount

            cur.execute("OPTIMIZE TABLE radacct")

            db.commit()
            return True, f"تم بنجاح أرشفة {archived_rows} جلسة وضغط جدول المحاسبة."
    except Exception as e:
        return False, str(e)
    finally:
        db.close()

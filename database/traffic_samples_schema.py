"""Compact report-only checkpoints and quarter-hour rollups; no RADIUS triggers."""
import uuid
import datetime as dt

CHECKPOINT_SQL = '''CREATE TABLE IF NOT EXISTS wisp_traffic_checkpoints (
 session_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
 radacctid BIGINT NOT NULL, observed_at DATETIME NOT NULL, closed_at DATETIME NULL,
 download BIGINT NULL, upload BIGINT NULL, owner_scope BIGINT NOT NULL DEFAULT 0,
 point_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 KEY idx_checkpoint_expiry (closed_at,observed_at), KEY idx_checkpoint_radacct (radacctid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin'''
ROLLUP_SQL = '''CREATE TABLE IF NOT EXISTS wisp_traffic_rollups (
 bucket_at DATETIME NOT NULL, owner_scope BIGINT NOT NULL DEFAULT 0,
 point_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 nas_address VARCHAR(45) NOT NULL DEFAULT '', service_type VARCHAR(20) NOT NULL DEFAULT '',
 server_name VARCHAR(128) NOT NULL DEFAULT '', server_interface VARCHAR(128) NOT NULL DEFAULT '',
 point_known TINYINT NOT NULL DEFAULT 0,
 download_bytes BIGINT UNSIGNED NOT NULL DEFAULT 0, upload_bytes BIGINT UNSIGNED NOT NULL DEFAULT 0,
 download_known BIGINT UNSIGNED NOT NULL DEFAULT 0, upload_known BIGINT UNSIGNED NOT NULL DEFAULT 0,
 gaps BIGINT UNSIGNED NOT NULL DEFAULT 0, observations BIGINT UNSIGNED NOT NULL DEFAULT 0,
 PRIMARY KEY (bucket_at,owner_scope,point_key), KEY idx_rollup_owner (owner_scope,bucket_at),
 KEY idx_rollup_point (point_key,bucket_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin'''
STATE_SQL = '''CREATE TABLE IF NOT EXISTS wisp_traffic_rollup_state (
 id INT NOT NULL PRIMARY KEY, epoch CHAR(32) NOT NULL, cursor_id BIGINT NOT NULL DEFAULT 0,
 cycle_rows BIGINT NOT NULL DEFAULT 0, last_cycle_rows BIGINT NOT NULL DEFAULT 0,
 last_completed_at DATETIME NULL, started_at DATETIME NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin'''
EPOCH_KEY='traffic_rollup_epoch'


def ensure_epoch(conn):
    """An old backup restored by old Web cannot silently retain newer summaries."""
    with conn.cursor() as cur:
        cur.execute('SELECT epoch FROM wisp_traffic_rollup_state WHERE id=1 FOR UPDATE')
        state=cur.fetchone()
        cur.execute('SELECT `value` FROM wisp_system_settings WHERE `key`=%s',(EPOCH_KEY,))
        marker=cur.fetchone()
        if state and marker and state['epoch']==marker['value']:return state['epoch']
        epoch=uuid.uuid4().hex
        cur.execute('DELETE FROM wisp_traffic_checkpoints')
        cur.execute('DELETE FROM wisp_traffic_rollups')
        cur.execute('''INSERT INTO wisp_traffic_rollup_state(id,epoch,cursor_id,cycle_rows,last_cycle_rows,last_completed_at,started_at)
            VALUES(1,%s,0,0,0,NULL,%s) ON DUPLICATE KEY UPDATE epoch=VALUES(epoch),cursor_id=0,cycle_rows=0,last_cycle_rows=0,last_completed_at=NULL,started_at=VALUES(started_at)''',(epoch,dt.datetime.now(dt.timezone(dt.timedelta(hours=3))).replace(tzinfo=None)))
        cur.execute('''INSERT INTO wisp_system_settings(`key`,`value`,description) VALUES(%s,%s,'Report history generation')
            ON DUPLICATE KEY UPDATE `value`=VALUES(`value`)''',(EPOCH_KEY,epoch))
        return epoch


def install_traffic_samples_schema(conn):
    with conn.cursor() as cur:
        for ddl in (CHECKPOINT_SQL,ROLLUP_SQL,STATE_SQL):cur.execute(ddl)
    with conn.cursor() as cur:
        cur.execute('ALTER TABLE wisp_traffic_rollup_state ADD COLUMN IF NOT EXISTS started_at DATETIME NULL')
        cur.execute('UPDATE wisp_traffic_rollup_state SET started_at=%s WHERE started_at IS NULL',(dt.datetime.now(dt.timezone(dt.timedelta(hours=3))).replace(tzinfo=None),))
    ensure_epoch(conn)


def clear_legacy_rollups(conn):
    """Used when any component of the consistent checkpoint/summary set is absent."""
    with conn.cursor() as cur:
        cur.execute('DELETE FROM wisp_traffic_rollup_state')
    ensure_epoch(conn)

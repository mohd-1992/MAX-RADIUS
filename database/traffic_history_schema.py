"""Complete retained session payloads, without financial/RADIUS triggers."""
DETAIL_COLUMNS = {
    'realm': 'VARCHAR(64) NULL', 'groupname': 'VARCHAR(64) NULL',
    'nasporttype': 'VARCHAR(32) NULL', 'acctinterval': 'INT NULL',
    'acctsessiontime': 'BIGINT UNSIGNED NULL', 'acctauthentic': 'VARCHAR(32) NULL',
    'connectinfo_start': 'VARCHAR(128) NULL', 'connectinfo_stop': 'VARCHAR(128) NULL',
    'acctinputgigawords': 'BIGINT NULL', 'acctoutputgigawords': 'BIGINT NULL',
    'class': 'VARCHAR(64) NULL', 'framedipaddress': 'VARCHAR(45) NULL',
    'callingstationid': 'VARCHAR(128) NULL', 'framedipv6address': 'VARCHAR(45) NULL',
    'framedipv6prefix': 'VARCHAR(45) NULL', 'framedinterfaceid': 'VARCHAR(44) NULL',
    'delegatedipv6prefix': 'VARCHAR(45) NULL',
    'origin_archived_at': 'DATETIME NULL',
    'payload_version': 'TINYINT NOT NULL DEFAULT 0',
}
TABLE_SQL = '''CREATE TABLE IF NOT EXISTS wisp_traffic_history (
 history_id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
 session_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL UNIQUE,
 radacctid BIGINT NOT NULL, origin_table VARCHAR(48) NOT NULL,
 source_fallback VARCHAR(96) NOT NULL DEFAULT '',
 acctuniqueid VARCHAR(32) NOT NULL DEFAULT '', acctsessionid VARCHAR(64) NOT NULL DEFAULT '',
 username VARCHAR(64) NOT NULL DEFAULT '', account_username VARCHAR(64) NULL,
 account_subscriber_id BIGINT NULL, account_voucher_id BIGINT NULL, reseller_id BIGINT NULL,
 account_kind VARCHAR(20) NOT NULL DEFAULT 'unknown', display_name VARCHAR(120) NULL,
 nasipaddress VARCHAR(45) NOT NULL DEFAULT '', calledstationid VARCHAR(128) NOT NULL DEFAULT '',
 nasportid VARCHAR(128) NULL, framedprotocol VARCHAR(32) NULL, servicetype VARCHAR(32) NULL,
 acctstarttime DATETIME NULL, acctstoptime DATETIME NULL, acctupdatetime DATETIME NULL,
 acctinputoctets BIGINT NULL, acctoutputoctets BIGINT NULL,
 acctterminatecause VARCHAR(64) NULL,
 realm VARCHAR(64) NULL, groupname VARCHAR(64) NULL, nasporttype VARCHAR(32) NULL,
 acctinterval INT NULL, acctsessiontime BIGINT UNSIGNED NULL, acctauthentic VARCHAR(32) NULL,
 connectinfo_start VARCHAR(128) NULL, connectinfo_stop VARCHAR(128) NULL,
 acctinputgigawords BIGINT NULL, acctoutputgigawords BIGINT NULL, class VARCHAR(64) NULL,
 framedipaddress VARCHAR(45) NULL, callingstationid VARCHAR(128) NULL,
 framedipv6address VARCHAR(45) NULL, framedipv6prefix VARCHAR(45) NULL,
 framedinterfaceid VARCHAR(44) NULL, delegatedipv6prefix VARCHAR(45) NULL,
 origin_archived_at DATETIME NULL,
 payload_version TINYINT NOT NULL DEFAULT 0,
 captured_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 KEY idx_traffic_owner (reseller_id,acctstarttime), KEY idx_traffic_start (acctstarttime),
 KEY idx_traffic_subscriber (account_subscriber_id), KEY idx_traffic_voucher (account_voucher_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin'''


def session_key_sql(alias, source, username='username'):
    if source not in ('radacct','radacct_archive','wisp_imported_session_history'):
        raise ValueError('Invalid history source')
    return f"""CONVERT(SHA2(CONVERT(JSON_ARRAY(COALESCE({alias}.acctuniqueid,''),COALESCE({alias}.{username},''),
        COALESCE({alias}.acctsessionid,''),COALESCE({alias}.nasipaddress,''),CAST({alias}.acctstarttime AS CHAR),
        CASE WHEN COALESCE({alias}.acctuniqueid,'')='' AND COALESCE({alias}.acctsessionid,'')=''
        AND {alias}.acctstarttime IS NULL THEN CONCAT('{source}_owned:',{alias}.radacctid) ELSE '' END) USING utf8mb4),256) USING ascii) COLLATE ascii_bin"""


def owner_scope_sql(table):
    if table=='wisp_traffic_history_owned':return 'reseller_id=%s',1
    source=table.removesuffix('_owned')
    key=session_key_sql(table,source,'original_username')
    return f'''(EXISTS (SELECT 1 FROM wisp_traffic_history h WHERE h.session_key={key} AND h.reseller_id=%s)
        OR (NOT EXISTS (SELECT 1 FROM wisp_traffic_history h WHERE h.session_key={key})
        AND BINARY username IN (SELECT BINARY username FROM wisp_vouchers WHERE reseller_id=%s)))''',2


def install_archive_payload_schema(conn):
    """Old full backups restore the archive table and its old expanded view too."""
    with conn.cursor() as cur:
        cur.execute("SELECT GET_LOCK(CONCAT(DATABASE(), ':archive-schema'),10) acquired")
        if not cur.fetchone()['acquired']:
            raise RuntimeError('تعذر حجز ترقية تفاصيل أرشيف الجلسات.')
        try:
            cur.execute("SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='radacct_archive'")
            if not cur.fetchone():
                return
            cur.execute('SHOW COLUMNS FROM radacct_archive')
            names=[row['Field'] for row in cur.fetchall()]
            for name in ('framedipv6address','framedipv6prefix','framedinterfaceid','delegatedipv6prefix'):
                if name not in names:
                    cur.execute(f'ALTER TABLE radacct_archive ADD COLUMN `{name}` {DETAIL_COLUMNS[name]}')
            # SELECT a.* in a MariaDB view is expanded when the view is created;
            # adding table columns alone does not expose them through the old view.
            cur.execute('SHOW COLUMNS FROM radacct_archive')
            names=[row['Field'] for row in cur.fetchall()]
            if 'account_read_username' in names:
                ownership={'account_username','account_subscriber_id','account_voucher_id','account_read_username'}
                fields=','.join('a.account_read_username AS username' if name=='username' else 'a.`'+name+'`' for name in names if name not in ownership)
                cur.execute('CREATE OR REPLACE VIEW radacct_archive_owned AS SELECT '+fields+',a.username AS original_username FROM radacct_archive a')
        finally:
            cur.execute("SELECT RELEASE_LOCK(CONCAT(DATABASE(), ':archive-schema'))")


def install_traffic_history_schema(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT GET_LOCK(CONCAT(DATABASE(), ':traffic-history-schema'),10) acquired")
        if not cur.fetchone()['acquired']:
            raise RuntimeError('تعذر حجز تهيئة مخزن تاريخ الجلسات.')
        try:
            install_archive_payload_schema(conn)
            cur.execute(TABLE_SQL)
            cur.execute('SHOW COLUMNS FROM wisp_traffic_history')
            names=[row['Field'] for row in cur.fetchall()]
            for name, definition in DETAIL_COLUMNS.items():
                if name not in names:
                    cur.execute(f'ALTER TABLE wisp_traffic_history ADD COLUMN `{name}` {definition}')
            cur.execute('SHOW COLUMNS FROM wisp_traffic_history')
            names=[row['Field'] for row in cur.fetchall()]
            fields=','.join('COALESCE(a.account_username,a.username) AS username' if name=='username' else 'a.`'+name+'`' for name in names)
            cur.execute('CREATE OR REPLACE VIEW wisp_traffic_history_owned AS SELECT '+fields+',a.username AS original_username FROM wisp_traffic_history a')
        finally:
            cur.execute("SELECT RELEASE_LOCK(CONCAT(DATABASE(), ':traffic-history-schema'))")

def clear_legacy_traffic_history(conn):
    """An explicit old-backup restore cannot inherit a newer database's history."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='wisp_traffic_history'")
        if cur.fetchone():cur.execute('DELETE FROM wisp_traffic_history')

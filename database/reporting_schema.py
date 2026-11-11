"""Additive reporting metadata; never backfill money or change quota triggers."""

INTERVAL_SQL = '''CREATE TABLE IF NOT EXISTS wisp_traffic_intervals (
 interval_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
 start_at DATETIME NOT NULL,end_at DATETIME NOT NULL,
 owner_scope BIGINT NOT NULL DEFAULT 0,point_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 nas_address VARCHAR(45) NOT NULL DEFAULT '',service_type VARCHAR(20) NOT NULL DEFAULT '',
 server_name VARCHAR(128) NOT NULL DEFAULT '',server_interface VARCHAR(128) NOT NULL DEFAULT '',
 point_known TINYINT NOT NULL DEFAULT 0,download BIGINT NULL,upload BIGINT NULL,
 reason VARCHAR(32) NOT NULL DEFAULT 'delayed_sample',
 KEY idx_interval_owner(owner_scope,end_at),KEY idx_interval_point(point_key,end_at),KEY idx_interval_time(end_at,start_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin'''

EVIDENCE_SQL = '''CREATE TABLE IF NOT EXISTS wisp_session_service_evidence (
 acctuniqueid VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 nasipaddress VARCHAR(45) NOT NULL,acctsessionid VARCHAR(64) NOT NULL,acctstarttime DATETIME NOT NULL,
 original_username VARCHAR(100) NOT NULL,service_type VARCHAR(16) NOT NULL,
 observed_at DATETIME NOT NULL,source VARCHAR(32) NOT NULL DEFAULT 'verified_nas',
 PRIMARY KEY(acctuniqueid,nasipaddress,acctsessionid,acctstarttime),KEY idx_service_evidence_age(observed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin'''

INVOICE_COLUMNS = (
    ('report_cost','DECIMAL(12,2) NULL'),
    ('cost_source',"VARCHAR(32) NOT NULL DEFAULT ''"),
    ('package_service_type',"VARCHAR(16) NOT NULL DEFAULT ''"),
)


def install_reporting_schema(conn):
    from database.db import is_mysql_conn
    if not is_mysql_conn(conn):return
    with conn.cursor() as cur:
        cur.execute(INTERVAL_SQL);cur.execute(EVIDENCE_SQL)
        cur.execute('SHOW COLUMNS FROM wisp_invoices')
        present={row['Field'] for row in cur.fetchall()}
        for name,definition in INVOICE_COLUMNS:
            if name not in present:cur.execute('ALTER TABLE wisp_invoices ADD COLUMN '+name+' '+definition)
        cur.execute('SHOW COLUMNS FROM wisp_sale_sources')
        if 'package_service_type' not in {row['Field'] for row in cur.fetchall()}:
            cur.execute("ALTER TABLE wisp_sale_sources ADD COLUMN package_service_type VARCHAR(16) NOT NULL DEFAULT ''")

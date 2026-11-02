"""Additive durable event-to-internal-server snapshots, without financial rewrites."""

TABLE_SQL = '''CREATE TABLE IF NOT EXISTS wisp_sale_sources (
    event_kind VARCHAR(16) NOT NULL, event_id BIGINT NOT NULL,
    server_key VARCHAR(64) NOT NULL DEFAULT 'unknown', nas_address VARCHAR(128) NOT NULL DEFAULT '',
    nas_label VARCHAR(128) NOT NULL DEFAULT '', service_type VARCHAR(16) NOT NULL DEFAULT '',
    server_name VARCHAR(128) NOT NULL DEFAULT '', server_interface VARCHAR(128) NOT NULL DEFAULT '',
    evidence_uid VARCHAR(64) NOT NULL DEFAULT '', attribution VARCHAR(40) NOT NULL DEFAULT 'unknown',
    service_attribution VARCHAR(40) NOT NULL DEFAULT '',
    resolution_status VARCHAR(24) NOT NULL DEFAULT 'unknown',
    resolution_reason VARCHAR(128) NOT NULL DEFAULT '',
    resolution_version INT NOT NULL DEFAULT 0, reviewed_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(event_kind,event_id), INDEX idx_sale_server(server_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci'''

AUDIT_SQL = '''CREATE TABLE IF NOT EXISTS wisp_sale_source_audit (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    event_kind VARCHAR(16) NOT NULL,event_id BIGINT NOT NULL,
    evidence_hash CHAR(64) NOT NULL,before_json TEXT NOT NULL,after_json TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_sale_source_event(event_kind,event_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci'''


def clear_legacy_sale_sources(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='wisp_sale_sources'")
        if cur.fetchone():
            cur.execute('DELETE FROM wisp_sale_sources')
        cur.execute("SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='wisp_sale_source_audit'")
        if cur.fetchone():cur.execute('DELETE FROM wisp_sale_source_audit')
    conn.commit()

# Inserted in the existing native first-activation trigger, using this packet's
# NEW values. Never uses the account's latest later session.
ACTIVATION_SOURCE_SQL = '''
        INSERT IGNORE INTO wisp_sale_sources
            (event_kind,event_id,server_key,nas_address,nas_label,service_type,server_name,server_interface,evidence_uid,attribution)
        SELECT 'voucher',s.id,'pending',COALESCE(NEW.nasipaddress,''),
            COALESCE((SELECT MIN(shortname) FROM nas WHERE nasname=NEW.nasipaddress COLLATE utf8mb4_unicode_ci),NEW.nasipaddress,''),
            COALESCE(p.service_type,''),COALESCE(NEW.calledstationid,''),
            CASE WHEN p.service_type='pppoe' THEN COALESCE(NEW.nasportid,'') ELSE '' END,
            COALESCE(NEW.acctuniqueid,''),'accounting_start'
        FROM wisp_voucher_sales s JOIN wisp_packages p ON p.id=v_package_id
        WHERE s.first_activation_voucher_id=v_id;
'''


def install_sales_source_schema(conn):
    from database.db import is_mysql_conn
    if is_mysql_conn(conn):
        with conn.cursor() as cur:
            cur.execute(TABLE_SQL)
            cur.execute(AUDIT_SQL)
            cur.execute('SHOW INDEX FROM wisp_sale_source_audit')
            audit_indexes={row['Key_name'] for row in cur.fetchall()}
            if 'uq_sale_source_evidence' in audit_indexes:
                cur.execute('ALTER TABLE wisp_sale_source_audit DROP INDEX uq_sale_source_evidence')
            if 'idx_sale_source_event' not in audit_indexes:
                cur.execute('ALTER TABLE wisp_sale_source_audit ADD INDEX idx_sale_source_event(event_kind,event_id)')
            cur.execute("SHOW COLUMNS FROM wisp_sale_sources")
            present={row['Field'] for row in cur.fetchall()}
            for name, definition in (
                ('service_attribution',"VARCHAR(40) NOT NULL DEFAULT ''"),
                ('resolution_status',"VARCHAR(24) NOT NULL DEFAULT 'unknown'"),
                ('resolution_reason',"VARCHAR(128) NOT NULL DEFAULT ''"),
                ('resolution_version','INT NOT NULL DEFAULT 0'),('reviewed_at','DATETIME NULL')):
                if name not in present:cur.execute(f'ALTER TABLE wisp_sale_sources ADD COLUMN {name} {definition}')
            cur.execute('SELECT VERSION() AS version')
            maria = 'mariadb' in str(cur.fetchone()['version']).lower()
            from database.schema_objects import ensure_schema_object
            ensure_schema_object(cur, '''CREATE TRIGGER trg_sale_source_key BEFORE INSERT ON wisp_sale_sources
                FOR EACH ROW BEGIN
                  SET NEW.nas_address=COALESCE(INET6_NTOA(INET6_ATON(TRIM(NEW.nas_address))),''),NEW.server_name=TRIM(NEW.server_name),NEW.server_interface=TRIM(NEW.server_interface);
                  IF NEW.service_type='hotspot' THEN SET NEW.server_interface=''; END IF;
                  IF NEW.service_type NOT IN ('hotspot','pppoe') THEN SET NEW.service_type=''; END IF;
                  IF INET6_ATON(NEW.nas_address) IS NULL OR NEW.nas_address IN ('0.0.0.0','::','::1')
                     OR NEW.nas_address LIKE '127.%' OR (NEW.service_type='pppoe' AND NEW.server_interface='')
                     OR CONCAT(NEW.server_name,NEW.server_interface) REGEXP '[[:cntrl:]]' THEN
                    SET NEW.server_key='unknown',NEW.attribution='unknown',NEW.nas_address='',NEW.nas_label='',NEW.server_name='',NEW.server_interface='',NEW.evidence_uid='';
                  ELSEIF NEW.service_type='' OR NEW.server_name='' THEN
                    SET NEW.server_key='unknown',NEW.attribution='unknown';
                  ELSE
                    SET NEW.server_key=SHA2(CONCAT(CHAR_LENGTH(NEW.nas_address),':',NEW.nas_address,
                        CHAR_LENGTH(NEW.service_type),':',NEW.service_type,CHAR_LENGTH(NEW.server_name),':',NEW.server_name,
                        CHAR_LENGTH(NEW.server_interface),':',NEW.server_interface),256);
                  END IF;
                END''', maria=maria)
        conn.commit()

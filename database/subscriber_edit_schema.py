"""Additive subscriber identity ownership; physical RADIUS history is never renamed."""


def clear_legacy_restore_ownership(conn, voucher_only=False):
    """A legacy backup has no identity metadata; never reuse the newer DB's IDs."""
    with conn.cursor() as cur:
        tables = ('wisp_voucher_names','wisp_voucher_edit_receipts')
        if not voucher_only:
            tables += ('wisp_subscriber_names','wisp_subscriber_edit_receipts')
        for table in tables:
            cur.execute('SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s',(table,))
            if cur.fetchone():cur.execute(f'DELETE FROM {table}')
        for table in ('radacct','radacct_archive','wisp_imported_session_history'):
            for owner in (('account_voucher_id',) if voucher_only else ('account_subscriber_id','account_voucher_id')):
                cur.execute("SELECT 1 FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s",(table,owner))
                if cur.fetchone():cur.execute(f'UPDATE {table} SET account_username=NULL,{owner}=NULL WHERE {owner} IS NOT NULL')
    conn.commit()


def install_subscriber_edit_schema(conn, mysql=True):
    from database.account_mac_schema import install_account_mac_schema
    install_account_mac_schema(conn)
    suffix = ' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci' if mysql else ''
    with conn.cursor() as cur:
        cur.execute('''CREATE TABLE IF NOT EXISTS wisp_subscriber_names (
            username VARCHAR(64) PRIMARY KEY, subscriber_id BIGINT NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )''' + suffix)
        cur.execute('''CREATE TABLE IF NOT EXISTS wisp_subscriber_edit_receipts (
            operation_id VARCHAR(64) PRIMARY KEY, subscriber_id BIGINT NOT NULL,
            actor VARCHAR(64) NOT NULL, fingerprint VARCHAR(64) NOT NULL,
            result TEXT NOT NULL, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )''' + suffix)
        cur.execute('''CREATE TABLE IF NOT EXISTS wisp_voucher_names (
            username VARCHAR(64) PRIMARY KEY, voucher_id BIGINT NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )''' + suffix)
        cur.execute('''CREATE TABLE IF NOT EXISTS wisp_voucher_edit_receipts (
            operation_id VARCHAR(64) PRIMARY KEY, voucher_id BIGINT NOT NULL,
            actor VARCHAR(64) NOT NULL, fingerprint VARCHAR(64) NOT NULL,
            result TEXT NOT NULL, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )''' + suffix)
        for table in ('radacct', 'radacct_archive', 'wisp_imported_session_history'):
            cur.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION", (table,))
            columns = [x['COLUMN_NAME'] for x in cur.fetchall()]
            if not columns:
                continue
            additions = {
                'account_subscriber_id': 'BIGINT NULL',
                'account_voucher_id': 'BIGINT NULL',
                'account_username': 'VARCHAR(64) NULL',
                'account_read_username': 'VARCHAR(64) GENERATED ALWAYS AS (COALESCE(account_username,username)) VIRTUAL',
            }
            for name, definition in additions.items():
                if name not in columns:
                    cur.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
            cur.execute(f'SHOW INDEX FROM {table}')
            indexes = {x['Key_name'] for x in cur.fetchall()}
            if 'idx_subscriber_account_read' not in indexes:
                cur.execute(f'CREATE INDEX idx_subscriber_account_read ON {table}(account_read_username,acctstarttime)')
            if 'idx_subscriber_account_owner' not in indexes:
                cur.execute(f'CREATE INDEX idx_subscriber_account_owner ON {table}(account_subscriber_id)')
            if 'idx_voucher_account_owner' not in indexes:
                cur.execute(f'CREATE INDEX idx_voucher_account_owner ON {table}(account_voucher_id)')
            # A direct MERGE view exposes an indexed generated column, so lookup
            # remains indexed and SELECT FOR UPDATE locks physical rows.
            fields = ','.join('a.account_read_username AS username' if n == 'username' else 'a.`'+n+'`' for n in columns if n not in additions)
            statement = f'''CREATE OR REPLACE VIEW {table}_owned AS
                SELECT {fields}, a.username AS original_username FROM {table} a'''
            cur.execute(statement)
            if table == 'radacct':
                from database.schema_objects import ensure_schema_object
                cur.execute('SELECT VERSION() AS version')
                maria='mariadb' in str(cur.fetchone()['version']).lower()
                ensure_schema_object(cur,'''CREATE TRIGGER trg_subscriber_account_owner
                    BEFORE INSERT ON radacct FOR EACH ROW
                    BEGIN
                      DECLARE owner_id BIGINT DEFAULT NULL;
                      DECLARE owner_name VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL;
                      DECLARE CONTINUE HANDLER FOR NOT FOUND SET owner_id=NULL;
                      SELECT s.id,s.username INTO owner_id,owner_name
                        FROM wisp_subscriber_names n JOIN wisp_subscribers s ON s.id=n.subscriber_id
                        WHERE n.username COLLATE utf8mb4_unicode_ci=NEW.username COLLATE utf8mb4_unicode_ci LIMIT 1;
                      IF owner_id IS NOT NULL THEN
                        SET NEW.account_subscriber_id=owner_id,NEW.account_username=owner_name;
                      ELSE
                        SELECT v.id,v.username INTO owner_id,owner_name
                          FROM wisp_voucher_names n JOIN wisp_vouchers v ON v.id=n.voucher_id
                          WHERE n.username COLLATE utf8mb4_unicode_ci=NEW.username COLLATE utf8mb4_unicode_ci LIMIT 1;
                        IF owner_id IS NOT NULL THEN
                          SET NEW.account_voucher_id=owner_id,NEW.account_username=owner_name;
                        END IF;
                      END IF;
                    END''',maria=maria)
    conn.commit()

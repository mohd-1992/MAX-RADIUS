"""Additive, idempotent MAC allow-list schema; never infer restrictions from traffic."""


def clear_legacy_mac_restore(conn):
    """A backup without MAC metadata cannot inherit policies for unrelated IDs."""
    with conn.cursor() as cur:
        for table in ('wisp_account_mac_addresses', 'wisp_account_mac_policies', 'wisp_account_mac_receipts'):
            cur.execute('SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s', (table,))
            if cur.fetchone():
                cur.execute(f'DELETE FROM {table}')
    conn.commit()


def install_account_mac_schema(conn):
    from database.db import is_mysql_conn
    mysql = is_mysql_conn(conn)
    suffix = ' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci' if mysql else ''
    with conn.cursor() as cur:
        cur.execute('''CREATE TABLE IF NOT EXISTS wisp_account_mac_policies (
            account_type VARCHAR(16) NOT NULL, account_id BIGINT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(account_type,account_id)
        )''' + suffix)
        cur.execute('''CREATE TABLE IF NOT EXISTS wisp_account_mac_addresses (
            account_type VARCHAR(16) NOT NULL, account_id BIGINT NOT NULL,
            mac VARCHAR(17) NOT NULL, note VARCHAR(250) NOT NULL DEFAULT '',
            PRIMARY KEY(account_type,account_id,mac)
        )''' + suffix)
        cur.execute('''CREATE TABLE IF NOT EXISTS wisp_account_mac_receipts (
            operation_id VARCHAR(32) PRIMARY KEY, account_type VARCHAR(16) NOT NULL,
            account_id BIGINT NOT NULL, actor VARCHAR(64) NOT NULL,
            fingerprint VARCHAR(64) NOT NULL, result TEXT NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )''' + suffix)
        view = '''CREATE VIEW wisp_account_mac_usernames AS
            SELECT p.account_type,p.account_id,p.enabled,s.username
              FROM wisp_account_mac_policies p JOIN wisp_subscribers s
                ON p.account_type='subscriber' AND p.account_id=s.id
            UNION ALL
            SELECT p.account_type,p.account_id,p.enabled,v.username
              FROM wisp_account_mac_policies p JOIN wisp_vouchers v
                ON p.account_type='voucher' AND p.account_id=v.id'''
        if mysql:
            cur.execute(view.replace('CREATE VIEW', 'CREATE OR REPLACE VIEW', 1))
            cur.execute('''CREATE OR REPLACE VIEW wisp_mac_radcheck AS
                SELECT r.* FROM radcheck r WHERE r.attribute<>'Calling-Station-Id'
                  OR NOT EXISTS(SELECT 1 FROM wisp_account_mac_usernames p
                     WHERE p.username=r.username COLLATE utf8mb4_unicode_ci)''')
            from database.schema_objects import ensure_schema_object
            cur.execute('SELECT VERSION() AS version')
            maria = 'mariadb' in str(cur.fetchone()['version']).lower()
            ensure_schema_object(cur, '''CREATE FUNCTION fn_account_mac_allowed(
                p_username VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci,
                p_station VARCHAR(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci)
                RETURNS INT READS SQL DATA
                BEGIN
                  RETURN NOT EXISTS(
                    SELECT 1 FROM wisp_account_mac_usernames p
                    WHERE p.username=p_username AND p.enabled=1 AND NOT EXISTS(
                      SELECT 1 FROM wisp_account_mac_addresses m
                      WHERE m.account_type=p.account_type AND m.account_id=p.account_id
                        AND m.mac=UPPER(REPLACE(p_station,'-',':'))));
                END''', maria=maria)
        else:
            cur.execute('DROP VIEW IF EXISTS wisp_account_mac_usernames')
            cur.execute(view)
        # Preserve actual legacy RADIUS restrictions, never a first-seen MAC
        # recorded by accounting. Lock the account rows as policy writers do.
        from core.mac_policy import normalize_mac
        for kind, table in (('subscriber', 'wisp_subscribers'), ('voucher', 'wisp_vouchers')):
            cur.execute(f'''SELECT a.id,r.value,r.op FROM {table} a
                LEFT JOIN wisp_account_mac_policies p ON p.account_type='{kind}' AND p.account_id=a.id
                LEFT JOIN radcheck r ON r.username=a.username AND r.attribute='Calling-Station-Id'
                WHERE p.account_id IS NULL ORDER BY a.id''' + (' FOR UPDATE' if mysql else ''))
            groups = {}
            for row in cur.fetchall():
                groups.setdefault(row['id'], []).append(row)
            for ident, checks in groups.items():
                if len(checks) != 1 or (checks[0]['value'] is not None and checks[0]['op'] != '=='):
                    continue  # Nonstandard legacy rules remain enforced, never discarded.
                old = checks[0]['value']
                try:
                    address = normalize_mac(old) if old is not None else None
                except ValueError:
                    continue
                marker = '%s' if mysql else '?'
                cur.execute(f'INSERT INTO wisp_account_mac_policies(account_type,account_id,enabled) VALUES({marker},{marker},{marker})', (kind, ident, int(address is not None)))
                if address:
                    cur.execute(f'INSERT INTO wisp_account_mac_addresses(account_type,account_id,mac,note) VALUES({marker},{marker},{marker},{marker})', (kind, ident, address, ''))
    conn.commit()

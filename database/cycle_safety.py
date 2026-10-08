"""Database guards shared by account writers and RADIUS cycle transitions."""

GUARD_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS wisp_renewal_guards (
 username VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci PRIMARY KEY,
 token CHAR(32) NOT NULL, expires_at DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


def install_cycle_safety(conn):
    """Serialize cross-table username changes, including direct SQL imports."""
    with conn.cursor() as cur:
        cur.execute(GUARD_TABLE_SQL)
        cur.execute("""CREATE TABLE IF NOT EXISTS wisp_username_write_lock (
            id INT PRIMARY KEY) ENGINE=InnoDB""")
        cur.execute('INSERT IGNORE INTO wisp_username_write_lock(id) VALUES(1)')
        cur.execute('SELECT VERSION() AS version')
        maria = 'mariadb' in str(cur.fetchone()['version']).lower()
        for table, other in (('wisp_subscribers', 'wisp_vouchers'),
                             ('wisp_vouchers', 'wisp_subscribers')):
            cur.execute(f"SHOW COLUMNS FROM {table} LIKE 'pause_reason'")
            if not cur.fetchone():
                cur.execute(f"ALTER TABLE {table} ADD COLUMN pause_reason VARCHAR(255) NOT NULL DEFAULT ''")
            for event in ('INSERT', 'UPDATE'):
                name = 'trg_' + table + '_username_' + event.lower()
                condition = 'TRUE' if event == 'INSERT' else 'NOT (OLD.username <=> NEW.username)'
                sql = f"""CREATE TRIGGER {name} BEFORE {event} ON {table} FOR EACH ROW
                BEGIN
                  DECLARE occupied BIGINT DEFAULT NULL;
                  DECLARE CONTINUE HANDLER FOR NOT FOUND SET occupied = NULL;
                  IF NEW.status = 'disabled' THEN
                    SET NEW.status = 'suspended';
                  END IF;
                  IF NEW.status = 'suspended' AND COALESCE(NEW.pause_reason,'') = '' THEN
                    SET NEW.pause_reason = 'إيقاف سابق أو مستورد';
                  END IF;
                  IF {condition} THEN
                    INSERT INTO wisp_username_write_lock(id) VALUES(1)
                     ON DUPLICATE KEY UPDATE id=VALUES(id);
                    SELECT id INTO occupied FROM {other}
                     WHERE username=NEW.username LIMIT 1 FOR UPDATE;
                    IF occupied IS NOT NULL THEN
                      SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Username already belongs to another account';
                    END IF;
                  END IF;
                END"""
                if maria:
                    sql = sql.replace('CREATE TRIGGER', 'CREATE OR REPLACE TRIGGER', 1)
                else:
                    cur.execute('DROP TRIGGER IF EXISTS ' + name)
                cur.execute(sql)
            cur.execute(f"UPDATE {table} SET status='suspended' WHERE status='disabled'")
            cur.execute(f"UPDATE {table} SET pause_reason='إيقاف سابق أو مستورد' WHERE status='suspended' AND pause_reason=''")
    conn.commit()

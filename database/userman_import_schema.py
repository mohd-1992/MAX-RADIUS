"""Additive, transactional import receipts; install before starting an import."""


def install_userman_import_schema(conn):
    with conn.cursor() as cur:
        cur.execute('SELECT @@SESSION.lock_wait_timeout AS previous_timeout')
        previous_timeout = cur.fetchone()['previous_timeout']
        cur.execute('SET SESSION lock_wait_timeout=2')
        try:
            cur.execute("SHOW INDEX FROM wisp_session_reservations WHERE Column_name='username' AND Seq_in_index=1")
            if not cur.fetchone():
                cur.execute('ALTER TABLE wisp_session_reservations ADD INDEX ix_userman_reservation_user(username)')
            cur.execute('''CREATE TABLE IF NOT EXISTS wisp_userman_import_batches (
                job_id CHAR(32) NOT NULL, batch_no INT NOT NULL,
                source_hash CHAR(64) NOT NULL, PRIMARY KEY(job_id,batch_no)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')
            cur.execute('''CREATE TABLE IF NOT EXISTS wisp_userman_import_journal (
                job_id CHAR(32) NOT NULL, kind VARCHAR(24) NOT NULL,
                identity_key VARCHAR(128) NOT NULL, before_json LONGTEXT NOT NULL,
                after_json LONGTEXT NOT NULL, state VARCHAR(24) NOT NULL DEFAULT 'applied',
                PRIMARY KEY(job_id,kind,identity_key)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')
        finally:
            cur.execute('SET SESSION lock_wait_timeout=%s', (previous_timeout,))
    conn.commit()

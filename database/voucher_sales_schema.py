"""An idempotent first-activation sale, without restricting renewal sales."""
from database.db import is_mysql_conn

def ensure_activation_sale_key(conn):
    if not is_mysql_conn(conn):
        return
    with conn.cursor() as cur:
        cur.execute("SELECT DATABASE() AS db")
        database = cur.fetchone()['db']
        lock = 'max-first-sale-schema:' + database
        cur.execute('SELECT GET_LOCK(%s,30) AS acquired', (lock,))
        if cur.fetchone()['acquired'] != 1:
            raise RuntimeError('First-sale schema migration is busy')
        try:
            cur.execute("SELECT 1 AS present FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='wisp_voucher_sales' AND COLUMN_NAME='first_activation_voucher_id'")
            if not cur.fetchone():
                cur.execute('ALTER TABLE wisp_voucher_sales ADD COLUMN first_activation_voucher_id BIGINT NULL')
            cur.execute("SELECT INDEX_NAME,NON_UNIQUE,COLUMN_NAME,SEQ_IN_INDEX FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='wisp_voucher_sales' AND INDEX_NAME='uq_voucher_first_activation'")
            rows = cur.fetchall()
            if not rows:
                cur.execute('ALTER TABLE wisp_voucher_sales ADD UNIQUE KEY uq_voucher_first_activation (first_activation_voucher_id)')
            elif len(rows) != 1 or rows[0]['NON_UNIQUE'] or rows[0]['COLUMN_NAME'] != 'first_activation_voucher_id':
                raise RuntimeError('Invalid first-activation sale index')
            # Mark one historical sale per voucher; preserve every financial
            # field and every subsequent renewal row. NULL permits renewals.
            cur.execute("""UPDATE wisp_voucher_sales s
                JOIN (SELECT voucher_id,MIN(id) AS first_id FROM wisp_voucher_sales GROUP BY voucher_id
                      HAVING SUM(first_activation_voucher_id IS NOT NULL)=0) f ON f.first_id=s.id
                SET s.first_activation_voucher_id=s.voucher_id
                WHERE s.first_activation_voucher_id IS NULL""")
            conn.commit()
        finally:
            cur.execute('SELECT RELEASE_LOCK(%s)', (lock,))

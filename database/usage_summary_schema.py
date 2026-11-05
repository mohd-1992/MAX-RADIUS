"""Settled voucher accounting aggregates; never fabricated RADIUS sessions.

The quota-only view is read-only. Operational settlement continues to use the
MERGE radacct_owned view and its physical row locks.
"""

TABLE_SQL = '''CREATE TABLE IF NOT EXISTS wisp_account_usage_summary (
 summary_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
 voucher_id BIGINT NOT NULL, username VARCHAR(64) NOT NULL, reseller_id BIGINT NULL,
 cycle_start DATETIME NULL, first_start DATETIME NOT NULL, last_start DATETIME NOT NULL,
 nasipaddress VARCHAR(45) NOT NULL DEFAULT '', calledstationid VARCHAR(128) NOT NULL DEFAULT '',
 nasportid VARCHAR(128) NOT NULL DEFAULT '', framedprotocol VARCHAR(32) NOT NULL DEFAULT '',
 servicetype VARCHAR(32) NOT NULL DEFAULT '', report_visible TINYINT NOT NULL DEFAULT 1,
 input_bytes DECIMAL(30,0) NOT NULL, output_bytes DECIMAL(30,0) NOT NULL,
 uptime_seconds DECIMAL(30,0) NOT NULL, session_count BIGINT NOT NULL,
 unconfirmed_sessions BIGINT NOT NULL DEFAULT 0,
 KEY idx_usage_voucher (voucher_id,first_start), KEY idx_usage_owner (reseller_id,first_start)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci'''

SEALS_SQL = '''CREATE TABLE IF NOT EXISTS wisp_account_usage_seals (
 acctuniqueid VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
 voucher_id BIGINT NOT NULL, input_bytes BIGINT NOT NULL, output_bytes BIGINT NOT NULL,
 uptime_seconds BIGINT NOT NULL, KEY idx_usage_seal_voucher(voucher_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci'''


def install_usage_summary_schema(conn):
    with conn.cursor() as cur:
        cur.execute(TABLE_SQL)
        cur.execute('SHOW COLUMNS FROM wisp_account_usage_summary')
        if 'report_visible' not in {r['Field'] for r in cur.fetchall()}:
            cur.execute('ALTER TABLE wisp_account_usage_summary ADD COLUMN report_visible TINYINT NOT NULL DEFAULT 1 AFTER servicetype')
        cur.execute('SHOW COLUMNS FROM wisp_account_usage_summary')
        if 'unconfirmed_sessions' not in {r['Field'] for r in cur.fetchall()}:
            cur.execute('ALTER TABLE wisp_account_usage_summary ADD COLUMN unconfirmed_sessions BIGINT NOT NULL DEFAULT 0')
        cur.execute(SEALS_SQL)
        cur.execute('SHOW COLUMNS FROM radacct_owned')
        names = [r['Field'] for r in cur.fetchall()]
        fields = {
            'radacctid': '-CAST(CONV(SUBSTR(h.summary_key,1,15),16,10) AS SIGNED)',
            'username': 'v.username', 'original_username': 'h.username',
            'acctsessionid': 'h.summary_key', 'acctuniqueid': 'LEFT(h.summary_key,32)',
            'acctstarttime': 'h.first_start', 'acctstoptime': 'h.first_start',
            'acctupdatetime': 'h.last_start', 'acctinputoctets': 'h.input_bytes',
            'acctoutputoctets': 'h.output_bytes', 'acctsessiontime': 'h.uptime_seconds',
            'acctterminatecause': "'Settled-Usage-Summary'",
        }
        for name in ('nasipaddress','calledstationid','nasportid','framedprotocol','servicetype'):
            fields[name] = 'h.' + name
        deltas = {'acctinputoctets':'input_bytes','acctoutputoctets':'output_bytes','acctsessiontime':'uptime_seconds'}
        def actual_field(n):
            if n == 'radacctid':return 'CAST(a.radacctid AS SIGNED)'
            if n in deltas:
                return f'CASE WHEN seal.acctuniqueid IS NULL THEN a.`{n}` ELSE GREATEST(0,CAST(a.`{n}` AS DECIMAL(30,0))-seal.{deltas[n]}) END'
            return 'a.`'+n+'`'
        actual = ','.join(actual_field(n)+' AS `'+n+'`' for n in names)
        summary = ','.join(fields.get(n, 'NULL')+' AS `'+n+'`' for n in names)
        cur.execute('''CREATE OR REPLACE VIEW radacct_quota_owned AS SELECT '''+actual+''',CASE WHEN seal.acctuniqueid IS NULL THEN 1 ELSE 0 END AS session_weight
            FROM radacct_owned a LEFT JOIN wisp_account_usage_seals seal ON seal.acctuniqueid=CONVERT(a.acctuniqueid USING ascii)
            UNION ALL SELECT '''+summary+''',h.session_count AS session_weight
            FROM wisp_account_usage_summary h JOIN wisp_vouchers v ON v.id=h.voucher_id''')


def clear_legacy_usage_summary(conn):
    with conn.cursor() as cur:
        cur.execute('DELETE FROM wisp_account_usage_summary')
        cur.execute('DELETE FROM wisp_account_usage_seals')

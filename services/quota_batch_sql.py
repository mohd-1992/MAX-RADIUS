"""Narrow, read-only equivalent of the quota view for account-list reads.

Keep seal deltas and settled-summary identity identical to usage_summary_schema.
This never replaces the operational accounting view or its physical row locks.
"""


def batch_quota_source(usernames):
    names = tuple(dict.fromkeys(usernames))
    if not names:
        raise ValueError('A quota source requires at least one account name.')
    marks = ','.join('?' for _ in names)
    counters = ','.join(
        f'CASE WHEN seal.acctuniqueid IS NULL THEN a.{column} ELSE '
        f'GREATEST(0,CAST(a.{column} AS DECIMAL(30,0))-seal.{sealed}) END AS {column}'
        for column, sealed in (
            ('acctinputoctets', 'input_bytes'),
            ('acctoutputoctets', 'output_bytes'),
            ('acctsessiontime', 'uptime_seconds')))
    source = f'''(
        SELECT CAST(a.radacctid AS SIGNED) AS radacctid,a.username,
            a.acctstarttime,a.acctstoptime,{counters}
        FROM radacct_owned a LEFT JOIN wisp_account_usage_seals seal
            ON seal.acctuniqueid=CONVERT(a.acctuniqueid USING ascii)
        WHERE a.username IN ({marks})
        UNION ALL
        SELECT -CAST(CONV(SUBSTR(h.summary_key,1,15),16,10) AS SIGNED),v.username,
            h.first_start,h.first_start,h.input_bytes,h.output_bytes,h.uptime_seconds
        FROM wisp_account_usage_summary h JOIN wisp_vouchers v ON v.id=h.voucher_id
        WHERE v.username IN ({marks})
    )'''
    return source, names + names

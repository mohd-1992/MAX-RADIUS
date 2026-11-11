"""Dashboard periods over the sales ledger and shared measured-traffic summaries."""
import datetime as dt
from zoneinfo import ZoneInfo
from services.sales_reporting_service import date_bounds, LEDGER, ARABIC_MONTHS

# The dashboard retains recorded sales amounts. Invoice cost belongs to the
# separate subscription-cost report and must not remove historical cash sales.
DASHBOARD_LEDGER=LEDGER.replace('i.report_cost,i.report_cost','i.amount,i.amount')


def local_today(now, timezone):
    return (now or dt.datetime.now(dt.timezone.utc)).astimezone(ZoneInfo(timezone)).date()


def windows_sql(windows, field, timezone):
    cases, values = [], []
    for label, first, last in windows:
        start, end = date_bounds('custom', first.isoformat(), last.isoformat(), timezone=timezone)
        cases.append(f'WHEN {field}>=%s AND {field}<%s THEN %s')
        values.extend((start, end, label))
    return 'CASE '+' '.join(cases)+' END', tuple(values)


def traffic_series(query, days=7, owner=None, now=None, timezone='Asia/Aden'):
    from services.traffic_sample_service import daily_rows
    return daily_rows(query,days=days,owner=owner,now=now,timezone=timezone)


def sales_series(query, owner=None, now=None, timezone='Asia/Aden'):
    today = local_today(now, timezone)
    windows = []
    for offset in range(5,-1,-1):
        index = today.year*12+today.month-1-offset
        year, month = index//12, index%12+1
        first = dt.date(year,month,1)
        next_index = index+1
        following = dt.date(next_index//12,next_index%12+1,1)
        windows.append((f'{year:04d}-{month:02d}',first,min(today,following-dt.timedelta(days=1))))
    bucket, values = windows_sql(windows, 'e.event_at', timezone)
    start, end = date_bounds('custom',windows[0][1].isoformat(),today.isoformat(),timezone=timezone)
    scope = '' if owner is None else " AND e.event_kind='voucher' AND e.reseller_id=%s"
    params = values+(start,end)+(() if owner is None else (int(owner),))
    rows = query(f'''SELECT {bucket} AS bucket,e.event_kind,SUM(e.price) AS total,SUM(e.price IS NULL) unknown_amounts
        FROM {DASHBOARD_LEDGER} WHERE e.event_at>=%s AND e.event_at<%s{scope}
        GROUP BY bucket,e.event_kind''', params)
    indexed = {(r['bucket'],r['event_kind']):r['total'] for r in rows}
    labels, vouchers, invoices = [], [], []
    for label, first, last in windows:
        labels.append(ARABIC_MONTHS[first.month-1]+(f' {first.year}' if first.year!=today.year else ''))
        voucher=indexed.get((label,'voucher'),0)
        invoice=indexed.get((label,'invoice'),0)
        vouchers.append(None if voucher is None else float(voucher))
        invoices.append(None if invoice is None else float(invoice))
    return dict(labels=labels,voucher_sales=vouchers,package_sales=invoices,unknown_amounts=sum(int(r.get('unknown_amounts') or 0) for r in rows),
                sales=[None if v is None and i is None else round((v or 0)+(i or 0),2) for v,i in zip(vouchers,invoices)])


def read_series(reader, **kwargs):
    from database.db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
            def query(sql, params=()):
                cur.execute(sql,params)
                return list(cur.fetchall())
            try:
                return reader(query,**kwargs)
            finally:
                conn.rollback()


def daily_traffic_metrics(owner=None, now=None, timezone=None):
    from core.time_service import get_configured_timezone_name
    from core.rate_limit import format_bytes
    timezone = timezone or get_configured_timezone_name()
    today = local_today(now,timezone).isoformat()
    try:
        row = read_series(traffic_series,days=1,owner=owner,now=now,timezone=timezone)[0]
        down, up = row['download'], row['upload']
        total = None if down is None or up is None else int(down)+int(up)
        show = lambda value: format_bytes(value) if value is not None else 'غير متاح'
        return dict(traffic_in_str=show(up),traffic_out_str=show(down),traffic_total_str=show(total),
                    traffic_status_text='',traffic_day=today)
    except Exception:
        return dict(traffic_in_str='غير متاح',traffic_out_str='غير متاح',traffic_total_str='غير متاح',
                    traffic_status_text='تعذر تحديث الاستهلاك',traffic_day=today)

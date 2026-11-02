"""Calendar axes in the configured display timezone; database timestamps are UTC+3."""
import datetime as dt
from zoneinfo import ZoneInfo

STORAGE = dt.timezone(dt.timedelta(hours=3))


def local_time(value, timezone):
    if isinstance(value, str):
        value = dt.datetime.fromisoformat(value)
    return (value.replace(tzinfo=STORAGE) if value.tzinfo is None else value).astimezone(ZoneInfo(timezone))


def axis(selected, now=None, timezone='Asia/Aden', extent=(None, None)):
    from services.sales_reporting_service import date_bounds, month_label
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(ZoneInfo(timezone))
    start, end = date_bounds(selected['period'], selected.get('date_from', ''), selected.get('date_to', ''), now, timezone)
    first = (local_time(start or extent[0], timezone) if start or extent[0] else now).replace(hour=0,minute=0,second=0,microsecond=0)
    last = local_time(end, timezone) if end else ((local_time(extent[1], timezone).replace(hour=0,minute=0,second=0,microsecond=0)+dt.timedelta(days=1)) if extent[1] else first+dt.timedelta(days=1))
    period = selected['period']
    if period == 'custom' and selected.get('date_from') and not selected.get('date_to'):
        last = now.replace(hour=0,minute=0,second=0,microsecond=0)+dt.timedelta(days=1)
    if period == 'this_week':
        last = first + dt.timedelta(days=7)
    elif period in ('this_month', 'last_month'):
        last = (first.replace(day=28)+dt.timedelta(days=4)).replace(day=1)
    elif period == 'this_year':
        last = first.replace(year=first.year+1, month=1, day=1)
    if last <= first:
        # An open custom range may end before the oldest available observation.
        if selected.get('date_from'):
            last = first+dt.timedelta(days=1)
        else:
            first = last-dt.timedelta(days=1)
    mode = 'hour' if (last-first).total_seconds() <= 86400 else 'month' if period == 'this_year' or (last-first).days > 62 else 'day'
    current = first.replace(minute=0, second=0, microsecond=0)
    if mode == 'month': current = current.replace(day=1, hour=0)
    windows = []
    while current < last:
        if len(windows)>=1200:
            raise ValueError('اختر فترة أقصر لعرض الرسم البياني.')
        following = ((current.replace(day=28)+dt.timedelta(days=4)).replace(day=1) if mode == 'month' else current+dt.timedelta(hours=1) if mode == 'hour' else current+dt.timedelta(days=1))
        label = current.strftime('%H:00') if mode == 'hour' else month_label(current.strftime('%Y-%m')) if mode == 'month' else current.strftime('%Y-%m-%d')
        windows.append((label, current, following))
        current = following
    return mode, windows, now


def bucket_index(value, windows, timezone):
    value = local_time(value, timezone)
    return next((i for i, (_, start, end) in enumerate(windows) if start <= value < end), None)


def sales_series(query, source, where, params, selected, now=None, timezone='Asia/Aden'):
    # Sales have an exact event time: an empty elapsed bucket is a recorded zero.
    extent=(None,None)
    if selected['period']=='all' or selected['period']=='custom' and (not selected.get('date_from') or not selected.get('date_to')):
        edges=query(f'SELECT MIN(e.event_at) first_seen,MAX(e.event_at) last_seen FROM {source} WHERE {where}',params)[0]
        extent=(edges['first_seen'],edges['last_seen'])
    mode, windows, clock = axis(selected, now, timezone, extent)
    clauses=[];boundaries=[]
    for index,(_,start,end) in enumerate(windows):
        clauses.append(f'WHEN e.event_at>=%s AND e.event_at<%s THEN {index}')
        boundaries.extend((start.astimezone(STORAGE).replace(tzinfo=None),end.astimezone(STORAGE).replace(tzinfo=None)))
    bucket='CASE '+' '.join(clauses)+' END'
    rows=query(f'SELECT {bucket} bucket,SUM(e.price) amount FROM {source} WHERE {where} AND e.event_at IS NOT NULL GROUP BY bucket',tuple(boundaries)+params)
    values = [0.0 if start <= clock else None for _, start, end in windows]
    for row in rows:
        index = row['bucket']
        if index is not None:
            values[index] = (values[index] or 0) + float(row['amount'])
    return dict(labels=[w[0] for w in windows], sales=values, granularity=mode)

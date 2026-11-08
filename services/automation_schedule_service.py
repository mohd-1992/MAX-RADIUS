"""Bounded, timezone-aware wall-clock schedules; no application imports."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import calendar
import re


def normalize(value):
    if not isinstance(value, dict):
        raise ValueError('جدول غير صالح')
    s = dict(value)
    if s.get('mode') not in ('daily', 'weekly', 'monthly', 'interval', 'once'):
        raise ValueError('نمط جدولة غير صالح')
    s['timezone'] = str(s.get('timezone', 'Asia/Aden'))
    ZoneInfo(s['timezone'])
    datetime.strptime(s['start'], '%Y-%m-%d')
    times = s.get('times', ['03:00'])
    if not isinstance(times, list) or not 1 <= len(times) <= 6:
        raise ValueError('حدد من وقت إلى ستة أوقات')
    if any(not isinstance(t, str) or not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', t) for t in times):
        raise ValueError('وقت غير صالح')
    if len(set(times)) != len(times):
        raise ValueError('وقت التشغيل مكرر')
    s['times'] = sorted(times)
    s['days'] = sorted(set(int(d) for d in s.get('days', [])))
    if any(d not in range(7) for d in s['days']) or (s['mode'] == 'weekly' and not s['days']):
        raise ValueError('حدد أيام التشغيل')
    s['monthDay'] = int(s.get('monthDay', 1))
    if not 1 <= s['monthDay'] <= 31 or s.get('fallback', 'last') not in ('last', 'skip'):
        raise ValueError('يوم الشهر غير صالح')
    s['fallback'] = s.get('fallback', 'last')
    s['interval'] = int(s.get('interval', 1))
    s['unit'] = s.get('unit', 'hours')
    if not 1 <= s['interval'] <= 365 or s['unit'] not in ('minutes', 'hours', 'days'):
        raise ValueError('فترة غير صالحة')
    s['startTime'] = s.get('startTime', s['times'][0])
    if not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', s['startTime']):
        raise ValueError('وقت بداية غير صالح')
    s['endMode'] = s.get('endMode', 'never')
    if s['endMode'] not in ('never', 'date', 'count'):
        raise ValueError('حد نهاية غير صالح')
    if s['endMode'] == 'date':
        datetime.strptime(s['endDate'], '%Y-%m-%d')
        if s['endDate'] < s['start']:
            raise ValueError('النهاية تسبق البداية')
    s['maxRuns'] = int(s.get('maxRuns', 10))
    if not 1 <= s['maxRuns'] <= 10000:
        raise ValueError('عدد التشغيلات غير صالح')
    if s['mode'] == 'once' and len(s['times']) != 1:
        raise ValueError('المرة الواحدة تحتاج وقتًا واحدًا')
    return s


def next_times(schedule, after=None, count=5, scheduled_count=0):
    s = normalize(schedule)
    tz = ZoneInfo(s['timezone'])
    after = after or datetime.now(timezone.utc)
    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)
    local = after.astimezone(tz)
    remaining = s['maxRuns'] - scheduled_count if s['endMode'] == 'count' else count
    limit = min(count, max(0, remaining))
    if not limit:
        return []
    start = datetime.strptime(s['start'], '%Y-%m-%d').replace(tzinfo=tz)
    end = datetime.strptime(s['endDate'], '%Y-%m-%d').date() if s['endMode'] == 'date' else None
    result = []
    if s['mode'] == 'interval':
        h, m = map(int, s['startTime'].split(':'))
        anchor = (start + timedelta(hours=h, minutes=m)).astimezone(timezone.utc)
        step = timedelta(**{s['unit']: s['interval']})
        skipped = max(0, int((after - anchor) // step) + 1)
        for n in range(limit):
            d = anchor + (skipped + n) * step
            if end and d.astimezone(tz).date() > end:
                break
            result.append(d)
        return result
    first = start.date() if s['mode'] == 'once' else max(start.date(), local.date())
    for n in range(1462):
        day = first + timedelta(days=n)
        if end and day > end or s['mode'] == 'once' and n:
            break
        if s['mode'] == 'weekly' and (day.weekday() + 1) % 7 not in s['days']:
            continue
        last = calendar.monthrange(day.year, day.month)[1]
        if s['mode'] == 'monthly' and (day.day != min(last, s['monthDay']) or
                s['monthDay'] > last and s['fallback'] == 'skip'):
            continue
        for clock in s['times']:
            h, m = map(int, clock.split(':'))
            d = datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
            # Skip nonexistent DST times; ambiguous wall time uses its first fold.
            utc = d.astimezone(timezone.utc)
            if utc.astimezone(tz).replace(tzinfo=None) != d.replace(tzinfo=None):
                continue
            if utc > after:
                result.append(utc)
                if len(result) == limit:
                    return result
    return result

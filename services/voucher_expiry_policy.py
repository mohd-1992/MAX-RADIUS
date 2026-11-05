"""One conservative expiry classification for counters, filters and locked deletion.

Unknown/imported/manual reasons remain 'other'; they are eligible only for 'all'.
Time includes validity and connection-time exhaustion, as the UI explicitly says.
"""
import re

QUOTA_REASONS = frozenset((
    'تم استهلاك رصيد البيانات بالكامل', 'تم استهلاك رصيد البيانات',
    'نفاد رصيد البيانات', 'نفاذ رصيد البيانات', 'انتهاء رصيد البيانات',
    'quota', 'quota_exhausted', 'data_quota_exhausted', 'volume_quota_exceeded',
    'data quota exceeded', 'data quota exhausted', 'quota exceeded',
    'data limit reached', 'volume quota exceeded',
))
TIME_REASONS = frozenset((
    'تم استهلاك مدة الصلاحية', 'تم استهلاك رصيد الوقت المسموح للباقة',
    'انتهاء الصلاحية', 'انتهاء مدة الصلاحية', 'انتهاء وقت الاتصال',
    'time_expired', 'validity_expired', 'uptime_exhausted', 'time expired',
    'validity expired', 'uptime limit reached', 'time quota exceeded',
    'session time limit exceeded',
))


def expiry_category(reason):
    value = str(reason or '').strip(' ').lower()
    if value in QUOTA_REASONS:
        return 'quota'
    if value in TIME_REASONS:
        return 'time'
    return 'other'


def category_sql(column='expire_reason'):
    if not re.fullmatch(r'(?:[A-Za-z_]+\.)?[A-Za-z_]+', column):
        raise ValueError('Invalid expiry column')
    def literals(values):
        return ','.join("'"+v.replace("'", "''")+"'" for v in sorted(values))
    return (f"CASE WHEN LOWER(TRIM(COALESCE({column},''))) IN ({literals(QUOTA_REASONS)}) THEN 'quota' "
            f"WHEN LOWER(TRIM(COALESCE({column},''))) IN ({literals(TIME_REASONS)}) THEN 'time' ELSE 'other' END")

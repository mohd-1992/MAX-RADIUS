"""Input validation for package forms and bulk settings (no application imports)."""
import math
import re


def nonnegative_number(value):
    try:
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise ValueError
        return number
    except (ValueError, TypeError, OverflowError):
        raise ValueError('يرجى إدخال قيمة رقمية صالحة غير سالبة.') from None


def integer(value, minimum=0, maximum=2147483647):
    number = nonnegative_number(value)
    if not number.is_integer() or not minimum <= number <= maximum:
        raise ValueError('يرجى إدخال عدد صحيح ضمن الحدود المسموحة.')
    return int(number)


def validate_package_form(form):
    values = dict(form)
    if not str(values.get('name', '')).strip():
        raise ValueError('اسم الباقة مطلوب.')
    if values.get('service_type', 'hotspot') not in ('hotspot', 'pppoe', 'both'):
        raise ValueError('نوع الخدمة غير صالح.')
    if values.get('validity_unit', 'days') not in ('days', 'hours', 'minutes', 'months'):
        raise ValueError('وحدة الصلاحية غير صالحة.')
    for key in ('price', 'cost'):
        values[key] = nonnegative_number(values.get(key) or 0)
    for key, default, minimum, maximum in (
        ('volume_quota_mb', 0, 0, 2147483647),
        ('uptime_limit_mins', 0, 0, 2147483647),
        ('validity_value', values.get('validity_days') or 0, 0, 71582788),
        ('loyalty_points', 0, 0, 2147483647),
        ('burst_time', 16, 1, 120), ('priority', 8, 1, 8),
        ('simultaneous_sessions', 1, 1, 10),
    ):
        values[key] = integer(values.get(key) if values.get(key) not in (None, '') else default, minimum, maximum)
    for key in ('rate_download', 'rate_upload', 'burst_download', 'burst_upload',
                'burst_threshold_down', 'burst_threshold_up', 'min_download', 'min_upload'):
        value = str(values.get(key, '2M' if key == 'rate_download' else '1M' if key == 'rate_upload' else '')).strip()
        if value.lower() in ('unlimited', 'مفتوح', 'غير محدود', '0b'):
            value = '0'
        if value and not re.fullmatch(r'(?:\d+(?:\.\d+)?|\.\d+)[kKmMgG]?', value):
            raise ValueError('صيغة السرعة غير صالحة؛ استخدم مثلاً 2M أو 512k أو 0.')
        if not value and key in ('rate_download', 'rate_upload'):
            raise ValueError('السرعة الأساسية مطلوبة؛ استخدم 0 للسرعة المفتوحة.')
        if value and float(value.rstrip('kKmMgG')) == 0:
            value = '0'
        values[key] = value
    def bps(key):
        value = values[key].lower()
        return float(value.rstrip('kmg') or 0) * {'k': 1000, 'm': 1000000, 'g': 1000000000}.get(value[-1:] or '', 1)
    down, up = bps('rate_download'), bps('rate_upload')
    if (down == 0) != (up == 0):
        raise ValueError('السرعة المفتوحة تتطلب 0 في التحميل والرفع معًا؛ فتح اتجاه واحد غير مدعوم في باني السمات الحالي.')
    if down > 0 and up > 0:
        burst_down, burst_up = bps('burst_download'), bps('burst_upload')
        if (burst_down > 0 or burst_up > 0) and (burst_down <= down or burst_up <= up):
            raise ValueError('أدخل سرعتي بيرست أعلى من السرعتين الأساسيتين، أو اترك حقلي البيرست فارغين.')
        if bps('min_download') > down or bps('min_upload') > up:
            raise ValueError('الحد الأدنى للسرعة يجب ألا يتجاوز السرعة الأساسية.')
    return values

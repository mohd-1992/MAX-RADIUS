"""Canonical complete package rates and fail-closed historical snapshot reads.

No database access or contract mutation. Missing historical burst cannot be
reconstructed from today's package, so an unknown limited snapshot is refused.
"""


def package_rate_limit(package):
    from core.rate_limit import build_mikrotik_rate_limit, is_zero_or_empty_rate
    down = package.get('rate_download') or '0'
    up = package.get('rate_upload') or '0'
    if is_zero_or_empty_rate(down) != is_zero_or_empty_rate(up):
        raise ValueError('يجب تحديد سرعتي الرفع والتنزيل معًا، أو جعلهما مفتوحتين معًا.')
    return build_mikrotik_rate_limit(
        download=down, upload=up,
        burst_down=package.get('burst_download'), burst_up=package.get('burst_upload'),
        threshold_down=package.get('burst_threshold_down'), threshold_up=package.get('burst_threshold_up'),
        burst_time=package.get('burst_time') or 16, priority=package.get('priority') or 8,
        min_down=package.get('min_download'), min_up=package.get('min_upload')) or '0/0'


def snapshot_rate_limit(value, download=None, upload=None):
    """Keep a complete known contract; only proven unlimited may fill a gap."""
    if value is not None and str(value).strip():
        rate = str(value).strip()
        if len(rate) > 255 or any(ord(c) < 32 for c in rate):
            raise ValueError('صيغة سياسة سرعة الكرت غير صالحة.')
        return rate
    from core.rate_limit import is_zero_or_empty_rate
    if (download is not None and upload is not None
            and str(download).strip() and str(upload).strip()
            and is_zero_or_empty_rate(download) and is_zero_or_empty_rate(upload)):
        return '0/0'
    raise ValueError('سياسة سرعة الكرت المحفوظة غير مكتملة؛ يلزم تصحيحها من مصدر موثوق قبل متابعة العملية.')

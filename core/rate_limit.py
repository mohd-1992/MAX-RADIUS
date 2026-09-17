# -*- coding: utf-8 -*-
"""
MikroTik-Rate-Limit builder and parser.
Format:
rx-rate[/tx-rate] [rx-burst-rate[/tx-burst-rate]] [rx-burst-threshold[/tx-burst-threshold]] [rx-burst-time[/tx-burst-time]] [priority] [rx-rate-min[/tx-rate-min]]
Note: For MikroTik, rx is upload (client to router) and tx is download (router to client).
"""

def is_zero_or_empty_rate(rate_val):
    if rate_val is None:
        return True
    s = str(rate_val).strip().lower()
    return s in ['', '0', '0m', '0k', '0g', '0b', 'unlimited', 'مفتوح', 'غير محدود']

def parse_rate_to_bps(val):
    """Convert rate string like '10M', '512k', '1G', '1000' to integer bits-per-second."""
    if not val:
        return 0
    s = str(val).strip().lower()
    if is_zero_or_empty_rate(s):
        return 0
    try:
        if s.endswith('g'):
            return int(float(s[:-1]) * 1000 * 1000 * 1000)
        elif s.endswith('m'):
            return int(float(s[:-1]) * 1000 * 1000)
        elif s.endswith('k'):
            return int(float(s[:-1]) * 1000)
        else:
            return int(float(s))
    except Exception:
        return 0

def build_mikrotik_rate_limit(download, upload, burst_down=None, burst_up=None,
                              threshold_down=None, threshold_up=None, burst_time=16,
                              priority=8, min_down=None, min_up=None):
    if is_zero_or_empty_rate(download) or is_zero_or_empty_rate(upload):
        return None

    rate_str = f"{upload}/{download}"
    
    down_bps = parse_rate_to_bps(download)
    up_bps = parse_rate_to_bps(upload)
    b_down_bps = parse_rate_to_bps(burst_down)
    b_up_bps = parse_rate_to_bps(burst_up)
    
    # MikroTik strict requirement: burst-limit MUST be strictly greater than max-limit
    # If burst is <= max-limit, MikroTik rejects queue creation with:
    # "failed to add queue: download-burst-limit less than download-max-limit"
    if b_down_bps > down_bps and b_up_bps > up_bps:
        b_down = str(burst_down).strip()
        b_up = str(burst_up).strip()
        t_down = str(threshold_down).strip() if threshold_down and not is_zero_or_empty_rate(threshold_down) else download
        t_up = str(threshold_up).strip() if threshold_up and not is_zero_or_empty_rate(threshold_up) else upload
        b_time = f"{burst_time}/{burst_time}" if isinstance(burst_time, (int, str)) and '/' not in str(burst_time) else str(burst_time)
        prio = str(priority or 8)
        
        rate_str += f" {b_up}/{b_down} {t_up}/{t_down} {b_time} {prio}"
        
        m_down_bps = parse_rate_to_bps(min_down)
        m_up_bps = parse_rate_to_bps(min_up)
        if m_down_bps > 0 and m_up_bps > 0:
            rate_str += f" {min_up}/{min_down}"
            
    return rate_str

from core.helpers import format_bytes, format_duration

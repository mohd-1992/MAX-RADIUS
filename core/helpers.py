# -*- coding: utf-8 -*-
"""
Reusable Helper Functions & Utilities.
Consolidates byte/duration formatting, sequential indexing, date arithmetic, and common data converters.
"""

import math
import datetime

def format_bytes(bytes_val):
    """Convert bytes to human-readable string (KB, MB, GB, TB)."""
    try:
        val = float(bytes_val or 0)
    except (ValueError, TypeError):
        return '0.00 B'

    if val <= 0:
        return '0.00 B'
    
    units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
    i = int(math.floor(math.log(val, 1024))) if val > 0 else 0
    i = min(i, len(units) - 1)
    p = math.pow(1024, i)
    s = round(val / p, 2)
    return f"{s} {units[i]}"

def format_duration(seconds_val):
    """Convert seconds to human-readable duration (Xd Xh Xm Xs)."""
    try:
        secs = int(seconds_val or 0)
    except (ValueError, TypeError):
        return '0 ثانية'

    if secs <= 0:
        return '0 ثانية'

    days = secs // 86400
    hours = (secs % 86400) // 3600
    minutes = (secs % 3600) // 60
    seconds = secs % 60

    parts = []
    if days > 0:
        parts.append(f"{days} يوم")
    if hours > 0:
        parts.append(f"{hours} ساعة")
    if minutes > 0:
        parts.append(f"{minutes} دقيقة")
    if seconds > 0 and len(parts) < 2:
        parts.append(f"{seconds} ثانية")

    return " و ".join(parts) if parts else "0 ثانية"

def calculate_expiration_date(start_dt, val, unit):
    """
    Computes expiration datetime based on integer value and unit (minutes, hours, days, months).
    If val <= 0, returns None (Unlimited / Open validity).
    """
    if not val or int(val) <= 0:
        return None

    val = int(val)
    unit = str(unit or 'days').lower().strip()
    
    if isinstance(start_dt, str):
        try:
            start_dt = datetime.datetime.fromisoformat(start_dt.replace('Z', ''))
        except Exception:
            start_dt = datetime.datetime.now()
    elif not isinstance(start_dt, (datetime.datetime, datetime.date)):
        start_dt = datetime.datetime.now()

    if unit == 'minutes':
        return start_dt + datetime.timedelta(minutes=val)
    elif unit == 'hours':
        return start_dt + datetime.timedelta(hours=val)
    elif unit == 'months':
        return start_dt + datetime.timedelta(days=val * 30)
    else:  # days
        return start_dt + datetime.timedelta(days=val)

def assign_sequential_ids(items, id_key='sequential_id', start=1):
    """Injects a 1-based sequential chronological ID into a list of dictionaries."""
    for idx, item in enumerate(items, start=start):
        if isinstance(item, dict):
            item[id_key] = idx
    return items

def get_system_time(tz_name=None):
    """Returns current NTP-corrected localized datetime object for the configured timezone."""
    try:
        from core.time_service import get_system_now
        return get_system_now(tz_name)
    except Exception:
        return datetime.datetime.now()

def format_localized_datetime(dt, tz_name=None, fmt='%Y-%m-%d %H:%M:%S'):
    """Formats datetime string or object localized to the configured timezone."""
    try:
        from core.time_service import format_system_datetime
        return format_system_datetime(dt, fmt=fmt, tz_name=tz_name)
    except Exception:
        return str(dt) if dt else '-'
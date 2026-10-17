# -*- coding: utf-8 -*-
"""Host-clock authority and display timezone helpers.

NTP probes report agreement only; they never adjust application/host time.
Database and FreeRADIUS storage retain their fixed +03:00 contract.
"""

import os
import sys
import time
import datetime
import threading
import logging
import math

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from pytz import timezone as ZoneInfo

try:
    import ntplib
    NTPLIB_AVAILABLE = True
except ImportError:
    NTPLIB_AVAILABLE = False

try:
    import pytz
    PYTZ_AVAILABLE = True
except ImportError:
    pytz = None
    PYTZ_AVAILABLE = False

logger = logging.getLogger('time_service')

# List of trusted authoritative NTP pools to query
NTP_SERVERS = [
    'pool.ntp.org',
    'time.google.com',
    'time.cloudflare.com',
    'time.windows.com',
    '0.pool.ntp.org',
    '1.pool.ntp.org'
]

# Global State for NTP Synchronization
_TIME_LOCK = threading.Lock()
_NTP_OFFSET_SECONDS = 0.0      # Diagnostic only; never applied to timestamps
_IS_NTP_SYNCED = False
_LAST_NTP_SYNC_TIME = None
_NTP_SERVER_USED = None
_LAST_NTP_PROBE_MONOTONIC = None
NTP_AGREEMENT_SECONDS = 2.0
NTP_PROBE_MAX_AGE_SECONDS = 7200
_NTP_WORKER_THREAD = None
_NTP_WORKER_RUNNING = False

DEFAULT_TIMEZONE = 'Asia/Aden'
DB_STORAGE_TIMEZONE = os.environ.get('DB_STORAGE_TIMEZONE', 'Asia/Aden')


def get_db_storage_timezone():
    """Returns the fixed storage timezone of the database and FreeRADIUS."""
    # Match database.db's fixed +03:00 session offset, independently of display settings.
    return datetime.timezone(datetime.timedelta(hours=3))


def get_db_storage_now():
    """Returns the current real datetime (host clock) in the DB storage timezone."""
    utc_now = get_real_utc_now()
    return utc_now.astimezone(get_db_storage_timezone())


def sync_ntp_time(timeout=2.5):
    """
    Diagnose host/NTP agreement without setting any clock or applying an offset.
    Success means a finite probe within two seconds, not kernel NTP verification.
    Returns:
        tuple: (success: bool, offset_seconds: float, server_used: str, msg: str)
    """
    global _NTP_OFFSET_SECONDS, _IS_NTP_SYNCED, _LAST_NTP_SYNC_TIME, _NTP_SERVER_USED, _LAST_NTP_PROBE_MONOTONIC

    with _TIME_LOCK:
        _IS_NTP_SYNCED = False

    if not NTPLIB_AVAILABLE:
        logger.warning("Diagnostic NTP probe unavailable: ntplib is not installed.")
        return False, 0.0, None, "فحص NTP غير متاح؛ المرجع هو ساعة المضيف"

    client = ntplib.NTPClient()
    for server in NTP_SERVERS:
        try:
            # Query the NTP server with a short timeout
            response = client.request(server, version=3, timeout=timeout)
            
            # response.offset is the clock offset in seconds: (true_time - system_time)
            offset = float(response.offset)
            if not math.isfinite(offset):
                raise ValueError("Nonfinite diagnostic NTP offset")
            
            with _TIME_LOCK:
                _NTP_OFFSET_SECONDS = offset
                _IS_NTP_SYNCED = abs(offset) <= NTP_AGREEMENT_SECONDS
                _LAST_NTP_SYNC_TIME = datetime.datetime.now(datetime.timezone.utc)
                _NTP_SERVER_USED = server
                _LAST_NTP_PROBE_MONOTONIC = time.monotonic()

            agrees = abs(offset) <= NTP_AGREEMENT_SECONDS
            logger.info("Diagnostic NTP probe: agrees=%s offset_seconds=%.4f", agrees, offset)
            message = ("ساعة المضيف متوافقة مع فحص NTP" if agrees else
                       "يوجد فرق بين ساعة المضيف وفحص NTP؛ لم يغير الفحص الوقت")
            return agrees, offset, server, message
        except Exception as e:
            logger.debug(f"NTP query to {server} failed: {e}")
            continue

    logger.warning("All diagnostic NTP queries failed; no clock changes made.")
    return False, _NTP_OFFSET_SECONDS, _NTP_SERVER_USED, "تعذر الوصول إلى خوادم فحص NTP؛ لم يتم تغيير الوقت"


def _ntp_sync_worker_loop(interval_seconds=3600):
    """Background diagnostic probe; never changes the application clock."""
    global _NTP_WORKER_RUNNING
    logger.info(f"Starting background NTP sync worker (Interval: {interval_seconds}s)...")
    
    # Run first sync immediately
    try:
        sync_ntp_time()
    except Exception as e:
        logger.error(f"Initial NTP sync error: {e}")

    while _NTP_WORKER_RUNNING:
        try:
            time.sleep(interval_seconds)
            if not _NTP_WORKER_RUNNING:
                break
            sync_ntp_time()
        except Exception as err:
            logger.error(f"NTP sync worker exception: {err}")
            time.sleep(60)


def start_ntp_sync_worker(interval_seconds=3600):
    """Starts the background NTP sync worker daemon thread if not already running."""
    global _NTP_WORKER_THREAD, _NTP_WORKER_RUNNING
    with _TIME_LOCK:
        if _NTP_WORKER_RUNNING and _NTP_WORKER_THREAD and _NTP_WORKER_THREAD.is_alive():
            return _NTP_WORKER_THREAD
        
        _NTP_WORKER_RUNNING = True
        _NTP_WORKER_THREAD = threading.Thread(
            target=_ntp_sync_worker_loop,
            args=(interval_seconds,),
            daemon=True,
            name="NtpSyncWorkerThread"
        )
        _NTP_WORKER_THREAD.start()
        return _NTP_WORKER_THREAD


def stop_ntp_sync_worker():
    """Stops the background NTP worker."""
    global _NTP_WORKER_RUNNING
    with _TIME_LOCK:
        _NTP_WORKER_RUNNING = False


def get_ntp_offset():
    """Return the last diagnostic offset; callers must not apply it to time."""
    with _TIME_LOCK:
        return _NTP_OFFSET_SECONDS


def get_real_timestamp():
    """Return the host Unix epoch, with no independent application offset."""
    return time.time()


def get_real_utc_now():
    """Return host time as a timezone-aware UTC datetime."""
    real_ts = get_real_timestamp()
    return datetime.datetime.fromtimestamp(real_ts, tz=datetime.timezone.utc)


_CACHED_TIMEZONE_NAME = None
_CACHED_TZ_LOCK = threading.Lock()
_IS_LOADING_TZ = False


def get_configured_timezone_name(force_refresh=False):
    """
    Reads the configured timezone name from cache or `wisp_system_settings` table.
    Defaults to 'Asia/Riyadh' or 'Asia/Dubai' or system env if uninitialized.
    """
    global _CACHED_TIMEZONE_NAME, _IS_LOADING_TZ
    if _CACHED_TIMEZONE_NAME is not None and not force_refresh:
        return _CACHED_TIMEZONE_NAME

    if _IS_LOADING_TZ:
        return os.environ.get('TZ', DEFAULT_TIMEZONE)

    tz = None
    try:
        _IS_LOADING_TZ = True
        from database.db import query_one
        row = query_one("SELECT `value` FROM wisp_system_settings WHERE `key` = ?", ('timezone',))
        if row and row.get('value') and row['value'].strip():
            tz = row['value'].strip()
    except Exception:
        pass
    finally:
        _IS_LOADING_TZ = False

    if not tz:
        tz = os.environ.get('TZ', DEFAULT_TIMEZONE)

    with _CACHED_TZ_LOCK:
        _CACHED_TIMEZONE_NAME = tz

    try:
        from database.db import set_active_db_timezone
        set_active_db_timezone(tz)
    except Exception:
        pass

    return tz


def update_system_timezone(new_tz_name):
    """
    Update display timezone only; preserve host clock and fixed DB storage offset.
    """
    global _CACHED_TIMEZONE_NAME
    if not new_tz_name or not isinstance(new_tz_name, str):
        return
    tz_clean = new_tz_name.strip()
    with _CACHED_TZ_LOCK:
        _CACHED_TIMEZONE_NAME = tz_clean

    # Display preferences must not change the process clock used by billing.

    try:
        from database.db import set_active_db_timezone
        set_active_db_timezone(tz_clean)
    except Exception:
        pass


KNOWN_TZ_OFFSETS = {
    'asia/aden': 3,
    'asia/riyadh': 3,
    'asia/baghdad': 3,
    'asia/kuwait': 3,
    'asia/qatar': 3,
    'asia/bahrain': 3,
    'africa/cairo': 2,
    'asia/dubai': 4,
    'asia/muscat': 4,
    'africa/tripoli': 2,
    'africa/khartoum': 2,
    'europe/istanbul': 3,
    'utc': 0,
    'gmt': 0,
}


def get_system_timezone(tz_name=None):
    """
    Returns a timezone object (ZoneInfo or pytz.timezone or datetime.timezone) for the system or requested name.
    """
    import re
    target_name = (tz_name or get_configured_timezone_name()).strip()

    # Offset string parsing like "+03:00", "+03", "-05:00"
    m = re.match(r'^([+-])(\d{1,2})(?::?(\d{2}))?$', target_name)
    if m:
        sign = 1 if m.group(1) == '+' else -1
        hours = int(m.group(2))
        minutes = int(m.group(3) or 0)
        return datetime.timezone(datetime.timedelta(hours=sign * hours, minutes=sign * minutes))

    # Try standard zoneinfo
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(target_name)
    except Exception:
        pass

    # Fallback to pytz
    if PYTZ_AVAILABLE and pytz:
        try:
            return pytz.timezone(target_name)
        except Exception:
            pass

    # Fallback to known standard timezone offsets
    lower_name = target_name.lower()
    if lower_name in KNOWN_TZ_OFFSETS:
        return datetime.timezone(datetime.timedelta(hours=KNOWN_TZ_OFFSETS[lower_name]))

    # Fallback to UTC
    return datetime.timezone.utc


def get_system_now(tz_name=None):
    """
    Returns the current real datetime (host clock), localized to the system's configured timezone.
    """
    utc_now = get_real_utc_now()
    target_tz = get_system_timezone(tz_name)
    return utc_now.astimezone(target_tz)


def get_utc_now_str(fmt='%Y-%m-%d %H:%M:%S'):
    """Returns current UTC date/time string matching database timestamp clock."""
    return get_real_utc_now().strftime(fmt)


def get_db_cutoff_str(timeout_minutes=5):
    """
    Returns naive wall-clock timestamp string (%Y-%m-%d %H:%M:%S) for database queries
    (radacct, active sessions, heartbeats) which accurately matches the active
    MariaDB/FreeRADIUS server wall clock in the DB storage timezone.
    """
    try:
        now_dt = get_db_storage_now()
    except Exception:
        now_dt = datetime.datetime.fromtimestamp(time.time(), datetime.timezone.utc).astimezone(get_db_storage_timezone())
    return (now_dt - datetime.timedelta(minutes=int(timeout_minutes))).strftime('%Y-%m-%d %H:%M:%S')


def get_utc_cutoff_str(timeout_minutes=5):
    """
    Alias for get_db_cutoff_str to preserve backwards compatibility for existing callers.
    """
    return get_db_cutoff_str(timeout_minutes=timeout_minutes)


def get_system_now_str(fmt='%Y-%m-%d %H:%M:%S', tz_name=None):
    """Returns current localized date/time string formatted according to `fmt`."""
    return get_system_now(tz_name).strftime(fmt)


def to_system_timezone(dt, tz_name=None, source_tz=None):
    """
    Converts any datetime object (naive or aware) to the configured display system timezone.
    If dt is naive, it is assumed to originate from the database storage timezone (Asia/Aden / UTC+3),
    preventing spurious timezone shifts when displaying database timestamps.
    """
    if dt is None:
        return None

    target_tz = get_system_timezone(tz_name)
    src_tz = get_system_timezone(source_tz) if source_tz else get_db_storage_timezone()

    # String input conversion
    if isinstance(dt, str):
        dt_str = dt.strip()
        try:
            dt = datetime.datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
        except Exception:
            for pattern in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
                try:
                    dt = datetime.datetime.strptime(dt_str, pattern)
                    break
                except Exception:
                    continue
        if isinstance(dt, str):
            return dt

    if not isinstance(dt, (datetime.datetime, datetime.date)):
        return dt

    if isinstance(dt, datetime.date) and not isinstance(dt, datetime.datetime):
        dt = datetime.datetime.combine(dt, datetime.time.min)

    # If naive, assume it originated from database storage timezone
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=src_tz)

    return dt.astimezone(target_tz)


def format_system_datetime(dt, fmt='%Y-%m-%d %H:%M:%S', tz_name=None):
    """Localizes and formats a datetime object or string for display."""
    if not dt:
        return '-'
    try:
        converted = to_system_timezone(dt, tz_name)
        if isinstance(converted, datetime.datetime):
            return converted.strftime(fmt)
        return str(converted)
    except Exception:
        return str(dt)


def get_time_sync_status():
    """
    Returns a comprehensive status dictionary regarding NTP sync, offset, and timezone.
    """
    with _TIME_LOCK:
        offset_sec = _NTP_OFFSET_SECONDS
        is_synced = _IS_NTP_SYNCED
        last_sync = _LAST_NTP_SYNC_TIME
        server_used = _NTP_SERVER_USED
        probe_at = _LAST_NTP_PROBE_MONOTONIC
    probe_age = max(0.0, time.monotonic() - probe_at) if probe_at is not None else None
    is_synced = bool(is_synced and probe_age is not None and probe_age <= NTP_PROBE_MAX_AGE_SECONDS)

    tz_name = get_configured_timezone_name()
    now_dt = get_system_now(tz_name)
    now_str = now_dt.strftime('%Y-%m-%d %H:%M:%S')
    time_only_str = now_dt.strftime('%H:%M:%S')
    date_only_str = now_dt.strftime('%Y-%m-%d')
    timestamp_ms = int(now_dt.timestamp() * 1000)
    
    last_sync_str = last_sync.strftime('%Y-%m-%d %H:%M:%S UTC') if last_sync else 'لم يتم فحص NTP بعد'

    return {
        'clock_source': 'host',
        'ntp_role': 'diagnostic',
        'offset_applied': False,
        'ntp_probe_age_seconds': round(probe_age, 3) if probe_age is not None else None,
        'ntp_synced': is_synced,
        'ntp_server': server_used or 'pool.ntp.org',
        'ntp_offset_seconds': round(offset_sec, 6),
        'ntp_offset_ms': round(offset_sec * 1000.0, 2),
        'last_sync_time': last_sync_str,
        'timezone': tz_name,
        'system_now': now_str,
        'time_str': time_only_str,
        'date_str': date_only_str,
        'timestamp_ms': timestamp_ms,
        'utc_now': now_dt.astimezone(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    }

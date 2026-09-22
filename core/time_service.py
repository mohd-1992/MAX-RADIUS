# -*- coding: utf-8 -*-
"""
Core NTP Time Synchronization & Dynamic Timezone Engine for MAX RADIUS.
Provides:
1. Periodic background NTP synchronization with authoritative pools (pool.ntp.org, time.google.com, etc.).
2. Accurate clock offset calculation to ensure tamper-proof timestamps for accounting, sessions, and billing.
3. Dynamic database-backed Timezone resolution (e.g. Asia/Aden, Asia/Riyadh, Africa/Cairo).
4. Helper functions to obtain localized datetime objects, ISO strings, and timezone-aware timestamps.
"""

import os
import sys
import time
import datetime
import threading
import logging

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
_NTP_OFFSET_SECONDS = 0.0      # Offset to add to time.time()
_IS_NTP_SYNCED = False
_LAST_NTP_SYNC_TIME = None
_NTP_SERVER_USED = None
_NTP_WORKER_THREAD = None
_NTP_WORKER_RUNNING = False

DEFAULT_TIMEZONE = 'Asia/Aden'


def sync_ntp_time(timeout=2.5):
    """
    Queries trusted NTP servers to calculate the local clock offset.
    Returns:
        tuple: (success: bool, offset_seconds: float, server_used: str, msg: str)
    """
    global _NTP_OFFSET_SECONDS, _IS_NTP_SYNCED, _LAST_NTP_SYNC_TIME, _NTP_SERVER_USED

    if not NTPLIB_AVAILABLE:
        logger.warning("ntplib is not installed; falling back to host system clock.")
        return False, 0.0, None, "ntplib library not available"

    client = ntplib.NTPClient()
    for server in NTP_SERVERS:
        try:
            # Query the NTP server with a short timeout
            response = client.request(server, version=3, timeout=timeout)
            
            # response.offset is the clock offset in seconds: (true_time - system_time)
            offset = float(response.offset)
            
            with _TIME_LOCK:
                _NTP_OFFSET_SECONDS = offset
                _IS_NTP_SYNCED = True
                _LAST_NTP_SYNC_TIME = datetime.datetime.now(datetime.timezone.utc)
                _NTP_SERVER_USED = server

            offset_ms = offset * 1000.0
            logger.info(f"NTP Time Sync Successful via [{server}]. Clock Offset: {offset_ms:+.2f} ms ({offset:+.4f} s)")
            return True, offset, server, f"Synchronized with {server} (Offset: {offset_ms:+.2f} ms)"
        except Exception as e:
            logger.debug(f"NTP query to {server} failed: {e}")
            continue

    logger.warning("All NTP pool queries timed out or failed. Retaining current clock state.")
    return False, _NTP_OFFSET_SECONDS, _NTP_SERVER_USED, "Could not reach any NTP server"


def _ntp_sync_worker_loop(interval_seconds=3600):
    """Background worker daemon that periodically updates the NTP clock offset."""
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
    """Returns the current NTP clock offset in seconds."""
    with _TIME_LOCK:
        return _NTP_OFFSET_SECONDS


def get_real_timestamp():
    """Returns the NTP-corrected Unix timestamp (float seconds)."""
    return time.time() + get_ntp_offset()


def get_real_utc_now():
    """Returns a timezone-aware UTC datetime corrected by NTP."""
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
    Dynamically updates the active timezone in memory, DB session offsets, and environment.
    """
    global _CACHED_TIMEZONE_NAME
    if not new_tz_name or not isinstance(new_tz_name, str):
        return
    tz_clean = new_tz_name.strip()
    with _CACHED_TZ_LOCK:
        _CACHED_TIMEZONE_NAME = tz_clean

    os.environ['TZ'] = tz_clean
    if hasattr(time, 'tzset'):
        try:
            time.tzset()
        except Exception:
            pass

    try:
        from database.db import set_active_db_timezone
        set_active_db_timezone(tz_clean)
    except Exception:
        pass


def get_system_timezone(tz_name=None):
    """
    Returns a timezone object (ZoneInfo or pytz.timezone) for the system or requested name.
    """
    target_name = (tz_name or get_configured_timezone_name()).strip()
    
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

    # Fallback to UTC
    return datetime.timezone.utc


def get_system_now(tz_name=None):
    """
    Returns the current real datetime (NTP corrected), localized to the system's configured timezone.
    """
    utc_now = get_real_utc_now()
    target_tz = get_system_timezone(tz_name)
    return utc_now.astimezone(target_tz)


def get_utc_now_str(fmt='%Y-%m-%d %H:%M:%S'):
    """Returns current UTC date/time string matching database timestamp clock."""
    return get_real_utc_now().strftime(fmt)


def get_utc_cutoff_str(timeout_minutes=5):
    """
    Returns UTC timestamp string for database queries (radacct, sessions, heartbeats)
    which matches MariaDB/FreeRADIUS UTC clock.
    """
    try:
        utc_now = get_real_utc_now()
    except Exception:
        utc_now = datetime.datetime.now(datetime.timezone.utc)
    return (utc_now - datetime.timedelta(minutes=int(timeout_minutes))).strftime('%Y-%m-%d %H:%M:%S')


def get_system_now_str(fmt='%Y-%m-%d %H:%M:%S', tz_name=None):
    """Returns current localized date/time string formatted according to `fmt`."""
    return get_system_now(tz_name).strftime(fmt)


def to_system_timezone(dt, tz_name=None):
    """
    Converts any datetime object (naive or aware) to the configured system timezone.
    """
    if dt is None:
        return None

    target_tz = get_system_timezone(tz_name)

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

    # If naive, assume UTC or system local
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)

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

    tz_name = get_configured_timezone_name()
    now_dt = get_system_now(tz_name)
    now_str = now_dt.strftime('%Y-%m-%d %H:%M:%S')
    time_only_str = now_dt.strftime('%H:%M:%S')
    date_only_str = now_dt.strftime('%Y-%m-%d')
    timestamp_ms = int(now_dt.timestamp() * 1000)
    
    last_sync_str = last_sync.strftime('%Y-%m-%d %H:%M:%S UTC') if last_sync else 'لم تتم المزامنة بعد'

    return {
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
        'utc_now': get_real_utc_now().strftime('%Y-%m-%d %H:%M:%S UTC')
    }

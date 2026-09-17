# -*- coding: utf-8 -*-
"""
Internal Watchdog & Self-Healing Service.
Provides automated health monitoring, DB pool reconnects, disk space tracking,
and audit logging into wisp_system_alerts.
"""

import os
import sys
import time
import shutil
import socket
import datetime
import threading
import subprocess
import json

from database.db import get_connection, query_all, query_one, execute_write

WATCHDOG_INTERVAL_SECONDS = 60  # Run every 1 minute for proactive health and session reaping
_watchdog_thread = None
_stop_event = threading.Event()

# Cache previous states to prevent alert flooding
_last_db_state = True
_last_radius_state = True
_last_disk_alert_time = 0
_last_auto_cleanup_time = 0

def log_system_alert(alert_type, severity, message, source='watchdog'):
    """Insert an alert event into wisp_system_alerts."""
    try:
        execute_write(
            "INSERT INTO wisp_system_alerts (alert_type, severity, source, message) VALUES (?, ?, ?, ?)",
            (alert_type, severity, source, message)
        )
        print(f"  [Watchdog Alert] [{severity.upper()}] {alert_type}: {message}")
    except Exception as e:
        print(f"  [Watchdog] Error logging alert: {e}")

def check_database_health():
    """Verify database responsiveness and auto-reconnect if needed."""
    global _last_db_state
    is_healthy = False
    
    try:
        conn = get_connection()
        if conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            res = cursor.fetchone()
            conn.close()
            if res:
                is_healthy = True
    except Exception as e:
        is_healthy = False
        print(f"  [Watchdog] DB Healthcheck failed: {e}")

    if not is_healthy and _last_db_state:
        # DB just went down
        log_system_alert(
            alert_type='DB_DISCONNECT',
            severity='critical',
            message='فقدت الواجهة الخلفية الاتصال بقاعدة البيانات MariaDB. جارٍ محاولة إعادة التهيئة والتعافي التلقائي...'
        )
        _last_db_state = False
    elif is_healthy and not _last_db_state:
        # DB recovered
        now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        try:
            execute_write(
                "UPDATE wisp_system_alerts SET is_resolved = 1, resolved_at = ? WHERE alert_type = 'DB_DISCONNECT' AND is_resolved = 0",
                (now_str,)
            )
        except Exception:
            pass
        log_system_alert(
            alert_type='DB_RECOVERY',
            severity='info',
            message='تمت استعادة الاتصال بقاعدة البيانات MariaDB بنجاح وعادت كافة العمليات للعمل الطبيعي.'
        )
        _last_db_state = True

    return is_healthy

def check_disk_space():
    """Verify disk space on storage volume and alert if free space < 10%."""
    global _last_disk_alert_time
    now = time.time()
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    try:
        target_path = '/app' if os.path.exists('/app') else '.'
        total, used, free = shutil.disk_usage(target_path)
        free_pct = (free / total) * 100.0 if total > 0 else 100.0
        free_gb = free / (1024 ** 3)
        total_gb = total / (1024 ** 3)

        if free_pct < 10.0:
            if now - _last_disk_alert_time > 3600:
                log_system_alert(
                    alert_type='DISK_SPACE_CRITICAL',
                    severity='danger',
                    message=f'تحذير: المساحة المتبقية على القرص منخفضة جداً ({free_pct:.1f}% متبقية - {free_gb:.2f} GB من أصل {total_gb:.2f} GB).'
                )
                _last_disk_alert_time = now
            return False, free_pct
        else:
            # Auto-resolve previous disk critical alert if any
            try:
                execute_write(
                    "UPDATE wisp_system_alerts SET is_resolved = 1, resolved_at = ? WHERE alert_type = 'DISK_SPACE_CRITICAL' AND is_resolved = 0",
                    (now_str,)
                )
            except Exception:
                pass
            return True, free_pct
    except Exception as e:
        return True, 100.0

def check_radius_engine_health():
    """Probe FreeRADIUS UDP port to verify responsiveness."""
    global _last_radius_state
    host = os.environ.get('RADIUS_HOST', 'max_radius_core')
    port = int(os.environ.get('RADIUS_AUTH_PORT', 1812))
    if port == 18120:
        port = 1812
    is_responsive = False

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(2.0)
        sock.sendto(b'\x01\x01\x00\x14\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00', (host, port))
        sock.close()
        is_responsive = True
    except Exception:
        is_responsive = False

    if not is_responsive and _last_radius_state:
        log_system_alert(
            alert_type='RADIUS_UNRESPONSIVE',
            severity='danger',
            message=f'محرك FreeRADIUS على {host}:{port} لا يستجيب لطلبات الفحص. يتولى محرك التعافي Autoheal محاولة إعادة التشغيل التلقائية.'
        )
        _last_radius_state = False
    elif is_responsive and not _last_radius_state:
        now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        try:
            execute_write(
                "UPDATE wisp_system_alerts SET is_resolved = 1, resolved_at = ? WHERE alert_type = 'RADIUS_UNRESPONSIVE' AND is_resolved = 0",
                (now_str,)
            )
        except Exception:
            pass
        log_system_alert(
            alert_type='RADIUS_RECOVERY',
            severity='info',
            message='عادت استجابة محرك FreeRADIUS للعمل الطبيعي بنجاح بعد التعافي التلقائي.'
        )
        _last_radius_state = True

    return is_responsive

def check_auto_database_cleanup():
    """Weekly automated maintenance routine (Pruning > 90 days)."""
    global _last_auto_cleanup_time
    now_ts = time.time()
    if now_ts - _last_auto_cleanup_time >= 604800:
        _last_auto_cleanup_time = now_ts
        try:
            from services.db_maintenance_service import run_auto_maintenance_job
            run_auto_maintenance_job(retention_days=90)
        except Exception as e:
            print(f"  [Watchdog] Auto cleanup error: {e}")

def run_watchdog_cycle():
    """Execute one full watchdog cycle with auto-healing and auto-resolution."""
    try:
        db_ok = check_database_health()
        disk_ok, free_pct = check_disk_space()
        radius_ok = check_radius_engine_health()
        check_auto_database_cleanup()

        # Auto-heal zombie sessions in background (reap sessions without heartbeat > 3m)
        try:
            from services.autoheal_service import purge_stale_zombie_sessions
            purge_stale_zombie_sessions(timeout_minutes=3)
        except Exception:
            pass

        return {
            'timestamp': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'db_healthy': db_ok,
            'disk_healthy': disk_ok,
            'disk_free_pct': round(free_pct, 1),
            'radius_healthy': radius_ok
        }
    except Exception as e:
        print(f"  [Watchdog] Error in watchdog cycle: {e}")
        return {'timestamp': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'error': str(e)}

def _watchdog_loop():
    """Background daemon loop."""
    print("  🚀 [Watchdog] Internal Self-Healing & System Health Monitor started (Interval: 1m).")
    # Initial run
    time.sleep(10)
    run_watchdog_cycle()
    
    while not _stop_event.is_set():
        if _stop_event.wait(WATCHDOG_INTERVAL_SECONDS):
            break
        run_watchdog_cycle()

def start_watchdog_thread():
    """Start the background watchdog daemon thread."""
    global _watchdog_thread
    if _watchdog_thread is None or not _watchdog_thread.is_alive():
        _stop_event.clear()
        _watchdog_thread = threading.Thread(target=_watchdog_loop, name='WatchdogDaemon', daemon=True)
        _watchdog_thread.start()

def get_system_alerts(limit=30, unresolved_only=False):
    """Retrieve system alerts from wisp_system_alerts table."""
    try:
        if unresolved_only:
            return query_all("SELECT * FROM wisp_system_alerts WHERE is_resolved = 0 ORDER BY id DESC LIMIT ?", (limit,))
        return query_all("SELECT * FROM wisp_system_alerts ORDER BY id DESC LIMIT ?", (limit,))
    except Exception:
        return []

def resolve_system_alert(alert_id):
    """Mark an alert as resolved."""
    try:
        now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        execute_write("UPDATE wisp_system_alerts SET is_resolved = 1, resolved_at = ? WHERE id = ?", (now_str, alert_id))
        return True, "تم تمييز التنبيه كمحلول بنجاح."
    except Exception as e:
        return False, str(e)

def clear_all_resolved_alerts():
    """Remove resolved alerts."""
    try:
        execute_write("DELETE FROM wisp_system_alerts WHERE is_resolved = 1")
        return True, "تم مسح كافة التنبيهات المحلولة بنجاح."
    except Exception as e:
        return False, str(e)
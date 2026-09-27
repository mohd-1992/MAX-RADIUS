# -*- coding: utf-8 -*-
"""
System Control & Service Management Service.
Provides live status, uptime tracking, database size metrics,
and control actions (restart/stop/start) for the 3 core system engines:
1. Web Server / Backend (Python WSGI)
2. FreeRADIUS 3.x Engine
3. MariaDB 10.11 Database
"""

import os
import sys
import time
import datetime
import socket
import subprocess
import json
import threading
from database.db import get_connection, query_one, is_mysql_conn

# Record process start time for Web Server Uptime
SERVER_START_TIME = time.time()

def format_seconds(seconds_val):
    """Convert integer seconds to localized human readable uptime."""
    try:
        secs = int(seconds_val or 0)
    except (ValueError, TypeError):
        return '0 دقيقة'

    if secs <= 0:
        return 'أقل من دقيقة'

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
    if not parts or (len(parts) < 2 and seconds > 0):
        parts.append(f"{seconds} ثانية")

    return " و ".join(parts) if parts else "أقل من دقيقة"

def query_docker_socket(endpoint, method='GET'):
    """Query Docker Daemon via Unix socket using curl."""
    sock_path = '/var/run/docker.sock'
    if not os.path.exists(sock_path):
        return None

    try:
        cmd = ['curl', '-s', '-w', '\n%{http_code}', '--unix-socket', sock_path, '-X', method, f'http://localhost{endpoint}']
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            lines = res.stdout.strip().split('\n')
            http_code = lines[-1].strip() if lines else '200'
            body = "\n".join(lines[:-1]).strip() if len(lines) > 1 else ""
            
            if http_code in ['200', '204', '304']:
                if body:
                    try:
                        return json.loads(body)
                    except Exception:
                        return body
                return True
    except Exception:
        pass
    return None

def get_container_info(container_names):
    """Retrieve container status and uptime from Docker socket or CLI."""
    if isinstance(container_names, str):
        candidates = [container_names]
    else:
        candidates = list(container_names)

    # 1. Try Docker Socket REST API on candidates
    for name in candidates:
        info = query_docker_socket(f'/containers/{name}/json')
        if isinstance(info, dict) and 'State' in info:
            state = info.get('State', {})
            status = state.get('Status', 'unknown') # running, exited, restarting
            is_running = state.get('Running', False)
            started_at = state.get('StartedAt', '')
            
            uptime_str = 'غير متوفر'
            if is_running and started_at:
                try:
                    cleaned = started_at.split('.')[0].replace('Z', '')
                    dt_start = datetime.datetime.fromisoformat(cleaned)
                    now_utc = datetime.datetime.utcnow()
                    diff_sec = int((now_utc - dt_start).total_seconds())
                    uptime_str = format_seconds(diff_sec)
                except Exception:
                    pass
                    
            return {
                'exists': True,
                'name': name,
                'status': status,
                'is_running': is_running,
                'uptime': uptime_str,
                'image': info.get('Config', {}).get('Image', '') or info.get('Image', '')
            }

    # 2. Try docker CLI fallback if present
    for name in candidates:
        try:
            res = subprocess.run(['docker', 'inspect', name], capture_output=True, text=True, timeout=4)
            if res.returncode == 0:
                data = json.loads(res.stdout)
                if data and len(data) > 0:
                    state = data[0].get('State', {})
                    is_running = state.get('Running', False)
                    status = state.get('Status', 'running' if is_running else 'stopped')
                    started_at = state.get('StartedAt', '')
                    uptime_str = 'غير متوفر'
                    if is_running and started_at:
                        try:
                            cleaned = started_at.split('.')[0].replace('Z', '')
                            dt_start = datetime.datetime.fromisoformat(cleaned)
                            now_utc = datetime.datetime.utcnow()
                            diff_sec = int((now_utc - dt_start).total_seconds())
                            uptime_str = format_seconds(diff_sec)
                        except Exception:
                            pass
                    return {
                        'exists': True,
                        'name': name,
                        'status': status,
                        'is_running': is_running,
                        'uptime': uptime_str,
                        'image': data[0].get('Config', {}).get('Image', '')
                    }
        except Exception:
            pass

    return {'exists': False, 'name': candidates[0], 'status': 'unknown', 'is_running': False, 'uptime': 'غير متوفر'}

def get_database_metrics():
    """Calculates live database size, table count, and server uptime."""
    size_mb = 0.0
    table_count = 0
    total_rows = 0
    db_uptime_sec = 0
    is_connected = False
    
    try:
        conn = get_connection()
        is_connected = True
        conn.close()

        # Database size query from information_schema
        size_res = query_one("""
            SELECT 
                ROUND(SUM(data_length + index_length) / 1024 / 1024, 2) AS size_mb,
                COUNT(table_name) AS table_count,
                SUM(table_rows) AS total_rows
            FROM information_schema.tables 
            WHERE table_schema = DATABASE()
        """)
        if size_res:
            size_mb = float(size_res.get('size_mb') or 0.0)
            table_count = int(size_res.get('table_count') or 0)
            total_rows = int(size_res.get('total_rows') or 0)

        # MariaDB Server Uptime query
        uptime_res = query_one("SHOW GLOBAL STATUS LIKE 'Uptime'")
        if uptime_res:
            db_uptime_sec = int(uptime_res.get('Value') or uptime_res.get('value') or 0)
    except Exception:
        is_connected = False

    return {
        'is_connected': is_connected,
        'size_mb': size_mb,
        'table_count': table_count,
        'total_rows': total_rows,
        'uptime_sec': db_uptime_sec,
        'uptime_str': format_seconds(db_uptime_sec) if db_uptime_sec > 0 else 'غير متوفر'
    }

def check_freeradius_probe():
    """Performs a live probe against FreeRADIUS server."""
    host = os.environ.get('RADIUS_HOST', 'freeradius')
    port = int(os.environ.get('RADIUS_AUTH_PORT', 1812))
    
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(2.0)
        sock.sendto(b'\x01\x01\x00\x14\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00', (host, port))
        sock.close()
        return True
    except Exception:
        return False

def get_all_services_status():
    """
    Returns full status dictionary for all 3 core engines:
    1. Web Server (Python Waitress WSGI)
    2. FreeRADIUS 3.x Engine
    3. MariaDB 10.11 Database
    """
    now = datetime.datetime.now()
    
    # 1. Web Server Info
    web_uptime_sec = int(time.time() - SERVER_START_TIME)
    web_container = get_container_info(['max_radius_web', 'wisp_web_v2', 'max-radius-wisp-web'])
    web_status = {
        'id': 'web',
        'name': 'خادم النظام الرئيسي ولوحة التحكم',
        'subname': 'Python WSGI & REST API Backend',
        'icon': 'fa-solid fa-server',
        'color': 'blue',
        'status': 'running',
        'is_running': True,
        'status_text': 'يعمل بنشاط (Active / Running)',
        'uptime': format_seconds(web_uptime_sec),
        'port': os.environ.get('PORT', '5090'),
        'type': 'خادم الويب وإدارة الجلسات',
        'details': f'منفذ الويب: 5090 TCP | بيئة بايثون 3.11 مع محرك Waitress'
    }

    # 2. FreeRADIUS 3.x Info
    fr_container = get_container_info(['max_radius_core', 'wisp_freeradius_v2', 'max-radius-freeradius'])
    fr_probe = check_freeradius_probe()
    
    fr_is_running = fr_container.get('is_running', False) if fr_container.get('exists') else fr_probe
    fr_status_code = 'running' if fr_is_running else 'stopped'
    fr_uptime_str = fr_container.get('uptime', 'يعمل بنشاط') if fr_container.get('exists') else ('يعمل بنشاط' if fr_probe else 'متوقف')

    fr_status = {
        'id': 'freeradius',
        'name': 'محرك FreeRADIUS 3.x الرسمي',
        'subname': 'RADIUS Authentication & Accounting Engine',
        'icon': 'fa-solid fa-tower-broadcast',
        'color': 'emerald',
        'status': fr_status_code,
        'is_running': fr_is_running,
        'status_text': 'يعمل بنشاط (Active / Running)' if fr_is_running else 'متوقف (Stopped)',
        'uptime': fr_uptime_str,
        'port': '1812 / 1813 UDP (Host 18120 / 18130)',
        'type': 'محرك المصادقة والمحاسبة',
        'details': 'معالجة اتصالات MikroTik Hotspot & PPPoE بروابط rlm_sql_mysql'
    }

    # 3. MariaDB Database Info
    db_metrics = get_database_metrics()
    db_container = get_container_info(['max_radius_db', 'wisp_mariadb_v2'])
    
    db_is_running = db_container.get('is_running', False) if db_container.get('exists') else db_metrics['is_connected']
    db_status_code = 'running' if db_is_running else 'stopped'
    db_uptime_str = db_container.get('uptime') if db_container.get('exists') else db_metrics['uptime_str']

    db_status = {
        'id': 'mariadb',
        'name': 'محرك قواعد البيانات MariaDB 10.11',
        'subname': 'Relational SQL Database & FreeRADIUS Storage',
        'icon': 'fa-solid fa-database',
        'color': 'sky',
        'status': db_status_code,
        'is_running': db_is_running,
        'status_text': 'متصل ويعمل (Healthy / Running)' if db_is_running else 'غير متصل (Disconnected)',
        'uptime': db_uptime_str,
        'size_mb': db_metrics['size_mb'],
        'table_count': db_metrics['table_count'],
        'total_rows': db_metrics['total_rows'],
        'port': '3306 TCP (Host 3307)',
        'type': 'مخزن البيانات والمشتركين',
        'details': f'الحجم: {db_metrics["size_mb"]} MB | {db_metrics["table_count"]} جدول | {db_metrics["total_rows"]} سجل'
    }

    # 4. Autoheal & Watchdog Engine Info
    autoheal_container = get_container_info(['max_radius_autoheal', 'wisp_autoheal_v2'])
    autoheal_is_running = autoheal_container.get('is_running', False)
    autoheal_status = {
        'id': 'autoheal',
        'name': 'حاوية التعافي التلقائي والمراقب الآلي',
        'subname': 'Docker Autoheal Watchdog & Self-Healing Daemon',
        'icon': 'fa-solid fa-heart-pulse',
        'color': 'purple',
        'status': 'running' if autoheal_is_running else 'stopped',
        'is_running': autoheal_is_running,
        'status_text': 'يراقب الحاويات بنشاط (Autoheal Active)' if autoheal_is_running else 'متوقف (Stopped)',
        'uptime': autoheal_container.get('uptime', 'يعمل بنشاط') if autoheal_is_running else 'متوقف',
        'port': 'Docker Socket (/var/run/docker.sock)',
        'type': 'المراقبة الذاتية وإعادة التشغيل التلقائي',
        'details': 'إعادة تشغيل فورية لأي محرك يتحول إلى غير صحي (Unhealthy)'
    }

    # 5. Backup Scheduler & Automation Engine Info
    try:
        from services.backup_scheduler_service import get_scheduler_status
        backup_sched_status = get_scheduler_status()
    except Exception as e:
        backup_sched_status = {
            'id': 'backup_scheduler',
            'name': 'مجدول النسخ الاحتياطي والأتمتة',
            'subname': 'APScheduler Automated Backup Engine',
            'icon': 'fa-solid fa-clock-rotate-left',
            'color': 'amber',
            'status': 'stopped',
            'is_running': False,
            'status_text': f'خطأ: {e}',
            'uptime': 'متوقف',
            'type': 'الأتمتة والنسخ الاحتياطي',
            'details': 'خدمة الجدولة التلقائية للنسخ الاحتياطي'
        }

    # 6. Fetch Recent System Alerts
    from core.watchdog import get_system_alerts, run_watchdog_cycle
    system_alerts = get_system_alerts(limit=25)

    return {
        'timestamp': now.strftime('%Y-%m-%d %H:%M:%S'),
        'services': [web_status, fr_status, db_status, autoheal_status, backup_sched_status],
        'db_metrics': db_metrics,
        'system_alerts': system_alerts
    }


def execute_service_action(service_id, action, admin_username='admin'):
    """
    Executes a control action (restart / stop / start) on the specified service.
    """
    service_id = str(service_id).lower().strip()
    action = str(action).lower().strip()
    
    if action not in ['restart', 'stop', 'start']:
        return False, f"إجراء غير معروف: {action}"

    # Handle internal backup scheduler service
    if service_id == 'backup_scheduler':
        try:
            from services.backup_scheduler_service import (
                start_backup_scheduler, stop_backup_scheduler, restart_backup_scheduler
            )
            if action == 'start':
                return start_backup_scheduler()
            elif action == 'stop':
                return stop_backup_scheduler()
            elif action == 'restart':
                return restart_backup_scheduler()
        except Exception as e:
            return False, f"فشل تنفيذ {action} على مجدول النسخ الاحتياطي: {e}"

    container_candidates = {
        'freeradius': ['max_radius_core', 'wisp_freeradius_v2', 'max-radius-freeradius'],
        'mariadb': ['max_radius_db', 'wisp_mariadb_v2'],
        'web': ['max_radius_web', 'wisp_web_v2', 'max-radius-wisp-web'],
        'autoheal': ['max_radius_autoheal', 'wisp_autoheal_v2']
    }

    candidates = container_candidates.get(service_id)
    if not candidates:
        return False, f"الخدمة المطلوبة غير صالحة: {service_id}"

    # Find the actual existing container name
    info = get_container_info(candidates)
    container_name = info.get('name', candidates[0])

    # Special handling for Web Server Restart to return response before restart
    if service_id == 'web':
        if action == 'restart':
            def delayed_restart():
                time.sleep(1.0)
                query_docker_socket(f'/containers/{container_name}/restart', method='POST')
                try:
                    os._exit(0)
                except Exception:
                    pass

            threading.Thread(target=delayed_restart, daemon=True).start()
            return True, "تم إرسال أمر إعادة تشغيل خادم الويب بنجاح. ستتم إعادة التحميل خلال ثوانٍ معدودة."
        elif action == 'stop':
            return False, "لا يمكن إيقاف خادم الويب من داخله لتجنب انقطاع لوحة التحكم بشكل دائم."

    # 1. Try Docker Socket API
    socket_res = query_docker_socket(f'/containers/{container_name}/{action}', method='POST')
    if socket_res is not None and socket_res is not False:
        action_names = {'restart': 'إعادة تشغيل', 'stop': 'إيقاف', 'start': 'تشغيل'}
        return True, f"تم {action_names.get(action, action)} محرك [{service_id}] بنجاح."

    # 2. Try docker CLI subprocess
    try:
        res = subprocess.run(['docker', action, container_name], capture_output=True, text=True, timeout=15)
        if res.returncode == 0:
            action_names = {'restart': 'إعادة تشغيل', 'stop': 'إيقاف', 'start': 'تشغيل'}
            return True, f"تم {action_names.get(action, action)} محرك [{service_id}] بنجاح."
        else:
            return False, f"فشل تنفيذ الأمر: {res.stderr.strip() or res.stdout.strip()}"
    except Exception:
        pass

    # 3. Systemd / Linux service fallback
    systemd_map = {
        'freeradius': 'freeradius',
        'mariadb': 'mariadb',
        'web': 'wisp-web'
    }
    systemd_service = systemd_map.get(service_id)
    if systemd_service:
        try:
            res = subprocess.run(['systemctl', action, systemd_service], capture_output=True, text=True, timeout=10)
            if res.returncode == 0:
                return True, f"تم تنفيذ {action} على الخدمة {systemd_service} بنجاح عبر Systemd."
        except Exception:
            pass

    return False, f"تعذر الوصول لمحرك التحكم لتنفيذ الأمر على خدمة [{service_id}]."
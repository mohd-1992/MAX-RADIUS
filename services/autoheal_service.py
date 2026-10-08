# -*- coding: utf-8 -*-
"""
Auto-Healing & Watchdog Management Service for MAX RADIUS.
Provides:
1. Docker Engine Telemetry & Container Fleet Health Inspection (/var/run/docker.sock).
2. Live Container Controls (Inspect, Logs, Healthchecks, Safe Restart).
3. Port & Socket Network Prober (FreeRADIUS UDP 1812/1813, MariaDB TCP 3306, Web GUI).
4. Deep Self-Healing Diagnostic Engine (Health score 0-100%, automated fault discovery).
5. Watchdog Daemon Thread status & Alert Integration (wisp_system_alerts).
6. Stale Session Purger & Memory Defragmentation Triggers.
"""

import os
import sys
import time
import json
import socket
import shutil
import http.client
import datetime

from database.db import query_all, query_one, execute_write, execute_update, get_connection
from core.watchdog import (
    run_watchdog_cycle, get_system_alerts, resolve_system_alert,
    clear_all_resolved_alerts, log_system_alert
)

import core.watchdog as watchdog

DOCKER_SOCKET_PATH = '/var/run/docker.sock'
MONITORED_CONTAINER_NAMES = [
    'max_radius_autoheal',
    'max_radius_core',
    'max_radius_web',
    'max_radius_db',
    'max_radius_l2tp'
]

class UnixSocketHTTPConnection(http.client.HTTPConnection):
    """HTTP Client communicating directly with local Docker Engine Unix domain socket."""
    def __init__(self, socket_path=DOCKER_SOCKET_PATH, timeout=6.0):
        super().__init__('localhost')
        self.socket_path = socket_path
        self.timeout = timeout

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)

def docker_api_request(method, path, body=None, timeout=6.0):
    """Performs an HTTP request to Docker Engine API over unix domain socket."""
    if not os.path.exists(DOCKER_SOCKET_PATH):
        return None, "Docker Socket غير متوفر في بيئة التشغيل الحالية"
    
    try:
        conn = UnixSocketHTTPConnection(DOCKER_SOCKET_PATH, timeout=timeout)
        headers = {'Host': 'localhost', 'Connection': 'close'}
        if body:
            headers['Content-Type'] = 'application/json'
        conn.request(method, path, body=body, headers=headers)
        res = conn.getresponse()
        raw_data = res.read()
        conn.close()
        if '/logs?' in path and res.status < 400:
            chunks = []
            offset = 0
            while offset + 8 <= len(raw_data) and raw_data[offset] in (1, 2) and raw_data[offset+1:offset+4] == bytes(3):
                length = int.from_bytes(raw_data[offset+4:offset+8], 'big')
                chunks.append(raw_data[offset+8:offset+8+length])
                offset += 8 + length
            return (b''.join(chunks) if chunks else raw_data).decode('utf-8', errors='replace'), None
        
        # Check HTTP status
        if res.status >= 400:
            return None, f"Docker API Error ({res.status}): {raw_data.decode('utf-8', errors='ignore')}"
        
        if res.status == 204: # No Content (e.g. restart success)
            return True, None
            
        try:
            return json.loads(raw_data.decode('utf-8', errors='ignore')), None
        except Exception:
            return raw_data.decode('utf-8', errors='ignore'), None
    except Exception as e:
        return None, str(e)

def get_container_fleet_status():
    """
    Inspects all system containers and returns clean, structured metadata for dashboard rendering.
    """
    raw_list, err = docker_api_request('GET', '/containers/json?all=1')
    if err or not isinstance(raw_list, list):
        return _get_fallback_container_fleet(err)

    containers_map = {}
    for c in raw_list:
        names = c.get('Names', [])
        for name in names:
            clean_name = name.lstrip('/')
            containers_map[clean_name] = c

    fleet = []
    current = next((c for c in raw_list if c.get('Id', '').startswith(os.environ.get('HOSTNAME', '!'))), None)
    project = (current or {}).get('Labels', {}).get('com.docker.compose.project')
    if not project:
        details, _ = docker_api_request('GET', '/containers/' + os.environ.get('HOSTNAME', 'unknown') + '/json')
        project = ((details or {}).get('Config', {}).get('Labels', {}) if isinstance(details, dict) else {}).get('com.docker.compose.project')
    if not project:
        return _get_fallback_container_fleet('تعذر تحديد منظومة الحاويات؛ لن يتم التحكم بحاويات أخرى')
    if project:
        by_service = {c.get('Labels', {}).get('com.docker.compose.service'): c for c in raw_list if c.get('Labels', {}).get('com.docker.compose.project') == project}
        for default, role in zip(MONITORED_CONTAINER_NAMES, ('autoheal', 'freeradius', 'wisp-web', 'mariadb', 'l2tp')):
            aliases = {'freeradius': 'radius_core', 'wisp-web': 'web', 'mariadb': 'db'}
            match = by_service.get(role) or by_service.get(aliases.get(role))
            containers_map[default] = match
        containers_map = {key: value for key, value in containers_map.items() if value and value.get('Labels', {}).get('com.docker.compose.project') == project}
    
    meta_info = {
        'max_radius_autoheal': {
            'title': 'حاوية التعافي التلقائي والمراقب الآلي',
            'desc': 'المراقب الذاتي لحالات فشل الحاويات (Auto-Heal & Watchdog Container)',
            'role': 'autoheal',
            'icon': 'fa-solid fa-heart-pulse',
            'color': 'emerald'
        },
        'max_radius_core': {
            'title': 'محرك المصادقة والمحاسبة FreeRADIUS',
            'desc': 'خادم RADIUS الأساسي لمعالجة طلبات الدخول والـ Accounting والـ CoA',
            'role': 'radius',
            'icon': 'fa-solid fa-satellite-dish',
            'color': 'indigo'
        },
        'max_radius_web': {
            'title': 'لوحة التحكم وبوابة المشتركين Web GUI',
            'desc': 'الواجهة البرمجية وإدارة الشبكة والمشتركين وبوابة الخدمة الذاتية',
            'role': 'web',
            'icon': 'fa-solid fa-globe',
            'color': 'blue'
        },
        'max_radius_db': {
            'title': 'قاعدة البيانات المركزية MariaDB',
            'desc': 'مستودع بيانات المشتركين والكروت وجلسات الاستهلاك وفهارس RADIUS',
            'role': 'database',
            'icon': 'fa-solid fa-database',
            'color': 'cyan'
        },
        'max_radius_l2tp': {
            'title': 'خادم الأنفاق L2TP/PPP VPN Server',
            'desc': 'خادم الربط الآمن لراوترات MikroTik وتعيين عناوين IP الثابتة (Port 1701 UDP)',
            'role': 'vpn',
            'icon': 'fa-solid fa-network-wired',
            'color': 'amber'
        }
    }

    for target_name in MONITORED_CONTAINER_NAMES:
        c_raw = containers_map.get(target_name)
        meta = meta_info.get(target_name, {
            'title': target_name,
            'desc': 'حاوية نظام',
            'role': 'service',
            'icon': 'fa-solid fa-cube',
            'color': 'slate'
        })
        
        if c_raw:
            target_name = c_raw.get('Names', ['/' + target_name])[0].lstrip('/')
            c_id = c_raw.get('Id', '')[:12]
            state = c_raw.get('State', 'unknown').lower()
            status_text = c_raw.get('Status', '')
            image = c_raw.get('Image', '')
            created_ts = c_raw.get('Created', 0)
            
            health = 'unhealthy' if '(unhealthy)' in status_text.lower() else ('healthy' if '(healthy)' in status_text.lower() else ('starting' if 'health: starting' in status_text.lower() else 'unknown'))
            is_healthy = state == 'running' and health == 'healthy'
            health_label = {'healthy': 'سليم وصحي (Healthy)', 'unhealthy': 'غير سليم (Unhealthy)', 'starting': 'الفحص قيد التشغيل'}.get(health, 'يعمل؛ الصحة غير متحققة' if state == 'running' else 'متوقف')
            
            fleet.append({
                'name': target_name,
                'id': c_id,
                'title': meta['title'],
                'desc': meta['desc'],
                'role': meta['role'],
                'icon': meta['icon'],
                'color': meta['color'],
                'state': state,
                'status': status_text,
                'health_label': health_label,
                'is_healthy': is_healthy,
                'image': image,
                'created': datetime.datetime.fromtimestamp(created_ts).strftime('%Y-%m-%d %H:%M:%S') if isinstance(created_ts, (int, float)) and created_ts > 0 else 'مباشر'
            })
        else:
            fleet.append({
                'name': target_name,
                'id': 'N/A',
                'title': meta['title'],
                'desc': meta['desc'],
                'role': meta['role'],
                'icon': meta['icon'],
                'color': meta['color'],
                'state': 'offline',
                'status': 'غير موجودة أو معطلة',
                'health_label': 'غير متصلة (Offline)',
                'is_healthy': False,
                'image': 'N/A',
                'created': '-'
            })

    return fleet

def _get_fallback_container_fleet(err_msg):
    """Fallback when Docker socket is not directly mountable."""
    return [
        {
            'name': 'max_radius_autoheal',
            'id': 'internal',
            'title': 'حاوية التعافي التلقائي والمراقب الآلي',
            'desc': 'المراقب الذاتي لحالات فشل الحاويات (Auto-Heal & Watchdog)',
            'role': 'autoheal',
            'icon': 'fa-solid fa-heart-pulse',
            'color': 'emerald',
            'state': 'unknown',
            'status': f'تعذر التحقق من Docker [{err_msg or "OK"}]',
            'health_label': 'غير متحقق',
            'is_healthy': False,
            'image': 'willfarrell/autoheal',
            'created': '-'
        }
    ]

def _allowed_container(name):
    return name in {c['name'] for c in get_container_fleet_status() if c['id'] not in ('N/A', 'internal')}

def inspect_container_details(container_name):
    """Fetches full inspect payload for a specific container."""
    container_name = container_name.strip()
    if not _allowed_container(container_name):
        return None, 'الحاوية ليست ضمن هذه المنظومة'
    data, err = docker_api_request('GET', f'/containers/{container_name}/json')
    if err:
        return None, err
    return data, None

def get_live_container_logs(container_name, lines=60):
    """Fetches real-time stdout/stderr log chunk for a container."""
    container_name = container_name.strip()
    if not _allowed_container(container_name):
        return 'تعذر استخراج السجلات: الحاوية ليست ضمن هذه المنظومة'
    lines = min(max(10, int(lines or 60)), 300)
    raw_logs, err = docker_api_request('GET', f'/containers/{container_name}/logs?stdout=1&stderr=1&tail={lines}&timestamps=1')
    if err:
        return f"تعذر استخراج السجلات: {err}"
    
    if isinstance(raw_logs, str):
        clean_lines = []
        for line in raw_logs.splitlines():
            if len(line) > 8 and ord(line[0]) in (1, 2) and line[1:4] == '\x00\x00\x00':
                clean_lines.append(line[8:])
            else:
                clean_lines.append(line)
        return '\n'.join(clean_lines)
    return str(raw_logs)

def restart_system_container(container_name):
    """
    Safely triggers container restart via Docker API.
    """
    container_name = container_name.strip()
    if not _allowed_container(container_name):
        return False, f"الحاوية '{container_name}' غير مصرح بإعادة تشغيلها من هذه اللوحة."
    
    log_system_alert('CONTAINER_RESTART_PENDING', 'info', f'جارٍ إعادة تشغيل [{container_name}]', source='container:' + container_name)
    res, err = docker_api_request('POST', f'/containers/{container_name}/restart?t=5', timeout=15.0)
    if err:
        execute_update("UPDATE wisp_system_alerts SET alert_type='CONTAINER_RESTART_FAILED', severity='danger', message=? WHERE source=? AND alert_type='CONTAINER_RESTART_PENDING' AND is_resolved=0", (f'فشلت إعادة تشغيل [{container_name}]: {err}', 'container:' + container_name))
        return False, f'فشل أمر إعادة التشغيل: {err}'
    return True, f'تم قبول إعادة تشغيل [{container_name}]؛ تكتمل العملية في السجل بعد إثبات الصحة.'


def probe_all_network_ports():
    """
    Actively probes network sockets and UDP ports across the MAX RADIUS fleet.
    Returns status and response latency in milliseconds.
    """
    results = []

    # 1. MariaDB Database (Query Ping)
    t0 = time.time()
    db_ok = False
    try:
        conn = get_connection()
        if conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            conn.close()
            db_ok = True
    except Exception:
        db_ok = False
    db_lat = round((time.time() - t0) * 1000, 2)
    results.append({
        'name': 'قاعدة البيانات MariaDB (SQL Port)',
        'target': 'db:3306',
        'type': 'TCP / SQL Handshake',
        'is_ok': db_ok,
        'latency_ms': db_lat,
        'status_str': f'استجابة سريعة ({db_lat} ms)' if db_ok else 'فشل الاتصال بقاعدة البيانات'
    })

    from services.health_probe_service import radius_probe
    for name, port in [('منفذ المصادقة FreeRADIUS (Authentication)', int(os.environ.get('RADIUS_AUTH_PORT', 1812))), ('منفذ المحاسبة FreeRADIUS (Accounting)', int(os.environ.get('RADIUS_ACCT_PORT', 1813)))]:
        t0 = time.monotonic()
        ok, message = radius_probe(port)
        results.append({'name': name, 'target': f"{os.environ.get('RADIUS_HOST') or os.environ.get('RADIUS_SERVER_IP') or 'radius_core'}:{port}/UDP", 'type': 'Authenticated Status-Server', 'is_ok': ok, 'latency_ms': round((time.monotonic()-t0)*1000, 2), 'status_str': message})
    vpn = next((c for c in get_container_fleet_status() if c['role'] == 'vpn' and c['id'] != 'N/A'), None)
    results.append({'name': 'خادم الأنفاق L2TP/PPP', 'target': vpn['name'] if vpn else 'خدمة خارجية أو غير متاحة', 'type': 'Docker process and listener healthcheck', 'is_ok': vpn['is_healthy'] if vpn else None, 'latency_ms': None, 'status_str': vpn['health_label'] if vpn else 'لم يتم التحقق من خدمة الأنفاق الخارجية'})

    # 5. Docker Engine Socket (/var/run/docker.sock)
    t0 = time.time()
    docker_sock_ok = False
    try:
        if os.path.exists(DOCKER_SOCKET_PATH):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(1.0)
            s.connect(DOCKER_SOCKET_PATH)
            s.close()
            docker_sock_ok = True
    except Exception:
        docker_sock_ok = False
    docker_lat = round((time.time() - t0) * 1000, 2)
    results.append({
        'name': 'مقبس محرك الدوكر (Docker Daemon Socket)',
        'target': DOCKER_SOCKET_PATH,
        'type': 'Unix Domain Socket',
        'is_ok': docker_sock_ok,
        'latency_ms': docker_lat,
        'status_str': f'متصل ونشط ({docker_lat} ms)' if docker_sock_ok else 'تعذر الوصول إلى Socket'
    })

    return results

def run_deep_system_diagnostic():
    """
    Executes a comprehensive healthcheck & self-healing diagnostic across the full ecosystem.
    Calculates an overall health score (0 - 100%) and returns actionable findings.
    """
    findings = []
    score = 100

    # 1. Check Monitored Containers
    fleet = get_container_fleet_status()
    total_containers = len(fleet)
    healthy_containers = sum(1 for c in fleet if c['is_healthy'])
    
    if healthy_containers < total_containers:
        diff = total_containers - healthy_containers
        deduct = diff * 20
        score -= deduct
        unhealthy_names = [c['name'] for c in fleet if not c['is_healthy']]
        findings.append({
            'type': 'danger',
            'title': f'حاويات متوقفة أو غير مستقرة ({len(unhealthy_names)} حاوية)',
            'desc': f"حاويات متوقفة أو لم تثبت صحتها: {', '.join(unhealthy_names)}. التعافي التلقائي يشمل الحاويات الموسومة التي تفشل في فحص الصحة فقط.",
            'action': 'إعادة تشغيل الحاويات'
        })
    else:
        findings.append({
            'type': 'success',
            'title': 'أسطول الحاويات متكامل وسليم 100%',
            'desc': f'كافة حاويات النظام الأساسية ({total_containers}/{total_containers}) تعمل بكفاءة وحالتها الصحية Healthy.',
            'action': 'لا يتطلب أي إجراء'
        })

    # 2. Check Database Latency & Pool
    ports = probe_all_network_ports()
    for probe in ports[1:]:
        if probe['is_ok'] is not True:
            score -= 10
            findings.append({'type': 'warning', 'title': probe['name'], 'desc': probe['status_str'], 'action': 'التحقق من الخدمة وإعداد الفحص'})
    db_probe = next((p for p in ports if 'MariaDB' in p['name']), None)
    if not db_probe or not db_probe['is_ok']:
        score -= 30
        findings.append({
            'type': 'critical',
            'title': 'فشل الاتصال بقاعدة البيانات المركزية',
            'desc': 'تعذر تنفيذ استعلام الاختبار على خادم MariaDB. قد تتوقف عمليات المصادقة والفواتير.',
            'action': 'إعادة تشغيل max_radius_db'
        })
    elif db_probe['latency_ms'] > 100:
        score -= 10
        findings.append({
            'type': 'warning',
            'title': f'استجابة قاعدة البيانات بطيئة ({db_probe["latency_ms"]} ms)',
            'desc': 'زمن الاستجابة يتجاوز المعدل الطبيعي. يُنصح بتشغيل تحسين الجداول (Optimize Tables).',
            'action': 'صيانة الجداول'
        })

    # 3. Check Disk Space
    try:
        target_path = str(__import__('core.config', fromlist=['STORAGE_DIR']).STORAGE_DIR)
        total_b, used_b, free_b = shutil.disk_usage(target_path)
        free_pct = (free_b / total_b) * 100.0 if total_b > 0 else 100.0
        free_gb = round(free_b / (1024 ** 3), 2)
        total_gb = round(total_b / (1024 ** 3), 2)

        if free_pct < 10.0:
            score -= 25
            findings.append({
                'type': 'danger',
                'title': f'مساحة القرص منخفضة جداً ({free_pct:.1f}% متبقية)',
                'desc': f'المساحة المتبقية على وحدة التخزين هي {free_gb} GB من أصل {total_gb} GB. قد يسبب امتلاء القرص توقف قواعد البيانات.',
                'action': 'تطهير السجلات والنسخ القديمة'
            })
        elif free_pct < 20.0:
            score -= 5
            findings.append({
                'type': 'warning',
                'title': f'مساحة القرص تقترب من الحد الحرج ({free_pct:.1f}% متبقية)',
                'desc': f'المتبقي {free_gb} GB من {total_gb} GB.',
                'action': 'مراجعة التخزين'
            })
    except Exception as e:
        free_pct = 0.0
        free_gb = 0.0
        score -= 10
        findings.append({'type': 'warning', 'title': 'تعذر قياس التخزين', 'desc': str(e), 'action': 'التحقق من التخزين'})

    # 4. Check Stale Zombie Sessions in Accounting
    try:
        from core.time_service import get_utc_cutoff_str
        z_timeout = get_zombie_session_timeout()
        cutoff_utc = get_utc_cutoff_str(z_timeout)
        zombie_sessions = query_one("""
            SELECT COUNT(*) as cnt 
            FROM radacct 
            WHERE acctstoptime IS NULL 
              AND (
                  (acctupdatetime IS NOT NULL AND acctupdatetime < ?)
                  OR (acctupdatetime IS NULL AND acctstarttime < ?)
              )
        """, (cutoff_utc, cutoff_utc))
        z_count = int(zombie_sessions['cnt'] or 0) if zombie_sessions else 0
        if z_count > 20:
            score -= 10
            findings.append({
                'type': 'warning',
                'title': f'تراكم جلسات معلقة بدون تحديث ({z_count} جلسة)',
                'desc': f'جلسات اتصال في radacct لم ترسل تحديثات Interim-Update لأكثر من {z_timeout} دقيقة.',
                'action': 'تفريغ الجلسات العالقة'
            })
    except Exception as exc:
        z_count = None
        score -= 10
        findings.append({'type':'warning','title':'تعذر قراءة الجلسات','desc':str(exc),'action':'التحقق من قاعدة البيانات'})

    # 5. Check Watchdog Daemon State
    is_watchdog_alive = bool(watchdog._watchdog_thread and watchdog._watchdog_thread.is_alive())
    if not is_watchdog_alive:
        score -= 10
        findings.append({
            'type': 'warning',
            'title': 'المراقب الآلي الداخلي (Watchdog Daemon) متوقف',
            'desc': 'خيط المراقبة الخلفي غير نشط حالياً. يلزم إعادة تشغيل خدمة الويب لاستعادته.',
            'action': 'تفعيل المراقب'
        })

    score = max(0, min(100, score))

    return {
        'score': score,
        'timestamp': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'healthy_containers': healthy_containers,
        'total_containers': total_containers,
        'ports': ports,
        'findings': findings,
        'zombie_sessions_count': z_count,
        'disk_free_pct': round(free_pct, 1),
        'disk_free_gb': free_gb
    }

def get_zombie_session_timeout():
    """Returns configured timeout in minutes from wisp_system_settings (default 15)."""
    try:
        row = query_one("SELECT `value` FROM wisp_system_settings WHERE `key` = 'zombie_session_timeout_mins'")
        if row and row.get('value'):
            return max(5, int(row['value']))
    except Exception:
        pass
    return 15

def set_zombie_session_timeout(timeout_mins):
    """Saves configured timeout in minutes into wisp_system_settings."""
    timeout_mins = max(5, int(timeout_mins or 15))
    try:
        execute_write("""
            INSERT INTO wisp_system_settings (`key`, `value`) 
            VALUES ('zombie_session_timeout_mins', ?)
            ON DUPLICATE KEY UPDATE `value` = ?
        """, (str(timeout_mins), str(timeout_mins)))
        return True, f"تم حفظ مهلة الجلسات العالقة بنجاح إلى ({timeout_mins} دقيقة)."
    except Exception:
        try:
            execute_write("INSERT OR REPLACE INTO wisp_system_settings (`key`, `value`) VALUES ('zombie_session_timeout_mins', ?)", (str(timeout_mins),))
            return True, f"تم حفظ مهلة الجلسات العالقة بنجاح إلى ({timeout_mins} دقيقة)."
        except Exception as e:
            return False, f"خطأ أثناء حفظ الإعداد: {e}"

def purge_stale_zombie_sessions(timeout_minutes=None):
    """
    Cleans up orphaned sessions where users disconnected without sending Acct-Stop.
    Uses the configured database storage clock for accounting timestamps.
    """
    if timeout_minutes is None:
        timeout_minutes = get_zombie_session_timeout()
    else:
        timeout_minutes = max(5, int(timeout_minutes or 15))

    try:
        from core.time_service import get_utc_cutoff_str, get_utc_now_str
        cutoff_utc = get_utc_cutoff_str(timeout_minutes)
        now_utc = get_utc_now_str()

        affected = query_one("""
            SELECT COUNT(*) as cnt FROM radacct
            WHERE acctstoptime IS NULL 
              AND (
                  (acctupdatetime IS NOT NULL AND acctupdatetime < ?)
                  OR (acctupdatetime IS NULL AND acctstarttime < ?)
              )
        """, (cutoff_utc, cutoff_utc))
        count = int(affected['cnt'] or 0) if affected else 0

        if count > 0:
            count = execute_update("""
                UPDATE radacct
                SET acctstoptime = COALESCE(acctupdatetime, acctstarttime),
                    acctterminatecause = 'Watchdog-Autoheal-Timeout'
                WHERE acctstoptime IS NULL
                  AND (
                      (acctupdatetime IS NOT NULL AND acctupdatetime < ?)
                      OR (acctupdatetime IS NULL AND acctstarttime < ?)
                  )
            """, (cutoff_utc, cutoff_utc))

            log_system_alert(
                alert_type='ZOMBIE_SESSIONS_PURGED',
                severity='info',
                source='watchdog_purge',
                resolved=True,
                message=f"تم إغلاق وتنظيف {count} جلسة معلقة (Zombie Sessions) تجاوزت مدة انقطاع التحديثات {timeout_minutes} دقيقة."
            )
            return True, f"تم تنظيف وإغلاق {count} جلسة معلقة بنجاح."
        return True, "لا توجد جلسات تجاوزت مهلة التنظيف المحددة."
    except Exception as e:
        return False, f"خطأ أثناء تفريغ الجلسات: {e}"

def get_autoheal_dashboard_full(alert_filter=None):
    """
    Consolidates all metrics, container fleet status, ports, alerts, and watchdog metadata.
    """
    fleet = get_container_fleet_status()
    ports = probe_all_network_ports()
    alerts = get_system_alerts(limit=200, unresolved_only=False, state=alert_filter)
    unresolved_row = query_one('SELECT COUNT(*) AS cnt FROM wisp_system_alerts WHERE is_resolved = 0')
    unresolved_alerts_count = int((unresolved_row or {}).get('cnt') or 0)
    
    failed_row = query_one("SELECT COUNT(*) AS cnt FROM wisp_system_alerts WHERE is_resolved=0 AND alert_type LIKE ?", ('%FAILED',))
    failed_count = int((failed_row or {}).get('cnt') or 0)
    autoheal_c = next((c for c in fleet if c['role'] == 'autoheal'), None)
    
    target_path = str(__import__('core.config', fromlist=['STORAGE_DIR']).STORAGE_DIR)
    total_b, used_b, free_b = shutil.disk_usage(target_path)
    free_pct = round((free_b / total_b) * 100.0 if total_b > 0 else 100.0, 1)
    used_pct = round(100.0 - free_pct, 1)
    free_gb = round(free_b / (1024 ** 3), 2)
    total_gb = round(total_b / (1024 ** 3), 2)
    used_gb = round(used_b / (1024 ** 3), 2)

    zombie_timeout = get_zombie_session_timeout()

    return {
        'fleet': fleet,
        'ports': ports,
        'alerts': alerts,
        'unresolved_alerts_count': unresolved_alerts_count,
        'ongoing_alerts_count': unresolved_alerts_count - failed_count,
        'failed_alerts_count': failed_count,
        'autoheal_container': autoheal_c,
        'watchdog_active': bool(watchdog._watchdog_thread and watchdog._watchdog_thread.is_alive()),
        'zombie_timeout': zombie_timeout,
        'disk': {
            'total_gb': total_gb,
            'used_gb': used_gb,
            'free_gb': free_gb,
            'used_pct': used_pct,
            'free_pct': free_pct
        },
        'now_str': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }

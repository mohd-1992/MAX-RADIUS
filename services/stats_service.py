import os
import sys
import shutil
import platform
import datetime
import time
import http.client
import socket
import json
from database.db import query_all, query_one
from core.rate_limit import format_bytes


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('localhost')
        self.path = path
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.path)


import threading

_LIVE_LOCK = threading.Lock()
_LIVE_METRICS_CACHE = {
    'total_subscribers': 0,
    'online_subscribers': 0,
    'active_sessions': 0,
    'vouchers': {
        'unused': 0,
        'active': 0,
        'expired': 0,
        'disabled': 0,
        'total': 0
    },
    'voucher_summary': {
        'available': 0,
        'activated': 0,
        'online': 0,
        'expired': 0,
        'total': 0
    },
    'total_nas': 0,
    'traffic_in_str': '0 B',
    'traffic_out_str': '0 B',
    'traffic_total_str': '0 B',
    'resources': {
        'cpu_percent': 0.5,
        'ram_percent': 1.0,
        'ram_used_gb': 0.0,
        'ram_total_gb': 8.0,
        'disk_percent': 20.0,
        'disk_used_gb': 10.0,
        'disk_total_gb': 100.0,
        'containers': {}
    }
}
_WORKER_STARTED = False
_LAST_ACTIVATION_SYNC = 0


from concurrent.futures import ThreadPoolExecutor


def _fetch_single_container_stats(cname):
    conn = None
    try:
        conn = UnixHTTPConnection('/var/run/docker.sock')
        conn.request('GET', f'/containers/{cname}/stats?stream=false')
        resp = conn.getresponse()
        if resp.status == 200:
            raw_data = resp.read().decode()
            stats = json.loads(raw_data)

            cpu_stats = stats.get('cpu_stats', {})
            precpu_stats = stats.get('precpu_stats', {})
            cpu_delta = cpu_stats.get('cpu_usage', {}).get('total_usage', 0) - precpu_stats.get('cpu_usage', {}).get('total_usage', 0)
            system_cpu_delta = cpu_stats.get('system_cpu_usage', 0) - precpu_stats.get('system_cpu_usage', 0)
            online_cpus = cpu_stats.get('online_cpus') or len(cpu_stats.get('cpu_usage', {}).get('percpu_usage') or [1]) or 1

            c_cpu = (cpu_delta / system_cpu_delta * online_cpus * 100.0) if system_cpu_delta > 0 else 0.0
            mem_stats = stats.get('memory_stats', {})
            c_mem_bytes = mem_stats.get('usage', 0)
            c_limit_bytes = mem_stats.get('limit', 0)

            display_name = 'FreeRADIUS Core' if 'core' in cname else ('MariaDB Database' if 'db' in cname else 'Web App')
            return {
                'key': cname,
                'name': display_name,
                'cpu_percent': round(c_cpu, 1),
                'mem_used_mb': round(c_mem_bytes / (1024 * 1024), 1),
                'mem_limit_gb': round(c_limit_bytes / (1024 ** 3), 1),
                'mem_bytes': c_mem_bytes,
                'limit_bytes': c_limit_bytes
            }
    except Exception:
        pass
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    return None


def _collect_server_resources_internal():
    cpu_percent = 0.0
    ram_percent = 0.0
    ram_used_gb = 0.0
    ram_total_gb = 8.0
    containers_info = {}

    target_containers = ['max_radius_core', 'max_radius_db', 'max_radius_web']

    # 1. Direct Docker Socket Query for exact container metrics via parallel threads
    try:
        if os.path.exists('/var/run/docker.sock'):
            total_container_cpu = 0.0
            total_container_mem_bytes = 0
            container_mem_limit = 0

            with ThreadPoolExecutor(max_workers=3) as executor:
                results = list(executor.map(_fetch_single_container_stats, target_containers))

            for r in results:
                if r:
                    containers_info[r['key']] = {
                        'name': r['name'],
                        'cpu_percent': r['cpu_percent'],
                        'mem_used_mb': r['mem_used_mb'],
                        'mem_limit_gb': r['mem_limit_gb']
                    }
                    total_container_cpu += r['cpu_percent']
                    total_container_mem_bytes += r['mem_bytes']
                    if r['limit_bytes'] > container_mem_limit:
                        container_mem_limit = r['limit_bytes']

            cpu_percent = round(total_container_cpu, 1)
            ram_used_gb = round(total_container_mem_bytes / (1024 ** 3), 2)
            ram_total_gb = round(container_mem_limit / (1024 ** 3), 1) if container_mem_limit > 0 else 8.0
            ram_percent = round((ram_used_gb / ram_total_gb) * 100.0, 1) if ram_total_gb > 0 else 0.0
    except Exception:
        pass

    # 2. Linux /proc and cgroup fallback if Docker socket didn't return data
    if not containers_info and os.path.exists('/proc/meminfo'):
        try:
            with open('/proc/meminfo', 'r') as f:
                lines = f.readlines()
            mem_dict = {}
            for l in lines:
                parts = l.split(':')
                if len(parts) == 2:
                    mem_dict[parts[0].strip()] = int(parts[1].replace('kB', '').strip())
            total_kb = mem_dict.get('MemTotal', 0)
            avail_kb = mem_dict.get('MemAvailable', mem_dict.get('MemFree', 0))
            if total_kb > 0:
                ram_total_gb = round(total_kb / (1024 * 1024), 1)
                ram_used_gb = round((total_kb - avail_kb) / (1024 * 1024), 2)
                ram_percent = round((ram_used_gb / ram_total_gb) * 100.0, 1)
        except Exception:
            pass

    # 3. Disk usage
    try:
        target_path = 'C:/' if os.name == 'nt' else '/'
        total, used, free = shutil.disk_usage(target_path)
        disk_percent = round((used / total) * 100, 1)
        disk_total_gb = round(total / (1024**3), 1)
        disk_used_gb = round(used / (1024**3), 1)
    except Exception:
        disk_percent = 25.0
        disk_total_gb = 100.0
        disk_used_gb = 25.0

    return {
        'cpu_percent': max(0.5, cpu_percent),
        'ram_percent': max(1.0, ram_percent),
        'ram_used_gb': ram_used_gb,
        'ram_total_gb': ram_total_gb,
        'disk_percent': disk_percent,
        'disk_used_gb': disk_used_gb,
        'disk_total_gb': disk_total_gb,
        'containers': containers_info
    }


def _get_cutoff_str(timeout_minutes=5):
    try:
        from core.time_service import get_utc_cutoff_str
        return get_utc_cutoff_str(timeout_minutes)
    except Exception:
        return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=int(timeout_minutes))).strftime('%Y-%m-%d %H:%M:%S')

def _collect_db_metrics_internal():
    # Index-backed fast count aggregations with Interim-Update Heartbeat
    cutoff_str = _get_cutoff_str(5)

    sub_count = query_one('SELECT COUNT(*) as total FROM wisp_subscribers')
    total_subs = sub_count['total'] if sub_count else 0
    
    # 0. Total Active Sessions across all interfaces (Hotspot + PPPoE) matching MikroTik Active
    active_sessions_q = query_one('''
        SELECT COUNT(*) as total 
        FROM radacct 
        WHERE acctstoptime IS NULL
          AND (
            (acctupdatetime IS NOT NULL AND acctupdatetime >= ?)
            OR
            (acctupdatetime IS NULL AND acctstarttime >= ?)
          )
    ''', (cutoff_str, cutoff_str))
    total_active_sessions = active_sessions_q['total'] if active_sessions_q else 0
    
    # 1. Total Card Subscribers (All activated vouchers: expired and non-expired)
    tot_act_q = query_one("SELECT COUNT(*) as c FROM wisp_vouchers WHERE first_used_at IS NOT NULL OR status IN ('active', 'used', 'expired')")
    total_card_subscribers = tot_act_q['c'] if tot_act_q else 0

    # 2. Active Card Subscribers (Non-expired active vouchers only)
    act_valid_q = query_one("SELECT COUNT(*) as c FROM wisp_vouchers WHERE status IN ('active', 'used') AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)")
    active_subscribers = act_valid_q['c'] if act_valid_q else 0

    # 3. Online Live Vouchers & Subscribers via Index with Heartbeat
    # Broadband / PPPoE
    online_sub_q = query_one('''
        SELECT 
            COUNT(DISTINCT a.username) as unique_users,
            COUNT(a.radacctid) as total_devices
        FROM radacct a
        INNER JOIN wisp_subscribers s ON a.username = s.username
        WHERE a.acctstoptime IS NULL
          AND (
            (a.acctupdatetime IS NOT NULL AND a.acctupdatetime >= ?)
            OR
            (a.acctupdatetime IS NULL AND a.acctstarttime >= ?)
          )
    ''', (cutoff_str, cutoff_str))
    online_subscribers = online_sub_q['unique_users'] if online_sub_q else 0
    online_subscriber_devices = online_sub_q['total_devices'] if online_sub_q else 0
    
    # Hotspot Vouchers
    online_vch_q = query_one('''
        SELECT 
            COUNT(DISTINCT a.username) as unique_cards,
            COUNT(a.radacctid) as total_devices
        FROM radacct a
        INNER JOIN wisp_vouchers v ON a.username = v.username
        WHERE a.acctstoptime IS NULL
          AND (
            (a.acctupdatetime IS NOT NULL AND a.acctupdatetime >= ?)
            OR
            (a.acctupdatetime IS NULL AND a.acctstarttime >= ?)
          )
    ''', (cutoff_str, cutoff_str))
    online_vouchers_cards = online_vch_q['unique_cards'] if online_vch_q else 0
    online_vouchers_devices = online_vch_q['total_devices'] if online_vch_q else 0

    # Total Connected Devices / Active Sessions
    total_connected_devices = max(total_active_sessions, online_subscriber_devices + online_vouchers_devices)
    total_unique_online = online_subscribers + online_vouchers_cards

    # 4. Expired Card Subscribers (Quota or Time exhausted)
    exp_q = query_one('''
        SELECT COUNT(*) as c
        FROM wisp_vouchers
        WHERE status = 'expired'
           OR (expires_at IS NOT NULL AND expires_at <= CURRENT_TIMESTAMP)
    ''')
    expired_vouchers = exp_q['c'] if exp_q else 0

    # Available Vouchers (Inventory)
    avail_q = query_one("SELECT COUNT(*) as c FROM wisp_vouchers WHERE status = 'unused'")
    available_vouchers = avail_q['c'] if avail_q else 0

    # Disabled & Recharged Vouchers
    dis_q = query_one("SELECT COUNT(*) as c FROM wisp_vouchers WHERE status = 'disabled'")
    disabled_vouchers = dis_q['c'] if dis_q else 0

    rech_q = query_one("SELECT COUNT(*) as c FROM wisp_vouchers WHERE status = 'recharged'")
    recharged_vouchers = rech_q['c'] if rech_q else 0

    tot_q = query_one("SELECT COUNT(*) as c FROM wisp_vouchers")
    total_vouchers = tot_q['c'] if tot_q else 0

    v_stats = {
        'unused': available_vouchers,
        'active': active_subscribers,
        'expired': expired_vouchers,
        'disabled': disabled_vouchers,
        'recharged': recharged_vouchers,
        'total': total_vouchers
    }

    voucher_summary = {
        'total_card_subscribers': total_card_subscribers,
        'active_subscribers': active_subscribers,
        'online_subscribers': online_vouchers_cards,
        'online_devices': online_vouchers_devices,
        'online': online_vouchers_devices,
        'online_cards': online_vouchers_cards,
        'expired_subscribers': expired_vouchers,
        'disabled_subscribers': disabled_vouchers,
        'recharged_subscribers': recharged_vouchers,
        'available': available_vouchers,
        'activated': total_card_subscribers,
        'expired': expired_vouchers,
        'disabled': disabled_vouchers,
        'recharged': recharged_vouchers,
        'total': total_vouchers
    }
        
    nas_devices = query_one('SELECT COUNT(*) as total FROM wisp_nas_devices')
    total_nas = nas_devices['total'] if nas_devices else 0
    
    traffic = query_one('''
        SELECT COALESCE(SUM(a.acctinputoctets), 0) as total_in,
               COALESCE(SUM(a.acctoutputoctets), 0) as total_out
        FROM radacct a
        WHERE a.username IN (SELECT username FROM wisp_subscribers)
           OR a.username IN (SELECT username FROM wisp_vouchers)
    ''')
    raw_in = traffic['total_in'] or 0 if traffic else 0
    raw_out = traffic['total_out'] or 0 if traffic else 0

    return {
        'total_subscribers': total_subs,
        'online_subscribers': online_subscribers,
        'online_subscriber_devices': online_subscriber_devices,
        'active_sessions': total_connected_devices,
        'total_unique_online': total_unique_online,
        'vouchers': v_stats,
        'voucher_summary': voucher_summary,
        'total_nas': total_nas,
        'traffic_in_str': format_bytes(raw_in),
        'traffic_out_str': format_bytes(raw_out),
        'traffic_total_str': format_bytes(raw_in + raw_out)
    }


def _metrics_collector_loop():
    global _LIVE_METRICS_CACHE
    while True:
        try:
            resources = _collect_server_resources_internal()
            db_stats = _collect_db_metrics_internal()
            combined = dict(db_stats)
            combined['resources'] = resources

            with _LIVE_LOCK:
                _LIVE_METRICS_CACHE = combined
        except Exception:
            pass
        time.sleep(2)


def start_live_metrics_daemon():
    global _WORKER_STARTED
    if not _WORKER_STARTED:
        _WORKER_STARTED = True
        t = threading.Thread(target=_metrics_collector_loop, daemon=True, name="LiveMetricsCollector")
        t.start()


# Auto-start background daemon on module load
start_live_metrics_daemon()


def get_server_resources():
    with _LIVE_LOCK:
        return _LIVE_METRICS_CACHE.get('resources', {
            'cpu_percent': 0.5,
            'ram_percent': 1.0,
            'ram_used_gb': 0.0,
            'ram_total_gb': 8.0,
            'disk_percent': 20.0,
            'disk_used_gb': 10.0,
            'disk_total_gb': 100.0,
            'containers': {}
        })


def get_dashboard_metrics():
    with _LIVE_LOCK:
        res = dict(_LIVE_METRICS_CACHE)
    # If cache is still at defaults on the very first sub-second startup, calculate immediately
    if res.get('total_subscribers') == 0 and res.get('vouchers', {}).get('total') == 0:
        try:
            db_stats = _collect_db_metrics_internal()
            res.update(db_stats)
        except Exception:
            pass
    return res


def get_recent_live_sessions(limit=10):
    from core.rate_limit import format_duration
    cutoff_str = _get_cutoff_str(5)
    sessions = query_all('''
        SELECT 
            a.radacctid, a.username, a.framedipaddress, a.callingstationid,
            a.nasipaddress, a.acctsessiontime, a.acctinputoctets, a.acctoutputoctets,
            v.id as voucher_id, vp.name as voucher_pkg_name,
            s.id as sub_id, sp.name as sub_pkg_name
        FROM radacct a
        LEFT JOIN wisp_vouchers v ON a.username = v.username
        LEFT JOIN wisp_packages vp ON v.package_id = vp.id
        LEFT JOIN wisp_subscribers s ON a.username = s.username
        LEFT JOIN wisp_packages sp ON s.package_id = sp.id
        WHERE a.acctstoptime IS NULL
          AND (
            (a.acctupdatetime IS NOT NULL AND a.acctupdatetime >= ?)
            OR
            (a.acctupdatetime IS NULL AND a.acctstarttime >= ?)
          )
        ORDER BY a.radacctid DESC
        LIMIT ?
    ''', (cutoff_str, cutoff_str, limit))
    
    result = []
    for s in (sessions or []):
        if s.get('voucher_id'):
            user_type = 'voucher'
            type_label = 'كرت هوتسبوت'
            pkg_name = s.get('voucher_pkg_name') or 'باقة هوتسبوت'
        else:
            user_type = 'subscriber'
            type_label = 'مشترك منزلي'
            pkg_name = s.get('sub_pkg_name') or 'باقة عامة'

        result.append({
            'radacctid': s['radacctid'],
            'username': s['username'],
            'user_type': user_type,
            'type_label': type_label,
            'package_name': pkg_name,
            'framedipaddress': s.get('framedipaddress') or '-',
            'callingstationid': s.get('callingstationid') or '-',
            'nasipaddress': s.get('nasipaddress') or '-',
            'download_str': format_bytes(s.get('acctoutputoctets') or 0),
            'upload_str': format_bytes(s.get('acctinputoctets') or 0),
            'duration_str': format_duration(s.get('acctsessiontime') or 0)
        })
    return result


def get_traffic_chart_data():
    """
    Returns real traffic (Download & Upload) for the last 7 days using optimized single-pass query.
    """
    arabic_days = ['الاثنين', 'الثلاثاء', 'الأربعاء', 'الخميس', 'الجمعة', 'السبت', 'الأحد']
    today = datetime.date.today()
    start_date = today - datetime.timedelta(days=6)
    start_str = start_date.strftime('%Y-%m-%d 00:00:00')
    
    rows = query_all("""
        SELECT DATE(acctstarttime) as day_date,
               COALESCE(SUM(acctinputoctets), 0) as total_in,
               COALESCE(SUM(acctoutputoctets), 0) as total_out
        FROM radacct
        WHERE acctstarttime >= ?
        GROUP BY DATE(acctstarttime)
    """, (start_str,))
    
    day_map = {r['day_date'].strftime('%Y-%m-%d') if hasattr(r['day_date'], 'strftime') else str(r['day_date']): r for r in (rows or [])}
    
    labels = []
    download_gb = []
    upload_gb = []
    
    for i in range(6, -1, -1):
        day_date = today - datetime.timedelta(days=i)
        day_str = day_date.strftime('%Y-%m-%d')
        weekday_name = arabic_days[day_date.weekday()]
        label = f"{weekday_name} ({day_date.strftime('%m/%d')})"
        labels.append(label)
        
        row = day_map.get(day_str, {})
        up_bytes = row.get('total_in', 0)
        down_bytes = row.get('total_out', 0)
        
        upload_gb.append(round(up_bytes / (1024 ** 3), 3))
        download_gb.append(round(down_bytes / (1024 ** 3), 3))
        
    return {
        'labels': labels,
        'download': download_gb,
        'upload': upload_gb
    }

def get_sales_chart_data():
    """
    Returns real monthly sales for the last 6 months from wisp_voucher_sales and wisp_invoices.
    Optimized with grouped single queries for zero CPU overhead.
    """
    arabic_months = {
        1: 'يناير', 2: 'فبراير', 3: 'مارس', 4: 'أبريل',
        5: 'مايو', 6: 'يونيو', 7: 'يوليو', 8: 'أغسطس',
        9: 'سبتمبر', 10: 'أكتوبر', 11: 'نوفمبر', 12: 'ديسمبر'
    }
    
    today = datetime.date.today()
    labels = []
    sales_arr = []
    voucher_sales_arr = []
    package_sales_arr = []
    
    year = today.year
    month = today.month
    
    months_list = []
    for i in range(5, -1, -1):
        m = month - i
        y = year
        while m <= 0:
            m += 12
            y -= 1
        months_list.append((y, m))
        
    earliest_month_str = f"{months_list[0][0]:04d}-{months_list[0][1]:02d}-01 00:00:00"
    
    v_rows = query_all('''
        SELECT SUBSTRING(activated_at, 1, 7) as ym, COALESCE(SUM(price), 0) as total
        FROM wisp_voucher_sales
        WHERE activated_at >= ?
        GROUP BY SUBSTRING(activated_at, 1, 7)
    ''', (earliest_month_str,))
    v_map = {r['ym']: float(r['total']) for r in (v_rows or []) if r.get('ym')}
    
    i_rows = query_all('''
        SELECT SUBSTRING(paid_at, 1, 7) as ym, COALESCE(SUM(amount), 0) as total
        FROM wisp_invoices
        WHERE status = 'paid' AND paid_at >= ?
        GROUP BY SUBSTRING(paid_at, 1, 7)
    ''', (earliest_month_str,))
    i_map = {r['ym']: float(r['total']) for r in (i_rows or []) if r.get('ym')}
    
    for y, m in months_list:
        month_str = f"{y:04d}-{m:02d}"
        month_label = f"{arabic_months.get(m, str(m))} {y}" if y != today.year else arabic_months.get(m, str(m))
        labels.append(month_label)
        
        v_tot = round(v_map.get(month_str, 0.0), 2)
        i_tot = round(i_map.get(month_str, 0.0), 2)
        tot = round(v_tot + i_tot, 2)
        
        voucher_sales_arr.append(v_tot)
        package_sales_arr.append(i_tot)
        sales_arr.append(tot)

    return {
        'labels': labels,
        'sales': sales_arr,
        'voucher_sales': voucher_sales_arr,
        'package_sales': package_sales_arr
    }


def get_internet_ping(host="8.8.8.8", port=53, timeout=1.5):
    """
    Checks Internet connectivity and round-trip ping latency in milliseconds.
    """
    start = time.time()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        s.close()
        latency_ms = round((time.time() - start) * 1000)
        quality = 'ممتاز' if latency_ms < 60 else ('جيد جداً' if latency_ms < 120 else 'مقبول')
        return {
            'is_connected': True,
            'ping_ms': latency_ms,
            'status_text': 'متصل بالإنترنت',
            'host': host,
            'quality': quality
        }
    except Exception:
        # Secondary fallback
        try:
            start_fb = time.time()
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect(("1.1.1.1", 53))
            s.close()
            latency_ms = round((time.time() - start_fb) * 1000)
            quality = 'ممتاز' if latency_ms < 60 else ('جيد جداً' if latency_ms < 120 else 'مقبول')
            return {
                'is_connected': True,
                'ping_ms': latency_ms,
                'status_text': 'متصل بالإنترنت',
                'host': "1.1.1.1",
                'quality': quality
            }
        except Exception:
            return {
                'is_connected': False,
                'ping_ms': 0,
                'status_text': 'غير متصل بالإنترنت',
                'host': host,
                'quality': 'منقطع'
            }


_CONTAINER_START_TIME = time.time()
try:
    if os.path.exists('/proc/1/stat') and os.path.exists('/proc/uptime'):
        clk_tck = os.sysconf(os.sysconf_names.get('SC_CLK_TCK', 'SC_CLK_TCK')) if hasattr(os, 'sysconf') else 100
        with open('/proc/uptime', 'r') as f:
            sys_uptime = float(f.readline().split()[0])
        with open('/proc/1/stat', 'r') as f:
            stat_fields = f.read().split(')')[-1].split()
            starttime_jiffies = float(stat_fields[19])
            proc_start_uptime = starttime_jiffies / clk_tck
            _CONTAINER_START_TIME = time.time() - (sys_uptime - proc_start_uptime)
    elif os.path.exists('/proc/1'):
        _CONTAINER_START_TIME = os.stat('/proc/1').st_mtime
except Exception:
    _CONTAINER_START_TIME = time.time()


def get_container_uptime_seconds():
    try:
        if os.path.exists('/proc/1/stat') and os.path.exists('/proc/uptime'):
            clk_tck = os.sysconf(os.sysconf_names.get('SC_CLK_TCK', 'SC_CLK_TCK')) if hasattr(os, 'sysconf') else 100
            with open('/proc/uptime', 'r') as f:
                sys_uptime = float(f.readline().split()[0])
            with open('/proc/1/stat', 'r') as f:
                stat_fields = f.read().split(')')[-1].split()
                starttime_jiffies = float(stat_fields[19])
                proc_start_uptime = starttime_jiffies / clk_tck
                return max(1.0, sys_uptime - proc_start_uptime)
        elif os.path.exists('/proc/1'):
            return max(1.0, time.time() - os.stat('/proc/1').st_mtime)
    except Exception:
        pass
    return max(1.0, time.time() - _CONTAINER_START_TIME)


def get_system_uptime_str():
    """
    Returns human-friendly container uptime in Arabic.
    """
    try:
        uptime_seconds = get_container_uptime_seconds()
        days = int(uptime_seconds // 86400)
        hours = int((uptime_seconds % 86400) // 3600)
        minutes = int((uptime_seconds % 3600) // 60)
        
        parts = []
        if days > 0:
            if days == 1:
                parts.append("يوم واحد")
            elif days == 2:
                parts.append("يومان")
            elif 3 <= days <= 10:
                parts.append(f"{days} أيام")
            else:
                parts.append(f"{days} يوماً")
        if hours > 0:
            if hours == 1:
                parts.append("ساعة واحدة")
            elif hours == 2:
                parts.append("ساعتان")
            elif 3 <= hours <= 10:
                parts.append(f"{hours} ساعات")
            else:
                parts.append(f"{hours} ساعة")
        if minutes > 0:
            if minutes == 1:
                parts.append("دقيقة واحدة")
            elif minutes == 2:
                parts.append("دقيقتان")
            elif 3 <= minutes <= 10:
                parts.append(f"{minutes} دقائق")
            else:
                parts.append(f"{minutes} دقيقة")
                
        if not parts:
            return "أقل من دقيقة"
            
        return " و ".join(parts)
    except Exception:
        return "أقل من دقيقة"


def get_latest_system_operations(limit=5):
    """
    Fetches the latest 5 activations, renewals, recharges, and billing operations.
    """
    import re
    try:
        rows = query_all('''
            SELECT a.id, a.username, a.action, a.module, a.details, a.created_at, a.admin_id,
                   COALESCE(m.full_name, m.username, 'النظام الذكي') as operator_name
            FROM wisp_audit_logs a
            LEFT JOIN wisp_managers m ON a.admin_id = m.id
            WHERE a.action IN (
                'ACTIVATE_VOUCHER', 'CREATE_SUBSCRIBER', 'SELF_REGISTER', 'RENEW_PACKAGE',
                'RECHARGE_CARD', 'CARD_RECHARGE', 'ADD_WALLET_BALANCE', 'ADD_DATA_QUOTA',
                'EXTEND_VALIDITY', 'CHANGE_PACKAGE', 'TOPUP_RESELLER', 'WALLET_RECHARGE',
                'PACKAGE_RENEWAL', 'SUBSCRIBER_CREATE'
            ) OR a.module IN ('vouchers', 'subscribers', 'subscriber', 'portal', 'resellers', 'quick_actions')
            ORDER BY a.id DESC LIMIT ?
        ''', (limit * 3,))
    except Exception:
        rows = []

    ops = []
    seen_keys = set()
    
    for r in rows:
        action = r.get('action') or ''
        details = r.get('details') or ''
        username = r.get('username') or ''
        operator = r.get('operator_name') or 'النظام الذكي'
        
        target_name = username
        if action == 'ACTIVATE_VOUCHER':
            m = re.search(r'Card\s+(\w+)', details)
            if m:
                target_name = m.group(1)
            op_label = 'تفعيل كارت إنترنت'
            op_icon = 'fa-solid fa-bolt'
            badge_class = 'bg-amber-500/20 text-amber-300 border-amber-500/30'
            user_type_label = 'كارت هوتسبوت'
            user_type_badge = 'bg-amber-500/10 text-amber-400 border border-amber-500/20'
        elif action in ('CREATE_SUBSCRIBER', 'SUBSCRIBER_CREATE'):
            m = re.search(r'Subscriber\s+\[?(\w+)\]?', details)
            if m:
                target_name = m.group(1)
            op_label = 'إضافة مشترك جديد'
            op_icon = 'fa-solid fa-user-plus'
            badge_class = 'bg-blue-500/20 text-blue-300 border-blue-500/30'
            user_type_label = 'مشترك منزلي'
            user_type_badge = 'bg-blue-500/10 text-blue-400 border border-blue-500/20'
        elif action == 'SELF_REGISTER':
            m = re.search(r'Subscriber\s+\[?(\w+)\]?', details)
            if m:
                target_name = m.group(1)
            op_label = 'تسجيل ذاتي لمشترك'
            op_icon = 'fa-solid fa-address-card'
            badge_class = 'bg-teal-500/20 text-teal-300 border-teal-500/30'
            operator = 'بوابة المشترك'
            user_type_label = 'تسجيل ذاتي'
            user_type_badge = 'bg-teal-500/10 text-teal-400 border border-teal-500/20'
        elif action in ('RENEW_PACKAGE', 'PACKAGE_RENEWAL'):
            op_label = 'تجديد اشتراك باقة'
            op_icon = 'fa-solid fa-rotate-right'
            badge_class = 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30'
            user_type_label = 'اشتراك شهري'
            user_type_badge = 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20'
        elif action in ('CARD_RECHARGE', 'RECHARGE_CARD', 'WALLET_RECHARGE'):
            op_label = 'شحن رصيد / كارت'
            op_icon = 'fa-solid fa-credit-card'
            badge_class = 'bg-indigo-500/20 text-indigo-300 border-indigo-500/30'
            user_type_label = 'شحن رصيد'
            user_type_badge = 'bg-indigo-500/10 text-indigo-400 border border-indigo-500/20'
        elif action == 'ADD_WALLET_BALANCE':
            op_label = 'شحن محفظة المشترك'
            op_icon = 'fa-solid fa-wallet'
            badge_class = 'bg-purple-500/20 text-purple-300 border-purple-500/30'
            user_type_label = 'محفظة رقمية'
            user_type_badge = 'bg-purple-500/10 text-purple-400 border border-purple-500/20'
        elif action == 'ADD_DATA_QUOTA':
            op_label = 'إضافة رصيد بيانات (كوتة)'
            op_icon = 'fa-solid fa-database'
            badge_class = 'bg-sky-500/20 text-sky-300 border-sky-500/30'
            user_type_label = 'كوتة إضافية'
            user_type_badge = 'bg-sky-500/10 text-sky-400 border border-sky-500/20'
        elif action == 'EXTEND_VALIDITY':
            op_label = 'تمديد صلاحية اشتراك'
            op_icon = 'fa-solid fa-calendar-plus'
            badge_class = 'bg-violet-500/20 text-violet-300 border-violet-500/30'
            user_type_label = 'تمديد وقت'
            user_type_badge = 'bg-violet-500/10 text-violet-400 border border-violet-500/20'
        elif action == 'CHANGE_PACKAGE':
            op_label = 'ترقية / تغيير باقة'
            op_icon = 'fa-solid fa-right-left'
            badge_class = 'bg-cyan-500/20 text-cyan-300 border-cyan-500/30'
            user_type_label = 'تغيير باقة'
            user_type_badge = 'bg-cyan-500/10 text-cyan-400 border border-cyan-500/20'
        elif action == 'TOPUP_RESELLER':
            op_label = 'شحن رصيد موزع'
            op_icon = 'fa-solid fa-money-bill-wave'
            badge_class = 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30'
            user_type_label = 'موزع معتمد'
            user_type_badge = 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20'
        else:
            if not any(k in action for k in ('VOUCHER', 'SUBSCRIBER', 'RENEW', 'RECHARGE', 'QUOTA', 'WALLET', 'PACKAGE')):
                continue
            op_label = action
            op_icon = 'fa-solid fa-circle-info'
            badge_class = 'bg-slate-500/20 text-slate-300 border-slate-500/30'
            user_type_label = 'عملية نظام'
            user_type_badge = 'bg-slate-500/10 text-slate-400 border border-slate-500/20'
            
        created_at_dt = r.get('created_at')
        if hasattr(created_at_dt, 'strftime'):
            dt_str = created_at_dt.strftime('%Y-%m-%d %H:%M:%S')
        else:
            dt_str = str(created_at_dt or '')
            
        op_key = f"{target_name}_{action}_{dt_str}"
        if op_key in seen_keys:
            continue
        seen_keys.add(op_key)
        
        ops.append({
            'target_name': target_name,
            'op_label': op_label,
            'op_icon': op_icon,
            'badge_class': badge_class,
            'user_type_label': user_type_label,
            'user_type_badge': user_type_badge,
            'created_at_str': dt_str,
            'operator': operator,
            'details': details
        })
        if len(ops) >= limit:
            break

    # Fallback to recent activated vouchers if list is less than limit
    if len(ops) < limit:
        needed = limit - len(ops)
        try:
            recent_vouchers = query_all('''
                SELECT v.id, v.username, v.first_used_at, v.created_at, p.name as package_name
                FROM wisp_vouchers v
                LEFT JOIN wisp_packages p ON v.package_id = p.id
                WHERE v.first_used_at IS NOT NULL
                ORDER BY v.first_used_at DESC LIMIT ?
            ''', (needed,))
            for v in recent_vouchers:
                target_name = v.get('username') or ''
                dt = v.get('first_used_at') or v.get('created_at')
                dt_str = dt.strftime('%Y-%m-%d %H:%M:%S') if hasattr(dt, 'strftime') else str(dt or '')
                op_key = f"{target_name}_VOUCHER_{dt_str}"
                if op_key not in seen_keys:
                    seen_keys.add(op_key)
                    ops.append({
                        'target_name': target_name,
                        'op_label': 'تفعيل كارت إنترنت',
                        'op_icon': 'fa-solid fa-bolt',
                        'badge_class': 'bg-amber-500/20 text-amber-300 border-amber-500/30',
                        'user_type_label': 'كارت هوتسبوت',
                        'user_type_badge': 'bg-amber-500/10 text-amber-400 border border-amber-500/20',
                        'created_at_str': dt_str,
                        'operator': 'النظام الذكي',
                        'details': f"تفعيل كارت {target_name} ({v.get('package_name', 'باقة عامة')})"
                    })
        except Exception:
            pass

    return ops[:limit]



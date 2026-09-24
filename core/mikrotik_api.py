# -*- coding: utf-8 -*-
"""
High-Performance MikroTik RouterOS API Client & Network Probe.
Supports RouterOS v6 & v7 binary API protocol, resource metrics,
active session queries, and concurrent multi-router status monitoring.
"""

import socket
import struct
import hashlib
import time
import subprocess
import threading
import concurrent.futures
from database.db import query_all, query_one

_nas_live_cache = {
    'timestamp': 0,
    'data': []
}
_nas_cache_lock = threading.Lock()

class RouterOSApiProtocol:
    """Implements MikroTik RouterOS API binary word-based protocol (Port 8728/8729)."""
    
    def __init__(self, host, port=8728, timeout=1.5, use_ssl=False):
        self.host = str(host)
        self.port = int(port)
        self.timeout = float(timeout)
        self.use_ssl = bool(use_ssl)
        self.sock = None

    def connect(self):
        raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw_sock.settimeout(self.timeout)
        raw_sock.connect((self.host, self.port))
        if self.use_ssl:
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self.sock = ctx.wrap_socket(raw_sock, server_hostname=self.host)
        else:
            self.sock = raw_sock

    def execute_command(self, cmd, words=None):
        """Convenience method to execute a command sentence, e.g. '/tool/user-manager/profile/print'."""
        sentence = [cmd]
        if words:
            if isinstance(words, (list, tuple)):
                sentence.extend(words)
            elif isinstance(words, dict):
                for k, v in words.items():
                    sentence.append(f'={k}={v}')
        return self.talk(sentence)

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def _write_len(self, length):
        if length < 0x80:
            self.sock.sendall(struct.pack('!B', length))
        elif length < 0x4000:
            length |= 0x8000
            self.sock.sendall(struct.pack('!H', length))
        elif length < 0x200000:
            length |= 0xC00000
            self.sock.sendall(struct.pack('!I', length)[1:])
        elif length < 0x10000000:
            length |= 0xE0000000
            self.sock.sendall(struct.pack('!I', length))
        else:
            self.sock.sendall(b'\xF0' + struct.pack('!I', length))

    def _read_len(self):
        b1 = self.sock.recv(1)
        if not b1:
            return 0
        b1_val = b1[0]
        if (b1_val & 0x80) == 0:
            return b1_val
        elif (b1_val & 0xC0) == 0x80:
            b2 = self.sock.recv(1)[0]
            return ((b1_val & 0x3F) << 8) + b2
        elif (b1_val & 0xE0) == 0xC0:
            b2, b3 = self.sock.recv(2)
            return ((b1_val & 0x1F) << 16) + (b2 << 8) + b3
        elif (b1_val & 0xF0) == 0xE0:
            b2, b3, b4 = self.sock.recv(3)
            return ((b1_val & 0x0F) << 24) + (b2 << 16) + (b3 << 8) + b4
        elif (b1_val & 0xF8) == 0xF0:
            b = self.sock.recv(4)
            return struct.unpack('!I', b)[0]
        return 0

    def write_word(self, word):
        if isinstance(word, str):
            word_bytes = word.encode('utf-8')
        else:
            word_bytes = word
        self._write_len(len(word_bytes))
        self.sock.sendall(word_bytes)

    def write_sentence(self, words):
        for w in words:
            self.write_word(w)
        self.write_word(b'')  # Terminate sentence with 0 length

    def read_sentence(self):
        res = []
        while True:
            w_len = self._read_len()
            if w_len == 0:
                break
            data = bytearray()
            while len(data) < w_len:
                chunk = self.sock.recv(w_len - len(data))
                if not chunk:
                    break
                data.extend(chunk)
            res.append(data.decode('utf-8', errors='replace'))
        return res

    def talk_raw(self, words):
        """Sends sentence and returns list of raw (reply_type, attrs) tuples."""
        self.write_sentence(words)
        replies = []
        while True:
            sentence = self.read_sentence()
            if not sentence:
                break
            reply_type = sentence[0] if sentence else ''
            attrs = {}
            for item in sentence[1:]:
                if item.startswith('='):
                    parts = item[1:].split('=', 1)
                    if len(parts) == 2:
                        attrs[parts[0]] = parts[1]
                    else:
                        attrs[parts[0]] = ''
            replies.append((reply_type, attrs))
            if reply_type in ('!done', '!trap', '!fatal'):
                break
        return replies

    def talk(self, words):
        """Standard query returning data rows (list of dicts)."""
        replies = self.talk_raw(words)
        results = []
        for r_type, attrs in replies:
            if r_type == '!re':
                results.append(attrs)
            elif r_type == '!done' and attrs:
                results.append(attrs)
            elif r_type == '!trap':
                results.append({'!trap': attrs.get('message', 'Error')})
        return results

    def login(self, username, password):
        """Supports RouterOS v6 (MD5 challenge) and RouterOS v7 (Plain auth)."""
        # 1. Try modern ROS login (v6.43+ and v7.x)
        replies = self.talk_raw(['/login', f'=name={username}', f'=password={password}'])
        has_trap = False
        for r_type, _ in replies:
            if r_type == '!done':
                return True
            if r_type == '!trap':
                has_trap = True
                break

        # 2. Try classic ROS v6 MD5 challenge (< 6.43) only if no explicit bad-credentials trap
        if not has_trap:
            chal_replies = self.talk_raw(['/login'])
            for r_type, attrs in chal_replies:
                if r_type == '!done' and 'ret' in attrs:
                    chal_hex = attrs['ret']
                    chal_bytes = bytes.fromhex(chal_hex)
                    md5_in = b'\x00' + password.encode('utf-8') + chal_bytes
                    resp_hex = '00' + hashlib.md5(md5_in).hexdigest()
                    res2 = self.talk_raw(['/login', f'=name={username}', f'=response={resp_hex}'])
                    for r2_type, _ in res2:
                        if r2_type == '!done':
                            return True

        return False


def ping_host(host, timeout=0.25, extra_port=None):
    """
    Ultra-Fast MikroTik reachability check:
    1. Direct TCP connect probe on router API / designated port (completes in < 60ms).
    2. Fast ICMP fallback (0.25s).
    """
    if not host:
        return False, 0

    # 1. Direct TCP Probe on Designated API Port / Winbox
    ports_to_try = [extra_port] if (extra_port and isinstance(extra_port, int)) else [8728, 8291]
    for port in ports_to_try:
        if not port:
            continue
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        t_start = time.time()
        try:
            s.connect((host, port))
            latency = max(1, int((time.time() - t_start) * 1000))
            return True, latency
        except Exception:
            continue
        finally:
            try:
                s.close()
            except Exception:
                pass

    # 2. Fast ICMP Ping Fallback
    try:
        t0 = time.time()
        import platform
        if platform.system().lower() == 'windows':
            cmd = ['ping', '-n', '1', '-w', '250', str(host)]
        else:
            cmd = ['ping', '-c', '1', '-W', '1', str(host)]
        res = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=0.35
        )
        out = res.stdout.lower()
        if res.returncode == 0 and ('1 packets received' in out or '1 received' in out or 'bytes=' in out) and '100% packet loss' not in out and 'unreachable' not in out:
            if 'time=' in out:
                try:
                    time_part = out.split('time=')[1].split(' ')[0].replace('ms', '')
                    return True, max(1, int(float(time_part)))
                except Exception:
                    pass
            return True, max(1, int((time.time() - t0) * 1000))
    except Exception:
        pass

    return False, 0


def fetch_single_nas_status(nas_row):
    """
    Fetches real-time hardware status and live metrics for a single NAS router.
    """
    nas_id = nas_row['id']
    host = nas_row['ip_address']
    api_port = int(nas_row.get('api_port') or 8728)
    api_user = nas_row.get('api_username') or 'admin'
    api_pass = nas_row.get('api_password') or ''
    
    # Active sessions from radius accounting table
    db_sessions_count = 0
    try:
        acct_row = query_one(
            "SELECT COUNT(*) as active_cnt FROM radacct WHERE nasipaddress = ? AND acctstoptime IS NULL",
            (host,)
        )
        if acct_row:
            db_sessions_count = int(acct_row['active_cnt'] or 0)
    except Exception:
        pass

    # 1. Real Ping & Reachability Check (Fast 0.35s probe)
    is_online, latency_ms = ping_host(host, timeout=0.35, extra_port=api_port)
    is_l2tp = False

    # If direct ping probe fails, check if this router is reachable via L2TP VPN Container Bridge
    if not is_online:
        try:
            from services.l2tp_service import get_active_ppp_sessions, _docker_exec_run, L2TP_CONTAINER_NAME
            # Direct ICMP ping through L2TP container tap_vpn bridge
            res = _docker_exec_run(L2TP_CONTAINER_NAME, ['ping', '-c', '1', '-W', '1', str(host)], timeout=1.2)
            out = (res.get('stdout') or '').lower()
            if 'bytes from' in out or '1 packets received' in out or '1 received' in out:
                is_online = True
                is_l2tp = True
                latency_ms = 12
                if 'time=' in out:
                    try:
                        latency_ms = max(1, int(float(out.split('time=')[1].split()[0].replace('ms', ''))))
                    except Exception:
                        pass
            
            if not is_online:
                l2tp_tunnel = query_one(
                    "SELECT username FROM wisp_l2tp_tunnels WHERE tunnel_ip = ? OR name = ?",
                    (host, nas_row.get('name'))
                )
                if l2tp_tunnel:
                    u = l2tp_tunnel['username'].lower()
                    active_l2tp = get_active_ppp_sessions()
                    if u in active_l2tp:
                        is_online = True
                        is_l2tp = True
                        latency_ms = 12
        except Exception:
            pass

    if not is_online:
        return {
            'id': nas_id,
            'name': nas_row.get('name', 'MikroTik Router'),
            'ip_address': host,
            'nas_type': nas_row.get('nas_type', 'mikrotik'),
            'coa_port': int(nas_row.get('coa_port') or 3799),
            'api_port': api_port,
            'is_online': False,
            'status': 'offline',
            'status_text': 'غير متصل (Offline)',
            'latency_ms': 0,
            'board_name': '-',
            'ros_version': '-',
            'uptime': '-',
            'active_users': db_sessions_count,
            'cpu_load': 0,
            'ram_usage_pct': 0,
            'total_memory_mb': 0,
            'free_memory_mb': 0,
            'api_connected': False,
            'api_error': 'الراوتر غير متصل بالشبكة (Host Unreachable)',
            'last_check': time.strftime('%H:%M:%S')
        }

    # 2. If host is reachable, attempt RouterOS API Connection
    status_label = 'متصل (L2TP VPN)' if is_l2tp else 'متصل (Online)'
    result = {
        'id': nas_id,
        'name': nas_row.get('name', 'MikroTik Router'),
        'ip_address': host,
        'nas_type': nas_row.get('nas_type', 'mikrotik'),
        'coa_port': int(nas_row.get('coa_port') or 3799),
        'api_port': api_port,
        'is_online': True,
        'status': 'online',
        'status_text': status_label,
        'latency_ms': latency_ms,
        'board_name': 'RouterBOARD',
        'ros_version': 'RouterOS (L2TP)' if is_l2tp else 'RouterOS (API مغلق)',
        'uptime': 'متصل بالنفق' if is_l2tp else 'متصل بالشبكة',
        'active_users': db_sessions_count,
        'cpu_load': 0,
        'ram_usage_pct': 0,
        'total_memory_mb': 0,
        'free_memory_mb': 0,
        'api_connected': False,
        'api_error': 'بانتظار تفعيل منفذ الـ API في الراوتر (/ip service enable api)',
        'last_check': time.strftime('%H:%M:%S')
    }

    client = RouterOSApiProtocol(host, port=api_port, timeout=0.8)
    try:
        client.connect()
        logged_in = client.login(api_user, api_pass)
        if logged_in:
            result['api_connected'] = True
            result['api_error'] = None
            
            # Fetch /system/resource/print
            res_data = client.talk(['/system/resource/print'])
            if res_data and len(res_data) > 0 and '!trap' not in res_data[0]:
                res = res_data[0]
                result['uptime'] = res.get('uptime', result['uptime'])
                result['ros_version'] = res.get('version', result['ros_version'])
                result['board_name'] = res.get('board-name', res.get('platform', 'RouterBOARD'))
                
                # CPU load
                try:
                    result['cpu_load'] = int(res.get('cpu-load', 0))
                except Exception:
                    result['cpu_load'] = 0

                # Memory Calculation
                try:
                    total_mem = int(res.get('total-memory', 0))
                    free_mem = int(res.get('free-memory', 0))
                    if total_mem > 0:
                        result['total_memory_mb'] = round(total_mem / (1024 * 1024), 1)
                        result['free_memory_mb'] = round(free_mem / (1024 * 1024), 1)
                        used_mem = total_mem - free_mem
                        result['ram_usage_pct'] = round((used_mem / total_mem) * 100, 1)
                except Exception:
                    pass

            # Fetch Active Hotspot & PPP Users from RouterOS API
            try:
                hotspot_active = client.talk(['/ip/hotspot/active/print'])
                ppp_active = client.talk(['/ppp/active/print'])
                
                api_active_count = 0
                if hotspot_active and '!trap' not in hotspot_active[0]:
                    api_active_count += len(hotspot_active)
                if ppp_active and '!trap' not in ppp_active[0]:
                    api_active_count += len(ppp_active)
                    
                result['active_users'] = api_active_count if api_active_count > 0 else db_sessions_count
            except Exception:
                pass
        else:
            result['api_error'] = 'فشل تسجيل الدخول للـ API: اسم المستخدم أو كلمة المرور غير صحيحة'
    except socket.timeout:
        result['api_error'] = f'انتهت مهلة الانتظار (Timeout) لمنفذ الـ API {api_port}. تأكد من تفعيل الخدمة في ميكروتيك.'
    except ConnectionRefusedError:
        result['api_error'] = f'منفذ API {api_port} مغلق في ميكروتيك. يرجى تفعيله عبر /ip service enable api'
    except Exception as e:
        result['api_error'] = f'خطأ اتصال بالـ API: {str(e)}'
    finally:
        client.close()

    return result


def get_all_nas_live_status(force_refresh=False):
    """
    Asynchronously queries all NAS routers in parallel using ThreadPoolExecutor.
    Guarantees fast batch probing even with dozens of routers.
    Includes in-memory cache with 10s TTL to prevent thread storms.
    """
    now = time.time()
    if not force_refresh and (now - _nas_live_cache['timestamp'] < 10) and _nas_live_cache['data']:
        return _nas_live_cache['data']

    try:
        devices = query_all('SELECT * FROM wisp_nas_devices ORDER BY id ASC')
    except Exception:
        devices = []

    if not devices:
        return []

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(12, len(devices))) as executor:
        future_map = {executor.submit(fetch_single_nas_status, dev): dev for dev in devices}
        for future in concurrent.futures.as_completed(future_map):
            try:
                data = future.result()
                results.append(data)
            except Exception:
                dev = future_map[future]
                results.append({
                    'id': dev['id'],
                    'name': dev.get('name', 'Router'),
                    'ip_address': dev.get('ip_address', ''),
                    'is_online': False,
                    'status': 'offline',
                    'status_text': 'غير متصل (Offline)',
                    'latency_ms': 0,
                    'board_name': '-',
                    'ros_version': '-',
                    'uptime': '-',
                    'active_users': 0,
                    'cpu_load': 0,
                    'ram_usage_pct': 0,
                    'api_connected': False
                })

    results.sort(key=lambda x: x['id'])
    with _nas_cache_lock:
        _nas_live_cache['timestamp'] = now
        _nas_live_cache['data'] = results
    return results

def get_nas_status(host, api_port=8728, coa_port=3799):
    """Helper for single router status."""
    is_online, latency = ping_host(host, timeout=0.35)
    if is_online:
        return {'status': 'online', 'latency_ms': latency, 'method': f'TCP/ICMP {api_port}'}
    return {'status': 'offline', 'latency_ms': 0, 'method': 'None'}

def sync_mikrotik_router_clock(host, username, password, port=8728, tz_name=None):
    """
    Synchronizes the MikroTik RouterOS clock and timezone via RouterOS API.
    Executes /system/clock/set with server current date, time, and timezone.
    Returns: (success: bool, msg: str)
    """
    if not host or not username:
        return False, "بيانات الاتصال بـ API غير مكتملة"

    from core.time_service import get_system_now, get_configured_timezone_name
    tz = tz_name or get_configured_timezone_name()
    now = get_system_now(tz)
    
    date_str = now.strftime('%b/%d/%Y')  # e.g. Sep/22/2026
    time_str = now.strftime('%H:%M:%S')  # e.g. 01:40:00
    
    ros = RouterOSApiProtocol(host, port=port, timeout=3.0)
    try:
        ros.connect()
        if not ros.login(username, password or ''):
            return False, "فشل تسجيل الدخول إلى MikroTik API"

        # 1. Set Clock
        cmd = [
            '/system/clock/set',
            f'=time-zone-name={tz}',
            f'=date={date_str}',
            f'=time={time_str}'
        ]
        ros.talk(cmd)

        # 2. Configure NTP Client (RouterOS v6 & v7 friendly)
        try:
            ros.talk([
                '/system/ntp/client/set',
                '=enabled=yes',
                '=servers=time.google.com,pool.ntp.org'
            ])
        except Exception:
            pass

        ros.close()
        return True, f"تمت مزامنة ساعة الراوتر بنجاح مع توقيت السيرفر ({tz} - {time_str} - {date_str})"
    except Exception as e:
        ros.close()
        return False, f"خطأ أثناء مزامنة ساعة الراوتر: {e}"
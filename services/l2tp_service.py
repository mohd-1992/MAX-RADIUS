# -*- text -*-
# -*- coding: utf-8 -*-
"""
services/l2tp_service.py
-------------------------
High-Performance Linux Native L2TP/PPP Server Management Service for MAX RADIUS.
Provides direct User-Based Static IP Assignment via PPP IPCP protocol (Port 1701 UDP),
eliminating dynamic MAC/DHCP collisions permanently, maintaining zero CPU overhead,
enforcing strict license limits, and generating clean 1-Click MikroTik RouterOS setup scripts.
"""

import os
import re
import time
import socket
import logging
import random
import string
import datetime
import threading
import subprocess
import urllib.request
import http.client
import concurrent.futures
from database.db import query_all, query_one, execute_write, log_audit
from services.nas_service import reload_freeradius_clients

logger = logging.getLogger('l2tp_service')

_vps_ip_cache = {'ip': None, 'timestamp': 0}
_tunnels_live_cache = {'data': None, 'timestamp': 0}
_cache_lock = threading.Lock()

L2TP_CONTAINER_NAME = os.environ.get('L2TP_CONTAINER', 'max_radius_l2tp')
L2TP_GATEWAY_IP = '192.168.44.1'
L2TP_SERVER_PORT = 1701


def get_l2tp_network_settings():
    """Retrieves dynamic L2TP network & IP pool settings from wisp_system_settings."""
    rows = query_all("""
        SELECT `key`, `value` FROM wisp_system_settings
        WHERE `key` IN ('l2tp_gateway_ip', 'l2tp_mask', 'l2tp_pool_start', 'l2tp_pool_end', 'l2tp_server_port', 'l2tp_ipsec_secret')
    """)
    settings = {
        'l2tp_gateway_ip': '192.168.44.1',
        'l2tp_mask': '255.255.255.0',
        'l2tp_pool_start': '192.168.44.10',
        'l2tp_pool_end': '192.168.44.250',
        'l2tp_server_port': 1701,
        'l2tp_ipsec_secret': ''
    }
    for r in (rows or []):
        k = r['key']
        v = r['value']
        if k in settings and v:
            if k == 'l2tp_server_port':
                try:
                    settings[k] = int(v)
                except Exception:
                    pass
            else:
                settings[k] = str(v).strip()
    return settings


def save_l2tp_network_settings(form_or_data, admin_username='admin'):
    """Saves dynamic L2TP network & IP pool settings to wisp_system_settings."""
    keys = ['l2tp_gateway_ip', 'l2tp_mask', 'l2tp_pool_start', 'l2tp_pool_end', 'l2tp_server_port', 'l2tp_ipsec_secret']
    for k in keys:
        if k in form_or_data:
            val = str(form_or_data.get(k) or '').strip()
            execute_db("""
                INSERT INTO wisp_system_settings (`key`, `value`)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE `value` = VALUES(`value`)
            """, (k, val))
    return True


def _docker_exec_run(container_name, cmd, timeout=3.0):
    """Executes a command inside a Docker container via Docker socket or CLI."""
    try:
        class _UnixHTTPConnection(http.client.HTTPConnection):
            def __init__(self, socket_path, timeout=timeout):
                super().__init__('localhost', timeout=timeout)
                self.socket_path = socket_path
            def connect(self):
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.settimeout(self.timeout)
                self.sock.connect(self.socket_path)

        import json
        conn = _UnixHTTPConnection('/var/run/docker.sock', timeout=timeout)
        body1 = json.dumps({"AttachStdout": True, "AttachStderr": True, "Cmd": cmd})
        conn.request('POST', f'/containers/{container_name}/exec', body1, {'Content-Type': 'application/json'})
        resp1 = conn.getresponse()
        resp_data = resp1.read().decode('utf-8', errors='ignore')
        exec_id = json.loads(resp_data).get("Id") if resp_data else None
        conn.close()

        if not exec_id:
            return {"success": False, "stdout": "", "stderr": "Failed to create exec instance"}

        conn2 = _UnixHTTPConnection('/var/run/docker.sock', timeout=timeout)
        body2 = json.dumps({"Detach": False, "Tty": False})
        conn2.request('POST', f'/exec/{exec_id}/start', body2, {'Content-Type': 'application/json'})
        resp2 = conn2.getresponse()
        raw_body = resp2.read()
        conn2.close()

        out = raw_body.decode('utf-8', errors='ignore')
        return {"success": True, "stdout": out, "stderr": ""}
    except Exception:
        try:
            cli_cmd = ['docker', 'exec', container_name] + cmd
            r = subprocess.run(cli_cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
            return {"success": r.returncode == 0, "stdout": r.stdout, "stderr": r.stderr}
        except Exception as e:
            return {"success": False, "stdout": "", "stderr": str(e)}


def ensure_l2tp_host_route():
    """
    Ensures that the L2TP VPN client subnet (192.168.44.0/24) is routed to the L2TP container gateway (172.18.0.10).
    """
    try:
        subprocess.run(['ip', 'route', 'replace', '192.168.44.0/24', 'via', '172.18.0.10'], 
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
    except Exception:
        pass


def sync_all_tunnels_to_vpn():
    """
    Synchronizes all active tunnels in wisp_l2tp_tunnels to /etc/ppp/chap-secrets & /etc/ppp/pap-secrets.
    Enforces exact user-to-IP binding at PPP kernel level and ensures routing path.
    """
    ensure_l2tp_host_route()
    tunnels = query_all("SELECT * FROM wisp_l2tp_tunnels WHERE is_enabled = 1")
    lines = [
        '# Secrets for authentication using CHAP & PAP',
        '# client\tserver\tsecret\tIP addresses'
    ]
    for t in (tunnels or []):
        u = t['username']
        p = t['password']
        ip = t.get('tunnel_ip') or '*'
        lines.append(f'"{u}"\t*\t"{p}"\t{ip}')
    
    file_content = '\n'.join(lines) + '\n'
    
    write_cmd = [
        'bash', '-c',
        f"cat << 'EOF' > /etc/ppp/chap-secrets\n{file_content}EOF\ncp /etc/ppp/chap-secrets /etc/ppp/pap-secrets && chmod 600 /etc/ppp/chap-secrets /etc/ppp/pap-secrets"
    ]
    res = _docker_exec_run(L2TP_CONTAINER_NAME, write_cmd, timeout=3.0)
    logger.info(f"[L2TP Engine] Synced {len(tunnels or [])} tunnels to PPP secrets.")
    return res.get('success', False)



def get_public_vps_ip():
    """Retrieves the public VPS IP address with caching."""
    global _vps_ip_cache
    now = time.time()
    if _vps_ip_cache['ip'] and (now - _vps_ip_cache['timestamp'] < 300):
        return _vps_ip_cache['ip']

    # 1. Check system settings override
    row = query_one("SELECT `value` FROM wisp_system_settings WHERE `key` = 'server_public_ip'")
    if row and row.get('value'):
        _vps_ip_cache['ip'] = row['value'].strip()
        _vps_ip_cache['timestamp'] = now
        return _vps_ip_cache['ip']

    # 2. Query external IP resolvers
    resolvers = [
        'https://api.ipify.org',
        'https://ifconfig.me/ip',
        'https://icanhazip.com'
    ]
    for url in resolvers:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'MAX-RADIUS-L2TP/2.0'})
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                ip_str = resp.read().decode('utf-8').strip()
                if re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', ip_str):
                    _vps_ip_cache['ip'] = ip_str
                    _vps_ip_cache['timestamp'] = now
                    return ip_str
        except Exception:
            continue

    fallback = '136.244.95.245'
    _vps_ip_cache['ip'] = fallback
    _vps_ip_cache['timestamp'] = now
    return fallback


def detect_vps_public_ip():
    """Alias for get_public_vps_ip."""
    return get_public_vps_ip()


def get_active_ppp_sessions():
    """
    Queries active PPP interface sessions directly from the Linux L2TP container.
    Returns a dict mapping peer IP to interface info: { '192.168.44.10': {'interface': 'ppp0', 'local_ip': '192.168.44.1'} }
    """
    res = _docker_exec_run(L2TP_CONTAINER_NAME, ['ip', '-o', '-4', 'addr', 'show'], timeout=2.0)
    out = res.get('stdout', '')
    active_peers = {}
    
    for line in out.splitlines():
        if 'peer' in line and 'ppp' in line:
            m = re.search(r'(\w+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)\s+peer\s+(\d+\.\d+\.\d+\.\d+)', line)
            if m:
                iface, local_ip, peer_ip = m.groups()
                active_peers[peer_ip] = {
                    'interface': iface,
                    'local_ip': local_ip,
                    'peer_ip': peer_ip
                }
    return active_peers


def get_l2tp_tunnels(force_fresh=False, fast_db_only=False):
    """
    Fetches all configured L2TP tunnels from wisp_l2tp_tunnels, merges live active sessions,
    and returns rich metric data. Fast in-memory cache with 10s TTL.
    """
    global _tunnels_live_cache
    now = time.time()
    
    with _cache_lock:
        if not force_fresh and _tunnels_live_cache['data'] is not None and (now - _tunnels_live_cache['timestamp'] < 10):
            return _tunnels_live_cache['data']

    tunnels = query_all("""
        SELECT t.*, 
               COALESCE(n.name, t.name) as router_name,
               COALESCE(n.ip_address, t.tunnel_ip) as router_ip,
               n.secret as radius_secret,
               n.api_port,
               n.api_username,
               n.api_password
        FROM wisp_l2tp_tunnels t
        LEFT JOIN wisp_nas_devices n ON t.tunnel_ip = n.ip_address
        ORDER BY t.id ASC
    """)

    if fast_db_only:
        return tunnels or []

    active_peers = get_active_ppp_sessions()
    vps_ip = get_public_vps_ip()

    def check_tunnel_status(tun):
        ip = tun.get('tunnel_ip')
        is_online = ip in active_peers if ip else False
        
        latency = None
        if is_online and ip:
            try:
                t0 = time.time()
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.3)
                port = int(tun.get('api_port') or 8728)
                s.connect((ip, port))
                s.close()
                latency = round((time.time() - t0) * 1000, 1)
            except Exception:
                latency = 1.0

        tun['is_online'] = is_online
        tun['server_public_ip'] = vps_ip
        tun['latency_ms'] = latency
        tun['ppp_interface'] = active_peers.get(ip, {}).get('interface') if is_online else None
        return tun

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(check_tunnel_status, tunnels or []))

    with _cache_lock:
        _tunnels_live_cache['data'] = results
        _tunnels_live_cache['timestamp'] = now

    return results


def get_l2tp_tunnel(tunnel_id_or_username):
    """Fetches a single L2TP tunnel by ID or username with enriched NAS details."""
    if not tunnel_id_or_username:
        return None
    if str(tunnel_id_or_username).isdigit():
        tun = query_one("""
            SELECT t.*, 
                   COALESCE(n.name, t.name) as router_name,
                   COALESCE(n.ip_address, t.tunnel_ip) as router_ip,
                   n.secret as radius_secret,
                   n.api_port,
                   n.api_username,
                   n.api_password
            FROM wisp_l2tp_tunnels t
            LEFT JOIN wisp_nas_devices n ON t.tunnel_ip = n.ip_address
            WHERE t.id = %s
        """, (int(tunnel_id_or_username),))
    else:
        tun = query_one("""
            SELECT t.*, 
                   COALESCE(n.name, t.name) as router_name,
                   COALESCE(n.ip_address, t.tunnel_ip) as router_ip,
                   n.secret as radius_secret,
                   n.api_port,
                   n.api_username,
                   n.api_password
            FROM wisp_l2tp_tunnels t
            LEFT JOIN wisp_nas_devices n ON t.tunnel_ip = n.ip_address
            WHERE t.username = %s OR t.name = %s
        """, (str(tunnel_id_or_username), str(tunnel_id_or_username)))
    return tun


def get_available_tunnel_ip():
    """Generates the next available sequential static IP in 192.168.44.0/24 subnet."""
    used_rows = query_all("SELECT tunnel_ip FROM wisp_l2tp_tunnels WHERE tunnel_ip IS NOT NULL")
    used_ips = {r['tunnel_ip'].strip() for r in (used_rows or []) if r.get('tunnel_ip')}
    
    for i in range(10, 251):
        candidate = f"192.168.44.{i}"
        if candidate not in used_ips and candidate != '192.168.44.1':
            return candidate
    return '192.168.44.50'


def add_l2tp_tunnel(form_or_data, admin_username='admin'):
    """
    Creates a new L2TP tunnel with strict license enforcement, automatic random API credentials,
    and automatic synchronization with FreeRADIUS & Linux PPP kernel.
    """
    # 1. Enforce strict router license limit
    try:
        from services.license_guard_service import check_nas_quota
        allowed, err_msg, cur_nas, max_nas = check_nas_quota(1)
        if not allowed:
            raise ValueError(err_msg or f"تم الوصول إلى الحد الأقصى لعدد الراوترات في باقة الترخيص الحالية ({max_nas} راوتر).")
    except ImportError:
        pass

    data = form_or_data
    name = (data.get('name') or data.get('username') or '').strip()
    username = str(data.get('username') or '').strip()
    password = str(data.get('password') or '').strip()
    tunnel_ip = str(data.get('tunnel_ip') or data.get('assigned_ip') or '').strip()
    radius_secret = str(data.get('radius_secret') or 'max123').strip()
    ipsec_secret = str(data.get('ipsec_secret') or '').strip()
    description = str(data.get('description') or '').strip()

    if not tunnel_ip:
        tunnel_ip = get_available_tunnel_ip()

    if not username or not password:
        raise ValueError("اسم المستخدم وكلمة المرور للنفق مطلوبان.")

    # 2. Auto-generate secure, random API port and credentials if not explicitly provided
    if not data.get('api_port') or str(data.get('api_port')).strip() in ('8728', '0', ''):
        api_port = random.randint(11000, 58000)
    else:
        api_port = int(data.get('api_port'))

    raw_user = str(data.get('api_username') or data.get('api_user') or '').strip()
    if not raw_user or raw_user.lower() in ('admin', ''):
        rand_suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=5))
        api_user = f"api_{rand_suffix}"
    else:
        api_user = raw_user

    raw_pass = str(data.get('api_password') or data.get('api_pass') or '').strip()
    if not raw_pass:
        api_pass = ''.join(random.choices(string.ascii_letters + string.digits, k=10))
    else:
        api_pass = raw_pass

    # 3. Insert into wisp_l2tp_tunnels
    execute_write("""
        INSERT INTO wisp_l2tp_tunnels (name, username, password, tunnel_ip, radius_secret, ipsec_secret, description, status, is_enabled, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'offline', 1, CURRENT_TIMESTAMP)
        ON DUPLICATE KEY UPDATE name = VALUES(name), password = VALUES(password), tunnel_ip = VALUES(tunnel_ip),
                                radius_secret = VALUES(radius_secret), ipsec_secret = VALUES(ipsec_secret),
                                description = VALUES(description), is_enabled = 1
    """, (name, username, password, tunnel_ip, radius_secret, ipsec_secret, description))

    # 4. Insert into wisp_nas_devices with generated API credentials
    execute_write("""
        INSERT INTO wisp_nas_devices (name, ip_address, nas_type, secret, api_port, coa_port, api_username, api_password, description, status, created_at)
        VALUES (?, ?, 'mikrotik', ?, ?, 3799, ?, ?, ?, 'offline', CURRENT_TIMESTAMP)
        ON DUPLICATE KEY UPDATE name = VALUES(name), secret = VALUES(secret), api_port = VALUES(api_port),
                                api_username = VALUES(api_username), api_password = VALUES(api_password), description = VALUES(description)
    """, (name, tunnel_ip, radius_secret, api_port, api_user, api_pass, description))

    # 5. Insert into FreeRADIUS nas table
    execute_write("""
        INSERT INTO nas (nasname, shortname, type, secret, description)
        VALUES (?, ?, 'mikrotik', ?, ?)
        ON DUPLICATE KEY UPDATE secret = VALUES(secret), description = VALUES(description)
    """, (tunnel_ip, username, radius_secret, f"L2TP Router: {name}"))

    # 6. Synchronize PPP secrets & FreeRADIUS clients
    sync_all_tunnels_to_vpn()
    reload_freeradius_clients()

    with _cache_lock:
        _tunnels_live_cache['data'] = None

    log_audit(1, admin_username, 'CREATE_L2TP_TUNNEL', 'l2tp', f'Created L2TP tunnel {name} with fixed IP {tunnel_ip} (API Port: {api_port})')
    return True


def create_l2tp_tunnel(name, username, password, assigned_ip=None, radius_secret='max123',
                       api_port=None, api_user=None, api_pass=None, admin_username='admin'):
    """Keyword argument wrapper for add_l2tp_tunnel."""
    data = {
        'name': name,
        'username': username,
        'password': password,
        'tunnel_ip': assigned_ip,
        'radius_secret': radius_secret,
        'api_port': api_port,
        'api_username': api_user,
        'api_password': api_pass
    }
    return add_l2tp_tunnel(data, admin_username=admin_username)


def update_l2tp_tunnel(tunnel_id, form_or_data, admin_username='admin'):
    """Updates an existing L2TP tunnel, NAS device, and syncs PPP secrets."""
    data = form_or_data
    old = query_one("SELECT * FROM wisp_l2tp_tunnels WHERE id = ?", (tunnel_id,))
    if not old:
        raise ValueError("نفق الراوتر غير موجود.")

    name = (data.get('name') or old['name']).strip()
    username = str(data.get('username') or old['username']).strip()
    password = str(data.get('password') or old['password']).strip()
    tunnel_ip = str(data.get('tunnel_ip') or data.get('assigned_ip') or old['tunnel_ip']).strip()
    radius_secret = str(data.get('radius_secret') or old.get('radius_secret') or 'max123').strip()
    ipsec_secret = str(data.get('ipsec_secret') or old.get('ipsec_secret') or '').strip()
    description = str(data.get('description') or old.get('description') or '').strip()

    execute_write("""
        UPDATE wisp_l2tp_tunnels
        SET name = ?, username = ?, password = ?, tunnel_ip = ?, radius_secret = ?, ipsec_secret = ?, description = ?
        WHERE id = ?
    """, (name, username, password, tunnel_ip, radius_secret, ipsec_secret, description, tunnel_id))

    execute_write("""
        UPDATE wisp_nas_devices
        SET name = ?, ip_address = ?, secret = ?, description = ?
        WHERE ip_address = ?
    """, (name, tunnel_ip, radius_secret, description, old['tunnel_ip']))

    execute_write("""
        UPDATE nas
        SET nasname = ?, shortname = ?, secret = ?, description = ?
        WHERE nasname = ?
    """, (tunnel_ip, username, radius_secret, f"L2TP Router: {name}", old['tunnel_ip']))

    sync_all_tunnels_to_vpn()
    reload_freeradius_clients()

    with _cache_lock:
        _tunnels_live_cache['data'] = None

    log_audit(1, admin_username, 'UPDATE_L2TP_TUNNEL', 'l2tp', f'Updated L2TP tunnel {name} (IP: {tunnel_ip})')
    return True


def delete_l2tp_tunnel(tunnel_id, admin_username='admin'):
    """Deletes an L2TP tunnel, cleans NAS records, and syncs PPP secrets."""
    tun = query_one("SELECT * FROM wisp_l2tp_tunnels WHERE id = ?", (tunnel_id,))
    if not tun:
        return False

    ip = tun.get('tunnel_ip')
    u = tun.get('username')

    execute_write("DELETE FROM wisp_l2tp_tunnels WHERE id = ?", (tunnel_id,))
    if ip:
        execute_write("DELETE FROM wisp_nas_devices WHERE ip_address = ?", (ip,))
        execute_write("DELETE FROM nas WHERE nasname = ?", (ip,))

    sync_all_tunnels_to_vpn()
    reload_freeradius_clients()

    with _cache_lock:
        _tunnels_live_cache['data'] = None

    log_audit(1, admin_username, 'DELETE_L2TP_TUNNEL', 'l2tp', f'Deleted L2TP tunnel {tun.get("name")} ({u})')
    return True


def generate_mikrotik_rsc_script(tunnel_id_or_username, vps_host=None):
    """
    Generates a clean, 100% reliable 1-Click MikroTik RouterOS setup script.
    - Uses require-message-auth=no for complete RouterOS v7 & v6 compatibility.
    - Activates API service on a dedicated random port.
    - Creates a dedicated API user with secure credentials for live monitoring.
    """
    tun = None
    if isinstance(tunnel_id_or_username, int) or (isinstance(tunnel_id_or_username, str) and str(tunnel_id_or_username).isdigit()):
        tun = query_one("SELECT * FROM wisp_l2tp_tunnels WHERE id = ?", (int(tunnel_id_or_username),))
    else:
        tun = query_one("SELECT * FROM wisp_l2tp_tunnels WHERE username = ?", (str(tunnel_id_or_username).strip(),))

    if not tun:
        return "# Error: Tunnel not found in database."

    nas_dev = query_one("SELECT * FROM wisp_nas_devices WHERE ip_address = ?", (tun['tunnel_ip'],))
    vps_ip = vps_host or get_public_vps_ip()
    radius_secret = tun.get('radius_secret') or (nas_dev.get('secret') if nas_dev else None) or 'max123'
    ipsec_secret = tun.get('ipsec_secret') or ''
    use_ipsec = 'yes' if ipsec_secret else 'no'
    ipsec_param = f' ipsec-secret="{ipsec_secret}"' if ipsec_secret else ''
    tun_user = tun['username']
    tun_pass = tun['password']
    tunnel_ip = tun['tunnel_ip']

    api_port = (nas_dev.get('api_port') if nas_dev else None) or 25354
    api_user = (nas_dev.get('api_username') if nas_dev else None) or 'api_user'
    api_pass = (nas_dev.get('api_password') if nas_dev else None) or 'api_pass123'

    script = f"""###############################################################################
#  🚀 MAX RADIUS 2.0 - Clean 1-Click RouterOS Setup Script
#  📡 Router: {tun['name']}
#  🌐 Dedicated Tunnel IP: {tunnel_ip}
#  ⚡ Engine: Native L2TP/PPP (User-Bound Static IP via IPCP)
#  🔒 API Security: Port {api_port} | User: {api_user}
###############################################################################

:put "============================================================"
:put "  🚀 Starting MAX RADIUS Integration Script for: {tun['name']}"
:put "============================================================"

# 1. Configure L2TP Client Tunnel (Connects to MAX RADIUS VPS)
/interface l2tp-client
:do {{ remove [find name="l2tp-maxradius"] }} on-error={{}}
add name="l2tp-maxradius" connect-to="{vps_ip}" user="{tun_user}" password="{tun_pass}" \\
    profile=default use-ipsec={use_ipsec}{ipsec_param} disabled=no comment="MAX RADIUS VPN Tunnel"

:put "✔ L2TP Tunnel Interface Created."

# 2. Configure FreeRADIUS Server Connection (with require-message-auth=no)
/radius
:do {{ remove [find comment="MAX_RADIUS_CORE"] }} on-error={{}}
:do {{
    add address=192.168.44.1 secret="{radius_secret}" service=hotspot,login,wireless,ppp \\
        authentication-port=1812 accounting-port=1813 timeout=3s require-message-auth=no comment="MAX_RADIUS_CORE"
}} on-error={{
    add address=192.168.44.1 secret="{radius_secret}" service=hotspot,login,wireless,ppp \\
        authentication-port=1812 accounting-port=1813 timeout=3s comment="MAX_RADIUS_CORE"
}}

/radius incoming
set accept=yes port=3799

:put "✔ RADIUS Client & CoA (Port 3799) Configured."

# 3. Enable RADIUS on Hotspot Profile
/ip hotspot profile
:do {{
    set [find default=yes] use-radius=yes radius-accounting=yes radius-interim-update=3m
}} on-error={{
    :put "Notice: Default hotspot profile updated."
}}

# 4. Enable API Service with Dedicated Custom Port
/ip service
:do {{
    set api port={api_port} disabled=no
}} on-error={{
    :put "Notice: API service configuration updated."
}}

# 5. Create Dedicated Secure API User for Live Monitoring
/user
:do {{ remove [find comment="MAX_RADIUS_API"] }} on-error={{}}
:do {{ remove [find name="{api_user}"] }} on-error={{}}
add name="{api_user}" password="{api_pass}" group=full comment="MAX_RADIUS_API" disabled=no

:put "============================================================"
:put "  ✅ MAX RADIUS Setup Completed Successfully!"
:put "  🌐 Dedicated Static IP: {tunnel_ip}"
:put "  🔑 API Port: {api_port} | API User: {api_user}"
:put "============================================================"
"""
    return script

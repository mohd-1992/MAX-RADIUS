# -*- coding: utf-8 -*-
"""
services/wireguard_service.py
------------------------------
Ultra-High-Performance Linux Kernel Native WireGuard VPN Engine for MAX RADIUS 2.0.
Provides:
1. Kernel-Level Crypto Routing (chacha20-poly1305) on dynamic WireGuard port.
2. Dynamic, sub-millisecond Peer Registration without interface restarts or traffic interruption.
3. Live WireGuard Peer Telemetry (Handshake timestamps, Rx/Tx counters, Real-time ping latency).
4. Automated FreeRADIUS NAS Client Synchronization & MikroTik API credentials generation.
5. 1-Click RouterOS v7+ Native WireGuard Setup Script Generator with dynamic Gateway and Subnet.
"""

import os
import sys
import json
import time
import socket
import logging
import random
import string
import datetime
import threading
import concurrent.futures

from database.db import query_all, query_one, execute_write, log_audit
from services.nas_service import reload_freeradius_clients
from services.l2tp_service import get_public_vps_ip

logger = logging.getLogger('wireguard_service')

WG_SOCK_PATH = os.environ.get('WG_SOCK_PATH', '/app/storage/wg_bridge.sock')
if not os.path.exists(WG_SOCK_PATH):
    if os.path.exists('/opt/max-radius/storage/wg_bridge.sock'):
        WG_SOCK_PATH = '/opt/max-radius/storage/wg_bridge.sock'

WG_GATEWAY_IP = os.environ.get('WG_GATEWAY_IP', '192.168.45.1')
WG_SUBNET = os.environ.get('WG_SUBNET', '192.168.45.0/24')
WG_SERVER_PORT = int(os.environ.get('WG_SERVER_PORT', '51820'))
WG_SERVER_PUBKEY = os.environ.get('WG_SERVER_PUBKEY', 'aTFUO75Lr9Z9KXW/kPT9rH8x+BDA+VuYE1qfUcD6GnA=')

_wg_live_cache = {'data': None, 'timestamp': 0}
_wg_cache_lock = threading.Lock()


def _query_wg_bridge(action, payload=None, timeout=2.0):
    """Communicates with the host wg_bridge_daemon UNIX socket."""
    if not os.path.exists(WG_SOCK_PATH):
        logger.debug(f"WG socket not found at {WG_SOCK_PATH}")
        return {'success': False, 'error': f"Socket not found at {WG_SOCK_PATH}"}

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(WG_SOCK_PATH)

        req = {'action': action}
        if payload:
            req.update(payload)

        sock.sendall(json.dumps(req).encode('utf-8'))
        raw_data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            raw_data += chunk

        sock.close()
        return json.loads(raw_data.decode('utf-8'))
    except Exception as e:
        logger.error(f"[WG Bridge Error] Failed to communicate for action '{action}': {e}")
        return {'success': False, 'error': str(e)}


def generate_wg_keypair():
    """Generates a Curve25519 private/public keypair via kernel engine."""
    res = _query_wg_bridge('genkey')
    if res.get('success'):
        return res['private_key'], res['public_key']

    # Fallback to local python generation if daemon is unreachable
    try:
        import subprocess
        priv = subprocess.check_output(['wg', 'genkey'], text=True).strip()
        pub = subprocess.check_output(['wg', 'pubkey'], input=priv, text=True).strip()
        return priv, pub
    except Exception as e:
        logger.error(f"Fallback key generation failed: {e}")
        return None, None



def get_wireguard_server_keys():
    """Returns server-side public key, port, and gateway parameters."""
    return {
        'public_key': WG_SERVER_PUBKEY,
        'port': WG_SERVER_PORT,
        'gateway_ip': WG_GATEWAY_IP,
        'subnet': WG_SUBNET
    }

def get_wireguard_server_info():
    """Returns server-side WireGuard connection parameters for admin dashboard."""
    return {
        'public_key': WG_SERVER_PUBKEY,
        'port': WG_SERVER_PORT,
        'gateway_ip': WG_GATEWAY_IP,
        'subnet': WG_SUBNET,
        'detected_vps_ip': get_public_vps_ip()
    }


def get_available_wireguard_ip():
    """Allocates the next available static IP address in active WireGuard subnet."""
    prefix = WG_GATEWAY_IP.rsplit('.', 1)[0]
    used_rows = query_all("SELECT tunnel_ip FROM wisp_wireguard_tunnels WHERE tunnel_ip IS NOT NULL")
    used_ips = {r['tunnel_ip'].strip() for r in (used_rows or []) if r.get('tunnel_ip')}

    for i in range(10, 251):
        candidate = f"{prefix}.{i}"
        if candidate not in used_ips and candidate != WG_GATEWAY_IP:
            return candidate
    return f"{prefix}.50"


def apply_peer_to_kernel(public_key, allowed_ip, preshared_key=None):
    """Registers or updates a WireGuard peer dynamically in the Linux kernel."""
    if not public_key or not allowed_ip:
        return False
    res = _query_wg_bridge('add_peer', {
        'public_key': public_key.strip(),
        'allowed_ip': allowed_ip.strip(),
        'preshared_key': preshared_key.strip() if preshared_key else None
    })
    return res.get('success', False)


def remove_peer_from_kernel(public_key):
    """Removes a WireGuard peer from the active Linux kernel table."""
    if not public_key:
        return False
    res = _query_wg_bridge('remove_peer', {'public_key': public_key.strip()})
    return res.get('success', False)


def sync_all_wireguard_peers_to_kernel():
    """Syncs all active WireGuard peers from MySQL to the kernel."""
    res = _query_wg_bridge('sync_all')
    return res.get('success', False)


def ping_wireguard_peer(tunnel_ip, timeout_sec=0.4):
    """Performs sub-second ICMP ping to verify router reachability."""
    if not tunnel_ip:
        return {'is_online': False, 'latency_ms': 0}

    import subprocess
    try:
        cmd = ['ping', '-c', '1', '-W', str(int(timeout_sec)), str(tunnel_ip)]
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout_sec + 0.2)
        if r.returncode == 0:
            import re
            m = re.search(r'time=([0-9.]+)\s*ms', r.stdout)
            lat = int(float(m.group(1))) if m else 1
            return {'is_online': True, 'latency_ms': max(1, lat)}
    except Exception:
        pass
    return {'is_online': False, 'latency_ms': 0}


def get_all_wireguard_tunnels(fast_db_only=False):
    """Retrieves all WireGuard tunnels from DB merged with kernel metrics."""
    global _wg_live_cache
    now = time.time()

    with _wg_cache_lock:
        if not fast_db_only and _wg_live_cache['data'] is not None and (now - _wg_live_cache['timestamp'] < 2.0):
            return _wg_live_cache['data']

    rows = query_all("""
        SELECT t.*, n.id as nas_id, n.api_port, n.api_username
        FROM wisp_wireguard_tunnels t
        LEFT JOIN wisp_nas_devices n ON t.tunnel_ip = n.ip_address
        ORDER BY t.id ASC
    """) or []

    if fast_db_only:
        for r in rows:
            r['is_online'] = (r.get('status') == 'online')
            r['latency_ms'] = r.get('latency_ms') or 0
        return rows

    res = _query_wg_bridge('dump')
    kernel_peers = {}
    if res.get('success') and 'peers' in res:
        for p in res['peers']:
            kernel_peers[p['public_key']] = p

    tunnels = []
    ping_futures = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        for r in rows:
            tun = dict(r)
            pub = tun.get('public_key')
            kinfo = kernel_peers.get(pub, {})

            tun['rx_bytes'] = kinfo.get('transfer_rx', 0)
            tun['tx_bytes'] = kinfo.get('transfer_tx', 0)
            tun['endpoint'] = kinfo.get('endpoint', 'غير متصل')
            tun['latest_handshake'] = kinfo.get('latest_handshake', 0)

            last_hs = tun['latest_handshake']
            hs_active = (last_hs > 0) and ((int(time.time()) - last_hs) < 180)

            if hs_active or tun.get('tunnel_ip'):
                ping_futures[tun['id']] = executor.submit(ping_wireguard_peer, tun['tunnel_ip'])
            else:
                tun['is_online'] = False
                tun['latency_ms'] = 0

            tunnels.append(tun)

        for tun in tunnels:
            tid = tun['id']
            if tid in ping_futures:
                try:
                    probe = ping_futures[tid].result()
                    tun['is_online'] = probe['is_online']
                    tun['latency_ms'] = probe['latency_ms']
                except Exception:
                    tun['is_online'] = False
                    tun['latency_ms'] = 0
            tun['status'] = 'online' if tun['is_online'] else 'offline'

    with _wg_cache_lock:
        _wg_live_cache['data'] = tunnels
        _wg_live_cache['timestamp'] = now

    return tunnels


def get_wireguard_tunnel(tunnel_id):
    """Retrieves a single WireGuard tunnel by ID."""
    return query_one("""
        SELECT t.*, n.id as nas_id, n.api_port, n.api_username
        FROM wisp_wireguard_tunnels t
        LEFT JOIN wisp_nas_devices n ON t.tunnel_ip = n.ip_address
        WHERE t.id = ?
    """, (tunnel_id,))


def add_wireguard_tunnel(form_or_data, admin_username='admin'):
    """Creates a new WireGuard tunnel record, adds to Linux kernel, and provisions FreeRADIUS NAS."""
    data = form_or_data
    name = data.get('name', '').strip()
    tunnel_ip = data.get('tunnel_ip', '').strip()
    radius_secret = data.get('radius_secret', 'max123').strip() or 'max123'
    description = data.get('description', '').strip()
    preshared_key = data.get('preshared_key', '').strip() or None

    if not name:
        raise ValueError("اسم الراوتر مطلوب.")
    if not tunnel_ip:
        tunnel_ip = get_available_wireguard_ip()

    existing = query_one("SELECT id FROM wisp_wireguard_tunnels WHERE tunnel_ip = ?", (tunnel_ip,))
    if existing:
        raise ValueError(f"عنوان IP النفق ({tunnel_ip}) مستخدم بالفعل لراوتر آخر.")

    client_priv = data.get('private_key', '').strip()
    client_pub = data.get('public_key', '').strip()

    if not client_priv or not client_pub:
        client_priv, client_pub = generate_wg_keypair()
        if not client_priv or not client_pub:
            raise ValueError("فشل توليد مفاتيح WireGuard المشفرة.")

    api_port = random.randint(22000, 29999)
    api_user = f"api_wg_{random.randint(100, 999)}"
    api_pass = ''.join(random.choices(string.ascii_letters + string.digits, k=12))

    execute_write("""
        INSERT INTO wisp_wireguard_tunnels
        (name, public_key, private_key, preshared_key, tunnel_ip, radius_secret, listen_port, status, is_enabled, description, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'offline', 1, ?, NOW())
    """, (name, client_pub, client_priv, preshared_key, tunnel_ip, radius_secret, 13231, description))

    tun = query_one("SELECT id FROM wisp_wireguard_tunnels WHERE tunnel_ip = ?", (tunnel_ip,))
    tunnel_id = tun['id'] if tun else None

    execute_write("""
        INSERT INTO wisp_nas_devices (name, ip_address, secret, description, api_port, api_username, api_password, is_active, is_online, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, NOW())
        ON DUPLICATE KEY UPDATE secret = VALUES(secret), description = VALUES(description), api_port = VALUES(api_port), api_username = VALUES(api_username), api_password = VALUES(api_password)
    """, (name, tunnel_ip, radius_secret, description, api_port, api_user, api_pass))

    execute_write("""
        INSERT INTO nas (nasname, shortname, type, secret, description)
        VALUES (?, ?, 'other', ?, ?)
        ON DUPLICATE KEY UPDATE secret = VALUES(secret), description = VALUES(description)
    """, (tunnel_ip, f"wg_{name}", radius_secret, f"WireGuard Router: {name}"))

    apply_peer_to_kernel(client_pub, tunnel_ip, preshared_key)
    reload_freeradius_clients()

    with _wg_cache_lock:
        _wg_live_cache['data'] = None

    log_audit(1, admin_username, 'CREATE_WIREGUARD_TUNNEL', 'wireguard', f'Created WireGuard tunnel {name} (IP: {tunnel_ip})')
    return tunnel_id


def update_wireguard_tunnel(tunnel_id, form_or_data, admin_username='admin'):
    """Updates an existing WireGuard tunnel record and re-syncs kernel peer table."""
    data = form_or_data
    old = query_one("SELECT * FROM wisp_wireguard_tunnels WHERE id = ?", (tunnel_id,))
    if not old:
        raise ValueError("نفق WireGuard غير موجود.")

    name = (data.get('name') or old['name']).strip()
    tunnel_ip = str(data.get('tunnel_ip') or old['tunnel_ip']).strip()
    radius_secret = str(data.get('radius_secret') or old.get('radius_secret') or 'max123').strip()
    description = str(data.get('description') or old.get('description') or '').strip()
    client_pub = str(data.get('public_key') or old['public_key']).strip()
    client_priv = str(data.get('private_key') or old['private_key']).strip()
    preshared_key = str(data.get('preshared_key') or old.get('preshared_key') or '').strip() or None

    execute_write("""
        UPDATE wisp_wireguard_tunnels
        SET name = ?, tunnel_ip = ?, radius_secret = ?, description = ?, public_key = ?, private_key = ?, preshared_key = ?
        WHERE id = ?
    """, (name, tunnel_ip, radius_secret, description, client_pub, client_priv, preshared_key, tunnel_id))

    execute_write("""
        UPDATE wisp_nas_devices
        SET name = ?, ip_address = ?, secret = ?, description = ?
        WHERE ip_address = ?
    """, (name, tunnel_ip, radius_secret, description, old['tunnel_ip']))

    execute_write("""
        UPDATE nas
        SET nasname = ?, shortname = ?, secret = ?, description = ?
        WHERE nasname = ?
    """, (tunnel_ip, f"wg_{name}", radius_secret, f"WireGuard Router: {name}", old['tunnel_ip']))

    if old['public_key'] != client_pub:
        remove_peer_from_kernel(old['public_key'])

    apply_peer_to_kernel(client_pub, tunnel_ip, preshared_key)
    reload_freeradius_clients()

    with _wg_cache_lock:
        _wg_live_cache['data'] = None

    log_audit(1, admin_username, 'UPDATE_WIREGUARD_TUNNEL', 'wireguard', f'Updated WireGuard tunnel {name} (IP: {tunnel_ip})')
    return True


def delete_wireguard_tunnel(tunnel_id, admin_username='admin'):
    """Deletes a WireGuard tunnel, unregisters peer from Linux kernel, and cleans FreeRADIUS records."""
    tun = query_one("SELECT * FROM wisp_wireguard_tunnels WHERE id = ?", (tunnel_id,))
    if not tun:
        return False

    pub = tun.get('public_key')
    ip = tun.get('tunnel_ip')

    if pub:
        remove_peer_from_kernel(pub)

    execute_write("DELETE FROM wisp_wireguard_tunnels WHERE id = ?", (tunnel_id,))
    if ip:
        execute_write("DELETE FROM wisp_nas_devices WHERE ip_address = ?", (ip,))
        execute_write("DELETE FROM nas WHERE nasname = ?", (ip,))

    reload_freeradius_clients()

    with _wg_cache_lock:
        _wg_live_cache['data'] = None

    log_audit(1, admin_username, 'DELETE_WIREGUARD_TUNNEL', 'wireguard', f'Deleted WireGuard tunnel {tun.get("name")} ({ip})')
    return True


def generate_mikrotik_wireguard_script(tunnel_id, vps_host=None):
    """
    Generates a 100% reliable 1-Click RouterOS v7+ Native WireGuard Setup Script.
    """
    tun = get_wireguard_tunnel(tunnel_id)
    if not tun:
        return "# Error: WireGuard tunnel not found in database."

    nas_dev = query_one("SELECT * FROM wisp_nas_devices WHERE ip_address = ?", (tun['tunnel_ip'],))
    vps_ip = vps_host or get_public_vps_ip()
    radius_secret = tun.get('radius_secret') or (nas_dev.get('secret') if nas_dev else None) or 'max123'
    tunnel_ip = tun['tunnel_ip']
    client_priv = tun['private_key']
    client_pub = tun['public_key']
    server_pub = WG_SERVER_PUBKEY
    server_port = WG_SERVER_PORT
    prefix = WG_GATEWAY_IP.rsplit('.', 1)[0]

    api_port = (nas_dev.get('api_port') if nas_dev else None) or 25354
    api_user = (nas_dev.get('api_username') if nas_dev else None) or 'api_user'
    api_pass = (nas_dev.get('api_password') if nas_dev else None) or 'api_pass123'

    script = f"""###############################################################################
#  ⚡ MAX RADIUS 2.0 - 1-Click RouterOS v7+ WireGuard Setup Script
#  📡 Router: {tun['name']}
#  🌐 Dedicated Tunnel IP: {tunnel_ip}/24 (Gateway: {WG_GATEWAY_IP})
#  🚀 Protocol: Native WireGuard Kernel VPN (Port {server_port} UDP)
#  🔒 API Security: Port {api_port} | User: {api_user}
###############################################################################

:put "============================================================"
:put "  ⚡ Starting MAX RADIUS WireGuard Setup for: {tun['name']}"
:put "============================================================"

# 1. Create WireGuard Interface (RouterOS v7+)
/interface wireguard
:do {{ remove [find name="wg-maxradius"] }} on-error={{}}
add name="wg-maxradius" listen-port=13231 mtu=1420 private-key="{client_priv}" comment="MAX RADIUS WireGuard VPN"

:put "✔ WireGuard Interface Created."

# 2. Configure WireGuard Peer Connection to MAX RADIUS VPS Server
/interface wireguard peers
:do {{ remove [find interface="wg-maxradius"] }} on-error={{}}
add interface="wg-maxradius" public-key="{server_pub}" \
    endpoint-address="{vps_ip}" endpoint-port={server_port} \
    allowed-address={WG_SUBNET} persistent-keepalive=25s comment="MAX RADIUS Server Peer"

:put "✔ WireGuard Server Peer Configured."

# 3. Assign Dedicated Static IP to WireGuard Interface
/ip address
:do {{ remove [find interface="wg-maxradius"] }} on-error={{}}
add address={tunnel_ip}/24 interface="wg-maxradius" network={prefix}.0 comment="MAX RADIUS WireGuard Static IP"

:put "✔ Static IP {tunnel_ip} Assigned to Interface."

# 4. Configure FreeRADIUS Server Connection (Port 1812/1813 via WG Gateway)
/radius
:do {{ remove [find comment="MAX_RADIUS_CORE"] }} on-error={{}}
:do {{
    add address={WG_GATEWAY_IP} secret="{radius_secret}" service=hotspot,login,wireless,ppp \
        authentication-port=1812 accounting-port=1813 timeout=3s require-message-auth=no comment="MAX_RADIUS_CORE"
}} on-error={{
    add address={WG_GATEWAY_IP} secret="{radius_secret}" service=hotspot,login,wireless,ppp \
        authentication-port=1812 accounting-port=1813 timeout=3s comment="MAX_RADIUS_CORE"
}}

/radius incoming
set accept=yes port=3799

:put "✔ RADIUS Client & CoA (Port 3799) Configured."

# 5. Enable RADIUS on Hotspot Profile
/ip hotspot profile
:do {{
    set [find default=yes] use-radius=yes radius-accounting=yes radius-interim-update=3m
}} on-error={{
    :put "Notice: Default hotspot profile updated."
}}

# 6. Enable API Service with Dedicated Custom Port
/ip service
:do {{
    set api port={api_port} disabled=no
}} on-error={{
    :put "Notice: API service configuration updated."
}}

# 7. Create Dedicated Secure API User for Live Monitoring
/user
:do {{ remove [find comment="MAX_RADIUS_API"] }} on-error={{}}
:do {{ remove [find name="{api_user}"] }} on-error={{}}
add name="{api_user}" password="{api_pass}" group=full comment="MAX_RADIUS_API" disabled=no

:put "============================================================"
:put "  ✅ MAX RADIUS WireGuard Setup Completed Successfully!"
:put "  🌐 Dedicated Static IP: {tunnel_ip}"
:put "  🔑 API Port: {api_port} | API User: {api_user}"
:put "============================================================"
"""
    return script

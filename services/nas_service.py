# -*- coding: utf-8 -*-
"""
NAS / MikroTik Router management service:
Coordinates router list, live ping connectivity, API credentials,
and Change of Authorization (CoA) testing.
"""

from database.db import query_all, query_one, execute_write, log_audit
from core.mikrotik_api import get_all_nas_live_status, fetch_single_nas_status, get_nas_status
from core.coa import RadiusCoaClient

def get_nas_devices(skip_live_probe=False):
    devices = query_all('SELECT * FROM wisp_nas_devices ORDER BY id ASC')
    if not devices:
        return []
    
    # Fast session counting from radacct
    session_map = {}
    try:
        active_counts = query_all('SELECT nasipaddress, COUNT(*) as cnt FROM radacct WHERE acctstoptime IS NULL GROUP BY nasipaddress')
        for ac in (active_counts or []):
            session_map[ac['nasipaddress']] = ac['cnt']
    except Exception:
        pass

    if not skip_live_probe:
        live_statuses = {d['id']: d for d in get_all_nas_live_status()}
    else:
        live_statuses = {}

    for d in devices:
        st = live_statuses.get(d['id'], {})
        d['live_status'] = st.get('status', 'offline')
        d['is_online'] = st.get('is_online', False)
        d['status_text'] = st.get('status_text', 'غير متصل')
        d['latency_ms'] = st.get('latency_ms', 0)
        d['board_name'] = st.get('board_name', '-')
        d['ros_version'] = st.get('ros_version', '-')
        d['uptime'] = st.get('uptime', '-')
        d['active_users'] = st.get('active_users', session_map.get(d['ip_address'], 0))
        d['cpu_load'] = st.get('cpu_load', 0)
        d['ram_usage_pct'] = st.get('ram_usage_pct', 0)
        d['total_memory_mb'] = st.get('total_memory_mb', 0)
        d['free_memory_mb'] = st.get('free_memory_mb', 0)
        d['api_connected'] = st.get('api_connected', False)
        d['api_error'] = st.get('api_error')
    return devices


def reload_freeradius_clients():
    """
    Applies NAS configuration changes to the FreeRADIUS daemon.
    In FreeRADIUS 3.0, client secrets and new NAS definitions require a daemon restart
    or supported signal to re-read the SQL nas table.
    Sends a restart request via Docker API socket and verifies HTTP response.
    Returns (success: bool, message: str).
    """
    import os
    import socket

    # Update nasreload timestamp table
    try:
        execute_write("""
            INSERT INTO nasreload (nasipaddress, reloadtime) 
            VALUES ('0.0.0.0', NOW()) 
            ON DUPLICATE KEY UPDATE reloadtime = NOW()
        """)
    except Exception:
        pass

    from services import hosted_control_service as hosted
    if not hosted.configured() and not os.path.exists('/var/run/docker.sock'):
        return True, 'تم حفظ بيانات أجهزة NAS في قاعدة البيانات بنجاح (بيئة غير حاوية).'
    # Reuse the same verified project/full-ID boundary as the service controls.
    # Never restart a global canonical name belonging to another installation.
    from services.system_control_service import execute_service_action
    return execute_service_action('freeradius', 'restart', admin_username='admin')


def sync_nas_table_entries():
    """
    Ensures SQL nas table strictly matches wisp_nas_devices, removing orphan entries.
    Each NAS device retains its own independent secret and IP configuration (Points 11, 12, 13).
    Does NOT force a single router's secret across other devices or subnets.
    """
    INTERNAL_CLIENT_IPS = ('127.0.0.1', '::1', '172.18.0.4', 'localhost')
    devices = query_all('SELECT * FROM wisp_nas_devices ORDER BY id DESC')
    if not devices:
        int_placeholders = ','.join(['?'] * len(INTERNAL_CLIENT_IPS))
        execute_write(f"DELETE FROM nas WHERE nasname NOT IN ({int_placeholders})", INTERNAL_CLIENT_IPS)
        return reload_freeradius_clients()

    valid_ips = [d['ip_address'].strip() for d in devices]
    # Delete orphan routers from nas without touching protected internal clients
    all_protected = list(valid_ips) + list(INTERNAL_CLIENT_IPS)
    placeholders = ','.join(['?'] * len(all_protected))
    execute_write(f"DELETE FROM nas WHERE nasname NOT IN ({placeholders})", tuple(all_protected))

    for d in devices:
        ip = d['ip_address'].strip()
        sec = d['secret'].strip()
        name = d['name'].strip()[:32]
        ntype = d.get('nas_type', 'mikrotik')
        
        exists = query_one('SELECT id FROM nas WHERE nasname = ?', (ip,))
        if exists:
            execute_write('UPDATE nas SET shortname = ?, type = ?, secret = ?, description = ? WHERE nasname = ?',
                          (name, ntype, sec, d.get('description', 'WISP Router'), ip))
        else:
            execute_write('INSERT INTO nas (nasname, shortname, type, secret, ports, description) VALUES (?, ?, ?, ?, 1812, ?)',
                          (ip, name, ntype, sec, d.get('description', 'WISP Router')))

    return reload_freeradius_clients()

def add_nas_device(data, admin_username='admin'):
    from services.license_guard_service import check_nas_quota
    allowed, err_msg, _, _ = check_nas_quota(1)
    if not allowed:
        raise ValueError(err_msg)

    nas_id = execute_write('''
        INSERT INTO wisp_nas_devices (
            name, ip_address, nas_type, secret, api_port, coa_port,
            api_username, api_password, hotspot_login_url, description
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        data['name'].strip(),
        data['ip_address'].strip(),
        data.get('nas_type', 'mikrotik'),
        data['secret'].strip(),
        int(data.get('api_port', 8728)),
        int(data.get('coa_port', 3799)),
        data.get('api_username', 'admin').strip(),
        data.get('api_password', '').strip(),
        data.get('hotspot_login_url', 'http://192.168.88.1/login').strip(),
        data.get('description', '')
    ))
    
    applied, msg = sync_nas_table_entries()
    log_audit(1, admin_username, 'ADD_NAS', 'nas', f'Added NAS router {data["name"]} ({data["ip_address"]})')
    return nas_id, applied, msg

def update_nas_device(nas_id, data, admin_username='admin'):
    old_nas = query_one('SELECT ip_address FROM wisp_nas_devices WHERE id = ?', (nas_id,))
    
    execute_write('''
        UPDATE wisp_nas_devices SET
            name = ?, ip_address = ?, nas_type = ?, secret = ?,
            api_port = ?, coa_port = ?, api_username = ?, api_password = ?,
            hotspot_login_url = ?, description = ?
        WHERE id = ?
    ''', (
        data['name'].strip(),
        data['ip_address'].strip(),
        data.get('nas_type', 'mikrotik'),
        data['secret'].strip(),
        int(data.get('api_port', 8728)),
        int(data.get('coa_port', 3799)),
        data.get('api_username', 'admin').strip(),
        data.get('api_password', '').strip(),
        data.get('hotspot_login_url', 'http://192.168.88.1/login').strip(),
        data.get('description', ''),
        nas_id
    ))
    
    if old_nas and old_nas['ip_address'] != data['ip_address'].strip():
        execute_write('DELETE FROM nas WHERE nasname = ?', (old_nas['ip_address'],))
        
    applied, msg = sync_nas_table_entries()
    log_audit(1, admin_username, 'UPDATE_NAS', 'nas', f'Updated NAS ID {nas_id}')
    return True, applied, msg

def delete_nas_device(nas_id, admin_username='admin'):
    nas = query_one('SELECT ip_address, name FROM wisp_nas_devices WHERE id = ?', (nas_id,))
    if nas:
        ip = nas.get('ip_address', '').strip()
        name = nas.get('name', '').strip()
        # A similarly named tunnel is not proof of an IP binding. Complete any
        # exact linked-tunnel removal before deleting this NAS's SQL credentials.
        from services.l2tp_service import delete_l2tp_tunnel
        tunnels = query_all('SELECT id FROM wisp_l2tp_tunnels WHERE tunnel_ip=?', (ip,))
        if len(tunnels) > 1:
            raise ValueError('ربط النفق غير فريد؛ لم تُحذف بيانات NAS.')
        if tunnels and not delete_l2tp_tunnel(tunnels[0]['id'], admin_username=admin_username):
            raise ValueError('تعذر حذف النفق المرتبط؛ لم تُحذف بيانات NAS.')
        if ip:
            execute_write('DELETE FROM nas WHERE nasname=?', (ip,))
        execute_write('DELETE FROM wisp_nas_devices WHERE id = ?', (nas_id,))
        applied, msg = sync_nas_table_entries()
        log_audit(1, admin_username, 'DELETE_NAS', 'nas', f'Deleted NAS {name or nas_id} ({ip}) and cleaned all associated radius client entries')
        return True, applied, msg
    return False, False, "الجهاز غير موجود"

def test_nas_coa(nas_id):
    nas = query_one('SELECT * FROM wisp_nas_devices WHERE id = ?', (nas_id,))
    if not nas:
        return {'success': False, 'message': 'جهاز NAS غير موجود.'}
        
    client = RadiusCoaClient(
        nas_ip=nas['ip_address'],
        secret=nas['secret'],
        port=nas.get('coa_port', 3799),
        timeout=2.0
    )
    res = client.disconnect_user(username='_radius_probe_test_user_', is_test_probe=True)
    
    # If CoA succeeded (ACK or NAK 503 Session Not Found)
    if res.get('success'):
        return res
        
    # If CoA failed/timed out, check if MikroTik API is reachable
    if nas.get('api_port') and nas.get('api_username'):
        try:
            from core.mikrotik_api import RouterOSApiProtocol, sync_mikrotik_router_clock
            ros = RouterOSApiProtocol(nas['ip_address'], port=int(nas['api_port']), timeout=2.0)
            ros.connect()
            ok = ros.login(nas['api_username'], nas.get('api_password') or '')
            ros.close()
            if ok:
                # Synchronize clock as well
                sync_mikrotik_router_clock(nas['ip_address'], nas['api_username'], nas.get('api_password') or '', port=int(nas['api_port']))
                return {
                    'success': True,
                    'status': 'api_verified',
                    'method': 'mikrotik_api',
                    'message': f'منفذ CoA لم يستجب (Timeout)، ولكن تم التحقق من اتصال RouterOS API بنجاح ومزامنة ساعة الراوتر عبر المنفذ {nas["api_port"]}.'
                }
        except Exception as e:
            pass
            
    return res


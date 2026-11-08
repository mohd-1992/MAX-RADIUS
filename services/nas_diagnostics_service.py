# -*- coding: utf-8 -*-
"""
services/nas_diagnostics_service.py
-----------------------------------
Diagnostics & Live Probing Service for NAS / MikroTik Routers.
"""

from services.nas_service import get_nas_devices, test_nas_coa
from database.db import query_all, query_one
from core.coa import RadiusCoaClient

def get_nas_diagnostics_overview(skip_live_probe=False):
    devices = get_nas_devices(skip_live_probe=skip_live_probe)
    total_nas = len(devices)
    online_nas = sum(1 for d in devices if d.get('is_online'))
    total_active_sessions = sum(d.get('active_users', 0) for d in devices)
    return {
        'devices': devices,
        'total_nas': total_nas,
        'online_nas': online_nas,
        'total_active_sessions': total_active_sessions
    }

def test_coa_port(ip, port=3799, secret=None):
    if not ip:
        return False, 'لم يتم تحديد عنوان IP صالح'
    try:
        port = int(port)
    except (TypeError, ValueError, OverflowError):
        return False, 'منفذ CoA غير صالح'
    if not 1 <= port <= 65535:
        return False, 'منفذ CoA غير صالح'
    
    # Try finding secret if not provided
    if not secret:
        nas = query_one('SELECT secret FROM wisp_nas_devices WHERE ip_address = ?', (ip,))
        if nas:
            secret = nas['secret']
        else:
            secret = 'testing123'
            
    client = RadiusCoaClient(
        nas_ip=ip,
        secret=secret,
        port=port,
        timeout=2.0
    )
    res = client.disconnect_user(username='_radius_probe_test_user_', is_test_probe=True)
    return res.get('success', False), res.get('message', 'تم إرسال طلب الفحص')


def send_test_coa_disconnect(ip, nas_secret=None, coa_port=3799):
    """Compatibility route: probe the reserved diagnostic identity only."""
    return test_coa_port(ip, port=coa_port, secret=nas_secret)

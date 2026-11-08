"""
services/radius_sim_service.py
------------------------------
Live RADIUS Protocol Lab & Simulator Service for MAX RADIUS.
Provides direct Access-Request and CoA packet testing, attribute validation, and timing analysis.
"""

import time
import subprocess
import re
from database.db import get_connection

_get_db = get_connection

from core.license_feature_policy import guarded as _license_guarded


@_license_guarded('radius_simulator')
def simulate_radius_auth(username, password, nas_ip='127.0.0.1', nas_secret='testing123', radius_server='127.0.0.1', radius_port=1812):
    """
    Simulates a live RADIUS Access-Request packet using FreeRADIUS radtest / radclient.
    Returns response type (Access-Accept / Access-Reject), returned AVPs, and response time.
    """
    start_time = time.time()
    
    db = _get_db()
    db_info = {}
    reply_attrs = []
    try:
        with db.cursor() as cur:
            cur.execute("""
                SELECT u.id, u.username, u.value as password, u.attribute,
                       (SELECT groupname FROM radusergroup WHERE username = u.username LIMIT 1) as groupname,
                       (SELECT status FROM wisp_subscribers WHERE username = u.username LIMIT 1) as sub_status,
                       (SELECT status FROM wisp_vouchers WHERE username = u.username LIMIT 1) as voucher_status
                FROM radcheck u
                WHERE u.username = %s
                LIMIT 1
            """, (username,))
            db_info = cur.fetchone() or {}
            
            groupname = db_info.get('groupname')
            if groupname:
                cur.execute("SELECT attribute, op, value FROM radgroupreply WHERE groupname = %s", (groupname,))
                reply_attrs = cur.fetchall()
    finally:
        db.close()

    raw_output = ""
    is_accept = False
    attributes = []
    verified = False
    
    try:
        from services import hosted_control_service as hosted
        from services.system_control_service import get_container_info
        if hosted.configured():
            result = hosted.radius_simulation(username, password, nas_secret,
                                               radius_server, radius_port)
            raw_output, exit_code = result['output'], result['exit_code']
        else:
            core = get_container_info(['max_radius_core'])
            ident = core.get('container_id', '')
            if not core.get('is_running') or not re.fullmatch(r'[a-f0-9]{64}', ident):
                raise RuntimeError('No unique running owned RADIUS container')
            port = int(radius_port)
            if not 1 <= port <= 65535:
                raise ValueError('Invalid RADIUS port')
            destination = radius_server if port == 1812 else f'{radius_server}:{port}'
            cmd = ['docker', 'exec', ident, 'radtest', username,
                   password or 'none', destination, '0', nas_secret]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=3.5)
            raw_output, exit_code = proc.stdout, proc.returncode
        elapsed_ms = round((time.time() - start_time) * 1000, 1)
        
        if exit_code == 0 and re.search(r'^\s*Received Access-Accept\b', raw_output, re.MULTILINE):
            is_accept = True
            verified = True
            status_code = "Access-Accept"
        elif re.search(r'^\s*Received Access-Reject\b', raw_output, re.MULTILINE):
            is_accept = False
            verified = True
            status_code = "Access-Reject"
        elif "No reply" in raw_output or "timeout" in raw_output.lower():
            is_accept = False
            status_code = "Timeout / No Reply"
        else:
            is_accept = False
            status_code = "Unknown Response"

        safe_lines = []
        for line in raw_output.splitlines():
            line = line.strip()
            if re.search(r'(?:User|Cleartext|CHAP|MS-CHAP)-Password\s*[=:]', line, re.I):
                safe_lines.append('[credential redacted]')
                continue
            safe_lines.append(line)
            if "=" in line and not line.startswith("Sent") and not line.startswith("rad_recv"):
                parts = line.split("=", 1)
                attributes.append({
                    'attr': parts[0].strip(),
                    'val': parts[1].strip()
                })
        raw_output = '\n'.join(safe_lines)

    except Exception:
        elapsed_ms = round((time.time() - start_time) * 1000, 1)
        is_accept = False
        status_code = 'Unavailable / Not Verified'
        raw_output = 'تعذر تنفيذ اختبار RADIUS موثق ضمن حدود هذه المنظومة؛ لم تثبت نتيجة مصادقة.'

    return {
        'username': username,
        'status': status_code,
        'is_accept': is_accept,
        'verified': verified,
        'elapsed_ms': elapsed_ms,
        'raw_output': raw_output or 'لم يصل رد RADIUS موثق.',
        'attributes': attributes if attributes else [{'attr': ra['attribute'], 'val': ra['value']} for ra in reply_attrs],
        'db_user_exists': bool(db_info),
        'assigned_group': db_info.get('groupname', 'N/A') if db_info else 'غير مسجل'
    }

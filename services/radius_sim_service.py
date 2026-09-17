"""
services/radius_sim_service.py
------------------------------
Live RADIUS Protocol Lab & Simulator Service for MAX RADIUS.
Provides direct Access-Request and CoA packet testing, attribute validation, and timing analysis.
"""

import time
import subprocess
from database.db import get_connection

_get_db = get_connection

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

    cmd = [
        "docker", "exec", "max_radius_core", 
        "radtest", username, password or "none", radius_server, "0", nas_secret
    ]
    
    raw_output = ""
    is_accept = False
    attributes = []
    
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=3.5)
        elapsed_ms = round((time.time() - start_time) * 1000, 1)
        raw_output = proc.stdout
        
        if "Received Access-Accept" in raw_output or "Access-Accept" in raw_output:
            is_accept = True
            status_code = "Access-Accept"
        elif "Received Access-Reject" in raw_output or "Access-Reject" in raw_output:
            is_accept = False
            status_code = "Access-Reject"
        elif "No reply" in raw_output or "timeout" in raw_output.lower():
            is_accept = False
            status_code = "Timeout / No Reply"
        else:
            is_accept = False
            status_code = "Unknown Response"

        for line in raw_output.splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("Sent") and not line.startswith("rad_recv"):
                parts = line.split("=", 1)
                attributes.append({
                    'attr': parts[0].strip(),
                    'val': parts[1].strip()
                })

    except Exception as e:
        elapsed_ms = round((time.time() - start_time) * 1000, 1)
        if db_info:
            is_match = (db_info.get('password') == password) or (not password and db_info.get('attribute') == 'Cleartext-Password')
            if is_match and db_info.get('voucher_status') in (None, 'active', 'unused', 'used'):
                is_accept = True
                status_code = "Access-Accept (Direct DB Check)"
                for ra in reply_attrs:
                    attributes.append({'attr': ra['attribute'], 'val': ra['value']})
            else:
                is_accept = False
                status_code = "Access-Reject (Invalid Password/Status)"
        else:
            is_accept = False
            status_code = f"Access-Reject (User Not Found: {str(e)})"

    return {
        'username': username,
        'status': status_code,
        'is_accept': is_accept,
        'elapsed_ms': elapsed_ms,
        'raw_output': raw_output if raw_output else "Direct Evaluation",
        'attributes': attributes if attributes else [{'attr': ra['attribute'], 'val': ra['value']} for ra in reply_attrs],
        'db_user_exists': bool(db_info),
        'assigned_group': db_info.get('groupname', 'N/A') if db_info else 'غير مسجل'
    }

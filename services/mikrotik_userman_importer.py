# -*- coding: utf-8 -*-
"""
services/mikrotik_userman_importer.py
-------------------------------------
Fully Isolated Importer Module for MikroTik User Manager v6.
Supports:
1. Offline RouterOS script (.rsc) parsing.
2. Online direct RouterOS API synchronization.
"""

import os
import re
import time
import datetime
from database.db import get_connection, is_mysql_conn, adapt_query, query_all, query_one, execute_write, log_audit
from core.mikrotik_api import RouterOSApiProtocol

# Helper: parse human-readable bandwidth (e.g. '1M', '512k', '10M/10M', '1048576') -> (rx_kbps, tx_kbps)
def parse_mikrotik_rate_limit(rate_str):
    if not rate_str or rate_str == '0':
        return 0, 0
    parts = str(rate_str).strip().split('/')
    rx_raw = parts[0].strip()
    tx_raw = parts[1].strip() if len(parts) > 1 else rx_raw
    
    def to_kbps(val):
        val = str(val).strip().upper()
        if not val:
            return 0
        try:
            if val.endswith('M') or val.endswith('MBPS'):
                num = float(re.sub(r'[^\d.]', '', val))
                return int(num * 1024)
            elif val.endswith('K') or val.endswith('KBPS'):
                num = float(re.sub(r'[^\d.]', '', val))
                return int(num)
            elif val.endswith('G') or val.endswith('GBPS'):
                num = float(re.sub(r'[^\d.]', '', val))
                return int(num * 1024 * 1024)
            else:
                num = float(val)
                # If large number in bps, convert to kbps
                if num > 100000:
                    return int(num / 1000)
                return int(num)
        except Exception:
            return 0
            
    rx_k = to_kbps(rx_raw)
    tx_k = to_kbps(tx_raw)
    return rx_k, tx_k

# Helper: parse validity (e.g. '1d', '30d', '1h', '30m', '4w') -> (value, unit)
def parse_mikrotik_validity(val_str):
    if not val_str or str(val_str).strip() in ('0', '0s', ''):
        return 30, 'days'
    s = str(val_str).strip().lower()
    
    # Check compound or single units
    match = re.match(r'^(\d+)\s*([a-z]+)?$', s)
    if match:
        num = int(match.group(1))
        unit = match.group(2) or 'd'
        if unit.startswith('d'):
            return num, 'days'
        elif unit.startswith('h'):
            return num, 'hours'
        elif unit.startswith('m') and not unit.startswith('mo'):
            return num, 'minutes'
        elif unit.startswith('w'):
            return num * 7, 'days'
        elif unit.startswith('mo'):
            return num, 'months'
    return 30, 'days'

# Helper: parse shared users / override-shared-users
def parse_shared_users(val):
    if not val:
        return 1
    s = str(val).strip().lower()
    if s in ('unlimited', 'off', 'none', 'auto', '0', ''):
        return 1
    try:
        return max(1, int(re.sub(r'[^\d]', '', s) or 1))
    except Exception:
        return 1

# Helper: parse bytes / quota to MB
def parse_mikrotik_bytes_to_mb(byte_val):
    if not byte_val:
        return 0
    try:
        s = str(byte_val).strip().upper()
        if any(s.endswith(u) for u in ('GIB', 'GB', 'G')):
            num = float(re.sub(r'[^\d.]', '', s))
            return int(num * 1024)
        elif any(s.endswith(u) for u in ('MIB', 'MB', 'M')):
            num = float(re.sub(r'[^\d.]', '', s))
            return int(num)
        elif any(s.endswith(u) for u in ('KIB', 'KB', 'K')):
            num = float(re.sub(r'[^\d.]', '', s))
            return max(1, int(num / 1024))
        elif any(s.endswith(u) for u in ('TIB', 'TB', 'T')):
            num = float(re.sub(r'[^\d.]', '', s))
            return int(num * 1024 * 1024)
        else:
            num = float(s)
            if num > 1024 * 1024:
                return int(num / (1024 * 1024))
            return int(num)
    except Exception:
        return 0

# Helper: parse uptime to minutes
def parse_mikrotik_uptime_to_mins(uptime_val):
    if not uptime_val or str(uptime_val).strip() in ('0', '0s', ''):
        return 0
    s = str(uptime_val).strip().lower()
    total_mins = 0
    
    # Pattern like '1w2d3h4m' or '2h30m' or '1d'
    for match in re.finditer(r'(\d+)\s*([a-z]+)', s):
        num = int(match.group(1))
        unit = match.group(2)
        if unit.startswith('w'):
            total_mins += num * 7 * 24 * 60
        elif unit.startswith('d'):
            total_mins += num * 24 * 60
        elif unit.startswith('h'):
            total_mins += num * 60
        elif unit.startswith('m'):
            total_mins += num
        elif unit.startswith('s'):
            total_mins += max(1, num // 60)
            
    if total_mins == 0:
        try:
            total_mins = int(s)
        except Exception:
            pass
    return total_mins


# -------------------------------------------------------------
# 1. RouterOS Script (.rsc) Parser
# -------------------------------------------------------------
def parse_rsc_content(raw_text):
    """
    Parses a MikroTik User Manager v6 .rsc script text into:
    - profiles: dict of profile_name -> profile_info
    - limitations: dict of limit_name -> limit_info
    - users: list of user_dict
    """
    lines = raw_text.splitlines()
    cleaned_lines = []
    
    # Merge multiline commands ending with backslash '\'
    curr_line = ""
    for line in lines:
        line_str = line.strip()
        if not line_str or line_str.startswith('#'):
            continue
        if line_str.endswith('\\'):
            curr_line += " " + line_str[:-1].strip()
        else:
            curr_line += " " + line_str
            cleaned_lines.append(curr_line.strip())
            curr_line = ""
    if curr_line:
        cleaned_lines.append(curr_line.strip())

    current_section = None
    profiles = {}
    limitations = {}
    profile_limits = []
    users = []

    def parse_attributes(cmd_str):
        attrs = {}
        # Match key=val or key="val" or val flags
        tokens = re.findall(r'(?:[^\s"]|"(?:\\.|[^"])*")+', cmd_str)
        for tok in tokens:
            if '=' in tok:
                k, v = tok.split('=', 1)
                k = k.strip().lower()
                v = v.strip()
                if v.startswith('"') and v.endswith('"'):
                    v = v[1:-1]
                attrs[k] = v
            else:
                # flag token
                t = tok.strip()
                if t.startswith('"') and t.endswith('"'):
                    t = t[1:-1]
                attrs[t] = True
        return attrs

    for line in cleaned_lines:
        if line.startswith('/tool user-manager profile limitation'):
            current_section = 'limitations'
            continue
        elif line.startswith('/tool user-manager profile profile-limitation'):
            current_section = 'profile_limits'
            continue
        elif line.startswith('/tool user-manager profile'):
            current_section = 'profiles'
            continue
        elif line.startswith('/tool user-manager user'):
            current_section = 'users'
            continue
        elif line.startswith('/'):
            current_section = None
            continue

        if not current_section:
            continue

        if line.startswith('add ') or line.startswith('set '):
            attrs = parse_attributes(line[4:])
            
            if current_section == 'profiles':
                p_name = attrs.get('name') or attrs.get('name-for-users')
                if p_name:
                    val_v, val_u = parse_mikrotik_validity(attrs.get('validity', '30d'))
                    profiles[p_name] = {
                        'name': p_name,
                        'name_for_users': attrs.get('name-for-users', p_name),
                        'price': float(attrs.get('price', 0.0) or 0.0),
                        'validity_value': val_v,
                        'validity_unit': val_u,
                        'starts_at': attrs.get('starts-at', 'logon'),
                        'override_shared_users': parse_shared_users(attrs.get('override-shared-users', 1))
                    }
                    
            elif current_section == 'limitations':
                l_name = attrs.get('name')
                if l_name:
                    rx_k, tx_k = parse_mikrotik_rate_limit(attrs.get('rate-limit-rx', '') or attrs.get('rate-limit', ''))
                    if not rx_k and attrs.get('rate-limit-rx'):
                        rx_k, _ = parse_mikrotik_rate_limit(attrs.get('rate-limit-rx'))
                    if not tx_k and attrs.get('rate-limit-tx'):
                        _, tx_k = parse_mikrotik_rate_limit(attrs.get('rate-limit-tx'))
                        
                    download_mb = parse_mikrotik_bytes_to_mb(attrs.get('download-limit') or attrs.get('transfer-limit') or 0)
                    upload_mb = parse_mikrotik_bytes_to_mb(attrs.get('upload-limit') or 0)
                    uptime_mins = parse_mikrotik_uptime_to_mins(attrs.get('uptime-limit') or 0)
                    
                    limitations[l_name] = {
                        'name': l_name,
                        'rate_down': f"{rx_k}k" if rx_k else "10M",
                        'rate_up': f"{tx_k}k" if tx_k else "5M",
                        'quota_mb': download_mb,
                        'uptime_mins': uptime_mins
                    }
                    
            elif current_section == 'profile_limits':
                p_ref = attrs.get('profile')
                l_ref = attrs.get('limitation')
                if p_ref and l_ref:
                    profile_limits.append({'profile': p_ref, 'limitation': l_ref})
                    
            elif current_section == 'users':
                username = attrs.get('username')
                if username:
                    users.append({
                        'username': username,
                        'password': attrs.get('password', username),
                        'actual_profile': attrs.get('actual-profile') or attrs.get('profile') or '',
                        'caller_id': attrs.get('caller-id', ''),
                        'comment': attrs.get('comment', ''),
                        'email': attrs.get('email', ''),
                        'phone': attrs.get('phone', ''),
                        'shared_users': parse_shared_users(attrs.get('shared-users', 1)),
                        'disabled': str(attrs.get('disabled', 'no')).lower() in ('yes', 'true', '1')
                    })

    # Combine profiles with their limitations
    for pl in profile_limits:
        p_name = pl['profile']
        l_name = pl['limitation']
        if p_name in profiles and l_name in limitations:
            lim = limitations[l_name]
            profiles[p_name]['rate_down'] = lim['rate_down']
            profiles[p_name]['rate_up'] = lim['rate_up']
            profiles[p_name]['quota_mb'] = lim['quota_mb']
            profiles[p_name]['uptime_mins'] = lim['uptime_mins']

    # Default fallback for profiles without explicit limitations
    for p_name, p in profiles.items():
        if 'rate_down' not in p:
            p['rate_down'] = '10M'
        if 'rate_up' not in p:
            p['rate_up'] = '5M'
        if 'quota_mb' not in p:
            p['quota_mb'] = 0
        if 'uptime_mins' not in p:
            p['uptime_mins'] = 0

    return {
        'profiles': list(profiles.values()),
        'users': users,
        'total_profiles': len(profiles),
        'total_users': len(users),
        'active_users': sum(1 for u in users if not u['disabled']),
        'disabled_users': sum(1 for u in users if u['disabled'])
    }


# -------------------------------------------------------------
# 2. RouterOS Live API Importer
# -------------------------------------------------------------
def fetch_userman_via_api(host, username, password, port=8728, use_ssl=False, timeout=5.0):
    """
    Connects to live MikroTik router and pulls User Manager v6 profiles and users.
    """
    client = RouterOSApiProtocol(host=host, port=port, use_ssl=use_ssl, timeout=timeout)
    client.connect()
    logged_in = client.login(username=username, password=password)
    if not logged_in:
        client.close()
        raise ValueError("فشل تسجيل الدخول إلى راوتر الميكروتك. يرجى التحقق من اسم المستخدم وكلمة المرور.")

    try:
        # Fetch Profiles
        raw_profiles = client.execute_command('/tool/user-manager/profile/print')
        # Fetch Limitations
        raw_limitations = client.execute_command('/tool/user-manager/profile/limitation/print')
        # Fetch Profile-Limitations
        raw_profile_limits = client.execute_command('/tool/user-manager/profile/profile-limitation/print')
        # Fetch Users
        raw_users = client.execute_command('/tool/user-manager/user/print')
    finally:
        client.close()

    # Build limitations map
    limit_map = {}
    for lim in raw_limitations:
        l_name = lim.get('name')
        if l_name:
            rx_k, tx_k = parse_mikrotik_rate_limit(lim.get('rate-limit-rx') or lim.get('rate-limit') or '')
            if not rx_k and lim.get('rate-limit-rx'):
                rx_k, _ = parse_mikrotik_rate_limit(lim.get('rate-limit-rx'))
            if not tx_k and lim.get('rate-limit-tx'):
                _, tx_k = parse_mikrotik_rate_limit(lim.get('rate-limit-tx'))
            limit_map[l_name] = {
                'rate_down': f"{rx_k}k" if rx_k else "10M",
                'rate_up': f"{tx_k}k" if tx_k else "5M",
                'quota_mb': parse_mikrotik_bytes_to_mb(lim.get('download-limit') or lim.get('transfer-limit') or 0),
                'uptime_mins': parse_mikrotik_uptime_to_mins(lim.get('uptime-limit') or 0)
            }

    # Map profile to limits
    prof_limit_ref = {}
    for pl in raw_profile_limits:
        p_name = pl.get('profile')
        l_name = pl.get('limitation')
        if p_name and l_name:
            prof_limit_ref[p_name] = l_name

    profiles_list = []
    for p in raw_profiles:
        p_name = p.get('name') or p.get('name-for-users')
        if not p_name:
            continue
        val_v, val_u = parse_mikrotik_validity(p.get('validity', '30d'))
        l_name = prof_limit_ref.get(p_name)
        lim_info = limit_map.get(l_name, {})

        profiles_list.append({
            'name': p_name,
            'name_for_users': p.get('name-for-users', p_name),
            'price': float(p.get('price', 0.0) or 0.0),
            'validity_value': val_v,
            'validity_unit': val_u,
            'rate_down': lim_info.get('rate_down', '10M'),
            'rate_up': lim_info.get('rate_up', '5M'),
            'quota_mb': lim_info.get('quota_mb', 0),
            'uptime_mins': lim_info.get('uptime_mins', 0),
            'override_shared_users': int(p.get('override-shared-users', 1) or 1)
        })

    users_list = []
    for u in raw_users:
        uname = u.get('username')
        if not uname:
            continue
        users_list.append({
            'username': uname,
            'password': u.get('password', uname),
            'actual_profile': u.get('actual-profile') or u.get('profile') or '',
            'caller_id': u.get('caller-id', ''),
            'comment': u.get('comment', ''),
            'email': u.get('email', ''),
            'phone': u.get('phone', ''),
            'shared_users': int(u.get('shared-users', 1) or 1),
            'disabled': str(u.get('disabled', 'false')).lower() in ('true', 'yes', '1')
        })

    return {
        'profiles': profiles_list,
        'users': users_list,
        'total_profiles': len(profiles_list),
        'total_users': len(users_list),
        'active_users': sum(1 for u in users_list if not u['disabled']),
        'disabled_users': sum(1 for u in users_list if u['disabled'])
    }


# -------------------------------------------------------------
# 3. Execution Engine: Insert into MAX RADIUS
# -------------------------------------------------------------
def execute_userman_import(parsed_data, target_type='subscribers', fallback_package=None, duplicate_action='skip', admin_user='admin'):
    """
    Executes the isolated database insertion:
    - Creates/matches packages in wisp_packages
    - Inserts users into wisp_subscribers or wisp_vouchers
    - Creates FreeRADIUS radcheck, radreply, radusergroup entries
    """
    start_t = time.time()
    db = get_connection()
    cur = db.cursor()
    
    profiles = parsed_data.get('profiles', [])
    users = parsed_data.get('users', [])
    
    stats = {
        'created_packages': 0,
        'created_users': 0,
        'updated_users': 0,
        'skipped_users': 0,
        'errors': []
    }

    try:
        if is_mysql_conn(db):
            cur.execute("SET foreign_key_checks = 0;")

        # 1. Sync / Create Packages
        package_map = {}  # profile_name -> package_id
        
        # Load existing packages
        cur.execute("SELECT id, name FROM wisp_packages")
        for row in cur.fetchall():
            if isinstance(row, dict):
                package_map[row['name'].strip()] = row['id']
            else:
                package_map[row[1].strip()] = row[0]

        for prof in profiles:
            p_name = prof['name'].strip()
            if p_name not in package_map:
                # Insert new package
                cur.execute("""
                    INSERT INTO wisp_packages (
                        name, price, cost, validity_value, validity_unit,
                        volume_quota_mb, uptime_limit_mins, rate_download, rate_upload,
                        simultaneous_sessions, is_active, created_at
                    ) VALUES (?, ?, 0.00, ?, ?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
                """.replace('?', '%s' if is_mysql_conn(db) else '?'), (
                    p_name,
                    prof.get('price', 0.0),
                    prof.get('validity_value', 30),
                    prof.get('validity_unit', 'days'),
                    prof.get('quota_mb', 0),
                    prof.get('uptime_mins', 0),
                    prof.get('rate_down', '10M'),
                    prof.get('rate_up', '5M'),
                    prof.get('override_shared_users', 1)
                ))
                pkg_id = cur.lastrowid
                package_map[p_name] = pkg_id
                stats['created_packages'] += 1

        # Fallback default package
        default_pkg_id = None
        if fallback_package and fallback_package.strip() in package_map:
            default_pkg_id = package_map[fallback_package.strip()]
        elif package_map:
            default_pkg_id = next(iter(package_map.values()))
        else:
            cur.execute("SELECT id FROM wisp_packages LIMIT 1")
            row = cur.fetchone()
            if row:
                default_pkg_id = row['id'] if isinstance(row, dict) else row[0]

        # 2. Insert / Update Users
        for u in users:
            uname = u['username'].strip()
            upass = u.get('password', uname).strip() or uname
            uprofile = u.get('actual_profile', '').strip()
            pkg_id = package_map.get(uprofile, default_pkg_id)
            mac = u.get('caller_id', '').strip() or None
            comment = u.get('comment', '').strip()
            email = u.get('email', '').strip()
            phone = u.get('phone', '').strip()
            simul = u.get('shared_users', 1)
            status = 'inactive' if u.get('disabled') else 'active'

            # Check if user already exists
            cur.execute("SELECT id FROM radcheck WHERE username = ? AND attribute = 'Cleartext-Password'".replace('?', '%s' if is_mysql_conn(db) else '?'), (uname,))
            existing = cur.fetchone()

            if existing:
                if duplicate_action == 'skip':
                    stats['skipped_users'] += 1
                    continue
                elif duplicate_action == 'overwrite':
                    # Update password in radcheck
                    cur.execute("UPDATE radcheck SET value = ? WHERE username = ? AND attribute = 'Cleartext-Password'".replace('?', '%s' if is_mysql_conn(db) else '?'), (upass, uname))
                    stats['updated_users'] += 1
            else:
                # A. FreeRADIUS radcheck (Cleartext-Password)
                cur.execute("INSERT INTO radcheck (username, attribute, op, value) VALUES (?, 'Cleartext-Password', ':=', ?)".replace('?', '%s' if is_mysql_conn(db) else '?'), (uname, upass))
                
                # B. FreeRADIUS radcheck (Calling-Station-Id if MAC exists)
                if mac:
                    cur.execute("INSERT INTO radcheck (username, attribute, op, value) VALUES (?, 'Calling-Station-Id', '==', ?)".replace('?', '%s' if is_mysql_conn(db) else '?'), (uname, mac))

                # C. FreeRADIUS radusergroup
                if uprofile:
                    cur.execute("INSERT INTO radusergroup (username, groupname, priority) VALUES (?, ?, 1)".replace('?', '%s' if is_mysql_conn(db) else '?'), (uname, uprofile))

                # D. MAX RADIUS Application Entity
                if target_type == 'vouchers':
                    # Ensure an import batch exists
                    cur.execute("SELECT id FROM wisp_voucher_batches WHERE name = 'MikroTik_UserMan_Import' LIMIT 1")
                    b_row = cur.fetchone()
                    if b_row:
                        batch_id = b_row['id'] if isinstance(b_row, dict) else b_row[0]
                    else:
                        cur.execute("""
                            INSERT INTO wisp_voucher_batches (name, package_id, total_cards, created_at)
                            VALUES ('MikroTik_UserMan_Import', ?, 0, CURRENT_TIMESTAMP)
                        """.replace('?', '%s' if is_mysql_conn(db) else '?'), (pkg_id,))
                        batch_id = cur.lastrowid

                    cur.execute("""
                        INSERT INTO wisp_vouchers (
                            batch_id, package_id, serial_number, username, password, pin_code, status,
                            bound_mac, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """.replace('?', '%s' if is_mysql_conn(db) else '?'), (
                        batch_id, pkg_id, uname, uname, upass, upass, 'unused' if status == 'active' else 'disabled', mac
                    ))
                else:
                    full_name = comment or uname
                    cur.execute("""
                        INSERT INTO wisp_subscribers (
                            username, password, full_name, package_id, status,
                            mac_binding, email, phone, notes, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """.replace('?', '%s' if is_mysql_conn(db) else '?'), (
                        uname, upass, full_name, pkg_id, status, mac, email, phone, comment
                    ))

                stats['created_users'] += 1


        if is_mysql_conn(db):
            cur.execute("SET foreign_key_checks = 1;")
            
        db.commit()
        elapsed = round(time.time() - start_t, 2)
        log_audit(1, admin_user or 'admin', 'USERMAN_IMPORT', 'tools', f"Imported {stats['created_users']} users and {stats['created_packages']} packages from MikroTik User Manager in {elapsed}s.")
        
        return {
            'success': True,
            'duration_seconds': elapsed,
            'stats': stats,
            'message': f"تم بنجاح استيراد {stats['created_users']:,} مستخدماً و {stats['created_packages']} باقة خلال {elapsed} ثانية."
        }
    except Exception as e:
        db.rollback()
        if is_mysql_conn(db):
            try:
                cur.execute("SET foreign_key_checks = 1;")
                db.commit()
            except Exception:
                pass
        return {
            'success': False,
            'error': str(e),
            'message': f"فشلت عملية الاستيراد: {str(e)}"
        }
    finally:
        cur.close()
        db.close()

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
from services.import_state_service import (present_value, import_now, imported_state,
    historical_accounting_values, imported_radius_checks, ensure_import_package_radius)

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
    if val_str is None or str(val_str).strip() == '':
        return 30, 'days'
    if str(val_str).strip() in ('0', '0s'):
        return 0, 'days'
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
    s_raw = str(byte_val).strip().lower()
    if s_raw in ('unlimited', 'off', 'none', 'auto', '0', '0s', ''):
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
            num = float(re.sub(r'[^\d.]', '', s) or 0)
            if num > 1024 * 1024:
                return int(num / (1024 * 1024))
            return int(num)
    except Exception:
        return 0

# Helper: extract quota in MB from profile/limitation name (e.g. '100GB', '350M', '3GB', '1.5GB', '4 قيقا')
def extract_quota_from_name(name_str):
    if not name_str:
        return 0
    s = str(name_str).strip()
    
    # Check for GB patterns: e.g. "100GB", "100 GB", "1.5GB", "100 قيقا", "100G" (not followed by bps)
    gb_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:GB|GIB|قيقا|جيجا|G(?![A-Za-z]))', s, re.IGNORECASE)
    if gb_match:
        try:
            val = float(gb_match.group(1))
            return int(val * 1024)
        except Exception:
            pass

    # Check for MB patterns: e.g. "350M", "350MB", "350 ميجا", "350 ميقا"
    mb_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:MB|MIB|ميقا|ميجا|M(?![A-Za-z]))', s, re.IGNORECASE)
    if mb_match:
        try:
            val = float(mb_match.group(1))
            return int(val)
        except Exception:
            pass

    return 0

# Helper: parse uptime to minutes
def parse_mikrotik_uptime_to_mins(uptime_val):
    if not uptime_val:
        return 0
    s_raw = str(uptime_val).strip().lower()
    if s_raw in ('unlimited', 'off', 'none', 'auto', '0', '0s', ''):
        return 0
    s = s_raw
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
            num_clean = re.sub(r'[^\d]', '', s)
            total_mins = int(num_clean) if num_clean else 0
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
                        
                    def _clean_limit(val):
                        if not val or str(val).strip().lower() in ('0', '0s', 'none', 'unlimited', 'off', ''):
                            return None
                        return val

                    quota_raw = (
                        _clean_limit(attrs.get('transfer-limit')) or
                        _clean_limit(attrs.get('total-limit')) or
                        _clean_limit(attrs.get('limit-bytes-total')) or
                        _clean_limit(attrs.get('download-limit')) or
                        _clean_limit(attrs.get('limit-bytes-in')) or
                        _clean_limit(attrs.get('upload-limit')) or
                        _clean_limit(attrs.get('limit-bytes-out'))
                    )
                    download_mb = parse_mikrotik_bytes_to_mb(quota_raw)
                    if quota_raw is None and not any(k in attrs for k in ('transfer-limit','total-limit','limit-bytes-total','download-limit','limit-bytes-in','upload-limit','limit-bytes-out')):
                        download_mb = extract_quota_from_name(l_name)
                    uptime_raw = _clean_limit(attrs.get('uptime-limit'))
                    uptime_mins = parse_mikrotik_uptime_to_mins(uptime_raw or 0) if uptime_raw else 0
                    
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
                    act_prof = (attrs.get('actual-profile') or attrs.get('profile') or '').strip()
                    uptime_used = parse_mikrotik_uptime_to_mins(attrs.get('uptime-used') or 0)
                    down_used = int(re.sub(r'[^\d]', '', str(attrs.get('download-used') or 0)) or 0)
                    up_used = int(re.sub(r'[^\d]', '', str(attrs.get('upload-used') or 0)) or 0)
                    has_prof = bool(act_prof and act_prof.lower() != 'none')
                    users.append({
                        'username': username,
                        'password': attrs.get('password', username),
                        'actual_profile': act_prof,
                        'has_profile': has_prof,
                        'caller_id': attrs.get('caller-id', ''),
                        'comment': attrs.get('comment', ''),
                        'email': attrs.get('email', ''),
                        'phone': attrs.get('phone', ''),
                        'shared_users': parse_shared_users(attrs.get('shared-users', 1)),
                        'disabled': str(attrs.get('disabled', 'no')).lower() in ('yes', 'true', '1'),
                        'uptime_used_mins': uptime_used,
                        'download_used_bytes': down_used,
                        'upload_used_bytes': up_used,
                        'till_time': attrs.get('till-time', '')
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
            p['quota_mb'] = extract_quota_from_name(p.get('name')) or extract_quota_from_name(p.get('name_for_users'))
        if 'uptime_mins' not in p:
            p['uptime_mins'] = 0

    active_cnt = sum(1 for u in users if not u['disabled'] and u.get('has_profile', True))
    expired_cnt = sum(1 for u in users if not u['disabled'] and not u.get('has_profile', True))

    return {
        'profiles': list(profiles.values()),
        'users': users,
        'total_profiles': len(profiles),
        'total_users': len(users),
        'active_users': active_cnt,
        'disabled_users': sum(1 for u in users if u['disabled']),
        'expired_users': expired_cnt
    }


# -------------------------------------------------------------
# 2. RouterOS Live API Importer
# -------------------------------------------------------------
class BufferedRouterOSApiProtocol(RouterOSApiProtocol):
    """
    High-performance buffered RouterOS API protocol client.
    Reads data in 64KB stream buffers instead of 1-byte recv syscalls,
    drastically reducing CPU and memory overhead during large User Manager fetches.
    """
    def connect(self):
        super().connect()
        self._buf = bytearray()
        self._pos = 0

    def _read_raw(self, n):
        res = bytearray()
        while len(res) < n:
            avail = len(self._buf) - self._pos
            if avail <= 0:
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                self._buf = chunk
                self._pos = 0
                avail = len(self._buf)
            take = min(n - len(res), avail)
            res.extend(self._buf[self._pos : self._pos + take])
            self._pos += take
        return res

    def _read_len(self):
        b1 = self._read_raw(1)
        if not b1:
            return 0
        v = b1[0]
        if (v & 0x80) == 0:
            return v
        elif (v & 0xC0) == 0x80:
            return ((v & 0x3F) << 8) + self._read_raw(1)[0]
        elif (v & 0xE0) == 0xC0:
            b = self._read_raw(2)
            return ((v & 0x1F) << 16) + (b[0] << 8) + b[1]
        elif (v & 0xF0) == 0xE0:
            b = self._read_raw(3)
            return ((v & 0x0F) << 24) + (b[0] << 16) + (b[1] << 8) + b[2]
        elif (v & 0xF8) == 0xF0:
            import struct
            return struct.unpack('!I', self._read_raw(4))[0]
        return 0

    def read_sentence(self):
        res = []
        while True:
            l = self._read_len()
            if l == 0:
                break
            res.append(self._read_raw(l).decode('utf-8', errors='replace'))
        return res


def fetch_userman_via_api(host, username, password, port=8728, use_ssl=False, timeout=90.0):
    """
    Connects to live MikroTik router and pulls User Manager v6 profiles and users
    using optimized property lists and buffered socket to prevent router crashes.
    """
    client = BufferedRouterOSApiProtocol(host=host, port=port, use_ssl=use_ssl, timeout=timeout)
    client.connect()
    logged_in = client.login(username=username, password=password)
    if not logged_in:
        client.close()
        raise ValueError("فشل تسجيل الدخول إلى راوتر الميكروتك. يرجى التحقق من اسم المستخدم وكلمة المرور.")

    try:
        # 1. Fetch Profiles
        raw_profiles = client.execute_command('/tool/user-manager/profile/print')
        # 2. Fetch Limitations
        raw_limitations = client.execute_command('/tool/user-manager/profile/limitation/print')
        # 3. Fetch Profile-Limitations
        raw_profile_limits = client.execute_command('/tool/user-manager/profile/profile-limitation/print')
        
        # 4. Fetch Users with .proplist to prevent router freeze and out-of-memory
        props = '.id,username,password,actual-profile,caller-id,comment,email,phone,shared-users,disabled,uptime-used,download-used,upload-used,till-time'
        raw_users = client.execute_command('/tool/user-manager/user/print', words=[f'=.proplist={props}'])
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
            
            def _clean_limit(val):
                if not val or str(val).strip().lower() in ('0', '0s', 'none', 'unlimited', 'off', ''):
                    return None
                return val

            quota_raw = (
                _clean_limit(lim.get('transfer-limit')) or
                _clean_limit(lim.get('total-limit')) or
                _clean_limit(lim.get('limit-bytes-total')) or
                _clean_limit(lim.get('download-limit')) or
                _clean_limit(lim.get('limit-bytes-in')) or
                _clean_limit(lim.get('upload-limit')) or
                _clean_limit(lim.get('limit-bytes-out'))
            )
            quota_mb = parse_mikrotik_bytes_to_mb(quota_raw)
            if quota_raw is None and not any(k in lim for k in ('transfer-limit','total-limit','limit-bytes-total','download-limit','limit-bytes-in','upload-limit','limit-bytes-out')):
                quota_mb = extract_quota_from_name(l_name)

            uptime_raw = _clean_limit(lim.get('uptime-limit'))
            uptime_mins = parse_mikrotik_uptime_to_mins(uptime_raw or 0) if uptime_raw else 0

            limit_map[l_name] = {
                'rate_down': f"{rx_k}k" if rx_k else "10M",
                'rate_up': f"{tx_k}k" if tx_k else "5M",
                'quota_mb': quota_mb,
                'uptime_mins': uptime_mins
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

        p_quota = present_value(lim_info, 'quota_mb', extract_quota_from_name(p_name) or extract_quota_from_name(p.get('name-for-users', '')))

        profiles_list.append({
            'name': p_name,
            'name_for_users': p.get('name-for-users', p_name),
            'price': float(re.sub(r'[^\d.]', '', str(p.get('price', 0.0) or 0)) or 0.0),
            'validity_value': val_v,
            'validity_unit': val_u,
            'rate_down': lim_info.get('rate_down', '10M'),
            'rate_up': lim_info.get('rate_up', '5M'),
            'quota_mb': p_quota,
            'uptime_mins': lim_info.get('uptime_mins', 0),
            'override_shared_users': parse_shared_users(p.get('override-shared-users', 1))
        })

    users_list = []
    for u in raw_users:
        uname = u.get('username')
        if not uname:
            continue
        act_prof = (u.get('actual-profile') or u.get('profile') or '').strip()
        uptime_used = parse_mikrotik_uptime_to_mins(u.get('uptime-used') or 0)
        down_used = int(re.sub(r'[^\d]', '', str(u.get('download-used') or 0)) or 0)
        up_used = int(re.sub(r'[^\d]', '', str(u.get('upload-used') or 0)) or 0)
        has_prof = bool(act_prof and act_prof.lower() != 'none')
        users_list.append({
            'username': uname,
            'password': u.get('password', uname),
            'actual_profile': act_prof,
            'has_profile': has_prof,
            'caller_id': u.get('caller-id', ''),
            'comment': u.get('comment', ''),
            'email': u.get('email', ''),
            'phone': u.get('phone', ''),
            'shared_users': parse_shared_users(u.get('shared-users', 1)),
            'disabled': str(u.get('disabled', 'false')).lower() in ('true', 'yes', '1'),
            'uptime_used_mins': uptime_used,
            'download_used_bytes': down_used,
            'upload_used_bytes': up_used,
            'till_time': u.get('till-time', '')
        })

    active_cnt = sum(1 for u in users_list if not u['disabled'] and u.get('has_profile', True))
    expired_cnt = sum(1 for u in users_list if not u.get('has_profile', True))

    return {
        'profiles': profiles_list,
        'users': users_list,
        'total_profiles': len(profiles_list),
        'total_users': len(users_list),
        'active_users': active_cnt,
        'disabled_users': sum(1 for u in users_list if u['disabled']),
        'expired_users': expired_cnt
    }


# -------------------------------------------------------------
# 3. Execution Engine: Insert into MAX RADIUS
# -------------------------------------------------------------
def execute_userman_import(parsed_data, target_type='vouchers', fallback_package=None, duplicate_action='skip', ignore_expired=True, import_consumption=False, admin_user='admin', profile_costs=None, chunk_index=None, total_chunks=None, state_cache=None):
    """
    Executes the isolated database insertion:
    - Creates/matches packages in wisp_packages
    - Creates dedicated batches for each package in wisp_voucher_batches
    - Supports client-side chunking/batching to prevent Cloudflare 100s timeouts
    - Filters expired / no-profile cards if ignore_expired is True
    - Imports past bandwidth and uptime consumption to radacct if import_consumption is True
    - Inserts users into wisp_subscribers or wisp_vouchers
    - Creates FreeRADIUS radcheck, radreply, radusergroup entries
    """
    start_t = time.time()
    db = get_connection()
    cur = db.cursor()
    
    profiles = parsed_data.get('profiles', [])
    users = parsed_data.get('users', [])
    if profile_costs is None:
        profile_costs = {}
    package_costs_map = {}

    is_chunked = (chunk_index is not None and total_chunks is not None)
    is_first_chunk = (not is_chunked) or (chunk_index == 0)
    is_last_chunk = (not is_chunked) or (chunk_index >= total_chunks - 1)
    
    if is_chunked and not is_first_chunk and state_cache and 'stats' in state_cache:
        stats = state_cache['stats']
    else:
        stats = {
            'created_packages': 0,
            'created_batches': 0,
            'created_users': 0,
            'updated_users': 0,
            'skipped_users': 0,
            'ignored_expired_users': 0,
            'imported_consumption_users': 0,
            'batch_details': [],
            'errors': []
        }

    try:
        if is_mysql_conn(db):
            cur.execute("SET foreign_key_checks = 0;")

        # 1. Sync / Create Packages
        package_map = {}  # profile_name -> package_id
        package_names = {} # package_id -> profile_name
        batch_map = {} # package_id -> batch_id
        default_pkg_id = None
        profiles_dict = {}

        if is_first_chunk:
            # 1. Sync / Create Packages
            cur.execute("SELECT id, name FROM wisp_packages")
            for row in cur.fetchall():
                if isinstance(row, dict):
                    package_map[row['name'].strip()] = row['id']
                    package_names[row['id']] = row['name'].strip()
                else:
                    package_map[row[1].strip()] = row[0]
                    package_names[row[0]] = row[1].strip()

            for prof in profiles:
                p_name = prof['name'].strip()
                p_quota = int(present_value(prof, 'quota_mb', extract_quota_from_name(p_name) or 0))
                p_uptime = int(prof.get('uptime_mins', 0) or 0)
                p_down = prof.get('rate_down', '10M')
                p_up = prof.get('rate_up', '5M')
                p_price = float(prof.get('price', 0.0) or 0.0)
                p_cost = float(profile_costs.get(p_name, p_price))
                package_costs_map[p_name] = p_cost
                p_val = int(present_value(prof, 'validity_value', 30))
                p_unit = prof.get('validity_unit', 'days') or 'days'
                p_simul = int(prof.get('override_shared_users', 1) or 1)

                if p_name not in package_map:
                    # Insert new package
                    cur.execute("""
                        INSERT INTO wisp_packages (
                            name, price, cost, validity_value, validity_unit,
                            volume_quota_mb, uptime_limit_mins, rate_download, rate_upload,
                            simultaneous_sessions, is_active, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
                    """.replace('?', '%s' if is_mysql_conn(db) else '?'), (
                        p_name, p_price, p_cost, p_val, p_unit, p_quota, p_uptime, p_down, p_up, p_simul
                    ))
                    pkg_id = cur.lastrowid
                    package_map[p_name] = pkg_id
                    package_names[pkg_id] = p_name
                    stats['created_packages'] += 1
                else:
                    pkg_id = package_map[p_name]
                    if p_quota > 0:
                        cur.execute("UPDATE wisp_packages SET volume_quota_mb = ? WHERE id = ? AND volume_quota_mb IS NULL".replace('?', '%s' if is_mysql_conn(db) else '?'), (p_quota, pkg_id))

            # Fallback default package
            if fallback_package and fallback_package.strip() in package_map:
                default_pkg_id = package_map[fallback_package.strip()]
            elif package_map:
                default_pkg_id = next(iter(package_map.values()))
            else:
                cur.execute("SELECT id FROM wisp_packages LIMIT 1")
                row = cur.fetchone()
                if row:
                    default_pkg_id = row['id'] if isinstance(row, dict) else row[0]

            # 2. Setup Batches Mapping for Vouchers
            profiles_dict = {p['name'].strip(): p for p in profiles}

            if target_type == 'vouchers':
                for pkg_id, pkg_name_actual in package_names.items():
                    cur.execute("SELECT id FROM wisp_voucher_batches WHERE package_id = ? LIMIT 1".replace('?', '%s' if is_mysql_conn(db) else '?'), (pkg_id,))
                    b_row = cur.fetchone()
                    if b_row:
                        batch_map[pkg_id] = b_row['id'] if isinstance(b_row, dict) else b_row[0]
                    else:
                        clean_code = re.sub(r'[^A-Za-z0-9]', '', pkg_name_actual)[:6].upper() or 'PKG'
                        batch_num = f"UM-{clean_code}-{datetime.datetime.now().strftime('%m%d%H%M')}"
                        cur.execute("INSERT INTO wisp_voucher_batches (batch_number, name, package_id, card_count, created_at) VALUES (?, ?, ?, 0, CURRENT_TIMESTAMP)".replace('?', '%s' if is_mysql_conn(db) else '?'), (batch_num, f"استيراد يوزرمانجر - {pkg_name_actual}", pkg_id))
                        batch_map[pkg_id] = cur.lastrowid
                        stats['created_batches'] += 1
        else:
            # Hydrate from state_cache
            state_cache = state_cache or {}
            package_map = state_cache.get('package_map', {})
            package_names = { (int(k) if str(k).isdigit() else k): v for k, v in state_cache.get('package_names', {}).items() }
            batch_map = { (int(k) if str(k).isdigit() else k): v for k, v in state_cache.get('batch_map', {}).items() }
            default_pkg_id = state_cache.get('default_pkg_id')
            package_costs_map = state_cache.get('package_costs_map', {})
            profiles_dict = state_cache.get('profiles_dict', {p['name'].strip(): p for p in profiles})

        now_dt = import_now(cur)
        cur.execute('SELECT * FROM wisp_packages')
        plans_by_id = {row['id']: row for row in cur.fetchall()}
        used_package_ids = {package_map.get(u.get('actual_profile', '').strip(), default_pkg_id) for u in users}
        ensure_import_package_radius(db, [pid for pid in used_package_ids if pid is not None])
        now_str = now_dt.strftime('%Y-%m-%d %H:%M:%S')

        # Include model rows even when expired credentials were removed.
        chunk_unames = list(set(u.get('username', '').strip() for u in users if u.get('username')))
        existing_radcheck, existing_targets = set(), set()
        target_table = 'wisp_vouchers' if target_type == 'vouchers' else 'wisp_subscribers'
        for i_sub in range(0, len(chunk_unames), 500):
            names = chunk_unames[i_sub:i_sub + 500]
            marks = ','.join(['%s' if is_mysql_conn(db) else '?'] * len(names))
            for table in ('radcheck', 'wisp_vouchers', 'wisp_subscribers'):
                cur.execute(f'SELECT username FROM {table} WHERE username IN ({marks})', names)
                found = {row['username'].strip().lower() for row in cur.fetchall()}
                existing_radcheck.update(found)
                if table == target_table:
                    existing_targets.update(found)

        # Bulk Buffers
        vouchers_bulk = []
        subscribers_bulk = []
        radcheck_bulk = []
        radusergroup_bulk = []
        radacct_bulk = []
        update_radcheck_bulk = []
        seen_in_chunk = set()

        # 3. Process Users
        for u in users:
            uname = u['username'].strip()
            uname_lower = uname.lower()
            upass = u.get('password', uname).strip() or uname
            uprofile = u.get('actual_profile', '').strip()
            has_prof = bool(uprofile and uprofile.lower() != 'none')

            # Skip expired/no-profile card if requested
            if ignore_expired and not has_prof:
                stats['ignored_expired_users'] += 1
                continue

            pkg_id = package_map.get(uprofile, default_pkg_id)
            if not pkg_id and package_map:
                pkg_id = next(iter(package_map.values()))

            mac = u.get('caller_id', '').strip() or None
            comment = u.get('comment', '').strip()
            email = u.get('email', '').strip()
            phone = u.get('phone', '').strip()
            simul = u.get('shared_users', 1)

            uptime_mins = u.get('uptime_used_mins', 0)
            down_bytes = u.get('download_used_bytes', 0)
            up_bytes = u.get('upload_used_bytes', 0)
            has_usage = (uptime_mins > 0 or down_bytes > 0 or up_bytes > 0)

            # Check duplicate
            is_existing = (uname_lower in existing_radcheck) or (uname_lower in seen_in_chunk)
            if is_existing:
                if duplicate_action == 'skip':
                    stats['skipped_users'] += 1
                    continue
                elif duplicate_action == 'overwrite':
                    if uname_lower not in existing_targets:
                        stats['skipped_users'] += 1
                        continue
                    update_radcheck_bulk.append((upass, uname))
                    stats['updated_users'] += 1
                    continue

            seen_in_chunk.add(uname_lower)

            pkg_name_actual = package_names.get(pkg_id, uprofile)
            prof_info = profiles_dict.get(uprofile, {})
            plan = dict(plans_by_id[pkg_id])
            for source, target in (('quota_mb', 'volume_quota_mb'), ('uptime_mins', 'uptime_limit_mins'),
                                   ('validity_value', 'validity_value'), ('validity_unit', 'validity_unit'),
                                   ('override_shared_users', 'simultaneous_sessions'),
                                   ('rate_down', 'rate_download'), ('rate_up', 'rate_upload'), ('price', 'price')):
                if prof_info.get(source) is not None:
                    plan[target] = prof_info[source]
            state = imported_state(u, plan, now_dt, import_consumption)
            radcheck_bulk.extend(imported_radius_checks(uname, upass, state))
            if mac:
                radcheck_bulk.append((uname, 'Calling-Station-Id', '==', mac.upper()))
            if pkg_name_actual:
                radusergroup_bulk.append((uname, pkg_name_actual, 1))
            if state['history']:
                radacct_bulk.append(historical_accounting_values(uname, state, now_dt, mac))
                stats['imported_consumption_users'] += 1
            if target_type == 'vouchers':
                snap_v_val = int(present_value(plan, 'validity_value', present_value(plan, 'validity_days', 30)))
                snap_v_unit = plan.get('validity_unit') or 'days'
                snap_v_days = snap_v_val if snap_v_unit == 'days' else int(present_value(plan, 'validity_days', 30))
                snap_rd = str(plan.get('rate_download') or '0')
                snap_ru = str(plan.get('rate_upload') or '0')
                v_status = 'unused' if state['status'] == 'inactive' else state['status']
                vouchers_bulk.append((
                    batch_map[pkg_id], pkg_id, uname[:30], uname, upass, upass, v_status, mac,
                    state['first_used_at'], state['expires_at'], state['last_renewed_at'],
                    float(plan.get('price') or 0), float(package_costs_map.get(uprofile, plan.get('cost') or 0)),
                    int(plan.get('volume_quota_mb') or 0), int(plan.get('uptime_limit_mins') or 0),
                    snap_v_val, snap_v_unit, snap_v_days, snap_rd, snap_ru, f"{snap_rd}/{snap_ru}",
                    int(plan.get('simultaneous_sessions') or 1)
                ))
            else:
                subscribers_bulk.append((
                    uname, upass, comment or uname, pkg_id, state['status'], mac, email, phone, comment,
                    state['first_used_at'], state['expires_at'], state['last_renewed_at'],
                    int(plan.get('volume_quota_mb') or 0)
                ))

            stats['created_users'] += 1

        # Flush Bulk Inserts to Database
        if update_radcheck_bulk:
            up_sql = "UPDATE radcheck SET value = ? WHERE username = ? AND attribute = 'Cleartext-Password'".replace('?', '%s' if is_mysql_conn(db) else '?')
            cur.executemany(up_sql, update_radcheck_bulk)
            table = 'wisp_vouchers' if target_type == 'vouchers' else 'wisp_subscribers'
            model_sql = adapt_query(f'UPDATE {table} SET password=? WHERE username=?', db)
            cur.executemany(model_sql, update_radcheck_bulk)
            credential_sql = adapt_query(f"""
                INSERT INTO radcheck(username,attribute,op,value)
                SELECT username,'Cleartext-Password',':=',password FROM {table} t
                WHERE username=? AND status IN ('active','inactive','unused')
                AND NOT EXISTS(SELECT 1 FROM radcheck r WHERE r.username=t.username AND r.attribute='Cleartext-Password')
            """, db)
            cur.executemany(credential_sql, [(username,) for _, username in update_radcheck_bulk])

        if radcheck_bulk:
            rc_sql = "INSERT INTO radcheck (username, attribute, op, value) VALUES (?, ?, ?, ?)".replace('?', '%s' if is_mysql_conn(db) else '?')
            cur.executemany(rc_sql, radcheck_bulk)

        if radusergroup_bulk:
            rg_sql = "INSERT INTO radusergroup (username, groupname, priority) VALUES (?, ?, ?)".replace('?', '%s' if is_mysql_conn(db) else '?')
            cur.executemany(rg_sql, radusergroup_bulk)

        if vouchers_bulk:
            v_sql = """
                INSERT INTO wisp_vouchers (
                    batch_id, package_id, serial_number, username, password, pin_code, status, bound_mac,
                    first_used_at, expires_at, last_renewed_at, created_at,
                    snap_price, snap_cost, snap_volume_quota_mb, snap_uptime_limit_mins,
                    snap_validity_value, snap_validity_unit, snap_validity_days,
                    snap_rate_download, snap_rate_upload, snap_rate_limit_str,
                    snap_simultaneous_sessions, snap_mikrotik_group
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, CURRENT_TIMESTAMP,
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?,
                    ?, 'ALL-SPEED'
                )
            """.replace('?', '%s' if is_mysql_conn(db) else '?')
            cur.executemany(v_sql, vouchers_bulk)

        if subscribers_bulk:
            s_sql = """
                INSERT INTO wisp_subscribers (
                    username, password, full_name, package_id, status,
                    mac_binding, email, phone, notes, first_used_at, expires_at, last_renewed_at,
                    snap_volume_quota_mb, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """.replace('?', '%s' if is_mysql_conn(db) else '?')
            cur.executemany(s_sql, subscribers_bulk)

        if radacct_bulk:
            acct_sql = """
                INSERT INTO radacct (
                    acctsessionid, acctuniqueid, username, realm, nasipaddress, nasportid, nasporttype,
                    acctstarttime, acctupdatetime, acctstoptime, acctinterval, acctsessiontime, acctauthentic,
                    connectinfo_start, connectinfo_stop,
                    acctinputoctets, acctoutputoctets, acctinputgigawords, acctoutputgigawords,
                    calledstationid, callingstationid,
                    acctterminatecause, servicetype, framedprotocol, framedipaddress
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?,
                    ?, ?,
                    ?, ?, ?, ?,
                    ?, ?,
                    ?, ?, ?, ?
                )
            """.replace('?', '%s' if is_mysql_conn(db) else '?')
            cur.executemany(acct_sql, radacct_bulk)

        # Update batch card_count and remove empty batches (only on final chunk or non-chunked import)
        if is_last_chunk and target_type == 'vouchers' and batch_map:
            for p_id, b_id in list(batch_map.items()):
                cur.execute("UPDATE wisp_voucher_batches SET card_count = (SELECT COUNT(*) FROM wisp_vouchers WHERE batch_id = ?) WHERE id = ?".replace('?', '%s' if is_mysql_conn(db) else '?'), (b_id, b_id))
                cur.execute("SELECT name, card_count FROM wisp_voucher_batches WHERE id = ?".replace('?', '%s' if is_mysql_conn(db) else '?'), (b_id,))
                b_info = cur.fetchone()
                if b_info:
                    c_name = b_info['name'] if isinstance(b_info, dict) else b_info[0]
                    c_count = b_info['card_count'] if isinstance(b_info, dict) else b_info[1]
                    if c_count == 0:
                        cur.execute("DELETE FROM wisp_voucher_batches WHERE id = ?".replace('?', '%s' if is_mysql_conn(db) else '?'), (b_id,))
                    else:
                        stats['batch_details'].append({
                            'id': b_id,
                            'name': c_name,
                            'total_cards': c_count
                        })

        if is_mysql_conn(db):
            cur.execute("SET foreign_key_checks = 1;")
            
        db.commit()

        if is_last_chunk:
            try:
                from services.license_guard_service import get_active_license_status
                get_active_license_status(force_refresh=True)
            except Exception:
                pass

        elapsed = round(time.time() - start_t, 2)
        if is_last_chunk:
            log_audit(1, admin_user or 'admin', 'USERMAN_IMPORT', 'tools', f"Imported {stats['created_users']} users across {len(batch_map)} batches from MikroTik User Manager in {elapsed}s.")
        
        state_cache_out = {
            'package_map': package_map,
            'package_names': {str(k): v for k, v in package_names.items()},
            'batch_map': {str(k): v for k, v in batch_map.items()},
            'default_pkg_id': default_pkg_id,
            'package_costs_map': package_costs_map,
            'profiles_dict': profiles_dict,
            'stats': stats
        }

        msg = (
            f"تم بنجاح استيراد {stats['created_users']:,} كرت/مشترك مقسمة عبر {len(stats['batch_details'])} حزمة باقات خلال {elapsed} ثانية."
            if is_last_chunk
            else f"تم استيراد الدفعة {(chunk_index or 0) + 1} من {total_chunks} بنجاح."
        )

        return {
            'success': True,
            'duration_seconds': elapsed,
            'is_chunked': is_chunked,
            'chunk_index': chunk_index,
            'total_chunks': total_chunks,
            'is_complete': is_last_chunk,
            'stats': stats,
            'state_cache': state_cache_out,
            'message': msg
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
            'error': f"فشل الاستيراد والزرع: {str(e)}",
            'stats': stats
        }
    finally:
        cur.close()
        db.close()

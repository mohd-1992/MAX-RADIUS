# -*- coding: utf-8 -*-
"""
Voucher & Scratch Cards service: batch generation, PIN/User-Pass algorithms,
QR code generation data, printing templates, and sales tracking.
"""

import secrets
import string
import datetime
import time
from database.db import query_all, query_one, execute_write, execute_many, log_audit
from core.radius_sync import sync_voucher_to_radius, delete_user_from_radius

from core.rate_limit import build_mikrotik_rate_limit

def generate_random_code(length=8, char_type='numbers'):
    if char_type == 'numbers':
        chars = string.digits
        first = secrets.choice('123456789')
        rest = ''.join(secrets.choice(chars) for _ in range(length - 1))
        return first + rest
    elif char_type == 'alphanumeric':
        chars = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ'
        return ''.join(secrets.choice(chars) for _ in range(length))
    else:
        chars = '23456789abcdefghjkmnpqrstuvwxyz'
        return ''.join(secrets.choice(chars) for _ in range(length))

def generate_voucher_batch(name, package_id, count, prefix='', pin_only=True,
                           char_type='numbers', code_length=8, reseller_id=None,
                           created_by='admin', same_user_pass=False, pass_length=None):
    pkg = query_one('SELECT * FROM wisp_packages WHERE id = ?', (package_id,))
    if not pkg:
        raise ValueError('الباقة المحددة غير موجودة.')
        
    count = int(count)
    same_user_pass = bool(same_user_pass)
    now_dt = datetime.datetime.now()
    batch_num = f"B{now_dt.strftime('%y%m%d%H%M%S')}-{secrets.randbelow(1000):03d}"
    
    if not name or not str(name).strip():
        short_seq = f"{secrets.randbelow(900) + 100}"
        name = f"حزمة {now_dt.strftime('%Y/%m/%d %H:%M:%S')} #{short_seq}"
    else:
        name = str(name).strip()
    
    # 1. Capture Immutable Contract / Package Snapshot at this exact moment
    rate_str = build_mikrotik_rate_limit(
        download=pkg['rate_download'],
        upload=pkg['rate_upload'],
        burst_down=pkg.get('burst_download'),
        burst_up=pkg.get('burst_upload'),
        threshold_down=pkg.get('burst_threshold_down'),
        threshold_up=pkg.get('burst_threshold_up'),
        burst_time=pkg.get('burst_time', 16),
        priority=pkg.get('priority', 8),
        min_down=pkg.get('min_download'),
        min_up=pkg.get('min_upload')
    )
    pkg_price = float(pkg['price'] or 0.0)
    pkg_cost = float(pkg.get('cost') or 0.0)
    pkg_quota = int(pkg.get('volume_quota_mb') or 0)
    pkg_uptime = int(pkg.get('uptime_limit_mins') or 0)
    pkg_val = int(pkg.get('validity_value') if pkg.get('validity_value') is not None else (pkg.get('validity_days') or 30))
    pkg_unit = pkg.get('validity_unit') or 'days'
    pkg_days = int(pkg.get('validity_days') or 30)
    pkg_simul = int(pkg.get('simultaneous_sessions') or 1)
    pkg_mgroup = str(pkg.get('mikrotik_group') or '').strip()
    
    batch_id = execute_write('''
        INSERT INTO wisp_voucher_batches (
            batch_number, name, package_id, reseller_id, card_count,
            prefix, pin_only, char_type, code_length, created_by,
            price, cost, volume_quota_mb, uptime_limit_mins,
            validity_value, validity_unit, validity_days,
            rate_download, rate_upload, rate_limit_str,
            simultaneous_sessions, mikrotik_group
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        batch_num, name, package_id, reseller_id or None, count,
        prefix, 1 if (pin_only or same_user_pass) else 0, char_type, code_length, created_by,
        pkg_price, pkg_cost, pkg_quota, pkg_uptime,
        pkg_val, pkg_unit, pkg_days,
        pkg['rate_download'], pkg['rate_upload'], rate_str,
        pkg_simul, pkg_mgroup
    ))
    
    if reseller_id:
        mgr = query_one('SELECT * FROM wisp_managers WHERE id = ?', (reseller_id,))
        if mgr:
            total_cost = round(count * float(pkg.get('cost') or (float(pkg['price']) * 0.8)), 2)
            cur_bal = float(mgr.get('wallet_balance') or 0.0)
            new_bal = round(cur_bal - total_cost, 2)
            execute_write('UPDATE wisp_managers SET wallet_balance = ? WHERE id = ?', (new_bal, reseller_id))
            inv_number = f"INV-CRD-{reseller_id}-{datetime.datetime.now().strftime('%y%m%d%H%M%S')}-{secrets.token_hex(2).upper()}"
            execute_write('''
                INSERT INTO wisp_manager_invoices (
                    invoice_number, manager_id, transaction_type, amount,
                    payment_type, balance_before, balance_after, notes, is_voided, created_by
                ) VALUES (?, ?, 'card_purchase', ?, 'cash', ?, ?, ?, 0, ?)
            ''', (
                inv_number, reseller_id, total_cost, cur_bal, new_bal,
                f'شراء دفعة كروت {name} ({count} كرت - دفعة #{batch_num})', created_by
            ))
        else:
            reseller = query_one('SELECT * FROM wisp_resellers WHERE id = ?', (reseller_id,))
            if reseller:
                total_cost = count * float(pkg.get('cost') or (float(pkg['price']) * 0.8))
                new_balance = float(reseller['balance']) - total_cost
                execute_write('UPDATE wisp_resellers SET balance = ? WHERE id = ?', (new_balance, reseller_id))
                execute_write('''
                    INSERT INTO wisp_reseller_transactions (reseller_id, type, amount, balance_after, description, reference_id)
                    VALUES (?, 'card_purchase', ?, ?, ?, ?)
                ''', (reseller_id, total_cost, new_balance, f'شراء دفعة كروت {name} ({count} كرت)', batch_num))

    vouchers_data = []
    radcheck_data = []
    radreply_data = []
    radusergroup_data = []
    
    pkg_name = pkg['name']
    
    for i in range(count):
        serial = f"{datetime.datetime.now().strftime('%y%m')}{secrets.randbelow(100000000):08d}"
        
        if pin_only:
            pin = f"{prefix}{generate_random_code(code_length, char_type)}"
            user = pin
            pwd = pin
        elif same_user_pass:
            user = f"{prefix}{generate_random_code(code_length, char_type)}"
            pwd = user
            pin = user
        else:
            user = f"{prefix}{generate_random_code(code_length, char_type)}"
            p_len = int(pass_length) if pass_length else code_length
            pwd = generate_random_code(p_len, 'numbers' if char_type == 'numbers' else 'alphanumeric')
            pin = pwd
            
        vouchers_data.append((
            batch_id, package_id, reseller_id or None, serial, user, pwd, pin, 'unused'
        ))
        
        # 1. Direct radcheck attributes (Password only, Simultaneous-Use is handled in radgroupcheck)
        radcheck_data.append((user, 'Cleartext-Password', ':=', pwd))
        
        # 2. Assign package group (Reply attributes and Simultaneous-Use apply from group)
        radusergroup_data.append((user, pkg_name, 1))
        
    execute_many('''
        INSERT INTO wisp_vouchers (
            batch_id, package_id, reseller_id, serial_number, username, password, pin_code, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', vouchers_data)
    
    execute_many('INSERT INTO radcheck (username, attribute, op, value) VALUES (?, ?, ?, ?)', radcheck_data)
    execute_many('INSERT INTO radusergroup (username, groupname, priority) VALUES (?, ?, ?)', radusergroup_data)
    
    log_audit(1, created_by, 'GENERATE_BATCH', 'vouchers', f'Generated batch {batch_num} with {count} cards for package {pkg_name}')
    return batch_id, batch_num

def get_batches(search=None, package_id=None, reseller_id=None):
    query = '''
        SELECT b.*, p.name as package_name, 
               COALESCE(p.price, b.price) as package_price, 
               COALESCE(p.rate_download, b.rate_download) as rate_download, 
               COALESCE(p.rate_upload, b.rate_upload) as rate_upload,
               COALESCE(p.volume_quota_mb, b.volume_quota_mb) as volume_quota_mb,
               COALESCE(p.validity_days, b.validity_days) as validity_days,
               COALESCE(p.validity_value, b.validity_value) as validity_value,
               COALESCE(p.validity_unit, b.validity_unit) as validity_unit,
               COALESCE(NULLIF(m.full_name, ''), m.username, r.name) as reseller_name,
               COALESCE(st.unused_count, 0) as unused_count,
               COALESCE(st.active_count, 0) as active_count,
               COALESCE(st.expired_count, 0) as expired_count,
               COALESCE(st.recharged_count, 0) as recharged_count,
               COALESCE(st.disabled_count, 0) as disabled_count
        FROM wisp_voucher_batches b
        LEFT JOIN wisp_packages p ON b.package_id = p.id
        LEFT JOIN wisp_resellers r ON b.reseller_id = r.id
        LEFT JOIN wisp_managers m ON b.reseller_id = m.id
        LEFT JOIN (
            SELECT batch_id,
                   SUM(CASE WHEN status = 'unused' THEN 1 ELSE 0 END) as unused_count,
                   SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) as active_count,
                   SUM(CASE WHEN status = 'expired' THEN 1 ELSE 0 END) as expired_count,
                   SUM(CASE WHEN status = 'recharged' THEN 1 ELSE 0 END) as recharged_count,
                   SUM(CASE WHEN status = 'disabled' THEN 1 ELSE 0 END) as disabled_count
            FROM wisp_vouchers
            GROUP BY batch_id
        ) st ON b.id = st.batch_id
        WHERE 1=1
    '''
    params = []
    if search:
        query += ' AND (b.name LIKE ? OR b.batch_number LIKE ?)'
        params.extend([f'%{search}%', f'%{search}%'])
    if package_id:
        try:
            query += ' AND b.package_id = ?'
            params.append(int(package_id))
        except (ValueError, TypeError):
            pass
    if reseller_id:
        try:
            query += ' AND b.reseller_id = ?'
            params.append(int(reseller_id))
        except (ValueError, TypeError):
            pass
    query += ' ORDER BY b.id DESC'
    return query_all(query, tuple(params))

def get_voucher_summary_counts():
    row = query_one('''
        SELECT 
            (SELECT COUNT(*) FROM wisp_voucher_batches) as total_batches,
            (SELECT COUNT(*) FROM wisp_vouchers) as total_cards,
            (SELECT COUNT(*) FROM wisp_vouchers WHERE status = 'unused') as unused_cards,
            (SELECT COUNT(*) FROM wisp_vouchers WHERE status = 'active') as active_cards,
            (SELECT COUNT(*) FROM wisp_vouchers WHERE status = 'expired') as expired_cards,
            (SELECT COUNT(*) FROM wisp_vouchers WHERE status IN ('recharged', 'disabled')) as recharged_cards
    ''')
    if not row:
        return {'total_batches': 0, 'total_cards': 0, 'unused_cards': 0, 'active_cards': 0, 'expired_cards': 0, 'recharged_cards': 0}
    return row

def get_vouchers(batch_id=None, status=None, search=None, limit=100):
    query = '''
        SELECT v.*, p.name as package_name, 
               COALESCE(v.snap_price, p.price, b.price) as price, 
               COALESCE(v.snap_volume_quota_mb, p.volume_quota_mb, b.volume_quota_mb) as volume_quota_mb,
               b.batch_number, COALESCE(NULLIF(m.full_name, ''), m.username, r.name) as reseller_name
        FROM wisp_vouchers v
        LEFT JOIN wisp_packages p ON v.package_id = p.id
        LEFT JOIN wisp_voucher_batches b ON v.batch_id = b.id
        LEFT JOIN wisp_resellers r ON v.reseller_id = r.id
        LEFT JOIN wisp_managers m ON v.reseller_id = m.id
        WHERE 1=1
    '''
    params = []
    if batch_id:
        query += ' AND v.batch_id = ?'
        params.append(batch_id)
    if status:
        query += ' AND v.status = ?'
        params.append(status)
    if search:
        query += ' AND (v.username LIKE ? OR v.serial_number LIKE ? OR v.pin_code LIKE ?)'
        like_str = f'%{search}%'
        params.extend([like_str, like_str, like_str])
        
    query += ' ORDER BY v.id DESC LIMIT ?'
    params.append(limit)
    return query_all(query, tuple(params))

def get_batch_cards_for_print(batch_id):
    batch = query_one('''
        SELECT b.*, p.name as package_name, 
               COALESCE(p.price, b.price) as package_price,
               COALESCE(p.validity_value, b.validity_value) as validity_value, 
               COALESCE(p.validity_unit, b.validity_unit) as validity_unit, 
               COALESCE(p.validity_days, b.validity_days) as validity_days,
               COALESCE(p.rate_download, b.rate_download) as rate_download, 
               COALESCE(p.rate_upload, b.rate_upload) as rate_upload, 
               COALESCE(p.volume_quota_mb, b.volume_quota_mb) as volume_quota_mb
        FROM wisp_voucher_batches b
        LEFT JOIN wisp_packages p ON b.package_id = p.id
        WHERE b.id = ?
    ''', (batch_id,))
    
    if not batch:
        return None, [], None
        
    cards = query_all('SELECT * FROM wisp_vouchers WHERE batch_id = ? ORDER BY id ASC', (batch_id,))
    
    hotspot_domain = query_one("SELECT `value` FROM wisp_system_settings WHERE `key` = 'hotspot_domain'")
    isp_name = query_one("SELECT `value` FROM wisp_system_settings WHERE `key` = 'isp_name'")
    currency = query_one("SELECT `value` FROM wisp_system_settings WHERE `key` = 'currency_symbol'")

    
    domain = hotspot_domain['value'] if hotspot_domain else 'wifi.hotspot'
    net_name = isp_name['value'] if isp_name else 'شبكة الأفق الذكية'
    curr = currency['value'] if currency else 'ر.س'
    
    template = query_one('SELECT * FROM wisp_card_templates WHERE is_default = 1 LIMIT 1')
    
    v_val = batch.get('validity_value') or batch.get('validity_days') or 30
    v_unit = batch.get('validity_unit') or 'days'
    unit_ar = 'ساعة' if v_unit == 'hours' else ('دقيقة' if v_unit == 'minutes' else ('أشهر' if v_unit == 'months' else 'يوم'))

    for c in cards:
        c_price = c.get('snap_price') or batch['package_price']
        c_quota_mb = c.get('snap_volume_quota_mb') if c.get('snap_volume_quota_mb') is not None else (batch.get('volume_quota_mb') or 0)
        c_rate_down = c.get('snap_rate_download') or batch.get('rate_download', '2M')
        c_rate_up = c.get('snap_rate_upload') or batch.get('rate_upload', '1M')
        c_val = c.get('snap_validity_value') or v_val
        c_unit = c.get('snap_validity_unit') or v_unit
        c_unit_ar = 'ساعة' if c_unit == 'hours' else ('دقيقة' if c_unit == 'minutes' else ('أشهر' if c_unit == 'months' else 'يوم'))

        c['login_url'] = f"http://{domain}/login?username={c['username']}&password={c['password']}"
        c['isp_name'] = net_name
        c['currency'] = curr
        c['package_name'] = batch['package_name']
        c['price'] = c_price
        c['speed'] = f"{c_rate_down} / {c_rate_up}"
        c['quota'] = f"{c_quota_mb // 1024} GB" if c_quota_mb >= 1024 else (f"{c_quota_mb} MB" if c_quota_mb > 0 else "غير محدود")
        c['validity'] = f"{c_val} {c_unit_ar}"
        
    return batch, cards, template

def delete_batch(batch_id, admin_username='admin'):
    batch = query_one('SELECT * FROM wisp_voucher_batches WHERE id = ?', (batch_id,))
    if not batch:
        return False

    # Check for unused cards to refund
    unused_row = query_one("SELECT COUNT(*) as cnt FROM wisp_vouchers WHERE batch_id = ? AND status = 'unused'", (batch_id,))
    unused_count = unused_row['cnt'] if unused_row else 0
    reseller_id = batch.get('reseller_id')

    if reseller_id and unused_count > 0:
        cost_per_card = float(batch.get('cost') or (float(batch.get('price', 0)) * 0.8))
        refund_amount = round(unused_count * cost_per_card, 2)
        if refund_amount > 0:
            mgr = query_one('SELECT * FROM wisp_managers WHERE id = ?', (reseller_id,))
            if mgr:
                bal_before = float(mgr.get('wallet_balance') or 0.0)
                bal_after = round(bal_before + refund_amount, 2)
                execute_write('UPDATE wisp_managers SET wallet_balance = ? WHERE id = ?', (bal_after, reseller_id))
                inv_number = f"INV-REF-{reseller_id}-{datetime.datetime.now().strftime('%y%m%d%H%M%S')}-{secrets.token_hex(2).upper()}"
                execute_write('''
                    INSERT INTO wisp_manager_invoices (
                        invoice_number, manager_id, transaction_type, amount,
                        payment_type, balance_before, balance_after, notes, is_voided, created_by
                    ) VALUES (?, ?, 'refund', ?, 'cash', ?, ?, ?, 0, ?)
                ''', (
                    inv_number, reseller_id, refund_amount, bal_before, bal_after,
                    f'استرجاع قيمة {unused_count} كرت غير مستخدم عند حذف الدفعة #{batch.get("batch_number", batch_id)}',
                    admin_username
                ))
            else:
                reseller = query_one('SELECT * FROM wisp_resellers WHERE id = ?', (reseller_id,))
                if reseller:
                    new_bal = float(reseller['balance']) + refund_amount
                    execute_write('UPDATE wisp_resellers SET balance = ? WHERE id = ?', (new_bal, reseller_id))
                    execute_write('''
                        INSERT INTO wisp_reseller_transactions (reseller_id, type, amount, balance_after, description, reference_id)
                        VALUES (?, 'refund', ?, ?, ?, ?)
                    ''', (reseller_id, refund_amount, new_bal, f'استرجاع قيمة {unused_count} كرت لحذف الدفعة', batch.get('batch_number', '')))

    cards = query_all('SELECT username FROM wisp_vouchers WHERE batch_id = ?', (batch_id,))
    for c in cards:
        delete_user_from_radius(c['username'])
        
    execute_write('DELETE FROM wisp_vouchers WHERE batch_id = ?', (batch_id,))
    execute_write('DELETE FROM wisp_voucher_batches WHERE id = ?', (batch_id,))
    log_audit(1, admin_username, 'DELETE_BATCH', 'vouchers', f'Deleted batch ID {batch_id} (refunded {unused_count} unused cards)')
    return True

def update_voucher_batch(batch_id, name, package_id, admin_username='admin'):
    batch = query_one('SELECT * FROM wisp_voucher_batches WHERE id = ?', (batch_id,))
    if not batch:
        raise ValueError('الدفعة غير موجودة.')

    new_pkg = query_one('SELECT * FROM wisp_packages WHERE id = ?', (package_id,))
    if not new_pkg:
        raise ValueError('الباقة المحددة غير موجودة.')

    old_package_id = batch['package_id']
    execute_write('UPDATE wisp_voucher_batches SET name = ?, package_id = ? WHERE id = ?', (name.strip(), package_id, batch_id))

    if int(package_id) != int(old_package_id):
        execute_write('UPDATE wisp_vouchers SET package_id = ? WHERE batch_id = ?', (package_id, batch_id))
        cards = query_all('SELECT username FROM wisp_vouchers WHERE batch_id = ?', (batch_id,))
        new_groupname = new_pkg['name']
        for c in cards:
            u = c['username']
            exists = query_one('SELECT 1 FROM radusergroup WHERE username = ?', (u,))
            if exists:
                execute_write('UPDATE radusergroup SET groupname = ? WHERE username = ?', (new_groupname, u))
            else:
                execute_write('INSERT INTO radusergroup (username, groupname, priority) VALUES (?, ?, 1)', (u, new_groupname))

    log_audit(1, admin_username, 'UPDATE_BATCH', 'vouchers', f'Updated batch {batch_id} to name "{name}" and package "{new_pkg["name"]}"')
    return True

def update_voucher_card(card_id, new_username, new_password, admin_username='admin'):
    card = query_one('SELECT * FROM wisp_vouchers WHERE id = ?', (card_id,))
    if not card:
        raise ValueError('الكرت غير موجود.')

    old_username = card['username']
    new_username = new_username.strip()
    new_password = new_password.strip()

    if not new_username or not new_password:
        raise ValueError('اسم المستخدم وكلمة المرور مطلوبان.')

    if new_username != old_username:
        duplicate = query_one('SELECT 1 FROM wisp_vouchers WHERE username = ? AND id != ?', (new_username, card_id))
        if duplicate:
            raise ValueError(f'اسم المستخدم {new_username} مستخدم بالفعل لكارت آخر.')
        dup_sub = query_one('SELECT 1 FROM wisp_subscribers WHERE username = ?', (new_username,))
        if dup_sub:
            raise ValueError(f'اسم المستخدم {new_username} مستخدم بالفعل لمشترك آخر.')

    pin_code = new_password if card['pin_code'] == card['password'] else (new_username if card['pin_code'] == old_username else card['pin_code'])
    execute_write('''
        UPDATE wisp_vouchers
        SET username = ?, password = ?, pin_code = ?
        WHERE id = ?
    ''', (new_username, new_password, pin_code, card_id))

    # FreeRADIUS sync
    pwd_check = query_one("SELECT 1 FROM radcheck WHERE username = ? AND attribute = 'Cleartext-Password'", (old_username,))
    if pwd_check:
        execute_write("UPDATE radcheck SET username = ?, value = ? WHERE username = ? AND attribute = 'Cleartext-Password'", (new_username, new_password, old_username))
    else:
        execute_write("INSERT INTO radcheck (username, attribute, op, value) VALUES (?, 'Cleartext-Password', ':=', ?)", (new_username, new_password))
    
    execute_write("UPDATE radcheck SET username = ? WHERE username = ?", (new_username, old_username))
    execute_write("UPDATE radusergroup SET username = ? WHERE username = ?", (new_username, old_username))
    execute_write("UPDATE radreply SET username = ? WHERE username = ?", (new_username, old_username))
    execute_write("UPDATE radacct SET username = ? WHERE username = ?", (new_username, old_username))

    log_audit(1, admin_username, 'UPDATE_VOUCHER_CARD', 'vouchers', f'Updated card ID {card_id}: {old_username} -> {new_username}')
    return True

def calculate_package_expiration(validity_value, validity_unit='days', start_dt=None):
    """
    Calculates exact expiration datetime and FreeRADIUS Expiration attribute value.
    If validity_value is 0 or None, returns (None, None) for unlimited validity.
    Returns: (expiry_iso_str, freeradius_expiration_str)
    e.g. ('2026-09-04 14:00:00', '04 Sep 2026 14:00:00') or (None, None)
    """
    try:
        val = int(validity_value or 0)
    except (ValueError, TypeError):
        val = 0

    if val <= 0:
        return None, None

    if start_dt is None:
        try:
            from core.time_service import get_system_now
            start_dt = get_system_now()
        except Exception:
            start_dt = datetime.datetime.now()
    
    unit = str(validity_unit or 'days').lower().strip()
    
    if unit in ['minutes', 'minute', 'دقائق', 'دقيقة']:
        delta = datetime.timedelta(minutes=val)
    elif unit in ['hours', 'hour', 'ساعات', 'ساعة']:
        delta = datetime.timedelta(hours=val)
    elif unit in ['months', 'month', 'أشهر', 'شهر']:
        delta = datetime.timedelta(days=val * 30)
    else:  # days
        delta = datetime.timedelta(days=val)
        
    expiry_dt = start_dt + delta
    expiry_iso = expiry_dt.strftime('%Y-%m-%d %H:%M:%S')
    freeradius_exp = expiry_dt.strftime('%d %b %Y %H:%M:%S')
    
    return expiry_iso, freeradius_exp

def check_and_update_expired_vouchers():
    """
    Scans active/used vouchers and subscribers and marks those past their expiration date or volume quota as 'expired':
    1. Time expiration: marks expired with 'تم استهلاك مدة الصلاحية'.
    2. Data Quota expiration: marks expired with 'تم استهلاك رصيد البيانات بالكامل'.
    """
    # 1. Voucher Time expiration (using DB CURRENT_TIMESTAMP directly)
    execute_write("""
        UPDATE wisp_vouchers
        SET status = 'expired',
            expire_reason = 'تم استهلاك مدة الصلاحية'
        WHERE status IN ('active', 'used')
          AND expires_at IS NOT NULL
          AND expires_at <= CURRENT_TIMESTAMP
    """)

    # 2. Voucher & Subscriber Data Quota expiration (only if accounting records exist)
    has_acct = query_one("SELECT 1 FROM radacct LIMIT 1")
    if has_acct:
        execute_write("""
            UPDATE wisp_vouchers v
            JOIN (
                SELECT a.username
                FROM radacct a
                JOIN wisp_vouchers v2 ON a.username = v2.username AND v2.status IN ('active', 'used')
                JOIN wisp_packages p ON v2.package_id = p.id AND p.volume_quota_mb > 0
                WHERE (v2.last_renewed_at IS NULL OR COALESCE(a.acctstarttime, a.acctupdatetime) >= v2.last_renewed_at)
                GROUP BY a.username, p.volume_quota_mb, v2.extra_quota_mb
                HAVING SUM(COALESCE(a.acctinputoctets, 0) + COALESCE(a.acctoutputoctets, 0)) >= ((p.volume_quota_mb + v2.extra_quota_mb) * 1048576)
            ) over_limit ON v.username = over_limit.username
            SET v.status = 'expired',
                v.expire_reason = 'تم استهلاك رصيد البيانات بالكامل'
            WHERE v.status IN ('active', 'used')
        """)

        execute_write("""
            UPDATE wisp_subscribers s
            JOIN (
                SELECT a.username
                FROM radacct a
                JOIN wisp_subscribers s2 ON a.username = s2.username AND s2.status = 'active'
                JOIN wisp_packages p ON s2.package_id = p.id AND p.volume_quota_mb > 0
                WHERE (s2.last_renewed_at IS NULL OR COALESCE(a.acctstarttime, a.acctupdatetime) >= s2.last_renewed_at)
                GROUP BY a.username, p.volume_quota_mb, s2.extra_quota_mb
                HAVING SUM(COALESCE(a.acctinputoctets, 0) + COALESCE(a.acctoutputoctets, 0)) >= ((p.volume_quota_mb + s2.extra_quota_mb) * 1048576)
            ) over_limit ON s.username = over_limit.username
            SET s.status = 'expired'
            WHERE s.status = 'active'
        """)

    # 4. Clean up FreeRADIUS credentials for expired vouchers and subscribers
    try:
        execute_write("""
            DELETE FROM radcheck 
            WHERE attribute = 'Cleartext-Password'
              AND username IN (
                  SELECT username FROM wisp_vouchers 
                  WHERE status IN ('expired', 'disabled', 'suspended', 'recharged')
              )
        """)
        
        execute_write("""
            DELETE FROM radcheck 
            WHERE attribute = 'Cleartext-Password'
              AND username IN (
                  SELECT username FROM wisp_subscribers 
                  WHERE status IN ('expired', 'disabled', 'suspended')
              )
        """)
    except Exception:
        pass

    # 5. Auto-Disconnect Watchdog: Disconnect active sessions for any expired/exhausted/disabled accounts
    try:
        from services.subscriber_service import disconnect_subscriber_session
        
        has_live_sessions = query_one("SELECT 1 FROM radacct WHERE acctstoptime IS NULL LIMIT 1")
        if has_live_sessions:
            # Find active sessions of expired or disabled vouchers
            expired_vouchers = query_all("""
                SELECT DISTINCT a.username
                FROM radacct a
                JOIN wisp_vouchers v ON a.username = v.username
                WHERE a.acctstoptime IS NULL
                  AND (v.status IN ('expired', 'disabled', 'suspended', 'recharged')
                       OR (v.expires_at IS NOT NULL AND v.expires_at <= CURRENT_TIMESTAMP))
            """)
            
            # Find active sessions of expired or disabled subscribers
            expired_subscribers = query_all("""
                SELECT DISTINCT a.username
                FROM radacct a
                JOIN wisp_subscribers s ON a.username = s.username
                WHERE a.acctstoptime IS NULL
                  AND (s.status IN ('expired', 'disabled', 'suspended')
                       OR (s.expires_at IS NOT NULL AND s.expires_at <= CURRENT_TIMESTAMP))
            """)
            
            to_kick = set()
            if expired_vouchers:
                for r in expired_vouchers:
                    if r.get('username'):
                        to_kick.add(r['username'])
            if expired_subscribers:
                for r in expired_subscribers:
                    if r.get('username'):
                        to_kick.add(r['username'])
            
            for user in to_kick:
                try:
                    disconnect_subscriber_session(user)
                except Exception:
                    pass
    except Exception as err:
        logger.error(f"Error in auto-disconnect watchdog: {err}")

    return True


def activate_voucher_card(username, bound_mac=None, nas_ip=None):
    """
    Activates a voucher card upon first login or manual activation.
    1. Locks the row with SELECT ... FOR UPDATE within an atomic transaction.
    2. Checks idempotency: if status != 'unused', returns False.
    3. Captures live package snapshot at this exact moment and saves it into wisp_vouchers snap_* columns.
    4. Calculates exact expiration date from validity_value and validity_unit natively in SQL.
    5. Updates status = 'active', first_used_at, and expires_at.
    6. Writes snapshotted rate-limit and VSAs to radreply.
    7. Records the card sale and revenue in wisp_voucher_sales table.
    """
    if not username:
        return False

    from database.db import db_session, adapt_query, is_mysql_conn

    try:
        with db_session() as conn:
            cursor = conn.cursor()
            lock_clause = "FOR UPDATE" if is_mysql_conn(conn) else ""
            sql = adapt_query(f'''
                SELECT v.*, 
                       p.name as package_name, p.price, p.cost,
                       p.validity_value, p.validity_unit, p.validity_days,
                       p.volume_quota_mb, p.uptime_limit_mins,
                       p.rate_download, p.rate_upload, p.burst_download, p.burst_upload,
                       p.burst_threshold_down, p.burst_threshold_up, p.burst_time,
                       p.priority, p.min_download, p.min_upload,
                       p.simultaneous_sessions, p.mikrotik_group,
                       b.name as batch_name, b.id as batch_id
                FROM wisp_vouchers v
                JOIN wisp_packages p ON v.package_id = p.id
                JOIN wisp_voucher_batches b ON v.batch_id = b.id
                WHERE LOWER(v.username) = LOWER(?) OR v.pin_code = ?
                LIMIT 1 {lock_clause}
            ''', conn)

            cursor.execute(sql, (username, username))
            card = cursor.fetchone()
            if not card:
                return False

            if not isinstance(card, dict):
                card = dict(card)

            if card['status'] != 'unused':
                return False

            val = card.get('validity_value') if card.get('validity_value') is not None else (card.get('validity_days') or 30)
            unit = card.get('validity_unit') or 'days'
            quota_mb = int(card.get('volume_quota_mb') or 0)
            uptime_mins = int(card.get('uptime_limit_mins') or 0)
            pkg_price = float(card.get('price') or 0.0)
            pkg_cost = float(card.get('cost') or 0.0)
            simul = int(card.get('simultaneous_sessions') or 1)
            mgroup = str(card.get('mikrotik_group') or '').strip()
            
            rate_str = build_mikrotik_rate_limit(
                download=card['rate_download'],
                upload=card['rate_upload'],
                burst_down=card.get('burst_download'),
                burst_up=card.get('burst_upload'),
                threshold_down=card.get('burst_threshold_down'),
                threshold_up=card.get('burst_threshold_up'),
                burst_time=card.get('burst_time', 16),
                priority=card.get('priority', 8),
                min_down=card.get('min_download'),
                min_up=card.get('min_upload')
            )

            # 1. Update card status and freeze snapshot on voucher record
            update_sql = adapt_query('''
                UPDATE wisp_vouchers
                SET status = 'active',
                    first_used_at = IFNULL(first_used_at, CURRENT_TIMESTAMP),
                    last_renewed_at = IFNULL(last_renewed_at, CURRENT_TIMESTAMP),
                    expires_at = IFNULL(expires_at, 
                        CASE 
                            WHEN ? = 'minutes' THEN DATE_ADD(CURRENT_TIMESTAMP, INTERVAL ? MINUTE)
                            WHEN ? = 'hours' THEN DATE_ADD(CURRENT_TIMESTAMP, INTERVAL ? HOUR)
                            WHEN ? = 'months' THEN DATE_ADD(CURRENT_TIMESTAMP, INTERVAL ? MONTH)
                            ELSE DATE_ADD(CURRENT_TIMESTAMP, INTERVAL ? DAY)
                        END
                    ),
                    expire_reason = '',
                    bound_mac = IFNULL(bound_mac, ''),
                    snap_price = ?,
                    snap_cost = ?,
                    snap_volume_quota_mb = ?,
                    snap_uptime_limit_mins = ?,
                    snap_validity_value = ?,
                    snap_validity_unit = ?,
                    snap_validity_days = ?,
                    snap_rate_download = ?,
                    snap_rate_upload = ?,
                    snap_rate_limit_str = ?,
                    snap_simultaneous_sessions = ?,
                    snap_mikrotik_group = ?
                WHERE id = ? AND batch_id = ?
            ''', conn)
            cursor.execute(update_sql, (
                unit, val, unit, val, unit, val, val,
                pkg_price, pkg_cost, quota_mb, uptime_mins,
                val, unit, val, str(card.get('rate_download') or ''), str(card.get('rate_upload') or ''),
                rate_str, simul, mgroup,
                card['id'], card['batch_id']
            ))

            # 1.1 Ensure global sequence ID is assigned
            seq_ins = adapt_query('''
                INSERT INTO wisp_global_sequence (entity_type, entity_id, created_at)
                VALUES ('voucher', ?, CURRENT_TIMESTAMP)
                ON DUPLICATE KEY UPDATE seq_id = seq_id
            ''', conn)
            cursor.execute(seq_ins, (card['id'],))

            seq_sel = adapt_query("SELECT seq_id FROM wisp_global_sequence WHERE entity_type = 'voucher' AND entity_id = ?", conn)
            cursor.execute(seq_sel, (card['id'],))
            seq_row = cursor.fetchone()
            if seq_row:
                s_id = seq_row['seq_id'] if isinstance(seq_row, dict) else seq_row[0]
                seq_up = adapt_query("UPDATE wisp_vouchers SET global_seq_id = ? WHERE id = ?", conn)
                cursor.execute(seq_up, (s_id, card['id']))

            # 2. Write frozen rate-limit & reply attributes to radreply
            if rate_str:
                rr_rate = adapt_query(
                    'INSERT INTO radreply (username, attribute, op, value) VALUES (?, ?, ?, ?) ON DUPLICATE KEY UPDATE value = ?',
                    conn
                )
                cursor.execute(rr_rate, (card['username'], 'MikroTik-Rate-Limit', ':=', rate_str, rate_str))

            rr_int = adapt_query(
                'INSERT INTO radreply (username, attribute, op, value) VALUES (?, ?, ?, ?) ON DUPLICATE KEY UPDATE value = ?',
                conn
            )
            cursor.execute(rr_int, (card['username'], 'Acct-Interim-Interval', ':=', '180', '180'))

            if mgroup:
                rr_grp = adapt_query(
                    'INSERT INTO radreply (username, attribute, op, value) VALUES (?, ?, ?, ?) ON DUPLICATE KEY UPDATE value = ?',
                    conn
                )
                cursor.execute(rr_grp, (card['username'], 'Mikrotik-Group', ':=', mgroup, mgroup))

            # 3. Remove any static Expiration attribute in radcheck so FreeRADIUS computes dynamic Session-Timeout from MariaDB
            del_rc = adapt_query("DELETE FROM radcheck WHERE LOWER(username) = LOWER(?) AND attribute = 'Expiration'", conn)
            cursor.execute(del_rc, (card['username'],))

            # 4. Record sale revenue in wisp_voucher_sales
            chk_sale = adapt_query('SELECT 1 FROM wisp_voucher_sales WHERE voucher_id = ?', conn)
            cursor.execute(chk_sale, (card['id'],))
            existing_sale = cursor.fetchone()
            if not existing_sale:
                ins_sale = adapt_query('''
                    INSERT INTO wisp_voucher_sales (
                        voucher_id, batch_id, batch_name, username, serial_number,
                        package_name, price, cost, reseller_id, activated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ''', conn)
                cursor.execute(ins_sale, (
                    card['id'], card['batch_id'], card['batch_name'], card['username'],
                    card['serial_number'], card['package_name'], pkg_price,
                    pkg_cost, card['reseller_id']
                ))

            log_audit(1, 'system', 'ACTIVATE_VOUCHER', 'vouchers',
                      f'Card {card["username"]} activated with frozen package snapshot ({quota_mb}MB / {rate_str or "Unlimited"}).')
            return True
    except Exception as e:
        print(f"[Voucher Activation Error in services/voucher_service]: {e}")
        return False

def sync_voucher_sales(batch_size=5000):
    """Backfills all activated/expired vouchers into wisp_voucher_sales if missing in efficient chunks."""
    has_missing = query_one("""
        SELECT 1 FROM wisp_vouchers v
        LEFT JOIN wisp_voucher_sales s ON s.voucher_id = v.id
        WHERE v.status IN ('active', 'expired') AND s.id IS NULL
        LIMIT 1
    """)
    if not has_missing:
        return

    while True:
        affected = execute_write('''
            INSERT INTO wisp_voucher_sales (
                voucher_id, batch_id, batch_name, username, serial_number,
                package_name, price, cost, reseller_id, activated_at
            )
            SELECT v.id, v.batch_id, b.name, v.username, v.serial_number,
                   p.name, COALESCE(v.snap_price, p.price), COALESCE(v.snap_cost, p.cost), v.reseller_id,
                   COALESCE(v.first_used_at, v.created_at)
            FROM wisp_vouchers v
            JOIN wisp_packages p ON v.package_id = p.id
            JOIN wisp_voucher_batches b ON v.batch_id = b.id
            LEFT JOIN wisp_voucher_sales s ON s.voucher_id = v.id
            WHERE v.status IN ('active', 'expired')
              AND s.id IS NULL
            LIMIT 5000
        ''')
        if not affected or affected <= 0:
            break

_LAST_VOUCHER_SYNC_TIME = 0

def sync_voucher_activations(force=False):
    """
    Scans radacct sessions and radpostauth for newly logged in unused cards,
    and automatically activates them, calculates validity, and logs sales.
    Throttled to run at most once every 60 seconds to keep page loads fast.
    """
    global _LAST_VOUCHER_SYNC_TIME
    now = time.time()
    if not force and (now - _LAST_VOUCHER_SYNC_TIME < 60):
        return

    _LAST_VOUCHER_SYNC_TIME = now
    try:
        has_acct = query_one("SELECT 1 FROM radacct LIMIT 1")
        if has_acct:
            cards_to_activate = query_all('''
                SELECT DISTINCT v.username, 
                       COALESCE(r.nasipaddress, '') as nas_ip
                FROM wisp_vouchers v
                INNER JOIN radacct r ON v.username = r.username
                WHERE v.status = 'unused'
                LIMIT 50
            ''')
            for c in cards_to_activate:
                activate_voucher_card(c['username'], bound_mac=None, nas_ip=c.get('nas_ip'))

        postauth_cards = query_all('''
            SELECT DISTINCT v.username, 
                   '' as nas_ip
            FROM wisp_vouchers v
            INNER JOIN radpostauth p ON v.username = p.username
            WHERE v.status = 'unused' AND p.reply = 'Access-Accept'
            LIMIT 50
        ''')
        for c in postauth_cards:
            activate_voucher_card(c['username'], bound_mac=None, nas_ip=c.get('nas_ip'))

        check_and_update_expired_vouchers()
    except Exception:
        pass


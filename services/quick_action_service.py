# -*- coding: utf-8 -*-
"""
Quick Actions Service for Subscribers and Voucher Cards:
Handles:
1. Extend Validity (days)
2. Terminate Subscription (Immediate expiration + CoA kick + reset extra quota)
3. Renew Package (Reset cycle, re-calculate validity & quota, log invoice/sale, CoA kick)
4. Change Package (Update package_id, radusergroup, validity, quota, CoA kick)
5. Add Data Quota (extra_quota_mb in MB/GB)
6. Usage History (radacct sessions breakdown)
7. Add Wallet Balance (balance deposit + transaction log)
8. Deduct Wallet Balance (balance deduction with balance verification + log)
9. Disconnect / Kick (CoA Disconnect-Request to NAS)
"""

import datetime
from core.time_service import get_db_storage_now
import uuid
from database.db import query_one, query_all, execute_write, log_audit, log_user_audit, db_session, is_mysql_conn, adapt_query
from core.coa import RadiusCoaClient
from core.rate_limit import format_bytes, format_duration
from services.voucher_service import calculate_package_expiration
from services.quota_service import calculate_cycle_usage_and_rollover, record_session_baselines

def format_mb_or_gb(mb):
    if not mb:
        return "0 MB"
    if mb >= 1024:
        gb = round(mb / 1024.0, 2)
        gb_str = f"{int(gb)}" if gb.is_integer() else f"{gb:.2f}"
        return f"{gb_str} جيجابايت"
    return f"{int(mb)} ميجابايت"

def generate_invoice_number(sub_id=1):
    return f"INV-{get_db_storage_now().replace(tzinfo=None).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"

def get_target_entity(entity_type, entity_id=None):
    """
    Find subscriber or voucher card by ID or username.
    Returns: (entity_dict, 'subscriber' or 'voucher')
    """
    if entity_id is None:
        entity_id = entity_type
        entity_type = None

    if entity_type == 'subscriber':
        if isinstance(entity_id, int) or str(entity_id).isdigit():
            row = query_one("""
                SELECT s.*, p.name as package_name, p.price as package_price,
                       p.validity_value, p.validity_unit, COALESCE(s.snap_volume_quota_mb, p.volume_quota_mb, 0) as volume_quota_mb
                FROM wisp_subscribers s
                JOIN wisp_packages p ON s.package_id = p.id
                WHERE s.id = ?
            """, (int(entity_id),))
        else:
            row = query_one("""
                SELECT s.*, p.name as package_name, p.price as package_price,
                       p.validity_value, p.validity_unit, COALESCE(s.snap_volume_quota_mb, p.volume_quota_mb, 0) as volume_quota_mb
                FROM wisp_subscribers s
                JOIN wisp_packages p ON s.package_id = p.id
                WHERE LOWER(s.username) = LOWER(?)
            """, (str(entity_id).strip(),))
        return row, 'subscriber'
    elif entity_type == 'voucher':
        if isinstance(entity_id, int) or str(entity_id).isdigit():
            row = query_one("""
                SELECT v.*, p.name as package_name, 
                       COALESCE(v.snap_price, p.price) as package_price,
                       COALESCE(v.snap_validity_value, p.validity_value) as validity_value, 
                       COALESCE(v.snap_validity_unit, p.validity_unit) as validity_unit, 
                       COALESCE(v.snap_volume_quota_mb, p.volume_quota_mb) as volume_quota_mb,
                       b.name as batch_name
                FROM wisp_vouchers v
                JOIN wisp_packages p ON v.package_id = p.id
                JOIN wisp_voucher_batches b ON v.batch_id = b.id
                WHERE v.id = ?
            """, (int(entity_id),))
        else:
            row = query_one("""
                SELECT v.*, p.name as package_name, 
                       COALESCE(v.snap_price, p.price) as package_price,
                       COALESCE(v.snap_validity_value, p.validity_value) as validity_value, 
                       COALESCE(v.snap_validity_unit, p.validity_unit) as validity_unit, 
                       COALESCE(v.snap_volume_quota_mb, p.volume_quota_mb) as volume_quota_mb,
                       b.name as batch_name
                FROM wisp_vouchers v
                JOIN wisp_packages p ON v.package_id = p.id
                JOIN wisp_voucher_batches b ON v.batch_id = b.id
                WHERE LOWER(v.username) = LOWER(?) OR v.pin_code = ?
            """, (str(entity_id).strip(), str(entity_id).strip()))
        return row, 'voucher'
    else:  # entity_type is None, probe voucher then subscriber
        v_res, _ = get_target_entity('voucher', entity_id)
        if v_res:
            return v_res, 'voucher'
        s_res, _ = get_target_entity('subscriber', entity_id)
        if s_res:
            return s_res, 'subscriber'
        return None, 'unknown'


def action_delete_entity(entity_type, entity_id, admin_username='admin'):
    """0. حذف المشترك أو الكرت بالكامل ومسح سمات الراديوس وطرد الجلسة"""
    entity, etype = get_target_entity(entity_type, entity_id)
    if not entity:
        return False, "المشترك أو الكرت غير موجود"
        
    username = entity['username']
    
    # 1. Disconnect user first via CoA Disconnect if active
    try:
        action_disconnect_user(entity_type, entity_id, admin_username=admin_username)
    except Exception:
        pass
        
    # 2. FreeRADIUS tables deletion
    execute_write("DELETE FROM radcheck WHERE LOWER(username) = LOWER(?)", (username,))
    execute_write("DELETE FROM radreply WHERE LOWER(username) = LOWER(?)", (username,))
    execute_write("DELETE FROM radusergroup WHERE LOWER(username) = LOWER(?)", (username,))
    
    # 3. System database tables deletion
    if etype == 'subscriber':
        execute_write("DELETE FROM wisp_invoices WHERE subscriber_id = ?", (entity['id'],))
        execute_write("DELETE FROM wisp_subscribers WHERE id = ?", (entity['id'],))
        log_audit(1, admin_username, 'DELETE_SUBSCRIBER', 'subscribers', f'Deleted subscriber {username} (ID: {entity["id"]})')
    else:
        # Preserve historical sales/revenue ledger in wisp_voucher_sales upon voucher deletion
        execute_write("DELETE FROM wisp_vouchers WHERE id = ?", (entity['id'],))
        log_audit(1, admin_username, 'DELETE_VOUCHER', 'vouchers', f'Deleted voucher card {username} (ID: {entity["id"]})')
        
    return True, f"تم حذف [{username}] بالكامل من النظام والراديوس بنجاح."

def action_bulk_execute(action_fn, entity_type, target_ids, **kwargs):
    """
    Executes an action for a single ID or a list/array of target IDs.
    Ensures safe atomic handling per item and reports batch progress.
    """
    import json
    
    if isinstance(action_fn, str):
        action_map = {
            'delete': action_delete_entity,
            'extend_time': action_extend_time,
            'terminate': action_terminate_subscription,
            'renew': action_renew_package,
            'change_package': action_change_package,
            'add_quota': action_add_quota,
            'add_wallet': action_add_wallet_balance,
            'deduct_wallet': action_deduct_wallet_balance,
            'disconnect': action_disconnect_user
        }
        action_fn = action_map.get(action_fn.lower().strip(), action_delete_entity)

    if isinstance(target_ids, str):
        target_ids = target_ids.strip()
        if target_ids.startswith('[') and target_ids.endswith(']'):
            try:
                target_ids = json.loads(target_ids)
            except Exception:
                target_ids = [target_ids]
        elif ',' in target_ids:
            target_ids = [x.strip() for x in target_ids.split(',') if x.strip()]
        elif target_ids:
            target_ids = [target_ids]
        else:
            target_ids = []
            
    if not isinstance(target_ids, (list, tuple, set)):
        target_ids = [target_ids] if target_ids is not None else []
        
    target_ids = [x for x in target_ids if x is not None and str(x).strip() != '']
    if not target_ids:
        return False, "لم يتم تحديد أي مشتركين لتنفيذ العملية الجماعية."
        
    if len(target_ids) == 1:
        return action_fn(entity_type, target_ids[0], **kwargs)
        
    success_count = 0
    fail_count = 0
    errors = []
    
    for tid in target_ids:
        try:
            ok, msg = action_fn(entity_type, tid, **kwargs)
            if ok:
                success_count += 1
            else:
                fail_count += 1
                errors.append(f"#{tid}: {msg}")
        except Exception as ex:
            fail_count += 1
            errors.append(f"#{tid}: {str(ex)}")
            
    is_success = (success_count > 0)
    summary = f"تم تنفيذ العملية بنجاح على {success_count} من أصل {len(target_ids)}."
    if fail_count > 0:
        summary += f" (تعذر التنفيذ على {fail_count})"
        
    return is_success, summary

def action_extend_time(entity_type, entity_id, days, admin_username='admin'):
    """1. تمديد الصلاحية"""
    entity, etype = get_target_entity(entity_type, entity_id)
    if not entity:
        return False, "الحساب أو الكرت غير موجود"
    
    try:
        days = int(days)
        if days <= 0:
            return False, "يرجى إدخال عدد أيام صحيح أكبر من 0"
    except (ValueError, TypeError):
        return False, "قيمة الأيام المدخلة غير صحيحة"
    
    username = entity['username']
    now = get_db_storage_now().replace(tzinfo=None)
    current_exp = entity.get('expires_at')
    
    start_dt = now
    if current_exp and str(current_exp).strip() not in ['', 'None', 'غير محدد']:
        try:
            exp_dt = datetime.datetime.strptime(str(current_exp).split('.')[0].strip(), '%Y-%m-%d %H:%M:%S')
            if exp_dt > now:
                start_dt = exp_dt
        except Exception:
            start_dt = now
            
    new_exp_dt = start_dt + datetime.timedelta(days=days)
    new_exp_iso = new_exp_dt.strftime('%Y-%m-%d %H:%M:%S')
    new_freeradius_exp = new_exp_dt.strftime('%d %b %Y %H:%M:%S')
    
    if etype == 'subscriber':
        execute_write("UPDATE wisp_subscribers SET expires_at = ?, status = 'active' WHERE id = ?", (new_exp_iso, entity['id']))
    else:
        execute_write("UPDATE wisp_vouchers SET expires_at = ?, status = 'active', expire_reason = '' WHERE id = ?", (new_exp_iso, entity['id']))
        
    execute_write("DELETE FROM radcheck WHERE LOWER(username) = LOWER(?) AND attribute = 'Expiration'", (username,))
    execute_write("INSERT INTO radcheck (username, attribute, op, value) VALUES (?, 'Expiration', ':=', ?)", (username, new_freeradius_exp))
    
    # Ensure active password in radcheck
    user_pwd = entity.get('password') or entity.get('pin_code') or username
    execute_write("DELETE FROM radcheck WHERE LOWER(username) = LOWER(?) AND (attribute = 'Cleartext-Password' OR (attribute = 'Auth-Type' AND value = 'Reject'))", (username,))
    execute_write("INSERT INTO radcheck (username, attribute, op, value) VALUES (?, 'Cleartext-Password', ':=', ?)", (username, user_pwd))
    
    log_audit(1, admin_username, 'EXTEND_VALIDITY', etype, f'Extended validity by {days} days for {username}. New expiry: {new_exp_iso}')
    return True, f"تم تمديد الصلاحية بنجاح بمقدار {days} يوم. تاريخ الانتهاء الجديد: {new_exp_iso}"

def action_terminate_subscription(entity_type, entity_id, admin_username='admin'):
    """2. إنهاء الاشتراك فوراً وطرده من ميكروتيك"""
    entity, etype = get_target_entity(entity_type, entity_id)
    if not entity:
        return False, "الحساب أو الكرت غير موجود"
        
    username = entity['username']
    now = get_db_storage_now().replace(tzinfo=None)
    now_iso = now.strftime('%Y-%m-%d %H:%M:%S')
    now_fr = now.strftime('%d %b %Y %H:%M:%S')
    
    if etype == 'subscriber':
        execute_write("UPDATE wisp_subscribers SET expires_at = ?, status = 'expired', extra_quota_mb = 0 WHERE id = ?", (now_iso, entity['id']))
    else:
        execute_write("UPDATE wisp_vouchers SET expires_at = ?, status = 'expired', expire_reason = 'تم إنهاء الاشتراك يدوياً بواسطة الإدارة', extra_quota_mb = 0 WHERE id = ?", (now_iso, entity['id']))
        
    execute_write("DELETE FROM radcheck WHERE LOWER(username) = LOWER(?) AND attribute = 'Expiration'", (username,))
    execute_write("INSERT INTO radcheck (username, attribute, op, value) VALUES (?, 'Expiration', ':=', ?)", (username, now_fr))
    
    # Send CoA Disconnect
    action_disconnect_user(entity_type, entity_id, admin_username=admin_username)
    
    log_audit(1, admin_username, 'TERMINATE_SUBSCRIPTION', etype, f'Terminated subscription for {username}')
    return True, "تم إنهاء الاشتراك فوراً، تصفير الرصيد المتبقي، وفصل المشترك من الميكروتيك."

def action_renew_package(entity_type, entity_id, admin_username='admin'):
    """3. تجديد الباقة الحالية مع دعم ميزة ترحيل الرصيد (Data & Time Rollover) وتسوية السلفة"""
    with db_session() as conn:
        cursor = conn.cursor()
        lock_clause = "FOR UPDATE" if is_mysql_conn(conn) else ""

        entity_resolved, etype = get_target_entity(entity_type, entity_id)
        if not entity_resolved:
            return False, "الحساب أو الكرت غير موجود"

        target_table = "wisp_subscribers" if etype == 'subscriber' else "wisp_vouchers"
        sql_ent = adapt_query(f"SELECT * FROM {target_table} WHERE id = ? LIMIT 1 {lock_clause}", conn)
        cursor.execute(sql_ent, (entity_resolved['id'],))
        entity = cursor.fetchone()
        if not entity:
            return False, "الحساب أو الكرت غير موجود"
        if not isinstance(entity, dict):
            entity = dict(entity)

        username = entity['username']
        pkg_id = entity['package_id']
        sql_pkg = adapt_query("SELECT * FROM wisp_packages WHERE id = ? LIMIT 1", conn)
        cursor.execute(sql_pkg, (pkg_id,))
        pkg = cursor.fetchone()
        if not pkg:
            return False, "باقة المشترك غير موجودة"
        if not isinstance(pkg, dict):
            pkg = dict(pkg)

        # Single DB timestamp for renewal baseline and last_renewed_at (Defect 3 & 11)
        cursor.execute("SELECT CURRENT_TIMESTAMP")
        r_now = cursor.fetchone()
        db_now = r_now[0] if not isinstance(r_now, dict) else list(r_now.values())[0]
        if isinstance(db_now, str):
            db_now_dt = datetime.datetime.strptime(db_now.split('.')[0].strip(), '%Y-%m-%d %H:%M:%S')
        else:
            db_now_dt = db_now
        db_now_str = db_now_dt.strftime('%Y-%m-%d %H:%M:%S')

        is_rollover_enabled = bool(pkg.get('is_rollover_enabled'))

        # 1. Defect 1: Calculate cycle consumption & rollover BEFORE updating session baselines
        try:
            calc = calculate_cycle_usage_and_rollover(entity, pkg, is_rollover_enabled=is_rollover_enabled, conn=conn, now=db_now_dt)
            rem_data_mb = calc['rem_data_mb']
            rem_time_delta = calc['rem_time_delta']
            rem_days = calc['rem_days']
            rem_hours = calc['rem_hours']
        except Exception as e:
            conn.rollback()
            return False, f"تعذر قراءة استهلاك المشترك من سجلات المحاسبة، تم إيقاف عملية التجديد بأمان: {e}"

        # 2. Record session baselines for active sessions using the single DB timestamp
        record_session_baselines(username, renewed_at=db_now_str, conn=conn)

        # 3. Defect 7: Full loan settlement
        loan_mb = int(entity.get('loan_balance_mb') or 0)
        has_active_loan = (int(entity.get('loan_status') or 0) == 1 or loan_mb > 0)
        new_base_quota_mb = int(entity.get('snap_volume_quota_mb') if etype == 'voucher' and entity.get('snap_volume_quota_mb') is not None else (pkg.get('volume_quota_mb') or 0))
        is_unlimited_quota = (new_base_quota_mb == 0)

        if is_rollover_enabled:
            new_extra_mb = float(rem_data_mb)
        else:
            new_extra_mb = 0.0

        new_loan_balance_mb = 0
        new_loan_status = 0
        deducted_loan_mb = 0

        if has_active_loan:
            if is_unlimited_quota:
                deducted_loan_mb = loan_mb
                new_loan_balance_mb = 0
                new_loan_status = 0
            else:
                total_capacity = new_base_quota_mb + new_extra_mb
                if total_capacity >= loan_mb:
                    deducted_loan_mb = loan_mb
                    if new_extra_mb >= loan_mb:
                        new_extra_mb -= loan_mb
                    else:
                        shortfall = loan_mb - new_extra_mb
                        new_extra_mb = -shortfall
                    new_loan_balance_mb = 0
                    new_loan_status = 0
                else:
                    deducted_loan_mb = total_capacity
                    unsettled_loan = loan_mb - total_capacity
                    new_extra_mb = -new_base_quota_mb
                    new_loan_balance_mb = int(unsettled_loan)
                    new_loan_status = 1

        # 4. Defect 8: Validity calculation (single add of remaining time)
        val = int(pkg.get('validity_value') if pkg.get('validity_value') is not None else (pkg.get('validity_days') or 30))
        unit = pkg.get('validity_unit') or 'days'
        
        if val <= 0:
            new_exp_iso = None
            new_fr_exp = None
        else:
            if unit == 'hours':
                base_delta = datetime.timedelta(hours=val)
            elif unit == 'minutes':
                base_delta = datetime.timedelta(minutes=val)
            elif unit == 'months':
                base_delta = datetime.timedelta(days=val * 30)
            else:
                base_delta = datetime.timedelta(days=val)
                
            new_exp_dt = db_now_dt + base_delta + rem_time_delta
            new_exp_iso = new_exp_dt.strftime('%Y-%m-%d %H:%M:%S')
            new_fr_exp = new_exp_dt.strftime('%d %b %Y %H:%M:%S')

        # 5. Database updates
        if etype == 'subscriber':
            upd_sub = adapt_query("""
                UPDATE wisp_subscribers
                SET snap_volume_quota_mb = NULL, expires_at = ?,
                    last_renewed_at = ?,
                    extra_quota_mb = ?,
                    loan_balance_mb = ?,
                    loan_status = ?,
                    status = 'active'
                WHERE id = ?
            """, conn)
            cursor.execute(upd_sub, (new_exp_iso, db_now_str, new_extra_mb, new_loan_balance_mb, new_loan_status, entity['id']))

            inv_num = generate_invoice_number(entity['id'])
            ins_inv = adapt_query("""
                INSERT INTO wisp_invoices (invoice_number, subscriber_id, subscriber_name, amount, status, package_name, notes, paid_at)
                VALUES (?, ?, ?, ?, 'paid', ?, 'تجديد باقة يدوي من لوحة التحكم', ?)
            """, conn)
            cursor.execute(ins_inv, (inv_num, entity['id'], entity.get('full_name') or username, pkg['price'], pkg['name'], db_now_str))
        else:
            snap_quota = entity.get('snap_volume_quota_mb') if entity.get('snap_volume_quota_mb') is not None else int(pkg.get('volume_quota_mb') or 0)
            snap_uptime = entity.get('snap_uptime_limit_mins') if entity.get('snap_uptime_limit_mins') is not None else int(pkg.get('uptime_limit_mins') or 0)
            upd_vouch = adapt_query("""
                UPDATE wisp_vouchers
                SET expires_at = ?,
                    last_renewed_at = ?,
                    extra_quota_mb = ?,
                    status = 'active',
                    expire_reason = '',
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
                WHERE id = ?
            """, conn)
            cursor.execute(upd_vouch, (
                new_exp_iso, db_now_str, new_extra_mb,
                float(pkg.get('price') or 0.0), float(pkg.get('cost') or 0.0), int(snap_quota),
                int(snap_uptime),
                val,
                pkg.get('validity_unit') or 'days', int(pkg.get('validity_days') or 30),
                pkg.get('rate_download') or '', pkg.get('rate_upload') or '',
                pkg.get('rate_limit_str') or '',
                int(pkg.get('simultaneous_sessions') or 1),
                pkg.get('mikrotik_group') or '',
                entity['id']
            ))

            ins_sale = adapt_query("""
                INSERT INTO wisp_voucher_sales (
                    voucher_id, batch_id, batch_name, username, serial_number,
                    package_name, price, cost, reseller_id, activated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, conn)
            cursor.execute(ins_sale, (
                entity['id'], entity.get('batch_id') or 1, entity.get('batch_name') or 'Direct',
                entity['username'], entity.get('serial_number') or '',
                pkg['name'], pkg['price'], pkg.get('cost') or 0, entity.get('reseller_id'), db_now_str
            ))

        # 6. RADIUS attributes
        cursor.execute(adapt_query("DELETE FROM radcheck WHERE LOWER(username) = LOWER(?) AND attribute = 'Expiration'", conn), (username,))
        if new_fr_exp:
            cursor.execute(adapt_query("INSERT INTO radcheck (username, attribute, op, value) VALUES (?, 'Expiration', ':=', ?)", conn), (username, new_fr_exp))

        user_pwd = entity.get('password') or entity.get('pin_code') or username
        cursor.execute(adapt_query("DELETE FROM radcheck WHERE LOWER(username) = LOWER(?) AND (attribute = 'Cleartext-Password' OR (attribute = 'Auth-Type' AND value = 'Reject'))", conn), (username,))
        cursor.execute(adapt_query("INSERT INTO radcheck (username, attribute, op, value) VALUES (?, 'Cleartext-Password', ':=', ?)", conn), (username, user_pwd))

        cursor.execute(adapt_query("DELETE FROM radcheck WHERE LOWER(username) = LOWER(?) AND attribute = 'Max-Total-Octets'", conn), (username,))
        cursor.execute(adapt_query("DELETE FROM radusergroup WHERE LOWER(username) = LOWER(?)", conn), (username,))
        cursor.execute(adapt_query("INSERT INTO radusergroup (username, groupname, priority) VALUES (?, ?, 1)", conn), (username, pkg['name']))

    # Outside transaction: Disconnect & Audit
    action_disconnect_user(entity_type, entity_id, admin_username=admin_username)

    rolled_gb = round(rem_data_mb / 1024.0, 2)
    rollover_parts = []
    if rolled_gb > 0:
        gb_str = f"{int(rolled_gb)}" if rolled_gb.is_integer() else f"{rolled_gb:.2f}"
        rollover_parts.append(f"{gb_str} جيجابايت")
    if rem_days > 0:
        rollover_parts.append(f"{rem_days} {'أيام' if 3 <= rem_days <= 10 else 'يوم'}")
    elif rem_hours > 0:
        rollover_parts.append(f"{rem_hours} {'ساعات' if 3 <= rem_hours <= 10 else 'ساعة'}")

    loan_text = f" (تم سداد سلفة {format_mb_or_gb(deducted_loan_mb)})" if deducted_loan_mb > 0 else ""
    if is_rollover_enabled and rollover_parts:
        rollover_text = " و ".join(rollover_parts)
        res_msg = f"تم التجديد بنجاح! تم ترحيل {rollover_text} إلى رصيدك الجديد{loan_text}."
        audit_change = f"تم تجديد الباقة مع ترحيل الرصيد ({rollover_text}){loan_text}"
    else:
        res_msg = f"تم تجديد باقة ({pkg['name']}) بنجاح للمشترك{loan_text} وفصل الجلسة لتطبيق الإعدادات الجديدة."
        audit_change = f"تجديد باقة {pkg['name']}{loan_text}"

    log_user_audit(etype, entity['id'], username, admin_username, 'RENEW_PACKAGE', audit_change)
    log_audit(1, admin_username, 'RENEW_PACKAGE', etype, f'Renewed package {pkg["name"]} for {username}: {audit_change}')
    return True, res_msg


def action_change_package(entity_type, entity_id, new_package_id, enable_rollover=None, admin_username='admin'):
    """4. تغيير الباقة مع خيار ترحيل الرصيد الذكي (Data & Time Rollover) وتسوية السلفة"""
    with db_session() as conn:
        cursor = conn.cursor()
        lock_clause = "FOR UPDATE" if is_mysql_conn(conn) else ""

        entity_resolved, etype = get_target_entity(entity_type, entity_id)
        if not entity_resolved:
            return False, "الحساب أو الكرت غير موجود"

        target_table = "wisp_subscribers" if etype == 'subscriber' else "wisp_vouchers"
        sql_ent = adapt_query(f"SELECT * FROM {target_table} WHERE id = ? LIMIT 1 {lock_clause}", conn)
        cursor.execute(sql_ent, (entity_resolved['id'],))
        entity = cursor.fetchone()
        if not entity:
            return False, "الحساب أو الكرت غير موجود"
        if not isinstance(entity, dict):
            entity = dict(entity)

        cursor.execute(adapt_query("SELECT * FROM wisp_packages WHERE id = ?", conn), (new_package_id,))
        new_pkg = cursor.fetchone()
        if not new_pkg:
            return False, "الباقة الجديدة المحددة غير موجودة"
        if not isinstance(new_pkg, dict):
            new_pkg = dict(new_pkg)

        # Defect 6: Retrieve subscriber's current / old package to calculate rollover from!
        old_pkg_id = entity.get('package_id')
        old_pkg = None
        if old_pkg_id:
            cursor.execute(adapt_query("SELECT * FROM wisp_packages WHERE id = ?", conn), (old_pkg_id,))
            old_pkg = cursor.fetchone()
            if old_pkg and not isinstance(old_pkg, dict):
                old_pkg = dict(old_pkg)
        if not old_pkg:
            old_pkg = new_pkg

        if enable_rollover is None:
            enable_rollover = bool(old_pkg.get('is_rollover_enabled'))
        else:
            enable_rollover = bool(enable_rollover)

        username = entity['username']

        # Single DB timestamp for renewal baseline and last_renewed_at (Defect 3 & 11)
        cursor.execute("SELECT CURRENT_TIMESTAMP")
        r_now = cursor.fetchone()
        db_now = r_now[0] if not isinstance(r_now, dict) else list(r_now.values())[0]
        if isinstance(db_now, str):
            db_now_dt = datetime.datetime.strptime(db_now.split('.')[0].strip(), '%Y-%m-%d %H:%M:%S')
        else:
            db_now_dt = db_now
        db_now_str = db_now_dt.strftime('%Y-%m-%d %H:%M:%S')

        # 1. Defect 1 & 6: Calculate cycle consumption & rollover from OLD package BEFORE recording baselines
        try:
            calc = calculate_cycle_usage_and_rollover(entity, old_pkg, is_rollover_enabled=enable_rollover, conn=conn, now=db_now_dt)
            rem_data_mb = calc['rem_data_mb']
            rem_time_delta = calc['rem_time_delta']
            rem_days = calc['rem_days']
            rem_hours = calc['rem_hours']
        except Exception as e:
            conn.rollback()
            return False, f"تعذر قراءة استهلاك المشترك من سجلات المحاسبة، تم إيقاف عملية تغيير الباقة بأمان: {e}"

        # 2. Record session baselines for active sessions
        record_session_baselines(username, renewed_at=db_now_str, conn=conn)

        # 3. Defect 7: Comprehensive loan settlement against new package capacity
        loan_mb = int(entity.get('loan_balance_mb') or 0)
        has_active_loan = (int(entity.get('loan_status') or 0) == 1 or loan_mb > 0)
        new_base_quota_mb = int(new_pkg.get('volume_quota_mb') or 0)
        is_unlimited_quota = (new_base_quota_mb == 0)

        if enable_rollover:
            new_extra_mb = float(rem_data_mb)
        else:
            new_extra_mb = 0.0

        new_loan_balance_mb = 0
        new_loan_status = 0
        deducted_loan_mb = 0

        if has_active_loan:
            if is_unlimited_quota:
                deducted_loan_mb = loan_mb
                new_loan_balance_mb = 0
                new_loan_status = 0
            else:
                total_capacity = new_base_quota_mb + new_extra_mb
                if total_capacity >= loan_mb:
                    deducted_loan_mb = loan_mb
                    if new_extra_mb >= loan_mb:
                        new_extra_mb -= loan_mb
                    else:
                        shortfall = loan_mb - new_extra_mb
                        new_extra_mb = -shortfall
                    new_loan_balance_mb = 0
                    new_loan_status = 0
                else:
                    deducted_loan_mb = total_capacity
                    unsettled_loan = loan_mb - total_capacity
                    new_extra_mb = -new_base_quota_mb
                    new_loan_balance_mb = int(unsettled_loan)
                    new_loan_status = 1

        # 4. Defect 8: Validity calculation
        val = new_pkg.get('validity_value') if new_pkg.get('validity_value') is not None else (new_pkg.get('validity_days') or 30)
        unit = new_pkg.get('validity_unit') or 'days'
        
        if val <= 0:
            new_exp_iso = None
            new_fr_exp = None
        else:
            if unit == 'hours':
                base_delta = datetime.timedelta(hours=val)
            elif unit == 'minutes':
                base_delta = datetime.timedelta(minutes=val)
            elif unit == 'months':
                base_delta = datetime.timedelta(days=val * 30)
            else:
                base_delta = datetime.timedelta(days=val)
                
            new_exp_dt = db_now_dt + base_delta + rem_time_delta
            new_exp_iso = new_exp_dt.strftime('%Y-%m-%d %H:%M:%S')
            new_fr_exp = new_exp_dt.strftime('%d %b %Y %H:%M:%S')

        # 5. Database updates
        if etype == 'subscriber':
            upd_sub = adapt_query("""
                UPDATE wisp_subscribers
                SET snap_volume_quota_mb = NULL, package_id = ?, expires_at = ?, last_renewed_at = ?, 
                    extra_quota_mb = ?, loan_balance_mb = ?, loan_status = ?, status = 'active'
                WHERE id = ?
            """, conn)
            cursor.execute(upd_sub, (new_pkg['id'], new_exp_iso, db_now_str, new_extra_mb, new_loan_balance_mb, new_loan_status, entity['id']))

            inv_num = generate_invoice_number(entity['id'])
            ins_inv = adapt_query("""
                INSERT INTO wisp_invoices (invoice_number, subscriber_id, subscriber_name, amount, status, package_name, notes, paid_at)
                VALUES (?, ?, ?, ?, 'paid', ?, 'تغيير باقة وترقية من لوحة التحكم', ?)
            """, conn)
            cursor.execute(ins_inv, (inv_num, entity['id'], entity.get('full_name') or username, new_pkg['price'], new_pkg['name'], db_now_str))
        else:
            upd_vouch = adapt_query("""
                UPDATE wisp_vouchers
                SET package_id = ?,
                    expires_at = ?,
                    last_renewed_at = ?,
                    extra_quota_mb = ?,
                    status = 'active',
                    expire_reason = '',
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
                WHERE id = ?
            """, conn)
            cursor.execute(upd_vouch, (
                new_pkg['id'], new_exp_iso, db_now_str, new_extra_mb,
                float(new_pkg.get('price') or 0.0), float(new_pkg.get('cost') or 0.0), int(new_pkg.get('volume_quota_mb') or 0),
                int(new_pkg.get('uptime_limit_mins') or 0),
                int(val),
                unit, int(new_pkg.get('validity_days') or 30),
                new_pkg.get('rate_download') or '', new_pkg.get('rate_upload') or '',
                new_pkg.get('rate_limit_str') or '',
                int(new_pkg.get('simultaneous_sessions') or 1),
                new_pkg.get('mikrotik_group') or '',
                entity['id']
            ))

            ins_sale = adapt_query("""
                INSERT INTO wisp_voucher_sales (
                    voucher_id, batch_id, batch_name, username, serial_number,
                    package_name, price, cost, reseller_id, activated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, conn)
            cursor.execute(ins_sale, (
                entity['id'], entity.get('batch_id') or 1, entity.get('batch_name') or 'Direct',
                entity['username'], entity.get('serial_number') or '',
                new_pkg['name'], new_pkg['price'], new_pkg.get('cost') or 0, entity.get('reseller_id'), db_now_str
            ))

        # 6. RADIUS attributes
        cursor.execute(adapt_query("DELETE FROM radcheck WHERE LOWER(username) = LOWER(?) AND attribute = 'Expiration'", conn), (username,))
        if new_fr_exp:
            cursor.execute(adapt_query("INSERT INTO radcheck (username, attribute, op, value) VALUES (?, 'Expiration', ':=', ?)", conn), (username, new_fr_exp))

        user_pwd = entity.get('password') or entity.get('pin_code') or username
        cursor.execute(adapt_query("DELETE FROM radcheck WHERE LOWER(username) = LOWER(?) AND (attribute = 'Cleartext-Password' OR (attribute = 'Auth-Type' AND value = 'Reject'))", conn), (username,))
        cursor.execute(adapt_query("INSERT INTO radcheck (username, attribute, op, value) VALUES (?, 'Cleartext-Password', ':=', ?)", conn), (username, user_pwd))

        cursor.execute(adapt_query("DELETE FROM radcheck WHERE LOWER(username) = LOWER(?) AND attribute = 'Max-Total-Octets'", conn), (username,))
        cursor.execute(adapt_query("DELETE FROM radusergroup WHERE LOWER(username) = LOWER(?)", conn), (username,))
        cursor.execute(adapt_query("INSERT INTO radusergroup (username, groupname, priority) VALUES (?, ?, 1)", conn), (username, new_pkg['name']))

    # Outside transaction: Disconnect & Audit
    action_disconnect_user(entity_type, entity_id, admin_username=admin_username)

    rolled_gb = round(rem_data_mb / 1024.0, 2)
    rollover_parts = []
    if rolled_gb > 0:
        gb_str = f"{int(rolled_gb)}" if rolled_gb.is_integer() else f"{rolled_gb:.2f}"
        rollover_parts.append(f"{gb_str} جيجابايت")
    if rem_days > 0:
        rollover_parts.append(f"{rem_days} {'أيام' if 3 <= rem_days <= 10 else 'يوم'}")
    elif rem_hours > 0:
        rollover_parts.append(f"{rem_hours} {'ساعات' if 3 <= rem_hours <= 10 else 'ساعة'}")

    loan_text = f" (تم سداد سلفة {format_mb_or_gb(deducted_loan_mb)})" if deducted_loan_mb > 0 else ""
    if enable_rollover and rollover_parts:
        rollover_text = " و ".join(rollover_parts)
        res_msg = f"تم ترقية/تغيير الباقة إلى ({new_pkg['name']}) بنجاح! تم ترحيل {rollover_text} إلى رصيدك الجديد{loan_text}."
        audit_note = f"ترقية باقة إلى {new_pkg['name']} مع ترحيل الرصيد ({rollover_text}){loan_text}"
    else:
        res_msg = f"تم تغيير الباقة إلى ({new_pkg['name']}) بنجاح للمشترك{loan_text} وتطبيق الإعدادات الجديدة فوراً."
        audit_note = f"تغيير باقة إلى {new_pkg['name']}{loan_text}"

    log_user_audit(etype, entity['id'], username, admin_username, 'CHANGE_PACKAGE', audit_note)
    log_audit(1, admin_username, 'CHANGE_PACKAGE', etype, f'Changed package to {new_pkg["name"]} for {username}: {audit_note}')
    return True, res_msg

def action_add_quota(entity_type, entity_id, quota_amount, quota_unit='GB', admin_username='admin'):
    """5. إضافة رصيد تحميل (Data Quota)"""
    entity, etype = get_target_entity(entity_type, entity_id)
    if not entity:
        return False, "الحساب أو الكرت غير موجود"
        
    try:
        val = float(quota_amount)
        if val <= 0:
            return False, "يرجى إدخال سعة بيانات صحيحة أكبر من 0"
    except (ValueError, TypeError):
        return False, "قيمة السعة المدخلة غير صحيحة"
        
    mb_val = round(val * 1024.0, 2) if str(quota_unit).upper() == 'GB' else round(val, 2)
    
    if etype == 'subscriber':
        execute_write("""
            UPDATE wisp_subscribers
            SET extra_quota_mb = COALESCE(extra_quota_mb, 0) + ?,
                status = CASE WHEN status = 'expired' THEN 'active' ELSE status END
            WHERE id = ?
        """, (mb_val, entity['id']))
    else:
        execute_write("""
            UPDATE wisp_vouchers
            SET extra_quota_mb = COALESCE(extra_quota_mb, 0) + ?,
                status = CASE WHEN status = 'expired' THEN 'active' ELSE status END,
                expire_reason = ''
            WHERE id = ?
        """, (mb_val, entity['id']))
        
    # Ensure active password in radcheck
    user_pwd = entity.get('password') or entity.get('pin_code') or entity['username']
    execute_write("DELETE FROM radcheck WHERE LOWER(username) = LOWER(?) AND attribute = 'Cleartext-Password'", (entity['username'],))
    execute_write("INSERT INTO radcheck (username, attribute, op, value) VALUES (?, 'Cleartext-Password', ':=', ?)", (entity['username'], user_pwd))
    
    # Disconnect active session so router fetches new quota limit immediately
    action_disconnect_user(entity_type, entity_id, admin_username=admin_username)
        
    log_audit(1, admin_username, 'ADD_DATA_QUOTA', etype, f'Added {val} {quota_unit} ({mb_val} MB) quota to {entity["username"]}')
    return True, f"تمت إضافة رصيد تحميل بمقدار {val} {quota_unit} ({mb_val} MB) بنجاح وتحديث جلسة المستخدم."

def action_get_usage_history(entity_type, entity_id):
    """6. استعراض سجل الاستهلاك والجلسات"""
    entity, etype = get_target_entity(entity_type, entity_id)
    if not entity:
        return False, "الحساب أو الكرت غير موجود", {}
        
    username = entity['username']
    
    # Reuse the same session display state as subscriber/card details and the portal.
    from services.subscriber_service import get_subscriber_sessions
    sessions = get_subscriber_sessions(username, limit=50)
    for s in sessions:
        s['total_traffic'] = s['total_str']
        s['status_str'] = 'متصل' if s['is_active'] else 'غير متصل'

    # Aggregated totals
    summary = query_one("""
        SELECT COUNT(DISTINCT acctsessionid) as total_sessions,
               COALESCE(SUM(max_down), 0) as total_down_bytes,
               COALESCE(SUM(max_up), 0) as total_up_bytes,
               COALESCE(SUM(max_time), 0) as total_time_sec
        FROM (
            SELECT nasipaddress, acctsessionid,
                   MAX((CAST(COALESCE(acctoutputgigawords, 0) AS UNSIGNED) * 4294967296) + CAST(COALESCE(acctoutputoctets, 0) AS UNSIGNED)) as max_down,
                   MAX((CAST(COALESCE(acctinputgigawords, 0) AS UNSIGNED) * 4294967296) + CAST(COALESCE(acctinputoctets, 0) AS UNSIGNED)) as max_up,
                   MAX(acctsessiontime) as max_time
            FROM radacct
            WHERE LOWER(username) = LOWER(?)
            GROUP BY nasipaddress, acctsessionid
        ) AS t
    """, (username,))

    
    sum_data = {
        'username': username,
        'full_name': entity.get('full_name') or username,
        'package_name': entity.get('package_name') or '-',
        'total_sessions': summary['total_sessions'] if summary else 0,
        'total_download': format_bytes(summary['total_down_bytes'] if summary else 0),
        'total_upload': format_bytes(summary['total_up_bytes'] if summary else 0),
        'total_traffic': format_bytes((summary['total_down_bytes'] or 0) + (summary['total_up_bytes'] or 0) if summary else 0),
        'total_duration': format_duration(summary['total_time_sec'] if summary else 0),
        'sessions': sessions
    }
    return True, "تم جلب سجل الاستهلاك بنجاح", sum_data

def action_add_wallet_balance(entity_type, entity_id, amount, notes='', admin_username='admin'):
    """7. إضافة رصيد مالي للمحفظة"""
    entity, etype = get_target_entity(entity_type, entity_id)
    if not entity:
        return False, "الحساب أو الكرت غير موجود"
        
    try:
        val = float(amount)
        if val <= 0:
            return False, "يرجى إدخال مبلغ مالي صحيح أكبر من 0"
    except (ValueError, TypeError):
        return False, "قيمة المبلغ المدخل غير صحيحة"
        
    username = entity['username']
    if etype == 'subscriber':
        execute_write("UPDATE wisp_subscribers SET balance = COALESCE(balance, 0) + ? WHERE id = ?", (val, entity['id']))
        inv_num = generate_invoice_number(entity['id'])
        execute_write("""
            INSERT INTO wisp_invoices (invoice_number, subscriber_id, subscriber_name, amount, status, package_name, notes, paid_at)
            VALUES (?, ?, ?, ?, 'paid', 'إيداع رصيد محفظة', ?, CURRENT_TIMESTAMP)
        """, (inv_num, entity['id'], entity.get('full_name') or username, val, notes or f'إيداع رصيد بالمحفظة بواسطة {admin_username}'))
    else:
        execute_write("UPDATE wisp_vouchers SET balance = COALESCE(balance, 0) + ? WHERE id = ?", (val, entity['id']))
        
    log_audit(1, admin_username, 'ADD_WALLET_BALANCE', etype, f'Added {val} to wallet for {username}')
    return True, f"تمت إضافة {val} إلى محفظة المشترك بنجاح."

def action_deduct_wallet_balance(entity_type, entity_id, amount, notes='', admin_username='admin'):
    """8. سحب / تصحيح رصيد مالي من المحفظة"""
    entity, etype = get_target_entity(entity_type, entity_id)
    if not entity:
        return False, "الحساب أو الكرت غير موجود"
        
    try:
        val = float(amount)
        if val <= 0:
            return False, "يرجى إدخال مبلغ مالي صحيح أكبر من 0"
    except (ValueError, TypeError):
        return False, "قيمة المبلغ المدخل غير صحيحة"
        
    curr_balance = float(entity.get('balance') or 0.0)
    if curr_balance < val:
        return False, f"الرصيد الحالي ({curr_balance}) غير كافٍ لسحب مبلغ ({val})"
        
    username = entity['username']
    if etype == 'subscriber':
        execute_write("UPDATE wisp_subscribers SET balance = balance - ? WHERE id = ?", (val, entity['id']))
        inv_num = generate_invoice_number(entity['id'])
        execute_write("""
            INSERT INTO wisp_invoices (invoice_number, subscriber_id, subscriber_name, amount, status, package_name, notes, paid_at)
            VALUES (?, ?, ?, ?, 'refunded', 'سحب/تصحيح رصيد محفظة', ?, CURRENT_TIMESTAMP)
        """, (inv_num, entity['id'], entity.get('full_name') or username, val, notes or f'سحب/تصحيح رصيد بواسطة {admin_username}'))
    else:
        execute_write("UPDATE wisp_vouchers SET balance = balance - ? WHERE id = ?", (val, entity['id']))
        
    log_audit(1, admin_username, 'DEDUCT_WALLET_BALANCE', etype, f'Deducted {val} from wallet for {username}')
    return True, f"تم خصم/سحب {val} من رصيد المحفظة بنجاح. الرصيد المتبقي: {curr_balance - val}"

def action_disconnect_user(entity_type, entity_id=None, admin_username='admin'):
    """9. قطع الاتصال وطرد المشترك عبر CoA Disconnect-Request بنمط غير متزامن فوري"""
    entity, etype = get_target_entity(entity_type, entity_id)
    if not entity:
        return False, "الحساب أو الكرت غير موجود"
        
    username = entity['username']
    
    # 1. Look up active session
    active_session = query_one("""
        SELECT nasipaddress, acctsessionid, framedipaddress, callingstationid
        FROM radacct
        WHERE LOWER(username) = LOWER(?) AND acctstoptime IS NULL
        ORDER BY radacctid DESC
        LIMIT 1
    """, (username,))
    
    if active_session:
        from services.coa_queue_service import enqueue_disconnect
        enqueue_disconnect(
            username=username,
            nas_ip=active_session.get('nasipaddress'),
            framed_ip=active_session.get('framedipaddress'),
            session_id=active_session.get('acctsessionid'),
            mac_address=active_session.get('callingstationid'),
            reason="Admin Quick Action Disconnect",
            admin_username=admin_username
        )
        log_audit(1, admin_username, 'DISCONNECT_USER', etype, f'Enqueued CoA Disconnect for active user {username} on NAS {active_session.get("nasipaddress")}.')
        return True, f"تم إرسال أمر قطع الاتصال (CoA Disconnect) للمشترك [{username}] إلى طابور المعالجة اللحظية بنجاح."
    else:
        log_audit(1, admin_username, 'DISCONNECT_USER', etype, f'Disconnect called for offline user {username}.')
        return True, f"المشترك [{username}] غير متصل حالياً (لا توجد جلسة نشطة)."
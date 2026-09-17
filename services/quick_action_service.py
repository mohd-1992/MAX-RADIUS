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
import uuid
from database.db import query_one, query_all, execute_write, log_audit, log_user_audit
from core.coa import RadiusCoaClient
from core.rate_limit import format_bytes, format_duration
from services.voucher_service import calculate_package_expiration

def generate_invoice_number(sub_id=1):
    return f"INV-{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"

def get_target_entity(entity_type, entity_id):
    """
    Find subscriber or voucher card by ID or username.
    Returns: (entity_dict, 'subscriber' or 'voucher')
    """
    if entity_type == 'subscriber':
        if isinstance(entity_id, int) or str(entity_id).isdigit():
            row = query_one("""
                SELECT s.*, p.name as package_name, p.price as package_price,
                       p.validity_value, p.validity_unit, p.volume_quota_mb
                FROM wisp_subscribers s
                JOIN wisp_packages p ON s.package_id = p.id
                WHERE s.id = ?
            """, (int(entity_id),))
        else:
            row = query_one("""
                SELECT s.*, p.name as package_name, p.price as package_price,
                       p.validity_value, p.validity_unit, p.volume_quota_mb
                FROM wisp_subscribers s
                JOIN wisp_packages p ON s.package_id = p.id
                WHERE LOWER(s.username) = LOWER(?)
            """, (str(entity_id).strip(),))
        return row, 'subscriber'
    else:  # voucher / card
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
        execute_write("DELETE FROM wisp_voucher_sales WHERE voucher_id = ?", (entity['id'],))
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
    now = datetime.datetime.now()
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
    
    log_audit(1, admin_username, 'EXTEND_VALIDITY', etype, f'Extended validity by {days} days for {username}. New expiry: {new_exp_iso}')
    return True, f"تم تمديد الصلاحية بنجاح بمقدار {days} يوم. تاريخ الانتهاء الجديد: {new_exp_iso}"

def action_terminate_subscription(entity_type, entity_id, admin_username='admin'):
    """2. إنهاء الاشتراك فوراً وطرده من ميكروتيك"""
    entity, etype = get_target_entity(entity_type, entity_id)
    if not entity:
        return False, "الحساب أو الكرت غير موجود"
        
    username = entity['username']
    now = datetime.datetime.now()
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
    """3. تجديد الباقة الحالية مع دعم ميزة ترحيل الرصيد (Data & Time Rollover)"""
    entity, etype = get_target_entity(entity_type, entity_id)
    if not entity:
        return False, "الحساب أو الكرت غير موجود"
        
    username = entity['username']
    pkg_id = entity['package_id']
    pkg = query_one("SELECT * FROM wisp_packages WHERE id = ?", (pkg_id,))
    if not pkg:
        return False, "باقة المشترك غير موجودة"

    now = datetime.datetime.now()
    is_rollover_enabled = bool(pkg.get('is_rollover_enabled'))
    
    # 1. حساب الرصيد المتبقي (البيانات والزمن) في حال تفعيل الترحيل
    rem_data_mb = 0.0
    rem_time_delta = datetime.timedelta(0)
    rem_days = 0
    rem_hours = 0

    if is_rollover_enabled:
        # أ. حساب البيانات المتبقية (Data Rollover)
        total_allowed_mb = float(pkg.get('volume_quota_mb') or 0) + float(entity.get('extra_quota_mb') or 0)
        if total_allowed_mb > 0:
            cycle_start = entity.get('last_renewed_at') or entity.get('first_used_at') or entity.get('created_at')
            usage_q = query_one("""
                SELECT COALESCE(SUM(total_in + total_out), 0) as total_bytes
                FROM (
                    SELECT nasipaddress, acctsessionid,
                           MAX(acctinputoctets) as total_in,
                           MAX(acctoutputoctets) as total_out
                    FROM radacct
                    WHERE LOWER(username) = LOWER(?)
                      AND COALESCE(acctstarttime, acctupdatetime, CURRENT_TIMESTAMP) >= ?
                    GROUP BY nasipaddress, acctsessionid
                ) t
            """, (username, str(cycle_start)))
            used_bytes = float(usage_q['total_bytes'] or 0) if usage_q else 0.0
            used_mb = used_bytes / (1024.0 * 1024.0)
            rem_data_mb = max(0.0, total_allowed_mb - used_mb)

        # ب. حساب الأيام/الساعات المتبقية (Time Rollover)
        if entity.get('expires_at'):
            try:
                exp_str = str(entity['expires_at']).replace('T', ' ').split('.')[0]
                exp_dt = datetime.datetime.strptime(exp_str, '%Y-%m-%d %H:%M:%S')
                if exp_dt > now:
                    rem_time_delta = exp_dt - now
                    rem_days = int(rem_time_delta.total_seconds() // 86400)
                    rem_hours = int((rem_time_delta.total_seconds() % 86400) // 3600)
            except Exception:
                pass

    # 2. حساب تاريخ الانتهاء الجديد (مع إضافة الوقت المتبقي إن وجد)
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
            
        new_exp_dt = now + base_delta + rem_time_delta
        new_exp_iso = new_exp_dt.strftime('%Y-%m-%d %H:%M:%S')
        new_fr_exp = new_exp_dt.strftime('%d %b %Y %H:%M:%S')

    new_extra_mb = round(rem_data_mb, 2) if is_rollover_enabled else 0.0

    # 3. تحديث السجلات وتصفير عداد الدورة في قاعدة البيانات
    if etype == 'subscriber':
        execute_write("""
            UPDATE wisp_subscribers
            SET expires_at = ?,
                last_renewed_at = CURRENT_TIMESTAMP,
                extra_quota_mb = ?,
                status = 'active'
            WHERE id = ?
        """, (new_exp_iso, new_extra_mb, entity['id']))
        
        inv_num = generate_invoice_number(entity['id'])
        execute_write("""
            INSERT INTO wisp_invoices (invoice_number, subscriber_id, subscriber_name, amount, status, package_name, notes, paid_at)
            VALUES (?, ?, ?, ?, 'paid', ?, 'تجديد باقة يدوي من لوحة التحكم', CURRENT_TIMESTAMP)
        """, (inv_num, entity['id'], entity.get('full_name') or username, pkg['price'], pkg['name']))
    else:
        execute_write("""
            UPDATE wisp_vouchers
            SET expires_at = ?,
                last_renewed_at = CURRENT_TIMESTAMP,
                extra_quota_mb = ?,
                status = 'active',
                expire_reason = ''
            WHERE id = ?
        """, (new_exp_iso, new_extra_mb, entity['id']))
        
        execute_write("""
            INSERT INTO wisp_voucher_sales (
                voucher_id, batch_id, batch_name, username, serial_number,
                package_name, price, cost, reseller_id, activated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (
            entity['id'], entity.get('batch_id') or 1, entity.get('batch_name') or 'Direct',
            entity['username'], entity.get('serial_number') or '',
            pkg['name'], pkg['price'], pkg.get('cost') or 0, entity.get('reseller_id')
        ))
        
    # 4. تحديث سمات FreeRADIUS في radcheck
    execute_write("DELETE FROM radcheck WHERE LOWER(username) = LOWER(?) AND attribute = 'Expiration'", (username,))
    if new_fr_exp:
        execute_write("INSERT INTO radcheck (username, attribute, op, value) VALUES (?, 'Expiration', ':=', ?)", (username, new_fr_exp))
        
    # تنظيف أي سمات كوتا زائدة من radcheck (تتم إدارة الكوتا ديناميكياً عبر SQL)
    execute_write("DELETE FROM radcheck WHERE LOWER(username) = LOWER(?) AND attribute = 'Max-Total-Octets'", (username,))

    # 5. فصل الجلسة لتطبيق الإعدادات والكوتا الجديدة فوراً
    action_disconnect_user(entity_type, entity_id, admin_username=admin_username)

    # 6. تجهيز رسالة التنبيه وسجل التدقيق
    rolled_gb = round(rem_data_mb / 1024.0, 2)
    rollover_parts = []
    if rolled_gb > 0:
        gb_str = f"{int(rolled_gb)}" if rolled_gb.is_integer() else f"{rolled_gb:.2f}"
        rollover_parts.append(f"{gb_str} جيجابايت")
    if rem_days > 0:
        rollover_parts.append(f"{rem_days} {'أيام' if 3 <= rem_days <= 10 else 'يوم'}")
    elif rem_hours > 0:
        rollover_parts.append(f"{rem_hours} {'ساعات' if 3 <= rem_hours <= 10 else 'ساعة'}")

    if is_rollover_enabled and rollover_parts:
        rollover_text = " و ".join(rollover_parts)
        res_msg = f"تم التجديد بنجاح! تم ترحيل {rollover_text} إلى رصيدك الجديد."
        audit_change = f"تم تجديد الباقة مع ترحيل الرصيد ({rollover_text})"
    else:
        res_msg = f"تم تجديد باقة ({pkg['name']}) بنجاح للمشترك وفصل الجلسة لتطبيق الإعدادات الجديدة."
        audit_change = f"تجديد باقة {pkg['name']}"

    log_user_audit(etype, entity['id'], username, admin_username, 'RENEW_PACKAGE', audit_change)
    log_audit(1, admin_username, 'RENEW_PACKAGE', etype, f'Renewed package {pkg["name"]} for {username}: {audit_change}')
    return True, res_msg

def action_change_package(entity_type, entity_id, new_package_id, admin_username='admin'):
    """4. تغيير الباقة"""
    entity, etype = get_target_entity(entity_type, entity_id)
    if not entity:
        return False, "الحساب أو الكرت غير موجود"
        
    new_pkg = query_one("SELECT * FROM wisp_packages WHERE id = ?", (new_package_id,))
    if not new_pkg:
        return False, "الباقة الجديدة المحددة غير موجودة"
        
    username = entity['username']
    val = new_pkg.get('validity_value') if new_pkg.get('validity_value') is not None else (new_pkg.get('validity_days') or 30)
    unit = new_pkg.get('validity_unit') or 'days'
    new_exp_iso, new_fr_exp = calculate_package_expiration(val, unit)
    
    if etype == 'subscriber':
        execute_write("""
            UPDATE wisp_subscribers
            SET package_id = ?, expires_at = ?, last_renewed_at = CURRENT_TIMESTAMP, status = 'active'
            WHERE id = ?
        """, (new_pkg['id'], new_exp_iso, entity['id']))
        # Record invoice
        inv_num = generate_invoice_number(entity['id'])
        execute_write("""
            INSERT INTO wisp_invoices (invoice_number, subscriber_id, subscriber_name, amount, status, package_name, notes, paid_at)
            VALUES (?, ?, ?, ?, 'paid', ?, 'تغيير باقة وترقية من لوحة التحكم', CURRENT_TIMESTAMP)
        """, (inv_num, entity['id'], entity.get('full_name') or username, new_pkg['price'], new_pkg['name']))
    else:
        execute_write("""
            UPDATE wisp_vouchers
            SET package_id = ?, expires_at = ?, last_renewed_at = CURRENT_TIMESTAMP, status = 'active', expire_reason = ''
            WHERE id = ?
        """, (new_pkg['id'], new_exp_iso, entity['id']))
        
    # Update FreeRADIUS radusergroup
    execute_write("DELETE FROM radusergroup WHERE LOWER(username) = LOWER(?)", (username,))
    execute_write("INSERT INTO radusergroup (username, groupname, priority) VALUES (?, ?, 1)", (username, new_pkg['name']))
    
    # Update Expiration in radcheck
    execute_write("DELETE FROM radcheck WHERE LOWER(username) = LOWER(?) AND attribute = 'Expiration'", (username,))
    if new_fr_exp:
        execute_write("INSERT INTO radcheck (username, attribute, op, value) VALUES (?, 'Expiration', ':=', ?)", (username, new_fr_exp))
        
    action_disconnect_user(entity_type, entity_id, admin_username=admin_username)
    log_audit(1, admin_username, 'CHANGE_PACKAGE', etype, f'Changed package to {new_pkg["name"]} for {username}')
    return True, f"تم تغيير الباقة إلى ({new_pkg['name']}) بنجاح ومزامنة FreeRADIUS."

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
        
    log_audit(1, admin_username, 'ADD_DATA_QUOTA', etype, f'Added {val} {quota_unit} ({mb_val} MB) quota to {entity["username"]}')
    return True, f"تمت إضافة رصيد تحميل بمقدار {val} {quota_unit} ({mb_val} MB) بنجاح."

def action_get_usage_history(entity_type, entity_id):
    """6. استعراض سجل الاستهلاك والجلسات"""
    entity, etype = get_target_entity(entity_type, entity_id)
    if not entity:
        return False, "الحساب أو الكرت غير موجود", {}
        
    username = entity['username']
    
    # Sessions list
    sessions = query_all("""
        SELECT radacctid, acctsessionid, framedipaddress, nasipaddress, callingstationid,
               acctstarttime, acctstoptime, acctsessiontime,
               acctinputoctets, acctoutputoctets, acctterminatecause
        FROM radacct
        WHERE LOWER(username) = LOWER(?)
        ORDER BY radacctid DESC
        LIMIT 50
    """, (username,))
    
    for s in sessions:
        s['download_str'] = format_bytes(s['acctoutputoctets'] or 0)
        s['upload_str'] = format_bytes(s['acctinputoctets'] or 0)
        s['total_traffic'] = format_bytes((s['acctinputoctets'] or 0) + (s['acctoutputoctets'] or 0))
        s['duration_str'] = format_duration(s['acctsessiontime'] or 0)
        s['status_str'] = 'متصل حالياً (Active)' if not s['acctstoptime'] else (s['acctterminatecause'] or 'مكتملة')
        
    # Aggregated totals
    summary = query_one("""
        SELECT COUNT(DISTINCT acctsessionid) as total_sessions,
               COALESCE(SUM(max_down), 0) as total_down_bytes,
               COALESCE(SUM(max_up), 0) as total_up_bytes,
               COALESCE(SUM(max_time), 0) as total_time_sec
        FROM (
            SELECT nasipaddress, acctsessionid,
                   MAX(acctoutputoctets) as max_down,
                   MAX(acctinputoctets) as max_up,
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

def action_disconnect_user(entity_type, entity_id, admin_username='admin'):
    """9. قطع الاتصال وطرد المشترك عبر CoA Disconnect-Request أو MikroTik API"""
    entity, etype = get_target_entity(entity_type, entity_id)
    if not entity:
        return False, "الحساب أو الكرت غير موجود"
        
    username = entity['username']
    
    from services.subscriber_service import disconnect_subscriber_session
    res = disconnect_subscriber_session(username, admin_username=admin_username)
    
    if res.get('success'):
        msg = res.get('message') or f"تم قطع اتصال وطرد المشترك [{username}] بنجاح."
        return True, msg
    else:
        status = res.get('status')
        if status == 'no_active_session':
            return True, f"المشترك [{username}] غير متصل حالياً (لا توجد جلسة نشطة)."
        else:
            return False, res.get('message') or f"فشل قطع اتصال المشترك [{username}]."
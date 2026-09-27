# -*- coding: utf-8 -*-
"""
services/quota_service.py
-------------------------
Unified Quota & Validity Calculation Engine (Core Architecture Pillar 1).

Single Source of Truth for:
- FreeRADIUS Accounting Aggregation (Gigawords + Octets).
- Hotspot & Scratch Cards (wisp_vouchers) quota, validity, and depletion state.
- PPPoE & Static Subscribers (wisp_subscribers) quota, validity, and depletion state.
- Dynamic Mid-Flight Top-Up (Data volume + Time extension) with CoA integration.
- High-Performance Bulk Calculation for Lists, Dashboards, and Expiry Sweeps.
"""

import datetime
from database.db import query_one, query_all, execute_write, get_connection

GIGAWORD_MULTIPLIER = 4294967296  # 2^32 bytes (RFC 2869 / RFC 5176)
BYTES_PER_MB = 1048576             # 1024 * 1024

def get_accounting_totals(username, since_timestamp=None):
    """
    Calculates exact upload, download, total bytes and uptime seconds from radacct
    using authoritative 64-bit Gigawords formula.
    
    If since_timestamp is provided, limits aggregation to sessions that were active
    or updated at or after that timestamp.
    """
    username = str(username or '').strip()
    if not username:
        return {'up_bytes': 0, 'down_bytes': 0, 'total_bytes': 0, 'uptime_secs': 0, 'sessions_count': 0}

    params = [username]
    time_filter = ""
    if since_timestamp:
        time_filter = "AND COALESCE(acctstoptime, acctupdatetime, acctstarttime, CURRENT_TIMESTAMP) >= %s"
        params.append(str(since_timestamp))

    sql = f"""
        SELECT 
            COUNT(DISTINCT acctsessionid) as sessions_count,
            COALESCE(SUM(
                (CAST(COALESCE(acctinputgigawords, 0) AS UNSIGNED) * {GIGAWORD_MULTIPLIER}) + 
                CAST(COALESCE(acctinputoctets, 0) AS UNSIGNED)
            ), 0) as up_bytes,
            COALESCE(SUM(
                (CAST(COALESCE(acctoutputgigawords, 0) AS UNSIGNED) * {GIGAWORD_MULTIPLIER}) + 
                CAST(COALESCE(acctoutputoctets, 0) AS UNSIGNED)
            ), 0) as down_bytes,
            COALESCE(SUM(acctsessiontime), 0) as uptime_secs
        FROM radacct
        WHERE username = %s {time_filter}
    """
    
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            row = cur.fetchone()
            if not row:
                return {'up_bytes': 0, 'down_bytes': 0, 'total_bytes': 0, 'uptime_secs': 0, 'sessions_count': 0}
            
            up_b = int(row.get('up_bytes') or 0)
            down_b = int(row.get('down_bytes') or 0)
            return {
                'up_bytes': up_b,
                'down_bytes': down_b,
                'total_bytes': up_b + down_b,
                'uptime_secs': int(row.get('uptime_secs') or 0),
                'sessions_count': int(row.get('sessions_count') or 0)
            }
    finally:
        conn.close()

def calculate_account_quota(username_or_id, entity_type=None):
    """
    Authoritative evaluation of quota, validity, and depletion status for any account.
    Returns comprehensive metrics dictionary.
    """
    target = str(username_or_id or '').strip()
    if not target:
        return None

    entity = None
    actual_type = entity_type

    # 1. Resolve Voucher or Subscriber record
    if actual_type in (None, 'voucher', 'card'):
        entity = query_one("""
            SELECT v.*, p.name as package_name, 
                   COALESCE(v.snap_price, p.price, 0) as resolved_price,
                   COALESCE(v.snap_volume_quota_mb, p.volume_quota_mb, 0) as base_quota_mb,
                   COALESCE(v.snap_uptime_limit_mins, p.uptime_limit_mins, 0) as base_uptime_mins,
                   COALESCE(v.snap_validity_value, p.validity_value, 0) as resolved_validity_value,
                   COALESCE(v.snap_validity_unit, p.validity_unit, 'days') as resolved_validity_unit
            FROM wisp_vouchers v
            LEFT JOIN wisp_packages p ON v.package_id = p.id
            WHERE v.id = %s OR v.username = %s OR v.pin_code = %s
            LIMIT 1
        """, (target, target, target))
        if entity:
            actual_type = 'voucher'

    if not entity and actual_type in (None, 'subscriber', 'user'):
        entity = query_one("""
            SELECT s.*, p.name as package_name,
                   COALESCE(p.price, 0) as resolved_price,
                   COALESCE(p.volume_quota_mb, 0) as base_quota_mb,
                   COALESCE(p.uptime_limit_mins, 0) as base_uptime_mins,
                   COALESCE(p.validity_value, 0) as resolved_validity_value,
                   COALESCE(p.validity_unit, 'days') as resolved_validity_unit
            FROM wisp_subscribers s
            LEFT JOIN wisp_packages p ON s.package_id = p.id
            WHERE s.id = %s OR s.username = %s
            LIMIT 1
        """, (target, target))
        if entity:
            actual_type = 'subscriber'

    if not entity:
        return None

    username = entity['username']
    last_renewed_at = entity.get('last_renewed_at')
    
    # If last_renewed_at is NULL, fallback to first_used_at or created_at for voucher
    since_ts = last_renewed_at or entity.get('first_used_at') or entity.get('created_at')

    # 2. Get authoritative accounting totals
    usage = get_accounting_totals(username, since_timestamp=since_ts)
    used_bytes = usage['total_bytes']
    used_mb = round(used_bytes / BYTES_PER_MB, 2)
    used_uptime_secs = usage['uptime_secs']
    used_uptime_mins = round(used_uptime_secs / 60, 1)

    # 3. Quota Calculations (Base + Extra)
    base_quota_mb = int(entity.get('base_quota_mb') or 0)
    extra_quota_mb = int(entity.get('extra_quota_mb') or 0)
    total_quota_mb = base_quota_mb + extra_quota_mb if base_quota_mb > 0 else 0
    total_quota_bytes = total_quota_mb * BYTES_PER_MB

    is_quota_unlimited = (total_quota_mb == 0)
    if is_quota_unlimited:
        remaining_bytes = 0
        remaining_mb = 0
        quota_used_pct = 0.0
        is_quota_depleted = False
    else:
        remaining_bytes = max(0, total_quota_bytes - used_bytes)
        remaining_mb = round(remaining_bytes / BYTES_PER_MB, 2)
        quota_used_pct = min(100.0, round((used_bytes / total_quota_bytes) * 100.0, 1)) if total_quota_bytes > 0 else 100.0
        is_quota_depleted = (used_bytes >= total_quota_bytes)

    # 4. Time/Uptime Limit Calculations
    base_uptime_mins = int(entity.get('base_uptime_mins') or 0)
    total_uptime_secs = base_uptime_mins * 60
    is_uptime_unlimited = (base_uptime_mins == 0)
    if is_uptime_unlimited:
        remaining_uptime_secs = 0
        remaining_uptime_mins = 0
        uptime_used_pct = 0.0
        is_uptime_depleted = False
    else:
        remaining_uptime_secs = max(0, total_uptime_secs - used_uptime_secs)
        remaining_uptime_mins = round(remaining_uptime_secs / 60, 1)
        uptime_used_pct = min(100.0, round((used_uptime_secs / total_uptime_secs) * 100.0, 1)) if total_uptime_secs > 0 else 100.0
        is_uptime_depleted = (used_uptime_secs >= total_uptime_secs)

    # 5. Date Expiry Calculations
    expires_at = entity.get('expires_at') or entity.get('expiration_date')
    now = datetime.datetime.now()
    is_time_expired = False
    days_remaining = None

    if expires_at:
        if isinstance(expires_at, str):
            try:
                expires_dt = datetime.datetime.strptime(expires_at, '%Y-%m-%d %H:%M:%S')
            except Exception:
                try:
                    expires_dt = datetime.datetime.strptime(expires_at[:19], '%Y-%m-%dT%H:%M:%S')
                except Exception:
                    expires_dt = None
        else:
            expires_dt = expires_at

        if expires_dt:
            is_time_expired = (now >= expires_dt)
            delta = expires_dt - now
            days_remaining = max(0, delta.days)

    # 6. Overall State Synthesis
    is_exhausted = (is_quota_depleted or is_uptime_depleted or is_time_expired)
    current_status = entity.get('status', 'active')
    
    exhaust_reason = None
    if is_quota_depleted:
        exhaust_reason = "تم استهلاك رصيد البيانات بالكامل"
    elif is_uptime_depleted:
        exhaust_reason = "تم استهلاك رصيد الوقت المسموح للباقة"
    elif is_time_expired:
        exhaust_reason = "تم انتهاء مدة صلاحية الاشتراك"

    return {
        'entity_type': actual_type,
        'entity_id': entity.get('id'),
        'username': username,
        'package_name': entity.get('package_name') or 'باقة افتراضية',
        'current_status': current_status,
        'is_active': (current_status in ('active', 'used') and not is_exhausted),
        'is_exhausted': is_exhausted,
        'exhaust_reason': exhaust_reason,
        'last_renewed_at': str(last_renewed_at) if last_renewed_at else None,
        'expires_at': str(expires_at) if expires_at else None,
        'days_remaining': days_remaining,
        
        # Traffic Metrics
        'used_up_bytes': usage['up_bytes'],
        'used_down_bytes': usage['down_bytes'],
        'used_total_bytes': used_bytes,
        'used_mb': used_mb,
        'base_quota_mb': base_quota_mb,
        'extra_quota_mb': extra_quota_mb,
        'total_quota_mb': total_quota_mb,
        'remaining_mb': remaining_mb,
        'remaining_bytes': remaining_bytes,
        'is_quota_unlimited': is_quota_unlimited,
        'is_quota_depleted': is_quota_depleted,
        'quota_used_pct': quota_used_pct,
        
        # Uptime Metrics
        'used_uptime_secs': used_uptime_secs,
        'used_uptime_mins': used_uptime_mins,
        'total_uptime_mins': base_uptime_mins,
        'remaining_uptime_mins': remaining_uptime_mins,
        'is_uptime_unlimited': is_uptime_unlimited,
        'is_uptime_depleted': is_uptime_depleted,
        'uptime_used_pct': uptime_used_pct,
        
        # Time Expiry
        'is_time_expired': is_time_expired,
        'sessions_count': usage['sessions_count']
    }

def calculate_batch_quotas(usernames, entity_type='voucher'):
    """
    High-performance bulk calculation for cards/subscribers list pages.
    Executes a single consolidated SQL query to prevent N+1 queries.
    """
    if not usernames:
        return {}

    usernames_list = [str(u).strip() for u in usernames if str(u).strip()]
    if not usernames_list:
        return {}

    placeholders = ','.join(['%s'] * len(usernames_list))
    
    # Bulk aggregation query using 64-bit Gigawords formula
    sql = f"""
        SELECT 
            username,
            COALESCE(SUM(
                (CAST(COALESCE(acctinputgigawords, 0) AS UNSIGNED) * {GIGAWORD_MULTIPLIER}) + 
                CAST(COALESCE(acctinputoctets, 0) AS UNSIGNED)
            ), 0) as up_bytes,
            COALESCE(SUM(
                (CAST(COALESCE(acctoutputgigawords, 0) AS UNSIGNED) * {GIGAWORD_MULTIPLIER}) + 
                CAST(COALESCE(acctoutputoctets, 0) AS UNSIGNED)
            ), 0) as down_bytes,
            COALESCE(SUM(acctsessiontime), 0) as uptime_secs,
            COUNT(DISTINCT acctsessionid) as sessions_count
        FROM radacct
        WHERE username IN ({placeholders})
        GROUP BY username
    """
    
    conn = get_connection()
    results = {}
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(usernames_list))
            rows = cur.fetchall()
            for r in rows:
                u = r['username']
                up_b = int(r.get('up_bytes') or 0)
                down_b = int(r.get('down_bytes') or 0)
                tot_b = up_b + down_b
                results[u] = {
                    'up_bytes': up_b,
                    'down_bytes': down_b,
                    'total_bytes': tot_b,
                    'used_mb': round(tot_b / BYTES_PER_MB, 2),
                    'uptime_secs': int(r.get('uptime_secs') or 0),
                    'uptime_mins': round(int(r.get('uptime_secs') or 0) / 60, 1),
                    'sessions_count': int(r.get('sessions_count') or 0)
                }
    finally:
        conn.close()

    # Fill default zero for usernames with no accounting yet
    for u in usernames_list:
        if u not in results:
            results[u] = {
                'up_bytes': 0, 'down_bytes': 0, 'total_bytes': 0, 'used_mb': 0.0,
                'uptime_secs': 0, 'uptime_mins': 0.0, 'sessions_count': 0
            }

    return results

def apply_midflight_topup(username_or_id, extra_mb=0, extra_days=0, admin_username='admin'):
    """
    Applies live mid-flight top-up (Data volume or Time extension) to an active or expired card/subscriber.
    - Updates extra_quota_mb and/or extends expires_at.
    - Restores status to 'active' and reinstates radcheck credentials if previously expired.
    - Enqueues asynchronous CoA / Disconnect or Speed Change to apply changes instantly.
    """
    quota_info = calculate_account_quota(username_or_id)
    if not quota_info:
        return False, "الحساب أو الكرت غير موجود.", None

    username = quota_info['username']
    entity_type = quota_info['entity_type']
    entity_id = quota_info['entity_id']

    try:
        extra_mb = max(0, int(extra_mb or 0))
        extra_days = max(0, int(extra_days or 0))
    except (ValueError, TypeError):
        return False, "قيم التجديد/الإضافة غير صالحة.", None

    if extra_mb == 0 and extra_days == 0:
        return False, "يجب تحديد سعة ميجابايت أو عدد أيام لإجراء الشحن الإضافي.", None

    table_name = "wisp_vouchers" if entity_type == 'voucher' else "wisp_subscribers"

    # Compute new expiration date if extra days given
    extend_sql = ""
    extend_params = []
    if extra_days > 0:
        extend_sql = """, expires_at = CASE 
            WHEN expires_at IS NULL OR expires_at < CURRENT_TIMESTAMP 
            THEN DATE_ADD(CURRENT_TIMESTAMP, INTERVAL %s DAY)
            ELSE DATE_ADD(expires_at, INTERVAL %s DAY)
        END """
        extend_params.extend([extra_days, extra_days])

    # Execute DB update
    update_sql = f"""
        UPDATE {table_name}
        SET extra_quota_mb = COALESCE(extra_quota_mb, 0) + %s,
            status = CASE WHEN status IN ('expired', 'used') THEN 'active' ELSE status END,
            expire_reason = ''
            {extend_sql}
        WHERE id = %s
    """
    params = [extra_mb] + extend_params + [entity_id]
    execute_write(update_sql, tuple(params))

    # Reinstate credentials in radcheck if account was restored to active
    pwd_row = query_one(f"SELECT password FROM {table_name} WHERE id = %s", (entity_id,))
    if pwd_row and pwd_row.get('password'):
        pwd = pwd_row['password']
        execute_write("""
            INSERT INTO radcheck (username, attribute, op, value) 
            VALUES (%s, 'Cleartext-Password', ':=', %s)
            ON DUPLICATE KEY UPDATE value = %s
        """, (username, pwd, pwd))

    # Trigger Pillar 3 (Asynchronous CoA) to apply quota or refresh session
    try:
        from services.coa_queue_service import enqueue_disconnect
        enqueue_disconnect(username, reason=f"Mid-flight top-up applied (+{extra_mb}MB, +{extra_days}d)")
    except Exception as coa_err:
        print(f"Warning: CoA notification after topup failed: {coa_err}")

    # Re-evaluate new quota state
    new_quota = calculate_account_quota(username, entity_type=entity_type)
    return True, f"تمت إضافة {extra_mb} MB و {extra_days} يوم بنجاح للمستخدم [{username}].", new_quota

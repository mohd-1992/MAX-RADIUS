# -*- coding: utf-8 -*-
"""
Subscriber management service: CRUD operations, MAC address binding,
accounting history, and instant Disconnect (CoA).
"""

import datetime
from core.time_service import get_db_storage_now
import concurrent.futures
from database.db import query_all, query_one, execute_write, execute_update, log_audit, log_user_audit

_COA_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="coa_bg_worker")
from core.radius_sync import sync_subscriber_to_radius, delete_user_from_radius
from core.coa import RadiusCoaClient
from core.rate_limit import format_bytes, format_duration
from core.time_service import get_system_now, get_configured_timezone_name, get_utc_cutoff_str

def get_heartbeat_cutoff_str(timeout_minutes=5):
    """Returns cutoff timestamp string based on UTC time for interim-update heartbeat check matching database radacct."""
    return get_utc_cutoff_str(timeout_minutes)

def get_user_audit_logs(user_type, user_id=None, username=None):
    """Fetches audit activity log for a specific subscriber or voucher."""
    logs = query_all('''
        SELECT * FROM user_audit_logs
        WHERE (user_type = ? AND user_id = ?) OR LOWER(username) = LOWER(?)
        ORDER BY id DESC
        LIMIT 100
    ''', (user_type, user_id or 0, username or ''))
    return logs

def get_user_usage_analytics(username, days=30):
    """
    Returns daily aggregated usage (Download, Upload, Connection Duration, Sessions)
    from radacct for Chart.js / ApexCharts.
    """
    try:
        stats = query_all('''
            SELECT 
                DATE(acctstarttime) as usage_date,
                COALESCE(SUM(CAST(COALESCE(acctoutputoctets, 0) AS UNSIGNED)), 0) as down_bytes,
                COALESCE(SUM(CAST(COALESCE(acctinputoctets, 0) AS UNSIGNED)), 0) as up_bytes,
                COALESCE(SUM(acctsessiontime), 0) as duration_sec,
                COUNT(*) as session_count
            FROM (
                SELECT acctstarttime,acctoutputoctets,acctinputoctets,acctsessiontime
                FROM radacct r WHERE LOWER(username)=LOWER(?)
                  AND (COALESCE(acctterminatecause,'')<>'Consolidated-Historical-Import'
                    OR NOT EXISTS (SELECT 1 FROM wisp_imported_session_history h WHERE h.username=r.username))
                UNION ALL
                SELECT acctstarttime,acctoutputoctets,acctinputoctets,acctsessiontime
                FROM wisp_imported_session_history WHERE LOWER(username)=LOWER(?)
            ) history
            GROUP BY DATE(acctstarttime)
            ORDER BY usage_date ASC
        ''', (username,username))
    except Exception as e:
        print(f"Error querying usage analytics: {e}")
        stats = []

    chart_labels = []
    down_mb = []
    up_mb = []
    duration_hours = []
    total_down_bytes = 0
    total_up_bytes = 0
    total_dur_sec = 0
    total_sess_count = 0

    for row in stats:
        d_val = row.get('usage_date')
        d_str = str(d_val) if d_val else ''
        chart_labels.append(d_str)
        
        d_bytes = float(row.get('down_bytes') or 0)
        u_bytes = float(row.get('up_bytes') or 0)
        dur = float(row.get('duration_sec') or 0)
        cnt = int(row.get('session_count') or 0)

        total_down_bytes += d_bytes
        total_up_bytes += u_bytes
        total_dur_sec += dur
        total_sess_count += cnt

        down_mb.append(round(d_bytes / (1024 * 1024), 2))
        up_mb.append(round(u_bytes / (1024 * 1024), 2))
        duration_hours.append(round(dur / 3600.0, 2))

    return {
        'labels': chart_labels,
        'download_mb': down_mb,
        'upload_mb': up_mb,
        'duration_hours': duration_hours,
        'total_down_str': format_bytes(total_down_bytes),
        'total_up_str': format_bytes(total_up_bytes),
        'total_duration_str': format_duration(total_dur_sec),
        'total_sessions': total_sess_count,
        'has_data': len(chart_labels) > 0
    }

def get_subscriber_status_counts(search=None, service_type=None, package_id=None):
    """
    Returns counts for active, online, expired, suspended, and all status categories
    for quick filter tabs using consolidated fast queries.
    """
    base_where = ' WHERE 1=1'
    base_params = []
    if search:
        base_where += ' AND (s.username LIKE ? OR s.full_name LIKE ? OR s.phone LIKE ? OR s.mac_binding LIKE ?)'
        like_str = f'%{search}%'
        base_params.extend([like_str, like_str, like_str, like_str])
    if service_type:
        base_where += ' AND s.service_type = ?'
        base_params.append(service_type)
    if package_id:
        base_where += ' AND s.package_id = ?'
        base_params.append(package_id)

    cutoff_str = get_heartbeat_cutoff_str(5)

    try:
        # Consolidated counts for all, active, expired, and suspended in a single query
        stats_sql = f'''
            SELECT 
                COUNT(*) as count_all,
                SUM(CASE WHEN s.status = 'active' AND (s.expires_at IS NULL OR s.expires_at > CURRENT_TIMESTAMP) THEN 1 ELSE 0 END) as count_active,
                SUM(CASE WHEN s.status NOT IN ('suspended','disabled') AND (s.status = 'expired' OR (s.expires_at IS NOT NULL AND s.expires_at <= CURRENT_TIMESTAMP)) THEN 1 ELSE 0 END) as count_expired,
                SUM(CASE WHEN s.status IN ('suspended','disabled') THEN 1 ELSE 0 END) as count_suspended
            FROM wisp_subscribers s
            {base_where}
        '''
        stats_row = query_one(stats_sql, tuple(base_params))
        count_all = stats_row['count_all'] if stats_row else 0
        count_active = int(stats_row['count_active'] or 0) if stats_row else 0
        count_expired = int(stats_row['count_expired'] or 0) if stats_row else 0
        count_suspended = int(stats_row['count_suspended'] or 0) if stats_row else 0

        # Online (Active radacct session)
        online_where = base_where + ''' AND s.username IN (
            SELECT username FROM radacct
            WHERE acctstoptime IS NULL
              AND (
                (acctupdatetime IS NOT NULL AND acctupdatetime >= ?)
                OR
                (acctupdatetime IS NULL AND acctstarttime >= ?)
              )
        )'''
        online_params = list(base_params) + [cutoff_str, cutoff_str]
        online_row = query_one(f'SELECT COUNT(*) as c FROM wisp_subscribers s {online_where}', tuple(online_params))
        count_online = online_row['c'] if online_row else 0

        return {
            'all': count_all,
            'active': count_active,
            'online': count_online,
            'expired': count_expired,
            'suspended': count_suspended
        }
    except Exception as e:
        print(f"Error fetching subscriber status counts: {e}")
        return {'all': 0, 'active': 0, 'online': 0, 'expired': 0, 'suspended': 0}

def get_subscribers(search=None, service_type=None, status=None, package_id=None):
    query = '''
        SELECT COALESCE(s.global_seq_id, s.id) as seq_id,
               s.*, p.name as package_name, p.price, p.rate_download, p.rate_upload,
               p.validity_value, p.validity_unit, COALESCE(s.snap_volume_quota_mb, p.volume_quota_mb, 0) as volume_quota_mb
        FROM wisp_subscribers s
        JOIN wisp_packages p ON s.package_id = p.id
        WHERE 1=1
    '''
    params = []
    if search:
        query += ' AND (s.username LIKE ? OR s.full_name LIKE ? OR s.phone LIKE ? OR s.mac_binding LIKE ?)'
        like_str = f'%{search}%'
        params.extend([like_str, like_str, like_str, like_str])
    if service_type:
        query += ' AND s.service_type = ?'
        params.append(service_type)
    if package_id:
        query += ' AND s.package_id = ?'
        params.append(package_id)

    cutoff_str = get_heartbeat_cutoff_str(5)

    if status == 'active':
        query += ' AND s.status = ? AND (s.expires_at IS NULL OR s.expires_at > CURRENT_TIMESTAMP)'
        params.append('active')
    elif status == 'online':
        query += ''' AND s.username IN (
            SELECT username FROM radacct
            WHERE acctstoptime IS NULL
              AND (
                (acctupdatetime IS NOT NULL AND acctupdatetime >= ?)
                OR
                (acctupdatetime IS NULL AND acctstarttime >= ?)
              )
        )'''
        params.extend([cutoff_str, cutoff_str])
    elif status == 'expired':
        query += " AND s.status NOT IN ('suspended','disabled') AND (s.status = ? OR (s.expires_at IS NOT NULL AND s.expires_at <= CURRENT_TIMESTAMP))"
        params.append('expired')
    elif status in ('suspended', 'disabled'):
        query += " AND s.status IN ('suspended','disabled')"
    elif status in (None, '', 'all'):
        pass
    else:
        query += ' AND s.status = ?'
        params.append(status)
        
    query += ' ORDER BY s.id DESC'
    subs = query_all(query, tuple(params))
    
    usernames = [s['username'] for s in subs if s.get('username')]
    active_sessions = {}
    last_sessions = {}
    usage_map = {}

    if usernames:
        placeholders = ','.join(['?'] * len(usernames))

        # 1. Active live sessions with Interim-Update Heartbeat (scoped to loaded subscribers)
        cutoff_str = get_heartbeat_cutoff_str(5)
        active_sessions_list = query_all(f'''
            SELECT username, acctsessionid, framedipaddress, nasipaddress, calledstationid,
                   acctstarttime, callingstationid, acctinputoctets, acctoutputoctets
            FROM radacct
            WHERE username IN ({placeholders})
              AND acctstoptime IS NULL
              AND (
                (acctupdatetime IS NOT NULL AND acctupdatetime >= ?)
                OR
                (acctupdatetime IS NULL AND acctstarttime >= ?)
              )
        ''', tuple(usernames) + (cutoff_str, cutoff_str))
        active_sessions = {r['username'].lower(): r for r in (active_sessions_list or [])}
        
        # 2. Last known closed session per subscriber (scoped to loaded subscribers)
        last_sessions_list = query_all(f'''
            SELECT a.username, a.acctstoptime, a.framedipaddress, a.calledstationid, a.nasipaddress,
                   a.acctinputoctets, a.acctoutputoctets
            FROM radacct a
            INNER JOIN (
                SELECT username, MAX(radacctid) as max_id
                FROM radacct
                WHERE username IN ({placeholders})
                GROUP BY username
            ) m ON a.radacctid = m.max_id
        ''', tuple(usernames))
        last_sessions = {r['username'].lower(): r for r in (last_sessions_list or [])}

        # radacct octets already include Gigawords; adding them again doubles high counters.
        # 3. Total data usage per subscriber in current cycle (Download + Upload) (scoped to loaded subscribers)
        usage_list = query_all(f'''
            SELECT s.id, LOWER(s.username) as username,
                   COALESCE(SUM(t.max_in), 0) as total_in,
                   COALESCE(SUM(t.max_out), 0) as total_out
            FROM wisp_subscribers s
            LEFT JOIN (
                SELECT username, nasipaddress, acctsessionid,
                       MAX(CAST(COALESCE(acctinputoctets, 0) AS UNSIGNED)) as max_in,
                       MAX(CAST(COALESCE(acctoutputoctets, 0) AS UNSIGNED)) as max_out,
                       MIN(COALESCE(acctstarttime, acctupdatetime)) as sess_start
                FROM radacct
                WHERE username IN ({placeholders})
                GROUP BY username, nasipaddress, acctsessionid
            ) t ON LOWER(s.username) = LOWER(t.username)
               AND (s.last_renewed_at IS NULL OR t.sess_start >= s.last_renewed_at)
            WHERE s.username IN ({placeholders})
            GROUP BY s.id, s.username
        ''', tuple(usernames) + tuple(usernames))
        usage_map = {r['username'].lower(): r for r in (usage_list or [])}

    now = get_db_storage_now().replace(tzinfo=None)

    for sub in subs:
        u_key = (sub['username'] or '').lower()
        active_session = active_sessions.get(u_key)
        last_session = last_sessions.get(u_key)
        usage_data = usage_map.get(u_key, {})

        # 1. Status & Online state
        if active_session:
            sub['is_online'] = True
            sub['session_id'] = active_session.get('acctsessionid')
            sub['ip_address'] = active_session.get('framedipaddress') or sub.get('static_ip') or '-'
            sub['nas_ip'] = active_session.get('nasipaddress') or '-'
            sub['session_mac'] = active_session.get('callingstationid') or sub.get('mac_binding') or '-'
            sub['access_point'] = active_session.get('calledstationid') or active_session.get('nasipaddress') or 'MikroTik Hotspot'
            sub['last_seen_str'] = 'متصل الآن'
        else:
            sub['is_online'] = False
            sub['session_id'] = None
            if last_session:
                sub['ip_address'] = last_session.get('framedipaddress') or sub.get('static_ip') or '-'
                sub['nas_ip'] = last_session.get('nasipaddress') or '-'
                sub['session_mac'] = sub.get('mac_binding') or '-'
                sub['access_point'] = last_session.get('calledstationid') or last_session.get('nasipaddress') or '-'
                stop_time = last_session.get('acctstoptime')
                if stop_time:
                    if isinstance(stop_time, datetime.datetime):
                        sub['last_seen_str'] = stop_time.strftime('%Y-%m-%d %H:%M')
                    else:
                        sub['last_seen_str'] = str(stop_time)[:16]
                else:
                    sub['last_seen_str'] = 'لم يتصل بعد'
            else:
                sub['ip_address'] = sub.get('static_ip') or '-'
                sub['nas_ip'] = '-'
                sub['session_mac'] = sub.get('mac_binding') or '-'
                sub['access_point'] = '-'
                sub['last_seen_str'] = 'لم يتصل بعد'

        # 2. Remaining Days Calculation
        exp_dt = sub.get('expires_at')
        if exp_dt:
            if isinstance(exp_dt, str):
                try:
                    exp_dt = datetime.datetime.fromisoformat(exp_dt.replace('Z', ''))
                except Exception:
                    exp_dt = None

            if exp_dt:
                delta = exp_dt - now
                if delta.total_seconds() > 0:
                    days = delta.days
                    total_sec = int(delta.total_seconds())
                    hours = int(total_sec / 3600)
                    minutes = int((total_sec % 3600) / 60)
                    if days > 0:
                        sub['days_remaining_str'] = f"{days} يوم"
                        sub['days_remaining_num'] = days
                    elif hours > 0:
                        sub['days_remaining_str'] = f"{hours} ساعة"
                        sub['days_remaining_num'] = 0.5
                    else:
                        sub['days_remaining_str'] = f"{max(1, minutes)} دقيقة"
                        sub['days_remaining_num'] = 0.1
                    sub['is_expired'] = False
                else:
                    sub['days_remaining_str'] = 'منتهي'
                    sub['days_remaining_num'] = 0
                    sub['is_expired'] = True
            else:
                sub['days_remaining_str'] = 'غير محدد'
                sub['days_remaining_num'] = 9999
                sub['is_expired'] = False
        else:
            sub['days_remaining_str'] = 'غير محدد'
            sub['days_remaining_num'] = 9999
            sub['is_expired'] = False

        # 3. Consumption & Quota Progress Bar
        total_in_bytes = float(usage_data.get('total_in') or 0)
        total_out_bytes = float(usage_data.get('total_out') or 0)
        used_bytes = total_in_bytes + total_out_bytes
        sub['bytes_in'] = format_bytes(total_in_bytes)
        sub['bytes_out'] = format_bytes(total_out_bytes)
        sub['used_bytes_str'] = format_bytes(used_bytes)

        base_quota_mb = float(sub.get('volume_quota_mb') or 0)
        extra_quota_mb = float(sub.get('extra_quota_mb') or 0)
        is_limited_package = (base_quota_mb > 0)
        pkg_quota_mb = max(0.0, base_quota_mb + extra_quota_mb)

        if is_limited_package or extra_quota_mb > 0:
            quota_bytes = pkg_quota_mb * 1024 * 1024
            sub['quota_bytes_str'] = format_bytes(quota_bytes)
            percent = 100.0 if quota_bytes == 0 else min(100.0, round((used_bytes / quota_bytes) * 100.0, 1))
            remaining_bytes = max(0, quota_bytes - used_bytes)
            sub['remaining_bytes_str'] = format_bytes(remaining_bytes)
            sub['has_quota'] = True
            sub['consumption_percent'] = percent
        else:
            sub['quota_bytes_str'] = 'غير محدود'
            sub['remaining_bytes_str'] = 'مفتوح'
            sub['has_quota'] = False
            sub['consumption_percent'] = 0

        # 4. Formatted helpers for optional columns
        sub['created_by_name'] = 'المدير العام (admin)'
        if sub.get('created_at'):
            sub['created_at_str'] = sub['created_at'].strftime('%Y-%m-%d %H:%M') if isinstance(sub['created_at'], datetime.datetime) else str(sub['created_at'])[:16]
        else:
            sub['created_at_str'] = '-'

        if sub.get('expires_at'):
            sub['expires_at_str'] = sub['expires_at'].strftime('%Y-%m-%d %H:%M') if isinstance(sub['expires_at'], datetime.datetime) else str(sub['expires_at'])[:16]
        else:
            sub['expires_at_str'] = 'غير محدد'

        sub['balance_formatted'] = f"{float(sub.get('balance') or 0):,.2f}"

    return subs

def create_subscriber(data, admin_username='admin'):
    from services.license_guard_service import get_active_license_status
    lic_st = get_active_license_status()
    if not lic_st.get('valid'):
        raise ValueError(lic_st.get('message', "النظام مقفل أو غير مرخص. لا يمكن إضافة مشتركين جدد."))
    if lic_st.get('status') == 'revoked':
        raise ValueError("الترخيص محظور من قِبل المطور. لا يمكن إضافة مشتركين جدد.")

    username = str(data.get('username') or '').strip()
    if query_one('SELECT id FROM wisp_vouchers WHERE username=?', (username,)):
        raise ValueError('اسم المستخدم مستخدم بالفعل لكرت؛ اختر اسمًا مختلفًا.')
    exp_iso = data.get('expires_at') or None
    validity_mode = data.get('validity_mode', 'package')
    
    if validity_mode == 'unlimited':
        exp_iso = None
    elif validity_mode == 'manual' and data.get('expires_at'):
        exp_iso = data.get('expires_at').strip().replace('T', ' ')
        if len(exp_iso) == 16:
            exp_iso += ':00'
    elif not exp_iso and data.get('package_id'):
        pkg = query_one('SELECT validity_value, validity_unit, validity_days FROM wisp_packages WHERE id = ?', (int(data['package_id']),))
        if pkg:
            now_dt = get_db_storage_now().replace(tzinfo=None)
            raw_val = pkg.get('validity_value')
            if raw_val is None:
                raw_val = pkg.get('validity_days')
            val = int(raw_val if raw_val is not None else 30)
            unit = (pkg.get('validity_unit') or 'days').lower()
            if val <= 0:
                exp_dt = None
            elif unit == 'minutes':
                exp_dt = now_dt + datetime.timedelta(minutes=val)
            elif unit == 'hours':
                exp_dt = now_dt + datetime.timedelta(hours=val)
            elif unit == 'months':
                exp_dt = now_dt + datetime.timedelta(days=val * 30)
            else:
                exp_dt = now_dt + datetime.timedelta(days=val)
            exp_iso = exp_dt.strftime('%Y-%m-%d %H:%M:%S') if exp_dt else None

    initial_balance = float(data.get('balance') or 0.0)
    extra_quota = int(data.get('extra_quota_mb') or 0)
    email_val = data.get('email', '').strip()

    sub_id = execute_write('''
        INSERT INTO wisp_subscribers (
            username, password, full_name, phone, email, national_id, address,
            service_type, package_id, mac_binding, static_ip, status, balance,
            extra_quota_mb, expires_at, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        data['username'].strip(),
        data['password'].strip(),
        (data.get('full_name') or data.get('fullname') or '').strip(),
        data.get('phone', '').strip(),
        email_val,
        data.get('national_id', '').strip(),
        data.get('address', '').strip(),
        data.get('service_type', 'pppoe'),
        int(data['package_id']),
        data.get('mac_binding', '').strip(),
        data.get('static_ip', '').strip(),
        'suspended' if data.get('status') == 'disabled' else data.get('status', 'active'),
        initial_balance,
        extra_quota,
        exp_iso,
        data.get('notes', '')
    ))
    
    sync_subscriber_to_radius(sub_id)
    log_audit(1, admin_username, 'CREATE_SUBSCRIBER', 'subscribers', f'Created subscriber {data["username"]}')
    return sub_id

def update_subscriber(sub_id, data, admin_username='admin'):
    old_sub = query_one('''
        SELECT s.*, p.name as package_name 
        FROM wisp_subscribers s 
        LEFT JOIN wisp_packages p ON s.package_id = p.id 
        WHERE s.id = ?
    ''', (sub_id,))
    if not old_sub:
        raise ValueError('المشترك غير موجود.')

    new_pwd = data['password'].strip()
    new_fullname = (data.get('full_name') or data.get('fullname') or '').strip()
    new_phone = data.get('phone', '').strip()
    new_national_id = data.get('national_id', '').strip()
    new_address = data.get('address', '').strip()
    new_service_type = data.get('service_type', 'pppoe')
    new_pkg_id = int(data['package_id'])
    new_mac = data.get('mac_binding', '').strip()
    new_ip = data.get('static_ip', '').strip()
    new_status = data.get('status', 'active')
    if new_status == 'disabled':
        new_status = 'suspended'
    new_expires_at = data.get('expires_at') or None
    new_notes = data.get('notes', '')

    # License Check: If transitioning to active from suspended/expired
    old_status = str(old_sub.get('status') or '').lower()
    if old_status != 'active' and str(new_status).lower() == 'active':
        from services.license_guard_service import get_active_license_status
        lic_st = get_active_license_status()
        if not lic_st.get('valid'):
            raise ValueError(f"لا يمكن تنشيط المشترك: {lic_st.get('message', 'النظام غير مرخص')}")
        if lic_st.get('status') == 'revoked':
            raise ValueError("لا يمكن تنشيط المشترك: الترخيص محظور من قِبل المطور.")

    new_pkg = query_one('SELECT name FROM wisp_packages WHERE id = ?', (new_pkg_id,))
    new_pkg_name = new_pkg['name'] if new_pkg else str(new_pkg_id)

    # Track changes for detailed audit log
    changes = []
    if old_sub.get('full_name') != new_fullname:
        changes.append(f"تعديل الاسم من '{old_sub.get('full_name')}' إلى '{new_fullname}'")
    if old_sub.get('password') != new_pwd:
        changes.append("تعديل كلمة المرور في RADIUS")
    if old_sub.get('package_id') != new_pkg_id:
        changes.append(f"تغيير الباقة من '{old_sub.get('package_name')}' إلى '{new_pkg_name}'")
    if str(old_sub.get('service_type')) != str(new_service_type):
        changes.append(f"تغيير نوع الربط إلى '{new_service_type}'")
    if str(old_sub.get('status')) != str(new_status):
        changes.append(f"تغيير حالة الحساب من '{old_sub.get('status')}' إلى '{new_status}'")
    if str(old_sub.get('expires_at') or '') != str(new_expires_at or ''):
        changes.append(f"تعديل تاريخ الانتهاء إلى '{new_expires_at or 'غير محدد'}'")
    if str(old_sub.get('mac_binding') or '') != str(new_mac or ''):
        changes.append(f"تعديل تقييد الماك إلى '{new_mac or 'إلغاء التقييد'}'")
    if str(old_sub.get('static_ip') or '') != str(new_ip or ''):
        changes.append(f"تعديل عنوان IP الثابت إلى '{new_ip or 'تلقائي'}'")

    execute_write('''
        UPDATE wisp_subscribers SET
            snap_volume_quota_mb = CASE WHEN package_id = ? THEN snap_volume_quota_mb ELSE NULL END,
            password = ?, full_name = ?, phone = ?, national_id = ?, address = ?,
            service_type = ?, package_id = ?, mac_binding = ?, static_ip = ?,
            status = ?, expires_at = ?, notes = ?
        WHERE id = ?
    ''', (
        new_pkg_id, new_pwd, new_fullname, new_phone, new_national_id, new_address,
        new_service_type, new_pkg_id, new_mac, new_ip,
        new_status, new_expires_at, new_notes,
        sub_id
    ))
    sync_subscriber_to_radius(sub_id)
    
    change_summary = "، ".join(changes) if changes else "تحديث عام لبيانات المشترك"
    log_user_audit('subscriber', sub_id, old_sub['username'], admin_username, 'UPDATE_PROFILE', change_summary)
    log_audit(1, admin_username, 'UPDATE_SUBSCRIBER', 'subscribers', f'Updated subscriber {old_sub["username"]}: {change_summary}')
    return True

def delete_subscriber(sub_id, admin_username='admin'):
    from services.account_lifecycle_service import delete_account
    result = delete_account('subscriber', sub_id)
    if result.get('pending'):
        raise ValueError('تم منع الدخول وطلب فصل جميع الجلسات؛ أعد الحذف بعد إغلاقها. الفواتير محفوظة.')
    if result.get('deleted'):
        log_audit(1, admin_username, 'DELETE_SUBSCRIBER', 'subscribers', f'Deleted subscriber {sub_id}; invoices retained')
    return bool(result.get('deleted'))

def get_subscriber_sessions(username, limit=15):
    cutoff_str = get_heartbeat_cutoff_str(5)
    # Imported details are display-only; quota/authentication read radacct exclusively.
    columns = 'radacctid,acctsessionid,acctuniqueid,username,nasipaddress,acctstarttime,acctupdatetime,acctstoptime,acctsessiontime,acctinputoctets,acctoutputoctets,acctterminatecause,framedipaddress,callingstationid'
    sessions = query_all(f"""
        SELECT * FROM (
          SELECT {columns}, CASE WHEN acctstoptime IS NULL
            AND COALESCE(acctupdatetime,acctstarttime)>=? THEN 1 ELSE 0 END is_active,
            0 imported_history FROM radacct r WHERE LOWER(username)=LOWER(?)
            AND (COALESCE(acctterminatecause,'')<>'Consolidated-Historical-Import'
              OR NOT EXISTS (SELECT 1 FROM wisp_imported_session_history h WHERE h.username=r.username))
          UNION ALL
          SELECT {columns},0 is_active,1 imported_history
            FROM wisp_imported_session_history WHERE LOWER(username)=LOWER(?)
        ) history ORDER BY acctstarttime DESC,radacctid DESC LIMIT ?
    """, (cutoff_str,username,username,limit))
    
    for s in sessions:
        down_bytes = float(int(s.get('acctoutputoctets') or 0))
        up_bytes = float(int(s.get('acctinputoctets') or 0))
        s['download_str'] = format_bytes(down_bytes)
        s['upload_str'] = format_bytes(up_bytes)
        s['total_str'] = format_bytes(down_bytes + up_bytes)
        s['duration_str'] = format_duration(s.get('acctsessiontime', 0))
        s['is_active'] = bool(s['is_active'])
    return sessions

def disconnect_subscriber_session(username, admin_username='admin'):
    """Disconnect all user sessions through the shared, independently verified path."""
    username = (username or '').strip()
    if not username:
        return {'success': False, 'status': 'invalid_user', 'message': 'اسم المستخدم غير محدد'}
    from services.coa_queue_service import _process_coa_task
    res = _process_coa_task({'action': 'disconnect', 'username': username,
                             'admin_username': admin_username,
                             'reason': 'Subscriber lifecycle disconnect'})
    log_audit(1, admin_username, 'DISCONNECT_USER', 'subscribers',
              f'Disconnect request for {username}: {res.get("message", "")}')
    return res


def dispatch_async_disconnect(username, admin_username='admin'):
    if not username:
        return
    from services.coa_queue_service import enqueue_disconnect
    ok,message=enqueue_disconnect(username,reason='Subscriber administrative disconnect',admin_username=admin_username)
    if not ok:
        log_audit(1, admin_username, 'DISCONNECT_QUEUE_REJECTED', 'subscribers', message)
    return ok

def clean_stale_sessions(timeout_minutes=None):
    """
    Finds and forcibly closes orphaned/ghost sessions where acctstoptime IS NULL
    and no interim-update has been received for longer than timeout_minutes.
    Preserves multiple simultaneous connections for the same user if they are active.
    """
    if timeout_minutes is None:
        from services.autoheal_service import get_zombie_session_timeout
        timeout_minutes = get_zombie_session_timeout()
    cutoff_str = get_heartbeat_cutoff_str(timeout_minutes)

    closed_count = 0
    try:
        closed_count = execute_update("""
            UPDATE radacct 
            SET acctstoptime = COALESCE(acctupdatetime, acctstarttime), 
                acctterminatecause = 'Stale-Session-Timeout' 
            WHERE acctstoptime IS NULL 
              AND (
                (acctupdatetime IS NOT NULL AND acctupdatetime < ?)
                OR 
                (acctupdatetime IS NULL AND acctstarttime < ?)
              )
        """, (cutoff_str, cutoff_str))
    except Exception as e:
        print(f"Error cleaning stale sessions: {e}")

    return closed_count

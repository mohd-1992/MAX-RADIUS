# -*- coding: utf-8 -*-
"""
User Portal & Client Area Service:
Handles authentication, subscriber profile, wallet recharges via vouchers,
package renewals & changes, and personal session history.
"""

import re
import datetime
from core.time_service import get_db_storage_now
from core.config import DB_TYPE
from database.db import query_one, query_all, execute_write, execute_update, db_session, adapt_query, is_mysql_conn, log_audit, log_user_audit
from core.rate_limit import format_bytes, format_duration
from core.radius_sync import sync_subscriber_to_radius, delete_user_from_radius
from services.subscriber_service import disconnect_subscriber_session, clean_stale_sessions, get_heartbeat_cutoff_str, dispatch_async_disconnect
from services.quota_service import calculate_cycle_usage_and_rollover, record_session_baselines

def format_remaining_time(expires_at_val):
    """
    Formats exact remaining time for subscriber/card into clear Arabic representation:
    e.g. 'باقي 1 شهر و 5 أيام', 'باقي 5 أيام و 3 ساعات', 'باقي 2 ساعة و 15 دقيقة', 'باقي 40 دقيقة', 'منتهي الصلاحية'
    """
    if not expires_at_val or str(expires_at_val).strip() in ['', 'غير محدد', 'None']:
        return "غير محدد (مفتوح)", 0

    try:
        if isinstance(expires_at_val, datetime.datetime):
            exp_dt = expires_at_val
        else:
            clean_str = str(expires_at_val).split('.')[0].strip()
            exp_dt = datetime.datetime.strptime(clean_str, '%Y-%m-%d %H:%M:%S')
    except Exception:
        return str(expires_at_val), 0

    now = get_db_storage_now().replace(tzinfo=None)
    diff = exp_dt - now
    total_seconds = int(diff.total_seconds())

    if total_seconds <= 0:
        return "منتهي الصلاحية", 0

    days = diff.days
    seconds = diff.seconds
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60

    parts = []
    if days >= 30:
        months = days // 30
        rem_days = days % 30
        if months == 1:
            m_str = "1 شهر"
        elif months == 2:
            m_str = "شهرين"
        elif 3 <= months <= 10:
            m_str = f"{months} أشهر"
        else:
            m_str = f"{months} شهر"
        parts.append(m_str)

        if rem_days > 0:
            if rem_days == 1:
                d_str = "1 يوم"
            elif rem_days == 2:
                d_str = "يومين"
            elif 3 <= rem_days <= 10:
                d_str = f"{rem_days} أيام"
            else:
                d_str = f"{rem_days} يوم"
            parts.append(d_str)
    elif days > 0:
        if days == 1:
            d_str = "1 يوم"
        elif days == 2:
            d_str = "يومين"
        elif 3 <= days <= 10:
            d_str = f"{days} أيام"
        else:
            d_str = f"{days} يوم"
        parts.append(d_str)

        if hours > 0:
            if hours == 1:
                h_str = "1 ساعة"
            elif hours == 2:
                h_str = "ساعتين"
            elif 3 <= hours <= 10:
                h_str = f"{hours} ساعات"
            else:
                h_str = f"{hours} ساعة"
            parts.append(h_str)
    elif hours > 0:
        if hours == 1:
            h_str = "1 ساعة"
        elif hours == 2:
            h_str = "ساعتين"
        elif 3 <= hours <= 10:
            h_str = f"{hours} ساعات"
        else:
            h_str = f"{hours} ساعة"
        parts.append(h_str)

        if minutes > 0:
            if minutes == 1:
                min_str = "1 دقيقة"
            elif minutes == 2:
                min_str = "دقيقتين"
            elif 3 <= minutes <= 10:
                min_str = f"{minutes} دقائق"
            else:
                min_str = f"{minutes} دقيقة"
            parts.append(min_str)
    elif minutes > 0:
        if minutes == 1:
            min_str = "1 دقيقة"
        elif minutes == 2:
            min_str = "دقيقتين"
        elif 3 <= minutes <= 10:
            min_str = f"{minutes} دقائق"
        else:
            min_str = f"{minutes} دقيقة"
        parts.append(min_str)
    else:
        parts.append("أقل من دقيقة")

    return f"باقي {' و '.join(parts)}", total_seconds

def translate_portal_error(raw_error):
    """
    Translates raw MikroTik Hotspot and FreeRADIUS error messages into friendly Arabic alerts.
    Handles URL encodings, double escapes (+ and %), and translates all router/RADIUS error codes.
    """
    if not raw_error:
        return ""
    import urllib.parse
    import re

    err = str(raw_error).strip()
    for _ in range(3):
        try:
            decoded = urllib.parse.unquote_plus(err)
            if decoded == err:
                break
            err = decoded
        except Exception:
            break

    err_normalized = err.lower().replace('+', ' ').replace('_', ' ').replace('-', ' ').strip()

    if any(k in err_normalized for k in ["invalid username", "invalid password", "wrong password", "bad password", "authentication failed", "auth failed", "internal error"]):
        return "رقم الكرت أو كلمة المرور غير صحيحة، يرجى التأكد وإعادة المحاولة."
    if any(k in err_normalized for k in ["uptime limit", "uptime reached", "time limit", "time reached", "limit uptime"]):
        return "عذراً، لقد انتهى الوقت المخصص لهذا الكرت (صلاحية الوقت منتهية)."
    if any(k in err_normalized for k in ["traffic limit", "quota reached", "bytes limit", "transfer limit", "limit bytes", "data limit", "limit reached"]):
        return "عذراً، لقد انتهى رصيد البيانات (الجيجابايت) المخصص لهذا الكرت."
    if any(k in err_normalized for k in ["already logged in", "simultaneous", "session limit", "too many sessions", "active session"]):
        return "هذا الكرت متصل حالياً من جهاز آخر (جلسة نشطة)."
    if any(k in err_normalized for k in ["not responding", "radius timeout", "radius server", "timeout"]):
        return "تعذر الاتصال بسيرفر المصادقة، يرجى المحاولة بعد قليل."
    if any(k in err_normalized for k in ["user not found", "user does not exist", "no such user", "invalid user"]):
        return "رقم الكرت غير مسجل بالنظام، تأكد من كتابة الأرقام بدقة."
    if any(k in err_normalized for k in ["cannot log in", "disabled", "account suspended", "suspended"]):
        return "هذا الحساب موقوف أو معلق من قبل إدارة الشبكة."
    if any(k in err_normalized for k in ["mac cookie", "invalid mac", "mac address", "mac lock"]):
        return "هذا الكرت مرتبط بجهاز آخر ولا يمكن استخدامه من هذا الجهاز."

    if re.search(r'[\u0600-\u06FF]', err):
        return err

    return "تعذر تسجيل الدخول: يرجى التحقق من رقم الكرت والمحاولة مجدداً."

def format_mb_or_gb(val_mb):
    """
    Formats megabytes dynamically:
    - If >= 1024 MB: formats in GB (e.g. '25 GB', '1.5 GB')
    - If < 1024 MB: formats in MB (e.g. '500 MB', '0 MB')
    - If None or empty: returns 'غير محدود'
    """
    if val_mb is None or str(val_mb).strip() in ['', 'None', 'غير محدود', 'غير محدود (Unlimited)']:
        return "غير محدود"
    try:
        val_float = float(val_mb)
    except (ValueError, TypeError):
        return str(val_mb)

    if val_float <= 0:
        return "0 MB"

    if val_float >= 1024.0:
        gb_val = val_float / 1024.0
        if gb_val == int(gb_val):
            return f"{int(gb_val)} GB"
        else:
            return f"{gb_val:.2f}".rstrip('0').rstrip('.') + " GB"
    else:
        if val_float == int(val_float):
            return f"{int(val_float)} MB"
        else:
            return f"{val_float:.2f}".rstrip('0').rstrip('.') + " MB"

COLOR_THEMES = {
    'emerald': {
        'hex': '#10b981',
        'text': '#34d399',
        'bg': 'rgba(16, 185, 129, 0.08)',
        'border': 'rgba(16, 185, 129, 0.45)',
        'active_bg': 'linear-gradient(135deg, #059669, #10b981)',
        'active_border': '#34d399',
        'active_text': '#ffffff',
        'status_bg': 'rgba(16, 185, 129, 0.12)',
        'status_border': 'rgba(16, 185, 129, 0.35)',
        'status_text': '#34d399',
        'badge_bg': 'rgba(16, 185, 129, 0.22)',
        'badge_text': '#34d399',
        'glow': 'rgba(16, 185, 129, 0.35)',
        'default_emoji': '💰'
    },
    'sky': {
        'hex': '#0ea5e9',
        'text': '#38bdf8',
        'bg': 'rgba(14, 165, 233, 0.08)',
        'border': 'rgba(14, 165, 233, 0.45)',
        'active_bg': 'linear-gradient(135deg, #0284c7, #0ea5e9)',
        'active_border': '#38bdf8',
        'active_text': '#ffffff',
        'status_bg': 'rgba(14, 165, 233, 0.12)',
        'status_border': 'rgba(14, 165, 233, 0.35)',
        'status_text': '#38bdf8',
        'badge_bg': 'rgba(14, 165, 233, 0.22)',
        'badge_text': '#38bdf8',
        'glow': 'rgba(14, 165, 233, 0.35)',
        'default_emoji': '⚡'
    },
    'rose': {
        'hex': '#f43f5e',
        'text': '#fb7185',
        'bg': 'rgba(244, 63, 94, 0.08)',
        'border': 'rgba(244, 63, 94, 0.45)',
        'active_bg': 'linear-gradient(135deg, #e11d48, #f43f5e)',
        'active_border': '#fb7185',
        'active_text': '#ffffff',
        'status_bg': 'rgba(244, 63, 94, 0.12)',
        'status_border': 'rgba(244, 63, 94, 0.35)',
        'status_text': '#fb7185',
        'badge_bg': 'rgba(244, 63, 94, 0.22)',
        'badge_text': '#fb7185',
        'glow': 'rgba(244, 63, 94, 0.35)',
        'default_emoji': '🎮'
    },
    'indigo': {
        'hex': '#3b82f6',
        'text': '#60a5fa',
        'bg': 'rgba(59, 130, 246, 0.08)',
        'border': 'rgba(59, 130, 246, 0.45)',
        'active_bg': 'linear-gradient(135deg, #2563eb, #3b82f6)',
        'active_border': '#f59e0b',
        'active_text': '#ffffff',
        'status_bg': 'rgba(59, 130, 246, 0.12)',
        'status_border': 'rgba(59, 130, 246, 0.35)',
        'status_text': '#60a5fa',
        'badge_bg': 'rgba(59, 130, 246, 0.22)',
        'badge_text': '#60a5fa',
        'glow': 'rgba(59, 130, 246, 0.35)',
        'default_emoji': '🚀'
    },
    'purple': {
        'hex': '#a855f7',
        'text': '#c084fc',
        'bg': 'rgba(168, 85, 247, 0.08)',
        'border': 'rgba(168, 85, 247, 0.45)',
        'active_bg': 'linear-gradient(135deg, #7e22ce, #a855f7)',
        'active_border': '#c084fc',
        'active_text': '#ffffff',
        'status_bg': 'rgba(168, 85, 247, 0.12)',
        'status_border': 'rgba(168, 85, 247, 0.35)',
        'status_text': '#c084fc',
        'badge_bg': 'rgba(168, 85, 247, 0.22)',
        'badge_text': '#c084fc',
        'glow': 'rgba(168, 85, 247, 0.35)',
        'default_emoji': '🔥'
    },
    'amber': {
        'hex': '#f59e0b',
        'text': '#fbbf24',
        'bg': 'rgba(245, 158, 11, 0.08)',
        'border': 'rgba(245, 158, 11, 0.45)',
        'active_bg': 'linear-gradient(135deg, #d97706, #f59e0b)',
        'active_border': '#fbbf24',
        'active_text': '#ffffff',
        'status_bg': 'rgba(245, 158, 11, 0.12)',
        'status_border': 'rgba(245, 158, 11, 0.35)',
        'status_text': '#fbbf24',
        'badge_bg': 'rgba(245, 158, 11, 0.22)',
        'badge_text': '#fbbf24',
        'glow': 'rgba(245, 158, 11, 0.35)',
        'default_emoji': '⚡'
    },
    'cyan': {
        'hex': '#06b6d4',
        'text': '#22d3ee',
        'bg': 'rgba(6, 182, 212, 0.08)',
        'border': 'rgba(6, 182, 212, 0.45)',
        'active_bg': 'linear-gradient(135deg, #0891b2, #06b6d4)',
        'active_border': '#22d3ee',
        'active_text': '#ffffff',
        'status_bg': 'rgba(6, 182, 212, 0.12)',
        'status_border': 'rgba(6, 182, 212, 0.35)',
        'status_text': '#22d3ee',
        'badge_bg': 'rgba(6, 182, 212, 0.22)',
        'badge_text': '#22d3ee',
        'glow': 'rgba(6, 182, 212, 0.35)',
        'default_emoji': '🌐'
    }
}

def get_portal_speed_options(settings_dict=None):
    """
    Speed selector feature has been removed. Always returns empty list.
    """
    return []


def authenticate_portal_user(username, password):
    """
    Authenticate against FreeRADIUS radcheck table or subscribers/vouchers.
    """
    username = (username or '').strip()
    password = (password or '').strip()
    if not username:
        return None, "يرجى إدخال اسم المستخدم"
    setting = query_one("SELECT value FROM wisp_system_settings WHERE `key`='portal_login_username_only'")
    username_only = bool(setting and str(setting.get('value')) == '1')
    if username_only:
        # This policy applies to portal login only, including expired accounts.
        sub = query_one("SELECT id,username FROM wisp_subscribers WHERE LOWER(username)=LOWER(?)", (username,))
        if sub:
            return {'username': sub['username'], 'type': 'subscriber', 'id': sub['id']}, None
        voucher = query_one("SELECT id,username FROM wisp_vouchers WHERE LOWER(username)=LOWER(?) OR pin_code=?", (username, username))
        if voucher:
            return {'username': voucher['username'], 'type': 'voucher', 'id': voucher['id']}, None
        radius_user = query_one("SELECT username FROM radcheck WHERE LOWER(username)=LOWER(?) AND attribute IN ('Cleartext-Password','User-Password','MD5-Password') LIMIT 1", (username,))
        if radius_user:
            return {'username': radius_user['username'], 'type': 'radius_user', 'id': None}, None
        return None, "اسم المستخدم غير صحيح"
    if not password:
        return None, "يرجى إدخال اسم المستخدم وكلمة المرور"

    # 1. Check radcheck
    rad_row = query_one(
        "SELECT * FROM radcheck WHERE LOWER(username) = LOWER(?) AND attribute IN ('Cleartext-Password', 'User-Password', 'MD5-Password')",
        (username,)
    )
    if rad_row:
        stored_pwd = rad_row['value']
        if stored_pwd == password:
            actual_username = rad_row['username']
            # Determine user type
            sub = query_one("SELECT * FROM wisp_subscribers WHERE LOWER(username) = LOWER(?)", (actual_username,))
            if sub:
                return {'username': sub['username'], 'type': 'subscriber', 'id': sub['id']}, None
            v = query_one("SELECT * FROM wisp_vouchers WHERE LOWER(username) = LOWER(?) OR pin_code = ?", (actual_username, actual_username))
            if v:
                return {'username': v['username'], 'type': 'voucher', 'id': v['id']}, None
            return {'username': actual_username, 'type': 'radius_user', 'id': None}, None

    # 2. Check wisp_subscribers
    sub = query_one("SELECT * FROM wisp_subscribers WHERE LOWER(username) = LOWER(?)", (username,))
    if sub and sub['password'] == password:
        return {'username': sub['username'], 'type': 'subscriber', 'id': sub['id']}, None

    # 3. Check wisp_vouchers
    v = query_one("SELECT * FROM wisp_vouchers WHERE (LOWER(username) = LOWER(?) OR pin_code = ?)", (username, username))
    if v and (v['password'] == password or v['pin_code'] == password or v['username'] == password):
        return {'username': v['username'], 'type': 'voucher', 'id': v['id']}, None

    return None, "اسم المستخدم أو كلمة المرور غير صحيحة"

def get_portal_user_data(username):
    """
    Fetch comprehensive dashboard data for the authenticated subscriber.
    """
    try:
        pass  # Background watchdog owns stale-session cleanup.
    except Exception:
        pass
    # 1. Check if subscriber
    sub = query_one("""
        SELECT s.*, p.name as package_name, p.price as package_price,
               p.rate_download, p.rate_upload, COALESCE(s.snap_volume_quota_mb, p.volume_quota_mb, 0) as volume_quota_mb,
               p.uptime_limit_mins, p.validity_days, p.description as package_desc
        FROM wisp_subscribers s
        LEFT JOIN wisp_packages p ON s.package_id = p.id
        WHERE LOWER(s.username) = LOWER(?)
    """, (username,))

    v_card = None
    if sub:
        user_info = {
            'type': 'subscriber',
            'username': sub['username'],
            'full_name': sub['full_name'],
            'phone': sub['phone'] or '',
            'service_type': sub['service_type'],
            'balance': float(sub.get('balance') or 0.0),
            'extra_quota_mb': float(sub.get('extra_quota_mb') or 0.0),
            'loan_balance_mb': int(sub.get('loan_balance_mb') or 0),
            'loan_status': int(sub.get('loan_status') or 0),
            'status': sub['status'],
            'expires_at': str(sub['expires_at']) if sub['expires_at'] else 'غير محدد',
            'package_id': sub['package_id'],
            'package_name': sub['package_name'] or 'باقة عامة',
            'package_price': float(sub['package_price'] or 0.0),
            'rate_download': sub['rate_download'] if sub.get('rate_download') is not None else '2M',
            'rate_upload': sub['rate_upload'] if sub.get('rate_upload') is not None else '1M',
            'volume_quota_mb': sub['volume_quota_mb'] or 0,
            'validity_days': sub['validity_days'] or 30,
            'package_desc': sub['package_desc'] or ''
        }
    else:
        # Check voucher
        v_card = query_one("""
            SELECT v.*, p.name as package_name, 
                   COALESCE(v.snap_price, p.price) as package_price,
                   COALESCE(v.snap_rate_download, p.rate_download) as rate_download, 
                   COALESCE(v.snap_rate_upload, p.rate_upload) as rate_upload, 
                   COALESCE(v.snap_volume_quota_mb, p.volume_quota_mb) as volume_quota_mb,
                   COALESCE(v.snap_uptime_limit_mins, p.uptime_limit_mins) as uptime_limit_mins, 
                   COALESCE(v.snap_validity_days, p.validity_days) as validity_days, 
                   p.description as package_desc,
                   b.name as batch_name
            FROM wisp_vouchers v
            JOIN wisp_packages p ON v.package_id = p.id
            JOIN wisp_voucher_batches b ON v.batch_id = b.id
            WHERE LOWER(v.username) = LOWER(?) OR v.pin_code = ?
        """, (username, username))
        if v_card:
            user_info = {
                'type': 'voucher',
                'username': v_card['username'],
                'full_name': f"كرت إنترنت ({v_card['username']})",
                'phone': '',
                'service_type': 'hotspot',
                'balance': float(v_card.get('balance') or 0.0),
                'extra_quota_mb': float(v_card.get('extra_quota_mb') or 0.0),
                'status': v_card['status'],
                'expires_at': str(v_card['expires_at']) if v_card['expires_at'] else 'غير محدد',
                'package_id': v_card['package_id'],
                'package_name': v_card['package_name'] or 'باقة كروت',
                'package_price': float(v_card['package_price'] or 0.0),
                'rate_download': v_card['rate_download'] if v_card.get('rate_download') is not None else '2M',
                'rate_upload': v_card['rate_upload'] if v_card.get('rate_upload') is not None else '1M',
                'volume_quota_mb': v_card['volume_quota_mb'] or 0,
                'validity_days': v_card['validity_days'] or 30,
                'package_desc': v_card['package_desc'] or ''
            }
        else:
            # Generic radusergroup fallback
            group = query_one("SELECT groupname FROM radusergroup WHERE LOWER(username) = LOWER(?)", (username,))
            user_info = {
                'type': 'radius_user',
                'username': username,
                'full_name': username,
                'phone': '',
                'service_type': 'pppoe',
                'balance': 0.0,
                'status': 'active',
                'expires_at': 'غير محدد',
                'package_id': None,
                'package_name': group['groupname'] if group else 'Default Profile',
                'package_price': 0.0,
                'rate_download': '2M',
                'rate_upload': '1M',
                'volume_quota_mb': 0,
                'validity_days': 30,
                'package_desc': ''
            }

    # 2. Check active live connection in radacct with Interim-Update Heartbeat
    if DB_TYPE == 'mysql':
        active_session = query_one("""
            SELECT * FROM radacct
            WHERE LOWER(username) = LOWER(?) 
              AND acctstoptime IS NULL
              AND (
                (acctupdatetime IS NOT NULL AND acctupdatetime >= DATE_SUB(NOW(), INTERVAL 5 MINUTE))
                OR
                (acctupdatetime IS NULL AND acctstarttime >= DATE_SUB(NOW(), INTERVAL 5 MINUTE))
              )
            ORDER BY radacctid DESC LIMIT 1
        """, (username,))
    else:
        cutoff_str = get_heartbeat_cutoff_str(5)
        active_session = query_one("""
            SELECT * FROM radacct
            WHERE LOWER(username) = LOWER(?) 
              AND acctstoptime IS NULL
              AND (
                (acctupdatetime IS NOT NULL AND acctupdatetime >= ?)
                OR
                (acctupdatetime IS NULL AND acctstarttime >= ?)
              )
            ORDER BY radacctid DESC LIMIT 1
        """, (username, cutoff_str, cutoff_str))

    if active_session:
        user_info['is_online'] = True
        user_info['current_ip'] = active_session.get('framedipaddress') or '-'
        user_info['mac_address'] = active_session.get('callingstationid') or '-'
        user_info['session_start'] = str(active_session.get('acctstarttime'))
        user_info['nas_ip'] = active_session.get('nasipaddress') or '-'
        sess_time = int(active_session.get('acctsessiontime') or 0)
        user_info['uptime_str'] = format_duration(sess_time) if sess_time > 0 else 'أقل من دقيقة'
        bytes_out = (int(active_session.get('acctoutputgigawords') or 0) * 4294967296) + int(active_session.get('acctoutputoctets') or 0)
        bytes_in = (int(active_session.get('acctinputgigawords') or 0) * 4294967296) + int(active_session.get('acctinputoctets') or 0)
        user_info['bytes_out_str'] = format_bytes(bytes_out)
        user_info['bytes_in_str'] = format_bytes(bytes_in)
    else:
        user_info['is_online'] = False
        user_info['current_ip'] = '-'
        user_info['mac_address'] = '-'
        user_info['session_start'] = '-'
        user_info['nas_ip'] = '-'
        user_info['uptime_str'] = 'غير متصل'
        user_info['bytes_out_str'] = '0 MB'
        user_info['bytes_in_str'] = '0 MB'

    cycle_start = None
    if sub:
        cycle_start = sub.get('last_renewed_at') or sub.get('created_at')
    elif v_card:
        cycle_start = v_card.get('last_renewed_at') or v_card.get('first_used_at') or v_card.get('created_at')

    # 3. Calculate current cycle consumption via unified accounting engine (Defect 9)
    from services.quota_service import get_accounting_totals, AccountingReadError
    try:
        usage = get_accounting_totals(username, since_timestamp=cycle_start)
        user_info['accounting_available'] = True
    except AccountingReadError:
        usage = {}
        user_info['accounting_available'] = False
    raw_in = usage.get('up_bytes') or 0
    raw_out = usage.get('down_bytes') or 0
    raw_time = usage.get('uptime_secs') or 0

    user_info['total_download'] = format_bytes(raw_out)
    user_info['total_upload'] = format_bytes(raw_in)
    user_info['total_traffic'] = format_bytes(usage.get('total_bytes') or (raw_in + raw_out))
    user_info['total_duration'] = format_duration(raw_time)

    # Quota calculations
    base_quota_mb = float(user_info.get('volume_quota_mb') or 0)
    extra_quota_mb = float(user_info.get('extra_quota_mb') or 0)
    total_allowed_quota_mb = base_quota_mb + extra_quota_mb

    if not user_info['accounting_available']:
        unavailable = 'بيانات الاستهلاك غير متاحة مؤقتاً'
        for field in ('total_download', 'total_upload', 'total_traffic', 'total_duration',
                      'quota_used_str', 'quota_rem_str'):
            user_info[field] = unavailable
        user_info.update(has_quota=base_quota_mb > 0, is_unlimited=base_quota_mb == 0,
                         quota_used_mb=None, quota_total_mb=total_allowed_quota_mb,
                         quota_rem_mb=None, quota_total_str=format_mb_or_gb(total_allowed_quota_mb),
                         quota_percent=0, quota_used_percent=0)
    elif base_quota_mb > 0:
        total_used_mb = float(raw_in + raw_out) / (1024.0 * 1024.0)
        rem_mb = max(0.0, total_allowed_quota_mb - total_used_mb)
        user_info['has_quota'] = True
        user_info['is_unlimited'] = False
        user_info['quota_used_mb'] = round(total_used_mb, 2)
        user_info['quota_total_mb'] = total_allowed_quota_mb
        user_info['quota_rem_mb'] = round(rem_mb, 2)
        user_info['quota_used_str'] = format_mb_or_gb(total_used_mb)
        user_info['quota_total_str'] = format_mb_or_gb(total_allowed_quota_mb)
        user_info['quota_rem_str'] = format_mb_or_gb(rem_mb)
        user_info['quota_percent'] = max(0.0, min(100.0, round((rem_mb / total_allowed_quota_mb) * 100.0, 1))) if total_allowed_quota_mb > 0 else 0
        user_info['quota_used_percent'] = min(100.0, round((total_used_mb / total_allowed_quota_mb) * 100.0, 1)) if total_allowed_quota_mb > 0 else 100
    else:
        user_info['has_quota'] = False
        user_info['is_unlimited'] = True
        user_info['quota_used_mb'] = 0.00
        user_info['quota_total_mb'] = 'غير محدود (Unlimited)'
        user_info['quota_rem_mb'] = 'غير محدود'
        user_info['quota_used_str'] = '0 MB'
        user_info['quota_total_str'] = 'غير محدود (Unlimited)'
        user_info['quota_rem_str'] = 'غير محدود'
        user_info['quota_percent'] = 100.0
        user_info['quota_used_percent'] = 0.0

    # Remaining Time calculation
    rem_time_str, rem_sec = format_remaining_time(user_info.get('expires_at'))
    user_info['remaining_time'] = rem_time_str
    user_info['remaining_seconds'] = rem_sec

    # Loan Eligibility calculation (السلفة)
    if user_info.get('type') == 'subscriber':
        settings_rows = query_all("SELECT `key`, `value` FROM wisp_system_settings WHERE `key` IN ('loan_threshold_mb', 'loan_amount_mb', 'allow_data_loan')")
        settings_dict = {r['key']: r['value'] for r in settings_rows} if settings_rows else {}
        try:
            threshold_mb = float(settings_dict.get('loan_threshold_mb', 100) or 100)
            if threshold_mb <= 0:
                threshold_mb = 100.0
        except Exception:
            threshold_mb = 100.0

        rem_val = user_info.get('quota_rem_mb')
        loan_stat = int(user_info.get('loan_status') or 0)
        quota_is_low = False
        
        user_status = str(user_info.get('status') or '').lower().strip()
        is_admin_blocked = user_status in ['disabled', 'suspended']

        # Check expired or low quota based on dynamic threshold (excluding administratively blocked accounts)
        if not is_admin_blocked:
            if rem_sec <= 0 or user_status == 'expired':
                quota_is_low = True
            elif isinstance(rem_val, (int, float)) and rem_val <= threshold_mb:
                quota_is_low = True
            elif str(user_info.get('quota_rem_str')) == '0 MB':
                quota_is_low = True

        allow_loan = settings_dict.get('allow_data_loan', '1') in ('1', 'true', 'True', 1, True)
        try:
            loan_amount_val = int(settings_dict.get('loan_amount_mb', 1024) or 1024)
            if loan_amount_val <= 0:
                loan_amount_val = 1024
        except Exception:
            loan_amount_val = 1024

        user_info['allow_data_loan'] = allow_loan
        user_info['loan_amount_mb'] = loan_amount_val
        user_info['loan_amount_str'] = format_mb_or_gb(loan_amount_val)
        user_info['loan_threshold_mb'] = threshold_mb
        user_info['loan_threshold_str'] = format_mb_or_gb(threshold_mb)
        user_info['can_request_loan'] = bool(user_info['accounting_available'] and allow_loan and loan_stat == 0 and quota_is_low and not is_admin_blocked)
    else:
        user_info['allow_data_loan'] = False
        user_info['loan_amount_mb'] = 1024
        user_info['loan_amount_str'] = '1 GB'
        user_info['loan_threshold_mb'] = 100.0
        user_info['loan_threshold_str'] = '100 MB'
        user_info['loan_balance_mb'] = 0
        user_info['loan_status'] = 0
        user_info['can_request_loan'] = False

    # 3. Active QoS Rate-Limit from radreply (Live Speed Override)
    rad_rate = query_one("SELECT value FROM radreply WHERE LOWER(username) = LOWER(?) AND attribute = 'MikroTik-Rate-Limit'", (username,))
    if rad_rate and rad_rate.get('value'):
        val = rad_rate['value'].strip()
        parts = val.split('/')
        val_down = parts[0].strip()
        val_up = parts[1].strip().split()[0] if len(parts) > 1 else val_down
        user_info['rate_download'] = val_down
        user_info['rate_upload'] = val_up
        user_info['current_rate_str'] = val
        user_info['is_open_speed'] = True if val in ['0/0', '0M/0M', '0K/0K', '0'] else False
    else:
        if str(user_info.get('rate_download', '')).strip() in ['0', '0M', '0K', '']:
            user_info['is_open_speed'] = True
            user_info['current_rate_str'] = '0/0'
        else:
            user_info['is_open_speed'] = False
            user_info['current_rate_str'] = f"{user_info.get('rate_download', '2M')}/{user_info.get('rate_upload', '1M')}"

    return user_info

def _sync_recharge_radius(cursor, conn, username, sub, card, target_type, new_fr_exp):
    """Write authentication on the billing connection; any failure rolls back the billing transaction."""
    def run(sql, params):
        cursor.execute(adapt_query(sql, conn), params)

    if target_type == 'subscriber':
        for table in ('radcheck', 'radreply'):
            run(f"DELETE FROM {table} WHERE LOWER(username) = LOWER(?)", (username,))
    else:
        run("DELETE FROM radcheck WHERE LOWER(username) = LOWER(?) AND (attribute IN ('Cleartext-Password', 'Expiration', 'Max-Total-Octets') OR (attribute = 'Auth-Type' AND value = 'Reject'))", (username,))
    password = sub.get('password') or sub.get('pin_code') or username
    run("INSERT INTO radcheck (username, attribute, op, value) VALUES (?, 'Cleartext-Password', ':=', ?)", (username, password))
    run("DELETE FROM radusergroup WHERE LOWER(username) = LOWER(?)", (username,))
    run("INSERT INTO radusergroup (username, groupname, priority) VALUES (?, ?, 1)", (username, card['package_name']))
    if new_fr_exp:
        run("INSERT INTO radcheck (username, attribute, op, value) VALUES (?, 'Expiration', ':=', ?)", (username, new_fr_exp))
    if target_type == 'voucher':
        run("DELETE FROM radreply WHERE LOWER(username) = LOWER(?) AND attribute IN ('MikroTik-Rate-Limit', 'Mikrotik-Group')", (username,))
        for attribute, value in (('MikroTik-Rate-Limit', card['policy_rate_limit_str']), ('Mikrotik-Group', card['pkg_mikrotik_group'])):
            if value:
                run("INSERT INTO radreply (username, attribute, op, value) VALUES (?, ?, ':=', ?)", (username, attribute, value))
    if target_type == 'subscriber':
        mac = str(sub.get('mac_binding') or '').strip()
        if len(mac) > 5:
            run("INSERT INTO radcheck (username, attribute, op, value) VALUES (?, 'Calling-Station-Id', '==', ?)", (username, mac.replace('-', ':').upper()))
        static_ip = str(sub.get('static_ip') or '').strip()
        if len(static_ip) > 6:
            run("INSERT INTO radreply (username, attribute, op, value) VALUES (?, 'Framed-IP-Address', ':=', ?)", (username, static_ip))
        group = str(sub.get('mikrotik_group') or card.get('pkg_mikrotik_group') or '').strip()
        if group:
            run("INSERT INTO radreply (username, attribute, op, value) VALUES (?, 'Mikrotik-Group', ':=', ?)", (username, group))


def recharge_user_wallet_by_card(username, card_code, recharge_type='balance'):
    """
    Recharges user balance or directly tops up package data and validity duration using an unused voucher card.
    recharge_type: 'balance' (شحن رصيد مالي) | 'package' (شحن كباقة وإضافة البيانات والوقت)
    Features:
    - Automatic Data Rollover (ترحيل الرصيد التراكمي المتبقي إذا كانت الباقة تدعم الترحيل)
    - Automatic Loyalty Points Award (منح نقاط الولاء تلقائياً لمحفظة المشترك إذا كانت الباقة مفعلة بالنقاط)
    - Supports both PPP/DHCP subscribers (wisp_subscribers) and Hotspot card users (wisp_vouchers)
    - Concurrency-safe atomic execution & FreeRADIUS sync
    """
    username = (username or '').strip()
    card_code = (card_code or '').strip()
    recharge_type = (recharge_type or 'balance').strip().lower()

    if not card_code:
        return False, "يرجى إدخال رقم كرت الشحن أو القسيمة"

    # Atomic execution using db_session
    try:
        with db_session() as conn:
            cursor = conn.cursor()
            lock_clause = "FOR UPDATE" if is_mysql_conn(conn) else ""
            
            # 1. Lock and find unused card with package details including loyalty & rollover
            sql_card = adapt_query(f"""
                SELECT v.*, 
                       COALESCE(v.snap_price, p.price, 0) as card_price, 
                       p.name as package_name, COALESCE(v.snap_mikrotik_group, p.mikrotik_group) as pkg_mikrotik_group,
                       COALESCE(v.snap_cost, p.cost, 0) as policy_cost,
                       COALESCE(v.snap_uptime_limit_mins, p.uptime_limit_mins, 0) as uptime_limit_mins,
                       COALESCE(v.snap_rate_download, p.rate_download, '0') as policy_rate_download,
                       COALESCE(v.snap_rate_upload, p.rate_upload, '0') as policy_rate_upload,
                       COALESCE(v.snap_rate_limit_str, CONCAT(p.rate_download, '/', p.rate_upload), '0/0') as policy_rate_limit_str,
                       COALESCE(v.snap_simultaneous_sessions, p.simultaneous_sessions, 1) as policy_simultaneous_sessions,
                       COALESCE(v.snap_volume_quota_mb, p.volume_quota_mb, 0) as volume_quota_mb, 
                       COALESCE(v.snap_validity_value, p.validity_value) as validity_value, 
                       COALESCE(NULLIF(v.snap_validity_unit, ''), p.validity_unit) as validity_unit, 
                       COALESCE(v.snap_validity_days, p.validity_days) as validity_days,
                       COALESCE(p.is_rollover_enabled, 0) as is_rollover_enabled,
                       COALESCE(p.is_loyalty_enabled, 0) as is_loyalty_enabled,
                       COALESCE(p.loyalty_points, 0) as loyalty_points,
                       b.name as batch_name
                FROM wisp_vouchers v
                JOIN wisp_packages p ON v.package_id = p.id
                JOIN wisp_voucher_batches b ON v.batch_id = b.id
                WHERE (v.username = ? OR v.pin_code = ? OR v.serial_number = ?)
                LIMIT 1 {lock_clause}
            """, conn)
            cursor.execute(sql_card, (card_code, card_code, card_code))
            card = cursor.fetchone()
            if not card:
                return False, "رقم الكرت أو القسيمة غير موجود في النظام. يرجى التأكد من الرقم والمحاولة مجدداً."
            if not isinstance(card, dict):
                card = dict(card)
            if card['status'] != 'unused':
                return False, "هذا الكرت مستخدم مسبقاً أو منتهي الصلاحية ولا يمكن استخدامه."

            card_value = float(card['card_price'] or 0.0)

            # 2. Lock target user (check wisp_subscribers first, then wisp_vouchers)
            sql_sub = adapt_query(f"SELECT * FROM wisp_subscribers WHERE LOWER(username) = LOWER(?) LIMIT 1 {lock_clause}", conn)
            cursor.execute(sql_sub, (username,))
            sub = cursor.fetchone()
            target_type = 'subscriber'
            if sub and not isinstance(sub, dict):
                sub = dict(sub)

            if not sub:
                sql_voucher = adapt_query(f"SELECT * FROM wisp_vouchers WHERE LOWER(username) = LOWER(?) LIMIT 1 {lock_clause}", conn)
                cursor.execute(sql_voucher, (username,))
                v_target = cursor.fetchone()
                if v_target:
                    target_type = 'voucher'
                    sub = dict(v_target) if not isinstance(v_target, dict) else v_target
                else:
                    return False, "حساب المشترك غير مسجل في قائمة الاشتراكات أو الكروت."

            if str(sub['username']).lower() == str(card['username']).lower():
                return False, 'لا يمكن استخدام الكرت لشحن الحساب نفسه'
            if recharge_type == 'package' and str(sub.get('status') or '').lower() in ('disabled', 'suspended', 'recharged'):
                return False, 'لا يمكن شحن باقة لحساب معطل أو موقوف أو كرت مستهلك للشحن.'
            username = sub['username']
            loan_mb = int(sub.get('loan_balance_mb') or 0)
            loan_status = int(sub.get('loan_status') or 0)
            has_active_loan = (loan_status == 1 or loan_mb > 0)
            rem_data_mb = 0.0
            is_rollover = False

            # Fetch single DB timestamp for cycle boundary and renewal (Defect 3 & 11)
            cursor.execute("SELECT CURRENT_TIMESTAMP")
            r_now = cursor.fetchone()
            db_now = r_now[0] if not isinstance(r_now, dict) else list(r_now.values())[0]
            if isinstance(db_now, str):
                db_now_dt = datetime.datetime.strptime(db_now.split('.')[0].strip(), '%Y-%m-%d %H:%M:%S')
            else:
                db_now_dt = db_now
            db_now_str = db_now_dt.strftime('%Y-%m-%d %H:%M:%S')

            if recharge_type == 'package':
                new_base_quota_mb = float(card.get('volume_quota_mb') or 0.0)
                add_quota_mb = new_base_quota_mb

                # --- ROLLOVER DATA CALCULATION ---
                curr_pkg_id = sub.get('package_id')
                curr_pkg = None
                if curr_pkg_id:
                    cursor.execute(adapt_query("SELECT * FROM wisp_packages WHERE id = ?", conn), (curr_pkg_id,))
                    curr_pkg = cursor.fetchone()
                    if curr_pkg and not isinstance(curr_pkg, dict):
                        curr_pkg = dict(curr_pkg)

                is_rollover = bool(
                    (curr_pkg and curr_pkg.get('is_rollover_enabled')) or 
                    card.get('is_rollover_enabled')
                )

                # 1. Defect 1 & 8: Calculate cycle consumption & rollover BEFORE recording session baselines
                try:
                    calc = calculate_cycle_usage_and_rollover(sub, curr_pkg, is_rollover, conn=conn, now=db_now_dt)
                    rem_data_mb = calc['rem_data_mb']
                    rem_time_delta = calc['rem_time_delta']
                except Exception as e:
                    conn.rollback()
                    return False, f"تعذر قراءة استهلاك الحساب الحالي من سجلات المحاسبة، تم إلغاء عملية الشحن بأمان: {e}"

                # 2. Record session baselines using the DB renewal timestamp
                record_session_baselines(username, renewed_at=db_now_str, conn=conn)

                # 3. Defect 4: Comprehensive loan settlement against new package capacity & rollover
                is_unlimited_quota = (new_base_quota_mb == 0)

                if is_rollover:
                    new_extra_mb = float(rem_data_mb)
                else:
                    new_extra_mb = 0.0

                new_loan_balance_mb = 0
                new_loan_status = 0
                deducted_loan_mb = 0

                if has_active_loan and target_type == 'subscriber':
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

                net_quota_mb = max(0.0, float(new_base_quota_mb + new_extra_mb)) if not is_unlimited_quota else float(new_base_quota_mb)

                # 4. Defect 5: Validity calculation (single addition of remaining time)
                val = card.get('validity_value') if card.get('validity_value') is not None else (card.get('validity_days') or 30)
                unit = str(card.get('validity_unit') or 'days').lower().strip()
                if int(val) <= 0:
                    new_exp_iso = None
                    new_fr_exp = None
                else:
                    if unit in ['minutes', 'minute', 'دقائق', 'دقيقة']:
                        delta = datetime.timedelta(minutes=int(val))
                    elif unit in ['hours', 'hour', 'ساعات', 'ساعة']:
                        delta = datetime.timedelta(hours=int(val))
                    elif unit in ['months', 'month', 'أشهر', 'شهر']:
                        delta = datetime.timedelta(days=int(val) * 30)
                    else:
                        delta = datetime.timedelta(days=int(val))

                    new_exp_dt = db_now_dt + delta + rem_time_delta
                    new_exp_iso = new_exp_dt.strftime('%Y-%m-%d %H:%M:%S')
                    new_fr_exp = new_exp_dt.strftime('%d %b %Y %H:%M:%S')

                # Mark card as recharged first inside transaction
                card_expire_msg = f"تم استخدامه في شحن الباقة (تم سداد سلفة {format_mb_or_gb(deducted_loan_mb)})" if deducted_loan_mb > 0 else "تم استخدامه في شحن الباقة والوقت"
                sql_up_card = adapt_query("""
                    UPDATE wisp_vouchers SET
                        status = 'recharged',
                        expire_reason = ?,
                        first_used_at = ?,
                        snap_volume_quota_mb = ?,
                        bound_mac = ?
                    WHERE id = ? AND status = 'unused'
                """, conn)
                cursor.execute(sql_up_card, (card_expire_msg, db_now_str, add_quota_mb, f"TOPUP:{username}"[:28], card['id']))
                if cursor.rowcount != 1:
                    conn.rollback()
                    return False, "عذراً، هذا الكرت تم استخدامه في نفس اللحظة أو لم يعد متاحاً."

                # Update subscriber / voucher record inside transaction
                if target_type == 'subscriber':
                    sql_up_sub = adapt_query("""
                        UPDATE wisp_subscribers SET
                            status = 'active',
                            package_id = ?,
                            expires_at = ?,
                            extra_quota_mb = ?,
                            loan_balance_mb = ?,
                            loan_status = ?,
                            snap_volume_quota_mb = ?,
                            last_renewed_at = ?
                        WHERE id = ?
                    """, conn)
                    cursor.execute(sql_up_sub, (card['package_id'], new_exp_iso, new_extra_mb, new_loan_balance_mb, new_loan_status, new_base_quota_mb, db_now_str, sub['id']))
                else:
                    sql_up_v = adapt_query("""
                        UPDATE wisp_vouchers SET
                            status = 'active',
                            package_id = ?,
                            expires_at = ?,
                            extra_quota_mb = ?,
                            snap_volume_quota_mb = ?,
                            snap_uptime_limit_mins = ?,
                            snap_validity_value = ?, snap_validity_unit = ?, snap_validity_days = ?,
                            snap_price = ?, snap_cost = ?,
                            snap_rate_download = ?, snap_rate_upload = ?, snap_rate_limit_str = ?,
                            snap_simultaneous_sessions = ?, snap_mikrotik_group = ?,
                            expire_reason = '',
                            last_renewed_at = ?
                        WHERE id = ?
                    """, conn)
                    cursor.execute(sql_up_v, (
                        card['package_id'], new_exp_iso, new_extra_mb, add_quota_mb,
                        card['uptime_limit_mins'], val, unit, card['validity_days'],
                        card_value, card['policy_cost'], card['policy_rate_download'], card['policy_rate_upload'],
                        card['policy_rate_limit_str'], card['policy_simultaneous_sessions'], card['pkg_mikrotik_group'],
                        db_now_str, sub['id']))

                # Record in sales inside transaction
                sql_sales = adapt_query("""
                    INSERT INTO wisp_voucher_sales (
                        voucher_id, batch_id, batch_name, username, serial_number,
                        package_name, price, cost, reseller_id, activated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 0.0, NULL, CURRENT_TIMESTAMP)
                """, conn)
                cursor.execute(sql_sales, (card['id'], card['batch_id'], card['batch_name'], card['username'], card['serial_number'], card['package_name'], card_value))

            else:
                # Balance recharge
                if card_value <= 0:
                    card_value = 10.0

                # Mark card as recharged first inside transaction
                sql_up_card = adapt_query("""
                    UPDATE wisp_vouchers SET
                        status = 'recharged',
                        expire_reason = 'تم استخدامه في شحن الرصيد',
                        first_used_at = CURRENT_TIMESTAMP,
                        snap_volume_quota_mb = 0,
                        bound_mac = ?
                    WHERE id = ? AND status = 'unused'
                """, conn)
                cursor.execute(sql_up_card, (f"RECHARGE:{username}"[:28], card['id']))
                if cursor.rowcount != 1:
                    conn.rollback()
                    return False, "عذراً، هذا الكرت تم استخدامه في نفس اللحظة أو لم يعد متاحاً."

                # Update subscriber / voucher balance inside transaction
                if target_type == 'subscriber':
                    sql_up_sub = adapt_query("UPDATE wisp_subscribers SET balance = COALESCE(balance, 0) + ? WHERE id = ?", conn)
                    cursor.execute(sql_up_sub, (card_value, sub['id']))
                else:
                    sql_up_v = adapt_query("UPDATE wisp_vouchers SET balance = COALESCE(balance, 0) + ? WHERE id = ?", conn)
                    cursor.execute(sql_up_v, (card_value, sub['id']))

                # Record in sales inside transaction
                sql_sales = adapt_query("""
                    INSERT INTO wisp_voucher_sales (
                        voucher_id, batch_id, batch_name, username, serial_number,
                        package_name, price, cost, reseller_id, activated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 0.0, NULL, CURRENT_TIMESTAMP)
                """, conn)
                cursor.execute(sql_sales, (card['id'], card['batch_id'], card['batch_name'], card['username'], card['serial_number'], card['package_name'], card_value))

            if recharge_type == 'package':
                _sync_recharge_radius(cursor, conn, username, sub, card, target_type, new_fr_exp)
            for table in ('radcheck', 'radreply', 'radusergroup'):
                cursor.execute(adapt_query(f"DELETE FROM {table} WHERE LOWER(username) = LOWER(?)", conn), (card['username'],))

        # Billing and RADIUS changes are committed together; notifications follow.

        # Award loyalty points if package has loyalty enabled
        points_awarded = 0
        is_loyalty = bool(card.get('is_loyalty_enabled'))
        pkg_points = int(card.get('loyalty_points') or 0)
        if is_loyalty and pkg_points > 0:
            try:
                from services.loyalty_rewards_service import award_loyalty_points
                reason_txt = f"شحن كرت باقة: {card['package_name']} ({card['serial_number']})"
                ok_pts, _ = award_loyalty_points(username, pkg_points, reason=reason_txt)
                if ok_pts:
                    points_awarded = pkg_points
            except Exception as e_pts:
                print(f"Error awarding loyalty points on card recharge: {e_pts}")

        loyalty_msg = f" وتمت إضافة {points_awarded} نقطة ولاء إلى رصيدك! 🪙" if points_awarded > 0 else ""
        rollover_msg = f" (تم ترحيل {format_mb_or_gb(rem_data_mb)} من رصيدك السابق)" if is_rollover and rem_data_mb > 0 else ""

        if recharge_type == 'package':
            dispatch_async_disconnect(username)

            # Trigger Telegram alert for recharge if enabled
            try:
                from services.bot_notifications_service import trigger_recharge_notification
                trigger_recharge_notification(
                    username=username,
                    package_name=card.get('package_name', 'باقة مخصصة'),
                    price=float(card.get('card_price') or card_value or 0.0),
                    card_number=card.get('serial_number') or card_code,
                    recharge_type=recharge_type
                )
            except Exception as e_tg:
                print(f"[Telegram Hook Recharge Error]: {e_tg}")

            if deducted_loan_mb > 0:
                return True, f"تم شحن الباقة بنجاح! تم سداد السلفة السابقة ({format_mb_or_gb(deducted_loan_mb)}) وإضافة السعة الصافية ({format_mb_or_gb(net_quota_mb)}){rollover_msg} وتمديد الصلاحية حتى {new_exp_iso}.{loyalty_msg}"
            else:
                return True, f"تم شحن الباقة بنجاح! تمت إضافة {format_mb_or_gb(add_quota_mb)} بيانات{rollover_msg} وتمديد الصلاحية حتى {new_exp_iso}.{loyalty_msg}"
        else:
            new_bal = float(sub.get('balance') or 0.0) + card_value

            # Trigger Telegram alert for balance topup
            try:
                from services.bot_notifications_service import trigger_recharge_notification
                trigger_recharge_notification(
                    username=username,
                    package_name='رصيد مالي بالمحفظة',
                    price=float(card_value),
                    card_number=card.get('serial_number') or card_code,
                    recharge_type='balance'
                )
            except Exception as e_tg:
                print(f"[Telegram Hook Recharge Error]: {e_tg}")

            return True, f"تم شحن محفظتك بنجاح بمبلغ {card_value:.2f}. رصيدك الحالي أصبح: {new_bal:.2f}.{loyalty_msg}"

    except Exception as ex:
        return False, f"فشل تنفيذ عملية الشحن: {str(ex)}"


def request_data_loan(username):
    """
    Handles Data Loan (السلفة) for subscribers dynamically based on system settings:
    1. Reads allow_data_loan and loan_amount_mb from wisp_system_settings.
    2. Validates subscriber exists and loan_status == 0 under FOR UPDATE lock.
    3. Prevents reactivating administratively disabled or suspended subscribers (Defect 10).
    4. Validates remaining quota < threshold or expired using get_accounting_totals.
    5. Sets loan_balance_mb = loan_amount_mb, loan_status = 1.
    6. Increases extra_quota_mb by loan_amount_mb.
    7. Extends expiration by 24 hours.
    8. Updates FreeRADIUS radcheck and disconnects user session.
    """
    username = (username or '').strip()
    if not username:
        return False, "اسم المستخدم غير محدد"

    # Check system settings
    settings_rows = query_all("SELECT `key`, `value` FROM wisp_system_settings WHERE `key` IN ('allow_data_loan', 'loan_amount_mb', 'loan_threshold_mb')")
    settings_dict = {r['key']: r['value'] for r in settings_rows} if settings_rows else {}

    allow_loan = settings_dict.get('allow_data_loan', '1')
    if allow_loan in ('0', 'false', 'False', 0):
        return False, "عذراً، خدمة السلفة معطلة حالياً من قِبل إدارة الشبكة."

    try:
        loan_amount_mb = int(settings_dict.get('loan_amount_mb', 1024))
        if loan_amount_mb <= 0:
            loan_amount_mb = 1024
    except (ValueError, TypeError):
        loan_amount_mb = 1024

    try:
        threshold_mb = float(settings_dict.get('loan_threshold_mb', 100) or 100)
        if threshold_mb <= 0:
            threshold_mb = 100.0
    except Exception:
        threshold_mb = 100.0

    with db_session() as conn:
        cursor = conn.cursor()
        lock_clause = "FOR UPDATE" if is_mysql_conn(conn) else ""

        sql_sub = adapt_query(f"""
            SELECT s.*, p.name as package_name, COALESCE(s.snap_volume_quota_mb, p.volume_quota_mb, 0) as volume_quota_mb
            FROM wisp_subscribers s
            JOIN wisp_packages p ON s.package_id = p.id
            WHERE LOWER(s.username) = LOWER(?)
            LIMIT 1 {lock_clause}
        """, conn)
        cursor.execute(sql_sub, (username,))
        sub = cursor.fetchone()
        if not sub:
            return False, "حساب المشترك غير مسجل في قائمة الاشتراكات."
        if not isinstance(sub, dict):
            sub = dict(sub)

        # Defect 10: Block suspended and disabled accounts from requesting loans
        sub_status = str(sub.get('status') or '').lower().strip()
        if sub_status in ('disabled', 'suspended'):
            return False, "لا يمكن طلب سلفة لحساب معطل أو موقوف إدارياً من قِبل إدارة الشبكة."

        # Re-verify loan status under lock
        if int(sub.get('loan_status') or 0) == 1 or int(sub.get('loan_balance_mb') or 0) > 0:
            existing_loan = int(sub.get('loan_balance_mb') or loan_amount_mb)
            return False, f"لديك سلفة نشطة مسبقاً بقيمة {format_mb_or_gb(existing_loan)} لم يتم سدادها بعد. يرجى شحن كرت لسداد السلفة."

        # Verify quota / expiry under lock using get_accounting_totals (Defect 9 & 10)
        from services.quota_service import get_accounting_totals
        cycle_start = sub.get('last_renewed_at') or sub.get('created_at')
        try:
            usage = get_accounting_totals(username, since_timestamp=cycle_start, conn=conn)
        except Exception as e:
            return False, f"تعذر التحقق من أهلية السلفة بسبب خطأ في قراءة سجلات الاستهلاك: {e}"
        base_quota_mb = float(sub.get('volume_quota_mb') or 0)
        extra_quota_mb = float(sub.get('extra_quota_mb') or 0)
        total_allowed_quota_mb = base_quota_mb + extra_quota_mb

        quota_is_low = False
        cursor.execute("SELECT CURRENT_TIMESTAMP")
        r_now = cursor.fetchone()
        db_now = r_now[0] if not isinstance(r_now, dict) else list(r_now.values())[0]
        if isinstance(db_now, str):
            db_now_dt = datetime.datetime.strptime(db_now.split('.')[0].strip(), '%Y-%m-%d %H:%M:%S')
        else:
            db_now_dt = db_now

        current_exp = sub.get('expires_at')
        current_exp_dt = None
        if current_exp:
            if isinstance(current_exp, datetime.datetime):
                current_exp_dt = current_exp.replace(tzinfo=None) if current_exp.tzinfo else current_exp
            else:
                try:
                    current_exp_dt = datetime.datetime.strptime(str(current_exp).split('.')[0].strip(), '%Y-%m-%d %H:%M:%S')
                except Exception:
                    current_exp_dt = None

        if current_exp_dt and current_exp_dt <= db_now_dt:
            quota_is_low = True
        elif sub_status == 'expired':
            quota_is_low = True
        elif base_quota_mb > 0:
            used_mb = float(usage['total_bytes']) / (1024.0 * 1024.0)
            rem_mb = max(0.0, total_allowed_quota_mb - used_mb)
            if rem_mb <= threshold_mb:
                quota_is_low = True

        if not quota_is_low:
            thresh_str = format_mb_or_gb(threshold_mb)
            return False, f"طلب السلفة متاح فقط عند اقتراب انتهاء الرصيد (أقل من {thresh_str}) أو انتهاء الصلاحية."

        # 24 Hours extension from max(current_exp, db_now)
        if current_exp_dt and current_exp_dt > db_now_dt:
            new_exp_dt = current_exp_dt + datetime.timedelta(hours=24)
        else:
            new_exp_dt = db_now_dt + datetime.timedelta(hours=24)

        new_exp_iso = new_exp_dt.strftime('%Y-%m-%d %H:%M:%S')
        new_fr_exp = new_exp_dt.strftime('%d %b %Y %H:%M:%S')

        loan_mb = loan_amount_mb
        new_extra_mb = float(sub.get('extra_quota_mb') or 0.0) + loan_mb
        new_status = 'active' if sub_status == 'expired' else sub['status']

        upd_sub = adapt_query("""
            UPDATE wisp_subscribers SET
                loan_balance_mb = ?,
                loan_status = 1,
                extra_quota_mb = ?,
                expires_at = ?,
                status = ?
            WHERE id = ?
        """, conn)
        cursor.execute(upd_sub, (loan_mb, new_extra_mb, new_exp_iso, new_status, sub['id']))

        cursor.execute(adapt_query("DELETE FROM radcheck WHERE LOWER(username) = LOWER(?) AND attribute = 'Max-Total-Octets'", conn), (username,))
        cursor.execute(adapt_query("DELETE FROM radcheck WHERE LOWER(username) = LOWER(?) AND attribute = 'Expiration'", conn), (username,))
        cursor.execute(adapt_query("INSERT INTO radcheck (username, attribute, op, value) VALUES (?, 'Expiration', ':=', ?)", conn), (username, new_fr_exp))

        cursor.execute(adapt_query("SELECT 1 FROM radcheck WHERE LOWER(username) = LOWER(?) AND attribute = 'Cleartext-Password'", conn), (username,))
        if not cursor.fetchone():
            cursor.execute(adapt_query("INSERT INTO radcheck (username, attribute, op, value) VALUES (?, 'Cleartext-Password', ':=', ?)", conn), (username, sub['password']))

        cursor.execute(adapt_query("SELECT 1 FROM radusergroup WHERE LOWER(username) = LOWER(?)", conn), (username,))
        if not cursor.fetchone():
            cursor.execute(adapt_query("INSERT INTO radusergroup (username, groupname, priority) VALUES (?, ?, 1)", conn), (username, sub['package_name']))

    # Outside transaction: Disconnect session and Log audit
    try:
        disconnect_subscriber_session(username)
    except Exception:
        pass

    loan_str = format_mb_or_gb(loan_amount_mb)
    try:
        log_user_audit('subscriber', sub['id'], username, 'self', 'DATA_LOAN', f'حصل المشترك على سلفة بيانات ({loan_str}) صالحة لمدة 24 ساعة')
        log_audit(1, 'system', 'DATA_LOAN', 'subscribers', f'Granted {loan_str} ({loan_mb} MB) data loan (24h) to subscriber {username} (ID: {sub["id"]})')
    except Exception:
        pass

    return True, f"تم تفعيل سلفة البيانات بنجاح بقيمة {loan_str} وتمديد الصلاحية 24 ساعة."


def renew_or_change_package(username, new_pkg_id):
    """
    Renews current package or upgrades to another package from subscriber's balance
    with Data & Time Rollover support inside an atomic transaction.
    """
    username = (username or '').strip()
    if not username:
        return False, "اسم المشترك غير محدد"

    with db_session() as conn:
        cursor = conn.cursor()
        lock_clause = "FOR UPDATE" if is_mysql_conn(conn) else ""

        sql_sub = adapt_query(f"SELECT * FROM wisp_subscribers WHERE LOWER(username) = LOWER(?) LIMIT 1 {lock_clause}", conn)
        cursor.execute(sql_sub, (username,))
        sub = cursor.fetchone()
        if not sub:
            return False, "المشترك غير موجود في سجلات الاشتراكات"
        if not isinstance(sub, dict):
            sub = dict(sub)

        cursor.execute(adapt_query("SELECT * FROM wisp_packages WHERE id = ? AND is_active = 1", conn), (new_pkg_id,))
        pkg = cursor.fetchone()
        if not pkg:
            return False, "الباقة المطلوبة غير متوفرة أو تم إيقافها"
        if not isinstance(pkg, dict):
            pkg = dict(pkg)

        pkg_price = float(pkg['price'] or 0.0)
        user_balance = float(sub['balance'] or 0.0)

        if user_balance < pkg_price:
            needed = pkg_price - user_balance
            return False, f"رصيدك الحالي ({user_balance:.2f}) لا يكفي لتفعيل باقة {pkg['name']} (السعر: {pkg_price:.2f}). ينقصك {needed:.2f}. يرجى شحن رصيدك أولاً."

        # Defect 6: Retrieve current / old package to calculate rollover from!
        curr_pkg_id = sub['package_id']
        cursor.execute(adapt_query("SELECT * FROM wisp_packages WHERE id = ?", conn), (curr_pkg_id,))
        curr_pkg = cursor.fetchone()
        if curr_pkg and not isinstance(curr_pkg, dict):
            curr_pkg = dict(curr_pkg)
        if not curr_pkg:
            curr_pkg = pkg

        is_rollover_enabled = bool(curr_pkg.get('is_rollover_enabled'))

        # Single DB timestamp (Defect 3 & 11)
        cursor.execute("SELECT CURRENT_TIMESTAMP")
        r_now = cursor.fetchone()
        db_now = r_now[0] if not isinstance(r_now, dict) else list(r_now.values())[0]
        if isinstance(db_now, str):
            db_now_dt = datetime.datetime.strptime(db_now.split('.')[0].strip(), '%Y-%m-%d %H:%M:%S')
        else:
            db_now_dt = db_now
        db_now_str = db_now_dt.strftime('%Y-%m-%d %H:%M:%S')

        # 1. Defect 1: Calculate cycle consumption & rollover BEFORE recording session baselines
        try:
            calc = calculate_cycle_usage_and_rollover(sub, curr_pkg, is_rollover_enabled, conn=conn, now=db_now_dt)
            rem_data_mb = calc['rem_data_mb']
            rem_time_delta = calc['rem_time_delta']
            rem_days = calc['rem_days']
            rem_hours = calc['rem_hours']
        except Exception as e:
            conn.rollback()
            return False, f"تعذر قراءة استهلاك الحساب من سجلات المحاسبة، تم إيقاف عملية التجديد بأمان: {e}"

        # 2. Record session baselines for active sessions
        record_session_baselines(username, renewed_at=db_now_str, conn=conn)

        # 3. Defect 7: Full loan settlement
        loan_mb = int(sub.get('loan_balance_mb') or 0)
        has_active_loan = (int(sub.get('loan_status') or 0) == 1 or loan_mb > 0)
        new_base_quota_mb = int(pkg.get('volume_quota_mb') or 0)
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

        # 4. Defect 8: Expiration calculation (single addition of remaining time)
        val = int(pkg.get('validity_value') if pkg.get('validity_value') is not None else (pkg.get('validity_days') or 30))
        unit = pkg.get('validity_unit') or 'days'
        if val <= 0:
            new_expiry = None
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
            new_expiry = new_exp_dt.strftime('%Y-%m-%d %H:%M:%S')
            new_fr_exp = new_exp_dt.strftime('%d %b %Y %H:%M:%S')

        # 5. Atomic update of subscriber
        upd_sql = adapt_query("""
            UPDATE wisp_subscribers SET
                balance = balance - ?,
                snap_volume_quota_mb = NULL,
                package_id = ?,
                status = 'active',
                last_renewed_at = ?,
                extra_quota_mb = ?,
                expires_at = ?,
                loan_balance_mb = ?,
                loan_status = ?
            WHERE id = ? AND balance >= ?
        """, conn)
        cursor.execute(upd_sql, (pkg_price, pkg['id'], db_now_str, new_extra_mb, new_expiry, new_loan_balance_mb, new_loan_status, sub['id'], pkg_price))
        if cursor.rowcount == 0:
            conn.rollback()
            return False, "فشلت العملية: رصيد الحساب غير كافٍ أو حدث تعارض في المعالجة."

        # Authentication and wallet changes must commit or roll back together.
        _sync_recharge_radius(cursor, conn, sub['username'], sub,
                              {'package_name': pkg['name'],
                               'pkg_mikrotik_group': pkg.get('mikrotik_group')},
                              'subscriber', new_fr_exp)

    try:
        dispatch_async_disconnect(sub['username'])
    except Exception:
        pass

    # 6. منح نقاط الولاء إن كانت الباقة تدعم النقاط
    points_awarded = 0
    if pkg.get('is_loyalty_enabled') and int(pkg.get('loyalty_points') or 0) > 0:
        try:
            from services.loyalty_rewards_service import award_loyalty_points
            ok_pts, _ = award_loyalty_points(username, int(pkg['loyalty_points']), reason=f"تجديد باقة {pkg['name']} من الرصيد")
            if ok_pts:
                points_awarded = int(pkg['loyalty_points'])
        except Exception as e_pts:
            print(f"Error awarding points on renew: {e_pts}")

    # 7. تجهيز رسالة التنبيه وسجل التدقيق
    rolled_gb = round(rem_data_mb / 1024.0, 2)
    rollover_parts = []
    if rolled_gb > 0:
        gb_str = f"{int(rolled_gb)}" if rolled_gb.is_integer() else f"{rolled_gb:.2f}"
        rollover_parts.append(f"{gb_str} جيجابايت")
    if rem_days > 0:
        rollover_parts.append(f"{rem_days} {'أيام' if 3 <= rem_days <= 10 else 'يوم'}")
    elif rem_hours > 0:
        rollover_parts.append(f"{rem_hours} {'ساعات' if 3 <= rem_hours <= 10 else 'ساعة'}")

    loyalty_text = f" وتمت إضافة {points_awarded} نقطة ولاء إلى رصيدك! 🪙" if points_awarded > 0 else ""
    if is_rollover_enabled and rollover_parts:
        rollover_text = " و ".join(rollover_parts)
        ret_msg = f"تم تجديد باقة [{pkg['name']}] بنجاح مع ترحيل {rollover_text}! تم خصم {pkg_price:.2f} من رصيدك. الصلاحية الجديدة حتى {new_expiry}.{loyalty_text}"
        audit_change = f"تم تجديد الباقة عبر بوابة المشترك مع ترحيل الرصيد ({rollover_text})"
    else:
        ret_msg = f"تم تفعيل باقة [{pkg['name']}] بنجاح! تم خصم {pkg_price:.2f} من رصيدك. صلاحية الباقة حتى {new_expiry}.{loyalty_text}"
        audit_change = f"تجديد الباقة عبر بوابة المشترك [{pkg['name']}]"

    from database.db import log_user_audit
    log_user_audit('subscriber', sub['id'], sub['username'], 'UserPortal', 'RENEW_PACKAGE', audit_change)

    return True, ret_msg

def get_user_sessions_history(username, limit=30):
    """Use the same heartbeat display status as admin session history."""
    from services.subscriber_service import get_subscriber_sessions
    return get_subscriber_sessions(username, limit=limit)


def ensure_initial_package():
    """
    Ensures that the default initial package 'الباقة الأولية' exists in wisp_packages.
    Specifications for self-registration:
    - Data Quota: 1 MB (volume_quota_mb = 1)
    - Uptime Limit: 1 Minute (uptime_limit_mins = 1)
    - Simultaneous Sessions: 5 Sessions (simultaneous_sessions = 5)
    - Validity: 1 Minute (validity_value = 1, validity_unit = 'minutes')
    - Price: 0.0, Cost: 0.0
    Returns the package id.
    """
    pkg = query_one("SELECT id, volume_quota_mb, uptime_limit_mins, simultaneous_sessions FROM wisp_packages WHERE name = 'الباقة الأولية'")
    if pkg:
        if pkg.get('volume_quota_mb') != 1 or pkg.get('uptime_limit_mins') != 1 or pkg.get('simultaneous_sessions') != 5:
            execute_write("""
                UPDATE wisp_packages
                SET volume_quota_mb = 1,
                    uptime_limit_mins = 1,
                    validity_value = 1,
                    validity_unit = 'minutes',
                    validity_days = 1,
                    simultaneous_sessions = 5,
                    rate_download = '2M',
                    rate_upload = '1M',
                    price = 0.0,
                    cost = 0.0,
                    is_active = 1,
                    description = 'الباقة الأولية الافتراضية للتسجيل الذاتي (تحميل 1 ميجا، وقت 1 دقيقة، 5 جلسات متزامنة)'
                WHERE id = ?
            """, (pkg['id'],))
            try:
                from core.radius_sync import sync_package_to_radius
                sync_package_to_radius(pkg['id'])
            except Exception:
                pass
        return pkg['id']
        
    pkg_id = execute_write("""
        INSERT INTO wisp_packages (
            name, service_type, price, cost, rate_download, rate_upload,
            volume_quota_mb, uptime_limit_mins, validity_days, validity_value, validity_unit,
            simultaneous_sessions, is_active, show_in_portal, is_rollover_enabled, description
        ) VALUES (
            'الباقة الأولية', 'both', 0.0, 0.0, '2M', '1M',
            1, 1, 1, 1, 'minutes',
            5, 1, 0, 0, 'الباقة الأولية الافتراضية للتسجيل الذاتي (تحميل 1 ميجا، وقت 1 دقيقة، 5 جلسات متزامنة)'
        )
    """)
    try:
        from core.radius_sync import sync_package_to_radius
        sync_package_to_radius(pkg_id)
    except Exception:
        pass
    return pkg_id

def register_portal_subscriber(form_data):
    """
    Handles Self-Registration of new subscribers via User Portal:
    - Validates Arabic full_name (^[\\u0600-\\u06FF]+\\s+[\\u0600-\\u06FF]+.*$)
    - Validates numeric 9-digit username (^\\d{9}$)
    - Validates 9-digit phone starting with 7 (^7\\d{8}$)
    - Assigns password = username
    - Binds to 'الباقة الأولية'
    - Synchronizes credentials to FreeRADIUS (radcheck, radusergroup)
    """
    full_name = (form_data.get('full_name') or '').strip()
    username = (form_data.get('username') or '').strip()
    phone = (form_data.get('phone') or '').strip()
    email = (form_data.get('email') or '').strip()
    national_id = (form_data.get('national_id') or '').strip()

    # 1. التحقق من صحة الاسم الكامل (عربي فقط ومقطعين على الأقل)
    name_regex = r'^[\u0600-\u06FF]+\s+[\u0600-\u06FF]+.*$'
    if not full_name or not re.match(name_regex, full_name):
        return False, "يجب كتابة الاسم الكامل باللغة العربية ويتكون من مقطعين على الأقل (الاسم الأول والأخير)."

    # 2. التحقق من اسم المستخدم (9 أرقام بالضبط)
    username_regex = r'^\d{9}$'
    if not username or not re.match(username_regex, username):
        return False, "اسم المستخدم يجب أن يتكون من 9 أرقام فقط."

    # 3. التحقق من رقم الجوال (9 أرقام ويبدأ برقم 7)
    phone_regex = r'^7\d{8}$'
    if not phone or not re.match(phone_regex, phone):
        return False, "رقم الجوال يجب أن يتكون من 9 أرقام ويبدأ بالرقم 7 (مثال: 770000000)."

    # 4. التحقق من البريد الإلكتروني إن وجد
    if email:
        email_regex = r'^[\w\.-]+@[\w\.-]+\.\w+$'
        if not re.match(email_regex, email):
            return False, "صيغة البريد الإلكتروني غير صحيحة."

    # 5. التحقق من عدم تكرار اسم المستخدم
    existing_user = query_one("""
        SELECT 1 FROM wisp_subscribers WHERE LOWER(username) = LOWER(?)
        UNION
        SELECT 1 FROM wisp_vouchers WHERE LOWER(username) = LOWER(?)
        UNION
        SELECT 1 FROM radcheck WHERE LOWER(username) = LOWER(?)
    """, (username, username, username))
    if existing_user:
        return False, f"اسم المستخدم ({username}) مسجل مسبقاً في النظام. يرجى اختيار رقم آخر أو تسجيل الدخول."

    # 6. التحقق من عدم تكرار رقم الجوال
    existing_phone = query_one("SELECT 1 FROM wisp_subscribers WHERE phone = ?", (phone,))
    if existing_phone:
        return False, f"رقم الجوال ({phone}) مسجل مسبقاً لمشترك آخر."

    # 7. تعيين كلمة المرور لتطابق اسم المستخدم
    password = username

    # 8. ربط الحساب بالباقة الأولية
    pkg_id = ensure_initial_package()
    pkg = query_one("SELECT * FROM wisp_packages WHERE id = ?", (pkg_id,))
    now_dt = get_db_storage_now().replace(tzinfo=None)
    exp_iso = None
    if pkg:
        val = int(pkg.get('validity_value') or pkg.get('validity_days') or 30)
        unit = (pkg.get('validity_unit') or 'days').lower()
        if unit == 'minutes':
            exp_dt = now_dt + datetime.timedelta(minutes=val)
        elif unit == 'hours':
            exp_dt = now_dt + datetime.timedelta(hours=val)
        elif unit == 'months':
            exp_dt = now_dt + datetime.timedelta(days=val * 30)
        else:
            exp_dt = now_dt + datetime.timedelta(days=val)
        exp_iso = exp_dt.strftime('%Y-%m-%d %H:%M:%S')

    # 9. حفظ المشترك في قاعدة البيانات
    sub_id = execute_write("""
        INSERT INTO wisp_subscribers (
            username, password, full_name, phone, email, national_id,
            service_type, package_id, status, balance, extra_quota_mb, expires_at, notes
        ) VALUES (?, ?, ?, ?, ?, ?, 'hotspot', ?, 'active', 0.00, 0, ?, 'تسجيل ذاتي عبر بوابة المشتركين')
    """, (username, password, full_name, phone, email, national_id, pkg_id, exp_iso))

    # 10. مزامنة الحساب مع FreeRADIUS
    sync_subscriber_to_radius(sub_id)

    # 11. تسجيل العملية في سجل تدقيق المشترك
    log_user_audit('subscriber', sub_id, username, 'Self-Registration', 'REGISTER', 'تسجيل حساب ذاتي جديد عبر البوابة وربطه بالباقة الأولية')
    log_audit(1, 'portal', 'SELF_REGISTER', 'subscribers', f'New self-registered subscriber {username} ({full_name})')

    return True, "تم إنشاء حسابك بنجاح، يمكنك الآن تسجيل الدخول وشحن رصيدك"

def change_portal_password(username, old_password, new_password, confirm_password):
    """Change portal and RADIUS credentials atomically under the subscriber lock."""
    username = (username or '').strip()
    if not new_password or len(new_password) < 4:
        return False, "كلمة المرور الجديدة يجب أن تتكون من 4 خانات على الأقل."
    if new_password != confirm_password:
        return False, "كلمة المرور الجديدة وتأكيدها غير متطابقين."
    with db_session() as conn:
        cursor = conn.cursor()
        lock = 'FOR UPDATE' if is_mysql_conn(conn) else ''
        cursor.execute(adapt_query(f"SELECT * FROM wisp_subscribers WHERE LOWER(username)=LOWER(?) LIMIT 1 {lock}", conn), (username,))
        sub = cursor.fetchone()
        if not sub:
            return False, "حساب المشترك غير مسجل في قائمة المشتركين."
        sub = dict(sub)
        if sub['password'] != old_password:
            return False, "كلمة المرور الحالية غير صحيحة."
        cursor.execute(adapt_query("UPDATE wisp_subscribers SET password=? WHERE id=?", conn), (new_password, sub['id']))
        cursor.execute(adapt_query("UPDATE radcheck SET value=? WHERE LOWER(username)=LOWER(?) AND attribute IN ('Cleartext-Password','User-Password')", conn), (new_password, sub['username']))
        # A missing credential must not turn a successful password change into an unusable account.
        if sub['status'] == 'active':
            cursor.execute(adapt_query("SELECT 1 FROM radcheck WHERE LOWER(username)=LOWER(?) AND attribute IN ('Cleartext-Password','User-Password') LIMIT 1", conn), (sub['username'],))
            if not cursor.fetchone():
                cursor.execute(adapt_query("INSERT INTO radcheck(username,attribute,op,value) VALUES (?,'Cleartext-Password',':=',?)", conn), (sub['username'], new_password))

    log_user_audit('subscriber', sub['id'], sub['username'], 'UserPortal', 'CHANGE_PASSWORD', 'تعديل كلمة المرور عبر بوابة المشترك')
    return True, "تم تغيير كلمة المرور بنجاح."


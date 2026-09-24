# -*- coding: utf-8 -*-
"""
User Portal & Client Area Service:
Handles authentication, subscriber profile, wallet recharges via vouchers,
package renewals & changes, and personal session history.
"""

import re
import datetime
from database.db import query_one, query_all, execute_write, log_audit, log_user_audit
from core.rate_limit import format_bytes, format_duration
from core.radius_sync import sync_subscriber_to_radius, delete_user_from_radius
from services.subscriber_service import disconnect_subscriber_session, clean_stale_sessions, get_heartbeat_cutoff_str

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

    now = datetime.datetime.now()
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
    Returns the list of speed options parsed from settings or standard 4 defaults.
    Each item includes complete styling tokens and default selection flag.
    """
    import json
    if settings_dict is None:
        try:
            rows = query_all('SELECT `key`, `value` FROM wisp_system_settings')
            settings_dict = {r['key']: r['value'] for r in rows} if rows else {}
        except Exception:
            settings_dict = {}

    options_list = None
    raw = settings_dict.get('portal_speed_options')
    if raw:
        try:
            if isinstance(raw, str):
                parsed = json.loads(raw)
            elif isinstance(raw, list):
                parsed = raw
            else:
                parsed = None
                
            if isinstance(parsed, list) and len(parsed) > 0:
                options_list = parsed
        except Exception:
            pass

    if not options_list:
        eco = (settings_dict.get('portal_speed_eco', '4M') or '4M').strip()
        bal = (settings_dict.get('portal_speed_balanced', '10M') or '10M').strip()
        turbo = (settings_dict.get('portal_speed_turbo', '25M') or '25M').strip()
        options_list = [
            {
                'id': 'eco',
                'name': 'سرعة اقتصادية',
                'rate_down': eco,
                'rate_up': eco,
                'description': 'توفير البيانات',
                'icon': 'fa-solid fa-sack-dollar',
                'emoji': '💰',
                'badge': f"{eco}bps" if not eco.lower().endswith('bps') else eco,
                'color': 'emerald',
                'is_open': False,
                'is_default': False
            },
            {
                'id': 'balanced',
                'name': 'سرعة متوسطة',
                'rate_down': bal,
                'rate_up': bal,
                'description': 'تصفح وفيديو',
                'icon': 'fa-solid fa-bolt',
                'emoji': '⚡',
                'badge': f"{bal}bps" if not bal.lower().endswith('bps') else bal,
                'color': 'sky',
                'is_open': False,
                'is_default': True
            },
            {
                'id': 'turbo',
                'name': 'سرعة العاب الاونلاين',
                'rate_down': turbo,
                'rate_up': turbo,
                'description': 'بنج منخفض',
                'icon': 'fa-solid fa-gamepad',
                'emoji': '🎮',
                'badge': f"{turbo}bps" if not turbo.lower().endswith('bps') else turbo,
                'color': 'rose',
                'is_open': False,
                'is_default': False
            },
            {
                'id': 'open',
                'name': 'سرعة مفتوحة',
                'rate_down': '0',
                'rate_up': '0',
                'description': 'أقصى سرعة بدون تحديد',
                'icon': 'fa-solid fa-rocket',
                'emoji': '🚀',
                'badge': 'أقصى سرعة',
                'color': 'indigo',
                'is_open': True,
                'is_default': False
            }
        ]

    cleaned = []
    has_default = False
    for i, item in enumerate(options_list):
        sp_id = str(item.get('id') or f"speed_{i+1}").strip()
        name = str(item.get('name') or f"سرعة {i+1}").strip()
        r_down = str(item.get('rate_down', '10M')).strip()
        r_up = str(item.get('rate_up', r_down)).strip()
        desc = str(item.get('description', '')).strip()
        icon = str(item.get('icon', 'fa-solid fa-bolt')).strip()
        color_key = str(item.get('color', 'sky')).strip().lower()
        if color_key not in COLOR_THEMES:
            color_key = 'sky'
        
        is_open = item.get('is_open') is True or str(r_down).strip() in ['0', '0M', '0K', ''] or 'مفتوح' in name
        is_def = item.get('is_default') in (True, '1', 1, 'true', 'True')
        if is_def:
            has_default = True

        badge = str(item.get('badge') or '').strip()
        if not badge:
            badge = "أقصى سرعة" if is_open else (f"{r_down}bps" if not r_down.lower().endswith('bps') else r_down)

        # Format clear readable numbers
        import re
        if is_open or str(r_down).strip() in ['0', '0M', '0K', '']:
            disp_val = 'MAX'
            disp_unit = 'مفتوحة'
            disp_sub = 'أقصى سرعة بدون تحديد'
        else:
            m = re.match(r'^(\d+(?:\.\d+)?)\s*([A-Za-z]+)?$', str(r_down).strip())
            if m:
                disp_val = m.group(1)
                u = (m.group(2) or 'M').upper()
                if u in ['M', 'MB', 'MBPS']:
                    disp_unit = 'Mbps'
                elif u in ['K', 'KB', 'KBPS']:
                    disp_unit = 'Kbps'
                elif u in ['G', 'GB', 'GBPS']:
                    disp_unit = 'Gbps'
                else:
                    disp_unit = u
                disp_sub = f"تنزيل {disp_val}{disp_unit}"
            else:
                disp_val = str(r_down)
                disp_unit = ''
                disp_sub = f"سرعة {r_down}"

        theme_tokens = COLOR_THEMES[color_key]
        emoji = str(item.get('emoji') or theme_tokens.get('default_emoji', '⚡')).strip()

        cleaned.append({
            'id': sp_id,
            'name': name,
            'rate_down': r_down,
            'rate_up': r_up,
            'description': desc,
            'icon': icon,
            'emoji': emoji,
            'badge': badge,
            'display_val': disp_val,
            'display_unit': disp_unit,
            'display_sub': disp_sub,
            'color': color_key,
            'is_open': is_open,
            'is_default': is_def,
            'theme': theme_tokens
        })

    # If no default set, make the 2nd (or 1st) default
    if not has_default and cleaned:
        def_idx = 1 if len(cleaned) > 1 else 0
        cleaned[def_idx]['is_default'] = True

    return cleaned

def authenticate_portal_user(username, password):
    """
    Authenticate against FreeRADIUS radcheck table or subscribers/vouchers.
    """
    username = (username or '').strip()
    password = (password or '').strip()
    if not username or not password:
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
        clean_stale_sessions(timeout_minutes=10)
    except Exception:
        pass
    # 1. Check if subscriber
    sub = query_one("""
        SELECT s.*, p.name as package_name, p.price as package_price,
               p.rate_download, p.rate_upload, p.volume_quota_mb,
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
                'full_name': f"كرت إنترنت ({v_card['batch_name']})",
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
        user_info['current_ip'] = active_session['framedipaddress'] or '10.x.x.x'
        user_info['mac_address'] = active_session['callingstationid'] or '-'
        user_info['session_start'] = str(active_session['acctstarttime'])
        user_info['nas_ip'] = active_session['nasipaddress']
    else:
        user_info['is_online'] = False
        user_info['current_ip'] = '-'
        user_info['mac_address'] = '-'
        user_info['session_start'] = '-'
        user_info['nas_ip'] = '-'

    cycle_start = None
    if sub:
        cycle_start = sub.get('last_renewed_at') or sub.get('created_at')
    elif v_card:
        cycle_start = v_card.get('last_renewed_at') or v_card.get('first_used_at') or v_card.get('created_at')

    # 3. Calculate current cycle consumption (deduplicated by session)
    if cycle_start:
        usage = query_one("""
            SELECT COALESCE(SUM(total_in), 0) as total_in,
                   COALESCE(SUM(total_out), 0) as total_out,
                   COALESCE(SUM(total_time), 0) as total_time
            FROM (
                SELECT nasipaddress, acctsessionid,
                       MAX(acctinputoctets) as total_in,
                       MAX(acctoutputoctets) as total_out,
                       MAX(acctsessiontime) as total_time
                FROM radacct
                WHERE username = ?
                  AND COALESCE(acctstarttime, acctupdatetime, CURRENT_TIMESTAMP) >= ?
                GROUP BY nasipaddress, acctsessionid
            ) AS sub_usage
        """, (username, str(cycle_start)))
    else:
        usage = query_one("""
            SELECT COALESCE(SUM(total_in), 0) as total_in,
                   COALESCE(SUM(total_out), 0) as total_out,
                   COALESCE(SUM(total_time), 0) as total_time
            FROM (
                SELECT nasipaddress, acctsessionid,
                       MAX(acctinputoctets) as total_in,
                       MAX(acctoutputoctets) as total_out,
                       MAX(acctsessiontime) as total_time
                FROM radacct
                WHERE username = ?
                GROUP BY nasipaddress, acctsessionid
            ) AS sub_usage
        """, (username,))

    raw_in = usage['total_in'] or 0 if usage else 0
    raw_out = usage['total_out'] or 0 if usage else 0
    raw_time = usage['total_time'] or 0 if usage else 0

    user_info['total_download'] = format_bytes(raw_out)
    user_info['total_upload'] = format_bytes(raw_in)
    user_info['total_traffic'] = format_bytes(raw_in + raw_out)
    user_info['total_duration'] = format_duration(raw_time)

    # Quota calculations
    base_quota_mb = float(user_info.get('volume_quota_mb') or 0)
    extra_quota_mb = float(user_info.get('extra_quota_mb') or 0)
    total_allowed_quota_mb = base_quota_mb + extra_quota_mb

    if total_allowed_quota_mb > 0:
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
        user_info['quota_percent'] = max(0.0, min(100.0, round((rem_mb / total_allowed_quota_mb) * 100.0, 1)))
        user_info['quota_used_percent'] = min(100.0, round((total_used_mb / total_allowed_quota_mb) * 100.0, 1))
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
        
        # Check expired or low quota based on dynamic threshold
        if rem_sec <= 0 or str(user_info.get('status')).lower() in ['expired', 'disabled', 'suspended']:
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
        user_info['can_request_loan'] = bool(allow_loan and loan_stat == 0 and quota_is_low)
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

def recharge_user_wallet_by_card(username, card_code, recharge_type='balance'):
    """
    Recharges user balance or directly tops up package data and validity duration using an unused voucher card.
    recharge_type: 'balance' (شحن رصيد مالي) | 'package' (شحن كباقة وإضافة البيانات والوقت)
    In both cases: The recharged card is marked as 'used' with detailed expire_reason,
    and credentials are removed from RADIUS tables so it cannot be used to login.
    """
    username = (username or '').strip()
    card_code = (card_code or '').strip()
    recharge_type = (recharge_type or 'balance').strip().lower()

    if not card_code:
        return False, "يرجى إدخال رقم كرت الشحن أو القسيمة"

    # 1. Find unused card
    card = query_one("""
        SELECT v.*, 
               COALESCE(v.snap_price, p.price) as card_price, 
               p.name as package_name,
               COALESCE(v.snap_volume_quota_mb, p.volume_quota_mb) as volume_quota_mb, 
               COALESCE(v.snap_validity_value, p.validity_value) as validity_value, 
               COALESCE(v.snap_validity_unit, p.validity_unit) as validity_unit, 
               COALESCE(v.snap_validity_days, p.validity_days) as validity_days,
               b.name as batch_name
        FROM wisp_vouchers v
        JOIN wisp_packages p ON v.package_id = p.id
        JOIN wisp_voucher_batches b ON v.batch_id = b.id
        WHERE (v.username = ? OR v.pin_code = ? OR v.serial_number = ?)
    """, (card_code, card_code, card_code))

    if not card:
        return False, "رقم الكرت أو القسيمة غير موجود في النظام. يرجى التأكد من الرقم والمحاولة مجدداً."

    if card['status'] != 'unused':
        return False, "هذا الكرت مستخدم مسبقاً أو منتهي الصلاحية ولا يمكن استخدامه."

    card_value = float(card['card_price'] or 0.0)

    # 2. Check if subscriber exists
    sub = query_one("SELECT * FROM wisp_subscribers WHERE LOWER(username) = LOWER(?)", (username,))
    if not sub:
        return False, "حساب المشترك غير مسجل في قائمة الاشتراكات."

    loan_mb = int(sub.get('loan_balance_mb') or 0)
    loan_status = int(sub.get('loan_status') or 0)
    has_active_loan = (loan_status == 1 or loan_mb > 0)

    if recharge_type == 'package':
        # ---------- شحن كباقة (إضافة حجم البيانات وتمديد الصلاحية مع خصم السلفة إن وجدت) ----------
        add_quota_mb = float(card.get('volume_quota_mb') or 0.0)
        
        deducted_loan_mb = 0
        net_quota_mb = add_quota_mb
        
        # اعتراض عملية الشحن والتحقق من السلفة
        if has_active_loan:
            if add_quota_mb < loan_mb:
                return False, f"حجم الكرت ({format_mb_or_gb(add_quota_mb)}) لا يغطي السلفة السابقة ({format_mb_or_gb(loan_mb)})."
            
            deducted_loan_mb = loan_mb
            net_quota_mb = add_quota_mb - loan_mb

        # Calculate validity delta from card's package
        val = card.get('validity_value') if card.get('validity_value') is not None else (card.get('validity_days') or 30)
        unit = str(card.get('validity_unit') or 'days').lower().strip()
        
        if unit in ['minutes', 'minute', 'دقائق', 'دقيقة']:
            delta = datetime.timedelta(minutes=int(val))
        elif unit in ['hours', 'hour', 'ساعات', 'ساعة']:
            delta = datetime.timedelta(hours=int(val))
        elif unit in ['months', 'month', 'أشهر', 'شهر']:
            delta = datetime.timedelta(days=int(val) * 30)
        else: # days
            delta = datetime.timedelta(days=int(val))
            
        now = datetime.datetime.now()
        
        # Determine base time for extending validity
        current_exp = sub.get('expires_at')
        if current_exp and isinstance(current_exp, str):
            try:
                current_exp_dt = datetime.datetime.fromisoformat(current_exp.replace('Z', ''))
            except Exception:
                try:
                    current_exp_dt = datetime.datetime.strptime(current_exp.split('.')[0].strip(), '%Y-%m-%d %H:%M:%S')
                except Exception:
                    current_exp_dt = None
        elif isinstance(current_exp, datetime.datetime):
            current_exp_dt = current_exp
        else:
            current_exp_dt = None
            
        if current_exp_dt and current_exp_dt > now and sub.get('status') == 'active':
            new_exp_dt = current_exp_dt + delta
        else:
            new_exp_dt = now + delta
            
        new_exp_iso = new_exp_dt.strftime('%Y-%m-%d %H:%M:%S')
        new_fr_exp = new_exp_dt.strftime('%d %b %Y %H:%M:%S')
        
        # Under Option 1: The card grants strictly its purchased capacity and validity duration
        # Update package to the recharged card's package and set quota adjustments
        card_extra_quota = -deducted_loan_mb if deducted_loan_mb > 0 else 0
        
        # Update subscriber: activate, extend expiry, set package to card package, set exact net quota, reset loan
        execute_write("""
            UPDATE wisp_subscribers SET
                status = 'active',
                package_id = ?,
                expires_at = ?,
                extra_quota_mb = ?,
                loan_balance_mb = 0,
                loan_status = 0,
                last_renewed_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (card['package_id'], new_exp_iso, card_extra_quota, sub['id']))
        
        # Sync subscriber with FreeRADIUS
        try:
            sync_subscriber_to_radius(sub['id'])
        except Exception as e:
            print(f"Error syncing subscriber to RADIUS: {e}")

        # Ensure no invalid Max-Total-Octets check item exists in radcheck
        execute_write("DELETE FROM radcheck WHERE LOWER(username) = LOWER(?) AND attribute = 'Max-Total-Octets'", (username,))
        
        # Update radusergroup to match card's package speed & profile
        execute_write("DELETE FROM radusergroup WHERE LOWER(username) = LOWER(?)", (username,))
        execute_write("INSERT INTO radusergroup (username, groupname, priority) VALUES (?, ?, 1)", (username, card['package_name']))

        # Update radcheck Expiration
        execute_write("DELETE FROM radcheck WHERE LOWER(username) = LOWER(?) AND attribute = 'Expiration'", (username,))
        execute_write("INSERT INTO radcheck (username, attribute, op, value) VALUES (?, 'Expiration', ':=', ?)", (username, new_fr_exp))
            
        # Mark card as recharged / disabled
        card_expire_msg = f"تم استخدامه في شحن الباقة (تم سداد سلفة {format_mb_or_gb(deducted_loan_mb)})" if deducted_loan_mb > 0 else "تم استخدامه في شحن الباقة والوقت"
        execute_write("""
            UPDATE wisp_vouchers SET
                status = 'recharged',
                expire_reason = ?,
                first_used_at = CURRENT_TIMESTAMP,
                bound_mac = ?
            WHERE id = ?
        """, (card_expire_msg, f"TOPUP:{username}"[:28], card['id']))
        
        # Remove used card from RADIUS
        try:
            delete_user_from_radius(card['username'])
        except Exception as e:
            print(f"Error removing recharged voucher {card['username']} from RADIUS: {e}")
            
        # Record in sales
        try:
            execute_write("""
                INSERT INTO wisp_voucher_sales (
                    voucher_id, batch_id, batch_name, username, serial_number,
                    package_name, price, cost, reseller_id, activated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0.0, NULL, CURRENT_TIMESTAMP)
            """, (card['id'], card['batch_id'], card['batch_name'], card['username'], card['serial_number'], card['package_name'], card_value))
        except Exception:
            pass
            
        # Disconnect active session if any so new quota/expiry applies immediately
        try:
            disconnect_subscriber_session(username)
        except Exception:
            pass

        if deducted_loan_mb > 0:
            return True, f"تم شحن الباقة بنجاح! تم سداد السلفة السابقة ({format_mb_or_gb(deducted_loan_mb)}) وإضافة السعة الصافية ({format_mb_or_gb(net_quota_mb)}) إلى رصيدك وتمديد الصلاحية حتى {new_exp_iso}."
        else:
            return True, f"تم شحن الباقة بنجاح! تمت إضافة {format_mb_or_gb(add_quota_mb)} بيانات وتمديد الصلاحية حتى {new_exp_iso}."

    else:
        # ---------- شحن كرصيد مالي (الوضع الافتراضي) ----------
        if card_value <= 0:
            card_value = 10.0  # default fallback if price is zero
            
        new_balance = float(sub['balance'] or 0.0) + card_value
        execute_write("UPDATE wisp_subscribers SET balance = ? WHERE id = ?", (new_balance, sub['id']))
        
        # Mark card as recharged / disabled and remove from FreeRADIUS
        execute_write("""
            UPDATE wisp_vouchers SET
                status = 'recharged',
                expire_reason = 'تم استخدامه في شحن الرصيد',
                first_used_at = CURRENT_TIMESTAMP,
                bound_mac = ?
            WHERE id = ?
        """, (f"RECHARGE:{username}"[:28], card['id']))

        # Remove used card from FreeRADIUS tables so it cannot be used to login
        try:
            delete_user_from_radius(card['username'])
        except Exception as e:
            print(f"Error removing recharged voucher {card['username']} from RADIUS: {e}")

        # Record in sales
        try:
            execute_write("""
                INSERT INTO wisp_voucher_sales (
                    voucher_id, batch_id, batch_name, username, serial_number,
                    package_name, price, cost, reseller_id, activated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0.0, NULL, CURRENT_TIMESTAMP)
            """, (card['id'], card['batch_id'], card['batch_name'], card['username'], card['serial_number'], card['package_name'], card_value))
        except Exception:
            pass

        return True, f"تم شحن محفظتك بنجاح بمبلغ {card_value:.2f}. رصيدك الحالي أصبح: {new_balance:.2f}"


def request_data_loan(username):
    """
    Handles Data Loan (السلفة) for subscribers dynamically based on system settings:
    1. Reads allow_data_loan and loan_amount_mb from wisp_system_settings.
    2. Validates subscriber exists and loan_status == 0.
    3. Validates remaining quota < 100 MB or expired.
    4. Sets loan_balance_mb = loan_amount_mb, loan_status = 1.
    5. Increases extra_quota_mb by loan_amount_mb.
    6. Extends expiration by 24 hours.
    7. Updates FreeRADIUS radcheck:
       - Increments Max-Total-Octets by (loan_amount_mb * 1024 * 1024) bytes.
       - Extends Expiration attribute by 24 hours.
    8. Disconnects user session so new limits apply immediately.
    """
    username = (username or '').strip()
    
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

    sub = query_one("""
        SELECT s.*, p.name as package_name, p.volume_quota_mb
        FROM wisp_subscribers s
        JOIN wisp_packages p ON s.package_id = p.id
        WHERE LOWER(s.username) = LOWER(?)
    """, (username,))
    
    if not sub:
        return False, "حساب المشترك غير مسجل في قائمة الاشتراكات."
        
    loan_str = format_mb_or_gb(loan_amount_mb)
    if int(sub.get('loan_status') or 0) == 1 or int(sub.get('loan_balance_mb') or 0) > 0:
        existing_loan = int(sub.get('loan_balance_mb') or loan_amount_mb)
        return False, f"لديك سلفة نشطة مسبقاً بقيمة {format_mb_or_gb(existing_loan)} لم يتم سدادها بعد. يرجى شحن كرت لسداد السلفة."

    # Verify quota or expiry condition
    user_info = get_portal_user_data(username)
    if not user_info.get('can_request_loan'):
        rem_str = user_info.get('quota_rem_str', '')
        thresh_str = format_mb_or_gb(threshold_mb)
        return False, f"طلب السلفة متاح فقط عند اقتراب انتهاء الرصيد (أقل من {thresh_str}). رصيدك المتبقي الحالي: {rem_str}."

    now = datetime.datetime.now()
    # 24 Hours extension
    current_exp = sub.get('expires_at')
    current_exp_dt = None
    if current_exp:
        if isinstance(current_exp, datetime.datetime):
            current_exp_dt = current_exp
        else:
            try:
                current_exp_dt = datetime.datetime.fromisoformat(str(current_exp).replace('Z', ''))
            except Exception:
                try:
                    current_exp_dt = datetime.datetime.strptime(str(current_exp).split('.')[0].strip(), '%Y-%m-%d %H:%M:%S')
                except Exception:
                    current_exp_dt = None

    if current_exp_dt and current_exp_dt > now:
        new_exp_dt = current_exp_dt + datetime.timedelta(hours=24)
    else:
        new_exp_dt = now + datetime.timedelta(hours=24)

    new_exp_iso = new_exp_dt.strftime('%Y-%m-%d %H:%M:%S')
    new_fr_exp = new_exp_dt.strftime('%d %b %Y %H:%M:%S')
    
    loan_mb = loan_amount_mb
    loan_bytes = loan_mb * 1024 * 1024
    new_extra_mb = float(sub.get('extra_quota_mb') or 0.0) + loan_mb

    # 1. Update wisp_subscribers
    execute_write("""
        UPDATE wisp_subscribers SET
            loan_balance_mb = ?,
            loan_status = 1,
            extra_quota_mb = ?,
            expires_at = ?,
            status = 'active'
        WHERE id = ?
    """, (loan_mb, new_extra_mb, new_exp_iso, sub['id']))

    # 2. Ensure no invalid Max-Total-Octets check item exists in radcheck
    execute_write("DELETE FROM radcheck WHERE LOWER(username) = LOWER(?) AND attribute = 'Max-Total-Octets'", (username,))

    # 3. Update radcheck Expiration
    execute_write("DELETE FROM radcheck WHERE LOWER(username) = LOWER(?) AND attribute = 'Expiration'", (username,))
    execute_write("INSERT INTO radcheck (username, attribute, op, value) VALUES (?, 'Expiration', ':=', ?)", (username, new_fr_exp))

    # 4. Ensure Cleartext-Password and Group are in RADIUS
    rad_pwd = query_one("SELECT * FROM radcheck WHERE LOWER(username) = LOWER(?) AND attribute = 'Cleartext-Password'", (username,))
    if not rad_pwd:
        execute_write("INSERT INTO radcheck (username, attribute, op, value) VALUES (?, 'Cleartext-Password', ':=', ?)", (username, sub['password']))
    
    rad_grp = query_one("SELECT * FROM radusergroup WHERE LOWER(username) = LOWER(?)", (username,))
    if not rad_grp:
        execute_write("INSERT INTO radusergroup (username, groupname, priority) VALUES (?, ?, 1)", (username, sub['package_name']))

    # 5. Disconnect user to force re-auth with new quota & expiry limits
    try:
        disconnect_subscriber_session(username)
    except Exception:
        pass

    # 6. Log audit
    try:
        log_user_audit('subscriber', sub['id'], username, 'self', 'DATA_LOAN', f'حصل المشترك على سلفة بيانات ({loan_str}) صالحة لمدة 24 ساعة')
        log_audit(1, 'system', 'DATA_LOAN', 'subscribers', f'Granted {loan_str} ({loan_mb} MB) data loan (24h) to subscriber {username} (ID: {sub["id"]})')
    except Exception:
        pass

    return True, f"تم تفعيل سلفة {loan_str} بنجاح لمدة 24 ساعة إضافية حتى {new_exp_iso}! سيتم خصم {loan_str} تلقائياً من بطاقة الشحن القادمة."


def renew_or_change_package(username, new_pkg_id):
    """
    Renews current package or upgrades to another package from subscriber's balance
    with Data & Time Rollover support.
    """
    username = (username or '').strip()
    sub = query_one("SELECT * FROM wisp_subscribers WHERE LOWER(username) = LOWER(?)", (username,))
    if not sub:
        return False, "المشترك غير موجود في سجلات الاشتراكات"

    pkg = query_one("SELECT * FROM wisp_packages WHERE id = ? AND is_active = 1", (new_pkg_id,))
    if not pkg:
        return False, "الباقة المطلوبة غير متوفرة أو تم إيقافها"

    pkg_price = float(pkg['price'] or 0.0)
    user_balance = float(sub['balance'] or 0.0)

    if user_balance < pkg_price:
        needed = pkg_price - user_balance
        return False, f"رصيدك الحالي ({user_balance:.2f}) لا يكفي لتفعيل باقة {pkg['name']} (السعر: {pkg_price:.2f}). ينقصك {needed:.2f}. يرجى شحن رصيدك أولاً."

    # 1. التحقق من تفعيل الترحيل في باقة المشترك الحالية
    curr_pkg = query_one("SELECT * FROM wisp_packages WHERE id = ?", (sub['package_id'],))
    is_rollover_enabled = bool(curr_pkg and curr_pkg.get('is_rollover_enabled'))

    now = datetime.datetime.now()
    rem_data_mb = 0.0
    rem_time_delta = datetime.timedelta(0)
    rem_days = 0
    rem_hours = 0

    if is_rollover_enabled:
        # حساب البيانات المتبقية
        total_allowed_mb = float((curr_pkg.get('volume_quota_mb') if curr_pkg else 0) or 0) + float(sub.get('extra_quota_mb') or 0)
        if total_allowed_mb > 0:
            cycle_start = sub.get('last_renewed_at') or sub.get('created_at')
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

        # حساب الزمن المتبقي
        if sub.get('expires_at'):
            try:
                exp_str = str(sub['expires_at']).replace('T', ' ').split('.')[0]
                exp_dt = datetime.datetime.strptime(exp_str, '%Y-%m-%d %H:%M:%S')
                if exp_dt > now:
                    rem_time_delta = exp_dt - now
                    rem_days = int(rem_time_delta.total_seconds() // 86400)
                    rem_hours = int((rem_time_delta.total_seconds() % 86400) // 3600)
            except Exception:
                pass

    # 2. حساب تاريخ الانتهاء الجديد
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
        new_exp_dt = now + base_delta + rem_time_delta
        new_expiry = new_exp_dt.strftime('%Y-%m-%d %H:%M:%S')
        new_fr_exp = new_exp_dt.strftime('%d %b %Y %H:%M:%S')

    new_extra_mb = round(rem_data_mb, 2) if is_rollover_enabled else 0.0
    new_balance = user_balance - pkg_price

    # 3. تحديث حساب المشترك وتصفير عداد الدورة
    execute_write("""
        UPDATE wisp_subscribers SET
            balance = ?,
            package_id = ?,
            status = 'active',
            last_renewed_at = CURRENT_TIMESTAMP,
            extra_quota_mb = ?,
            expires_at = ?
        WHERE id = ?
    """, (new_balance, pkg['id'], new_extra_mb, new_expiry, sub['id']))

    # 4. تحديث FreeRADIUS radusergroup و radcheck
    execute_write("DELETE FROM radusergroup WHERE LOWER(username) = LOWER(?)", (username,))
    execute_write("INSERT INTO radusergroup (username, groupname, priority) VALUES (?, ?, 1)", (sub['username'], pkg['name']))

    execute_write("DELETE FROM radcheck WHERE LOWER(username) = LOWER(?) AND attribute = 'Expiration'", (username,))
    if new_fr_exp:
        execute_write("INSERT INTO radcheck (username, attribute, op, value) VALUES (?, 'Expiration', ':=', ?)", (username, new_fr_exp))

    # تنظيف أي سمات كوتا زائدة من radcheck (تتم إدارة الكوتا ديناميكياً عبر SQL)
    execute_write("DELETE FROM radcheck WHERE LOWER(username) = LOWER(?) AND attribute = 'Max-Total-Octets'", (username,))

    # 5. مزامنة المشترك وفصل الجلسة لتطبيق الإعدادات
    try:
        sync_subscriber_to_radius(sub['id'])
    except Exception:
        pass

    try:
        disconnect_subscriber_session(sub['username'])
    except Exception:
        pass

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
        ret_msg = f"تم تجديد باقة [{pkg['name']}] بنجاح مع ترحيل {rollover_text}! تم خصم {pkg_price:.2f} من رصيدك. الصلاحية الجديدة حتى {new_expiry}."
        audit_change = f"تم تجديد الباقة عبر بوابة المشترك مع ترحيل الرصيد ({rollover_text})"
    else:
        ret_msg = f"تم تفعيل باقة [{pkg['name']}] بنجاح! تم خصم {pkg_price:.2f} من رصيدك. صلاحية الباقة حتى {new_expiry}."
        audit_change = f"تجديد الباقة عبر بوابة المشترك [{pkg['name']}]"

    from database.db import log_user_audit
    log_user_audit('subscriber', sub['id'], sub['username'], 'UserPortal', 'RENEW_PACKAGE', audit_change)

    return True, ret_msg

def get_user_sessions_history(username, limit=30):
    """
    Get session history for a specific subscriber from radacct.
    """
    sessions = query_all("""
        SELECT radacctid, acctsessionid, nasipaddress, framedipaddress,
               callingstationid, acctstarttime, acctstoptime, acctsessiontime,
               acctinputoctets, acctoutputoctets, acctterminatecause
        FROM radacct
        WHERE LOWER(username) = LOWER(?)
        ORDER BY radacctid DESC
        LIMIT ?
    """, (username, limit))

    for s in sessions:
        s['download_str'] = format_bytes(s['acctoutputoctets'] or 0)
        s['upload_str'] = format_bytes(s['acctinputoctets'] or 0)
        s['total_str'] = format_bytes((s['acctoutputoctets'] or 0) + (s['acctinputoctets'] or 0))
        s['duration_str'] = format_duration(s['acctsessiontime'] or 0)
        s['is_active'] = (s['acctstoptime'] is None)

    return sessions

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
    now_dt = datetime.datetime.now()
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
    """
    Changes password for subscriber from user portal.
    """
    username = (username or '').strip()
    sub = query_one("SELECT * FROM wisp_subscribers WHERE LOWER(username) = LOWER(?)", (username,))
    if not sub:
        return False, "حساب المشترك غير مسجل في قائمة المشتركين."

    if sub['password'] != old_password:
        return False, "كلمة المرور الحالية غير صحيحة."

    if not new_password or len(new_password) < 4:
        return False, "كلمة المرور الجديدة يجب أن تتكون من 4 خانات على الأقل."

    if new_password != confirm_password:
        return False, "كلمة المرور الجديدة وتأكيدها غير متطابقين."

    execute_write("UPDATE wisp_subscribers SET password = ? WHERE id = ?", (new_password, sub['id']))
    execute_write("""
        UPDATE radcheck SET value = ?
        WHERE LOWER(username) = LOWER(?) AND attribute IN ('Cleartext-Password', 'User-Password')
    """, (new_password, username))

    log_user_audit('subscriber', sub['id'], sub['username'], 'UserPortal', 'CHANGE_PASSWORD', 'تعديل كلمة المرور عبر بوابة المشترك')
    return True, "تم تغيير كلمة المرور بنجاح."


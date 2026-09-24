# -*- coding: utf-8 -*-
"""
services/whatsapp_service.py
----------------------------
Centralized WhatsApp Engine & Interactive Bot Service for MAX RADIUS.
Provides:
1. Multi-Gateway Connectivity (Evolution API, WPPConnect, Meta Cloud API, Local Simulator).
2. Live QR-Code Generator and Connection Watchdog.
3. Smart Interactive Bot Parser & Conversational State Engine.
4. Dynamic Notification Dispatcher (New Voucher, Quota Alert, Expiry Warning, Wallet Top-up).
5. Mass Campaign Broadcast Dispatcher with Audience Targeting.
6. Full Message Lifecycle Tracking and Audit Logging.
"""

import os
import re
import json
import time
import logging
import datetime
import urllib.request
import urllib.parse
import urllib.error
import threading

from database.db import query_one, query_all, execute_write, execute_update
from core.config import APP_NAME, APP_VERSION
from core.time_service import get_system_now, get_system_now_str

logger = logging.getLogger('whatsapp_service')

DEFAULT_TEMPLATES = [
    {
        'event_key': 'welcome_menu',
        'title': 'القائمة الرئيسية والترحيب',
        'template_text': """👋 أهلاً بك في خدمة العملاء والمساعد الآلي لشبكة *{network_name}*!

يرجى اختيار رقم الخدمة المطلوبة:
1️⃣ *الاستعلام عن الرصيد والصلاحية*
2️⃣ *شحن رصيد / تجديد الاشتراك بكارت*
3️⃣ *عرض الباقات والأسعار المتاحة*
4️⃣ *خدمة العملاء والدعم الفني*

💡 _يمكنك كتابة رقم الخدمة (مثلاً: 1) أو كتابة اسم الكرت مباشرة._"""
    },
    {
        'event_key': 'voucher_issued',
        'title': 'إشعار استلام كارت إنترنت جديد',
        'template_text': """🎉 *تم إصدار كارت الإنترنت بنجاح!*
━━━━━━━━━━━━━━━━━━
👤 *اسم المستخدم:* `{username}`
🔑 *كلمة المرور:* `{password}`
📦 *الباقة:* {package_name}
📊 *الرصيد:* {quota}
⏳ *الصلاحية:* {validity}
💰 *السعر:* {price} {currency}
━━━━━━━━━━━━━━━━━━
🌐 *رابط تسجيل الدخول السريع:*
{login_url}

نتمنى لكم تجربة تصفح ممتعة وسريعة 🚀"""
    },
    {
        'event_key': 'quota_warning',
        'title': 'تنبيه اقتراب نفاد سعة التحميل (80%)',
        'template_text': """⚠️ *تنبيه استهلاك البيانات*
عزيزي المشترك *{username}*،
لقد استهلكت أكثر من *80%* من رصيد باقتك ({package_name}).
📊 *الرصيد المتبقي:* {quota_remaining}
⏳ *تاريخ الانتهاء:* {expiry_date}

💡 _لتجديد الباقة أو شحن كارت إضافي، أرسل رقم 2 أو تواصل مع أقرب نقطة بيع._"""
    },
    {
        'event_key': 'expiry_warning',
        'title': 'تنبيه قرب انتهاء صلاحية الاشتراك',
        'template_text': """⏳ *تنبيه اقتراب انتهاء الاشتراك*
عزيزي المشترك *{username}*،
ينتهي اشتراكك في باقة *{package_name}* بتاريخ:
📅 *{expiry_date}*

يرجى تجديد الاشتراك لضمان استمرار الخدمة دون انقطاع."""
    },
    {
        'event_key': 'wallet_recharge',
        'title': 'إشعار شحن محفظة الوكيل / المشترك',
        'template_text': """💳 *عملية شحن رصيد ناجحة*
━━━━━━━━━━━━━━━━━━
👤 *الحساب:* {username}
💵 *المبلغ المشحون:* +{amount} {currency}
💰 *الرصيد الحالي:* {new_balance} {currency}
📅 *التاريخ:* {transaction_time}
━━━━━━━━━━━━━━━━━━
شكراً لتعاملكم معنا ✨"""
    }
]


def seed_default_whatsapp_data():
    """Ensures default settings and templates exist in the database."""
    try:
        settings = query_one("SELECT id FROM wisp_whatsapp_settings LIMIT 1")
        if not settings:
            execute_write(
                """INSERT INTO wisp_whatsapp_settings 
                (gateway_provider, api_endpoint, instance_name, is_bot_enabled, is_notifications_enabled, status)
                VALUES ('simulator', 'http://localhost:8080', 'max_radius_bot', 1, 1, 'connected')"""
            )
            logger.info("Default WhatsApp settings initialized.")

        for tmpl in DEFAULT_TEMPLATES:
            exists = query_one("SELECT id FROM wisp_whatsapp_templates WHERE event_key = ?", (tmpl['event_key'],))
            if not exists:
                execute_write(
                    """INSERT INTO wisp_whatsapp_templates (event_key, title, template_text, is_enabled)
                    VALUES (?, ?, ?, 1)""",
                    (tmpl['event_key'], tmpl['title'], tmpl['template_text'])
                )
    except Exception as e:
        logger.warning(f"Error seeding WhatsApp default data: {e}")


def get_whatsapp_settings():
    """Retrieves WhatsApp gateway configuration and current status."""
    seed_default_whatsapp_data()
    row = query_one("SELECT * FROM wisp_whatsapp_settings ORDER BY id ASC LIMIT 1")
    if not row:
        return {
            'gateway_provider': 'simulator',
            'api_endpoint': 'http://localhost:8080',
            'api_key': '',
            'instance_name': 'max_radius_bot',
            'phone_number': '+967777000000',
            'is_bot_enabled': 1,
            'is_notifications_enabled': 1,
            'meta_app_id': '',
            'meta_phone_number_id': '',
            'meta_access_token': '',
            'meta_webhook_verify_token': 'max_radius_whatsapp_token_2026',
            'status': 'connected',
            'qr_code_raw': None,
            'last_connected_at': get_system_now_str()
        }
    return dict(row)


def update_whatsapp_settings(data):
    """Updates WhatsApp gateway configuration."""
    settings = get_whatsapp_settings()
    gateway_provider = data.get('gateway_provider', settings.get('gateway_provider', 'simulator'))
    api_endpoint = data.get('api_endpoint', settings.get('api_endpoint', 'http://localhost:8080')).strip()
    api_key = data.get('api_key', settings.get('api_key', '')).strip()
    instance_name = data.get('instance_name', settings.get('instance_name', 'max_radius_bot')).strip()
    phone_number = data.get('phone_number', settings.get('phone_number', '')).strip()
    is_bot_enabled = 1 if str(data.get('is_bot_enabled', '1')).lower() in ('1', 'true', 'on') else 0
    is_notifications_enabled = 1 if str(data.get('is_notifications_enabled', '1')).lower() in ('1', 'true', 'on') else 0
    meta_app_id = data.get('meta_app_id', '').strip()
    meta_phone_number_id = data.get('meta_phone_number_id', '').strip()
    meta_access_token = data.get('meta_access_token', '').strip()
    meta_webhook_verify_token = data.get('meta_webhook_verify_token', 'max_radius_whatsapp_token_2026').strip()

    status = 'connected' if gateway_provider == 'simulator' else settings.get('status', 'disconnected')

    execute_update(
        """UPDATE wisp_whatsapp_settings SET 
            gateway_provider = ?, api_endpoint = ?, api_key = ?, instance_name = ?, phone_number = ?,
            is_bot_enabled = ?, is_notifications_enabled = ?, meta_app_id = ?, meta_phone_number_id = ?,
            meta_access_token = ?, meta_webhook_verify_token = ?, status = ?
            WHERE id = ?""",
        (
            gateway_provider, api_endpoint, api_key, instance_name, phone_number,
            is_bot_enabled, is_notifications_enabled, meta_app_id, meta_phone_number_id,
            meta_access_token, meta_webhook_verify_token, status, settings['id']
        )
    )
    return True, "تم حفظ إعدادات الواتساب بنجاح."


def get_evolution_qr_code():
    """Fetches the live QR code and pairing code from Evolution API container."""
    settings = get_whatsapp_settings()
    endpoint = settings.get('api_endpoint', '').rstrip('/')
    api_key = settings.get('api_key', '')
    instance = settings.get('instance_name', 'max_radius_bot')

    endpoints_to_try = [endpoint, 'http://max_evolution_whatsapp:8080', 'http://localhost:3010']
    for ep in endpoints_to_try:
        if not ep:
            continue
        try:
            url = f"{ep.rstrip('/')}/instance/connect/{instance}"
            req = urllib.request.Request(url, headers={"apikey": api_key}, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                base64_qr = data.get('base64', '')
                pairing_code = data.get('pairingCode', '')
                code = data.get('code', '')
                if base64_qr:
                    execute_update(
                        "UPDATE wisp_whatsapp_settings SET status = 'qr_ready', qr_code_raw = ? WHERE id = ?",
                        (base64_qr, settings['id'])
                    )
                return {
                    'success': True,
                    'base64': base64_qr,
                    'pairingCode': pairing_code,
                    'code': code,
                    'instance': instance
                }
        except Exception:
            continue

    return {'success': False, 'message': 'تعذر الاتصال بحاوية Evolution API. يرجى التأكد من تشغيل الحاوية.'}


def get_evolution_connection_state():
    """Queries instance connection state ('open', 'connecting', 'close') and updates database."""
    settings = get_whatsapp_settings()
    endpoint = settings.get('api_endpoint', '').rstrip('/')
    api_key = settings.get('api_key', '')
    instance = settings.get('instance_name', 'max_radius_bot')

    endpoints_to_try = [endpoint, 'http://max_evolution_whatsapp:8080', 'http://localhost:3010']
    for ep in endpoints_to_try:
        if not ep:
            continue
        try:
            url = f"{ep.rstrip('/')}/instance/connectionState/{instance}"
            req = urllib.request.Request(url, headers={"apikey": api_key}, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                state = data.get('instance', {}).get('state', 'close')
                db_status = 'connected' if state == 'open' else ('qr_ready' if state == 'connecting' else 'disconnected')
                execute_update(
                    "UPDATE wisp_whatsapp_settings SET status = ?, last_connected_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (db_status, settings['id'])
                )
                return {'success': True, 'state': state, 'status': db_status}
        except Exception:
            continue

    return {'success': False, 'status': 'disconnected', 'state': 'offline'}


def restart_evolution_instance():
    """Restarts or recreates instance in Evolution API."""
    settings = get_whatsapp_settings()
    endpoint = settings.get('api_endpoint', '').rstrip('/')
    api_key = settings.get('api_key', '')
    instance = settings.get('instance_name', 'max_radius_bot')

    endpoints_to_try = [endpoint, 'http://max_evolution_whatsapp:8080', 'http://localhost:3010']
    for ep in endpoints_to_try:
        if not ep:
            continue
        try:
            url = f"{ep.rstrip('/')}/instance/restart/{instance}"
            req = urllib.request.Request(url, headers={"apikey": api_key}, method="POST")
            with urllib.request.urlopen(req, timeout=6) as resp:
                return {'success': True, 'message': 'تمت إعادة تشغيل جلسة الواتساب بنجاح.'}
        except Exception:
            continue
    return {'success': False, 'message': 'فشل إعادة تشغيل الجلسة.'}


def get_all_templates():
    """Fetches all customizable message templates."""
    seed_default_whatsapp_data()
    rows = query_all("SELECT * FROM wisp_whatsapp_templates ORDER BY id ASC")
    return [dict(r) for r in rows] if rows else []


def get_template_by_key(event_key):
    """Fetches a specific template by event key."""
    row = query_one("SELECT * FROM wisp_whatsapp_templates WHERE event_key = ?", (event_key,))
    return dict(row) if row else None


def save_template(template_id, title, template_text, is_enabled=1):
    """Updates a message template."""
    execute_update(
        "UPDATE wisp_whatsapp_templates SET title = ?, template_text = ?, is_enabled = ? WHERE id = ?",
        (title, template_text, is_enabled, template_id)
    )
    return True, "تم تحديث القالب بنجاح."


def format_bytes_display(mb_val):
    """Formats MB value to human readable string."""
    try:
        mb = float(mb_val)
        if mb <= 0:
            return "غير محدود (Unlimited)"
        if mb >= 1024:
            return f"{mb / 1024:.2f} GB"
        return f"{mb:.0f} MB"
    except Exception:
        return f"{mb_val} MB"


def clean_phone_number(raw_phone):
    """Normalizes phone number to international E.164 digits without plus or spaces."""
    if not raw_phone:
        return ""
    digits = re.sub(r'\D', '', str(raw_phone))
    if digits.startswith('00'):
        digits = digits[2:]
    if digits.startswith('0') and len(digits) == 10:
        digits = '967' + digits[1:]
    return digits


def log_whatsapp_message(direction, phone_number, message_body, message_type='text', event_type='bot_reply', status='sent', error_message=None):
    """Logs message into database audit stream."""
    try:
        execute_write(
            """INSERT INTO wisp_whatsapp_logs 
            (direction, phone_number, message_body, message_type, event_type, status, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (direction, clean_phone_number(phone_number), message_body, message_type, event_type, status, error_message)
        )
    except Exception as e:
        logger.error(f"Error logging WhatsApp message: {e}")


def get_whatsapp_logs(limit=100, filter_status=None, filter_phone=None):
    """Fetches message audit logs with optional filtering."""
    sql = "SELECT * FROM wisp_whatsapp_logs WHERE 1=1"
    params = []
    if filter_status and filter_status != 'all':
        sql += " AND status = ?"
        params.append(filter_status)
    if filter_phone:
        sql += " AND phone_number LIKE ?"
        params.append(f"%{filter_phone}%")
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    rows = query_all(sql, tuple(params))
    return [dict(r) for r in rows] if rows else []


def clear_whatsapp_logs():
    """Clears message history."""
    execute_update("DELETE FROM wisp_whatsapp_logs")
    return True, "تم مسح سجل الرسائل بنجاح."


def send_whatsapp_message(phone_number, message_text, event_type='manual', message_type='text'):
    """
    Main entry point for dispatching a WhatsApp message.
    Routes to the configured gateway provider.
    """
    settings = get_whatsapp_settings()
    phone = clean_phone_number(phone_number)
    if not phone:
        return False, "رقم الهاتف غير صالح."

    provider = settings.get('gateway_provider', 'simulator')

    success = False
    err_msg = None

    if provider == 'simulator':
        # Simulated dispatch
        success = True
        log_whatsapp_message('out', phone, message_text, message_type, event_type, 'sent')
        return True, "تم إرسال الرسالة عبر المحاكي الافتراضي بنجاح."

    elif provider == 'evolution':
        api_key = settings.get('api_key', '')
        instance = settings.get('instance_name', 'max_radius_bot')
        payload = {
            "number": phone,
            "text": message_text,
            "textMessage": {"text": message_text},
            "options": {"delay": 1000, "presence": "composing"}
        }
        
        endpoints_to_try = [settings.get('api_endpoint', '').rstrip('/'), 'http://max_evolution_whatsapp:8080', 'http://localhost:3010']
        for ep in endpoints_to_try:
            if not ep:
                continue
            try:
                url = f"{ep}/message/sendText/{instance}"
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode('utf-8'),
                    headers={"Content-Type": "application/json", "apikey": api_key},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=8) as resp:
                    if resp.status in (200, 201):
                        success = True
                        err_msg = None
                        break
                    else:
                        err_msg = f"HTTP {resp.status}"
            except Exception as e:
                err_msg = str(e)

    elif provider == 'meta_cloud':
        phone_id = settings.get('meta_phone_number_id', '')
        token = settings.get('meta_access_token', '')
        url = f"https://graph.facebook.com/v20.0/{phone_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": phone,
            "type": "text",
            "text": {"preview_url": True, "body": message_text}
        }
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode('utf-8'),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status in (200, 201):
                    success = True
                else:
                    err_msg = f"HTTP {resp.status}"
        except Exception as e:
            err_msg = str(e)

    else:
        # Generic HTTP Webhook
        endpoint = settings.get('api_endpoint', '')
        payload = {"to": phone, "message": message_text, "type": message_type}
        try:
            req = urllib.request.Request(
                endpoint,
                data=json.dumps(payload).encode('utf-8'),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                success = (resp.status in (200, 201))
        except Exception as e:
            err_msg = str(e)

    status = 'sent' if success else 'failed'
    log_whatsapp_message('out', phone, message_text, message_type, event_type, status, err_msg)
    return success, ("تم إرسال الرسالة بنجاح." if success else f"فشل الإرسال: {err_msg}")


# --------------------------------------------------------------------------
# Smart Interactive Bot Engine (Auto-Reply & Commands)
# --------------------------------------------------------------------------

def get_or_create_session(phone_number, sender_name=''):
    """Retrieves or creates conversational state for the given phone number."""
    phone = clean_phone_number(phone_number)
    row = query_one("SELECT * FROM wisp_whatsapp_sessions WHERE phone_number = ?", (phone,))
    if not row:
        execute_write(
            "INSERT INTO wisp_whatsapp_sessions (phone_number, sender_name, current_state) VALUES (?, ?, 'idle')",
            (phone, sender_name)
        )
        row = query_one("SELECT * FROM wisp_whatsapp_sessions WHERE phone_number = ?", (phone,))
    else:
        execute_update(
            "UPDATE wisp_whatsapp_sessions SET last_interaction_at = CURRENT_TIMESTAMP, sender_name = ? WHERE id = ?",
            (sender_name or row['sender_name'], row['id'])
        )
    return dict(row)


def update_session_state(session_id, state, data=None):
    """Updates the user session conversation state."""
    execute_update(
        "UPDATE wisp_whatsapp_sessions SET current_state = ?, session_data = ? WHERE id = ?",
        (state, json.dumps(data) if data else None, session_id)
    )


def query_voucher_status(search_term):
    """Finds voucher by username or PIN and returns a structured summary."""
    term = search_term.strip()
    v = query_one(
        """SELECT v.*, p.name as package_name, p.price, p.validity_value, p.validity_unit,
                  p.volume_quota_mb, p.rate_download, p.rate_upload
           FROM wisp_vouchers v
           LEFT JOIN wisp_packages p ON v.package_id = p.id
           WHERE v.username = ? OR v.pin_code = ? OR v.serial_number = ?
           LIMIT 1""",
        (term, term, term)
    )
    if not v:
        return None

    # Calculate used quota from radacct
    acct = query_one(
        """SELECT COALESCE(SUM(acctinputoctets), 0) as total_in,
                  COALESCE(SUM(acctoutputoctets), 0) as total_out,
                  COALESCE(SUM(acctsessiontime), 0) as total_time
           FROM radacct WHERE username = ?""",
        (v['username'],)
    )
    total_bytes = (acct['total_in'] + acct['total_out']) if acct else 0
    used_mb = total_bytes / (1024 * 1024)
    total_quota_mb = v['snap_volume_quota_mb'] or v['volume_quota_mb'] or 0
    remaining_mb = max(0, total_quota_mb - used_mb) if total_quota_mb > 0 else 0

    return {
        'username': v['username'],
        'pin_code': v.get('pin_code', ''),
        'status': v['status'],
        'package_name': v['package_name'] or 'باقة عامة',
        'price': v['price'] or 0,
        'used_mb': used_mb,
        'total_quota_mb': total_quota_mb,
        'remaining_mb': remaining_mb,
        'expires_at': v.get('expires_at') or 'لم يبدأ بعد',
        'first_used_at': v.get('first_used_at') or 'غير نشط'
    }


def query_subscriber_status(search_term):
    """Finds PPPoE subscriber by username or phone."""
    term = search_term.strip()
    s = query_one(
        """SELECT s.*, p.name as package_name, p.price, p.volume_quota_mb
           FROM wisp_subscribers s
           LEFT JOIN wisp_packages p ON s.package_id = p.id
           WHERE s.username = ? OR s.phone = ?
           LIMIT 1""",
        (term, term)
    )
    if not s:
        return None

    acct = query_one(
        """SELECT COALESCE(SUM(acctinputoctets), 0) + COALESCE(SUM(acctoutputoctets), 0) as total_bytes
           FROM radacct WHERE username = ?""",
        (s['username'],)
    )
    total_bytes = acct['total_bytes'] if acct else 0
    used_mb = total_bytes / (1024 * 1024)
    total_quota_mb = s['volume_quota_mb'] or 0
    remaining_mb = max(0, total_quota_mb - used_mb) if total_quota_mb > 0 else 0

    return {
        'username': s['username'],
        'full_name': s.get('full_name') or s['username'],
        'status': 'نشط' if s.get('is_active', 1) else 'موقف',
        'package_name': s['package_name'] or 'باقة منزلية',
        'price': s['price'] or 0,
        'used_mb': used_mb,
        'total_quota_mb': total_quota_mb,
        'remaining_mb': remaining_mb,
        'expires_at': s.get('expires_at') or 'اشتراك مفتوح'
    }


def process_incoming_whatsapp_message(phone_number, message_text, sender_name=''):
    """
    Core Bot Dialog Engine: Parses incoming WhatsApp message and returns reply.
    """
    settings = get_whatsapp_settings()
    phone = clean_phone_number(phone_number)
    text = (message_text or '').strip()

    # Log incoming message
    log_whatsapp_message('in', phone, text, 'text', 'incoming_command', 'received')

    if not settings.get('is_bot_enabled', 1):
        return None

    session = get_or_create_session(phone, sender_name)
    state = session.get('current_state', 'idle')

    # 1. State: Awaiting Card / Voucher Number for Inquiry
    if state == 'awaiting_inquiry_card':
        update_session_state(session['id'], 'idle')
        # Check voucher
        v_info = query_voucher_status(text)
        if v_info:
            status_text = {
                'unused': '🟢 كارت جديد (جاهز للاستخدام)',
                'active': '⚡ نشط حالياً',
                'depleted': '🔴 رصيد منتهي',
                'expired': '⌛ منتهي الصلاحية'
            }.get(v_info['status'], v_info['status'])

            quota_str = format_bytes_display(v_info['remaining_mb']) if v_info['total_quota_mb'] > 0 else "غير محدود"
            used_str = format_bytes_display(v_info['used_mb'])

            return f"""📊 *تفاصيل كارت الإنترنت:*
━━━━━━━━━━━━━━━━━━
👤 *اسم الكارت:* `{v_info['username']}`
📦 *الباقة:* {v_info['package_name']}
🏷️ *الحالة:* {status_text}
📥 *المستهلك:* {used_str}
📊 *الرصيد المتبقي:* *{quota_str}*
📅 *تاريخ الانتهاء:* {v_info['expires_at']}
━━━━━━━━━━━━━━━━━━
💡 _لطلب خدمة أخرى أرسل رقم 0 أو كلمة (مساعدة)._"""

        # Check subscriber
        s_info = query_subscriber_status(text)
        if s_info:
            quota_str = format_bytes_display(s_info['remaining_mb']) if s_info['total_quota_mb'] > 0 else "غير محدود"
            return f"""📊 *تفاصيل اشتراك المشترك:*
━━━━━━━━━━━━━━━━━━
👤 *المشترك:* {s_info['full_name']} (`{s_info['username']}`)
📦 *الباقة:* {s_info['package_name']}
🏷️ *الحالة:* {s_info['status']}
📊 *الرصيد المتبقي:* *{quota_str}*
📅 *تاريخ التجديد:* {s_info['expires_at']}
━━━━━━━━━━━━━━━━━━
💡 _لطلب خدمة أخرى أرسل رقم 0 أو كلمة (مساعدة)._"""

        return f"""❌ *لم يتم العثور على أي كارت أو مشترك بالاسم:* `{text}`

يرجى التأكد من كتابة اسم المستخدم أو رقم الكارت بدقة وإعادة المحاولة.
_أرسل 0 للعودة للقائمة الرئيسية._"""

    # 2. State: Awaiting Recharge PIN
    if state == 'awaiting_recharge_pin':
        update_session_state(session['id'], 'idle')
        v_check = query_voucher_status(text)
        if v_check and v_check['status'] == 'unused':
            return f"""✅ *كارت الشحن صالح وجاهز للاستخدام!*
📦 *الباقة:* {v_check['package_name']}
💰 *السعر:* {v_check['price']} ريال
🔑 *رمز الكارت:* `{v_check['username']}`

لإكمال تسجيل الدخول، افتح متصفحك وسجل الدخول باستخدام رمز الكارت أعلاه مباشرة في شبكة الواي فاي 🚀"""
        elif v_check:
            return f"""⚠️ *كارت الشحن هذا مستخدم مسبقاً أو منتهي الصلاحية.*
الحالة الحالية: *{v_check['status']}*
_أرسل 0 للعودة للقائمة الرئيسية._"""
        else:
            return f"""❌ *رمز كارت الشحن غير صحيح.*
يرجى التأكد من الأرقام وإعادة المحاولة.
_أرسل 0 للعودة للقائمة الرئيسية._"""

    # 3. Main Command Matcher (Regex & Keyword detection)
    cmd = text.lower().strip()

    # Inquiry shortcuts (1 or words)
    if cmd in ('1', 'رصيدي', 'رصيد', 'استعلام', 'كرتي', 'حسابي', 'balance', 'my balance'):
        # Check if phone number matches any subscriber directly
        s_match = query_subscriber_status(phone)
        if s_match:
            quota_str = format_bytes_display(s_match['remaining_mb']) if s_match['total_quota_mb'] > 0 else "غير محدود"
            return f"""📊 *أهلاً بك {s_match['full_name']}! تفاصيل اشتراكك:*
━━━━━━━━━━━━━━━━━━
👤 *اسم المستخدم:* `{s_match['username']}`
📦 *الباقة:* {s_match['package_name']}
🏷️ *الحالة:* {s_match['status']}
📊 *الرصيد المتبقي:* *{quota_str}*
📅 *تاريخ الانتهاء:* {s_match['expires_at']}
━━━━━━━━━━━━━━━━━━
💡 _لشحن كارت إضافي أرسل 2_"""

        update_session_state(session['id'], 'awaiting_inquiry_card')
        return "🔍 *يرجى إرسال اسم المستخدم أو رقم كارت الإنترنت للاستعلام عن رصيده:*"

    # Recharge shortcut (2)
    elif cmd in ('2', 'شحن', 'تعبئة', 'تجديد', 'recharge', 'topup'):
        update_session_state(session['id'], 'awaiting_recharge_pin')
        return "💳 *يرجى إرسال رقم كارت الشحن (PIN) لتفعيله والتحقق منه:*"

    # Packages & Prices list (3)
    elif cmd in ('3', 'باقات', 'عروض', 'اسعار', 'الاسعار', 'الباقات', 'packages', 'plans'):
        packages = query_all("SELECT name, price, volume_quota_mb, validity_value, validity_unit FROM wisp_packages WHERE is_active = 1 ORDER BY price ASC LIMIT 10")
        if not packages:
            return "📦 *لا توجد باقات متاحة حالياً. يرجى التواصل مع الإدارة.*"

        pkg_lines = []
        for idx, p in enumerate(packages, 1):
            quota_txt = format_bytes_display(p['volume_quota_mb'])
            val_txt = f"{p['validity_value']} {p['validity_unit']}"
            pkg_lines.append(f"{idx}️⃣ *{p['name']}*\n   📊 السعة: {quota_txt} | ⏳ الصلاحية: {val_txt}\n   💰 السعر: *{p['price']} ريال*")

        pkgs_text = "\n\n".join(pkg_lines)
        return f"""📋 *قائمة الباقات والعروض المتاحة:*
━━━━━━━━━━━━━━━━━━
{pkgs_text}
━━━━━━━━━━━━━━━━━━
💡 _لشراء أو شحن أي باقة، يمكنك التواصل مع أقرب موزع أو إرسال رقم 2 للشحن بكارت._"""

    # Support / Contact (4)
    elif cmd in ('4', 'دعم', 'مساعدة', 'اتصال', 'تواصل', 'شكوى', 'help', 'support'):
        return f"""📞 *خدمة العملاء والدعم الفني:*
━━━━━━━━━━━━━━━━━━
إذا كنت تواجه أي مشكلة في الشبكة أو سرعة الإنترنت، يرجى كتابة تفاصيل مشكلتك وسيقوم فريق الدعم بالرد عليك في أقرب وقت.

⏰ *أوقات العمل:* 24/7 على مدار الساعة.
📍 *إدارة شبكة:* {APP_NAME}"""

    # Direct card inquiry if user sends a voucher-like string directly (e.g. 4+ digits/chars)
    elif len(cmd) >= 4 and not ' ' in cmd and not cmd in ('0', 'menu', 'قائمة', 'أوامر'):
        v_auto = query_voucher_status(cmd)
        if v_auto:
            status_text = {
                'unused': '🟢 كارت جديد (جاهز للاستخدام)',
                'active': '⚡ نشط حالياً',
                'depleted': '🔴 رصيد منتهي',
                'expired': '⌛ منتهي الصلاحية'
            }.get(v_auto['status'], v_auto['status'])
            quota_str = format_bytes_display(v_auto['remaining_mb']) if v_auto['total_quota_mb'] > 0 else "غير محدود"
            return f"""📊 *بيانات الكارت `{v_auto['username']}`:*
━━━━━━━━━━━━━━━━━━
📦 *الباقة:* {v_auto['package_name']}
🏷️ *الحالة:* {status_text}
📊 *الرصيد المتبقي:* *{quota_str}*
📅 *تاريخ الانتهاء:* {v_auto['expires_at']}
━━━━━━━━━━━━━━━━━━
_أرسل 0 للعودة للقائمة الرئيسية._"""

    # Reseller Wallet Check shortcut (for registered resellers)
    elif cmd in ('محفظتي', 'رصيد المحفظة', 'وكيل'):
        reseller = query_one("SELECT * FROM wisp_resellers WHERE phone = ? LIMIT 1", (phone,))
        if reseller:
            return f"""💼 *بيانات محفظة الوكيل:*
━━━━━━━━━━━━━━━━━━
👤 *الوكيل:* {reseller['name']}
💰 *رصيد المحفظة الحالي:* *{reseller['wallet_balance']} ريال*
📦 *نسبة الخصم:* {reseller.get('discount_percent', 0)}%
━━━━━━━━━━━━━━━━━━"""

    # Default / Welcome Menu
    welcome_tmpl = get_template_by_key('welcome_menu')
    if welcome_tmpl and welcome_tmpl.get('template_text'):
        return welcome_tmpl['template_text'].replace('{network_name}', APP_NAME)

    return f"""👋 أهلاً بك في خدمة العملاء لشبكة *{APP_NAME}*!
أرسل:
1️⃣ للاستعلام عن الرصيد
2️⃣ لشحن كارت جديد
3️⃣ لعرض الباقات والأسعار
4️⃣ للدعم الفني المباشر"""


# --------------------------------------------------------------------------
# Automated Notification Triggers
# --------------------------------------------------------------------------

def notify_voucher_issued(voucher_dict, recipient_phone):
    """Dispatches a formatted voucher card notification via WhatsApp."""
    settings = get_whatsapp_settings()
    if not settings.get('is_notifications_enabled', 1):
        return False, "الإشعارات معطلة."

    tmpl = get_template_by_key('voucher_issued')
    if not tmpl or not tmpl.get('is_enabled', 1):
        return False, "قالب إشعار الكارت معطل."

    text = tmpl['template_text']
    text = text.replace('{username}', str(voucher_dict.get('username', '')))
    text = text.replace('{password}', str(voucher_dict.get('pin_code') or voucher_dict.get('password') or voucher_dict.get('username', '')))
    text = text.replace('{package_name}', str(voucher_dict.get('package_name', 'باقة عامة')))
    text = text.replace('{quota}', format_bytes_display(voucher_dict.get('volume_quota_mb', 0)))
    text = text.replace('{validity}', f"{voucher_dict.get('validity_value', 1)} {voucher_dict.get('validity_unit', 'أيام')}")
    text = text.replace('{price}', str(voucher_dict.get('price', 0)))
    text = text.replace('{currency}', 'ريال')
    text = text.replace('{login_url}', voucher_dict.get('login_url', 'http://1.1.1.1/login'))

    return send_whatsapp_message(recipient_phone, text, event_type='voucher_issued')


def notify_quota_warning(username, recipient_phone, package_name, remaining_mb, expiry_date):
    """Sends 80% quota warning notification."""
    settings = get_whatsapp_settings()
    if not settings.get('is_notifications_enabled', 1):
        return False, "الإشعارات معطلة."

    tmpl = get_template_by_key('quota_warning')
    if not tmpl or not tmpl.get('is_enabled', 1):
        return False, "قالب تنبيه الاستهلاك معطل."

    text = tmpl['template_text']
    text = text.replace('{username}', str(username))
    text = text.replace('{package_name}', str(package_name))
    text = text.replace('{quota_remaining}', format_bytes_display(remaining_mb))
    text = text.replace('{expiry_date}', str(expiry_date))

    return send_whatsapp_message(recipient_phone, text, event_type='quota_warning')


def notify_wallet_recharge(username, recipient_phone, amount, new_balance):
    """Sends wallet top-up confirmation notification."""
    settings = get_whatsapp_settings()
    if not settings.get('is_notifications_enabled', 1):
        return False, "الإشعارات معطلة."

    tmpl = get_template_by_key('wallet_recharge')
    if not tmpl or not tmpl.get('is_enabled', 1):
        return False, "قالب شحن المحفظة معطل."

    text = tmpl['template_text']
    text = text.replace('{username}', str(username))
    text = text.replace('{amount}', str(amount))
    text = text.replace('{new_balance}', str(new_balance))
    text = text.replace('{currency}', 'ريال')
    text = text.replace('{transaction_time}', get_system_now_str())

    return send_whatsapp_message(recipient_phone, text, event_type='wallet_recharge')


def send_broadcast_campaign(target_audience, message_text, specific_reseller_id=None):
    """
    Dispatches a mass broadcast campaign to subscribers or resellers.
    """
    if not message_text or not message_text.strip():
        return 0, 0, "نص الرسالة فارغ."

    recipients = []
    if target_audience in ('all_subscribers', 'active_subscribers'):
        sql = "SELECT DISTINCT phone, username FROM wisp_subscribers WHERE phone IS NOT NULL AND phone != ''"
        if target_audience == 'active_subscribers':
            sql += " AND is_active = 1"
        rows = query_all(sql)
        if rows:
            recipients.extend([r['phone'] for r in rows])

    elif target_audience == 'resellers':
        sql = "SELECT DISTINCT phone FROM wisp_resellers WHERE phone IS NOT NULL AND phone != ''"
        if specific_reseller_id:
            sql += " AND id = ?"
            rows = query_all(sql, (specific_reseller_id,))
        else:
            rows = query_all(sql)
        if rows:
            recipients.extend([r['phone'] for r in rows])

    unique_phones = list(set([clean_phone_number(p) for p in recipients if clean_phone_number(p)]))
    if not unique_phones:
        return 0, 0, "لم يتم العثور على أي أرقام هواتف مسجلة للجمهور المحدد."

    sent_count = 0
    failed_count = 0

    def _broadcast_worker():
        nonlocal sent_count, failed_count
        for ph in unique_phones:
            ok, _ = send_whatsapp_message(ph, message_text, event_type='broadcast')
            if ok:
                sent_count += 1
            else:
                failed_count += 1
            time.sleep(0.5)

    worker = threading.Thread(target=_broadcast_worker, daemon=True)
    worker.start()

    return len(unique_phones), 0, f"تم بدء إرسال الحملة إلى {len(unique_phones)} رقم في الخلفية."

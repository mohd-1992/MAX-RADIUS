"""
services/bot_notifications_service.py
-------------------------------------
Smart Telegram & WhatsApp Notifications and Bot Integration for MAX RADIUS.
Handles alert dispatching to admins, threshold alerts to subscribers,
automated database backup shipping via Telegram Document API, and log tracking.
"""

import os
import urllib.request
import urllib.parse
import json
import time
import requests
from database.db import get_connection

_get_db = get_connection

def ensure_notifications_tables():
    """Ensure notification settings and dispatch log tables exist with all required columns."""
    db = _get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS wisp_notification_channels (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    channel_type VARCHAR(32) NOT NULL DEFAULT 'telegram',
                    bot_token VARCHAR(255) NULL,
                    chat_id VARCHAR(128) NULL,
                    backup_chat_id VARCHAR(128) NULL,
                    is_enabled TINYINT(1) DEFAULT 1,
                    auto_send_backups TINYINT(1) DEFAULT 1,
                    notify_on_low_quota TINYINT(1) DEFAULT 1,
                    notify_on_recharge TINYINT(1) DEFAULT 1,
                    notify_on_nas_down TINYINT(1) DEFAULT 1,
                    notify_daily_summary TINYINT(1) DEFAULT 1,
                    custom_webhook_url TEXT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS wisp_notification_logs (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    channel_type VARCHAR(32) NOT NULL,
                    recipient VARCHAR(128) NOT NULL,
                    message_type VARCHAR(64) NOT NULL,
                    message_text TEXT NOT NULL,
                    status ENUM('sent', 'failed') DEFAULT 'sent',
                    response_details TEXT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_recipient (recipient),
                    INDEX idx_msg_type (message_type)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)

            # Ensure dynamic columns exist for existing installations
            try:
                cur.execute("SHOW COLUMNS FROM wisp_notification_channels LIKE 'backup_chat_id'")
                if not cur.fetchone():
                    cur.execute("ALTER TABLE wisp_notification_channels ADD COLUMN backup_chat_id VARCHAR(128) NULL AFTER chat_id")
            except Exception:
                pass

            try:
                cur.execute("SHOW COLUMNS FROM wisp_notification_channels LIKE 'auto_send_backups'")
                if not cur.fetchone():
                    cur.execute("ALTER TABLE wisp_notification_channels ADD COLUMN auto_send_backups TINYINT(1) DEFAULT 1 AFTER is_enabled")
            except Exception:
                pass

            # Initialize default channel record if empty
            cur.execute("SELECT COUNT(*) as cnt FROM wisp_notification_channels")
            if (cur.fetchone() or {}).get('cnt', 0) == 0:
                cur.execute("""
                    INSERT INTO wisp_notification_channels (channel_type, bot_token, chat_id, is_enabled, auto_send_backups)
                    VALUES ('telegram', '', '', 0, 1)
                """)
            db.commit()
    finally:
        db.close()

def get_notification_settings():
    """Get active notification settings and recent log history."""
    ensure_notifications_tables()
    db = _get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT * FROM wisp_notification_channels LIMIT 1")
            settings = cur.fetchone() or {}

            cur.execute("SELECT COUNT(*) as total_sent FROM wisp_notification_logs WHERE status = 'sent'")
            total_sent = (cur.fetchone() or {}).get('total_sent', 0)

            cur.execute("SELECT COUNT(*) as total_failed FROM wisp_notification_logs WHERE status = 'failed'")
            total_failed = (cur.fetchone() or {}).get('total_failed', 0)

            cur.execute("SELECT * FROM wisp_notification_logs ORDER BY id DESC LIMIT 25")
            logs = cur.fetchall()

        return {
            'settings': settings,
            'total_sent': total_sent,
            'total_failed': total_failed,
            'logs': logs
        }
    finally:
        db.close()

def verify_telegram_bot(bot_token=None):
    """Test Telegram Bot Token via getMe API call and return bot profile details."""
    token = (bot_token or '').strip()
    if not token:
        settings_data = get_notification_settings()
        token = (settings_data.get('settings') or {}).get('bot_token', '').strip()

    if not token:
        return False, "لم يتم تحديد رمز البوت (Bot Token)."

    try:
        url = f"https://api.telegram.org/bot{token}/getMe"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if data.get('ok'):
            result = data.get('result', {})
            bot_info = {
                'id': result.get('id'),
                'username': result.get('username'),
                'first_name': result.get('first_name'),
                'can_join_groups': result.get('can_join_groups', True),
                'can_read_all_group_messages': result.get('can_read_all_group_messages', False),
            }
            return True, bot_info
        else:
            return False, data.get('description', 'فشل التحقق من رمز البوت.')
    except Exception as e:
        return False, f"خطأ في الاتصال بسيرفرات تيليجرام: {str(e)}"

def update_notification_settings(bot_token, chat_id, is_enabled, low_quota, recharge, nas_down, daily_summary, backup_chat_id=None, auto_send_backups=1):
    """Save updated notification channel and backup shipping settings."""
    ensure_notifications_tables()
    db = _get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""
                UPDATE wisp_notification_channels SET
                    bot_token = %s,
                    chat_id = %s,
                    backup_chat_id = %s,
                    is_enabled = %s,
                    auto_send_backups = %s,
                    notify_on_low_quota = %s,
                    notify_on_recharge = %s,
                    notify_on_nas_down = %s,
                    notify_daily_summary = %s
                WHERE id = 1
            """, (
                bot_token.strip(),
                chat_id.strip(),
                (backup_chat_id or '').strip() or None,
                int(is_enabled),
                int(auto_send_backups),
                int(low_quota),
                int(recharge),
                int(nas_down),
                int(daily_summary)
            ))
            db.commit()
            return True, "تم حفظ إعدادات مركز الإشعارات والربط بنجاح."
    except Exception as e:
        return False, str(e)
    finally:
        db.close()

def send_telegram_alert(message_text, message_type='SYSTEM_ALERT', override_chat_id=None):
    """Dispatch a text alert via official Telegram Bot API and record audit log."""
    ensure_notifications_tables()
    db = _get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT bot_token, chat_id, is_enabled FROM wisp_notification_channels LIMIT 1")
            row = cur.fetchone()
        
        if not row:
            return False, "قناة الإشعارات غير مهيأة"
        
        token = row.get('bot_token')
        chat_id = override_chat_id or row.get('chat_id')
        
        if not token or not chat_id:
            return False, "يرجى تحديد الـ Bot Token و Chat ID أولاً."

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            'chat_id': chat_id,
            'text': message_text,
            'parse_mode': 'HTML'
        }

        try:
            resp = requests.post(url, json=payload, timeout=12)
            res_data = resp.json()
            if not res_data.get('ok') and 'can\'t parse entities' in str(res_data.get('description', '')).lower():
                # Fallback to plain text without parse_mode
                payload.pop('parse_mode', None)
                resp = requests.post(url, json=payload, timeout=12)
                res_data = resp.json()
            status = 'sent' if res_data.get('ok') else 'failed'
            details = json.dumps(res_data, ensure_ascii=False)
        except Exception as err:
            status = 'failed'
            details = str(err)

        # Log event
        with db.cursor() as cur:
            cur.execute("""
                INSERT INTO wisp_notification_logs (channel_type, recipient, message_type, message_text, status, response_details)
                VALUES ('telegram', %s, %s, %s, %s, %s)
            """, (str(chat_id), message_type, message_text, status, details))
            db.commit()

        if status == 'sent':
            return True, "تم إرسال الإشعار بنجاح."
        else:
            return False, f"فشل الإرسال: {details}"

    except Exception as e:
        return False, str(e)
    finally:
        db.close()

def send_telegram_document(filepath, caption='', message_type='BACKUP_ARCHIVE', override_chat_id=None):
    """
    Dispatch a document/archive file directly to Telegram using official sendDocument API.
    Supports file uploads up to Telegram's 50MB Bot API limit with resilient HTML/Plain fallback.
    """
    ensure_notifications_tables()
    
    if not filepath or not os.path.isfile(filepath):
        return False, f"الملف المطلوب غير موجود على السيرفر: {filepath}"

    file_size_bytes = os.path.getsize(filepath)
    file_size_mb = file_size_bytes / (1024 * 1024)

    # Telegram Bot API hard limit for file uploads is 50 MB
    if file_size_mb > 50.0:
        return False, f"حجم الملف ({file_size_mb:.2f} MB) يتجاوز الحد الأقصى المسموح به في بوتات تيليجرام (50 MB)."

    db = _get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT bot_token, chat_id, backup_chat_id, is_enabled FROM wisp_notification_channels LIMIT 1")
            row = cur.fetchone()

        if not row:
            return False, "قناة الإشعارات غير مهيأة"

        token = row.get('bot_token')
        # Use dedicated backup_chat_id if provided, else fall back to main chat_id
        chat_id = override_chat_id or row.get('backup_chat_id') or row.get('chat_id')

        if not token or not chat_id:
            return False, "يرجى تهيئة Bot Token ومعرف المحادثة Chat ID أولاً في مركز الإشعارات."

        url = f"https://api.telegram.org/bot{token}/sendDocument"
        filename = os.path.basename(filepath)

        with open(filepath, 'rb') as f:
            files = {
                'document': (filename, f)
            }
            data = {
                'chat_id': str(chat_id),
                'caption': caption or f"📦 نسخة احتياطية من MAX RADIUS\nالملف: {filename}",
                'parse_mode': 'HTML'
            }
            try:
                resp = requests.post(url, data=data, files=files, timeout=120)
                res_data = resp.json()
                if not res_data.get('ok') and 'can\'t parse entities' in str(res_data.get('description', '')).lower():
                    # Retry without parse_mode as plain text
                    f.seek(0)
                    data.pop('parse_mode', None)
                    resp = requests.post(url, data=data, files=files, timeout=120)
                    res_data = resp.json()

                status = 'sent' if res_data.get('ok') else 'failed'
                details = json.dumps(res_data, ensure_ascii=False)
            except Exception as err:
                status = 'failed'
                details = str(err)

        # Log event in notification history
        with db.cursor() as cur:
            cur.execute("""
                INSERT INTO wisp_notification_logs (channel_type, recipient, message_type, message_text, status, response_details)
                VALUES ('telegram', %s, %s, %s, %s, %s)
            """, (str(chat_id), message_type, f"Document: {filename} ({file_size_mb:.2f} MB)", status, details))
            db.commit()

        if status == 'sent':
            return True, f"تم إرسال الملف [{filename}] بنجاح عبر تيليجرام."
        else:
            return False, f"تعذر الإرسال عبر تيليجرام: {details}"

    except Exception as e:
        return False, str(e)
    finally:
        db.close()


# ==============================================================================
# Automatic Event Trigger Handlers
# ==============================================================================

import threading
import datetime

_NAS_OFFLINE_CACHE = {} # nas_id -> last_alert_timestamp
_LOW_QUOTA_CACHE = {}   # f"{username}_{date}" -> True

def trigger_recharge_notification(username, package_name, price, card_number=None, recharge_type='package'):
    """
    Trigger alert upon successful voucher recharge/activation.
    Respects notify_on_recharge setting and dispatches asynchronously.
    """
    def _run():
        try:
            settings_data = get_notification_settings()
            st = settings_data.get('settings') or {}
            if not st.get('is_enabled') or not st.get('notify_on_recharge') or not st.get('bot_token'):
                return

            now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            card_str = str(card_number or '').strip()
            if len(card_str) >= 8:
                card_mask = f"<code>{card_str[:3]}****{card_str[-3:]}</code>"
            elif card_str:
                card_mask = f"<code>{card_str}</code>"
            else:
                card_mask = "شحن محفظة/قسيمة"

            type_label = "شحن باقة إنترنت" if recharge_type == 'package' else "شحن رصيد مالي"

            msg = (
                f"💳 <b>عملية شحن كرت جديدة - MAX RADIUS</b>\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>المشترك:</b> <code>{username}</code>\n"
                f"📦 <b>الباقة:</b> {package_name}\n"
                f"💰 <b>القيمة:</b> {price} ريال\n"
                f"🏷️ <b>نوع الشحن:</b> {type_label}\n"
                f"🎫 <b>الكرت:</b> {card_mask}\n"
                f"🕒 <b>التوقيت:</b> <code>{now_str}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"#Recharge #Payment #Voucher"
            )
            send_telegram_alert(msg, message_type='RECHARGE_ALERT')
        except Exception as e:
            print(f"[Telegram Recharge Alert Error]: {e}")

    threading.Thread(target=_run, daemon=True).start()


def trigger_nas_down_notification(nas_id, nas_name, nas_ip, status_text='Offline'):
    """
    Trigger alert when a NAS router is discovered to be down/offline.
    Includes anti-flood throttling (at most once every 30 minutes per device).
    """
    now = time.time()
    last_time = _NAS_OFFLINE_CACHE.get(nas_id, 0)
    if (now - last_time) < 1800:
        return

    _NAS_OFFLINE_CACHE[nas_id] = now

    def _run():
        try:
            settings_data = get_notification_settings()
            st = settings_data.get('settings') or {}
            if not st.get('is_enabled') or not st.get('notify_on_nas_down') or not st.get('bot_token'):
                return

            now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            msg = (
                f"🚨 <b>تنبيه: انقطاع اتصال رواتر NAS</b>\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"📡 <b>اسم الرواتر:</b> <b>{nas_name}</b>\n"
                f"🌐 <b>الآيبي IP:</b> <code>{nas_ip}</code>\n"
                f"⚠️ <b>الحالة:</b> غير متصل ({status_text})\n"
                f"🕒 <b>وقت الرصد:</b> <code>{now_str}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"يرجى التحقق من اتصال الرواتر أو كابل الشبكة.\n"
                f"#NAS_Alert #Offline #Router_Down"
            )
            send_telegram_alert(msg, message_type='NAS_DOWN_ALERT')
        except Exception as e:
            print(f"[Telegram NAS Down Alert Error]: {e}")

    threading.Thread(target=_run, daemon=True).start()


def trigger_nas_recovered_notification(nas_id, nas_name, nas_ip):
    """
    Trigger recovery alert when a previously offline NAS router recovers and is back online.
    """
    if nas_id in _NAS_OFFLINE_CACHE:
        del _NAS_OFFLINE_CACHE[nas_id]
        def _run():
            try:
                settings_data = get_notification_settings()
                st = settings_data.get('settings') or {}
                if not st.get('is_enabled') or not st.get('notify_on_nas_down') or not st.get('bot_token'):
                    return

                now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                msg = (
                    f"✅ <b>استعادة اتصال رواتر NAS</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"📡 <b>اسم الرواتر:</b> <b>{nas_name}</b>\n"
                    f"🌐 <b>الآيبي IP:</b> <code>{nas_ip}</code>\n"
                    f"📶 <b>الحالة:</b> متصل ويعمل بنجاح (Online)\n"
                    f"🕒 <b>توقيت العودة:</b> <code>{now_str}</code>\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"#NAS_Recovery #Online"
                )
                send_telegram_alert(msg, message_type='NAS_RECOVERED_ALERT')
            except Exception as e:
                print(f"[Telegram NAS Recovery Error]: {e}")

        threading.Thread(target=_run, daemon=True).start()


def trigger_daily_sales_summary():
    """
    Compute daily financial figures and dispatch formatted summary to Telegram.
    Executed automatically at 23:59 or upon admin trigger.
    """
    try:
        settings_data = get_notification_settings()
        st = settings_data.get('settings') or {}
        if not st.get('is_enabled') or not st.get('notify_daily_summary') or not st.get('bot_token'):
            return False, "إشعار التقرير اليومي غير مفعل أو القناة غير مهيأة."

        today_str = datetime.date.today().strftime('%Y-%m-%d')
        start_t = f"{today_str} 00:00:00"
        end_t = f"{today_str} 23:59:59"

        db = _get_db()
        try:
            with db.cursor() as cur:
                # 1. Total vouchers activated today & revenue
                cur.execute("""
                    SELECT COUNT(*) as cnt, COALESCE(SUM(price), 0) as total_rev
                    FROM wisp_voucher_sales
                    WHERE activated_at BETWEEN %s AND %s
                """, (start_t, end_t))
                sales_row = cur.fetchone() or {}
                vouchers_count = sales_row.get('cnt', 0)
                total_revenue = float(sales_row.get('total_rev', 0.0))

                # If sales table was empty, fallback to wisp_vouchers
                if vouchers_count == 0:
                    cur.execute("""
                        SELECT COUNT(*) as cnt, COALESCE(SUM(COALESCE(snap_price, 0)), 0) as total_rev
                        FROM wisp_vouchers
                        WHERE activated_at BETWEEN %s AND %s
                    """, (start_t, end_t))
                    v_row = cur.fetchone() or {}
                    vouchers_count = v_row.get('cnt', 0)
                    total_revenue = float(v_row.get('total_rev', 0.0))

                # 2. Total active subscribers
                cur.execute("SELECT COUNT(*) as cnt FROM wisp_subscribers WHERE status = 'active'")
                active_subs = (cur.fetchone() or {}).get('cnt', 0)

                # 3. Current online sessions in radacct
                cur.execute("SELECT COUNT(DISTINCT username) as cnt FROM radacct WHERE acctstoptime IS NULL")
                online_sessions = (cur.fetchone() or {}).get('cnt', 0)
        finally:
            db.close()

        msg = (
            f"📊 <b>التقرير المالي والمبيعات اليومي - MAX RADIUS</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📅 <b>التاريخ:</b> <code>{today_str}</code>\n"
            f"🎫 <b>الكروت المفعلة اليوم:</b> <b>{vouchers_count} كرت</b>\n"
            f"💵 <b>إجمالي الإيرادات اليوم:</b> <b>{total_revenue:,.2f} ريال</b>\n"
            f"👥 <b>المشتركون النشطون:</b> <b>{active_subs} مشترك</b>\n"
            f"📶 <b>المتصلون الآن بالشبكة:</b> <b>{online_sessions} جلسة</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"#Daily_Sales #Financial_Report #MAX_RADIUS"
        )
        return send_telegram_alert(msg, message_type='DAILY_SUMMARY')
    except Exception as e:
        return False, str(e)


def trigger_low_quota_notification(username, remaining_mb, total_mb):
    """
    Trigger warning alert when subscriber remaining quota drops below threshold.
    Throttled to at most once per 24 hours per user.
    """
    today_key = f"{username}_{datetime.date.today().strftime('%Y-%m-%d')}"
    if today_key in _LOW_QUOTA_CACHE:
        return

    _LOW_QUOTA_CACHE[today_key] = True

    def _run():
        try:
            settings_data = get_notification_settings()
            st = settings_data.get('settings') or {}
            if not st.get('is_enabled') or not st.get('notify_on_low_quota') or not st.get('bot_token'):
                return

            now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            pct = (remaining_mb / total_mb * 100) if total_mb > 0 else 0

            msg = (
                f"📉 <b>تنبيه: اقتراب نفاد رصيد المشترك</b>\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>المشترك:</b> <code>{username}</code>\n"
                f"📊 <b>الرصيد المتبقي:</b> <b>{remaining_mb:.1f} MB</b> (من أصل {total_mb:.1f} MB)\n"
                f"⚠️ <b>النسبة المتبقية:</b> <b>{pct:.1f}%</b>\n"
                f"🕒 <b>التوقيت:</b> <code>{now_str}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"#Low_Quota #Subscribers #Alert"
            )
            send_telegram_alert(msg, message_type='LOW_QUOTA_ALERT')
        except Exception as e:
            print(f"[Telegram Low Quota Alert Error]: {e}")

    threading.Thread(target=_run, daemon=True).start()


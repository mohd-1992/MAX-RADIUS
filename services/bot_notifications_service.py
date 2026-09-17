"""
services/bot_notifications_service.py
-------------------------------------
Smart Telegram & WhatsApp Notifications and Bot Integration for MAX RADIUS.
Handles alert dispatching to admins, threshold alerts to subscribers, and log tracking.
"""

import urllib.request
import urllib.parse
import json
import time
from database.db import get_connection

_get_db = get_connection

def ensure_notifications_tables():
    """Ensure notification settings and dispatch log tables exist."""
    db = _get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS wisp_notification_channels (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    channel_type VARCHAR(32) NOT NULL DEFAULT 'telegram',
                    bot_token VARCHAR(255) NULL,
                    chat_id VARCHAR(128) NULL,
                    is_enabled TINYINT(1) DEFAULT 1,
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
            # Initialize default channel record if empty
            cur.execute("SELECT COUNT(*) as cnt FROM wisp_notification_channels")
            if (cur.fetchone() or {}).get('cnt', 0) == 0:
                cur.execute("""
                    INSERT INTO wisp_notification_channels (channel_type, bot_token, chat_id, is_enabled)
                    VALUES ('telegram', '', '', 0)
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

            cur.execute("SELECT * FROM wisp_notification_logs ORDER BY id DESC LIMIT 20")
            logs = cur.fetchall()

        return {
            'settings': settings,
            'total_sent': total_sent,
            'total_failed': total_failed,
            'logs': logs
        }
    finally:
        db.close()

def update_notification_settings(bot_token, chat_id, is_enabled, low_quota, recharge, nas_down, daily_summary):
    """Save updated notification channel settings."""
    ensure_notifications_tables()
    db = _get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""
                UPDATE wisp_notification_channels SET
                    bot_token = %s,
                    chat_id = %s,
                    is_enabled = %s,
                    notify_on_low_quota = %s,
                    notify_on_recharge = %s,
                    notify_on_nas_down = %s,
                    notify_daily_summary = %s
                WHERE id = 1
            """, (bot_token, chat_id, int(is_enabled), int(low_quota), int(recharge), int(nas_down), int(daily_summary)))
            db.commit()
            return True, "تم حفظ إعدادات قنوات الإشعارات بنجاح."
    except Exception as e:
        return False, str(e)
    finally:
        db.close()

def send_telegram_alert(message_text, message_type='SYSTEM_ALERT', override_chat_id=None):
    """Dispatch a Telegram message via official Bot API and log the event."""
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
        payload = urllib.parse.urlencode({
            'chat_id': chat_id,
            'text': message_text,
            'parse_mode': 'Markdown'
        }).encode('utf-8')

        req = urllib.request.Request(url, data=payload, headers={'User-Agent': 'MAX-RADIUS-Bot/2.0'})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                res_data = json.loads(resp.read().decode('utf-8'))
                status = 'sent' if res_data.get('ok') else 'failed'
                details = json.dumps(res_data)
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

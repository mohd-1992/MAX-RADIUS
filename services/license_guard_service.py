# -*- coding: utf-8 -*-
"""
License Guard Service for MAX RADIUS:
Handles license storage in DB, live status lookup, quota enforcement,
grace period management, and automated heartbeat sync with vendor license hub.
"""

import os
import json
import time
import logging
import datetime
import urllib.request
import urllib.error
from pathlib import Path

from database.db import query_all, query_one, execute_write, get_connection, is_mysql_conn
from core.config import APP_VERSION, APP_EDITION
from core.hardware_fingerprint import get_machine_id, get_instance_uuid, get_combined_binding_code
from core.licensing import decode_license_string, verify_license_package, get_master_public_key_pem
from core.license_protocol import exchange as authenticated_license_exchange
from core.config import STORAGE_DIR

logger = logging.getLogger('license_guard_service')

DEFAULT_MASTER_SERVER_URL = os.environ.get("MASTER_LICENSE_SERVER_URL", "https://license.max-net.net")

_LICENSE_CACHE = {
    'data': None,
    'timestamp': 0
}

def clear_license_cache():
    global _LICENSE_CACHE
    _LICENSE_CACHE = {'data': None, 'timestamp': 0}

def ensure_license_tables():
    """Ensures wisp_license_info, wisp_revoked_licenses, and wisp_session_reservations tables and triggers exist."""
    try:
        conn = get_connection()
        is_mysql = is_mysql_conn(conn)
        conn.close()
        
        if is_mysql:
            sql = """
            CREATE TABLE IF NOT EXISTS `wisp_license_info` (
                `id` INT AUTO_INCREMENT PRIMARY KEY,
                `license_id` VARCHAR(100) NOT NULL,
                `client_name` VARCHAR(255) NOT NULL,
                `plan_tier` VARCHAR(50) DEFAULT 'Enterprise',
                `hardware_id` VARCHAR(100) DEFAULT 'ANY',
                `max_subscribers` INT DEFAULT 5000,
                `max_active_sessions` INT DEFAULT 0,
                `max_nas` INT DEFAULT 15,
                `max_managers` INT DEFAULT 10,
                `features_json` LONGTEXT,
                `raw_package_json` LONGTEXT NOT NULL,
                `token_b64` LONGTEXT,
                `signature_b64` VARCHAR(255) NOT NULL,
                `issued_at` VARCHAR(50),
                `expires_at` VARCHAR(50),
                `status` VARCHAR(30) DEFAULT 'active',
                `grace_period_until` DATETIME,
                `last_verified_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
                `last_heartbeat_at` DATETIME,
                `master_server_url` VARCHAR(255) DEFAULT 'http://127.0.0.1:5095',
                `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
                `updated_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY `uk_license` (`license_id`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            """
            execute_write(sql)
            execute_write("""
            CREATE TABLE IF NOT EXISTS `wisp_revoked_licenses` (
                `license_id` VARCHAR(100) PRIMARY KEY,
                `reason` TEXT,
                `revoked_at` DATETIME DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            """)
            execute_write("""
            CREATE TABLE IF NOT EXISTS `wisp_session_reservations` (
                `id` INT AUTO_INCREMENT PRIMARY KEY,
                `session_key` VARCHAR(191) NOT NULL,
                `username` VARCHAR(64) NOT NULL,
                `nasipaddress` VARCHAR(45) NOT NULL,
                `callingstationid` VARCHAR(50) NOT NULL,
                `reserved_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                `expires_at` DATETIME NOT NULL,
                UNIQUE KEY `uk_session_key` (`session_key`),
                INDEX `idx_res_expires` (`expires_at`),
                INDEX `idx_res_nas` (`nasipaddress`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            """)
            # Ensure max_active_sessions column in wisp_license_info
            try:
                execute_write("ALTER TABLE `wisp_license_info` ADD COLUMN `max_active_sessions` INT DEFAULT 0 AFTER `max_subscribers`")
            except Exception:
                pass
            # Ensure nasipaddress & callingstationid columns in radpostauth
            try:
                execute_write("ALTER TABLE `radpostauth` ADD COLUMN `nasipaddress` VARCHAR(45) DEFAULT NULL")
            except Exception:
                pass
            try:
                execute_write("ALTER TABLE `radpostauth` ADD COLUMN `callingstationid` VARCHAR(50) DEFAULT NULL")
            except Exception:
                pass
        else:
            sql = """
            CREATE TABLE IF NOT EXISTS `wisp_license_info` (
                `id` INTEGER PRIMARY KEY AUTOINCREMENT,
                `license_id` TEXT UNIQUE NOT NULL,
                `client_name` TEXT NOT NULL,
                `plan_tier` TEXT DEFAULT 'Enterprise',
                `hardware_id` TEXT DEFAULT 'ANY',
                `max_subscribers` INTEGER DEFAULT 5000,
                `max_active_sessions` INTEGER DEFAULT 0,
                `max_nas` INTEGER DEFAULT 15,
                `max_managers` INTEGER DEFAULT 10,
                `features_json` TEXT,
                `raw_package_json` TEXT NOT NULL,
                `token_b64` TEXT,
                `signature_b64` TEXT NOT NULL,
                `issued_at` TEXT,
                `expires_at` TEXT,
                `status` TEXT DEFAULT 'active',
                `grace_period_until` DATETIME,
                `last_verified_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
                `last_heartbeat_at` DATETIME,
                `master_server_url` TEXT DEFAULT 'http://127.0.0.1:5095',
                `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
                `updated_at` DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            """
            execute_write(sql)
            execute_write("""
            CREATE TABLE IF NOT EXISTS `wisp_revoked_licenses` (
                `license_id` TEXT PRIMARY KEY,
                `reason` TEXT,
                `revoked_at` DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            """)
            execute_write("""
            CREATE TABLE IF NOT EXISTS `wisp_session_reservations` (
                `id` INTEGER PRIMARY KEY AUTOINCREMENT,
                `session_key` TEXT UNIQUE NOT NULL,
                `username` TEXT NOT NULL,
                `nasipaddress` TEXT NOT NULL,
                `callingstationid` TEXT NOT NULL,
                `reserved_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
                `expires_at` DATETIME NOT NULL
            );
            """)
            try:
                execute_write("ALTER TABLE `wisp_license_info` ADD COLUMN `max_active_sessions` INTEGER DEFAULT 0")
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Error initializing wisp_license_info table: {e}")

def is_license_blacklisted(lic_id):
    """Checks if a license ID is in local permanent blacklist."""
    try:
        ensure_license_tables()
        conn = get_connection()
        is_mysql = is_mysql_conn(conn)
        conn.close()
        row = query_one("SELECT license_id FROM wisp_revoked_licenses WHERE license_id = %s" if is_mysql else "SELECT license_id FROM wisp_revoked_licenses WHERE license_id = ?", (lic_id,))
        return bool(row)
    except Exception:
        return False

def record_license_revocation(lic_id, reason="Revoked by master license server"):
    """Records a license ID into the persistent local revocation blacklist."""
    try:
        ensure_license_tables()
        conn = get_connection()
        is_mysql = is_mysql_conn(conn)
        conn.close()
        if is_mysql:
            execute_write("INSERT INTO wisp_revoked_licenses (license_id, reason) VALUES (%s, %s) ON DUPLICATE KEY UPDATE reason = VALUES(reason)", (lic_id, reason))
        else:
            execute_write("INSERT OR REPLACE INTO wisp_revoked_licenses (license_id, reason) VALUES (?, ?)", (lic_id, reason))
    except Exception as e:
        logger.warning(f"[License Guard] Failed to record revocation: {e}")

def remove_license_revocation(lic_id):
    """Removes a license ID from local blacklist if reactivated."""
    try:
        conn = get_connection()
        is_mysql = is_mysql_conn(conn)
        conn.close()
        if is_mysql:
            execute_write("DELETE FROM wisp_revoked_licenses WHERE license_id = %s", (lic_id,))
        else:
            execute_write("DELETE FROM wisp_revoked_licenses WHERE license_id = ?", (lic_id,))
    except Exception as e:
        logger.warning(f"[License Guard] Failed to remove revocation: {e}")

def get_current_active_sessions_count():
    """
    Returns current active sessions count (Hotspot + PPPoE) based on:
    acctstoptime IS NULL AND (
        (acctupdatetime IS NOT NULL AND acctupdatetime >= NOW() - INTERVAL 5 MINUTE)
        OR (acctupdatetime IS NULL AND acctstarttime >= NOW() - INTERVAL 5 MINUTE)
    )
    Total concurrent active sessions count >= 0.
    Returns -1 on DB failure (fail-closed, never assumes 0).
    """
    try:
        conn = get_connection()
        is_mysql = is_mysql_conn(conn)
        conn.close()
        
        if is_mysql:
            sql = """
            SELECT COUNT(*) AS total
            FROM radacct
            WHERE acctstoptime IS NULL
              AND (
                (acctupdatetime IS NOT NULL AND acctupdatetime >= NOW() - INTERVAL 5 MINUTE)
                OR (acctupdatetime IS NULL AND acctstarttime >= NOW() - INTERVAL 5 MINUTE)
              )
            """
            row = query_one(sql)
        else:
            cutoff = (datetime.datetime.now() - datetime.timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M:%S')
            sql = """
            SELECT COUNT(*) AS total
            FROM radacct
            WHERE acctstoptime IS NULL
              AND (
                (acctupdatetime IS NOT NULL AND acctupdatetime >= ?)
                OR (acctupdatetime IS NULL AND acctstarttime >= ?)
              )
            """
            row = query_one(sql, (cutoff, cutoff))
            
        if row is not None and 'total' in row:
            return int(row['total'])
        return -1
    except Exception as e:
        logger.error(f"[License Guard] Error reading active sessions count: {e}")
        return -1

def get_pending_reservations_count():
    """
    Returns count of active pending authorization reservations before accounting starts.
    Pending reservations expire after five minutes unless accounting links them.
    Returns -1 on a read failure; zero is a successful empty count.
    """
    try:
        conn = get_connection()
        is_mysql = is_mysql_conn(conn)
        conn.close()
        
        if is_mysql:
            from database.license_capacity import PENDING_RESERVATION_PREDICATE
            sql = "SELECT COUNT(*) AS total FROM wisp_session_reservations WHERE " + PENDING_RESERVATION_PREDICATE
            row = query_one(sql)
        else:
            now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            sql = "SELECT COUNT(*) AS total FROM wisp_session_reservations WHERE expires_at > ? AND radacctid IS NULL"
            row = query_one(sql, (now_str,))
            
        if row is not None and 'total' in row:
            return int(row['total'])
        return -1
    except Exception as e:
        logger.warning(f"[License Guard] Error reading pending reservations count: {e}")
        return -1

def get_current_system_counts():
    """Returns active subscribers count, NAS devices count, and active sessions count."""
    try:
        v_count = query_one("SELECT COUNT(*) as c FROM wisp_vouchers WHERE status != 'expired'")
        s_count = query_one("SELECT COUNT(*) as c FROM wisp_subscribers WHERE status = 'active'")
        nas_count = query_one("SELECT COUNT(*) as c FROM wisp_nas_devices")
        
        subs_total = (v_count.get('c', 0) if v_count else 0) + (s_count.get('c', 0) if s_count else 0)
        nas_total = nas_count.get('c', 0) if nas_count else 0
        active_sess = get_current_active_sessions_count()
        return subs_total, nas_total, active_sess
    except Exception as e:
        logger.error(f"[License Guard] Error fetching system counts: {e}")
        return 0, 0, -1


def get_consumed_license_seats():
    try:
        from database.license_capacity import CAPACITY_COUNT_SQL
        row = query_one(CAPACITY_COUNT_SQL)
        return int(row['total']) if row else -1
    except Exception:
        return -1

def check_and_update_monotonic_time():
    """
    Guards against system clock rollback (Anti-Clock-Rollback).
    If current server time is behind the last recorded time by > 300 seconds,
    it flags a clock rollback tampering attempt.
    """
    now_epoch = time.time()
    try:
        row = query_one("SELECT `value` FROM wisp_system_settings WHERE `key` = 'license_monotonic_timestamp'")
        if row and row.get('value'):
            try:
                last_epoch = float(row['value'])
                if now_epoch < (last_epoch - 300):
                    logger.error(f"[License Guard] Clock rollback detected! Current: {now_epoch}, Last: {last_epoch}")
                    return False, "تم اكتشاف تراجع في ساعة وتاريخ السيرفر (Clock Rollback). تم قفل النظام لحماية الترخيص."
                if now_epoch > last_epoch:
                    try:
                        execute_write("UPDATE wisp_system_settings SET `value` = ? WHERE `key` = 'license_monotonic_timestamp'", (str(now_epoch),))
                    except Exception:
                        execute_write("UPDATE wisp_system_settings SET `value` = %s WHERE `key` = 'license_monotonic_timestamp'", (str(now_epoch),))
            except (ValueError, TypeError):
                return False, 'سجل حماية وقت الترخيص غير صالح.'
        else:
            try:
                execute_write("INSERT INTO wisp_system_settings (`key`, `value`) VALUES (?, ?)", ('license_monotonic_timestamp', str(now_epoch)))
            except Exception:
                execute_write("INSERT INTO wisp_system_settings (`key`, `value`) VALUES (%s, %s)", ('license_monotonic_timestamp', str(now_epoch)))
    except Exception as e:
        logger.warning(f"[License Guard] Monotonic check notice: {e}")
        return False, 'تعذر التحقق من سجل وقت الترخيص.'
    return True, ""

def _build_default_license_dict(status="unlicensed", status_text="", message="", valid=False, subs_count=0, nas_count=0, active_sessions_count=0):
    active_err = (active_sessions_count < 0)
    current_active = None if active_err else max(0, active_sessions_count)
    return {
        "has_license": status not in ("unlicensed", "error"),
        "status": status,
        "status_text": status_text or "النظام مقفل: بانتظار إدخال وتفعيل الترخيص الرسمي 🔴",
        "license_id": "",
        "client_name": "غير مرخص (Unlicensed)",
        "plan_tier": "Unlicensed",
        "days_left": 0,
        "expires_at": "غير مفعل",
        "is_lifetime": False,
        "license_mode": "active_sessions",
        "max_active_sessions": 0,
        "current_active_sessions": current_active,
        "active_sessions_read_error": active_err,
        "pending_reservations": 0,
        "total_consumed_capacity": 0 if current_active is None else current_active,
        "available_capacity": 0,
        "capacity_usage_pct": 0.0,
        "is_legacy_license": False,
        "max_subscribers": 0,
        "max_nas": 0,
        "max_managers": 0,
        "current_subscribers": subs_count,
        "current_nas": nas_count,
        "current_machine_id": get_machine_id(),
        "current_instance_uuid": get_instance_uuid(),
        "combined_binding_code": get_combined_binding_code(),
        "licensed_machine_id": "NONE",
        "features": {
            "user_portal": False,
            "automation_rules": False,
            "gis_map": False,
            "autoheal": False,
            "accounting_archiver": False,
            "radius_simulator": False,
            "traffic_analytics": False,
            "api_access": False,
            "white_label": False,
            "vpn_tunnels": False,
            "whatsapp_gateway": False,
            "multi_router_sync": False,
            "loyalty_rewards": False,
            "card_designer": False
        },
        "valid": bool(valid),
        "is_over_quota": False,
        "quota_warning": "",
        "message": message or "النظام مقفل بالكامل وغير مرخص.",
        "grace_period": False,
        "grace_hours_left": 0,
        "last_verified_at": "",
        "last_heartbeat_at": "",
        "master_server_url": DEFAULT_MASTER_SERVER_URL
    }

def get_active_license_status(force_refresh=False):
    """
    Returns verified live status object of the system license.
    Cached in memory for 60 seconds to avoid repeating DB queries and crypto verification on every HTTP request.
    """
    global _LICENSE_CACHE
    now_t = time.time()
    if not force_refresh and _LICENSE_CACHE['data'] is not None:
        if (now_t - _LICENSE_CACHE['timestamp']) < 60:
            return _LICENSE_CACHE['data']

    ensure_license_tables()
    current_machine_id = get_machine_id()
    subs_count, nas_count, active_sessions_count = get_current_system_counts()
    
    # 1. Anti-Clock Rollback check
    clock_ok, clock_msg = check_and_update_monotonic_time()
    if not clock_ok:
        res = _build_default_license_dict(
            status="clock_tampered",
            status_text="تم قفل النظام: تلاعب في ساعة السيرفر 🔴",
            message=clock_msg,
            valid=False,
            subs_count=subs_count,
            nas_count=nas_count,
            active_sessions_count=active_sessions_count
        )
        res["has_license"] = True
        res["client_name"] = "مقفل أمنياً"
        res["plan_tier"] = "Locked"
        res["licensed_machine_id"] = "LOCKED"
        _LICENSE_CACHE = {'data': res, 'timestamp': now_t}
        return res

    row = query_one("SELECT * FROM wisp_license_info ORDER BY id DESC LIMIT 1")
    if not row:
        res = _build_default_license_dict(
            status="unlicensed",
            status_text="النظام مقفل: بانتظار إدخال وتفعيل الترخيص الرسمي 🔴",
            message="النظام مقفل بالكامل وغير مرخص. يرجى تزويد المطور ببصمة الجهاز وتفعيل مفتاح الترخيص الرسمي للبدء.",
            valid=False,
            subs_count=subs_count,
            nas_count=nas_count,
            active_sessions_count=active_sessions_count
        )
        _LICENSE_CACHE = {'data': res, 'timestamp': now_t}
        return res
        
    try:
        raw_pkg = json.loads(row['raw_package_json'])
        valid, msg, info = verify_license_package(
            raw_pkg,
            current_active_sessions=max(0, active_sessions_count),
            current_nas=nas_count,
            current_subscribers=subs_count
        )
        
        features = info.get('features', {})

        # Check database status override (e.g. if revoked by heartbeat or grace expired)
        db_status = row.get('status', 'active')
        grace_period_active = False
        grace_hours_left = 24
        
        # Check local permanent blacklist
        if is_license_blacklisted(row['license_id']):
            valid = False
            db_status = 'revoked'
            msg = "تم حظر وإلغاء هذا الترخيص عن بُعد من قِبل إدارة المطور 🔴"
        elif db_status == 'revoked':
            valid = False
            msg = "تم حظر وإلغاء هذا الترخيص عن بُعد من قِبل إدارة المطور 🔴"
        elif db_status == 'suspended':
            valid = False
            msg = "تم تعليق هذا الترخيص مؤقتاً من قِبل إدارة المطور ⏸️"
        elif db_status == 'grace_expired':
            valid = False
            msg = "انتهت فترة السماح (24 ساعة) دون التمكن من مزامنة الترخيص مع السيرفر المركزي. يرجى التأكد من اتصال الإنترنت."
        else:
            # Calculate offline duration
            try:
                last_sync = row.get('last_heartbeat_at') or row.get('last_verified_at') or row.get('created_at')
                if not last_sync:
                    raise ValueError('Missing license verification timestamp')
                lh_dt = last_sync if isinstance(last_sync, datetime.datetime) else datetime.datetime.fromisoformat(str(last_sync).replace('Z', '+00:00'))
                if lh_dt.tzinfo is None:
                    # SQL sessions store their wall clock at the fixed +03:00 database offset.
                    lh_dt = lh_dt.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=3)))
                elapsed_sec = (datetime.datetime.now(datetime.timezone.utc) - lh_dt).total_seconds()
                if elapsed_sec > 86400: # Over 24 hours without successful sync
                    valid = False
                    db_status = 'grace_expired'
                    msg = "انتهت فترة السماح (24 ساعة) دون التمكن من مزامنة الترخيص مع السيرفر المركزي."
                elif elapsed_sec > 3600: # More than 1 hour offline -> grace notice
                    grace_period_active = True
                    grace_hours_left = max(1, int((86400 - elapsed_sec) / 3600))
            except Exception:
                valid = False
                db_status = 'grace_expired'
                msg = 'تعذر التحقق من وقت آخر مزامنة للترخيص.'
            
        # Determine license mode ('active_sessions' vs 'legacy_subscribers')
        lic_mode = info.get('license_mode', 'active_sessions')
        max_active_sessions = info.get('max_active_sessions', row.get('max_active_sessions', 0))
        max_s = info.get('max_subscribers', row.get('max_subscribers', 0))
        max_n = info.get('max_nas', row.get('max_nas', 0))
        is_over_quota = False
        quota_warning = ""

        active_err = (active_sessions_count < 0)
        current_active = None if active_err else max(0, active_sessions_count)
        pending_reservations = get_pending_reservations_count()
        actual_active = max(0, active_sessions_count) if not active_err else 0
        consumed_seats = get_consumed_license_seats()
        capacity_err = active_err or pending_reservations < 0 or consumed_seats < 0
        total_consumed = None if capacity_err else consumed_seats
        pending_reservations = -1 if capacity_err else max(0, consumed_seats - actual_active)

        max_active_int = int(max_active_sessions or 0)
        if capacity_err:
            available_capacity = None
            capacity_usage_pct = None
        elif max_active_int > 0:
            available_capacity = max(0, max_active_int - total_consumed)
            capacity_usage_pct = round((total_consumed / max_active_int) * 100, 1)
        else:
            available_capacity = None
            capacity_usage_pct = 0.0

        is_legacy = (lic_mode == 'legacy_subscribers')

        # Fail-closed check: if counter query failed in active_sessions mode
        if lic_mode == 'active_sessions' and capacity_err:
            msg = "تعذر قراءة عداد الجلسات النشطة من قاعدة البيانات. تم إيقاف قبول الاتصالات الجديدة أمنياً."

        # Quota checks
        if lic_mode == 'active_sessions':
            if not capacity_err and max_active_int > 0 and total_consumed >= max_active_int:
                is_over_quota = True
                quota_warning = f"تم بلوغ الحد الأقصى للجلسات المتصلة المسموح بها في ترخيص النظام: ({actual_active:,} متصلة + {pending_reservations} معلقة / {max_active_int:,} سعة الترخيص)"
            elif max_n and int(max_n) > 0 and nas_count > int(max_n):
                is_over_quota = True
                quota_warning = f"تجاوز سقف أجهزة الراوتر: ({nas_count:,} / {int(max_n):,})"
        else:
            if max_n and int(max_n) > 0 and nas_count > int(max_n):
                is_over_quota = True
                quota_warning = f"تجاوز سقف أجهزة الراوتر: ({nas_count:,} / {int(max_n):,})"

        if not valid:
            status_code = db_status
            if db_status == 'revoked':
                status_text = "الترخيص محظور وملغى 🔴"
            elif db_status == 'suspended':
                status_text = "الترخيص معلق مؤقتاً ⏸️"
            elif db_status == 'grace_expired':
                status_text = "فترة السماح منتهية 🔴"
            else:
                status_text = "الترخيص منتهي أو غير صالح 🔴"
        else:
            if is_over_quota:
                status_code = 'over_quota'
                if lic_mode == 'active_sessions':
                    status_text = f"مرخص (بلغ سقف الجلسات: {actual_active:,}/{max_active_int:,} - حظر الاتصالات الجديدة فقط) ⚠️"
                else:
                    status_text = f"مرخص (تنبيه: يتطلب تحديث باقة الترخيص القديمة) ⚠️"
            elif grace_period_active:
                status_code = 'active'
                status_text = f"مرخص ومفعل (فترة سماح متبقية: {grace_hours_left} ساعة) 🟡"
            else:
                status_code = 'active'
                if is_legacy:
                    status_text = "مرخص (باقة ترخيص قديمة - يرجى التحديث) ⚠️"
                else:
                    status_text = "مرخص ومفعل بالكامل 🟢"
        
        res = {
            "has_license": True,
            "status": status_code,
            "status_text": status_text,
            "license_id": row['license_id'],
            "client_name": row['client_name'],
            "plan_tier": row['plan_tier'],
            "days_left": info.get('days_left', 0),
            "expires_at": row['expires_at'],
            "is_lifetime": info.get('is_lifetime', False),
            "license_mode": lic_mode,
            "max_active_sessions": max_active_int,
            "current_active_sessions": current_active,
            "active_sessions_read_error": active_err,
            "pending_reservations": None if pending_reservations < 0 else pending_reservations,
            "capacity_read_error": capacity_err,
            "is_unlimited_capacity": lic_mode == "active_sessions" and max_active_int == 0,
            "total_consumed_capacity": total_consumed,
            "available_capacity": available_capacity,
            "capacity_usage_pct": capacity_usage_pct,
            "is_legacy_license": is_legacy,
            "max_subscribers": info.get('max_subscribers', row.get('max_subscribers', 0)),
            "max_nas": info.get('max_nas', row.get('max_nas', 0)),
            "max_managers": info.get('max_managers', 10),
            "current_subscribers": subs_count,
            "current_nas": nas_count,
            "current_machine_id": current_machine_id,
            "current_instance_uuid": get_instance_uuid(),
            "combined_binding_code": get_combined_binding_code(),
            "licensed_machine_id": row['hardware_id'],
            "features": features,
            "valid": valid,
            "is_over_quota": is_over_quota,
            "quota_warning": quota_warning,
            "message": msg if not valid else (quota_warning or msg),
            "grace_period": grace_period_active,
            "grace_hours_left": grace_hours_left,
            "last_verified_at": str(row.get('last_verified_at', '')),
            "last_heartbeat_at": str(row.get('last_heartbeat_at', '')),
            "master_server_url": row.get('master_server_url', DEFAULT_MASTER_SERVER_URL)
        }
        _LICENSE_CACHE = {'data': res, 'timestamp': now_t}
        return res
    except Exception as e:
        logger.error(f"Error parsing active license: {e}")
        res = _build_default_license_dict(
            status="error",
            status_text="خطأ في قراءة الترخيص 🔴",
            message=f"حدث خطأ أثناء فحص الترخيص: {str(e)}",
            valid=False,
            subs_count=subs_count if 'subs_count' in locals() else 0,
            nas_count=nas_count if 'nas_count' in locals() else 0,
            active_sessions_count=active_sessions_count if 'active_sessions_count' in locals() else 0
        )
        res["has_license"] = True
        _LICENSE_CACHE = {'data': res, 'timestamp': now_t}
        return res

def has_license_feature(feature_key, default_if_missing=True):
    """
    Checks whether a specific feature is enabled in the active verified license.
    Returns False if system is unlicensed, invalid, or the feature is explicitly set to False.
    """
    lic = get_active_license_status()
    if not lic or not lic.get('valid'):
        return False
    features = lic.get('features')
    if isinstance(features, dict) and feature_key in features:
        return bool(features[feature_key])
    return default_if_missing

def check_active_session_quota(additional_count=1):
    """
    Checks if accepting new active sessions exceeds the license quota.
    Enforces fail-closed: if counter fails or license invalid, returns False.
    Returns (allowed: bool, err_msg: str, current_count: int, max_limit: int).
    """
    status = get_active_license_status()
    if not status.get('valid'):
        return False, status.get('message', "النظام مقفل وغير مرخص. لا يمكن قبول اتصالات جديدة."), status.get('current_active_sessions', 0), 0
    if status.get('status') in ('revoked', 'suspended', 'grace_expired', 'clock_tampered'):
        return False, f"حالة الترخيص غير صالحة ({status.get('status')}). لا يمكن قبول اتصالات جديدة.", status.get('current_active_sessions', 0), 0
        
    lic_mode = status.get('license_mode', 'active_sessions')
    if lic_mode == 'active_sessions':
        max_sessions = status.get('max_active_sessions', 0)
        curr_sessions = status.get('total_consumed_capacity')
        if status.get('capacity_read_error') or curr_sessions is None or curr_sessions < 0:
            return False, "فشل في قراءة عداد الجلسات النشطة. تم إيقاف قبول الاتصالات أمنياً.", curr_sessions, max_sessions
        if max_sessions and max_sessions > 0:
            if (curr_sessions + additional_count) > max_sessions:
                return False, f"تم بلوغ الحد الأقصى للجلسات المتصلة المسموح بها في ترخيص النظام ({max_sessions:,}). الجلسات الحالية: {curr_sessions:,}", curr_sessions, max_sessions
        return True, "", curr_sessions, max_sessions
    else:
        # Legacy mode: concurrent sessions not bounded, but check validity
        return True, "", status.get('current_active_sessions', 0), 0

def check_subscriber_quota(additional_count=1):
    """Compatibility API: inventory is unrestricted; license must remain valid."""
    status = get_active_license_status()
    return (bool(status.get('valid')), '' if status.get('valid') else status.get('message', 'License required'), status.get('current_subscribers', 0), 0)

def check_nas_quota(additional_count=1):
    """
    Checks if adding new NAS routers exceeds the license quota.
    Returns (allowed: bool, err_msg: str, current_count: int, max_limit: int).
    """
    status = get_active_license_status(force_refresh=True)
    if not status.get('valid'):
        return False, status.get('message', "النظام مقفل وغير مرخص. لا يمكن إضافة أجهزة راوتر جديدة."), status.get('current_nas', 0), 0
    if status.get('status') == 'revoked':
        return False, "الترخيص محظور من قِبل المطور. لا يمكن إضافة أجهزة بث جديدة.", status.get('current_nas', 0), status.get('max_nas', 0)
        
    max_nas = status.get('max_nas', 15)
    if max_nas and max_nas > 0:
        current_nas = status.get('current_nas', 0)
        if (current_nas + additional_count) > max_nas:
            return False, f"تم تجاوز الحد الأقصى لأجهزة الراوتر (NAS) المسموح بها في باقة ترخيصك ({max_nas:,}). الأجهزة الحالية: {current_nas:,}", current_nas, max_nas
            
    return True, "", status.get('current_nas', 0), max_nas

def check_manager_quota(additional_count=1):
    """
    Checks if creating new manager accounts exceeds the license quota.
    Returns (allowed: bool, err_msg: str, current_count: int, max_limit: int).
    """
    status = get_active_license_status()
    if not status.get('valid'):
        return False, status.get('message', "النظام مقفل وغير مرخص. لا يمكن إنشاء حسابات مدراء جديدة."), 0, 0
    if status.get('status') == 'revoked':
        return False, "الترخيص محظور من قِبل المطور. لا يمكن إضافة حسابات مدراء جديدة.", 0, 0
        
    max_managers = int(status.get('max_managers', 10))
    
    try:
        mgr_row = query_one("SELECT COUNT(*) as c FROM wisp_managers")
        if not mgr_row or 'c' not in mgr_row:
            raise RuntimeError('Manager count is unavailable')
        current_managers = int(mgr_row['c'])
    except Exception:
        return False, 'تعذر قراءة عدد المدراء؛ تم منع الإضافة.', None, max_managers
        
    if max_managers and max_managers > 0:
        if (current_managers + additional_count) > max_managers:
            return False, f"تم تجاوز الحد الأقصى لحسابات المدراء والموزعين المسموح بها في باقة ترخيصك ({max_managers:,}). الحسابات الحالية: {current_managers:,}", current_managers, max_managers
            
    return True, "", current_managers, max_managers

def install_and_activate_license(license_str, master_server_url=DEFAULT_MASTER_SERVER_URL):
    """
    Installs, validates and activates a new license string or JSON payload into DB.
    Performs mandatory synchronous live verification with Master Server.
    """
    ensure_license_tables()
    clear_license_cache()
    
    pkg_dict, err = decode_license_string(license_str)
    if err:
        return False, err
        
    subs_count, nas_count, active_sess_count = get_current_system_counts()
    valid, msg, info = verify_license_package(
        pkg_dict,
        current_active_sessions=max(0, active_sess_count),
        current_nas=nas_count,
        current_subscribers=subs_count
    )
    if not valid:
        return False, msg
        
    payload = pkg_dict['payload']
    lic_id = payload.get('license_id')
    client_name = payload.get('client_name')
    plan_tier = payload.get('plan_tier', 'Enterprise')
    hw_id = payload.get('hardware_id', 'ANY')
    limits = payload.get('limits', {})
    max_subs = limits.get('max_subscribers', 5000)
    max_active = limits.get('max_active_sessions', limits.get('max_concurrent_sessions', 0))
    max_nas = limits.get('max_nas', 15)
    max_managers = limits.get('max_managers', 10)
    features = payload.get('features', {})
    current_machine_id = get_machine_id()
    
    # 1. Live Synchronous Verification with Master License Server
    endpoint = f"{master_server_url.rstrip('/')}/api/v1/heartbeat/ping"
    verify_payload = {
        "license_id": lic_id,
        "hardware_id": current_machine_id,
        "instance_uuid": get_instance_uuid(),
        "app_version": f"v{APP_VERSION}-{APP_EDITION}",
        "subscribers_count": subs_count,
        "nas_count": nas_count,
        "current_active_sessions": active_sess_count if active_sess_count >= 0 else None,
        "active_sessions_count": active_sess_count if active_sess_count >= 0 else None,
        "max_active_sessions": max_active,
        "license_mode": info.get('license_mode', 'unknown'),
        "system_uptime": "Activation"
    }
    
    try:
        res_json = authenticated_license_exchange(master_server_url, verify_payload, STORAGE_DIR, get_master_public_key_pem())
        if res_json.get('status') == 'instance_limit':
            return False, 'تم بلوغ الحد الأقصى للنسخ المسموح بها في هذا الترخيص.'
        
        # If server explicitly revoked or invalidated -> reject immediately & blacklist
        if res_json.get('status') in ('revoked', 'suspended', 'blacklisted', 'hwid_mismatch', 'unlicensed') or res_json.get('valid') is False:
            record_license_revocation(lic_id, "Revoked on master license server during activation")
            try:
                conn = get_connection()
                is_mysql = is_mysql_conn(conn)
                conn.close()
                execute_write("UPDATE wisp_license_info SET status = 'revoked' WHERE license_id = %s" if is_mysql else "UPDATE wisp_license_info SET status = 'revoked' WHERE license_id = ?", (lic_id,))
            except Exception:
                pass
            clear_license_cache()
            return False, "فشل التفعيل: هذا الترخيص محظور وملغى من قِبل إدارة المطور في السيرفر المركزي 🔴"
        else:
            # Master server approved (Active) -> remove from blacklist if previously revoked
            remove_license_revocation(lic_id)
    except Exception as ex:
        logger.warning(f"[License Guard] Live activation ping notice: {ex}")
        return False, 'تعذر التفعيل الآمن: تحقق من HTTPS واعتماد مفتاح اتصال النسخة في خادم التراخيص.'

    raw_json = json.dumps(pkg_dict, ensure_ascii=False)
    sig_b64 = pkg_dict.get('signature', '')
    
    # Save into wisp_license_info
    conn = get_connection()
    is_mysql = is_mysql_conn(conn)
    conn.close()
    
    # Maintain single active license record in wisp_license_info
    execute_write("DELETE FROM wisp_license_info WHERE license_id != %s" if is_mysql else "DELETE FROM wisp_license_info WHERE license_id != ?", (lic_id,))
    existing = query_one("SELECT id FROM wisp_license_info WHERE license_id = %s" if is_mysql else "SELECT id FROM wisp_license_info WHERE license_id = ?", (lic_id,))
    if existing:
        if is_mysql:
            execute_write("""
            UPDATE wisp_license_info SET
                client_name = %s, plan_tier = %s, hardware_id = %s,
                max_subscribers = %s, max_active_sessions = %s, max_nas = %s, max_managers = %s,
                features_json = %s, raw_package_json = %s, signature_b64 = %s,
                issued_at = %s, expires_at = %s, status = 'active',
                last_verified_at = CURRENT_TIMESTAMP, last_heartbeat_at = CURRENT_TIMESTAMP,
                master_server_url = %s
            WHERE id = %s
            """, (
                client_name, plan_tier, hw_id, max_subs, max_active, max_nas, max_managers,
                json.dumps(features), raw_json, sig_b64,
                payload.get('issued_at'), payload.get('expires_at'),
                master_server_url, existing['id']
            ))
        else:
            execute_write("""
            UPDATE wisp_license_info SET
                client_name = ?, plan_tier = ?, hardware_id = ?,
                max_subscribers = ?, max_active_sessions = ?, max_nas = ?, max_managers = ?,
                features_json = ?, raw_package_json = ?, signature_b64 = ?,
                issued_at = ?, expires_at = ?, status = 'active',
                last_verified_at = CURRENT_TIMESTAMP, last_heartbeat_at = CURRENT_TIMESTAMP,
                master_server_url = ?
            WHERE id = ?
            """, (
                client_name, plan_tier, hw_id, max_subs, max_active, max_nas, max_managers,
                json.dumps(features), raw_json, sig_b64,
                payload.get('issued_at'), payload.get('expires_at'),
                master_server_url, existing['id']
            ))
    else:
        if is_mysql:
            execute_write("""
            INSERT INTO wisp_license_info (
                license_id, client_name, plan_tier, hardware_id,
                max_subscribers, max_active_sessions, max_nas, max_managers, features_json,
                raw_package_json, signature_b64, issued_at, expires_at,
                status, last_heartbeat_at, master_server_url
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', CURRENT_TIMESTAMP, %s)
            """, (
                lic_id, client_name, plan_tier, hw_id, max_subs, max_active, max_nas, max_managers,
                json.dumps(features), raw_json, sig_b64,
                payload.get('issued_at'), payload.get('expires_at'), master_server_url
            ))
        else:
            execute_write("""
            INSERT INTO wisp_license_info (
                license_id, client_name, plan_tier, hardware_id,
                max_subscribers, max_active_sessions, max_nas, max_managers, features_json,
                raw_package_json, signature_b64, issued_at, expires_at,
                status, last_heartbeat_at, master_server_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', CURRENT_TIMESTAMP, ?)
            """, (
                lic_id, client_name, plan_tier, hw_id, max_subs, max_active, max_nas, max_managers,
                json.dumps(features), raw_json, sig_b64,
                payload.get('issued_at'), payload.get('expires_at'), master_server_url
            ))
        
    clear_license_cache()
    return True, f"تم تفعيل وتوثيق الترخيص الرقمي ({lic_id}) بنجاح!"


def _apply_verified_package_to_db(pkg_dict, master_server_url=DEFAULT_MASTER_SERVER_URL):
    """
    Applies and saves an already verified license package payload into local wisp_license_info.
    """
    payload = pkg_dict['payload']
    lic_id = payload.get('license_id')
    client_name = payload.get('client_name')
    plan_tier = payload.get('plan_tier', 'Enterprise')
    hw_id = payload.get('hardware_id', 'ANY')
    limits = payload.get('limits', {})
    max_subs = limits.get('max_subscribers', 5000)
    max_active = limits.get('max_active_sessions', limits.get('max_concurrent_sessions', 0))
    max_nas = limits.get('max_nas', 15)
    max_managers = limits.get('max_managers', 10)
    features = payload.get('features', {})
    raw_json = json.dumps(pkg_dict, ensure_ascii=False)
    sig_b64 = pkg_dict.get('signature', '')
    
    conn = get_connection()
    is_mysql = is_mysql_conn(conn)
    conn.close()
    
    existing = query_one("SELECT id FROM wisp_license_info WHERE license_id = %s" if is_mysql else "SELECT id FROM wisp_license_info WHERE license_id = ?", (lic_id,))
    if existing:
        if is_mysql:
            execute_write("""
            UPDATE wisp_license_info SET
                client_name = %s, plan_tier = %s, hardware_id = %s,
                max_subscribers = %s, max_active_sessions = %s, max_nas = %s, max_managers = %s,
                features_json = %s, raw_package_json = %s, signature_b64 = %s,
                issued_at = %s, expires_at = %s, status = 'active',
                last_verified_at = CURRENT_TIMESTAMP, last_heartbeat_at = CURRENT_TIMESTAMP,
                master_server_url = %s
            WHERE id = %s
            """, (
                client_name, plan_tier, hw_id, max_subs, max_active, max_nas, max_managers,
                json.dumps(features), raw_json, sig_b64,
                payload.get('issued_at'), payload.get('expires_at'),
                master_server_url, existing['id']
            ))
        else:
            execute_write("""
            UPDATE wisp_license_info SET
                client_name = ?, plan_tier = ?, hardware_id = ?,
                max_subscribers = ?, max_active_sessions = ?, max_nas = ?, max_managers = ?,
                features_json = ?, raw_package_json = ?, signature_b64 = ?,
                issued_at = ?, expires_at = ?, status = 'active',
                last_verified_at = CURRENT_TIMESTAMP, last_heartbeat_at = CURRENT_TIMESTAMP,
                master_server_url = ?
            WHERE id = ?
            """, (
                client_name, plan_tier, hw_id, max_subs, max_active, max_nas, max_managers,
                json.dumps(features), raw_json, sig_b64,
                payload.get('issued_at'), payload.get('expires_at'),
                master_server_url, existing['id']
            ))
    else:
        if is_mysql:
            execute_write("""
            INSERT INTO wisp_license_info (
                license_id, client_name, plan_tier, hardware_id,
                max_subscribers, max_active_sessions, max_nas, max_managers, features_json,
                raw_package_json, signature_b64, issued_at, expires_at,
                status, last_heartbeat_at, master_server_url
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', CURRENT_TIMESTAMP, %s)
            """, (
                lic_id, client_name, plan_tier, hw_id, max_subs, max_active, max_nas, max_managers,
                json.dumps(features), raw_json, sig_b64,
                payload.get('issued_at'), payload.get('expires_at'), master_server_url
            ))
        else:
            execute_write("""
            INSERT INTO wisp_license_info (
                license_id, client_name, plan_tier, hardware_id,
                max_subscribers, max_active_sessions, max_nas, max_managers, features_json,
                raw_package_json, signature_b64, issued_at, expires_at,
                status, last_heartbeat_at, master_server_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', CURRENT_TIMESTAMP, ?)
            """, (
                lic_id, client_name, plan_tier, hw_id, max_subs, max_active, max_nas, max_managers,
                json.dumps(features), raw_json, sig_b64,
                payload.get('issued_at'), payload.get('expires_at'), master_server_url
            ))
    clear_license_cache()

def sync_license_heartbeat_with_server(max_retries=3):
    """
    Sends background heartbeat to master license server with automatic 3x retries.
    Enforces immediate remote revocation and manages the 24-hour grace period window.
    """
    lic = query_one("SELECT * FROM wisp_license_info ORDER BY id DESC LIMIT 1")
    server_url = (lic.get('master_server_url') if lic else None) or DEFAULT_MASTER_SERVER_URL
    endpoint = f"{server_url.rstrip('/')}/api/v1/heartbeat/ping"
    
    subs_count, nas_count, active_sess_count = get_current_system_counts()
    max_active = lic.get('max_active_sessions', 0) if lic else 0
    payload = {
        "license_id": lic['license_id'] if lic else "",
        "hardware_id": get_machine_id(),
        "instance_uuid": get_instance_uuid(),
        "app_version": f"v{APP_VERSION}-{APP_EDITION}",
        "subscribers_count": subs_count,
        "nas_count": nas_count,
        "current_active_sessions": active_sess_count if active_sess_count >= 0 else None,
        "active_sessions_count": active_sess_count if active_sess_count >= 0 else None,
        "max_active_sessions": max_active,
        "license_mode": ('active_sessions' if lic and 'max_active_sessions' in json.loads(lic['raw_package_json']).get('payload', {}).get('limits', {}) else 'legacy_subscribers'),
        "system_uptime": "Online"
    }
    
    conn = get_connection()
    is_mysql = is_mysql_conn(conn)
    conn.close()
    
    last_error = ""
    for attempt in range(1, max_retries + 1):
        try:
            res_json = authenticated_license_exchange(server_url, payload, STORAGE_DIR, get_master_public_key_pem())
            
            # If fresh instance with no license, check if master assigned one
            if not lic:
                remote_act = res_json.get('remote_action')
                remote_payload = res_json.get('remote_payload') or res_json.get('license_token')
                if remote_payload and res_json.get('valid'):
                    try:
                        pkg_dict, err = decode_license_string(remote_payload)
                        if not err and pkg_dict:
                            valid, msg, info = verify_license_package(
                                pkg_dict,
                                current_active_sessions=max(0, active_sess_count),
                                current_nas=nas_count,
                                current_subscribers=subs_count
                            )
                            if valid:
                                _apply_verified_package_to_db(pkg_dict, server_url)
                                logger.info(f"[License Guard] Initial Remote License assigned and activated OTA: {pkg_dict['payload'].get('license_id')}")
                                clear_license_cache()
                                return True, "تم تفعيل الترخيص المخصص لهذه النسخة عن بعد بنجاح"
                    except Exception as e_init:
                        logger.warning(f"[License Guard] Remote auto-activate notice: {e_init}")
                clear_license_cache()
                return False, "النسخة مكتشفة لدى السيرفر المركزي بانتظار الترخيص"

            # 1. Server explicitly revoked, suspended, or invalidated license -> Lock immediately
            remote_act = res_json.get('remote_action')
            srv_status = res_json.get('status')
            is_srv_valid = res_json.get('valid')

            if srv_status == 'instance_limit':
                execute_write("UPDATE wisp_license_info SET status = 'suspended' WHERE id = ?", (lic['id'],))
                clear_license_cache()
                return False, 'تم بلوغ سعة النسخ المرخصة؛ يلزم مراجعة إدارة الترخيص.'

            if srv_status in ('revoked', 'suspended', 'blacklisted', 'hwid_mismatch', 'unlicensed') or remote_act in ('suspend', 'revoke', 'unlicensed') or (is_srv_valid is False and srv_status != 'over_quota'):
                if remote_act == 'suspend' or srv_status == 'suspended':
                    execute_write("UPDATE wisp_license_info SET status = 'suspended' WHERE id = %s" if is_mysql else "UPDATE wisp_license_info SET status = 'suspended' WHERE id = ?", (lic['id'],))
                    clear_license_cache()
                    logger.warning(f"[License Guard] License {lic['license_id']} explicitly suspended by Master Server.")
                    return False, "تم تعليق هذا الترخيص مؤقتاً من قِبل إدارة المطور"
                else:
                    record_license_revocation(lic['license_id'], "Revoked or deleted by master license server")
                    execute_write("UPDATE wisp_license_info SET status = 'revoked' WHERE id = %s" if is_mysql else "UPDATE wisp_license_info SET status = 'revoked' WHERE id = ?", (lic['id'],))
                    clear_license_cache()
                    logger.warning(f"[License Guard] License {lic['license_id']} explicitly revoked/deleted by Master Server.")
                    return False, "تم إشعار النظام بحظر أو حذف الترخيص من قِبل المطور"
            
            # 2. Server confirmed active license -> Update heartbeat and clear grace
            remove_license_revocation(lic['license_id'])
            execute_write("UPDATE wisp_license_info SET last_heartbeat_at = CURRENT_TIMESTAMP, status = 'active' WHERE id = %s" if is_mysql else "UPDATE wisp_license_info SET last_heartbeat_at = CURRENT_TIMESTAMP, status = 'active' WHERE id = ?", (lic['id'],))
            
            # 3. Handle OTA License Updates (Quotas, Features, Validity, Plans)
            remote_act = res_json.get('remote_action')
            remote_payload = res_json.get('remote_payload') or res_json.get('license_token')
            if remote_payload:
                try:
                    pkg_dict, err = decode_license_string(remote_payload)
                    if not err and pkg_dict:
                        subs_c, nas_c, active_c = get_current_system_counts()
                        valid, msg, info = verify_license_package(pkg_dict, current_active_sessions=max(0, active_c), current_nas=nas_c, current_subscribers=subs_c)
                        if valid:
                            current_sig = lic.get('signature_b64', '')
                            new_sig = pkg_dict.get('signature', '')
                            if remote_act == 'sync_license' or (new_sig and new_sig != current_sig):
                                _apply_verified_package_to_db(pkg_dict, server_url)
                                logger.info(f"[License Guard] OTA License Update applied successfully for {lic['license_id']}")
                except Exception as e_ota:
                    logger.warning(f"[License Guard] Failed applying OTA license update: {e_ota}")
            
            # Record server time for anti-clock-rollback
            server_time_str = res_json.get('server_time')
            if server_time_str:
                try:
                    st_epoch = datetime.datetime.fromisoformat(server_time_str.replace('Z', '')).timestamp()
                    check_and_update_monotonic_time()
                except Exception:
                    pass
                    
            clear_license_cache()
            logger.info(f"[License Guard] Heartbeat synced successfully on attempt {attempt}.")
            return True, "تمت مزامنة نبضات الترخيص مع الخادم بنجاح"
        except Exception as e:
            last_error = str(e)
            logger.warning(f"[License Guard] Heartbeat attempt {attempt}/{max_retries} failed: {e}")
            if attempt < max_retries:
                time.sleep(3)
                
    # If all retries failed -> System is in Grace Period or Expired
    logger.warning(f"[License Guard] All {max_retries} heartbeat retries failed: {last_error}")
    return False, f"تعذر الوصول لخادم التراخيص بعد {max_retries} محاولات: {last_error}"

def start_license_heartbeat_daemon(interval_seconds=300):
    """Starts background heartbeat sync loop (Default: Every 5 minutes)."""
    import threading
    if any(t.name == 'LicenseHeartbeatWorker' and t.is_alive() for t in threading.enumerate()):
        return
    def _worker():
        # Run initial sync after 10 seconds of system boot
        time.sleep(10)
        while True:
            try:
                sync_license_heartbeat_with_server(max_retries=3)
            except Exception as e:
                logger.warning(f"[License Guard Daemon] Exception: {e}")
            time.sleep(interval_seconds)
            
    t = threading.Thread(target=_worker, daemon=True, name="LicenseHeartbeatWorker")
    t.start()



def publish_radius_license_state():
    """Publish a short verified lease, bound to the exact signed package bytes."""
    import hashlib
    lic = get_active_license_status(force_refresh=True)
    row = query_one("SELECT id, raw_package_json FROM wisp_license_info ORDER BY id DESC LIMIT 1")
    allowed = bool(row and lic.get('valid') and not lic.get('capacity_read_error'))
    lease_until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=90)
    if row:
        package = json.loads(row['raw_package_json'])
        valid, _, info = verify_license_package(package)
        allowed = allowed and valid
        expires = package['payload'].get('expires_at')
        if expires and not info.get('is_lifetime'):
            exp = datetime.datetime.fromisoformat(expires.replace('Z', '+00:00'))
            if exp.tzinfo is None: exp = exp.replace(tzinfo=datetime.timezone.utc)
            lease_until = min(lease_until, exp.astimezone(datetime.timezone.utc))
    else:
        info = {}
    # Pending/stale seats are not expired by a wall clock: only a confirmed Stop releases them.
    execute_write("""INSERT INTO wisp_license_runtime_state
        (id,license_row_id,package_hash,is_valid,license_mode,max_active_sessions,valid_until)
        VALUES(1,?,?,?,?,?,?) ON DUPLICATE KEY UPDATE
        license_row_id=VALUES(license_row_id),package_hash=VALUES(package_hash),
        is_valid=VALUES(is_valid),license_mode=VALUES(license_mode),
        max_active_sessions=VALUES(max_active_sessions),valid_until=VALUES(valid_until)""",
        (row['id'] if row else None,
         hashlib.sha256(row['raw_package_json'].encode('utf-8')).hexdigest() if row else None,
         int(allowed),info.get('license_mode','unknown'),info.get('max_active_sessions'),
         lease_until.replace(tzinfo=None)))


def start_radius_license_state_daemon():
    import threading
    if any(t.name == 'RadiusLicenseLeaseWorker' and t.is_alive() for t in threading.enumerate()):
        return
    def worker():
        ready = False
        while True:
            try:
                if not ready:
                    from database.schema_healer import heal_database_schema
                    if not heal_database_schema(backfill=False):
                        raise RuntimeError('Capacity schema is not ready')
                    ready = True
                publish_radius_license_state()
            except Exception as exc:
                logger.error('RADIUS license lease refresh failed: %s', exc)
            time.sleep(30)
    threading.Thread(target=worker, daemon=True, name='RadiusLicenseLeaseWorker').start()

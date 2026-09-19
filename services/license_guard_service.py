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
from pathlib import Path

from database.db import query_all, query_one, execute_write, get_connection, is_mysql_conn
from core.config import APP_VERSION, APP_EDITION
from core.hardware_fingerprint import get_machine_id
from core.licensing import decode_license_string, verify_license_package, get_master_public_key_pem

logger = logging.getLogger('license_guard_service')

DEFAULT_MASTER_SERVER_URL = os.environ.get("MASTER_LICENSE_SERVER_URL", "http://127.0.0.1:5095")

_LICENSE_CACHE = {
    'data': None,
    'timestamp': 0
}

def clear_license_cache():
    global _LICENSE_CACHE
    _LICENSE_CACHE = {'data': None, 'timestamp': 0}

def ensure_license_tables():
    """Ensures wisp_license_info and wisp_revoked_licenses tables exist."""
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
        else:
            sql = """
            CREATE TABLE IF NOT EXISTS `wisp_license_info` (
                `id` INTEGER PRIMARY KEY AUTOINCREMENT,
                `license_id` TEXT UNIQUE NOT NULL,
                `client_name` TEXT NOT NULL,
                `plan_tier` TEXT DEFAULT 'Enterprise',
                `hardware_id` TEXT DEFAULT 'ANY',
                `max_subscribers` INTEGER DEFAULT 5000,
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

def get_current_system_counts():
    """Returns active subscribers count and NAS devices count."""
    try:
        v_count = query_one("SELECT COUNT(*) as c FROM wisp_vouchers WHERE status != 'expired'")
        s_count = query_one("SELECT COUNT(*) as c FROM wisp_subscribers WHERE status = 'active'")
        nas_count = query_one("SELECT COUNT(*) as c FROM wisp_nas_devices")
        
        subs_total = (v_count.get('c', 0) if v_count else 0) + (s_count.get('c', 0) if s_count else 0)
        nas_total = nas_count.get('c', 0) if nas_count else 0
        return subs_total, nas_total
    except Exception:
        return 0, 0

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
                pass
        else:
            try:
                execute_write("INSERT INTO wisp_system_settings (`key`, `value`) VALUES (?, ?)", ('license_monotonic_timestamp', str(now_epoch)))
            except Exception:
                execute_write("INSERT INTO wisp_system_settings (`key`, `value`) VALUES (%s, %s)", ('license_monotonic_timestamp', str(now_epoch)))
    except Exception as e:
        logger.warning(f"[License Guard] Monotonic check notice: {e}")
    return True, ""

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
    subs_count, nas_count = get_current_system_counts()
    
    # 1. Anti-Clock Rollback check
    clock_ok, clock_msg = check_and_update_monotonic_time()
    if not clock_ok:
        res = {
            "has_license": True,
            "status": "clock_tampered",
            "status_text": "تم قفل النظام: تلاعب في ساعة السيرفر 🔴",
            "client_name": "مقفل أمنياً",
            "plan_tier": "Locked",
            "days_left": 0,
            "expires_at": "مقفل",
            "is_lifetime": False,
            "max_subscribers": 0,
            "max_nas": 0,
            "max_managers": 0,
            "current_subscribers": subs_count,
            "current_nas": nas_count,
            "current_machine_id": current_machine_id,
            "licensed_machine_id": "LOCKED",
            "features": {},
            "valid": False,
            "message": clock_msg
        }
        _LICENSE_CACHE = {'data': res, 'timestamp': now_t}
        return res

    row = query_one("SELECT * FROM wisp_license_info ORDER BY id DESC LIMIT 1")
    if not row:
        res = {
            "has_license": False,
            "status": "unlicensed",
            "status_text": "النظام مقفل: بانتظار إدخال وتفعيل الترخيص الرسمي 🔴",
            "client_name": "غير مرخص (Unlicensed)",
            "plan_tier": "Unlicensed",
            "days_left": 0,
            "expires_at": "غير مفعل",
            "is_lifetime": False,
            "max_subscribers": 0,
            "max_nas": 0,
            "max_managers": 0,
            "current_subscribers": subs_count,
            "current_nas": nas_count,
            "current_machine_id": current_machine_id,
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
                "white_label": False
            },
            "valid": False,
            "message": "النظام مقفل بالكامل وغير مرخص. يرجى تزويد المطور ببصمة الجهاز وتفعيل مفتاح الترخيص الرسمي للبدء."
        }
        _LICENSE_CACHE = {'data': res, 'timestamp': now_t}
        return res
        
    try:
        raw_pkg = json.loads(row['raw_package_json'])
        valid, msg, info = verify_license_package(raw_pkg, subs_count, nas_count)
        
        features = {}
        if row.get('features_json'):
            try:
                features = json.loads(row['features_json'])
            except Exception:
                pass
                
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
            msg = "تم حظر وإلغاء هذا الترخيص عن بُعد من قِبل إدارة المطور"
        elif db_status == 'grace_expired':
            valid = False
            msg = "انتهت فترة السماح (24 ساعة) دون التمكن من مزامنة الترخيص مع السيرفر المركزي. يرجى التأكد من اتصال الإنترنت."
        elif row.get('last_heartbeat_at'):
            # Calculate offline duration
            try:
                if isinstance(row['last_heartbeat_at'], datetime.datetime):
                    lh_dt = row['last_heartbeat_at']
                else:
                    lh_dt = datetime.datetime.fromisoformat(str(row['last_heartbeat_at']).replace('Z', ''))
                elapsed_sec = (datetime.datetime.now() - lh_dt).total_seconds()
                if elapsed_sec > 86400: # Over 24 hours without successful sync
                    valid = False
                    db_status = 'grace_expired'
                    msg = "انتهت فترة السماح (24 ساعة) دون التمكن من مزامنة الترخيص مع السيرفر المركزي."
                elif elapsed_sec > 3600: # More than 1 hour offline -> grace notice
                    grace_period_active = True
                    grace_hours_left = max(1, int((86400 - elapsed_sec) / 3600))
            except Exception:
                pass
            
        status_code = "active" if valid else db_status
        if valid:
            status_text = f"مرخص ومفعل (فترة سماح متبقية: {grace_hours_left} ساعة) 🟡" if grace_period_active else "مرخص ومفعل بالكامل 🟢"
        else:
            status_text = "الترخيص محظور 🔴" if db_status == 'revoked' else ("فترة السماح منتهية 🔴" if db_status == 'grace_expired' else "الترخيص منتهي أو غير صالح 🔴")
        
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
            "max_subscribers": row['max_subscribers'],
            "max_nas": row['max_nas'],
            "current_subscribers": subs_count,
            "current_nas": nas_count,
            "current_machine_id": current_machine_id,
            "licensed_machine_id": row['hardware_id'],
            "features": features,
            "valid": valid,
            "message": msg,
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
        res = {
            "has_license": True,
            "status": "error",
            "status_text": "خطأ في ملف الترخيص",
            "valid": False,
            "current_machine_id": current_machine_id,
            "message": str(e)
        }
        _LICENSE_CACHE = {'data': res, 'timestamp': now_t}
        return res

def check_subscriber_quota(additional_count=1):
    """
    Checks if adding new subscribers exceeds the license quota.
    Returns (allowed: bool, err_msg: str, current_count: int, max_limit: int).
    """
    status = get_active_license_status()
    if not status.get('valid'):
        return False, status.get('message', "النظام مقفل وغير مرخص. لا يمكن إضافة مشتركين جدد."), status.get('current_subscribers', 0), 0
    if status.get('status') == 'revoked':
        return False, "الترخيص محظور من قِبل المطور. لا يمكن إضافة مشتركين جدد.", status.get('current_subscribers', 0), status.get('max_subscribers', 0)
    
    max_subs = status.get('max_subscribers', 5000)
    if max_subs and max_subs > 0:
        current_subs = status.get('current_subscribers', 0)
        if (current_subs + additional_count) > max_subs:
            return False, f"تم تجاوز الحد الأقصى للمشتركين المسموح به في باقة ترخيصك ({max_subs:,}). المشتركين الحاليين: {current_subs:,}", current_subs, max_subs
            
    return True, "", status.get('current_subscribers', 0), max_subs

def check_nas_quota(additional_count=1):
    """
    Checks if adding new NAS routers exceeds the license quota.
    Returns (allowed: bool, err_msg: str, current_count: int, max_limit: int).
    """
    status = get_active_license_status()
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
        
    row = query_one("SELECT max_managers FROM wisp_license_info ORDER BY id DESC LIMIT 1")
    max_managers = (row.get('max_managers', 10) if row else 10) or 10
    
    try:
        mgr_row = query_one("SELECT COUNT(*) as c FROM wisp_managers")
        current_managers = mgr_row.get('c', 0) if mgr_row else 0
    except Exception:
        current_managers = 0
        
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
        
    subs_count, nas_count = get_current_system_counts()
    valid, msg, info = verify_license_package(pkg_dict, subs_count, nas_count)
    if not valid:
        return False, msg
        
    payload = pkg_dict['payload']
    lic_id = payload.get('license_id')
    client_name = payload.get('client_name')
    plan_tier = payload.get('plan_tier', 'Enterprise')
    hw_id = payload.get('hardware_id', 'ANY')
    limits = payload.get('limits', {})
    max_subs = limits.get('max_subscribers', 5000)
    max_nas = limits.get('max_nas', 15)
    max_managers = limits.get('max_managers', 10)
    features = payload.get('features', {})
    current_machine_id = get_machine_id()
    
    # 1. Live Synchronous Verification with Master License Server
    endpoint = f"{master_server_url.rstrip('/')}/api/v1/heartbeat/ping"
    verify_payload = {
        "license_id": lic_id,
        "hardware_id": current_machine_id,
        "app_version": f"v{APP_VERSION}-{APP_EDITION}",
        "subscribers_count": subs_count,
        "nas_count": nas_count,
        "system_uptime": "Activation"
    }
    
    try:
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(verify_payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=6) as response:
            res_json = json.loads(response.read().decode('utf-8'))
            
            # If server explicitly revoked or invalidated -> reject immediately & blacklist
            if res_json.get('status') == 'revoked' or res_json.get('valid') is False:
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
        # If server is offline / unreachable, fallback to local blacklist:
        if is_license_blacklisted(lic_id):
            return False, "فشل التفعيل: هذا الترخيص تم حظره مسبقاً ولا يمكن تفعيله Offline دون الاتصال بالسيرفر المركزي 🔴"

    raw_json = json.dumps(pkg_dict, ensure_ascii=False)
    sig_b64 = pkg_dict.get('signature', '')
    
    # Save into wisp_license_info
    conn = get_connection()
    is_mysql = is_mysql_conn(conn)
    conn.close()
    
    existing = query_one("SELECT id FROM wisp_license_info WHERE license_id = %s" if is_mysql else "SELECT id FROM wisp_license_info WHERE license_id = ?", (lic_id,))
    if existing:
        if is_mysql:
            execute_write("""
            UPDATE wisp_license_info SET
                client_name = %s, plan_tier = %s, hardware_id = %s,
                max_subscribers = %s, max_nas = %s, max_managers = %s,
                features_json = %s, raw_package_json = %s, signature_b64 = %s,
                issued_at = %s, expires_at = %s, status = 'active',
                last_verified_at = CURRENT_TIMESTAMP, last_heartbeat_at = CURRENT_TIMESTAMP,
                master_server_url = %s
            WHERE id = %s
            """, (
                client_name, plan_tier, hw_id, max_subs, max_nas, max_managers,
                json.dumps(features), raw_json, sig_b64,
                payload.get('issued_at'), payload.get('expires_at'),
                master_server_url, existing['id']
            ))
        else:
            execute_write("""
            UPDATE wisp_license_info SET
                client_name = ?, plan_tier = ?, hardware_id = ?,
                max_subscribers = ?, max_nas = ?, max_managers = ?,
                features_json = ?, raw_package_json = ?, signature_b64 = ?,
                issued_at = ?, expires_at = ?, status = 'active',
                last_verified_at = CURRENT_TIMESTAMP, last_heartbeat_at = CURRENT_TIMESTAMP,
                master_server_url = ?
            WHERE id = ?
            """, (
                client_name, plan_tier, hw_id, max_subs, max_nas, max_managers,
                json.dumps(features), raw_json, sig_b64,
                payload.get('issued_at'), payload.get('expires_at'),
                master_server_url, existing['id']
            ))
    else:
        if is_mysql:
            execute_write("""
            INSERT INTO wisp_license_info (
                license_id, client_name, plan_tier, hardware_id,
                max_subscribers, max_nas, max_managers, features_json,
                raw_package_json, signature_b64, issued_at, expires_at,
                status, last_heartbeat_at, master_server_url
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', CURRENT_TIMESTAMP, %s)
            """, (
                lic_id, client_name, plan_tier, hw_id, max_subs, max_nas, max_managers,
                json.dumps(features), raw_json, sig_b64,
                payload.get('issued_at'), payload.get('expires_at'), master_server_url
            ))
        else:
            execute_write("""
            INSERT INTO wisp_license_info (
                license_id, client_name, plan_tier, hardware_id,
                max_subscribers, max_nas, max_managers, features_json,
                raw_package_json, signature_b64, issued_at, expires_at,
                status, last_heartbeat_at, master_server_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', CURRENT_TIMESTAMP, ?)
            """, (
                lic_id, client_name, plan_tier, hw_id, max_subs, max_nas, max_managers,
                json.dumps(features), raw_json, sig_b64,
                payload.get('issued_at'), payload.get('expires_at'), master_server_url
            ))
        
    clear_license_cache()
    return True, f"تم تفعيل وتوثيق الترخيص الرقمي ({lic_id}) بنجاح!"

def sync_license_heartbeat_with_server(max_retries=3):
    """
    Sends background heartbeat to master license server with automatic 3x retries.
    Enforces immediate remote revocation and manages the 24-hour grace period window.
    """
    lic = query_one("SELECT * FROM wisp_license_info ORDER BY id DESC LIMIT 1")
    if not lic:
        return False, "لا يوجد ترخيص مسجل للاتصال"
        
    server_url = lic.get('master_server_url') or DEFAULT_MASTER_SERVER_URL
    endpoint = f"{server_url.rstrip('/')}/api/v1/heartbeat/ping"
    
    subs_count, nas_count = get_current_system_counts()
    payload = {
        "license_id": lic['license_id'],
        "hardware_id": get_machine_id(),
        "app_version": f"v{APP_VERSION}-{APP_EDITION}",
        "subscribers_count": subs_count,
        "nas_count": nas_count,
        "system_uptime": "Online"
    }
    
    conn = get_connection()
    is_mysql = is_mysql_conn(conn)
    conn.close()
    
    last_error = ""
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(
                endpoint,
                data=json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                res_json = json.loads(response.read().decode('utf-8'))
                
                # 1. Server explicitly revoked or invalidated license -> Lock immediately & Blacklist
                if res_json.get('status') == 'revoked' or res_json.get('valid') is False:
                    record_license_revocation(lic['license_id'], "Revoked by master license server during heartbeat")
                    execute_write("UPDATE wisp_license_info SET status = 'revoked' WHERE id = %s" if is_mysql else "UPDATE wisp_license_info SET status = 'revoked' WHERE id = ?", (lic['id'],))
                    clear_license_cache()
                    logger.warning(f"[License Guard] License {lic['license_id']} explicitly revoked by Master Server.")
                    return False, "تم إشعار النظام بأن هذا الترخيص محظور من قِبل المطور"
                
                # 2. Server confirmed active license -> Update heartbeat and clear grace
                remove_license_revocation(lic['license_id'])
                execute_write("UPDATE wisp_license_info SET last_heartbeat_at = CURRENT_TIMESTAMP, status = 'active' WHERE id = %s" if is_mysql else "UPDATE wisp_license_info SET last_heartbeat_at = CURRENT_TIMESTAMP, status = 'active' WHERE id = ?", (lic['id'],))
                
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


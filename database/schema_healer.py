# -*- coding: utf-8 -*-
"""
database/schema_healer.py
-------------------------
Enterprise Self-Healing Database Schema Engine for MAX RADIUS.
Automatically inspects, repairs, and backfills missing tables, columns, indexes,
and triggers whenever an old backup is restored or when new features are deployed.
"""

import sys
from database.db import get_connection, is_mysql_conn, adapt_query

# Schema Definition Registry
from database.maintenance_jobs import MAINTENANCE_JOBS_DDL

REQUIRED_TABLES = {
    'wisp_maintenance_jobs': MAINTENANCE_JOBS_DDL,
    'wisp_whatsapp_settings': """
        CREATE TABLE IF NOT EXISTS wisp_whatsapp_settings (
            id INT AUTO_INCREMENT PRIMARY KEY,
            gateway_provider VARCHAR(40) DEFAULT 'simulator',
            api_endpoint VARCHAR(255) DEFAULT 'http://localhost:8080',
            api_key VARCHAR(255) DEFAULT '',
            instance_name VARCHAR(80) DEFAULT 'max_radius_bot',
            phone_number VARCHAR(40) DEFAULT '',
            is_bot_enabled TINYINT(1) DEFAULT 1,
            is_notifications_enabled TINYINT(1) DEFAULT 1,
            meta_app_id VARCHAR(80) DEFAULT '',
            meta_phone_number_id VARCHAR(80) DEFAULT '',
            meta_access_token TEXT DEFAULT NULL,
            meta_webhook_verify_token VARCHAR(120) DEFAULT 'max_radius_whatsapp_token_2026',
            status VARCHAR(30) DEFAULT 'disconnected',
            qr_code_raw MEDIUMTEXT DEFAULT NULL,
            last_connected_at DATETIME DEFAULT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    'wisp_whatsapp_templates': """
        CREATE TABLE IF NOT EXISTS wisp_whatsapp_templates (
            id INT AUTO_INCREMENT PRIMARY KEY,
            template_key VARCHAR(60) NOT NULL UNIQUE,
            title VARCHAR(100) NOT NULL,
            category VARCHAR(40) DEFAULT 'notification',
            message_body TEXT NOT NULL,
            is_active TINYINT(1) DEFAULT 1,
            variables_hint VARCHAR(255) DEFAULT '',
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    'wisp_whatsapp_logs': """
        CREATE TABLE IF NOT EXISTS wisp_whatsapp_logs (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            recipient_phone VARCHAR(30) NOT NULL,
            message_type VARCHAR(30) DEFAULT 'text',
            direction ENUM('inbound', 'outbound') DEFAULT 'outbound',
            message_body TEXT NOT NULL,
            status ENUM('pending', 'sent', 'delivered', 'read', 'failed') DEFAULT 'sent',
            error_message TEXT DEFAULT NULL,
            entity_type VARCHAR(30) DEFAULT NULL,
            entity_id INT DEFAULT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_phone (recipient_phone),
            INDEX idx_status (status),
            INDEX idx_created (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    'wisp_global_sequence': """
        CREATE TABLE IF NOT EXISTS wisp_global_sequence (
            seq_id BIGINT AUTO_INCREMENT PRIMARY KEY,
            entity_type VARCHAR(20) NOT NULL,
            entity_id INT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_entity (entity_type, entity_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    'user_audit_logs': """
        CREATE TABLE IF NOT EXISTS user_audit_logs (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_type VARCHAR(20) NOT NULL,
            user_id INT DEFAULT 0,
            username VARCHAR(100) NOT NULL,
            admin_name VARCHAR(100) DEFAULT 'Admin',
            action VARCHAR(50) DEFAULT 'UPDATE_PROFILE',
            change_details TEXT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_user (user_type, user_id),
            INDEX idx_username (username)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    'wisp_voucher_sales': """
        CREATE TABLE IF NOT EXISTS wisp_voucher_sales (
            id INT AUTO_INCREMENT PRIMARY KEY,
            voucher_id INT NOT NULL,
            batch_id INT NULL,
            batch_name VARCHAR(100) NULL,
            username VARCHAR(100) NOT NULL,
            serial_number VARCHAR(100) NULL,
            package_name VARCHAR(100) NULL,
            price DECIMAL(10,2) DEFAULT 0.00,
            cost DECIMAL(10,2) DEFAULT 0.00,
            reseller_id INT NULL,
            activated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_voucher (voucher_id),
            INDEX idx_user (username)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    'wisp_sstp_tunnels': """
        CREATE TABLE IF NOT EXISTS wisp_sstp_tunnels (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(80) NOT NULL,
            username VARCHAR(64) NOT NULL UNIQUE,
            password VARCHAR(64) NOT NULL,
            tunnel_ip VARCHAR(45) NOT NULL UNIQUE,
            radius_secret VARCHAR(64) NOT NULL DEFAULT '123',
            reseller_id INT DEFAULT NULL,
            status VARCHAR(20) DEFAULT 'offline',
            is_enabled TINYINT(1) DEFAULT 1,
            latency_ms INT DEFAULT 0,
            last_connected_at DATETIME DEFAULT NULL,
            description TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_sstp_reseller (reseller_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    'wisp_session_baselines': """
        CREATE TABLE IF NOT EXISTS wisp_session_baselines (
            radacctid BIGINT NOT NULL,
            username VARCHAR(64) NOT NULL,
            baseline_input_bytes BIGINT UNSIGNED NOT NULL DEFAULT 0,
            baseline_output_bytes BIGINT UNSIGNED NOT NULL DEFAULT 0,
            baseline_bytes BIGINT UNSIGNED NOT NULL DEFAULT 0,
            baseline_seconds INT UNSIGNED NOT NULL DEFAULT 0,
            renewed_at DATETIME NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (radacctid, renewed_at),
            INDEX idx_username_renewed (username, renewed_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
    """,
    'wisp_session_reservations': """
        CREATE TABLE IF NOT EXISTS wisp_session_reservations (
            id INT AUTO_INCREMENT PRIMARY KEY,
            session_key VARCHAR(191) NOT NULL,
            username VARCHAR(64) NOT NULL,
            nasipaddress VARCHAR(45) NOT NULL,
            callingstationid VARCHAR(50) NOT NULL,
            reserved_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at DATETIME NOT NULL,
            UNIQUE KEY uk_session_key (session_key),
            INDEX idx_res_expires (expires_at),
            INDEX idx_res_nas (nasipaddress)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
    """
}

REQUIRED_COLUMNS = {
    'wisp_license_info': [
        ('max_active_sessions', 'INT DEFAULT 0 AFTER max_subscribers')
    ],
    'radpostauth': [
        ('nasipaddress', 'VARCHAR(45) DEFAULT NULL'),
        ('callingstationid', 'VARCHAR(50) DEFAULT NULL')
    ],
    'wisp_subscribers': [
        ('snap_volume_quota_mb', 'BIGINT NULL DEFAULT NULL'),
        ('global_seq_id', 'BIGINT NULL AFTER id'),
        ('email', "VARCHAR(120) DEFAULT '' AFTER phone"),
        ('notes', "TEXT NULL"),
        ('first_used_at', "DATETIME NULL"),
        ('last_renewed_at', "DATETIME NULL"),
        ('expires_at', "DATETIME NULL")
    ],
    'wisp_vouchers': [
        ('global_seq_id', 'BIGINT NULL AFTER id'),
        ('first_used_at', 'DATETIME NULL'),
        ('last_renewed_at', 'DATETIME NULL'),
        ('expires_at', 'DATETIME NULL'),
        ('bound_mac', 'VARCHAR(50) DEFAULT NULL'),
        ('snap_price', 'DECIMAL(10,2) DEFAULT 0.00'),
        ('snap_cost', 'DECIMAL(10,2) DEFAULT 0.00'),
        ('snap_volume_quota_mb', 'BIGINT DEFAULT 0'),
        ('snap_uptime_limit_mins', 'INT DEFAULT 0'),
        ('snap_validity_value', 'INT DEFAULT 30'),
        ('snap_validity_unit', "VARCHAR(20) DEFAULT 'days'"),
        ('snap_validity_days', 'INT DEFAULT 30'),
        ('snap_rate_download', "VARCHAR(50) DEFAULT '0'"),
        ('snap_rate_upload', "VARCHAR(50) DEFAULT '0'"),
        ('snap_rate_limit_str', "VARCHAR(100) DEFAULT '0/0'"),
        ('snap_simultaneous_sessions', 'INT DEFAULT 1'),
        ('snap_mikrotik_group', "VARCHAR(100) DEFAULT 'ALL-SPEED'")
    ],
    'wisp_voucher_batches': [
        ('price', 'DECIMAL(10,2) DEFAULT 0.00'),
        ('cost', 'DECIMAL(10,2) DEFAULT 0.00'),
        ('volume_quota_mb', 'BIGINT DEFAULT 0'),
        ('validity_days', 'INT DEFAULT 30'),
        ('validity_value', 'INT DEFAULT 30'),
        ('validity_unit', "VARCHAR(20) DEFAULT 'days'"),
        ('uptime_limit_mins', 'INT DEFAULT 0'),
        ('rate_download', "VARCHAR(50) DEFAULT '0'"),
        ('rate_upload', "VARCHAR(50) DEFAULT '0'"),
        ('rate_limit_str', "VARCHAR(100) DEFAULT '0/0'"),
        ('simultaneous_sessions', 'INT DEFAULT 1'),
        ('mikrotik_group', "VARCHAR(100) DEFAULT 'ALL-SPEED'"),
        ('prefix', "VARCHAR(20) DEFAULT ''"),
        ('pin_only', 'TINYINT(1) DEFAULT 0'),
        ('reseller_id', 'INT NULL')
    ],
    'wisp_packages': [
        ('show_in_portal', 'TINYINT(1) DEFAULT 1 AFTER is_active'),
        ('is_rollover_enabled', 'TINYINT(1) DEFAULT 0 AFTER is_active'),
        ('is_loyalty_enabled', 'TINYINT(1) DEFAULT 0 AFTER is_rollover_enabled'),
        ('loyalty_points', 'INT DEFAULT 0 AFTER is_loyalty_enabled'),
        ('validity_value', 'INT DEFAULT 30'),
        ('validity_unit', "VARCHAR(20) DEFAULT 'days'"),
        ('validity_days', 'INT DEFAULT 30'),
        ('mikrotik_group', "VARCHAR(100) DEFAULT 'ALL-SPEED'"),
        ('cost', 'DECIMAL(10,2) DEFAULT 0.00')
    ],
    'wisp_managers': [
        ('wallet_balance', 'DECIMAL(12,2) DEFAULT 0.00'),
        ('notes', 'TEXT NULL')
    ],
    'radacct': [
        ('acctinputgigawords', 'BIGINT DEFAULT 0'),
        ('acctoutputgigawords', 'BIGINT DEFAULT 0')
    ],
    'wisp_session_baselines': [
        ('baseline_input_bytes', 'BIGINT UNSIGNED NOT NULL DEFAULT 0 AFTER username'),
        ('baseline_output_bytes', 'BIGINT UNSIGNED NOT NULL DEFAULT 0 AFTER baseline_input_bytes')
    ]
}

TRIGGER_SUB_SQL = """
CREATE TRIGGER trg_radacct_subscriber_activate AFTER INSERT ON radacct
FOR EACH ROW
BEGIN
    UPDATE wisp_subscribers s
    JOIN wisp_packages p ON s.package_id = p.id
    SET s.expires_at = CASE
            WHEN s.status = 'inactive' AND s.first_used_at IS NULL AND s.last_renewed_at IS NULL
            THEN IFNULL(s.expires_at,
                CASE
                    WHEN COALESCE(p.validity_value, p.validity_days, 0) <= 0 THEN NULL
                    WHEN p.validity_unit = 'minutes' THEN DATE_ADD(NEW.acctstarttime, INTERVAL p.validity_value MINUTE)
                    WHEN p.validity_unit = 'hours' THEN DATE_ADD(NEW.acctstarttime, INTERVAL p.validity_value HOUR)
                    WHEN p.validity_unit = 'months' THEN DATE_ADD(NEW.acctstarttime, INTERVAL p.validity_value MONTH)
                    ELSE DATE_ADD(NEW.acctstarttime, INTERVAL COALESCE(p.validity_value, p.validity_days, 1) DAY)
                END)
            ELSE s.expires_at
        END,
        s.status = 'active',
        s.first_used_at = IFNULL(s.first_used_at, NEW.acctstarttime),
        s.last_renewed_at = IFNULL(s.last_renewed_at, NEW.acctstarttime)
    WHERE s.username = NEW.username AND s.status IN ('active', 'inactive')
      AND COALESCE(@max_radius_import, 0) = 0;
END;
"""

TRIGGER_VOUCHER_SQL = """
CREATE TRIGGER trg_radacct_activate_voucher AFTER INSERT ON radacct
FOR EACH ROW
BEGIN
    DECLARE v_id INT DEFAULT NULL;
    DECLARE v_batch_id INT DEFAULT NULL;
    DECLARE v_batch_name VARCHAR(100) DEFAULT NULL;
    DECLARE v_serial_number VARCHAR(100) DEFAULT NULL;
    DECLARE v_pkg_name VARCHAR(80) DEFAULT NULL;
    DECLARE v_pkg_price DECIMAL(10,2) DEFAULT 0.00;
    DECLARE v_pkg_cost DECIMAL(10,2) DEFAULT 0.00;
    DECLARE v_reseller_id INT DEFAULT NULL;
    DECLARE v_val INT DEFAULT 30;
    DECLARE v_unit VARCHAR(20) DEFAULT 'days';
    DECLARE v_exp_date DATETIME DEFAULT NULL;
    DECLARE v_rad_exp VARCHAR(50) DEFAULT NULL;
    DECLARE v_quota BIGINT DEFAULT 0;
    DECLARE v_uptime INT DEFAULT 0;
    DECLARE v_r_down VARCHAR(50) DEFAULT NULL;
    DECLARE v_r_up VARCHAR(50) DEFAULT NULL;
    DECLARE v_simul INT DEFAULT 1;
    DECLARE v_mgroup VARCHAR(100) DEFAULT NULL;
    DECLARE v_assigned_seq BIGINT DEFAULT NULL;
    
    SELECT v.id, v.batch_id, b.name, v.serial_number,
           p.name, COALESCE(v.snap_price, p.price), COALESCE(v.snap_cost, p.cost), v.reseller_id,
           COALESCE(v.snap_validity_value, v.snap_validity_days, p.validity_value, p.validity_days, 30),
           COALESCE(v.snap_validity_unit, p.validity_unit, 'days'),
           COALESCE(v.snap_volume_quota_mb, p.volume_quota_mb, 0),
           COALESCE(v.snap_uptime_limit_mins, p.uptime_limit_mins, 0),
           COALESCE(v.snap_rate_download, p.rate_download),
           COALESCE(v.snap_rate_upload, p.rate_upload),
           COALESCE(v.snap_simultaneous_sessions, p.simultaneous_sessions, 1),
           COALESCE(v.snap_mikrotik_group, p.mikrotik_group)
    INTO v_id, v_batch_id, v_batch_name, v_serial_number,
         v_pkg_name, v_pkg_price, v_pkg_cost, v_reseller_id,
         v_val, v_unit, v_quota, v_uptime,
         v_r_down, v_r_up, v_simul, v_mgroup
    FROM wisp_vouchers v
    JOIN wisp_packages p ON v.package_id = p.id
    JOIN wisp_voucher_batches b ON v.batch_id = b.id
    WHERE (LOWER(v.username) = LOWER(NEW.username) OR v.pin_code = NEW.username)
      AND v.status = 'unused'
      AND COALESCE(@max_radius_import, 0) = 0
    LIMIT 1;
    
    IF v_id IS NOT NULL THEN
        IF v_val <= 0 THEN
            SET v_exp_date = NULL;
            SET v_rad_exp = NULL;
        ELSEIF v_unit = 'minutes' THEN
            SET v_exp_date = DATE_ADD(COALESCE(NEW.acctstarttime, CURRENT_TIMESTAMP), INTERVAL v_val MINUTE);
            SET v_rad_exp = DATE_FORMAT(v_exp_date, '%d %b %Y %H:%i:%s');
        ELSEIF v_unit = 'hours' THEN
            SET v_exp_date = DATE_ADD(COALESCE(NEW.acctstarttime, CURRENT_TIMESTAMP), INTERVAL v_val HOUR);
            SET v_rad_exp = DATE_FORMAT(v_exp_date, '%d %b %Y %H:%i:%s');
        ELSEIF v_unit = 'months' THEN
            SET v_exp_date = DATE_ADD(COALESCE(NEW.acctstarttime, CURRENT_TIMESTAMP), INTERVAL v_val MONTH);
            SET v_rad_exp = DATE_FORMAT(v_exp_date, '%d %b %Y %H:%i:%s');
        ELSE
            SET v_exp_date = DATE_ADD(COALESCE(NEW.acctstarttime, CURRENT_TIMESTAMP), INTERVAL v_val DAY);
            SET v_rad_exp = DATE_FORMAT(v_exp_date, '%d %b %Y %H:%i:%s');
        END IF;
        
        INSERT INTO wisp_global_sequence (entity_type, entity_id, created_at)
        VALUES ('voucher', v_id, CURRENT_TIMESTAMP)
        ON DUPLICATE KEY UPDATE seq_id = seq_id;
        
        SELECT seq_id INTO v_assigned_seq 
        FROM wisp_global_sequence 
        WHERE entity_type = 'voucher' AND entity_id = v_id;
        
        UPDATE wisp_vouchers
        SET status = 'active',
            first_used_at = IFNULL(first_used_at, COALESCE(NEW.acctstarttime, CURRENT_TIMESTAMP)),
            last_renewed_at = IFNULL(last_renewed_at, COALESCE(NEW.acctstarttime, CURRENT_TIMESTAMP)),
            expires_at = IFNULL(expires_at, v_exp_date),
            bound_mac = CASE WHEN (bound_mac IS NULL OR bound_mac = '') AND NEW.callingstationid IS NOT NULL AND NEW.callingstationid != '' THEN NEW.callingstationid ELSE bound_mac END,
            global_seq_id = IFNULL(global_seq_id, v_assigned_seq),
            snap_price = COALESCE(snap_price, v_pkg_price),
            snap_cost = COALESCE(snap_cost, v_pkg_cost),
            snap_volume_quota_mb = COALESCE(snap_volume_quota_mb, v_quota),
            snap_uptime_limit_mins = COALESCE(snap_uptime_limit_mins, v_uptime),
            snap_validity_value = COALESCE(snap_validity_value, v_val),
            snap_validity_unit = COALESCE(NULLIF(snap_validity_unit, ''), v_unit),
            snap_validity_days = COALESCE(snap_validity_days, v_val),
            snap_rate_download = COALESCE(NULLIF(snap_rate_download, ''), v_r_down),
            snap_rate_upload = COALESCE(NULLIF(snap_rate_upload, ''), v_r_up),
            snap_simultaneous_sessions = COALESCE(snap_simultaneous_sessions, v_simul),
            snap_mikrotik_group = COALESCE(NULLIF(snap_mikrotik_group, ''), v_mgroup)
        WHERE id = v_id;
        
        IF NOT EXISTS (SELECT 1 FROM wisp_voucher_sales WHERE voucher_id = v_id) THEN
            INSERT INTO wisp_voucher_sales (
                voucher_id, batch_id, batch_name, username, serial_number,
                package_name, price, cost, reseller_id, activated_at
            ) VALUES (
                v_id, v_batch_id, v_batch_name, NEW.username, v_serial_number,
                v_pkg_name, v_pkg_price, v_pkg_cost, v_reseller_id, CURRENT_TIMESTAMP
            );
        END IF;
        
        DELETE FROM radcheck WHERE LOWER(username) = LOWER(NEW.username) AND attribute = 'Expiration';
        IF v_rad_exp IS NOT NULL THEN
            INSERT INTO radcheck (username, attribute, op, value)
            VALUES (NEW.username, 'Expiration', ':=', v_rad_exp);
        END IF;
    END IF;
END;
"""

from database.license_capacity import (LINK_SQL as TRIGGER_RESERVATION_RELEASE_INSERT_SQL,
    STOP_SQL as TRIGGER_RESERVATION_RELEASE_UPDATE_SQL, install_capacity_schema)

def install_accounting_triggers(conn):
    """Install the shared definitions and fail if any definition is not active."""
    import re
    if not is_mysql_conn(conn):
        return
    install_capacity_schema(conn)
    with conn.cursor() as cur:
        cur.execute('SELECT VERSION() AS version')
        maria = 'mariadb' in str(cur.fetchone()['version']).lower()
        for name, sql in (
            ('trg_radacct_subscriber_activate', TRIGGER_SUB_SQL),
            ('trg_radacct_activate_voucher', TRIGGER_VOUCHER_SQL),
            ('trg_radacct_release_reservation_insert', TRIGGER_RESERVATION_RELEASE_INSERT_SQL),
            ('trg_radacct_release_reservation_update', TRIGGER_RESERVATION_RELEASE_UPDATE_SQL)
        ):
            if maria:
                cur.execute(sql.replace('CREATE TRIGGER', 'CREATE OR REPLACE TRIGGER', 1))
            else:
                cur.execute(f'DROP TRIGGER IF EXISTS {name}')
                cur.execute(sql)
            cur.execute('SELECT ACTION_STATEMENT FROM information_schema.TRIGGERS '
                        'WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME=%s', (name,))
            row = cur.fetchone()
            normalize = lambda text: re.sub(r'\s+', ' ', text.strip().rstrip(';')).strip()
            expected = sql.split('FOR EACH ROW', 1)[1]
            if not row or normalize(row['ACTION_STATEMENT']) != normalize(expected):
                raise RuntimeError(f'Trigger verification failed: {name}')


def heal_database_schema(backfill=True, install_triggers=True):
    """
    Scans the database schema, compares against REQUIRED_TABLES and REQUIRED_COLUMNS,
    and executes ALTER / CREATE statements for any missing element.
    Safe and idempotent. Set backfill=False for schema-only migration preparation.
    """
    try:
        conn = get_connection()
        is_mysql = is_mysql_conn(conn)
        cur = conn.cursor()

        from database.loyalty_schema import ensure_loyalty_schema
        ensure_loyalty_schema(conn)

        # 1. Create any missing tables
        for tbl_name, create_sql in REQUIRED_TABLES.items():
            try:
                if is_mysql:
                    cur.execute(create_sql)
            except Exception as e:
                print(f"[Schema Healer] Table {tbl_name} check notice: {e}")

        # 2. Add missing columns across all registered tables
        for tbl_name, col_defs in REQUIRED_COLUMNS.items():
            existing_cols = set()
            try:
                if is_mysql:
                    cur.execute(f"SHOW COLUMNS FROM `{tbl_name}`")
                    rows = cur.fetchall()
                    for r in rows:
                        col_field = r['Field'] if isinstance(r, dict) else r[0]
                        existing_cols.add(col_field.lower())
                else:
                    cur.execute(f"PRAGMA table_info({tbl_name})")
                    rows = cur.fetchall()
                    for r in rows:
                        existing_cols.add(str(r[1]).lower())
            except Exception:
                # Table might not exist yet
                continue

            for col_name, col_spec in col_defs:
                if col_name.lower() not in existing_cols:
                    try:
                        alter_sql = f"ALTER TABLE `{tbl_name}` ADD COLUMN `{col_name}` {col_spec}"
                        cur.execute(alter_sql)
                        conn.commit()
                        print(f"[Schema Healer] Added missing column `{col_name}` to `{tbl_name}`.")
                    except Exception as e:
                        print(f"[Schema Healer] Notice adding column `{col_name}` to `{tbl_name}`: {e}")

        # Preserve the first session as the first cycle; defaults must not timestamp unused accounts.
        if is_mysql:
            for table in ('wisp_vouchers', 'wisp_subscribers'):
                cur.execute(f"ALTER TABLE {table} ALTER COLUMN last_renewed_at SET DEFAULT NULL")
            conn.commit()

        # 3. Backfill missing global sequence IDs
        try:
            if is_mysql and backfill:
                cur.execute("""
                    INSERT INTO wisp_global_sequence (entity_type, entity_id, created_at)
                    SELECT 'subscriber', id, NOW() FROM wisp_subscribers WHERE global_seq_id IS NULL
                    ON DUPLICATE KEY UPDATE seq_id = seq_id
                """)
                cur.execute("""
                    UPDATE wisp_subscribers s
                    JOIN wisp_global_sequence g ON g.entity_type = 'subscriber' AND g.entity_id = s.id
                    SET s.global_seq_id = g.seq_id
                    WHERE s.global_seq_id IS NULL
                """)
                cur.execute("""
                    INSERT INTO wisp_global_sequence (entity_type, entity_id, created_at)
                    SELECT 'voucher', id, NOW() FROM wisp_vouchers WHERE global_seq_id IS NULL
                    ON DUPLICATE KEY UPDATE seq_id = seq_id
                """)
                cur.execute("""
                    UPDATE wisp_vouchers v
                    JOIN wisp_global_sequence g ON g.entity_type = 'voucher' AND g.entity_id = v.id
                    SET v.global_seq_id = g.seq_id
                    WHERE v.global_seq_id IS NULL
                """)
                conn.commit()
        except Exception as e:
            print(f"[Schema Healer] Sequence backfill notice: {e}")

        # 4. Backfill missing snap columns on vouchers from batches / packages
        try:
            if is_mysql and backfill:
                cur.execute("""
                    UPDATE wisp_vouchers v
                    JOIN wisp_packages p ON v.package_id = p.id
                    SET v.snap_price = IFNULL(v.snap_price, p.price),
                        v.snap_cost = IFNULL(v.snap_cost, p.cost),
                        v.snap_volume_quota_mb = IFNULL(v.snap_volume_quota_mb, p.volume_quota_mb),
                        v.snap_uptime_limit_mins = IFNULL(v.snap_uptime_limit_mins, p.uptime_limit_mins),
                        v.snap_validity_value = IFNULL(v.snap_validity_value, p.validity_value),
                        v.snap_validity_unit = IFNULL(v.snap_validity_unit, p.validity_unit),
                        v.snap_validity_days = IFNULL(v.snap_validity_days, p.validity_days),
                        v.snap_rate_download = IFNULL(v.snap_rate_download, p.rate_download),
                        v.snap_rate_upload = IFNULL(v.snap_rate_upload, p.rate_upload),
                        v.snap_simultaneous_sessions = IFNULL(v.snap_simultaneous_sessions, p.simultaneous_sessions),
                        v.snap_mikrotik_group = IFNULL(v.snap_mikrotik_group, p.mikrotik_group)
                    WHERE v.snap_price IS NULL OR v.snap_price = 0.00
                """)
                conn.commit()
        except Exception as e:
            print(f"[Schema Healer] Snap backfill notice: {e}")

        # 4b. Permanently decouple sales ledger from vouchers to prevent deletion on voucher purge
        try:
            if is_mysql:
                cur.execute("""
                    SELECT CONSTRAINT_NAME
                    FROM information_schema.KEY_COLUMN_USAGE
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = 'wisp_voucher_sales'
                      AND REFERENCED_TABLE_NAME IS NOT NULL
                """)
                fk_rows = cur.fetchall()
                for fk in fk_rows:
                    fk_name = fk['CONSTRAINT_NAME'] if isinstance(fk, dict) else fk[0]
                    try:
                        cur.execute(f"ALTER TABLE `wisp_voucher_sales` DROP FOREIGN KEY `{fk_name}`;")
                        print(f"[Schema Healer] Dropped foreign key `{fk_name}` from `wisp_voucher_sales` to protect sales ledger.")
                    except Exception:
                        pass
                conn.commit()
        except Exception as e:
            print(f"[Schema Healer] Sales FK preservation notice: {e}")

        # 5. Ensure Triggers Exist
        try:
            if is_mysql and install_triggers:
                install_accounting_triggers(conn)
                conn.commit()
        except Exception as e:
            print(f"[Schema Healer] Trigger creation notice: {e}")

        if backfill:
            # Backfill missing Cleartext-Password in radcheck for subscribers and vouchers
            try:
                cur.execute('''
                    INSERT INTO radcheck (username, attribute, op, value)
                    SELECT s.username, 'Cleartext-Password', ':=', s.password
                    FROM wisp_subscribers s
                    WHERE s.status = 'active'
                    AND NOT EXISTS (
                        SELECT 1 FROM radcheck r WHERE r.username = s.username AND r.attribute = 'Cleartext-Password'
                    );
                ''')
                if is_mysql:
                    conn.commit()
            except Exception:
                pass
        conn.close()
        return True
    except Exception as e:
        print(f"[Schema Healer Critical Error]: {e}")
        return False

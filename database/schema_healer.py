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
REQUIRED_TABLES = {
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
    'wisp_l2tp_tunnels': """
        CREATE TABLE IF NOT EXISTS wisp_l2tp_tunnels (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(80) NOT NULL,
            username VARCHAR(64) NOT NULL UNIQUE,
            password VARCHAR(64) NOT NULL,
            tunnel_ip VARCHAR(45) NOT NULL UNIQUE,
            radius_secret VARCHAR(64) NOT NULL DEFAULT '123',
            ipsec_secret VARCHAR(64) NOT NULL DEFAULT '',
            reseller_id INT DEFAULT NULL,
            status VARCHAR(20) DEFAULT 'offline',
            is_enabled TINYINT(1) DEFAULT 1,
            latency_ms INT DEFAULT 0,
            last_connected_at DATETIME DEFAULT NULL,
            description TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_l2tp_reseller (reseller_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
}

REQUIRED_COLUMNS = {
    'wisp_subscribers': [
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
        ('validity_value', 'INT DEFAULT 30'),
        ('validity_unit', "VARCHAR(20) DEFAULT 'days'"),
        ('validity_days', 'INT DEFAULT 30'),
        ('mikrotik_group', "VARCHAR(100) DEFAULT 'ALL-SPEED'"),
        ('cost', 'DECIMAL(10,2) DEFAULT 0.00')
    ],
    'wisp_managers': [
        ('wallet_balance', 'DECIMAL(12,2) DEFAULT 0.00'),
        ('notes', 'TEXT NULL')
    ]
}

TRIGGER_SUB_SQL = """
CREATE TRIGGER trg_radacct_subscriber_activate AFTER INSERT ON radacct
FOR EACH ROW
BEGIN
    UPDATE wisp_subscribers s
    JOIN wisp_packages p ON s.package_id = p.id
    SET s.status = 'active',
        s.first_used_at = IFNULL(s.first_used_at, NEW.acctstarttime),
        s.last_renewed_at = IFNULL(s.last_renewed_at, NEW.acctstarttime),
        s.expires_at = IFNULL(s.expires_at, 
            CASE 
                WHEN p.validity_unit = 'minutes' THEN DATE_ADD(NEW.acctstarttime, INTERVAL COALESCE(p.validity_value, 1) MINUTE)
                WHEN p.validity_unit = 'hours' THEN DATE_ADD(NEW.acctstarttime, INTERVAL COALESCE(p.validity_value, 1) HOUR)
                WHEN p.validity_unit = 'months' THEN DATE_ADD(NEW.acctstarttime, INTERVAL COALESCE(p.validity_value, 1) MONTH)
                ELSE DATE_ADD(NEW.acctstarttime, INTERVAL COALESCE(p.validity_value, p.validity_days, 1) DAY)
            END
        )
    WHERE s.username = NEW.username AND (s.status = 'inactive' OR s.expires_at IS NULL);
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
           p.name, p.price, p.cost, v.reseller_id,
           COALESCE(p.validity_value, p.validity_days, 30),
           COALESCE(p.validity_unit, 'days'),
           COALESCE(p.volume_quota_mb, 0),
           COALESCE(p.uptime_limit_mins, 0),
           p.rate_download, p.rate_upload,
           COALESCE(p.simultaneous_sessions, 1),
           p.mikrotik_group
    INTO v_id, v_batch_id, v_batch_name, v_serial_number,
         v_pkg_name, v_pkg_price, v_pkg_cost, v_reseller_id,
         v_val, v_unit, v_quota, v_uptime,
         v_r_down, v_r_up, v_simul, v_mgroup
    FROM wisp_vouchers v
    JOIN wisp_packages p ON v.package_id = p.id
    JOIN wisp_voucher_batches b ON v.batch_id = b.id
    WHERE (LOWER(v.username) = LOWER(NEW.username) OR v.pin_code = NEW.username)
      AND (v.status = 'unused' OR v.snap_volume_quota_mb IS NULL)
    LIMIT 1;
    
    IF v_id IS NOT NULL THEN
        IF v_unit = 'minutes' THEN
            SET v_exp_date = DATE_ADD(CURRENT_TIMESTAMP, INTERVAL v_val MINUTE);
        ELSEIF v_unit = 'hours' THEN
            SET v_exp_date = DATE_ADD(CURRENT_TIMESTAMP, INTERVAL v_val HOUR);
        ELSEIF v_unit = 'months' THEN
            SET v_exp_date = DATE_ADD(CURRENT_TIMESTAMP, INTERVAL v_val MONTH);
        ELSE
            SET v_exp_date = DATE_ADD(CURRENT_TIMESTAMP, INTERVAL v_val DAY);
        END IF;
        
        SET v_rad_exp = DATE_FORMAT(v_exp_date, '%d %b %Y %H:%i:%s');
        
        INSERT INTO wisp_global_sequence (entity_type, entity_id, created_at)
        VALUES ('voucher', v_id, CURRENT_TIMESTAMP)
        ON DUPLICATE KEY UPDATE seq_id = seq_id;
        
        SELECT seq_id INTO v_assigned_seq 
        FROM wisp_global_sequence 
        WHERE entity_type = 'voucher' AND entity_id = v_id;
        
        UPDATE wisp_vouchers
        SET status = 'active',
            first_used_at = IFNULL(first_used_at, CURRENT_TIMESTAMP),
            last_renewed_at = IFNULL(last_renewed_at, CURRENT_TIMESTAMP),
            expires_at = IFNULL(expires_at, v_exp_date),
            global_seq_id = IFNULL(global_seq_id, v_assigned_seq),
            snap_price = IFNULL(snap_price, v_pkg_price),
            snap_cost = IFNULL(snap_cost, v_pkg_cost),
            snap_volume_quota_mb = IFNULL(snap_volume_quota_mb, v_quota),
            snap_uptime_limit_mins = IFNULL(snap_uptime_limit_mins, v_uptime),
            snap_validity_value = IFNULL(snap_validity_value, v_val),
            snap_validity_unit = IFNULL(snap_validity_unit, v_unit),
            snap_validity_days = IFNULL(snap_validity_days, v_val),
            snap_rate_download = IFNULL(snap_rate_download, v_r_down),
            snap_rate_upload = IFNULL(snap_rate_upload, v_r_up),
            snap_simultaneous_sessions = IFNULL(snap_simultaneous_sessions, v_simul),
            snap_mikrotik_group = IFNULL(snap_mikrotik_group, v_mgroup)
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
        INSERT INTO radcheck (username, attribute, op, value)
        VALUES (NEW.username, 'Expiration', ':=', v_rad_exp);
    END IF;
END;
"""

def heal_database_schema():
    """
    Scans the database schema, compares against REQUIRED_TABLES and REQUIRED_COLUMNS,
    and executes ALTER / CREATE statements for any missing element.
    Safe and idempotent.
    """
    try:
        conn = get_connection()
        is_mysql = is_mysql_conn(conn)
        cur = conn.cursor()
        if is_mysql:
            try:
                cur.execute("SET SESSION innodb_lock_wait_timeout = 2;")
            except Exception:
                pass

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

        # 3. Backfill missing global sequence IDs
        try:
            if is_mysql:
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
            if is_mysql:
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

        # 5. Ensure performance indexes exist
        try:
            if is_mysql:
                perf_indexes = [
                    ('radacct', 'idx_radacct_user_acct', 'CREATE INDEX idx_radacct_user_acct ON radacct(username, acctstoptime, acctupdatetime, acctstarttime)'),
                    ('radacct', 'idx_radacct_user_session', 'CREATE INDEX idx_radacct_user_session ON radacct(username, nasipaddress, acctsessionid)'),
                    ('wisp_vouchers', 'idx_voucher_status_id', 'CREATE INDEX idx_voucher_status_id ON wisp_vouchers(status, id DESC)'),
                    ('wisp_subscribers', 'idx_sub_status_id', 'CREATE INDEX idx_sub_status_id ON wisp_subscribers(status, id DESC)')
                ]
                for tbl, idx_name, sql in perf_indexes:
                    try:
                        cur.execute(f"SHOW INDEX FROM `{tbl}` WHERE Key_name = '{idx_name}'")
                        if not cur.fetchall():
                            cur.execute(sql)
                            conn.commit()
                            print(f"[Schema Healer] Created performance index `{idx_name}` on `{tbl}`.")
                    except Exception as e:
                        pass
        except Exception as e:
            print(f"[Schema Healer] Performance index notice: {e}")

        conn.close()
        print("[Schema Healer] Database schema verified and healed successfully.")
        return True
    except Exception as e:
        print(f"[Schema Healer Critical Error]: {e}")
        return False

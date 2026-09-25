-- =============================================================================
-- Complete MySQL / MariaDB Schema for FreeRADIUS & WISP Manager
-- Includes standard FreeRADIUS tables + WISP Billing Extensions + High-Speed Indexes
-- =============================================================================

SET FOREIGN_KEY_CHECKS = 0;

-- 1. Standard FreeRADIUS Tables
CREATE TABLE IF NOT EXISTS `radcheck` (
    `id` INT(11) UNSIGNED NOT NULL AUTO_INCREMENT,
    `username` VARCHAR(64) NOT NULL DEFAULT '',
    `attribute` VARCHAR(64) NOT NULL DEFAULT '',
    `op` CHAR(2) NOT NULL DEFAULT '==',
    `value` VARCHAR(253) NOT NULL DEFAULT '',
    PRIMARY KEY (`id`),
    KEY `idx_radcheck_username` (`username`(32))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `radreply` (
    `id` INT(11) UNSIGNED NOT NULL AUTO_INCREMENT,
    `username` VARCHAR(64) NOT NULL DEFAULT '',
    `attribute` VARCHAR(64) NOT NULL DEFAULT '',
    `op` CHAR(2) NOT NULL DEFAULT '=',
    `value` VARCHAR(253) NOT NULL DEFAULT '',
    PRIMARY KEY (`id`),
    KEY `idx_radreply_username` (`username`(32))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `radgroupcheck` (
    `id` INT(11) UNSIGNED NOT NULL AUTO_INCREMENT,
    `groupname` VARCHAR(64) NOT NULL DEFAULT '',
    `attribute` VARCHAR(64) NOT NULL DEFAULT '',
    `op` CHAR(2) NOT NULL DEFAULT '==',
    `value` VARCHAR(253) NOT NULL DEFAULT '',
    PRIMARY KEY (`id`),
    KEY `idx_radgroupcheck_groupname` (`groupname`(32))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `radgroupreply` (
    `id` INT(11) UNSIGNED NOT NULL AUTO_INCREMENT,
    `groupname` VARCHAR(64) NOT NULL DEFAULT '',
    `attribute` VARCHAR(64) NOT NULL DEFAULT '',
    `op` CHAR(2) NOT NULL DEFAULT '=',
    `value` VARCHAR(253) NOT NULL DEFAULT '',
    PRIMARY KEY (`id`),
    KEY `idx_radgroupreply_groupname` (`groupname`(32))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `radusergroup` (
    `id` INT(11) UNSIGNED NOT NULL AUTO_INCREMENT,
    `username` VARCHAR(64) NOT NULL DEFAULT '',
    `groupname` VARCHAR(64) NOT NULL DEFAULT '',
    `priority` INT(11) NOT NULL DEFAULT 1,
    PRIMARY KEY (`id`),
    KEY `idx_radusergroup_username` (`username`(32)),
    KEY `idx_radusergroup_groupname` (`groupname`(32))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `radacct` (
    `radacctid` BIGINT(21) NOT NULL AUTO_INCREMENT,
    `acctsessionid` VARCHAR(64) NOT NULL DEFAULT '',
    `acctuniqueid` VARCHAR(32) NOT NULL DEFAULT '',
    `username` VARCHAR(64) NOT NULL DEFAULT '',
    `realm` VARCHAR(64) DEFAULT '',
    `nasipaddress` VARCHAR(15) NOT NULL DEFAULT '',
    `nasportid` VARCHAR(32) DEFAULT NULL,
    `nasporttype` VARCHAR(32) DEFAULT NULL,
    `acctstarttime` DATETIME NULL DEFAULT NULL,
    `acctupdatetime` DATETIME NULL DEFAULT NULL,
    `acctstoptime` DATETIME NULL DEFAULT NULL,
    `acctinterval` INT(12) DEFAULT NULL,
    `acctsessiontime` INT(12) UNSIGNED DEFAULT NULL,
    `acctauthentic` VARCHAR(32) DEFAULT NULL,
    `connectinfo_start` VARCHAR(128) DEFAULT NULL,
    `connectinfo_stop` VARCHAR(128) DEFAULT NULL,
    `acctinputoctets` BIGINT(20) DEFAULT NULL,
    `acctoutputoctets` BIGINT(20) DEFAULT NULL,
    `calledstationid` VARCHAR(50) NOT NULL DEFAULT '',
    `callingstationid` VARCHAR(50) NOT NULL DEFAULT '',
    `acctterminatecause` VARCHAR(32) NOT NULL DEFAULT '',
    `servicetype` VARCHAR(32) DEFAULT NULL,
    `framedprotocol` VARCHAR(32) DEFAULT NULL,
    `framedipaddress` VARCHAR(15) NOT NULL DEFAULT '',
    `framedipv6address` VARCHAR(45) NOT NULL DEFAULT '',
    `framedipv6prefix` VARCHAR(45) NOT NULL DEFAULT '',
    `framedinterfaceid` VARCHAR(44) NOT NULL DEFAULT '',
    `delegatedipv6prefix` VARCHAR(45) NOT NULL DEFAULT '',
    `class` VARCHAR(64) DEFAULT NULL,
    PRIMARY KEY (`radacctid`),
    UNIQUE KEY `acctuniqueid` (`acctuniqueid`),
    KEY `idx_radacct_username` (`username`),
    KEY `idx_radacct_session` (`acctsessionid`),
    KEY `idx_radacct_nasip` (`nasipaddress`),
    KEY `idx_radacct_start` (`acctstarttime`),
    KEY `idx_radacct_stop` (`acctstoptime`),
    KEY `idx_radacct_active` (`nasipaddress`, `acctstoptime`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `radpostauth` (
    `id` INT(11) NOT NULL AUTO_INCREMENT,
    `username` VARCHAR(64) NOT NULL DEFAULT '',
    `pass` VARCHAR(64) NOT NULL DEFAULT '',
    `reply` VARCHAR(32) NOT NULL DEFAULT '',
    `authdate` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    `class` VARCHAR(64) DEFAULT NULL,
    PRIMARY KEY (`id`),
    KEY `idx_radpostauth_username` (`username`(32))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `nas` (
    `id` INT(10) NOT NULL AUTO_INCREMENT,
    `nasname` VARCHAR(128) NOT NULL,
    `shortname` VARCHAR(32) DEFAULT NULL,
    `type` VARCHAR(30) DEFAULT 'other',
    `ports` INT(5) DEFAULT NULL,
    `secret` VARCHAR(60) NOT NULL DEFAULT 'secret',
    `server` VARCHAR(64) DEFAULT NULL,
    `community` VARCHAR(50) DEFAULT NULL,
    `description` VARCHAR(200) DEFAULT 'RADIUS Client',
    PRIMARY KEY (`id`),
    KEY `idx_nas_nasname` (`nasname`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `nasreload` (
    `nasipaddress` VARCHAR(15) NOT NULL,
    `reloadtime` DATETIME NOT NULL,
    PRIMARY KEY (`nasipaddress`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 2. WISP Billing & Management Extension Tables
CREATE TABLE IF NOT EXISTS `wisp_packages` (
    `id` INT(11) NOT NULL AUTO_INCREMENT,
    `name` VARCHAR(80) NOT NULL,
    `service_type` VARCHAR(20) DEFAULT 'hotspot',
    `price` DECIMAL(10,2) DEFAULT 0.00,
    `cost` DECIMAL(10,2) DEFAULT 0.00,
    `rate_download` VARCHAR(20) DEFAULT '2M',
    `rate_upload` VARCHAR(20) DEFAULT '1M',
    `burst_download` VARCHAR(20) DEFAULT '4M',
    `burst_upload` VARCHAR(20) DEFAULT '2M',
    `burst_threshold_down` VARCHAR(20) DEFAULT '1500k',
    `burst_threshold_up` VARCHAR(20) DEFAULT '750k',
    `burst_time` INT(11) DEFAULT 16,
    `priority` INT(11) DEFAULT 8,
    `min_download` VARCHAR(20) DEFAULT '512k',
    `min_upload` VARCHAR(20) DEFAULT '256k',
    `volume_quota_mb` BIGINT(20) DEFAULT 0,
    `uptime_limit_mins` INT(11) DEFAULT 0,
    `validity_days` INT(11) DEFAULT 30,
    `validity_value` INT(11) DEFAULT 30,
    `validity_unit` VARCHAR(20) DEFAULT 'days',
    `mikrotik_group` VARCHAR(100) DEFAULT '',
    `simultaneous_sessions` INT(11) DEFAULT 1,
    `is_active` TINYINT(1) DEFAULT 1,
    `show_in_portal` TINYINT(1) DEFAULT 1,
    `is_rollover_enabled` TINYINT(1) DEFAULT 0,
    `description` TEXT,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `idx_pkg_name` (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `wisp_subscribers` (
    `id` INT(11) NOT NULL AUTO_INCREMENT,
    `global_seq_id` BIGINT(20) DEFAULT NULL,
    `username` VARCHAR(64) NOT NULL,
    `password` VARCHAR(64) NOT NULL,
    `full_name` VARCHAR(120) NOT NULL,
    `phone` VARCHAR(30) DEFAULT '',
    `email` VARCHAR(120) DEFAULT '',
    `national_id` VARCHAR(50) DEFAULT '',
    `address` TEXT,
    `service_type` VARCHAR(20) DEFAULT 'pppoe',
    `package_id` INT(11) NOT NULL,
    `mac_binding` VARCHAR(30) DEFAULT '',
    `static_ip` VARCHAR(45) DEFAULT '',
    `status` VARCHAR(20) DEFAULT 'active',
    `balance` DECIMAL(10,2) DEFAULT 0.00,
    `extra_quota_mb` BIGINT(20) DEFAULT 0,
    `loan_balance_mb` INT(11) DEFAULT 0,
    `loan_status` TINYINT(1) DEFAULT 0,
    `first_used_at` DATETIME NULL DEFAULT NULL,
    `expires_at` DATETIME NULL DEFAULT NULL,
    `last_renewed_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    `notes` TEXT,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `idx_subscriber_username` (`username`),
    KEY `idx_subs_pkg` (`package_id`),
    CONSTRAINT `fk_subs_package` FOREIGN KEY (`package_id`) REFERENCES `wisp_packages` (`id`) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `wisp_resellers` (
    `id` INT(11) NOT NULL AUTO_INCREMENT,
    `name` VARCHAR(120) NOT NULL,
    `contact_person` VARCHAR(100) DEFAULT '',
    `phone` VARCHAR(30) DEFAULT '',
    `email` VARCHAR(120) DEFAULT '',
    `balance` DECIMAL(10,2) DEFAULT 0.00,
    `commission_percent` DECIMAL(5,2) DEFAULT 10.00,
    `allowed_packages` TEXT,
    `status` VARCHAR(20) DEFAULT 'active',
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `wisp_reseller_transactions` (
    `id` INT(11) NOT NULL AUTO_INCREMENT,
    `reseller_id` INT(11) NOT NULL,
    `type` VARCHAR(20) NOT NULL,
    `amount` DECIMAL(10,2) NOT NULL,
    `balance_after` DECIMAL(10,2) NOT NULL,
    `description` TEXT,
    `reference_id` VARCHAR(64) DEFAULT '',
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_reseller_id` (`reseller_id`),
    CONSTRAINT `fk_trans_reseller` FOREIGN KEY (`reseller_id`) REFERENCES `wisp_resellers` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `wisp_voucher_batches` (
    `id` INT(11) NOT NULL AUTO_INCREMENT,
    `batch_number` VARCHAR(40) NOT NULL,
    `name` VARCHAR(100) NOT NULL,
    `package_id` INT(11) NOT NULL,
    `reseller_id` INT(11) DEFAULT NULL,
    `card_count` INT(11) NOT NULL,
    `prefix` VARCHAR(10) DEFAULT '',
    `pin_only` TINYINT(1) DEFAULT 1,
    `char_type` VARCHAR(20) DEFAULT 'numbers',
    `code_length` INT(11) DEFAULT 8,
    `price` DECIMAL(10,2) DEFAULT 0.00,
    `cost` DECIMAL(10,2) DEFAULT 0.00,
    `volume_quota_mb` BIGINT(20) DEFAULT 0,
    `uptime_limit_mins` INT(11) DEFAULT 0,
    `validity_value` INT(11) DEFAULT 30,
    `validity_unit` VARCHAR(20) DEFAULT 'days',
    `validity_days` INT(11) DEFAULT 30,
    `rate_download` VARCHAR(50) DEFAULT '0',
    `rate_upload` VARCHAR(50) DEFAULT '0',
    `rate_limit_str` VARCHAR(100) DEFAULT '0/0',
    `simultaneous_sessions` INT(11) DEFAULT 1,
    `mikrotik_group` VARCHAR(100) DEFAULT 'ALL-SPEED',
    `created_by` VARCHAR(64) DEFAULT 'admin',
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `idx_batch_number` (`batch_number`),
    KEY `idx_batch_package` (`package_id`),
    KEY `idx_batch_reseller` (`reseller_id`),
    CONSTRAINT `fk_batch_pkg` FOREIGN KEY (`package_id`) REFERENCES `wisp_packages` (`id`) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `wisp_l2tp_tunnels` (
    `id` INT(11) NOT NULL AUTO_INCREMENT,
    `name` VARCHAR(80) NOT NULL,
    `username` VARCHAR(64) NOT NULL,
    `password` VARCHAR(64) NOT NULL,
    `tunnel_ip` VARCHAR(45) NOT NULL,
    `radius_secret` VARCHAR(64) NOT NULL DEFAULT '123',
    `ipsec_secret` VARCHAR(64) NOT NULL DEFAULT '',
    `reseller_id` INT(11) DEFAULT NULL,
    `status` VARCHAR(20) DEFAULT 'offline',
    `is_enabled` TINYINT(1) DEFAULT 1,
    `latency_ms` INT(11) DEFAULT 0,
    `last_connected_at` DATETIME DEFAULT NULL,
    `description` TEXT,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `idx_l2tp_username` (`username`),
    UNIQUE KEY `idx_l2tp_ip` (`tunnel_ip`),
    KEY `idx_l2tp_reseller` (`reseller_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `wisp_vouchers` (
    `id` INT(11) NOT NULL AUTO_INCREMENT,
    `global_seq_id` BIGINT(20) DEFAULT NULL,
    `batch_id` INT(11) NOT NULL,
    `package_id` INT(11) NOT NULL,
    `reseller_id` INT(11) DEFAULT NULL,
    `serial_number` VARCHAR(30) NOT NULL,
    `username` VARCHAR(64) NOT NULL,
    `password` VARCHAR(64) NOT NULL,
    `pin_code` VARCHAR(32) NOT NULL,
    `status` VARCHAR(20) DEFAULT 'unused',
    `first_used_at` DATETIME DEFAULT NULL,
    `expires_at` DATETIME DEFAULT NULL,
    `last_renewed_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    `bound_mac` VARCHAR(50) DEFAULT NULL,
    `extra_quota_mb` BIGINT(20) DEFAULT 0,
    `expire_reason` VARCHAR(60) DEFAULT '',
    `snap_price` DECIMAL(10,2) DEFAULT 0.00,
    `snap_cost` DECIMAL(10,2) DEFAULT 0.00,
    `snap_volume_quota_mb` BIGINT(20) DEFAULT 0,
    `snap_uptime_limit_mins` INT(11) DEFAULT 0,
    `snap_validity_value` INT(11) DEFAULT 30,
    `snap_validity_unit` VARCHAR(20) DEFAULT 'days',
    `snap_validity_days` INT(11) DEFAULT 30,
    `snap_rate_download` VARCHAR(50) DEFAULT '0',
    `snap_rate_upload` VARCHAR(50) DEFAULT '0',
    `snap_rate_limit_str` VARCHAR(100) DEFAULT '0/0',
    `snap_simultaneous_sessions` INT(11) DEFAULT 1,
    `snap_mikrotik_group` VARCHAR(100) DEFAULT 'ALL-SPEED',
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `idx_voucher_serial` (`serial_number`),
    UNIQUE KEY `idx_voucher_username` (`username`),
    KEY `idx_voucher_batch` (`batch_id`),
    KEY `idx_voucher_package` (`package_id`),
    KEY `idx_voucher_status` (`status`),
    CONSTRAINT `fk_voucher_batch` FOREIGN KEY (`batch_id`) REFERENCES `wisp_voucher_batches` (`id`) ON DELETE CASCADE,
    CONSTRAINT `fk_voucher_pkg` FOREIGN KEY (`package_id`) REFERENCES `wisp_packages` (`id`) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `wisp_voucher_sales` (
    `id` INT(11) NOT NULL AUTO_INCREMENT,
    `voucher_id` INT(11) NOT NULL,
    `batch_id` INT(11) NOT NULL,
    `batch_name` VARCHAR(100) DEFAULT '',
    `username` VARCHAR(64) NOT NULL,
    `serial_number` VARCHAR(30) DEFAULT '',
    `package_name` VARCHAR(80) NOT NULL,
    `price` DECIMAL(10,2) NOT NULL,
    `cost` DECIMAL(10,2) DEFAULT 0.00,
    `reseller_id` INT(11) DEFAULT NULL,
    `activated_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_sales_voucher` (`voucher_id`),
    KEY `idx_sales_batch` (`batch_id`),
    KEY `idx_sales_activated` (`activated_at`),
    CONSTRAINT `fk_sales_voucher` FOREIGN KEY (`voucher_id`) REFERENCES `wisp_vouchers` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `wisp_invoices` (
    `id` INT(11) NOT NULL AUTO_INCREMENT,
    `invoice_number` VARCHAR(40) NOT NULL,
    `subscriber_id` INT(11) DEFAULT NULL,
    `subscriber_name` VARCHAR(120) DEFAULT '',
    `package_name` VARCHAR(80) DEFAULT '',
    `amount` DECIMAL(10,2) NOT NULL,
    `status` VARCHAR(20) DEFAULT 'unpaid',
    `issue_date` DATE DEFAULT NULL,
    `due_date` DATE DEFAULT NULL,
    `paid_at` DATETIME DEFAULT NULL,
    `payment_method` VARCHAR(40) DEFAULT 'cash',
    `notes` TEXT,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `idx_invoice_num` (`invoice_number`),
    KEY `idx_inv_subscriber` (`subscriber_id`),
    CONSTRAINT `fk_inv_subscriber` FOREIGN KEY (`subscriber_id`) REFERENCES `wisp_subscribers` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `wisp_nas_devices` (
    `id` INT(11) NOT NULL AUTO_INCREMENT,
    `name` VARCHAR(80) NOT NULL,
    `ip_address` VARCHAR(45) NOT NULL,
    `nas_type` VARCHAR(30) DEFAULT 'mikrotik',
    `secret` VARCHAR(64) NOT NULL,
    `api_port` INT(11) DEFAULT 8728,
    `coa_port` INT(11) DEFAULT 3799,
    `api_username` VARCHAR(64) DEFAULT 'admin',
    `api_password` VARCHAR(64) DEFAULT '',
    `hotspot_login_url` VARCHAR(255) DEFAULT 'http://192.168.88.1/login',
    `status` VARCHAR(20) DEFAULT 'online',
    `latency_ms` INT(11) DEFAULT 0,
    `description` TEXT,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `idx_nas_ip` (`ip_address`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `wisp_l2tp_tunnels` (
    `id` INT(11) NOT NULL AUTO_INCREMENT,
    `name` VARCHAR(80) NOT NULL,
    `username` VARCHAR(64) NOT NULL,
    `password` VARCHAR(64) NOT NULL,
    `tunnel_ip` VARCHAR(45) NOT NULL,
    `radius_secret` VARCHAR(64) NOT NULL DEFAULT '123',
    `ipsec_secret` VARCHAR(64) NOT NULL DEFAULT '',
    `reseller_id` INT(11) DEFAULT NULL,
    `status` VARCHAR(20) DEFAULT 'offline',
    `is_enabled` TINYINT(1) DEFAULT 1,
    `latency_ms` INT(11) DEFAULT 0,
    `last_connected_at` DATETIME DEFAULT NULL,
    `description` TEXT,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `idx_l2tp_user` (`username`),
    UNIQUE KEY `idx_l2tp_ip` (`tunnel_ip`),
    KEY `idx_l2tp_reseller` (`reseller_id`),
    CONSTRAINT `fk_l2tp_reseller` FOREIGN KEY (`reseller_id`) REFERENCES `wisp_resellers` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `wisp_card_templates` (
    `id` INT(11) NOT NULL AUTO_INCREMENT,
    `name` VARCHAR(80) NOT NULL,
    `width_mm` INT(11) DEFAULT 85,
    `height_mm` INT(11) DEFAULT 55,
    `cards_per_row` INT(11) DEFAULT 2,
    `background_color` VARCHAR(20) DEFAULT '#ffffff',
    `border_color` VARCHAR(20) DEFAULT '#cbd5e1',
    `header_color` VARCHAR(20) DEFAULT '#1e40af',
    `show_qr` TINYINT(1) DEFAULT 1,
    `show_logo` TINYINT(1) DEFAULT 1,
    `show_price` TINYINT(1) DEFAULT 1,
    `show_validity` TINYINT(1) DEFAULT 1,
    `header_title` VARCHAR(100) DEFAULT 'كارت إنترنت فائق السرعة',
    `footer_text` VARCHAR(150) DEFAULT 'نتمنى لكم تجربة تصفح ممتعة',
    `logo_image` VARCHAR(255) DEFAULT '',
    `is_default` TINYINT(1) DEFAULT 0,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `wisp_backups` (
    `id` INT(11) NOT NULL AUTO_INCREMENT,
    `filename` VARCHAR(255) NOT NULL,
    `file_path` VARCHAR(255) NOT NULL,
    `file_size_bytes` BIGINT(20) DEFAULT 0,
    `file_size_mb` DECIMAL(10,2) DEFAULT 0.00,
    `system_version` VARCHAR(20) DEFAULT '2.4.0',
    `checksum_sha256` VARCHAR(64) DEFAULT '',
    `backup_type` VARCHAR(20) DEFAULT 'full',
    `created_by` VARCHAR(64) DEFAULT 'admin',
    `notes` TEXT,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `idx_backup_filename` (`filename`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `wisp_audit_logs` (
    `id` INT(11) NOT NULL AUTO_INCREMENT,
    `admin_id` INT(11) DEFAULT NULL,
    `username` VARCHAR(64) DEFAULT '',
    `action` VARCHAR(60) NOT NULL,
    `module` VARCHAR(40) NOT NULL,
    `details` TEXT,
    `ip_address` VARCHAR(45) DEFAULT '',
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_audit_module` (`module`),
    KEY `idx_audit_action` (`action`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `wisp_system_alerts` (
    `id` INT(11) NOT NULL AUTO_INCREMENT,
    `alert_type` VARCHAR(50) NOT NULL,
    `severity` VARCHAR(20) DEFAULT 'warning',
    `source` VARCHAR(50) DEFAULT 'watchdog',
    `message` TEXT NOT NULL,
    `is_resolved` TINYINT(1) DEFAULT 0,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    `resolved_at` DATETIME NULL,
    PRIMARY KEY (`id`),
    KEY `idx_alert_type` (`alert_type`),
    KEY `idx_alert_resolved` (`is_resolved`),
    KEY `idx_alert_created` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `wisp_admins` (
    `id` INT(11) NOT NULL AUTO_INCREMENT,
    `username` VARCHAR(64) NOT NULL,
    `password` VARCHAR(255) NOT NULL,
    `full_name` VARCHAR(120) NOT NULL,
    `role` VARCHAR(30) DEFAULT 'superadmin',
    `is_active` TINYINT(1) DEFAULT 1,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `idx_admin_username` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 3. Advanced RBAC (Roles & Permissions) & Manager Billing/Ledger
CREATE TABLE IF NOT EXISTS `wisp_permissions` (
    `id` INT(11) NOT NULL AUTO_INCREMENT,
    `code` VARCHAR(80) NOT NULL,
    `name` VARCHAR(120) NOT NULL,
    `category` VARCHAR(60) NOT NULL,
    `description` VARCHAR(255) DEFAULT '',
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `idx_perm_code` (`code`),
    KEY `idx_perm_category` (`category`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `wisp_roles` (
    `id` INT(11) NOT NULL AUTO_INCREMENT,
    `name` VARCHAR(80) NOT NULL,
    `code` VARCHAR(50) NOT NULL,
    `description` VARCHAR(255) DEFAULT '',
    `is_system` TINYINT(1) DEFAULT 0,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `idx_role_code` (`code`),
    UNIQUE KEY `idx_role_name` (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `wisp_role_permissions` (
    `id` INT(11) NOT NULL AUTO_INCREMENT,
    `role_id` INT(11) NOT NULL,
    `permission_id` INT(11) NOT NULL,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `idx_role_perm` (`role_id`, `permission_id`),
    KEY `idx_rp_role` (`role_id`),
    KEY `idx_rp_perm` (`permission_id`),
    CONSTRAINT `fk_rp_role` FOREIGN KEY (`role_id`) REFERENCES `wisp_roles` (`id`) ON DELETE CASCADE,
    CONSTRAINT `fk_rp_perm` FOREIGN KEY (`permission_id`) REFERENCES `wisp_permissions` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `wisp_managers` (
    `id` INT(11) NOT NULL AUTO_INCREMENT,
    `username` VARCHAR(64) NOT NULL,
    `password_hash` VARCHAR(255) NOT NULL,
    `full_name` VARCHAR(120) NOT NULL,
    `phone` VARCHAR(30) DEFAULT '',
    `email` VARCHAR(120) DEFAULT '',
    `role_id` INT(11) NOT NULL,
    `wallet_balance` DECIMAL(12,2) DEFAULT 0.00,
    `credit_limit` DECIMAL(12,2) DEFAULT 0.00,
    `commission_percent` DECIMAL(5,2) DEFAULT 0.00,
    `allowed_packages` TEXT,
    `is_active` TINYINT(1) DEFAULT 1,
    `is_deleted` TINYINT(1) DEFAULT 0,
    `notes` TEXT,
    `last_login_at` DATETIME NULL,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `idx_manager_username` (`username`),
    KEY `idx_mgr_role` (`role_id`),
    CONSTRAINT `fk_mgr_role` FOREIGN KEY (`role_id`) REFERENCES `wisp_roles` (`id`) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `wisp_manager_invoices` (
    `id` INT(11) NOT NULL AUTO_INCREMENT,
    `invoice_number` VARCHAR(50) NOT NULL,
    `manager_id` INT(11) NOT NULL,
    `transaction_type` VARCHAR(30) NOT NULL, -- 'deposit', 'deduction', 'debt_payment', 'card_purchase', 'refund', 'void'
    `amount` DECIMAL(12,2) NOT NULL,
    `payment_type` VARCHAR(30) NOT NULL DEFAULT 'cash', -- 'cash', 'credit', 'transfer'
    `balance_before` DECIMAL(12,2) NOT NULL DEFAULT 0.00,
    `balance_after` DECIMAL(12,2) NOT NULL DEFAULT 0.00,
    `notes` TEXT,
    `is_voided` TINYINT(1) DEFAULT 0,
    `void_reason` TEXT,
    `created_by` VARCHAR(64) DEFAULT 'admin',
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `idx_mgr_inv_number` (`invoice_number`),
    KEY `idx_mgr_inv_manager` (`manager_id`),
    KEY `idx_mgr_inv_type` (`transaction_type`),
    KEY `idx_mgr_inv_payment` (`payment_type`),
    KEY `idx_mgr_inv_created` (`created_at`),
    CONSTRAINT `fk_mgr_inv_manager` FOREIGN KEY (`manager_id`) REFERENCES `wisp_managers` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Healthcheck User for FreeRADIUS Container Live Probe
INSERT INTO `radcheck` (`username`, `attribute`, `op`, `value`) VALUES
('healthcheck', 'Cleartext-Password', ':=', 'healthpass')
ON DUPLICATE KEY UPDATE `value` = 'healthpass';

-- Default Settings & Admin Seed
INSERT INTO `wisp_admins` (`username`, `password`, `full_name`, `role`) VALUES
('admin', 'admin', 'مدير النظام الرئيسي', 'superadmin')
ON DUPLICATE KEY UPDATE `full_name` = VALUES(`full_name`);

INSERT INTO `wisp_roles` (`id`, `name`, `code`, `description`, `is_system`) VALUES
(1, 'المدير العام (Super Admin)', 'superadmin', 'صلاحيات كاملة وغير مقيدة على كافة أقسام النظام والخدمات', 1),
(2, 'مدير فرع / شبكة (Manager)', 'manager', 'إدارة المشتركين، الكروت، الباقات، ومراقبة الراوترات', 0),
(3, 'موزع رئيسي (Main Reseller)', 'main_reseller', 'توليد الكروت، شحن نقاط البيع، واستعراض مبيعاته وفواتيره', 0),
(4, 'نقطة بيع (POS Agent)', 'pos_agent', 'استعراض الكروت المخصصة له، تفعيل الاشتراكات، وشحن أرصدة المستخدمين', 0)
ON DUPLICATE KEY UPDATE `name` = VALUES(`name`);

INSERT INTO `wisp_managers` (`id`, `username`, `password_hash`, `full_name`, `phone`, `email`, `role_id`, `wallet_balance`, `is_active`)
VALUES (1, 'admin', 'admin', 'مدير النظام الرئيسي', '+967 770 000 000', 'admin@wisp-network.com', 1, 0.00, 1)
ON DUPLICATE KEY UPDATE `role_id` = 1, `is_active` = 1;

CREATE TABLE IF NOT EXISTS `wisp_system_settings` (
    `key` VARCHAR(64) NOT NULL,
    `value` TEXT,
    `description` VARCHAR(255) DEFAULT '',
    PRIMARY KEY (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT INTO `wisp_system_settings` (`key`, `value`, `description`) VALUES
('network_name', 'MAX RADIUS', 'اسم الشبكة أو مزود الخدمة'),
('company_name', 'MAX RADIUS', 'اسم الشركة'),
('isp_name', 'MAX RADIUS', 'اسم مزود الخدمة'),
('network_logo', '', 'مسار شعار الشبكة'),
('currency', 'YER', 'رمز كود العملة'),
('currency_symbol', 'ر.ي', 'رمز العملة المعروض'),
('timezone', 'Asia/Aden', 'المنطقة الزمنية للنظام'),
('support_phone', '777366226', 'رقم هاتف الدعم الفني'),
('support_email', 'support@max-radius.net', 'البريد الإلكتروني للدعم الفني'),
('address', 'المركز الرئيسي لخدمات الإنترنت', 'العنوان والمقر'),
('hotspot_domain', 'wifi.maxradius.net', 'نطاق صفحة تسجيل الدخول'),
('system_title', 'MAX RADIUS - نظام إدارة الشبكات والفوترة و FreeRADIUS', 'عنوان النظام'),
('sms_sender_id', 'MAX-RADIUS', 'اسم مرسل الرسائل'),
('allow_data_loan', '1', 'تفعيل أو تعطيل ميزة سلفة البيانات للمشتركين'),
('loan_amount_mb', '1024', 'حجم سلفة البيانات بالميجابايت الافتراضية'),
('loan_threshold_mb', '100', 'حد الرصيد المتبقي بالميجابايت الذي يسمح بطلب السلفة عنده')
ON DUPLICATE KEY UPDATE `description` = VALUES(`description`);

-- Auto-Activation Triggers for FreeRADIUS Accounting & Auth
DROP TRIGGER IF EXISTS `trg_radacct_activate_voucher`;
DELIMITER //
CREATE TRIGGER `trg_radacct_activate_voucher`
AFTER INSERT ON `radacct`
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
      AND v.status = 'unused'
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
        
        UPDATE wisp_vouchers
        SET status = 'active',
            first_used_at = CURRENT_TIMESTAMP,
            last_renewed_at = CURRENT_TIMESTAMP,
            expires_at = v_exp_date,
            bound_mac = CASE WHEN (bound_mac IS NULL OR bound_mac = '') AND NEW.callingstationid != '' THEN NEW.callingstationid ELSE bound_mac END,
            snap_price = v_pkg_price,
            snap_cost = v_pkg_cost,
            snap_volume_quota_mb = v_quota,
            snap_uptime_limit_mins = v_uptime,
            snap_validity_value = v_val,
            snap_validity_unit = v_unit,
            snap_validity_days = v_val,
            snap_rate_download = v_r_down,
            snap_rate_upload = v_r_up,
            snap_simultaneous_sessions = v_simul,
            snap_mikrotik_group = v_mgroup
        WHERE id = v_id;
        
        INSERT INTO wisp_voucher_sales (
            voucher_id, batch_id, batch_name, username, serial_number,
            package_name, price, cost, reseller_id, activated_at
        ) VALUES (
            v_id, v_batch_id, v_batch_name, NEW.username, v_serial_number,
            v_pkg_name, v_pkg_price, v_pkg_cost, v_reseller_id, CURRENT_TIMESTAMP
        );
        
        DELETE FROM radcheck WHERE LOWER(username) = LOWER(NEW.username) AND attribute = 'Expiration';
        INSERT INTO radcheck (username, attribute, op, value)
        VALUES (NEW.username, 'Expiration', ':=', v_rad_exp);
    END IF;
END //
DELIMITER ;

SET FOREIGN_KEY_CHECKS = 1;
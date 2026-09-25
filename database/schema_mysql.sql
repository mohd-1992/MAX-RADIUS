-- ============================================================
-- MAX RADIUS 2.0 - Master Complete Database Schema & Seed Data
-- ============================================================
SET FOREIGN_KEY_CHECKS=0;

/*M!999999\- enable the sandbox mode */ 
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `nas` (
  `id` int(10) NOT NULL AUTO_INCREMENT,
  `nasname` varchar(128) NOT NULL,
  `shortname` varchar(32) DEFAULT NULL,
  `type` varchar(30) DEFAULT 'other',
  `ports` int(5) DEFAULT NULL,
  `secret` varchar(60) NOT NULL DEFAULT 'secret',
  `server` varchar(64) DEFAULT NULL,
  `community` varchar(50) DEFAULT NULL,
  `description` varchar(200) DEFAULT 'RADIUS Client',
  PRIMARY KEY (`id`),
  KEY `idx_nas_nasname` (`nasname`)
) ENGINE=InnoDB AUTO_INCREMENT=10 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `nasreload` (
  `nasipaddress` varchar(15) NOT NULL,
  `reloadtime` datetime NOT NULL,
  PRIMARY KEY (`nasipaddress`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `radacct` (
  `radacctid` bigint(21) NOT NULL AUTO_INCREMENT,
  `acctsessionid` varchar(64) NOT NULL DEFAULT '',
  `acctuniqueid` varchar(32) NOT NULL DEFAULT '',
  `username` varchar(64) NOT NULL DEFAULT '',
  `realm` varchar(64) DEFAULT '',
  `nasipaddress` varchar(15) NOT NULL DEFAULT '',
  `nasportid` varchar(32) DEFAULT NULL,
  `nasporttype` varchar(32) DEFAULT NULL,
  `acctstarttime` datetime DEFAULT NULL,
  `acctupdatetime` datetime DEFAULT NULL,
  `acctstoptime` datetime DEFAULT NULL,
  `acctinterval` int(12) DEFAULT NULL,
  `acctsessiontime` int(12) unsigned DEFAULT NULL,
  `acctauthentic` varchar(32) DEFAULT NULL,
  `connectinfo_start` varchar(128) DEFAULT NULL,
  `connectinfo_stop` varchar(128) DEFAULT NULL,
  `acctinputoctets` bigint(20) DEFAULT NULL,
  `acctinputgigawords` bigint(20) DEFAULT 0,
  `acctoutputoctets` bigint(20) DEFAULT NULL,
  `acctoutputgigawords` bigint(20) DEFAULT 0,
  `calledstationid` varchar(50) NOT NULL DEFAULT '',
  `callingstationid` varchar(50) NOT NULL DEFAULT '',
  `acctterminatecause` varchar(32) NOT NULL DEFAULT '',
  `servicetype` varchar(32) DEFAULT NULL,
  `framedprotocol` varchar(32) DEFAULT NULL,
  `framedipaddress` varchar(15) NOT NULL DEFAULT '',
  `framedipv6address` varchar(45) NOT NULL DEFAULT '',
  `framedipv6prefix` varchar(45) NOT NULL DEFAULT '',
  `framedinterfaceid` varchar(44) NOT NULL DEFAULT '',
  `delegatedipv6prefix` varchar(45) NOT NULL DEFAULT '',
  `class` varchar(64) DEFAULT NULL,
  PRIMARY KEY (`radacctid`),
  UNIQUE KEY `acctuniqueid` (`acctuniqueid`),
  KEY `idx_radacct_username` (`username`),
  KEY `idx_radacct_session` (`acctsessionid`),
  KEY `idx_radacct_nasip` (`nasipaddress`),
  KEY `idx_radacct_start` (`acctstarttime`),
  KEY `idx_radacct_stop` (`acctstoptime`),
  KEY `idx_radacct_active` (`nasipaddress`,`acctstoptime`),
  KEY `idx_radacct_user_acct` (`username`,`acctstoptime`,`acctupdatetime`,`acctstarttime`),
  KEY `idx_radacct_user_session` (`username`,`nasipaddress`,`acctsessionid`),
  KEY `idx_radacct_user_stop` (`username`,`acctstoptime`),
  KEY `idx_radacct_user_start` (`username`,`acctstarttime`)
) ENGINE=InnoDB AUTO_INCREMENT=302309 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!50003 SET @saved_cs_client      = @@character_set_client */ ;
/*!50003 SET @saved_cs_results     = @@character_set_results */ ;
/*!50003 SET @saved_col_connection = @@collation_connection */ ;
/*!50003 SET character_set_client  = utf8mb4 */ ;
/*!50003 SET character_set_results = utf8mb4 */ ;
/*!50003 SET collation_connection  = utf8mb4_general_ci */ ;
/*!50003 SET @saved_sql_mode       = @@sql_mode */ ;
/*!50003 SET sql_mode              = 'STRICT_TRANS_TABLES,ERROR_FOR_DIVISION_BY_ZERO,NO_AUTO_CREATE_USER,NO_ENGINE_SUBSTITUTION' */ ;
DELIMITER ;;
/*!50003 CREATE*/ /*!50017 DEFINER=`radius`@`%`*/ /*!50003 TRIGGER trg_radacct_subscriber_activate AFTER INSERT ON radacct
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
END 
*/;;
DELIMITER ;
/*!50003 SET sql_mode              = @saved_sql_mode */ ;
/*!50003 SET character_set_client  = @saved_cs_client */ ;
/*!50003 SET character_set_results = @saved_cs_results */ ;
/*!50003 SET collation_connection  = @saved_col_connection */ ;
/*!50003 SET @saved_cs_client      = @@character_set_client */ ;
/*!50003 SET @saved_cs_results     = @@character_set_results */ ;
/*!50003 SET @saved_col_connection = @@collation_connection */ ;
/*!50003 SET character_set_client  = utf8mb4 */ ;
/*!50003 SET character_set_results = utf8mb4 */ ;
/*!50003 SET collation_connection  = utf8mb4_general_ci */ ;
/*!50003 SET @saved_sql_mode       = @@sql_mode */ ;
/*!50003 SET sql_mode              = 'STRICT_TRANS_TABLES,ERROR_FOR_DIVISION_BY_ZERO,NO_AUTO_CREATE_USER,NO_ENGINE_SUBSTITUTION' */ ;
DELIMITER ;;
/*!50003 CREATE*/ /*!50017 DEFINER=`radius`@`%`*/ /*!50003 TRIGGER trg_radacct_activate_voucher AFTER INSERT ON radacct
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
            bound_mac = CASE WHEN (bound_mac IS NULL OR bound_mac = '') AND NEW.callingstationid IS NOT NULL AND NEW.callingstationid != '' THEN NEW.callingstationid ELSE bound_mac END,
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
END 
*/;;
DELIMITER ;
/*!50003 SET sql_mode              = @saved_sql_mode */ ;
/*!50003 SET character_set_client  = @saved_cs_client */ ;
/*!50003 SET character_set_results = @saved_cs_results */ ;
/*!50003 SET collation_connection  = @saved_col_connection */ ;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `radcheck` (
  `id` int(11) unsigned NOT NULL AUTO_INCREMENT,
  `username` varchar(64) NOT NULL DEFAULT '',
  `attribute` varchar(64) NOT NULL DEFAULT '',
  `op` char(2) NOT NULL DEFAULT '==',
  `value` varchar(253) NOT NULL DEFAULT '',
  PRIMARY KEY (`id`),
  KEY `idx_radcheck_username` (`username`(32))
) ENGINE=InnoDB AUTO_INCREMENT=6832 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `radgroupcheck` (
  `id` int(11) unsigned NOT NULL AUTO_INCREMENT,
  `groupname` varchar(64) NOT NULL DEFAULT '',
  `attribute` varchar(64) NOT NULL DEFAULT '',
  `op` char(2) NOT NULL DEFAULT '==',
  `value` varchar(253) NOT NULL DEFAULT '',
  PRIMARY KEY (`id`),
  KEY `idx_radgroupcheck_groupname` (`groupname`(32))
) ENGINE=InnoDB AUTO_INCREMENT=13 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `radgroupreply` (
  `id` int(11) unsigned NOT NULL AUTO_INCREMENT,
  `groupname` varchar(64) NOT NULL DEFAULT '',
  `attribute` varchar(64) NOT NULL DEFAULT '',
  `op` char(2) NOT NULL DEFAULT '=',
  `value` varchar(253) NOT NULL DEFAULT '',
  PRIMARY KEY (`id`),
  KEY `idx_radgroupreply_groupname` (`groupname`(32))
) ENGINE=InnoDB AUTO_INCREMENT=25 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `radpostauth` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `username` varchar(64) NOT NULL DEFAULT '',
  `pass` varchar(64) NOT NULL DEFAULT '',
  `reply` varchar(32) NOT NULL DEFAULT '',
  `authdate` timestamp NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
  `class` varchar(64) DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_radpostauth_username` (`username`(32))
) ENGINE=InnoDB AUTO_INCREMENT=17216 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `radreply` (
  `id` int(11) unsigned NOT NULL AUTO_INCREMENT,
  `username` varchar(64) NOT NULL DEFAULT '',
  `attribute` varchar(64) NOT NULL DEFAULT '',
  `op` char(2) NOT NULL DEFAULT '=',
  `value` varchar(253) NOT NULL DEFAULT '',
  PRIMARY KEY (`id`),
  KEY `idx_radreply_username` (`username`(32))
) ENGINE=InnoDB AUTO_INCREMENT=25 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `radusergroup` (
  `id` int(11) unsigned NOT NULL AUTO_INCREMENT,
  `username` varchar(64) NOT NULL DEFAULT '',
  `groupname` varchar(64) NOT NULL DEFAULT '',
  `priority` int(11) NOT NULL DEFAULT 1,
  PRIMARY KEY (`id`),
  KEY `idx_radusergroup_username` (`username`(32)),
  KEY `idx_radusergroup_groupname` (`groupname`(32))
) ENGINE=InnoDB AUTO_INCREMENT=6330 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `user_audit_logs` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `user_type` varchar(20) NOT NULL,
  `user_id` int(11) DEFAULT 0,
  `username` varchar(100) NOT NULL,
  `admin_name` varchar(100) DEFAULT 'Admin',
  `action` varchar(50) DEFAULT 'UPDATE_PROFILE',
  `change_details` text DEFAULT NULL,
  `created_at` datetime DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  KEY `idx_user` (`user_type`,`user_id`),
  KEY `idx_username` (`username`)
) ENGINE=InnoDB AUTO_INCREMENT=5 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `wisp_access_points` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `tower_id` int(11) NOT NULL,
  `name` varchar(128) NOT NULL,
  `device_model` varchar(80) DEFAULT 'Ubiquiti Rocket',
  `ip_address` varchar(64) DEFAULT NULL,
  `snmp_community` varchar(64) DEFAULT 'public',
  `interface_name` varchar(64) DEFAULT NULL,
  `frequency_mhz` int(11) DEFAULT 5800,
  `azimuth_deg` int(11) DEFAULT 0,
  `beamwidth_deg` int(11) DEFAULT 90,
  `ssid` varchar(128) DEFAULT NULL,
  `coverage_radius_meters` int(11) DEFAULT 400,
  `status` enum('active','maintenance','offline') DEFAULT 'active',
  `notes` varchar(255) DEFAULT NULL,
  `created_at` timestamp NULL DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  KEY `idx_ap_tower` (`tower_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `wisp_admins` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `username` varchar(64) NOT NULL,
  `password` varchar(255) NOT NULL,
  `full_name` varchar(120) NOT NULL,
  `role` varchar(30) DEFAULT 'superadmin',
  `is_active` tinyint(1) DEFAULT 1,
  `created_at` datetime DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `idx_admin_username` (`username`)
) ENGINE=InnoDB AUTO_INCREMENT=3 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `wisp_audit_logs` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `admin_id` int(11) DEFAULT NULL,
  `username` varchar(64) DEFAULT '',
  `action` varchar(60) NOT NULL,
  `module` varchar(40) NOT NULL,
  `details` text DEFAULT NULL,
  `ip_address` varchar(45) DEFAULT '',
  `created_at` datetime DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  KEY `idx_audit_module` (`module`),
  KEY `idx_audit_action` (`action`)
) ENGINE=InnoDB AUTO_INCREMENT=155 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `wisp_backups` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `filename` varchar(255) NOT NULL,
  `file_path` varchar(255) NOT NULL,
  `file_size_bytes` bigint(20) DEFAULT 0,
  `file_size_mb` decimal(10,2) DEFAULT 0.00,
  `system_version` varchar(20) DEFAULT '2.4.0',
  `checksum_sha256` varchar(64) DEFAULT '',
  `backup_type` varchar(20) DEFAULT 'full',
  `created_by` varchar(64) DEFAULT 'admin',
  `notes` text DEFAULT NULL,
  `created_at` datetime DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `idx_backup_filename` (`filename`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `wisp_card_templates` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `name` varchar(80) NOT NULL,
  `width_mm` int(11) DEFAULT 85,
  `height_mm` int(11) DEFAULT 55,
  `cards_per_row` int(11) DEFAULT 2,
  `background_color` varchar(20) DEFAULT '#ffffff',
  `border_color` varchar(20) DEFAULT '#cbd5e1',
  `header_color` varchar(20) DEFAULT '#1e40af',
  `show_qr` tinyint(1) DEFAULT 1,
  `show_logo` tinyint(1) DEFAULT 1,
  `show_price` tinyint(1) DEFAULT 1,
  `show_validity` tinyint(1) DEFAULT 1,
  `header_title` varchar(100) DEFAULT 'كارت إنترنت فائق السرعة',
  `footer_text` varchar(150) DEFAULT 'نتمنى لكم تجربة تصفح ممتعة',
  `logo_image` varchar(255) DEFAULT '',
  `is_default` tinyint(1) DEFAULT 0,
  `created_at` datetime DEFAULT current_timestamp(),
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `wisp_global_sequence` (
  `seq_id` bigint(20) NOT NULL AUTO_INCREMENT,
  `entity_type` varchar(20) NOT NULL,
  `entity_id` int(11) NOT NULL,
  `created_at` datetime DEFAULT current_timestamp(),
  PRIMARY KEY (`seq_id`),
  UNIQUE KEY `uq_entity` (`entity_type`,`entity_id`)
) ENGINE=InnoDB AUTO_INCREMENT=32864 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `wisp_invoices` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `invoice_number` varchar(40) NOT NULL,
  `subscriber_id` int(11) DEFAULT NULL,
  `subscriber_name` varchar(120) DEFAULT '',
  `package_name` varchar(80) DEFAULT '',
  `amount` decimal(10,2) NOT NULL,
  `status` varchar(20) DEFAULT 'unpaid',
  `issue_date` date DEFAULT NULL,
  `due_date` date DEFAULT NULL,
  `paid_at` datetime DEFAULT NULL,
  `payment_method` varchar(40) DEFAULT 'cash',
  `notes` text DEFAULT NULL,
  `created_at` datetime DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `idx_invoice_num` (`invoice_number`),
  KEY `idx_inv_subscriber` (`subscriber_id`),
  CONSTRAINT `fk_inv_subscriber` FOREIGN KEY (`subscriber_id`) REFERENCES `wisp_subscribers` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB AUTO_INCREMENT=12 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `wisp_l2tp_tunnels` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `name` varchar(80) NOT NULL,
  `username` varchar(64) NOT NULL,
  `password` varchar(64) NOT NULL,
  `tunnel_ip` varchar(45) NOT NULL,
  `radius_secret` varchar(64) NOT NULL DEFAULT '123',
  `ipsec_secret` varchar(64) NOT NULL DEFAULT '',
  `reseller_id` int(11) DEFAULT NULL,
  `status` varchar(20) DEFAULT 'offline',
  `is_enabled` tinyint(1) DEFAULT 1,
  `latency_ms` int(11) DEFAULT 0,
  `last_connected_at` datetime DEFAULT NULL,
  `description` text DEFAULT NULL,
  `created_at` datetime DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `idx_l2tp_username` (`username`),
  UNIQUE KEY `idx_l2tp_ip` (`tunnel_ip`),
  KEY `idx_l2tp_reseller` (`reseller_id`)
) ENGINE=InnoDB AUTO_INCREMENT=4 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `wisp_license_info` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `license_id` varchar(100) NOT NULL,
  `client_name` varchar(255) NOT NULL,
  `plan_tier` varchar(50) DEFAULT 'Enterprise',
  `hardware_id` varchar(100) DEFAULT 'ANY',
  `max_subscribers` int(11) DEFAULT 5000,
  `max_nas` int(11) DEFAULT 15,
  `max_managers` int(11) DEFAULT 10,
  `features_json` longtext DEFAULT NULL,
  `raw_package_json` longtext NOT NULL,
  `token_b64` longtext DEFAULT NULL,
  `signature_b64` varchar(255) NOT NULL,
  `issued_at` varchar(50) DEFAULT NULL,
  `expires_at` varchar(50) DEFAULT NULL,
  `status` varchar(30) DEFAULT 'active',
  `grace_period_until` datetime DEFAULT NULL,
  `last_verified_at` datetime DEFAULT current_timestamp(),
  `last_heartbeat_at` datetime DEFAULT NULL,
  `master_server_url` varchar(255) DEFAULT 'http://127.0.0.1:5095',
  `created_at` datetime DEFAULT current_timestamp(),
  `updated_at` datetime DEFAULT current_timestamp() ON UPDATE current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_license` (`license_id`)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `wisp_manager_invoices` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `invoice_number` varchar(50) NOT NULL,
  `manager_id` int(11) NOT NULL,
  `transaction_type` varchar(30) NOT NULL,
  `amount` decimal(12,2) NOT NULL,
  `payment_type` varchar(30) NOT NULL DEFAULT 'cash',
  `balance_before` decimal(12,2) NOT NULL DEFAULT 0.00,
  `balance_after` decimal(12,2) NOT NULL DEFAULT 0.00,
  `notes` text DEFAULT NULL,
  `is_voided` tinyint(1) DEFAULT 0,
  `void_reason` text DEFAULT NULL,
  `created_by` varchar(64) DEFAULT 'admin',
  `created_at` datetime DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `idx_mgr_inv_number` (`invoice_number`),
  KEY `idx_mgr_inv_manager` (`manager_id`),
  KEY `idx_mgr_inv_type` (`transaction_type`),
  KEY `idx_mgr_inv_payment` (`payment_type`),
  KEY `idx_mgr_inv_created` (`created_at`),
  CONSTRAINT `fk_mgr_inv_manager` FOREIGN KEY (`manager_id`) REFERENCES `wisp_managers` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `wisp_managers` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `username` varchar(64) NOT NULL,
  `password_hash` varchar(255) NOT NULL,
  `full_name` varchar(120) NOT NULL,
  `phone` varchar(30) DEFAULT '',
  `email` varchar(120) DEFAULT '',
  `role_id` int(11) NOT NULL,
  `wallet_balance` decimal(12,2) DEFAULT 0.00,
  `credit_limit` decimal(12,2) DEFAULT 0.00,
  `commission_percent` decimal(5,2) DEFAULT 0.00,
  `allowed_packages` text DEFAULT NULL,
  `is_active` tinyint(1) DEFAULT 1,
  `is_deleted` tinyint(1) DEFAULT 0,
  `notes` text DEFAULT NULL,
  `last_login_at` datetime DEFAULT NULL,
  `created_at` datetime DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `idx_manager_username` (`username`),
  KEY `idx_mgr_role` (`role_id`),
  CONSTRAINT `fk_mgr_role` FOREIGN KEY (`role_id`) REFERENCES `wisp_roles` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=5 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `wisp_nas_devices` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `name` varchar(80) NOT NULL,
  `ip_address` varchar(45) NOT NULL,
  `nas_type` varchar(30) DEFAULT 'mikrotik',
  `secret` varchar(64) NOT NULL,
  `api_port` int(11) DEFAULT 8728,
  `coa_port` int(11) DEFAULT 3799,
  `api_username` varchar(64) DEFAULT 'admin',
  `api_password` varchar(64) DEFAULT '',
  `hotspot_login_url` varchar(255) DEFAULT 'http://192.168.88.1/login',
  `status` varchar(20) DEFAULT 'online',
  `latency_ms` int(11) DEFAULT 0,
  `description` text DEFAULT NULL,
  `created_at` datetime DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `idx_nas_ip` (`ip_address`)
) ENGINE=InnoDB AUTO_INCREMENT=8 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `wisp_packages` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `name` varchar(80) NOT NULL,
  `service_type` varchar(20) DEFAULT 'hotspot',
  `price` decimal(10,2) DEFAULT 0.00,
  `cost` decimal(10,2) DEFAULT 0.00,
  `rate_download` varchar(20) DEFAULT '2M',
  `rate_upload` varchar(20) DEFAULT '1M',
  `burst_download` varchar(20) DEFAULT '4M',
  `burst_upload` varchar(20) DEFAULT '2M',
  `burst_threshold_down` varchar(20) DEFAULT '1500k',
  `burst_threshold_up` varchar(20) DEFAULT '750k',
  `burst_time` int(11) DEFAULT 16,
  `priority` int(11) DEFAULT 8,
  `min_download` varchar(20) DEFAULT '512k',
  `min_upload` varchar(20) DEFAULT '256k',
  `volume_quota_mb` bigint(20) DEFAULT 0,
  `uptime_limit_mins` int(11) DEFAULT 0,
  `validity_days` int(11) DEFAULT 30,
  `validity_value` int(11) DEFAULT 30,
  `validity_unit` varchar(20) DEFAULT 'days',
  `mikrotik_group` varchar(100) DEFAULT '',
  `simultaneous_sessions` int(11) DEFAULT 1,
  `is_active` tinyint(1) DEFAULT 1,
  `show_in_portal` tinyint(1) DEFAULT 1,
  `is_rollover_enabled` tinyint(1) DEFAULT 0,
  `description` text DEFAULT NULL,
  `created_at` datetime DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `idx_pkg_name` (`name`)
) ENGINE=InnoDB AUTO_INCREMENT=12 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `wisp_permissions` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `code` varchar(80) NOT NULL,
  `name` varchar(120) NOT NULL,
  `category` varchar(60) NOT NULL,
  `description` varchar(255) DEFAULT '',
  `created_at` datetime DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `idx_perm_code` (`code`),
  KEY `idx_perm_category` (`category`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `wisp_reseller_transactions` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `reseller_id` int(11) NOT NULL,
  `type` varchar(20) NOT NULL,
  `amount` decimal(10,2) NOT NULL,
  `balance_after` decimal(10,2) NOT NULL,
  `description` text DEFAULT NULL,
  `reference_id` varchar(64) DEFAULT '',
  `created_at` datetime DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  KEY `idx_reseller_id` (`reseller_id`),
  CONSTRAINT `fk_trans_reseller` FOREIGN KEY (`reseller_id`) REFERENCES `wisp_resellers` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `wisp_resellers` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `name` varchar(120) NOT NULL,
  `contact_person` varchar(100) DEFAULT '',
  `phone` varchar(30) DEFAULT '',
  `email` varchar(120) DEFAULT '',
  `balance` decimal(10,2) DEFAULT 0.00,
  `commission_percent` decimal(5,2) DEFAULT 10.00,
  `allowed_packages` text DEFAULT NULL,
  `status` varchar(20) DEFAULT 'active',
  `created_at` datetime DEFAULT current_timestamp(),
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `wisp_revoked_licenses` (
  `license_id` varchar(100) NOT NULL,
  `reason` text DEFAULT NULL,
  `revoked_at` datetime DEFAULT current_timestamp(),
  PRIMARY KEY (`license_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `wisp_role_permissions` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `role_id` int(11) NOT NULL,
  `permission_id` int(11) NOT NULL,
  `created_at` datetime DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `idx_role_perm` (`role_id`,`permission_id`),
  KEY `idx_rp_role` (`role_id`),
  KEY `idx_rp_perm` (`permission_id`),
  CONSTRAINT `fk_rp_perm` FOREIGN KEY (`permission_id`) REFERENCES `wisp_permissions` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_rp_role` FOREIGN KEY (`role_id`) REFERENCES `wisp_roles` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `wisp_roles` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `name` varchar(80) NOT NULL,
  `code` varchar(50) NOT NULL,
  `description` varchar(255) DEFAULT '',
  `is_system` tinyint(1) DEFAULT 0,
  `created_at` datetime DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `idx_role_code` (`code`),
  UNIQUE KEY `idx_role_name` (`name`)
) ENGINE=InnoDB AUTO_INCREMENT=5 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `wisp_sstp_tunnels` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `name` varchar(80) NOT NULL,
  `username` varchar(64) NOT NULL,
  `password` varchar(64) NOT NULL,
  `tunnel_ip` varchar(45) NOT NULL,
  `radius_secret` varchar(64) NOT NULL DEFAULT '123',
  `reseller_id` int(11) DEFAULT NULL,
  `status` varchar(20) DEFAULT 'offline',
  `is_enabled` tinyint(1) DEFAULT 1,
  `latency_ms` int(11) DEFAULT 0,
  `last_connected_at` datetime DEFAULT NULL,
  `description` text DEFAULT NULL,
  `created_at` datetime DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `username` (`username`),
  UNIQUE KEY `tunnel_ip` (`tunnel_ip`),
  KEY `idx_sstp_reseller` (`reseller_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `wisp_subscribers` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `global_seq_id` bigint(20) DEFAULT NULL,
  `username` varchar(64) NOT NULL,
  `password` varchar(64) NOT NULL,
  `full_name` varchar(120) NOT NULL,
  `phone` varchar(30) DEFAULT '',
  `email` varchar(120) DEFAULT '',
  `national_id` varchar(50) DEFAULT '',
  `address` text DEFAULT NULL,
  `service_type` varchar(20) DEFAULT 'pppoe',
  `package_id` int(11) NOT NULL,
  `mac_binding` varchar(30) DEFAULT '',
  `static_ip` varchar(45) DEFAULT '',
  `status` varchar(20) DEFAULT 'active',
  `balance` decimal(10,2) DEFAULT 0.00,
  `extra_quota_mb` bigint(20) DEFAULT 0,
  `loan_balance_mb` int(11) DEFAULT 0,
  `loan_status` tinyint(1) DEFAULT 0,
  `first_used_at` datetime DEFAULT NULL,
  `expires_at` datetime DEFAULT NULL,
  `last_renewed_at` datetime DEFAULT current_timestamp(),
  `notes` text DEFAULT NULL,
  `created_at` datetime DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `idx_subscriber_username` (`username`),
  KEY `idx_subs_pkg` (`package_id`),
  KEY `idx_sub_status_id` (`status`,`id` DESC),
  CONSTRAINT `fk_subs_package` FOREIGN KEY (`package_id`) REFERENCES `wisp_packages` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=60 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `wisp_system_alerts` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `alert_type` varchar(50) NOT NULL,
  `severity` varchar(20) DEFAULT 'warning',
  `source` varchar(50) DEFAULT 'watchdog',
  `message` text NOT NULL,
  `is_resolved` tinyint(1) DEFAULT 0,
  `created_at` datetime DEFAULT current_timestamp(),
  `resolved_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_alert_type` (`alert_type`),
  KEY `idx_alert_resolved` (`is_resolved`),
  KEY `idx_alert_created` (`created_at`)
) ENGINE=InnoDB AUTO_INCREMENT=10 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `wisp_system_settings` (
  `key` varchar(64) NOT NULL,
  `value` text DEFAULT NULL,
  `description` varchar(255) DEFAULT '',
  PRIMARY KEY (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `wisp_tower_locations` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `nas_id` int(11) DEFAULT NULL,
  `nas_ip` varchar(64) DEFAULT NULL,
  `tower_name` varchar(128) NOT NULL,
  `latitude` decimal(10,8) NOT NULL DEFAULT 15.36944500,
  `longitude` decimal(11,8) NOT NULL DEFAULT 44.19100600,
  `coverage_radius_meters` int(11) DEFAULT 500,
  `tower_type` enum('hotspot','fiber_olt','pppoe_tower','relay','backhaul') DEFAULT 'hotspot',
  `notes` varchar(255) DEFAULT NULL,
  `updated_at` timestamp NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
  PRIMARY KEY (`id`),
  KEY `idx_tower_nas` (`nas_id`),
  KEY `idx_tower_ip` (`nas_ip`)
) ENGINE=InnoDB AUTO_INCREMENT=8 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `wisp_voucher_batches` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `batch_number` varchar(40) NOT NULL,
  `name` varchar(100) NOT NULL,
  `package_id` int(11) NOT NULL,
  `reseller_id` int(11) DEFAULT NULL,
  `card_count` int(11) NOT NULL,
  `prefix` varchar(10) DEFAULT '',
  `pin_only` tinyint(1) DEFAULT 1,
  `char_type` varchar(20) DEFAULT 'numbers',
  `code_length` int(11) DEFAULT 8,
  `price` decimal(10,2) DEFAULT 0.00,
  `cost` decimal(10,2) DEFAULT 0.00,
  `volume_quota_mb` bigint(20) DEFAULT 0,
  `uptime_limit_mins` int(11) DEFAULT 0,
  `validity_value` int(11) DEFAULT 30,
  `validity_unit` varchar(20) DEFAULT 'days',
  `validity_days` int(11) DEFAULT 30,
  `rate_download` varchar(50) DEFAULT '0',
  `rate_upload` varchar(50) DEFAULT '0',
  `rate_limit_str` varchar(100) DEFAULT '0/0',
  `simultaneous_sessions` int(11) DEFAULT 1,
  `mikrotik_group` varchar(100) DEFAULT 'ALL-SPEED',
  `created_by` varchar(64) DEFAULT 'admin',
  `created_at` datetime DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `idx_batch_number` (`batch_number`),
  KEY `idx_batch_package` (`package_id`),
  KEY `idx_batch_reseller` (`reseller_id`),
  CONSTRAINT `fk_batch_pkg` FOREIGN KEY (`package_id`) REFERENCES `wisp_packages` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=78 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `wisp_voucher_sales` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `voucher_id` int(11) NOT NULL,
  `batch_id` int(11) NOT NULL,
  `batch_name` varchar(100) DEFAULT '',
  `username` varchar(64) NOT NULL,
  `serial_number` varchar(30) DEFAULT '',
  `package_name` varchar(80) NOT NULL,
  `price` decimal(10,2) NOT NULL,
  `cost` decimal(10,2) DEFAULT 0.00,
  `reseller_id` int(11) DEFAULT NULL,
  `activated_at` datetime DEFAULT current_timestamp(),
  `created_at` datetime DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  KEY `idx_sales_voucher` (`voucher_id`),
  KEY `idx_sales_batch` (`batch_id`),
  KEY `idx_sales_activated` (`activated_at`),
  CONSTRAINT `fk_sales_voucher` FOREIGN KEY (`voucher_id`) REFERENCES `wisp_vouchers` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=36893 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `wisp_vouchers` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `global_seq_id` bigint(20) DEFAULT NULL,
  `batch_id` int(11) NOT NULL,
  `package_id` int(11) NOT NULL,
  `reseller_id` int(11) DEFAULT NULL,
  `serial_number` varchar(30) NOT NULL,
  `username` varchar(64) NOT NULL,
  `password` varchar(64) NOT NULL,
  `pin_code` varchar(32) NOT NULL,
  `status` varchar(20) DEFAULT 'unused',
  `first_used_at` datetime DEFAULT NULL,
  `expires_at` datetime DEFAULT NULL,
  `last_renewed_at` datetime DEFAULT current_timestamp(),
  `bound_mac` varchar(50) DEFAULT NULL,
  `extra_quota_mb` bigint(20) DEFAULT 0,
  `expire_reason` varchar(60) DEFAULT '',
  `snap_price` decimal(10,2) DEFAULT 0.00,
  `snap_cost` decimal(10,2) DEFAULT 0.00,
  `snap_volume_quota_mb` bigint(20) DEFAULT 0,
  `snap_uptime_limit_mins` int(11) DEFAULT 0,
  `snap_validity_value` int(11) DEFAULT 30,
  `snap_validity_unit` varchar(20) DEFAULT 'days',
  `snap_validity_days` int(11) DEFAULT 30,
  `snap_rate_download` varchar(50) DEFAULT '0',
  `snap_rate_upload` varchar(50) DEFAULT '0',
  `snap_rate_limit_str` varchar(100) DEFAULT '0/0',
  `snap_simultaneous_sessions` int(11) DEFAULT 1,
  `snap_mikrotik_group` varchar(100) DEFAULT 'ALL-SPEED',
  `created_at` datetime DEFAULT current_timestamp(),
  `balance` decimal(10,2) DEFAULT 0.00,
  PRIMARY KEY (`id`),
  UNIQUE KEY `idx_voucher_serial` (`serial_number`),
  UNIQUE KEY `idx_voucher_username` (`username`),
  KEY `idx_voucher_batch` (`batch_id`),
  KEY `idx_voucher_package` (`package_id`),
  KEY `idx_voucher_status` (`status`),
  KEY `idx_voucher_status_id` (`status`,`id` DESC),
  CONSTRAINT `fk_voucher_batch` FOREIGN KEY (`batch_id`) REFERENCES `wisp_voucher_batches` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_voucher_pkg` FOREIGN KEY (`package_id`) REFERENCES `wisp_packages` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=28605 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `wisp_whatsapp_logs` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  `recipient_phone` varchar(30) NOT NULL,
  `message_type` varchar(30) DEFAULT 'text',
  `direction` enum('inbound','outbound') DEFAULT 'outbound',
  `message_body` text NOT NULL,
  `status` enum('pending','sent','delivered','read','failed') DEFAULT 'sent',
  `error_message` text DEFAULT NULL,
  `entity_type` varchar(30) DEFAULT NULL,
  `entity_id` int(11) DEFAULT NULL,
  `created_at` datetime DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  KEY `idx_phone` (`recipient_phone`),
  KEY `idx_status` (`status`),
  KEY `idx_created` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `wisp_whatsapp_settings` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `gateway_provider` varchar(40) DEFAULT 'simulator',
  `api_endpoint` varchar(255) DEFAULT 'http://localhost:8080',
  `api_key` varchar(255) DEFAULT '',
  `instance_name` varchar(80) DEFAULT 'max_radius_bot',
  `phone_number` varchar(40) DEFAULT '',
  `is_bot_enabled` tinyint(1) DEFAULT 1,
  `is_notifications_enabled` tinyint(1) DEFAULT 1,
  `meta_app_id` varchar(80) DEFAULT '',
  `meta_phone_number_id` varchar(80) DEFAULT '',
  `meta_access_token` text DEFAULT NULL,
  `meta_webhook_verify_token` varchar(120) DEFAULT 'max_radius_whatsapp_token_2026',
  `status` varchar(30) DEFAULT 'disconnected',
  `qr_code_raw` mediumtext DEFAULT NULL,
  `last_connected_at` datetime DEFAULT NULL,
  `updated_at` datetime DEFAULT current_timestamp() ON UPDATE current_timestamp(),
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `wisp_whatsapp_templates` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `template_key` varchar(60) NOT NULL,
  `title` varchar(100) NOT NULL,
  `category` varchar(40) DEFAULT 'notification',
  `message_body` text NOT NULL,
  `is_active` tinyint(1) DEFAULT 1,
  `variables_hint` varchar(255) DEFAULT '',
  `updated_at` datetime DEFAULT current_timestamp() ON UPDATE current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `template_key` (`template_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `wisp_wireguard_tunnels` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `name` varchar(64) NOT NULL,
  `public_key` varchar(64) NOT NULL,
  `private_key` varchar(64) NOT NULL,
  `preshared_key` varchar(64) DEFAULT NULL,
  `tunnel_ip` varchar(15) NOT NULL,
  `radius_secret` varchar(64) DEFAULT 'max123',
  `listen_port` int(11) DEFAULT 13231,
  `status` enum('online','offline') DEFAULT 'offline',
  `is_enabled` tinyint(1) DEFAULT 1,
  `latency_ms` int(11) DEFAULT 0,
  `rx_bytes` bigint(20) DEFAULT 0,
  `tx_bytes` bigint(20) DEFAULT 0,
  `last_handshake_at` datetime DEFAULT NULL,
  `description` varchar(255) DEFAULT NULL,
  `created_at` datetime DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `tunnel_ip` (`tunnel_ip`)
) ENGINE=InnoDB AUTO_INCREMENT=5 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*M!999999\- enable the sandbox mode */ 
INSERT INTO `wisp_roles` VALUES
(1,'المدير العام (Super Admin)','superadmin','صلاحيات كاملة وغير مقيدة على كافة أقسام النظام والخدمات',1,'2026-09-24 05:49:18'),
(2,'مدير فرع / شبكة (Manager)','manager','إدارة المشتركين، الكروت، الباقات، ومراقبة الراوترات',0,'2026-09-24 05:49:18'),
(3,'موزع رئيسي (Main Reseller)','main_reseller','توليد الكروت، شحن نقاط البيع، واستعراض مبيعاته وفواتيره',0,'2026-09-24 05:49:18'),
(4,'نقطة بيع (POS Agent)','pos_agent','استعراض الكروت المخصصة له، تفعيل الاشتراكات، وشحن أرصدة المستخدمين',0,'2026-09-24 05:49:18');
INSERT INTO `wisp_system_settings` VALUES
('address','المركز الرئيسي لخدمات الإنترنت','العنوان والمقر'),
('allow_data_loan','0','تفعيل أو تعطيل ميزة سلفة البيانات للمشتركين'),
('company_name','شبكة ماكس نت الاسلكية','اسم الشركة'),
('currency','YER','رمز كود العملة'),
('currency_symbol','ر.ي','رمز العملة المعروض'),
('default_coa_port','3799','إعداد نظام'),
('enable_user_portal','1',''),
('hotspot_domain','wifi.maxradius.net','نطاق صفحة تسجيل الدخول'),
('hotspot_folder_name','max-radius',''),
('hotspot_server_host','199.247.3.47',''),
('hotspot_total_download','150M',''),
('hotspot_total_upload','75M',''),
('isp_name','شبكة ماكس نت الاسلكية','اسم مزود الخدمة'),
('license_monotonic_timestamp','1790376518.9039583',''),
('loan_amount_mb','1024','حجم سلفة البيانات بالميجابايت الافتراضية'),
('loan_threshold_mb','100','حد الرصيد المتبقي بالميجابايت الذي يسمح بطلب السلفة عنده'),
('network_logo','logo_20260924_214947.png','مسار شعار الشبكة'),
('network_name','شبكة ماكس نت الاسلكية','اسم الشبكة أو مزود الخدمة'),
('portal_about_text','نبذة عن شبكة ماكس نت','إعداد نظام'),
('portal_allow_package_change','1','إعداد نظام'),
('portal_allow_password_change','0','إعداد نظام'),
('portal_allow_registration','1','إعداد نظام'),
('portal_default_theme','dark','إعداد نظام'),
('portal_enable_speed_selector','1',''),
('portal_login_username_only','1','إعداد نظام'),
('portal_network_subtitle','نقطة بث واي فاي فائقة السرعة','إعداد نظام'),
('portal_parent_queue','','إعداد نظام'),
('portal_show_network_tab','1','إعداد نظام'),
('portal_show_pricing','1','إعداد نظام'),
('portal_show_speedtest','1','إعداد نظام'),
('portal_show_support_tab','1','إعداد نظام'),
('portal_speed_balanced','10M','إعداد نظام'),
('portal_speed_eco','4M','إعداد نظام'),
('portal_speed_options','[{\"badge\":\"4Mbps\",\"color\":\"emerald\",\"description\":\"توفير البيانات\",\"icon\":\"fa-solid fa-leaf\",\"id\":\"eco\",\"is_default\":false,\"is_open\":false,\"name\":\"سرعه اقتصادية\",\"rate_down\":\"4M\",\"rate_up\":\"4M\",\"theme\":{\"active_bg\":\"rgba(16, 185, 129, 0.18)\",\"active_border\":\"#10b981\",\"badge_bg\":\"rgba(16, 185, 129, 0.22)\",\"badge_text\":\"#34d399\",\"bg\":\"rgba(16, 185, 129, 0.12)\",\"border\":\"rgba(16, 185, 129, 0.35)\",\"glow\":\"rgba(16, 185, 129, 0.35)\",\"hex\":\"#10b981\",\"text\":\"#34d399\"}},{\"badge\":\"10Mbps\",\"color\":\"sky\",\"description\":\"يوتيوب و HD\",\"icon\":\"fa-solid fa-play\",\"id\":\"balanced\",\"is_default\":false,\"is_open\":false,\"name\":\"متوازن\",\"rate_down\":\"10M\",\"rate_up\":\"10M\",\"theme\":{\"active_bg\":\"rgba(14, 165, 233, 0.18)\",\"active_border\":\"#0ea5e9\",\"badge_bg\":\"rgba(14, 165, 233, 0.22)\",\"badge_text\":\"#38bdf8\",\"bg\":\"rgba(14, 165, 233, 0.12)\",\"border\":\"rgba(14, 165, 233, 0.35)\",\"glow\":\"rgba(14, 165, 233, 0.35)\",\"hex\":\"#0ea5e9\",\"text\":\"#38bdf8\"}},{\"badge\":\"25Mbps\",\"color\":\"purple\",\"description\":\"بنج منخفض\",\"icon\":\"fa-solid fa-gamepad\",\"id\":\"turbo\",\"is_default\":false,\"is_open\":false,\"name\":\"ألعاب فائقة\",\"rate_down\":\"25M\",\"rate_up\":\"25M\",\"theme\":{\"active_bg\":\"rgba(168, 85, 247, 0.18)\",\"active_border\":\"#a855f7\",\"badge_bg\":\"rgba(168, 85, 247, 0.22)\",\"badge_text\":\"#c084fc\",\"bg\":\"rgba(168, 85, 247, 0.12)\",\"border\":\"rgba(168, 85, 247, 0.35)\",\"glow\":\"rgba(168, 85, 247, 0.35)\",\"hex\":\"#a855f7\",\"text\":\"#c084fc\"}},{\"badge\":\"أقصى سرعة\",\"color\":\"amber\",\"description\":\"أقصى سرعة بدون تحديد\",\"icon\":\"fa-solid fa-bolt-lightning\",\"id\":\"open\",\"is_default\":true,\"is_open\":true,\"name\":\"سرعة مفتوحة\",\"rate_down\":\"0\",\"rate_up\":\"0\",\"theme\":{\"active_bg\":\"rgba(245, 158, 11, 0.18)\",\"active_border\":\"#f59e0b\",\"badge_bg\":\"rgba(245, 158, 11, 0.22)\",\"badge_text\":\"#fbbf24\",\"bg\":\"rgba(245, 158, 11, 0.12)\",\"border\":\"rgba(245, 158, 11, 0.35)\",\"glow\":\"rgba(245, 158, 11, 0.35)\",\"hex\":\"#f59e0b\",\"text\":\"#fbbf24\"}}]','إعداد نظام'),
('portal_speed_turbo','25M','إعداد نظام'),
('portal_support_phone','773570053','إعداد نظام'),
('portal_welcome_title','بوابة المصادقة الذكية','إعداد نظام'),
('portal_whatsapp','967773570053','إعداد نظام'),
('sms_api_key','','إعداد نظام'),
('sms_gateway_url','','إعداد نظام'),
('sms_sender_id','MAX-RADIUS','اسم مرسل الرسائل'),
('support_email','support@max-radius.net','البريد الإلكتروني للدعم الفني'),
('support_phone','777366226','رقم هاتف الدعم الفني'),
('system_title','MAX RADIUS - نظام إدارة الشبكات والفوترة و FreeRADIUS','عنوان النظام'),
('timezone','Asia/Aden','المنطقة الزمنية للنظام');

-- Initial Default Super Admin Seed
INSERT INTO `wisp_managers` (`id`, `username`, `password_hash`, `full_name`, `phone`, `email`, `role_id`, `wallet_balance`, `credit_limit`, `commission_percent`, `allowed_packages`, `is_active`, `is_deleted`, `notes`, `created_at`) 
VALUES (1, 'admin', 'admin', 'مدير النظام الرئيسي', '+967 770 000 000', 'admin@max-net.net', 1, 0.00, 0.00, 0.00, NULL, 1, 0, 'حساب المدير العام الأساسي للمنظومة', NOW())
ON DUPLICATE KEY UPDATE `is_active` = 1;

SET FOREIGN_KEY_CHECKS=1;

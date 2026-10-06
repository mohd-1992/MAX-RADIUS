-- Migration: 20261005_active_sessions_licensing.sql
-- Goal: Upgrade licensing system from stored cards/subscribers count to concurrent active sessions capacity.

-- 1. Ensure max_active_sessions column in wisp_license_info
SET @col_exists = (
    SELECT COUNT(*) FROM information_schema.COLUMNS 
    WHERE TABLE_SCHEMA = DATABASE() 
      AND TABLE_NAME = 'wisp_license_info' 
      AND COLUMN_NAME = 'max_active_sessions'
);

SET @stmt = IF(@col_exists = 0, 
    'ALTER TABLE `wisp_license_info` ADD COLUMN `max_active_sessions` INT DEFAULT 0 AFTER `max_subscribers`', 
    'SELECT 1'
);
PREPARE stmt FROM @stmt;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- 2. Ensure radpostauth has nasipaddress and callingstationid
SET @col_exists = (
    SELECT COUNT(*) FROM information_schema.COLUMNS 
    WHERE TABLE_SCHEMA = DATABASE() 
      AND TABLE_NAME = 'radpostauth' 
      AND COLUMN_NAME = 'nasipaddress'
);
SET @stmt = IF(@col_exists = 0, 
    'ALTER TABLE `radpostauth` ADD COLUMN `nasipaddress` VARCHAR(45) DEFAULT NULL', 
    'SELECT 1'
);
PREPARE stmt FROM @stmt;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @col_exists = (
    SELECT COUNT(*) FROM information_schema.COLUMNS 
    WHERE TABLE_SCHEMA = DATABASE() 
      AND TABLE_NAME = 'radpostauth' 
      AND COLUMN_NAME = 'callingstationid'
);
SET @stmt = IF(@col_exists = 0, 
    'ALTER TABLE `radpostauth` ADD COLUMN `callingstationid` VARCHAR(50) DEFAULT NULL', 
    'SELECT 1'
);
PREPARE stmt FROM @stmt;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- 3. Create wisp_session_reservations table
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

ALTER TABLE wisp_session_reservations ADD COLUMN IF NOT EXISTS radacctid BIGINT NULL;
ALTER TABLE wisp_session_reservations ADD COLUMN IF NOT EXISTS nasportid VARCHAR(50) NOT NULL DEFAULT '';



CREATE TABLE IF NOT EXISTS wisp_license_runtime_state (
 id INT PRIMARY KEY, license_row_id INT NULL, package_hash CHAR(64) NULL,
 is_valid TINYINT NOT NULL DEFAULT 0, license_mode VARCHAR(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'unknown',
 max_active_sessions INT NULL, valid_until DATETIME NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT IGNORE INTO wisp_license_runtime_state(id) VALUES(1);
UPDATE wisp_license_runtime_state SET is_valid=0,valid_until=NULL WHERE id=1;
DROP TRIGGER IF EXISTS trg_radpostauth_reserve_capacity;
DELIMITER //
CREATE OR REPLACE FUNCTION fn_check_license_auth(p_username VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci, p_nasip VARCHAR(45) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci, p_station VARCHAR(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci)
RETURNS VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci READS SQL DATA
BEGIN
 DECLARE ok_count INT DEFAULT 0;
 SELECT COUNT(*) INTO ok_count FROM wisp_license_runtime_state s
 JOIN wisp_license_info l ON l.id=s.license_row_id
 WHERE s.id=1 AND s.is_valid=1 AND s.valid_until>UTC_TIMESTAMP()
 AND s.package_hash=SHA2(l.raw_package_json,256)
 AND s.license_mode IN ('active_sessions','legacy_subscribers')
 AND l.status IN ('active','over_quota')
 AND NOT EXISTS (SELECT 1 FROM wisp_revoked_licenses b WHERE b.license_id=l.license_id);
 IF ok_count=0 THEN RETURN 'License verification unavailable or license invalid'; END IF;
 RETURN NULL;
END
//
CREATE OR REPLACE FUNCTION fn_reserve_license_slot(p_key CHAR(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci, p_username VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci,
 p_nasip VARCHAR(45) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci, p_station VARCHAR(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci, p_port VARCHAR(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci, p_source VARCHAR(45) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci)
RETURNS INT MODIFIES SQL DATA
BEGIN
 DECLARE cap INT DEFAULT NULL;
 DECLARE mode_name VARCHAR(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
 DECLARE used_slots INT DEFAULT 0;
 DECLARE existing_slot INT DEFAULT 0;
 DECLARE EXIT HANDLER FOR SQLEXCEPTION RETURN -1;
 IF p_username='healthcheck' AND p_source IN ('127.0.0.1','::1') THEN RETURN 1; END IF;
 IF p_key IS NULL OR LENGTH(p_key)<>32 THEN RETURN -1; END IF;
 -- The UPDATE holds this row lock until the SELECT statement transaction commits.
 UPDATE wisp_license_runtime_state SET id=id WHERE id=1;
 IF fn_check_license_auth(p_username,p_nasip,p_station) IS NOT NULL THEN RETURN -1; END IF;
 SELECT max_active_sessions,license_mode INTO cap,mode_name FROM wisp_license_runtime_state WHERE id=1;
 IF mode_name='legacy_subscribers' THEN RETURN 1; END IF;
 IF cap IS NULL OR cap<0 THEN RETURN -1; END IF;
 IF cap=0 THEN RETURN 1; END IF;
 SELECT COUNT(*) INTO existing_slot FROM wisp_session_reservations r
 LEFT JOIN radacct a ON a.radacctid=r.radacctid
 WHERE r.session_key=p_key AND r.username=p_username AND r.nasipaddress=p_nasip
 AND ((r.radacctid IS NULL AND r.expires_at>NOW()) OR
 (a.acctstoptime IS NULL AND COALESCE(a.acctupdatetime,a.acctstarttime)>=NOW()-INTERVAL 5 MINUTE));
 IF existing_slot>0 THEN RETURN 1; END IF;
 SELECT (SELECT COUNT(*) FROM radacct WHERE acctstoptime IS NULL AND COALESCE(acctupdatetime,acctstarttime) >= NOW() - INTERVAL 5 MINUTE)
 +(SELECT COUNT(*) FROM wisp_session_reservations WHERE radacctid IS NULL AND expires_at > NOW())
 INTO used_slots;
 IF used_slots>=cap THEN RETURN 0; END IF;
 INSERT INTO wisp_session_reservations(session_key,username,nasipaddress,callingstationid,nasportid,reserved_at,expires_at,radacctid)
 VALUES(p_key,p_username,p_nasip,COALESCE(p_station,''),COALESCE(p_port,''),NOW(),NOW()+INTERVAL 5 MINUTE,NULL)
 ON DUPLICATE KEY UPDATE reserved_at=NOW(),expires_at=NOW()+INTERVAL 5 MINUTE,radacctid=NULL;
 RETURN 1;
END
//
DROP TRIGGER IF EXISTS trg_radacct_release_reservation_insert
//
CREATE TRIGGER trg_radacct_release_reservation_insert AFTER INSERT ON radacct FOR EACH ROW
BEGIN
 DECLARE token_key VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL;
  IF NEW.class LIKE 'maxcap:%' THEN
   SET token_key=SUBSTRING(NEW.class,8);
  ELSE
   -- NAS that omit Class: transfer ONE pending seat, never every matching seat.
   SELECT session_key INTO token_key FROM wisp_session_reservations
   WHERE radacctid IS NULL AND username=NEW.username
   AND nasipaddress=NEW.nasipaddress
   AND ((NEW.callingstationid<>'' AND callingstationid=NEW.callingstationid)
    OR ((NEW.callingstationid IS NULL OR NEW.callingstationid='') AND nasportid=COALESCE(NEW.nasportid,'')))
   ORDER BY reserved_at,id LIMIT 1;
  END IF;
 IF NEW.acctstoptime IS NULL THEN
  UPDATE wisp_session_reservations SET radacctid=NEW.radacctid
  WHERE session_key=token_key AND username=NEW.username AND nasipaddress=NEW.nasipaddress AND radacctid IS NULL;
 ELSEIF COALESCE(NEW.acctterminatecause,'') NOT IN ('Stale-Session-Timeout','Watchdog-Autoheal-Timeout','Backup-Restored-Closed') THEN
  DELETE FROM wisp_session_reservations WHERE session_key=token_key AND username=NEW.username
  AND nasipaddress=NEW.nasipaddress AND (radacctid IS NULL OR radacctid=NEW.radacctid);
 END IF;
END
//
DROP TRIGGER IF EXISTS trg_radacct_release_reservation_update
//
CREATE TRIGGER trg_radacct_release_reservation_update AFTER UPDATE ON radacct FOR EACH ROW
BEGIN
 IF NEW.acctstoptime IS NOT NULL AND COALESCE(NEW.acctterminatecause,'') NOT IN
 ('Stale-Session-Timeout','Watchdog-Autoheal-Timeout','Backup-Restored-Closed') THEN
  DELETE FROM wisp_session_reservations WHERE radacctid=NEW.radacctid;
 END IF;
END
//
CREATE OR REPLACE TRIGGER trg_license_limit_max_nas BEFORE INSERT ON wisp_nas_devices FOR EACH ROW
BEGIN
 DECLARE resource_limit INT DEFAULT NULL;
 DECLARE row_count INT DEFAULT 0;
 DECLARE licensed_rows INT DEFAULT 0;
 UPDATE wisp_license_runtime_state SET id=id WHERE id=1;
 SELECT COUNT(*) INTO licensed_rows FROM wisp_license_info;
 IF licensed_rows>0 THEN
  IF fn_check_license_auth('','','') IS NOT NULL THEN
   SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Verified license state is unavailable';
  END IF;
  SELECT COALESCE(CAST(JSON_UNQUOTE(JSON_EXTRACT(l.raw_package_json,'$.payload.limits.max_nas')) AS SIGNED),15)
  INTO resource_limit FROM wisp_license_runtime_state s JOIN wisp_license_info l ON l.id=s.license_row_id WHERE s.id=1;
  SELECT COUNT(*) INTO row_count FROM wisp_nas_devices;
  IF resource_limit IS NULL OR resource_limit<0 OR (resource_limit>0 AND row_count>=resource_limit) THEN
   SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='License resource capacity reached';
  END IF;
 END IF;
END
//
CREATE OR REPLACE TRIGGER trg_license_limit_max_managers BEFORE INSERT ON wisp_managers FOR EACH ROW
BEGIN
 DECLARE resource_limit INT DEFAULT NULL;
 DECLARE row_count INT DEFAULT 0;
 DECLARE licensed_rows INT DEFAULT 0;
 UPDATE wisp_license_runtime_state SET id=id WHERE id=1;
 SELECT COUNT(*) INTO licensed_rows FROM wisp_license_info;
 IF licensed_rows>0 THEN
  IF fn_check_license_auth('','','') IS NOT NULL THEN
   SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Verified license state is unavailable';
  END IF;
  SELECT COALESCE(CAST(JSON_UNQUOTE(JSON_EXTRACT(l.raw_package_json,'$.payload.limits.max_managers')) AS SIGNED),10)
  INTO resource_limit FROM wisp_license_runtime_state s JOIN wisp_license_info l ON l.id=s.license_row_id WHERE s.id=1;
  SELECT COUNT(*) INTO row_count FROM wisp_managers;
  IF resource_limit IS NULL OR resource_limit<0 OR (resource_limit>0 AND row_count>=resource_limit) THEN
   SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='License resource capacity reached';
  END IF;
 END IF;
END
//
DELIMITER ;

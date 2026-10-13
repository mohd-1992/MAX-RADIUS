-- Phase 1. MariaDB only: take a backup; Python installer is preferred.
DELIMITER ;;
ALTER TABLE wisp_vouchers ADD COLUMN IF NOT EXISTS policy_frozen_at DATETIME NULL;;
ALTER TABLE wisp_vouchers MODIFY snap_rate_limit_str VARCHAR(255) NULL DEFAULT NULL;;
ALTER TABLE wisp_vouchers MODIFY snap_rate_upload VARCHAR(50) NULL DEFAULT NULL;;
ALTER TABLE wisp_vouchers MODIFY snap_rate_download VARCHAR(50) NULL DEFAULT NULL;;
ALTER TABLE wisp_vouchers MODIFY snap_volume_quota_mb BIGINT NULL DEFAULT NULL;;
ALTER TABLE wisp_vouchers MODIFY snap_cost DECIMAL(10,2) NULL DEFAULT NULL;;
ALTER TABLE wisp_vouchers MODIFY snap_price DECIMAL(10,2) NULL DEFAULT NULL;;
UPDATE wisp_vouchers v SET policy_frozen_at=COALESCE(first_used_at,last_renewed_at,CURRENT_TIMESTAMP)
              WHERE policy_frozen_at IS NULL AND (status NOT IN ('unused','suspended','disabled') OR first_used_at IS NOT NULL OR last_renewed_at IS NOT NULL
                OR expires_at IS NOT NULL
                OR EXISTS(SELECT 1 FROM radacct a WHERE a.username=v.username)
                OR EXISTS(SELECT 1 FROM radpostauth a WHERE a.username=v.username AND a.reply='Access-Accept')
                OR EXISTS(SELECT 1 FROM wisp_voucher_sales s WHERE s.voucher_id=v.id));;
UPDATE wisp_vouchers SET snap_price=NULL,snap_cost=NULL,snap_volume_quota_mb=NULL,snap_rate_download=NULL,snap_rate_upload=NULL,snap_rate_limit_str=NULL WHERE policy_frozen_at IS NULL;;
UPDATE wisp_subscribers s JOIN wisp_packages p ON p.id=s.package_id
          SET s.snap_volume_quota_mb=COALESCE(p.volume_quota_mb,0)
          WHERE s.snap_volume_quota_mb IS NULL;;
CREATE OR REPLACE FUNCTION fn_freeze_voucher_policy(p_username VARCHAR(64))
RETURNS INT MODIFIES SQL DATA
BEGIN
    DECLARE v_id INT DEFAULT NULL;
    DECLARE v_frozen DATETIME DEFAULT NULL;
    DECLARE v_status VARCHAR(32);
    DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_id = NULL;
    SELECT id, policy_frozen_at, status INTO v_id, v_frozen, v_status
      FROM wisp_vouchers WHERE username=p_username LIMIT 1 FOR UPDATE;
    IF v_id IS NULL THEN RETURN 1; END IF;
    IF v_frozen IS NULL AND v_status='unused' THEN
        UPDATE wisp_vouchers v JOIN wisp_packages p ON p.id=v.package_id
        SET v.snap_price=COALESCE(p.price,0), v.snap_cost=COALESCE(p.cost,0),
            v.snap_volume_quota_mb=COALESCE(p.volume_quota_mb,0),
            v.snap_rate_download=COALESCE(p.rate_download,'0'),
            v.snap_rate_upload=COALESCE(p.rate_upload,'0'),
            v.snap_rate_limit_str=COALESCE((SELECT r.value FROM radgroupreply r
              WHERE r.groupname=p.name AND LOWER(r.attribute)='mikrotik-rate-limit'
              ORDER BY r.id DESC LIMIT 1),'0/0'),
            v.policy_frozen_at=CURRENT_TIMESTAMP
        WHERE v.id=v_id AND v.policy_frozen_at IS NULL;
        SELECT policy_frozen_at INTO v_frozen FROM wisp_vouchers WHERE id=v_id;
        IF v_frozen IS NULL THEN RETURN 0; END IF;
        RETURN 3;
    END IF;
    RETURN 2;
END;;
CREATE OR REPLACE TRIGGER trg_voucher_first_policy_insert
          BEFORE INSERT ON wisp_vouchers FOR EACH ROW BEGIN
          IF NEW.status IN ('unused','suspended','disabled') AND NEW.first_used_at IS NULL AND NEW.last_renewed_at IS NULL
            AND NEW.expires_at IS NULL AND NEW.policy_frozen_at IS NULL THEN
          SET NEW.snap_price=NULL;
SET NEW.snap_cost=NULL;
SET NEW.snap_volume_quota_mb=NULL;
SET NEW.snap_rate_download=NULL;
SET NEW.snap_rate_upload=NULL;
SET NEW.snap_rate_limit_str=NULL;
          ELSE
          SET NEW.snap_price=COALESCE(NEW.snap_price,(SELECT price FROM wisp_packages WHERE id=NEW.package_id),0);
SET NEW.snap_cost=COALESCE(NEW.snap_cost,(SELECT cost FROM wisp_packages WHERE id=NEW.package_id),0);
SET NEW.snap_volume_quota_mb=COALESCE(NEW.snap_volume_quota_mb,(SELECT volume_quota_mb FROM wisp_packages WHERE id=NEW.package_id),0);
SET NEW.snap_rate_download=COALESCE(NEW.snap_rate_download,(SELECT rate_download FROM wisp_packages WHERE id=NEW.package_id),'0');
SET NEW.snap_rate_upload=COALESCE(NEW.snap_rate_upload,(SELECT rate_upload FROM wisp_packages WHERE id=NEW.package_id),'0');SET NEW.snap_rate_limit_str=COALESCE(NEW.snap_rate_limit_str,
          (SELECT r.value FROM radgroupreply r JOIN wisp_packages p ON p.name=r.groupname
            WHERE p.id=NEW.package_id AND LOWER(r.attribute)='mikrotik-rate-limit' ORDER BY r.id DESC LIMIT 1),'0/0');
          SET NEW.policy_frozen_at=COALESCE(NEW.policy_frozen_at,NEW.first_used_at,NEW.last_renewed_at,CURRENT_TIMESTAMP);
          END IF; END;;
CREATE OR REPLACE TRIGGER trg_voucher_first_policy_update
          BEFORE UPDATE ON wisp_vouchers FOR EACH ROW BEGIN
          IF OLD.policy_frozen_at IS NOT NULL AND COALESCE(@max_radius_import,0)=1 THEN
          SET NEW.snap_price=OLD.snap_price;
SET NEW.snap_cost=OLD.snap_cost;
SET NEW.snap_volume_quota_mb=OLD.snap_volume_quota_mb;
SET NEW.snap_rate_download=OLD.snap_rate_download;
SET NEW.snap_rate_upload=OLD.snap_rate_upload;
SET NEW.snap_rate_limit_str=OLD.snap_rate_limit_str;
          SET NEW.policy_frozen_at=OLD.policy_frozen_at;
          ELSEIF NEW.policy_frozen_at IS NULL AND NEW.status IN ('unused','suspended','disabled')
            AND NEW.first_used_at IS NULL AND NEW.last_renewed_at IS NULL AND NEW.expires_at IS NULL THEN
          SET NEW.snap_price=NULL;
SET NEW.snap_cost=NULL;
SET NEW.snap_volume_quota_mb=NULL;
SET NEW.snap_rate_download=NULL;
SET NEW.snap_rate_upload=NULL;
SET NEW.snap_rate_limit_str=NULL;
          ELSE
          IF NEW.policy_frozen_at IS NULL THEN
          SET NEW.snap_price=COALESCE(NEW.snap_price,(SELECT price FROM wisp_packages WHERE id=NEW.package_id),0);
SET NEW.snap_cost=COALESCE(NEW.snap_cost,(SELECT cost FROM wisp_packages WHERE id=NEW.package_id),0);
SET NEW.snap_volume_quota_mb=COALESCE(NEW.snap_volume_quota_mb,(SELECT volume_quota_mb FROM wisp_packages WHERE id=NEW.package_id),0);
SET NEW.snap_rate_download=COALESCE(NEW.snap_rate_download,(SELECT rate_download FROM wisp_packages WHERE id=NEW.package_id),'0');
SET NEW.snap_rate_upload=COALESCE(NEW.snap_rate_upload,(SELECT rate_upload FROM wisp_packages WHERE id=NEW.package_id),'0');SET NEW.snap_rate_limit_str=COALESCE(NEW.snap_rate_limit_str,
          (SELECT r.value FROM radgroupreply r JOIN wisp_packages p ON p.name=r.groupname
            WHERE p.id=NEW.package_id AND LOWER(r.attribute)='mikrotik-rate-limit' ORDER BY r.id DESC LIMIT 1),'0/0');
          END IF;
          SET NEW.policy_frozen_at=COALESCE(NEW.policy_frozen_at,NEW.first_used_at,NEW.last_renewed_at,CURRENT_TIMESTAMP);
          END IF; END;;
CREATE OR REPLACE TRIGGER trg_subscriber_quota_insert
              BEFORE INSERT ON wisp_subscribers FOR EACH ROW BEGIN
              IF NEW.snap_volume_quota_mb IS NULL THEN
                SET NEW.snap_volume_quota_mb=COALESCE((SELECT volume_quota_mb FROM wisp_packages WHERE id=NEW.package_id),0);
              END IF; END;;
CREATE OR REPLACE TRIGGER trg_subscriber_quota_update
              BEFORE UPDATE ON wisp_subscribers FOR EACH ROW BEGIN
              IF NEW.snap_volume_quota_mb IS NULL THEN
                SET NEW.snap_volume_quota_mb=COALESCE((SELECT volume_quota_mb FROM wisp_packages WHERE id=NEW.package_id),0);
              END IF; END;;
DELIMITER ;

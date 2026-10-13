"""Freeze voucher commercial policy at accepted login; subscriber quota per cycle.

NULL on a genuinely unused voucher means follow the package until acceptance.
The marker is separate from Accounting-Start: a lost Start must not change a
contract that has already been sent to a NAS. No historical ledger is repriced.
"""

FIELDS = ('price', 'cost', 'volume_quota_mb', 'rate_download', 'rate_upload', 'rate_limit_str')

FREEZE_SQL = """CREATE OR REPLACE FUNCTION fn_freeze_voucher_policy(p_username VARCHAR(64))
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
END"""


def install_first_use_policy(conn):
    """Idempotent MariaDB migration. Called before accounting trigger install."""
    cur = conn.cursor()
    try:
        cur.execute("SHOW COLUMNS FROM wisp_vouchers LIKE 'policy_frozen_at'")
        first_install = not cur.fetchone()
        if first_install:
            cur.execute('ALTER TABLE wisp_vouchers ADD policy_frozen_at DATETIME NULL')
        for field, definition in (
            ('price', 'DECIMAL(10,2)'), ('cost', 'DECIMAL(10,2)'),
            ('volume_quota_mb', 'BIGINT'), ('rate_download', 'VARCHAR(50)'),
            ('rate_upload', 'VARCHAR(50)'), ('rate_limit_str', 'VARCHAR(255)')):
            cur.execute(f"SHOW COLUMNS FROM wisp_vouchers LIKE 'snap_{field}'")
            column = cur.fetchone()
            if column['Null'] != 'YES' or column['Default'] is not None or (field == 'rate_limit_str' and column.get('Type') != 'varchar(255)'):
                cur.execute(f'ALTER TABLE wisp_vouchers MODIFY snap_{field} {definition} NULL DEFAULT NULL')
        if first_install:
            # Never reinterpret a used/imported or already accepted card as new.
            cur.execute("""UPDATE wisp_vouchers v SET policy_frozen_at=COALESCE(first_used_at,last_renewed_at,CURRENT_TIMESTAMP)
              WHERE policy_frozen_at IS NULL AND (status NOT IN ('unused','suspended','disabled') OR first_used_at IS NOT NULL OR last_renewed_at IS NOT NULL
                OR expires_at IS NOT NULL
                OR EXISTS(SELECT 1 FROM radacct a WHERE a.username=v.username)
                OR EXISTS(SELECT 1 FROM radpostauth a WHERE a.username=v.username AND a.reply='Access-Accept')
                OR EXISTS(SELECT 1 FROM wisp_voucher_sales s WHERE s.voucher_id=v.id))""")
            cur.execute('UPDATE wisp_vouchers SET ' + ','.join('snap_'+f+'=NULL' for f in FIELDS)
                        + ' WHERE policy_frozen_at IS NULL')
        cur.execute("""UPDATE wisp_subscribers s JOIN wisp_packages p ON p.id=s.package_id
          SET s.snap_volume_quota_mb=COALESCE(p.volume_quota_mb,0)
          WHERE s.snap_volume_quota_mb IS NULL""")
        cur.execute(FREEZE_SQL)
        assignments = '\n'.join('SET NEW.snap_'+f+'=NULL;' for f in FIELDS)
        missing = '\n'.join(
            f'SET NEW.snap_{f}=COALESCE(NEW.snap_{f},(SELECT {f} FROM wisp_packages WHERE id=NEW.package_id),{default});'
            for f, default in (('price','0'),('cost','0'),('volume_quota_mb','0'),('rate_download',"'0'"),('rate_upload',"'0'")))
        missing += """SET NEW.snap_rate_limit_str=COALESCE(NEW.snap_rate_limit_str,
          (SELECT r.value FROM radgroupreply r JOIN wisp_packages p ON p.name=r.groupname
            WHERE p.id=NEW.package_id AND LOWER(r.attribute)='mikrotik-rate-limit' ORDER BY r.id DESC LIMIT 1),'0/0');"""
        cur.execute("""CREATE OR REPLACE TRIGGER trg_voucher_first_policy_insert
          BEFORE INSERT ON wisp_vouchers FOR EACH ROW BEGIN
          IF NEW.status IN ('unused','suspended','disabled') AND NEW.first_used_at IS NULL AND NEW.last_renewed_at IS NULL
            AND NEW.expires_at IS NULL AND NEW.policy_frozen_at IS NULL THEN
          """ + assignments + """
          ELSE
          """ + missing + """
          SET NEW.policy_frozen_at=COALESCE(NEW.policy_frozen_at,NEW.first_used_at,NEW.last_renewed_at,CURRENT_TIMESTAMP);
          END IF; END""")
        protected = '\n'.join('SET NEW.snap_'+f+'=OLD.snap_'+f+';' for f in FIELDS)
        cur.execute("""CREATE OR REPLACE TRIGGER trg_voucher_first_policy_update
          BEFORE UPDATE ON wisp_vouchers FOR EACH ROW BEGIN
          IF OLD.policy_frozen_at IS NOT NULL AND COALESCE(@max_radius_import,0)=1 THEN
          """ + protected + """
          SET NEW.policy_frozen_at=OLD.policy_frozen_at;
          ELSEIF NEW.policy_frozen_at IS NULL AND NEW.status IN ('unused','suspended','disabled')
            AND NEW.first_used_at IS NULL AND NEW.last_renewed_at IS NULL AND NEW.expires_at IS NULL THEN
          """ + assignments + """
          ELSE
          IF NEW.policy_frozen_at IS NULL THEN
          """ + missing + """
          END IF;
          SET NEW.policy_frozen_at=COALESCE(NEW.policy_frozen_at,NEW.first_used_at,NEW.last_renewed_at,CURRENT_TIMESTAMP);
          END IF; END""")
        for operation in ('INSERT', 'UPDATE'):
            cur.execute(f"""CREATE OR REPLACE TRIGGER trg_subscriber_quota_{operation.lower()}
              BEFORE {operation} ON wisp_subscribers FOR EACH ROW BEGIN
              IF NEW.snap_volume_quota_mb IS NULL THEN
                SET NEW.snap_volume_quota_mb=COALESCE((SELECT volume_quota_mb FROM wisp_packages WHERE id=NEW.package_id),0);
              END IF; END""")
    finally:
        cur.close()

"""Durable NAS counter evidence and retry queue schema."""

def install_nas_session_safety(conn):
    with conn.cursor() as cur:
        cur.execute('CREATE TABLE IF NOT EXISTS wisp_nas_session_seals (\n radacctid BIGINT(21) NOT NULL PRIMARY KEY,\n username VARCHAR(64) NOT NULL,\n nasipaddress VARCHAR(15) NOT NULL,\n acctsessionid VARCHAR(64) NOT NULL,\n input_bytes BIGINT UNSIGNED NOT NULL,\n output_bytes BIGINT UNSIGNED NOT NULL,\n session_seconds BIGINT UNSIGNED NOT NULL,\n sealed_at DATETIME NOT NULL,\n evidence_token CHAR(32) NOT NULL,\n INDEX seals_username(username),\n CONSTRAINT fk_max_nas_seal_session FOREIGN KEY(radacctid) REFERENCES radacct(radacctid) ON DELETE CASCADE\n) ENGINE=InnoDB')
        cur.execute("CREATE TABLE IF NOT EXISTS wisp_online_policy_outbox (username VARCHAR(64) PRIMARY KEY,status VARCHAR(16) NOT NULL DEFAULT 'pending',next_attempt DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,last_error VARCHAR(100) NULL,updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, INDEX idx_policy_pending(status,next_attempt)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci")
        cur.execute("SELECT VERSION() AS version")
        maria="mariadb" in str(cur.fetchone()["version"]).lower()
        trigger='CREATE OR REPLACE TRIGGER trg_max_nas_seal_preserve BEFORE UPDATE ON radacct FOR EACH ROW\nBEGIN\n DECLARE frozen_input BIGINT UNSIGNED;\n DECLARE frozen_output BIGINT UNSIGNED;\n DECLARE frozen_seconds BIGINT UNSIGNED;\n DECLARE frozen_at DATETIME;\n DECLARE CONTINUE HANDLER FOR NOT FOUND SET frozen_at=NULL;\n SELECT input_bytes,output_bytes,session_seconds,sealed_at\n INTO frozen_input,frozen_output,frozen_seconds,frozen_at\n FROM wisp_nas_session_seals\n WHERE radacctid=OLD.radacctid AND username=OLD.username\n AND nasipaddress=OLD.nasipaddress AND acctsessionid=OLD.acctsessionid;\n IF frozen_at IS NOT NULL THEN\n  SET NEW.acctinputoctets=frozen_input;\n  SET NEW.acctoutputoctets=frozen_output;\n  SET NEW.acctsessiontime=frozen_seconds;\n  SET NEW.acctstoptime=COALESCE(OLD.acctstoptime,frozen_at);\n END IF;\nEND'
        if not maria:
            cur.execute("DROP TRIGGER IF EXISTS trg_max_nas_seal_preserve")
            trigger=trigger.replace("CREATE OR REPLACE TRIGGER", "CREATE TRIGGER",1)
        cur.execute(trigger)
    conn.commit()

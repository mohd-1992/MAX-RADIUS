"""Additive quick issue receipts; never changes existing cards or balances."""


def install_quick_card_schema(conn, mysql=True):
    cursor = conn.cursor()
    try:
        cursor.execute('''CREATE TABLE IF NOT EXISTS wisp_quick_card_issues (
            id BIGINT PRIMARY KEY AUTO_INCREMENT,
            actor_id INT NOT NULL,
            reseller_id INT NULL,
            request_key VARCHAR(64) NOT NULL,
            package_id INT NOT NULL,
            card_count INT NOT NULL,
            batch_id INT NOT NULL,
            amount DECIMAL(12,2) NOT NULL DEFAULT 0,
            balance_after DECIMAL(12,2) NULL,
            created_at DATETIME NOT NULL,
            print_requests INT NOT NULL DEFAULT 0,
            last_print_requested_at DATETIME NULL,
            UNIQUE KEY uq_quick_request(actor_id,request_key),
            KEY ix_quick_owner(reseller_id,id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''' if mysql else '''
            CREATE TABLE IF NOT EXISTS wisp_quick_card_issues (
            id INTEGER PRIMARY KEY, actor_id INTEGER NOT NULL, reseller_id INTEGER,
            request_key TEXT NOT NULL, package_id INTEGER NOT NULL, card_count INTEGER NOT NULL,
            batch_id INTEGER NOT NULL, amount NUMERIC NOT NULL DEFAULT 0, balance_after NUMERIC,
            created_at TEXT NOT NULL, print_requests INTEGER NOT NULL DEFAULT 0,
            last_print_requested_at TEXT, UNIQUE(actor_id,request_key))''')
    finally:
        cursor.close()

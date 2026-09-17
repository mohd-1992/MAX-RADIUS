-- ==========================================================
-- WISP ISP Billing & Management Extension Tables
-- ==========================================================

CREATE TABLE IF NOT EXISTS wisp_admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username VARCHAR(64) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    full_name VARCHAR(100) NOT NULL,
    email VARCHAR(120),
    role VARCHAR(32) DEFAULT 'admin', -- superadmin, admin, support, accountant
    is_active INTEGER DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS wisp_resellers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(120) NOT NULL,
    contact_person VARCHAR(100),
    phone VARCHAR(30),
    email VARCHAR(120),
    balance DECIMAL(10,2) DEFAULT 0.00,
    commission_percent DECIMAL(5,2) DEFAULT 10.00,
    allowed_packages TEXT DEFAULT '', -- comma-separated package IDs or 'all'
    status VARCHAR(20) DEFAULT 'active', -- active, suspended
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS wisp_reseller_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reseller_id INTEGER NOT NULL,
    type VARCHAR(20) NOT NULL, -- deposit, card_purchase, refund, adjustment
    amount DECIMAL(10,2) NOT NULL,
    balance_after DECIMAL(10,2) NOT NULL,
    description TEXT,
    reference_id VARCHAR(64),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (reseller_id) REFERENCES wisp_resellers(id)
);

CREATE TABLE IF NOT EXISTS wisp_packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(80) UNIQUE NOT NULL,
    service_type VARCHAR(20) DEFAULT 'hotspot', -- hotspot, pppoe, both
    price DECIMAL(10,2) DEFAULT 0.00,
    cost DECIMAL(10,2) DEFAULT 0.00,
    
    -- Speed parameters
    rate_download VARCHAR(20) DEFAULT '2M',
    rate_upload VARCHAR(20) DEFAULT '1M',
    burst_download VARCHAR(20) DEFAULT '4M',
    burst_upload VARCHAR(20) DEFAULT '2M',
    burst_threshold_down VARCHAR(20) DEFAULT '1500k',
    burst_threshold_up VARCHAR(20) DEFAULT '750k',
    burst_time INTEGER DEFAULT 16, -- in seconds
    priority INTEGER DEFAULT 8,     -- 1 (highest) to 8 (lowest)
    min_download VARCHAR(20) DEFAULT '512k',
    min_upload VARCHAR(20) DEFAULT '256k',
    
    -- Quota & Validity
    volume_quota_mb BIGINT DEFAULT 0, -- 0 = unlimited
    uptime_limit_mins INTEGER DEFAULT 0, -- 0 = unlimited
    validity_days INTEGER DEFAULT 30, -- 0 = unlimited
    simultaneous_sessions INTEGER DEFAULT 1,
    
    is_active INTEGER DEFAULT 1,
    show_in_portal INTEGER DEFAULT 1,
    is_rollover_enabled INTEGER DEFAULT 0,
    description TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS wisp_subscribers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username VARCHAR(64) UNIQUE NOT NULL,
    password VARCHAR(64) NOT NULL,
    full_name VARCHAR(120) NOT NULL,
    phone VARCHAR(30),
    email VARCHAR(120),
    national_id VARCHAR(50),
    address TEXT,
    service_type VARCHAR(20) DEFAULT 'pppoe', -- pppoe, hotspot
    package_id INTEGER NOT NULL,
    mac_binding VARCHAR(30) DEFAULT '',
    static_ip VARCHAR(45) DEFAULT '',
    status VARCHAR(20) DEFAULT 'active', -- active, expired, suspended
    balance DECIMAL(10,2) DEFAULT 0.00,
    expires_at DATETIME,
    last_renewed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    notes TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (package_id) REFERENCES wisp_packages(id)
);

CREATE TABLE IF NOT EXISTS wisp_voucher_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_number VARCHAR(40) UNIQUE NOT NULL,
    name VARCHAR(100) NOT NULL,
    package_id INTEGER NOT NULL,
    reseller_id INTEGER,
    card_count INTEGER NOT NULL,
    prefix VARCHAR(10) DEFAULT '',
    pin_only INTEGER DEFAULT 1, -- 1 = PIN only (username==password), 0 = user+pass
    char_type VARCHAR(20) DEFAULT 'numbers', -- numbers, alphanumeric, lowercase
    code_length INTEGER DEFAULT 8,
    created_by VARCHAR(64) DEFAULT 'admin',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (package_id) REFERENCES wisp_packages(id),
    FOREIGN KEY (reseller_id) REFERENCES wisp_resellers(id)
);

CREATE TABLE IF NOT EXISTS wisp_vouchers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    package_id INTEGER NOT NULL,
    reseller_id INTEGER,
    serial_number VARCHAR(30) UNIQUE NOT NULL,
    username VARCHAR(64) UNIQUE NOT NULL,
    password VARCHAR(64) NOT NULL,
    pin_code VARCHAR(32) NOT NULL,
    status VARCHAR(20) DEFAULT 'unused', -- unused, active, expired, disabled
    first_used_at DATETIME,
    expires_at DATETIME,
    last_renewed_at DATETIME,
    bound_mac VARCHAR(30) DEFAULT '',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (batch_id) REFERENCES wisp_voucher_batches(id),
    FOREIGN KEY (package_id) REFERENCES wisp_packages(id),
    FOREIGN KEY (reseller_id) REFERENCES wisp_resellers(id)
);

CREATE TABLE IF NOT EXISTS wisp_card_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(80) NOT NULL,
    width_mm INTEGER DEFAULT 85,
    height_mm INTEGER DEFAULT 55,
    cards_per_row INTEGER DEFAULT 2,
    background_color VARCHAR(20) DEFAULT '#ffffff',
    border_color VARCHAR(20) DEFAULT '#cbd5e1',
    header_color VARCHAR(20) DEFAULT '#1e40af',
    show_qr INTEGER DEFAULT 1,
    show_logo INTEGER DEFAULT 1,
    show_price INTEGER DEFAULT 1,
    show_serial INTEGER DEFAULT 1,
    show_validity INTEGER DEFAULT 1,
    instructions_ar TEXT DEFAULT 'طريقة الاستخدام: اتصل بشبكة الواي فاي، افتح المتصفح، ادخل رمز الكرت أو امسح الـ QR كود للتسجيل التلقائي.',
    is_default INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS wisp_nas_devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(100) NOT NULL,
    ip_address VARCHAR(45) UNIQUE NOT NULL,
    nas_type VARCHAR(30) DEFAULT 'mikrotik',
    secret VARCHAR(60) NOT NULL,
    api_port INTEGER DEFAULT 8728,
    coa_port INTEGER DEFAULT 3799,
    api_username VARCHAR(64) DEFAULT 'admin',
    api_password VARCHAR(64) DEFAULT '',
    hotspot_login_url VARCHAR(255) DEFAULT 'http://192.168.88.1/login',
    status VARCHAR(20) DEFAULT 'online', -- online, offline, unknown
    last_ping_at DATETIME,
    response_time_ms INTEGER DEFAULT 0,
    description TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS wisp_audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER,
    username VARCHAR(64),
    action VARCHAR(60) NOT NULL,
    module VARCHAR(40) NOT NULL,
    details TEXT,
    ip_address VARCHAR(45),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS wisp_system_settings (
    key VARCHAR(64) PRIMARY KEY,
    value TEXT,
    description VARCHAR(255)
);

CREATE TABLE IF NOT EXISTS wisp_invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_number VARCHAR(40) UNIQUE NOT NULL,
    subscriber_id INTEGER,
    subscriber_name VARCHAR(120),
    package_name VARCHAR(80),
    amount DECIMAL(10,2) NOT NULL,
    status VARCHAR(20) DEFAULT 'unpaid',
    issue_date DATE DEFAULT (DATE('now')),
    due_date DATE,
    paid_at DATETIME,
    payment_method VARCHAR(40) DEFAULT 'cash',
    notes TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (subscriber_id) REFERENCES wisp_subscribers(id)
);

CREATE TABLE IF NOT EXISTS wisp_voucher_sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    voucher_id INTEGER NOT NULL,
    batch_id INTEGER NOT NULL,
    batch_name VARCHAR(100),
    username VARCHAR(64) NOT NULL,
    serial_number VARCHAR(30),
    package_name VARCHAR(80) NOT NULL,
    price DECIMAL(10,2) NOT NULL,
    cost DECIMAL(10,2) DEFAULT 0.00,
    reseller_id INTEGER,
    activated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (voucher_id) REFERENCES wisp_vouchers(id),
    FOREIGN KEY (batch_id) REFERENCES wisp_voucher_batches(id)
);

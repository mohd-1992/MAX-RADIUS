-- =============================================================================
-- Migration Schema: Advanced RBAC (Roles & Permissions) & Manager Billing/Ledger
-- =============================================================================

SET FOREIGN_KEY_CHECKS = 0;

-- 1. Permissions Definition Table
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

-- 2. Roles Definition Table
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

-- 3. Role <-> Permission Pivot Table
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

-- 4. Managers & Admins & Resellers Table
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
    `notes` TEXT,
    `last_login_at` DATETIME NULL,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `idx_manager_username` (`username`),
    KEY `idx_mgr_role` (`role_id`),
    CONSTRAINT `fk_mgr_role` FOREIGN KEY (`role_id`) REFERENCES `wisp_roles` (`id`) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 5. Manager Invoices & Financial Ledger Table
CREATE TABLE IF NOT EXISTS `wisp_manager_invoices` (
    `id` INT(11) NOT NULL AUTO_INCREMENT,
    `invoice_number` VARCHAR(50) NOT NULL,
    `manager_id` INT(11) NOT NULL,
    `transaction_type` VARCHAR(30) NOT NULL, -- 'deposit', 'deduction', 'payment', 'card_purchase', 'commission'
    `amount` DECIMAL(12,2) NOT NULL,
    `payment_type` VARCHAR(30) NOT NULL DEFAULT 'cash', -- 'cash' (نقدي), 'credit' (آجل), 'transfer' (تحويل بنكي)
    `balance_before` DECIMAL(12,2) NOT NULL DEFAULT 0.00,
    `balance_after` DECIMAL(12,2) NOT NULL DEFAULT 0.00,
    `notes` TEXT,
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

-- =============================================================================
-- Seed Standard Permissions
-- =============================================================================
INSERT INTO `wisp_permissions` (`code`, `name`, `category`, `description`) VALUES
-- 1. المشتركين (Subscribers)
('subscribers.view', 'عرض المشتركين', 'إدارة المشتركين', 'إمكانية استعراض قائمة وبيانات المشتركين'),
('subscribers.create', 'إضافة مشترك جديد', 'إدارة المشتركين', 'إمكانية إنشاء حسابات جديدة للمشتركين وتوثيقها'),
('subscribers.edit', 'تعديل بيانات المشترك', 'إدارة المشتركين', 'تعديل الباقة أو كلمة المرور أو البيانات الشخصية'),
('subscribers.delete', 'حذف المشتركين', 'إدارة المشتركين', 'حذف حسابات المشتركين من النظام والراديوس'),
('subscribers.renew', 'تجديد اشتراك', 'إدارة المشتركين', 'إمكانية تجديد الباقة وتمديد صلاحية المشترك'),
('subscribers.disconnect', 'فصل جلسة المشترك (CoA)', 'إدارة المشتركين', 'إرسال أمر طرد وفصل الجلسة الآنية عبر CoA'),

-- 2. نظام الكروت (Vouchers)
('vouchers.view', 'استعراض الحزم والكروت', 'نظام الكروت', 'عرض دفعات الكروت والمستخدمين النشطين'),
('vouchers.generate', 'توليد دفعات كروت جديدة', 'نظام الكروت', 'إنشاء وتوليد كروت جديدة وطباعتها'),
('vouchers.print', 'طباعة وتصميم الكروت', 'نظام الكروت', 'الوصول لقوالب الطباعة وتصدير الكروت'),
('vouchers.delete', 'حذف دفعات الكروت', 'نظام الكروت', 'حذف الحزم والكروت غير المستخدمة'),
('vouchers.actions', 'إجراءات الكروت المتقدمة', 'نظام الكروت', 'تعديل مدة الصلاحية وإضافة الرصيد والفصل الفوري'),

-- 3. العمليات المالية والمحافظ (Financial & Wallets)
('wallets.manage', 'إدارة الأرصدة والمحافظ', 'العمليات المالية', 'إيداع وسحب الرصيد النقدي والآجل لحسابات المشتركين والموزعين'),
('invoices.view', 'عرض الفواتير والتقارير المالية', 'العمليات المالية', 'استعراض فواتير الاشتراكات ومبيعات الكروت'),
('invoices.manage', 'إصدار وتسديد الفواتير', 'العمليات المالية', 'تسجيل الدفعات وتغيير حالة الفواتير إلى مسددة'),

-- 4. الباقات والأسعار (Packages)
('packages.view', 'عرض الباقات', 'الباقات والسرعات', 'استعراض باقات الإنترنت ومحددات السرعة'),
('packages.manage', 'إضافة وتعديل الباقات', 'الباقات والسرعات', 'إنشاء باقات جديدة وتعديل أسعارها وسرعاتها'),

-- 5. أجهزة الميكروتيك (NAS / Routers)
('nas.view', 'عرض راوترات NAS', 'خوادم البث (NAS)', 'استعراض حالة الراوترات والمراقبة الحية'),
('nas.manage', 'إضافة وتعديل وحذف الراوترات', 'خوادم البث (NAS)', 'ربط راوترات جديدة وتعديل منافذ وأسرار RADIUS'),
('nas.test_coa', 'اختبار بروتوكول CoA', 'خوادم البث (NAS)', 'إرسال حزم PoD / Disconnect تجريبية للراوتر'),

-- 6. المدراء والموزعين (Managers & RBAC)
('managers.view', 'عرض المدراء والموزعين', 'المدراء والموزعين', 'استعراض حسابات المدراء وموزعي نقاط البيع'),
('managers.manage', 'إدارة المدراء والموزعين', 'المدراء والموزعين', 'إضافة وتعديل وتعطيل حسابات المدراء'),
('managers.billing', 'إدارة فواتير وكشوفات الموزعين', 'المدراء والموزعين', 'شحن أرصدة الموزعين نقدياً وآجلاً وإصدار كشوفات الحساب'),
('roles.manage', 'إدارة الصلاحيات والأدوار', 'المدراء والموزعين', 'إنشاء وتعديل مجموعات الصلاحيات والأدوار'),

-- 7. خدمات النظام والإعدادات (System Settings & Services)
('system.view_services', 'مراقبة خدمات النظام', 'خدمات النظام', 'استعراض حالة Web Server و FreeRADIUS و MariaDB'),
('system.control_services', 'التحكم بالخدمات والتعافي', 'خدمات النظام', 'إعادة تشغيل وإيقاف خدمات النظام'),
('system.backups', 'النسخ الاحتياطي', 'إعدادات النظام', 'أخذ واستعادة وتحميل النسخ الاحتياطية لقاعدة البيانات'),
('system.settings', 'إعدادات النظام والشبكة', 'إعدادات النظام', 'تعديل هوية الشبكة والشعار ومنافذ الاتصال الافتراضية'),
('system.audit_logs', 'سجلات التدقيق والأمان', 'إعدادات النظام', 'استعراض سجلات تتبع عمليات المدراء')
ON DUPLICATE KEY UPDATE `name` = VALUES(`name`), `category` = VALUES(`category`);

-- =============================================================================
-- Seed Standard Roles
-- =============================================================================
INSERT INTO `wisp_roles` (`id`, `name`, `code`, `description`, `is_system`) VALUES
(1, 'المدير العام (Super Admin)', 'superadmin', 'صلاحيات كاملة وغير مقيدة على كافة أقسام النظام والخدمات', 1),
(2, 'مدير فرع / شبكة (Manager)', 'manager', 'إدارة المشتركين، الكروت، الباقات، ومراقبة الراوترات', 0),
(3, 'موزع رئيسي (Main Reseller)', 'main_reseller', 'توليد الكروت، شحن نقاط البيع، واستعراض مبيعاته وفواتيره', 0),
(4, 'نقطة بيع (POS Agent)', 'pos_agent', 'استعراض الكروت المخصصة له، تفعيل الاشتراكات، وشحن أرصدة المستخدمين', 0)
ON DUPLICATE KEY UPDATE `name` = VALUES(`name`), `description` = VALUES(`description`);

-- =============================================================================
-- Link Superadmin and Standard Role Permissions
-- =============================================================================
-- Role 1 (Superadmin) gets all permissions
INSERT IGNORE INTO `wisp_role_permissions` (`role_id`, `permission_id`)
SELECT 1, `id` FROM `wisp_permissions`;

-- Role 2 (Manager)
INSERT IGNORE INTO `wisp_role_permissions` (`role_id`, `permission_id`)
SELECT 2, `id` FROM `wisp_permissions`
WHERE `code` IN (
    'subscribers.view', 'subscribers.create', 'subscribers.edit', 'subscribers.renew', 'subscribers.disconnect',
    'vouchers.view', 'vouchers.generate', 'vouchers.print', 'vouchers.actions',
    'wallets.manage', 'invoices.view', 'invoices.manage',
    'packages.view', 'nas.view', 'nas.test_coa',
    'managers.view', 'system.view_services'
);

-- Role 3 (Main Reseller)
INSERT IGNORE INTO `wisp_role_permissions` (`role_id`, `permission_id`)
SELECT 3, `id` FROM `wisp_permissions`
WHERE `code` IN (
    'vouchers.view', 'vouchers.generate', 'vouchers.print',
    'wallets.manage', 'invoices.view', 'managers.view', 'managers.billing'
);

-- Role 4 (POS Agent)
INSERT IGNORE INTO `wisp_role_permissions` (`role_id`, `permission_id`)
SELECT 4, `id` FROM `wisp_permissions`
WHERE `code` IN (
    'subscribers.view', 'subscribers.renew',
    'vouchers.view', 'vouchers.print', 'wallets.manage', 'invoices.view'
);

-- =============================================================================
-- Seed Initial Super Admin Manager
-- =============================================================================
INSERT INTO `wisp_managers` (`id`, `username`, `password_hash`, `full_name`, `phone`, `email`, `role_id`, `wallet_balance`, `is_active`)
VALUES (1, 'admin', 'admin', 'مدير النظام الرئيسي', '+967 770 000 000', 'admin@wisp-network.com', 1, 0.00, 1)
ON DUPLICATE KEY UPDATE `role_id` = 1, `is_active` = 1;

SET FOREIGN_KEY_CHECKS = 1;

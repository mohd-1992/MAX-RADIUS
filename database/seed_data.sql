-- Seed Data for Radius WISP Manager
INSERT OR IGNORE INTO wisp_admins (id, username, password_hash, full_name, email, role)
VALUES (1, 'admin', 'pbkdf2:sha256:600000', 'مدير النظام الرئيسي', 'admin@wisp.net', 'superadmin');

INSERT OR IGNORE INTO wisp_system_settings (key, value, description) VALUES
('isp_name', 'شبكة الأفق الذكية WISP', 'اسم مزود خدمة الإنترنت'),
('currency', 'SAR', 'العملة المحلية'),
('currency_symbol', 'ر.س', 'رمز العملة'),
('default_coa_port', '3799', 'منفذ بروتوكول CoA الافتراضي'),
('sms_gateway_url', '', 'رابط بوابة إرسال الرسائل القصيرة'),
('sms_api_key', '', 'مفتاح API الخاص بالـ SMS'),
('sms_sender_id', 'WISP-NET', 'اسم مرسل الرسائل'),
('company_phone', '+966500000000', 'هاتف خدمة العملاء'),
('hotspot_domain', 'wifi.hotspot', 'نطاق صفحة الهوتسبوت');

INSERT OR IGNORE INTO nas (id, nasname, shortname, type, ports, secret, description)
VALUES (1, '192.168.88.1', 'MikroTik-Main', 'mikrotik', 1812, 'radiusSecret123', 'راوتر الميكروتيك الرئيسي - برج الإرسال 1');

INSERT OR IGNORE INTO wisp_nas_devices (id, name, ip_address, nas_type, secret, api_port, coa_port, api_username, api_password, hotspot_login_url, description)
VALUES (1, 'راوتر الميكروتيك الرئيسي CCR-1009', '192.168.88.1', 'mikrotik', 'radiusSecret123', 8728, 3799, 'admin', 'admin', 'http://192.168.88.1/login', 'راوتر توزيع وتغذية شبكة الهوتسبوت و PPPoE');

INSERT OR IGNORE INTO wisp_packages (id, name, service_type, price, cost, rate_download, rate_upload, burst_download, burst_upload, burst_threshold_down, burst_threshold_up, burst_time, priority, volume_quota_mb, uptime_limit_mins, validity_days, description)
VALUES 
(1, 'باقة 2 ميجا يومية', 'hotspot', 5.00, 2.00, '2M', '1M', '4M', '2M', '1500k', '750k', 16, 8, 2048, 1440, 1, 'باقة اقتصادية يومية بسعة 2 جيجابايت وسرعة 2 ميجا'),
(2, 'باقة 4 ميجا أسبوعية', 'hotspot', 20.00, 8.00, '4M', '2M', '8M', '4M', '3M', '1500k', 16, 6, 10240, 10080, 7, 'باقة أسبوعية بسعة 10 جيجابايت مع خاصية Turbo Burst'),
(3, 'باقة 10 ميجا شهرية - PPPoE المنزلية', 'pppoe', 120.00, 50.00, '10M', '5M', '15M', '8M', '8M', '4M', 20, 4, 0, 0, 30, 'اشتراك شهري غير محدود للمنازل عبر PPPoE');

INSERT OR IGNORE INTO wisp_resellers (id, name, contact_person, phone, email, balance, commission_percent)
VALUES 
(1, 'مركز الخليج للاتصالات', 'أحمد السعيد', '0551234567', 'gulf@reseller.net', 500.00, 15.00),
(2, 'بقالة ومكتبة النور', 'خالد المنصور', '0569876543', 'alnoor@reseller.net', 250.00, 12.00);

INSERT OR IGNORE INTO wisp_card_templates (id, name, width_mm, height_mm, cards_per_row, background_color, border_color, header_color, show_qr, show_logo, show_price, show_serial, show_validity)
VALUES (1, 'قالب كروت قياسي (مودرن مع QR)', 85, 52, 2, '#ffffff', '#2563eb', '#1e40af', 1, 1, 1, 1, 1);

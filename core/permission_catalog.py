"""Additive permission details. Pure metadata; no application or DB imports.

Existing permissions remain prerequisites. The installer inherits existing
grants only when a detail is first inserted, never when an operator revokes it.
"""

GROUPS = {
    'subscribers': 'إدارة المشتركين', 'vouchers': 'نظام الكروت',
    'wallets': 'العمليات المالية', 'invoices': 'العمليات المالية',
    'packages': 'الباقات والسرعات', 'nas': 'خوادم البث (NAS)',
    'managers': 'المدراء والموزعين', 'roles': 'المدراء والموزعين',
    'system': 'إعدادات النظام',
}

# code suffix, Arabic label, legacy prerequisite, administrative endpoints.
DETAILS = (
 ('vouchers.quick_issue', 'إصدار كروت سريع', 'vouchers.generate', 'quick_cards_page quick_cards_issue'),
 ('vouchers.quick_history', 'سجل إصدار الكروت السريع', 'vouchers.view', 'quick_cards_history'),
 ('vouchers.quick_cards', 'عرض ونسخ كروت الإصدار السريع', 'vouchers.view', 'quick_cards_details'),
 ('vouchers.quick_print', 'طباعة وإعادة طباعة الإصدار السريع', 'vouchers.print', 'quick_cards_print'),
 ('system.reseller_issue_policy', 'إعدادات إصدار كروت الموزعين', 'system.settings', 'reseller_issue_settings'),
 ('subscribers.details', 'تفاصيل المشترك وسجل جلساته', 'subscribers.view', 'subscriber_details subscriber_sessions'),
 ('vouchers.active_users', 'مشتركو الكروت وتفاصيلهم', 'vouchers.view', 'active_card_users active_card_user_details'),
 ('vouchers.inspect', 'فحص الكرت', 'vouchers.view', 'card_inspect'),
 ('vouchers.batch_details', 'تفاصيل حزمة الكروت', 'vouchers.view', 'view_batch_details'),
 ('vouchers.designs_view', 'عرض تصاميم الكروت', 'vouchers.print', 'voucher_designs_list card_designer card_designer_short edit_voucher_design edit_voucher_design_direct'),
 ('vouchers.designs_manage', 'إنشاء وحفظ وحذف تصاميم الكروت', 'vouchers.actions', 'new_voucher_design save_card_design_api save_card_designer_action delete_card_design_api duplicate_card_design_api set_default_card_design_api upload_card_bg_api'),
 ('vouchers.batch_edit', 'تعديل حزمة الكروت', 'vouchers.actions', 'edit_batch_action'),
 ('vouchers.card_edit', 'تعديل بيانات الكرت', 'vouchers.actions', 'edit_card_action edit_active_card_action update_card_action'),
 ('vouchers.activate', 'تفعيل الكرت', 'vouchers.actions', 'api_activate_voucher'),
 ('vouchers.disconnect', 'فصل جلسة الكرت', 'vouchers.actions', 'api_disconnect_voucher'),
 ('vouchers.extend', 'إضافة أيام وبيانات للكرت', 'vouchers.actions', 'api_extend_voucher'),
 ('vouchers.reset_mac', 'فك ارتباط الكرت بالجهاز', 'vouchers.actions', 'api_reset_voucher_mac'),
 ('invoices.sales_view', 'تقارير المبيعات والرسم البياني', 'invoices.view', 'sales_reports'),
 ('invoices.sales_export', 'تصدير المبيعات', 'invoices.view', 'export_sales'),
 ('invoices.create', 'إنشاء فاتورة مشترك', 'invoices.manage', 'create_invoice_action'),
 ('invoices.pay', 'تسجيل سداد فاتورة مشترك', 'invoices.manage', 'pay_invoice_action'),
 ('invoices.design', 'تعديل تصميم الفواتير', 'invoices.manage', 'save_invoice_design_action'),
 ('packages.create', 'إضافة باقة جديدة', 'packages.manage', 'new_package_page add_package_action'),
 ('packages.edit', 'تعديل الباقة ومزامنة سمات الراديوس', 'packages.manage', 'edit_package_action'),
 ('packages.clone', 'نسخ الباقة', 'packages.manage', 'clone_package_action'),
 ('packages.delete', 'حذف الباقة', 'packages.manage', 'delete_package_action'),
 ('nas.details', 'تفاصيل الراوتر وحالته وتشخيصه', 'nas.view', 'nas_details_page nas_diagnostics_page api_nas_diagnostics_status api_nas_live_status api_nas_single_status_compat api_nas_status_compat api_system_ping'),
 ('nas.create', 'إضافة راوتر', 'nas.manage', 'add_nas_action'),
 ('nas.edit', 'تعديل الراوتر', 'nas.manage', 'edit_nas_action api_nas_diagnostics_coa_port'),
 ('nas.delete', 'حذف الراوتر', 'nas.manage', 'delete_nas_action delete_nas_action_alt'),
 ('nas.setup_script', 'إنشاء وتنزيل سكربت ربط الراوتر', 'nas.manage', 'nas_setup_script_page download_nas_script_file'),
 ('nas.coverage_view', 'خريطة التغطية ونقاط البث', 'nas.view', 'coverage_map_page api_coverage_map_data api_access_points_list'),
 ('nas.coverage_manage', 'تعديل خريطة التغطية والأبراج ونقاط البث', 'nas.manage', 'api_coverage_map_update api_access_points_create api_access_points_update api_access_points_delete api_towers_create api_towers_delete'),
 ('nas.l2tp_view', 'عرض أنفاق L2TP وحالتها', 'nas.manage', 'l2tp_tunnels_page tunnels_short api_l2tp_live_status'),
 ('nas.l2tp_manage', 'إدارة أنفاق L2TP وإعداداتها وسكربتاتها', 'nas.manage', 'l2tp_add_tunnel_action l2tp_edit_tunnel_action l2tp_delete_tunnel_action l2tp_delete_tunnel_action_alt l2tp_save_settings_action api_l2tp_settings api_l2tp_mikrotik_script download_l2tp_mikrotik_script'),
 ('nas.wireguard_manage', 'إدارة أنفاق WireGuard ومفاتيحها وسكربتاتها', 'nas.manage', 'wireguard_add_tunnel_action wireguard_edit_tunnel_action wireguard_delete_tunnel_action wireguard_delete_tunnel_action_alt api_wireguard_live_status api_wireguard_generate_keys api_wireguard_mikrotik_script download_wireguard_mikrotik_script'),
 ('nas.portal_view', 'عرض إعدادات لوحة المشترك', 'nas.view', 'subscriber_portal_page'),
 ('nas.portal_manage', 'حفظ إعدادات لوحة المشترك', 'nas.manage', 'api_subscriber_portal_save'),
 ('nas.hotspot_view', 'عرض مولد صفحات الهوتسبوت', 'nas.view', 'hotspot_generator_page'),
 ('nas.hotspot_manage', 'حفظ تصميم صفحة الهوتسبوت', 'nas.manage', 'api_hotspot_generator_save'),
 ('nas.userman_view', 'عرض استيراد User Manager', 'nas.view', 'mikrotik_userman_import_page'),
 ('nas.userman_import', 'تحليل وتنفيذ استيراد User Manager', 'nas.manage', 'api_mikrotik_userman_analyze_rsc api_mikrotik_userman_test_api api_mikrotik_userman_execute api_userman_job_fetch api_userman_job_import api_userman_job_status api_userman_job_result api_userman_job_cancel'),
 ('managers.create', 'إضافة مدير أو موزع', 'managers.manage', 'add_manager_action add_reseller_action'),
 ('managers.edit', 'تعديل المدير أو الموزع', 'managers.manage', 'edit_manager_action'),
 ('managers.status', 'إيقاف وتنشيط المدير أو الموزع', 'managers.manage', 'toggle_manager_status_action'),
 ('managers.delete', 'حذف المدير أو الموزع', 'managers.manage', 'delete_manager_action'),
 ('managers.statement', 'عرض كشف حساب الموزع', 'managers.billing', 'manager_statement_page'),
 ('managers.invoices', 'عرض فواتير الموزعين', 'managers.billing', 'manager_invoices_page manager_operation_receipt'),
 ('managers.wallets_view', 'عرض محافظ الموزعين', 'managers.billing', 'reseller_wallets_page'),
 ('managers.deposit', 'شحن محفظة الموزع نقديًا أو آجلًا', 'managers.billing', 'deposit_manager_action topup_reseller_action api_reseller_wallet_deposit'),
 ('managers.deduct', 'خصم رصيد محفظة الموزع', 'managers.billing', 'deduct_manager_action'),
 ('managers.transfer', 'تحويل الرصيد بين الموزعين', 'managers.billing', 'api_reseller_wallet_transfer'),
 ('managers.settle_debt', 'تسجيل سداد دين الموزع', 'managers.billing', 'settle_manager_debt_action'),
 ('managers.void_invoice', 'إلغاء فاتورة الموزع', 'managers.billing', 'void_manager_invoice_action'),
 ('roles.view', 'عرض الأدوار وصلاحياتها', 'roles.manage', 'roles_page api_role_permissions'),
 ('roles.create', 'إنشاء دور جديد', 'roles.manage', 'add_role_action'),
 ('roles.edit', 'تعديل صلاحيات الدور', 'roles.manage', 'edit_role_action'),
 ('roles.delete', 'حذف الدور', 'roles.manage', 'delete_role_action'),
 ('system.backups_view', 'عرض النسخ الاحتياطية وحالتها', 'system.backups', 'backups_page tools_backups_short api_backup_health'),
 ('system.backups_create', 'إنشاء نسخة احتياطية', 'system.backups', 'api_create_backup'),
 ('system.backups_download', 'تنزيل نسخة احتياطية', 'system.backups', 'download_backup download_backup_file_route'),
 ('system.backups_upload', 'رفع نسخة احتياطية', 'system.backups', 'api_upload_backup'),
 ('system.backups_restore', 'استعادة نسخة احتياطية', 'system.backups', 'api_restore_backup'),
 ('system.backups_delete', 'حذف نسخة احتياطية', 'system.backups', 'api_delete_backup'),
 ('system.backups_settings', 'إعدادات وجدولة النسخ وإرسالها إلى تيليجرام', 'system.backups', 'api_backup_settings api_backup_scheduler_action api_send_backup_telegram'),
 ('system.import_view', 'عرض استيراد البيانات وتحليلها ومتابعتها', 'system.settings', 'import_data_page database_migration_studio download_sample_template api_migration_analyze api_migration_progress'),
 ('system.import_execute', 'تنفيذ وإلغاء استيراد البيانات', 'system.settings', 'process_import_action api_migration_execute api_migration_cancel'),
 ('system.maintenance_view', 'عرض صيانة القاعدة والتدقيق وأحجام الجداول', 'system.settings', 'database_maintenance_page api_maintenance_stats api_table_sizes api_run_database_audit api_cleanup_job_status api_latest_cleanup_job'),
 ('system.maintenance_run', 'تنفيذ تنظيف وأرشفة وتحسين القاعدة وإصلاح التدقيق', 'system.settings', 'api_clean_expired_vouchers api_maintenance_archive_sessions api_optimize_tables api_prune_historical_logs api_fix_audit_issue api_fix_all_audit_issues'),
 ('system.factory_reset', 'طلب وإلغاء إعادة ضبط المصنع', 'system.settings', 'api_factory_reset_database api_factory_reset_cancel api_factory_reset_status'),
 ('system.license_view', 'عرض الترخيص وحالته', 'system.settings', 'license_management license_short license_status license_status_page'),
 ('system.license_manage', 'تفعيل ومزامنة الترخيص', 'system.settings', 'activate_license_action sync_license_heartbeat_action'),
 ('system.settings_save', 'حفظ إعدادات النظام والشبكة', 'system.settings', 'update_settings_action'),
 ('system.time', 'عرض ومزامنة الوقت', 'system.settings', 'api_time_status api_time_sync'),
 ('system.notifications_view', 'عرض مركز الإشعارات', 'system.settings', 'notifications_center_page'),
 ('system.notifications_manage', 'إعداد الإشعارات والتحقق من البوت وإرسال تجربة', 'system.settings', 'api_notifications_settings_save api_notifications_verify_bot api_notifications_test_send'),
 ('system.automation_view', 'عرض قواعد الأتمتة', 'system.settings', 'automation_rules_page'),
 ('system.automation_manage', 'تفعيل وتشغيل قواعد الأتمتة', 'system.settings', 'api_automation_toggle api_automation_run'),
 ('system.loyalty_view', 'عرض المكافآت ونقاط الولاء', 'system.settings', 'loyalty_rewards_page loyalty_rewards_short api_loyalty_reward_get'),
 ('system.loyalty_manage', 'إنشاء وتعديل وحذف وتفعيل المكافآت', 'system.settings', 'api_loyalty_reward_add api_loyalty_reward_edit api_loyalty_reward_delete api_loyalty_reward_toggle'),
 ('system.loyalty_award', 'منح نقاط الولاء', 'system.settings', 'api_loyalty_award_points'),
 ('system.loyalty_redeem', 'استبدال نقاط الولاء بمكافأة', 'system.settings', 'api_loyalty_redeem_reward'),
 ('system.update_view', 'عرض تحديثات النظام والتحقق منها', 'system.settings', 'system_update_page api_system_update_check api_system_update_progress'),
 ('system.update_apply', 'تطبيق تحديث النظام', 'system.settings', 'api_system_update_apply'),
 ('system.autoheal_view', 'عرض التعافي التلقائي والتشخيص والسجلات', 'system.settings', 'autoheal_page api_autoheal_status api_autoheal_logs api_autoheal_diagnostic api_autoheal_probe_ports'),
 ('system.autoheal_manage', 'إعداد التعافي التلقائي وتشغيل الإصلاح والتنظيف', 'system.settings', 'api_autoheal_save_settings api_autoheal_restart api_autoheal_purge_zombies api_autoheal_resolve_alert api_autoheal_clear_resolved_alerts'),
 ('system.whatsapp_view', 'عرض واتساب وحالة الربط ورمز QR', 'system.settings', 'whatsapp_dashboard api_whatsapp_sync_status api_whatsapp_qrcode'),
 ('system.whatsapp_manage', 'إعداد واتساب وإعادة تشغيل الربط', 'system.settings', 'api_whatsapp_save_settings api_whatsapp_restart_instance'),
 ('system.whatsapp_templates', 'عرض وتعديل قوالب واتساب', 'system.settings', 'whatsapp_templates_page api_whatsapp_save_template'),
 ('system.whatsapp_logs', 'عرض ومسح سجلات واتساب', 'system.settings', 'whatsapp_logs_page api_whatsapp_clear_logs'),
 ('system.whatsapp_send', 'إرسال رسائل واتساب الجماعية والتجريبية', 'system.settings', 'whatsapp_broadcast_page api_whatsapp_send_broadcast api_whatsapp_send_test'),
 ('system.whatsapp_simulator', 'عرض وتشغيل محاكي واتساب', 'system.settings', 'whatsapp_simulator_page api_whatsapp_simulate_chat'),
 ('system.archiver_view', 'عرض أرشفة المحاسبة وحالتها', 'system.view_services', 'accounting_archiver_page api_accounting_archiver_status'),
 ('system.archiver_run', 'تنفيذ وضغط أرشيف المحاسبة', 'system.control_services', 'api_accounting_archiver_run api_accounting_archiver_compact'),
 ('system.simulator_view', 'عرض محاكي الراديوس', 'system.view_services', 'radius_simulator_page'),
 ('system.simulator_run', 'تنفيذ اختبار محاكي الراديوس', 'system.control_services', 'api_radius_simulator_test'),
 ('system.alerts_manage', 'حل ومسح تنبيهات النظام وتشغيل فحص المراقب', 'system.control_services', 'api_resolve_system_alert api_clear_resolved_alerts api_watchdog_manual_check'),
)

ACCOUNT_ACTIONS = {
 'delete': ('حذف الحساب', 'delete'), 'extend-time': ('إضافة أو خصم أيام', 'renew'),
 'renew': ('تجديد الاشتراك', 'renew'), 'disconnect': ('فصل الجلسات', 'disconnect'),
 'terminate': ('إنهاء الاشتراك', 'edit'), 'disable': ('إيقاف الحساب', 'edit'),
 'activate-subscriber': ('إعادة تنشيط الحساب', 'edit'),
 'change-package': ('تغيير الباقة وترحيل الرصيد', 'edit'),
 'add-quota': ('إضافة أو خصم بيانات', 'edit'), 'usage-history': ('عرض سجل الاستهلاك', 'view'),
 'add-wallet': ('إضافة رصيد نقدي', 'wallets.manage'),
 'deduct-wallet': ('خصم رصيد نقدي', 'wallets.manage'),
}
ACTION_ENDPOINTS = {
 'api_delete_entity': 'delete', 'api_extend_time': 'extend-time', 'api_renew': 'renew',
 'api_disconnect': 'disconnect', 'api_terminate': 'terminate', 'api_disable_account': 'disable',
 'api_activate_subscriber': 'activate-subscriber', 'api_change_package': 'change-package',
 'api_add_quota': 'add-quota', 'api_usage_history': 'usage-history',
 'api_add_wallet': 'add-wallet', 'api_deduct_wallet': 'deduct-wallet',
}
BATCH_ACTIONS = {
 'delete': ('حذف الحزم والكروت', 'vouchers.delete'),
 'export': ('تصدير كروت الحزم', 'vouchers.print'),
 'change_package': ('تغيير باقة الحزم', 'vouchers.actions'),
 'assign_reseller': ('تخصيص الحزم لموزع', 'vouchers.actions'),
}
PACKAGE_ACTIONS = {
 'delete': 'حذف الباقات جماعيًا', 'activate': 'تفعيل الباقات جماعيًا',
 'deactivate': 'إيقاف الباقات جماعيًا', 'update_price': 'تعديل أسعار الباقات جماعيًا',
 'update_validity': 'تعديل صلاحية الباقات جماعيًا',
 'update_service_type': 'تعديل نوع خدمة الباقات جماعيًا',
 'update_rollover': 'تفعيل أو إيقاف ترحيل الرصيد للباقات',
 'update_loyalty': 'تفعيل أو إيقاف نقاط الولاء للباقات',
 'update_portal_visibility': 'إظهار أو إخفاء الباقات في لوحة المشترك',
}


def permission_rows():
    rows = []
    for suffix, name, parent, endpoints in DETAILS:
        category = 'خدمات النظام' if parent in ('system.view_services', 'system.control_services') else GROUPS[suffix.split('.')[0]]
        rows.append(dict(code='detail.' + suffix, name=name, category=category,
                         description='يتطلب صلاحية القسم الأساسية؛ يقتصر الموزع على بياناته.', parent=parent))
    for kind in ('subscribers', 'vouchers'):
        for action, (name, old) in ACCOUNT_ACTIONS.items():
            parent = old if '.' in old else 'subscribers.' + old if kind == 'subscribers' else 'vouchers.delete' if action == 'delete' else 'vouchers.view' if action == 'usage-history' else 'vouchers.actions'
            rows.append(dict(code='detail.' + kind + '.action_' + action.replace('-', '_'),
                             name=name + (' للمشتركين' if kind == 'subscribers' else ' للكروت'),
                             category=GROUPS[kind], description='للعمليات الفردية والجماعية؛ يتطلب صلاحية القسم الأساسية.', parent=parent))
    for action, (name, parent) in BATCH_ACTIONS.items():
        rows.append(dict(code='detail.vouchers.bulk_' + action, name=name,
                         category=GROUPS['vouchers'], description='عملية جماعية على الحزم المسموحة فقط.', parent=parent))
    for action, name in PACKAGE_ACTIONS.items():
        rows.append(dict(code='detail.packages.bulk_' + action, name=name,
                         category=GROUPS['packages'], description='يتطلب إدارة الباقات.', parent='packages.manage'))
    return rows


# Stable base catalog; insert missing metadata without replaying historical RBAC seed SQL.
BASE_PERMISSIONS = (
    ('subscribers.view', 'عرض المشتركين', 'إدارة المشتركين', 'إمكانية استعراض قائمة وبيانات المشتركين'),
    ('subscribers.create', 'إضافة مشترك جديد', 'إدارة المشتركين', 'إمكانية إنشاء حسابات جديدة للمشتركين وتوثيقها'),
    ('subscribers.edit', 'تعديل بيانات المشترك', 'إدارة المشتركين', 'تعديل الباقة أو كلمة المرور أو البيانات الشخصية'),
    ('subscribers.delete', 'حذف المشتركين', 'إدارة المشتركين', 'حذف حسابات المشتركين من النظام والراديوس'),
    ('subscribers.renew', 'تجديد اشتراك', 'إدارة المشتركين', 'إمكانية تجديد الباقة وتمديد صلاحية المشترك'),
    ('subscribers.disconnect', 'فصل جلسة المشترك (CoA)', 'إدارة المشتركين', 'إرسال أمر طرد وفصل الجلسة الآنية عبر CoA'),
    ('vouchers.view', 'استعراض الحزم والكروت', 'نظام الكروت', 'عرض دفعات الكروت والمستخدمين النشطين'),
    ('vouchers.generate', 'توليد دفعات كروت جديدة', 'نظام الكروت', 'إنشاء وتوليد كروت جديدة وطباعتها'),
    ('vouchers.print', 'طباعة وتصميم الكروت', 'نظام الكروت', 'الوصول لقوالب الطباعة وتصدير الكروت'),
    ('vouchers.delete', 'حذف دفعات الكروت', 'نظام الكروت', 'حذف الحزم والكروت غير المستخدمة'),
    ('vouchers.actions', 'إجراءات الكروت المتقدمة', 'نظام الكروت', 'تعديل مدة الصلاحية وإضافة الرصيد والفصل الفوري'),
    ('wallets.manage', 'إدارة الأرصدة والمحافظ', 'العمليات المالية', 'إيداع وسحب الرصيد النقدي والآجل لحسابات المشتركين والموزعين'),
    ('invoices.view', 'عرض الفواتير والتقارير المالية', 'العمليات المالية', 'استعراض فواتير الاشتراكات ومبيعات الكروت'),
    ('invoices.manage', 'إصدار وتسديد الفواتير', 'العمليات المالية', 'تسجيل الدفعات وتغيير حالة الفواتير إلى مسددة'),
    ('packages.view', 'عرض الباقات', 'الباقات والسرعات', 'استعراض باقات الإنترنت ومحددات السرعة'),
    ('packages.manage', 'إضافة وتعديل الباقات', 'الباقات والسرعات', 'إنشاء باقات جديدة وتعديل أسعارها وسرعاتها'),
    ('nas.view', 'عرض راوترات NAS', 'خوادم البث (NAS)', 'استعراض حالة الراوترات والمراقبة الحية'),
    ('nas.manage', 'إضافة وتعديل وحذف الراوترات', 'خوادم البث (NAS)', 'ربط راوترات جديدة وتعديل منافذ وأسرار RADIUS'),
    ('nas.test_coa', 'اختبار بروتوكول CoA', 'خوادم البث (NAS)', 'إرسال حزم PoD / Disconnect تجريبية للراوتر'),
    ('managers.view', 'عرض المدراء والموزعين', 'المدراء والموزعين', 'استعراض حسابات المدراء وموزعي نقاط البيع'),
    ('managers.manage', 'إدارة المدراء والموزعين', 'المدراء والموزعين', 'إضافة وتعديل وتعطيل حسابات المدراء'),
    ('managers.billing', 'إدارة فواتير وكشوفات الموزعين', 'المدراء والموزعين', 'شحن أرصدة الموزعين نقدياً وآجلاً وإصدار كشوفات الحساب'),
    ('roles.manage', 'إدارة الصلاحيات والأدوار', 'المدراء والموزعين', 'إنشاء وتعديل مجموعات الصلاحيات والأدوار'),
    ('system.view_services', 'مراقبة خدمات النظام', 'خدمات النظام', 'استعراض حالة Web Server و FreeRADIUS و MariaDB'),
    ('system.control_services', 'التحكم بالخدمات والتعافي', 'خدمات النظام', 'إعادة تشغيل وإيقاف خدمات النظام'),
    ('system.backups', 'النسخ الاحتياطي', 'إعدادات النظام', 'أخذ واستعادة وتحميل النسخ الاحتياطية لقاعدة البيانات'),
    ('system.settings', 'إعدادات النظام والشبكة', 'إعدادات النظام', 'تعديل هوية الشبكة والشعار ومنافذ الاتصال الافتراضية'),
    ('system.audit_logs', 'سجلات التدقيق والأمان', 'إعدادات النظام', 'استعراض سجلات تتبع عمليات المدراء'),
)


def base_permission_rows():
    return [dict(code=code, name=name, category=category, description=description)
            for code, name, category, description in BASE_PERMISSIONS]


def standard_role_permissions(code):
    """Explicit operational profiles. No role inherits superadmin's bypass."""
    manager_parents = {
        'subscribers.view', 'subscribers.create', 'subscribers.edit',
        'subscribers.delete', 'subscribers.renew', 'subscribers.disconnect',
        'vouchers.view', 'vouchers.generate', 'vouchers.print', 'vouchers.delete',
        'vouchers.actions', 'packages.view', 'packages.manage', 'nas.view',
        'invoices.view', 'system.view_services',
    }
    shared_reads = {
        'detail.vouchers.active_users', 'detail.vouchers.inspect',
        'detail.vouchers.batch_details', 'detail.vouchers.quick_history',
        'detail.vouchers.quick_cards', 'detail.vouchers.quick_print',
        'detail.invoices.sales_view', 'detail.invoices.sales_export',
        'detail.managers.statement', 'detail.managers.invoices',
        'detail.managers.wallets_view',
    }
    if code == 'manager':
        grants = set(manager_parents)
        for row in permission_rows():
            if row['parent'] not in grants:
                continue
            if row['code'].startswith(('detail.subscribers.', 'detail.vouchers.', 'detail.packages.')):
                grants.add(row['code'])
            elif row['code'] in {
                'detail.nas.details', 'detail.nas.coverage_view',
                'detail.invoices.sales_view', 'detail.invoices.sales_export',
            }:
                grants.add(row['code'])
        # Infrastructure and system jobs are separately authorized, not implied by monitoring.
        return grants
    if code in ('main_reseller', 'pos_agent'):
        grants = {'vouchers.view', 'vouchers.print', 'vouchers.actions',
                  'invoices.view', 'managers.billing'} | shared_reads
        grants.update({'detail.vouchers.activate', 'detail.vouchers.action_activate_subscriber'})
        if code == 'main_reseller':
            grants.update({'vouchers.generate', 'detail.vouchers.quick_issue'})
        return grants
    raise ValueError('Unknown standard operational role')


ENDPOINT_DETAILS = {endpoint: 'detail.' + suffix for suffix, _, _, endpoints in DETAILS for endpoint in endpoints.split()}


def required_detail(endpoint, data=None):
    endpoint = (endpoint or '').rsplit('.', 1)[-1]
    data = data or {}
    if not hasattr(data, 'get'):
        return '!invalid'
    if endpoint in ACTION_ENDPOINTS:
        kind = {'subscriber': 'subscribers', 'voucher': 'vouchers', 'card': 'vouchers'}.get(data.get('target_type', 'subscriber'))
        return 'detail.' + kind + '.action_' + ACTION_ENDPOINTS[endpoint].replace('-', '_') if kind else '!invalid'
    if endpoint in ('batches_bulk_action', 'packages_bulk_action'):
        action = str(data.get('action') or '').strip()
        actions, kind = (BATCH_ACTIONS, 'vouchers') if endpoint == 'batches_bulk_action' else (PACKAGE_ACTIONS, 'packages')
        return 'detail.' + kind + '.bulk_' + action if action in actions else '!invalid'
    return ENDPOINT_DETAILS.get(endpoint)

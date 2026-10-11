# خريطة المشروع
هذا الملف نقطة البداية؛ لا تقرأ جميع الخدمات. الفهرس المولد في `generated/source-index.json` يحدد التعريفات والمسارات ومواقعها، ولا يثبت تدفق التنفيذ.

| المجال | نقاط الدخول والملفات المسؤولة | وثيقة المجال |
|---|---|---|
| التشغيل والتكوين | [run.py](../run.py)، [core/config.py](../core/config.py)، [entrypoint.sh](../entrypoint.sh)، [web/__init__.py](../web/__init__.py) | [التطوير](DEVELOPMENT.md) |
| المشتركون والعقود | [routes/subscribers.py](../web/routes/subscribers.py)، [subscriber_service.py](../services/subscriber_service.py)، [subscribers.html](../web/templates/subscribers.html) | [القواعد](BUSINESS_RULES.md) |
| الكروت والحزم والطباعة | [routes/vouchers.py](../web/routes/vouchers.py)، [voucher_service.py](../services/voucher_service.py)، [card_design_service.py](../services/card_design_service.py)، [active_card_users.html](../web/templates/active_card_users.html) | [القواعد](BUSINESS_RULES.md) |
| التجديد وتغيير الباقة والكوتا والمحفظة | [quick_action_service.py](../services/quick_action_service.py)، [user_portal_service.py](../services/user_portal_service.py)، [quota_service.py](../services/quota_service.py) | [القواعد](BUSINESS_RULES.md) |
| لوحة المشترك والسلفة | [routes/portal.py](../web/routes/portal.py)، [user_portal_service.py](../services/user_portal_service.py)، [subscriber_portal_service.py](../services/subscriber_portal_service.py) | [API](API.md) |
| المكافآت | [loyalty_rewards_service.py](../services/loyalty_rewards_service.py)، [loyalty_schema.py](../database/loyalty_schema.py) | [القواعد](BUSINESS_RULES.md) |
| الإيقاف والتفعيل والحذف | [account_lifecycle_service.py](../services/account_lifecycle_service.py)، [quick_action_service.py](../services/quick_action_service.py) | [القواعد](BUSINESS_RULES.md) |
| المحاسبة والعدادات | [routes/accounting.py](../web/routes/accounting.py)، [queries.conf](../docker/freeradius/queries.conf)، [quota_service.py](../services/quota_service.py) | [قاعدة البيانات](DATABASE.md) |
| الفصل وتسوية الدورة | [coa_queue_service.py](../services/coa_queue_service.py)، [renewal_settlement_service.py](../services/renewal_settlement_service.py)، [nas_session_settlement.py](../services/nas_session_settlement.py)، [nas_session_safety.py](../database/nas_session_safety.py) | [قرار التسوية](decisions/001-session-settlement.md) |
| سياسة الجلسة على الراوتر | [online_policy_service.py](../services/online_policy_service.py)، [core/coa.py](../core/coa.py)، [mikrotik_api.py](../core/mikrotik_api.py) | [البنية](ARCHITECTURE.md) |
| الباقات ومزامنة RADIUS | [routes/packages.py](../web/routes/packages.py)، [radius_sync.py](../core/radius_sync.py)، [rate_limit.py](../core/rate_limit.py) | [القواعد](BUSINESS_RULES.md) |
| الراوتر والأنفاق | [routes/nas.py](../web/routes/nas.py)، [nas_service.py](../services/nas_service.py)، [nas_diagnostics_service.py](../services/nas_diagnostics_service.py)، [l2tp_service.py](../services/l2tp_service.py)، [docker/l2tp](../docker/l2tp) | [النشر](DEPLOYMENT.md) |
| الترخيص والمقاعد | [license_guard_service.py](../services/license_guard_service.py)، [license_capacity.py](../database/license_capacity.py)، [license-capacity-postauth.conf](../docker/freeradius/license-capacity-postauth.conf) | [الأمان](SECURITY.md) |
| الاستيراد | [import_service.py](../services/import_service.py)، [database_migration_service.py](../services/database_migration_service.py)، [mikrotik_userman_importer.py](../services/mikrotik_userman_importer.py)، [import_state_service.py](../services/import_state_service.py) | [قاعدة البيانات](DATABASE.md) |
| إعادة المصنع والصيانة | [factory_reset_service.py](../services/factory_reset_service.py)، [maintenance_job_service.py](../services/maintenance_job_service.py)، [db_maintenance_service.py](../services/db_maintenance_service.py)، [hosted controller](../deploy/hosted-max/README.md) | [النشر](DEPLOYMENT.md) |
| النسخ والاستعادة | [backup_service.py](../services/backup_service.py)، [backup_scheduler_service.py](../services/backup_scheduler_service.py) | [الفحص](TESTING.md) |
| الموزعون والمديرون | [routes/resellers.py](../web/routes/resellers.py)، [reseller_wallet_service.py](../services/reseller_wallet_service.py)، [manager_service.py](../services/manager_service.py)، [rbac.py](../core/rbac.py) | [الأمان](SECURITY.md) |
| الواجهة والتعريب | [base.html](../web/templates/base.html)، [web/templates/components](../web/templates/components)، [web/static](../web/static)، [web/utils.py](../web/utils.py) | [الواجهة](FRONTEND.md) |
| الزمن والإحصاءات والمراقبة | [time_service.py](../core/time_service.py)، [stats_service.py](../services/stats_service.py)، [watchdog.py](../core/watchdog.py)، [health_probe_service.py](../services/health_probe_service.py) | [البنية](ARCHITECTURE.md) |

لوحة إدارة الدومينات مشروع مستقل؛ انظر [حدود مساحة العمل](WORKSPACE.md). لا تخلط `core/config.py` مع إعدادات اللوحة.

للبحث المحدد: `python scripts/project_tools.py index` ثم `python scripts/project_tools.py find action_renew_package`. أسماء الاختبارات وأوامرها في [TESTING](TESTING.md).

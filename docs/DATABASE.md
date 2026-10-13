# قاعدة البيانات والترحيلات
مصدر الاتصال: [database/db.py](../database/db.py). الاتصال يلتزم بالخلفية المحددة ويمنع التحول التلقائي من MySQL إلى SQLite عند تعطل DB. استخدم معاملات placeholders و`db_session`/أدوات المعاملات الموجودة بدل تركيب قيم SQL.

| المجموعة | الجداول/المصدر |
|---|---|
| هوية الحساب وعقده | `wisp_subscribers`, `wisp_vouchers`, `wisp_packages`, `wisp_voucher_batches` |
| RADIUS | `radacct`, `radpostauth`, `radcheck`, `radreply`, `radusergroup`, `radgroupreply` |
| المالية | `wisp_voucher_sales`, `wisp_invoices`؛ بقية جداول المحفظة والموزعين في الخدمات المعنية |
| المقاعد | `wisp_license_runtime_state`, `wisp_session_reservations`؛ [license_capacity.py](../database/license_capacity.py) |
| حماية الدورة | `wisp_renewal_guards`, `wisp_username_write_lock`؛ [cycle_safety.py](../database/cycle_safety.py) |
| أدلة التسوية | `wisp_nas_session_seals`, `wisp_online_policy_outbox`؛ [nas_session_safety.py](../database/nas_session_safety.py) |
| المكافآت | `wisp_loyalty_wallets`, `wisp_loyalty_rewards`, `wisp_loyalty_transactions`؛ [loyalty_schema.py](../database/loyalty_schema.py) |
| الصيانة | `wisp_maintenance_jobs`؛ [maintenance_jobs.py](../database/maintenance_jobs.py) |

مصادر إنشاء المخطط: `database/schema_mysql.sql` و`init_db.sql`، وتعريفات الترقية/التريغرات في `database/schema_healer.py` والوحدات المتخصصة. `database/full_db.sql` dump تاريخي مستبعد من Git؛ لا تعدله كبديل لترحيل ولا تغير أعمدته مع INSERT positional قديم.

عند تعديل المخطط: حدّث كل مسار إنشاء يستخدم التعريف، وثبّت ترحيلًا قابلًا لإعادة التشغيل ويحافظ على البيانات. تحقق من أنواع القيم والفهارس والتريغرات والقراءات والكتابات في الخدمات. احتفظ بنسخة قبل الترحيل الحي ضمن نطاق التفويض. DDL في MySQL قد يثبت ضمنيًا؛ لا تفترض أن rollback يعكس ALTER.

اختبر التريغر من اتصال مستقل تحت التزامن؛ الاختبار الوظيفي التسلسلي لا يكشف gap locks. تشغيل ترحيل البيع مجددًا يجب ألا يغيّر المبيعات أو ينقل مفتاح التفعيل الموجود. [قرار البيع](decisions/002-first-activation-sale.md).

الاستيراد: حدد نوع المحرك وخيار التفاصيل، ثم قارن المصدر بالنتيجة باستخدام أعداد وIDs وحالات وأرصدة وجلسات. راجع `@max_radius_import` وآلية snapshots في المحرك المعني قبل إدخال تاريخ محاسبة قد يشغل تريغرات التفعيل.

امتداد بيانات صلاحيات المدراء: `database/permission_catalog.py`، يستدعيه bootstrap MySQL بعد التهيئة. المعاملة تضيف إلى wisp_permissions وwisp_role_permissions فقط؛ لا تغير المخطط أو تعيد منح خيارات موجودة. المصدر الوصفي `core/permission_catalog.py`؛ راجع MANAGERS لتبعيات الصلاحيات وحدود ملكية الموزع. لا تستدعِ init_database كله لتحديث هذا الكتالوج في نسخة تعمل؛ استخدم المثبت المحدد ضمن نشر مصرح به.

أداة المقارنة المحلية تقبل JSON مخطط فقط بصيغة `{"tables":{"table":{"columns":{},"indexes":{}}}}`؛ لا ترفع بيانات العملاء أو credentials إلى هذه الملفات.

## إيصالات الإصدار السريع

`database/quick_card_schema.py:install_quick_card_schema` يضيف جدول wisp_quick_card_issues، بمفتاح فريد (actor_id,request_key) وفهرس الملكية. يستدعيه bootstrap MySQL بعد تركيب RBAC؛ النشر الذي يتجاوز bootstrap يشغله صراحة قبل الويب. CREATE IF NOT EXISTS متكرر آمن، وDDL مستقل قبل أي إصدار مالي. لا ترحيل لأرصدة أو تعديل لصفوف قديمة. لا FK للحزمة حتى يبقى إيصال العملية بعد حذف كروتها؛ IDs الموجودة رتيبة ولا يعاد استخدامها. إعادة المصنع يمسح هذا السجل مع الكروت؛ إعداد صيغة الإصدار العام يبقى ضمن إعدادات النظام كالمعتاد.

# جرد مسارات الحذف وتغطية الإصلاح — 3 نوفمبر 2026

> هذا جرد تاريخي قبل الإصلاح. أغلقت فجوات الربط والفلاتر والاختبارات محليًا في [القبول اللاحق](EXPIRED_DELETION_ALL_PATHS_LOCAL_20261103.md). وصف «الحزم تتجاوز الإصلاح» أدناه لا يمثل المصدر الحالي.


## الحكم

المراجعة الحالية تتبعت المصدر والقوالب فقط، ولم تشغل حذفًا أو مجدولًا أو عميلًا. الاختبارات السابقة قاست الخدمة المشتركة على نسخة العميل محليًا، لا كل زر أو مرشح أو حدث مجدول منفردًا. **لا اعتماد شامل لجميع مسارات الحذف:** حذف الحزم ما زال يتجاوز purge_details الجديد ويحتفظ بتفاصيل التاريخ وفق preserve_accounts القديم.

## خريطة نقاط الدخول

| نقطة الدخول | مسار التنفيذ الحالي | الإصلاح الجديد | مستوى الدليل |
|---|---|---|---|
| صفحة صيانة قاعدة البيانات، حذف كل المنتهي | database_maintenance.html → POST /api/tools/database-maintenance/clean-expired → start_cleanup_job → _worker → delete_expired_vouchers(all) → delete_account(voucher) → purge_details إذاstatus=expired | يصل إليه | الخدمة الحقيقية all اختُبرت؛ لم يختبر زرHTTP/العامل من الواجهة لهذه الجولة |
| الصيانة، انتهاء الوقت فقط | نفس السلسلة معdelete_type=time | يصل إليه بعد اختيار المرشح | تتبع مصدر؛ لم ينفذ مرشحtime مستقلًا |
| الصيانة، نفاد البيانات فقط | نفس السلسلة معdelete_type=quota | يصل إليه بعد اختيار المرشح | تتبع مصدر؛ لم ينفذ مرشحquota مستقلًا |
| تنظيف الكروت المجدول | backup_scheduler_service._scheduled_automation_rules_job → run_due_rules → execute_rule_now(CLEAN_EXPIRED_CARDS) → delete_expired_vouchers(min_age_days≥60) | يصل إليه | تتبع مصدر؛ لا تشغيل مجدول جديد أو إثبات أنه مفعل حيًا |
| قائمة مشتركي الكروت، فردي/جماعي | active_card_users.html → /api/actions/delete → action_bulk_execute → action_delete_entity → delete_account(voucher) | يصل إليه للكرت المنتهي فقط | الخدمة الأساسية والتزامن/rollback مختبرة؛ زر/صلاحياتHTTP الفردي والجماعي لم يختبرا جديدًا |
| حذف الحزمة المفردة | /vouchers/delete-batch/id → voucher_service.delete_batch → delete_voucher_batch | **يتجاوز الإصلاح** في مسار الحذف النهائي | مؤكد بالمصدر: preserve_accounts ثمDELETEحسبbatch_id |
| حذف الحزم الجماعي | /vouchers/batches/bulk-action(action=delete) → حلقةdelete_batch | **يتجاوز الإصلاح** كما في المفرد | تتبع مصدر، لا اختبار مالي/استرداد جديد |
| قائمة المشتركين الحقيقيين، فردي/جماعي، أو/subscribers/delete/id | action_delete_entity أوsubscriber_service.delete_subscriber → delete_account(subscriber) | لا ينفذ purge الخاص بالكروت؛ يحافظ على التاريخ القديم | اختلاف نوع حساب مقصود في الكود؛ لا يدعى قبول مسار حذف مختزل للمشتركين |
| حذف الباقة من صفحة الباقات | delete_packages | لا يحذف الكروت أصلًا؛ يرفض إذا ارتبطت به حسابات/دفعات | تتبع مصدر |
| الاستيراد البديل والتراجع عن استيراد وإعادة المصنع | database_migration_service وFACTORY_RESET_TABLES | مسارات استبدال/استرجاع/مسح مستقلة وليست تنظيف كروت منتهية | تتبع فقط؛ لا يجوز وصلها بعمليات حذف عادية عشوائيًا |

## فروق مؤكدة تحتاج الانتباه

1. delete_voucher_batch في account_lifecycle_service يحتجز الحزمة والكروت ويمنع الحذف مع جلسة/حجز، ثم يحسب رد حصة الكروت غير المستخدمة ويكتب حركة الاسترداد في نفس المعاملة. بعدها يستدعي preserve_accounts لجميع الكروت ويحذفها حسبbatch_id. لا يستدعي purge_details في المسار الطبيعي؛ الاستدعاء الجزئيdelete_account عند اكتشاف جلسات ليس بديلًا عن ربط الحذف النهائي. لهذا لا يجوز تعميم نتيجة22.31MiBعلى حذف الحزم من الواجهة.
2. فلترquota يعتمد علىنصexpire_reason:البيانات/كوتا/quota. فلترtimeهو عكس هذا الشرط، وليس اختبارexpires_atوحده؛ يشمل السبب الفارغ أو أي سبب آخر، مثل الإنهاء الإداري، ما دامت الحالةexpired. لذلك لا يثبت أن زر«الوقت» يختار انتهاء الأيام فقط.
3. إحصاءexpired_by_quotaفيget_maintenance_statsيفحصالبيانات/كوتافقط، بينما الحذف يفحصquotaبالإنجليزيةأيضًا. يوجد اختلاف بين عقد العداد والاختيار عند وجود سبب إنجليزي، ولم يُقَس انتشاره في بيانات عميل حي.
4. القاعدة المجدولةCLEAN_EXPIRED_CARDSتمررحدًا أدنى60يومًا؛ العمر يطبق علىexpires_at، وليس وقت انتقالstatusإلىexpired. expires_at=NULLأوتاريخالصلاحيةالمستقبليللكرتالمنتهيبالرصيدلايجتازهذاالمرشح. ليس المجدول مساويًا للحذف اليدويall.
5. delete_accountيفعل التنظيف المختزل إذاkind=voucherوstatus=expired. غير المنتهي أوsubscriberيبقىفيpreserve_accountالتفصيلي؛ لا يوصف الإصلاح بأنه حذف مختزل لكل حالات الكروت وكل أنواع الحسابات.

## التغطية المطلوبة قبل اعتماد جميع المداخل

اختبار اختيارall/time/quotaببيانات تشملصلاحية،نفادبيانات،نفادوقتاتصال،سببفارغ،إنهاءإداري،وسببإنجليزي؛ مطابقةالعدادمعالمحدداتوالكروتالباقية. اختبارالمجدولمعحالاتحد60يومًاوNULLوالتجديدالمتزامن. اختبارHTTPوالصلاحياتوالملكيةوالعاملغيرالمتزامنوفقدالردوالحذفالفرديوالجماعي. توحيدحذفالحزمالمنتهيةمعالحمايةالجديدةدونتكراراستردادأوتغييررصيد،واختبارحزمةمختلطةوحذفجماعيوتزامنوفشلوإعادةمحاولة. الحفاظعلىفصلالباقاتوالاستيرادوإعادةالمصنعوالمشتركينعنسياسةالكروتالمحددة.

هذه فجوات ربط/اختبار، لا ادعاء تنفيذ إصلاحاتها في هذا الجرد. مصدر النتيجة الكود الحالي: services/account_lifecycle_service.py، db_maintenance_service.py، maintenance_job_service.py، automation_rules_service.py، backup_scheduler_service.py، quick_action_service.py، voucher_service.py، subscriber_service.py، web/routes/system.py وvouchers.py وsubscribers.py، وقوالبdatabase_maintenance وactive_card_users وsubscribers. [نتائج الاختبار السابق وحدوده](EXPIRED_VOUCHER_SUMMARY_BACKUP_PURGE_LOCAL_20261103.md).

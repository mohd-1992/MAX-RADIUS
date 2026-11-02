> تحديث لاحق: نُفذ الربط محليًا؛ راجع [نتائج التنفيذ](OPERATION_NOTIFICATIONS_270_20261030.md). وصف «لم ينفذ» أدناه يوثق جولة الجرد الأصلية.

# حصر اختفاء إشعارات العمليات مبكرًا — 30 أكتوبر 2026

الحالة: تشخيص مصدر ومحاكاة مؤقتات فقط على مصدر MAX2.7.0 المحلي. لم ينفذ أي حذف أو تجديد أو استعادة أو إرسال أو اتصال NAS أو SQL أو Docker أو نشر، ولم يتغير كود الإشعارات بهذه الجولة. لا تعمم النتيجة على صورة منشورة لم تفحص هويتها.

## النتيجة والسبب المثبت

طبقة `Swal.fire` في base تحول نافذة انتظار غير تفاعلية إلى toast أعلى اليسار. رغم اكتشاف didOpen/showLoading وisLoading=True، تحسب المدة بـ`opts.timer || (opts.icon === 'error' ? 5000 : 3800)` ثم تمرر duration صراحة إلى المكوّن. لذلك تتجاوز القيمة الصريحة افتراضي المكوّن الذي كان سيجعل isLoading بلا مؤقت. يبدأ إغلاق الإشعار بعد3.8ثانية ويزال العنصر بعد280ms إضافية. لا ينتظر اكتمال fetch أو المهمة. allowOutsideClick=False/showConfirmButton=False لا يعطلان هذا المؤقت، وtimer=0 وحده لا يعمل مع عامل ||.

أثبت تشغيل JavaScript الأصلي لمحرك الإشعارات والجسر في VM بساعة وDOM مصطنعين: ظهر إشعار انتظار ثم أزيل عند4080ms دون وجود إشارة انتهاء للعملية. direct showToastNotification(isLoading=True) بلا مدة صريحة بقي ظاهرًا بعد100ثانية في المحاكاة. تكررت النتيجة نفسها لمحرك بوابة المشترك؛ لا توجد في الحصر الحالي استدعاءات انتظار showLoading فعلية من صفحات البوابة، لذلك هي قابلية كامنة في الجسر وليست عمليات إضافية مثبتة في البوابة.

سبب إضافي مستقل: كل Swal.fire لاحق غير تفاعلي يغلق activeLoadingToast السابق، وSwal.close يغلق آخر إشعار انتظار عام؛ قد يغلق إشعار عملية أخرى ما زالت جارية. الإغلاق اليدوي ممكن أيضًا. هذه أسباب عرض؛ اختفاء الإشعار لا يلغي طلب الخادم ولا يثبت فشل العملية أو نجاحها. قد تصل النتيجة لاحقًا ويظهر إشعار جديد. لا تعِد العملية المالية بسبب اختفاء الإشعار وحده.

**حد النتيجة:** وجود المؤقت مثبت؛ القول إنه يختفي قبل انتهاء كل عملية بعينها غير مثبت. العملية الأسرع من3.8ثانية قد تنتهي أولًا؛ المتأثرة عمليًا هي الطلبات الأطول أو التي تطول بسبب الشبكة أو الضغط. لم تقس مدد حية أو تشغل عمليات العملاء لتثبتها.

## الحصر بحسب الصفحة

52 موضع انتظار في المصدر ضمن17ملفًا مستخدمًا في الصفحات/المتابعة المشتركة، لا52عملية مستقلة. تشمل النسخ المتكررة للوظيفة نفسها وتعاريف CoA القديمة والجديدة. helper إضافي في unified_import يرفع عدد المواضع المعرفة إلى53 لكنه غير مستدعى في المصدر الحالي؛ فصل أدناه ولا يحسب مسارًا فعليًا.

| الصفحة/المكوّن | إشعارات الانتظار المعرضة للمؤقت | عدد المواضع |
|---|---|---:|
| المشتركون | جارٍ الحذف، تمديد الصلاحية، إنهاء الاشتراك، تجديد الباقة، تغيير الباقة، إضافة الكوتا، إيداع الرصيد، خصم الرصيد، إرسال أمر الفصل | 9 |
| مشتركو الكروت | العمليات التسع نفسها على الكروت | 9 |
| تفاصيل المشترك وتفاصيل مشترك الكرت وفحص الكرت | إرسال أمر/حزمة الفصل في الصفحات الثلاث، فك الماك والتمديد في فحص الكرت | 5 |
| متابعة الجلسات المشتركة | «جارٍ التحقق من الجلسات»؛ يتابع الفصل اليدوي وتطبيق سياسة MAC بعد الحفظ | 1 |
| النسخ الاحتياطية | حفظ الإعدادات، تنفيذ إجراء الجدولة، إنشاء النسخة الشاملة، رفع النسخة، استعادة قاعدة البيانات والملفات، الرفع والإرسال إلى Telegram | 6 |
| صيانة قاعدة البيانات وأداة الأرشفة | نقل وأرشفة السجلات في الصفحتين، تنظيف السجلات القديمة، إعادة بناء الجداول وتحرير المساحة، الإصلاح الهندسي الشامل | 5 |
| خدمات النظام والتعافي الذاتي | تشغيل/إيقاف/إعادة تشغيل خدمة، فحص الصحة الذاتية، اختبار منافذ الحاويات، تنظيف الجلسات العالقة، إعادة تشغيل الحاوية | 5 |
| أجهزة NAS وتفاصيل الراوتر | فحص منفذ/حزمة CoA؛ في كل قالب تعريفان للاختبار نفسه، لا أربعة اختبارات مستقلة. التعريف السفلي قد يحل محل العلوي عند اكتمال تحميل scripts | 4 |
| مركز الإشعارات | التحقق من البوت، حفظ الإعدادات، إرسال تنبيه اختبار | 3 |
| الباقات | تطبيق التعديلات الجماعية، نسخ الباقة | 2 |
| الأتمتة | تشغيل قاعدة الأتمتة يدويًا «جارٍ التنفيذ» | 1 |
| حزم الكروت | «جارٍ تجهيز الملف» لتصدير Excel/PDF، وحذف حزمة الكروت «جارٍ الحذف» | 2 |

### أولوية المراجعة

1. إنشاء/استعادة/رفع/إرسال النسخ، تصدير الكروت Excel/PDF، الأرشفة والتحسين وإصلاح قاعدة البيانات؛ أعمال قد تطول طبيعيًا.
2. إشعار «جارٍ التحقق من الجلسات» الجديد في session-operations.js: polling قد يستمر حتى200ثانية، بينما الإشعار نفسه مؤقت؛ قد تستمر المتابعة دون واجهة انتظار مرئية. يشمل تطبيق MAC بعد الحفظ، ولا يعني أن السياسة لم تحفظ. نافذة تحرير MAC نفسها ذات زر إلغاء/preConfirm ليست مصابة بالمؤقت العام.
3. التجديد وتغيير الباقات والمحافظ والحذف: الخطر في سوء تفسير الانتظار وإعادة النقر، لا دليل هنا على تكرار مالي حدث بالفعل. حواجز/idempotency الخلفية تختلف حسب العملية ولا تقبل بالتعميم.
4. بقية اختبارات الشبكة/الإرسال والإعدادات: تتأثر إذا طال الطلب، ولا يفترض أن كل حفظ بسيط يستغرق أكثر من المؤقت.

## غير المصابة بهذا المؤقت التلقائي وفق المصدر

| المسار | سبب الفصل عن المشكلة |
|---|---|
| PDF جلسات الكرت والمشترك بعد التوحيد الأخير | يستخدم المكوّن مباشرة مع isLoading وduration=0، وزر إلغاء؛ يبقى حتى نتيجة fetch ثم ينتقل لإشعار نتيجة منفصل |
| رفع/معالجة الصورة في مصمم الكروت | notify يمرر duration=0 أثناء التحميل، ثم يغلقه عند الرد؛ الإغلاق اليدوي ليس انتهاء آليًا |
| متابعة User Manager في الاستيراد الموحد | UsermanProgress يستخدم نافذة حقيقية showCancelButton=True وpolling؛ الجسر لا يحولها إلى toast مؤقت |
| نافذة التحميل في أداة MikroTik Userman القديمة | loadingOverlay عنصر صفحة يظهر/يخفى صراحة؛ ليس إشعار toast ذا مؤقت |
| إعادة المصنع | asModal=True؛ متابعة ومهام مستقلة عن toast الانتظار العام |
| حذف الكروت المتسلسل من الصيانة | لوحة cleanupJobProgress داخل الصفحة مع متابعة حالة المهمة |
| تحديث النظام | عناصر progressBarFill/progressLiveLabel داخل الصفحة تتابع المتحكم، وليست إشعار انتظار مؤقتًا |
| نماذج التأكيد/الإدخال ذات showCancelButton أو input أو asModal | نوافذ تفاعلية أصلية؛ انتهاء النافذة بإجراء المستخدم ليس دليل المشكلة هنا |
| إشعارات النجاح/الفشل بعد الرد | اختفاؤها الزمني تصميم طبيعي للنتائج، ولا تدخل في حصر انتظار عملية جارية |

هذه استثناءات للمؤقت فقط، وليست ضمان اكتمال محاسبة/NAS أو ضمانًا ضد الإغلاق اليدوي أو تغيير الصفحة أو أخطاء الشبكة. تصدير المبيعات Excel/CSV يبدأ كرابط تنزيل أصلي بلا إشعار انتظار؛ هو نقص توحيد تجربة آخر ولا يحسب كإشعار يختفي.

## helper غير مستدعى وحدود العد

`showMikrotikLoading` في tools/unified_import.html معرف عند1736 ويستخدم Swal.showLoading، لكنه لا يملك استدعاءً فعليًا في المصدر الحالي؛ جلب User Manager الحالي يمر عبر UsermanProgress. لهذا لا ينسب خلل3.8ثانية إلى مسار الاستيراد الحالي اعتمادًا على وجود helper قديم وحده.

## الملفات التي تفسر الجذر

- [base](../web/templates/base.html): المكوّن عند2488، مدة الجسر عند2758، إنشاء toast عند2759، والتعامل مع activeLoadingToast في2674وما بعدها. أرقام السطور مؤرخة وقابلة للتغير.
- [user_base](../web/templates/user_portal/user_base.html): نسخة الجسر المكافئة عند1255؛ خطر كامن من دون مستهلك انتظار مثبت في صفحات البوابة هنا.
- [متابعة الجلسات](../web/static/session-operations.js): إشعار الانتظار عند13، حلقة polling مستقلة عن عمر الإشعار.
- [PDF الموحد](../web/static/heavy-export.js): الاستدعاء المباشر duration=0 عند19؛ لا تعِده إلى جسر Swal المؤقت.

## اقتراح الإصلاح، لم ينفذ

- اجعل انتظار العملية بلا مؤقت، مع إزالة مؤشر مدة العرض وعدم تسميته تقدم ملف.
- اربط كل إشعار بمعرف/مقبض العملية ويغلقه الرد أو الحالة النهائية المطابقة فقط؛ إشعار عام جديد أو اكتمال طلب آخر لا يغلق انتظارًا ما زال نشطًا.
- تعامل مع timeout/فقد الرصد كحالة غير مؤكدة، دون ادعاء فشل أو إعادة عملية مالية تلقائية. افصل إغلاق الإشعار عن إلغاء مهمة الخادم.
- اختبر طلبًا أبطأ من4ثوانٍ وعمليتين متزامنتين والنجاح/الفشل/الإلغاء وتغيير الصفحة وإعادة المتابعة؛ الاختبارات المالية تكون في fixture جديدة، لا بإعادة عملية على عميل.
- احفظ الثيمين والمكان والـRTL وCSRF والصلاحيات وحماية تأكيدات الإدخال. لا تجعل كل إشعارات النظام دائمة؛ التثبيت يخص حالة الانتظار فقط.

## فهرس المواضع للمراجعة

الروابط إلى الملف، ورقم السطر وقت الفحص. بعض عناوين العمليات ديناميكية؛ وصف الطلب ومكان الاستدعاء مثبتان، ولا يشغل هذا الجدول العملية.

| الملف | السطر | النص/الدالة | الطلب التالي بحسب المصدر |
|---|---:|---|---|
| [active_card_users.html](../web/templates/active_card_users.html) | 1388 | جارٍ الحذف... | `/api/actions/delete` |
| [active_card_users.html](../web/templates/active_card_users.html) | 1432 | جارٍ تمديد الصلاحية... | `/api/actions/extend-time` |
| [active_card_users.html](../web/templates/active_card_users.html) | 1471 | جارٍ إنهاء الاشتراك... | `/api/actions/terminate` |
| [active_card_users.html](../web/templates/active_card_users.html) | 1517 | جارٍ تجديد الباقة... | `/api/actions/renew` |
| [active_card_users.html](../web/templates/active_card_users.html) | 1559 | جارٍ تغيير الباقة... | `/api/actions/change-package` |
| [active_card_users.html](../web/templates/active_card_users.html) | 1595 | جارٍ إضافة الكوتا... | `/api/actions/add-quota` |
| [active_card_users.html](../web/templates/active_card_users.html) | 1670 | جارٍ إيداع الرصيد... | `/api/actions/add-wallet` |
| [active_card_users.html](../web/templates/active_card_users.html) | 1705 | جارٍ خصم الرصيد... | `/api/actions/deduct-wallet` |
| [active_card_users.html](../web/templates/active_card_users.html) | 1744 | جارٍ إرسال أمر الفصل... | `/api/actions/disconnect` |
| [active_card_user_details.html](../web/templates/active_card_user_details.html) | 748 | جارٍ إرسال أمر الفصل... | `/api/actions/disconnect` |
| [autoheal.html](../web/templates/autoheal.html) | 514 | جارٍ اختبار منافذ الحاويات... | `/api/tools/autoheal/probe-ports` |
| [autoheal.html](../web/templates/autoheal.html) | 595 | جارٍ التنظيف... | `/api/tools/autoheal/purge-zombies` |
| [autoheal.html](../web/templates/autoheal.html) | 670 | جارٍ إعادة تشغيل الحاوية... | `/api/tools/autoheal/restart` |
| [automation_rules.html](../web/templates/automation_rules.html) | 221 | جارٍ التنفيذ... | `/api/automation-rules/run` |
| [backups.html](../web/templates/backups.html) | 509 | جارٍ حفظ الإعدادات... | `/api/backup/settings` |
| [backups.html](../web/templates/backups.html) | 542 | جارٍ تنفيذ الإجراء... | `/api/backup/scheduler/action` |
| [backups.html](../web/templates/backups.html) | 586 | جارٍ إنشاء النسخة الشاملة... | `/api/backups/create` |
| [backups.html](../web/templates/backups.html) | 615 | جارٍ رفع النسخة الاحتياطية... | `/api/backups/upload` |
| [backups.html](../web/templates/backups.html) | 656 | جارٍ استعادة قاعدة البيانات والملفات... | `/api/backups/restore` |
| [backups.html](../web/templates/backups.html) | 724 | جارٍ الرفع والإرسال إلى تيليجرام... | `/api/backups/send-telegram` |
| [card_inspect.html](../web/templates/card_inspect.html) | 442 | جارٍ إرسال حزمة الفصل... | `/api/vouchers/disconnect` |
| [card_inspect.html](../web/templates/card_inspect.html) | 471 | جارٍ فك الماك... | `/api/vouchers/reset-mac` |
| [card_inspect.html](../web/templates/card_inspect.html) | 511 | جارٍ التمديد... | `/api/vouchers/extend-quota-time` |
| [database_maintenance.html](../web/templates/database_maintenance.html) | 1054 | جارٍ نقل وأرشفة السجلات... | `{{ url_for("api_maintenance_archive_sessions") }}` |
| [database_maintenance.html](../web/templates/database_maintenance.html) | 1091 | جاري تنظيف السجلات القديمة... | `{{ url_for("api_prune_historical_logs") }}` |
| [database_maintenance.html](../web/templates/database_maintenance.html) | 1134 | جاري إعادة بناء الجداول وتحرير المساحة... | `{{ url_for("api_optimize_tables") }}` |
| [database_maintenance.html](../web/templates/database_maintenance.html) | 1494 | جاري تنفيذ الإصلاح الهندسي الشامل... | `{{ url_for("api_fix_all_audit_issues") }}` |
| [nas.html](../web/templates/nas.html) | 281 | جارٍ فحص منفذ CoA... | `/api/nas/test-coa/` |
| [nas.html](../web/templates/nas.html) | 1715 | جارٍ الإرسال... | `/api/nas/test-coa/${nasId}` |
| [nas_details.html](../web/templates/nas_details.html) | 107 | جارٍ فحص منفذ CoA... | `/api/nas/test-coa/` |
| [nas_details.html](../web/templates/nas_details.html) | 603 | جارٍ اختبار حزمة CoA... | `/api/nas/test-coa/${nasId}` |
| [notifications_center.html](../web/templates/notifications_center.html) | 389 | جارٍ التحقق من البوت... | `/api/notifications/verify-bot` |
| [notifications_center.html](../web/templates/notifications_center.html) | 442 | جارٍ حفظ الإعدادات... | `/api/notifications/settings/save` |
| [notifications_center.html](../web/templates/notifications_center.html) | 469 | جارٍ الإرسال... | `/api/notifications/test-send` |
| [packages.html](../web/templates/packages.html) | 1006 | جاري تطبيق التعديلات... | `/packages/bulk-action` |
| [packages.html](../web/templates/packages.html) | 1168 | جاري نسخ الباقة... | `/packages/clone/${pkgId}` |
| [subscribers.html](../web/templates/subscribers.html) | 1373 | جارٍ الحذف... | `/api/actions/delete` |
| [subscribers.html](../web/templates/subscribers.html) | 1417 | جارٍ تمديد الصلاحية... | `/api/actions/extend-time` |
| [subscribers.html](../web/templates/subscribers.html) | 1456 | جارٍ إنهاء الاشتراك... | `/api/actions/terminate` |
| [subscribers.html](../web/templates/subscribers.html) | 1502 | جارٍ تجديد الباقة... | `/api/actions/renew` |
| [subscribers.html](../web/templates/subscribers.html) | 1544 | جارٍ تغيير الباقة... | `/api/actions/change-package` |
| [subscribers.html](../web/templates/subscribers.html) | 1580 | جارٍ إضافة الكوتا... | `/api/actions/add-quota` |
| [subscribers.html](../web/templates/subscribers.html) | 1655 | جارٍ إيداع الرصيد... | `/api/actions/add-wallet` |
| [subscribers.html](../web/templates/subscribers.html) | 1690 | جارٍ خصم الرصيد... | `/api/actions/deduct-wallet` |
| [subscribers.html](../web/templates/subscribers.html) | 1729 | جارٍ إرسال أمر الفصل... | `/api/actions/disconnect` |
| [subscriber_details.html](../web/templates/subscriber_details.html) | 807 | جارٍ إرسال أمر الفصل... | `/api/actions/disconnect` |
| [system_services.html](../web/templates/system_services.html) | 274 | جارٍ ${actionLabel} الخدمة... | `/api/system-services/action` |
| [system_services.html](../web/templates/system_services.html) | 319 | جارٍ فحص الصحة الذاتية... | `/api/system-services/watchdog/check` |
| [vouchers.html](../web/templates/vouchers.html) | 809 | جارٍ تجهيز الملف... | `متابعة GET لحالة المهمة` |
| [vouchers.html](../web/templates/vouchers.html) | 1001 | جارٍ الحذف... | `/vouchers/batches/bulk-action` |
| [tools/accounting_archiver.html](../web/templates/tools/accounting_archiver.html) | 142 | جارٍ نقل وأرشفة السجلات... | `/api/tools/accounting-archiver/run` |
| [session-operations.js](../web/static/session-operations.js) | 13 | جارٍ التحقق من الجلسات | `/api/actions/session-operation/` |

## الأدلة

PRIVATE/notification-lifetime-audit-20261030-r1 يحتوي SOURCE-INVENTORY.private.json لكل53موضعًا معرفًا وOTHER-WAIT-TITLES.private.json وTIMER-REPRO.private.json ومشغل VM. PRIVATE هو C:/Users/lenovo/Documents/Codex/private/max-radius. الأدلة لا تحتوي سجلات عملاء أو مفاتيح أو نتائج NAS؛ لا تعد الاختبار محاكاة لعملية خادم، بل لإغلاق إشعار الواجهة. لم يعدل سلوك المنتج بهذا الطلب.

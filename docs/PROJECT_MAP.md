## مزامنة المصدر والتثبيت — 2.6.5، 24 أكتوبر 2026

[التثبيت الموافق للصورة المنشورة](REPOSITORY_265_INSTALL.md): 248 ملف مصدر مطابق، صور مثبتة بالبصمة، وفصل Compose التطوير عن الإنتاج.

## 23 أكتوبر 2026 — رفع البناء الكامل وتحديث m وmax1 وshn مكتمل

بتفويض لاحق صريح رُفعت الصورة الكاملة المختبرة2.6.5 تحت `latest` و`20261023-v2.6.5-full-r1` بالبصمة `sha256:c61766679bbcaf349d1301d7e45dc1d7f329461f2fb40cbf3d9b465c36b61096`. تحقق Registry config/rootfs مطابق للمرشح المحلي169ملفًا موقّعًا و72قالبًا؛ الحجم المضغوط نحو583MB، والمحلي2.1GiB، وعددطبقاتها10مقابل26للصورةالسابقة. انتهت محاولةCLIالأولى بمهلة600ثانية قبل اكتمالmanifest؛ راجعت404وثباتlatestوالدومينات ثم استكمل نفسالصورةوالوسمبمهلةأطول، دون إعادةبناء.

ثبت Web وupdate_controller يدويًا في الثلاثة على البصمة نفسها، مع بقاء Core/DB وهوياتهما وأوقاتبدئهما دون تغيير. الخدمات healthy/restart0، startupأكدتهيئةالمخططدونModuleNotFound/traceback، والمنحوالأدوار162ثابتة، وصفحات /loginالعامة200للثلاثة. حفظت الحاويات الست السابقة وCompose؛55حاويةسابقةأخرىثابتة. لاGitpushأوتعديلعروضمركزالتحديثأوعميلمحليأودومينآخر؛المراقبةPAUSED.

في mثبت4قبولمصادقةطبيعيبعدبدءWebو264جلسةNASمطابقةلـSQL؛عينة431صفًا بلا فقد أوتغيرUIDأوانخفاضعدادات،وتقدم263منها و93صفًاحدثبعدالبداية. فيmax1ثبت12قبولًا و82جلسةNASمطابقة؛عينة307صفوفبلاانحرافوتقدم77منها و59بعدالبداية. shnبلاNASمسجلأومرورفعلي؛قبولهتشغيليوليساختبارمصادقةومحاسبةطرفًا لطرف.

التشغيلالكاملحدّثتعريفتريغرقياسيواحد `trg_max_nas_seal_preserve`:أضافثلاثةCOLLATEutf8mb4_unicode_ciصريحةإلىمقارنةusername/NAS/Session-ID.ثبتأنبقيةSQLtokensمطابقةوأنجميعالتريغراتالأخرىثابتة،بمافيهاتريغراغلقالحادثةالخاصانبـm.نجحت10حالاتMariaDBمعزولةعلىالوحدةالمجمعةلنوعيgeneral/unicode:حفظالعداداتوالإغلاقالمختوم،ورفضمطابقةمستخدم/NASمختلف،وتقدمالجلسةغيرالمختومة.ليساختلافبصمةشكليًافقط،ولايُصنففسادًامحاسبيًا؛هوتغييرهذهالنسخةالمراجَعوالمختبر.

الدليل `PRIVATE/release-265-full-three-20261023/FINAL.private.json` ولقطاتNAS/SQLوالإيصالات.لا تعاد مشغلات النشر أو prepare/dispatch/controller أو أي محاولة مكتملة؛لااستعادةDBتاريخيةللرجوع. الفقرةالتاليةعنالبناءالمحليدوننشرتمثلمرحلةسابقةقبلالتفويضالأخير.

## 23 أكتوبر 2026 — بناء Web كامل محليًا واختباره فقط، بلا نشر

طلب الإنسان بناء الصورة بكامل ملفات التشغيل واختبارها فقط دون رفع. بُنيت `max-radius-web:full-2.6.5-local-20261023-r1` بالبصمة `sha256:c61766679bbcaf349d1301d7e45dc1d7f329461f2fb40cbf3d9b465c36b61096` من248ملف مصدر مجمد مطابق للـcheckout وقت القبول؛ ليست overlay لتصحيحات محددة. ثبتت إصدارات مكتباتPythonعلى المنشورة، وأصلح استثناءملفيمنفذالتحديثفي.dockerignoreوأضيفتوسومالإصداروالقناةإلىDockerfile. الناتج169ملفتشغيلموقعًا و72قالبًا مطابقًا؛ mysql_bootstrap وbackup_archive_policy وweb.license_features موجودة ومستوردة فعليًا، ولاPythonمصدريةفيruntime.

نجحت فحوصالصورةالمجمعة للصلاحياتوالملكية،15حالةإعدادات و16إعادةمحاولةبمافيهاعاملDockerمعزول و32منفذالتحديث؛ إضافةاختباراتحارسالميزاتHTTPوفحصالأرشيفوربطsocketمعحفظComposeوالرجوع. تهيئةMariaDBفارغةفعليةأنتجت56جدولًا و162صلاحيةوأدوار73/20/18و15تريغرًا؛ البدايةالثانيةحفظتالمنح، والتطبيقالكاملحمل305مساراتوGET/login200. البياناتصناعيةوالترخيصالإيجابيمحقونوالعمالالخلفيونمعطلونفيfixture؛ لافحصNASأوعميلإنتاجيولاضمانخلوكلالمساراتمنالأخطاء.

الدليل `PRIVATE/web-full-local-20261023/FINAL.private.json`. لم يرفع شيء ولميتغيرlatest:Web5f582abe/Coreb0e1b2e1؛54حاويةمحليةقائمةثابتة،وحاويةMariaDBالمختبريةالجديدةأوقفتوأزيلت. المراقبةPAUSED. لا تعاد مشغلات البناء/اختبارالقاعدةالمكتملة؛ الصورة المحليةتنتظرأمرالإنسانولايفهمنجاحالاختبارتفويضنشر. أخطاءتجهيزfixturesالأولى(Windowsfcntlومساراتالاختبار)موثقة،والقبولالنهائيفيLinuxناجح.

## 23 أكتوبر 2026 — نشر تصحيح RBAC وتحديث max1 وshn بتفويض الإنسان

أعيد رفع Web بنفس **2.6.5** تحت `latest` والوسم الثابت `20261023-v2.6.5-rbac-fix-r1`؛ البصمة `sha256:5f582abe49d75b7a5fed40f57318dac5e0c62fa1da2f02b05d88d4644c1f7a2f`. تطابقت Registry config/rootfs مع المرشح المختبر، والتوقيع وmanifest للملفات166 مع تصحيح m (ست وحدات متغيرة،160 ثابتة). بقي Core/L2TP latest وعروض مركز التحديث كما هي؛ لم ينفذ Git push.

حدث Web يدويًا في max1 وshn فقط، وثبتت الصورة بالبصمة في Compose مع حفظ الحاويتين السابقتين. انتهى التبديل والتحقق في37.594 و33.417ثانية؛ هذه مدة مرحلة وليست قياس انقطاع المشترك. Core/DB/controller وm ثابتة. منح الأدوار الحالية محفوظة في الدومينين؛ لم ينسخ توزيع أدوار m إليهما. max1 كتالوجه162 أصلًا؛ shn استكمل28سجلًا أساسيًا و28ربطًا لدور الجذر فقط بمعاملة محفوظة مسبقًا خارج الخادم، مع ثبات بصمات بيانات الأعمال داخل المعاملة.

ثبت في max1 بعد البداية5قبول مصادقة طبيعي وتقدم61صفًا محاسبيًا، و62جلسةNASمطابقة لـSQL؛ عينة306صفوف دون فقد أو تغيرUID أو انخفاضعدادات، والتريغرات وهوية النسخة ثابتة. shn بلاNASمسجل أو مرورفعلي؛ تحققها تشغيلي وتوقيع وترخيص فقط. المراقبة تبقىPAUSED والعميل المحلي مؤجل. أدلة الجولة `PRIVATE/release-265-rbac-two-20261023/FINAL.private.json`؛ لا تعاد مشغلات النشر أو التبديل أو معاملة الكتالوج المكتملة. تعثر تجهيز أولي في قراءة SQL قبل أي تبديل صُحح بعد حفظ دليله؛ فحوص شn المبكرة سبقت انتهاء المعاملة وتضمنت عامل max1 منتهيًا، والقبول النهائي يميز تغييرات النطاق المفوضة.

## إصلاح الصلاحيات على m للمعاينة — 23 أكتوبر 2026

[الأدوار والحدود والقبول](MANAGERS.md): استكمل كتالوج m إلى162وضبطت أدوار مدير الشبكة/الموزع الرئيسي/POS. صورة Web خاصة محلية بنفس2.6.5، دون نشر أو latest أو تحديث غير m. core/permission_catalog وdatabase/permission_catalog وmanager_service وownership وقالبا roles/base؛ الفحص tests/test_rbac_repair.py. بانتظار معاينة الإنسان، والمراقبة متوقفة.

## تثبيت 2.6.5 المصحح على m وmax1 وshn — 23 أكتوبر 2026

[قبول التثبيت اليدوي وحدوده](RELEASE_20261023_265.md): بتفويض لاحق ثبتت Web وupdate_controller بالبصمة ef10effb على الثلاثة. Core/DB ثابتان، والمصادقة والمحاسبة الفعليتان مثبتتان في m/max1؛ shn بلا NAS أو مرور فعلي. المراقبة موقوفة وخطة العميل المحلي مؤجلة. لا تعاد جولة التثبيت المكتملة، وأوصاف الرفع فقط التالية تاريخية.

## بناء 2.6.5 المصحح لقيد إعادة المحاولة

[المطابقة والتصحيح والرفع فقط](RELEASE_20261023_265.md)، [بصمةالبناء](../releases/build-20261023-v2.6.5-retry-fix-r1.json): latestصارef10effb بنفسرقم2.6.5؛ وحدةsystem_update_host فقط تغيرت و165ملفًا ثابتًا. يسمح بإعادة محاولة فشل مؤكد قبلالتبديل مع حفظالتاريخ والتحقق من انتهاءالعامل. لا تثبيت أو تغييرقناة أو مسارإنشاء؛ ينتظرالإنسانالخطوةالتالية.

## خطة تحديث العميل المحلي المحفوظة

[الخطة المؤجلة بطلب الإنسان](CLIENT_LOCAL_UPGRADE_PLAN_20261023.md): تحديث مرحلي بعد تثبيت الهوية والتوافق وتجربة معزولة، مع حماية أحدث البيانات وتقليل الانقطاع. حفظ فقط؛ لا تشغيل أو جدولة أو توسيع إتاحة2.6.5.

## تصحيح 2.6.5 الموجه — 23 أكتوبر 2026

[النشر والتحقق وحدود التثبيت](RELEASE_20261023_265.md): حفظ الإعدادات والشعار حسب الترخيص عبر `services/system_settings_service.py`، وكاش الترحيل في مساحة المستأجر عبر `services/migration_upload_service.py`. Web latest منشور؛ عروض القناة موجهة فقط إلى m/max1/shn، دون تثبيت. نُقل كاش m السابق بتفويض لاحق وتحققت سلامة الملفات؛ محاولة التحديث المحفوظة تحتاج مراجعة مستقلة، والمراقبة متوقفة.

## تشخيص حفظ الإعدادات في 2.6.4 — 23 أكتوبر 2026

[سبب الاستيراد المفقود ونطاق الأثر والحل المقترح](SETTINGS_264_DIAGNOSIS_20261023.md): وحدة system المجمعة تستدعي سياسة ميزات لم تدخل صورة الإصدار؛ يتعطل نموذج الحفظ، وقد يسبق الخطأ حفظ الشعار جزئيًا. تشخيص مؤكد في m والصورة المثبتة، وسبع حالات معزولة. الوثيقة تشمل تنبيه سلامة مستقلًا سببه كاش الترحيل خارج مساحات البيانات المعتمدة، وثلاث حالات مجمعة؛ لا إصلاح أو نشر، والمراقبة الدورية متوقفة.

## معالجة حسابين في m — 23 أكتوبر 2026

[المعالجة اليدوية وحدود الإثبات](M_ACCOUNTING_RECOVERY_20261023.md): دورة تعويضية مفوضة وحماية محددة من الأجيال القديمة المتأخرة، مع حفظ التاريخ والمال. تفاصيل التطبيق والهوية خارج Git؛ ليست ترحيلًا عامًا أو توسيعًا لتفويض المراقبة. قبول دخول الجهاز الفعلي موضح منفصلًا عن نجاح التفعيل.

## مراقبة المصادقة والمحاسبة — 23 أكتوبر 2026

[تجهيز max1 وm وحدوده](AUTH_ACCOUNTING_MONITOR.md): جامع قراءة مستقل `deploy/hosted-max/auth_accounting_collect.py` ومحلل لقطات `auth_accounting_monitor.py`، واختبار معزول مخصص. لا تغيير للتطبيق أو إصلاح أو جدولة مفعلة؛ التفاصيل والحالة والاعتمادات خارج Git. يراقب التناقض بين النتائج الفعلية حتى دون رسائل خطأ، ولا يقدم شهادة بسلامة كل عملية مالية.

## إصدار 2.6.4 — 22 أكتوبر 2026

مسار زر تحديث اللوحة القديمة الموحد: [العقد والحدود](OLD_PANEL_CONTROL_UPDATES.md)، والمحول `deploy/hosted-max/panel_updates.py` ومنفذ المضيف `panel_update_controller.py`. يشمل الإنشاءات الجديدة والتهيئة الكسولة للقائمة عند طلب الإنسان فقط؛ لا يعتمد `latest` ولا يثبت إصدارًا دون نشره في المركز.

مسار إنشاء الدومينات الجديدة من اللوحة القديمة: [عقد التهيئة والحدود](OLD_PANEL_NEW_DOMAIN_UPDATES.md)، المساعد `deploy/hosted-max/new_domain_updates.py` وpatch المجاور. يربط منفذ2.6.4وهويةدومينمستقلةويتحققمنالقناةقبلنجاحالإنشاء؛لايغيرواجهةأومسارتحديثمنظومةقائمة.

[مستهلك control وعقد الإصدار](RELEASE_20261022_264.md): `services/system_update_channel.py` للتحقق، و`system_update_control.py` لجسر Web، و`system_update_executor.py` للتنفيذ المستقل، و`deploy/hosted-max/update_controller.py` و`provision_update_controller.py` لمنفذ المضيف والتهيئة. الدعم مصدر وصورة ضمن جولة الإصدار؛ لا يستنتج ربط دومين قائم من رفعها. سجل النشر النهائي في بيان الإصدار الخاص بهذه الجولة.

## الاستئناف الحالي — 22 أكتوبر 2026

ابدأ بـ[تسليم المحادثة المختصر](SESSION_RESUME_20261022.md) قبل الفقرات التاريخية: 4k ثبتت، وناشر control جهز؛ مستهلك القناة و2.6.4 وlatest وإنشاء الدومين لم تنفذ.

ثم [التسليم الشامل وخريطة الأنظمة والوصول](FULL_SYSTEM_HANDOVER_20261022.md)؛ يشمل اللوحات القديمة والجديدة ومراجع الاعتمادات الخاصة دون أسرار في المصدر.

## تجهيز تحديث 4k — 22 أكتوبر 2026

[الإتاحة المقبولة وحدودها](FOUR_K_UPDATE_PREPARATION_20261022.md).2.6.3متاح لـ4kفقط والتثبيت للإنسان ولم يبدأ؛ المصدر العام/latestوالعملاء الآخرون خارج التجربة. [الجسر](../deploy/hosted-max/pilot_web_bridge.py)، [المتحكم](../deploy/hosted-max/pilot_fleet_controller.py)، [المنفذ](../deploy/hosted-max/pilot_host_engine.py). لا تعادfixturesأوclaimsمكتملة.

## 22 أكتوبر 2026 — حد Telegram المعتمد

حد المستند والجزء45,000,000بايت، وسقف الأرشيف5GiB محفوظ، ومهلة الرفع متناسبة مع الحجم: [telegram_backup_parts.py](../services/telegram_backup_parts.py)، [السلوك والقبول](TELEGRAM_BACKUPS.md). اعتمد على Web العميل ثم التجارب فقط؛48حالة مترجمة، وإيصال ملف واحد22.20MB أكده الإنسان على العميل. هويات كل بيئة وحدود NAS/النسخ في [HANDOFF](../HANDOFF.md)؛ لا نشر عام أو تعديل للدومين المستضاف.

## قبول مصدر لوحة V2 محليًا — 20 أكتوبر 2026

أغلقت مسارات القبول المحلية المرتبطة باللوحة، مع حراسة ميزات MAX وحفظ العقود القديمة، وأمان أرشيف الاستعادة، ومعالجة خطأ DB المؤقت في watchdog. [التغيير وحدود الدليل](PANEL_V2_LOCAL_CHANGES_20261020.md). قبلت الاستعادة الأصلية في جولة Docker جديدة، وقبل التحديث والتراجع للمكونات المحلية. كل موارد القبول أوقفت؛ لا عميل أو خادم أو نشر أو اتصال خارجي، ولا تعاد الجولات المغلقة.

هذه الفقرة تخص عمل لوحة max_saas_panel_v2_local؛ لا تستأنف منها أي مهمة خادم تاريخية أو تغييرات نشر أو محاسبة. يحتفظ المشروع بالتغييرات السابقة غير المرتبطة بهذه المهمة. الفهرسة AST فقط دون استيراد web.app أو core.config.

مسار تهيئة MySQL المعدل محليًا19أكتوبر: [mysql_bootstrap](../database/mysql_bootstrap.py) و[db](../database/db.py)، مع قواعد first_use_policy وnas_session_safety. [العقد](DATABASE.md) و[الفحوص](TESTING.md). مصدر فقط، بلا نشر؛ دليلDocker/RouterOS والتكامل مع اللوحة في المشروع الشقيق max_saas_panel_v2_local.

## قبول خادم Linux الأصلي في مختبر اللوحة — 19 أكتوبر 2026

لم يُعدّل كود MAX في هذه الجولة؛ طابقت بصمات خدمات L2TP والمحدّث وملفات docker/l2tp المصدر المجمد. اختُبر الخادم الأصلي في Linux 6.6.142 داخل QEMU وحاوية معزولة لتجاوز قيد نواة WSL دون تغييرها. نجح PPP مع RouterOS والربط من اللوحة وNAS API و105/105 حزم ناجحة، وصف SQL واحد مع Interim عند 900 ثانية وStop فعلي عند 1052 ثانية؛ الإدخال 92614 والإخراج 77370 بايت وactive=0. نجح التعافي الصريح والتحديث الأصلي والتراجع ورفض مرشح تالف؛ التوقيع والترخيص والـlease بعد الرجوع صحيحة.

التقرير والدليل في المشروع الشقيق `max_saas_panel_v2_local/reports/LINUX_L2TP_ACCEPTANCE_20261019.md` وJSON المجاور، ووصفة `docs/LINUX_L2TP_LAB.md` فيه. يعلن التقرير ترجمة QEMU وNAS API ومسارات المختبر وإعادة RADIUS الصريحة، وإيصال تراجع تحقق بعد مهلة الرد دون إعادة تنفيذه. لا تعيد أي مرحلة أو driver أو restore من هذه الجولة؛ صفها المالي مغلق فعليًا. توقفت جميع الحاويات الـ39 الجديدة، وحالات 344 حاوية سابقة لم تتغير. لا نشر أو عميل أو مفاتيح إنتاجية.

المتبقي: HTTPS enrollment/heartbeat وRegistry، وشبكة نشر ممثلة دون الترجمة، وتقوية افتراضات عناوين/اعتمادات Linux وإبطال اعتماد قديم، وإعادة تشغيل مضيف كامل والحمل الطويل. هذه نتيجة مختبر مستقلة ولا تعتمد أي بيئة LAB أو عميل تاريخية أخرى.

## قبول مصدر MAX في مختبر Docker مستقل — 19 أكتوبر 2026

اكتمل قبول إصلاح التهيئة وCollation في جولة جديدة مملوكة لمشروع اللوحة الشقيق. نجحت تهيئة 56 جدولًا وتكرارها دون تغيير الأعمال، وfirst-use والعدادات مع Collation مختلط. ثبت RouterOS CHR 7.22 وL2TP وNAS/RADIUS من اللوحة، و35/35 حزمة، وStop فعلي عند 455 ثانية مع SQL active=0. نجح التحديث الأصلي والنسخة الإلزامية وhelper والتراجع ورفض مرشح تالف، والتحقق الأصلي من التوقيع والترخيص بعد الرجوع. نجحت 9 حالات bootstrap و26 حالة updater داخل Linux. توقفت جميع موارد هذه المهمة؛ 292 حاوية سابقة بقيت دون تغير. لا اتصال بعميل أو نشر أو تعديل خدمة قائمة.

التقرير والدليل في المشروع الشقيق: `max_saas_panel_v2_local/reports/NATIVE_ACCEPTANCE_20261019.md` وملف JSON المجاور. هذه نتيجة المصدر المجمد في المختبر بمفاتيح اختبار وOCI محلي، وليست قبول Registry أو HTTPS enrollment/heartbeat أو خادم Linux/xl2tpd أو نشر. تحفظ الجولات التشخيصية دون إعادة تنفيذ أو تسوية SQL مصطنعة؛ لا تمنح هذه الإضافة قبولًا لتغييرات أخرى أو أية بيئة تاريخية.

التعافي والمراقب الآلي: [AUTOHEAL_MONITORING](AUTOHEAL_MONITORING.md)؛ تصحيح ربط الهوية وأزرار الحاويات مستقل ومحدود بعد الإلغاء، واختباره `test_autoheal_container_binding.py`، خدمة autoheal_service وcore/watchdog وقالب autoheal. ألغيت إضافات 19 أكتوبر بطلب الإنسان وأعيد الأصل على LAB؛ مرجع الرجوع في HANDOFF.

# خريطة المشروع

مرشح ثبات هوية المحاسبة عند تغير NAS-Port-Id: [ACCOUNTING_IDENTITY](ACCOUNTING_IDENTITY.md)، وسياسة docker/freeradius/max-accounting-identity.conf. مصدر ومختبر فقط؛ اعتماد LAB يتطلب تحققًا فعليًا، ولا نشر عميل.

مرشح LAB 2.6.3 يجمع هوية الناشر وحماية العرض وتحسينات Telegram وإصلاح التحديث المباشر، ولم يُنشر. مصدر التحديث [system_update_service](../services/system_update_service.py) ومشغله [system_update_runtime](../services/system_update_runtime.py)؛ [العقد والفحوص](SYSTEM_UPDATES.md). فحص التغيير tests/test_system_update.py معزول، والتشغيل المباشر مقيد بـWeb الحالية وصورة Registry مثبتة ونسخ إلزامي وتحقيق فعلي قبل النجاح.

معاينة هوية الناشر وحماية القوالب على LAB فقط: [PRODUCT_IDENTITY_PREVIEW](PRODUCT_IDENTITY_PREVIEW.md). المصدر core/product_identity.py، والتحميل المضمّن في web/__init__.py، وتجهيز القوالب في packaging/embed_templates.py؛ لا تغيير لبيانات دعم صاحب الشبكة أو لمسار المصادقة والمحاسبة.

رفع النسخ إلى Telegram والأجزاء وأداة التجميع: [TELEGRAM_BACKUPS](TELEGRAM_BACKUPS.md)؛ مدمج في مرشح LAB 2.6.3 غير المنشور، مع حدود المهلات والاختبارات وعدم إعادة الإرسال عند ضياع الرد. نشر العميل الخاص السابق مستقل ولا يجيز تحديثه بهذا المرشح.

أداة حادثة الساعة المنفصلة: clock-record-repair (`../deploy/clock-record-repair/README.md`، مرجع عمل محلي خارج حزمة المستودع)،وفهرس AST/SHAخاصبها SOURCE_MANIFEST (`../deploy/clock-record-repair/SOURCE_MANIFEST.json`، مرجع عمل محلي خارج حزمة المستودع). تشملالمختبرومشغّلالعميلالمقيدوالإيصالوالتراجع؛ليستمسارويبأوترحيلبدءتشغيل.لاتستوردالتطبيقأوتشغلهابغيرتفويضحالةمحدد؛[HANDOFF](../HANDOFF.md)يفصلالتطبيقالمكتملوالحدود.
هذا الملف نقطة البداية؛ لا تقرأ جميع الخدمات. الفهرس المولد في `generated/source-index.json` يحدد التعريفات والمسارات ومواقعها، ولا يثبت تدفق التنفيذ.

اختبار تصحيح التواريخ فقط: clock-record-repair (`../deploy/clock-record-repair/README.md`، مرجع عمل محلي خارج حزمة المستودع)، فهرسه المحلي (`../deploy/clock-record-repair/SOURCE_MANIFEST.json`، مرجع عمل محلي خارج حزمة المستودع). المرشح المعدل يعطي الأولوية للمرور الفعلي بعزل اتصال مخصص ومعاملة لكل كرت؛ نجح53فحصًا و52ردUDPموقعًا. لا ضمان صفر تأخير مطلق، ولا مشغل حي أو نشر. تفاصيل القفل القديم محفوظة للمقارنة.

مرشح وقاية وقت الإقلاع **محلي غير منشور**: [clock-guard](../deploy/clock-guard/README.md)، و[بصمات المرشح](../deploy/clock-guard/SOURCE_MANIFEST.json). خارج تغطية الفهرس الرئيسي الذي يفهرس deploy/hosted-max فقط؛ وجوده لا يعني تفعيله أو تصحيح تواريخ قديمة.

التشغيل الحالي في [ENVIRONMENTS](ENVIRONMENTS.md)، وحدود المشاريع في [WORKSPACE](WORKSPACE.md)، وتسليم الجولة في [HANDOFF](../HANDOFF.md). الجرد والتحقق الموسع خارج نطاق أداة check-docs في [DOC_AUDIT](DOC_AUDIT.md).

| المجال | نقاط الدخول والملفات المسؤولة | وثيقة المجال |
|---|---|---|
| التشغيل والتكوين | [run.py](../run.py)، [core/config.py](../core/config.py)، [entrypoint.sh](../entrypoint.sh)، [web/__init__.py](../web/__init__.py) | [التطوير](DEVELOPMENT.md) |
| المشتركون والعقود | [routes/subscribers.py](../web/routes/subscribers.py)، [subscriber_service.py](../services/subscriber_service.py)، [subscribers.html](../web/templates/subscribers.html) | [إدارة المشتركين](SUBSCRIBERS.md)، [القواعد](BUSINESS_RULES.md) |
| الكروت والحزم والطباعة | [routes/vouchers.py](../web/routes/vouchers.py)، [الإصدار السريع](../web/routes/quick_cards.py)، [خدمته](../services/quick_card_service.py)، [voucher_service.py](../services/voucher_service.py)، [card_design_service.py](../services/card_design_service.py)، [active_card_users.html](../web/templates/active_card_users.html) | [صفحات الكروت](VOUCHERS.md)، [القواعد](BUSINESS_RULES.md) |
| التجديد وتغيير الباقة والكوتا والمحفظة | [quick_action_service.py](../services/quick_action_service.py)، [user_portal_service.py](../services/user_portal_service.py)، [quota_service.py](../services/quota_service.py) | [القواعد](BUSINESS_RULES.md) |
| لوحة المشترك والسلفة | [routes/portal.py](../web/routes/portal.py)، [user_portal_service.py](../services/user_portal_service.py)، [subscriber_portal_service.py](../services/subscriber_portal_service.py) | [API](API.md) |
| المكافآت | [loyalty_rewards_service.py](../services/loyalty_rewards_service.py)، [loyalty_schema.py](../database/loyalty_schema.py) | [الولاء](LOYALTY.md)، [القواعد](BUSINESS_RULES.md) |
| الإيقاف والتفعيل والحذف | [account_lifecycle_service.py](../services/account_lifecycle_service.py)، [quick_action_service.py](../services/quick_action_service.py) | [القواعد](BUSINESS_RULES.md) |
| المحاسبة والعدادات | [routes/accounting.py](../web/routes/accounting.py)، [queries.conf](../docker/freeradius/queries.conf)، [quota_service.py](../services/quota_service.py) | [قاعدة البيانات](DATABASE.md) |
| الفصل وتسوية الدورة | [coa_queue_service.py](../services/coa_queue_service.py)، [renewal_settlement_service.py](../services/renewal_settlement_service.py)، [nas_session_settlement.py](../services/nas_session_settlement.py)، [nas_session_safety.py](../database/nas_session_safety.py) | [قرار التسوية](decisions/001-session-settlement.md) |
| سياسة الجلسة على الراوتر | [online_policy_service.py](../services/online_policy_service.py)، [core/coa.py](../core/coa.py)، [mikrotik_api.py](../core/mikrotik_api.py) | [البنية](ARCHITECTURE.md) |
| الباقات ومزامنة RADIUS | [routes/packages.py](../web/routes/packages.py)، [package_validation.py](../services/package_validation.py)، [radius_sync.py](../core/radius_sync.py)، [rate_limit.py](../core/rate_limit.py) | [صفحة الباقات](PACKAGES.md)، [القواعد](BUSINESS_RULES.md) |
| الراوتر والأنفاق | [routes/nas.py](../web/routes/nas.py)، [nas_service.py](../services/nas_service.py)، [nas_diagnostics_service.py](../services/nas_diagnostics_service.py)، [l2tp_service.py](../services/l2tp_service.py)، [docker/l2tp](../docker/l2tp) | [النشر](DEPLOYMENT.md) |
| الترخيص والمقاعد | [license_guard_service.py](../services/license_guard_service.py)، [license_capacity.py](../database/license_capacity.py)، [license-capacity-postauth.conf](../docker/freeradius/license-capacity-postauth.conf) | [الأمان](SECURITY.md) |
| الاستيراد | [import_service.py](../services/import_service.py)، [database_migration_service.py](../services/database_migration_service.py)، [mikrotik_userman_importer.py](../services/mikrotik_userman_importer.py)، [مهام User Manager](../services/userman_job_service.py)، [نافذة التقدم](../web/static/userman-progress.js)، [تنسيقها](../web/static/userman-progress.css)، [import_state_service.py](../services/import_state_service.py) | [الاستيراد](IMPORTS.md)، [قاعدة البيانات](DATABASE.md) |
| إعادة المصنع والصيانة | [factory_reset_service.py](../services/factory_reset_service.py)، [maintenance_job_service.py](../services/maintenance_job_service.py)، [db_maintenance_service.py](../services/db_maintenance_service.py)، [maintenance_reset_state.py](../services/maintenance_reset_state.py)، [عقد الصيانة](DATABASE_MAINTENANCE.md)، [hosted controller](../deploy/hosted-max/README.md) | [النشر](DEPLOYMENT.md) |
| النسخ والاستعادة | [backup_service.py](../services/backup_service.py)، [backup_scheduler_service.py](../services/backup_scheduler_service.py)، [تنبيه تفعيل النسخ](../web/templates/components/backup_activation_notice.html) | [الفحص](TESTING.md) |
| الموزعون والمديرون | [routes/resellers.py](../web/routes/resellers.py)، [reseller_wallet_service.py](../services/reseller_wallet_service.py)، [manager_service.py](../services/manager_service.py)، [rbac.py](../core/rbac.py)، [ownership.py](../core/ownership.py)، [permission_catalog.py](../core/permission_catalog.py)، [مثبت الصلاحيات](../database/permission_catalog.py) | [المديرون](MANAGERS.md)، [الأمان](SECURITY.md) |
| الواجهة والتعريب | [base.html](../web/templates/base.html)، [web/templates/components](../web/templates/components)، [web/static](../web/static)، [web/utils.py](../web/utils.py) | [الواجهة](FRONTEND.md) |
| الزمن والإحصاءات والمراقبة | [time_service.py](../core/time_service.py)، [stats_service.py](../services/stats_service.py)، [watchdog.py](../core/watchdog.py)، [health_probe_service.py](../services/health_probe_service.py) | [البنية](ARCHITECTURE.md) |

لوحة إدارة الدومينات مشروع مستقل؛ انظر [حدود مساحة العمل](WORKSPACE.md). لا تخلط `core/config.py` مع إعدادات اللوحة.

معاينة مراقبة NAS الخاصة بـAS وحدودها ونشرة مهلات API: [AS_NAS_PREVIEW](AS_NAS_PREVIEW.md). مصدرها overlay مستقل، وليست تعديلًا للكود العام أو مهلات RADIUS/CoA.

للبحث المحدد: `python scripts/project_tools.py index` ثم `python scripts/project_tools.py find action_renew_package`. أسماء الاختبارات وأوامرها في [TESTING](TESTING.md).

سياسة الكروت عند أول استخدام وحصة دورة المشترك: [FIRST_USE_POLICY](FIRST_USE_POLICY.md)، المثبت [first_use_policy.py](../database/first_use_policy.py)، والتثبيت قبل الرد في license-capacity-postauth.conf.

اعتماد المفتاح ضمن التفعيل: [LICENSE_ENROLLMENT](LICENSE_ENROLLMENT.md). خادم الإصدار مشروع منفصل؛ مكونات الإصدار المتأثرة محفوظة في deploy/license-server.

وقاية الساعة والطاقة المحلية ومناطق العرض: [BOOT_POWER_RESILIENCE](BOOT_POWER_RESILIENCE.md). مرجع الوقت `core/time_service.py`، وساعة العرض `web/static/server-clock.js`، واختبارات `deploy/clock-guard/test_clock_authority.py` و`test_server_clock.cjs`. لم تنشر هذه الجولة على العميل.

اختبار VMware الحقيقي الجاري على LAB فقط: [VMWARE_BOOT_POWER_TESTS](VMWARE_BOOT_POWER_TESTS.md)، والتسليم هو مرجع PowerOff اليدوي والشاهد المقيد والتراجع النشط.

جاهزية ترخيص RADIUS عند الإقلاع وإعادة المحاولة المحدودة: [LICENSE_STARTUP_READINESS](LICENSE_STARTUP_READINESS.md)، والاختبار المعزول `tests/test_license_startup_retry.py`. حالة النشر الفعلية في HANDOFF والتقرير الخاص.


إصلاحات الوقت وجاهزية الترخيص وأدوات وقاية المضيف الاختيارية: [TIME_AND_STARTUP](TIME_AND_STARTUP.md). الاختبارات والوصول العام والتراجع دون restart موثقة هناك؛أدواتتصحيححوادثالعملاءليستضمنالتوزيع.

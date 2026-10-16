# البيئات والخدمات

## CLIENT_AS — R10، 16 أكتوبر 2026، اعتماد نهائي 12:43 عدن

نُشر Web المحمي المخصص `mohd777/max-radius-web:20261016-voucher-lock-r10` بهوية `sha256:dc0bfce378226c69d3baf7450753385f06beac753061069be3c8c6cde6822837` على CLIENT_AS وحده. الحاوية الحالية0a76c98fafad بدأت12:27:59عدن. نجح الاعتماد الذري بعد112ثانية؛ اكتملت مراقبة الرجوع المستقلة15دقيقة عند12:42:44عدن، validation_passed=true، دون تراجع أو إعادة تشغيل جديدة. فحص12:42:59أكد بقاء27401هوية/عقد، وتقدم32من32عداد جلسة خط أساس دون تناقص و20Stopحقيقيًا. تفعيل/دورة طبيعية واحدة استبعدت من ثبات اللقطة، وازداد البيع من237إلى238. رُصدت53بداية و49نهاية جلسة و83صف محاسبة محدثًا منذ التبديل. آخر مطابقة12:41:36جلسةRADIUS معSQL/NAS، وخمس جلسات محليةradius=false مستثناة، وصفرمرشحين لانتهاء. هناك حالتاAccess-Reject في الفترة؛ السبب التفصيلي غير محفوظ فيradpostauth، ولا دليل على أنهما عطل أو انتهاء حصرًا. لم يتغير Core/DB/L2TP/Autoheal أو MAX أو latest أو Git البعيد.

يحتوي الإصدار إصلاح أقفال التفعيل مع فهرس PIN وتعريف التريغر المطابق؛ فُحصت الصورة المجمعة نفسها في المختبر، وليست اختبارات مصدر فقط. التوقيع صحيح و94وحدة محمية بلا مصادرPython تشغيلية مكشوفة. حافظ الفحص الأولي على27401هوية وعقد كرت، ومفاتيح الهوية، والترخيص active وlease حديثة؛ تحركت العدادات ووصل Stop حقيقي. عند12:28تطابقت33جلسةRADIUS مع SQL/NAS واستبعدت جلستان محليتان radius=false؛ لا مرشحين لانتهاء غير مفصول. تحذير DB واحد عن اتصال مغلق وافق إيقاف Web القديم، دون أخطاء Core/Web جديدة عند الاعتماد. هذه القراءات لا تثبت انعدام كل فقد حزم أو جميع حالات الراوتر.

حاويةR9 نفسها محفوظة متوقفة باسم `max_radius_web_rollback_20261016_r10b` مع الصور والملفات والنسخ السابقة. لا حذف ولا تشغيل لهذه الأرشيفات أو Compose/install/prune عام. المحاولة الأولىR10 تراجعت آليًا بسبب تأخر تسجيل الاعتماد الخارجي عن180ثانية؛ لم يكن ذلك فشل بيانات أو راديوس مثبتًا. عولج سباق الاعتماد والرجوع بقفل ذري، واختبرت الخطة مستقلة ثم استخدمت جولة جديدة بمهلة300ثانية؛ لا تعاد الجولة القديمة أو markers الخاصة بها. لا استعادةDB قديمة؛ الرجوع Web وتعريف التريغر فقط مع إبقاء أحدث المحاسبة.

الأدلة والنسخ المشفرة ومراجع التشغيل الخاصة في `C:/Users/lenovo/Documents/Codex/private/max-radius/client-r10b-rollout-20261016`؛ الصورة واختبارات التغليف في client-r10-rollout-20261016. مفتاح فك النسخة الاحتياطية خارج العميل. ما يلي سجل تاريخي إذا تعارض مع هذه الفقرة.

## CLIENT_AS — تحديث16أكتوبر2026، 09:31عدن

هذه نسخة عميل محلية عبر مسار AS؛ ليست مستأجر AS المستضاف، ولا MAX، ولا مختبر LAB_UBUNTU. Web الحالي `max_radius_web` بهوية حاوية7e251afd5b00 وصورة R9 `sha256:6f1f4a441c58661b1d92ed22c221eb7b361fbbda61cd185dceaf308a5ba4908d`. الصورة محمية94وحدة، دون مصادرPython تشغيلية مكشوفة، وبالمفتاح العام نفسه. النشر نسخة مثبتة محددة، وليس إثباتًا أن latest أو كل البيئات محدثة.

الحاوية الأصلية8e5a8c7b71a7 محفوظة متوقفة باسم `max_radius_web_rollback_20261016_r9`، مع writable layer وإصلاح الاستعادة. كذلك R8 المحاولة السابقة محفوظة متوقفة. سياسة تشغيل النسخ المحفوظة no، وسياسة الأصل قبل الحفظ محفوظة وتستعاد عند الرجوع. لا تشغيل أو حذف لهذه النسخ أثناء المراقبة. قاعدة DB وتخزينها وCore وL2TP وAutoheal الأصلية بقيت في مكانها وبالهويات وStartedAt نفسها.

ملفات التشغيل والـoverride المثبت وخطة الرجوع وخدمة المراقب والنسخ المشفرة ومفاتيح فكها موثقة في PRIVATE: client-r9-rollout-20261016. مفتاح الفك خارج العميل؛ نسخة DB متسقة متحقق منها موجودة على المضيف وخارجه. لا تشغّل Compose عام أو إزالة orphan containers: الاحتفاظ بحاويات labels متماثلة يحتاج اتباع الخطة الخاصة التي تحفظ القديم فعليًا. الصورة الجارية لا تعتمد على إصلاح نصي في طبقتها؛ الذي يعتمد عليه هو الأصل المحفوظ فقط. اكتملت مراقبة09:31بنجاح؛ راجع CURRENT_STATE لأحدث اعتماد.

آخر قراءة حية: **2026-10-13 21:51:27 Asia/Aden**. هذه لقطة، وليست مراقبة مستمرة. معرفات البيئات ثابتة؛ لا يستخدم اسم latest لإثبات الكود.

تحديث لاحق مصرح في 13 أكتوبر 2026: ويب AS وحده على الصورة `max-radius-web:as-api-safety-20261013-r3` بهوية `sha256:3ac47cce6617bceb3cfa2aacc14487f2f35d6af3faafdb02ca41b522a26d0337`، healthy. سجل الصورة السابق في الجدول تاريخي لويب AS؛ Core/DB والمستأجرون الآخرون لم تتغير هويات حاوياتهم في هذا التحديث. انظر [معاينة NAS](AS_NAS_PREVIEW.md). تفاصيل Compose والتراجع محمية في PRIVATE: as-api-safety-20261013.

العناوين الخارجية وعناوين الشبكات وملفات التشغيل الإدارية وحسابات الدخول محفوظة في المرجع المحلي الخاص: `C:/Users/lenovo/Documents/Codex/private/max-radius`. ابدأ هناك بسجلات ENVIRONMENTS.private.md و ACCESS.private.md، ولا تنسخها إلى صورة أو مستودع أو حزمة. في جهاز آخر اطلب مرجعًا محميًا مكافئًا، ولا تفترض وجود هذا المسار.

## المشاريع وحدودها

| المعرف | المشروع المسؤول | المصدر والتشغيل | مستوى التحقق |
|---|---|---|---|
| TENANT_SOURCE | MAX RADIUS للمستأجر | جذر Git لهذا المستودع؛ main عند HEAD المذكور في HANDOFF | Git محلي وبعيد وقراءة الكود |
| LOCAL | تطبيق المستأجر في Docker Desktop | ملف Compose الجذر مع override تطوير خارج المستودع؛ المسارات في LOCAL_WEB | metadata حالية |
| MAX / 4K / AS | ثلاثة مستأجرين مستقلين على VPS | Compose و DB و storage وشبكة لكل مستأجر؛ سجلات PRIVATE: MAX_RUN/4K_RUN/AS_RUN | metadata وبصمات ومخطط حالية |
| CENTRAL_PANEL | saas-manager-next، مستقل عن المستأجر | خدمة WSGI على المضيف مع Nginx، وليست حاوية تطبيق عميل | service metadata وملفات المصدر؛ اختلاف محلي مسجل |
| LICENSE_ISSUER | max-license-server، مستقل عن المشروعين | خدمة WSGI على المضيف و SQLite؛ ليس Docker في النشر الحالي | metadata؛30 ملفًا مشتركًا مطابقًا |
| SHARED_TRANSPORT | L2TP/PPP المركزي والأنفاق/عمال اللوحة | حاوية نقل مشتركة وعمال systemd؛ ملكية كل نفق في سجل اللوحة | metadata وسجل ملكية قراءة فقط |
| LOCAL_CHR | مختبر RouterOS/CHR | أدلة المحاكاة في مساحة الجولات | تاريخي؛ الاتصال الحالي لم يُختبر |
| CLIENT_TESTS | عملاء محاكاة تاريخيون | حاويات محلية متوقفة؛ لا تعاد drivers المالية | قائمة Docker حالية + أدلة تاريخية |
| UBUNTU_OTHER | تثبيت المستخدم على كمبيوتر آخر | طريقة الوصول والمسار والإصدار غير متاحة | ناقص؛ لا ادعاء نجاح تفعيل أو نشر |
| MARKETING_SITE | الموقع التسويقي المنفصل | مصدر مستقل في جهاز العمل | وجود المصدر فقط؛ النشر الحالي غير مفحوص |

## سجل الحاويات الفعلية

الصورة المشار إليها هي Config.Image؛ ID هو Docker inspect.Image، ولا يفترض أنه يساوي digest السجل. RepoDigests و OCI revision محفوظان في المرجع الخاص. الويب/Core يعملان في /app و/etc/freeradius/3.0 على الترتيب وفق metadata؛ DB و L2TP و Autoheal يحتفظون بالمشغلات الخاصة بصورهم.

| البيئة | الحاوية/الدور | مرجع الصورة الحالي | Image ID الحالي | الحالة |
|---|---|---|---|---|
| LOCAL | `max_radius_web` | `mohd777/max-radius-web:20261013-auto-enrollment` | `sha256:2b69714d16c709625af2c40049a5f907e3386cb1a485f17ff36a630f257a32ea` | healthy |
| LOCAL | `max_radius_core` | `mohd777/max-radius-core:latest` | `sha256:4fc47d656557294424c3fc05f4dbc7bb95a52b6ec33e45a6dbab89c876f88c74` | healthy |
| LOCAL | `max_radius_db` | `mariadb:10.11` | `sha256:7f22313fc130a377a44999965bcb0a08dd5b21e8502824c1b864f792f9bc66ab` | healthy |
| LOCAL | `max_radius_l2tp` | `mohd777/max-radius-l2tp:latest` | `sha256:27550c5e217d8c4a77906a917b430da4fee4386889e02c87bb3a88c4884adea7` | healthy |
| LOCAL | `max_radius_autoheal` | `willfarrell/autoheal:latest` | `sha256:658d4db0d5d570d07f1977e6cd344d5d9789f4453a050abf784caa330458ab26` | healthy |
| MAX | `max_radius_max_web` | `max-radius-web:20261013-auto-enrollment-max` | `sha256:c70d634ce16bc2547ec0ae60f0038ba29267e3be0c0eaaf92442bc0d7b08c488` | healthy |
| MAX | `max_radius_max_core` | `mohd777/max-radius-core:20261013-auto-enrollment` | `sha256:4d609e0c03a32be6a979dfd110ccd10d8c8ceda72c3495a1c4734de653f42d59` | healthy |
| MAX | `max_radius_max_db` | `mariadb:10.11` | `sha256:7db29378d4fdab73f8123bbc2b48905c90d1a4b00cf848b028f1e81e623257f2` | healthy |
| 4K | `max_radius_4k_web` | `max-radius-web:20261013-backup-notice-4k` | `sha256:0b8bfba136d7bf285bf7bc9d218dcb37ca23fd4f2c0876c0d7577e4f1a3ba2a1` | healthy |
| 4K | `max_radius_4k_core` | `mohd777/max-radius-core:latest` | `sha256:ee4b06f9ecdf1941bf1345e45528e341bef28bb3f7aeec30c4bce01d2ce0ad1b` | healthy |
| 4K | `max_radius_4k_db` | `mariadb:10.11` | `sha256:7db29378d4fdab73f8123bbc2b48905c90d1a4b00cf848b028f1e81e623257f2` | healthy |
| AS | `max_radius_as_web` | `mohd777/max-radius-web:as-nas-preview-20261013` | `sha256:e7d644d5bf6d9955a854ca9b08a1d9770dd4455c90510d26cba4c9c9a5c29719` | healthy |
| AS | `max_radius_as_core` | `mohd777/max-radius-core@sha256:4fc47d656557294424c3fc05f4dbc7bb95a52b6ec33e45a6dbab89c876f88c74` | `sha256:4fc47d656557294424c3fc05f4dbc7bb95a52b6ec33e45a6dbab89c876f88c74` | healthy |
| AS | `max_radius_as_db` | `mariadb:10.11` | `sha256:7db29378d4fdab73f8123bbc2b48905c90d1a4b00cf848b028f1e81e623257f2` | healthy |
| SHARED_TRANSPORT | `max_panel_l2tp` | `max-panel-l2tp:4` | `sha256:6091005066b1c636dfd2c80c7fdd0dd61dfd734fa403895666e1ae5d0cdaaf5b` | running |

## LOCAL: ملفات Compose والروابط تتقدم على الصورة

- اسم مشروع Compose الفعلي `max-radius`. Labels الويب تسجل ملف docker-compose.yml في الجذر ثم override `work/managers-audit-20261012/local-runtime/compose.local.yml` من مساحة الجولات. لا تشغل Compose واحدًا فقط على الويب وتفترض أنه يحفظ هذا التشغيل.

- ربط `work/first-use-policy-20261013/run-before.py` إلى /app/run.py **ما زال موجودًا الآن**. ملفات core/services/web/database/storage مرتبطة كذلك؛ /app/web/static/uploads و Docker socket مربوطان. مصدر الربط الكامل وأولوية الملفات في PRIVATE: LOCAL_WEB.

- روابط /app/core و/app/services تجعل المصادر الأمنية الست مرئية داخل الويب المحلي رغم أن صورة الإصدار تحتوي النسخ المجمعة. هذه بيئة تطوير، ولا تثبت فحص صورة الإنتاج مجردة من overrides.

- Core المحلي يربط entrypoint و queries.conf؛ L2TP المحلي يربط entrypoint و chap-secrets/pap-secrets/options؛ DB يربط مخطط إنشاء مع volume البيانات؛ Autoheal يربط Docker socket. تفاصيل المصدر/RW والـ volumes والمنافذ الخارجية في PRIVATE: LOCAL_CONTAINERS.

- لا تصف Core/L2TP المحليين بأنهما على آخر هوية منشورة: هما على صور سياسة المرحلة الأولى السابقة؛ وسوم latest في Config.Image لا تغيّر الحاوية القائمة تلقائيًا.

## المستأجرون المستضافون

- MAX: كود التطبيق النصي المشترك مع المصدر مطابق 183/184، والاستثناء run.py يحافظ على تشغيل الاستضافة دون bootstrap عام أو إعادة تهيئة الأنفاق. هذه المقارنة لا تشمل إثبات كل وحدة مجمعة أو اختبار عمليات مالية حي.

- 4K: إصدار backup-notice أقدم؛169/182 ملفًا مشتركًا مطابقًا، و 13 اختلافًا تشمل خدمات السياسة/الاستيراد/الولاء والمخطط والترخيص والمشغّل. غابت دالة تجميد سياسة أول استخدام وعلامتها في قراءة المخطط الحالية. **لم يحدث تحديث 4K في جولة التوثيق**.

- AS: معاينة NAS خاصة؛179/184 ملفًا مشتركًا مطابقًا، وخمسة اختلافات مع helper إضافي. تتضمن مهلة الوصول 1.5 ثانية وإعادة محاولة للفشل و API3 ثوانٍ وفصل عرض النفق/API. المعاينة ليست في HEAD أو latest، ولا تعني تغيير مهلات RADIUS/CoA.

- MAX و AS: سياسة أول الاستخدام موجودة في المخطط: policy_frozen_at، دالة fn_freeze_voucher_policy وأربعة تريغرات. أسماء الكائنات تحقق تركيب المخطط فقط، لا صحة تنفيذ مصادقة جديدة.

- لكل مستأجر Web/Core/DB فقط في Compose الحالي؛ التخزين و uploads وواجهة Docker المقيدة mounts للويب، ولا روابط core/services/web/database تتجاوز الصورة. إعداد الشبكات والمنافذ الخارجية الخاصة في المرجع المحلي؛ لا تستخدم شبكة مشتركة للبيانات بدل شبكة المستأجر.

- MariaDB volumes مستقلة؛ أسماء القواعد وحساباتها وإحالات الأسرار في المرجع الخاص. لا تنقل DB أو بيانات العملاء عند نقل نفق إلا بطلب مستقل.

## اللوحات والوصول

| الواجهة | المشروع/البيئة | المصادقة | رابط/اعتماد |
|---|---|---|---|
| إدارة النظام والمدير/الموزع | TENANT في LOCAL/MAX/4K/AS | Flask session و RBAC و CSRF وملكية الصفوف | PRIVATE: LOCAL_ADMIN/MAX_ADMIN/4K_ADMIN/AS_ADMIN |
| لوحة المشترك | TENANT في البيئة المختارة | جلسة المشترك وحساب DB الخاص بالمستأجر؛ ليست حساب إدارة VPS | PRIVATE: *_PORTAL، والمسارات في API.md |
| إدارة المنظومات/الأنفاق | CENTRAL_PANEL | جلسة مدير اللوحة و CSRF؛ SSH منفصل | PRIVATE: PANEL_ADMIN |
| إصدار التراخيص والنسخ والمفاتيح | LICENSE_ISSUER | جلسة مدير خادم الإصدار؛ API تفعيل موقع مختلف | PRIVATE: LICENSE_ISSUER_ADMIN |
| الموقع التسويقي | MARKETING_SITE | موقع تعريف منفصل، وليس لوحة إصدار أو إدارة | PRIVATE: MARKETING_SOURCE؛ النشر غير متحقق |

- فحوص GET العامة لأربع واجهات مستضافة رجعت 403 عبر Cloudflare من أداة القراءة. هذا لا يثبت تعطل الأصل، ولا يثبت نجاح تسجيل دخول متصفح. لا تختبر اعتمادًا افتراضيًا لتجاوزها.

## الخدمات على المضيف

| الخدمة | الدور والنطاق | الحالة الحالية |
|---|---|---|
| saas-manager.service | WSGI اللوحة؛ لا حاوية tenant | active |
| max-license-server.service | WSGI الإصدار و SQLite؛ لا Docker | active |
| max-panel-l2tp.service | تجهيز PPP المركزي وقواعده حسب الملكية | active |
| max-panel-tunnels.service | عامل الأنفاق المركزي | active |
| max-panel-nas-reload.service | إعادة تحميل عملاء NAS لتطبيق المستأجر المقيد | active |
| max-panel-fleet@{max,4k,as}.socket/service | metadata/controller خاص بالمستأجر؛ ليست Docker socket عامة | active لكل نسخة |
| max-as-nas-preview-status.service | قراءة حالة نفق AS فقط عبر Unix socket؛ لا تحكم أو SQL تعديل | active |
| max-radius-wg-bridge.service | جسر WireGuard قديم على المضيف؛ لا يفترض أنه داخل Compose لكل مستأجر | active؛ تفاصيله في المرجع الخاص |
| nginx.service / ssh.service | proxy والوصول الإداري | active |

- لوحة الاستضافة الحالية تشمل controller إعادة المصنع المقيد، و shared transport. لا توجد حاوية L2TP أو Autoheal مستقلة في Compose لكل MAX/4K/AS. standalone LOCAL لهما نموذج مختلف.

- L2TP المحلي والمركزي يستعملان PPP/xl2tpd. لا تساوِ L2TP تلقائيًا بـ IPsec أو اتصال مشفر؛ Dockerfile الحالي لا يثبت وجود IPsec، والمركزي ينص على عدم تشغيل daemonIPsec.

## المصدر والتحديث والتراجع

- تطبيق المستأجر: HEAD والوسم وبصمات ملفات التشغيل و overrides. اللوحة: service WorkingDirectory وبصمات ملفات النشر قبل استخدام نسخة مساحة الجولات. خادم الإصدار: مصدر مستقل كامل؛ deploy/license-server ملفات متأثرة فقط، خارج صورة العميل.

- المصدر المحلي لخادم الإصدار يطابق 30 ملفًا منشورًا. نسخة اللوحة تختلف في management_service.py؛ ملفات controller و vendor/waitress على المضيف ليست كلها في نسخة اللوحة المحلية. لا تدّع توحيد نسخة اللوحة أو تدفعها كاملة دون مقارنة.

- إصلاح تحديث اللوحة منشور تاريخيًا في 13 أكتوبر: يقبل صور الاستضافة المعروفة، يسحب قناة الإصدار ويثبت الصورة، ويجهز تشغيل الاستضافة من الكود الجديد مع نسخة DB وترحيل schema بلا backfill مالي عام. مسار الإنشاء المركزي يستعمل مخطط الصورة. مقارنة ملفاته الحالية تؤكد وجود الإصلاح، ولا تنفذ update_tenant في جولة توثيق.

- معاينة AS تُستبدل عند تحديثه من القناة العامة؛ الرجوع يجب أن يشمل Compose وصورة الويب وخدمة القراءة الخاصة. لا تعممها على الآخرين دون طلب واختبارات مستقلة.

- تحديث الويب وحده لا يثبت تحديث RADIUS؛ نسخة Core الجديدة تحتاج مخطط السياسة المناسب. الرجوع عن الصورة لا يعكس DDL أو العمليات المالية. راجع DEPLOYMENT.md ومرجع PRIVATE: BACKUP_AND_ROLLBACK.

## الخدمات المساندة وحدود التغطية

- Registry العام للصور و GitHub origin متاحان للقراءة؛ التحقق الحديث وقيود الوصول في DOC_AUDIT.md. لا يوجد token أو كلمة مرور في هذه الوثيقة.

- أسرار DB/NAS/PPP/WireGuard و SMTP/WhatsApp/webhook و sessions ومفاتيح التراخيص تبقى في البيئة أو secret store. أسماء إعدادات التكامل المقروءة وحدها لا تثبت نجاح إرسال رسالة أو تشغيل بوابة WhatsApp.

- بوابة WhatsApp المحلية وبعض عملاء المتصفح/المحاكاة متوقفون. المحاكاة التاريخية لا تتحول إلى خدمة إنتاج ولا تعاد بغرض تجهيز HANDOFF.

- UBUNTU_OTHER يظل ناقصًا حتى يقدم المستخدم المضيف ومرجع الوصول. LOCAL_CHR لم يُفحص حيًا. لم يختبر تسجيل دخول إداري أو مشترك أو إصدار ترخيص أو RADIUS/Stop/عملية مالية في هذه الجولة.


## قراءات تكميلية

- LOCAL: علامة سياسة أول استخدام والدالة وأربعة تريغرات موجودة. entrypoint/queries.conf الفعليان لـ Core و entrypoint لـ L2TP يطابقان المصدر الحالي بالبايت رغم اختلاف هوية الصورة؛ لذلك اختلاف image لا يعني وحده اختلاف السياسة.
- MARKETING_SITE: مصدر الموقع المنفصل يطابق HTML المقروء من خادم محلي حاليًا، HTTP200. نشره العام لم يُتحقق منه.
- LOCAL_CHR: منفذ الإدارة المحلي التاريخي يقبل TCP، لكن طلب API باستخدام الاعتماد المحفوظ عبر MAX انتهى بمهلة انتظار. هوية/إصدار NAS الحالي غير متحققين؛ لا نستنتج تعطل الجهاز من هذا وحده.
- GitHub Pages: آخر run38440870742 مكتمل success على HEAD الحالي عند قراءة API العامة. ليست صورة Docker، ولا تعني تشغيل معاينة AS فيالمستودع.

## Registry الحالي، قراءة دون pull أو build

الهوية التالية digest لفهرس OCI، وليست config digest أو manifest خاص بمنصة. قيمهما محفوظة في المرجع الخاص. الصور الثلاث تحمل OCI revision مطابقًا لـ HEAD؛ هذا لا يثبت هوية حاوية قديمة تحمل latest.

| الصورة | digest latest الحالي |
|---|---|
| mohd777/max-radius-web | `sha256:2b69714d16c709625af2c40049a5f907e3386cb1a485f17ff36a630f257a32ea` |
| mohd777/max-radius-core | `sha256:4d609e0c03a32be6a979dfd110ccd10d8c8ceda72c3495a1c4734de653f42d59` |
| mohd777/max-radius-l2tp | `sha256:a7fa96b5a7a485ce47755e0379c5553d1a0490855fcfda950602d6c1d9affee7` |

## الحاويات المساندة

الحاويات التالية موجودة محليًا ومتوقفة عند القراءة. صورها و mounts ومنافذها في PRIVATE: auxiliary-containers.json. لا تدخل قائمةخدماتالإنتاج، ولا تعاد drivers للتوثيق.

- `max_final6_clients` — عميل محاكاة/متصفح أو NAS mock تاريخي، متوقف
- `max_sales_clients` — عميل محاكاة/متصفح أو NAS mock تاريخي، متوقف
- `max_retest_clients` — عميل محاكاة/متصفح أو NAS mock تاريخي، متوقف
- `max_final_clients` — عميل محاكاة/متصفح أو NAS mock تاريخي، متوقف
- `max_disconnect_pilot` — عميل محاكاة/متصفح أو NAS mock تاريخي، متوقف
- `max_continuous_clients` — عميل محاكاة/متصفح أو NAS mock تاريخي، متوقف
- `max_stress_clients` — عميل محاكاة/متصفح أو NAS mock تاريخي، متوقف
- `max_acceptance_clients` — عميل محاكاة/متصفح أو NAS mock تاريخي، متوقف
- `max_chr_clients` — عميل محاكاة/متصفح أو NAS mock تاريخي، متوقف
- `max_radius_simulator` — عميل محاكاة/متصفح أو NAS mock تاريخي، متوقف
- `max_evolution_whatsapp` — بوابة تكامل WhatsApp متوقفة
- `max_browser_client` — عميل محاكاة/متصفح أو NAS mock تاريخي، متوقف
- `max_client2` — عميل محاكاة/متصفح أو NAS mock تاريخي، متوقف
- `max_client1` — عميل محاكاة/متصفح أو NAS mock تاريخي، متوقف
- `max_client3` — عميل محاكاة/متصفح أو NAS mock تاريخي، متوقف
- `max_mock_routeros` — عميل محاكاة/متصفح أو NAS mock تاريخي، متوقف

## LAB_UBUNTU — جولة التهيئة16أكتوبر2026

بيئة مختبر مستقلة عنCLIENT_AS وعنLOCAL؛ تفاصيل المضيف والوصول في المرجع الخاص `schema-startup-fix-20261016`. المصدر المحلي العام مع تغييرات التهيئة غير الملتزمة، وصورةr8 المذكورة فيCURRENT_STATE. حاويات المضيف الخمس الأصلية لم تُستبدل أو تعد تشغيلها. الاختبارات استعملت حاويات وقواعد منفصلة وشبكة داخلية دون منافذ منشورة أو بيانات تخزين الأصل. مقبس هويةDocker الذي احتاجته التسوية واجهةGET خاصة بالمختبر، لا مقبس المضيف؛ تم إيقافه وحاويات الاختبار بعد الفحص. الصور والأحجام والأرشيفات محفوظة؛ لا تعِد الدوافع المالية أو تزيل حراسها. آخر تحقق فعلي وقيوده في التقرير الخاص؛ لا يدل على نشر الصورة على أي عميل.

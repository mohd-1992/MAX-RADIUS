# جرد التوثيق وحدود الاكتمال —13 أكتوبر 2026

## النتيجة والمنهج

أُنجز الجرد المتاح، لكن **بيانات الوصول والتحقق ليست كاملة لجميع البيئات**. الفجوات معلنة أدناه. لم تنفذ هذه الجولة بناء أو نشرًا أو إعادة تشغيل أو تغييرات SQL/ترخيص/مالية أو commit/push. قراءة المخطط و SQLite كانت للقراءة فقط؛ AST لا يستورد التطبيق.

بدأ Git بشجرة نظيفة، و HEAD/origin/main متطابقان. المرجع الخاص خارج جذري المصدر والجولات وسياقات البناء المعروفة، بصلاحيات المستخدم و SYSTEM. يحفظ إحالات الاعتماد بدل نسخ كلمات المرور أو محتوى مفاتيح خاصة أو بيانات عملاء.

## قائمة التغطية

| العنصر | الحالة | الدليل/الحد |
|---|---|---|
| LOCAL_WEB | موثق ومتحقق حاليًا | Docker metadata/image/mounts؛ ليست عملية عميل |
| LOCAL_CORE | موثق ومتحقق حاليًا | Docker metadata/image/mounts؛ ليست عملية عميل |
| LOCAL_DB | موثق ومتحقق حاليًا | Docker metadata/image/mounts؛ ليست عملية عميل |
| LOCAL_L2TP | موثق ومتحقق حاليًا | Docker metadata/image/mounts؛ ليست عملية عميل |
| LOCAL_AUTOHEAL | موثق ومتحقق حاليًا | Docker metadata/image/mounts؛ ليست عملية عميل |
| MAX_WEB | موثق ومتحقق حاليًا | Docker metadata/image/mounts؛ ليست عملية عميل |
| MAX_CORE | موثق ومتحقق حاليًا | Docker metadata/image/mounts؛ ليست عملية عميل |
| MAX_DB | موثق ومتحقق حاليًا | Docker metadata/image/mounts؛ ليست عملية عميل |
| 4K_WEB | موثق ومتحقق حاليًا | Docker metadata/image/mounts؛ ليست عملية عميل |
| 4K_CORE | موثق ومتحقق حاليًا | Docker metadata/image/mounts؛ ليست عملية عميل |
| 4K_DB | موثق ومتحقق حاليًا | Docker metadata/image/mounts؛ ليست عملية عميل |
| AS_WEB | موثق ومتحقق حاليًا | Docker metadata/image/mounts؛ ليست عملية عميل |
| AS_CORE | موثق ومتحقق حاليًا | Docker metadata/image/mounts؛ ليست عملية عميل |
| AS_DB | موثق ومتحقق حاليًا | Docker metadata/image/mounts؛ ليست عملية عميل |
| SHARED_L2TP | موثق ومتحقق حاليًا | Docker metadata/image/mounts؛ ليست عملية عميل |
| CENTRAL_PANEL | موثق ومتحقق حاليًا | systemd/WSGI ومقارنة المصدر؛ اختلاف management_service مسجل |
| LICENSE_ISSUER | موثق ومتحقق حاليًا | systemd/WSGI و SQLite و 30 ملفًا متطابقًا؛ لا إصدار/تفعيل |
| PANEL_WORKERS | موثق ومتحقق حاليًا | L2TP/tunnels/NAS reload active؛ مصادرها و units محفوظة خاصة |
| FLEET_CONTROLLERS | موثق ومتحقق حاليًا | ثلاث نسخ socket/service؛ المشغل/drop-ins مقروءة لا مفترضة |
| AS_PREVIEW_BRIDGE | موثق ومتحقق حاليًا | active ومعاينة مستقلة خارج HEAD |
| LEGACY_WG_BRIDGE | موثق ومتحقق حاليًا | active علىالمضيف، ليسداخلكل Compose |
| NGINX_SSH | موثق ومتحقق حاليًا | metadata/listeners؛ sshd alias وليسخادمًا ثانيًا |
| GIT_REGISTRY | موثق ومتحقق حاليًا | HEAD و origin و OCIindex/platform/config و revision |
| GITHUB_PAGES | موثق ومتحقق حاليًا | آخر run عام success على HEAD الحالي |
| CLIENT_TESTS_AND_WHATSAPP | موثق ومتحقق حاليًا | 16 حاوية مساندةمتوقفة؛ الصور/المنافذ/mounts خاصة |
| MARKETING_LOCAL | موثق ومتحقق حاليًا | HTTP200 و HTML مطابق للمصدر المحلي |
| TENANT_ADMIN_PORTAL | غير متاح أو ناقص التحقق | المشروع والمصدر وروابط الدخول وإحالات الاعتمادموثقة؛ login لم يُجرب. HTTP عام 403Cloudflare لا يثبتحالةالأصل |
| PANEL_ADMIN | غير متاح أو ناقص التحقق | مصدرالحساب/env/service محدد؛ جلسةمديرغيرمجربة |
| LICENSE_ISSUER_ADMIN | غير متاح أو ناقص التحقق | DB_PATH والمخزنومصدرالحسابمحدد؛ لااختبارجلسة/طلبترخيص |
| DB_NAS_TUNNEL_KEYS | غير متاح أو ناقص التحقق | مواضعالأسرارومراجعهاووجودهاقرئت؛ لمتصدّرالقيم ولمتجربكلرواترالعملاء |
| SMTP_WHATSAPP_WEBHOOKS | غير متاح أو ناقص التحقق | مفاتيحإعدادووجودقيمةفقط؛ لاتجربةإرسالأوخدمةخارجية |
| LOCAL_ADMIN | غير متاح أو ناقص التحقق | اسم/دورالحسابقرئ من DB؛ مرجع plaintext موثوقغيرمتاح، ولايجرب default |
| LOCAL_CHR | غير متاح أو ناقص التحقق | TCP المحليمتاح؛ API عبر VPS انتهى timeout، لاهوية RouterOS حالية |
| UBUNTU_OTHER | غير متاح أو ناقص التحقق | المضيف و SSH ومرجعالاعتماد ومسارالتشغيلغيرمتاحة |
| MARKETING_PUBLIC | غير متاح أو ناقص التحقق | النشرالعامغيرمفحوص |
| HISTORICAL_TESTS | موثق من دليل تاريخي | تقاريرالجولات فقط؛ لمتعادالاختباراتالماليةأوالترخيصية |

## مراجعة الوثائق والكود

جرد [DOCUMENT_CATALOG](DOCUMENT_CATALOG.md) يشمل Markdown المصدر والمهارات والتقارير المعروفة. قبل الإضافات كان الجرد 37 ملفًا في المصدر و 114 في مساحة الأدلة؛ لم يوجد Markdown في المصدر الكامل لخادم الإصدار. الإضافات الحالية مدرجة في الجرد النهائي. التقارير تاريخية وليست قياس تشغيل حاضرًا.

راجعت الجولة سياسة أول قبول والتريغرات والاستيراد والولاء واعتماد المفاتيح مقابل المصدر. الوثائق المتخصصة مرجع السلوك، و ENVIRONMENTS مرجع التشغيل، والخاص مرجع الوصول، و CURRENT_STATE ملخص العمل المتبقي. السجل السابق مؤرشف مع تصحيح الروابط النسبية.

مصدر اللوحة ليس Git المستأجر؛ management_service.py المحلي يختلف عن المضيف. خادم الإصدار يطابق 30 ملفًا مشتركًا؛ deploy/license-server يحتوي مكونات محددة، لا البرنامج الكامل. لم تُغيّر ملفات Python أو Dockerfile أو Compose أوسكربت تشغيل.

أعيد توليد الفهرس:94 وحدة و 303decorators للمسارات، بلا parse errors؛ بقي مطابقًا لمحتواه السابق لأن المصدر لم يتغير. الفهرس لا يمثل تسجيل Flask الديناميكي. نطاقه TENANT و deploy/hosted-max؛ ملفات Python في deploy/license-server فُحصت AST منفصلة دون استيرادها.

check-docs القياسي يغطي README و AGENTS و docs و skills و scenarios، ويغفل HANDOFF وبعض deploy. الفحص الموسع شمل كل Markdown المصدر والجديد وتلك المسارات والأرشيف. الملف الجديد غير الملتزم ليس متاحًا في clone حتى يراجعه المستخدم؛ لا stage أو commit هنا. مراجع اختبارات خادم الإصدار تحل بالنسبة لمشروعها، وليس tests في جذر المستأجر.

## فحوص الأسرار والاستبعادات

تفاصيل المخاطر في [SECURITY_REVIEW](SECURITY_REVIEW.md). فحص Git اعتمد ls-files، وفحص ignore يشمل مسارات مستقبلية مع استثناء المفتاح العام. راجع السياقات Root/Core/L2TP و COPY الانتقائي؛ المرجع الخاص لا يقع ضمنها. بيانات storage/userman-jobs ومراجع الاعتماد الخاصة محظورة من Git والبناء.

قراءة configENV من Registry لم تظهر أسماء اعتماد حساسة ثابتة؛ GPG_KEY فيصورة Python مفتاح تحقق عام. هذا لا يثبت خلو محتوىالكود أوكلطبقة: defaults منشورة تطابق LOCAL. لا يشملالتدقيق كامل Git history أو الحزم البعيدة. rsync القديم لا يطبق ignore، لذلك يحتاج إصلاحًا مستقلًا.

## المعلومات المطلوبة لاستكمال الوصول

1. UBUNTU_OTHER: المضيف، منفذ SSH، حساب مصرح وطريقة الوصول ومرجع الاعتماد في مخزن خاص، ومسار Compose/storage إن كان معروفًا. لا ترفق مفتاحًا أو كلمة مرور في ملف عام.
2. مرجع آمن لكلمة دخول LOCAL إذا كان مطلوبًا اختبار الواجهة؛ الحساب والدور معروفان، لكن password_hash لا يكشف كلمة المرور ولم يوجد مرجع plaintext موثوق.
3. عند العمل على CHR: تأكيد مسار الوصول المعتمد حاليًا ومرجع الاعتماد. TCP المحلي متاح، لكن قراءة API عبر VPS لم تنجح؛ لا تستخدم fallback أو كلمات تخمينية.

## المخرجات

الملفات المتغيرة Markdown وقواعد استبعاد فقط. الفهرس أعيد توليده دون تغيير محتوى. الأدلة التفصيلية الآمنة والمنافذ و units ومواضع المفاتيح في المرجع الخاص المشار إليه في ENVIRONMENTS و HANDOFF. لا بيانات وصول منشورة هنا، ولا نشر بعد الانتهاء.


## نتائج الفحص النهائي

-39 ملف Markdown للمصدر، شاملة HANDOFF وdeploy والمهارات والأرشيف والجديد؛ صفر أخطاءUTF-8 أو روابط نسبية مكسورة. جرد مساحة الجولات114ملفًا، صفر تنبيه روابط نسبية بحسب الجرد المتاح.
- check-docs القياسي:36وثيقة، صفر أخطاء. الفحص الموسع غطى الثلاث غير الداخلة فيه. مراجع الاختبارات المتاحة حُلّت بالنسبة لمشروعها، ولم يبق مسار اختبار مفقود أو غير متتبع ضمن المراجع المستخرجة. هذه ليست إعادة تنفيذ الاختبارات.
- AST الأساسي:95ملفًا مع أداة الفهرس، و4ملفات مكوناتIssuer خارجها؛ بلا أخطاء syntax. الفهرس94وحدة و303تعريفاتroute، أعيد توليده وبقي محتواه مطابقًا.
-9فحوص Gitignore و19فحصًا ساكنًا لاستبعادDocker نجحت؛ المفتاح العام مقصود ظاهر، والخاص غير متتبع. لم يستخدمDockerbuild لاختبار الاستبعادات.
- git diff --check بلا أخطاء، وHEAD لم يتغير. كل التغييراتMarkdown أوignore؛ لا ملف تشغيل أوكود أوتهيئةCompose تغير. ACL المجلد الخاص والملفات تحقق للمستخدم وSYSTEM فقط.

## الملفات المعدلة والمضافة

### معدلة

- `.agents/skills/max-project-onboarding/SKILL.md`
- `.dockerignore`
- `.gitignore`
- `AGENTS.md`
- `README.md`
- `deploy/hosted-max/README.md`
- `deploy/license-server/README.md`
- `docs/ARCHITECTURE.md`
- `docs/CURRENT_STATE.md`
- `docs/DEPLOYMENT.md`
- `docs/DEVELOPMENT.md`
- `docs/FIRST_USE_POLICY.md`
- `docs/LICENSE_ENROLLMENT.md`
- `docs/PROJECT_MAP.md`
- `docs/SECURITY.md`
- `docs/TESTING.md`
- `docs/WORKSPACE.md`

### مضافة غير ملتزمة

- `HANDOFF.md`
- `docker/freeradius/.dockerignore`
- `docker/l2tp/.dockerignore`
- `docs/DOCUMENT_CATALOG.md`
- `docs/DOC_AUDIT.md`
- `docs/ENVIRONMENTS.md`
- `docs/IMPORTS.md`
- `docs/LOYALTY.md`
- `docs/SECURITY_REVIEW.md`
- `docs/archive/CURRENT_STATE-before-doc-audit-20261013.md`

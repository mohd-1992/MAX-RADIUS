## 2 نوفمبر 2026 — تصدير TXT للحزم في r6 المحلية

[التنفيذ والمعاينة والفحوص](BATCH_TEXT_EXPORT_270_20261102.md): أسماء المستخدمين فقط، بنفس القائمة والثيمين والصلاحية والملكية. صورة Web محلية محدثة علىr5، دونCoreأوschemaأو نشر.

## 30 أكتوبر2026 — قبول التزامن وصلاحيات الكرت

[التقرير](ACCEPTANCE_270_POINTS_123_20261030.md): حاجز انتظار يمنعreloadقبل انتهاء كل عملية ومدة عرض نتيجتها؛ يشتركPDFفيه. رسالة الرد غير المقروء ثابتة دون تفاصيلparser، وملفاتJSبرقمكاشr2.24معاينةكمبيوتر/هاتف وثيمين نجحت. صحح اسم صلاحية تعديل الكرت إلى`detail.vouchers.card_edit`في الأزرار والخادم؛ ليست صلاحية جديدة. يبقى قبول تنزيل/إلغاء فعلي واسع في المتصفح، ولا يغلقه نقل مصطنع لاختبار الإشعار.

## 30 أكتوبر 2026 — عدد الجلسات في القائمتين

[التنفيذ والمعاينة والفحوص](LIST_SESSION_COUNTS_270_20261030.md): رقم بجانب الحالة الخضراء فقط في المشتركين والكروت، من نفس بيانات الجلسات الحديثة دون استعلام لكل صف. معاينة5270تضم1/2/3جلسات لكل نوع، ونجحت5اختبارات وفحصSQLومتصفح على بيانات اصطناعية. أعيد تشغيل حاوية المعاينة القائمة فقط؛ لاNASأو نشر. heartbeatالعينةمؤقت حتى19:02عدن30أكتوبر.

## 30 أكتوبر 2026 — ربط انتظار العمليات بنتائجها محليًا

[التنفيذ والفحوص والحدود](OPERATION_NOTIFICATIONS_270_20261030.md):52موضع انتظار بمقابض مستقلة، بلا مؤقت، ونتائج تخص كل عملية؛ أزيل الإغلاق العام المتداخل وأضيفت حالة تعذر تأكيد الرد دون إعادة التنفيذ. نجحت سيناريوهات الانتظار والتزامن وcallbacks فعلية مع نقل مصطنع، واختبارات متابعة الجلسات وPDF السابقة. لا عمليات عملاء أو نشر؛ المعاينة المتكاملة لم تحدث. هذه الحالة أحدث من «بلا إصلاح» في الجرد السابق.

## 30 أكتوبر 2026 — اختصار اسم مراقب الشبكة

اختصر اسم القائمة الجانبية إلى «مراقب الشبكة» مع منع التفافه، واستبدلت أيقونته وأيقونة رأس الصفحة بموجة `fa-wave-square` لتمييزه عن المبيعات. عنوان الصفحة الكامل «مراقب الشبكة وأوقات الذروة» محفوظ. تعديل محلي فقط؛ تدقيق مؤقتات الإشعارات ما زال بلا إصلاح.

## 30 أكتوبر 2026 — جرد مؤقت إشعارات الانتظار، بلا إصلاح

[التقرير المفصل وفهرس المواضع](NOTIFICATION_LIFETIME_AUDIT_270_20261030.md): جسر Swal يمرر3800ms لإشعارات showLoading غير التفاعلية؛ اختفاء مصطنع متحقق دون اكتمال عملية. الحصر52موضعًا مصدريًا في17ملفًا مع تعاريف متكررة، وhelper إضافي غير مستدعى مفصول عن المسارات الفعلية. تشمل متابعة الفصل/MAC وPDF/Excel الكروت والنسخ والصيانة وغيرها. PDF الجلسات الموحد ورفع الصورة وUsermanProgress وعناصر التقدم الداخلية لا تتأثر بهذا المؤقت وفق المصدر. لا تشغيل عمليات أو اتصال أو تعديل سلوك أو نشر. مدة العمليات الحقيقية لم تقس؛ الاستدعاء السريع قد ينتهي قبل الإشعار.

## 30 أكتوبر 2026 — إشعار PDF بهوية النظام

وحّد تصدير جلسات PDF على مكوّن الإشعارات المشترك أعلى اليسار، مع الثيمين وزر إلغاء ومؤشر انتظار مستمر حتى النتيجة، دون نسبة إنجاز مصطنعة. [التنفيذ والقبول المصطنع والحدود](EXPORT_TOAST_UI_270_20261030.md). لا تغيير آلية التصدير أو نشر؛ صفحة الطباعة المستقلة بلا محرك إشعارات تظل بتنزيل أصلي.

## 30 أكتوبر 2026 — تبسيط نصي تجهيز PDF وإلغائه

أصبح إشعار التجهيز «جارٍ تجهيز ملف PDF كاملًا؛ يمكنك متابعة الصفحة أو إلغاء التصدير.» ورد طلب الإلغاء «جارٍ إلغاء التصدير». تعديل نصوص محلي فقط؛ التقسيم الداخلي والتجميع والإلغاء وحدود الموارد لم تتغير، ولا نشر.

## 30 أكتوبر 2026 — إزالة نصوص نقص رصد الاستهلاك من العرض

بطلب الإنسان أزيلت عبارة فترات الرصد غير المكتملة وشارات «عدادات غير مكتملة» من الرسم وبطاقات الاستهلاك ولوحة التحكم، وشارة نقص العداد في صفوف تفاصيل الاستهلاك. حدث نص حالة البطاقة الدوري أيضًا حتى لا يعيد العبارة. هذا تعديل عرض محلي في 2.7.0؛ حساب الفروقات والعدادات المجهولة والقيم غير المتاحة وحالات فشل القراءة لم تتغير، ولا تحويل للمجهول إلى صفر. فحص القوالب والمصدر فقط؛ لا نشر أو تشغيل خدمة أو ادعاء تحميل المعاينة بالرقعة.

## Session operation results — 29 October 2026, local source

Manual disconnect and MAC application now poll a durable job instead of displaying queue admission as completed disconnection. `session-operations.js` resumes saved job IDs after navigation without submitting another disconnect; partial/unknown results remain warnings. MAC-save success and NAS application are separate. The shared MAC asset revision is `270-mac-session-r1`. [Local checks and remaining integrated/mobile/NAS acceptance](DEVELOPMENT_270_FOLLOWUP_20261029.md).

## Session history point6 —26October2026

Subscriber/card details show all sessions through50-row pages, keep the sessions hash on navigation and export the entire available history. Added NAS name, actual accounting-update and packet point columns. Wide tables scroll inside their wrapper; missing metadata stays unknown. Arabic landscape PDF repeats header/brand footer. [Contract and visual checks](SESSION_HISTORY_DEV_20261026.md).

## Approved product brand —26October2026

Current horizontal artwork is the human-selected bilingual aligned revision (MAX RADIUS / ماكس راديوس), viewBox1093×245; shared macro dimensions and aligned-r3 cache revision follow it.

[Display contract and verification](PRODUCT_BRAND_DEV_20261026.md): use shared `components/product_brand.html` and its head include. Release-owned assets are outside tenant uploads; light/dark SVGs follow `theme-light`, print uses light variant, browser icons follow browser color preference. Preserve tenant logo slots and independent publisher credits. License card logo is left on desktop and above text on mobile.

## Combined report section label —26October2026

The human-approved Arabic section name is المبيعات والاستهلاك. Sidebar, sales heading and page titles use the combined name; the inner sales/network tabs keep their individual names.

## Network consumption tab —26October2026

[Implementation and checks](NETWORK_TRAFFIC_DEV_20261026.md): a second sales view uses the existing sales styles, date buttons, theme, RTL and responsive layout. Independent period/NAS/protocol/server filters, byte cards, adaptive-unit chart and persistent sortable headings; missing counters show partial/unavailable. Desktop/mobile390 light/dark checked. Financial table display remains unchanged.

## Administrative list sorting —26 October2026

Subscribers, card subscribers and voucher batches now share sortable headings and independent browser-local preferences per admin/list. Clicking toggles ascending/descending with arrow and aria-sort; filters/pagination preserve the selected order. Optional columns retain their visibility controls. Card session/accounting sorts order the complete filtered result before pagination; they reuse existing hydration and may cost more for large result sets. Unknowns remain last. Batch card-status heading sorts by active-card count (tooltip). Development5122 only.

## Sales NAS display and date default —26 October2026

Sales tables show the current configured NAS name alone when available, with saved-label/IP fallback. The date default is today; buttons progress from day to week/month/year/all time, with custom last. Existing explicit date selections remain valid. See [verification](SALES_SOURCE_HISTORY_DEV_20261026.md).

## Sales table sorting — local development5122

Clickable column headings show direction and aria-sort; each table remembers its selection in this browser, scoped to the signed-in identity, with a default-reset link. Filters/pagination retain sorting; the server sorts the complete log before paging. [Contract and checks](SALES_SOURCE_HISTORY_DEV_20261026.md).

## Sales source confidence — development5122,26 October2026

[Preview and verification](SALES_SOURCE_HISTORY_DEV_20261026.md): coverage panel, quality filter and explicit inferred/review labels preserve page typography and both themes. Desktop/mobile checked; ordinary login and isolated-development banner. Main5090 has the earlier signed UI only.

# الواجهة والتعريب
## قيد التطوير المعتمد — 25 أكتوبر 2026

الهدف الأول لأي تطوير جديد هو نسخة Docker المحلية على `http://127.0.0.1:5090`، وفق [خط الأساس المثبت](LOCAL_DEV_BASELINE_20261025.md). لا يعني قبول تعديل محلي تفويض نشره على دومين أو عميل.

اشترط الإنسان الحفاظ على هوية النظام وألوانه وتصميمه والتوافق البصري مع الثيمين الليلي والنهاري. استخدم مكونات `base.html` القائمة وخط Tajawal والأزرار والحقول والبطاقات والقوائم والنوافذ المشتركة؛ لا تنشئ هوية أو لوحة ألوان مستقلة للميزة. اربط التنسيق بآلية `wisp_theme` و`theme-light` الحالية وراجع التباين والحدود والحالات التفاعلية في الثيمين. لا تعتمد لون خلفية أو نص ثابتًا يناسب ثيمًا واحدًا فقط. قبول أي تعديل واجهة يتطلب فحصًا بصريًا للثيمين، مع RTL وعرض الجوال وسطح المكتب، وتوثيق ما اختبر فعليًا.

القوالب في `web/templates`، والموارد المحلية في `web/static`. [base.html](../web/templates/base.html) يحمل الهوية والثيمات والموارد المشتركة. الصفحات ترثه وتحتوي أجزاء من JavaScript المضمن؛ تغيير خدمة قد يتطلب مراجعة معالج الرد في القالب أيضًا.

ابدأ بقالب الميزة وroute render_template المقابل. استخدم المكونات القائمة وSweetAlert والأيقونات والموارد المحلية. لا تعدّل vendor minified أو تنسخ مكتبة جديدة لمجرد تغيير سلوك صفحة.

تحقق من RTL والثيم الفاتح والداكن والجوال، ومن تموضع قائمة العمليات داخل الجداول ذات overflow. ميز خطأ native validation من خطأ الخدمة؛ التعريب لا يغير قواعد الحدود أو إشارات الأرقام.

إيقاف/تنشيط الحساب يرتبط بحالة الخادم، لا باسم التبويب فقط. رسائل النجاح لا تظهر قبل النتيجة الفعلية. لا تعرض تفاصيل البنية أو raw exception للمستخدم النهائي. حافظ على escaping عند إدخال اسم حساب في HTML أو JavaScript.

المراجع: [subscribers.html](../web/templates/subscribers.html)، [active_card_users.html](../web/templates/active_card_users.html)، [user_portal](../web/templates/user_portal)، [components](../web/templates/components). بيانات الاختبار في الصور والتقارير لا تتضمن كلمات مرور أو معلومات عملاء غير لازمة.

معاينة واجهة ماكس 2026-10-12: `web/static/ui-preview.css` و`ui-preview.js` يطبقان تجهيز النوافذ القائمة للتمرير وإدارة التركيز وEscape وتسميات الحقول، ومساحات اللمس والجوال. لا ينفذان طلبات API أو عمليات بيانات. تُستثنى نوافذ SweetAlert من حصر تركيز النوافذ اليدوية. عند عدم وجود تفضيلات أعمدة محفوظة، تستخدم صفحات المشتركين والكروت أعمدة مختصرة للجوال؛ تفضيلات المستخدم المحفوظة لها الأولوية. معاينة التصميم تحسب مقياس العرض من مساحة الحاوية وتعيد الحساب عند تغيير حجم الشاشة دون تغيير الأبعاد المحفوظة أو مسار الطباعة. نشرة المعاينة خاصة بماكس ولا تعني تحديث latest أو الحاوية المحلية.
# المبيعات 2.6.6 النقطة2

[العقد والمعاينة](SALES_REPORT_LOCAL_20261025.md): عرضان منفصلان للكروت واشتراكات المشتركين؛ جدول الباقات والسيرفر الداخلي والإجماليات والسجل والتصدير تتبع الفلاتر نفسها. شارة الموزع inline-flexمتصلة مع line-heightومساحة والتفاف؛ عمودNAS/Hotspot/PPPoEمستقل. aria-currentللتبويب النشط مع تباين نهاري صريح، وperiodمخفي يحفظ الفترة عند اختيار فلتر آخر؛ تغيير التاريخ يختارcustom. اختبرتالصورةالمجمعةبالثيمينوسطحالمكتبوعرض390×844وRTLوالتمريرالأفقي. لا بياناتمصطنعةفيالمحليةالفعالة.


## مراجعة الجداول — 25 أكتوبر 2026

صفحة المبيعات تستخدم `container_class` لملء العرض المتاح دون تغيير بقية الصفحات، و`sales-table` لتوحيد الخط والعناوين والمسافات والألوان. تستخدم الخلايا والشارات `white-space:nowrap` مع تمرير أفقي داخل الجدول؛ وتفاصيل العملية والجهاز أسطر ثانوية مقصودة. اختصرت العناوين إلى «السيرفر»، «الموزع»، «حصة الشبكة»، و«التاريخ». اختبرت Web `eceab63004be` بالثيمين وعرض 390×844 وسطح المكتب؛ [النتائج وحدود المعاينة](SALES_REPORT_LOCAL_20261025.md).

## قراءات لوحة التحكم — 26 أكتوبر 2026

عنوان الكرت «إجمالي استهلاك البيانات اليومي»، مع حالة نقص العدادات أو تعذر القراءة. فترتا الرسم سبعة أيام وستة أشهر محفوظتان. [العقد والفحص البصري بالثيمين والجوال](DASHBOARD_REPORT_PARITY_DEV_20261026.md).


## 27 أكتوبر 2026 — معاينة إصلاح تقارير المبيعات والاستهلاك

معاينة محلية من المصدر الحالي: http://127.0.0.1:5123/__reports_preview . قاعدة مستقلة ببيانات تجريبية فقط: 45 عملية بيع و45 جلسة موزعة على 15 يومًا وثلاث نقاط اتصال على جهازين. المعاينة السابقة 5122 لم تتغير؛ لا نشر أو تحديث دومين. تفاصيل التشغيل المحمية: PRIVATE/reports-session-fix-20261027-r1/native-r6/PREVIEW.private.json.

عرض اسم نقطة الاتصال المسجل لا يشترط اكتمال نوع الخدمة؛ لا يستنتج النوع ولا يعيد كتابة مراجع المبيعات التاريخية. التقارير المالية المجمعة لا تنسب مجموعة مجهولة مختلطة إلى اسم منفرد. رسم الفترة ذات النقطة الواحدة أعمدة، والفترات المتعددة خطوط؛ رسم السيرفرات أعمدة أفقية بوحدات متكيفة. الرسوم الثلاثة تستجيب للثيم وتحافظ على المجهول دون تحويله لصفر.

نجح اختبار MariaDB معزول من مخطط المصدر: مطابقة جدول/رسم/بطاقات الاستهلاك والفلاتر، تمييز NAS والواجهات، إزالة ازدواج الأرشيف، ملكية البيانات، مطابقة المبيعات دون كتابة مالية. نجح اختبار JavaScript للرسوم؛ فحص المتصفح عرض الرسوم والجدول والثيمين. هذا قبول معاينة اصطناعية، وليس إثبات محاسبة راوتر حي أو جاهزية إصدار منشور.


## 27 أكتوبر 2026 — المعاينة الواقعية الأحدث

استُبدل تحويل استهلاك اليوم إلى أعمدة بنقاط متصلة، وأعيدت الأرقام للإنجليزية. توزيع ساعي/يومي/شهري من فروقات عينات مستقلة، مع حفظ غياب القياس. معاينة 5124 بثلاثة أجهزة و12 نقطة و24 حسابًا وحركة اصطناعية متجددة؛ تفاصيل العقد واختلافه عن إجمالي الجلسات التاريخي والقبول وحدود النشر في [التقرير](REPORTS_REALISTIC_PREVIEW_20261027.md). هذه الفقرة أحدث من قبول معاينة 5123 أعلاه.
## 2.7.0 local preview — 2026-10-28

The analytics entry is directly below sales, labeled «مراقب الشبكة وأوقات الذروة», with its existing feature/permission checks. Traffic top10 is removed from network_traffic.html; tools/traffic_analytics.html retains its own top10. heavy-export.js handles existing PDF links with owner-scoped cancellation and completion/failure messages; cancellation does not deliver partial files. Print-card artwork receives no system logo. See [tested local scope](DEVELOPMENT_270_LOCAL_20261028.md); no release was deployed.

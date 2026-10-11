# الفحوص ومستويات الإثبات
فحص قائمة المشتركين: `python -m unittest discover -s tests -p test_subscriber_list_safety.py -v`؛ 15 حالة معزولة لاستهلاك الدورة وخطوط الأساس والكوتا المفتوحة وIDs المكررة وتهريب الأزرار والصلاحية، وتشغيل JavaScript الحقيقي للأزرار والمعاينة في Node.

فحص حماية إدارة المشتركين: `python -m unittest discover -s tests -p test_subscriber_admin_safety.py -v`؛ 18 حالة معزولة للصلاحيات وCSRF ومعاملات الإضافة والتعديل والمحفظة وحماية العقد. راجع [إدارة المشتركين](SUBSCRIBERS.md).

فحص صفحة الباقات المعزول: `python -m unittest discover -s tests -p test_package_page.py -v`؛ يغطي الحقول والإجراءات الجماعية والصلاحيات وCSRF والتراجع عند تعطل مزامنة المجموعة. راجع [سلوك الباقات](PACKAGES.md) وحدود الإثبات.

المستويات: syntax/روابط دون تشغيل، اختبارات معزولة، MariaDB معزولة، ثم RADIUS/NAS فعلي وواجهة مالية. لا تساوي نجاح SQLite أو mocks بمحاسبة الراوتر، ولا قبول API بإتمام الفصل.

`python scripts/project_tools.py test-plan` يعرض المجموعات. `run-tests --suite smoke` يشغل قائمة محددة تمت مراجعتها، وليس discovery لجميع tests. الملفات القديمة تحتوي اختبارات تكامل وسكربتات ذات آثار جانبية؛ اقرأها وحدد بيئتها قبل إضافتها للقائمة.

مجموعة smoke الأولى: عرض المحاسبة، حماية controller المستضاف، وتراجع تسوية الجلسة عبر نماذج معزولة. تحفظ الأداة ملخصًا وخروج كل اختبار، ولا تخفي الفشل بإعادة المحاولة تلقائيًا.

اختبار MariaDB للبيع: `tests/release_20261010/test_voucher_sales_concurrency.py`؛ يحتاج قاعدة fixture معزولة باسم يبدأ `max_sales_regression_`، جداول cloned وكروت unused ولا جلسات أو مبيعات. يغيّر بيانات تلك القاعدة، فلا توجهه لقاعدة عميل. يتطلب إعداد `SALES_REGRESSION_DATABASE` وصلاحيات fixture صراحة، ولا تهيئها أداة smoke.

للاختبار الحي سجل: هدف ومدة وسقف وIDs حسابات الاختبار، خط أساس البيانات، هوية NAS والخدمة، مصدر الترافك، المقاييس، الآثار المالية، وأسلوب النهاية والتنظيف. لا تعِد تشغيل driver يحتمل أنه نفذ عملية مالية قبل المهلة؛ افحص journal والأثر أولًا.

احفظ السجلات الخام خارج Git، وأخرج ملخص JSON يميز passed/failed/not-tested/expected-rejection/tool-error. تقدم المحاسبة وStop ومطابقة الجلسات وفقد/تكرار المال مقاييس مستقلة.

انحدار عمليات الكروت في البوابة: `python -m unittest discover -s tests -p test_portal_card_operations.py -v` يشغل ملفًا واحدًا معزولًا (SQL/Flask واختبار توقيع CoA)، دون استيراد التطبيق أو العمال. اختبار MariaDB الإضافي `tests/portal_card_mariadb_regression.py` يدعم نسخة `radius_max_final6` المعزولة فقط ويتطلب `MAX_PORTAL_REGRESSION=1`. يستخدم journal يمنع إعادة التنفيذ المالي؛ لا تحذف الحارس لإعادة الجولة. اختبارات UDP فيه على loopback وليست جلسات CHR، واختبار Flask لا يثبت middleware التطبيق الكامل.

الخطة الشاملة: [سيناريو الفحص النهائي](../tests/scenarios/final-system-check.md). اختبارات الأعطال المدمرة والاستيراد وإعادة المصنع على نسخة معزولة؛ الاختبارات الحية ضمن تفويض الجلسة الحالية فقط.

انحدار Excel وتعذر المحاسبة: `python -m unittest discover -s tests -p test_import_quota_regression.py -v`؛ ملف Excel فعلي وتحويل أعمدة عربية/إنجليزية والتكرار والقيم المجهولة. اختبار MariaDB يستخدم `MAX_PORTAL_REGRESSION_RUN` لهوية جولة جديدة ببيانات جديدة؛ لا تغير هوية محاولة غير مكتملة لإعادة تنفيذها. Hotspot يختبر عدم إرسال CoA وعدم تغيير الجلسات، وPPPoE توقيع ACK/NAK لكل الجلسات.

فحص صفحات إدارة الكروت: `python -m unittest discover -s tests -p test_voucher_pages.py -v`؛ 32 حالة معزولة للصلاحيات وCSRF والتوليد الذري والتعديل وإضافة الأيام والبيانات ومحاسبة الدورة وفك الإيقاف والطباعة والتصاميم. لا يرسل طلبات مالية أو فصل إلى بيئة حية. راجع [صفحات الكروت](VOUCHERS.md).

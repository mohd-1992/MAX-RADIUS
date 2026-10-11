# قرار: مفتاح فريد لبيع التفعيل الأول فقط
التاريخ: 2026-10-10. الالتزام: 30191ba.

قراءة NOT EXISTS على فهرس voucher_id قبل INSERT داخل تريغر Accounting-Start سببت gap-lock deadlocks مع تفعيل كروت متزامنة.

أضيف `first_activation_voucher_id` قابلًا لـNULL بفهرس UNIQUE. تريغر التفعيل يملؤه ثم يستخدم INSERT ... ON DUPLICATE KEY UPDATE دون تغيير الحقول المالية. التجديدات تتركه NULL وتبقى مبيعات مستقلة. جعل voucher_id فريدًا كان سيمنع التجديدات، لذلك لم يستخدم.

الترحيل يحافظ على جميع الصفوف المالية، ويحدد أول بيع تاريخي فقط عندما لا يوجد مفتاح تفعيل محفوظ للكرت. إعادته لا تنقل مفتاحًا موجودًا على صف لاحق.

المراجع: [voucher_sales_schema](../../database/voucher_sales_schema.py)، [schema_healer](../../database/schema_healer.py)، [اختبار التزامن](../../tests/release_20261010/test_voucher_sales_concurrency.py). نتائج الإثبات في [CURRENT_STATE](../CURRENT_STATE.md).

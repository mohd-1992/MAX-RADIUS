# المسارات وواجهات التطبيق
الفهرس المولد بواسطة `python scripts/project_tools.py index` يسجل decorators للمسارات وأسماء Blueprints ومعالجاتها وأسطرها وطرق HTTP. ابحث مثلًا: `python scripts/project_tools.py find /user/`.

| مجال المسار | المصدر |
|---|---|
| تسجيل الدخول والجلسة | [auth.py](../web/routes/auth.py)، [web/__init__.py](../web/__init__.py) |
| المشتركون وعملياتهم | [subscribers.py](../web/routes/subscribers.py) |
| الكروت والحزم | [vouchers.py](../web/routes/vouchers.py) |
| الباقات | [packages.py](../web/routes/packages.py) |
| لوحة المشترك | [portal.py](../web/routes/portal.py) |
| NAS والأنفاق | [nas.py](../web/routes/nas.py) |
| المحاسبة | [accounting.py](../web/routes/accounting.py) |
| النظام والاستيراد والصيانة | [system.py](../web/routes/system.py) |

عند تطوير مسار حدد: طريقة HTTP، نوع الجسم JSON/form، هوية المستخدم وصلاحياته، نوع الحساب وID، حدود القيم ووحداتها، شكل النجاح والفشل، الخدمة والآثار المالية، والتحقق من تكرار الطلب. احفظ الرد المتوقع للمستهلكين الحاليين؛ لا تستبدل JSON بصفحة HTML أو tuple بنتيجة مختلفة دون تحديث الطرفين.

الكشف الساكن لا يستورد Flask ولا يحسب decorators الديناميكية أو الاستدعاءات الناتجة عن wildcard imports؛ إنه فهرس بداية. تحقق من تسجيل Blueprint والحماية في app factory ومن الخدمة الفعلية قبل التعديل.

واجهات الدومينات المركزية ليست هذه المسارات. انظر [WORKSPACE](WORKSPACE.md). لا تطلب dump لجميع ردود API؛ استخدم حالات مختصرة خالية من الأسرار.

## تحديث عمليات لوحة المشترك — 2026-10-11

مسارا `/user/renew-package` و`/user/change-package` يستخدمان `renew_or_change_package` للمشتركين والكروت، ويحترمان `portal_allow_package_change`. تغيير كلمة المرور يدعم النوعين ويحترم `portal_allow_password_change`، ولا يفك الإيقاف. السلفة للمشتركين فقط.

`POST /user/api/change-speed` يتطلب جلسة `portal_user`؛ اسم حساب داخل الطلب لا يمنح صلاحية التعديل. `success=true` يعني حفظ سياسة السرعة، والرسالة تحدد النتيجة الحالية. حقل `coa` يتضمن `saved`, `sessions_total`, `sessions_acknowledged`, `live_status` (`acknowledged`, `partial`, `next_login`) ونتائج الطلبات دون الأسرار أو معرفات الجلسات. لا تدّع الواجهة تطبيقًا لحظيًا عند رفض الراوتر. لا يفصل هذا المسار جلسات الحساب.

سرعة Hotspot في البوابة: الحفظ الناجح يعيد `live_status=next_login` ولا يرسل CoA أو فصلًا؛ ظهور النجاح يعني حفظ السياسة للدخول القادم. PPPoE يحتفظ بنتائج acknowledged/partial/next_login وفق ردود كل الجلسات.

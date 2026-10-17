# وقت النظام وجاهزية الترخيص — R13

يعتمد التطبيق على ساعة المضيف للحظة UTC/epoch، ويكون فحص NTP داخل التطبيق تشخيصيًا فقط دون إضافة offset مستقل. تختار منطقة IANA طريقة العرض، مع بقاء عقد تخزين MariaDB وFreeRADIUS عند +03:00. الجداول الدورية الحالية تتبع تقويم المنطقة المحددة؛ هذا الإصدار لا يعيد كتابة التواريخ أو العقود ولا يغير ساعة الراوتر.

## جاهزية ترخيص RADIUS

يفحص عامل الترخيص اتصال DB قبل التحقق القائم للمخطط. يعيد المحاولة بعد2/4/8/10ثوانٍ للفشل، ثم يعود إلى دورة30ثانية عند النجاح. مدة الصلاحية90ثانية وحدود التوقيع والانتهاء والساعة والسعة باقية. لا يتجاوز العامل الترخيص ولا يجعل healthy بديلًا عن مصادقة ومحاسبة RADIUS.

## وقاية بدء Docker

حزمة التنصيب تتضمن أدوات المضيف تحت host/clock-guard، خارج صورة Web. الحماية خيار صريح MAX_CLOCK_GUARD=enable وليست تغييرًا تلقائيًا لكل مضيف. تدعم Linux/systemd مع systemd-timesyncd فعال وعلامة /run موثوقة وتأكيد kernel مرتين. المضيف الذي يستخدم chrony أو Docker Desktop يحتاج آلية مضيف مناسبة؛ لا تغير هذه الأدوات عميل NTP القائم.

الحاجز يؤثر في Docker كله، لذا لا يفعّل على مضيف متعدد المستأجرين دون قرار مسؤول المضيف. يؤخر البدء اللاحق حتى تأكيد الوقت ضمن180ثانية؛ عند تعذر التأكيد يرفض البدء بدل كتابة تواريخ غير موثوقة. تفعيل الأدوات يجري daemon-reload فقط دون restart/reboot، ولا يعني اختبار إقلاع كامل في كل تثبيت.

التفعيل الصريح بعد فحص توافق المضيف:

```bash
sudo python3 /opt/max-radius/host/clock-guard/manage.py status
sudo python3 /opt/max-radius/host/clock-guard/manage.py enable --journal /var/lib/max-radius-clock-guard/r13-local
```

للتراجع استخدم نفس journal، ولا تحذف helper يدويًا قبل نسيان systemd له:

```bash
sudo python3 /opt/max-radius/host/clock-guard/manage.py disable --journal /var/lib/max-radius-clock-guard/r13-local
```

يحفظ التراجع الملفات المؤرشفة، ويزيل drop-in ثم يعيد تحميل التعريف ويتحقق منه قبل أرشفة helper. يرفض الملفات التي عدلها مسؤول آخر ويحفظ الحالة عند فشل reload. لا يعيد تشغيل خدمة ولا يستعيد DB قديمة. لا تعد استخدام journal مكتملة لجولة جديدة.

## التوزيع والمطابقة

release.json يثبت Web وCore وL2TP وMariaDB وAutoheal بمعرفات Registry. runtime-release.env يطابق البيان، ولا يكفي وسم latest لإثبات نسخة حاوية جارية. يجب استبعاد روابط مصدر التطوير من بيئة مقارنة الإصدار، مع الاحتفاظ بروابط البيانات والتهيئة الخاصة بكل بيئة. تبقى حزم R12 ذات الملفات الخمسة مدعومة؛ R13 تتضمن عشرة ملفات محددة، مع رفض التكرار والروابط الرمزية والمسارات غير المعتمدة.

## الاختبارات والحدود

```bash
python -m unittest discover -s deploy/clock-guard -p 'test_*.py' -v
python -m unittest discover -s tests -p test_license_startup_retry.py -v
python -m unittest discover -s tests -p test_license_clock_recovery.py -v
python -m unittest discover -s tests -p test_runtime_distribution.py -v
node deploy/clock-guard/test_server_clock.cjs
```

تستعمل فحوص Python مجلدات وبدائل معزولة ولا تنشئ التطبيق أو تصل لقاعدة عميل. يحتاج Python على Windows اعتماد tzdata من requirements. اختبارات guard مكتملة على Linux؛ فحوص symlink قد لا تتاح دون صلاحيات Windows. اختبار Power Off سابق على VM منفصلة يدعم سلوك الحاجز، ولا يضمن سلامة كل قرص أو مزود تخزين أو انعدام فساد DB عند فقد الطاقة. إعدادات متانة MariaDB الإضافية مثال اختياري منفصل؛ لا يغيرها هذا الإصدار صامتًا. أدوات تصحيح حوادث تواريخ قديمة لا تدخل المثبت أو الصورة ولا تشغّل تلقائيًا.

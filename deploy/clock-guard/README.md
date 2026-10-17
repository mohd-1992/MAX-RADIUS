# وقاية بدء Docker من وقت غير موثوق

جزء من R13، وأدوات مضيف خارج صورة Web. راجع [مرجع الوقت والترخيص والتفعيل والتراجع](../../docs/TIME_AND_STARTUP.md).

الملفات التشغيلية الموزعة خمسة: clock_ready.py وmanaged_files.py وactive_files.py وmanage.py و99-max-radius-clock-ready.conf. لا تتضمن بيانات أو وصول عميل، ولا تغير الوقت أو الترخيص أو SQL، ولا تعيد تشغيل Docker. التفعيل خيار صريح على Linux/systemd-timesyncd المدعوم وله أثر على جميع حاويات المضيف عند بدء Docker اللاحق.

اختبارات test_*.py معزولة؛ fixture_probe وlinux_checks وrun_linux_fixture أدوات مختبر فقط، ولا تدخل حزمة التنصيب. مثال power-safe.override.example.yml يوضح خيار متانة DB منفصلًا ولا يفعله المثبت. لا تستخدمه فوق overrides قائمة دون مراجعة أو تستنتج تحمل كل حالات فقد الكهرباء.

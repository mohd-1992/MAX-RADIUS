import sys
sys.path.insert(0, '/app')
from web.app import app
from database.db import query_one

client = app.test_client()
with client.session_transaction() as sess:
    sess['logged_in'] = True
    sess['admin_id'] = 1
    sess['username'] = 'admin'
    sess['role'] = 'admin'

# Test all distinct main GUI pages
routes_to_test = [
    # Main Navigation
    ('/dashboard', 'لوحة التحكم الرئيسية'),
    ('/subscribers', 'إدارة المشتركين'),
    ('/vouchers', 'إدارة حزم الكروت'),
    ('/vouchers/active-users', 'مشتركين الكروت المفعلة'),
    ('/packages', 'إدارة الباقات والسرعات'),
    ('/nas', 'أجهزة الشبكة والراوترات NAS'),
    ('/invoices', 'سجل الفواتير والمطالبات'),
    ('/resellers', 'إدارة الموزعين ونقاط البيع'),
    ('/managers', 'إدارة المسؤولين والموظفين'),
    ('/roles', 'إدارة الأدوار والصلاحيات'),
    ('/reports/sales', 'سجل وتقارير المبيعات'),
    ('/settings', 'إعدادات النظام العامة'),
    ('/coverage-map', 'خريطة التغطية والأبراج'),
    ('/notifications', 'مركز الإشعارات'),
    ('/loyalty', 'برنامج الولاء والمكافآت'),
    ('/license', 'حالة الترخيص'),
    
    # Tools (الأدوات)
    ('/tools/backups', 'النسخ الاحتياطي'),
    ('/tools/system-services', 'خدمات النظام والعمليات'),
    ('/tools/import', 'استيراد البيانات Excel'),
    ('/tools/import-data', 'استيراد البيانات'),
    ('/tools/database-maintenance', 'صيانة وتطهير قاعدة البيانات'),
    ('/tools/database-migration', 'استوديو استيراد وترقية قواعد البيانات'),
    ('/tools/autoheal', 'المراقب الذكي Autoheal'),
    ('/tools/radius-simulator', 'محاكي فحص RADIUS'),
    ('/tools/traffic-analytics', 'تحليل استهلاك الترافيك'),
    ('/tools/accounting-archiver', 'أرشفة وتدوير سجلات المحاسبة'),
    ('/card-designer', 'مصمم الكروت'),
    ('/card-inspect', 'فحص الكروت'),
    ('/tunnels', 'أنفاق L2TP VPN'),
    
    # Sub-pages with IDs (if available in DB)
]

# Add parameterized routes
pkg = query_one('SELECT id FROM wisp_packages LIMIT 1')
if pkg:
    routes_to_test.append((f"/packages/{pkg['id']}", f"تفاصيل الباقة #{pkg['id']}"))

sub = query_one('SELECT id FROM wisp_subscribers LIMIT 1')
if sub:
    routes_to_test.append((f"/subscribers/{sub['id']}", f"تفاصيل المشترك #{sub['id']}"))

batch = query_one('SELECT id FROM wisp_voucher_batches LIMIT 1')
if batch:
    routes_to_test.append((f"/vouchers/batch/{batch['id']}", f"تفاصيل دفعة الكروت #{batch['id']}"))

card = query_one('SELECT id FROM wisp_vouchers WHERE status != "unused" LIMIT 1')
if card:
    routes_to_test.append((f"/vouchers/active-users/{card['id']}", f"تفاصيل كرت المشترك #{card['id']}"))

nas = query_one('SELECT id FROM wisp_nas_devices LIMIT 1')
if nas:
    routes_to_test.append((f"/nas/{nas['id']}", f"تفاصيل الراوتر NAS #{nas['id']}"))

print("=== STARTING FULL APPLICATION PAGES TEST ===")
broken_pages = []
working_pages = []

for url, label in routes_to_test:
    try:
        res = client.get(url, follow_redirects=True)
        if res.status_code == 200:
            working_pages.append((url, label, 200))
            print(f"✅ [200 OK] {label:<45} -> {url}")
        else:
            broken_pages.append((url, label, res.status_code, "HTTP status not 200"))
            print(f"❌ [{res.status_code}] {label:<45} -> {url}")
    except Exception as e:
        broken_pages.append((url, label, 500, str(e)))
        print(f"❌ [EXCEPTION] {label:<45} -> {url} (Error: {e})")

print("\n" + "=" * 80)
print(f"SUMMARY: {len(working_pages)} Working Pages | {len(broken_pages)} Broken Pages")
print("=" * 80)
for url, label, code, err in broken_pages:
    print(f"- {label} ({url}): Code {code} -> {err}")

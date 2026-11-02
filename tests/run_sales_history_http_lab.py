"""Read-only HTTP/export checks after run_sales_history_lab, on synthetic DB only."""
import os,sys,threading,json,csv,io,unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
assert sys.argv[1:]==['--run'] and os.environ.get('DB_NAME')=='sales266_fixture'
threading.Thread.start=lambda self:None
import services.license_guard_service as lic
active=lambda *a,**k:dict(valid=True,status='active',features={},limits={},plan_tier='Synthetic')
lic.get_active_license_status=active;lic.has_license_feature=lambda *a,**k:True
import core.license_feature_policy as feature
feature.verified_state=active
from web import create_app
from services.sales_reporting_service import report
from database.db import query_all,query_one,get_connection
from services.sales_source_service import enrich_sale_sources
from openpyxl import load_workbook
app=create_app({'TESTING':True,'SALES_SOURCE_ENRICHMENT':False})
prefix=json.loads(Path('/evidence/HISTORY-NATIVE.private.json').read_text())['prefix']
class Cases(unittest.TestCase):
 def setUp(self):
  self.client=app.test_client()
  with self.client.session_transaction() as s:s['admin_id']=1
 def test_page_source_quality_labels(self):
  response=self.client.get('/vouchers/sales',query_string={'q':prefix})
  self.assertEqual(response.status_code,200)
  html=response.get_data(as_text=True)
  for text in ('حالة التحديد','مصادر العمليات','مستدلة من مرجع الباقة','name="source_quality"'):
   self.assertIn(text,html)
 def test_exact_money_and_confidence_in_both_exports(self):
  for quality in ('','confirmed','inferred','incomplete','ambiguous'):
   args={'q':prefix,'source_quality':quality}
   expected=report(args,export=True,timezone='Asia/Aden')
   response=self.client.get('/sales/export',query_string=dict(args,format='csv'))
   self.assertEqual(response.status_code,200)
   rows=list(csv.reader(io.StringIO(response.get_data(as_text=True).lstrip('\ufeff'))))
   self.assertEqual(len(rows)-2,expected['total_items_count'])
   self.assertEqual(Decimal(rows[-1][6]),expected['gross_sales'])
   self.assertEqual(rows[0][-3:],['نوع الخدمة','حالة التحديد','ملاحظة التحديد'])
   if quality=='inferred':self.assertTrue(all(r[-2]=='مستدل من الباقة' for r in rows[1:-1]))
   response=self.client.get('/sales/export',query_string=dict(args,format='excel'))
   self.assertEqual(response.status_code,200)
   sheet=load_workbook(io.BytesIO(response.data)).active
   self.assertEqual(sheet.max_column,16)
   self.assertEqual(sheet.max_row-2,expected['total_items_count'])
   self.assertEqual(sheet.auto_filter.ref,'A1:P'+str(sheet.max_row-1))
 def test_invalid_and_anonymous(self):
  self.assertEqual(self.client.get('/vouchers/sales?source_quality=bad').status_code,400)
  self.assertEqual(self.client.get('/sales/export?source_quality=bad').status_code,400)
  self.assertEqual(app.test_client().get('/vouchers/sales').status_code,302)
 def test_report_get_does_not_change_evidence(self):
  before=query_all('SELECT * FROM wisp_sale_source_audit ORDER BY id')
  for quality in ('confirmed','inferred','incomplete','ambiguous'):
   self.assertEqual(self.client.get('/vouchers/sales',query_string={'q':prefix,'source_quality':quality}).status_code,200)
  self.assertEqual(query_all('SELECT * FROM wisp_sale_source_audit ORDER BY id'),before)
 def test_reseller_scope(self):
  with patch('core.ownership.owner_id',return_value=999999):
   data=report({'q':prefix},timezone='Asia/Aden')
   self.assertEqual(data['total_items_count'],0)
   self.assertEqual(data['nas_options'],[])
 def test_resume_of_finished_batch_is_noop(self):
  before=query_one('SELECT COUNT(*) n FROM wisp_sale_source_audit')['n']
  with get_connection() as c:result=enrich_sale_sources(c)
  self.assertEqual(result['scanned'],0)
  self.assertEqual(query_one('SELECT COUNT(*) n FROM wisp_sale_source_audit')['n'],before)
result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Cases))
Path('/evidence/HISTORY-HTTP.private.json').write_text(json.dumps({'passed':result.wasSuccessful(),'tests':result.testsRun,'full_factory':True,'synthetic':True}))
raise SystemExit(not result.wasSuccessful())

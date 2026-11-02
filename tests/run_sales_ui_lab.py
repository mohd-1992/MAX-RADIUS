"""Explicit synthetic MariaDB correction contracts; never main5090 or customer data."""
import os,sys,threading,json,datetime as dt,unittest
from pathlib import Path
from unittest.mock import patch
assert sys.argv[1:]==['--run'] and os.environ.get('DB_NAME')=='sales266_fixture'
threading.Thread.start=lambda self:None
from database.db import query_one,query_all,execute_write
from services.sales_reporting_service import report,month_label
from services.sales_source_service import server_key
marker=Path('/evidence/UI-SEED.private')
if not marker.exists():
 marker.write_text('synthetic fixture only')
 execute_write("INSERT INTO wisp_nas_devices(name,ip_address,secret) VALUES('Unused router','192.0.2.99','fixture-only')")
 for uid,start,up,down,server in [('ui-a','2026-10-25 00:00:00',1073741824,2147483648,'HS-without-sales'),('ui-b','2026-10-24 23:59:59',3221225472,4294967296,'HS-yesterday'),('ui-c','2025-10-25',1073741824,1073741824,'HS-old')]:
  execute_write('''INSERT INTO radacct(acctsessionid,acctuniqueid,username,nasipaddress,acctstarttime,acctstoptime,acctinputoctets,acctoutputoctets,calledstationid,servicetype,acctterminatecause) VALUES(?,?,?,'192.0.2.99',?,DATE_ADD(?,INTERVAL 1 DAY),?,?,?,'Login-User','User-Request')''',(uid,uid,'synthetic-ui',start,start,up,down,server))
 execute_write('''INSERT INTO radacct_archive(radacctid,acctsessionid,acctuniqueid,username,nasipaddress,acctstarttime,acctstoptime,acctinputoctets,acctoutputoctets,calledstationid,servicetype,acctterminatecause)
 SELECT radacctid,acctsessionid,acctuniqueid,username,nasipaddress,acctstarttime,acctstoptime,acctinputoctets,acctoutputoctets,calledstationid,servicetype,acctterminatecause FROM radacct WHERE acctuniqueid='ui-a' ''')
if not Path('/evidence/UI-MONEY-SEED.private').exists():
 Path('/evidence/UI-MONEY-SEED.private').write_text('synthetic fixture only')
 execute_write("INSERT INTO wisp_managers(username,password_hash,full_name,role_id) VALUES('ui-money-reseller','fixture-only','مدير النظام الرئيسي للاختبار الطويل',3)")
 manager=query_one("SELECT id FROM wisp_managers WHERE username='ui-money-reseller'")['id']
 for price,cost,owner,date in [(100,70,manager,'2026-10-25 00:00:00'),(150,90,manager,'2026-10-25 23:59:59'),(200,0,None,'2026-10-24 23:59:59')]:
  execute_write("INSERT INTO wisp_voucher_sales(voucher_id,batch_id,batch_name,username,serial_number,package_name,price,cost,reseller_id,activated_at) VALUES(910001,910001,'ui-money','ui-money','ui-money','باقة المعاينة',?,?,?,?)",(price,cost,owner,date))
if not Path('/evidence/UI-INVOICE-CATALOG-SEED.private').exists():
 Path('/evidence/UI-INVOICE-CATALOG-SEED.private').write_text('synthetic fixture only')
 execute_write("INSERT INTO wisp_invoices(invoice_number,subscriber_name,package_name,amount,status,paid_at) VALUES('ui-catalog-invoice','synthetic-ui','catalog-only',1,'paid','2026-10-25')")
 invoice=query_one("SELECT id FROM wisp_invoices WHERE invoice_number='ui-catalog-invoice'")['id']
 execute_write("INSERT INTO wisp_sale_sources(event_kind,event_id,nas_address,nas_label,service_type,server_name,server_interface) VALUES('invoice',?,'192.0.2.88','Invoice router','pppoe','invoice-only-server','bridge1')",(invoice,))
if not Path('/evidence/UI-NULL-SEED.private').exists():
 Path('/evidence/UI-NULL-SEED.private').write_text('synthetic fixture only')
 execute_write("INSERT INTO radacct(acctsessionid,acctuniqueid,username,nasipaddress,acctstarttime,acctstoptime,acctinputoctets,acctoutputoctets) VALUES('ui-null','ui-null','synthetic-ui','192.0.2.99','2026-10-27','2026-10-27',NULL,NULL)")
now=dt.datetime(2026,10,25,12,tzinfo=dt.timezone.utc)
def read(**args):return report(args,now=now,timezone='Asia/Aden')
class Cases(unittest.TestCase):
 def test_catalog_without_sales_and_date_independent(self):
  for period in ('all','today','yesterday'):
   data=read(period=period)
   self.assertIn(('192.0.2.99','Unused router'),data['nas_options'])
   self.assertTrue(any(p['server_name']=='HS-without-sales' for p in data['server_options']))
  self.assertEqual(read(nas='192.0.2.99')['total_items_count'],0)
  self.assertEqual(read(server=server_key('192.0.2.99','hotspot','HS-without-sales'))['gross_sales'],0)
 def test_server_catalog_shared_across_record_tabs_without_reattribution(self):
  data=read(record_kind='voucher')
  self.assertTrue(any(p['server_name']=='invoice-only-server' for p in data['server_options']))
  self.assertEqual(read(record_kind='voucher',server=server_key('192.0.2.88','pppoe','invoice-only-server','bridge1'))['total_items_count'],0)
 def test_traffic_period_and_archive_dedup(self):
  today=read(period='today')['traffic_chart']; yesterday=read(period='yesterday')['traffic_chart']
  self.assertEqual((today['labels'],today['upload'],today['download']),(['2026-10-25'],[1],[2]))
  self.assertEqual((yesterday['labels'],yesterday['upload'],yesterday['download']),(['2026-10-24'],[3],[4]))
  self.assertEqual(read(period='all')['traffic_chart']['labels'],['أكتوبر 2025','أكتوبر 2026'])
 def test_traffic_independent_of_financial_filters(self):
  self.assertEqual(read(period='today',q='no-match',record_kind='invoice')['traffic_chart'],read(period='today')['traffic_chart'])
 def test_native_unknown_counters_are_not_zero(self):
  data=read(period='custom',date_from='2026-10-27',date_to='2026-10-27')['traffic_chart']
  self.assertEqual((data['download'],data['upload'],data['unknown_counters']),([None],[None],1))
 def test_empty_custom_and_exclusive_end(self):
  self.assertEqual(read(period='custom',date_from='2026-10-23',date_to='2026-10-23')['traffic_chart']['labels'],[])
  self.assertEqual(read(period='custom',date_from='2026-10-24',date_to='2026-10-24')['traffic_chart']['download'],[4])
 def test_owner_no_global_catalog_or_traffic(self):
  with patch('core.ownership.owner_id',return_value=999999):
   data=read()
   self.assertEqual(data['nas_options'],[])
   self.assertEqual(data['traffic_chart']['labels'],[])
 def test_arabic_year_labels(self):
  self.assertEqual(month_label('2026-01'),'يناير 2026')
  self.assertEqual(month_label('2025-10'),'أكتوبر 2025')
 def test_exact_historical_money_under_time_filters(self):
  checks=[('all',3,450,360,90),('today',2,250,160,90),('yesterday',1,200,200,0)]
  for period,count,gross,net,commission in checks:
   data=read(q='ui-money',period=period)
   self.assertEqual((data['total_items_count'],data['gross_sales'],data['net_isp_revenue'],data['total_reseller_commissions']),(count,gross,net,commission))
 def test_filtered_financial_reconciliation(self):
  for args in ({},{'period':'today'},{'period':'yesterday'},{'service_type':'pppoe'},{'server':'unknown'},{'reseller_id':'direct'}):
   data=read(q='ui-money',**args)
   self.assertEqual(sum(p['total_gross_revenue'] for p in data['package_breakdown']),data['gross_sales'])
   self.assertEqual(sum(p['total_isp_revenue'] for p in data['server_breakdown']),data['net_isp_revenue'])
   self.assertEqual(sum(p['total_reseller_profit'] for p in data['package_breakdown']),data['total_reseller_commissions'])
result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Cases))
Path('/evidence/ui-contract-proof.private.json').write_text(json.dumps({'passed':result.wasSuccessful(),'tests':result.testsRun}))
raise SystemExit(not result.wasSuccessful())

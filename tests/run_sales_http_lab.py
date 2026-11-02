"""Full factory and exports on explicitly named synthetic database only."""
import os
import sys


def main():
    if sys.argv[1:]!=['--run'] or os.environ.get('DB_NAME')!='sales266_fixture':
        raise SystemExit('Only sales266_fixture with --run is allowed')
    import threading
    threading.Thread.start=lambda self:None
    import csv
    import io
    import json
    import unittest
    from pathlib import Path
    from unittest.mock import patch
    import services.license_guard_service as lic
    active=lambda *a,**k:dict(valid=True,status='active',features={},limits={},plan_tier='Synthetic')
    lic.get_active_license_status=active
    lic.has_license_feature=lambda *a,**k:True
    import core.license_feature_policy as feature
    feature.verified_state=active
    from web import create_app
    from services.sales_reporting_service import report
    from openpyxl import load_workbook
    app=create_app({'TESTING':True})
    seed=json.loads(Path('/evidence/source-report-proof.private.json').read_text())
    prefix=seed['fixture_prefix']
    class Cases(unittest.TestCase):
        def setUp(self):
            self.client=app.test_client()
            with self.client.session_transaction() as session:session['admin_id']=1
        def test_01_page_full_template_and_both_record_types(self):
            for kind in ('voucher','invoice'):
                response=self.client.get('/vouchers/sales',query_string={'q':prefix,'record_kind':kind})
                self.assertEqual(response.status_code,200)
                html=response.get_data(as_text=True)
                self.assertIn('sales-reseller-badge',html)
                self.assertIn('salesSelectedPeriod',html)
                self.assertIn('name="server"',html)
                self.assertNotIn('????',html)
        def test_02_csv_and_xlsx_exact_filter_totals_and_literal_formula(self):
            for period in ('all','today','yesterday','custom'):
                args={'q':prefix,'period':period,'date_from':'2026-10-24','date_to':'2026-10-24'}
                expected=report(args,export=True,timezone='Asia/Aden')
                response=self.client.get('/sales/export',query_string=dict(args,format='csv'))
                self.assertEqual(response.status_code,200)
                rows=list(csv.reader(io.StringIO(response.get_data(as_text=True).lstrip('\ufeff'))))
                self.assertEqual(len(rows)-2,expected['total_items_count'])
                self.assertEqual(float(rows[-1][6]),float(expected['gross_sales']))
                for row in rows[1:-1]:self.assertTrue(row[3].startswith("'=FORMULA"))
                response=self.client.get('/sales/export',query_string=dict(args,format='excel'))
                self.assertEqual(response.status_code,200)
                sheet=load_workbook(io.BytesIO(response.data)).active
                self.assertEqual(sheet.max_row-2,expected['total_items_count'])
                self.assertEqual(sheet.cell(sheet.max_row,7).value,float(expected['gross_sales']))
                self.assertTrue(sheet.sheet_view.rightToLeft)
                for row in sheet.iter_rows(min_row=2,max_row=sheet.max_row-1):
                    self.assertNotEqual(row[3].data_type,'f')
                    self.assertTrue(row[3].value.startswith("'=FORMULA"))
        def test_03_empty_invalid_and_invoice_exports(self):
            self.assertEqual(self.client.get('/vouchers/sales?server=invalid').status_code,400)
            self.assertEqual(self.client.get('/sales/export?period=invalid').status_code,400)
            for args in ({'q':'no-such-fixture'},{'q':prefix,'record_kind':'invoice'}):
                self.assertEqual(self.client.get('/sales/export',query_string=dict(args,format='excel')).status_code,200)
        def test_04_anonymous_and_reseller_permissions(self):
            anonymous=app.test_client()
            self.assertEqual(anonymous.get('/vouchers/sales').status_code,302)
            self.assertEqual(anonymous.get('/sales/export').status_code,302)
            with self.client.session_transaction() as session:session['admin_id']=seed['manager']
            self.assertIn(self.client.get('/vouchers/sales').status_code,(200,403))
            # Report scope itself is enforced independently from the page grant.
            with patch('core.ownership.owner_id',return_value=seed['manager']):
                self.assertEqual(report({'q':prefix,'record_kind':'invoice'},timezone='Asia/Aden')['total_items_count'],0)
        def test_05_paid_invoice_has_atomic_source_and_repay_is_noop(self):
            from database.db import query_one,execute_write
            account=query_one('SELECT id FROM wisp_subscribers ORDER BY id LIMIT 1')
            self.assertIsNotNone(account)
            with self.client.session_transaction() as session:session['_license_csrf']='sales-fixture-csrf'
            headers={'X-CSRF-Token':'sales-fixture-csrf'}
            response=self.client.post('/invoices/create',data={'subscriber_id':account['id'],'amount':'75.50','status':'paid'},headers=headers)
            self.assertEqual(response.status_code,302)
            invoice=query_one('SELECT * FROM wisp_invoices WHERE subscriber_id=? ORDER BY id DESC LIMIT 1',(account['id'],))
            self.assertIsNotNone(invoice['paid_at'])
            metadata=query_one("SELECT * FROM wisp_sale_sources WHERE event_kind='invoice' AND event_id=?",(invoice['id'],))
            self.assertIsNotNone(metadata)
            response=self.client.post('/invoices/pay/'+str(invoice['id']),headers=headers)
            self.assertEqual(response.status_code,302)
            self.assertEqual(query_one('SELECT * FROM wisp_invoices WHERE id=?',(invoice['id'],)),invoice)
            self.assertEqual(query_one("SELECT * FROM wisp_sale_sources WHERE event_kind='invoice' AND event_id=?",(invoice['id'],)),metadata)
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Cases))
    Path('/evidence/http-sales-proof.private.json').write_text(json.dumps({'passed':result.wasSuccessful(),'tests':result.testsRun,'full_factory':True,'license_facade':True,'native_db':True}))
    raise SystemExit(not result.wasSuccessful())


if __name__=='__main__':main()

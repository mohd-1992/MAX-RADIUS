"""Explicit synthetic MariaDB acceptance, never production or local5090 data."""
import os
import sys


def main():
    if sys.argv[1:]!=['--run'] or os.environ.get('DB_NAME')!='sales266_fixture':
        raise SystemExit('Only --run against sales266_fixture is allowed')
    import threading
    threading.Thread.start=lambda self:None
    import datetime as dt
    import json
    import unittest
    import uuid
    from decimal import Decimal
    from pathlib import Path
    from unittest.mock import patch
    from database.db import execute_write,query_all,query_one,get_connection
    from database.sales_source_schema import install_sales_source_schema
    from services.sales_reporting_service import report
    from services.sales_source_service import server_key,record_sale_source,backfill_sale_sources
    from services.online_policy_service import local
    stamp='sales_fixture_'+uuid.uuid4().hex[:8]
    now=dt.datetime(2026,10,25,12,tzinfo=dt.timezone.utc)
    execute_write("INSERT INTO wisp_managers(username,password_hash,full_name,role_id) VALUES(?,'fixture-only',?,3)",(stamp,'مدير النظام الرئيسي للاختبار الطويل'))
    manager=query_one('SELECT id FROM wisp_managers WHERE username=?',(stamp,))['id']
    ids=[]
    def sale(price,cost,reseller,date,server,kind='hotspot',port='',nas='192.0.2.10',label='Router A',name='الباقة المشتركة'):
        execute_write('''INSERT INTO wisp_voucher_sales(voucher_id,batch_id,batch_name,username,serial_number,
            package_name,price,cost,reseller_id,activated_at) VALUES(900001,900001,?,?,?,?,?,?,?,?)''',
            (stamp,stamp,'=FORMULA',name,price,cost,reseller,date))
        ident=query_one('SELECT MAX(id) id FROM wisp_voucher_sales')['id'];ids.append(ident)
        if server is not None:
            execute_write('''INSERT INTO wisp_sale_sources(event_kind,event_id,nas_address,nas_label,service_type,server_name,server_interface)
                VALUES('voucher',?,?,?,?,?,?)''',(ident,nas,label,kind,server,port))
        return ident
    # Independent old prices; one NAS, two HS names and two PPPoE interfaces.
    a=sale(100,70,manager,'2026-10-25 00:00:00','HS1')
    b=sale(150,90,manager,'2026-10-25 23:59:59','HS1',label='Router renamed')
    c=sale(200,0,None,'2026-10-24 23:59:59','HS2')
    d=sale(300,200,manager,'2026-10-24 00:00:00','PP','pppoe','bridge1')
    e=sale(400,250,manager,'2026-10-25 09:00:00','PP','pppoe','bridge2')
    f=sale(500,0,None,'2026-10-26 00:00:00',None)
    g=sale(50,0,None,None,None)
    def invoice(title,amount,status,paid):
        number=stamp+'_'+uuid.uuid4().hex[:5]
        execute_write('''INSERT INTO wisp_invoices(invoice_number,subscriber_name,package_name,amount,status,paid_at)
            VALUES(?,?,?,?,?,?)''',(number,stamp,title,amount,status,paid))
    invoice('اشتراك شهري',1000,'paid','2026-10-25 10:00:00')
    invoice('إيداع رصيد محفظة',2000,'paid','2026-10-25 10:00:00')
    invoice('اشتراك شهري',9999,'unpaid',None)
    invoice('اشتراك شهري',250,'paid',None)
    base={'q':stamp,'period':'all'}
    def read(**kwargs):return report(dict(base,**kwargs),now=now,timezone='Asia/Aden',export=False)
    class Cases(unittest.TestCase):
        def test_01_native_filters_reconcile_historical_money(self):
            checks=[({},7,1700,1360,340),({'period':'today'},3,650,410,240),
                    ({'period':'yesterday'},2,500,400,100),({'reseller_id':'direct'},3,750,750,0),
                    ({'reseller_id':str(manager)},4,950,610,340),({'service_type':'pppoe'},2,700,450,250),
                    ({'server':server_key('192.0.2.10','hotspot','HS1')},2,250,160,90),
                    ({'server':'unknown'},2,550,550,0),({'period':'custom','date_from':'2026-10-24','date_to':'2026-10-24'},2,500,400,100)]
            # All ledger amounts, including the zero-cost legacy/direct convention.
            for args,count,gross,net,commission in checks:
                with self.subTest(args=args):
                    data=read(**args)
                    self.assertEqual((data['total_items_count'],data['gross_sales'],data['net_isp_revenue'],data['total_reseller_commissions']),
                                     (count,Decimal(gross),Decimal(net),Decimal(commission)))
                    self.assertEqual(sum(r['price'] for r in data['recent_sales']),data['gross_sales'])
        def test_02_same_point_labels_do_not_split_server(self):
            data=read(service_type='hotspot')
            rows=[r for r in data['server_breakdown'] if r['server_name']=='HS1']
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['sold_count'],2)
        def test_03_invoice_tab_excludes_wallet_and_unpaid(self):
            data=read(record_kind='invoice')
            self.assertEqual((data['total_items_count'],data['gross_sales']),(2,Decimal(1250)))
            self.assertEqual(read(record_kind='invoice',period='today')['gross_sales'],Decimal(1000))
            self.assertEqual(data['unknown_date_count'],1)
        def test_04_reseller_scope_no_invoice_or_other_reseller_leak(self):
            with patch('core.ownership.owner_id',return_value=manager):
                self.assertEqual(read()['total_items_count'],4)
                self.assertEqual(read(record_kind='invoice')['total_items_count'],0)
                self.assertEqual(read(reseller_id='direct')['total_items_count'],0)
        def test_05_read_report_never_writes_ledger(self):
            before=query_all('SELECT * FROM wisp_voucher_sales WHERE batch_name=? ORDER BY id',(stamp,))
            read(period='today');read(period='yesterday');read(package_name='حذفت الباقة')
            self.assertEqual(query_all('SELECT * FROM wisp_voucher_sales WHERE batch_name=? ORDER BY id',(stamp,)),before)
        def test_06_native_key_matches_python(self):
            for row in query_all('SELECT * FROM wisp_sale_sources WHERE event_id IN ('+','.join('?' for _ in ids)+')',tuple(ids)):
                if row['server_key']!='unknown':
                    self.assertEqual(row['server_key'],server_key(row['nas_address'],row['service_type'],row['server_name'],row['server_interface']))
        def test_07_reinstall_is_idempotent_and_preserves_metadata(self):
            before=query_all('SELECT * FROM wisp_sale_sources ORDER BY event_kind,event_id')
            conn=get_connection()
            try:install_sales_source_schema(conn)
            finally:conn.close()
            self.assertEqual(query_all('SELECT * FROM wisp_sale_sources ORDER BY event_kind,event_id'),before)
        def test_08_capture_immutable_and_transaction_rollback(self):
            ident=sale(19,0,None,'2026-10-25',None)
            local.snapshot={'a':{'kind':'hotspot','row':{'username':stamp,'nasipaddress':'192.0.2.10','calledstationid':'HS-snapshot','acctuniqueid':'fixture-uid','acctstarttime':now,'acctupdatetime':now}}}
            conn=get_connection()
            try:
                record_sale_source(conn,'voucher',ident,stamp);conn.rollback()
                self.assertIsNone(query_one("SELECT * FROM wisp_sale_sources WHERE event_kind='voucher' AND event_id=?",(ident,)))
                record_sale_source(conn,'voucher',ident,stamp);conn.commit()
                first=query_one("SELECT * FROM wisp_sale_sources WHERE event_kind='voucher' AND event_id=?",(ident,))
                local.snapshot['a']['row']['calledstationid']='later-server'
                record_sale_source(conn,'voucher',ident,stamp);conn.commit()
                self.assertEqual(query_one("SELECT * FROM wisp_sale_sources WHERE event_kind='voucher' AND event_id=?",(ident,)),first)
            finally:conn.close();del local.snapshot
        def test_09_unknown_history_does_not_guess_latest_login(self):
            conn=get_connection()
            try:backfill_sale_sources(conn)
            finally:conn.close()
            rows=query_all("SELECT * FROM wisp_sale_sources WHERE event_kind='voucher' AND event_id IN (?,?)",(f,g))
            self.assertEqual(len(rows),2)
            self.assertTrue(all(r['server_key']=='unknown' for r in rows))
        def test_10_zero_cost_is_recorded_zero_and_null_is_unknown(self):
            name=stamp+'_zero'
            sale(100,0,manager,'2026-10-25',None,name=name)
            data=read(package_name=name)
            self.assertEqual((data['net_isp_revenue'],data['total_reseller_commissions']),(0,100))
            sale(50,None,manager,'2026-10-25',None,name=name)
            data=read(package_name=name)
            self.assertEqual(data['gross_sales'],150)
            self.assertEqual(data['unknown_cost_count'],1)
            self.assertTrue(any(r['isp_share'] is None for r in data['recent_sales']))
        def test_11_metadata_error_rolls_back_purchase_balance_and_invoice(self):
            from services.user_portal_service import renew_or_change_package
            from core.radius_sync import sync_subscriber_to_radius
            name=stamp+'_purchase'
            execute_write("INSERT INTO wisp_packages(name,service_type,price,cost,volume_quota_mb,validity_value,validity_unit,is_active) VALUES(?,'hotspot',25,7,1000,30,'days',1)",(name,))
            pkg=query_one('SELECT id FROM wisp_packages WHERE name=?',(name,))['id']
            execute_write("INSERT INTO wisp_subscribers(username,password,full_name,service_type,package_id,status,balance,last_renewed_at,expires_at) VALUES(?,'fixture-only',?,'hotspot',?,'active',100,NOW()-INTERVAL 1 DAY,NOW()+INTERVAL 1 DAY)",(name,name,pkg))
            account=query_one('SELECT * FROM wisp_subscribers WHERE username=?',(name,))
            sync_subscriber_to_radius(account['id'])
            before=query_one('SELECT * FROM wisp_subscribers WHERE id=?',(account['id'],))
            execute_write("CREATE TRIGGER sales_fixture_abort BEFORE INSERT ON wisp_sale_sources FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='synthetic source failure'")
            try:
                with patch('services.renewal_settlement_service.current_radius_container',return_value='isolated-core'), patch('core.license_feature_policy.verified_state',return_value=dict(valid=True,features={})):
                    with self.assertRaisesRegex(Exception,'synthetic source failure'):renew_or_change_package(name,pkg)
            finally:execute_write('DROP TRIGGER sales_fixture_abort')
            self.assertEqual(query_one('SELECT * FROM wisp_subscribers WHERE id=?',(account['id'],)),before)
            self.assertEqual(query_one('SELECT COUNT(*) n FROM wisp_invoices WHERE subscriber_id=?',(account['id'],))['n'],0)
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Cases))
    proof={'passed':result.wasSuccessful(),'tests':result.testsRun,'native_mariadb':True,'synthetic':True,
           'fixture_prefix':stamp,'manager':manager,'physical_nas':False}
    Path('/evidence/source-report-proof.private.json').write_text(json.dumps(proof),encoding='utf-8')
    raise SystemExit(not result.wasSuccessful())


if __name__=='__main__':main()

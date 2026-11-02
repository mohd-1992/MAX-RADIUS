"""Explicit synthetic MariaDB source enrichment; never customer or main5090 data."""
import os,sys
if sys.argv[1:]!=['--run'] or os.environ.get('DB_NAME')!='sales266_fixture':
    raise SystemExit('Only --run against synthetic sales266_fixture is allowed')
import threading,uuid,json,unittest
from pathlib import Path
threading.Thread.start=lambda self:None
from database.db import execute_write,query_one,query_all,get_connection
from database.sales_source_schema import install_sales_source_schema
from services.sales_source_service import enrich_sale_sources,server_key
from services.sales_reporting_service import report
stamp='hist_'+uuid.uuid4().hex[:8]
marker=Path('/evidence/HISTORY-NATIVE-STARTED.private')
assert not marker.exists(),'Inspect prior result; do not replay this seed round'
marker.write_text(stamp)

def card(label,kind='hotspot'):
    name=stamp+'_'+label
    execute_write("INSERT INTO wisp_packages(name,service_type,price,cost,is_active) VALUES(?,?,100,70,1)",(name,kind))
    pkg=query_one('SELECT id FROM wisp_packages WHERE name=?',(name,))['id']
    execute_write('INSERT INTO wisp_voucher_batches(batch_number,name,package_id,card_count) VALUES(?,?,?,1)',(name,name,pkg))
    batch=query_one('SELECT id FROM wisp_voucher_batches WHERE batch_number=?',(name,))['id']
    execute_write("INSERT INTO wisp_vouchers(batch_id,serial_number,username,password,pin_code,package_id,status,first_used_at,expires_at) VALUES(?,?,?,'fixture-only','fixture-only',?,'active','2026-10-20','2027-01-01')",(batch,name,name,pkg))
    voucher=query_one('SELECT id FROM wisp_vouchers WHERE username=?',(name,))['id']
    execute_write("INSERT INTO wisp_voucher_sales(voucher_id,batch_id,batch_name,username,serial_number,package_name,price,cost,activated_at) VALUES(?,?,?,?,?,?,100,70,'2026-10-25 10:00:00')",(voucher,batch,name,name,name,name))
    sale=query_one('SELECT MAX(id) id FROM wisp_voucher_sales')['id']
    execute_write("INSERT INTO wisp_sale_sources(event_kind,event_id) VALUES('voucher',?)",(sale,))
    return dict(name=name,voucher=voucher,sale=sale)

def packet(c,server='HS1',port='ether1',proto='',service='Login-User',start='2026-10-25 09:00:00',stop='2026-10-25 11:00:00'):
    uid=uuid.uuid4().hex
    execute_write("INSERT INTO radacct(username,acctsessionid,acctuniqueid,nasipaddress,nasportid,calledstationid,framedprotocol,servicetype,acctstarttime,acctupdatetime,acctstoptime,acctinputoctets,acctoutputoctets,acctterminatecause) VALUES(?,?,?,'192.0.2.10',?,?,?,?,?,?,?,10,20,'User-Request')",(c['name'],uid,uid,port,server,proto,service,start,stop,stop))
    return query_one('SELECT radacctid FROM radacct WHERE acctuniqueid=?',(uid,))['radacctid']

def enrich(retry=3600):
    with get_connection() as conn:return enrich_sale_sources(conn,limit=500,retry_seconds=retry)

def source(c):return query_one("SELECT * FROM wisp_sale_sources WHERE event_kind='voucher' AND event_id=?",(c['sale'],))
def audits(c):return query_one("SELECT COUNT(*) n FROM wisp_sale_source_audit WHERE event_kind='voucher' AND event_id=?",(c['sale'],))['n']

class Cases(unittest.TestCase):
    def test_01_package_inference_is_not_confirmed(self):
        c=card('inferred');packet(c,service='');enrich()
        s=source(c);self.assertEqual((s['service_type'],s['resolution_status'],s['service_attribution']),('hotspot','inferred','package_reference'))
        self.assertNotEqual(s['server_key'],'unknown')
        self.assertEqual(audits(c),1)
    def test_02_packet_proof_over_current_package(self):
        c=card('packet','pppoe');packet(c);enrich();s=source(c)
        self.assertEqual((s['service_type'],s['resolution_status']),('hotspot','confirmed'))
    def test_03_later_session_never_rewrites_old_sale(self):
        c=card('later');packet(c,start='2026-10-26',stop='2026-10-27');enrich()
        self.assertEqual(source(c)['server_key'],'unknown')
    def test_04_partial_nas_and_service_are_retained(self):
        c=card('partial');packet(c,server='');enrich();s=source(c)
        self.assertEqual((s['service_type'],s['nas_address'],s['server_key']),('hotspot','192.0.2.10','unknown'))
    def test_05_multiple_servers_need_review(self):
        c=card('ambiguous');packet(c);packet(c,server='HS2');enrich()
        self.assertEqual((source(c)['resolution_status'],source(c)['server_key']),('ambiguous','unknown'))
    def test_06_hotspot_ports_are_same_point(self):
        c=card('ports');packet(c);packet(c,port='ether2');enrich()
        self.assertEqual(source(c)['resolution_status'],'confirmed')
    def test_07_pppoe_interfaces_are_distinct(self):
        c=card('ppp','pppoe');packet(c,proto='PPP',service='',port='bridge1');packet(c,proto='PPP',service='',port='bridge2');enrich()
        self.assertEqual(source(c)['resolution_status'],'ambiguous')
    def test_08_renamed_identity_uses_stable_owner(self):
        c=card('renamed');old=c['name']+'_old'
        execute_write('INSERT INTO wisp_voucher_names(username,voucher_id) VALUES(?,?)',(old,c['voucher']))
        execute_write('UPDATE wisp_voucher_sales SET username=? WHERE id=?',(old,c['sale']))
        packet(dict(c,name=old));enrich()
        self.assertEqual(source(c)['resolution_status'],'confirmed')
    def test_09_foreign_owner_is_excluded(self):
        c=card('foreign');other=card('foreign_other');acct=packet(c)
        execute_write('UPDATE radacct SET account_voucher_id=? WHERE radacctid=?',(other['voucher'],acct));enrich()
        self.assertEqual(source(c)['server_key'],'unknown')
    def test_10_late_evidence_promotes_inference_and_audits_once(self):
        c=card('late');packet(c,service='');enrich();self.assertEqual(source(c)['resolution_status'],'inferred')
        execute_write("UPDATE radacct SET servicetype='Login-User' WHERE username=?",(c['name'],))
        enrich(retry=0);self.assertEqual(source(c)['resolution_status'],'confirmed');self.assertEqual(audits(c),2)
        enrich(retry=0);self.assertEqual(audits(c),2)
    def test_11_confirmed_point_stays_immutable(self):
        c=card('immutable');packet(c);enrich();before=source(c)
        packet(c,server='other');enrich(retry=0)
        self.assertEqual(source(c),before)
    def test_12_audit_failure_rolls_back_metadata_and_ledger_unchanged(self):
        c=card('rollback');packet(c);before=source(c)
        ledger=query_one('SELECT * FROM wisp_voucher_sales WHERE id=?',(c['sale'],))
        execute_write("CREATE TRIGGER history_audit_abort BEFORE INSERT ON wisp_sale_source_audit FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='synthetic audit failure'")
        try:
            with self.assertRaises(Exception):enrich()
        finally:execute_write('DROP TRIGGER history_audit_abort')
        self.assertEqual(source(c),before);self.assertEqual(audits(c),0)
        self.assertEqual(query_one('SELECT * FROM wisp_voucher_sales WHERE id=?',(c['sale'],)),ledger)
        enrich()
    def test_13_repeated_installer_preserves_history(self):
        before=query_all('SELECT * FROM wisp_sale_source_audit ORDER BY id')
        with get_connection() as conn:install_sales_source_schema(conn)
        self.assertEqual(query_all('SELECT * FROM wisp_sale_source_audit ORDER BY id'),before)
    def test_14_confidence_filters_preserve_money(self):
        all_data=report({'q':stamp},timezone='Asia/Aden')
        parts=[report({'q':stamp,'source_quality':k},timezone='Asia/Aden') for k in ('confirmed','inferred','incomplete','ambiguous')]
        self.assertEqual(sum(d['gross_sales'] for d in parts),all_data['gross_sales'])
        self.assertEqual(sum(d['total_items_count'] for d in parts),all_data['total_items_count'])
        self.assertGreater(report({'q':stamp,'source_quality':'inferred'},timezone='Asia/Aden')['inferred_count'],0)

result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Cases))
Path('/evidence/HISTORY-NATIVE.private.json').write_text(json.dumps({'passed':result.wasSuccessful(),'tests':result.testsRun,'prefix':stamp,'synthetic':True,'actual_router':False}))
raise SystemExit(not result.wasSuccessful())

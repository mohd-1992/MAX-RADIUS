"""Destructive history checks ONLY on the separately cloned local history DB."""
import datetime as dt
import json
import os
from pathlib import Path
import sys
import threading
import unittest
import uuid
from unittest.mock import patch

assert sys.argv[1:]==['--run'] and os.environ.get('DB_NAME')=='traffic266_history_fixture'
real_start=threading.Thread.start
threading.Thread.start=lambda self:None
import services.license_guard_service as lic
active=lambda *a,**k:dict(valid=True,status='active',features={},limits={},plan_tier='Synthetic')
lic.get_active_license_status=active
lic.has_license_feature=lambda *a,**k:True
import core.license_feature_policy as feature
feature.verified_state=active
from database.db import get_connection
from database.traffic_history_schema import install_traffic_history_schema
from database.subscriber_edit_schema import install_subscriber_edit_schema
from services.account_lifecycle_service import delete_account,delete_voucher_batch,sql
from services.traffic_history_service import preserve_sessions
from services.network_traffic_service import report
from services.sales_source_service import server_key
from services.db_maintenance_service import prune_historical_logs
from services.accounting_archiver_service import _archive_old_sessions_locked

conn=get_connection()
try:
    install_traffic_history_schema(conn)
    install_subscriber_edit_schema(conn)
    install_traffic_history_schema(conn)
    conn.commit()
finally:conn.close()


def run(statement,params=(),fetch=None):
    conn=get_connection()
    try:
        result=sql(conn,statement,params,fetch)
        ident=None
        if statement.lstrip().startswith('INSERT'):
            ident=sql(conn,'SELECT LAST_INSERT_ID() AS id',fetch='one')['id']
        conn.commit()
        return ident if ident is not None else result
    finally:conn.close()


class Retention(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # This lab also supports a freshly bootstrapped, empty fixture database.
        if not run('SELECT MIN(id) AS id FROM wisp_packages',fetch='one')['id']:
            run("INSERT INTO wisp_packages(name,service_type,price,cost,is_active) VALUES('retention-fixture','hotspot',25,7,1)")

    def setUp(self):
        self.prefix='retain_'+uuid.uuid4().hex[:10]
        self.pkg=run('SELECT MIN(id) AS id FROM wisp_packages',fetch='one')['id']
        self.batch=run('INSERT INTO wisp_voucher_batches(batch_number,name,package_id,card_count,reseller_id) VALUES(?,?,?,1,?)',
                       (self.prefix,self.prefix,self.pkg,99123))
        # Full-history deletion of a suspended card; expired-card summary deletion
        # has its own end-to-end case in test_reservation_release_index.py.
        self.card=run('''INSERT INTO wisp_vouchers(batch_id,package_id,reseller_id,serial_number,username,password,pin_code,status,last_renewed_at)
            VALUES(?,?,99123,?,?,? ,?,'suspended','2020-01-01')''',(self.batch,self.pkg,self.prefix,self.prefix,'synthetic',self.prefix))
        self.session=run('''INSERT INTO radacct(acctuniqueid,acctsessionid,username,nasipaddress,calledstationid,nasportid,
            acctstarttime,acctupdatetime,acctstoptime,acctinputoctets,acctoutputoctets,servicetype,acctterminatecause)
            VALUES(?,?,?,'192.0.2.51',?,'ether1','2001-01-02 01:00:00','2001-01-02 02:00:00','2001-01-02 02:00:00',50,100,'Login-User','User-Request')''',
            (self.prefix,self.prefix,self.prefix,self.prefix))
        self.sale=run('''INSERT INTO wisp_voucher_sales(voucher_id,batch_id,batch_name,username,package_name,price,cost,reseller_id,activated_at)
            VALUES(?,?,?,?,?,10,4,99123,'2001-01-02 01:00:00')''',(self.card,self.batch,self.prefix,self.prefix,self.prefix))

    def report(self,owner=None,**extra):
        args=dict(period='custom',date_from='2001-01-02',date_to='2001-01-02',nas='192.0.2.51')
        args.update(extra)
        with patch('core.ownership.owner_id',return_value=owner):
            return report(args,timezone='Asia/Aden')

    def signature(self,owner=None):
        result=self.report(owner)
        from services.sales_reporting_service import legacy_traffic_for_period as traffic_for_period
        conn=get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
                def query(statement,values):
                    cur.execute(statement,values);return list(cur.fetchall())
                chart=traffic_for_period(query,dict(period='custom',date_from='2001-01-02',date_to='2001-01-02'),owner=owner)
            conn.rollback()
        finally:conn.close()
        return result['totals'],[(p['server_key'],p['sessions'],p['download'],p['upload']) for p in result['points']],chart

    def preserve(self):
        conn=get_connection()
        try:
            preserve_sessions(conn,'radacct','a.radacctid=?',(self.session,))
            conn.commit()
        finally:conn.close()

    def test_delete_card_preserves_reports_and_reseller_ownership(self):
        before=self.signature();owned=self.signature(99123)
        sale=run('SELECT * FROM wisp_voucher_sales WHERE id=?',(self.sale,),'one')
        self.assertTrue(delete_account('voucher',self.card)['deleted'])
        self.assertEqual(self.signature(),before)
        self.assertEqual(self.signature(99123),owned)
        self.assertEqual(run('SELECT * FROM wisp_voucher_sales WHERE id=?',(self.sale,),'one'),sale)
        self.assertEqual(self.report(99124)['totals']['sessions'],0)
        row=run('SELECT account_kind FROM wisp_traffic_history WHERE username=?',(self.prefix,),'one')
        self.assertEqual(row['account_kind'],'voucher')

    def test_delete_batch_retains_sales_and_consumption(self):
        before=self.signature();owned=self.signature(99123)
        self.assertTrue(delete_voucher_batch(self.batch,'synthetic'))
        self.assertEqual(self.signature(),before)
        self.assertEqual(self.signature(99123),owned)
        self.assertIsNotNone(run('SELECT id FROM wisp_voucher_sales WHERE id=?',(self.sale,),'one'))

    def test_pruning_and_repeated_pruning_keep_exact_report(self):
        before=self.signature();owned=self.signature(99123)
        result=prune_historical_logs(90,prune_postauth=False)
        self.assertTrue(result['success'],result['message'])
        self.assertIsNone(run('SELECT radacctid FROM radacct WHERE radacctid=?',(self.session,),'one'))
        self.assertEqual(self.signature(),before)
        self.assertEqual(self.signature(99123),owned)
        self.assertTrue(prune_historical_logs(90,prune_postauth=False)['success'])
        self.assertEqual(self.signature(),before)

    def test_archive_then_prune_does_not_double_count(self):
        before=self.signature()
        success,message=_archive_old_sessions_locked(90,100)
        self.assertTrue(success,message)
        self.assertEqual(self.signature(),before)
        self.assertIsNotNone(run('SELECT radacctid FROM wisp_traffic_history WHERE radacctid=? AND username=?',(self.session,self.prefix),'one'))
        self.assertIsNone(run('SELECT radacctid FROM radacct_archive WHERE radacctid=?',(self.session,),'one'))
        self.assertTrue(delete_account('voucher',self.card)['deleted'])
        self.assertEqual(self.signature(),before)

    def test_failed_preservation_prevents_cleanup_and_account_deletion(self):
        before=self.signature()
        with patch('services.traffic_history_service.preserve_sessions',side_effect=RuntimeError('synthetic write failure')):
            result=prune_historical_logs(90,prune_postauth=False)
            self.assertFalse(result['success'])
            with self.assertRaises(RuntimeError):delete_account('voucher',self.card)
            with self.assertRaises(RuntimeError):delete_voucher_batch(self.batch,'synthetic')
        self.assertIsNotNone(run('SELECT radacctid FROM radacct WHERE radacctid=?',(self.session,),'one'))
        self.assertIsNotNone(run('SELECT id FROM wisp_vouchers WHERE id=?',(self.card,),'one'))
        self.assertEqual(self.signature(),before)

    def test_null_counters_idempotent_copy_and_concurrent_copy(self):
        run('UPDATE radacct SET acctinputoctets=NULL WHERE radacctid=?',(self.session,))
        before=self.signature()
        errors=[]
        def copy():
            try:self.preserve()
            except Exception as error:errors.append(type(error).__name__)
        threads=[threading.Thread(target=copy) for _ in range(2)]
        for thread in threads:real_start(thread)
        for thread in threads:thread.join(15)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors,[])
        self.preserve()
        self.assertEqual(run('SELECT COUNT(*) AS n FROM wisp_traffic_history WHERE username=?',(self.prefix,),'one')['n'],1)
        self.assertIsNone(run('SELECT acctinputoctets FROM wisp_traffic_history WHERE username=?',(self.prefix,),'one')['acctinputoctets'])
        self.assertEqual(self.signature(),before)

    def test_corrupt_identity_blocks_copy_without_delete(self):
        self.preserve()
        run("UPDATE wisp_traffic_history SET calledstationid='conflict' WHERE username=?",(self.prefix,))
        with self.assertRaises(RuntimeError):self.preserve()
        self.assertIsNotNone(run('SELECT radacctid FROM radacct WHERE radacctid=?',(self.session,),'one'))
        run('UPDATE wisp_traffic_history SET calledstationid=? WHERE username=?',(self.prefix,self.prefix))

    def test_subscriber_delete_keeps_invoice_and_identity(self):
        name=self.prefix+'_sub'
        subscriber=run('INSERT INTO wisp_subscribers(username,password,full_name,package_id,status,last_renewed_at) VALUES(?,?,?,?,?,?)',
                       (name,'synthetic','Synthetic retained subscriber',self.pkg,'expired','2020-01-01'))
        run('''INSERT INTO radacct(acctuniqueid,acctsessionid,username,nasipaddress,calledstationid,servicetype,
            acctstarttime,acctstoptime,acctinputoctets,acctoutputoctets) VALUES(?,?,?,'192.0.2.51',?,'Login-User','2001-01-02 04:00:00','2001-01-02 05:00:00',700,800)''',(name,name,name,name))
        invoice=run("INSERT INTO wisp_invoices(invoice_number,subscriber_id,subscriber_name,package_name,amount,status,paid_at) VALUES(?,?,?,?,10,'paid','2001-01-02')",(name,subscriber,name,'Synthetic'))
        before=self.signature()
        self.assertTrue(delete_account('subscriber',subscriber)['deleted'])
        self.assertEqual(self.signature(),before)
        row=run('SELECT subscriber_id,amount,status FROM wisp_invoices WHERE id=?',(invoice,),'one')
        self.assertIsNone(row['subscriber_id'])
        self.assertEqual(row['status'],'paid')
        self.assertEqual(int(row['amount']),10)
        consumer=run('SELECT account_kind,display_name FROM wisp_traffic_history WHERE username=?',(name,),'one')
        self.assertEqual(consumer['account_kind'],'subscriber')
        self.assertEqual(consumer['display_name'],'Synthetic retained subscriber')

    def test_renaming_saved_history_does_not_duplicate_sessions(self):
        from services.voucher_edit_service import _claim_history
        self.preserve()
        before=self.report()['totals']
        conn=get_connection()
        try:
            new=self.prefix+'_renamed'
            _claim_history(conn,self.card,self.prefix,new)
            sql(conn,'UPDATE wisp_vouchers SET username=? WHERE id=?',(new,self.card))
            conn.commit()
        finally:conn.close()
        self.assertEqual(self.report()['totals'],before)
        self.assertTrue(delete_account('voucher',self.card)['deleted'])
        self.assertEqual(self.report()['totals'],before)

    def test_native_insert_failure_rolls_back_cleaner(self):
        conn=get_connection()
        try:
            sql(conn,"CREATE TRIGGER traffic_lab_fail BEFORE INSERT ON wisp_traffic_history FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Synthetic retention failure'")
            conn.commit()
            result=prune_historical_logs(90,prune_postauth=False)
            self.assertFalse(result['success'])
            self.assertIsNotNone(run('SELECT radacctid FROM radacct WHERE radacctid=?',(self.session,),'one'))
        finally:
            sql(conn,'DROP TRIGGER IF EXISTS traffic_lab_fail')
            conn.commit();conn.close()

    def test_repeated_schema_install_retains_rows_and_bootstrap_matches(self):
        from database.traffic_history_schema import TABLE_SQL
        self.preserve()
        before=run('SELECT * FROM wisp_traffic_history WHERE username=?',(self.prefix,),'all')
        conn=get_connection()
        try:
            install_traffic_history_schema(conn)
            install_traffic_history_schema(conn)
            conn.commit()
        finally:conn.close()
        self.assertEqual(run('SELECT * FROM wisp_traffic_history WHERE username=?',(self.prefix,),'all'),before)
        self.assertIn(TABLE_SQL,Path('/source/database/schema_mysql.sql').read_text())

    def test_old_backup_history_clear_is_transactional(self):
        from database.traffic_history_schema import clear_legacy_traffic_history
        self.preserve()
        before=run('SELECT COUNT(*) AS n FROM wisp_traffic_history',fetch='one')['n']
        conn=get_connection()
        try:
            clear_legacy_traffic_history(conn)
            self.assertEqual(sql(conn,'SELECT COUNT(*) AS n FROM wisp_traffic_history',fetch='one')['n'],0)
            conn.rollback()
        finally:conn.close()
        self.assertEqual(run('SELECT COUNT(*) AS n FROM wisp_traffic_history',fetch='one')['n'],before)

    def test_anonymous_session_is_not_duplicated_by_archive_or_cleanup(self):
        run("UPDATE radacct SET acctuniqueid='',acctsessionid='',acctstarttime=NULL WHERE radacctid=?",(self.session,))
        before=self.report(period='all')['totals']
        success,message=_archive_old_sessions_locked(90,100)
        self.assertTrue(success,message)
        self.assertIsNotNone(run('SELECT radacctid FROM radacct WHERE radacctid=?',(self.session,),'one'))
        self.assertEqual(self.report(period='all')['totals'],before)
        self.assertTrue(prune_historical_logs(90,prune_postauth=False)['success'])
        self.assertEqual(self.report(period='all')['totals'],before)

    def test_archive_null_counter_replay_does_not_invent_zero(self):
        from services.traffic_history_service import FIELDS
        run('UPDATE radacct SET acctinputoctets=NULL WHERE radacctid=?',(self.session,))
        copied=tuple(f for f in FIELDS if f!='origin_archived_at')
        columns=','.join(copied)
        values=','.join("''" if f=='groupname' else f for f in copied)
        run(f'INSERT INTO radacct_archive({columns}) SELECT {values} FROM radacct WHERE radacctid=?',(self.session,))
        before=self.signature()
        success,message=_archive_old_sessions_locked(90,100)
        self.assertTrue(success,message)
        self.assertIsNone(run('SELECT acctinputoctets FROM wisp_traffic_history WHERE username=?',(self.prefix,),'one')['acctinputoctets'])
        self.assertEqual(self.signature(),before)

    def test_reused_username_does_not_transfer_historical_owner(self):
        before=self.signature(99123)
        self.assertTrue(delete_account('voucher',self.card)['deleted'])
        run('''INSERT INTO wisp_vouchers(batch_id,package_id,reseller_id,serial_number,username,password,pin_code,status)
            VALUES(?,?,99124,?,?,?,?,'expired')''',(self.batch,self.pkg,self.prefix+'_new',self.prefix,'synthetic',self.prefix+'_new'))
        self.assertEqual(self.signature(99123),before)
        foreign=self.report(99124,server=server_key('192.0.2.51','hotspot',self.prefix))
        self.assertEqual(foreign['totals']['sessions'],0)

    def test_replacement_capture_rolls_back_with_import_transaction(self):
        from services.traffic_history_service import preserve_before_replacement
        before=run('SELECT COUNT(*) AS n FROM wisp_traffic_history',fetch='one')['n']
        conn=get_connection()
        try:
            preserve_before_replacement(conn)
            self.assertIsNotNone(sql(conn,'SELECT session_key FROM wisp_traffic_history WHERE username=?',(self.prefix,),'one'))
            conn.rollback()
        finally:conn.close()
        self.assertEqual(run('SELECT COUNT(*) AS n FROM wisp_traffic_history',fetch='one')['n'],before)


if __name__=='__main__':
    result=unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromTestCase(Retention))
    Path('/evidence/TRAFFIC-RETENTION-LAB.private.json').write_text(json.dumps(dict(success=result.wasSuccessful(),tests=result.testsRun,errors=len(result.errors),failures=len(result.failures),scope='traffic266_history_fixture')))
    sys.exit(0 if result.wasSuccessful() else 1)

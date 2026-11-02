"""Native destructive regression cases; only a new, explicitly isolated fixture."""
import os,sys,runpy,json,datetime as dt,unittest
from pathlib import Path
from unittest.mock import patch

assert sys.argv[1:]==['--run'] and os.environ.get('DB_NAME')=='traffic266_history_fixture'
ns=runpy.run_path('/source/tests/run_traffic_retention_lab.py',run_name='retention_fixture_helpers')
Base=ns['Retention'];run=ns['run'];get_connection=ns['get_connection'];sql=ns['sql']
from services.account_lifecycle_service import delete_account,expiry_reason,HISTORY_ELIGIBLE
from services.traffic_history_service import preserve_sessions,compact_duplicate_history,FIELDS
from services.accounting_archiver_service import archive_old_sessions,get_archiver_status
from services.session_history_service import history_sql


class Fixes(unittest.TestCase):
    setUp=Base.setUp
    report=Base.report
    signature=Base.signature
    preserve=Base.preserve

    def test_a_restore_installer_upgrades_legacy_archive_and_view(self):
        from database.traffic_history_schema import install_traffic_history_schema
        from database.subscriber_edit_schema import install_subscriber_edit_schema
        # Explicitly isolated fixture only; do not discard another case's payload.
        self.assertEqual(run('SELECT COUNT(*) n FROM radacct_archive',fetch='one')['n'],0)
        fields=('framedipv6address','framedipv6prefix','framedinterfaceid','delegatedipv6prefix')
        with get_connection() as conn:
            sql(conn,'DROP VIEW radacct_archive_owned')
            for name in fields:sql(conn,f'ALTER TABLE radacct_archive DROP COLUMN {name}')
            install_subscriber_edit_schema(conn)
            try:
                install_traffic_history_schema(conn)
                install_traffic_history_schema(conn)
                columns=sql(conn,'SHOW COLUMNS FROM radacct_archive_owned',fetch='all')
                self.assertTrue(set(fields)<={row['Field'] for row in columns})
                statement,params=history_sql(self.prefix,None)
                self.assertEqual(len(sql(conn,statement,params,'all')),1)
            finally:
                install_traffic_history_schema(conn)
                conn.commit()

    def history(self,name=None):
        statement,values=history_sql(name or self.prefix,None)
        return run(statement,values,'all')

    def rich(self):
        run("""UPDATE radacct SET acctsessiontime=3600,acctinputoctets=4294967596,
            acctoutputoctets=8589935192,framedipaddress='198.51.100.23',
            callingstationid='02:00:00:00:00:23',framedipv6address='2001:db8::23',
            framedipv6prefix='2001:db8:1::/64',framedinterfaceid='0000:0000:0000:0023',
            delegatedipv6prefix='2001:db8:2::/64',class='preserved-class',
            acctinputgigawords=1,acctoutputgigawords=2,realm='fixture',
            nasporttype='Ethernet',acctinterval=60,acctauthentic='RADIUS',
            connectinfo_start='start',connectinfo_stop='stop' WHERE radacctid=?""",(self.session,))
        return run('SELECT * FROM radacct WHERE radacctid=?',(self.session,),'one')

    def test_delete_preserves_full_payload_and_only_one_old_copy(self):
        before=self.rich();report=self.signature(99123)
        sale=run('SELECT * FROM wisp_voucher_sales WHERE id=?',(self.sale,),'one')
        self.assertTrue(delete_account('voucher',self.card)['deleted'])
        self.assertIsNone(run('SELECT radacctid FROM radacct WHERE radacctid=?',(self.session,),'one'))
        saved=run('SELECT * FROM wisp_traffic_history WHERE username=?',(self.prefix,),'one')
        for field in FIELDS:
            if field not in ('groupname','origin_archived_at'):self.assertEqual(saved[field],before[field],field)
        displayed=self.history()[0]
        for field in ('acctsessiontime','framedipaddress','callingstationid','framedipv6address','delegatedipv6prefix'):
            self.assertEqual(displayed[field],before[field],field)
        self.assertEqual(self.signature(99123),report)
        self.assertEqual(run('SELECT * FROM wisp_voucher_sales WHERE id=?',(self.sale,),'one'),sale)

    def test_renamed_current_cycle_quota_survives_prune_archive_and_weekly(self):
        from services.voucher_edit_service import _claim_history
        from services.db_maintenance_service import prune_historical_logs
        import core.watchdog as watchdog
        self.rich()
        run("UPDATE wisp_vouchers SET status='active',last_renewed_at='2001-01-01',expires_at='2099-01-01',snap_volume_quota_mb=100 WHERE id=?",(self.card,))
        new=self.prefix+'_new'
        conn=get_connection()
        try:
            _claim_history(conn,self.card,self.prefix,new)
            sql(conn,'UPDATE wisp_vouchers SET username=? WHERE id=?',(new,self.card));conn.commit()
        finally:conn.close()
        def decision():
            row=run('SELECT *,NOW() checked_at FROM wisp_vouchers WHERE id=?',(self.card,),'one')
            with get_connection() as conn:return expiry_reason(conn,'voucher',row)
        before=run('SELECT * FROM radacct WHERE radacctid=?',(self.session,),'one')
        self.assertTrue(decision())
        self.assertTrue(prune_historical_logs(90,prune_postauth=False)['success'])
        self.assertTrue(archive_old_sessions(90,100)[0])
        run("INSERT INTO wisp_system_settings (`key`,value) VALUES ('history_cleanup_last_success',0) ON DUPLICATE KEY UPDATE value=0")
        watchdog.check_auto_database_cleanup()
        self.assertEqual(run('SELECT * FROM radacct WHERE radacctid=?',(self.session,),'one'),before)
        self.assertTrue(decision())
        self.assertEqual(run('SELECT COUNT(*) n FROM radacct a WHERE a.radacctid=? AND '+HISTORY_ELIGIBLE,(self.session,90),'one')['n'],0)

    def test_legacy_duplicate_backfills_then_compacts_and_repeats(self):
        original=self.rich();self.preserve()
        run('UPDATE wisp_traffic_history SET acctsessiontime=NULL,framedipaddress=NULL,callingstationid=NULL,framedipv6address=NULL,payload_version=0 WHERE username=?',(self.prefix,))
        before=self.signature(99123)
        result=compact_duplicate_history(max_pages=10000,page_size=2)
        self.assertFalse(result['pending']);self.assertGreaterEqual(result['removed'],1)
        self.assertIsNone(run('SELECT radacctid FROM radacct WHERE radacctid=?',(self.session,),'one'))
        saved=self.history()[0]
        for field in ('acctsessiontime','framedipaddress','callingstationid','framedipv6address'):
            self.assertEqual(saved[field],original[field])
        self.assertEqual(self.signature(99123),before)
        self.assertEqual(compact_duplicate_history(max_pages=10000)['removed'],0)

    def test_legacy_compaction_native_delete_failure_is_atomic(self):
        self.rich();self.preserve()
        run('UPDATE wisp_traffic_history SET framedipaddress=NULL,payload_version=0 WHERE username=?',(self.prefix,))
        run("CREATE TRIGGER retention_fix_failure BEFORE DELETE ON radacct FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Synthetic transfer failure'")
        try:
            with self.assertRaises(Exception):compact_duplicate_history(max_pages=10000)
            self.assertIsNotNone(run('SELECT radacctid FROM radacct WHERE radacctid=?',(self.session,),'one'))
            saved=run('SELECT framedipaddress,payload_version FROM wisp_traffic_history WHERE username=?',(self.prefix,),'one')
            self.assertIsNone(saved['framedipaddress']);self.assertEqual(saved['payload_version'],0)
        finally:run('DROP TRIGGER retention_fix_failure')
        self.assertGreaterEqual(compact_duplicate_history(max_pages=10000)['removed'],1)

    def test_full_metadata_conflict_blocks_compaction(self):
        self.rich();self.preserve()
        run("UPDATE wisp_traffic_history SET framedipaddress='198.51.100.99' WHERE username=?",(self.prefix,))
        try:
            with self.assertRaises(RuntimeError):compact_duplicate_history(max_pages=10000)
            self.assertIsNotNone(run('SELECT radacctid FROM radacct WHERE radacctid=?',(self.session,),'one'))
        finally:run("UPDATE wisp_traffic_history SET framedipaddress='198.51.100.23' WHERE username=?",(self.prefix,))

    def test_explicit_owner_conflict_blocks_compaction(self):
        self.rich();self.preserve()
        run('UPDATE radacct SET account_voucher_id=? WHERE radacctid=?',(self.card,self.session))
        run('UPDATE wisp_traffic_history SET account_voucher_id=? WHERE username=?',(self.card+100000,self.prefix))
        try:
            with self.assertRaises(RuntimeError):compact_duplicate_history(max_pages=10000)
            self.assertIsNotNone(run('SELECT radacctid FROM radacct WHERE radacctid=?',(self.session,),'one'))
        finally:run('UPDATE wisp_traffic_history SET account_voucher_id=? WHERE username=?',(self.card,self.prefix))

    def test_archive_uses_one_complete_payload_and_eligible_counts(self):
        original=self.rich()
        expected=run('SELECT COUNT(*) n FROM radacct a WHERE '+HISTORY_ELIGIBLE,(90,),'one')['n']
        self.assertEqual(get_archiver_status()['older_90d_count'],expected)
        self.assertTrue(archive_old_sessions(90,100)[0])
        self.assertIsNone(run('SELECT radacctid FROM radacct_archive WHERE radacctid=? AND acctuniqueid=?',(self.session,self.prefix),'one'))
        saved=run('SELECT * FROM wisp_traffic_history WHERE username=?',(self.prefix,),'one')
        for field in ('acctsessiontime','framedipaddress','callingstationid','class','framedipv6address','framedipv6prefix','framedinterfaceid','delegatedipv6prefix'):
            self.assertEqual(saved[field],original[field],field)

    def test_old_archive_is_folded_without_losing_archive_timestamp(self):
        self.rich();self.preserve()
        fields=tuple(f for f in FIELDS if f!='origin_archived_at')
        columns=','.join(fields);values=','.join("''" if f=='groupname' else f for f in fields)
        run(f'INSERT INTO radacct_archive({columns}) SELECT {values} FROM radacct WHERE radacctid=?',(self.session,))
        run("UPDATE radacct_archive SET archived_at='2002-01-01' WHERE radacctid=? AND acctuniqueid=?",(self.session,self.prefix))
        compact_duplicate_history(max_pages=10000)
        self.assertIsNone(run('SELECT radacctid FROM radacct_archive WHERE radacctid=? AND acctuniqueid=?',(self.session,self.prefix),'one'))
        saved=run('SELECT origin_archived_at FROM wisp_traffic_history WHERE username=?',(self.prefix,),'one')
        self.assertEqual(str(saved['origin_archived_at']),'2002-01-01 00:00:00')

    def test_final_sampler_delta_captured_and_recent_source_has_grace(self):
        from services.traffic_sample_service import capture_rows
        now=run('SELECT NOW() n',fetch='one')['n']
        run('UPDATE radacct SET acctstarttime=?,acctupdatetime=?,acctstoptime=NULL,acctoutputoctets=1000,acctinputoctets=100 WHERE radacctid=?',(now-dt.timedelta(minutes=3),now-dt.timedelta(minutes=1),self.session))
        row=run('SELECT * FROM radacct WHERE radacctid=?',(self.session,),'one')
        row.update(reseller_id=99123,owner_account_exists=True)
        with get_connection() as conn:capture_rows(conn,[row]);conn.commit()
        run("UPDATE radacct SET acctupdatetime=?,acctstoptime=?,acctoutputoctets=1300,acctinputoctets=130 WHERE radacctid=?",(now,now,self.session))
        self.assertTrue(delete_account('voucher',self.card)['deleted'])
        self.assertIsNotNone(run('SELECT radacctid FROM radacct WHERE radacctid=?',(self.session,),'one'))
        amount=run('SELECT SUM(download_bytes) n FROM wisp_traffic_rollups WHERE server_name=?',(self.prefix,),'one')['n']
        self.assertEqual(int(amount),300)
        compact_duplicate_history(max_pages=10000)
        self.assertEqual(int(run('SELECT SUM(download_bytes) n FROM wisp_traffic_rollups WHERE server_name=?',(self.prefix,),'one')['n']),300)

    def test_bounded_compaction_resumes_all_sources(self):
        self.rich();self.preserve()
        for _ in range(200):
            result=compact_duplicate_history(max_pages=1,page_size=2)
            if not result['pending']:break
        else:self.fail('Compaction did not finish the bounded cursor cycle')
        self.assertIsNone(run('SELECT radacctid FROM radacct WHERE radacctid=?',(self.session,),'one'))

    def test_pending_deletion_reasons_do_not_claim_all_cards_deleted(self):
        from services.db_maintenance_service import delete_expired_vouchers
        run('UPDATE radacct SET acctstoptime=NULL WHERE radacctid=?',(self.session,))
        with patch('services.coa_queue_service.enqueue_disconnect',return_value=(True,'fixture queued')):
            result=delete_expired_vouchers()
        self.assertTrue(result['success'],result.get('message'))
        self.assertGreaterEqual(result['pending'],1)
        self.assertGreaterEqual(result['pending_reasons'].get('open_session',0),1)
        self.assertFalse(result['deletion_complete'])
        self.assertIsNotNone(run('SELECT id FROM wisp_vouchers WHERE id=?',(self.card,),'one'))


if __name__=='__main__':
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Fixes))
    Path('/evidence/RETENTION-FIX-LAB.private.json').write_text(json.dumps(dict(success=result.wasSuccessful(),tests=result.testsRun,errors=len(result.errors),failures=len(result.failures))))
    sys.exit(0 if result.wasSuccessful() else 1)

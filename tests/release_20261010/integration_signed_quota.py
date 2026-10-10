import os,sys,uuid,unittest,datetime
from unittest.mock import patch
import pymysql
sys.path.insert(0,'/app')
import services
# Exercise the installed service code.
from database import db
from core import config

TEST='test_max_online_'+uuid.uuid4().hex[:10]
PASSWORD=os.environ['MAX_TEST_ROOT_PASSWORD']
for m in (db,config):
    m.DB_USER='root';m.DB_PASSWORD=PASSWORD;m.DB_NAME=TEST

from services import quota_service as q,quick_action_service as quick,user_portal_service as portal
from services import online_policy_service as online,loyalty_rewards_service as loyalty
from services import voucher_service as vouchers
from services import nas_session_settlement as nas_settlement

MB=1048576
class PreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin=pymysql.connect(host=config.DB_HOST,port=int(config.DB_PORT),user='root',password=PASSWORD,autocommit=True)
        with cls.admin.cursor() as c:
            c.execute('CREATE DATABASE `'+TEST+'`')
            c.execute('SHOW TABLES FROM radius_max');tables=[r[0] for r in c.fetchall()]
            c.execute('USE `'+TEST+'`');c.execute('SET FOREIGN_KEY_CHECKS=0')
            for t in tables:
                c.execute('SHOW CREATE TABLE radius_max.`'+t+'`');c.execute(c.fetchone()[1])
            c.execute("CREATE TABLE IF NOT EXISTS wisp_online_policy_outbox(username VARCHAR(64) PRIMARY KEY,status VARCHAR(16),next_attempt DATETIME,last_error VARCHAR(100),updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
            from pathlib import Path
            sql=Path('/tmp/nas_session_seals.sql').read_text()
            table,trigger=sql.split('DELIMITER //')
            c.execute(table.strip().rstrip(';'))
            c.execute(trigger.split('//\nDELIMITER')[0].strip())
        cls.mocks=[patch.object(online,'checkpoint',side_effect=cls.snapshot),patch.object(online,'preflight'),patch.object(online,'refresh_or_pending',return_value=True),
            patch('services.autoheal_service.docker_api_request',return_value=([{'Id':__import__('os').environ.get('HOSTNAME','test'),'Labels':{'com.docker.compose.project':'max','com.docker.compose.service':'web'}},{'Labels':{'com.docker.compose.project':'max','com.docker.compose.service':'radius_core'},'State':'running','Status':'Up (healthy)'}],None)),
            patch.object(quick,'log_audit'),patch.object(quick,'log_user_audit'),patch.object(portal,'log_audit'),patch.object(portal,'log_user_audit'),patch('services.bot_notifications_service.trigger_recharge_notification')]
        for m in cls.mocks:m.start()
    @classmethod
    def tearDownClass(cls):
        for m in reversed(cls.mocks):m.stop()
        assert TEST.startswith('test_max_online_')
        with cls.admin.cursor() as c:c.execute('DROP DATABASE `'+TEST+'`')
        cls.admin.close()
    @staticmethod
    def snapshot(username):
        rows=db.query_all('SELECT * FROM radacct WHERE username=%s AND acctstoptime IS NULL',(username,))
        return {r['radacctid']:{'row':r,'input':max(int(r['acctinputoctets']),120*MB if r['acctsessionid']=='s1' else 220*MB),
            'output':int(r['acctoutputoctets']),'seconds':max(3600,int(r['acctsessiontime']))} for r in rows}
    def setUp(self):
        with db.db_session() as conn:
            c=conn.cursor();c.execute('SET FOREIGN_KEY_CHECKS=0')
            for t in ('wisp_nas_session_seals','radacct','wisp_session_baselines','wisp_subscribers','wisp_vouchers','wisp_packages','wisp_voucher_batches','wisp_invoices','wisp_voucher_sales','wisp_renewal_guards','wisp_session_reservations','wisp_online_policy_outbox','radcheck','radreply','radusergroup','wisp_system_settings','wisp_loyalty_wallets','wisp_loyalty_rewards','wisp_loyalty_transactions'):
                c.execute('DELETE FROM '+t)
            c.execute("INSERT INTO wisp_packages(id,name,price,volume_quota_mb,validity_value,validity_unit,is_rollover_enabled) VALUES(1,'P1',10,1000,7,'days',1),(2,'P2',20,2000,7,'days',0)")
            c.execute("INSERT INTO wisp_subscribers(id,username,password,full_name,package_id,status,balance,created_at,last_renewed_at,expires_at) VALUES(1,'online-test','p','Test',1,'active',100,NOW()-INTERVAL 2 HOUR,NOW()-INTERVAL 2 HOUR,NOW()+INTERVAL 1 DAY)")
            c.execute("INSERT INTO wisp_voucher_batches(id,batch_number,name,package_id,card_count) VALUES(1,'preview-test','Test',2,1)")
            c.execute("INSERT INTO wisp_vouchers(id,serial_number,username,password,pin_code,package_id,batch_id,status,snap_volume_quota_mb,snap_validity_value,snap_validity_unit) VALUES(1,'preview-card','preview-card','p','CODE',2,1,'unused',2000,7,'days')")
            for i,size in ((1,100),(2,200)):
                c.execute("INSERT INTO radacct(radacctid,acctuniqueid,acctsessionid,username,nasipaddress,framedipaddress,callingstationid,acctstarttime,acctupdatetime,acctsessiontime,acctinputoctets,acctoutputoctets) VALUES(%s,%s,%s,'online-test','10.78.0.8',%s,%s,NOW()-INTERVAL 1 HOUR,NOW(),3500,%s,0)",(i,'u'+str(i),'s'+str(i),'10.1.0.'+str(i),'AA:00:00:00:00:0'+str(i),size*MB))
    def row(self):return db.query_one("SELECT * FROM wisp_subscribers WHERE id=1")
    def renew(self):
        ok,msg=quick.action_renew_package('subscriber',1);self.assertTrue(ok,msg);return self.row()
    def test_admin_renew_multisession_exact_cut(self):
        r=self.renew();self.assertEqual(float(r['extra_quota_mb']),660)
        b=db.query_all('SELECT * FROM wisp_session_baselines ORDER BY radacctid');self.assertEqual([x['baseline_input_bytes']//MB for x in b],[120,220]);self.assertIsNone(db.query_one('SELECT acctstoptime FROM radacct WHERE radacctid=1')['acctstoptime'])
    def test_missing_stop_verified_seal_then_wallet_renewal(self):
        snap=self.snapshot('online-test')
        for s in snap.values():s['evidence_token']='f'*32
        nas_settlement.seal_rows(snap)
        with patch.object(online,'checkpoint',return_value={}):
            ok,msg=portal.renew_or_change_package('online-test',2)
        self.assertTrue(ok,msg);self.assertEqual(float(self.row()['balance']),80)
        row=db.query_one('SELECT * FROM radacct WHERE radacctid=1')
        self.assertIsNotNone(row['acctstoptime']);self.assertEqual(int(row['acctinputoctets']),120*MB)
        renewed=self.row()['last_renewed_at']
        self.assertEqual(q.get_accounting_totals('online-test',renewed)['total_bytes'],0)
        db.execute_write("UPDATE radacct SET acctinputoctets=150*%s,acctsessiontime=9999,acctstoptime=NOW()+INTERVAL 1 MINUTE,acctterminatecause='User-Request' WHERE radacctid=1",(MB,))
        self.assertEqual(q.get_accounting_totals('online-test',renewed)['total_bytes'],0)
        self.assertEqual(db.query_one('SELECT acctstoptime FROM radacct WHERE radacctid=1')['acctstoptime'],row['acctstoptime'])
        db.execute_write("UPDATE radacct SET acctinputoctets=160*%s,acctstoptime=NULL,acctsessiontime=10000 WHERE radacctid=1",(MB,))
        self.assertEqual(q.get_accounting_totals('online-test',renewed)['total_bytes'],0)
        self.assertIsNotNone(db.query_one('SELECT acctstoptime FROM radacct WHERE radacctid=1')['acctstoptime'])
    def test_seal_rejects_db_counters_ahead_of_nas(self):
        snap=self.snapshot('online-test')
        for s in snap.values():s['evidence_token']='a'*32
        db.execute_write('UPDATE radacct SET acctinputoctets=999*%s WHERE radacctid=2',(MB,))
        with self.assertRaises(RuntimeError):nas_settlement.seal_rows(snap)
        self.assertEqual(db.query_one('SELECT COUNT(*) AS n FROM wisp_nas_session_seals')['n'],0)
        self.assertIsNone(db.query_one('SELECT acctstoptime FROM radacct WHERE radacctid=1')['acctstoptime'])
    def test_ppp_final_stop_preserves_teardown_bytes(self):
        snap=self.snapshot('online-test')
        for s in snap.values():s.update(kind='pppoe',evidence_token='b'*32)
        db.execute_write("UPDATE radacct SET acctinputoctets=400*%s,acctoutputoctets=52,acctstoptime=NOW(),acctterminatecause='NAS-Request'",(MB,))
        nas_settlement.seal_rows(snap)
        rows=db.query_all('SELECT * FROM wisp_nas_session_seals ORDER BY radacctid')
        self.assertEqual([(int(r['input_bytes']),int(r['output_bytes'])) for r in rows],[(400*MB,52)]*2)
    def test_ppp_without_final_stop_never_invents_final_counters(self):
        snap=self.snapshot('online-test')
        for s in snap.values():s.update(kind='pppoe',evidence_token='c'*32)
        with self.assertRaises(RuntimeError):nas_settlement.seal_rows(snap)
        self.assertEqual(db.query_one('SELECT COUNT(*) AS n FROM wisp_nas_session_seals')['n'],0)
        self.assertIsNone(db.query_one('SELECT acctstoptime FROM radacct WHERE radacctid=1')['acctstoptime'])
    def test_batch_reassignment_replaces_entire_unused_contract(self):
        db.execute_write("UPDATE wisp_packages SET uptime_limit_mins=600,validity_value=0,validity_days=0,simultaneous_sessions=5 WHERE id=1")
        self.assertTrue(vouchers.update_voucher_batch(1,'Changed',1))
        c=db.query_one('SELECT * FROM wisp_vouchers WHERE id=1')
        self.assertEqual((c['package_id'],int(c['snap_volume_quota_mb']),int(c['snap_uptime_limit_mins']),int(c['snap_validity_value']),int(c['snap_simultaneous_sessions'])),(1,1000,600,0,5))
        self.assertEqual(c['status'],'unused');self.assertIsNone(c['first_used_at'])
        self.assertEqual(db.query_one("SELECT groupname FROM radusergroup WHERE username='preview-card'")['groupname'],'P1')
        b=db.query_one('SELECT * FROM wisp_voucher_batches WHERE id=1');self.assertEqual(int(b['volume_quota_mb']),1000)
    def test_package_definition_edit_preserves_printed_card(self):
        db.execute_write('UPDATE wisp_packages SET volume_quota_mb=9999 WHERE id=2')
        vouchers.update_voucher_batch(1,'Rename',2)
        self.assertEqual(int(db.query_one('SELECT snap_volume_quota_mb FROM wisp_vouchers WHERE id=1')['snap_volume_quota_mb']),2000)
    def test_batch_used_card_rejected_without_partial_update(self):
        db.execute_write("UPDATE wisp_vouchers SET status='active',first_used_at=NOW() WHERE id=1")
        with self.assertRaises(ValueError): vouchers.update_voucher_batch(1,'Forbidden',1)
        self.assertEqual(db.query_one('SELECT package_id FROM wisp_vouchers WHERE id=1')['package_id'],2)
        self.assertEqual(db.query_one('SELECT name FROM wisp_voucher_batches WHERE id=1')['name'],'Test')
    def test_batch_snapshot_and_group_failure_roll_back(self):
        original = db.adapt_query
        def late_failure(sql,conn):
            if sql.startswith('INSERT INTO radusergroup'):
                return 'INVALID ISOLATED SQL'
            return original(sql,conn)
        with patch.object(db,'adapt_query',side_effect=late_failure):
            with self.assertRaises(Exception): vouchers.update_voucher_batch(1,'Rollback',1)
        self.assertEqual(db.query_one('SELECT package_id FROM wisp_vouchers WHERE id=1')['package_id'],2)
        self.assertEqual(db.query_one('SELECT package_id FROM wisp_voucher_batches WHERE id=1')['package_id'],2)
        self.assertEqual(int(db.query_one('SELECT snap_volume_quota_mb FROM wisp_vouchers WHERE id=1')['snap_volume_quota_mb']),2000)
    def test_late_interim_below_cut_not_charged(self):
        r=self.renew();db.execute_write('UPDATE radacct SET acctinputoctets=110*%s WHERE radacctid=1',(MB,));self.assertEqual(q.get_accounting_totals('online-test',r['last_renewed_at'])['total_bytes'],0)
    def test_new_traffic_only_charged_after_cut(self):
        r=self.renew();db.execute_write('UPDATE radacct SET acctinputoctets=150*%s WHERE radacctid=1',(MB,));db.execute_write('UPDATE radacct SET acctinputoctets=270*%s WHERE radacctid=2',(MB,));self.assertEqual(q.get_accounting_totals('online-test',r['last_renewed_at'])['total_bytes'],80*MB)
    def test_late_stop_preserves_other_session(self):
        r=self.renew();db.execute_write("UPDATE radacct SET acctinputoctets=150*%s,acctstoptime=NOW(),acctterminatecause='User-Request' WHERE radacctid=1",(MB,));self.assertEqual(q.get_accounting_totals('online-test',r['last_renewed_at'])['total_bytes'],30*MB);self.assertIsNone(db.query_one('SELECT acctstoptime FROM radacct WHERE radacctid=2')['acctstoptime'])
    def test_live_overlay_below_api_baseline_no_double_count(self):
        r=self.renew()
        with online.view(self.snapshot('online-test')):self.assertEqual(q.get_accounting_totals('online-test',r['last_renewed_at'])['total_bytes'],0)
    def test_portal_renew_wallet_charged_once(self):
        ok,msg=portal.renew_or_change_package('online-test',2);self.assertTrue(ok,msg);self.assertEqual(float(self.row()['balance']),80)
    def test_admin_package_change_preserves_sessions(self):
        ok,msg=quick.action_change_package('subscriber',1,2);self.assertTrue(ok,msg);self.assertEqual(self.row()['package_id'],2);self.assertEqual(len(online.rows_for('online-test')),2)
    def test_recharge_consumes_card_once(self):
        ok,msg=portal.recharge_user_wallet_by_card('online-test','CODE','package');self.assertTrue(ok,msg);self.assertEqual(db.query_one('SELECT status FROM wisp_vouchers WHERE id=1')['status'],'recharged')
        ok,msg=portal.recharge_user_wallet_by_card('online-test','CODE','package');self.assertFalse(ok)
    def test_coa_preflight_failure_no_billing(self):
        with patch.object(online,'preflight',side_effect=lambda x:online.fail()):
            ok,msg=portal.renew_or_change_package('online-test',2)
        self.assertFalse(ok);self.assertEqual(float(self.row()['balance']),100);self.assertEqual(self.row()['package_id'],1)
    def test_nas_checkpoint_failure_no_billing(self):
        with patch.object(online,'checkpoint',side_effect=lambda *a:online.fail()):ok,msg=portal.renew_or_change_package('online-test',2)
        self.assertFalse(ok);self.assertEqual(float(self.row()['balance']),100)
    def test_stopped_core_no_billing(self):
        with patch('services.autoheal_service.docker_api_request',return_value=([],None)):ok,msg=portal.renew_or_change_package('online-test',2)
        self.assertFalse(ok);self.assertEqual(float(self.row()['balance']),100)
    def test_suspended_self_service_rejected(self):
        db.execute_write("UPDATE wisp_subscribers SET status='suspended' WHERE id=1");ok,msg=portal.renew_or_change_package('online-test',2);self.assertFalse(ok);self.assertEqual(self.row()['status'],'suspended')
    def test_same_second_repeat_no_duplicate_invoice(self):
        db.execute_write('UPDATE wisp_subscribers SET last_renewed_at=NOW()+INTERVAL 1 SECOND WHERE id=1');ok,msg=quick.action_renew_package('subscriber',1);self.assertFalse(ok);self.assertEqual(db.query_one('SELECT COUNT(*) AS n FROM wisp_invoices')['n'],0)
    def test_add_quota_no_disconnect(self):
        with patch.object(quick,'action_disconnect_user') as kick:
            ok,msg=quick.action_add_quota('subscriber',1,100,'MB');self.assertTrue(ok,msg);kick.assert_not_called()
    def test_loan_no_disconnect(self):
        db.execute_write('UPDATE wisp_subscribers SET expires_at=NOW()-INTERVAL 1 HOUR WHERE id=1')
        with patch.object(portal,'disconnect_subscriber_session') as kick:
            ok,msg=portal.request_data_loan('online-test');self.assertTrue(ok,msg);kick.assert_not_called()
        self.assertEqual(int(self.row()['loan_status']),1)
    def test_transaction_failure_restores_wallet_and_cut(self):
        with patch.object(portal,'_sync_recharge_radius',side_effect=RuntimeError('test rollback')):
            try:portal.renew_or_change_package('online-test',2)
            except RuntimeError:pass
        self.assertEqual(float(self.row()['balance']),100);self.assertEqual(db.query_one('SELECT COUNT(*) AS n FROM wisp_session_baselines')['n'],0)

    def prepare_reward(self,kind):
        db.execute_write("INSERT INTO wisp_system_settings(`key`,`value`) VALUES('portal_enable_loyalty','1')")
        db.execute_write("INSERT INTO wisp_loyalty_wallets(username,points_balance) VALUES('online-test',1000)")
        db.execute_write("INSERT INTO wisp_loyalty_rewards(id,reward_name,points_cost,reward_type,reward_value,is_active) VALUES(1,'Test reward',100,%s,10,1)",(kind,))
    def test_data_reward_without_disconnect(self):
        self.prepare_reward('data_bonus_mb')
        with patch.object(quick,'action_disconnect_user') as kick:
            ok,msg=loyalty.redeem_reward('online-test',1);self.assertTrue(ok,msg);kick.assert_not_called()
        self.assertEqual(float(self.row()['extra_quota_mb']),10)
    def test_days_reward_without_disconnect(self):
        self.prepare_reward('validity_days');before=self.row()['expires_at']
        with patch.object(quick,'action_disconnect_user') as kick:
            ok,msg=loyalty.redeem_reward('online-test',1);self.assertTrue(ok,msg);kick.assert_not_called()
        self.assertEqual((self.row()['expires_at']-before).days,10)
    def test_money_reward_does_not_require_nas(self):
        self.prepare_reward('balance_credit')
        with patch.object(online,'checkpoint',side_effect=RuntimeError('NAS unavailable')),patch.object(quick,'action_disconnect_user') as kick:
            ok,msg=loyalty.redeem_reward('online-test',1);self.assertTrue(ok,msg);kick.assert_not_called()
        self.assertEqual(float(self.row()['balance']),110)
    def test_consumed_card_not_revived_by_reward(self):
        self.prepare_reward('validity_days');db.execute_write("UPDATE wisp_vouchers SET status='recharged' WHERE id=1")
        ok,msg=loyalty.redeem_reward('preview-card',1);self.assertFalse(ok)
        self.assertEqual(db.query_one('SELECT status FROM wisp_vouchers WHERE id=1')['status'],'recharged')
    def test_retry_persists_without_second_payment(self):
        with patch.object(online,'refresh_or_pending',return_value=False):
            ok,msg=portal.renew_or_change_package('online-test',2)
        self.assertTrue(ok,msg);self.assertEqual(float(self.row()['balance']),80)
        self.assertEqual(db.query_one("SELECT status FROM wisp_online_policy_outbox WHERE username='online-test'")['status'],'pending')

    def active_card(self):
        db.execute_write("UPDATE wisp_vouchers SET status='active',created_at=NOW()-INTERVAL 1 HOUR WHERE id=1")
        db.execute_write("INSERT INTO radacct(radacctid,acctuniqueid,acctsessionid,username,nasipaddress,acctstarttime,acctupdatetime,acctinputoctets,acctoutputoctets,acctsessiontime) VALUES(3,'card-live','card-s1','preview-card','10.78.0.8',NOW()-INTERVAL 1 MINUTE,NOW(),0,0,60)")
        return {'username':'preview-card','password':'p','status':'active','bound_mac':None}
    def test_live_identity_edit_no_mutation(self):
        p=self.active_card();p['username']='renamed-card'
        ok,msg=quick.action_change_package('voucher',1,2,voucher_profile=p);self.assertFalse(ok);self.assertEqual(db.query_one('SELECT username FROM wisp_vouchers WHERE id=1')['username'],'preview-card')
    def test_live_password_edit_no_mutation(self):
        p=self.active_card();p['password']='new-password'
        ok,msg=quick.action_change_package('voucher',1,2,voucher_profile=p);self.assertFalse(ok);self.assertEqual(db.query_one('SELECT password FROM wisp_vouchers WHERE id=1')['password'],'p')
    def test_live_disable_not_processed_as_package_change(self):
        p=self.active_card();p['status']='suspended'
        ok,msg=quick.action_change_package('voucher',1,2,voucher_profile=p);self.assertFalse(ok);self.assertEqual(db.query_one('SELECT status FROM wisp_vouchers WHERE id=1')['status'],'active')

    def test_parallel_requests_only_one_wallet_charge(self):
        import concurrent.futures,threading
        ready=threading.Barrier(2)
        def attempt():
            ready.wait();return portal.renew_or_change_package('online-test',2)[0]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            answers=list(pool.map(lambda _:attempt(),range(2)))
        self.assertEqual(sum(answers),1);self.assertEqual(float(self.row()['balance']),80)
    def test_interim_updates_during_three_renewals(self):
        import concurrent.futures,time,threading
        stop=threading.Event()
        def accounting():
            i=0
            while not stop.is_set():
                i+=1
                db.execute_write('UPDATE radacct SET acctinputoctets=GREATEST(acctinputoctets,%s),acctupdatetime=NOW(),acctsessiontime=GREATEST(acctsessiontime,%s) WHERE radacctid=1',(120*MB+i*1024,3600+i))
                time.sleep(.01)
            return i
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            writer=pool.submit(accounting)
            try:
                for _ in range(3):
                    ok,msg=portal.renew_or_change_package('online-test',2);self.assertTrue(ok,msg);time.sleep(1.05)
            finally:stop.set()
            self.assertGreater(writer.result(),10)
        self.assertEqual(float(self.row()['balance']),40)
        self.assertEqual(len(online.rows_for('online-test')),2)
        self.assertFalse(q.calculate_account_quota('online-test')['accounting_error'])


    def test_signed_subscriber_gb_deduction(self):
        ok,msg=quick.action_add_quota('subscriber',1,-0.5,'GB');self.assertTrue(ok,msg)
        r=self.row();self.assertEqual(int(r['extra_quota_mb']),-512);self.assertEqual(r['status'],'active');self.assertIn('خصم',msg)
        self.assertEqual(db.query_one("SELECT COUNT(*) n FROM radacct WHERE acctstoptime IS NULL")['n'],2)
    def test_signed_card_mb_deduction(self):
        ok,msg=quick.action_add_quota('voucher',1,-100,'MB');self.assertTrue(ok,msg)
        r=db.query_one('SELECT * FROM wisp_vouchers WHERE id=1');self.assertEqual(int(r['extra_quota_mb']),-100);self.assertEqual(r['status'],'unused')
    def test_deduction_keeps_expired_reason_and_credentials(self):
        db.execute_write("UPDATE wisp_vouchers SET status='expired',expire_reason='original' WHERE id=1")
        db.execute_write("INSERT INTO radcheck(username,attribute,op,value) VALUES('preview-card','Auth-Type',':=','Reject')")
        ok,msg=quick.action_add_quota('voucher',1,-1,'GB');self.assertTrue(ok,msg)
        r=db.query_one('SELECT * FROM wisp_vouchers WHERE id=1');self.assertEqual(r['status'],'expired');self.assertEqual(r['expire_reason'],'original')
        self.assertEqual(db.query_one("SELECT COUNT(*) n FROM radcheck WHERE username='preview-card' AND attribute='Cleartext-Password'")['n'],0)
        self.assertEqual(db.query_one("SELECT COUNT(*) n FROM radcheck WHERE username='preview-card' AND attribute='Auth-Type'")['n'],1)
    def test_deduction_keeps_suspended_state(self):
        db.execute_write("UPDATE wisp_subscribers SET status='suspended' WHERE id=1")
        ok,msg=quick.action_add_quota('subscriber',1,-10,'MB');self.assertTrue(ok,msg);self.assertEqual(self.row()['status'],'suspended')
    def test_deduction_to_zero_does_not_make_unlimited(self):
        ok,msg=quick.action_add_quota('subscriber',1,-1000,'MB');self.assertTrue(ok,msg)
        r=q.calculate_account_quota('online-test');self.assertFalse(r['is_quota_unlimited']);self.assertEqual(r['remaining_bytes'],0);self.assertTrue(r['is_exhausted'])
    def test_invalid_adjustments_before_nas_preflight(self):
        with patch.object(online,'checkpoint',side_effect=AssertionError('Must validate first')):
            for value,unit in [(0,'MB'),('NaN','MB'),('inf','GB'),('-inf','GB'),('bad','MB'),(1,'bad'),(1e30,'GB')]:
                ok,msg=quick.action_add_quota('subscriber',1,value,unit);self.assertFalse(ok,msg)
        self.assertEqual(int(self.row()['extra_quota_mb'] or 0),0)
    def test_positive_topup_still_works(self):
        ok,msg=quick.action_add_quota('subscriber',1,0.25,'GB');self.assertTrue(ok,msg);self.assertEqual(int(self.row()['extra_quota_mb']),256)
    def test_quota_and_password_failure_roll_back(self):
        original=quick.adapt_query
        def broken(sql,conn):
            return 'INVALID ISOLATED SQL' if sql.startswith('INSERT INTO radcheck') else original(sql,conn)
        with patch.object(quick,'adapt_query',side_effect=broken):
            with self.assertRaises(Exception):quick.action_add_quota('subscriber',1,1,'GB')
        self.assertEqual(int(self.row()['extra_quota_mb'] or 0),0)

if __name__=='__main__':unittest.main(verbosity=2)

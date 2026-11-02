"""Explicit synthetic MariaDB voucher identity/cycle acceptance. No customer DBs."""
import os
import sys


def main():
    if sys.argv[1:] != ['--run'] or not os.environ.get('DB_NAME','').startswith('subscriber_edit_fixture_'):
        raise SystemExit('Synthetic subscriber_edit_fixture_ database and --run required')
    import datetime as dt
    import hashlib
    import json
    import threading
    import unittest
    import uuid
    from pathlib import Path
    from unittest.mock import patch
    real_start=threading.Thread.start;threading.Thread.start=lambda self:None
    from database.db import init_database,query_one,query_all,execute_write,get_connection
    init_database()
    from services import voucher_edit_service as edit
    from services.quick_action_service import action_change_package
    from services.quota_service import get_accounting_totals
    from core.radius_sync import sync_voucher_to_radius
    from services.subscriber_service import get_subscriber_sessions,get_user_audit_logs
    from services.renewal_settlement_service import SettlementPending
    matrix=[];packages={}
    for label,quota in [('ten',10240),('small',1024),('open',0)]:
        name='voucher_fixture_'+uuid.uuid4().hex[:10]
        execute_write("INSERT INTO wisp_packages(name,service_type,volume_quota_mb,validity_value,validity_unit,validity_days,price,cost,is_active,simultaneous_sessions) VALUES(%s,'hotspot',%s,30,'days',30,25,7,1,100)",(name,quota))
        packages[label]=query_one('SELECT * FROM wisp_packages WHERE name=%s',(name,))
    batch='voucher_fixture_'+uuid.uuid4().hex[:10]
    execute_write('INSERT INTO wisp_voucher_batches(batch_number,name,package_id,card_count) VALUES(%s,%s,%s,1)',(batch,batch,packages['ten']['id']))
    batch_id=query_one('SELECT id FROM wisp_voucher_batches WHERE batch_number=%s',(batch,))['id']

    def card(label='ten',usage=0,status='active',independent_pin=False):
        name='v_edit_'+uuid.uuid4().hex[:12]
        execute_write("""INSERT INTO wisp_vouchers(batch_id,serial_number,username,password,pin_code,package_id,status,balance,
            snap_volume_quota_mb,snap_validity_value,snap_validity_unit,snap_validity_days,extra_quota_mb,
            created_at,first_used_at,last_renewed_at,expires_at,policy_frozen_at)
            VALUES(%s,%s,%s,'fixture-only',%s,%s,%s,123.45,%s,30,'days',30,0,
            NOW()-INTERVAL 2 DAY,NOW()-INTERVAL 2 DAY,NOW()-INTERVAL 1 DAY,NOW()+INTERVAL 10 DAY,NOW()-INTERVAL 2 DAY)""",
            (batch_id,name,name,'independent-fixture-pin' if independent_pin else 'fixture-only',packages[label]['id'],status,packages[label]['volume_quota_mb']))
        row=query_one('SELECT * FROM wisp_vouchers WHERE username=%s',(name,))
        if status=='unused':execute_write('UPDATE wisp_vouchers SET first_used_at=NULL,last_renewed_at=NULL,expires_at=NULL,policy_frozen_at=NULL WHERE id=%s',(row['id'],))
        assert sync_voucher_to_radius(row['id'])
        if usage:
            execute_write("""INSERT INTO radacct(username,acctsessionid,acctuniqueid,nasipaddress,acctstarttime,acctupdatetime,acctstoptime,
                acctsessiontime,acctinputoctets,acctoutputoctets,acctterminatecause) VALUES(%s,%s,%s,'192.0.2.1',
                NOW()-INTERVAL 2 HOUR,NOW()-INTERVAL 1 HOUR,NOW()-INTERVAL 1 HOUR,3600,0,%s,'User-Request')""",
                (name,name,hashlib.md5(name.encode()).hexdigest(),usage))
        return current(row)

    def current(row):return query_one('SELECT * FROM wisp_vouchers WHERE id=%s',(row['id'],))
    def request(row,**changes):return dict(operation_id=uuid.uuid4().hex,expected_revision=edit.revision(row),**changes)
    def change(row,**changes):
        body=request(row,**changes)
        return edit.edit_voucher(row['id'],body,actor='fixture-admin',can_change_expiry=True),body
    def package(row,target='small',roll=False,op=None,expected=None,profile=None):
        return action_change_package('voucher',row['id'],packages[target]['id'],enable_rollover=roll,admin_username='fixture-admin',
            operation_id=op or uuid.uuid4().hex,expected_revision=expected or edit.revision(row),voucher_profile=profile)
    def sales(row):return query_all('SELECT * FROM wisp_voucher_sales WHERE voucher_id=%s ORDER BY id',(row['id'],))

    class Cases(unittest.TestCase):
        def tearDown(self):self.assertEqual(query_one('SELECT COUNT(*) n FROM wisp_renewal_guards')['n'],0)
        def test_01_repeat_migration_keeps_counters_and_contract(self):
            row=card(usage=4*1048576);before=query_all('SELECT * FROM radacct WHERE username=%s',(row['username'],))
            from database.subscriber_edit_schema import install_subscriber_edit_schema
            with get_connection() as c:install_subscriber_edit_schema(c)
            self.assertEqual(current(row),row);self.assertEqual(before,query_all('SELECT * FROM radacct WHERE username=%s',(row['username'],)))
        def test_02_rename_preserves_uid_counters_cycle_money_and_sales(self):
            row=card(usage=4*1048576);package(row,'ten',op=uuid.uuid4().hex)
            row=current(row);old_sales=sales(row);physical=query_one('SELECT * FROM radacct WHERE username=%s',(row['username'],))
            result,_=change(row,username=row['username']+'_new');self.assertTrue(result['success']);new=current(row)
            for k in ('id','balance','first_used_at','last_renewed_at','expires_at','package_id','snap_volume_quota_mb','policy_frozen_at','extra_quota_mb'):
                self.assertEqual(new[k],row[k],k)
            self.assertEqual(sales(row),old_sales)
            after=query_one('SELECT * FROM radacct WHERE radacctid=%s',(physical['radacctid'],))
            for k in ('username','acctuniqueid','acctstarttime','acctstoptime','acctinputoctets','acctoutputoctets'):self.assertEqual(after[k],physical[k])
            self.assertEqual(after['account_voucher_id'],row['id'])
            self.assertEqual(get_accounting_totals(new['username'])['total_bytes'],4*1048576)
            self.assertEqual(query_one('SELECT original_username FROM radacct_archive_owned WHERE username=%s AND radacctid=%s',(new['username'],original['radacctid']))['original_username'],row['username'])
        def test_03_repeated_rename_and_return(self):
            row=card(usage=1048576);original=row['username']
            for name in (original+'_a',original+'_b',original):
                change(row,username=name);row=current(row);self.assertEqual(get_accounting_totals(name)['total_bytes'],1048576)
        def test_04_reserved_names_block_cards_and_subscribers(self):
            row=card();other=card();old=row['username'];change(row,username=old+'_new')
            for name in (old,old+'_new'):
                with self.assertRaises(ValueError):change(other,username=name)
                with self.assertRaises(Exception):execute_write("INSERT INTO wisp_subscribers(username,password,full_name,package_id,status) VALUES(%s,'fixture','Synthetic',%s,'active')",(name,packages['ten']['id']))
        def test_05_invalid_and_stale_requests_have_no_effect(self):
            row=card()
            for changes in (dict(username='bad name'),dict(bound_mac='invalid'),dict(expires_at='wrong'),dict(password=''),dict(status='unused')):
                with self.assertRaises(ValueError):change(row,**changes)
            with self.assertRaises(ValueError):edit.edit_voucher(row['id'],dict(request(row),expected_revision='wrong'))
            self.assertEqual(current(row),row)
        def test_06_expiry_extend_reduce_unlimited_keeps_usage_cycle_and_money(self):
            row=card(usage=3*1048576)
            for expiry in ((dt.datetime.now()+dt.timedelta(days=50)).isoformat(),'2001-01-01T00:00:00',''):
                change(row,expires_at=expiry);new=current(row)
                self.assertEqual(new['last_renewed_at'],row['last_renewed_at']);self.assertEqual(new['balance'],row['balance'])
                self.assertEqual(get_accounting_totals(new['username'])['total_bytes'],3*1048576);row=new
            suspended=card(status='suspended');change(suspended,expires_at='2090-01-01T00:00:00');self.assertEqual(current(suspended)['status'],'suspended')
        def test_07_password_and_pin_do_not_reset_accounting(self):
            for independent in (False,True):
                row=card(usage=1048576,independent_pin=independent);change(row,password='updated-fixture');new=current(row)
                self.assertEqual(new['pin_code'],'independent-fixture-pin' if independent else 'updated-fixture')
                self.assertEqual(new['last_renewed_at'],row['last_renewed_at']);self.assertEqual(get_accounting_totals(new['username'])['total_bytes'],1048576)
        def test_08_profile_sync_failure_rolls_back_names_history_and_receipt(self):
            row=card(usage=1048576);body=request(row,username=row['username']+'_fail')
            with patch('core.radius_sync.sync_voucher_to_radius',return_value=False):
                with self.assertRaises(ValueError):edit.edit_voucher(row['id'],body,actor='fixture-admin')
            self.assertEqual(current(row),row);self.assertIsNone(query_one('SELECT username FROM wisp_voucher_names WHERE voucher_id=%s',(row['id'],)))
            self.assertIsNone(query_one('SELECT operation_id FROM wisp_voucher_edit_receipts WHERE operation_id=%s',(body['operation_id'],)))
        def test_09_unsettled_identity_cannot_save(self):
            row=card();body=request(row,username=row['username']+'_new')
            with patch('services.nas_session_settlement.freeze_and_disconnect',side_effect=SettlementPending('fixture pending')):
                with self.assertRaises(SettlementPending):edit.edit_voucher(row['id'],body,actor='fixture-admin')
            self.assertEqual(current(row),row)
        def test_10_profile_replay_actor_and_noop(self):
            row=card();result,body=change(row,password='updated-fixture')
            self.assertEqual(edit.edit_voucher(row['id'],body,actor='fixture-admin'),result)
            with self.assertRaises(ValueError):edit.edit_voucher(row['id'],body,actor='other')
            new=current(row);self.assertTrue(change(new)[0]['success'])
            self.assertEqual(len(get_user_audit_logs('voucher',row['id'],new['username'])),1)
        def test_11_package_matrix_preserves_history_and_records_one_sale(self):
            for src,dst in (('ten','small'),('small','ten'),('ten','open'),('open','small')):
                for roll in (False,True):
                    row=card(src,usage=100*1048576);op=uuid.uuid4().hex;ok,msg=package(row,dst,roll,op);self.assertTrue(ok,msg);new=current(row)
                    expected=packages[src]['volume_quota_mb']-100 if roll and src!='open' else 0
                    self.assertEqual(new['snap_volume_quota_mb'],packages[dst]['volume_quota_mb']);self.assertEqual(float(new['extra_quota_mb']),expected)
                    self.assertEqual(new['balance'],row['balance']);self.assertEqual(get_accounting_totals(new['username'],new['last_renewed_at'])['total_bytes'],0)
                    self.assertEqual(get_accounting_totals(new['username'])['total_bytes'],100*1048576)
                    self.assertEqual(len(sales(row)),1);self.assertEqual(float(sales(row)[0]['price']),25)
                    self.assertTrue(package(row,dst,roll,op)[0]);self.assertEqual(new,current(row));self.assertEqual(len(sales(row)),1)
                    self.assertEqual(len(get_user_audit_logs('voucher',row['id'],row['username'])),1)
                    matrix.append(dict(source=src,target=dst,rollover=roll,passed=True))
        def test_12_stale_package_and_combined_rename_are_rejected(self):
            row=card();self.assertFalse(package(row,expected='wrong')[0]);self.assertEqual(current(row),row)
            profile=dict(username=row['username']+'_bypass',password=row['password'],status=row['status'])
            self.assertFalse(package(row,profile=profile)[0]);self.assertEqual(current(row),row);self.assertEqual(sales(row),[])
        def test_13_sale_failure_rolls_back_package_baseline_receipt(self):
            row=card(usage=1048576);op=uuid.uuid4().hex
            execute_write("CREATE TRIGGER voucher_sale_abort BEFORE INSERT ON wisp_voucher_sales FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='synthetic sale failure'")
            try:
                with self.assertRaisesRegex(Exception,'synthetic sale failure'):package(row,op=op)
            finally:execute_write('DROP TRIGGER voucher_sale_abort')
            self.assertEqual(current(row),row);self.assertEqual(sales(row),[])
            self.assertIsNone(query_one('SELECT operation_id FROM wisp_voucher_edit_receipts WHERE operation_id=%s',(op,)))
        def test_14_concurrent_profile_has_one_revision_winner(self):
            from concurrent.futures import ThreadPoolExecutor
            row=card();requests=[request(row,password=p) for p in ('fixture-a','fixture-b')]
            def apply(body):
                try:return edit.edit_voucher(row['id'],body,actor='fixture-admin')['success']
                except ValueError:return False
            with patch.object(threading.Thread,'start',real_start):
                with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(apply,requests))
            self.assertEqual(sorted(results),[False,True])
        def test_15_concurrent_package_same_receipt_one_sale(self):
            from concurrent.futures import ThreadPoolExecutor
            row=card();op=uuid.uuid4().hex
            with patch.object(threading.Thread,'start',real_start):
                with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:package(row,op=op),[0,1]))
            self.assertTrue(any(x[0] for x in results));self.assertEqual(len(sales(row)),1)
        def test_16_late_old_name_insert_and_update_keep_current_owner(self):
            row=card(usage=1048576);change(row,username=row['username']+'_new');new=current(row)
            execute_write("INSERT INTO radacct(username,acctsessionid,acctuniqueid,nasipaddress,acctstarttime,acctstoptime,acctsessiontime,acctinputoctets,acctoutputoctets,acctterminatecause) VALUES(%s,%s,%s,'192.0.2.1',NOW()-INTERVAL 1 HOUR,NOW(),3600,0,1048576,'User-Request')",(row['username'],uuid.uuid4().hex,uuid.uuid4().hex))
            self.assertEqual(get_accounting_totals(new['username'])['total_bytes'],2097152)
            execute_write('UPDATE radacct SET acctoutputoctets=acctoutputoctets+1048576 WHERE username=%s',(row['username'],))
            self.assertEqual(get_accounting_totals(new['username'])['total_bytes'],4194304)
        def test_17_unused_and_recharged_contract_rules(self):
            row=card(status='unused');change(row,username=row['username']+'_new');row=current(row)
            self.assertIsNone(row['first_used_at']);self.assertIsNone(row['expires_at'])
            with self.assertRaises(ValueError):change(row,expires_at='2090-01-01T00:00:00')
            profile=dict(username=row['username'],password=row['password'],bound_mac=row.get('bound_mac'),status='unused')
            self.assertTrue(package(row,profile=profile)[0]);new=current(row)
            self.assertEqual(new['status'],'unused');self.assertIsNone(new['last_renewed_at']);self.assertEqual(sales(row),[])
            spent=card(status='recharged')
            with self.assertRaises(ValueError):change(spent,password='new-fixture')
            self.assertFalse(package(spent)[0])
        def test_18_dump_omits_generated_owner_views(self):
            from services.backup_service import generate_mysql_dump
            text,_=generate_mysql_dump()
            self.assertNotIn('INSERT INTO `radacct_owned`',text);self.assertIn('wisp_voucher_names',text)
            import re
            for columns in re.findall(r'INSERT INTO `radacct`\s*\((.*?)\)',text,re.S):self.assertNotIn('account_read_username',columns)
        def test_19_archive_protects_current_cycle_after_rename(self):
            row=card(usage=1048576)
            execute_write('UPDATE wisp_vouchers SET last_renewed_at=NOW()-INTERVAL 120 DAY WHERE id=%s',(row['id'],))
            execute_write('UPDATE radacct SET acctstarttime=NOW()-INTERVAL 110 DAY,acctstoptime=NOW()-INTERVAL 109 DAY WHERE username=%s',(row['username'],))
            row=current(row);change(row,username=row['username']+'_archive')
            from services.accounting_archiver_service import archive_old_sessions
            with patch('core.license_feature_policy.verified_state',return_value={'valid':True,'features':{}}):
                ok,msg=archive_old_sessions(days_threshold=90)
            self.assertTrue(ok,msg)
            self.assertTrue(query_one('SELECT radacctid FROM radacct WHERE username=%s',(row['username'],)))
            self.assertEqual(get_accounting_totals(current(row)['username'])['total_bytes'],1048576)
        def test_20_archive_moves_old_cycle_with_voucher_ownership(self):
            row=card(usage=1048576)
            execute_write('UPDATE radacct SET acctstarttime=NOW()-INTERVAL 200 DAY,acctstoptime=NOW()-INTERVAL 199 DAY,acctupdatetime=NOW()-INTERVAL 199 DAY WHERE username=%s',(row['username'],))
            original=query_one('SELECT * FROM radacct WHERE username=%s',(row['username'],))
            change(row,username=row['username']+'_archive_old');new=current(row)
            from services.accounting_archiver_service import archive_old_sessions
            with patch('core.license_feature_policy.verified_state',return_value={'valid':True,'features':{}}):
                ok,msg=archive_old_sessions(days_threshold=90)
            self.assertTrue(ok,msg)
            self.assertIsNone(query_one('SELECT radacctid FROM radacct WHERE radacctid=%s',(original['radacctid'],)))
            archived=query_one('SELECT * FROM radacct_archive WHERE radacctid=%s',(original['radacctid'],))
            for key in ('username','acctuniqueid','acctinputoctets','acctoutputoctets','acctstarttime','acctstoptime'):
                self.assertEqual(archived[key],original[key],key)
            self.assertEqual(archived['account_voucher_id'],row['id']);self.assertEqual(archived['account_username'],new['username'])
            self.assertEqual(get_accounting_totals(new['username'],new['last_renewed_at'])['total_bytes'],0)
            self.assertEqual(get_subscriber_sessions(new['username'])[0]['original_username'],row['username'])

    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Cases))
    proof=dict(passed=result.wasSuccessful(),tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),
               matrix=matrix,mariadb_actual=True,docker_gate_actual=True,nas_actual=False)
    Path('/evidence/voucher-edit-proof.private.json').write_text(json.dumps(proof,indent=2))
    print(json.dumps(proof))
    if not result.wasSuccessful():raise SystemExit(1)
    row=card(usage=4*1048576);old=row['username'];change(row,username=old+'_radius');new=current(row)
    Path('/evidence/voucher-radius-case.private.json').write_text(json.dumps(dict(id=row['id'],old=old,new=new['username'],quota_bytes=10240*1048576,prior_bytes=4*1048576)))


if __name__=='__main__':main()

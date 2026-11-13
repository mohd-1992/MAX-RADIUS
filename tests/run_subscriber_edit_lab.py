"""Explicit disposable MariaDB fixture. Never run against customer databases."""
import os
import sys


def main():
    if sys.argv[1:] != ['--run'] or not os.environ.get('DB_NAME','').startswith('subscriber_edit_fixture_'):
        raise SystemExit('Explicit synthetic subscriber-edit database and --run required')
    import datetime as dt
    import hashlib
    import json
    import unittest
    from contextlib import contextmanager
    from pathlib import Path
    from unittest.mock import patch
    import threading
    thread_start=threading.Thread.start
    threading.Thread.start=lambda self:None
    from database.db import init_database,query_one,query_all,execute_write,db_session,get_connection
    init_database()
    from database.subscriber_edit_schema import install_subscriber_edit_schema
    from services import subscriber_edit_service as edit
    from services.online_policy_service import view,local
    from services.quota_service import get_accounting_totals
    from services.subscriber_service import create_subscriber,get_subscriber_sessions
    from core.radius_sync import sync_subscriber_to_radius
    from services.quick_action_service import action_change_package
    from services.renewal_settlement_service import SettlementPending
    execute_write('UPDATE wisp_license_runtime_state SET is_valid=1,valid_until=UTC_TIMESTAMP()+INTERVAL 1 DAY,max_active_sessions=1000 WHERE id=1')
    # Restore normal threads only after application workers have been suppressed.
    counter=0
    checks=[]
    packages={}
    for label,quota,days in [('ten',10240,30),('small',1024,7),('large',20480,40),('open',0,30)]:
        execute_write("INSERT INTO wisp_packages(name,service_type,volume_quota_mb,validity_value,validity_unit,validity_days,price,cost,rate_download,rate_upload,is_active) VALUES(%s,'hotspot',%s,%s,'days',%s,0,0,'4M','2M',1)",(label,quota,days,days))
        packages[label]=query_one('SELECT id FROM wisp_packages WHERE name=%s',(label,))['id']

    @contextmanager
    def boundary(username, **kwargs):
        with view({}):
            local.operation_succeeded=False
            yield

    def new_sub(label='ten',status='active',usage=0):
        nonlocal counter
        counter+=1;name='fixture_sub_'+str(counter)
        execute_write("INSERT INTO wisp_subscribers(username,password,full_name,service_type,package_id,status,balance,extra_quota_mb,snap_volume_quota_mb,created_at,first_used_at,last_renewed_at,expires_at) VALUES(%s,'fixture-only',%s,'hotspot',%s,%s,123.45,0,%s,NOW()-INTERVAL 2 DAY,NOW()-INTERVAL 2 DAY,NOW()-INTERVAL 1 DAY,NOW()+INTERVAL 10 DAY)",(name,name,packages[label],status,0 if label=='open' else {'ten':10240,'small':1024,'large':20480}[label]))
        row=query_one('SELECT * FROM wisp_subscribers WHERE username=%s',(name,))
        if usage:
            execute_write("INSERT INTO radacct(username,acctsessionid,acctuniqueid,nasipaddress,acctstarttime,acctupdatetime,acctstoptime,acctsessiontime,acctinputoctets,acctoutputoctets,acctterminatecause) VALUES(%s,%s,%s,'192.0.2.1',NOW()-INTERVAL 2 HOUR,NOW()-INTERVAL 1 HOUR,NOW()-INTERVAL 1 HOUR,3600,0,%s,'User-Request')",(name,name,hashlib.md5(name.encode()).hexdigest(),usage))
        return row

    def request(row,**changes):
        return dict(operation_id=os.urandom(16).hex(),expected_revision=edit.revision(row),username=row['username'],**changes)

    def change(row,**changes):
        body=request(row)
        body.update(changes)
        with patch('services.renewal_settlement_service.settled_account',boundary),patch('services.renewal_settlement_service.wait_for_final_accounting'),patch('services.nas_session_settlement.freeze_and_disconnect'):
            result=edit.edit_subscriber(row['id'],body,actor='fixture-admin',can_change_expiry=True)
        return result,body

    class Cases(unittest.TestCase):
        def test_01_migration_repeat_preserves_rows(self):
            row=new_sub(usage=4*1048576)
            before=query_all('SELECT username,acctuniqueid,acctinputoctets,acctoutputoctets,acctstoptime FROM radacct ORDER BY radacctid')
            with get_connection() as c:install_subscriber_edit_schema(c)
            self.assertEqual(before,query_all('SELECT username,acctuniqueid,acctinputoctets,acctoutputoctets,acctstoptime FROM radacct ORDER BY radacctid'))

        def test_02_rename_preserves_history_usage_contract_money(self):
            row=new_sub(usage=4*1048576);used=get_accounting_totals(row['username'])
            original=query_one('SELECT * FROM radacct WHERE username=%s',(row['username'],))
            result,body=change(row,username=row['username']+'_renamed')
            new=query_one('SELECT * FROM wisp_subscribers WHERE id=%s',(row['id'],))
            self.assertTrue(result['success']);self.assertEqual(used,get_accounting_totals(new['username']))
            for key in ['id','balance','first_used_at','last_renewed_at','expires_at','package_id','snap_volume_quota_mb','extra_quota_mb']:
                self.assertEqual(row[key],new[key],key)
            current=query_one('SELECT * FROM radacct WHERE radacctid=%s',(original['radacctid'],))
            for key in ['username','acctuniqueid','acctinputoctets','acctoutputoctets','acctstarttime','acctstoptime']:
                self.assertEqual(original[key],current[key],key)
            self.assertFalse(query_one('SELECT id FROM radcheck WHERE username=%s',(row['username'],)))
            self.assertTrue(query_one('SELECT id FROM radcheck WHERE username=%s',(new['username'],)))
            history=get_subscriber_sessions(new['username']);self.assertEqual(history[0]['original_username'],row['username'])
            # Duplicate submission returns the durable committed result, despite stale revision.
            self.assertEqual(result,edit.edit_subscriber(row['id'],body,actor='fixture-admin',can_change_expiry=True))

        def test_03_repeated_rename_and_return_to_original(self):
            row=new_sub(usage=8*1048576);name=row['username']
            for target in [name+'_a',name+'_b',name]:
                change(row,username=target);row=query_one('SELECT * FROM wisp_subscribers WHERE id=%s',(row['id'],))
                self.assertEqual(get_accounting_totals(target)['total_bytes'],8*1048576)

        def test_04_duplicate_username_and_old_alias_rejected(self):
            row=new_sub();other=new_sub();old=row['username'];change(row,username=old+'_next')
            with self.assertRaises(ValueError):change(other,username=old)
            with self.assertRaises(ValueError):change(other,username=old+'_next')
            with self.assertRaises(Exception):
                execute_write("INSERT INTO wisp_vouchers(username,password,package_id,status) VALUES(%s,'fixture',%s,'unused')",(old,packages['ten']))

        def test_05_invalid_profile_has_no_settlement(self):
            for changes in [dict(username='bad name'),dict(mac_binding='invalid'),dict(static_ip='999.1.2.3'),dict(service_type='bogus'),dict(expires_at='bad-date')]:
                row=new_sub()
                with patch('services.renewal_settlement_service.settled_account') as settle:
                    with self.assertRaises(ValueError):change(row,**changes)
                    settle.assert_not_called()

        def test_06_expiry_extend_reduce_unlimited_preserves_cycle(self):
            row=new_sub(usage=4*1048576)
            for expiry in [(dt.datetime.now()+dt.timedelta(days=50)).replace(microsecond=0),'2001-01-01T00:00:00',None]:
                change(row,expires_at=str(expiry) if expiry else '')
                new=query_one('SELECT * FROM wisp_subscribers WHERE id=%s',(row['id'],))
                self.assertEqual(new['last_renewed_at'],row['last_renewed_at']);self.assertEqual(new['balance'],row['balance'])
                self.assertEqual(get_accounting_totals(new['username'])['total_bytes'],4*1048576)
                self.assertEqual(new['status'],'expired' if expiry=='2001-01-01T00:00:00' else 'active')
                row=new

        def test_07_expiry_does_not_override_suspension_or_exhausted_quota(self):
            for status,usage in [('suspended',0),('expired',12*1024*1048576)]:
                row=new_sub(status=status,usage=usage);change(row,expires_at=(dt.datetime.now()+dt.timedelta(days=90)).isoformat())
                self.assertEqual(query_one('SELECT status FROM wisp_subscribers WHERE id=%s',(row['id'],))['status'],status)

        def test_08_permission_stale_revision_and_actor_bound_receipt(self):
            row=new_sub();body=request(row,expires_at='2090-01-01T00:00:00')
            with self.assertRaises(ValueError):edit.edit_subscriber(row['id'],body,can_change_expiry=False)
            body=request(row);body['expected_revision']='wrong'
            with self.assertRaises(ValueError):edit.edit_subscriber(row['id'],body)
            result,body=change(row,full_name='Updated')
            with self.assertRaises(ValueError):edit.edit_subscriber(row['id'],body,actor='different')

        def test_09_service_and_bindings_do_not_reset_usage(self):
            row=new_sub(usage=7*1048576)
            change(row,service_type='pppoe',mac_binding='AA-BB-CC-DD-EE-FF',static_ip='192.0.2.9')
            new=query_one('SELECT * FROM wisp_subscribers WHERE id=%s',(row['id'],))
            self.assertEqual(new['service_type'],'pppoe');self.assertEqual(new['mac_binding'],'AA:BB:CC:DD:EE:FF')
            self.assertEqual(new['last_renewed_at'],row['last_renewed_at']);self.assertEqual(get_accounting_totals(new['username'])['total_bytes'],7*1048576)

        def test_10_failed_radius_sync_rolls_back_everything(self):
            row=new_sub(usage=3*1048576)
            with patch('services.subscriber_service.sync_subscriber_to_radius',return_value=False):
                with self.assertRaises(ValueError):change(row,username=row['username']+'_failed')
            self.assertEqual(query_one('SELECT username FROM wisp_subscribers WHERE id=%s',(row['id'],))['username'],row['username'])
            self.assertFalse(query_one('SELECT username FROM wisp_subscriber_names WHERE subscriber_id=%s',(row['id'],)))
            self.assertEqual(query_one('SELECT account_username FROM radacct WHERE username=%s',(row['username'],))['account_username'],None)

        def test_11_unsettled_session_prevents_partial_profile(self):
            row=new_sub();body=request(row);body['username']=row['username']+'_new'
            with patch('services.renewal_settlement_service.settled_account',boundary),patch('services.nas_session_settlement.disconnect_for_identity',side_effect=SettlementPending('fixture pending')):
                with self.assertRaises(SettlementPending):edit.edit_subscriber(row['id'],body,can_change_expiry=True)
            self.assertEqual(query_one('SELECT username FROM wisp_subscribers WHERE id=%s',(row['id'],))['username'],row['username'])

        def test_12_package_matrix_existing_cycle_operation(self):
            for old_label,new_label in [('ten','small'),('small','large'),('ten','open'),('open','small')]:
                for rollover in [False,True]:
                    row=new_sub(old_label,usage=100*1048576);operation_id=os.urandom(16).hex()
                    with patch('services.renewal_settlement_service.settled_account',boundary):
                        ok,msg=action_change_package('subscriber',row['id'],packages[new_label],enable_rollover=rollover,admin_username='fixture-admin',operation_id=operation_id)
                    self.assertTrue(ok,msg)
                    new=query_one('SELECT * FROM wisp_subscribers WHERE id=%s',(row['id'],))
                    self.assertGreater(new['last_renewed_at'],row['last_renewed_at']);self.assertEqual(new['balance'],row['balance'])
                    expected={'ten':10240,'small':1024,'large':20480,'open':0}[new_label]
                    self.assertEqual(new['snap_volume_quota_mb'],expected)
                    self.assertEqual(get_accounting_totals(new['username'],since_timestamp=new['last_renewed_at'])['total_bytes'],0)
                    expected_extra=({'ten':10240,'small':1024,'large':20480}[old_label]-100) if rollover and old_label!='open' else 0
                    self.assertAlmostEqual(float(new['extra_quota_mb']),expected_extra,places=2)
                    # Confirm replay never advances cycle or repeats any side effect.
                    ok,msg=action_change_package('subscriber',row['id'],packages[new_label],enable_rollover=rollover,admin_username='fixture-admin',operation_id=operation_id)
                    self.assertTrue(ok,msg);self.assertEqual(new,query_one('SELECT * FROM wisp_subscribers WHERE id=%s',(row['id'],)))
                    checks.append({'from':old_label,'to':new_label,'rollover':rollover,'passed':True})

        def test_13_indexed_owned_lookup_and_late_counter_updates(self):
            row=new_sub(usage=1048576);change(row,username=row['username']+'_new')
            # Same physical row/UID; late packet updates stay attributed to its owner.
            execute_write('UPDATE radacct SET acctoutputoctets=acctoutputoctets+1048576 WHERE username=%s',(row['username'],))
            self.assertEqual(get_accounting_totals(row['username']+'_new')['total_bytes'],2*1048576)
            with db_session() as conn:
                with conn.cursor() as cur:
                    cur.execute('EXPLAIN SELECT * FROM radacct_owned WHERE username=%s',(row['username']+'_new',))
                    plan=cur.fetchall();self.assertTrue(any(x.get('key')=='idx_subscriber_account_read' for x in plan),plan)

        def test_14_points_wallet_follows_identity_without_rewriting_ledger(self):
            row=new_sub()
            execute_write("INSERT INTO wisp_loyalty_wallets(username,points_balance) VALUES(%s,77)",(row['username'],))
            change(row,username=row['username']+'_points')
            self.assertEqual(query_one('SELECT points_balance FROM wisp_loyalty_wallets WHERE username=%s',(row['username']+'_points',))['points_balance'],77)

        def test_15_current_cycle_cannot_be_archived_after_rename(self):
            row=new_sub(usage=2097152)
            execute_write('UPDATE wisp_subscribers SET last_renewed_at=NOW()-INTERVAL 120 DAY WHERE id=%s',(row['id'],))
            execute_write('UPDATE radacct SET acctstarttime=NOW()-INTERVAL 110 DAY,acctstoptime=NOW()-INTERVAL 109 DAY WHERE username=%s',(row['username'],))
            row=query_one('SELECT * FROM wisp_subscribers WHERE id=%s',(row['id'],));change(row,username=row['username']+'_archive')
            from services.accounting_archiver_service import archive_old_sessions
            with patch('core.license_feature_policy.verified_state',return_value={'valid':True,'features':{}}):
                ok,msg=archive_old_sessions(days_threshold=90)
            self.assertTrue(ok,msg)
            self.assertTrue(query_one('SELECT radacctid FROM radacct WHERE username=%s',(row['username'],)))
            self.assertEqual(get_accounting_totals(row['username']+'_archive')['total_bytes'],2097152)

        def test_16_dump_excludes_generated_values_and_views(self):
            from services.backup_service import generate_mysql_dump
            dump,tables=generate_mysql_dump()
            self.assertNotIn('INSERT INTO `radacct_owned`',dump)
            self.assertIn('GENERATED ALWAYS',dump)
            import re
            for columns in re.findall(r'INSERT INTO `radacct`\s*\((.*?)\)',dump,re.S):
                self.assertNotIn('account_read_username',columns)

        def test_17_concurrent_profile_cannot_overwrite_revision(self):
            from concurrent.futures import ThreadPoolExecutor
            row=new_sub();first=request(row,full_name='one');second=request(row,full_name='two')
            def apply(body):
                try:return edit.edit_subscriber(row['id'],body,actor='fixture-admin',can_change_expiry=True)['success']
                except ValueError:return False
            with patch.object(threading.Thread,'start',thread_start):
                with ThreadPoolExecutor(max_workers=2) as executor:out=list(executor.map(apply,[first,second]))
            self.assertEqual(sorted(out),[False,True])

        def test_18_late_insert_under_old_name_retains_owner(self):
            row=new_sub();change(row,username=row['username']+'_new')
            execute_write("INSERT INTO radacct(username,acctsessionid,acctuniqueid,nasipaddress,acctstarttime,acctstoptime,acctsessiontime,acctinputoctets,acctoutputoctets,acctterminatecause) VALUES(%s,'late-old','late-old-unique','192.0.2.1',NOW()-INTERVAL 1 HOUR,NOW(),3600,0,1048576,'User-Request')",(row['username'],))
            self.assertEqual(get_accounting_totals(row['username']+'_new')['total_bytes'],1048576)
            self.assertEqual(query_one("SELECT username,account_subscriber_id FROM radacct WHERE acctuniqueid='late-old-unique'")['username'],row['username'])

    suite=unittest.defaultTestLoader.loadTestsFromTestCase(Cases)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    proof={'passed':result.wasSuccessful(),'tests':result.testsRun,'package_matrix':checks,'failures':len(result.failures),'errors':len(result.errors),'nas_boundary_mocked':True,'mysql_actual':True}
    Path('/evidence/source-integration.private.json').write_text(json.dumps(proof,indent=2))
    if not result.wasSuccessful():raise SystemExit(1)
    # Keep a known renamed account for real FreeRADIUS packet verification later.
    row=new_sub(usage=4*1048576);result,body=change(row,username='fixture_radius_renamed')
    Path('/evidence/radius-case.private.json').write_text(json.dumps({'id':row['id'],'old':row['username'],'new':'fixture_radius_renamed','prior_bytes':4*1048576,'quota_bytes':10240*1048576}))
    print(json.dumps(proof))


if __name__=='__main__':main()

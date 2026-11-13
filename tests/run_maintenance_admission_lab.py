"""Explicit disposable MariaDB acceptance; never point at an application DB."""
import contextlib
import json
import os
from pathlib import Path
import sys
import threading
import unittest
import uuid
from unittest.mock import patch


def main():
    if '--run' not in sys.argv or os.environ.get('DB_HOST')!='max-license-test-db' or os.environ.get('DB_NAME')!='maintenance_admission_fixture':
        raise SystemExit('Requires --run and the disposable maintenance_admission_fixture')
    from database.db import get_connection,query_one,query_all,execute_write
    from services import maintenance_admission as gate,automation_task_service as tasks,automation_task_actions as actions
    from services import db_maintenance_service as maintenance,accounting_archiver_service as archive
    if '--compiled' in sys.argv:
        assert all(m.__file__.startswith('/app/') and m.__file__.endswith('.so') for m in (gate,tasks,actions,maintenance,archive))
    tasks.ensure_tables(seed_defaults=False)
    @contextlib.contextmanager
    def holder(name):
        ready=threading.Event();release=threading.Event();result={}
        def run():
            c=get_connection()
            try:
                with c.cursor() as q:
                    q.execute("SELECT GET_LOCK(CONCAT(DATABASE(),%s),0) n,CONNECTION_ID() id",(':'+name,));row=q.fetchone()
                    assert row['n']==1;result['owner']=row['id'];ready.set();assert release.wait(20)
                    q.execute('SELECT RELEASE_LOCK(CONCAT(DATABASE(),%s))',(':'+name,))
            finally:c.close()
        thread=threading.Thread(target=run);thread.start();assert ready.wait(10)
        try:yield release,result
        finally:release.set();thread.join(10);assert not thread.is_alive()

    class Cases(unittest.TestCase):
        def setUp(self):
            self.name='admission_'+uuid.uuid4().hex[:12]
            c=get_connection()
            try:
                with c.cursor() as q:
                    q.execute("INSERT INTO wisp_packages(name,service_type,price,cost,is_active) VALUES(%s,'hotspot',25,7,1)",(self.name,));pkg=q.lastrowid
                    q.execute('INSERT INTO wisp_voucher_batches(batch_number,name,package_id,card_count) VALUES(%s,%s,%s,2)',(self.name,self.name,pkg));self.batch=q.lastrowid
                    self.ids=[]
                    for suffix in ('target','untouched'):
                        name=self.name+'_'+suffix
                        q.execute("INSERT INTO wisp_vouchers(batch_id,package_id,serial_number,username,password,pin_code,status,last_renewed_at,expires_at) VALUES(%s,%s,%s,%s,'fixture','fixture','expired',NOW()-INTERVAL 60 DAY,NOW()-INTERVAL 30 DAY)",(self.batch,pkg,uuid.uuid4().hex[:18],name));self.ids.append(q.lastrowid)
                    q.execute("INSERT INTO wisp_voucher_sales(voucher_id,batch_id,batch_name,username,package_name,price,cost,activated_at) VALUES(%s,%s,'fixture',%s,'fixture',25,7,NOW()-INTERVAL 40 DAY)",(self.ids[0],self.batch,self.name+'_target'));self.sale=q.lastrowid
                    q.execute("INSERT INTO radacct(acctsessionid,acctuniqueid,username,nasipaddress,acctstarttime,acctupdatetime,acctstoptime,acctsessiontime,acctinputoctets,acctoutputoctets,acctterminatecause) VALUES(%s,%s,%s,'10.1.0.1',NOW()-INTERVAL 45 DAY,NOW()-INTERVAL 44 DAY,NOW()-INTERVAL 44 DAY,86400,12345,67890,'User-Request')",(self.name,uuid.uuid4().hex,self.name+'_target'))
                c.commit()
            finally:c.close()
            self.money=query_one('SELECT * FROM wisp_voucher_sales WHERE id=?',(self.sale,))
            self.other=query_one('SELECT * FROM wisp_vouchers WHERE id=?',(self.ids[1],))
            self.patches=[patch('services.license_guard_service.get_active_license_status',return_value=dict(valid=True,status='active',features={},limits={})),patch('services.license_guard_service.has_license_feature',return_value=True),patch('core.license_feature_policy.enabled',return_value=True),patch.object(tasks,'resource_pressure',return_value=False)]
            for p in self.patches:p.start();self.addCleanup(p.stop)
        def unchanged(self):
            self.assertEqual(self.money,query_one('SELECT * FROM wisp_voucher_sales WHERE id=?',(self.sale,)))
            self.assertEqual(self.other,query_one('SELECT * FROM wisp_vouchers WHERE id=?',(self.ids[1],)))
        def delete(self,**extra):
            return maintenance.delete_expired_vouchers(batch_limit=1,min_age_days=14,account_scope={'scope':'batch','batch_id':self.batch},**extra)
        def definition(self):
            return tasks.validate(dict(name='isolated admission',kind='delete',params={'scope':'batch','batch_id':self.batch,'age':14,'batch':1},schedule=dict(mode='daily',times=['06:00'],start='2026-11-13'),options={'resourcePause':False},enabled=False,consent=True))
        def run_record(self):
            ident=uuid.uuid4().hex;task=uuid.uuid4().hex;value=self.definition()
            execute_write("INSERT INTO wisp_automation_tasks(id,name,kind,definition,created_at) VALUES(?,?,'delete',?,UTC_TIMESTAMP())",(task,value['name'],json.dumps(value)))
            execute_write("INSERT INTO wisp_automation_runs(id,task_id,slot_key,state,definition,result,created_at,updated_at) VALUES(?,?,?,'queued',?,'{}',UTC_TIMESTAMP(),UTC_TIMESTAMP())",(ident,task,task+':'+uuid.uuid4().hex,json.dumps(value)))
            return ident
        def test_delete_waits_then_commits_once_preserving_sale_summary_and_other_card(self):
            with holder('factory-reset') as (release,owner):
                def progress(r):
                    if 'بانتظار' in r.get('message',''):
                        self.assertIsNotNone(query_one('SELECT id FROM wisp_vouchers WHERE id=?',(self.ids[0],)));release.set()
                result=self.delete(admission_timeout=3,progress_callback=progress)
            self.assertTrue(result['success'],result);self.assertEqual(result['deleted_vouchers'],1);self.unchanged()
            row=query_one('SELECT SUM(input_bytes) i,SUM(output_bytes) o FROM wisp_account_usage_summary WHERE voucher_id=?',(self.ids[0],))
            self.assertEqual((int(row['i']),int(row['o'])),(12345,67890))
        def test_delete_timeout_and_no_wait_compatibility_do_not_mutate_accounts(self):
            with holder('expired-card-delete'):
                result=self.delete(admission_timeout=.3)
                self.assertTrue(result['deferred']);self.assertEqual(result['processed'],0)
                old=self.delete();self.assertFalse(old['success']);self.assertIn('قيد التنفيذ',old['message'])
            self.assertIsNotNone(query_one('SELECT id FROM wisp_vouchers WHERE id=?',(self.ids[0],)));self.unchanged()
        def test_cancelled_wait_does_not_mutate(self):
            with holder('factory-reset'):
                result=self.delete(admission_timeout=2,cancel_callback=lambda:True)
            self.assertTrue(result['cancelled']);self.assertEqual(result['processed'],0);self.unchanged()
        def test_partial_group_released_and_owner_diagnostic_is_actual_connection(self):
            with holder('factory-reset') as (_,owner):
                seen=[]
                def progress(r):
                    self.assertIsNone(query_one("SELECT IS_USED_LOCK(CONCAT(DATABASE(),':accounting-archive')) n")['n'])
                    seen.extend(gate._describe_owners(['factory-reset']))
                with gate.maintenance_admission(('accounting-archive','factory-reset'),timeout=.3,progress=progress) as result:self.assertTrue(result['deferred'])
                self.assertEqual(seen[0]['connection'],owner['owner'])
        def test_summary_wait_admits_once_and_retains_account(self):
            with holder('factory-reset') as (release,_):
                result=archive.run_archive_maintenance(days=2,mode='summary',account_scope={'scope':'batch','batch_id':self.batch},account_limit=1,admission_timeout=3,progress_callback=lambda r:release.set())
            self.assertTrue(result['success'],result);self.assertEqual(result['processed'],1)
            self.assertIsNotNone(query_one('SELECT id FROM wisp_vouchers WHERE id=?',(self.ids[0],)));self.unchanged()
        def test_summary_timeout_never_runs_summarizer(self):
            with holder('accounting-archive'):
                result=archive.run_archive_maintenance(days=2,mode='summary',admission_timeout=.3)
            self.assertTrue(result['deferred']);self.assertEqual(result['processed'],0)
            self.assertEqual(query_one('SELECT COUNT(*) n FROM wisp_account_usage_summary WHERE voucher_id=?',(self.ids[0],))['n'],0);self.unchanged()
        def test_worker_timeout_records_skipped_and_terminal_slot_does_not_replay(self):
            ident=self.run_record()
            with holder('factory-reset'),patch.object(gate,'AUTOMATION_WAIT_SECONDS',.3):tasks.worker(ident)
            row=query_one('SELECT state,result FROM wisp_automation_runs WHERE id=?',(ident,));self.assertEqual(row['state'],'skipped');self.assertTrue(json.loads(row['result'])['deferred']);self.assertFalse(json.loads(row['result'])['partial'])
            tasks.worker(ident);self.assertIsNotNone(query_one('SELECT id FROM wisp_vouchers WHERE id=?',(self.ids[0],)));self.unchanged()
        def test_worker_wait_heartbeat_then_success_and_no_replay(self):
            ident=self.run_record();original=tasks.execute_write;heartbeats=[]
            with holder('factory-reset') as (release,_):
                def write(sql,args=()):
                    result=original(sql,args)
                    if 'SET result=?' in sql and args and 'بانتظار' in args[0]:heartbeats.append(args[0]);release.set()
                    return result
                with patch.object(tasks,'execute_write',side_effect=write),patch.object(gate,'AUTOMATION_WAIT_SECONDS',3):tasks.worker(ident)
            self.assertTrue(heartbeats);row=query_one('SELECT state,result FROM wisp_automation_runs WHERE id=?',(ident,));self.assertEqual(row['state'],'success',row)
            self.assertEqual(json.loads(row['result'])['deleted_vouchers'],1);tasks.worker(ident);self.unchanged()

    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Cases))
    print(json.dumps(dict(passed=result.wasSuccessful(),tests=result.testsRun,actual_mariadb=True,compiled='--compiled' in sys.argv,nas_access=False)))
    raise SystemExit(not result.wasSuccessful())


if __name__=='__main__':main()

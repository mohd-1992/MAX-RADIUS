"""Explicit isolated MariaDB runner; never uses application/customer configuration.

Required CLOCK_REPAIR_REPORT_DIR (new directory) and CLOCK_REPAIR_EVIDENCE_DIR.
Only anonymous dates, counters and quotas are projected from private SELECT evidence.
No account, NAS, session identity, credentials, or full customer database is copied.
"""
import ast
import copy
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
import traceback
import unittest
import uuid

import pymysql

ROOT = Path(__file__).resolve().parents[1]
REPORT = Path(os.environ['CLOCK_REPAIR_REPORT_DIR']).resolve()
EVIDENCE = Path(os.environ['CLOCK_REPAIR_EVIDENCE_DIR']).resolve()
REPORT.mkdir(parents=True, exist_ok=False)
MODE=os.environ.get('CLOCK_REPAIR_MODE','legacy')
assert MODE in ('legacy','priority')
CASE_MODULE=os.environ.get('CLOCK_REPAIR_CASE_MODULE','clock_record_priority_cases')
assert CASE_MODULE in ('clock_record_priority_cases','clock_boot_resilience_cases')
spec=importlib.util.spec_from_file_location('clock_record_lab',ROOT/'deploy/clock-record-repair/lab_repair.py')
module=importlib.util.module_from_spec(spec)
sys.modules['lab_repair']=module
spec.loader.exec_module(module)
LabRepair, Refused = module.LabRepair,module.Refused
token=uuid.uuid4().hex[:10]
name='max-clock-record-repair-'+token
if CASE_MODULE=='clock_boot_resilience_cases':name='max-clock-boot-resilience-'+token
schema='max_clock_repair_lab_'+token
password=secrets.token_hex(24)
container=None
config=None
raw=json.loads((EVIDENCE/'clock-record-repair-preflight-latest.private.json').read_text())
causal=json.loads((EVIDENCE/'expired-incident-causality-latest.private.json').read_text())
metadata=json.loads((EVIDENCE/'clock-record-schema-latest.private.json').read_text())['columns']
triggers=[t for t in raw['private_triggers'] if t['event']=='UPDATE']
expected={t['name']:t['statement_sha256'] for t in triggers}
assert len(raw['private_rows'])==50 and len(raw['private_auth'])==57 and len(triggers)==2


def connect():
    return pymysql.connect(**config,charset='utf8mb4',cursorclass=pymysql.cursors.DictCursor,
                           autocommit=False,read_timeout=10,write_timeout=10)


class Checks(unittest.TestCase):
    reservation_contention_reproduced=False
    def setUp(self):
        self.c=connect()
        self.e=LabRepair(self.c,expected)
        for table in ('clock_repair_lab_journal','wisp_session_reservations','wisp_nas_session_seals',
                      'wisp_session_baselines','wisp_voucher_sales','radacct','radpostauth',
                      'wisp_vouchers','wisp_voucher_batches','wisp_packages'):
            self.e.q('DELETE FROM '+table)
        self.e.q("INSERT INTO wisp_packages(id,name) VALUES(1,'anonymous-fixture')")
        self.e.q("INSERT INTO wisp_voucher_batches(id,batch_number,name,package_id,card_count) VALUES(1,'fixture','fixture',1,50)")
        originals=sorted(raw['private_rows'],key=lambda x:x['id'])
        cardmap={r['card_id']:i for i,r in enumerate(originals,1)}
        measures={r['id']:r for r in causal['private_measurements']}
        for i,r in enumerate(originals,1):
            m=measures[r['card_id']]
            user=f'fixture-{i:03}'
            self.e.q('INSERT INTO wisp_vouchers(id,batch_id,package_id,serial_number,username,password,pin_code,status,first_used_at,last_renewed_at,expires_at,snap_volume_quota_mb,extra_quota_mb,snap_uptime_limit_mins) VALUES(%s,1,1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                     [i,user,user,'fixture-only',user,m['status'],m['first_used'] or m['cycle'],m['cycle'],m['expires'] or None,m['quota_mb'],m['extra_mb'],m['uptime_limit_seconds']//60])
            self.e.q('INSERT INTO radacct(radacctid,username,acctsessionid,acctuniqueid,nasipaddress,framedipaddress,callingstationid,acctstarttime,acctupdatetime,acctstoptime,acctinputoctets,acctoutputoctets,acctsessiontime,acctinterval,acctterminatecause) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                     [i,user,f'session-{i}',f'unique-{i}','192.0.2.1',f'198.51.100.{i}',f'02:00:00:00:00:{i:02x}',r['start'],r['update'],r['stop'],r['input'],r['output'],r['seconds'],r['interval'],r['cause']])
            # Anonymous prior-cycle aggregate fixture, not a reconstructed real NAS session.
            base=dt.datetime.fromisoformat(m['cycle'])+dt.timedelta(seconds=1)
            self.e.q('INSERT INTO radacct(radacctid,username,acctsessionid,acctuniqueid,nasipaddress,acctstarttime,acctupdatetime,acctstoptime,acctinputoctets,acctoutputoctets,acctsessiontime) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s)',
                     [1000+i,user,f'prior-{i}',f'prior-unique-{i}','192.0.2.1',base,base,base,m['current']['bytes']-r['input']-r['output'],m['current']['seconds']-r['seconds']])
        hashes=sorted({a['account_sha256'] for a in raw['private_auth']})
        self.assertEqual(len(hashes),51)
        # Identity mapping intentionally discarded. 57 records including 7 internal healthchecks.
        for i,a in enumerate(sorted(raw['private_auth'],key=lambda x:x['id']),1):
            user=f'fixture-{(i-1)%50+1:03}' if i<=50 else 'internal-fixture-health'
            self.e.q('INSERT INTO radpostauth(id,username,pass,reply,authdate) VALUES(%s,%s,%s,%s,%s)',[i,user,'fixture-only',a['reply'],a['date']])
        self.e.q("INSERT INTO wisp_voucher_sales(id,voucher_id,batch_id,username,package_name,price,cost) VALUES(1,1,1,'fixture-001','fixture',10,3)")
        self.e.q("INSERT INTO lab_untouched_sentinel(id,payload) VALUES(1,'license-financial-placeholder') ON DUPLICATE KEY UPDATE payload=VALUES(payload)")
        self.c.commit()
        self.plan=self.e.prepare(list(range(1,51)),list(range(1,58)),list(range(1,51)),-10743,'same-boot-journal-step-and-host-receipt-sql')

    def tearDown(self):
        self.c.rollback();self.c.close()

    def totals(self):
        return self.e.q('SELECT v.id,COALESCE(SUM(a.acctinputoctets+a.acctoutputoctets),0) bytes,COALESCE(SUM(a.acctsessiontime),0) seconds FROM wisp_vouchers v LEFT JOIN radacct a ON a.username=v.username AND a.acctstarttime>=v.last_renewed_at GROUP BY v.id ORDER BY v.id')

    def full(self):
        return {t:self.e.q('SELECT * FROM '+t+' ORDER BY '+key) for t,key in
                [('radacct','radacctid'),('radpostauth','id'),('wisp_vouchers','id'),('wisp_voucher_sales','id'),('wisp_session_baselines','radacctid'),('lab_untouched_sentinel','id')]}

    def refuse_after(self,sql,params=(),rollback=False):
        if rollback:self.e.execute(self.plan)
        self.e.q(sql,params);self.c.commit()
        before=self.full()
        with self.assertRaises(Refused):self.e.execute(self.plan,rollback=rollback)
        self.assertEqual(self.full(),before)

    def test_01_success_dates_only_and_usage_same(self):
        before=self.full();totals=self.totals()
        self.assertEqual(self.e.execute(self.plan),'applied')
        after=self.full()
        for key in ('wisp_vouchers','wisp_voucher_sales','wisp_session_baselines','lab_untouched_sentinel'):
            self.assertEqual(before[key],after[key])
        self.assertEqual(totals,self.totals())
        for x,y in zip(before['radacct'],after['radacct']):
            if x['radacctid']<=50:
                for field in ('acctstarttime','acctupdatetime','acctstoptime'):
                    self.assertEqual((y[field]-x[field]).total_seconds(),-10743)
                    y[field]=x[field]
            self.assertEqual(x,y)
        for x,y in zip(before['radpostauth'],after['radpostauth']):
            self.assertEqual((y['authdate']-x['authdate']).total_seconds(),-10743)
            y['authdate']=x['authdate'];self.assertEqual(x,y)

    def test_02_exact_rollback(self):
        before=self.full();self.e.execute(self.plan);self.e.execute(self.plan,rollback=True)
        self.assertEqual(before,self.full())

    def test_03_idempotent_apply(self):
        self.e.execute(self.plan);before=self.full()
        self.assertEqual(self.e.execute(self.plan),'already_done');self.assertEqual(before,self.full())

    def test_04_idempotent_rollback_and_no_reapply(self):
        self.e.execute(self.plan);self.e.execute(self.plan,rollback=True)
        self.assertEqual(self.e.execute(self.plan,rollback=True),'already_done')
        with self.assertRaises(Refused):self.e.execute(self.plan)

    def test_05_failed_after_acct_atomic(self):
        self.fail_stage('accounting_updated')

    def test_06_failed_before_commit_atomic(self):
        self.fail_stage('before_commit')

    def fail_stage(self,stage):
        before=self.full()
        def fail(s):
            if s==stage:raise RuntimeError('simulated local failure')
        with self.assertRaises(RuntimeError):self.e.execute(self.plan,checkpoint=fail)
        self.assertEqual(before,self.full())
        self.assertEqual(self.e.q('SELECT * FROM clock_repair_lab_journal'),[])

    def test_07_changed_target_counter_refused(self):
        self.refuse_after('UPDATE radacct SET acctinputoctets=acctinputoctets+1 WHERE radacctid=1')

    def test_08_changed_target_stop_refused(self):
        self.refuse_after('UPDATE radacct SET acctstoptime=acctstoptime+INTERVAL 1 SECOND WHERE radacctid=1')

    def test_09_changed_auth_refused(self):
        self.refuse_after("UPDATE radpostauth SET reply='Access-Reject',authdate=authdate WHERE id=1")

    def test_10_changed_cycle_refused(self):
        self.refuse_after('UPDATE wisp_vouchers SET last_renewed_at=last_renewed_at+INTERVAL 1 SECOND WHERE id=1')

    def test_11_changed_status_refused(self):
        self.refuse_after("UPDATE wisp_vouchers SET status='suspended' WHERE id=1")

    def test_12_seal_refused_without_trigger_override(self):
        self.refuse_after("INSERT INTO wisp_nas_session_seals SELECT radacctid,username,nasipaddress,acctsessionid,acctinputoctets,acctoutputoctets,acctsessiontime,NOW(),'fixture' FROM radacct WHERE radacctid=1")

    def test_13_reservation_refused_no_deletion(self):
        self.refuse_after("INSERT INTO wisp_session_reservations(radacctid,session_key,username,nasipaddress,callingstationid,expires_at) VALUES(1,'fixture','fixture-001','192.0.2.1','fixture',NOW()+INTERVAL 1 HOUR)")
        self.assertEqual(self.e.q('SELECT COUNT(*) n FROM wisp_session_reservations')[0]['n'],1)

    def test_14_rollback_new_target_counter_refused(self):
        self.refuse_after('UPDATE radacct SET acctoutputoctets=acctoutputoctets+1 WHERE radacctid=1',rollback=True)

    def test_15_rollback_new_same_account_session_refused(self):
        self.refuse_after("INSERT INTO radacct(radacctid,username,acctuniqueid,acctsessionid,nasipaddress,acctstarttime,acctinputoctets,acctoutputoctets) VALUES(3000,'fixture-001','new-same','new-same','192.0.2.1','2026-10-17 17:15:00',123,456)",rollback=True)

    def test_16_rollback_new_sale_refused(self):
        self.refuse_after("INSERT INTO wisp_voucher_sales(voucher_id,batch_id,username,package_name,price) VALUES(1,1,'fixture-001','fixture',20)",rollback=True)

    def test_17_rollback_new_auth_refused(self):
        self.refuse_after("INSERT INTO radpostauth(username,reply,authdate) VALUES('fixture-001','Access-Accept','2026-10-17 17:15:00')",rollback=True)

    def test_18_rollback_new_baseline_refused(self):
        self.refuse_after("INSERT INTO wisp_session_baselines(radacctid,username,renewed_at) VALUES(1,'fixture-001','2026-10-17 17:15:00')",rollback=True)

    def test_19_changed_trigger_refused(self):
        self.e.q('DROP TRIGGER trg_radacct_release_reservation_update');self.c.commit()
        try:
            with self.assertRaises(Refused):self.e.execute(self.plan)
        finally:install_triggers(self.e,replace=True);self.c.commit()

    def test_20_missing_target_refused(self):
        self.refuse_after('DELETE FROM radacct WHERE radacctid=1')

    def test_21_manifest_tamper_refused(self):
        bad=copy.deepcopy(self.plan);bad['offset']=-10800
        with self.assertRaises(Refused):self.e.execute(bad)

    def test_22_unproven_offset_refused(self):
        with self.assertRaises(Refused):self.e.prepare([1],[1],[1],-10800,'same-boot-journal-step-and-host-receipt-sql')

    def test_23_open_row_refused(self):
        self.e.q('UPDATE radacct SET acctstoptime=NULL WHERE radacctid=1');self.c.commit()
        with self.assertRaises(Refused):self.e.prepare([1],[1],[1],-10743,'same-boot-journal-step-and-host-receipt-sql')

    def test_24_cycle_crossing_refused(self):
        self.e.q("UPDATE wisp_vouchers SET last_renewed_at='2026-10-17 16:00:00' WHERE id=1");self.c.commit()
        with self.assertRaises(Refused):self.e.prepare([1],[1],[1],-10743,'same-boot-journal-step-and-host-receipt-sql')

    def test_25_outside_window_refused(self):
        self.e.q("UPDATE radacct SET acctstarttime='2026-10-16 18:48:00' WHERE radacctid=1");self.c.commit()
        with self.assertRaises(Refused):self.e.prepare([1],[1],[1],-10743,'same-boot-journal-step-and-host-receipt-sql')

    def test_26_auth_timestamp_timezone_roundtrip(self):
        a=self.plan['auth'][0]['authdate'];self.e.execute(self.plan)
        self.e.q("SET time_zone='+00:00'")
        b=self.e.q('SELECT authdate FROM radpostauth WHERE id=1')[0]['authdate']
        self.assertEqual(b,a-dt.timedelta(seconds=10743,hours=3))
        self.e.q("SET time_zone='+03:00'")

    def test_27_fractional_step_is_not_stored_as_false_precision(self):
        self.e.execute(self.plan)
        self.assertTrue(all(x['acctstarttime'].microsecond==0 for x in self.e.q('SELECT acctstarttime FROM radacct')))

    def test_28_outside_account_new_accounting_preserved_and_rollback_allowed(self):
        self.e.execute(self.plan)
        self.e.q("INSERT INTO radacct(radacctid,username,acctuniqueid,acctsessionid,acctstarttime,acctinputoctets) VALUES(3001,'outside-fixture','outside','outside','2026-10-17 17:20:00',789)")
        self.c.commit();self.e.execute(self.plan,rollback=True)
        self.assertEqual(self.e.q('SELECT acctinputoctets n FROM radacct WHERE radacctid=3001')[0]['n'],789)

    def test_29_concurrent_unrelated_accounting(self):
        result=[]
        def concurrent():
            cc=connect()
            try:
                with cc.cursor() as cur:
                    cur.execute("INSERT INTO radacct(radacctid,username,acctuniqueid,acctsessionid,acctstarttime,acctinputoctets) VALUES(3002,'outside-concurrent','other','other','2026-10-17 17:20:00',654)")
                cc.commit();result.append('committed')
            except Exception as ex:result.append(type(ex).__name__)
            finally:cc.close()
        def hook(stage):
            if stage=='accounting_updated':
                th=threading.Thread(target=concurrent);th.start();th.join(5)
                self.assertFalse(th.is_alive());self.assertEqual(result,['committed'])
        self.e.execute(self.plan,checkpoint=hook)
        self.assertEqual(self.e.q('SELECT acctinputoctets n FROM radacct WHERE radacctid=3002')[0]['n'],654)

    def test_30_concurrent_target_update_waits_bounded(self):
        result=[]
        def update():
            cc=connect()
            try:
                with cc.cursor() as cur:
                    cur.execute('SET innodb_lock_wait_timeout=1')
                    cur.execute('UPDATE radacct SET acctinputoctets=acctinputoctets+7 WHERE radacctid=1')
                cc.commit();result.append('committed')
            except pymysql.OperationalError as ex:cc.rollback();result.append(ex.args[0])
            finally:cc.close()
        def hook(stage):
            if stage=='locked':
                th=threading.Thread(target=update);th.start();th.join(4)
                self.assertFalse(th.is_alive());self.assertEqual(result,[1205])
        self.e.execute(self.plan,checkpoint=hook)
        self.assertEqual(result,[1205])

    def test_31_connection_loss_rolls_back(self):
        before=self.full()
        def fail(stage):
            if stage=='accounting_updated':
                self.c.close();raise RuntimeError('simulated lost connection')
        with self.assertRaises(Exception):self.e.execute(self.plan,checkpoint=fail)
        self.c=connect();self.e=LabRepair(self.c,expected)
        self.assertEqual(before,self.full())

    def test_32_receipt_retains_original_and_after_journal(self):
        self.e.execute(self.plan)
        j=self.e.q('SELECT * FROM clock_repair_lab_journal')[0]
        self.assertEqual(j['state'],'applied')
        original=json.loads(j['plan_json'])
        self.assertEqual(len(original['acct']),50);self.assertEqual(len(original['auth']),57)
        self.assertEqual(j['post_context_sha'],module.digest(self.e.context(self.plan['cards'])))

    def test_33_refuses_non_lab_database(self):
        class Other:
            def cursor(self):return self
            def __enter__(self):return self
            def __exit__(self,*args):pass
            description=True
            def execute(self,*args):pass
            def fetchall(self):return [{'db':'customer_production'}]
        with self.assertRaises(Refused):LabRepair(Other(),expected)

    def test_34_target_column_types_precision_collation_match(self):
        actual=self.e.q("SELECT TABLE_NAME t,COLUMN_NAME c,COLUMN_TYPE ty,COLLATION_NAME co,DATETIME_PRECISION dp FROM information_schema.columns WHERE TABLE_SCHEMA=DATABASE()")
        mapping={(v['t'],v['c']):v for v in actual}
        for m in metadata:
            v=mapping[(m['table'],m['column'])]
            self.assertEqual((v['ty'],v['co'],v['dp']),(m['type'],m['collation'],m['precision']))

    def test_35_concurrent_renewal_lock_and_later_rollback_refusal(self):
        results=[]
        def renewal():
            cc=connect()
            try:
                with cc.cursor() as cur:
                    cur.execute('SET innodb_lock_wait_timeout=1')
                    cur.execute("UPDATE wisp_vouchers SET last_renewed_at='2026-10-17 17:25:00' WHERE id=1")
                cc.commit();results.append('committed')
            except pymysql.OperationalError as ex:cc.rollback();results.append(ex.args[0])
            finally:cc.close()
        def hook(stage):
            if stage=='locked':
                th=threading.Thread(target=renewal);th.start();th.join(4)
                self.assertFalse(th.is_alive());self.assertEqual(results,[1205])
        self.e.execute(self.plan,checkpoint=hook)
        self.e.q("UPDATE wisp_vouchers SET last_renewed_at='2026-10-17 17:25:00' WHERE id=1");self.c.commit()
        with self.assertRaises(Refused):self.e.execute(self.plan,rollback=True)

    def test_36_failure_during_rollback_is_atomic(self):
        self.e.execute(self.plan);before=self.full()
        def fail(stage):
            if stage=='accounting_updated':raise RuntimeError('rollback interruption')
        with self.assertRaises(RuntimeError):self.e.execute(self.plan,rollback=True,checkpoint=fail)
        self.assertEqual(before,self.full())
        self.assertEqual(self.e.q('SELECT state FROM clock_repair_lab_journal')[0]['state'],'applied')
        self.assertEqual(self.e.execute(self.plan,rollback=True),'rolled_back')

    def test_37_lost_commit_reply_does_not_reapply(self):
        def uncertain_reply():
            self.e.execute(self.plan)
            raise ConnectionError('simulated lost reply after commit')
        with self.assertRaises(ConnectionError):uncertain_reply()
        before=self.full()
        self.assertEqual(self.e.execute(self.plan),'already_done')
        self.assertEqual(before,self.full())

    def test_38_existing_cycle_baseline_extra_quota_preserved(self):
        cycle=self.plan['cards'][0]['last_renewed_at']
        self.e.q('INSERT INTO wisp_session_baselines(radacctid,username,renewed_at,baseline_input_bytes,baseline_output_bytes,baseline_seconds) VALUES(1,%s,%s,1,2,1)',('fixture-001',cycle))
        self.e.q('UPDATE wisp_vouchers SET extra_quota_mb=123 WHERE id=1');self.c.commit()
        plan=self.e.prepare(list(range(1,51)),list(range(1,58)),list(range(1,51)),-10743,'same-boot-journal-step-and-host-receipt-sql')
        sql='SELECT a.radacctid,CASE WHEN a.acctinputoctets>b.baseline_input_bytes THEN a.acctinputoctets-b.baseline_input_bytes ELSE 0 END+CASE WHEN a.acctoutputoctets>b.baseline_output_bytes THEN a.acctoutputoctets-b.baseline_output_bytes ELSE 0 END bytes,CASE WHEN a.acctsessiontime>b.baseline_seconds THEN a.acctsessiontime-b.baseline_seconds ELSE 0 END seconds FROM radacct a JOIN wisp_session_baselines b ON b.radacctid=a.radacctid JOIN wisp_vouchers v ON v.username=a.username AND b.renewed_at=v.last_renewed_at WHERE a.acctstarttime>=v.last_renewed_at OR b.radacctid IS NOT NULL'
        before=self.e.q(sql);baseline=self.e.q('SELECT * FROM wisp_session_baselines')
        self.e.execute(plan)
        self.assertEqual(self.e.q(sql),before)
        self.assertEqual(self.e.q('SELECT * FROM wisp_session_baselines'),baseline)
        self.assertEqual(self.e.q('SELECT extra_quota_mb n FROM wisp_vouchers WHERE id=1')[0]['n'],123)

    def test_39_known_limitation_unrelated_auth_reservation_waits(self):
        # A passing reproduction of a LIMITATION, not evidence of acceptable live latency.
        result=[]
        def reservation():
            cc=connect()
            try:
                with cc.cursor() as cur:
                    cur.execute('SET innodb_lock_wait_timeout=1')
                    cur.execute("INSERT INTO wisp_session_reservations(session_key,username,nasipaddress,callingstationid,expires_at) VALUES('unrelated-pending','unrelated-fixture','192.0.2.2','fixture',NOW()+INTERVAL 1 HOUR)")
                cc.commit();result.append('committed')
            except pymysql.OperationalError as ex:cc.rollback();result.append(ex.args[0])
            finally:cc.close()
        def hook(stage):
            if stage=='accounting_updated':
                th=threading.Thread(target=reservation);th.start();th.join(4)
                self.assertFalse(th.is_alive());self.assertEqual(result,[1205])
        self.e.execute(self.plan,checkpoint=hook)
        Checks.reservation_contention_reproduced=(result==[1205])


def install_triggers(engine,replace=False):
    for t in triggers:
        if replace:engine.q('DROP TRIGGER IF EXISTS '+t['name'])
        engine.q(f"CREATE TRIGGER {t['name']} {t['timing']} UPDATE ON radacct FOR EACH ROW "+t['statement'])


try:
    db_extra=['--stop-timeout','120'] if CASE_MODULE=='clock_boot_resilience_cases' else []
    db_command=['--innodb-flush-log-at-trx-commit=1','--innodb-doublewrite=ON','--sync-binlog=1'] if db_extra else []
    p=subprocess.run(['docker','run','-d','--name',name,'--label','max-owned-task=clock-record-repair-lab',
        '--restart=no','-p','127.0.0.1::3306','-e','MARIADB_ROOT_PASSWORD='+password,
        '-e','MARIADB_DATABASE='+schema,*db_extra,'mariadb:10.11',*db_command],capture_output=True)
    (REPORT/'launch.stderr.private').write_bytes(p.stderr)
    if p.returncode:raise RuntimeError('Could not create isolated database')
    container=p.stdout.decode().strip()
    port=json.loads(subprocess.check_output(['docker','inspect','--format','{{json .NetworkSettings.Ports}}',container]))['3306/tcp'][0]['HostPort']
    config=dict(host='127.0.0.1',port=int(port),user='root',password=password,database=schema)
    (REPORT/'fixture.private.json').write_text(json.dumps(dict(config,container=container)),encoding='utf-8')
    for _ in range(60):
        try:c=connect();break
        except pymysql.OperationalError:time.sleep(.5)
    else:raise RuntimeError('Fixture not ready')
    # Extract schema-only statements: no bootstrap account seeds or initializer workers.
    source=(ROOT/'database/schema_mysql.sql').read_text(encoding='utf-8-sig')
    tables=['radacct','radpostauth','wisp_packages','wisp_voucher_batches','wisp_vouchers',
            'wisp_voucher_sales','wisp_session_baselines','wisp_session_reservations']
    if MODE=='priority':
        tables+=['radcheck','radreply','radgroupreply','radgroupcheck','radusergroup',
                 'wisp_license_info','wisp_revoked_licenses','wisp_subscribers','nasreload']
    with c.cursor() as cur:
        for table in tables:
            statement=re.search(r'CREATE TABLE (?:IF NOT EXISTS )?`'+table+r'` \([\s\S]*?\) ENGINE=[^;]+;',source).group()
            cur.execute(statement)
        nas=ast.parse((ROOT/'database/nas_session_safety.py').read_text())
        create=next(n.args[0].value for n in ast.walk(nas) if isinstance(n,ast.Call) and n.args and isinstance(n.args[0],ast.Constant) and isinstance(n.args[0].value,str) and n.args[0].value.startswith('CREATE TABLE IF NOT EXISTS wisp_nas_session_seals'))
        # The installer inherits database defaults. Match independently read customer metadata.
        cur.execute(create+' DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci')
        cur.execute('CREATE TABLE clock_repair_lab_journal(manifest CHAR(64) PRIMARY KEY,state VARCHAR(20),plan_json LONGTEXT,post_context_sha CHAR(64)) ENGINE=InnoDB')
        cur.execute('CREATE TABLE lab_untouched_sentinel(id INT PRIMARY KEY,payload TEXT) ENGINE=InnoDB')
    c.commit()
    engine=LabRepair(c,expected);install_triggers(engine);c.commit();c.close()
    if MODE=='priority':
        plugin_spec=importlib.util.spec_from_file_location('priority_cases',ROOT/'tests'/(CASE_MODULE+'.py'))
        plugin=importlib.util.module_from_spec(plugin_spec);plugin_spec.loader.exec_module(plugin)
        plugin.install_fixture(sys.modules[__name__])
        case_class=plugin.make_cases(sys.modules[__name__])
    else:case_class=Checks
    with (REPORT/'suite.stdout.private').open('w',encoding='utf-8') as log:
        suite=unittest.defaultTestLoader.loadTestsFromTestCase(case_class)
        started=time.monotonic();result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
    summary={'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
             'success':result.wasSuccessful(),'elapsed_seconds':round(time.monotonic()-started,3),
             'fixture_rows':50,'fixture_auth':57,'actual_update_trigger_hashes':expected,
             'customer_database_writes':0,'customer_tests':0,'live_repair_entrypoint':False,
             'timestamp_normalization_seconds':-10743,'original_step_seconds':-10743.186176,
             'subsecond_offset_rounding_error_seconds':0.186176,
             'exact_packet_timestamp_recovered':False,'full_production_database_clone':False,
             'known_limitation_unrelated_reservation_blocking_reproduced':Checks.reservation_contention_reproduced,
             'approved_for_live_execution':False}
    if MODE=='priority':summary.update(plugin.summary())
    (REPORT/'FINAL.private.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    print(json.dumps(summary))
except Exception:
    (REPORT/'runner.traceback.private').write_text(traceback.format_exc(),encoding='utf-8')
    print(json.dumps({'unable_to_complete_lab':True,'private_diagnostics':True}))
finally:
    if MODE=='priority' and 'plugin' in globals():
        try:plugin.cleanup()
        except Exception:
            (REPORT/'udp-cleanup.traceback.private').write_text(traceback.format_exc(),encoding='utf-8')
    if container:
        p=subprocess.run(['docker','stop',container],capture_output=True,timeout=40)
        (REPORT/'stop.stderr.private').write_bytes(p.stderr)

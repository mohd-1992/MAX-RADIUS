"""Contention tests on disposable MariaDB only. No customer access or app imports."""
import ast
import copy
import hashlib
import json
import statistics
import sys
import threading
import time
import uuid

import pymysql

METRICS={'auth_baseline_ms':[],'auth_during_repair_ms':[],
         'accounting_during_repair_ms':[],'scoped_transaction_ms':[],
         'service_errors':[],'priority_yield_ms':[]}
RADIUS=None


def constants(path,names):
    tree=ast.parse(path.read_text(encoding='utf-8'))
    selected=[n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(x,ast.Name) and x.id in names for x in n.targets)]
    ns={}
    exec(compile(ast.Module(body=selected,type_ignores=[]),str(path),'exec'),ns)
    return ns


def install_fixture(h):
    global RADIUS
    sys.path.insert(0,str(h.ROOT/'deploy/clock-record-repair'))
    import priority_repair
    h.PriorityLabRepair=priority_repair.PriorityLabRepair
    h.Deferred=priority_repair.Deferred
    c=h.connect();e=h.LabRepair(c,h.expected)
    assert h.schema.startswith('max_clock_repair_lab_')
    e.q('ALTER DATABASE `'+h.schema+'` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
    defs=constants(h.ROOT/'database/license_capacity.py',
        {'ACTIVE_SESSION_PREDICATE','PENDING_RESERVATION_PREDICATE','SCHEMA_SQL','CHECK_SQL','RESERVE_SQL','LINK_SQL'})
    cycle=constants(h.ROOT/'database/cycle_safety.py',{'GUARD_TABLE_SQL'})
    freeze=constants(h.ROOT/'database/first_use_policy.py',{'FREEZE_SQL'})
    maintenance=constants(h.ROOT/'database/maintenance_jobs.py',{'MAINTENANCE_JOBS_DDL'})
    for sql in [defs['SCHEMA_SQL'],cycle['GUARD_TABLE_SQL'],maintenance['MAINTENANCE_JOBS_DDL'],defs['CHECK_SQL'],defs['RESERVE_SQL'],defs['LINK_SQL'],freeze['FREEZE_SQL']]:
        e.q(sql)
    # A bounded independent SQL lease fixture, not a customer license or signature test.
    e.q("INSERT INTO wisp_license_info(id,license_id,client_name,raw_package_json,signature_b64,status) VALUES(1,'isolated-fixture','isolated-fixture','{}','fixture-not-a-signature','active')")
    e.q("INSERT INTO wisp_license_runtime_state(id,license_row_id,package_hash,is_valid,license_mode,max_active_sessions,valid_until) VALUES(1,1,SHA2('{}',256),1,'active_sessions',10000,UTC_TIMESTAMP()+INTERVAL 1 HOUR)")
    c.commit();c.close()
    (h.REPORT/'sql-fixture-source-hashes.private.json').write_text(json.dumps({
        str(p.relative_to(h.ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in
        [h.ROOT/'database/license_capacity.py',h.ROOT/'database/first_use_policy.py',h.ROOT/'docker/freeradius/queries.conf']},indent=2))
    from clock_record_udp_fixture import RadiusFixture
    RADIUS=RadiusFixture(h);RADIUS.start()


def make_cases(h):
    class PriorityCases(h.Checks):
        def setUp(self):
            super().setUp()
            self.e=h.PriorityLabRepair(self.c,h.expected)
            self.parent_plan=self.plan
            self.plan=self.e.split_plan(self.parent_plan)[0]
            self.e.q("UPDATE wisp_license_runtime_state SET is_valid=1,max_active_sessions=10000,valid_until=UTC_TIMESTAMP()+INTERVAL 1 HOUR WHERE id=1")
            self.c.commit()

        def test_01_success_dates_only_and_usage_same(self):
            before=self.full();used=self.totals()
            self.assertTrue(self.e.run_scoped(self.parent_plan)['complete'])
            after=self.full();self.assertEqual(used,self.totals())
            for table in ('wisp_vouchers','wisp_voucher_sales','wisp_session_baselines','lab_untouched_sentinel'):
                self.assertEqual(before[table],after[table])
            for x,y in zip(before['radacct'],after['radacct']):
                if x['radacctid']<=50:
                    for field in ('acctstarttime','acctupdatetime','acctstoptime'):
                        self.assertEqual((y[field]-x[field]).total_seconds(),-10743);y[field]=x[field]
                self.assertEqual(x,y)
            for x,y in zip(before['radpostauth'],after['radpostauth']):
                self.assertEqual((y['authdate']-x['authdate']).total_seconds(),-10743)
                y['authdate']=x['authdate'];self.assertEqual(x,y)

        def test_32_receipt_retains_original_and_after_journal(self):
            self.e.execute(self.plan)
            j=self.e.q('SELECT * FROM clock_repair_lab_journal')[0]
            original=json.loads(j['plan_json'])
            self.assertEqual(len(original['acct']),1)
            self.assertEqual(len(original['auth']),len(self.plan['auth']))
            self.assertEqual(j['post_context_sha'],h.module.digest(self.e.context(self.plan['cards'])))

        def test_38_existing_cycle_baseline_extra_quota_preserved(self):
            cycle=self.plan['cards'][0]['last_renewed_at']
            self.e.q('INSERT INTO wisp_session_baselines(radacctid,username,renewed_at,baseline_input_bytes,baseline_output_bytes,baseline_seconds) VALUES(1,%s,%s,1,2,1)',('fixture-001',cycle))
            self.e.q('UPDATE wisp_vouchers SET extra_quota_mb=123 WHERE id=1');self.c.commit()
            plan=self.e.prepare([1],[r['id'] for r in self.plan['auth']],[1],-10743,'same-boot-journal-step-and-host-receipt-sql')
            before=self.e.q('SELECT * FROM wisp_session_baselines');used=self.totals()
            self.e.execute(plan)
            self.assertEqual(used,self.totals());self.assertEqual(before,self.e.q('SELECT * FROM wisp_session_baselines'))
            self.assertEqual(self.e.q('SELECT extra_quota_mb n FROM wisp_vouchers WHERE id=1')[0]['n'],123)

        def service(self,user='service-fixture', *, accounting=False, same_card=False):
            c=h.connect();started=time.monotonic()
            try:
                with c.cursor() as cur:
                    cur.execute("SET time_zone='+03:00'")
                    cur.execute('SET innodb_lock_wait_timeout=2')
                    if accounting:
                        unique=uuid.uuid4().hex
                        cur.execute("INSERT INTO radacct(username,acctuniqueid,acctsessionid,nasipaddress,callingstationid,acctstarttime,acctupdatetime,acctinputoctets,acctoutputoctets,acctsessiontime) VALUES(%s,%s,%s,'192.0.2.2','fixture',NOW(),NOW(),0,0,0)",(user,unique,unique))
                        rid=cur.lastrowid;c.commit()
                        for seconds,up,down in [(5,100,200),(10,150,350),(10,150,350)]:
                            cur.execute('UPDATE radacct SET acctsessiontime=GREATEST(COALESCE(acctsessiontime,0),%s),acctinputoctets=GREATEST(COALESCE(acctinputoctets,0),%s),acctoutputoctets=GREATEST(COALESCE(acctoutputoctets,0),%s),acctupdatetime=NOW() WHERE radacctid=%s AND acctstoptime IS NULL',(seconds,up,down,rid));c.commit()
                        cur.execute("UPDATE radacct SET acctstoptime=NOW(),acctupdatetime=NOW(),acctsessiontime=12,acctinputoctets=170,acctoutputoctets=390,acctterminatecause='User-Request' WHERE radacctid=%s",(rid,));c.commit()
                        cur.execute('SELECT acctinputoctets,acctoutputoctets,acctsessiontime,acctstoptime FROM radacct WHERE radacctid=%s',(rid,))
                        row=cur.fetchone()
                        assert (row['acctinputoctets'],row['acctoutputoctets'],row['acctsessiontime'])==(170,390,12) and row['acctstoptime'] is not None
                    else:
                        # Actual postauth reservation and first-policy functions, in their normal order.
                        cur.execute("SELECT fn_reserve_license_slot(%s,%s,'192.0.2.2','fixture','1','192.0.2.2') ok",(uuid.uuid4().hex,user))
                        assert cur.fetchone()['ok']==1
                        c.commit()
                        cur.execute('SELECT fn_freeze_voucher_policy(%s) ok',(user,))
                        assert cur.fetchone()['ok'] in (1,2,3)
                        c.commit()
                return (time.monotonic()-started)*1000
            finally:c.close()

        def thread_service(self,**kwargs):
            output=[]
            def task():
                try:output.append({'ok':True,'ms':self.service(**kwargs)})
                except Exception as ex:
                    error={'ok':False,'error':type(ex).__name__,'code':ex.args[0] if ex.args else None}
                    output.append(error);METRICS['service_errors'].append(error)
            th=threading.Thread(target=task);th.start()
            return th,output

        def assert_service(self,th,out):
            th.join(4);self.assertFalse(th.is_alive());self.assertTrue(out and out[0]['ok'],out)
            return out[0]['ms']

        def test_30_concurrent_target_update_waits_bounded(self):
            out=[];threads=[]
            def writer():
                c=h.connect();start=time.monotonic()
                try:
                    with c.cursor() as cur:
                        cur.execute('SET innodb_lock_wait_timeout=2')
                        cur.execute('UPDATE radacct SET acctinputoctets=acctinputoctets+7 WHERE radacctid=1')
                    c.commit();out.append({'ok':True,'ms':(time.monotonic()-start)*1000})
                except Exception as ex:out.append({'ok':False,'code':ex.args[0]})
                finally:c.close()
            def hook(stage):
                if stage=='locked':
                    th=threading.Thread(target=writer);threads.append(th);th.start();time.sleep(.13)
            with self.assertRaises(h.Deferred):self.e.execute(self.plan,checkpoint=hook)
            ms=self.assert_service(threads[0],out);METRICS['priority_yield_ms'].append(ms)
            self.assertEqual(self.e.q('SELECT acctstarttime FROM radacct WHERE radacctid=1')[0]['acctstarttime'],self.plan['acct'][0]['acctstarttime'])
            self.assertEqual(self.e.q('SELECT acctinputoctets n FROM radacct WHERE radacctid=1')[0]['n'],self.plan['acct'][0]['acctinputoctets']+7)

        def test_35_concurrent_renewal_lock_and_later_rollback_refusal(self):
            threads=[];out=[]
            def renewal():
                cc=h.connect();start=time.monotonic()
                try:
                    with cc.cursor() as cur:
                        cur.execute('SET innodb_lock_wait_timeout=2')
                        cur.execute("UPDATE wisp_vouchers SET last_renewed_at='2026-10-17 17:25:00' WHERE id=1")
                    cc.commit();out.append({'ok':True,'ms':(time.monotonic()-start)*1000})
                except Exception as ex:out.append({'ok':False,'code':ex.args[0]})
                finally:cc.close()
            def hook(stage):
                if stage=='locked':
                    th=threading.Thread(target=renewal);threads.append(th);th.start();time.sleep(.13)
            with self.assertRaises(h.Deferred):self.e.execute(self.plan,checkpoint=hook)
            METRICS['priority_yield_ms'].append(self.assert_service(threads[0],out))
            self.assertEqual(self.e.q('SELECT * FROM clock_repair_lab_journal'),[])

        def test_39_known_limitation_unrelated_auth_reservation_waits(self):
            # Same forced-hold scenario as the old failing service case; now real functions must finish.
            def hook(stage):
                if stage=='accounting_updated':
                    th,out=self.thread_service()
                    ms=self.assert_service(th,out);METRICS['auth_during_repair_ms'].append(ms)
            self.assertEqual(self.e.execute(self.plan,checkpoint=hook),'applied')

        def test_40_1893_reservations_unrelated_auth_and_accounting_finish(self):
            with self.c.cursor() as cur:
                cur.executemany("INSERT INTO wisp_session_reservations(session_key,username,nasipaddress,callingstationid,expires_at) VALUES(%s,%s,'192.0.2.3','fixture',NOW()+INTERVAL 1 HOUR)",[(f'pending-{i}',f'load-fixture-{i}') for i in range(1893)])
            self.c.commit()
            def hook(stage):
                if stage=='accounting_updated':
                    a,ao=self.thread_service(user='service-under-load')
                    b,bo=self.thread_service(user='acct-under-load',accounting=True)
                    METRICS['auth_during_repair_ms'].append(self.assert_service(a,ao))
                    METRICS['accounting_during_repair_ms'].append(self.assert_service(b,bo))
            self.assertEqual(self.e.execute(self.plan,checkpoint=hook),'applied')
            self.assertGreaterEqual(self.e.q('SELECT COUNT(*) n FROM wisp_session_reservations')[0]['n'],1894)

        def test_41_already_busy_card_repair_nowait(self):
            c=h.connect()
            try:
                with c.cursor() as cur:cur.execute('SELECT id FROM wisp_vouchers WHERE id=1 FOR UPDATE')
                start=time.monotonic()
                result=self.e.run_scoped(self.plan)
                self.assertFalse(result['complete']);self.assertLess(time.monotonic()-start,.5)
                self.assertEqual(result['completed'],[])
                self.assertEqual(self.e.q('SELECT * FROM clock_repair_lab_journal'),[])
            finally:c.rollback();c.close()

        def test_42_same_card_real_auth_wins_over_correction(self):
            saved=[]
            def hook(stage):
                if stage=='locked':
                    saved.extend(self.thread_service(user='fixture-001',same_card=True));time.sleep(.13)
            with self.assertRaises(h.Deferred):self.e.execute(self.plan,checkpoint=hook)
            ms=self.assert_service(saved[0],saved[1]);METRICS['priority_yield_ms'].append(ms)
            self.assertEqual(self.e.q('SELECT * FROM clock_repair_lab_journal'),[])

        def test_43_session_settings_not_global(self):
            c=h.connect()
            try:
                with c.cursor() as cur:
                    cur.execute('SELECT @@tx_isolation iso,@@max_statement_time deadline')
                    other=cur.fetchone()
                own=self.e.q('SELECT @@tx_isolation iso,@@max_statement_time deadline')[0]
                self.assertEqual(own['iso'],'READ-COMMITTED');self.assertEqual(other['iso'],'REPEATABLE-READ')
                self.assertEqual(float(other['deadline']),0);self.assertEqual(float(own['deadline']),.05)
            finally:c.close()

        def test_44_scoped_apply_resume_and_exact_rollback(self):
            before=self.full();used=self.totals()
            result=self.e.run_scoped(self.parent_plan);self.assertTrue(result['complete']);self.assertEqual(len(result['completed']),50)
            self.assertEqual(self.totals(),used)
            again=self.e.run_scoped(self.parent_plan);self.assertTrue(again['complete'])
            self.assertTrue(all(c['status']=='already_done' for c in again['completed']))
            METRICS['scoped_transaction_ms'].extend(self.e.timings)
            result=self.e.run_scoped(self.parent_plan,rollback=True);self.assertTrue(result['complete'])
            self.assertEqual(before,self.full())

        def test_45_partial_scope_stops_and_resumes_without_replay(self):
            c=h.connect()
            try:
                with c.cursor() as cur:cur.execute('SELECT id FROM wisp_vouchers WHERE id=2 FOR UPDATE')
                result=self.e.run_scoped(self.parent_plan)
                self.assertFalse(result['complete']);self.assertEqual(len(result['completed']),1)
                self.assertEqual(self.e.q('SELECT COUNT(*) n FROM clock_repair_lab_journal')[0]['n'],1)
            finally:c.rollback();c.close()
            result=self.e.run_scoped(self.parent_plan)
            self.assertTrue(result['complete']);self.assertEqual(result['completed'][0]['status'],'already_done')
            self.assertEqual(self.e.q('SELECT COUNT(*) n FROM clock_repair_lab_journal')[0]['n'],50)

        def test_46_new_reservation_for_frozen_historical_id_is_preserved(self):
            # Unsupported direct writer is nevertheless caught by the second pre-write guard.
            def hook(stage):
                if stage=='locked':
                    cc=h.connect()
                    try:
                        with cc.cursor() as cur:cur.execute("INSERT INTO wisp_session_reservations(radacctid,session_key,username,nasipaddress,callingstationid,expires_at) VALUES(1,'raced-historical','fixture-001','192.0.2.2','fixture',NOW()+INTERVAL 1 HOUR)")
                        cc.commit()
                    finally:cc.close()
            with self.assertRaises(h.Refused):self.e.execute(self.plan,checkpoint=hook)
            self.assertEqual(self.e.q("SELECT COUNT(*) n FROM wisp_session_reservations WHERE session_key='raced-historical'")[0]['n'],1)

        def test_47_transaction_budget_abandons_without_changes(self):
            before=self.full()
            e=h.PriorityLabRepair(self.c,h.expected,max_transaction_ms=20)
            def slow(stage):
                if stage=='locked':time.sleep(.03)
            with self.assertRaises(h.Deferred):e.execute(self.plan,checkpoint=slow)
            self.assertEqual(before,self.full())

        def test_48_auth_and_accounting_metrics_during_scoped_work(self):
            for _ in range(12):METRICS['auth_baseline_ms'].append(self.service())
            checks=[]
            def hook(stage):
                if stage=='accounting_updated' and len(checks)<12:
                    a,ao=self.thread_service(user='parallel-auth')
                    b,bo=self.thread_service(user='parallel-acct',accounting=True)
                    METRICS['auth_during_repair_ms'].append(self.assert_service(a,ao))
                    METRICS['accounting_during_repair_ms'].append(self.assert_service(b,bo))
                    checks.append(True)
            result=self.e.run_scoped(self.parent_plan,checkpoint=hook)
            self.assertTrue(result['complete'],result)
            self.assertEqual(len(checks),12)
            METRICS['scoped_transaction_ms'].extend(self.e.timings)

        def udp_card(self):
            self.e.q("INSERT INTO wisp_vouchers(id,batch_id,package_id,serial_number,username,password,pin_code,status,first_used_at,last_renewed_at,expires_at,policy_frozen_at,snap_volume_quota_mb,snap_rate_limit_str) VALUES(4000,1,1,'udp-fixture','udp-fixture','fixture-pass','udp-fixture','active','2026-10-16 00:00:00','2026-10-16 00:00:00',NOW()+INTERVAL 7 DAY,NOW(),1024,'1M/1M')")
            self.e.q("INSERT INTO radcheck(username,attribute,op,value) VALUES('udp-fixture','Cleartext-Password',':=','fixture-pass')")
            self.c.commit()

        def test_49_signed_udp_auth_and_accounting_while_repairing(self):
            self.udp_card()
            RADIUS.phase='baseline'
            for _ in range(8):self.assertEqual(RADIUS.request('udp-fixture')[0],2)
            self.assertEqual(RADIUS.request('udp-fixture',password='wrong-fixture-password')[0],3)
            cycles=[]
            def hook(stage):
                if stage=='accounting_updated' and len(cycles)<6:
                    RADIUS.phase='during_correction'
                    code,out=RADIUS.request('udp-fixture');self.assertEqual(code,2)
                    slot=out.get(25);self.assertTrue(slot)
                    session='udp-session-'+uuid.uuid4().hex[:12]
                    for status,seconds,up,down in [(1,0,0,0),(1,0,0,0),(3,5,100,200),(3,5,100,200),(2,12,170,390),(2,12,170,390)]:
                        code,_=RADIUS.request('udp-fixture',status=status,session=session,seconds=seconds,up=up,down=down,slot=slot)
                        self.assertEqual(code,5)
                    rows=self.e.q('SELECT * FROM radacct WHERE username=%s AND acctsessionid=%s',('udp-fixture',session))
                    self.assertEqual(len(rows),1)
                    self.assertEqual((rows[0]['acctinputoctets'],rows[0]['acctoutputoctets'],rows[0]['acctsessiontime']),(170,390,12))
                    self.assertIsNotNone(rows[0]['acctstoptime'])
                    self.assertEqual(self.e.q('SELECT COUNT(*) n FROM wisp_session_reservations WHERE radacctid=%s',(rows[0]['radacctid'],))[0]['n'],0)
                    cycles.append(True)
            result=self.e.run_scoped(self.parent_plan,checkpoint=hook)
            self.assertTrue(result['complete'],result);self.assertEqual(len(cycles),6)

        def test_50_same_card_udp_auth_correction_yields(self):
            self.udp_card()
            RADIUS.phase='priority_conflict'
            self.e.q("INSERT INTO radacct(radacctid,username,acctuniqueid,acctsessionid,nasipaddress,acctstarttime,acctupdatetime,acctstoptime,acctinputoctets,acctoutputoctets,acctsessiontime) VALUES(4000,'udp-fixture','udp-historical','udp-historical','192.0.2.99','2026-10-17 18:48:40','2026-10-17 18:48:40','2026-10-17 18:49:00',0,0,20)")
            self.c.commit()
            plan=self.e.prepare([4000],[],[4000],-10743,'same-boot-journal-step-and-host-receipt-sql')
            out=[];threads=[]
            def auth():
                try:out.append(RADIUS.request('udp-fixture')[0])
                except Exception as ex:out.append(type(ex).__name__)
            def hook(stage):
                if stage=='locked':
                    th=threading.Thread(target=auth);threads.append(th);th.start();time.sleep(.13)
            with self.assertRaises(h.Deferred):self.e.execute(plan,checkpoint=hook)
            threads[0].join(3);self.assertEqual(out,[2]);self.assertFalse(threads[0].is_alive())
            self.assertEqual(self.e.q('SELECT * FROM clock_repair_lab_journal'),[])

        def test_51_server_statement_deadline_abandons(self):
            before=self.full()
            def hook(stage):
                if stage=='locked':self.e.q('SELECT SLEEP(0.2)')
            start=time.monotonic()
            with self.assertRaises(h.Deferred):self.e.execute(self.plan,checkpoint=hook)
            self.assertLess(time.monotonic()-start,.5);self.assertEqual(before,self.full())

        def test_52_candidate_performs_no_schema_or_trigger_change(self):
            before=self.e.triggers();sql=[];original=self.e.q
            def traced(statement,params=()):
                sql.append(statement.lstrip().upper());return original(statement,params)
            self.e.q=traced
            self.e.execute(self.plan)
            self.assertFalse(any(s.startswith(('ALTER','CREATE','DROP','SET GLOBAL')) for s in sql))
            self.assertEqual(before,self.e.triggers())

        def test_53_unrelated_seal_can_be_written(self):
            result=[]
            def seal():
                cc=h.connect()
                try:
                    with cc.cursor() as cur:
                        cur.execute('SET innodb_lock_wait_timeout=1')
                        cur.execute("INSERT INTO wisp_nas_session_seals SELECT radacctid,username,nasipaddress,acctsessionid,COALESCE(acctinputoctets,0),COALESCE(acctoutputoctets,0),COALESCE(acctsessiontime,0),NOW(),'other-seal' FROM radacct WHERE radacctid=1001")
                    cc.commit();result.append(True)
                except Exception as ex:result.append(type(ex).__name__)
                finally:cc.close()
            def hook(stage):
                if stage=='accounting_updated':
                    th=threading.Thread(target=seal);th.start();th.join(2)
                    self.assertFalse(th.is_alive());self.assertEqual(result,[True])
            self.e.execute(self.plan,checkpoint=hook)
            self.assertEqual(result,[True])

    from clock_record_operator_cases import extend_cases
    return extend_cases(h,PriorityCases,RADIUS)


def summary():
    def stats(values):
        s=sorted(values)
        return {'count':len(s),'median_ms':round(statistics.median(s),3),'p95_ms':round(s[min(len(s)-1,int(.95*len(s)))],3),'max_ms':round(max(s),3)} if s else {'count':0}
    return {'mode':'priority','metrics':{k:stats(v) for k,v in METRICS.items() if k!='service_errors'},
            'sql_service_errors':len(METRICS['service_errors']),
            'customer_access_performed':False,'global_isolation_changed':False,'schema_or_trigger_change':False,
            'unrelated_auth_reservation_blocking_fixed_in_lab':True,'actual_radius_udp_tested':bool(RADIUS and RADIUS.metrics),
            'udp_signed_responses':len(RADIUS.metrics) if RADIUS else 0,
            'udp_response_counts':{str(code):sum(x['response_code']==code for x in RADIUS.metrics) for code in (2,3,5)} if RADIUS else {},
            'udp_fixture_core_image':RADIUS.image if RADIUS else None,
            'udp_timing':{phase:stats([x['ms'] for x in RADIUS.metrics if x['phase']==phase and x['response_code']==2]) for phase in ('baseline','during_correction','priority_conflict')} if RADIUS else {},
            'sql_license_lease_is_isolated_fixture_only':True,'absolute_zero_live_impact_proven':False,
            'approved_for_live_execution':False}


def cleanup():
    if RADIUS:RADIUS.stop()

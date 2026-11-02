"""Bound operator and host lifecycle checks. Disposable fixtures only."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
import uuid


def extend_cases(h,base,radius):
    import customer_operator as op
    import operator_kernel as kernel
    import host_dispatch as host
    import plan_codec as codec
    from lab_repair import digest,Refused

    class OperatorCases(base):
        def binding(self):
            row=self.e.q('SELECT DATABASE() db,@@hostname server_host,@@server_id server_id')[0]
            b=dict(row,incident=op.INCIDENT,authorized_after_local_tests=True,expires_epoch=time.time()+3600,
                radacct_ids=list(range(1,51)),auth_ids=list(range(1,58)),update_triggers=self.e.triggers(),
                internal_auth_user_sha=digest('internal-fixture-health'),lab_mode=True,
                db_host=h.config['host'],db_port=h.config['port'])
            for key in ('machine_sha256','web_id','db_id','core_id','web_image','core_image'):b[key]='fixture-'+key
            b['all_triggers']={r['name']:r['sha'] for r in self.e.q("SELECT TRIGGER_NAME name,SHA2(ACTION_STATEMENT,256) sha FROM information_schema.triggers WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE='radacct'")}
            b['routines']={r['name']:r['sha'] for r in self.e.q("SELECT ROUTINE_NAME name,SHA2(ROUTINE_DEFINITION,256) sha FROM information_schema.routines WHERE ROUTINE_SCHEMA=DATABASE() AND ROUTINE_NAME IN ('fn_freeze_voucher_policy','fn_check_license_auth','fn_reserve_license_slot')")}
            return b

        def operator(self,b=None,proof=None):
            b=b or self.binding()
            return op.CustomerOperator(self.c,b,proof or b,lab=True)

        def request(self,b,action,plans=None):
            return kernel.run(dict(binding=b,host_proof=b,db_host=h.config['host'],db_port=h.config['port'],
                root_password=h.password,lab=True,action=action,plans=plans or []))

        def test_54_bound_typed_apply_and_database_receipt_rollback(self):
            e=self.operator();e.ensure_journal();before=self.full();used=self.totals()
            p=e.prepare_bound([1],acct_ids=[1],auth_ids=[1,51,52,53,54,55,56,57])
            self.assertNotIn('fixture-only',codec.dumps(p))
            self.assertEqual(p,codec.loads(codec.dumps(p)))
            self.assertEqual(e.execute(p),'applied')
            stored=e.receipt_plan(p['sha256']);self.assertEqual(stored,p)
            self.assertEqual(e.execute(stored,rollback=True),'rolled_back')
            self.assertEqual(before,self.full());self.assertEqual(used,self.totals())

        def test_55_wrong_database_and_host_refused(self):
            b=self.binding();wrong=copy.deepcopy(b);wrong['server_host']='other'
            with self.assertRaises(Refused):self.operator(wrong,proof=b)
            wrong=copy.deepcopy(b);wrong['web_id']='other'
            with self.assertRaises(Refused):self.operator(b,proof=wrong)

        def test_56_expired_authorization_and_scope_refused(self):
            for change in ({'expires_epoch':0},{'authorized_after_local_tests':False},{'radacct_ids':[1]}):
                b=self.binding();b.update(change)
                with self.assertRaises(Refused):self.operator(b)

        def test_57_sql_write_allowlist(self):
            e=self.operator();before=self.full()
            for sql,params in [('UPDATE wisp_vouchers SET status=\'active\'',()),
                ('SET GLOBAL time_zone=\'+03:00\'',()),('DELETE FROM radacct',()),
                ('UPDATE radacct SET acctstarttime=%s,acctupdatetime=%s,acctstoptime=%s WHERE radacctid=%s',(None,None,None,1001))]:
                with self.assertRaises(Refused):e.q(sql,params)
            self.assertEqual(before,self.full())

        def test_58_changed_definition_and_wrong_manifest_refused(self):
            b=self.binding();b['routines']={};e=self.operator(b)
            with self.assertRaises(Refused):e.prepare_bound([1],acct_ids=[1],auth_ids=[1])
            e=self.operator();p=e.prepare_bound([1],acct_ids=[1],auth_ids=[1]);p['target_binding_sha']='bad'
            with self.assertRaises(Refused):e.execute(p)

        def test_59_production_rollback_refuses_new_sale(self):
            e=self.operator();e.ensure_journal();p=e.prepare_bound([1],acct_ids=[1],auth_ids=[1]);e.execute(p)
            self.e.q("INSERT INTO wisp_voucher_sales(voucher_id,batch_id,username,package_name,price,cost) VALUES(1,1,'fixture-001','new-fixture',1,1)");self.c.commit()
            before=self.full()
            with self.assertRaises(Refused):e.execute(e.receipt_plan(p['sha256']),rollback=True)
            self.assertEqual(before,self.full())

        def test_60_kernel_prepares_50_and_57_without_mutation(self):
            b=self.binding();before=self.full();result=self.request(b,'prepare')
            self.assertTrue(result['ok']);self.assertEqual(len(result['plans']),50)
            plans=[codec.loads(r['plan']) for r in result['plans']]
            self.assertEqual(sum(len(p['acct']) for p in plans),50);self.assertEqual(sum(len(p['auth']) for p in plans),57)
            self.assertEqual(before,self.full());self.assertTrue(result['summary']['pilot_has_no_open_sql'])

        def test_61_kernel_apply_then_receipt_rollback(self):
            b=self.binding();self.assertTrue(self.request(b,'journal')['ok'])
            prepared=self.request(b,'prepare');p=prepared['plans'][0];before=self.full()
            self.assertTrue(self.request(b,'apply',[p])['ok'])
            self.assertTrue(self.request(b,'rollback',[p])['ok']);self.assertEqual(before,self.full())

        def test_62_plan_codec_rejects_unknown_types(self):
            with self.assertRaises(ValueError):codec.loads('{"format":"pickle","plan":{}}')
            with self.assertRaises(ValueError):codec.decode({'$clock_type':'code','value':'anything'})

        def test_63_host_dispatch_real_docker_lifecycle_and_watchdog(self):
            # An owned, worker-free helper on the internal fixture network, never the development Web.
            helper=None
            try:
                image=subprocess.check_output(['docker','image','inspect','--format','{{.Id}}','max-radius-web:20261017-batch-export-filenames-lab'],text=True).strip()
                helper=subprocess.check_output(['docker','run','-d','--name','max-clock-operator-'+uuid.uuid4().hex[:10],
                    '--label','max-owned-task=clock-record-operator-lab','--network',radius.network,'--restart=no',
                    '--entrypoint','sleep',image,'infinity'],text=True).strip()
                b=self.binding();b.update(web_id=helper,db_id=h.container,core_id=radius.container,web_image=image,core_image=radius.image,db_host='fixture-db',db_port=3306)
                b['containers']=[]
                for cid in (helper,h.container,radius.container):
                    vals=subprocess.check_output(['docker','inspect','--format','{{.Id}} {{.Image}} {{.State.StartedAt}} {{.RestartCount}}',cid],text=True).split()
                    b['containers'].append(dict(id=vals[0],image=vals[1],started=vals[2],restarts=int(vals[3])))
                root=h.REPORT/'host-operator';(root/'source').mkdir(parents=True)
                sha={}
                for name in (*host.MODULES,'host_dispatch'):
                    src=h.ROOT/'deploy/clock-record-repair'/(name+'.py');shutil.copyfile(src,root/'source'/src.name)
                    sha[name]=hashlib.sha256(src.read_bytes()).hexdigest()
                host.atomic_json(root/'source-manifest.json',{'sha256':sha,'operator_tests_passed':True})
                host.atomic_json(root/'binding.private.json',b)
                d=host.Dispatcher(root,lab=True);self.assertEqual(d.run('status')['phase'],'staged')
                with self.assertRaises(FileNotFoundError):d.check_host()
                host.atomic_json(root/'backup.verified.private.json',{'verified_offhost':True,'binding_sha':digest(b),'verified_epoch':0})
                with self.assertRaises(RuntimeError):d.check_host()
                host.atomic_json(root/'backup.verified.private.json',{'verified_offhost':True,'binding_sha':digest(b),'verified_epoch':time.time()})
                before=self.full();self.assertEqual(d.run('prepare')['phase'],'prepared');self.assertEqual(before,self.full())
                with self.assertRaises(FileExistsError):d.run('prepare')
                with self.assertRaises(RuntimeError):d.run('pilot')
                self.assertEqual(d.run('journal')['phase'],'journal_ready')
                self.assertEqual(d.run('pilot')['phase'],'pilot_applied')
                with self.assertRaises(FileNotFoundError):d.run('remaining')
                self.assertEqual(d.run('watchdog')['phase'],'rolled_back');self.assertEqual(before,self.full())
                with self.assertRaises(RuntimeError):d.run('remaining')
                # Acceptance is checked without replaying or touching rows.
                host.atomic_json(root/'pilot.acceptance.private.json',{'passed':True,'pilot':json.loads((root/'plans.private.json').read_text())['pilot'],'binding_sha':digest(b)})
                self.assertEqual(d.run('watchdog')['phase'],'watchdog_accepted_no_action')
                root2=h.REPORT/'host-operator-accepted';(root2/'source').mkdir(parents=True)
                for src in (root/'source').glob('*.py'):shutil.copyfile(src,root2/'source'/src.name)
                b['run_nonce']=uuid.uuid4().hex
                host.atomic_json(root2/'source-manifest.json',{'sha256':sha,'operator_tests_passed':True})
                host.atomic_json(root2/'binding.private.json',b)
                host.atomic_json(root2/'backup.verified.private.json',{'verified_offhost':True,'binding_sha':digest(b),'verified_epoch':time.time()})
                d2=host.Dispatcher(root2,lab=True);self.assertEqual(d2.run('prepare')['phase'],'prepared')
                self.assertEqual(d2.run('journal')['phase'],'journal_ready')
                self.assertEqual(d2.run('pilot')['phase'],'pilot_applied')
                pilot=json.loads((root2/'plans.private.json').read_text())['pilot']
                host.atomic_json(root2/'pilot.acceptance.private.json',{'passed':True,'pilot':pilot,'binding_sha':digest(b)})
                self.assertEqual(d2.run('watchdog')['phase'],'watchdog_accepted_no_action')
                self.assertEqual(d2.run('remaining')['phase'],'completed')
                self.assertEqual(len(d2.kernel('status')['receipts']),51)
                self.assertEqual(d2.run('rollback')['phase'],'rolled_back');self.assertEqual(before,self.full())
                with self.assertRaises(FileExistsError):d2.run('rollback')
            finally:
                if helper:subprocess.run(['docker','stop',helper],capture_output=True,timeout=30)

    # Keep the new journal empty across cases, without granting its writes to the operator.
    original_setup=OperatorCases.setUp
    def setup(self):
        original_setup(self)
        self.e.q('CREATE TABLE IF NOT EXISTS '+op.JOURNAL+'(manifest CHAR(64) PRIMARY KEY,state VARCHAR(20),plan_json LONGTEXT,post_context_sha CHAR(64)) ENGINE=InnoDB')
        self.e.q('DELETE FROM '+op.JOURNAL);self.c.commit()
    OperatorCases.setUp=setup
    return OperatorCases

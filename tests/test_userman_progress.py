import ast
import copy
import json
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import nullcontext,contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
import importlib.util
spec=importlib.util.spec_from_file_location('progress_jobs',ROOT/'services/userman_job_service.py')
jobs=importlib.util.module_from_spec(spec);spec.loader.exec_module(jobs)

class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)
        self.p=patch.object(jobs,'root',return_value=self.folder);self.p.start();self.addCleanup(self.p.stop)
        gate=patch.object(jobs,'import_gate',return_value=nullcontext());gate.start();self.addCleanup(gate.stop)
        self.module=types.ModuleType('services.mikrotik_userman_importer')
        self.p2=patch.dict(sys.modules,{'services.mikrotik_userman_importer':self.module});self.p2.start();self.addCleanup(self.p2.stop)
    def wait(self,ident):
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            state=jobs.status(ident,7)
            if state['state']!='running':return state
            time.sleep(.01)
        self.fail('worker did not finish')
    def seed(self,count=450):
        ident='a'*32;path=self.folder/ident;path.mkdir()
        jobs.write(path/'state.json',{'id':ident,'owner':7,'kind':'fetch','state':'completed'})
        jobs.write(path/'source.json',{'users':[{'username':'fixture-'+str(i)} for i in range(count)],'profiles':[],'total_users':count})
        return ident
    def test_fetch_progress_preview_sample_and_owner_isolation(self):
        def fetch(*args,**kwargs):
            kwargs['progress'](stage='users',processed=500,total=600,percent=76)
            return {'users':[{'username':'fixture'}]*600,'profiles':[],'total_users':600}
        self.module.fetch_userman_via_api=fetch
        job=jobs.start_fetch(7,{'host':'192.0.2.1','username':'fixture','password':'private'})
        state=self.wait(job['id']);self.assertEqual(state['state'],'completed');self.assertEqual(state['processed'],600)
        result=jobs.result(job['id'],7);self.assertEqual(len(result['users']),100);self.assertEqual(result['total_users'],600)
        self.assertNotIn('private',(self.folder/job['id']/'state.json').read_text())
        with self.assertRaises(jobs.JobNotFound):jobs.result(job['id'],8)
        with self.assertRaises(jobs.JobNotFound):jobs.cancel(job['id'],8)
    def engine(self,calls,cancel_after=None):
        def execute(data,**kw):
            calls.append(len(data['users']));cache=kw.get('state_cache') or {'stats':{'created_users':0}}
            cache=copy.deepcopy(cache);cache['stats']['created_users']+=len(data['users'])
            if cancel_after and len(calls)==cancel_after:
                paths=list(self.folder.glob('*/state.json'))
                for path in paths:
                    state=json.loads(path.read_text())
                    if state.get('kind')=='import':(path.parent/'cancel').touch()
                try:kw['check_cancel']()
                except jobs.JobCancelled:return {'success':False}
            return {'success':True,'stats':cache['stats'],'state_cache':cache,'message':'fixture'}
        return execute
    def test_import_batches_commit_counts_and_idempotency(self):
        source=self.seed();calls=[];self.module.execute_userman_import=self.engine(calls)
        request={'source_job_id':source}
        job=jobs.start_import(7,request,'fixture');state=self.wait(job['id'])
        self.assertEqual(calls,[200,200,50]);self.assertEqual(state['processed'],450)
        result=jobs.result(job['id'],7);self.assertNotIn('state_cache',result)
        self.assertEqual(jobs.start_import(7,request,'fixture')['id'],job['id']);self.assertEqual(len(calls),3)
        with self.assertRaises(ValueError):jobs.start_import(7,dict(request,target_type='subscribers'),'fixture')
    def test_cancel_current_batch_does_not_report_uncommitted_counts(self):
        source=self.seed();calls=[];self.module.execute_userman_import=self.engine(calls,cancel_after=2)
        job=jobs.start_import(7,{'source_job_id':source},'fixture');state=self.wait(job['id'])
        self.assertEqual(state['state'],'cancelled');self.assertEqual(state['processed'],200)
        self.assertEqual(state['stats']['created_users'],200);self.assertEqual(len(calls),2)
    def test_fetch_cancellation_stops_worker(self):
        entered=threading.Event()
        def fetch(*args,**kw):
            entered.set()
            while True:kw['check_cancel']();time.sleep(.005)
        self.module.fetch_userman_via_api=fetch
        job=jobs.start_fetch(7,{'host':'192.0.2.1','username':'fixture'});entered.wait(1)
        jobs.cancel(job['id'],7);self.assertEqual(self.wait(job['id'])['state'],'cancelled')
        self.assertFalse((self.folder/job['id']/'source.json').exists())
    def test_duplicate_reference_never_replays_failed_or_cancelled_import(self):
        source=self.seed();calls=[];self.module.execute_userman_import=self.engine(calls,cancel_after=1)
        request={'source_job_id':source};job=jobs.start_import(7,request,'fixture');self.wait(job['id'])
        self.assertEqual(jobs.start_import(7,request,'fixture')['id'],job['id']);self.assertEqual(len(calls),1)
    def test_concurrent_operation_is_rejected(self):
        with jobs.operation_lock():
            with self.assertRaises(ValueError):jobs.start_fetch(7,{'host':'192.0.2.1','username':'fixture'})
    def test_job_ids_are_not_file_paths(self):
        with self.assertRaises(jobs.JobNotFound):jobs.status('../secret',7)
    def test_job_endpoints_share_existing_permission_and_csrf(self):
        p=(ROOT/'web/endpoint_permissions.py').read_text();catalog=(ROOT/'core/permission_catalog.py').read_text()
        for name in ['fetch','import','status','result','cancel']:
            self.assertIn('api_userman_job_'+name,p);self.assertIn('api_userman_job_'+name,catalog)
        tree=ast.parse((ROOT/'core/license_security.py').read_text())
        node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='protects_admin_account_request')
        ns={};exec(compile(ast.Module(body=[node],type_ignores=[]),'csrf','exec'),ns)
        self.assertTrue(ns['protects_admin_account_request'](types.SimpleNamespace(path='/api/tools/mikrotik-import/jobs/a/cancel')))

class StreamTests(unittest.TestCase):
    def setUp(self):
        import datetime,re
        tree=ast.parse((ROOT/'services/mikrotik_userman_importer.py').read_text(encoding='utf-8'))
        self.ns={'datetime':datetime,'re':re,'time':time,'present_value':lambda p,k,d=None:p[k] if p.get(k) is not None else d}
        wanted={'fetch_userman_via_api','parse_mikrotik_rate_limit','parse_mikrotik_bytes_to_mb','parse_mikrotik_uptime_to_mins','parse_mikrotik_validity','parse_shared_users','extract_quota_from_name'}
        module=ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in wanted],type_ignores=[])
        exec(compile(module,'stream','exec'),self.ns)
        class Client:
            def __init__(self,*args,**kw):self.sock=types.SimpleNamespace(settimeout=lambda value:None);self.closed=False;self.replies=iter([['!re','=username=fixture1','=actual-profile=P'],['!re','=username=fixture2','=actual-profile=P'],['!done']])
            def connect(self):pass
            def login(self,**kw):return True
            def close(self):self.closed=True
            def talk_raw(self,words):return [('!done',{'ret':'2'})]
            def execute_command(self,path,words=None):
                if path=='/tool/user-manager/profile/print':return [{'name':'P','validity':'1d'}]
                if path=='/tool/user-manager/profile/limitation/print':return [{'name':'L','transfer-limit':'100M'}]
                return [{'profile':'P','limitation':'L'}]
            def write_sentence(self,words):pass
            def read_sentence(self):return next(self.replies)
        self.client=Client();self.ns['BufferedRouterOSApiProtocol']=lambda **kw:self.client
    def test_progress_tracks_rows_before_done(self):
        updates=[];value=self.ns['fetch_userman_via_api']('192.0.2.1','fixture','fixture',progress=lambda **v:updates.append(v))
        self.assertEqual(value['total_users'],2);self.assertEqual([r['processed'] for r in updates if r['stage']=='users'],[0,1,2])
        self.assertTrue(self.client.closed)

    def test_cancel_during_rows_closes_socket_without_returning_preview(self):
        updates=[]
        def check():
            if any(r['processed']==1 for r in updates):raise jobs.JobCancelled()
        with self.assertRaises(jobs.JobCancelled):self.ns['fetch_userman_via_api']('192.0.2.1','fixture','fixture',progress=lambda **v:updates.append(v),check_cancel=check)
        self.assertTrue(self.client.closed)
    def test_router_trap_is_failure_not_empty_success(self):
        self.client.replies=iter([['!trap','=message=permission denied'],['!done']])
        with self.assertRaises(RuntimeError):self.ns['fetch_userman_via_api']('192.0.2.1','fixture','fixture',progress=lambda **v:None)
        self.assertTrue(self.client.closed)

class ResetCoordinationTests(unittest.TestCase):
    def test_import_is_denied_during_factory_reset(self):
        @contextmanager
        def lock(name):yield True
        lifecycle=types.ModuleType('services.account_lifecycle_service');lifecycle.job_lock=lock
        factory=types.ModuleType('services.factory_reset_service');factory.factory_reset_active=lambda:True
        with patch.dict(sys.modules,{'services.account_lifecycle_service':lifecycle,'services.factory_reset_service':factory}):
            with self.assertRaises(ValueError):
                with jobs.import_gate():self.fail('must not enter')
    def test_factory_reset_is_denied_while_import_holds_lock(self):
        tree=ast.parse((ROOT/'services/factory_reset_service.py').read_text())
        node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='start_factory_reset')
        @contextmanager
        def lock(name):yield name!='userman-import'
        writes=[];ns={'job_lock':lock,'ensure_job_table':lambda:None,'execute_write':lambda *a:writes.append(a)}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'reset','exec'),ns)
        with self.assertRaises(ValueError):ns['start_factory_reset']()
        self.assertEqual(writes,[])

if __name__=='__main__':unittest.main()

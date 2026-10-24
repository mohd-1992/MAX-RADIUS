"""Explicit retry after a confirmed pre-switch failure; isolated journals only."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RetryTests(unittest.TestCase):
    def setUp(self):
        if os.environ.get('MAX_RETRY_COMPILED') == '1':
            from services import system_update_host
            self.module = system_update_host
        else:
            self.module = load_file('retry_controller', ROOT/'deploy/hosted-max/update_controller.py')
        fixture = load_file('retry_protocol', ROOT/'tests/test_control_update_consumer.py').ProtocolTests()
        fixture.setUp()
        self.response = fixture.response()
        self.config = dict(fixture.config, project='owned', controller_mounts=[])
        self.body = {'version':'2.6.5','backup_first':True,'release_id':'r1',
                     'manifest_sha256':self.response['offer']['manifest_sha256']}
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.job = '1'*32
        self.folder = self.root/'jobs'/self.job
        self.folder.mkdir(parents=True)
        self.helper_id = 'a'*64
        self.helper = {'Id':self.helper_id,'Config':{'Labels':{'max.update.controller':'owned','max.update.job':self.job}},
                       'State':{'Running':False,'Status':'exited','ExitCode':0}}
        self.old = {}
        for role, char in [('web','b'),('core','c'),('db','d')]:
            self.old[role] = {'Id':char*64,'Image':'sha256:'+char*64,'Name':'/owned_'+role,
                              'Config':{},'HostConfig':{},'Mounts':[], 'State':{'StartedAt':'fixture'}}
        self.calls = []
        def api(method, path, body=None, **kw):
            self.calls.append((method,path))
            if method == 'GET':
                self.assertEqual(path,'/containers/'+self.helper_id+'/json')
                return copy.deepcopy(self.helper)
            self.assertTrue(self.receipt.exists())
            self.assertNotEqual(json.loads(self.receipt.read_text())['job'],self.job)
            if '/create?' in path: return {'Id':'e'*64}
        self.api = api
        self.controller = self.module.Controller(self.root, api)
        self.receipt = self.root/('release-'+hashlib.sha256(b'r1').hexdigest()+'.private.json')
        self.write(self.receipt, {'job':self.job,'release':'r1'})
        self.write(self.root/'current.private.json', {'job':self.job})
        self.write(self.folder/'state.json', {'status':'error','percent':0,'job':self.job,'target_version':'2.6.5',
                                            'stage':'توقف التجهيز دون تبديل الخدمات'})
        self.write(self.folder/'request.private.json', {'current':'2.6.4','response':self.response})
        self.write(self.folder/'helper.private.json', {'id':self.helper_id})
        self.write(self.folder/'failure.private.json', {'type':'UpdateError'})
        (self.folder/'PREPARATION_ATTEMPTED').write_text('legacy marker')

    def tearDown(self):
        self.temp.cleanup()

    def write(self, path, value):
        path.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')

    def change_state(self, **values):
        path=self.folder/'state.json';state=json.loads(path.read_text());state.update(values);self.write(path,state)

    def submit(self):
        with patch.object(self.controller,'config',return_value=self.config), \
             patch.object(self.controller,'current',return_value=('2.6.4',self.old)), \
             patch.object(self.module,'fetch',return_value=self.response):
            return self.controller.submit(self.body)

    def test_legacy_failure_retries_from_normal_apply_without_erasing_history(self):
        frozen={p.name:p.read_bytes() for p in self.folder.iterdir()}
        result=self.submit();self.assertTrue(result['success'])
        self.assertNotEqual(result['job'],self.job)
        self.assertEqual(frozen,{p.name:p.read_bytes() for p in self.folder.iterdir()})
        review=json.loads((self.root/'jobs'/result['job']/'retry-review.private.json').read_text())
        self.assertEqual(review['previous_receipt'],{'job':self.job,'release':'r1'})
        self.assertEqual(review['next_job'],result['job'])
        self.assertEqual(json.loads(self.receipt.read_text())['job'],result['job'])
        writes=[c for c in self.calls if c[0]=='POST']
        self.assertEqual(len(writes),2)
        self.assertIn('/containers/create?',writes[0][1])
        self.assertIn('/start',writes[1][1])

    def test_second_click_does_not_dispatch_again(self):
        self.assertTrue(self.submit()['success']);calls=len(self.calls)
        self.assertFalse(self.submit()['success']);self.assertEqual(len(self.calls),calls)

    def test_completed_and_unknown_states_are_not_retried(self):
        for status in ('completed','unknown','running','queued','recovery_required','rolled_back'):
            with self.subTest(status=status):
                self.change_state(status=status);self.assertFalse(self.submit()['success'])
        self.assertFalse(any(m=='POST' for m,p in self.calls))

    def test_any_switch_or_uncertain_create_remains_blocked(self):
        for values in ({'switch_attempted':True},{'services':{'web':{'touched':True}}},
                       {'new_id':'f'*64},{'create_attempted':True}):
            with self.subTest(values=values):
                self.change_state(switch_attempted=False,services={},new_id=None,create_attempted=False)
                self.change_state(**values);self.assertFalse(self.submit()['success'])

    def test_active_failed_or_foreign_helper_is_not_retried(self):
        original=copy.deepcopy(self.helper)
        changes=[('running',True),('exit',1),('project','foreign'),('job','2'*32),('paused','paused')]
        for key,value in changes:
            with self.subTest(key=key):
                self.helper=copy.deepcopy(original)
                if key=='running':self.helper['State']['Running']=value
                elif key=='exit':self.helper['State']['ExitCode']=value
                elif key=='paused':self.helper['State']['Status']=value
                else:self.helper['Config']['Labels']['max.update.'+('controller' if key=='project' else 'job')]=value
                self.assertFalse(self.submit()['success'])

    def test_missing_or_invalid_journal_is_not_retried(self):
        for name in ('helper.private.json','failure.private.json','request.private.json','PREPARATION_ATTEMPTED'):
            path=self.folder/name;raw=path.read_bytes();path.unlink()
            self.assertFalse(self.submit()['success']);path.write_bytes(raw)
        self.write(self.folder/'failure.private.json',{})
        self.assertFalse(self.submit()['success'])

    def test_unavailable_docker_does_not_release_claim(self):
        def unavailable(*args,**kw):raise self.module.UpdateError('fixture unavailable')
        self.controller.api=unavailable
        before=self.receipt.read_bytes();self.assertFalse(self.submit()['success']);self.assertEqual(before,self.receipt.read_bytes())

    def test_changed_release_offer_is_not_retried(self):
        path=self.folder/'request.private.json';prior=json.loads(path.read_text())
        prior['response']['offer']['manifest_sha256']='f'*64;self.write(path,prior)
        self.assertFalse(self.submit()['success'])

    def test_unknown_error_stage_is_not_retried(self):
        self.change_state(stage='unknown stage');self.assertFalse(self.submit()['success'])

    def test_worker_failure_explains_retry_without_restarting_services(self):
        self.change_state(status='queued');(self.folder/'PREPARATION_ATTEMPTED').unlink()
        with patch.object(self.module,'Docker',return_value=self.api), \
             patch.object(self.module.Controller,'config',return_value=self.config), \
             patch.object(self.module,'fetch',return_value=self.response), \
             patch.object(self.module,'prepare',side_effect=self.module.UpdateError('fixture preflight')):
            self.module.worker(self.root,self.job)
        state=json.loads((self.folder/'state.json').read_text())
        self.assertEqual(state['status'],'error');self.assertIn('زر التحديث',state['message'])
        self.assertEqual(json.loads((self.folder/'failure.private.json').read_text())['phase'],'preparation_before_switch')
        self.assertFalse(any(m=='POST' for m,p in self.calls))
        self.assertTrue(self.submit()['success'])

    def test_interrupted_preparation_stays_blocked(self):
        self.change_state(status='queued')
        with patch.object(self.module,'Docker',return_value=self.api), \
             patch.object(self.module.Controller,'config',return_value=self.config):
            self.module.worker(self.root,self.job)
        self.assertEqual(self.controller.progress()['status'],'recovery_required')
        self.assertFalse(self.submit()['success'])

    def test_symlinked_journal_is_not_retried(self):
        path=self.folder/'failure.private.json';raw=path.read_bytes();path.unlink()
        target=self.root/'other.json';target.write_bytes(raw);path.symlink_to(target)
        self.assertFalse(self.submit()['success'])

    def prepare_plan(self):
        path=self.root/'compose.yml';path.write_text('synthetic compose')
        self.write(self.folder/'plan.private.json', {'project':'owned','old':self.old,'compose_path':str(path),
                   'compose_sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
        self.change_state(stage='توقف التحديث قبل التبديل')

    def test_prepared_but_unswitched_failure_can_retry(self):
        self.prepare_plan();self.assertTrue(self.submit()['success'])

    def test_changed_compose_or_service_identity_blocks_prepared_retry(self):
        self.prepare_plan();path=self.root/'compose.yml';path.write_text('external edit')
        self.assertFalse(self.submit()['success'])
        path.write_text('synthetic compose');self.old['web']['State']['StartedAt']='changed'
        self.assertFalse(self.submit()['success'])

    def test_concurrent_apply_is_exclusive(self):
        import fcntl
        with (self.root/'dispatch.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):self.submit()
        self.assertFalse(any(m=='POST' for m,p in self.calls))

    @unittest.skipUnless(os.environ.get('MAX_RETRY_REAL_HELPER')=='1','explicit isolated Docker fixture')
    def test_actual_docker_worker_must_exit_before_retry(self):
        from services.system_update_runtime import Docker
        docker=Docker();image=os.environ['MAX_RETRY_FIXTURE_IMAGE']
        made=docker('POST','/containers/create',{'Image':image,'Entrypoint':['python','-B','-c'],
                    'Cmd':['import time;time.sleep(1)'],'Labels':{'max.update.controller':'owned','max.update.job':self.job},
                    'HostConfig':{'NetworkMode':'none','ReadonlyRootfs':True,'RestartPolicy':{'Name':'no'}}})
        ident=made['Id']
        try:
            self.helper_id=ident;self.write(self.folder/'helper.private.json',{'id':ident})
            def api(method,path,body=None,**kw):
                if method=='GET':return docker(method,path)
                return self.api(method,path,body,**kw)
            self.controller.api=api;docker('POST','/containers/'+ident+'/start')
            self.assertFalse(self.submit()['success'])
            docker('POST','/containers/'+ident+'/wait',timeout=20)
            self.assertTrue(self.submit()['success'])
            print(json.dumps({'isolated_helper':ident,'verified_finished_before_retry':True}))
        finally:
            if docker('GET','/containers/'+ident+'/json')['State']['Running']:
                docker('POST','/containers/'+ident+'/stop?t=2')


if __name__=='__main__':unittest.main()

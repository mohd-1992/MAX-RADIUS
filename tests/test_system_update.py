"""Isolated updater contract tests. Run on Linux; no Docker/network/DB operations."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
config = types.ModuleType('core.config');config.APP_VERSION='2.6.2';config.STORAGE_DIR='/tmp/max-update-tests'
sys.modules['core.config']=config

def load(name, path):
    spec=importlib.util.spec_from_file_location(name, ROOT/path)
    module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module);return module
r=load('services.system_update_runtime','services/system_update_runtime.py')
u=load('services.system_update_service','services/system_update_service.py')
OLD='a'*64;NEW='b'*64;IMAGE='sha256:'+'c'*64;PIN='mohd777/max-radius-web@sha256:'+'d'*64

def original():
    return {'Id':OLD,'Name':'/owned_web','Image':'sha256:'+'e'*64,
            'State':{'Running':True,'StartedAt':'original-start','Health':{'Status':'healthy'}},
            'Config':{'Hostname':OLD[:12],'Env':['HOSTNAME='+OLD[:12],'DB_PASSWORD=fixture-only'],
                      'Labels':{'com.docker.compose.project':'owned','com.docker.compose.service':'wisp-web'},'Entrypoint':['sleep'],'Cmd':['600']},
            'HostConfig':{'ReadonlyRootfs':True,'RestartPolicy':{'Name':'unless-stopped'},'Tmpfs':{'/tmp':'rw,size=512m'},'Binds':['/owned/storage:/app/storage']},
            'Mounts':[{'Type':'bind','Source':'/owned/storage','Destination':'/app/storage','RW':True}],
            'NetworkSettings':{'Networks':{'owned_net':{'IPAddress':'172.31.1.2','Aliases':['owned_web',OLD[:12]]}}}}

class Fake:
    def __init__(self):self.rows={OLD:original()};self.calls=[];self.fail=None;self.candidate_health='healthy';self.lose_create=False
    def __call__(self, method,path,body=None,timeout=30):
        self.calls.append((method,path,copy.deepcopy(body)))
        if self.fail and self.fail in path:raise r.UpdateError('injected')
        if path=='/containers/json?all=1':return [{'Id':x['Id'],'Names':[x['Name']]} for x in self.rows.values()]
        if path.startswith('/images/create'):return None
        if path.startswith('/images/'):return {'Id':IMAGE,'RepoDigests':[PIN]}
        if path.startswith('/containers/create?'):
            name='/'+path.split('name=')[1]
            if name.startswith('/max_web_update_helper_'):return {'Id':'f'*64}
            self.rows[NEW]={'Id':NEW,'Name':name,'Image':body['Image'],'Config':copy.deepcopy(body),'HostConfig':body['HostConfig'],
                           'State':{'Running':False,'StartedAt':'new','Health':{'Status':self.candidate_health}},
                           'NetworkSettings':{'Networks':{'owned_net':{'IPAddress':'172.31.1.2'}}}}
            if self.lose_create:raise r.UpdateError('lost create response')
            return {'Id':NEW}
        if path.startswith('/networks/'):
            ident=body['Container']
            if '/disconnect' in path:self.rows[ident]['NetworkSettings']['Networks']={}
            else:self.rows[ident]['NetworkSettings']['Networks']={'owned_net':{'IPAddress':'172.31.1.2'}}
            return None
        if path.startswith('/containers/'):
            ident=path.split('/')[2]
            if ident=='f'*64:return None
            row=self.rows[ident]
            if path.endswith('/json'):return copy.deepcopy(row)
            if '/update' in path:row['HostConfig']['RestartPolicy']=body['RestartPolicy']
            elif '/stop?' in path:row['State']['Running']=False
            elif '/start' in path:row['State']['Running']=True
            elif '/rename?' in path:row['Name']='/'+path.split('name=')[1]
            return None
        raise AssertionError(path)

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.folder=Path(self.t.name);self.api=Fake()
        self.plan={'job':'1'*32,'old':original(),'target_id':IMAGE,'endpoints':r.endpoints_for(original()),'key_hash':'fixture','version':'2.6.3'}
        r.write_json(self.folder/'plan.private.json',self.plan);r.write_json(self.folder/'state.json',{'status':'running'})
    def tearDown(self):self.t.cleanup()
    def runjob(self, good=True):return r.run_job(self.folder,self.api,verify=lambda *args:good,sleep=lambda n:None,clock=iter([0,1,130]).__next__)
    def test_success_requires_verified_runtime(self):
        self.assertEqual(self.runjob()['status'],'completed');self.assertFalse(self.api.rows[OLD]['State']['Running']);self.assertTrue(self.api.rows[NEW]['State']['Running'])
    def test_license_or_integrity_failure_rolls_back(self):
        self.assertEqual(self.runjob(False)['status'],'rolled_back');self.assertTrue(self.api.rows[OLD]['State']['Running']);self.assertEqual(self.api.rows[OLD]['Name'],'/owned_web')
    def test_unhealthy_candidate_rolls_back(self):
        self.api.candidate_health='unhealthy';self.assertEqual(self.runjob()['status'],'rolled_back')
    def test_create_response_loss_discovered_and_preserved(self):
        self.api.lose_create=True;self.assertEqual(self.runjob()['status'],'rolled_back');self.assertFalse(self.api.rows[NEW]['State']['Running'])
    def test_completed_job_cannot_replay(self):
        self.runjob();count=len(self.api.calls);self.runjob();self.assertEqual(len(self.api.calls),count)
    def test_interrupted_switch_restores_without_new_create(self):
        r.write_json(self.folder/'state.json',{'status':'running','switch_attempted':True});self.assertEqual(self.runjob()['status'],'rolled_back');self.assertNotIn(NEW,self.api.rows)
    def test_identity_change_aborts_before_stop(self):
        self.api.rows[OLD]['State']['StartedAt']='unexpected';self.assertEqual(self.runjob()['status'],'error');self.assertFalse(any('/stop?' in x[1] for x in self.api.calls))
    def test_docker_unavailable_never_completed(self):
        self.api.fail='/json';self.assertEqual(self.runjob()['status'],'error')
    def test_mounts_readonly_and_env_preserved_hostname_not_cloned(self):
        x=r.candidate_spec(original(),IMAGE,self.plan['endpoints']);self.assertTrue(x['HostConfig']['ReadonlyRootfs']);self.assertNotIn('Hostname',x);self.assertEqual(x['Env'],['DB_PASSWORD=fixture-only']);self.assertEqual(x['HostConfig']['Mounts'][0]['Source'],'/owned/storage')
    def test_arabic_runtime_status_text(self):
        source=(ROOT/'services/system_update_runtime.py').read_text(encoding='utf-8-sig')
        self.assertIn('اكتمل التحديث بعد التحقق',source)
        self.assertNotIn('ط§ظ„طھ',source)

    def test_scope_requires_exact_current_identity(self):
        with self.assertRaises(r.UpdateError):r.scope(self.api,'other')
    def test_scope_rejects_code_override(self):
        self.api.rows[OLD]['Mounts'].append({'Destination':'/app/services','Type':'bind','RW':False})
        with self.assertRaises(r.UpdateError):r.scope(self.api,OLD[:12])
    def test_scope_rejects_non_web_service(self):
        self.api.rows[OLD]['Config']['Labels']['com.docker.compose.service']='core'
        with self.assertRaises(r.UpdateError):r.scope(self.api,OLD[:12])
    def test_missing_shared_storage_fails(self):
        with self.assertRaises(r.UpdateError):r.storage_mount(original(),'/other')
    def test_only_web_and_owned_networks_mutated(self):
        self.runjob();self.assertFalse(any('compose' in x[1] or 'latest' in x[1] or 'prune' in x[1] for x in self.api.calls));self.assertTrue(all('/containers/' not in x[1] or '/create?' in x[1] or OLD in x[1] or NEW in x[1] for x in self.api.calls))

class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();u.STORAGE_DIR=Path(self.t.name);u._UPDATE_CACHE={'data':None,'timestamp':0}
    def tearDown(self):self.t.cleanup()
    def test_http_catalogue_not_treated_as_current(self):
        with patch.object(u,'DEFAULT_UPDATE_SERVER_URL','http://invalid/updates'):
            x=u.check_for_updates(True);self.assertFalse(x['success']);self.assertFalse(x['has_update'])
    def catalogue(self, data, url='https://trusted/updates'):
        response=types.SimpleNamespace(status=200,read=lambda n:json.dumps(data).encode(),geturl=lambda:url)
        class Context:
            def __enter__(self):return response
            def __exit__(self,*a):pass
        with patch.object(u,'DEFAULT_UPDATE_SERVER_URL','https://trusted/updates'),patch.object(u.urllib.request,'urlopen',return_value=Context()):return u.check_for_updates(True)
    def test_catalogue_requires_pin(self):self.assertFalse(self.catalogue({'version':'2.6.3','docker_image':'mohd777/max-radius-web:latest','update_scope':'web_only'})['success'])
    def test_catalogue_requires_scope(self):self.assertFalse(self.catalogue({'version':'2.6.3','docker_image':PIN})['success'])
    def test_valid_pin_catalogue(self):self.assertTrue(self.catalogue({'version':'2.6.3','docker_image':PIN,'update_scope':'web_only'})['has_update'])
    def test_downgrade_redirect_rejected(self):self.assertFalse(self.catalogue({'version':'2.6.3'},'http://untrusted/')['success'])
    def test_non_linux_dispatch_fails_without_affecting_app_import(self):
        with patch.object(u.os,'name','nt'):
            self.assertFalse(u.trigger_system_update('2.6.3')['success'])

    def test_no_backup_opt_out(self):self.assertFalse(u.trigger_system_update('2.6.3',False)['success'])
    def test_recovery_required_blocks_new_update(self):
        folder=u._root()/('1'*32);folder.mkdir();r.write_json(folder/'state.json',{'status':'recovery_required'});r.write_json(u._root()/'current.json',{'job':folder.name});self.assertFalse(u.trigger_system_update('2.6.3')['success'])
    def test_progress_survives_module_state(self):
        folder=u._root()/('1'*32);folder.mkdir();r.write_json(folder/'state.json',{'status':'completed','percent':100,'private':'secret'});r.write_json(u._root()/'current.json',{'job':folder.name});self.assertEqual(u.get_update_progress()['status'],'completed');self.assertNotIn('private',u.get_update_progress())
    def test_backup_failure_aborts_before_image_pull(self):
        api=Fake();folder=u._root()/('1'*32);folder.mkdir();r.write_json(folder/'state.json',{'status':'running'})
        with patch.object(u,'Docker',return_value=api),patch.object(u.os.environ,'get',return_value=OLD[:12]),patch.object(u,'_verified_backup',side_effect=r.UpdateError('backup fail')):
            u._execute_update_worker('2.6.3',{'docker_image':PIN},folder,'fixture')
        self.assertEqual(json.loads((folder/'state.json').read_text())['status'],'error');self.assertFalse(any('/images/' in x[1] or '/stop?' in x[1] for x in api.calls))
    def test_schema_of_success_is_not_percent_only(self):
        source=(ROOT/'web/templates/tools/system_update.html').read_text();self.assertNotIn("|| pct >= 100",source);self.assertIn("['error', 'rolled_back', 'recovery_required']",source)

if __name__=='__main__':unittest.main()

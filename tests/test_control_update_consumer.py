"""Control signatures, audience isolation, delivery and failure recovery; no live systems."""
import base64
import copy
import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from services import system_update_channel as c
from services import system_update_control as bridge
from services import system_update_executor as executor
from services import system_update_runtime as runtime

ROOT=Path(__file__).resolve().parents[1]


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.key=Ed25519PrivateKey.generate();public=self.key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
        self.config={'kind':'domain','reader_id':'target1','identity':'new.example.test','token':'fixture-only',
                     'public_key':base64.b64encode(public).decode(),'key_fingerprint':hashlib.sha256(public).hexdigest()}
        self.now=dt.datetime.now(dt.timezone.utc)
        self.manifest={'schema':1,'version':'2.6.5','title':'تحديث','changelog':['تجربة'],'release':'r1','published':self.now.isoformat(),
            'min_client_version':'2.6.4','images':{'web':'mohd777/max-radius-web@sha256:'+'a'*64},'components':['web'],
            'audience':{'kind':'domain','id':'target1','identity':'new.example.test'},'migration':'none','update_scope':'web_only',
            'automatic_install':False,'database_image_change':False,'shared_services_change':False}
    def response(self):
        m=copy.deepcopy(self.manifest)
        offer={'reader':self.config['reader_id'] if self.config['kind']!='public' else 'public',
               'release':m['release'],'manifest_sha256':hashlib.sha256(c.canonical(m)).hexdigest(),
               'current_version':'2.6.4','issued_at':self.now.isoformat(),'valid_until':(self.now+dt.timedelta(minutes=5)).isoformat()}
        return {'success':True,'has_update':True,'current_version':'2.6.4','manifest':m,'offer':offer,
                'key_fingerprint':self.config['key_fingerprint'],
                'signature':base64.b64encode(self.key.sign(c.canonical(m))).decode(),
                'offer_signature':base64.b64encode(self.key.sign(c.canonical(offer))).decode()}
    def verify(self,r=None,**kw):return c.verify_response(r or self.response(),self.config,'2.6.4',now=self.now,**kw)
    def test_valid_targeted_release(self):self.assertEqual(self.verify()['version'],'2.6.5')
    def test_valid_public_release_for_enrolled_reader(self):self.manifest['audience']={'kind':'public'};self.assertTrue(self.verify())
    def test_valid_local_release(self):
        self.config.update(kind='local',identity='installation-uuid');self.manifest['audience'].update(kind='local',identity='installation-uuid');self.assertTrue(self.verify())
    def test_public_reader_cannot_receive_domain_release(self):
        self.config['kind']='public'
        with self.assertRaises(c.ChannelError):self.verify()
    def test_tampering_rejected(self):
        r=self.response();r['manifest']['title']='tampered'
        with self.assertRaises(Exception):self.verify(r)
    def test_other_audience_rejected_even_if_signed(self):
        self.manifest['audience']['identity']='other.example.test'
        with self.assertRaises(c.ChannelError):self.verify()
    def test_other_reader_offer_rejected(self):
        r=self.response();r['offer']['reader']='other';r['offer_signature']=base64.b64encode(self.key.sign(c.canonical(r['offer']))).decode()
        with self.assertRaises(c.ChannelError):self.verify(r)
    def test_expiry_and_future_clock_rejected(self):
        for offset in (-301,1):
            self.now+=dt.timedelta(seconds=offset);r=self.response()
            with self.assertRaises(c.ChannelError):c.verify_response(r,self.config,'2.6.4',now=self.now-dt.timedelta(seconds=offset))
            self.now-=dt.timedelta(seconds=offset)
    def test_downgrade_and_minimum_rejected(self):
        for field,value in [('version','2.6.4'),('version','2.6.3'),('min_client_version','2.6.5')]:
            old=self.manifest[field];self.manifest[field]=value
            with self.assertRaises(c.ChannelError):self.verify()
            self.manifest[field]=old
    def test_unpinned_or_foreign_repository_rejected(self):
        for image in ('mohd777/max-radius-web:latest','foreign/web@sha256:'+'a'*64):
            self.manifest['images']['web']=image
            with self.assertRaises(c.ChannelError):self.verify()
    def test_core_bundle_supported(self):
        self.manifest['images']['core']='mohd777/max-radius-core@sha256:'+'b'*64
        self.manifest.update(components=['core','web'],update_scope='web_core');self.assertTrue(self.verify())
    def test_db_shared_and_explicit_migration_rejected(self):
        for field,value in [('database_image_change',True),('shared_services_change',True),('automatic_install',True),('migration','explicit')]:
            old=self.manifest[field];self.manifest[field]=value
            with self.assertRaises(c.ChannelError):self.verify()
            self.manifest[field]=old
    def test_manifest_offer_binding_rejected(self):
        r=self.response();r['offer']['manifest_sha256']='b'*64;r['offer_signature']=base64.b64encode(self.key.sign(c.canonical(r['offer']))).decode()
        with self.assertRaises(c.ChannelError):self.verify(r)
    def test_key_rotation_is_not_automatic(self):
        r=self.response();r['key_fingerprint']='b'*64
        with self.assertRaises(c.ChannelError):self.verify(r)
    def test_public_result_does_not_leak_credential_or_signed_private_payload(self):
        result=c.public_result(self.response(),self.config,'2.6.4')
        self.assertNotIn('token',json.dumps(result));self.assertNotIn('signature',result);self.assertEqual(result['release_id'],'r1')
    def test_no_update_and_invalid_response_distinct(self):
        self.assertIsNone(self.verify({'success':True,'has_update':False,'current_version':'2.6.4'}))
        with self.assertRaises(c.ChannelError):self.verify({'success':False,'has_update':False,'current_version':'2.6.4'})
    def test_missing_configuration_never_enrolls_or_generates_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(FileNotFoundError):c.configuration(folder)
            self.assertEqual(list(Path(folder).iterdir()),[])
    def test_redirect_never_forwards_bearer(self):
        with self.assertRaises(c.ChannelError):c.NoRedirect().redirect_request(None,None,302,'',{},'https://foreign.test')
    def test_bridge_unavailable_does_not_report_updated(self):
        with patch.object(bridge,'request',side_effect=OSError()):
            self.assertFalse(bridge.check('2.6.4')['success']);self.assertEqual(bridge.progress()['status'],'unavailable')
    def test_bridge_rejects_missing_authorization_and_backup_optout(self):
        with patch.object(bridge,'authorized',return_value=False),patch.object(bridge,'request',side_effect=AssertionError()):
            self.assertFalse(bridge.apply('2.6.5',True,'r1','a'*64)['success'])
        with patch.object(bridge,'authorized',return_value=True),patch.object(bridge,'request',side_effect=AssertionError()):
            self.assertFalse(bridge.apply('2.6.5',False,'r1','a'*64)['success'])


class ProvisionTests(unittest.TestCase):
    def test_new_creation_hook_preserves_existing_services_and_blocks_replay(self):
        spec=importlib.util.spec_from_file_location('provision',ROOT/'deploy/hosted-max/provision_update_controller.py');p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);tenant=root/'tenant';tenant.mkdir()
            old={'services':{'web':{'image':'mohd777/max-radius-web@sha256:'+'a'*64,'environment':{},'volumes':['./storage:/app/storage']},'radius_core':{'image':'core'},'db':{'image':'db'}}}
            path=tenant/'docker-compose.yml';path.write_text(json.dumps(old))
            p.provision(tenant,'newtenant',root/'private',root/'socket')
            import yaml
            result=yaml.safe_load(path.read_text());self.assertEqual(result['services']['db'],old['services']['db']);self.assertEqual(result['services']['radius_core'],old['services']['radius_core'])
            config=json.loads((root/'private/update-channel.private.json').read_text());self.assertEqual(config['kind'],'public');self.assertNotIn('token',config)
            self.assertIn('update_controller',result['services']);self.assertNotIn('/var/run/docker.sock',result['services']['web']['volumes'][-1])
            with self.assertRaises(ValueError):p.provision(tenant,'newtenant',root/'private',root/'socket')


class ControllerTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('controller',ROOT/'deploy/hosted-max/update_controller.py');self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
        self.t=tempfile.TemporaryDirectory();self.root=Path(self.t.name)
        fixture=ProtocolTests();fixture.setUp();self.config=dict(fixture.config,project='owned',controller_mounts=[])
        self.response=fixture.response();self.body={'version':'2.6.5','backup_first':True,'release_id':'r1','manifest_sha256':self.response['offer']['manifest_sha256']}
        self.calls=[]
        def api(method,path,body=None):
            self.calls.append((method,path))
            self.assertTrue((self.root/'current.private.json').exists())
            if '/create?' in path:return {'Id':'a'*64}
        self.controller=self.module.Controller(self.root,api)
    def tearDown(self):self.t.cleanup()
    def submit(self,body=None):
        with patch.object(self.controller,'config',return_value=self.config),patch.object(self.controller,'current',return_value=('2.6.4',{'web':{'Image':'sha256:'+'b'*64}})),patch.object(self.module,'fetch',return_value=self.response):
            return self.controller.submit(body or self.body)
    def test_claim_precedes_dispatch_and_repeated_click_is_not_redispatched(self):
        self.assertTrue(self.submit()['success']);n=len(self.calls);self.assertFalse(self.submit()['success']);self.assertEqual(len(self.calls),n)
    def test_unknown_dispatch_remains_claimed(self):
        def fail(*args,**kw):raise TimeoutError()
        self.controller.api=fail
        with self.assertRaises(TimeoutError):self.submit()
        self.assertEqual(self.controller.progress()['status'],'unknown');self.assertFalse(self.submit()['success'])
    def test_request_cannot_supply_images_or_project(self):
        for key,value in [('images',{}),('project','foreign')]:
            with self.assertRaises(ValueError):self.submit(dict(self.body,**{key:value}))
        self.assertEqual(self.calls,[])
    def test_changed_release_requires_review_without_claim(self):
        with self.assertRaises(ValueError):self.submit(dict(self.body,manifest_sha256='f'*64))
        self.assertFalse((self.root/'current.private.json').exists())
    def test_backup_failure_never_pulls_or_switches(self):
        original=executor.execute
        calls=[]
        def execute(*args,**kw):
            calls.append(args[2])
            if len(calls)==1:return {'version':'2.6.4','verified':True,'license':True,'lease':True,'publisher_key':'fixture','rows':[]}
            raise runtime.UpdateError('backup failed')
        def api(*args,**kw):raise AssertionError('Docker image pull or switch must not run')
        with patch.object(executor,'owned_snapshot',return_value={'web':{'Id':'a'*64}}),patch.object(executor,'execute',side_effect=execute):
            with self.assertRaises(runtime.UpdateError):executor.prepare(api,self.config,self.response,'2.6.4',self.root)
        self.assertFalse((self.root/'plan.private.json').exists())


class EngineTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('old_tests',ROOT/'tests/test_system_update.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        self.api=m.Fake();self.fake_module=m
        self.old=m.original();self.db=copy.deepcopy(self.old);self.db.update(Id='7'*64,Name='/owned_db');self.db['Config']['Labels']['com.docker.compose.service']='db'
        self.api.rows[self.db['Id']]=self.db
        self.t=tempfile.TemporaryDirectory();self.folder=Path(self.t.name)/('1'*32);self.folder.mkdir()
        compose=self.folder/'compose.yml';compose.write_text(json.dumps({'services':{'wisp-web':{'image':'old'},'core':{'image':'oldcore'},'db':{'image':'db'}}}))
        (self.folder/'compose-before.private').write_bytes(compose.read_bytes())
        self.plan={'job':'1'*32,'old':{'web':self.old,'db':self.db},'images':{'web':m.IMAGE},'references':{'web':m.PIN},
                   'version':'2.6.5','previous_version':'2.6.4','publisher_key':'fixture','accounting':[],
                   'compose_path':str(compose),'compose_services':{'web':'wisp-web','core':'core','db':'db'}}
        runtime.write_json(self.folder/'plan.private.json',self.plan);runtime.write_json(self.folder/'state.json',{'status':'queued'})
    def tearDown(self):self.t.cleanup()
    def runjob(self,wait=None):return executor.run_job(self.folder,self.api,wait or (lambda *a,**kw:None))
    def test_success_persists_digest_and_cannot_replay(self):
        self.assertEqual(self.runjob()['status'],'completed');calls=len(self.api.calls)
        self.assertEqual(self.runjob()['status'],'completed');self.assertEqual(len(self.api.calls),calls)
        import yaml
        self.assertEqual(yaml.safe_load(Path(self.plan['compose_path']).read_text())['services']['wisp-web']['image'],self.fake_module.PIN)
    def test_candidate_failure_requires_verified_rollback(self):
        def wait(*a,**kw):
            if not kw.get('rollback'):raise runtime.UpdateError('candidate failed')
        self.assertEqual(self.runjob(wait)['status'],'rolled_back');self.assertTrue(self.api.rows[self.old['Id']]['State']['Running'])
    def test_failed_rollback_probe_is_recovery_required(self):
        def wait(*a,**kw):raise runtime.UpdateError('not healthy')
        self.assertEqual(self.runjob(wait)['status'],'recovery_required')
    def test_lost_create_reply_resolved_without_second_create(self):
        self.api.lose_create=True
        self.assertEqual(self.runjob()['status'],'rolled_back')
        self.assertEqual(sum('/containers/create?' in v[1] for v in self.api.calls),1)
    def test_ownership_change_aborts_before_stop(self):
        self.api.rows[self.old['Id']]['Image']='changed'
        self.assertEqual(self.runjob()['status'],'error');self.assertFalse(any('/stop?' in v[1] for v in self.api.calls))
    def test_external_compose_change_never_overwritten(self):
        Path(self.plan['compose_path']).write_text('foreign change')
        self.assertEqual(self.runjob()['status'],'recovery_required');self.assertEqual(Path(self.plan['compose_path']).read_text(),'foreign change')


if __name__=='__main__':unittest.main()

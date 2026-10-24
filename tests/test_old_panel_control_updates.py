"""Explicit-button routing and lazy enrollment; no production calls."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'deploy/hosted-max'))
import panel_updates as panel
from services import system_update_executor as executor
from services import system_update_runtime as runtime


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.root=Path(self.t.name)
        self.tenant=self.root/'tenants'/'fixture';(self.tenant/'storage').mkdir(parents=True)
        (self.tenant/'tenant.json').write_text(json.dumps({'subdomain':'fixture','instance_uuid':'INST-1234567890'}))
        (self.tenant/'storage'/'instance_id').write_text('INST-1234567890')
        self.compose=self.tenant/'docker-compose.yml';self.compose.write_text('unchanged compose')
        self.cfg={'tenant_root':str(self.tenant.parent),'state_root':str(self.root/'private'),
                  'socket_root':str(self.root/'sockets'),'base_domain':'example.test','publisher_key_sha256':'test-publisher'}
        self.preflight=patch.object(panel.bootstrap,'preflight_new_domain_updates',return_value=self.cfg);self.preflight.start()
        self.state=self.root/'private/fixture';self.sock=self.root/'sockets/fixture'
        self.reader={'kind':'domain','identity':'fixture.example.test','reader_id':'test-reader','token':'test-secret-do-not-log'}
        self.config=dict(self.reader,project='fixture',executor='host',channel='https://control.max-net.net',
                         public_key=panel.PUBLIC_KEY,key_fingerprint=panel.bootstrap.PUBLIC_TRUST)
    def tearDown(self):self.preflight.stop();self.t.cleanup()
    def existing(self,managed=False):
        self.state.mkdir(parents=True);self.sock.mkdir(parents=True)
        (self.state/'update-channel.private.json').write_text(json.dumps(self.config))
        if managed:(self.state/'panel-controller.private.json').write_text(json.dumps({'status':'ready','instance':'INST-1234567890'}))
    def prepare(self,command=None,trust=None):
        with patch.object(executor,'owned_snapshot',return_value={'web':{'Id':'a'*64,'Mounts':[]}}),\
             patch.object(executor,'execute',return_value=trust or {'version':'2.6.3','verified':True,'key':'test-publisher'}),\
             patch.object(panel,'command',side_effect=command or (lambda *a,**kw:json.dumps(self.reader))):
            return panel.ensure_controller('fixture')
    def test_context_rejects_foreign_path_and_changed_installation(self):
        for name in ('../fixture','Fixture','a/b'):
            with self.assertRaises(panel.PanelUpdateError):panel.context(name)
        (self.tenant/'storage/instance_id').write_text('INST-OTHER00000')
        with self.assertRaises(panel.PanelUpdateError):panel.context('fixture')
    def test_native_new_installation_reuses_controller_without_enrollment_or_systemctl(self):
        self.existing()
        with patch.object(panel,'command',side_effect=AssertionError('Must not mutate native installation')):
            root,socket=panel.ensure_controller('fixture')
        self.assertEqual(root,self.state);self.assertEqual(socket,self.sock/'controller.sock')
    def test_legacy_installation_prepares_host_only_without_compose_or_web_changes(self):
        calls=[]
        def command(args,**kw):calls.append(args);return json.dumps(self.reader)
        self.prepare(command)
        self.assertEqual(self.compose.read_text(),'unchanged compose')
        self.assertEqual(len(calls),2);self.assertEqual(calls[-1][0],'systemctl')
        saved=json.loads((self.state/'update-channel.private.json').read_text())
        self.assertEqual(saved['reader_id'],'test-reader');self.assertEqual(saved['enrollment_socket'],str(self.sock))
        self.assertNotIn('test-secret',(self.state/'panel-controller.private.json').read_text())
    def test_untrusted_legacy_runtime_does_not_register_or_start_services(self):
        with self.assertRaises(panel.PanelUpdateError):
            self.prepare(command=lambda *a,**k:self.fail('must not dispatch'),trust={'version':'2.6.3','verified':False,'key':'foreign'})
        self.assertFalse(self.state.exists())
    def test_uncertain_enrollment_is_never_replayed(self):
        def failed(*a,**kw):raise RuntimeError(self.reader['token'])
        with self.assertRaises(panel.PanelUpdateError) as error:self.prepare(failed)
        self.assertNotIn(self.reader['token'],str(error.exception))
        with patch.object(panel,'command',side_effect=AssertionError('No replay')):
            with self.assertRaises(panel.PanelUpdateError):self.prepare()
    def test_partial_configuration_blocks_automatic_restart(self):
        self.existing(managed=True)
        (self.state/'panel-controller.private.json').write_text(json.dumps({'status':'review_required','instance':'INST-1234567890'}))
        with patch.object(panel,'command',side_effect=AssertionError('No restart')):
            with self.assertRaises(panel.PanelUpdateError):panel.ensure_controller('fixture')
    def run_update(self,responses):
        with patch.object(panel,'ensure_controller',return_value=(self.state,self.sock/'controller.sock')),\
             patch.object(panel,'wait_socket'),patch.object(panel,'request',side_effect=responses) as request,\
             patch.object(panel.time,'sleep'):
            result=panel.update_tenant('fixture')
            return result,request.call_args_list
    def test_no_published_release_never_applies_or_falls_back_to_latest(self):
        result,calls=self.run_update([{'status':'idle'},{'success':True,'has_update':False}])
        self.assertEqual([c.args[1] for c in calls],['progress','check']);self.assertIn('لم يبدأ',result)
    def test_unavailable_channel_is_error_not_up_to_date(self):
        with self.assertRaises(panel.PanelUpdateError):self.run_update([{'status':'idle'},{'success':False,'has_update':False}])
    def test_ambiguous_existing_job_blocks_dispatch(self):
        for status in ['queued','running','unknown','recovery_required']:
            with self.assertRaises(panel.PanelUpdateError):self.run_update([{'status':status}])
    def good_responses(self,terminal='completed'):
        return [{'status':'idle'},{'success':True,'has_update':True,'latest_version':'2.6.5','release_id':'signed-release','manifest_sha256':'a'*64},
                {'success':True,'job':'1'*32},{'status':terminal,'job':'1'*32}]
    def test_explicit_button_uses_manifest_and_mandatory_backup_then_waits_for_completion(self):
        result,calls=self.run_update(self.good_responses());body=calls[2].args[2]
        self.assertEqual(body,{'version':'2.6.5','release_id':'signed-release','manifest_sha256':'a'*64,'backup_first':True})
        self.assertIn('اكتمل',result)
    def test_rollback_error_and_unknown_are_not_reported_as_success(self):
        for state in ['rolled_back','error','unknown','recovery_required']:
            with self.assertRaises(panel.PanelUpdateError):self.run_update(self.good_responses(state))
    def test_changed_job_does_not_follow_another_installation(self):
        replies=self.good_responses();replies[-1]['job']='2'*32
        with self.assertRaises(panel.PanelUpdateError):self.run_update(replies)
    def test_legacy_rollback_cannot_replay_an_old_checkpoint_after_enrollment(self):
        self.existing()
        with self.assertRaises(panel.PanelUpdateError):panel.guard_legacy_rollback('fixture')
    def test_command_errors_do_not_expose_credentials(self):
        import types
        with patch.object(panel.subprocess,'run',return_value=types.SimpleNamespace(returncode=1,stdout='secret',stderr='secret')):
            with self.assertRaises(panel.PanelUpdateError) as e:panel.command(['fixture'])
        self.assertNotIn('secret',str(e.exception))


class EnrollmentEngineTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('engine_fixture',ROOT/'tests/test_control_update_consumer.py')
        m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        self.case=m.EngineTests();self.case.setUp()
        self.socket=self.case.folder/'socket';self.socket.mkdir()
        self.case.plan['enrollment_socket']=str(self.socket)
        runtime.write_json(self.case.folder/'plan.private.json',self.case.plan)
    def tearDown(self):self.case.tearDown()
    def test_enrollment_mount_only_added_to_candidate_and_persisted_after_success(self):
        self.assertEqual(self.case.runjob()['status'],'completed')
        import yaml
        web=yaml.safe_load(Path(self.case.plan['compose_path']).read_text())['services']['wisp-web']
        self.assertEqual(web['environment']['MAX_UPDATE_SOCKET'],'/max-update-socket/controller.sock')
        self.assertIn(str(self.socket)+':/max-update-socket:ro',web['volumes'])
        self.assertFalse(any(m['Destination']=='/max-update-socket' for m in self.case.old['Mounts']))
    def test_failed_candidate_restores_original_compose_without_enrollment(self):
        before=Path(self.case.plan['compose_path']).read_bytes()
        def fail(*args,**kw):
            if not kw.get('rollback'):raise runtime.UpdateError('fixture failure')
        self.assertEqual(self.case.runjob(fail)['status'],'rolled_back')
        self.assertEqual(Path(self.case.plan['compose_path']).read_bytes(),before)
    def test_conflicting_socket_mount_is_rejected_before_switch(self):
        self.case.old['Mounts'].append({'Type':'bind','Source':'/foreign','Destination':'/max-update-socket','RW':False})
        self.case.api.rows[self.case.old['Id']]=copy.deepcopy(self.case.old)
        runtime.write_json(self.case.folder/'plan.private.json',self.case.plan)
        self.assertEqual(self.case.runjob()['status'],'error')
        self.assertFalse(any('/stop?' in v[1] for v in self.case.api.calls))


@unittest.skipUnless(os.environ.get('MAX_CONTROL_FIXTURE_SOURCE'),'Frozen control fixture source is required')
class RegistrationTests(unittest.TestCase):
    def test_official_target_registration_is_encrypted_reused_and_inactive_target_rejected(self):
        import sqlite3
        import types
        from cryptography.fernet import Fernet
        sys.path.insert(0,os.environ['MAX_CONTROL_FIXTURE_SOURCE'])
        from platform_store import Store
        from release_center import ReleaseCenter
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);vault=Fernet.generate_key();(root/'vault.key').write_bytes(vault)
            store=Store.__new__(Store);store.root=root;store.path=root/'platform.sqlite3';store.vault=Fernet(vault)
            with sqlite3.connect(store.path) as db:
                db.execute('CREATE TABLE receipts(actor TEXT,key TEXT,fingerprint TEXT,result TEXT,PRIMARY KEY(actor,key))')
                db.execute('CREATE TABLE audit(id INTEGER PRIMARY KEY,created TEXT DEFAULT CURRENT_TIMESTAMP,actor TEXT NOT NULL,action TEXT NOT NULL,target TEXT NOT NULL,detail TEXT NOT NULL)')
            ReleaseCenter(store,types.SimpleNamespace())
            cfg={'control_backend':os.environ['MAX_CONTROL_FIXTURE_SOURCE'],'control_store':str(root),'base_domain':'example.test'}
            with patch.object(panel,'context',return_value=(cfg,root,'INST-1234567890')):
                one=panel.enrollment('fixture');two=panel.enrollment('fixture')
                self.assertEqual(one,two)
                with sqlite3.connect(store.path) as db:
                    row=db.execute('SELECT secret FROM update_targets').fetchone()
                    self.assertNotIn(one['token'],row[0]);self.assertEqual(store.decrypt(row[0]),one['token'])
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM update_targets').fetchone()[0],1)
                    db.execute('UPDATE update_targets SET active=0')
                with self.assertRaises(panel.PanelUpdateError):panel.enrollment('fixture')


class WorkerDispatchTests(unittest.TestCase):
    def test_host_worker_uses_fixed_command_and_bounded_recovery_without_docker_helper(self):
        import shutil
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'update_support').mkdir()
            shutil.copy(ROOT/'deploy/hosted-max/update_controller.py',root/'update_support/update_controller.py')
            shutil.copy(ROOT/'deploy/hosted-max/panel_update_controller.py',root/'panel_update_controller.py')
            spec=importlib.util.spec_from_file_location('dispatch_fixture',root/'panel_update_controller.py')
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            controller=module.Controller(root,lambda *a:self.fail('No Docker helper for old images'))
            with patch.object(panel,'command',return_value='') as command:
                controller.launch({'project':'fixture'},{},root,'1'*32)
            args=command.call_args.args[0]
            self.assertEqual(args[0],'systemd-run');self.assertIn('--property=Restart=on-failure',args)
            self.assertEqual(args[-3:],['work','fixture','1'*32])

    @unittest.skipIf(os.name=='nt','Real Unix socket test runs on Linux')
    def test_real_unix_bridge_routes_explicit_button_to_signed_offer_and_claimed_job(self):
        import multiprocessing
        spec=importlib.util.spec_from_file_location('protocol_fixture',ROOT/'tests/test_control_update_consumer.py')
        fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
        protocol=fixture.ProtocolTests();protocol.setUp()
        spec=importlib.util.spec_from_file_location('real_socket_fixture',ROOT/'deploy/hosted-max/update_controller.py')
        host=importlib.util.module_from_spec(spec);spec.loader.exec_module(host)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);sock=root/'controller.sock'
            cfg=dict(protocol.config,project='owned',executor='host',channel='https://control.max-net.net',compose_path=str(root/'compose.yml'))
            (root/'update-channel.private.json').write_text(json.dumps(cfg))
            class Controller(host.Controller):
                def current(self,config):return '2.6.4',{}
                def launch(self,config,old,folder,job):
                    self.assert_claim=(root/'current.private.json').exists()
                    assert self.assert_claim
                    runtime.write_json(folder/'state.json',{'job':job,'status':'completed','percent':100})
            controller=Controller(root,lambda *a:None)
            with patch.object(host,'fetch',return_value=protocol.response()):
                process=multiprocessing.get_context('fork').Process(target=host.serve,args=(controller,sock))
                process.start()
            try:
                panel.wait_socket(sock)
                with patch.object(panel,'ensure_controller',return_value=(root,sock)):
                    self.assertIn('اكتمل',panel.update_tenant('owned'))
                receipts=list(root.glob('release-*.private.json'));self.assertEqual(len(receipts),1)
                self.assertEqual(controller.progress()['status'],'completed')
            finally:
                process.terminate();process.join(timeout=5)


if __name__=='__main__':unittest.main()

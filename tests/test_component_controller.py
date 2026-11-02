"""Protocol4 local admission/socket/recovery contracts. Fake Docker, real locks/socket."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import socket
import time
import unittest

from tests.test_update_components import ComponentTests as _Fixture
from services import system_component_control as control
from services import system_update_components as engine


class ComponentControlTests(_Fixture):
    def admitted(self):
        value=control.enroll(self.root,self.registry,self.trust,self.api)
        self.assertFalse(value['services_changed'])
        return control.ComponentController(self.root,self.api,self.probe)

    def test_enrollment_once_and_keeps_no_private_key(self):
        controller=self.admitted()
        self.assertEqual(controller.engine()['engine_protocol'],4)
        self.assertEqual(controller.progress(),{'status':'idle'})
        self.assertEqual(set(json.loads((self.root/'component-trust.private.json').read_text())),{'public_key','key_fingerprint'})
        with self.assertRaises(engine.UpdateError):self.admitted()

    def test_enrollment_cannot_change_legacy_trust(self):
        engine.write_json(self.root/'update-channel.private.json',{'public_key':'other','key_fingerprint':'other'})
        with self.assertRaises(engine.UpdateError):self.admitted()

    def test_registry_cas_and_untrusted_http_input(self):
        controller=self.admitted()
        before=copy.deepcopy(self.api.rows)
        for body in ({'root':'/other'},{'registry':self.registry},{'command':'stop'}):
            with self.assertRaises(Exception):controller.submit(body)
        self.assertEqual(before,self.api.rows)
        marker=json.loads((self.root/'component-controller.private.json').read_text());marker['registry_revision']='0'*64
        engine.write_json(self.root/'component-controller.private.json',marker)
        with self.assertRaises(engine.UpdateError):controller.engine()

    def test_installation_rejects_shared_db_enrollment(self):
        for row in self.api.rows.values():
            if row['Config']['Labels']['com.docker.compose.service']=='db':row['Config']['Labels']['max.db.scope']='shared'
        with self.assertRaises(engine.UpdateError):self.admitted()

    def test_success_and_redacted_controller_progress(self):
        controller=self.admitted()
        result=controller.submit(self.envelope(self.bundle()))
        self.assertEqual(result['status'],'completed')
        self.assertEqual(set(controller.progress())-{'job','status','message'},set())

    def interrupted(self, after_start=False):
        self.admitted()
        class Crash(BaseException):pass
        fired=False
        def adapter(method,path,body=None,timeout=30):
            nonlocal fired
            if not fired and method=='POST' and '/stop?' in path and not after_start:
                fired=True;raise Crash()
            value=self.api(method,path,body,timeout)
            if not fired and after_start and method=='POST' and path.endswith('/start'):
                fired=True;raise Crash()
            return value
        adapter.rows=self.api.rows
        with self.assertRaises(Crash):engine.submit(self.root,self.envelope(self.bundle()),adapter,self.probe)
        self.assertTrue(fired)
        return json.loads((self.root/'component-current.private.json').read_text())['job']

    def test_recovery_before_stop_and_after_new_db_start(self):
        # Separate fixtures for each interruption, never replay a finished job.
        job=self.interrupted(after_start=True)
        result=engine.recover(self.root,job,self.api,self.probe)
        self.assertEqual(result['status'],'rolled_back')
        self.assertTrue(result['recovery_attempted'])
        before=copy.deepcopy(self.api.rows)
        self.assertEqual(engine.recover(self.root,job,self.api,self.probe),result)
        self.assertEqual(before,self.api.rows)

    def test_recovery_before_first_stop(self):
        job=self.interrupted()
        self.assertEqual(engine.recover(self.root,job,self.api,self.probe)['status'],'rolled_back')

    def test_startup_reconciliation_restores_without_reapplying(self):
        job=self.interrupted()
        controller=control.ComponentController(self.root,self.api,self.probe)
        self.assertEqual(controller.resume_interrupted()['status'],'rolled_back')
        frozen=copy.deepcopy(self.api.rows)
        self.assertEqual(controller.resume_interrupted()['status'],'rolled_back')
        self.assertEqual(frozen,self.api.rows)

    def test_live_worker_lock_prevents_recovery(self):
        import fcntl
        job=self.interrupted()
        folder=self.root/'component-jobs'/job
        with (folder/'execution.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
            frozen=copy.deepcopy(self.api.rows)
            with self.assertRaises(BlockingIOError):engine.recover(self.root,job,self.api,self.probe)
            self.assertEqual(frozen,self.api.rows)

    def test_interrupted_preparation_never_replays_backup(self):
        self.admitted();job='2'*32;folder=self.root/'component-jobs'/job;folder.mkdir(parents=True)
        engine.write_json(self.root/'component-current.private.json',{'job':job})
        engine.write_json(folder/'state.json',{'job':job,'status':'preparing'})
        count=self.backup_mock.call_count;frozen=copy.deepcopy(self.api.rows)
        self.assertEqual(engine.recover(self.root,job,self.api,self.probe)['status'],'error')
        self.assertEqual(self.backup_mock.call_count,count);self.assertEqual(frozen,self.api.rows)

    def test_legacy_tenant_socket_cannot_adopt_host_authority(self):
        self.registry['authority']='host';self.admitted()
        spec=importlib.util.spec_from_file_location('legacy_scope_controller',Path('deploy/hosted-max/update_controller.py'))
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        controller=module.Controller(self.root,self.api)
        controller.config=lambda: {'project':'alpha'}
        with self.assertRaises(ValueError):controller.components()

    def test_recovery_refuses_signature_tamper_without_mutation(self):
        job=self.interrupted()
        request=self.root/'component-jobs'/job/'request.private.json'
        value=json.loads(request.read_text());value['signature']='AAAA';engine.write_json(request,value)
        before=copy.deepcopy(self.api.rows)
        with self.assertRaises(Exception):engine.recover(self.root,job,self.api,self.probe)
        self.assertEqual(before,self.api.rows)

    def test_real_unix_socket_routes_reject_foreign_body(self):
        controller=self.admitted()
        spec=importlib.util.spec_from_file_location('socket_controller',Path('deploy/hosted-max/update_controller.py'))
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        path=self.root/'controller.sock';pid=os.fork()
        if pid==0:
            try:module.serve(controller,path)
            finally:os._exit(0)
        try:
            deadline=time.monotonic()+5
            while not path.exists() and time.monotonic()<deadline:time.sleep(.01)
            def request(method,route,body=b''):
                with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
                    client.settimeout(5);client.connect(str(path))
                    client.sendall((method+' /_max_updates/v4/components/'+route+' HTTP/1.0\r\nHost: local\r\nContent-Length: '+str(len(body))+'\r\n\r\n').encode()+body)
                    result=b''
                    while True:
                        chunk=client.recv(65536)
                        if not chunk:break
                        result+=chunk
                    return result
            self.assertIn(b'200 OK',request('GET','engine'))
            self.assertIn(b'"engine_protocol": 4',request('GET','engine'))
            self.assertIn(b'503',request('POST','apply',b'{"registry":{}}'))
            self.assertIn(b'403',request('DELETE','engine'))
            self.assertIn(b'"status": "idle"',request('GET','progress'))
        finally:
            import signal
            os.kill(pid,signal.SIGTERM);os.waitpid(pid,0)


if __name__=='__main__':unittest.main()

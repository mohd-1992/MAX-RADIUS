"""A diagnostic result requires a real RADIUS response from a scoped engine."""
import ast,re,types,unittest
from pathlib import Path
from unittest.mock import Mock,patch
class RadiusSimulatorScopeTests(unittest.TestCase):
 def setup_probe(self,hosted=False,ident='a'*64,running=True):
  source=Path('services/radius_sim_service.py');node=next(n for n in ast.parse(source.read_text(encoding='utf-8')).body if isinstance(n,ast.FunctionDef) and n.name=='simulate_radius_auth');node.decorator_list=[];cursor=Mock();cursor.__enter__=Mock(return_value=cursor);cursor.__exit__=Mock(return_value=False);cursor.fetchone.return_value=dict(password='local-secret',groupname='local',sub_status='suspended');cursor.fetchall.return_value=[];db=Mock();db.cursor.return_value=cursor;self.process=Mock(return_value=types.SimpleNamespace(stdout='Received Access-Accept Id 7\nUser-Password = "local-secret"\nSession-Timeout = 60',returncode=0));ns=dict(_get_db=lambda:db,time=types.SimpleNamespace(time=lambda:1.0),subprocess=types.SimpleNamespace(run=self.process,PIPE=-1,STDOUT=-2),re=re);modules={'services':types.ModuleType('services'),'services.hosted_control_service':types.SimpleNamespace(configured=lambda:hosted),'services.system_control_service':types.SimpleNamespace(get_container_info=Mock(return_value=dict(container_id=ident,is_running=running)))};self.modules=patch.dict('sys.modules',modules);self.modules.start();self.addCleanup(self.modules.stop);exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),ns);self.probe=ns[node.name]
 def test_full_owned_id_port_and_redacted_real_accept(self):
  self.setup_probe();r=self.probe('local-user','local-secret',radius_port=1912);cmd=self.process.call_args.args[0];self.assertEqual(cmd[2],'a'*64);self.assertIn('127.0.0.1:1912',cmd);self.assertTrue(r['verified'] and r['is_accept']);self.assertNotIn('local-secret',r['raw_output']);self.assertFalse(any('Password' in a['attr'] for a in r['attributes']))
 def test_missing_owned_engine_never_executes_global_name(self):
  self.setup_probe(ident='');r=self.probe('local-user','local-secret');self.process.assert_not_called();self.assertFalse(r['verified'] or r['is_accept'])
 def test_hosted_without_exec_capability_never_falls_back(self):
  self.setup_probe(hosted=True);r=self.probe('local-user','local-secret');self.process.assert_not_called();self.assertFalse(r['verified'] or r['is_accept'])
 def test_exec_failure_cannot_become_database_accept(self):
  self.setup_probe();self.process.side_effect=RuntimeError('local failure');r=self.probe('local-user','local-secret');self.assertFalse(r['verified'] or r['is_accept']);self.assertEqual(r['status'],'Unavailable / Not Verified')
 def test_sent_text_cannot_fake_received_accept(self):
  self.setup_probe();self.process.return_value=types.SimpleNamespace(stdout='User-Name = "Access-Accept"\nSent Access-Request',returncode=0);r=self.probe('local-user','local-secret');self.assertFalse(r['verified'] or r['is_accept'])
 def test_invalid_response_nonzero_exit_cannot_accept(self):
  self.setup_probe();self.process.return_value=types.SimpleNamespace(stdout='Received Access-Accept Id 7\nInvalid response authenticator',returncode=1);r=self.probe('local-user','local-secret');self.assertFalse(r['verified'] or r['is_accept'])
 def test_actual_reject_remains_verified_reject(self):
  self.setup_probe();self.process.return_value=types.SimpleNamespace(stdout='Received Access-Reject Id 7',returncode=1);r=self.probe('local-user','bad');self.assertTrue(r['verified']);self.assertFalse(r['is_accept'])
if __name__=='__main__':unittest.main()

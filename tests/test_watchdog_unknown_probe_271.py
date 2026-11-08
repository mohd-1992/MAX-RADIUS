"""An unavailable probe must not create an outage or recovery transition."""
import ast,types,unittest
from pathlib import Path
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[1]
class WatchdogUnknownProbeTests(unittest.TestCase):
 def setUp(self):
  self.probe=Mock();self.package=types.ModuleType('services');self.package.__path__=[];self.module=types.ModuleType('services.health_probe_service');self.module.radius_probe=self.probe
  tree=ast.parse((ROOT/'core/watchdog.py').read_text(encoding='utf-8'));node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='check_radius_engine_health');self.env=dict(os=types.SimpleNamespace(environ={}),_last_radius_state=True,log_system_alert=Mock(),execute_write=Mock(),datetime=__import__('datetime'));exec(compile(ast.Module(body=[node],type_ignores=[]),'core/watchdog.py','exec'),self.env)
 def call(self,result):
  self.probe.return_value=(result,'isolated probe')
  with patch.dict('sys.modules',{'services':self.package,'services.health_probe_service':self.module}):return self.env['check_radius_engine_health']()
 def test_unknown_preserves_last_healthy_state_without_false_outage(self):
  self.assertIsNone(self.call(None));self.assertIs(self.env['_last_radius_state'],True);self.env['log_system_alert'].assert_not_called();self.env['execute_write'].assert_not_called()
 def test_unknown_preserves_last_down_state_without_false_recovery(self):
  self.env['_last_radius_state']=False;self.assertIsNone(self.call(None));self.assertIs(self.env['_last_radius_state'],False);self.env['log_system_alert'].assert_not_called();self.env['execute_write'].assert_not_called()
 def test_verified_failure_creates_only_one_outage_transition(self):
  self.assertIs(self.call(False),False);self.assertIs(self.env['_last_radius_state'],False);self.env['log_system_alert'].assert_called_once();self.env['log_system_alert'].reset_mock();self.call(False);self.env['log_system_alert'].assert_not_called()
 def test_verified_success_resolves_only_after_real_response(self):
  self.env['_last_radius_state']=False;self.assertIs(self.call(True),True);self.assertIs(self.env['_last_radius_state'],True);self.env['execute_write'].assert_called_once();self.env['log_system_alert'].assert_called_once()
if __name__=='__main__':unittest.main()

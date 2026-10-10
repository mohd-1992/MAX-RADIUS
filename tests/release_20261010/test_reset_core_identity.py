import ast,types,sys,unittest
from pathlib import Path
from unittest.mock import patch
class Tests(unittest.TestCase):
 def check(self,project='max',service='radius_core',env=None,own_project='max',own_service='web'):
  tree=ast.parse((Path(__file__).resolve().parents[2]/'services/factory_reset_service.py').read_text(encoding='utf8'));node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_owns_core')
  ns={'_docker':lambda *a:{'Config':{'Labels':{'com.docker.compose.project':own_project,'com.docker.compose.service':own_service}}}}
  exec(compile(ast.Module(body=[node],type_ignores=[]),'test','exec'),ns)
  cfg=types.ModuleType('core.config');cfg.DB_NAME='radius_max'
  with patch.dict(sys.modules,{'core.config':cfg}):return ns['_owns_core']({'Config':{'Env':env or [],'Labels':{'com.docker.compose.project':project,'com.docker.compose.service':service}}})
 def test_local_compose_aliases(self):self.assertTrue(self.check(project='max-radius',service='freeradius',own_project='max-radius',own_service='wisp-web',env=['DB_NAME=radius_max']))
 def test_wrong_own_service_rejected(self):self.assertFalse(self.check(own_service='db'))
 def test_redacted_env_same_tenant(self):self.assertTrue(self.check())
 def test_other_tenant_rejected(self):self.assertFalse(self.check(project='other'))
 def test_same_database_other_tenant_rejected(self):self.assertFalse(self.check(project='other',env=['DB_NAME=radius_max']))
 def test_other_service_rejected(self):self.assertFalse(self.check(service='db'))
 def test_missing_self_identity_rejected(self):self.assertFalse(self.check(own_project=''))
 def test_explicit_wrong_database_rejected(self):self.assertFalse(self.check(env=['DB_NAME=radius_other']))
if __name__=='__main__':unittest.main(verbosity=2)

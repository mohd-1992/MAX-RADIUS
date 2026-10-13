import ast,json,logging,unittest
from pathlib import Path
from unittest.mock import MagicMock
ROOT=Path(__file__).resolve().parents[1]
class Tests(unittest.TestCase):
 def fixture(self,error=None,permit=True):
  pkg={'payload':{'license_id':'fixture','client_name':'fixture'},'signature':'fixture'}
  if permit:pkg['enrollment_token']='fixture-permit'
  sent=[];writes=[]
  def exchange(*args):
   sent.append(args[1])
   if error:raise error
   return {'valid':True,'status':'active'}
  ns=dict(DEFAULT_MASTER_SERVER_URL='https://license.max-net.net',ensure_license_tables=lambda:None,clear_license_cache=lambda:None,decode_license_string=lambda _: (pkg,None),get_current_system_counts=lambda:(0,0,0),verify_license_package=lambda *a,**k:(True,'',{}),get_machine_id=lambda:'HW-TEST',get_instance_uuid=lambda:'INST-TEST',APP_VERSION='test',APP_EDITION='test',STORAGE_DIR='fixture',get_master_public_key_pem=lambda:'fixture',authenticated_license_exchange=exchange,record_license_revocation=lambda *a:None,remove_license_revocation=lambda *a:None,get_connection=MagicMock,is_mysql_conn=lambda _:True,execute_write=lambda *a:writes.append(a),query_one=lambda *a:None,json=json,logger=logging.getLogger('test'))
  tree=ast.parse((ROOT/'services/license_guard_service.py').read_text(encoding='utf8'));fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='install_and_activate_license');exec(compile(ast.Module(body=[fn],type_ignores=[]),'guard','exec'),ns);return ns,sent,writes
 def test_capability_is_sent_but_not_stored(self):
  ns,sent,writes=self.fixture();self.assertTrue(ns['install_and_activate_license']('fixture')[0]);self.assertEqual(sent[0]['enrollment_token'],'fixture-permit');stored=[p for _,params in writes for p in params if isinstance(p,str) and p.startswith('{"payload"')];self.assertTrue(stored);self.assertNotIn('enrollment_token',json.loads(stored[0]))
 def test_legacy_enrolled_license_remains_compatible(self):
  ns,sent,writes=self.fixture(permit=False);self.assertTrue(ns['install_and_activate_license']('fixture')[0]);self.assertNotIn('enrollment_token',sent[0])
 def test_all_enrollment_errors_fail_without_license_writes(self):
  for code in ['enrollment_required','enrollment_token_invalid','enrollment_token_expired','enrollment_license_unavailable','enrollment_binding_mismatch','enrollment_blocked','enrollment_instance_limit','enrollment_key_mismatch']:
   with self.subTest(code=code):
    ex=ValueError(code);ex.code=code;ns,_,writes=self.fixture(error=ex);ok,msg=ns['install_and_activate_license']('fixture');self.assertFalse(ok);self.assertTrue(msg);self.assertEqual(writes,[]);self.assertNotIn('تعذر الاتصال الآمن',msg)
 def test_insecure_url_message_without_writes(self):
  ns,_,writes=self.fixture(error=ValueError('License server requires HTTPS'));ok,msg=ns['install_and_activate_license']('fixture');self.assertFalse(ok);self.assertIn('https://license.max-net.net',msg);self.assertEqual(writes,[])
 def test_network_failure_cannot_grant_license(self):
  ns,_,writes=self.fixture(error=TimeoutError());self.assertFalse(ns['install_and_activate_license']('fixture')[0]);self.assertEqual(writes,[])
 def test_standalone_has_https_default(self):self.assertNotIn('http://136.244.95.245:3030',(ROOT/'web/templates/license_standalone.html').read_text(encoding='utf8'))
if __name__=='__main__':unittest.main()

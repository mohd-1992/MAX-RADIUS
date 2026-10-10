import ast,types,sys,unittest
from pathlib import Path
from unittest.mock import Mock
source=Path(__file__).resolve().parents[2]/'services/coa_queue_service.py'
tree=ast.parse(source.read_text(encoding='utf8'))
functions=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ['_ppp_session_id','_mac','_nas_inventory','_select_nas_session','_disconnect_single_session']]
m=types.ModuleType('tested_disconnect');m.time=types.SimpleNamespace(sleep=lambda _:None)
exec(compile(ast.Module(body=functions,type_ignores=[]),str(source),'exec'),m.__dict__)
class API:
 def __init__(self,rows):self.rows=rows;self.remove=False;self.stuck=False;self.trap=False;self.invalid=False;self.login_ok=True
 def connect(self):pass
 def login(self,*x):return self.login_ok
 def close(self):pass
 def talk_raw(self,words):
  path=words[0]
  if self.invalid:return [('!trap',{})]
  if path.endswith('/print'):
   rows=self.rows if path.startswith('/ppp') else []
   return [('!re',r.copy()) for r in rows]+[('!done',{})]
  self.remove=True
  if self.trap:return [('!trap',{})]
  if not self.stuck:self.rows=[r for r in self.rows if r['.id']!=words[1].split('=',2)[-1]]
  return [('!done',{})]
def row(sid='0x81105BAB',ip='10.232.3.238',mac='AA:BB:CC:DD:EE:01',ident='*1'):
 return {'.id':ident,'name':'test','session-id':sid,'address':ip,'caller-id':mac}
class Tests(unittest.TestCase):
 def setUp(self):
  self.api=API([row()]);self.client=Mock();self.client.disconnect_user.return_value={'success':False,'status':'nak'}
  sys.modules['core.mikrotik_api']=types.SimpleNamespace(RouterOSApiProtocol=lambda *a,**kw:self.api)
  m._resolve_nas_credentials=lambda _:('10.78.0.8','test-only',3799,40837,'test','test')
  m.RadiusCoaClient=lambda **kw:self.client
  def seal(*args):
   reply=self.api.talk_raw(['/ppp/active/remove','=.id=*1'])
   if any(k=='!trap' for k,_ in reply):raise RuntimeError('test removal rejected')
  m._counter_verified_disconnect=seal
  m.query_one=Mock(return_value={'acctstoptime':None,'acctterminatecause':''})
 def call(self):return m._disconnect_single_session('test','10.78.0.8','81105bab','10.232.3.238','aa-bb-cc-dd-ee-01',1)
 def test_normalized_ppp_remove_verified(self):
  r=self.call();self.assertTrue(r['success']);self.assertTrue(self.api.remove);self.assertFalse(r['accounting_final'])
 def test_other_sessions_preserved(self):
  self.api.rows.append(row('0x81101234','10.232.3.237','AA:BB:CC:DD:EE:02','*2'));self.assertTrue(self.call()['success']);self.assertEqual([r['.id'] for r in self.api.rows],['*2'])
 def test_ack_without_removal_uses_verified_api(self):
  self.client.disconnect_user.return_value={'success':True,'status':'ack'};self.assertTrue(self.call()['success']);self.assertTrue(self.api.remove)
 def test_ack_still_present_never_success(self):
  self.client.disconnect_user.return_value={'success':True,'status':'ack'};self.api.stuck=True;self.assertFalse(self.call()['success'])
 def test_api_done_still_present_never_success(self):
  self.api.stuck=True;self.assertFalse(self.call()['success'])
 def test_wrong_generation_never_removes(self):
  self.api.rows[0]['session-id']='0x81109999';self.assertFalse(self.call()['success']);self.assertFalse(self.api.remove);self.client.disconnect_user.assert_not_called()
 def test_invalid_sid_never_claims_absence(self):
  self.api.rows[0]['session-id']='bad!';self.assertFalse(self.call()['success']);self.assertFalse(self.api.remove)
 def test_ambiguous_target_never_removes(self):
  self.api.rows.append(row(ident='*2'));self.assertFalse(self.call()['success']);self.assertFalse(self.api.remove)
 def test_inventory_trap_never_claims_absence(self):
  self.api.invalid=True;self.assertFalse(self.call()['success'])
 def test_remove_trap_never_success(self):
  self.api.trap=True;self.assertFalse(self.call()['success'])
 def test_login_failure_no_disconnect(self):
  self.api.login_ok=False;self.assertFalse(self.call()['success']);self.client.disconnect_user.assert_not_called()
 def test_true_absence_no_synthetic_stop(self):
  self.api.rows=[];r=self.call();self.assertTrue(r['success']);self.assertFalse(r['accounting_final']);self.client.disconnect_user.assert_not_called()
 def test_actual_stop_reported(self):
  m.query_one.return_value={'acctstoptime':'2026-10-10','acctterminatecause':'Admin-Reset'};self.assertTrue(self.call()['accounting_final'])
 def test_old_admin_closure_not_final_stop(self):
  m.query_one.return_value={'acctstoptime':'2026-10-10','acctterminatecause':'Admin-Reset-CoA'};self.assertFalse(self.call()['accounting_final'])
 def test_hotspot_requires_both_ip_mac(self):
  with self.assertRaises(RuntimeError):m._select_nas_session([('/ip/hotspot/active',{'.id':'*1','address':'10.0.0.1'})],'abc','10.0.0.1',None)
 def test_hotspot_exact_target(self):
  x=('/ip/hotspot/active',{'.id':'*1','address':'10.0.0.1','mac-address':'AA-BB-CC-DD-EE-01'})
  self.assertEqual(m._select_nas_session([x],'abc','10.0.0.1','AA:BB:CC:DD:EE:01'),[x])
if __name__=='__main__':unittest.main(verbosity=2)

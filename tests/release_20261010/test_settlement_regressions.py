import ast,datetime,sys,types,unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import contextmanager
ROOT=Path(__file__).resolve().parents[2]
def functions(path,names,env):
 tree=ast.parse((ROOT/path).read_text(encoding='utf8'))
 nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.Assign)) and (isinstance(n,ast.FunctionDef) and n.name in names or isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='NON_FINAL_CAUSES' for t in n.targets))]
 exec(compile(ast.Module(body=nodes,type_ignores=[]),str(path),'exec'),env)
class Cursor:
 def __init__(self,c):self.c=c
 def __enter__(self):return self
 def __exit__(self,*a):pass
 def execute(self,q,p):
  if q.startswith('SELECT * FROM radacct'):self.result=self.c.row.copy()
  elif q.startswith('SELECT * FROM wisp_nas'):self.result=self.c.seal
  elif q.startswith('INSERT INTO wisp_nas'):self.c.seal=dict(evidence_token=p[-1],input_bytes=p[4],output_bytes=p[5]);self.c.inserts+=1
  elif q.startswith('UPDATE radacct'):self.c.row.update(acctinputoctets=p[0],acctoutputoctets=p[1],acctsessiontime=p[2])
  else:raise AssertionError(q)
 def fetchone(self):return self.result
class Connection:
 def __init__(self,row):self.row=row;self.seal=None;self.inserts=0;self.commits=0;self.rollbacks=0
 def cursor(self):return Cursor(self)
 def commit(self):self.commits+=1
 def rollback(self):self.rollbacks+=1
 def close(self):pass
class Tests(unittest.TestCase):
 def fixture(self,stop=True,kind='hotspot'):
  row=dict(radacctid=1,username='test',nasipaddress='10.1.1.1',acctsessionid='abc',framedipaddress='10.2.2.2',acctuniqueid='unique',acctstarttime='start',acctinputoctets=120,acctoutputoctets=210,acctsessiontime=62,acctstoptime='end' if stop else None,acctterminatecause='Admin-Reset' if stop else '')
  c=Connection(row);s=dict(row={k:row[k] for k in ('username','nasipaddress','acctsessionid','framedipaddress','acctuniqueid','acctstarttime')},input=100,output=200,seconds=60,evidence_token='token',kind=kind);env={'get_connection':lambda:c};functions('services/nas_session_settlement.py',('actual_stop','seal_rows'),env);return c,s,env['seal_rows']
 def test_real_hotspot_stop_preserves_larger_final_counters_and_replay(self):
  c,s,seal=self.fixture();seal({1:s});seal({1:s});self.assertEqual((c.row['acctinputoctets'],c.row['acctoutputoctets'],c.row['acctsessiontime']),(120,210,62));self.assertEqual(c.inserts,1)
 def test_unverified_larger_interim_cannot_be_sealed(self):
  c,s,seal=self.fixture(False)
  with self.assertRaisesRegex(RuntimeError,'exceeds'):seal({1:s})
  self.assertEqual(c.inserts,0);self.assertEqual(c.rollbacks,1)
 def test_stop_from_wrong_generation_never_accepted(self):
  c,s,seal=self.fixture();s['row']['acctuniqueid']='other'
  with self.assertRaisesRegex(RuntimeError,'generation'):seal({1:s})
  self.assertEqual(c.inserts,0)
 def test_ppp_without_stop_never_claims_final(self):
  c,s,seal=self.fixture(False,'pppoe')
  with self.assertRaisesRegex(RuntimeError,'not yet verified'):seal({1:s})
 def test_moving_hotspot_without_stop_cannot_use_administrative_fallback(self):
  c,s,seal=self.fixture(False);s.update(input=150,output=250,requires_actual_stop=True)
  with self.assertRaisesRegex(RuntimeError,'not yet verified'):seal({1:s})
  self.assertEqual(c.inserts,0);self.assertEqual(c.rollbacks,1)
 def test_moving_hotspot_late_stop_merges_and_replay_is_idempotent(self):
  c,s,seal=self.fixture();s['requires_actual_stop']=True
  seal({1:s});seal({1:s});self.assertEqual(c.inserts,1);self.assertEqual(c.row['acctinputoctets'],120)
 def test_conflicting_proof_replay_is_rejected(self):
  c,s,seal=self.fixture();seal({1:s});s['evidence_token']='different'
  with self.assertRaisesRegex(RuntimeError,'Conflicting'):seal({1:s})
 def test_all_sessions_retry_under_one_guard(self):
  events=[];calls=[]
  class Pending(RuntimeError):pass
  @contextmanager
  def guard(u):
   events.append('enter')
   try:yield
   finally:events.append('exit')
  def checkpoint(u):
   self.assertEqual(events,['enter']);return {i:dict(row=dict(nasipaddress='nas')) for i in range(3)}
  def freeze(s,include_ppp):
   calls.append(len(s));self.assertEqual(events,['enter']);self.assertTrue(include_ppp)
   if len(calls)==1:raise Pending('late Stop')
   s.clear()
  modules={}
  for name,attributes in [('services.online_policy_service',dict(checkpoint=checkpoint)),('services.nas_session_settlement',dict(freeze_and_disconnect=freeze)),('services.renewal_settlement_service',dict(SettlementPending=Pending))]:
   module=types.ModuleType(name);module.__dict__.update(attributes);modules[name]=module
  env=dict(_disconnect_account_guard=guard,time=types.SimpleNamespace(sleep=lambda _:None));functions('services/coa_queue_service.py',('_disconnect_account_sessions',),env)
  with patch.dict(sys.modules,modules):result=env['_disconnect_account_sessions']('test')
  self.assertTrue(result['success']);self.assertEqual(calls,[3,3]);self.assertEqual(events,['enter','exit'])
if __name__=='__main__':unittest.main(verbosity=2)

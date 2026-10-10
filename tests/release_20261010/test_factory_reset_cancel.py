import ast,copy,json,threading,unittest
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
class Tests(unittest.TestCase):
 def setUp(self):
  self.row={'job_id':'test','state':'running','parameters':json.dumps({'job_kind':'factory-reset'}),'progress':'{}'}
  self.lock=threading.RLock();owner=self
  class Cursor:
   def execute(self,q,p):
    if q.startswith('SELECT'):return
    owner.row['parameters']=p[0]
    if 'progress=?' in q:owner.row['progress']=p[1]
   def fetchone(self):return copy.deepcopy(owner.row)
  class Conn:
   def cursor(self):return Cursor()
  @contextmanager
  def session():
   with self.lock:yield Conn()
  self.env={'json':json,'db_session':session,'adapt_query':lambda q,c:q,
   'query_one':lambda *a:copy.deepcopy(self.row),'get_factory_reset':lambda *a:copy.deepcopy(self.row)}
  tree=ast.parse((ROOT/'services/factory_reset_service.py').read_text(encoding='utf8'))
  names={'FactoryResetCancelled','_check_cancel','cancel_factory_reset','_claim_wipe'}
  exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name in names],type_ignores=[]),'cancel','exec'),self.env)
 def test_cancel_before_wipe_prevents_claim(self):
  self.env['cancel_factory_reset']('test')
  with self.assertRaises(self.env['FactoryResetCancelled']):self.env['_claim_wipe']('test',{})
  self.assertNotIn('wipe_started',json.loads(self.row['parameters']))
 def test_wipe_boundary_rejects_cancel(self):
  self.env['_claim_wipe']('test',{'stage':'wiping'})
  with self.assertRaises(ValueError):self.env['cancel_factory_reset']('test')
  self.assertNotIn('cancel_requested',json.loads(self.row['parameters']))
 def test_cancel_is_durable_and_idempotent(self):
  self.env['cancel_factory_reset']('test');self.env['cancel_factory_reset']('test')
  with self.assertRaises(self.env['FactoryResetCancelled']):self.env['_check_cancel']('test')
 def test_finished_job_rejects_cancel(self):
  for state in ['completed','failed','interrupted']:
   self.row['state']=state
   with self.assertRaises(ValueError):self.env['cancel_factory_reset']('test')
 def test_concurrent_cancel_and_claim_never_both_succeed(self):
  for _ in range(100):
   self.row['parameters']=json.dumps({'job_kind':'factory-reset'});result=[];barrier=threading.Barrier(2)
   def act(name):
    barrier.wait()
    try:self.env[name]('test',{}) if name=='_claim_wipe' else self.env[name]('test');result.append(name)
    except (ValueError,self.env['FactoryResetCancelled']):pass
   threads=[threading.Thread(target=act,args=(name,)) for name in ['_claim_wipe','cancel_factory_reset']]
   for t in threads:t.start()
   for t in threads:t.join()
   self.assertEqual(len(result),1)
 def test_worker_restores_and_never_wipes_when_cancelled(self):
  import sys,types
  source=(ROOT/'services/factory_reset_service.py').read_text(encoding='utf8');tree=ast.parse(source)
  worker=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_worker')
  @contextmanager
  def lock(*a,**k):yield True
  for cancel_at in [1,2,3,4,5,6]:
   events=[];checks=[0]
   def check(*a):
    checks[0]+=1
    if checks[0]==cancel_at:raise self.env['FactoryResetCancelled']('cancelled')
   env=dict(self.env,job_lock=lock,execute_update=lambda *a:1,_check_cancel=check,
    _drain_operational_requests=lambda **k:k['check_cancel'](),_cores=lambda:[{'id':'test','was_running':True}],
    _save=lambda *a,**k:events.append(('save',a[2] if len(a)>2 else 'running',k)),
    _close_sessions=lambda *a,**k:None,_docker=lambda *a:events.append(('docker',a)),
    _inspect=lambda *a:{'State':{'Running':False}},_restore=lambda *a:events.append(('restore',)) or [],
    query_one=lambda *a:{'n':0},log_audit=lambda *a:None,log=types.SimpleNamespace(exception=lambda *a:None))
   exec(compile(ast.Module(body=[worker],type_ignores=[]),'worker','exec'),env)
   backup=types.ModuleType('services.backup_service');backup.create_backup=lambda **k:(True,'',{})
   wipe=types.ModuleType('services.db_maintenance_service');wipe._wipe_factory_database=lambda *a:events.append(('WIPE',))
   with patch.dict(sys.modules,{'services.backup_service':backup,'services.db_maintenance_service':wipe}):env['_worker']('test',{'keep_packages':True,'keep_resellers':False},'test')
   self.assertNotIn(('WIPE',),events);self.assertIn(('restore',),events)
   self.assertEqual(events[-1][1],'cancelled')
if __name__=='__main__':unittest.main(verbosity=2)

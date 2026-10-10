import ast, unittest, types, sys
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch
root=Path(__file__).resolve().parents[2]/'services'
def function(file,name):
 tree=ast.parse((root/file).read_text(encoding='utf8')); node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
 return compile(ast.Module(body=[node],type_ignores=[]),file,'exec')
class Tests(unittest.TestCase):
 def sweep(self,guard,process_hook=None):
  events=[]
  @contextmanager
  def lock(*a,**k):
   events.append('lock')
   try:yield True
   finally:events.append('release')
  db=types.ModuleType('database.db');db.query_all=lambda q,*a:[{'id':1},{'id':2}] if 'SELECT id' in q else [{'radacctid':1,'acctsessionid':'s','nasipaddress':'nas'}]
  coa=types.ModuleType('services.coa_queue_service');coa.enqueue_disconnect=lambda *a,**k:(events.append('enqueue') or True,'ok')
  factory=types.ModuleType('services.factory_reset_service');factory.factory_reset_active=guard
  def tx(fn):
   events.append('transaction');r=fn(None);events.append('commit')
   if process_hook:process_hook()
   return r
  env=dict(job_lock=lock,run_transaction=tx,lock_account=lambda *a:('',{'username':'test','id':1,'status':'expired'}),expiry_reason=lambda *a:None,sql=lambda *a:None,ACCOUNTING_UNCERTAIN_REASON='uncertain')
  exec(function('account_lifecycle_service.py','sweep_expired_accounts'),env)
  with patch.dict(sys.modules,{'database.db':db,'services.coa_queue_service':coa,'services.factory_reset_service':factory}):result=env['sweep_expired_accounts']()
  return result,events
 def test_pending_reset_prevents_new_sweep(self):
  result,events=self.sweep(lambda:True);self.assertFalse(result);self.assertEqual(events,[])
 def test_reset_arrives_between_precheck_and_lock(self):
  guards=iter([False,True]);result,events=self.sweep(lambda:next(guards));self.assertFalse(result);self.assertEqual(events,['lock','release'])
 def test_current_account_commits_and_enqueues_before_yield(self):
  state={'pending':False};result,events=self.sweep(lambda:state['pending'],lambda:state.update(pending=True));self.assertFalse(result);self.assertEqual(events,['lock','transaction','commit','enqueue','release'])
 def test_sweep_resumes_when_no_active_reset(self):
  result,events=self.sweep(lambda:False);self.assertTrue(result);self.assertEqual(events.count('commit'),4);self.assertEqual(events[-1],'release')
 def worker(self,expiry):
  locks=[];saved=[]
  @contextmanager
  def lock(name,timeout=0):
   locks.append((name,timeout))
   yield expiry if name=='expiry-sweep' else True
  env=dict(_restore=lambda *a:[],log_audit=lambda *a:None,job_lock=lock,_save=lambda *a,**k:saved.append((a,k)),execute_update=lambda *a:1,_drain_operational_requests=lambda:(_ for _ in ()).throw(RuntimeError('test stop before any destructive work')),log=types.SimpleNamespace(exception=lambda *a:None))
  exec(function('factory_reset_service.py','_worker'),env);env['_worker']('test',{},'test');return locks,saved
 def test_reset_waits_bounded_and_does_not_wipe_on_timeout(self):
  locks,saved=self.worker(False);self.assertIn(('expiry-sweep',60),locks);self.assertFalse(saved[-1][0][1]['data_reset']);self.assertIn('مهلة',saved[-1][0][1]['message'])
 def test_reset_progresses_after_sweep_releases(self):
  locks,saved=self.worker(True);self.assertEqual(saved[-1][0][1]['message'],'test stop before any destructive work');self.assertFalse(saved[-1][0][1]['data_reset'])
if __name__=='__main__':unittest.main(verbosity=2)

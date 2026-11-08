import ast,copy,contextlib,threading,types,unittest
from pathlib import Path
from unittest.mock import Mock,patch
class MigrationProgressAdmissionTests(unittest.TestCase):
 def setUp(self):
  p=Path('services/database_migration_service.py');tree=ast.parse(p.read_text(encoding='utf-8'));names={'get_migration_progress','update_migration_progress','reset_migration_admission','cancel_migration','start_async_migration'};nodes=[x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name in names]
  for n in nodes:n.decorator_list=[]
  self.ns=dict(copy=copy,threading=threading,_PROGRESS_LOCK=threading.Lock(),_CANCEL_EVENT=threading.Event(),_PROGRESS_STATE=dict(status='completed',percent=100,stats={'old':2},error='old error',stage='old',message='old',processed=2,total=2));self.finished=threading.Event();self.release=threading.Event();self.addCleanup(self.release.set);self.modules=patch.dict('sys.modules',{'services.heavy_operation_service':types.SimpleNamespace(operation_slot=contextlib.nullcontext)});self.modules.start();self.addCleanup(self.modules.stop);exec(compile(ast.Module(body=nodes,type_ignores=[]),str(p),'exec'),self.ns)
 def test_old_terminal_is_reset_before_admission_returns(self):
  entered=threading.Event()
  def execute(*args,**kw):
   self.observed=(self.ns['get_migration_progress'](),kw);entered.set();self.release.wait(2);self.finished.set()
  self.ns['execute_database_migration']=execute;self.assertTrue(self.ns['start_async_migration']('fixture',{})['success']);self.assertTrue(entered.wait(1));state,kw=self.observed;self.assertEqual(state['status'],'running');self.assertEqual(state['stats'],{});self.assertIsNone(state['error']);self.assertTrue(kw['_cancel_prepared']);self.release.set();self.assertTrue(self.finished.wait(1))
 def test_cancel_after_admission_stays_set_at_worker(self):
  entered=threading.Event();observed=[]
  def execute(*args,**kw):
   entered.set();self.release.wait(2);observed.append(self.ns['_CANCEL_EVENT'].is_set());self.finished.set()
  self.ns['execute_database_migration']=execute;self.ns['start_async_migration']('fixture',{});self.assertTrue(entered.wait(1));self.assertTrue(self.ns['cancel_migration']()['success']);self.release.set();self.assertTrue(self.finished.wait(1));self.assertEqual(observed,[True])
 def test_completed_job_cannot_be_changed_to_cancelling(self):
  self.assertFalse(self.ns['cancel_migration']()['success']);self.assertEqual(self.ns['get_migration_progress']()['status'],'completed');self.assertFalse(self.ns['_CANCEL_EVENT'].is_set())
 def test_stats_and_returned_snapshot_have_no_mutable_alias(self):
  stats={'errors':[]};self.ns['update_migration_progress'](stats=stats);stats['errors'].append('not published');state=self.ns['get_migration_progress']();self.assertEqual(state['stats']['errors'],[]);state['stats']['errors'].append('reader edit');self.assertEqual(self.ns['get_migration_progress']()['stats']['errors'],[])
 def test_early_accepted_cancel_stops_before_data_processing(self):
  p=Path('services/database_migration_service.py');fn=next(n for n in ast.parse(p.read_text(encoding='utf-8')).body if isinstance(n,ast.FunctionDef) and n.name=='execute_database_migration');fn.decorator_list=[];end=next(i for i,n in enumerate(fn.body) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='stats' for t in n.targets));fn.body=fn.body[:end];exec(compile(ast.Module(body=[fn],type_ignores=[]),str(p),'exec'),self.ns);self.ns['_CANCEL_EVENT'].set()
  with self.assertRaises(RuntimeError):self.ns[fn.name]('unused',dict(collision_strategy='skip'),_cancel_prepared=True)
  self.assertEqual(self.ns['get_migration_progress']()['status'],'cancelled')
if __name__=='__main__':unittest.main()

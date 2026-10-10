import ast, unittest, types, sys
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
class Tests(unittest.TestCase):
 def test_authorize_query_fits_freeradius_parser(self):
  text=(ROOT/'docker/freeradius/queries.conf').read_text(encoding='utf8')
  query=text.split('\nauthorize_check_query =',1)[1].split('\nauthorize_reply_query =',1)[0]
  self.assertLess(len(query.replace('\\\n','').encode('utf8')),8192)
  self.assertIn("active_key='factory-reset'",query)
 def run_worker(self,close_error=False,late_session=False):
  events=[]; saved=[]; running={'value':True}
  @contextmanager
  def lock(*a,**k):yield True
  def close(progress):
   self.assertTrue(running['value']); events.append('close')
   if close_error:raise RuntimeError('missing Stop')
  def docker(method,path):events.append('stop');running['value']=False
  backup=types.ModuleType('services.backup_service');backup.create_backup=lambda **k:(True,'ok',{})
  maintenance=types.ModuleType('services.db_maintenance_service')
  maintenance._wipe_factory_database=lambda *a:events.append('wipe') or []
  maintenance._reclaim_factory_space=lambda *a:dict(success=True)
  def restore(*a):events.append('restore');running['value']=True;return []
  env=dict(job_lock=lock,execute_update=lambda *a:1,_drain_operational_requests=lambda:None,
   _cores=lambda:[dict(id='core',was_running=True)],_save=lambda *a,**k:saved.append(a),
   _close_sessions=close,_docker=docker,_inspect=lambda *a:dict(State=dict(Running=running['value'])),
   query_one=lambda *a:dict(n=int(late_session)),_restore=restore,log_audit=lambda *a:None,
   log=types.SimpleNamespace(exception=lambda *a:None))
  tree=ast.parse((ROOT/'services/factory_reset_service.py').read_text(encoding='utf8'))
  node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_worker')
  exec(compile(ast.Module(body=[node],type_ignores=[]),'worker','exec'),env)
  with patch.dict(sys.modules,{'services.backup_service':backup,'services.db_maintenance_service':maintenance}):
   env['_worker']('job',dict(keep_packages=True,keep_resellers=False),'admin')
  return events,saved[-1]
 def test_accounting_remains_running_until_sessions_close(self):
  events,last=self.run_worker();self.assertEqual(events,['close','stop','wipe','restore']);self.assertTrue(last[1]['success'])
 def test_missing_stop_never_stops_or_wipes(self):
  events,last=self.run_worker(close_error=True);self.assertEqual(events,['close','restore']);self.assertFalse(last[1]['data_reset'])
 def test_inflight_session_aborts_wipe_and_restores_core(self):
  events,last=self.run_worker(late_session=True);self.assertEqual(events,['close','stop','restore']);self.assertFalse(last[1]['data_reset'])
if __name__=='__main__':unittest.main(verbosity=2)

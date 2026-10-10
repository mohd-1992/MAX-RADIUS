import ast,types,sys,unittest,datetime
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
class Tests(unittest.TestCase):
 def run_action(self,reason=None,valid=True,status='suspended',sync=True):
  row=dict(id=1,username='test',status=status,package_id=1,extra_quota_mb=50,last_renewed_at='cycle',expires_at='expiry',pause_reason='manual')
  events=[];result={}
  def sql(conn,q,p=(),fetch=None):
   if q.startswith('SELECT id'):return dict(id=1)
   result.update(status=p[0],pause_reason=None);events.append('update')
  def transaction(fn):
   try:r=fn('connection');events.append('commit');return r
   except Exception:result.clear();events.append('rollback');raise
  lifecycle=types.ModuleType('services.account_lifecycle_service')
  lifecycle.run_transaction=transaction;lifecycle.lock_account=lambda *a:('wisp_subscribers',row);lifecycle.sql=sql
  lifecycle.expiry_reason=lambda conn,kind,candidate:reason if candidate['status']=='active' else 'bad evaluation'
  lifecycle.ACCOUNTING_UNCERTAIN_REASON='uncertain'
  license=types.ModuleType('services.license_guard_service');license.get_active_license_status=lambda:dict(valid=valid)
  radius=types.ModuleType('core.radius_sync');radius.sync_subscriber_to_radius=lambda *a,**k:events.append('sync') or sync
  env=dict(log_user_audit=lambda *a:events.append('audit'))
  tree=ast.parse((ROOT/'services/quick_action_service.py').read_text(encoding='utf8'))
  node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='action_activate_subscriber')
  exec(compile(ast.Module(body=[node],type_ignores=[]),'action','exec'),env)
  with patch.dict(sys.modules,{'services.account_lifecycle_service':lifecycle,'services.license_guard_service':license,'core.radius_sync':radius}):
   try:response=env['action_activate_subscriber'](1)
   except Exception as e:response=e
  return response,result,events,row
 def test_valid_entitlements_activate_without_renewal(self):
  response,result,events,row=self.run_action();self.assertEqual(response[2],'active');self.assertEqual(row['extra_quota_mb'],50);self.assertEqual(row['last_renewed_at'],'cycle');self.assertEqual(events,['update','sync','commit','audit'])
 def test_exhausted_data_days_or_uptime_moves_to_expired(self):
  for reason in ['data depleted','validity elapsed','uptime depleted']:
   with self.subTest(reason=reason):
    response,result,events,row=self.run_action(reason=reason);self.assertEqual(response[2],'expired');self.assertIsNone(result['pause_reason'])
 def test_uncertain_accounting_remains_suspended(self):
  response,result,events,row=self.run_action(reason='uncertain');self.assertIsInstance(response,ValueError);self.assertEqual(result,{});self.assertEqual(events,['rollback'])
 def test_invalid_license_prevents_activation(self):
  response,result,events,row=self.run_action(valid=False);self.assertIsInstance(response,ValueError);self.assertEqual(result,{})
 def test_sync_failure_rolls_back_status(self):
  response,result,events,row=self.run_action(sync=False);self.assertIsInstance(response,RuntimeError);self.assertEqual(result,{});self.assertNotIn('audit',events)
 def test_stale_button_does_not_change_active_account(self):
  response,result,events,row=self.run_action(status='active');self.assertIsInstance(response,ValueError);self.assertEqual(events,['rollback'])
if __name__=='__main__':unittest.main(verbosity=2)

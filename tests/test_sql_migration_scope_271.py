"""SQL migration collisions preserve target identity, contract, and accounting."""
import ast,types,unittest
from pathlib import Path
from unittest.mock import Mock,patch
class SqlMigrationScopeTests(unittest.TestCase):
 def setup_scope(self,responses):
  self.cursor=Mock();self.cursor.__enter__=Mock(return_value=self.cursor);self.cursor.__exit__=Mock(return_value=False);self.cursor.fetchone.side_effect=responses;self.conn=Mock();self.conn.cursor.return_value=self.cursor;self.lock=Mock();module=types.SimpleNamespace(adapt_query=lambda s,c:s);journal=types.SimpleNamespace(account_image=self.lock);source=Path('services/sql_migration_scope.py');ns={}
  self.patch=patch.dict('sys.modules',{'database.db':module,'services.userman_import_journal':journal});self.patch.start();self.addCleanup(self.patch.stop);exec(compile(source.read_text(encoding='utf-8'),str(source),'exec'),ns);self.prepare=ns['prepare_identity'];self.validate=ns['validate_session_owners'];self.stats=dict(skipped_duplicates=0,subscribers_imported=0,cards_imported=0)
 def writes(self):return [c.args[0] for c in self.cursor.execute.call_args_list if c.args[0].startswith(('INSERT','UPDATE','DELETE'))]
 def test_skip_existing_has_no_write_or_account_history_lock(self):
  self.setup_scope([dict(id=4,username='local'),None]);self.assertFalse(self.prepare(self.conn,'local','subscriber','new','skip',self.stats));self.assertEqual(self.writes(),[]);self.lock.assert_not_called();self.assertFalse(any('FOR UPDATE' in c.args[0] for c in self.cursor.execute.call_args_list))
 def test_new_identity_without_history_is_admitted_without_writes(self):
  self.setup_scope([None,None,None]);self.assertTrue(self.prepare(self.conn,'new-local','subscriber','pw','skip',self.stats));self.assertEqual(self.writes(),[])
 def test_reserved_history_is_not_a_new_identity(self):
  self.setup_scope([None,None,dict(occupied=1)]);self.assertFalse(self.prepare(self.conn,'old-local','subscriber','pw','overwrite',self.stats));self.assertEqual(self.writes(),[])
 def test_cross_kind_collision_never_converts_account(self):
  self.setup_scope([None,dict(id=7)]);self.assertFalse(self.prepare(self.conn,'local','subscriber','pw','overwrite',self.stats));self.assertEqual(self.writes(),[])
 def test_subscriber_overwrite_changes_only_metadata_and_password(self):
  self.setup_scope([dict(id=4,username='local'),None,dict(id=9)]);self.assertFalse(self.prepare(self.conn,'local','subscriber','pw','overwrite',self.stats,full_name='Name',phone='000'));self.lock.assert_called_once_with(self.conn,'local');sql=' '.join(self.writes());self.assertNotIn('DELETE',sql)
  for field in ('balance','expires_at','last_renewed_at','package_id','status','radacct','quota'):self.assertNotIn(field,sql)
  self.assertEqual(self.stats['updated_existing_accounts'],1)
 def test_voucher_overwrite_keeps_identity_contract_and_sales(self):
  self.setup_scope([dict(id=5,username='local-card'),None,None]);self.prepare(self.conn,'local-card','voucher','pw','overwrite',self.stats);sql=' '.join(self.writes());self.assertIn('password=?,pin_code=?',sql)
  for field in ('serial_number','batch_id','balance','expires_at','snap_','wisp_voucher_sales','radacct'):self.assertNotIn(field,sql)
 def test_unknown_collision_policy_rejected_before_sql(self):
  self.setup_scope([])
  with self.assertRaises(ValueError):self.prepare(self.conn,'local','subscriber','pw','replace-all',self.stats)
  self.cursor.execute.assert_not_called()
class MigrationNoWholesaleWrites(unittest.TestCase):
 def test_main_executor_has_no_global_clear_or_synthetic_live_stop(self):
  p=Path('services/database_migration_service.py');fn=next(x for x in ast.parse(p.read_text(encoding='utf-8')).body if isinstance(x,ast.FunctionDef) and x.name=='execute_database_migration');text=ast.unparse(fn);self.assertNotIn('Database-Imported-Closed',text);self.assertNotIn('preserve_before_replacement',text)
  for x in ast.walk(fn):
   if isinstance(x,ast.Constant) and isinstance(x.value,str):
    self.assertFalse(x.value.strip().upper().startswith('DELETE FROM'))
    if x.value.strip().startswith(('INSERT INTO wisp_subscribers','INSERT INTO wisp_vouchers')):self.assertNotIn('ON DUPLICATE KEY',x.value)
class SqlUidOwnerTests(unittest.TestCase):
 def setup_scope(self):SqlMigrationScopeTests.setup_scope(self,[])
 def test_other_existing_uid_owner_rejected(self):
  self.setup_scope();self.cursor.fetchall.return_value=[dict(acctuniqueid='uid',username='other')]
  with self.assertRaises(ValueError):self.validate(self.conn,[('sid','uid','new')])
 def test_duplicate_source_uid_for_two_users_rejected_before_sql(self):
  self.setup_scope()
  with self.assertRaises(ValueError):self.validate(self.conn,[('s1','uid','one'),('s2','uid','two')])
  self.cursor.execute.assert_not_called()
 def test_binding_count_and_chunking_for_301_uids(self):
  self.setup_scope();self.cursor.fetchall.return_value=[];self.validate(self.conn,[('s',str(i),'local') for i in range(301)]);self.assertEqual(len(self.cursor.execute.call_args_list),2)
  for call in self.cursor.execute.call_args_list:self.assertEqual(call.args[0].count('?'),len(call.args[1]))

class SqlHistoryUidOwnerTests(unittest.TestCase):
 def setup_scope(self):SqlMigrationScopeTests.setup_scope(self,[])
 def test_uid_owned_only_by_history_is_rejected(self):
  self.setup_scope();self.cursor.fetchall.side_effect=[[],[dict(acctuniqueid='uid',username='other')]]
  with self.assertRaises(ValueError):self.validate(self.conn,[('sid','uid','new')],history=True)
  self.assertIn('wisp_imported_session_history',self.cursor.execute.call_args_list[1].args[0])
 def test_same_history_owner_is_not_a_cross_account_collision(self):
  self.setup_scope();self.cursor.fetchall.side_effect=[[],[dict(acctuniqueid='uid',username='LOCAL')]];self.validate(self.conn,[('sid','uid','local')],history=True);self.assertEqual(self.cursor.execute.call_count,2)

if __name__=='__main__':unittest.main()

import sqlite3
from tests.test_invoice_design_dialect_271 import *
class ActualSQLiteTests(unittest.TestCase):
 def run_save(self,conn,form):
  path=Path('web/routes/subscribers.py');node=next(x for x in ast.parse(path.read_text(encoding='utf-8')).body if isinstance(x,ast.FunctionDef) and x.name=='save_invoice_design_action');node.decorator_list=[];flash=Mock()
  @contextlib.contextmanager
  def session():
   try:yield conn;conn.commit()
   except Exception:conn.rollback();raise
  module=types.SimpleNamespace(db_session=session,is_mysql_conn=lambda c:False,adapt_query=lambda s,c:s);ns=dict(request=types.SimpleNamespace(form=form),flash=flash,redirect=lambda x:x,url_for=lambda x:x)
  with patch.dict('sys.modules',{'database.db':module}):exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),ns);ns[node.name]()
  return flash
 def test_actual_sqlite_insert_and_partial_update(self):
  c=sqlite3.connect(':memory:');c.execute('CREATE TABLE wisp_system_settings (`key` TEXT PRIMARY KEY, `value` TEXT, description TEXT)');self.run_save(c,dict(invoice_header='H',tax_number='T',invoice_footer='F'));self.run_save(c,dict(invoice_footer=''));self.assertEqual(dict(c.execute('SELECT `key`,`value` FROM wisp_system_settings')) ,dict(invoice_header='H',tax_number='T',invoice_footer=''));c.close()
 def test_actual_sqlite_failure_rolls_back_prior_field(self):
  c=sqlite3.connect(':memory:');c.execute('CREATE TABLE wisp_system_settings (`key` TEXT PRIMARY KEY, `value` TEXT, description TEXT, CHECK (`key` != \'tax_number\'))');f=self.run_save(c,dict(invoice_header='H',tax_number='T'));self.assertEqual(c.execute('SELECT COUNT(*) FROM wisp_system_settings').fetchone()[0],0);self.assertEqual(f.call_args.args[1],'danger');c.close()
if __name__=='__main__':unittest.main(defaultTest='ActualSQLiteTests')

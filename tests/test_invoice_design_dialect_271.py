"""Invoice design saves use the correct dialect and a single transaction."""
import ast,contextlib,types,unittest
from pathlib import Path
from unittest.mock import Mock,patch
class InvoiceDesignTests(unittest.TestCase):
 def setup_handler(self,mysql,form,fail_at=None):
  source=Path(__file__).resolve().parents[1]/'web/routes/subscribers.py';node=next(x for x in ast.parse(source.read_text(encoding='utf-8')).body if isinstance(x,ast.FunctionDef) and x.name=='save_invoice_design_action');node.decorator_list=[];self.statements=[];self.committed=False;self.rolled_back=False
  def execute(sql,args):
   self.statements.append((sql,args))
   if fail_at==len(self.statements):raise RuntimeError('injected SQL failure')
  cursor=Mock();cursor.execute.side_effect=execute;cursor.__enter__=Mock(return_value=cursor);cursor.__exit__=Mock(return_value=False);conn=Mock();conn.cursor.return_value=cursor
  @contextlib.contextmanager
  def transaction():
   try:yield conn;self.committed=True
   except Exception:self.rolled_back=True;raise
  module=types.SimpleNamespace(db_session=transaction,is_mysql_conn=lambda c:mysql,adapt_query=lambda sql,c:sql.replace('?', '%s') if mysql else sql);self.flash=Mock();ns=dict(request=types.SimpleNamespace(form=form),flash=self.flash,redirect=lambda x:x,url_for=lambda x:x)
  with patch.dict('sys.modules',{'database.db':module}):
   exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),ns);ns[node.name]()
 def test_mariadb_three_fields_one_commit(self):
  self.setup_handler(True,dict(invoice_header='Head',tax_number='Tax',invoice_footer='Foot'));self.assertTrue(self.committed);self.assertEqual(len(self.statements),3);self.assertTrue(all('ON DUPLICATE KEY' in sql and 'ON CONFLICT' not in sql for sql,_ in self.statements))
 def test_sqlite_dialect_retained(self):
  self.setup_handler(False,dict(invoice_header='Head'));self.assertTrue(self.committed);self.assertIn('ON CONFLICT(`key`)',self.statements[0][0])
 def test_omitted_fields_not_written(self):
  self.setup_handler(True,dict(invoice_footer=''));self.assertEqual([args for _,args in self.statements],[('invoice_footer','')])
 def test_mid_save_error_rolls_back_and_never_flashes_success(self):
  self.setup_handler(True,dict(invoice_header='Head',tax_number='Tax'),fail_at=2);self.assertTrue(self.rolled_back);self.assertFalse(self.committed);self.assertEqual(self.flash.call_args.args[1],'danger')
if __name__=='__main__':unittest.main()

import ast, re, sqlite3, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
FILES=['services/subscriber_service.py','services/quick_action_service.py','services/whatsapp_service.py']
class Tests(unittest.TestCase):
 def test_real_import_counters_match_sql_displays(self):
  db=sqlite3.connect(':memory:')
  db.execute('CREATE TABLE radacct(acctinputoctets INTEGER,acctoutputoctets INTEGER,acctinputgigawords INTEGER,acctoutputgigawords INTEGER)')
  # Actual failing source counters, plus null counters and the 4 GiB boundary.
  for up,down in [(208352452276,11760165743),(4294967296,4294967295),(None,None)]:
   db.execute('DELETE FROM radacct');db.execute('INSERT INTO radacct VALUES(?,?,?,?)',(up,down,(up or 0)//4294967296,(down or 0)//4294967296))
   for name in FILES:
    text=(ROOT/name).read_text(encoding='utf8')
    expressions=re.findall(r'(?:MAX|SUM)\(CAST\(COALESCE\(acct(?:input|output)octets, 0\) AS UNSIGNED\)\)',text)
    self.assertTrue(expressions,name)
    for expression in expressions:
     with self.subTest(file=name,expression=expression,up=up):
      value=db.execute('SELECT '+expression+' FROM radacct').fetchone()[0]
      self.assertEqual(value,(up or 0) if 'input' in expression else (down or 0))
  db.close()
 def test_voucher_page_uses_cycle_aware_accounting(self):
  source=(ROOT/'web/routes/vouchers.py').read_text(encoding='utf8')
  self.assertIn('card_list_page_service', source)
  service=(ROOT/'services/card_list_page_service.py').read_text(encoding='utf8')
  self.assertIn('get_voucher_accounting_totals_batch',source)
  self.assertNotIn('acctinputgigawords *', source)
 def test_session_history_uses_full_counters_once(self):
  tree=ast.parse((ROOT/'services/session_history_service.py').read_text(encoding='utf8'))
  fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='format_history_rows')
  row=dict(acctinputoctets=208352452276,acctoutputoctets=11760165743,acctinputgigawords=48,acctoutputgigawords=2,is_active=0,acctsessiontime=60,acctstoptime='2026-01-01',acctstarttime='2026-01-01',imported_history=0,live_source=1)
  env=dict(enrich_session_display=lambda *a:None,format_bytes=lambda n:n,format_duration=lambda n:n)
  exec(compile(ast.Module(body=[fn],type_ignores=[]),'history formatter','exec'),env)
  env['format_history_rows']([row],lambda *a:[])
  self.assertEqual(row['total_str'],220112618019)
  self.assertFalse(row['is_active'])
if __name__=='__main__':unittest.main(verbosity=2)

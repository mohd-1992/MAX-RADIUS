import ast,sqlite3,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
class Tests(unittest.TestCase):
 def setUp(self):
  self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
  self.db.executescript((ROOT/'database/freeradius_standard.sql').read_text(encoding='utf8'))
  def query(q,p):return [dict(r) for r in self.db.execute(q,p)]
  env=dict(query_all=query,get_heartbeat_cutoff_str=lambda *a:'2026-10-10',format_bytes=str,format_duration=str)
  tree=ast.parse((ROOT/'services/subscriber_service.py').read_text(encoding='utf8'))
  nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('get_subscriber_sessions','get_user_usage_analytics')]
  exec(compile(ast.Module(body=nodes,type_ignores=[]),'history','exec'),env);self.env=env
  self.insert('radacct','summary',200,'2024-01-01','Consolidated-Historical-Import')
 def tearDown(self):self.db.close()
 def insert(self,table,key,bytes,date,cause=''):
  self.db.execute(f'INSERT INTO {table}(acctsessionid,acctuniqueid,username,acctstarttime,acctinputoctets,acctoutputoctets,acctsessiontime,acctterminatecause) VALUES (?,?,?,?,?,?,?,?)',(key,key,'test',date,bytes,0,60,cause))
 def test_full_history_hides_summary_and_is_never_online(self):
  self.insert('wisp_imported_session_history','old1',100,'2024-01-01');self.insert('wisp_imported_session_history','old2',300,'2024-02-01')
  self.insert('radacct','new',10,'2026-10-10')
  rows=self.env['get_subscriber_sessions']('test',50)
  self.assertEqual(len(rows),3);self.assertEqual(rows[0]['acctuniqueid'],'new')
  self.assertTrue(all(not r['is_active'] for r in rows if r['imported_history']))
  analytics=self.env['get_user_usage_analytics']('test');self.assertEqual(analytics['total_sessions'],3)
  self.assertEqual(analytics['total_up_str'],'410.0')
  self.assertEqual(self.db.execute('SELECT SUM(acctinputoctets) FROM radacct').fetchone()[0],210)
 def test_consolidated_mode_retains_summary(self):
  rows=self.env['get_subscriber_sessions']('test');self.assertEqual(len(rows),1)
  self.assertEqual(self.env['get_user_usage_analytics']('test')['total_sessions'],1)
if __name__=='__main__':unittest.main(verbosity=2)

"""SQLite algebra fixture; native MariaDB verifies the unmodified SQL separately."""
import ast,sqlite3,unittest,re
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
class Cursor:
 def __init__(self,db):self.db=db;self.cursor=db.cursor()
 def __enter__(self):return self
 def __exit__(self,*a):self.cursor.close()
 def execute(self,sql,values=()):
  if sql.startswith('START TRANSACTION'):return self
  sql=re.sub(r'CONVERT\((.*?) USING utf8mb4\) COLLATE utf8mb4_bin',r'\1 COLLATE BINARY',sql)
  self.cursor.execute(sql.replace('%s','?'),values);return self
 def fetchone(self):
  row=self.cursor.fetchone();return dict(row) if row is not None else None
 def fetchall(self):return [dict(r) for r in self.cursor.fetchall()]
class Connection:
 def __init__(self,db):self.db=db
 def __enter__(self):return self
 def __exit__(self,*a):pass
 def cursor(self):return Cursor(self.db)
 def rollback(self):self.db.rollback()
class Tests(unittest.TestCase):
 def setUp(self):
  self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
  self.db.create_function('CONCAT',-1,lambda *v:''.join(str(x) for x in v))
  self.db.executescript((ROOT/'database/freeradius_standard.sql').read_text(encoding='utf8'))
  for table in ('radacct','radacct_archive','wisp_imported_session_history','wisp_traffic_history'):
   if not self.db.execute("SELECT name FROM sqlite_master WHERE name=?",(table,)).fetchone():self.db.execute('CREATE TABLE '+table+' AS SELECT * FROM radacct WHERE 0')
   fields={r[1] for r in self.db.execute('PRAGMA table_info('+table+')')}
   for field in ('calledstationid','nasportid','framedprotocol','servicetype','framedipv6address','framedipv6prefix','framedinterfaceid','delegatedipv6prefix','source_fallback'):
    if field not in fields:self.db.execute('ALTER TABLE '+table+' ADD COLUMN '+field+' TEXT')
   self.db.execute('CREATE VIEW '+table+'_owned AS SELECT *,username AS original_username FROM '+table)
  def query(q,p):return [dict(r) for r in self.db.execute(q,p)]
  env=dict(query_all=query,format_bytes=str,format_duration=str,enrich_session_display=lambda *a:None)
  for file,names in [('services/session_history_service.py',('history_sql','get_session_history','format_history_rows')),('services/subscriber_service.py',('get_subscriber_sessions','get_user_usage_analytics'))]:
   nodes=[n for n in ast.parse((ROOT/file).read_text(encoding='utf8')).body if isinstance(n,ast.FunctionDef) and n.name in names]
   exec(compile(ast.Module(body=nodes,type_ignores=[]),file,'exec'),env)
  self.env=env
  self.patchers=[patch('database.db.get_connection',lambda:Connection(self.db)),patch('core.ownership.owner_id',lambda:None),patch('services.session_history_service.get_session_history',env['get_session_history'])]
  for p in self.patchers:p.start()
  self.insert('radacct','summary',200,'2024-01-01','Consolidated-Historical-Import')
 def tearDown(self):
  for p in reversed(self.patchers):p.stop()
  self.db.close()
 def insert(self,table,key,bytes,date,cause=''):
  self.db.execute(f'INSERT INTO {table}(acctsessionid,acctuniqueid,username,acctstarttime,acctinputoctets,acctoutputoctets,acctsessiontime,acctterminatecause) VALUES (?,?,?,?,?,?,?,?)',(key,key,'test',date,bytes,0,60,cause));self.db.commit()
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

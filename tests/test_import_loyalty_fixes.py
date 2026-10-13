"""Isolated regressions; actual concurrency is checked separately on MariaDB."""
import ast,logging,sqlite3,types,sys,unittest,time
from pathlib import Path
from decimal import Decimal,InvalidOperation
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
def load(path,names,ns):
 t=ast.parse((ROOT/path).read_text(encoding='utf8'))
 exec(compile(ast.Module(body=[x for x in t.body if isinstance(x,ast.FunctionDef) and x.name in names],type_ignores=[]),path,'exec'),ns)
class FixTests(unittest.TestCase):
 def test_import_rate_direction_open_burst_and_minimum(self):
  rate={};t=ast.parse((ROOT/'core/rate_limit.py').read_text(encoding='utf8'))
  exec(compile(ast.Module(body=[x for x in t.body if isinstance(x,ast.FunctionDef)],type_ignores=[]),'rate','exec'),rate)
  module=types.ModuleType('core.rate_limit');module.build_mikrotik_rate_limit=rate['build_mikrotik_rate_limit']
  ns={};load('services/import_state_service.py',{'imported_policy_rate'},ns)
  cases=[({'rate_download':'2M','rate_upload':'1M'},'1M/2M'),({},'0/0'),({'rate_download':'2M','rate_upload':'1M','burst_download':'4M','burst_upload':'3M','burst_threshold_down':'1M','burst_threshold_up':'512k','burst_time':10,'priority':3,'min_download':'512k','min_upload':'256k'},'1M/2M 3M/4M 512k/1M 10/10 3 256k/512k')]
  with patch.dict(sys.modules,{'core.rate_limit':module}):
   for plan,expected in cases:
    with self.subTest(plan=plan):self.assertEqual(ns['imported_policy_rate'](plan),expected)
 def fixture(self,fail_ledger=False,deadlock_once=False):
  conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
  conn.executescript('CREATE TABLE wisp_loyalty_wallets(username TEXT UNIQUE COLLATE NOCASE,points_balance INT,total_points_earned INT,tier_level TEXT);CREATE TABLE wisp_loyalty_transactions(username TEXT,transaction_type TEXT,points INT,balance_after INT,notes TEXT);')
  deadlocked=[]
  class Cursor:
   def __enter__(s):return s
   def __exit__(s,*a):pass
   def execute(s,q,args=()):
    if deadlock_once and not deadlocked and 'INSERT INTO wisp_loyalty_transactions' in q:deadlocked.append(True);raise RuntimeError(1213,'confirmed deadlock fixture')
    if fail_ledger and 'INSERT INTO wisp_loyalty_transactions' in q:raise RuntimeError('fixture ledger unavailable')
    q=q.replace('ON DUPLICATE KEY UPDATE username=username','ON CONFLICT(username) DO NOTHING').replace('FOR UPDATE','').replace('%s','?');s.cursor=conn.execute(q,args)
   def fetchone(s):
    x=s.cursor.fetchone();return dict(x) if x else None
  db=types.SimpleNamespace(cursor=Cursor,commit=conn.commit,rollback=conn.rollback,close=lambda:None)
  ns=dict(time=time,Decimal=Decimal,InvalidOperation=InvalidOperation,ensure_loyalty_tables=lambda:None,_get_db=lambda:db,logger=logging.getLogger('fixture'))
  load('services/loyalty_rewards_service.py',{'award_loyalty_points','calculate_tier'},ns)
  self.addCleanup(conn.close);return conn,ns['award_loyalty_points']
 def test_case_identity_grant_debit_and_reject_overdraw(self):
  c,grant=self.fixture();self.assertTrue(grant('Fixture',100)[0]);self.assertTrue(grant('fixture',5)[0]);self.assertTrue(grant('FIXTURE',-7)[0]);self.assertFalse(grant('fixture',-100)[0])
  self.assertEqual(c.execute('SELECT points_balance,total_points_earned FROM wisp_loyalty_wallets').fetchone()[:],(98,105));self.assertEqual(c.execute('SELECT SUM(points) FROM wisp_loyalty_transactions').fetchone()[0],98)
 def test_invalid_points_and_missing_negative_wallet(self):
  c,grant=self.fixture()
  for x in ('nan','inf',1.5,'wrong',2147483648,0):self.assertFalse(grant('fixture',x)[0])
  self.assertFalse(grant('missing',-5)[0]);self.assertEqual(c.execute('SELECT COUNT(*) FROM wisp_loyalty_wallets').fetchone()[0],0)
 def test_first_wallet_and_points_roll_back_if_ledger_fails(self):
  c,grant=self.fixture(True);self.assertFalse(grant('fixture',100)[0]);self.assertEqual(c.execute('SELECT COUNT(*) FROM wisp_loyalty_wallets').fetchone()[0],0)
 def test_integer_overflow_keeps_wallet_and_ledger(self):
  c,grant=self.fixture();self.assertTrue(grant('fixture',2147483647)[0]);self.assertFalse(grant('fixture',1)[0]);self.assertEqual(c.execute('SELECT COUNT(*) FROM wisp_loyalty_transactions').fetchone()[0],1)
 def test_confirmed_deadlock_retries_whole_transaction_once(self):
  c,grant=self.fixture(deadlock_once=True);self.assertTrue(grant('fixture',100)[0]);self.assertEqual(c.execute('SELECT points_balance FROM wisp_loyalty_wallets').fetchone()[0],100);self.assertEqual(c.execute('SELECT COUNT(*) FROM wisp_loyalty_transactions').fetchone()[0],1)
if __name__=='__main__':unittest.main()

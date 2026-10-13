"""New owned fixtures, guarded to a disposable MariaDB only."""
import ast,os,sys,json,uuid,threading,copy,time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]));assert os.environ.get('FIXES_MARIADB_REGRESSION')=='1' and os.environ.get('DB_NAME')=='firstpolicy_test','Explicit disposable DB opt-in required'
from database.db import query_one,query_all,execute_write,db_session
from services.loyalty_rewards_service import award_loyalty_points,redeem_reward
from services.mikrotik_userman_importer import execute_userman_import
from core.radius_sync import sync_package_to_radius
from services.import_service import execute_import
from services import import_state_service
prefix='fix'+uuid.uuid4().hex[:8];results=[]
def expect(cond,msg):
 if not cond:raise AssertionError(msg)
def check(fn):
 try:fn();results.append(dict(name=fn.__name__,passed=True))
 except Exception as e:results.append(dict(name=fn.__name__,passed=False,error=type(e).__name__+': '+str(e)))
def wallet(u):return query_one('SELECT * FROM wisp_loyalty_wallets WHERE username=%s',(u,))
def ledger(u):return query_one('SELECT COALESCE(SUM(points),0) n FROM wisp_loyalty_transactions WHERE username=%s',(u,))['n']
def race_existing_and_cold_wallets():
 for seed in (0,100):
  for _ in range(3):
   u=prefix+uuid.uuid4().hex[:8]
   if seed:expect(award_loyalty_points(u,seed)[0],'seed')
   gate=threading.Barrier(20)
   def give(_):gate.wait();return award_loyalty_points(u,5)
   with ThreadPoolExecutor(max_workers=20) as pool:out=list(pool.map(give,range(20)))
   w=wallet(u);expect(all(r[0] for r in out),'parallel award rejection')
   expect(w['points_balance']==seed+100 and w['total_points_earned']==seed+100 and ledger(u)==seed+100,'wallet/ledger race mismatch')
def mixed_grant_and_deduction():
 u=prefix+uuid.uuid4().hex[:8];expect(award_loyalty_points(u,100)[0],'seed')
 values=[5,-3]*10
 with ThreadPoolExecutor(max_workers=10) as pool:out=list(pool.map(lambda n:award_loyalty_points(u,n),values))
 expect(all(r[0] for r in out),'mixed race rejection');w=wallet(u)
 expect(w['points_balance']==120 and w['total_points_earned']==150 and ledger(u)==120,'mixed race ledger mismatch')
def parallel_overdraw_rejected():
 u=prefix+uuid.uuid4().hex[:8];award_loyalty_points(u,10)
 with ThreadPoolExecutor(max_workers=4) as pool:out=list(pool.map(lambda _:award_loyalty_points(u,-7),range(4)))
 expect(sum(r[0] for r in out)==1 and wallet(u)['points_balance']==3 and ledger(u)==3,'overdraw changed ledger')
def ledger_failure_rolls_back():
 u=prefix+uuid.uuid4().hex[:8]
 with db_session() as c:c.cursor().execute("CREATE TRIGGER fixture_loyalty_failure BEFORE INSERT ON wisp_loyalty_transactions FOR EACH ROW BEGIN IF NEW.username='"+u+"' THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='fixture ledger unavailable'; END IF; END")
 try:expect(not award_loyalty_points(u,100)[0],'forced failure accepted');expect(wallet(u) is None and ledger(u)==0,'failure retained wallet')
 finally:
  with db_session() as c:c.cursor().execute('DROP TRIGGER fixture_loyalty_failure')
def grant_and_redeem_same_wallet():
 u=prefix+uuid.uuid4().hex[:8]
 p=query_one('SELECT id FROM wisp_packages ORDER BY id LIMIT 1')['id']
 execute_write("INSERT INTO wisp_subscribers(username,password,full_name,package_id,status,balance) VALUES(%s,'fixture-pass',%s,%s,'active',0)",(u,u,p))
 execute_write('INSERT INTO wisp_system_settings (`key`,`value`) VALUES("portal_enable_loyalty","1") ON DUPLICATE KEY UPDATE `value`="1"')
 award_loyalty_points(u,100)
 rid=execute_write('INSERT INTO wisp_loyalty_rewards(reward_name,points_cost,reward_type,reward_value,is_active) VALUES(%s,10,"balance_credit",1,1)',(prefix,))
 values=['grant','redeem']*10
 def act(v):return award_loyalty_points(u,5) if v=='grant' else redeem_reward(u,rid)
 with ThreadPoolExecutor(max_workers=10) as pool:out=list(pool.map(act,values))
 expect(all(r[0] for r in out),'grant/redeem rejected');expect(wallet(u)['points_balance']==50 and ledger(u)==50,'grant/redeem points mismatch')
 expect(query_one('SELECT balance FROM wisp_subscribers WHERE username=%s',(u,))['balance']==10,'reward cash duplicated/lost')
def userman_collisions_chunks_duplicates_and_cancel():
 profiles=[dict(name='SAMEAA-'+prefix+str(i),quota_mb=64,rate_down='2M',rate_up='1M',price=10,validity_value=30,validity_unit='days') for i in range(2)]
 users=[dict(username=prefix+str(i).zfill(5),password='fixture-pass',actual_profile=profiles[i%2]['name']) for i in range(450)]
 cache=None
 for idx in range(3):
  ret=execute_userman_import(dict(profiles=profiles,users=users[idx*200:(idx+1)*200]),import_consumption=True,chunk_index=idx,total_chunks=3,state_cache=copy.deepcopy(cache))
  expect(ret['success'],'chunk '+str(idx)+': '+str(ret.get('error')));cache=ret['state_cache']
 expect(query_one('SELECT COUNT(*) n FROM wisp_vouchers WHERE username LIKE %s',(prefix+'%',))['n']==450,'chunk cards missing')
 batches=query_all('SELECT b.batch_number FROM wisp_voucher_batches b JOIN wisp_packages p ON p.id=b.package_id WHERE p.name IN (%s,%s)',tuple(p['name'] for p in profiles))
 expect(len(batches)==2 and len({x['batch_number'] for x in batches})==2 and all(len(x['batch_number'])<=30 for x in batches),'colliding or oversized batch number')
 ret=execute_userman_import(dict(profiles=profiles,users=users),import_consumption=True)
 expect(ret['success'] and ret['stats']['created_users']==0 and ret['stats']['skipped_users']==450,'repeated source duplicates')
 cancel_name='CANCEL-'+uuid.uuid4().hex[:10];calls=[]
 def cancel():
  calls.append(1)
  if len(calls)>=2:raise RuntimeError('owned cancellation')
 ret=execute_userman_import(dict(profiles=[dict(profiles[0],name=cancel_name)],users=[dict(users[0],username=prefix+'cancel'+str(i),actual_profile=cancel_name) for i in range(3)]),check_cancel=cancel)
 expect(not ret['success'],'cancel ignored');expect(query_one('SELECT COUNT(*) n FROM wisp_packages WHERE name=%s',(cancel_name,))['n']==0,'cancel left package')

with patch('services.license_guard_service.get_active_license_status',return_value=dict(valid=True,status='active')),patch('services.bot_notifications_service.trigger_recharge_notification',return_value=None):
 for fn in (race_existing_and_cold_wallets,mixed_grant_and_deduction,parallel_overdraw_rejected,ledger_failure_rolls_back,grant_and_redeem_same_wallet,userman_collisions_chunks_duplicates_and_cancel):check(fn)
Path('/tmp/fixes-regressions.json').write_text(json.dumps(dict(results=results,passed=sum(r['passed'] for r in results),total=len(results)),ensure_ascii=False,indent=2),encoding='utf8')
print(json.dumps(dict(passed=sum(r['passed'] for r in results),total=len(results),failed=[r for r in results if not r['passed']])))

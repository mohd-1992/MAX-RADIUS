"""Explicit opt-in MariaDB / actual UDP RADIUS suite on a disposable DB only.

DB_NAME must be firstpolicy_test. Never creates app workers or touches a NAS.
License lease is a bounded fixture, not a licensing certification.
Run with FIRST_POLICY_RADIUS_IP and FIRST_POLICY_RADIUS_SECRET injected privately.
"""
import os, sys, json, uuid, datetime, socket, struct, hashlib, hmac, time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
sys.path.insert(0, '/app')
from database.db import db_session, query_one, query_all, execute_write
from database.first_use_policy import install_first_use_policy, FIELDS
from core.radius_sync import sync_package_to_radius, sync_subscriber_to_radius, sync_voucher_to_radius
from services.voucher_service import generate_voucher_batch, activate_voucher_card, get_batch_cards_for_print, update_voucher_batch, update_voucher_card
from services.subscriber_service import create_subscriber
from services.quick_action_service import action_renew_package, action_change_package
from services.user_portal_service import renew_or_change_package, recharge_user_wallet_by_card
from services.import_service import execute_import

assert os.environ.get('DB_NAME') == 'firstpolicy_test', 'Disposable DB required'
SECRET=os.environ['FIRST_POLICY_RADIUS_SECRET'].encode()
IP=os.environ['FIRST_POLICY_RADIUS_IP']
RUN='fp'+uuid.uuid4().hex[:8]
results=[]
def check(name, fn):
 started=time.monotonic()
 try:fn();results.append(dict(name=name,passed=True,seconds=round(time.monotonic()-started,3)))
 except Exception as e:
  # Only synthetic test identities in errors; redact even these from stdout.
  results.append(dict(name=name,passed=False,error=type(e).__name__+': '+str(e),seconds=round(time.monotonic()-started,3)))
def expect(condition,why):
 if not condition:raise AssertionError(why)
def row(table,id):return query_one('SELECT * FROM '+table+' WHERE id=%s',(id,))
def attrs(kind,value):
 if isinstance(value,str):value=value.encode()
 return bytes([kind,len(value)+2])+value
def radius(user,password='fixture-pass',accounting=None,session=None,up=0,down=0):
 ident=os.urandom(1)[0];auth=os.urandom(16);code=1
 a=attrs(1,user)+attrs(4,socket.inet_aton('127.0.0.1'))+attrs(5,struct.pack('!I',1))
 if accounting:
  code=4;auth=b'\0'*16
  a+=attrs(40,struct.pack('!I',accounting))+attrs(44,session)+attrs(46,struct.pack('!I',5))
  a+=attrs(42,struct.pack('!I',up%2**32))+attrs(43,struct.pack('!I',down%2**32))
  a+=attrs(52,struct.pack('!I',up//2**32))+attrs(53,struct.pack('!I',down//2**32))
 else:
  clear=password.encode();clear+=b'\0'*((16-len(clear)%16)%16)
  enc=b'';prev=auth
  for pos in range(0,len(clear),16):
   mask=hashlib.md5(SECRET+prev).digest();prev=bytes(x^y for x,y in zip(clear[pos:pos+16],mask));enc+=prev
  a+=attrs(2,enc)+attrs(80,b'\0'*16)
 header=struct.pack('!BBH',code,ident,20+len(a))
 if code==1:
  packet=header+auth+a;ma=hmac.new(SECRET,packet,hashlib.md5).digest();a=a[:-16]+ma
 else:auth=hashlib.md5(header+auth+a+SECRET).digest()
 packet=header+auth+a
 with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as s:
  s.settimeout(5);s.sendto(packet,(IP,1813 if accounting else 1812));data=s.recv(65535)
 expect(data[1]==ident,'Response identifier mismatch')
 expect(hmac.compare_digest(data[4:20],hashlib.md5(data[:4]+auth+data[20:]+SECRET).digest()),'Invalid RADIUS response authenticator')
 out={};pos=20
 while pos<len(data):
  typ,n=data[pos:pos+2];v=data[pos+2:pos+n];pos+=n
  if typ==26 and len(v)>=6:
   vendor=struct.unpack('!I',v[:4])[0];sub=v[4];out[(vendor,sub)]=v[6:]
  else:out[typ]=v
 return data[0],out
def quota_reply(out):return struct.unpack('!I',out.get((14988,17),b'\0'*4))[0]+2**32*struct.unpack('!I',out.get((14988,18),b'\0'*4))[0]
def setpkg(id,quota=1536,price=15,cost=7,down='3M',up='2M',**extra):
 fields=dict(volume_quota_mb=quota,price=price,cost=cost,rate_download=down,rate_upload=up,**extra)
 with db_session() as c:
  cur=c.cursor();cur.execute('UPDATE wisp_packages SET '+','.join(k+'=%s' for k in fields)+' WHERE id=%s',tuple(fields.values())+(id,))
  expect(sync_package_to_radius(id,conn=c),'Package RADIUS sync failed')
def package():
 id=execute_write("INSERT INTO wisp_packages(name,price,cost,volume_quota_mb,rate_download,rate_upload,burst_download,burst_upload,burst_threshold_down,burst_threshold_up,min_download,min_upload,validity_value,validity_unit,validity_days,service_type,is_active,is_rollover_enabled,show_in_portal,mikrotik_group,simultaneous_sessions) VALUES(%s,10,4,1024,'2M','1M','0','0','0','0','0','0',30,'days',30,'hotspot',1,0,1,'',8)",(RUN+uuid.uuid4().hex[:6],))
 expect(sync_package_to_radius(id),'Initial package sync failed');return id
def generate(p,n=1):
 b,_=generate_voucher_batch(RUN,p,n,prefix=RUN,code_length=8,pin_only=False)
 # Service returns batch ID.
 return query_all('SELECT * FROM wisp_vouchers WHERE batch_id=%s ORDER BY id',(b,))
def manual_sub(p):
 name=RUN+uuid.uuid4().hex[:6]
 sid=create_subscriber(dict(username=name,password='fixture-pass',package_id=p,service_type='hotspot',status='active',balance=100))
 return row('wisp_subscribers',sid)
def close_auth(card,out=None):
 session=uuid.uuid4().hex
 expect(radius(card['username'],accounting=1,session=session)[0]==5,'Start not acknowledged')
 expect(radius(card['username'],accounting=3,session=session,up=2**32+2**20,down=3*2**20)[0]==5,'Interim not acknowledged')
 expect(radius(card['username'],accounting=2,session=session,up=2**32+2**20,down=3*2**20)[0]==5,'Stop not acknowledged')
 return session
def snapshot(v):return tuple(v.get('snap_'+f) for f in FIELDS)

def baseline():
 used='v.policy_frozen_at IS NOT NULL'
 comparisons=' OR '.join('NOT(v.snap_'+f+' <=> o.snap_'+f+')' for f in FIELDS)
 n=query_one('SELECT COUNT(*) n FROM wisp_vouchers v JOIN radius_wisp.wisp_vouchers o ON o.id=v.id WHERE '+used+' AND ('+comparisons+')')['n']
 expect(n==0,'Migration changed historical voucher snapshots')
 expect(query_one('SELECT COUNT(*) n FROM wisp_subscribers WHERE snap_volume_quota_mb IS NULL')['n']==0,'Subscribers left without frozen quota')
 expect(query_one('SELECT COUNT(*) n FROM wisp_vouchers WHERE policy_frozen_at IS NULL AND snap_volume_quota_mb IS NOT NULL')['n']==0,'Fresh migration did not clear issuance snapshot')
 for t in ('wisp_voucher_sales','wisp_invoices','wisp_manager_invoices'):
  expect(query_one('SELECT COUNT(*) n FROM '+t+' WHERE id <= (SELECT COALESCE(MAX(id),0) FROM radius_wisp.'+t+')')['n']==query_one('SELECT COUNT(*) n FROM radius_wisp.'+t)['n'],'Migration changed ledger count')
def migration_twice():
 with db_session() as c:install_first_use_policy(c);install_first_use_policy(c)
 baseline()
def old_unused():
 p=package()
 # Mimic pre-change issuance snapshots: same shape as legacy generation/import.
 batch=generate(p)[0]['batch_id']
 u=RUN+uuid.uuid4().hex[:6]
 cid=execute_write("INSERT INTO wisp_vouchers(batch_id,package_id,serial_number,username,password,pin_code,status,snap_price,snap_cost,snap_volume_quota_mb,snap_rate_download,snap_rate_upload,snap_rate_limit_str) VALUES(%s,%s,%s,%s,'fixture-pass',%s,'unused',10,4,1024,'2M','1M','1M/2M')",(batch,p,u,u,u))
 expect(sync_voucher_to_radius(cid),'Legacy card sync failed');v=row('wisp_vouchers',cid)
 expect(snapshot(v)==(None,)*6,'Old unused insertion not live')
 setpkg(p);code,out=radius(u)
 expect(code==2 and quota_reply(out)==1536*1048576,'First response is stale quota')
 expect(out.get((14988,8))==b'2M/3M','First response is stale rate')
 v=row('wisp_vouchers',cid);expect(v['snap_price']==15 and v['snap_cost']==7,'Price/cost not current')
 expect(v['status']=='unused' and v['policy_frozen_at'] is not None,'Acceptance marker missing or premature activation')
 frozen=snapshot(v);setpkg(p,quota=2048,price=20,cost=8,down='4M',up='3M')
 code,out=radius(u);expect(code==2 and quota_reply(out)==1536*1048576 and out[(14988,8)]==b'2M/3M','Lost Start changes accepted contract')
 expect(snapshot(row('wisp_vouchers',cid))==frozen,'Accepted snapshot was rewritten')
 session=close_auth(v)
 v=row('wisp_vouchers',cid);expect(v['status']=='active' and snapshot(v)==frozen,'Accounting activation loses policy')
 sale=query_one('SELECT * FROM wisp_voucher_sales WHERE first_activation_voucher_id=%s',(cid,));expect(sale['price']==15 and sale['cost']==7,'Activation sale does not match contract')
 radius(u,accounting=2,session=session,up=2**32+2**20,down=3*2**20)
 expect(query_one('SELECT COUNT(*) n FROM wisp_voucher_sales WHERE first_activation_voucher_id=%s',(cid,))['n']==1,'Duplicate activation sale')
 expect(query_one('SELECT acctinputoctets FROM radacct WHERE username=%s AND acctsessionid=%s',(u,session))['acctinputoctets']==2**32+2**20,'64-bit accounting regression')
 expect(radius(u)[0]==3,'Exhausted frozen quota accepted')
def generated():
 p=package();cards=generate(p,3);setpkg(p)
 for v in cards:
  code,out=radius(v['username'],v['password']);expect(code==2 and quota_reply(out)==1536*1048576,'Generated card wrong first response')
  expect(row('wisp_vouchers',v['id'])['snap_price']==15,'Generated card wrong price')
 code,out=radius(generate(p)[0]['username']) # password mismatch is intentional
 expect(code==3,'Wrong generated password accepted')
def rejection():
 p=package();v=generate(p)[0]
 expect(radius(v['username'],'invalid')[0]==3,'Bad password accepted')
 expect(row('wisp_vouchers',v['id'])['policy_frozen_at'] is None,'Failed login froze contract')
 execute_write('UPDATE wisp_license_runtime_state SET is_valid=0 WHERE id=1')
 try:expect(radius(v['username'],v['password'])[0]==3,'Unavailable license accepted')
 finally:execute_write('UPDATE wisp_license_runtime_state SET is_valid=1 WHERE id=1')
 expect(row('wisp_vouchers',v['id'])['policy_frozen_at'] is None,'Rejected license froze contract')
 execute_write("UPDATE wisp_vouchers SET status='suspended' WHERE id=%s",(v['id'],))
 expect(radius(v['username'],v['password'])[0]==3,'Suspended voucher accepted')
def used_imports():
 p=package();old=query_one('SELECT * FROM wisp_packages WHERE id=%s',(p,));rows=[]
 for used in (False,True):
  u=RUN+uuid.uuid4().hex[:6]
  rows.append(dict(username=u,password='fixture-pass',pin_code=u,serial_number=u,package_id=p,package_name=old['name'],matched_pkg=old,reseller_id=None,download_used_mb=1 if used else 0,upload_used_mb=0,uptime_used_mins=1 if used else 0))
 ret=execute_import('vouchers',rows,batch_name=RUN,import_consumption=True);expect(ret[0],'Actual file import failed')
 fresh,used=[query_one('SELECT * FROM wisp_vouchers WHERE username=%s',(r['username'],)) for r in rows]
 fixed=snapshot(used);expect(used['policy_frozen_at'] is not None and fresh['policy_frozen_at'] is None,'Imported history not distinguished')
 setpkg(p)
 code,out=radius(fresh['username']);expect(code==2 and quota_reply(out)==1536*1048576,'Fresh imported card did not adopt package')
 code,out=radius(used['username']);expect(code==2 and quota_reply(out)==1023*1048576,'Used imported card lost old quota/history')
 expect(out[(14988,8)]==b'1M/2M','Imported historical speed is not in upload/download order')
 expect(snapshot(row('wisp_vouchers',used['id']))==fixed,'Used import snapshot changed')
 with db_session() as c:
  cur=c.cursor();cur.execute('SET @max_radius_import=1');cur.execute('UPDATE wisp_vouchers SET snap_volume_quota_mb=9999,snap_price=99 WHERE id=%s',(used['id'],));cur.execute('SET @max_radius_import=0')
 expect(snapshot(row('wisp_vouchers',used['id']))==fixed,'Reimport overwrote used policy')
def unlimited_and_burst():
 p=package();setpkg(p,quota=0,down='0',up='0');v=generate(p)[0]
 code,out=radius(v['username'],v['password']);expect(code==2 and (14988,8) not in out and (14988,17) not in out,'Open policy sent rate/limit')
 setpkg(p);code,out=radius(v['username'],v['password']);expect(code==2 and (14988,8) not in out and (14988,17) not in out,'Frozen open became limited')
 p=package();v=generate(p)[0];setpkg(p,quota=5120,burst_download='6M',burst_upload='4M',burst_threshold_down='2M',burst_threshold_up='1M',min_download='1M',min_upload='512k',burst_time=10)
 expected=query_one("SELECT value FROM radgroupreply WHERE groupname=(SELECT name FROM wisp_packages WHERE id=%s) AND attribute='Mikrotik-Rate-Limit'",(p,))['value'].encode()
 code,out=radius(v['username'],v['password']);expect(code==2 and out[(14988,8)]==expected,'Burst/minimum parameters lost')
 expect(quota_reply(out)==5120*1048576,'Gigawords quota split incorrect')
def subscriber_cycles():
 p=package();s=manual_sub(p);billing=manual_sub(p);expect(s['snap_volume_quota_mb']==1024,'Creation missing subscriber snapshot')
 setpkg(p);code,out=radius(s['username']);expect(code==2 and quota_reply(out)==1024*1048576,'Subscriber follows changed package before renewal')
 # Finish zero-usage accounting so the paid settlement path sees a final Stop.
 session=uuid.uuid4().hex;radius(s['username'],accounting=1,session=session);radius(s['username'],accounting=2,session=session)
 # Missing NAS metadata is correctly rejected even after a synthetic Stop.
 expect(not action_renew_package('subscriber',s['id'])[0],'Missing NAS safety guard bypassed')
 # Full paid paths on a separate offline account do not need fake RouterOS data.
 s=billing
 time.sleep(1.1)
 ret=action_renew_package('subscriber',s['id']);expect(ret[0],'Admin renewal failed: '+str(ret[1]))
 expect(row('wisp_subscribers',s['id'])['snap_volume_quota_mb']==1536,'Admin renewal did not freeze new quota')
 setpkg(p,quota=2048);expect(row('wisp_subscribers',s['id'])['snap_volume_quota_mb']==1536,'Renewed cycle changed with package')
 time.sleep(1.1)
 ret=renew_or_change_package(s['username'],p);expect(ret[0],'Portal renewal failed: '+str(ret[1]))
 expect(row('wisp_subscribers',s['id'])['snap_volume_quota_mb']==2048,'Portal renewal quota incorrect')
 q=package();time.sleep(1.1);ret=action_change_package('subscriber',s['id'],q);expect(ret[0],'Admin package change failed: '+str(ret[1]))
 expect(row('wisp_subscribers',s['id'])['snap_volume_quota_mb']==1024,'Admin change snapshot incorrect')
 setpkg(q,quota=0);time.sleep(1.1);ret=renew_or_change_package(s['username'],q);expect(ret[0],'Portal unlimited renewal failed: '+str(ret[1]))
 expect(row('wisp_subscribers',s['id'])['snap_volume_quota_mb']==0,'Unlimited subscriber quota not frozen')
 setpkg(q);expect(row('wisp_subscribers',s['id'])['snap_volume_quota_mb']==0,'Open subscriber became limited without renewal')
def race():
 p=package();v=generate(p)[0];setpkg(p)
 def freeze(_):
  with db_session() as c:
   cur=c.cursor();cur.execute('SELECT fn_freeze_voucher_policy(%s) AS r',(v['username'],));return cur.fetchone()['r']
 with ThreadPoolExecutor(max_workers=8) as pool:
  outcomes=list(pool.map(freeze,range(16)))
  expect(all(x in (2,3) for x in outcomes) and outcomes.count(3)==1,'Parallel freeze failed or initialized more than once')
 snap=snapshot(row('wisp_vouchers',v['id']));expect(snap[:3]==(15,7,1536),'Parallel snapshot inconsistent')
 setpkg(p,quota=2000,price=33,cost=12)
 with ThreadPoolExecutor(max_workers=8) as pool:expect(all(x==2 for x in pool.map(freeze,range(16))),'Repeated freeze failed')
 expect(snapshot(row('wisp_vouchers',v['id']))==snap,'Parallel retries changed contract')
 # Concurrent backend activations must record one sale.
 with ThreadPoolExecutor(max_workers=4) as pool:out=list(pool.map(lambda _:activate_voucher_card(v['username']),range(4)))
 expect(out.count(True)==1,'Concurrent Python activation was not idempotent')
 expect(query_one('SELECT COUNT(*) n FROM wisp_voucher_sales WHERE first_activation_voucher_id=%s',(v['id'],))['n']==1,'Concurrent activation duplicate sale')
def print_policy():
 p=package();v=generate(p)[0];setpkg(p)
 batch,cards,_=get_batch_cards_for_print(v['batch_id'])
 expect(cards[0]['price']==15 and cards[0]['quota']=='1.5 GB','Print policy not live for unused card')
 expect(activate_voucher_card(v['username']),'Manual activation failed')
 fixed=snapshot(row('wisp_vouchers',v['id']));setpkg(p,quota=2048,price=30)
 _,cards,_=get_batch_cards_for_print(v['batch_id']);expect(cards[0]['price']==15 and snapshot(row('wisp_vouchers',v['id']))==fixed,'Used card print repriced')
 try:update_voucher_batch(v['batch_id'],RUN,package())
 except ValueError:pass
 else:raise AssertionError('Used batch reassignment allowed')
def rollback():
 p=package();v=generate(p)[0]
 with db_session() as c:
  cur=c.cursor();cur.execute('SELECT fn_freeze_voucher_policy(%s)',(v['username'],));c.rollback()
 expect(row('wisp_vouchers',v['id'])['policy_frozen_at'] is None,'Rollback persisted policy')
def recharge_contracts():
 p=package();bill=manual_sub(p);donor=generate(p)[0];setpkg(p)
 expect(recharge_user_wallet_by_card(bill['username'],donor['username'],'balance')[0],'Balance recharge failed')
 expect(row('wisp_subscribers',bill['id'])['balance']==115,'Recharge did not use current price')
 fixed=snapshot(row('wisp_vouchers',donor['id']));setpkg(p,price=40,cost=20)
 expect(snapshot(row('wisp_vouchers',donor['id']))==fixed,'Consumed recharge card policy changed')
 expect(not recharge_user_wallet_by_card(bill['username'],donor['username'],'balance')[0],'Double recharge accepted')
 p=package();target=manual_sub(p);donor=generate(p)[0];setpkg(p,quota=1536,burst_download='6M',burst_upload='4M')
 expected=query_one("SELECT value FROM radgroupreply WHERE groupname=(SELECT name FROM wisp_packages WHERE id=%s) AND attribute='Mikrotik-Rate-Limit'",(p,))['value']
 expect(recharge_user_wallet_by_card(target['username'],donor['username'],'package')[0],'Package recharge failed')
 expect(row('wisp_subscribers',target['id'])['snap_volume_quota_mb']==1536,'Recharge subscriber cycle quota incorrect')
 expect(row('wisp_vouchers',donor['id'])['snap_rate_limit_str']==expected,'Recharge burst policy lost')
 sale=query_one('SELECT * FROM wisp_voucher_sales WHERE voucher_id=%s',(donor['id'],));expect(sale['cost']==7 and sale['price']==15,'Recharge sale commercial policy incorrect')
 # Accepted card cannot be simultaneously spent as a recharge voucher.
 pending=generate(p)[0];expect(radius(pending['username'],pending['password'])[0]==2,'Pending accept failed')
 expect(not recharge_user_wallet_by_card(target['username'],pending['username'],'balance')[0],'Accepted pending card spent twice')
def rollover_and_loan():
 p=package();s=manual_sub(p);setpkg(p,quota=1536,is_rollover_enabled=1)
 execute_write('UPDATE wisp_subscribers SET extra_quota_mb=10,loan_balance_mb=200,loan_status=1 WHERE id=%s',(s['id'],))
 expect(action_renew_package('subscriber',s['id'])[0],'Rollover/loan renewal failed')
 v=row('wisp_subscribers',s['id'])
 expect(v['snap_volume_quota_mb']==1536 and v['extra_quota_mb']==834 and v['loan_balance_mb']==0,'Old quota rollover or loan settlement incorrect')
def voucher_cycles():
 p=package();v=generate(p)[0];expect(activate_voucher_card(v['username']),'Offline card activation failed')
 execute_write('UPDATE wisp_vouchers SET balance=100 WHERE id=%s',(v['id'],));setpkg(p)
 time.sleep(1.1);expect(action_renew_package('voucher',v['id'])[0],'Admin voucher renewal failed')
 expect(row('wisp_vouchers',v['id'])['snap_volume_quota_mb']==1024,'Existing same-package voucher renewal rule changed')
 time.sleep(1.1);expect(renew_or_change_package(v['username'],p)[0],'Portal voucher renewal failed')
 expect(row('wisp_vouchers',v['id'])['snap_volume_quota_mb']==1024,'Portal voucher renewal discarded snapshot')
 q=package();setpkg(q,quota=1600);time.sleep(1.1)
 expect(action_change_package('voucher',v['id'],q)[0],'Admin voucher package change failed')
 expect(row('wisp_vouchers',v['id'])['snap_volume_quota_mb']==1600,'Explicit voucher change did not adopt new quota')
 sales=query_one('SELECT COUNT(*) n,SUM(first_activation_voucher_id IS NOT NULL) activations FROM wisp_voucher_sales WHERE voucher_id=%s',(v['id'],))
 expect(sales['n']==4 and sales['activations']==1,'Renewal sales suppressed or activation duplicated')
def imported_subscribers():
 p=package();plan=query_one('SELECT * FROM wisp_packages WHERE id=%s',(p,));rows=[]
 for used in (False,True):
  u=RUN+uuid.uuid4().hex[:6]
  rows.append(dict(username=u,password='fixture-pass',full_name=RUN,phone='',service_type='hotspot',mac_binding='',static_ip='',notes='',package_id=p,package_name=plan['name'],matched_pkg=plan,reseller_id=None,download_used_mb=1 if used else 0,upload_used_mb=0,uptime_used_mins=1 if used else 0))
 ret=execute_import('subscribers',rows,import_consumption=True);expect(ret[0],'Subscriber actual import failed: '+str(ret[1]))
 setpkg(p)
 for data,remaining in zip(rows,(1024,1023)):
  s=query_one('SELECT * FROM wisp_subscribers WHERE username=%s',(data['username'],))
  expect(s['snap_volume_quota_mb']==1024,'Imported subscriber cycle changed with package')
  code,out=radius(s['username']);expect(code==2 and quota_reply(out)==remaining*1048576,'Imported subscriber RADIUS quota incorrect')
def suspended_fresh():
 for state in ('suspended','disabled'):
  p=package();v=generate(p)[0]
  execute_write('UPDATE wisp_vouchers SET status=%s WHERE id=%s',(state,v['id']))
  expect(row('wisp_vouchers',v['id'])['policy_frozen_at'] is None,'Suspending unused card froze policy')
  setpkg(p);execute_write("UPDATE wisp_vouchers SET status='unused' WHERE id=%s",(v['id'],))
  code,out=radius(v['username'],v['password']);expect(code==2 and quota_reply(out)==1536*1048576,'Reactivated unused card retained stale quota')
  expect(row('wisp_vouchers',v['id'])['snap_price']==15,'Reactivated unused price not frozen')
  try:update_voucher_card(v['id'],v['username']+'new',v['password'])
  except ValueError:pass
  else:raise AssertionError('Pending accepted identity rename allowed')
  frozen=snapshot(row('wisp_vouchers',v['id']))
  execute_write('UPDATE wisp_vouchers SET status=%s WHERE id=%s',(state,v['id']))
  setpkg(p,quota=2048,price=20,cost=8)
  execute_write("UPDATE wisp_vouchers SET status='unused' WHERE id=%s",(v['id'],))
  expect(snapshot(row('wisp_vouchers',v['id']))==frozen,'Stopping accepted card changed contract')
def issuance_ledger_and_duration():
 p=package()
 # Only test-fixture preparation bypasses the manager creation cap; the signed
 # license and the actual RADIUS capacity/rejection tests remain intact.
 from database.license_capacity import resource_limit_trigger
 with db_session() as c:
  cur=c.cursor();cur.execute('DROP TRIGGER trg_license_limit_max_managers')
  try:
   cur.execute("INSERT INTO wisp_managers(username,password_hash,full_name,role_id,is_active,wallet_balance) VALUES(%s,'fixture-only',%s,2,1,100)",(RUN+uuid.uuid4().hex[:6],RUN));owner=cur.lastrowid
  finally:cur.execute(resource_limit_trigger('wisp_managers','max_managers',10))
 b,_=generate_voucher_batch(RUN,p,2,prefix=RUN,pin_only=False,reseller_id=owner)
 invoice=query_one('SELECT * FROM wisp_manager_invoices WHERE manager_id=%s ORDER BY id DESC LIMIT 1',(owner,))
 expect(row('wisp_managers',owner)['wallet_balance']==92,'Issuance cost not charged correctly')
 v=query_one('SELECT * FROM wisp_vouchers WHERE batch_id=%s ORDER BY id LIMIT 1',(b,));setpkg(p)
 expect(activate_voucher_card(v['username']),'Reseller card activation failed')
 expect(row('wisp_managers',owner)['wallet_balance']==92,'First use charged reseller again')
 expect(query_one('SELECT * FROM wisp_manager_invoices WHERE id=%s',(invoice['id'],))==invoice,'Historical issue invoice was repriced')
 sale=query_one('SELECT * FROM wisp_voucher_sales WHERE first_activation_voucher_id=%s',(v['id'],));expect(sale['cost']==7 and sale['price']==15 and sale['reseller_id']==owner,'Activation policy/owner incorrect')
 p=package();setpkg(p,quota=1024,validity_value=0,uptime_limit_mins=10);v=generate(p)[0]
 setpkg(p,validity_value=100,uptime_limit_mins=20)
 code,out=radius(v['username'],v['password']);expect(code==2 and struct.unpack('!I',out[27])[0]==600,'Phase one changed issued uptime/validity')

check('Migration preserves imported used contracts and financial ledgers',baseline)
check('Migration repeat-safe twice',migration_twice)
# Never retain real router records in a disposable test environment.
execute_write('DELETE FROM nas')
execute_write('DELETE FROM wisp_nas_devices')
execute_write("UPDATE wisp_license_runtime_state s JOIN wisp_license_info l ON l.id=s.license_row_id SET s.is_valid=1,s.package_hash=SHA2(l.raw_package_json,256),s.valid_until=UTC_TIMESTAMP()+INTERVAL 2 HOUR,s.max_active_sessions=1000,s.license_mode='active_sessions' WHERE s.id=1")
with patch('services.license_guard_service.get_active_license_status',return_value=dict(valid=True,status='active')), patch('services.bot_notifications_service.trigger_recharge_notification',return_value=None):
 for name,fn in (
  ('Legacy generated shape, accepted-first snapshot, lost Start, 64-bit accounting and duplicate Stop',old_unused),
  ('New generation before/after package edit',generated),
  ('Wrong password, invalid license and suspended card reject',rejection),
  ('Actual mixed fresh/used file import and protected reimport',used_imports),
  ('Unlimited-to-limited, burst, minimum rate and >4GiB quota',unlimited_and_burst),
  ('Subscriber creation, admin/portal renewals and package changes',subscriber_cycles),
  ('16 parallel snapshots and 4 backend activations',race),
  ('Unused/used printing, manual activation and batch guard',print_policy),
  ('Transaction rollback of first freeze',rollback)):
  check(name,fn)
 check('Balance/package recharge freeze, cost, burst and duplicate/pending guards',recharge_contracts)
 check('Old subscriber quota rollover and loan repayment against new capacity',rollover_and_loan)
 check('Voucher admin/portal renewals, explicit change and multiple sales',voucher_cycles)
 check('Actual fresh/used subscriber import preserves current cycles',imported_subscribers)
 check('Stop/reactivate never-used vs accepted vouchers and rename guard',suspended_fresh)
 check('Reseller historical invoice/wallet preserved and issued duration unchanged',issuance_ledger_and_duration)
with open('/tmp/first-policy-results.json','w',encoding='utf-8') as f:json.dump(dict(run=RUN,results=results,passed=sum(r['passed'] for r in results),total=len(results)),f,ensure_ascii=False,indent=2,default=str)
print(json.dumps(dict(passed=sum(r['passed'] for r in results),total=len(results),failed=[r['name'] for r in results if not r['passed']])))
sys.exit(0 if all(r['passed'] for r in results) else 1)

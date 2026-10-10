import os,sys,json,uuid,gzip,re,datetime
from pathlib import Path
from unittest.mock import patch
import pymysql
sys.path.insert(0,'/app')
import services
if Path('/tmp/import-fix-services').exists():services.__path__=['/tmp/import-fix-services']+list(services.__path__)
from database import db
from core import config
SOURCE=config.DB_NAME
TEST='test_import_'+uuid.uuid4().hex[:12]
PASSWORD=os.environ['MAX_TEST_ROOT_PASSWORD']
for m in (db,config):m.DB_USER='root';m.DB_PASSWORD=PASSWORD;m.DB_NAME=TEST
import database.schema_healer as healer
healer.REQUIRED_TABLES['wisp_imported_session_history']='CREATE TABLE IF NOT EXISTS wisp_imported_session_history LIKE radacct'
from services import database_migration_service as migration
from services.quota_service import calculate_account_quota
from services.account_lifecycle_service import sweep_expired_accounts
admin=pymysql.connect(host=config.DB_HOST,port=int(config.DB_PORT),user='root',password=PASSWORD,autocommit=True)
path='/tmp/source-import-20261010.gz'
try:
 with admin.cursor() as c:
  c.execute('CREATE DATABASE `'+TEST+'`');c.execute('SHOW TABLES FROM `'+SOURCE+'`');tables=[r[0] for r in c.fetchall()];c.execute('USE `'+TEST+'`');c.execute('SET FOREIGN_KEY_CHECKS=0')
  for t in tables:c.execute('SHOW CREATE TABLE `'+SOURCE+'`.`'+t+'`');c.execute(c.fetchone()[1])
 # No production data, license, or session is modified. License preflight is tested separately.
 with patch.object(migration,'verify_migration_license'):
  result=migration.execute_database_migration(path,options={'session_mode':'full','import_resellers':False})
 assert result and result['success'],migration.get_migration_progress()
 users={};quotas={}
 with gzip.open(path,'rt',encoding='utf8',errors='replace') as f:
  for line in f:
   line=line.strip()
   if not (line.startswith('INSERT INTO `users`') or line.startswith('INSERT INTO `user_qutas`')):continue
   idx=line.find(' VALUES (');t=line[13:idx].rstrip('`')
   for row in line[idx+9:].removesuffix(');').split('),('):
    v=migration._parse_sql_tuple(row)
    if t=='users' and len(v)>54 and v[40]!='1' and v[5] and v[5]!='_invalid':users[v[0]]={'username':v[5],'enabled':v[38],'state':v[39],'expiry':v[51]}
    elif t=='user_qutas' and len(v)>13:quotas[v[1]]={'upload':int(v[7] or 0),'download':int(v[8] or 0),'total_traffic':int(v[10] or 0),'expiry':v[13]}
 failures=[];now=datetime.datetime.now();tested=0
 for uid,r in users.items():
  q=quotas.get(uid,{});expected='active';exp=q.get('expiry') or r['expiry']
  if r['enabled']=='0' or r['state']=='0':expected='suspended'
  elif r['state']=='2' or (exp and datetime.datetime.fromisoformat(exp[:19])<now):expected='expired'
  row=db.query_one('SELECT * FROM wisp_subscribers WHERE username=%s',(r['username'],));quota=calculate_account_quota(r['username'],'subscriber')
  if row['status']!=expected or quota['accounting_error']:failures.append({'source_id':uid,'kind':'status_or_accounting'})
  acct=db.query_one('SELECT COUNT(*) n,SUM(acctinputoctets+acctoutputoctets) bytes FROM radacct WHERE username=%s',(r['username'],))
  if q and (acct['n']!=1 or int(acct['bytes'] or 0)!=q['upload']+q['download']):failures.append({'source_id':uid,'kind':'duplicate_or_wrong_usage'})
  if q and q['total_traffic']>0 and not quota['is_quota_unlimited']:
   if not (q['total_traffic']<=int(quota['total_quota_mb'])*1048576<q['total_traffic']+1048576):failures.append({'source_id':uid,'kind':'allowance'})
  tested+=1
 with patch('services.coa_queue_service.enqueue_disconnect',return_value=(True,'test')):sweep_expired_accounts()
 actual=db.query_one('SELECT COUNT(*) n,SUM(acctinputoctets) up,SUM(acctoutputoctets) down,SUM(acctsessiontime) duration FROM wisp_imported_session_history')
 authoritative={r['username'] for r in db.query_all("SELECT username FROM radacct WHERE acctterminatecause='Consolidated-Historical-Import'")}
 expected={}
 with gzip.open(path,'rt',encoding='utf8',errors='replace') as f:
  for line in f:
   line=line.strip();idx=line.find(' VALUES (')
   if not line.startswith('INSERT INTO `radacct') or idx<0:continue
   for chunk in line[idx+9:].removesuffix(');').split('),('):
    v=migration._parse_sql_tuple(chunk)
    if len(v)>=30 and v[3] in authoritative:
     unique=v[2] or f"{v[1]}_{v[0]}"
     expected.setdefault(unique,(int(v[22] or 0),int(v[23] or 0),int(v[18] or 0)))
 assert actual['n']==len(expected),(actual['n'],len(expected))
 assert [int(actual[k]) for k in ('up','down','duration')]==[sum(v[i] for v in expected.values()) for i in range(3)]
 from services.subscriber_service import get_subscriber_sessions
 details=get_subscriber_sessions('33000123',1000)
 assert len(details)>1 and all(r['imported_history'] and not r['is_active'] for r in details)
 print(json.dumps({'historical_unique_sessions':actual['n'],'history_bytes_and_duration_match_source':True,'sample_detail_rows':len(details),'historical_sessions_online':False}))
 states=db.query_all('SELECT status,COUNT(*) n FROM wisp_subscribers GROUP BY status')
 print(json.dumps({'temporary_database':TEST,'subscribers_checked':tested,'failures':failures,'states_after_expiry_sweep':states,'import_stats':result['stats']},default=str,ensure_ascii=True))
 assert not failures
 assert {r['status']:r['n'] for r in states}=={'active':32,'expired':23,'suspended':1}
finally:
 assert TEST.startswith('test_import_')
 with admin.cursor() as c:c.execute('DROP DATABASE IF EXISTS `'+TEST+'`')
 admin.close()

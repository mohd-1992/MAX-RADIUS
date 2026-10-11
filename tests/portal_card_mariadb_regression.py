"""Explicit isolated MariaDB + Flask-route + signed UDP regression.

Not part of discovery. Requires MAX_PORTAL_REGRESSION=1 and radius_max_final6 clone.
Does not import web.app, start workers, use real NAS, or touch production accounts.
"""
import ast
import datetime
import hashlib
import json
import os
import re
import secrets
import socket
import struct
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, '/app')
from core.config import DB_NAME
assert os.environ.get('MAX_PORTAL_REGRESSION') == '1'
assert DB_NAME == 'radius_max_final6', 'Isolated database only'
from database.db import query_one, query_all, execute_write, db_session, adapt_query
from services import user_portal_service as P
from flask import Flask, request, session, jsonify, redirect, url_for, flash

R = Path('/isolated-storage/reports')
run_id = os.environ.get('MAX_PORTAL_REGRESSION_RUN', 'portal-fix')
assert re.fullmatch(r'[a-z0-9-]{1,64}', run_id), 'Invalid isolated run identity'
marker = R / (run_id + '-started.json')
assert not marker.exists(), 'Do not replay a possibly executed financial regression'
marker.write_text(json.dumps({'at': time.time()}))
events = []
prefix = 'PF-' + secrets.token_hex(4)


def check(name, passed, **detail):
    events.append(dict(name=name, passed=bool(passed), **detail))
    (R / (run_id + '-results.json')).write_text(json.dumps(
        {'complete': False, 'checks': events}, default=str, ensure_ascii=True, indent=2))


def clone(table, source_id, updates):
    columns = [r['Field'] for r in query_all('SHOW COLUMNS FROM ' + table)
               if r['Field'] != 'id' and 'GENERATED' not in r['Extra']]
    names = ','.join('`' + c + '`' for c in columns)
    select = ','.join('%s' if c in updates else '`' + c + '`' for c in columns)
    values = tuple(updates[c] for c in columns if c in updates)
    with db_session() as conn:
        with conn.cursor() as cur:
            cur.execute('INSERT INTO ' + table + '(' + names + ') SELECT ' + select + ' FROM ' + table + ' WHERE id=%s', values + (source_id,))
            new_id = cur.lastrowid
    return new_id


source_pkg = query_one('SELECT id FROM wisp_packages LIMIT 1')['id']
packages = []
for n in (1, 2):
    packages.append(clone('wisp_packages', source_pkg, dict(name=prefix + '-pkg' + str(n),
        price=20 if n == 1 else 30, is_active=1, volume_quota_mb=100 if n == 1 else 200,
        uptime_limit_mins=30, validity_value=2, validity_unit='days', validity_days=2,
        rate_download='2M', rate_upload='1M', rate_limit_str='1M/2M', simultaneous_sessions=2,
        is_rollover_enabled=1, is_loyalty_enabled=0, service_type='pppoe')))
source_v = query_one("SELECT id FROM wisp_vouchers WHERE status='unused' LIMIT 1")['id']
source_s = query_one('SELECT id FROM wisp_subscribers LIMIT 1')['id']
fixtures = []


def account(kind='voucher', status='active', balance=100):
    table = 'wisp_vouchers' if kind == 'voucher' else 'wisp_subscribers'
    u = prefix + '-' + secrets.token_hex(4)
    values = dict(username=u, password='old-test-pass', status=status,
                  balance=balance, package_id=packages[0], extra_quota_mb=0,
                  expires_at=None, last_renewed_at=None, snap_volume_quota_mb=55,
                  created_at=datetime.datetime.now())
    if kind == 'voucher':
        values.update(pin_code=u, serial_number=u, first_used_at=None,
                      snap_uptime_limit_mins=12, bound_mac=None)
    else:
        values.update(loan_balance_mb=0, loan_status=0, full_name='Portal regression', service_type='pppoe')
    i = clone(table, source_v if kind == 'voucher' else source_s, values)
    fixtures.append((kind, table, i, u))
    return table, i, u


def state(table, i):
    return query_one('SELECT * FROM ' + table + ' WHERE id=?', (i,))


# Run actual decorated billing paths, using clone Docker health and real SQL locks.
for kind, status in [('voucher', 'unused'), ('voucher', 'active'), ('voucher', 'expired'), ('subscriber', 'active')]:
    table, i, u = account(kind, status)
    result = P.renew_or_change_package(u, packages[0])
    row = state(table, i)
    check(kind + '_' + status + '_renew', result[0] and float(row['balance']) == 80 and row['status'] == 'active')
    if kind == 'voucher':
        check('renew_preserves_quota_snapshot', row['snap_volume_quota_mb'] == 55 and row['snap_uptime_limit_mins'] == 12)
        check('renew_sale_once', query_one('SELECT COUNT(*) n FROM wisp_voucher_sales WHERE voucher_id=?', (i,))['n'] == 1)
    time.sleep(1.2)
    result = P.renew_or_change_package(u, packages[1])
    row = state(table, i)
    check(kind + '_' + status + '_change', result[0] and float(row['balance']) == 50 and row['package_id'] == packages[1])
    if kind == 'voucher':
        check('change_refreshes_snapshot', row['snap_volume_quota_mb'] == 200 and row['snap_uptime_limit_mins'] == 30)

for status, cash, pkg in [('suspended',100,packages[0]), ('disabled',100,packages[0]),
                          ('recharged',100,packages[0]), ('active',19,packages[0]), ('active',100,-1)]:
    table, i, u = account(status=status, balance=cash)
    before = state(table, i)
    result = P.renew_or_change_package(u, pkg)
    check('reject_' + status + '_' + str(cash), not result[0] and state(table,i) == before and
          query_one('SELECT COUNT(*) n FROM wisp_voucher_sales WHERE voucher_id=?', (i,))['n'] == 0)

table, i, u = account()
before = state(table,i)
try:
    with patch.object(P, '_sync_recharge_radius', side_effect=RuntimeError('injected SQL boundary')):
        P.renew_or_change_package(u,packages[1])
except RuntimeError:
    pass
check('radius_failure_full_sql_rollback', state(table,i)==before and
      query_one('SELECT COUNT(*) n FROM wisp_voucher_sales WHERE voucher_id=?',(i,))['n']==0)

table, i, u = account(balance=20)
barrier = threading.Barrier(2)
def buy():
    barrier.wait()
    return P.renew_or_change_package(u,packages[0])[0]
with ThreadPoolExecutor(max_workers=2) as pool:
    results = list(pool.map(lambda _: buy(), range(2)))
check('concurrent_purchase_one_debit_one_sale', sum(results)==1 and float(state(table,i)['balance'])==0 and
      query_one('SELECT COUNT(*) n FROM wisp_voucher_sales WHERE voucher_id=?',(i,))['n']==1)

for kind in ('voucher','subscriber'):
    for status in ('active','unused','suspended','expired') if kind=='voucher' else ('active','suspended'):
        table,i,u=account(kind,status)
        before=state(table,i)
        result=P.change_portal_password(u,'wrong','new-test-pass','new-test-pass')
        check(kind+'_'+status+'_wrong_password_reject',not result[0] and state(table,i)==before)
        result=P.change_portal_password(u,'old-test-pass','new-test-pass','new-test-pass')
        check(kind+'_'+status+'_password_success',result[0] and state(table,i)['password']=='new-test-pass' and state(table,i)['status']==status)
        if status in ('active','unused'):
            check('radius_password_matches',query_one("SELECT value FROM radcheck WHERE username=? AND attribute='Cleartext-Password'",(u,))['value']=='new-test-pass')
        execute_write("DELETE FROM radcheck WHERE username=?",(u,))
        if kind=='voucher':
            execute_write("UPDATE wisp_system_settings SET value='0' WHERE `key`='portal_login_username_only'")
            check('old_pin_no_password_bypass',P.authenticate_portal_user(u,u)[0] is None and P.authenticate_portal_user(u,'new-test-pass')[0] is not None)

# Same route function bodies and Flask request/session machinery, without web workers.
app=Flask(__name__);app.secret_key=secrets.token_hex(16)
ns=dict(P.__dict__,request=request,session=session,jsonify=jsonify,redirect=redirect,url_for=url_for,flash=flash)
tree=ast.parse(Path('/app/web/routes/portal.py').read_text())
names={'user_renew_package_action','user_change_package_action','user_change_password_action','api_user_change_speed'}
nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
for node in nodes:node.decorator_list=[]
exec(compile(ast.Module(body=nodes,type_ignores=[]),'portal-route-bodies','exec'),ns)
for path, name in [('/renew','user_renew_package_action'),('/change','user_change_package_action'),('/password','user_change_password_action'),('/speed','api_user_change_speed')]:
    app.add_url_rule(path,endpoint=name,view_func=ns[name],methods=['POST'])
for name in ('user_login','user_dashboard','user_packages'):
    app.add_url_rule('/'+name,endpoint=name,view_func=lambda:'test')
table,i,u=account()
client=app.test_client()
check('anonymous_speed_not_authorized',client.post('/speed',json={'username':u,'speed':'2M'}).status_code==401)
with client.session_transaction() as sess:sess['portal_user']=u
execute_write("INSERT INTO wisp_system_settings (`key`,value) VALUES ('portal_allow_package_change','0') ON DUPLICATE KEY UPDATE value='0'")
before=state(table,i);client.post('/renew',data={'package_id':packages[0]})
check('http_disabled_renew_preserves',state(table,i)==before)
execute_write("UPDATE wisp_system_settings SET value='1' WHERE `key`='portal_allow_package_change'")
client.post('/renew',data={'package_id':packages[0]})
check('http_voucher_renew',float(state(table,i)['balance'])==80)
time.sleep(1.2)
client.post('/change',data={'package_id':packages[1]})
check('http_voucher_change',float(state(table,i)['balance'])==50 and state(table,i)['package_id']==packages[1])
execute_write("INSERT INTO wisp_system_settings (`key`,value) VALUES ('portal_allow_password_change','0') ON DUPLICATE KEY UPDATE value='0'")
client.post('/password',data={'old_password':'old-test-pass','new_password':'new-test-pass','confirm_password':'new-test-pass'})
check('http_disabled_password_preserves',state(table,i)['password']=='old-test-pass')
execute_write("UPDATE wisp_system_settings SET value='1' WHERE `key`='portal_allow_password_change'")
client.post('/password',data={'old_password':'old-test-pass','new_password':'new-test-pass','confirm_password':'new-test-pass'})
check('http_voucher_password',state(table,i)['password']=='new-test-pass')
offline=client.post('/speed',json={'speed':'open'}).get_json()
check('http_offline_speed_next_login',offline['success'] and offline['coa']['live_status']=='next_login' and
      query_one("SELECT value FROM radreply WHERE username=? AND attribute='MikroTik-Rate-Limit'",(u,))['value']=='0/0')

# Real signed UDP exchange on loopback; these rows are fixtures, not CHR sessions.
execute_write("INSERT INTO nas(nasname,shortname,type,secret) VALUES('127.0.0.1',?,'other','regression-secret')",(prefix,))
for n in (1,2):
    execute_write("""INSERT INTO radacct(username,acctsessionid,acctuniqueid,nasipaddress,acctstarttime,
        acctinputoctets,acctoutputoctets,acctsessiontime,framedipaddress,callingstationid)
        VALUES(?,?,?,'127.0.0.1',NOW(),0,0,0,'127.0.0.2','AA:BB:CC:DD:EE:FF')""",(u,prefix+str(n),secrets.token_hex(16)))
def exchange(codes):
    received=[]
    sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);sock.bind(('127.0.0.1',3799));sock.settimeout(8)
    def serve():
        try:
            for code in codes:
                packet,peer=sock.recvfrom(4096);received.append(packet)
                head=struct.pack('!BBH',code,packet[1],20)
                sock.sendto(head+hashlib.md5(head+packet[4:20]+b'regression-secret').digest(),peer)
        finally:sock.close()
    worker=threading.Thread(target=serve);worker.start()
    response=client.post('/speed',json={'speed':'2M'}).get_json();worker.join(10)
    return response,received
for codes, expected in [([44,44],'acknowledged'),([44,45],'partial'),([45,45],'next_login')]:
    response, packets=exchange(codes)
    check('http_signed_udp_'+expected,response['coa']['live_status']==expected and len(packets)==2 and packets[0]!=packets[1])
execute_write("UPDATE radacct SET acctstoptime=NOW(),acctterminatecause='Fixture-End' WHERE username=? AND acctstoptime IS NULL",(u,))
execute_write("UPDATE wisp_packages SET service_type='hotspot' WHERE id=?", (packages[0],))
execute_write("UPDATE radacct SET acctstoptime=NULL WHERE username=?", (u,))
before_sessions = query_all('SELECT * FROM radacct WHERE username=? ORDER BY radacctid', (u,))
with patch('core.coa.RadiusCoaClient', side_effect=AssertionError('Hotspot must not send CoA')):
    response = client.post('/speed', json={'speed': '2M'}).get_json()
check('hotspot_policy_deferred_without_stop', response['success'] and response['coa']['live_status']=='next_login'
      and response['coa']['sessions_total']==2 and response['coa']['sessions_acknowledged']==0
      and query_all('SELECT * FROM radacct WHERE username=? ORDER BY radacctid',(u,))==before_sessions)
execute_write("UPDATE radacct SET acctstoptime=NOW(),acctterminatecause='Fixture-End' WHERE username=? AND acctstoptime IS NULL",(u,))
for kind,table,i,u in fixtures:
    execute_write('UPDATE '+table+" SET status='suspended' WHERE id=?",(i,))
    execute_write("DELETE FROM radcheck WHERE username=?",(u,))
check('fixture_cleanup_no_open_sessions',query_one('SELECT COUNT(*) n FROM radacct WHERE username LIKE ? AND acctstoptime IS NULL',(prefix+'%',))['n']==0)
result={'complete':True,'checks':events,'passed':sum(x['passed'] for x in events),'failed':sum(not x['passed'] for x in events),
        'limits':['Isolated MariaDB; Flask route bodies via test client, no global CSRF middleware','Signed UDP loopback, not real CHR','Existing settlement wrapper used for offline billing; no production deployment']}
(R/(run_id+'-results.json')).write_text(json.dumps(result,ensure_ascii=True,default=str,indent=2))
print(json.dumps({k:v for k,v in result.items() if k!='checks'}))
sys.exit(bool(result['failed']))

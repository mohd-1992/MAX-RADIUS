"""Run ONLY against a new disposable local MariaDB, never a configured tenant.

The launcher supplies a loopback port and schema name. No RADIUS/NAS traffic.
"""
import copy
import datetime
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import patch

assert os.environ.get('USERMAN_ROOTFIX_LAB') == '1'
assert os.environ.get('DB_HOST') == '127.0.0.1'
assert os.environ.get('DB_NAME', '').startswith('userman_rootfix_lab_')
assert int(os.environ.get('DB_PORT', '3306')) != 3306
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database.db import get_connection
from database.userman_import_schema import install_userman_import_schema
from services.mikrotik_userman_importer import execute_userman_import
from services.userman_import_journal import rollback_import, query, encode

results = []


def read(sql, args=()):
    c=get_connection()
    try:return query(c,sql,args)
    finally:c.close()


def write(sql,args=()):
    c=get_connection()
    try:
        with c.cursor() as q:q.execute(sql,args)
        c.commit()
    finally:c.close()


def run(name, fn):
    fn();results.append(name)


def payload(count=1, history=False):
    suffix=uuid.uuid4().hex[:8]
    users=[]
    for n in range(count):
        user=dict(username='fixture_'+suffix+'_'+str(n),password='fixture-pass',actual_profile='fixture_'+suffix)
        if history:
            start=datetime.datetime.now().replace(microsecond=0)-datetime.timedelta(hours=1)
            user.update(first_used_at=str(start),expires_at=str(start+datetime.timedelta(days=30)),upload_used_bytes=100,download_used_bytes=200,uptime_used_seconds=90,
                source_sessions=[dict(session_id='s'+str(n),nas='192.0.2.1',mac='AA:00:00:00:00:01',ip='192.0.2.5',start=str(start),stop=str(start+datetime.timedelta(seconds=90)),upload=100,download=200,uptime=90)])
        users.append(user)
    return dict(profiles=[dict(name='fixture_'+suffix,quota_mb=100,rate_down='2M',rate_up='1M',validity_value=30)],users=users)


def apply(p, job=None, **kw):
    r=execute_userman_import(p,operation_id=job or uuid.uuid4().hex,import_consumption=True,**kw)
    assert r['success'],r.get('error')
    return r


def cancelled_before_commit():
    p=payload(3);job=uuid.uuid4().hex;calls=[]
    def cancel():
        calls.append(1)
        if len(calls)==3:raise RuntimeError('fixture cancel before commit')
    r=execute_userman_import(p,operation_id=job,check_cancel=cancel)
    assert not r['success']
    assert not read('SELECT * FROM wisp_userman_import_batches WHERE job_id=%s',(job,))
    assert not read('SELECT id FROM wisp_packages WHERE name=%s',(p['profiles'][0]['name'],))


def full_cancel_and_repeat():
    p=payload(3,True);job=uuid.uuid4().hex;apply(p,job)
    r=rollback_import(job);assert r['rollback_complete'],r
    assert not read('SELECT id FROM wisp_vouchers WHERE username=%s',(p['users'][0]['username'],))
    assert not read('SELECT id FROM wisp_packages WHERE name=%s',(p['profiles'][0]['name'],))
    assert rollback_import(job)['rollback_complete']
    r=execute_userman_import(p,operation_id=job,import_consumption=True)
    assert not r['success'] # receipt prevents replay after cancellation


def reject_aggregate_only_and_invented_dates():
    p=payload(1,True);u=p['users'][0];u.pop('source_sessions');u.pop('first_used_at')
    r=apply(p);assert r['stats']['review_required_users']==1
    assert not read('SELECT id FROM wisp_vouchers WHERE username=%s',(u['username'],))
    assert not read('SELECT radacctid FROM radacct WHERE username=%s',(u['username'],))


def real_usage_blocks_rollback():
    p=payload(2);job=uuid.uuid4().hex;apply(p,job);u=p['users'][0]['username']
    write("INSERT INTO radpostauth(username,pass,reply,authdate) VALUES(%s,'fixture','Access-Accept',NOW())",(u,))
    before=encode(read('SELECT * FROM radpostauth WHERE username=%s',(u,)))
    r=rollback_import(job);assert not r['rollback_complete']
    assert read('SELECT id FROM wisp_vouchers WHERE username=%s',(u,))
    assert not read('SELECT id FROM wisp_vouchers WHERE username=%s',(p['users'][1]['username'],))
    assert encode(read('SELECT * FROM radpostauth WHERE username=%s',(u,)))==before
    assert not rollback_import(job)['rollback_complete']


def changed_contract_blocks_rollback():
    p=payload();job=uuid.uuid4().hex;apply(p,job);u=p['users'][0]['username']
    write('UPDATE wisp_vouchers SET extra_quota_mb=7 WHERE username=%s',(u,))
    assert not rollback_import(job)['rollback_complete']
    assert read('SELECT extra_quota_mb FROM wisp_vouchers WHERE username=%s',(u,))[0]['extra_quota_mb']==7
    # A later chunk cannot adopt that outside change into its undo image.
    p['users'][0]['password']='later-fixture-password'
    r=execute_userman_import(p,operation_id=job,chunk_index=1,total_chunks=2,duplicate_action='overwrite')
    assert not r['success']
    assert read('SELECT password FROM wisp_vouchers WHERE username=%s',(u,))[0]['password']=='fixture-pass'
    assert not rollback_import(job)['rollback_complete']


def overwrite_rollback_restores_credentials():
    p=payload();first=apply(p);u=p['users'][0]['username'];p['users'][0]['password']='changed-fixture'
    job=uuid.uuid4().hex;apply(p,job,duplicate_action='overwrite')
    assert read('SELECT password FROM wisp_vouchers WHERE username=%s',(u,))[0]['password']=='changed-fixture'
    assert rollback_import(job)['rollback_complete']
    assert read('SELECT password FROM wisp_vouchers WHERE username=%s',(u,))[0]['password']=='fixture-pass'


def chunks_450_cancel():
    p=payload(450);job=uuid.uuid4().hex;cache=None
    for i in range(3):
        r=apply(dict(p,users=p['users'][i*200:(i+1)*200]),job,chunk_index=i,total_chunks=3,state_cache=copy.deepcopy(cache));cache=r['state_cache']
    assert r['stats']['created_users']==450
    r=rollback_import(job);assert r['rollback_complete'],r
    assert not read('SELECT id FROM wisp_packages WHERE name=%s',(p['profiles'][0]['name'],))


def atomic_failure_between_writes():
    p=payload();job=uuid.uuid4().hex
    from services import userman_import_journal as j
    with patch.object(j,'record',side_effect=RuntimeError('fixture journal write failure')):
        r=execute_userman_import(p,operation_id=job)
    assert not r['success']
    assert not read('SELECT id FROM wisp_vouchers WHERE username=%s',(p['users'][0]['username'],))
    assert not read('SELECT * FROM wisp_userman_import_batches WHERE job_id=%s',(job,))


def concurrent_same_receipt():
    p=payload();job=uuid.uuid4().hex;barrier=threading.Barrier(2);out=[]
    def one():
        barrier.wait();out.append(execute_userman_import(p,operation_id=job))
    ts=[threading.Thread(target=one) for _ in range(2)]
    for t in ts:t.start()
    for t in ts:t.join(20);assert not t.is_alive()
    assert sum(bool(r['success']) for r in out)==1
    assert len(read('SELECT * FROM wisp_vouchers WHERE username=%s',(p['users'][0]['username'],)))==1


def new_accounting_sale_and_reservation_each_block():
    for event in ('accounting','sale','reservation'):
        p=payload();job=uuid.uuid4().hex;apply(p,job);u=p['users'][0]['username']
        v=read('SELECT * FROM wisp_vouchers WHERE username=%s',(u,))[0]
        if event=='accounting':
            c=get_connection()
            with c.cursor() as q:
                q.execute('SET @max_radius_import=1')
                q.execute("INSERT INTO radacct(acctsessionid,acctuniqueid,username,nasipaddress,acctstarttime,acctupdatetime,acctstoptime,acctinputoctets,acctoutputoctets,acctsessiontime) VALUES('actual-fixture',%s,%s,'192.0.2.1',NOW(),NOW(),NOW(),12,20,60)",(uuid.uuid4().hex,u))
            c.commit();c.close()
        elif event=='sale':
            write("INSERT INTO wisp_voucher_sales(voucher_id,batch_id,username,package_name,price) VALUES(%s,%s,%s,'fixture',5)",(v['id'],v['batch_id'],u))
        else:
            write("INSERT INTO wisp_session_reservations(session_key,username,nasipaddress,callingstationid,expires_at) VALUES(%s,%s,'192.0.2.1','AA',NOW()+INTERVAL 1 MINUTE)",(uuid.uuid4().hex,u))
        assert not rollback_import(job)['rollback_complete'],event
        assert read('SELECT * FROM wisp_vouchers WHERE username=%s',(u,))==[v]


def commit_response_loss_is_not_replayed():
    for after in (False,True):
        p=payload();job=uuid.uuid4().hex
        class Connection:
            def __init__(self):self.c=get_connection();self.commits=0
            def __getattr__(self,key):return getattr(self.c,key)
            def commit(self):
                self.commits+=1
                if self.commits==2:
                    if after:self.c.commit()
                    raise RuntimeError('fixture commit response lost')
                self.c.commit()
        # is_mysql_conn needs to recognize this transparent fixture wrapper.
        from services import mikrotik_userman_importer as importer
        with patch.object(importer,'get_connection',side_effect=Connection),patch.object(importer,'is_mysql_conn',return_value=True):
            r=execute_userman_import(p,operation_id=job)
        assert not r['success']
        exists=bool(read('SELECT id FROM wisp_vouchers WHERE username=%s',(p['users'][0]['username'],)))
        assert exists==after
        assert rollback_import(job)['rollback_complete']


def rollback_response_loss_resumes_from_receipt():
    p=payload(2);job=uuid.uuid4().hex;apply(p,job)
    class Connection:
        def __init__(self):self.c=get_connection();self.once=False
        def __getattr__(self,key):return getattr(self.c,key)
        def commit(self):
            self.c.commit()
            if not self.once:self.once=True;raise RuntimeError('fixture rollback response lost')
    try:rollback_import(job,Connection)
    except RuntimeError:pass
    else:raise AssertionError('missing injected interruption')
    assert rollback_import(job)['rollback_complete']


def exact_shared_session_no_double_count():
    p=payload(1,True);u=p['users'][0];s=u['source_sessions'][0];c=get_connection()
    with c.cursor() as q:
        q.execute('SET @max_radius_import=1')
        q.execute('''INSERT INTO radacct(acctsessionid,acctuniqueid,username,nasipaddress,callingstationid,framedipaddress,
                    acctstarttime,acctupdatetime,acctstoptime,acctinputoctets,acctoutputoctets,acctsessiontime)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,150,250,120)''',
                  (s['session_id'],uuid.uuid4().hex,u['username'],s['nas'],s['mac'],s['ip'],s['start'],s['stop'],s['stop']))
    c.commit();c.close()
    actual=read('SELECT * FROM radacct WHERE username=%s',(u['username'],))
    job=uuid.uuid4().hex;apply(p,job)
    rows=read('SELECT * FROM radacct WHERE username=%s ORDER BY radacctid',(u['username'],))
    assert rows[0]==actual[0]
    assert sum(z['acctinputoctets']+z['acctoutputoctets'] for z in rows)==400
    assert sum(z['acctsessiontime'] for z in rows)==120
    # Exact rollback preserves the original actual NAS fixture row.
    assert rollback_import(job)['rollback_complete']
    assert read('SELECT * FROM radacct WHERE username=%s',(u['username'],))==actual


def forty_cycle_cases_keep_original_dates_and_enforcement():
    p=payload(40,True);c=get_connection()
    with c.cursor() as q:
        q.execute('SET @max_radius_import=1')
        for i,u in enumerate(p['users']):
            if i>=31:
                u['download_used_bytes']=100*1048576
                u['source_sessions'][0]['download']=u['download_used_bytes']
            s=u['source_sessions'][0]
            q.execute('''INSERT INTO radacct(acctsessionid,acctuniqueid,username,nasipaddress,callingstationid,framedipaddress,
                acctstarttime,acctupdatetime,acctstoptime,acctinputoctets,acctoutputoctets,acctsessiontime)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (s['session_id'],uuid.uuid4().hex,u['username'],s['nas'],s['mac'],s['ip'],s['start'],s['stop'],s['stop'],s['upload'],s['download'],s['uptime']))
    c.commit();c.close();job=uuid.uuid4().hex;r=apply(p,job)
    assert r['stats']['created_users']==40
    rows=read('SELECT status,COUNT(*) n FROM wisp_vouchers WHERE username LIKE %s GROUP BY status',(p['users'][0]['username'].rsplit('_',1)[0]+'_%',))
    assert {z['status']:z['n'] for z in rows}=={'active':31,'expired':9}
    for u in p['users']:
        v=read('SELECT first_used_at,expires_at FROM wisp_vouchers WHERE username=%s',(u['username'],))[0]
        assert str(v['first_used_at'])==u['first_used_at'] and str(v['expires_at'])==u['expires_at']
    assert not read("SELECT radacctid FROM radacct WHERE username LIKE %s AND acctterminatecause='Consolidated-Historical-Import'",(p['users'][0]['username'].rsplit('_',1)[0]+'_%',))
    assert rollback_import(job)['rollback_complete']


def profile_first_used_only_in_later_chunk():
    p=payload(1);second=payload(1);p['profiles']+=second['profiles'];job=uuid.uuid4().hex
    r=apply(p,job,chunk_index=0,total_chunks=2)
    r=apply(dict(p,users=second['users']),job,chunk_index=1,total_chunks=2,state_cache=r['state_cache'])
    assert r['stats']['created_users']==2
    assert rollback_import(job)['rollback_complete']


def bounded_metadata_wait_and_repeated_install():
    c=get_connection();before=read('SELECT COUNT(*) n FROM wisp_vouchers')[0]['n']
    query(c,'ALTER TABLE wisp_session_reservations DROP INDEX ix_userman_reservation_user');c.commit()
    blocker=get_connection();query(blocker,'LOCK TABLES wisp_session_reservations READ')
    previous=query(c,'SELECT @@SESSION.lock_wait_timeout t')[0]['t'];started=time.monotonic()
    try:
        try:install_userman_import_schema(c)
        except Exception as exc:assert exc.args[0]==1205
        else:raise AssertionError('Expected bounded metadata wait')
        assert time.monotonic()-started<4.5
        assert query(c,'SELECT @@SESSION.lock_wait_timeout t')[0]['t']==previous
    finally:
        query(blocker,'UNLOCK TABLES');blocker.close()
    install_userman_import_schema(c);install_userman_import_schema(c);c.close()
    assert read('SELECT COUNT(*) n FROM wisp_vouchers')[0]['n']==before


if __name__=='__main__':
    c=get_connection();install_userman_import_schema(c);install_userman_import_schema(c);c.close()
    # No licensing requests or bot notifications in this local DB suite.
    license_module=type(sys)('services.license_guard_service');license_module.get_active_license_status=lambda **kw:None
    with patch.dict(sys.modules,{'services.license_guard_service':license_module}),patch('services.mikrotik_userman_importer.log_audit',return_value=None):
        for fn in (cancelled_before_commit,full_cancel_and_repeat,reject_aggregate_only_and_invented_dates,
                   real_usage_blocks_rollback,changed_contract_blocks_rollback,overwrite_rollback_restores_credentials,
                   chunks_450_cancel,atomic_failure_between_writes,concurrent_same_receipt,
                   new_accounting_sale_and_reservation_each_block,commit_response_loss_is_not_replayed,
                   rollback_response_loss_resumes_from_receipt,exact_shared_session_no_double_count,
                   forty_cycle_cases_keep_original_dates_and_enforcement,profile_first_used_only_in_later_chunk,
                   bounded_metadata_wait_and_repeated_install):run(fn.__name__,fn)
    print(json.dumps({'passed':len(results),'cases':results,'customer_access':False,'radius_or_nas_tests':False}))

"""Disposable MariaDB ownership regression. Never targets a customer schema."""
import ast
import datetime
import json
import math
import os
import re
import secrets
import string
import sys
import types
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
import pymysql
from flask import Flask, g, has_request_context, request, session

database = os.environ.get('OWNERSHIP_REGRESSION_DATABASE', '')
if not re.fullmatch(r'max_ownership_regression_[0-9a-f]{12}', database):
    raise SystemExit('An explicitly isolated ownership schema is required.')
ROOT = Path('/app')


def connect():
    return pymysql.connect(host=os.environ.get('DB_HOST','wisp-db'),
        user=os.environ.get('DB_USER','radius'), password=os.environ['DB_PASSWORD'],
        database=database, autocommit=False, cursorclass=pymysql.cursors.DictCursor)


@contextmanager
def transaction():
    conn=connect()
    try:
        yield conn;conn.commit()
    except Exception:
        conn.rollback();raise
    finally:conn.close()


def read(sql, params=(), one=False):
    with transaction() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql.replace('?','%s'),params)
            return cursor.fetchone() if one else cursor.fetchall()


def write(sql,params=()):
    with transaction() as conn:
        with conn.cursor() as cursor:cursor.execute(sql,params)


def load(path,ns,names=None):
    tree=ast.parse((ROOT/path).read_text(encoding='utf8'))
    nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef,ast.Assign))
           and (names is None or getattr(n,'name',None) in names)]
    exec(compile(ast.Module(body=nodes,type_ignores=[]),path,'exec'),ns)


app=Flask('ownership-isolated');app.secret_key='isolated-fixture-only'
own=dict(json=json,re=re,g=g,has_request_context=has_request_context,request=request,
    raw_query_one=lambda s,p=():read(s,p,True),raw_query_all=read,
    get_current_manager=lambda: read('SELECT m.*,r.code AS role_code FROM wisp_managers m JOIN wisp_roles r ON m.role_id=r.id WHERE m.id=?',(session['admin_id'],),True))
load('core/ownership.py',own)
ns=dict(db_session=transaction,adapt_query=lambda q,c:q.replace('?','%s'),is_mysql_conn=lambda c:True,
    query_one=own['scoped_query_one'],query_all=own['scoped_query_all'],
    json=json,secrets=secrets,string=string,datetime=datetime,math=math,
    get_db_storage_now=lambda:datetime.datetime.now(),build_mikrotik_rate_limit=lambda **kw:'4M/2M')
load('services/voucher_service.py',ns,['generate_random_code','_VoucherWriter','generate_voucher_batch','get_batches','get_vouchers','get_voucher_summary_counts'])
license_mod=types.ModuleType('services.license_guard_service');license_mod.get_active_license_status=lambda:dict(valid=True,status='active')
owner_mod=types.ModuleType('core.ownership');owner_mod.generation_owner=own['generation_owner']
checks=[]
quick_mod=types.ModuleType('services.quick_card_service')
quick_mod.get_issue_policy=lambda tx=None:dict(prefix='',char_type='numbers',total_length=8,format_type='user_pass',password_length=8)
sys.modules['services.quick_card_service']=quick_mod


@contextmanager
def actor(ident):
    with app.test_request_context('/vouchers'):
        session['admin_id']=ident
        yield


def generate(owner=None,count=1,ident=2):
    with actor(ident):return ns['generate_voucher_batch']('isolated',1,count,reseller_id=owner)


def balance(ident):return float(read('SELECT wallet_balance FROM wisp_managers WHERE id=?',(ident,),True)['wallet_balance'])


with patch.dict(sys.modules,{'services.license_guard_service':license_mod,'core.ownership':owner_mod}):
    write("INSERT INTO wisp_roles(id,name,code) VALUES(1,'admin','superadmin'),(2,'reseller','reseller')")
    write("INSERT INTO wisp_managers(id,username,password_hash,full_name,role_id,wallet_balance,is_active,is_deleted,allowed_packages) VALUES(1,'fixture-admin','hash','admin',1,100,1,0,NULL),(2,'fixture-a','hash','a',2,100,1,0,'[1]'),(3,'fixture-b','hash','b',2,100,1,0,'[1]')")
    write("INSERT INTO wisp_packages(id,name,price,cost,rate_download,rate_upload,service_type) VALUES(1,'fixture',10,8,'4M','2M','hotspot')")
    bid,_=generate(None,2)
    assert balance(2)==84 and read('SELECT reseller_id FROM wisp_voucher_batches WHERE id=?',(bid,),True)['reseller_id']==2
    checks.append('omitted_owner_charges_current_wallet')
    generate(3)
    assert balance(2)==76 and balance(3)==100
    checks.append('forged_owner_does_not_charge_victim')
    generate(3,ident=1)
    with actor(2):
        assert ns['get_voucher_summary_counts']()['total_cards']==3
        assert len(ns['get_batches']())==2 and len(ns['get_vouchers']())==3
        assert own['scoped_query_one']('SELECT COUNT(*) AS n FROM (SELECT id FROM wisp_vouchers v) page JOIN wisp_vouchers v ON page.id=v.id')['n']==3
    checks.append('mariadb_scoped_aggregates_joins_pagination')
    write('UPDATE wisp_managers SET wallet_balance=0 WHERE id=2')
    before=read('SELECT COUNT(*) AS n FROM wisp_vouchers',one=True)['n']
    try:generate()
    except ValueError:pass
    else:raise AssertionError('Insufficient wallet accepted')
    assert read('SELECT COUNT(*) AS n FROM wisp_vouchers',one=True)['n']==before
    checks.append('insufficient_wallet_rollback')
    write("UPDATE wisp_managers SET wallet_balance=100,allowed_packages='[99]' WHERE id=2")
    try:generate()
    except PermissionError:pass
    else:raise AssertionError('Disallowed package accepted')
    checks.append('package_restriction')
    write("UPDATE wisp_managers SET allowed_packages='[1]' WHERE id=2")
    write("CREATE TRIGGER fail_fixture_invoice BEFORE INSERT ON wisp_manager_invoices FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='isolated invoice failure'")
    try:generate()
    except pymysql.MySQLError:pass
    else:raise AssertionError('Invoice failure not propagated')
    finally:write('DROP TRIGGER fail_fixture_invoice')
    assert balance(2)==100 and read('SELECT COUNT(*) AS n FROM wisp_vouchers',one=True)['n']==before
    checks.append('invoice_failure_rolls_back_wallet_and_cards')
    write('UPDATE wisp_managers SET wallet_balance=8 WHERE id=2')
    def attempt(_):
        try:generate();return True
        except ValueError:return False
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,range(2)))
    assert results.count(True)==1 and balance(2)==0
    checks.append('concurrent_purchases_no_overspend')
    bid,_=generate(ident=1)
    assert read('SELECT reseller_id FROM wisp_voucher_batches WHERE id=?',(bid,),True)['reseller_id'] is None
    checks.append('admin_unassigned_inventory')
print(json.dumps(dict(passed=len(checks),checks=checks,isolated=True)))

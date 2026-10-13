"""Explicit isolated MariaDB regression; never accepts a customer database name.

Run only after creating empty schema-cloned tables in max_managers_regression_*.
This file does not import the app, core.config or database.db, or start workers.
"""
import ast
import datetime
import hashlib
import json
import os
import re
import secrets
import sys
import types
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from pathlib import Path
from functools import wraps
from flask import Flask, session, request, jsonify, redirect, url_for, flash
import pymysql


def run():
    name=os.environ.get('MANAGER_REGRESSION_DATABASE','')
    if not re.fullmatch(r'max_managers_regression_[a-z0-9_]+',name):
        raise RuntimeError('Requires explicitly isolated manager database')
    def connect():
        return pymysql.connect(host=os.environ['DB_HOST'],user=os.environ['DB_USER'],password=os.environ['DB_PASSWORD'],
                               database=name,charset='utf8mb4',cursorclass=pymysql.cursors.DictCursor,autocommit=False)
    @contextmanager
    def tx():
        conn=connect()
        try:yield conn;conn.commit()
        except Exception:conn.rollback();raise
        finally:conn.close()
    def all_rows(sql,args=()):
        with tx() as conn:
            with conn.cursor() as cur:cur.execute(sql.replace('?','%s'),args);return list(cur.fetchall())
    def one(sql,args=()):
        rows=all_rows(sql,args);return rows[0] if rows else None
    def write(sql,args=()):
        with tx() as conn:
            with conn.cursor() as cur:cur.execute(sql.replace('?','%s'),args);return cur.lastrowid
    ns=dict(json=json,re=re,hashlib=hashlib,datetime=datetime,secrets=secrets,Decimal=Decimal,InvalidOperation=InvalidOperation,
            query_all=all_rows,query_one=one,execute_write=write,execute_update=write,log_audit=lambda *a:None,
            db_session=tx,adapt_query=lambda sql,conn:sql.replace('?','%s'),hash_manager_password=lambda p:'fixture-hash')
    path=Path('/app/services/manager_service.py')
    tree=ast.parse(path.read_text())
    functions=[n for n in tree.body if isinstance(n,ast.FunctionDef)]
    exec(compile(ast.Module(body=functions,type_ignores=[]),str(path),'exec'),ns)
    lic=types.ModuleType('services.license_guard_service');lic.check_manager_quota=lambda _: (True,'',0,0)
    sys.modules['services.license_guard_service']=lic
    for table in ['wisp_manager_invoices','wisp_managers','wisp_roles','wisp_permissions','wisp_admins']:
        if one('SELECT COUNT(*) n FROM '+table)['n']:raise RuntimeError('Fixture must be empty')
    write("INSERT INTO wisp_roles(id,name,code,is_system) VALUES(1,'Fixture','superadmin',1),(2,'Fixture manager','fixture',0)")
    for ident in [1,2,3]:
        write("INSERT INTO wisp_managers(id,username,password_hash,full_name,role_id,wallet_balance,credit_limit,is_active,is_deleted) VALUES(?,?,?,?,2,100,50,1,0)",(ident,'fixture'+str(ident),'fixture-hash','Fixture'))
    def balance(ident=2):return float(one('SELECT wallet_balance FROM wisp_managers WHERE id=?',(ident,))['wallet_balance'])
    def count():return one('SELECT COUNT(*) n FROM wisp_manager_invoices')['n']
    results=[]
    def check(label, fn):fn();results.append(label)
    def replay():
        with ThreadPoolExecutor(max_workers=8) as pool:
            refs=list(pool.map(lambda _:ns['deposit_manager_wallet'](2,10,request_id='concurrent_reference_0123456789'),range(8)))
        assert balance()==110 and count()==1 and len({r[1] for r in refs})==1
    check('8 concurrent duplicate deposits apply once',replay)
    def credit():
        before=count()
        def attempt(_):
            try:ns['deposit_manager_wallet'](3,30,'credit');return True
            except ValueError:return False
        with ThreadPoolExecutor(max_workers=2) as pool:answers=list(pool.map(attempt,range(2)))
        assert sum(answers)==1 and balance(3)==130 and count()==before+1
    check('concurrent credit deposits cannot exceed ceiling',credit)
    def transfer():
        before=count();a,b=balance(2),balance(3)
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda _:ns['transfer_manager_wallet'](2,3,20,request_id='transfer_reference_0123456789'),range(6)))
        assert balance(2)==a-20 and balance(3)==b+20 and count()==before+2
    check('6 concurrent duplicate transfers apply one paired transaction',transfer)
    def opposite():
        before=balance(2)+balance(3)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(ns['transfer_manager_wallet'],2,3,5),pool.submit(ns['transfer_manager_wallet'],3,2,5)]
            for future in futures:future.result(timeout=15)
        assert balance(2)+balance(3)==before
    check('opposite transfers lock in deterministic order',opposite)
    def rollback():
        before=(balance(2),balance(3),count());original=ns['_manager_tx'];seen=[]
        def fail(conn,sql,args=(),fetch=None):
            if 'INSERT INTO wisp_manager_invoices' in sql:
                seen.append(1)
                if len(seen)==2:raise RuntimeError('isolated injected failure')
            return original(conn,sql,args,fetch)
        ns['_manager_tx']=fail
        try:
            try:ns['transfer_manager_wallet'](2,3,10)
            except RuntimeError:pass
        finally:ns['_manager_tx']=original
        assert (balance(2),balance(3),count())==before
    check('second transfer invoice failure rolls back both balances',rollback)
    def create():
        ident=ns['create_manager'](dict(username='created-fixture',password='fixture-password',full_name='Fixture',role_id=2,wallet_balance=10))
        assert balance(ident)==10
        assert one('SELECT id FROM wisp_admins WHERE username=?',('created-fixture',))
    check('manager creation uses actual MySQL legacy upsert and opening invoice',create)
    def create_rollback():
        original=ns['_manager_tx'];before=count()
        def fail(conn,sql,args=(),fetch=None):
            if 'INSERT INTO wisp_admins' in sql:raise RuntimeError('isolated injected failure')
            return original(conn,sql,args,fetch)
        ns['_manager_tx']=fail
        try:
            try:ns['create_manager'](dict(username='failed-fixture',password='fixture-password',full_name='Fixture',role_id=2,wallet_balance=10))
            except RuntimeError:pass
        finally:ns['_manager_tx']=original
        assert not one('SELECT id FROM wisp_managers WHERE username=?',('failed-fixture',)) and count()==before
    check('creation failure leaves no partial manager or invoice',create_rollback)
    def permissions_existing_session():
        # Actual RBAC lookups read MariaDB on every request, not a mocked permission set.
        ns.update(session=session,request=request,jsonify=jsonify,redirect=redirect,url_for=url_for,flash=flash,wraps=wraps)
        tree=ast.parse(Path('/app/core/rbac.py').read_text())
        functions=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('get_current_manager','get_manager_permissions','has_permission','require_permission')]
        exec(compile(ast.Module(body=functions,type_ignores=[]),'core/rbac.py','exec'),ns)
        app=Flask('manager-permission-fixture');app.secret_key='isolated-regression-session-key'
        app.add_url_rule('/roles','roles_page',lambda:'roles')
        app.add_url_rule('/login','login',lambda:'login')
        app.add_url_rule('/dashboard','dashboard',lambda:'dashboard')
        app.add_url_rule('/api/probe/view','probe_view',ns['require_permission']('managers.view')(lambda:jsonify(success=True)))
        app.add_url_rule('/api/probe/billing','probe_billing',ns['require_permission']('managers.billing')(lambda:jsonify(success=True)))
        tree=ast.parse(Path('/app/web/routes/resellers.py').read_text())
        edit=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='edit_role_action')
        edit.decorator_list=[];exec(compile(ast.Module(body=[edit],type_ignores=[]),'resellers.py','exec'),ns)
        app.add_url_rule('/roles/edit/<int:role_id>','edit_role_action',ns['require_permission']('roles.manage')(ns['edit_role_action']),methods=['POST'])
        security={};exec(Path('/app/core/license_security.py').read_text(),security)
        security['install_csrf'](app,security['protects_admin_account_request'])
        for ident,code in [(1,'managers.view'),(2,'managers.billing')]:
            write('INSERT INTO wisp_permissions(id,code,name,category) VALUES(?,?,?,?)',(ident,code,code,'Fixture'))
        restricted=app.test_client();administrator=app.test_client()
        with restricted.session_transaction() as state:state['admin_id']=2
        with administrator.session_transaction() as state:state.update(admin_id=1,_license_csrf='fixture-token')
        assert restricted.get('/api/probe/view').status_code==403
        assert administrator.post('/roles/edit/2',data=dict(name='Fixture manager',permissions=['1'],csrf_token='fixture-token')).status_code==302
        assert restricted.get('/api/probe/view').status_code==200
        assert restricted.get('/api/probe/billing').status_code==403
        assert administrator.post('/roles/edit/2',data=dict(name='Fixture manager',permissions=['1','2'],csrf_token='fixture-token')).status_code==302
        assert restricted.get('/api/probe/billing').status_code==200
        assert administrator.post('/roles/edit/2',data=dict(name='Fixture manager',permissions=[],csrf_token='fixture-token')).status_code==302
        assert restricted.get('/api/probe/view').status_code==403
        assert restricted.get('/api/probe/billing').status_code==403
        # The same manager session sees a newly assigned role without signing in again.
        new_role=ns['create_role'](dict(name='Fixture view',code='fixture_view'),[1])
        ns['update_manager'](2,dict(full_name='Fixture',role_id=new_role))
        assert restricted.get('/api/probe/view').status_code==200
        assert restricted.get('/api/probe/billing').status_code==403
        write('UPDATE wisp_managers SET is_active=0 WHERE id=2')
        assert restricted.get('/api/probe/view').status_code==401
    check('grant/revoke actual role permissions through CSRF-protected HTTP affects existing manager session immediately',permissions_existing_session)
    results.append('reassigning manager role and disabling account also affect the existing session')
    print(json.dumps(dict(isolated=True,tests=len(results),passed=results,customer_operations=0)))


if __name__=='__main__':run()

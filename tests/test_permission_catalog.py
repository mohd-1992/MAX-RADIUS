"""Catalog migration, live grants and direct HTTP; no app/config/NAS imports."""
import ast
import sqlite3
import unittest
from pathlib import Path
from flask import Flask, request, session, jsonify, redirect, url_for, render_template
from core.permission_catalog import (permission_rows, required_detail, DETAILS,
                                     ACTION_ENDPOINTS, ACCOUNT_ACTIONS, BATCH_ACTIONS, PACKAGE_ACTIONS)
from database.permission_catalog import install_permission_catalog

ROOT = Path(__file__).resolve().parents[1]


class PermissionCatalogTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''CREATE TABLE wisp_roles(id INTEGER PRIMARY KEY);
            CREATE TABLE wisp_permissions(id INTEGER PRIMARY KEY,code TEXT UNIQUE,name TEXT,category TEXT,description TEXT);
            CREATE TABLE wisp_role_permissions(role_id INTEGER,permission_id INTEGER, UNIQUE(role_id,permission_id));
            INSERT INTO wisp_roles VALUES(1),(2),(3);''')
        self.catalog = {row['code']: row for row in permission_rows()}
        for parent in sorted({row['parent'] for row in self.catalog.values()}):
            self.db.execute('INSERT INTO wisp_permissions(code,name,category,description) VALUES(?,?,?,?)', (parent,parent,'fixture',''))
            ident = self.db.execute('SELECT id FROM wisp_permissions WHERE code=?', (parent,)).fetchone()[0]
            self.db.execute('INSERT INTO wisp_role_permissions VALUES(2,?)', (ident,))
        self.db.commit()
        with self.db:
            install_permission_catalog(self.db, mysql=False)
        def read(sql, args=()):
            return [dict(row) for row in self.db.execute(sql,args).fetchall()]
        def manager():
            return {'id':session.get('manager_id',2),'role_id':session.get('role_id',2)} if session.get('authenticated',True) else None
        rbac = dict(get_current_manager=manager,query_all=read,query_one=lambda s,p=(): next(iter(read(s,p)),None))
        tree=ast.parse((ROOT/'core/rbac.py').read_text(encoding='utf8'))
        exec(compile(ast.Module(body=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in ('has_permission','get_manager_permissions')],type_ignores=[]),'rbac','exec'),rbac)
        self.app=Flask('catalog_test',template_folder=str(ROOT/'web/templates'))
        self.app.secret_key='isolated-test'
        self.ns=dict(request=request,jsonify=jsonify,redirect=redirect,url_for=url_for,render_template=render_template,
            owner_id=lambda:None,can_access_owned_endpoint=lambda endpoint:True,guard_owned_request=lambda:None,
            required_detail=required_detail,ACTION_ENDPOINTS=ACTION_ENDPOINTS,has_permission=rbac['has_permission'],get_current_manager=manager)
        exec((ROOT/'web/endpoint_permissions.py').read_text(encoding='utf8'),self.ns)
        policy=ast.parse((ROOT/'web/access_policy.py').read_text(encoding='utf8'))
        exec(compile(ast.Module(body=[node for node in policy.body if isinstance(node,(ast.FunctionDef,ast.Assign))],type_ignores=[]),'policy','exec'),self.ns)
        self.app.add_url_rule('/login','login',lambda:'login')
        for index, (endpoint, code) in enumerate([(e,required_detail(e)) for _,_,_,eps in DETAILS for e in eps.split()]):
            self.app.add_url_rule('/check/'+endpoint,endpoint,lambda:jsonify(success=True),methods=['GET','POST'])
        for endpoint in (*ACTION_ENDPOINTS, 'batches_bulk_action','packages_bulk_action'):
            self.app.add_url_rule('/check/'+endpoint,endpoint,lambda:jsonify(success=True),methods=['GET','POST'])
        self.ns['install_visibility_policy'](self.app)
        self.client=self.app.test_client()

    def tearDown(self):
        self.db.close()

    def revoke(self,code):
        self.db.execute('DELETE FROM wisp_role_permissions WHERE role_id=2 AND permission_id=(SELECT id FROM wisp_permissions WHERE code=?)',(code,));self.db.commit()

    def grant(self,code):
        self.db.execute('INSERT OR IGNORE INTO wisp_role_permissions SELECT 2,id FROM wisp_permissions WHERE code=?',(code,));self.db.commit()

    def test_catalog_unique_arabic_and_known_endpoints(self):
        rows=permission_rows()
        self.assertEqual(len(rows),len(self.catalog))
        self.assertTrue(all(len(row['code'])<=80 and any('\u0600'<=c<='\u06ff' for c in row['name']) for row in rows))
        for _,_,parent,eps in DETAILS:
            for endpoint in eps.split():
                self.assertIn(endpoint,self.ns['ENDPOINT_PERMISSIONS'])
                self.assertIn(required_detail(endpoint),self.catalog)

    def test_migration_preserves_and_does_not_regrant(self):
        code='detail.system.factory_reset';self.revoke(code)
        before=list(self.db.execute('SELECT * FROM wisp_role_permissions ORDER BY role_id,permission_id'))
        with self.db:self.assertEqual(install_permission_catalog(self.db,mysql=False),0)
        self.assertEqual(before,list(self.db.execute('SELECT * FROM wisp_role_permissions ORDER BY role_id,permission_id')))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM wisp_role_permissions WHERE role_id=3').fetchone()[0],0)

    def test_every_detail_enforced_and_changes_without_new_login(self):
        for suffix,_,_,eps in DETAILS:
            code='detail.'+suffix
            for endpoint in eps.split():
              with self.subTest(endpoint=endpoint):
                self.revoke(code)
                response=self.client.get('/check/'+endpoint,headers={'X-Requested-With':'XMLHttpRequest'})
                self.assertEqual(response.status_code,403)
                self.grant(code)
                self.assertEqual(self.client.get('/check/'+endpoint).status_code,200)

    def test_account_type_permissions_are_independent(self):
        for endpoint,action in ACTION_ENDPOINTS.items():
            card_code=required_detail(endpoint,{'target_type':'voucher'})
            self.revoke(card_code)
            with self.subTest(action=action):
                self.assertEqual(self.client.post('/check/'+endpoint,json={'target_type':'card'}).status_code,403)
                self.assertEqual(self.client.post('/check/'+endpoint,json={'target_type':'subscriber'}).status_code,200)
            self.grant(card_code)

    def test_bulk_actions_checked_individually(self):
        for endpoint,actions in (('batches_bulk_action',BATCH_ACTIONS),('packages_bulk_action',PACKAGE_ACTIONS)):
            for action in actions:
                code=required_detail(endpoint,{'action':action});self.revoke(code)
                with self.subTest(action=action):
                    self.assertEqual(self.client.post('/check/'+endpoint,json={'action':' '+action+' '}).status_code,403)
                    self.grant(code)
                    self.assertEqual(self.client.post('/check/'+endpoint,json={'action':action}).status_code,200)
        self.assertEqual(self.client.post('/check/packages_bulk_action',json={'action':'unknown'}).status_code,400)
        self.assertEqual(self.client.post('/check/api_renew',json=['invalid']).status_code,400)

    def test_legacy_parent_still_required(self):
        self.revoke('system.settings')
        self.assertEqual(self.client.post('/check/api_factory_reset_database',json={}).status_code,403)

    def test_admin_keeps_all_details(self):
        for code in self.catalog:self.revoke(code)
        with self.client.session_transaction() as cookie:cookie['manager_id']=1;cookie['role_id']=1
        self.assertEqual(self.client.post('/check/api_factory_reset_database',json={}).status_code,200)

    def test_detailed_api_still_requires_login(self):
        with self.client.session_transaction() as cookie:cookie['authenticated']=False
        self.assertEqual(self.client.post('/check/api_factory_reset_database',json={}).status_code,401)

    def test_helpers_hide_denied_links_and_actions(self):
        self.revoke('detail.system.loyalty_view');self.revoke('detail.vouchers.action_renew')
        with self.app.test_request_context('/'):
            self.assertFalse(self.ns['can_access_endpoint']('loyalty_rewards_page'))
            self.assertFalse(self.ns['can_access_account_action']('renew','voucher'))
            self.assertTrue(self.ns['can_access_account_action']('renew','subscriber'))


if __name__=='__main__':unittest.main()

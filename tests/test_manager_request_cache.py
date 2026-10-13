"""Request-bound manager reads: isolated Flask contexts, no app/DB startup."""
import ast
import unittest
from pathlib import Path
from flask import Flask, session, request, g, has_request_context, current_app

ROOT=Path(__file__).resolve().parents[1]
class ManagerRequestCache(unittest.TestCase):
    def setUp(self):
        self.app=Flask('tenant-a');self.app.secret_key='fixture'
        self.calls=[];self.failure=False
        self.rows={2:dict(id=2,role_id=2,role_code='reseller',is_active=1),3:dict(id=3,role_id=3,role_code='reseller',is_active=1)}
        self.grants={2:{'vouchers.view'},3:set()}
        def query_one(sql,args):
            self.calls.append((current_app.name,args[0]))
            if self.failure:raise RuntimeError('fixture DB unavailable')
            row=self.rows.get(args[0]);return dict(row) if row and row['is_active'] else None
        self.ns=dict(session=session,request=request,query_one=query_one,
                     get_manager_permissions=lambda role_id:self.grants[role_id])
        tree=ast.parse((ROOT/'core/rbac.py').read_text(encoding='utf-8'))
        nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('get_current_manager','has_permission')]
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'rbac-cache-fixture','exec'),self.ns)
        self.get=self.ns['get_current_manager'];self.can=self.ns['has_permission']
    def test_many_checks_use_one_read(self):
        with self.app.test_request_context('/'):
            session['admin_id']=2
            for _ in range(100):self.assertTrue(self.can('vouchers.view'))
        self.assertEqual(len(self.calls),1)
    def test_next_request_observes_disabled_and_reactivated_account(self):
        for active in (1,0,1):
            self.rows[2]['is_active']=active
            with self.app.test_request_context('/'):
                session['admin_id']=2;self.assertEqual(self.get() is not None,bool(active))
        self.assertEqual(len(self.calls),3)
    def test_next_request_observes_role_and_grant_changes(self):
        with self.app.test_request_context('/'):
            session['admin_id']=2;self.assertTrue(self.can('vouchers.view'))
        self.grants[2]=set()
        with self.app.test_request_context('/'):
            session['admin_id']=2;self.assertFalse(self.can('vouchers.view'))
        self.rows[2]['role_id']=3;self.grants[3]={'vouchers.view'}
        with self.app.test_request_context('/'):
            session['admin_id']=2;self.assertTrue(self.can('vouchers.view'))
        self.assertEqual(len(self.calls),3)
    def test_identity_change_and_logout(self):
        with self.app.test_request_context('/'):
            session['admin_id']=2;self.assertEqual(self.get()['id'],2)
            session['admin_id']=3;self.assertEqual(self.get()['id'],3)
            session.clear();self.assertIsNone(self.get())
            session['admin_id']=2;self.assertEqual(self.get()['id'],2)
        self.assertEqual(len(self.calls),3)
    def test_returned_object_cannot_poison_cached_account(self):
        with self.app.test_request_context('/'):
            session['admin_id']=2;row=self.get();row['role_id']=1
            self.assertEqual(self.get()['role_id'],2)
        self.assertEqual(len(self.calls),1)
    def test_failure_denied_and_retried_next_request(self):
        self.failure=True
        with self.app.test_request_context('/'):
            session['admin_id']=2;self.assertIsNone(self.get());self.assertFalse(self.can('vouchers.view'))
        self.failure=False
        with self.app.test_request_context('/'):
            session['admin_id']=2;self.assertIsNotNone(self.get())
        self.assertEqual(len(self.calls),2)
    def test_missing_account_cached_only_for_request(self):
        with self.app.test_request_context('/'):
            session['admin_id']=99;self.assertIsNone(self.get());self.assertIsNone(self.get())
        self.assertEqual(len(self.calls),1)
    def test_retained_app_context_does_not_reuse_prior_request(self):
        with self.app.app_context():
            for _ in range(2):
                with self.app.test_request_context('/'):
                    session['admin_id']=2;self.assertIsNotNone(self.get())
        self.assertEqual(len(self.calls),2)
    def test_separate_tenants_with_same_account_id(self):
        other=Flask('tenant-b');other.secret_key='fixture'
        with self.app.test_request_context('/'):
            session['admin_id']=2;self.assertEqual(self.get()['role_id'],2)
            with other.test_request_context('/'):
                session['admin_id']=2;self.rows[2]['role_id']=3;self.assertEqual(self.get()['role_id'],3)
            self.assertEqual(self.get()['role_id'],2)
        self.assertEqual(self.calls,[('tenant-a',2),('tenant-b',2)])
    def test_no_request_or_no_login_denied_without_query(self):
        self.assertIsNone(self.get())
        with self.app.test_request_context('/'):self.assertIsNone(self.get())
        self.assertEqual(self.calls,[])
if __name__=='__main__':unittest.main()

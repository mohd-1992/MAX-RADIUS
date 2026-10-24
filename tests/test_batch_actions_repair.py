"""Batch UI, permissions and refunds on synthetic SQLite/Flask; no app workers/NAS."""
import ast
import datetime
import json
import sqlite3
import sys
import types
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from flask import Flask, jsonify, request, session, redirect, url_for

ROOT = Path(__file__).resolve().parents[1]


def load(path, names, namespace):
    tree = ast.parse((ROOT / path).read_text(encoding='utf8'))
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in names]
    for node in nodes:
        node.decorator_list = []
    exec(compile(ast.Module(body=nodes, type_ignores=[]), path, 'exec'), namespace)


class VisibilityTests(unittest.TestCase):
    def setUp(self):
        self.grants = set()
        self.ns = dict(can_access_owned_endpoint=lambda endpoint: True,
                       endpoint_enabled=lambda *args: True,
                       get_current_manager=lambda: {'id': 1},
                       has_permission=lambda code, *args: code in self.grants)
        exec((ROOT/'web/endpoint_permissions.py').read_text(encoding='utf8'), self.ns)
        catalog = {}
        exec((ROOT/'core/permission_catalog.py').read_text(encoding='utf8'), catalog)
        self.ns['required_detail'] = catalog['required_detail']
        load('web/access_policy.py', ['can_access_endpoint'], self.ns)

    def allowed(self, action):
        return self.ns['can_access_endpoint']('batches_bulk_action', {'action': action})

    def test_each_action_requires_own_parent_and_detail(self):
        for action, parent in [('delete','vouchers.delete'), ('export','vouchers.print'),
                               ('change_package','vouchers.actions'), ('assign_reseller','vouchers.actions')]:
            with self.subTest(action=action):
                detail='detail.vouchers.bulk_'+action
                self.grants.clear(); self.grants.update(['vouchers.view',parent,detail])
                self.assertTrue(self.allowed(action))
                self.grants.remove(detail); self.assertFalse(self.allowed(action))
                self.grants.add(detail); self.grants.remove(parent); self.assertFalse(self.allowed(action))

    def test_delete_and_export_do_not_require_general_actions(self):
        self.grants.update(['vouchers.view','vouchers.delete','vouchers.print',
                            'detail.vouchers.bulk_delete','detail.vouchers.bulk_export'])
        self.assertTrue(self.allowed('delete')); self.assertTrue(self.allowed('export'))
        self.assertFalse(self.allowed('change_package'))

    def test_no_grants_and_invalid_actions_hidden(self):
        self.assertFalse(self.allowed('delete')); self.assertFalse(self.allowed('unknown'))
        self.ns['has_permission']=lambda *args:True
        self.assertFalse(self.allowed('unknown'))

    def test_ownership_and_license_still_deny(self):
        self.grants.update(['vouchers.view','vouchers.delete','detail.vouchers.bulk_delete'])
        self.ns['can_access_owned_endpoint']=lambda _:False
        self.assertFalse(self.allowed('delete'))
        self.ns['can_access_owned_endpoint']=lambda _:True
        self.ns['endpoint_enabled']=lambda *args:False
        self.assertFalse(self.allowed('delete'))

    def test_direct_http_enforces_parent_and_detail(self):
        app=Flask('batch-permissions');app.secret_key='synthetic-only'
        self.ns.update(request=request,jsonify=jsonify,redirect=redirect,url_for=url_for,
                       guard_owned_request=lambda:None,permission_denied=lambda:(jsonify(success=False),403),
                       ADMIN_BLUEPRINTS=set(),TARGET_GUARDED_ENDPOINTS=set())
        load('web/access_policy.py',['install_visibility_policy'],self.ns)
        self.ns['install_visibility_policy'](app)
        app.add_url_rule('/bulk',endpoint='batches_bulk_action',view_func=lambda:jsonify(success=True),methods=['POST'])
        client=app.test_client()
        self.grants.update(['vouchers.view','detail.vouchers.bulk_delete'])
        self.assertEqual(client.post('/bulk',json={'action':'delete'}).status_code,403)
        self.grants.add('vouchers.delete')
        self.assertEqual(client.post('/bulk',json={'action':'delete'}).status_code,200)
        self.assertEqual(client.post('/bulk',json={'action':'export'}).status_code,403)
        # Use the real target-ownership guard for a reseller request.
        self.ns.update(owner_id=lambda:7, json=json,
                       raw_query_one=lambda q,p:{'id':1} if p==(1,7) else None)
        load('core/ownership.py',['require_owned','guard_owned_request'],self.ns)
        self.assertEqual(client.post('/bulk',json={'action':'delete','batch_ids':[1]}).status_code,200)
        self.assertEqual(client.post('/bulk',json={'action':'delete','batch_ids':[1,2]}).status_code,403)
        self.grants.update(['vouchers.actions','detail.vouchers.bulk_assign_reseller'])
        self.assertEqual(client.post('/bulk',json={'action':'assign_reseller','batch_ids':[1]}).status_code,403)


class RefundTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:'); self.db.row_factory=sqlite3.Row
        self.db.create_function('NOW',0,lambda:'2026-10-24 00:00:00')
        self.db.executescript('''
CREATE TABLE wisp_voucher_batches(id INTEGER PRIMARY KEY,batch_number TEXT,reseller_id INT,card_count INT,cost NUMERIC,price NUMERIC);
CREATE TABLE wisp_vouchers(id INTEGER PRIMARY KEY,batch_id INT,username TEXT,status TEXT);
CREATE TABLE radacct(username TEXT,acctstoptime TEXT,acctterminatecause TEXT);
CREATE TABLE wisp_session_reservations(username TEXT,expires_at TEXT);
CREATE TABLE wisp_managers(id INT,wallet_balance NUMERIC);
CREATE TABLE wisp_resellers(id INT,balance NUMERIC);
CREATE TABLE wisp_manager_invoices(invoice_number TEXT UNIQUE,manager_id INT,transaction_type TEXT,amount NUMERIC,payment_type TEXT,balance_before NUMERIC,balance_after NUMERIC,notes TEXT,is_voided INT,created_by TEXT);
CREATE TABLE wisp_reseller_transactions(reseller_id INT,type TEXT,amount NUMERIC,balance_after NUMERIC,description TEXT,reference_id TEXT);
CREATE TABLE radcheck(username TEXT); CREATE TABLE radreply(username TEXT); CREATE TABLE radusergroup(username TEXT);
INSERT INTO wisp_managers VALUES(7,100);
INSERT INTO wisp_voucher_batches VALUES(1,'FIX-1',7,2,8,10);
INSERT INTO wisp_vouchers VALUES(1,1,'synthetic-a','unused'),(2,1,'synthetic-b','unused');
INSERT INTO radacct VALUES('synthetic-a','2026-10-01','User-Request');
INSERT INTO radcheck VALUES('synthetic-a');
''')
        self.ns={'Decimal':Decimal,'sql':self.sql,'run_transaction':self.tx,'delete_account':self.defer}
        load('services/account_lifecycle_service.py', ['BatchDeletionPending','batch_refund_amount','delete_voucher_batch'], self.ns)
        module=types.ModuleType('database.db'); module.query_all=lambda q,p=():self.sql(self.db,q,p,'all')
        self.modules=patch.dict(sys.modules,{'database.db':module}); self.modules.start()
        self.deferred=[]

    def tearDown(self):
        self.modules.stop(); self.db.close()

    def defer(self,kind,ident): self.deferred.append((kind,ident))

    def sql(self,conn,q,p=(),fetch=None):
        p=tuple(float(v) if isinstance(v,Decimal) else v for v in p)
        c=conn.execute(q.replace(' FOR UPDATE',''),p)
        if fetch=='one':
            row=c.fetchone();return dict(row) if row else None
        if fetch=='all':return [dict(row) for row in c.fetchall()]
        return c.rowcount

    def tx(self,operation):
        with self.db:return operation(self.db)

    def purchase(self,paid,voided=0,marker='FIX-1'):
        self.db.execute("INSERT INTO wisp_manager_invoices(invoice_number,manager_id,transaction_type,amount,notes,is_voided) VALUES(?,7,'card_purchase',?,?,?)",
                        ('purchase-'+str(self.db.total_changes),paid,'شراء دفعة كروت (2 كرت - دفعة #'+marker+')',voided))
        self.db.commit()

    def delete(self):return self.ns['delete_voucher_batch'](1,'synthetic-admin')
    def balance(self):return self.db.execute('SELECT wallet_balance FROM wisp_managers').fetchone()[0]

    def test_zero_debit_never_refunds_price(self):
        self.db.execute('UPDATE wisp_voucher_batches SET cost=0'); self.purchase(0)
        self.assertTrue(self.delete()); self.assertEqual(self.balance(),100)

    def test_paid_refund_preserves_history_and_ledger(self):
        self.purchase(16); self.assertTrue(self.delete()); self.assertEqual(self.balance(),116)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM radacct').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM wisp_manager_invoices').fetchone()[0],2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM radcheck').fetchone()[0],0)

    def test_only_unused_share_is_refunded(self):
        self.purchase(16);self.db.execute("UPDATE wisp_vouchers SET status='expired' WHERE id=2");self.db.commit()
        self.delete();self.assertEqual(self.balance(),108)

    def test_package_cost_change_does_not_change_original_refund(self):
        self.purchase(16);self.db.execute('UPDATE wisp_voucher_batches SET cost=100');self.db.commit()
        self.delete();self.assertEqual(self.balance(),116)

    def test_missing_or_voided_debit_never_creates_credit(self):
        self.purchase(16,voided=1);self.delete();self.assertEqual(self.balance(),100)

    def test_imported_batch_without_purchase_no_credit(self):
        self.delete();self.assertEqual(self.balance(),100)

    def test_repeat_delete_has_no_second_refund(self):
        self.purchase(16);self.assertTrue(self.delete());self.assertFalse(self.delete());self.assertEqual(self.balance(),116)

    def test_duplicate_purchase_rolls_back(self):
        self.purchase(16);self.purchase(16)
        with self.assertRaises(ValueError):self.delete()
        self.assertEqual(self.balance(),100);self.assertEqual(self.db.execute('SELECT COUNT(*) FROM wisp_vouchers').fetchone()[0],2)

    def test_invoice_failure_rolls_back_wallet_and_cards(self):
        self.purchase(16)
        self.db.execute("CREATE TRIGGER fail_refund BEFORE INSERT ON wisp_manager_invoices WHEN NEW.transaction_type='refund' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END");self.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):self.delete()
        self.assertEqual(self.balance(),100);self.assertEqual(self.db.execute('SELECT COUNT(*) FROM wisp_vouchers').fetchone()[0],2)

    def test_open_session_defers_without_refund_or_delete(self):
        self.purchase(16);self.db.execute("INSERT INTO radacct VALUES('synthetic-a',NULL,'')");self.db.commit()
        with self.assertRaises(self.ns['BatchDeletionPending']):self.delete()
        self.assertEqual(self.deferred,[('voucher',1)]);self.assertEqual(self.balance(),100)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM wisp_vouchers').fetchone()[0],2)

    def test_reservation_defers_without_refund(self):
        self.purchase(16);self.db.execute("INSERT INTO wisp_session_reservations VALUES('synthetic-a','2030-01-01')");self.db.commit()
        with self.assertRaises(self.ns['BatchDeletionPending']):self.delete()
        self.assertEqual(self.balance(),100)

    def test_legacy_reseller_refund(self):
        self.db.execute('DELETE FROM wisp_managers');self.db.execute('INSERT INTO wisp_resellers VALUES(7,100)')
        self.db.execute("INSERT INTO wisp_reseller_transactions VALUES(7,'card_purchase',16,84,'synthetic','FIX-1')");self.db.commit()
        self.delete();self.assertEqual(self.db.execute('SELECT balance FROM wisp_resellers').fetchone()[0],116)

    def test_purchase_for_different_batch_cannot_refund(self):
        self.purchase(16,marker='FIX-12');self.delete();self.assertEqual(self.balance(),100)

    def test_purchase_marker_wildcards_are_literal(self):
        self.db.execute("UPDATE wisp_voucher_batches SET batch_number='FIX_%'");self.purchase(16,marker='FIX-other')
        self.delete();self.assertEqual(self.balance(),100)


class BulkResponseTests(unittest.TestCase):
    def setUp(self):
        self.app=Flask('batch-actions-fixture');self.app.secret_key='synthetic-only-not-production'
        self.ns={'request':request,'session':session,'jsonify':jsonify,'json':json,
                 'log_audit':lambda *args:None,'logger':types.SimpleNamespace(error=lambda *args:None)}
        load('web/routes/vouchers.py',['batches_bulk_action'],self.ns)
        self.app.add_url_rule('/bulk',view_func=self.ns['batches_bulk_action'],methods=['POST'])
        self.client=self.app.test_client()

    def test_mixed_result_and_pending_reasons(self):
        class Pending(ValueError):code='sessions_pending'
        def delete(ident,**kwargs):
            if ident==1:return True
            if ident==2:raise Pending('synthetic session pending')
            return False
        self.ns['delete_batch']=delete
        r=self.client.post('/bulk',json={'action':'delete','batch_ids':[1,2,3,1]})
        self.assertEqual(r.status_code,200);self.assertTrue(r.json['partial']);self.assertEqual(r.json['successful'],1)
        self.assertEqual([x['status'] for x in r.json['results']],['deleted','pending','not_found'])

    def test_internal_failure_does_not_leak_exception(self):
        def fail(*args,**kwargs):raise RuntimeError('synthetic confidential detail')
        self.ns['delete_batch']=fail
        r=self.client.post('/bulk',json={'action':'delete','batch_ids':[1]})
        self.assertFalse(r.json['success']);self.assertNotIn('confidential',r.get_data(as_text=True))

    def test_expected_rejection_reason_is_returned(self):
        def fail(*args,**kwargs):raise ValueError('synthetic invalid receipt')
        self.ns['delete_batch']=fail
        r=self.client.post('/bulk',json={'action':'delete','batch_ids':[1]})
        self.assertEqual(r.json['results'][0]['message'],'synthetic invalid receipt')


if __name__=='__main__':unittest.main()

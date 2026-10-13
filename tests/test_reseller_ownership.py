"""Real handlers/services on an isolated DB; no app startup, NAS or customer data."""
import ast
import datetime
import json
import re
import types
import unittest
from unittest.mock import patch
from flask import g, has_request_context, request, session, render_template, jsonify, redirect, url_for
from manager_page_fixture import fixture, ROOT, load
from core.permission_catalog import required_detail, ACTION_ENDPOINTS, permission_rows


class ResellerOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.app, self.db, self.ns, self.stubs = fixture()[:4]
        self.db.execute('CREATE TABLE wisp_invoices(id INTEGER PRIMARY KEY, amount REAL, status TEXT, paid_at TEXT, package_name TEXT)')
        raw_one, raw_all = self.ns['query_one'], self.ns['query_all']
        # Verified identity lookup deliberately uses raw identity metadata, not
        # scoped business reads (otherwise looking up the current owner recurses).
        identity = dict(session=session, query_one=raw_one)
        load('core/rbac.py', identity, ['get_current_manager'])
        def read(statement, params=(), one=False):
            statement = statement.replace('CURRENT_TIMESTAMP - INTERVAL 5 MINUTE', "datetime(CURRENT_TIMESTAMP,'-5 minutes')")
            return (raw_one if one else raw_all)(statement, params)
        own = dict(json=json, re=re, g=g, has_request_context=has_request_context,
                   request=request, get_current_manager=identity['get_current_manager'],
                   raw_query_one=lambda s,p=():read(s,p,True), raw_query_all=read)
        tree = ast.parse((ROOT/'core/ownership.py').read_text(encoding='utf8'))
        exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.Assign))],type_ignores=[]),'ownership.py','exec'),own)
        self.own = own
        self.ns.update(query_one=own['scoped_query_one'],query_all=own['scoped_query_all'],
                       get_current_manager=identity['get_current_manager'])
        mod = types.ModuleType('core.ownership')
        for name, value in own.items():
            if not name.startswith('__'):setattr(mod,name,value)
        self.stubs['core.ownership'] = mod
        quick_mod = types.ModuleType('services.quick_card_service')
        quick_mod.get_issue_policy = lambda tx=None: dict(prefix='',char_type='numbers',total_length=8,format_type='user_pass',password_length=8)
        self.stubs['services.quick_card_service'] = quick_mod
        endpoint_mod = types.ModuleType('web.endpoint_permissions')
        exec((ROOT/'web/endpoint_permissions.py').read_text(encoding='utf8'),endpoint_mod.__dict__)
        self.stubs['web.endpoint_permissions'] = endpoint_mod
        self.modules = patch.dict('sys.modules',self.stubs);self.modules.start()
        self.policy=dict(own,has_permission=self.ns['has_permission'],request=request,
                    jsonify=jsonify,redirect=redirect,url_for=url_for,render_template=render_template,
                    ENDPOINT_PERMISSIONS=endpoint_mod.ENDPOINT_PERMISSIONS)
        parents = {row['code']: row['parent'] for row in permission_rows()}
        legacy_check = self.ns['has_permission']
        self.policy.update(required_detail=required_detail, ACTION_ENDPOINTS=ACTION_ENDPOINTS,
            has_permission=lambda code, manager=None: legacy_check(parents.get(code, code), manager))
        self.ns['has_permission'] = self.policy['has_permission']
        policy_tree=ast.parse((ROOT/'web/access_policy.py').read_text(encoding='utf8'))
        exec(compile(ast.Module(body=[n for n in policy_tree.body if isinstance(n,ast.Assign)],type_ignores=[]),'policy_constants','exec'),self.policy)
        load('web/access_policy.py',self.policy)
        self.policy['install_visibility_policy'](self.app)
        self.client=self.app.test_client()
        self.login(2)
        self.db.execute('UPDATE wisp_voucher_batches SET reseller_id=3 WHERE id=1')
        self.db.execute('UPDATE wisp_vouchers SET reseller_id=3 WHERE batch_id=1')
        self.db.execute('UPDATE wisp_packages SET cost=8 WHERE id=1');self.db.commit()

    def tearDown(self):
        self.modules.stop();self.db.close()

    def login(self, ident):
        with self.client.session_transaction() as s:
            s['admin_id']=ident;s['admin_username']='audit'+str(ident)
            s['permissions']=['vouchers.view','vouchers.print','vouchers.generate','vouchers.actions','vouchers.delete',
                'managers.view','managers.manage','managers.billing','roles.manage','subscribers.view','wallets.manage','invoices.view']
            s['_license_csrf']='fixture-token'

    def context(self, ident=2):
        ctx=self.app.test_request_context('/vouchers');ctx.push()
        session['admin_id']=ident
        return ctx

    def post(self, path, **kw):
        return self.client.post(path,headers={'X-CSRF-Token':'fixture-token'},**kw)

    def make_own_batch(self):
        ctx=self.context()
        try:return self.ns['generate_voucher_batch']('owned',1,2,reseller_id=None)[0]
        finally:ctx.pop()

    def test_static_assets_remain_public_with_restricted_manager_cookie(self):
        self.app.static_folder = str(ROOT / 'web/static')
        for ident in (2, 1, None):
            with self.client.session_transaction() as cookie:
                cookie.clear()
                if ident is not None:
                    cookie['admin_id'] = ident
                    cookie['permissions'] = []
            for asset, mime in (
                ('vendor/tailwind/tailwind.js', 'javascript'),
                ('vendor/fonts/tajawal.css', 'text/css'),
                ('vendor/fontawesome/css/all.min.css', 'text/css'),
            ):
                with self.subTest(manager=ident, asset=asset):
                    response = self.client.get('/static/' + asset)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn(mime, response.content_type)
                    response.close()
            self.assertEqual(self.client.get('/static/not-a-real-asset.css').status_code, 404)
            self.assertEqual(self.client.get('/static/../core/ownership.py').status_code, 404)

    def test_omitted_owner_charges_current_wallet(self):
        bid=self.make_own_batch()
        self.assertEqual(self.db.execute('SELECT reseller_id FROM wisp_voucher_batches WHERE id=?',(bid,)).fetchone()[0],2)
        self.assertEqual(self.db.execute('SELECT wallet_balance FROM wisp_managers WHERE id=2').fetchone()[0],84)
        self.assertEqual(self.db.execute('SELECT manager_id,amount FROM wisp_manager_invoices').fetchone()[:],(2,16))

    def test_forged_owner_cannot_charge_another_wallet(self):
        ctx=self.context()
        try:self.ns['generate_voucher_batch']('owned',1,1,reseller_id=3)
        finally:ctx.pop()
        self.assertEqual(self.db.execute('SELECT wallet_balance FROM wisp_managers WHERE id=3').fetchone()[0],100)
        self.assertEqual(self.db.execute('SELECT wallet_balance FROM wisp_managers WHERE id=2').fetchone()[0],92)

    def test_insufficient_wallet_leaves_no_cards_or_invoice(self):
        self.db.execute('UPDATE wisp_managers SET wallet_balance=0 WHERE id=2');self.db.commit()
        ctx=self.context()
        try:
            with self.assertRaises(ValueError):self.ns['generate_voucher_batch']('owned',1,2,reseller_id=None)
        finally:ctx.pop()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM wisp_vouchers').fetchone()[0],6)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM wisp_manager_invoices').fetchone()[0],0)

    def test_disallowed_package_rejected_before_purchase(self):
        self.db.execute("UPDATE wisp_managers SET allowed_packages='[99]' WHERE id=2");self.db.commit()
        ctx=self.context()
        try:
            with self.assertRaises(PermissionError):self.ns['generate_voucher_batch']('owned',1,1)
        finally:ctx.pop()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM wisp_voucher_batches').fetchone()[0],1)

    def test_admin_can_generate_unassigned_inventory(self):
        ctx=self.context(1)
        try:bid=self.ns['generate_voucher_batch']('admin',1,1)[0]
        finally:ctx.pop()
        self.assertIsNone(self.db.execute('SELECT reseller_id FROM wisp_voucher_batches WHERE id=?',(bid,)).fetchone()[0])

    def test_read_aggregates_and_joins_are_scoped(self):
        bid=self.make_own_batch();ctx=self.context()
        try:
            self.assertEqual(len(self.ns['get_batches']()),1)
            self.assertEqual(len(self.ns['get_vouchers']()),2)
            self.assertEqual(self.ns['get_voucher_summary_counts']()['total_cards'],2)
            self.assertEqual([m['id'] for m in self.ns['get_all_managers']()],[2])
            self.assertEqual(len(self.ns['get_all_manager_invoices']()),1)
        finally:ctx.pop()

    def test_foreign_filter_cannot_expand_scope(self):
        self.make_own_batch();ctx=self.context()
        try:
            self.assertEqual(self.ns['get_batches'](reseller_id=3),[])
            self.assertEqual(self.ns['get_vouchers'](batch_id=1),[])
        finally:ctx.pop()

    def test_foreign_details_and_print_denied(self):
        for path in ['/vouchers/batch/1','/vouchers/print/1','/vouchers/active-users/2']:
            with self.subTest(path=path):self.assertEqual(self.client.get(path).status_code,403)

    def test_own_export_succeeds_foreign_and_mixed_rejected(self):
        own=self.make_own_batch()
        for ids in ([1],[own,1]):
            self.assertEqual(self.post('/vouchers/batches/bulk-action',json={'action':'export','batch_ids':ids}).status_code,403)
        self.assertEqual(self.post('/vouchers/batches/bulk-action',json={'action':'export','batch_ids':[own]}).status_code,200)

    def test_assignment_is_admin_only(self):
        own=self.make_own_batch()
        self.assertEqual(self.post('/vouchers/batches/bulk-action',json={'action':'assign_reseller','batch_ids':[own],'reseller_id':None}).status_code,403)
        self.assertEqual(self.db.execute('SELECT reseller_id FROM wisp_voucher_batches WHERE id=?',(own,)).fetchone()[0],2)

    def test_foreign_mutations_and_mac_reset_denied(self):
        cases=[('/vouchers/update-card',{'card_id':2}),('/api/vouchers/reset-mac',{'username':'fixture-2'}),
               ('/api/vouchers/extend-quota-time',{'username':'fixture-2','days':5}),
               ('/api/vouchers/disconnect',{'username':'fixture-2'}),('/api/vouchers/activate',{'card_id':2})]
        for path,data in cases:
            with self.subTest(path=path):self.assertEqual(self.post(path,json=data).status_code,403)

    def test_manager_writes_roles_and_money_denied_even_with_permission(self):
        cases=[('/managers/edit/1',{'full_name':'changed'}),('/managers/add',{}),
               ('/managers/2/deposit',{'amount':100,'request_id':'audit'}),
               ('/api/reseller-wallets/transfer',{'from_id':3,'to_id':2,'amount':5,'request_id':'audit'}),
               ('/roles/add',{'name':'admin'})]
        for path,data in cases:
            with self.subTest(path=path):self.assertEqual(self.post(path,data=data).status_code,403)
        self.assertEqual(self.db.execute('SELECT wallet_balance FROM wisp_managers WHERE id=2').fetchone()[0],100)

    def test_receipt_is_scoped_and_read_only(self):
        self.login(1)
        self.ns['deduct_manager_wallet'](2,10,notes='fixture')
        self.ns['deduct_manager_wallet'](3,10,notes='fixture')
        receipts=self.ns['query_all']('SELECT id,manager_id FROM wisp_manager_invoices')
        own=next(x['id'] for x in receipts if x['manager_id']==2)
        other=next(x['id'] for x in receipts if x['manager_id']==3)
        self.login(2)
        self.assertEqual(self.client.get('/managers/invoices/%s/receipt'%own).status_code,200)
        self.assertEqual(self.client.get('/managers/invoices/%s/receipt'%other).status_code,404)
        self.assertEqual(self.post('/managers/2/deduct',data={'amount':10,'reason':'سحب نقدي'}).status_code,403)
        self.login(1)
        self.assertEqual(len(self.ns['query_all']('SELECT id FROM wisp_manager_invoices')),2)

    def test_foreign_statement_denied_own_statement_visible(self):
        self.assertEqual(self.client.get('/managers/3/statement').status_code,403)
        self.assertEqual(self.client.get('/managers/2/statement').status_code,200)

    def test_no_legacy_identity_collision(self):
        ctx=self.context()
        try:self.assertEqual(self.ns['query_all']('SELECT * FROM wisp_resellers'),[])
        finally:ctx.pop()

    def test_admin_reads_all_rows(self):
        self.make_own_batch();ctx=self.context(1)
        try:
            self.assertEqual(len(self.ns['get_vouchers']()),8)
            self.assertEqual(len(self.ns['get_all_managers']()),3)
        finally:ctx.pop()

    def test_background_queries_unchanged(self):
        sql='SELECT * FROM wisp_vouchers WHERE id=?'
        self.assertEqual(self.own['scope_select'](sql),sql)

    def test_subscriber_portal_is_not_scoped_by_admin_cookie(self):
        with self.app.test_request_context('/portal'):
            session['admin_id']=2
            self.assertIsNone(self.own['owner_id']())

    def test_session_scope_changes_after_login_change(self):
        ctx=self.context(2)
        try:self.assertEqual(self.own['owner_id'](),2)
        finally:ctx.pop()

    def load_dashboard(self):
        policy=dict(self.own,has_permission=self.ns['has_permission'],request=request,
                    required_detail=required_detail, ACTION_ENDPOINTS=ACTION_ENDPOINTS,
                    jsonify=jsonify,redirect=redirect,url_for=url_for,render_template=render_template,
                    ENDPOINT_PERMISSIONS=self.stubs['web.endpoint_permissions'].ENDPOINT_PERMISSIONS)
        load('web/access_policy.py',policy)
        self.ns.update(filter_dashboard_metrics=policy['filter_dashboard_metrics'],
                       can_view_network_totals=policy['can_view_network_totals'],owner_id=self.own['owner_id'],
                       get_dashboard_metrics=lambda:dict(active_sessions=97312345123,total_subscribers=86312345123),
                       get_internet_ping=lambda:dict(online=True),get_system_uptime_str=lambda:'secret uptime',
                       get_latest_system_operations=lambda **kw:[],get_configured_timezone_name=lambda:'Asia/Aden',
                       APP_VERSION_FULL='test',render_template=render_template,jsonify=jsonify)
        rate=types.ModuleType('core.rate_limit');rate.format_bytes=lambda v:str(v)
        self.stubs['core.rate_limit']=rate
        load('services/stats_service.py',self.ns,['get_sales_chart_data','get_traffic_chart_data'])
        load('web/routes/dashboard.py',self.ns,['dashboard','api_dashboard_live_metrics'])
        self.app.view_functions['dashboard']=self.ns['dashboard']
        self.app.add_url_rule('/api/dashboard/live-metrics','api_dashboard_live_metrics',self.ns['api_dashboard_live_metrics'])
        self.app.context_processor(lambda:dict(can_access_endpoint=policy['can_access_endpoint']))
        return policy

    def test_dashboard_own_counts_not_cached_global_counts(self):
        self.make_own_batch();self.load_dashboard()
        result=self.client.get('/api/dashboard/live-metrics')
        self.assertEqual(result.status_code,200)
        metrics=result.json['metrics']
        self.assertEqual(metrics['vouchers']['total'],2)
        self.assertNotIn('total_subscribers',metrics)
        self.assertNotIn('total_nas',metrics)
        self.assertNotIn('resources',metrics)
        self.assertEqual(metrics['active_sessions'],0)

    def test_dashboard_html_no_foreign_counts_or_subscriber_section(self):
        self.make_own_batch();self.load_dashboard()
        result=self.client.get('/dashboard')
        self.assertEqual(result.status_code,200)
        html=result.get_data(as_text=True)
        self.assertNotIn('97312345123',html)
        self.assertNotIn('86312345123',html)
        self.assertNotIn('id="stat-online-subs"',html)

    def test_sales_and_traffic_queries_only_own_rows(self):
        bid=self.make_own_batch();self.load_dashboard()
        stamp=datetime.date.today().isoformat()+' 12:00:00'
        self.db.execute('INSERT INTO wisp_voucher_sales(reseller_id,price,activated_at) VALUES(2,11,?)',(stamp,))
        self.db.execute('INSERT INTO wisp_voucher_sales(reseller_id,price,activated_at) VALUES(3,9999,?)',(stamp,))
        self.db.commit();ctx=self.context()
        try:self.assertEqual(sum(self.ns['get_sales_chart_data']()['sales']),11)
        finally:ctx.pop()

    def test_request_target_guard_blocks_whole_mixed_action_before_handler(self):
        calls=[]
        self.app.add_url_rule('/api/actions/renew','api_renew',lambda:calls.append(True) or jsonify(success=True),methods=['POST'])
        self.make_own_batch()
        own=self.db.execute('SELECT id FROM wisp_vouchers WHERE reseller_id=2 LIMIT 1').fetchone()[0]
        result=self.post('/api/actions/renew',json={'target_type':'voucher','target_ids':[own,2]})
        self.assertEqual(result.status_code,403);self.assertEqual(calls,[])
        self.assertEqual(self.post('/api/actions/renew',json={'target_type':'voucher','target_ids':[own]}).status_code,200)

    def test_zero_permissions_still_blocked_by_existing_permission_guard(self):
        self.make_own_batch()
        with self.client.session_transaction() as s:s['permissions']=[]
        self.assertEqual(self.client.get('/vouchers').status_code,403)

    def test_whitespace_assignment_cannot_bypass_owner_guard(self):
        own=self.make_own_batch()
        self.assertEqual(self.post('/vouchers/batches/bulk-action',json={'action':' assign_reseller ','batch_ids':[own],'reseller_id':3}).status_code,403)

    def test_view_and_finance_html_do_not_include_other_managers(self):
        for path in ['/managers','/managers/invoices','/reseller-wallets']:
            with self.subTest(path=path):
                result=self.client.get(path);self.assertEqual(result.status_code,200)
                html=result.get_data(as_text=True)
                self.assertNotIn('audit1',html);self.assertNotIn('audit3',html)
                self.assertNotIn('onclick="quickDeposit(',html)
                self.assertNotIn('onclick="openDepositModal(',html)

    def test_own_voucher_page_no_unassigned_purchase_option(self):
        self.make_own_batch()
        result=self.client.get('/vouchers')
        self.assertEqual(result.status_code,200)
        html=result.get_data(as_text=True)
        self.assertNotIn('بدون موزع (المخزون الرئيسي للشبكة)',html)
        self.assertNotIn('onclick="openBulkAssignResellerModal()"',html)

    def test_scope_relation_aliases_nested_queries_and_backticks(self):
        self.make_own_batch();ctx=self.context()
        try:
            for sql in ['SELECT COUNT(*) AS n FROM `wisp_vouchers`',
                        'SELECT COUNT(*) AS n FROM wisp_vouchers AS v',
                        'SELECT COUNT(*) AS n FROM (SELECT id FROM wisp_vouchers v) page JOIN wisp_vouchers v ON page.id=v.id',
                        'SELECT (SELECT COUNT(*) FROM wisp_vouchers) AS n']:
                with self.subTest(sql=sql):self.assertEqual(self.ns['query_one'](sql)['n'],2)
        finally:ctx.pop()

    def test_owned_batch_details_and_print_work(self):
        own=self.make_own_batch()
        for path in [f'/vouchers/batch/{own}',f'/vouchers/print/{own}']:
            with self.subTest(path=path):self.assertEqual(self.client.get(path).status_code,200)

    def test_owned_card_rename_guard_checks_id_not_proposed_name(self):
        self.make_own_batch()
        card=self.db.execute('SELECT id FROM wisp_vouchers WHERE reseller_id=2 LIMIT 1').fetchone()[0]
        with self.app.test_request_context(f'/vouchers/card/edit/{card}',method='POST',data={'username':'new-own-name','package_id':1}):
            session['admin_id']=2
            request.view_args={'card_id':card};request.url_rule=types.SimpleNamespace(endpoint='edit_card_action')
            self.own['guard_owned_request']()

    def test_original_owned_package_remains_visible_after_catalog_restriction(self):
        self.make_own_batch()
        self.db.execute("UPDATE wisp_managers SET allowed_packages='[99]' WHERE id=2");self.db.commit()
        ctx=self.context()
        try:self.assertEqual(len(self.ns['get_vouchers']()),2)
        finally:ctx.pop()

    def test_cash_grant_to_owned_card_is_admin_only(self):
        self.app.add_url_rule('/api/actions/add-wallet','api_add_wallet',lambda:jsonify(success=True),methods=['POST'])
        self.make_own_batch()
        card=self.db.execute('SELECT id FROM wisp_vouchers WHERE reseller_id=2 LIMIT 1').fetchone()[0]
        self.assertEqual(self.post('/api/actions/add-wallet',json={'target_type':'voucher','target_ids':[card]}).status_code,403)

    def test_online_count_includes_owned_sessions_only(self):
        self.make_own_batch();self.load_dashboard()
        user=self.db.execute('SELECT username FROM wisp_vouchers WHERE reseller_id=2 LIMIT 1').fetchone()[0]
        stamp=datetime.datetime.now().isoformat(' ',timespec='seconds')
        self.db.execute('INSERT INTO radacct(username,acctstarttime,acctupdatetime,framedprotocol) VALUES(?,?,?,?)',(user,stamp,stamp,'PPP'))
        self.db.execute('INSERT INTO radacct(username,acctstarttime,acctupdatetime,framedprotocol) VALUES(?,?,?,?)',('fixture-2',stamp,stamp,'PPP'))
        self.db.commit()
        result=self.client.get('/api/dashboard/live-metrics')
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json['metrics']['active_sessions'],1)
        self.assertEqual(result.json['metrics']['voucher_summary']['online_cards'],1)
        ctx=self.context(3)
        try:self.assertEqual(self.own['owner_id'](),3)
        finally:ctx.pop()


if __name__=='__main__':unittest.main()

"""Administrative UI and direct-HTTP data exposure tests; no application startup."""
import ast, copy, json, unittest, sys, re, types, subprocess, tempfile, shutil
from pathlib import Path
from unittest.mock import patch
from flask import session, render_template, jsonify, request
from manager_page_fixture import fixture, load, ROOT
from visibility_fixture import install_fixture_visibility


class PermissionVisibility(unittest.TestCase):
    def setUp(self):
        self.app,self.db,self.ns,self.stubs,self.seed,_=fixture()
        self.policy=install_fixture_visibility(self.app,self.ns,enforce=True)
        self.app.context_processor(lambda: dict(has_permission=self.ns['has_permission'], settings={}, license_info={}))
        self.client=self.app.test_client()
        self.metrics=dict(total_subscribers=731234,online_subscribers=221234,online_subscriber_devices=251234,
            active_sessions=661234,total_unique_online=651234,total_nas=811234,
            vouchers=dict(total=941234),voucher_summary=dict(total_card_subscribers=941234,active_subscribers=931234,online_devices=921234,online_cards=911234,online_subscribers=911234,online=921234,expired_subscribers=901234),
            traffic_total_str='SECRET-TRAFFIC',traffic_in_str='SECRET-UP',traffic_out_str='SECRET-DOWN',
            resources=dict(cpu_cores=4,cpu_percent=5.0,ram_used_gb=1,ram_total_gb=8,ram_percent=12.0,disk_used_gb=5,disk_total_gb=80,disk_percent=10.0,containers={}),unknown='SECRET-UNKNOWN')
        self.calls=[]
        self.ns.update(filter_dashboard_metrics=self.policy['filter_dashboard_metrics'],can_view_network_totals=self.policy['can_view_network_totals'],
            get_dashboard_metrics=lambda:copy.deepcopy(self.metrics),
            get_traffic_chart_data=lambda:self.collect('traffic',dict(labels=['SECRET-TRAFFIC-CHART'],download=[1],upload=[2])),
            get_sales_chart_data=lambda:self.collect('sales',dict(labels=['SECRET-SALES'],sales=[5151234])),
            get_internet_ping=lambda:self.collect('ping',dict(online=True,latency=3)),
            get_system_uptime_str=lambda:self.collect('uptime','SECRET-UPTIME'),
            get_latest_system_operations=lambda **kw:self.collect('audit',[]),
            get_configured_timezone_name=lambda:'Asia/Aden',APP_VERSION_FULL='test',render_template=render_template,jsonify=jsonify)
        load('web/routes/dashboard.py',self.ns,['dashboard','api_dashboard_live_metrics'])
        self.app.view_functions['dashboard']=self.ns['dashboard']
        self.app.add_url_rule('/api/dashboard/live-metrics','api_dashboard_live_metrics',self.ns['api_dashboard_live_metrics'])
        # Endpoint aliases call a sentinel handler only if the shared guard authorizes them.
        for ep in ['sales_reports','export_sales','nas','api_nas_live_status','api_system_services_status','traffic_analytics_page','api_traffic_analytics_status','api_role_permissions','license_status_page']:
            if ep not in self.app.view_functions:self.app.add_url_rule('/probe/'+ep,ep,lambda:jsonify(data='SECRET-ENDPOINT'))
        self.login([])

    def tearDown(self):self.db.close()
    def collect(self,key,value):self.calls.append(key);return value
    def login(self,permissions):
        with self.client.session_transaction() as s:s.clear();s.update(admin_id=2,permissions=list(permissions),_license_csrf='fixture-token')
    def get(self,path):
        with patch.dict(sys.modules,self.stubs):return self.client.get(path)

    def test_no_permissions_dashboard_has_no_data_or_tabs(self):
        response=self.get('/dashboard');self.assertEqual(response.status_code,200)
        html=response.get_data(as_text=True)
        for text in ['731234','941234','811234','SECRET-TRAFFIC','SECRET-SALES','SECRET-UPTIME',"toggleMenu('vouchersMenu'","toggleMenu('subscribersMenu'","toggleMenu('managersMenu'","toggleMenu('nasMenu'","toggleMenu('toolsMenu'",'href="/managers"']:
            self.assertNotIn(text,html)
        self.assertEqual(self.calls,[])

    def test_no_permissions_live_api_omits_all_metrics(self):
        data=self.get('/api/dashboard/live-metrics').get_json();self.assertEqual(data['metrics'],{})
        self.assertEqual(data['latest_operations'],[]);self.assertIsNone(data['internet_status']);self.assertIsNone(data['system_uptime'])

    def test_vouchers_only_no_subscribers_sales_or_combined_totals(self):
        self.login(['vouchers.view']);response=self.get('/dashboard');self.assertEqual(response.status_code,200)
        html=response.get_data(as_text=True)
        self.assertIn('941234',html);self.assertIn("toggleMenu('vouchersMenu'",html)
        self.assertNotIn('<span>توليد كروت</span>',html)
        for value in ['731234','661234','SECRET-SALES','SECRET-TRAFFIC',"toggleMenu('subscribersMenu'"]:self.assertNotIn(value,html)
        data=self.get('/api/dashboard/live-metrics').get_json()['metrics'];self.assertEqual(set(data),{'vouchers','voucher_summary'})

    def test_subscribers_only_no_vouchers_or_combined_totals(self):
        self.login(['subscribers.view']);response=self.get('/dashboard');self.assertEqual(response.status_code,200)
        html=response.get_data(as_text=True);self.assertIn('731234',html)
        for value in ['941234','661234','SECRET-TRAFFIC',"toggleMenu('vouchersMenu'"]:self.assertNotIn(value,html)

    def test_finance_only_sales_not_inventory(self):
        self.login(['invoices.view']);response=self.get('/dashboard');self.assertEqual(response.status_code,200)
        html=response.get_data(as_text=True);self.assertIn('SECRET-SALES',html)
        for value in ['731234','941234','SECRET-TRAFFIC']:self.assertNotIn(value,html)
        self.assertEqual(self.calls,['sales'])

    def test_network_totals_require_both_populations(self):
        self.login(['subscribers.view','vouchers.view']);data=self.get('/api/dashboard/live-metrics').get_json()['metrics']
        self.assertEqual(data['active_sessions'],661234);self.assertEqual(data['traffic_total_str'],'SECRET-TRAFFIC')
        self.assertNotIn('resources',data);self.assertNotIn('total_nas',data)

    def test_services_only_not_customer_statistics(self):
        self.login(['system.view_services']);data=self.get('/api/dashboard/live-metrics').get_json()['metrics']
        self.assertEqual(set(data),{'resources'})

    def test_unknown_cache_fields_fail_closed_and_cache_not_mutated(self):
        original=copy.deepcopy(self.metrics)
        with self.app.test_request_context():
            session['permissions']=['subscribers.view','vouchers.view','nas.view','system.view_services']
            result=self.policy['filter_dashboard_metrics'](self.metrics)
        self.assertNotIn('unknown',result);self.assertEqual(original,self.metrics)

    def test_direct_sales_and_export_denied_without_finance(self):
        for ep in ['sales_reports','export_sales']:
            response=self.get('/probe/'+ep);self.assertEqual(response.status_code,403);self.assertNotIn('SECRET-ENDPOINT',response.get_data(as_text=True))
        self.login(['invoices.view']);self.assertEqual(self.get('/probe/sales_reports').status_code,200)

    def test_direct_nas_and_services_denied_without_permissions(self):
        for ep in ['nas','api_nas_live_status','api_system_services_status']:
            response=self.get('/probe/'+ep);self.assertEqual(response.status_code,403)
            self.assertNotIn('SECRET-ENDPOINT',response.get_data(as_text=True))

    def test_traffic_endpoint_requires_both_permissions(self):
        self.login(['subscribers.view']);self.assertEqual(self.get('/probe/api_traffic_analytics_status').status_code,403)
        self.login(['subscribers.view','vouchers.view']);self.assertEqual(self.get('/probe/api_traffic_analytics_status').status_code,200)

    def test_html_denial_is_friendly_and_api_denial_json(self):
        response=self.get('/probe/sales_reports');self.assertIn('text/html',response.content_type)
        self.assertIn('ليس لديك صلاحية',response.get_data(as_text=True))
        response=self.get('/api/dashboard/live-metrics') # harmless permitted shell
        self.assertTrue(response.is_json)

    def test_permission_changes_apply_without_new_login(self):
        self.assertEqual(self.get('/probe/sales_reports').status_code,403)
        with self.client.session_transaction() as s:s['permissions']=['invoices.view']
        self.assertEqual(self.get('/probe/sales_reports').status_code,200)
        with self.client.session_transaction() as s:s['permissions']=[]
        self.assertEqual(self.get('/probe/sales_reports').status_code,403)

    def test_manager_view_does_not_disclose_financial_columns_or_row_json(self):
        self.login(['managers.view']);response=self.get('/managers');self.assertEqual(response.status_code,200)
        html=re.sub(r'<script\b[\s\S]*?</script>', '', response.get_data(as_text=True))
        from html import unescape
        html=unescape(html)
        for value in ['رصيد المحفظة','الذمة المالية (الآجل)','إجمالي الإيداعات النقدية','"wallet_balance"','"net_outstanding_debt"','"total_cash_deposits"','"total_credit_deposits"','"total_debt_payments"','"invoices_count"']:
            self.assertFalse(value in html, 'Forbidden manager financial content: '+value)

    def test_statement_and_wallet_need_financial_permission(self):
        self.login(['managers.view'])
        for path in ['/managers/2/statement','/reseller-wallets','/managers/invoices']:self.assertEqual(self.get(path).status_code,403)
        self.login(['managers.billing'])
        for path in ['/managers/2/statement','/reseller-wallets','/managers/invoices']:self.assertEqual(self.get(path).status_code,200)

    def test_role_only_shows_role_tab_not_manager_accounts(self):
        self.login(['roles.manage']);response=self.get('/dashboard');self.assertEqual(response.status_code,200)
        html=response.get_data(as_text=True);self.assertIn('href="/roles"',html)
        self.assertNotIn('href="/managers"',html);self.assertNotIn('href="/reseller-wallets"',html)

    def test_account_money_redaction_preserves_identity(self):
        with self.app.test_request_context():
            session['permissions']=[]
            self.assertEqual(self.policy['filter_account_money'](dict(id=2,balance=99,balance_formatted='99',wallet_balance=8)),dict(id=2))
            session['permissions']=['wallets.manage'];self.assertEqual(self.policy['filter_account_money'](dict(balance=99)),dict(balance=99))

    def test_card_view_hides_invoice_and_audit_contents(self):
        self.login(['vouchers.view']);response=self.get('/vouchers/active-users/1');self.assertEqual(response.status_code,200)
        html=response.get_data(as_text=True)
        self.assertNotIn('id="tab-pane-invoices"',html);self.assertNotIn('id="tab-btn-invoices"',html)
        self.assertNotIn('id="tab-pane-audit"',html)

    def test_nas_view_does_not_expose_management_credentials(self):
        with self.app.test_request_context():
            session['permissions']=['nas.view']
            self.assertEqual(self.policy['filter_nas_credentials'](dict(id=1,secret='SECRET',api_password='SECRET',api_username='admin')),dict(id=1))

    def nas_fixture(self):
        self.device=dict(id=1,name='Fixture NAS',ip_address='10.0.0.1',nas_type='mikrotik',is_online=True,latency_ms=3,
                         active_users=871234,secret='SECRET-RADIUS',api_password='SECRET-API',api_username='PRIVATE-USER',coa_port=3799,status='active')
        self.status=dict(self.device,uptime='1h',ros_version='7.test',board_name='fixture',cpu_load=3,total_memory_mb=256,free_memory_mb=128,ram_usage_pct=50)
        self.device.update(self.status)
        original_query=self.ns['query_one']
        self.ns['query_one']=lambda sql,args=():dict(self.device) if 'wisp_nas_devices' in sql else original_query(sql,args)
        self.ns['get_nas_devices']=lambda **kw:[dict(self.device)]
        module=types.ModuleType('core.mikrotik_api');module.fetch_single_nas_status=lambda d:dict(self.status)
        self.stubs['core.mikrotik_api']=module
        service=types.ModuleType('services.nas_service');service.get_nas_devices=self.ns['get_nas_devices']
        self.stubs['services.nas_service']=service
        load('web/routes/nas.py',self.ns,['nas','nas_details_page','api_nas_status_compat','api_nas_live_status'])
        self.app.view_functions['nas']=self.ns['nas']
        self.app.add_url_rule('/fixture/nas/1','nas_details_page',lambda:self.ns['nas_details_page'](1))
        self.app.add_url_rule('/api/fixture/nas','api_nas_status_compat',self.ns['api_nas_status_compat'])

    def test_nas_actual_template_and_api_hide_counts_and_credentials(self):
        self.nas_fixture();self.login(['nas.view'])
        for path in ['/probe/nas','/fixture/nas/1']:
            response=self.get(path);self.assertEqual(response.status_code,200)
            html=response.get_data(as_text=True)
            for forbidden in ['SECRET-RADIUS','SECRET-API','PRIVATE-USER','871234','id="kpiActiveUsers"','id="detailActiveUsers"']:
                self.assertFalse(forbidden in html,'NAS data exposed: '+forbidden)
        data=self.get('/api/fixture/nas').get_json();self.assertNotIn('total_active_users',data)
        self.assertNotIn('active_users',data['devices'][0]);self.assertNotIn('secret',data['devices'][0])

    def test_dashboard_rendered_javascript_with_restricted_permissions_parses(self):
        node=shutil.which('node');self.assertTrue(node,'Node required for frontend verification')
        for permissions in [[],['vouchers.view'],['invoices.view'],['system.view_services']]:
            self.login(permissions);response=self.get('/dashboard');self.assertEqual(response.status_code,200)
            scripts=re.findall(r'<script(?:\s[^>]*)?>([\s\S]*?)</script>',response.get_data(as_text=True))
            with tempfile.TemporaryDirectory() as folder:
                for index,script in enumerate(scripts):
                    path=Path(folder)/f'script-{index}.js';path.write_text(script,encoding='utf8')
                    result=subprocess.run([node,'--check',str(path)],capture_output=True,text=True)
                    self.assertEqual(result.returncode,0,result.stderr[:500])

    def test_all_administrative_read_routes_have_policy(self):
        excluded={'api_usage_history','api_whatsapp_webhook'} # target-type guard / integration callback
        for path in (ROOT/'web/routes').glob('*.py'):
            if path.stem in ('__init__','auth','portal'):continue
            for node in ast.parse(path.read_text(encoding='utf8')).body:
                if not isinstance(node,ast.FunctionDef):continue
                for d in node.decorator_list:
                    if isinstance(d,ast.Call) and isinstance(d.func,ast.Attribute) and d.func.attr=='route':
                        kw={k.arg:ast.literal_eval(k.value) for k in d.keywords}
                        ep=kw.get('endpoint',node.name)
                        if 'GET' in kw.get('methods',['GET']) and ep not in excluded:self.assertIn(ep,self.policy['ENDPOINT_PERMISSIONS'],ep)

    def test_unknown_administrative_route_fails_closed(self):
        from flask import Blueprint
        bp=Blueprint('system_bp',__name__)
        bp.add_url_rule('/fixture/new-admin-route','unmapped_future_route',lambda:jsonify(data='SECRET-ENDPOINT'))
        self.app.register_blueprint(bp)
        response=self.get('/fixture/new-admin-route');self.assertEqual(response.status_code,403)
        self.assertNotIn('SECRET-ENDPOINT',response.get_data(as_text=True))

    def test_existing_settings_permission_names_remain_compatible(self):
        original=self.ns.get('get_manager_permissions')
        load('core/rbac.py',self.ns,['has_permission'])
        try:
            self.ns['get_manager_permissions']=lambda **kw:{'system.settings'}
            with self.app.test_request_context():
                manager=dict(id=2,role_id=2,role_code='restricted')
                for code in ['settings_manage','settings.manage','system.settings']:
                    self.assertTrue(self.ns['has_permission'](code,manager))
        finally:
            if original is None:self.ns.pop('get_manager_permissions',None)
            else:self.ns['get_manager_permissions']=original


if __name__=='__main__':unittest.main()

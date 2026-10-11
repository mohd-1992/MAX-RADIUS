"""Package HTTP/SQL regression tests without application workers or live data."""
import ast
import importlib.util
import logging
import sqlite3
import types
import sys
import unittest
from contextlib import contextmanager
from collections.abc import Mapping
from functools import wraps
from pathlib import Path
from unittest.mock import patch

from flask import Flask, request, jsonify, redirect, url_for, flash, session, render_template

ROOT = Path(__file__).resolve().parents[1]


def load_functions(path, names, namespace):
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    for node in nodes:
        node.decorator_list = []
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)


def fixture():
    db = sqlite3.connect(':memory:', check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.create_function('GREATEST', 2, max)
    db.executescript('''
        CREATE TABLE wisp_packages(id INTEGER PRIMARY KEY, name TEXT UNIQUE, service_type TEXT,
            price REAL,cost REAL,rate_download TEXT,rate_upload TEXT,burst_download TEXT,burst_upload TEXT,
            burst_threshold_down TEXT,burst_threshold_up TEXT,burst_time INTEGER,priority INTEGER,
            min_download TEXT,min_upload TEXT,volume_quota_mb INTEGER,uptime_limit_mins INTEGER,
            validity_days INTEGER,validity_value INTEGER,validity_unit TEXT,mikrotik_group TEXT,
            simultaneous_sessions INTEGER,is_active INTEGER,show_in_portal INTEGER,is_rollover_enabled INTEGER,
            is_loyalty_enabled INTEGER,loyalty_points INTEGER,description TEXT);
        CREATE TABLE wisp_subscribers(id INTEGER PRIMARY KEY,package_id INTEGER,status TEXT);
        CREATE TABLE wisp_vouchers(id INTEGER PRIMARY KEY,package_id INTEGER,status TEXT);
        CREATE TABLE wisp_voucher_batches(id INTEGER PRIMARY KEY,package_id INTEGER);
        CREATE TABLE radgroupreply(groupname TEXT,attribute TEXT,op TEXT,value TEXT);
        CREATE TABLE radgroupcheck(groupname TEXT,attribute TEXT,op TEXT,value TEXT);
        CREATE TABLE radusergroup(groupname TEXT);
        INSERT INTO wisp_subscribers VALUES(1,1,'inactive');
        INSERT INTO wisp_vouchers VALUES(1,1,'expired');
    ''')
    def write(sql, params=()):
        cursor = db.execute(sql, params)
        db.commit()
        return cursor.lastrowid
    def all_rows(sql, params=()):
        return [dict(row) for row in db.execute(sql, params).fetchall()]
    def one(sql, params=()):
        rows = all_rows(sql, params)
        return rows[0] if rows else None
    @contextmanager
    def transaction():
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
    namespace = dict(request=request, jsonify=jsonify, redirect=redirect, url_for=url_for,
                     flash=flash, render_template=render_template, query_all=all_rows, query_one=one,
                     execute_write=write, db_session=transaction, adapt_query=lambda sql, conn: sql,
                     sync_voucher_activations=lambda: None, Mapping=Mapping, logger=logging.getLogger('package-tests'))
    load_functions('services/package_validation.py', [], namespace)
    exec((ROOT / 'services/package_validation.py').read_text(encoding='utf-8'), namespace)
    load_functions('core/rate_limit.py', ['is_zero_or_empty_rate', 'parse_rate_to_bps', 'build_mikrotik_rate_limit'], namespace)
    load_functions('core/radius_sync.py', ['_write', '_one', 'sync_package_to_radius'], namespace)
    namespace.update(wraps=wraps, get_current_manager=lambda: session.get('manager'),
                     has_permission=lambda code, manager=None: code in session.get('permissions', []))
    load_functions('core/rbac.py', ['require_permission'], namespace)
    def lifecycle_sql(conn, statement, params=(), fetch=None):
        cursor = conn.execute(statement.replace(' FOR UPDATE', ''), params)
        if fetch == 'one':
            row = cursor.fetchone()
            return dict(row) if row else None
        return cursor.rowcount
    def lifecycle_transaction(operation):
        with transaction() as connection:
            return operation(connection)
    lifecycle_namespace = dict(sql=lifecycle_sql, run_transaction=lifecycle_transaction)
    load_functions('services/account_lifecycle_service.py', ['delete_packages'], lifecycle_namespace)
    namespace['delete_packages_fixture'] = lifecycle_namespace['delete_packages']
    routes = ['package_write', 'guard_package_permissions', 'packages', 'new_package_page',
              'add_package_action', 'package_details', 'edit_package_action', 'clone_package_action',
              'packages_bulk_action', 'delete_package_action']
    load_functions('web/routes/packages.py', routes, namespace)
    app = Flask('package-isolated-test', template_folder=str(ROOT / 'web/templates'), static_folder=str(ROOT / 'web/static'))
    app.secret_key = 'isolated-test-only'
    for path, endpoint, methods in [('/packages', 'packages', ['GET']),
            ('/packages/add', 'new_package_page', ['GET']), ('/packages/add', 'add_package_action', ['POST']),
            ('/packages/<int:pkg_id>', 'package_details', ['GET']),
            ('/packages/edit/<int:pkg_id>', 'edit_package_action', ['POST']),
            ('/packages/clone/<int:pkg_id>', 'clone_package_action', ['POST']),
            ('/packages/delete/<int:pkg_id>', 'delete_package_action', ['POST']),
            ('/packages/bulk-action', 'packages_bulk_action', ['POST'])]:
        app.add_url_rule(path, endpoint, namespace[endpoint], methods=methods)
    app.before_request(lambda: namespace['guard_package_permissions']() if request.path.startswith('/packages') else None)
    spec = importlib.util.spec_from_file_location('package_test_csrf', ROOT / 'core/license_security.py')
    csrf_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(csrf_module)
    csrf_module.install_csrf(app, lambda req: req.path.startswith('/packages'))
    @app.context_processor
    def template_context():
        return dict(currency_symbol='ر.ي', has_permission=namespace['has_permission'], has_feature=lambda *args: True, current_manager={},
                    settings={}, license_info={}, license_status={}, app_version='test', network_name='اختبار الباقات',
                    server_timestamp_ms=0, server_time_now='', server_date_only='', server_time_only='')
    # Keep real templates and scripts; unrelated navigation is inert in this isolated app.
    original_url_for = app.jinja_env.globals['url_for']
    app.jinja_env.globals['url_for'] = lambda endpoint, **kw: original_url_for(endpoint, **kw) if endpoint in app.view_functions else '#'
    app.jinja_env.filters['format_speed_pair'] = lambda value: ' / '.join(str(x) for x in value)
    app.jinja_env.filters['format_mb'] = str
    for ident, kind, name in [(1, 'hotspot', "باقة ' خاصة <script>"), (2, 'both', 'باقة مشتركة'), (3, 'pppoe', 'باقة منزلية')]:
        write('''INSERT INTO wisp_packages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (ident, name, kind, 20, 10, '4M', '2M', '8M', '4M', '', '', 16, 8,
               '1M', '512k', 1024, 60, 0, 0, 'days', 'test', 2, 1, 1, 0, 1, 7, 'وصف'))
        namespace['sync_package_to_radius'](ident)
    return app, db, namespace


class PackagePageTests(unittest.TestCase):
    def setUp(self):
        self.app, self.db, self.ns = fixture()
        self.client = self.app.test_client()
        with self.client.session_transaction() as state:
            state['manager'] = {'id': 2, 'role_id': 2}
            state['permissions'] = ['packages.view', 'packages.manage']
            state['_license_csrf'] = 'test-token'

    def tearDown(self):
        self.db.close()

    def post(self, payload):
        return self.client.post('/packages/bulk-action', json=payload, headers={'X-CSRF-Token': 'test-token'})

    def test_bulk_flags_selected_only_preserve_contracts_and_radius(self):
        radius = list(self.db.execute('SELECT * FROM radgroupreply'))
        for action, column in [('update_rollover', 'is_rollover_enabled'), ('update_portal_visibility', 'show_in_portal')]:
            for enabled in (1, 0):
                result = self.post(dict(action=action, package_ids=[1, 2, 2], enabled=enabled))
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json['count'], 2)
                self.assertEqual([row[0] for row in self.db.execute(f'SELECT {column} FROM wisp_packages WHERE id IN (1,2)')], [enabled, enabled])
        for enabled, points in [(1, 25), (1, 0), (0, 0)]:
            self.assertEqual(self.post(dict(action='update_loyalty', package_ids=[1,2], enabled=enabled, loyalty_points=points)).status_code, 200)
            self.assertEqual(tuple(self.db.execute('SELECT is_loyalty_enabled,loyalty_points FROM wisp_packages WHERE id=1').fetchone()), (enabled, points))
        self.assertEqual(self.db.execute('SELECT loyalty_points FROM wisp_packages WHERE id=3').fetchone()[0], 7)
        self.assertEqual(list(self.db.execute('SELECT * FROM radgroupreply')), radius)
        self.assertEqual(self.db.execute('SELECT uptime_limit_mins FROM wisp_packages WHERE id=1').fetchone()[0], 60)

    def test_invalid_bulk_never_writes(self):
        cases = [dict(action='update_loyalty', enabled=1, loyalty_points=-1),
            dict(action='update_loyalty', enabled=1, loyalty_points=1.5),
            dict(action='update_rollover', enabled=2), dict(action='update_portal_visibility', enabled='bad'),
            dict(action='update_price', price_value=-1), dict(action='update_price', price_value='NaN'),
            dict(action='update_price', price_value='Infinity'), dict(action='update_price', price_mode='bad'),
            dict(action='update_price', price_mode='discount_percent', price_value=101),
            dict(action='update_validity', validity_value=-1), dict(action='update_validity', validity_unit='bad'),
            dict(action='update_service_type', service_type='bad'), dict(action='unknown')]
        before = list(self.db.execute('SELECT * FROM wisp_packages'))
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertEqual(self.post(dict(package_ids=[1,2], **payload)).status_code, 400)
                self.assertEqual(list(self.db.execute('SELECT * FROM wisp_packages')), before)
        self.assertEqual(self.post(dict(action='activate', package_ids=[1,999])).status_code, 409)
        self.assertEqual(self.post(dict(action='activate', package_ids=1)).status_code, 400)

    def test_existing_bulk_actions(self):
        for action in ('activate', 'deactivate'):
            self.assertEqual(self.post(dict(action=action, package_ids=[1,2])).status_code, 200)
        for mode, value, expected in [('fixed', 10.25, 10.25), ('increase_percent', 100, 20.5),
                                      ('discount_percent', 50, 10.25), ('increase_amount', 2, 12.25), ('discount_amount', 99, 0)]:
            self.assertEqual(self.post(dict(action='update_price', package_ids=[1], price_mode=mode, price_value=value)).status_code, 200)
            self.assertEqual(self.db.execute('SELECT price FROM wisp_packages WHERE id=1').fetchone()[0], expected)
        for unit, days in [('days',2), ('hours',1), ('minutes',1), ('months',60)]:
            self.assertEqual(self.post(dict(action='update_validity', package_ids=[1], validity_value=2, validity_unit=unit)).status_code, 200)
            self.assertEqual(self.db.execute('SELECT validity_days FROM wisp_packages WHERE id=1').fetchone()[0], days)
        for kind in ('hotspot', 'pppoe', 'both'):
            self.assertEqual(self.post(dict(action='update_service_type', package_ids=[1], service_type=kind)).status_code, 200)
        for alias in ('percentage_increase', 'percentage_discount', 'amount_increase', 'amount_discount'):
            self.assertEqual(self.post(dict(action='update_price', package_ids=[1], price_type=alias, price_value=10)).status_code, 200)

    def test_permissions_and_csrf(self):
        with self.client.session_transaction() as state:
            state['permissions'] = ['packages.view']
        self.assertEqual(self.post(dict(action='activate', package_ids=[1])).status_code, 403)
        with self.client.session_transaction() as state:
            state['permissions'] = ['packages.manage']
        self.assertEqual(self.client.post('/packages/bulk-action', json=dict(action='activate', package_ids=[1])).status_code, 403)
        with self.client.session_transaction() as state:
            state.pop('manager')
        self.assertEqual(self.post(dict(action='activate', package_ids=[1])).status_code, 401)

    def form(self):
        return dict(name='edited', service_type='both', price='12.25', cost='2.01', rate_download='4M',
                    rate_upload='2M', burst_download='8M', burst_upload='4M', volume_quota_mb='2048',
                    validity_value='0', validity_unit='days', simultaneous_sessions='3',
                    is_active='1', show_in_portal='1', is_rollover_enabled='1', is_loyalty_enabled='1',
                    loyalty_points='25', csrf_token='test-token')

    def test_edit_preserves_imported_limits_and_syncs_radius(self):
        self.assertEqual(self.client.post('/packages/edit/1', data=self.form()).status_code, 302)
        row = self.ns['query_one']('SELECT * FROM wisp_packages WHERE id=1')
        self.assertEqual((row['uptime_limit_mins'], row['min_download'], row['min_upload'], row['validity_value']), (60, '1M', '512k', 0))
        self.assertEqual(row['price'], 12.25)
        self.assertEqual(row['loyalty_points'], 25)
        rate = self.ns['query_one']("SELECT value FROM radgroupreply WHERE groupname='edited' AND attribute='MikroTik-Rate-Limit'")
        self.assertEqual(rate['value'], '2M/4M 4M/8M 2M/4M 16/16 8 512k/1M')

    def test_edit_sync_failure_rolls_back_all_changes(self):
        before = list(self.db.execute('SELECT * FROM wisp_packages'))
        radius = list(self.db.execute('SELECT * FROM radgroupreply'))
        with patch.dict(self.ns, sync_package_to_radius=lambda *args, **kw: (_ for _ in ()).throw(RuntimeError('sync failed'))):
            with self.assertLogs('package-tests', level='ERROR'):
                self.client.post('/packages/edit/1', data=self.form())
        self.assertEqual(list(self.db.execute('SELECT * FROM wisp_packages')), before)
        self.assertEqual(list(self.db.execute('SELECT * FROM radgroupreply')), radius)

    def test_add_and_clone(self):
        self.assertEqual(self.client.post('/packages/add', data=self.form()).status_code, 302)
        result = self.client.post('/packages/clone/1', json={}, headers={'X-CSRF-Token':'test-token'})
        self.assertEqual(result.status_code, 200)
        copied = self.ns['query_one']('SELECT * FROM wisp_packages WHERE id=?', (result.json['new_id'],))
        self.assertEqual((copied['uptime_limit_mins'], copied['loyalty_points'], copied['min_download']), (60,7,'1M'))
        self.assertEqual(copied['description'], 'وصف')

    def test_delete_linked_rejected_unlinked_atomic(self):
        module = types.ModuleType('services.account_lifecycle_service')
        module.delete_packages = self.ns['delete_packages_fixture']
        with patch.dict(sys.modules, {'services.account_lifecycle_service': module}):
            self.assertEqual(self.post(dict(action='delete', package_ids=[1,2])).status_code, 400)
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM wisp_packages').fetchone()[0], 3)
            self.db.execute('INSERT INTO wisp_voucher_batches VALUES(1,2)')
            self.db.commit()
            self.assertEqual(self.post(dict(action='delete', package_ids=[2])).status_code, 400)
            response = self.post(dict(action='delete', package_ids=[3,3]))
            self.assertEqual((response.status_code, response.json['count']), (200,1))
            self.assertFalse(self.ns['query_one']('SELECT * FROM radgroupreply WHERE groupname=?', ('باقة منزلية',)))

    def test_all_form_fields_and_radius_saved(self):
        data = dict(self.form(), burst_threshold_down='3M', burst_threshold_up='1M',
                    burst_time='30', priority='2', min_download='1M', min_upload='512k',
                    validity_value='4', validity_unit='months', description='اختبار الوصف', mikrotik_group='profile')
        self.client.post('/packages/edit/1', data=data)
        row = self.ns['query_one']('SELECT * FROM wisp_packages WHERE id=1')
        for field in ('name','service_type','rate_download','rate_upload','burst_download','burst_upload',
                      'burst_threshold_down','burst_threshold_up','min_download','min_upload','description','mikrotik_group'):
            self.assertEqual(row[field], data[field], field)
        for field in ('burst_time','priority','volume_quota_mb','validity_value','simultaneous_sessions',
                      'is_active','show_in_portal','is_rollover_enabled','is_loyalty_enabled','loyalty_points'):
            self.assertEqual(row[field], int(data[field]), field)
        self.assertEqual(row['validity_days'], 120)
        self.assertEqual(self.ns['query_one']("SELECT value FROM radgroupreply WHERE groupname='edited' AND attribute='MikroTik-Rate-Limit'")['value'], '2M/4M 4M/8M 1M/3M 30/30 2 512k/1M')

    def test_zero_validity_wins_over_legacy_days(self):
        self.client.post('/packages/edit/1', data=dict(self.form(), validity_days='30'))
        self.assertEqual(self.db.execute('SELECT validity_value,validity_days FROM wisp_packages WHERE id=1').fetchone()[:], (0,0))

    def test_add_and_clone_sync_failure_leave_no_partial_package(self):
        before = list(self.db.execute('SELECT * FROM wisp_packages'))
        with patch.dict(self.ns, sync_package_to_radius=lambda *args, **kw: (_ for _ in ()).throw(RuntimeError('test sync failure'))):
            with self.assertLogs('package-tests', level='ERROR'):
                self.client.post('/packages/add', data=self.form())
            with self.assertLogs('package-tests', level='ERROR'):
                self.client.post('/packages/clone/1', json={}, headers={'X-CSRF-Token':'test-token'})
        self.assertEqual(list(self.db.execute('SELECT * FROM wisp_packages')), before)

    def test_form_invalid_does_not_write(self):
        for field, value in [('name',' '), ('price','-1'), ('cost','NaN'), ('priority','9'),
                            ('simultaneous_sessions','0'), ('volume_quota_mb','-1'), ('rate_upload','nonsense'),
                            ('validity_unit','bad'), ('loyalty_points','1.5'), ('burst_time','121'),
                            ('burst_download','2M'), ('min_download','9M'), ('rate_upload','0')]:
            before = list(self.db.execute('SELECT * FROM wisp_packages'))
            self.client.post('/packages/edit/1', data=dict(self.form(), **{field:value}))
            self.assertEqual(list(self.db.execute('SELECT * FROM wisp_packages')), before)

    def test_templates_render_real_base_and_escape_names(self):
        for path in ('/packages', '/packages/add', '/packages/1'):
            result = self.client.get(path)
            self.assertEqual(result.status_code, 200, result.data[:500])
        html = self.client.get('/packages').data.decode()
        self.assertIn('ترحيل الرصيد', html)
        self.assertNotIn("clonePackage('1', 'باقة", html)
        self.assertIn('إجمالي حسابات المشتركين والكروت', html)
        details = self.client.get('/packages/1').data.decode()
        self.assertIn('1</h4>', details)
        self.assertIn('512k/1M', details)
        filtered = self.client.get('/packages?service_type=hotspot').data.decode()
        self.assertIn('باقة مشتركة', filtered)
        self.assertNotIn('باقة منزلية', filtered)

    def test_time_limit_edit_and_zero_rates(self):
        self.client.post('/packages/edit/1', data=dict(self.form(), uptime_limit_mins='120', rate_upload='0m', rate_download='0'))
        row = self.ns['query_one']('SELECT * FROM wisp_packages WHERE id=1')
        self.assertEqual((row['uptime_limit_mins'], row['rate_upload']), (120,'0'))
        self.assertIsNone(self.ns['query_one']("SELECT value FROM radgroupreply WHERE groupname='edited' AND attribute='MikroTik-Rate-Limit'"))
        self.client.post('/packages/edit/1', data=dict(self.form(), uptime_limit_mins='0'))
        self.assertEqual(self.db.execute('SELECT uptime_limit_mins FROM wisp_packages WHERE id=1').fetchone()[0], 0)


    def test_minimum_and_priority_without_burst(self):
        cases = [
            ({'priority': 8}, '2M/4M'),
            ({'priority': 3}, '2M/4M 0/0 0/0 0/0 3'),
            ({'priority': 8, 'min_up': '512k', 'min_down': '1M'}, '2M/4M 0/0 0/0 0/0 8 512k/1M'),
            ({'priority': 2, 'min_down': '1M'}, '2M/4M 0/0 0/0 0/0 2 2M/1M'),
            ({'priority': 2, 'min_up': '512k'}, '2M/4M 0/0 0/0 0/0 2 512k/4M'),
        ]
        for kwargs, expected in cases:
            self.assertEqual(self.ns['build_mikrotik_rate_limit']('4M', '2M', **kwargs), expected)
        data = dict(self.form(), burst_download='', burst_upload='', min_download='1M', min_upload='512k', priority='3')
        self.client.post('/packages/edit/1', data=data)
        self.assertEqual(self.ns['query_one']("SELECT value FROM radgroupreply WHERE groupname='edited' AND attribute='MikroTik-Rate-Limit'")['value'], '2M/4M 0/0 0/0 0/0 3 512k/1M')
        html = self.client.get('/packages/1').data.decode()
        self.assertIn('فترة تحديث المحاسبة', html)
        self.assertIn('حد الجلسات لدى خادم المصادقة', html)
        self.assertLess(html.index('name="min_download"'), html.index('<!-- Section 3: Quota'))


    def test_all_burst_fields_blank_save_base_rate(self):
        data = dict(self.form(), burst_download='', burst_upload='', burst_threshold_down='', burst_threshold_up='', burst_time='', priority='', min_download='', min_upload='')
        response = self.client.post('/packages/edit/1', data=data)
        self.assertEqual(response.status_code, 302)
        row = self.ns['query_one']('SELECT burst_time,priority FROM wisp_packages WHERE id=1')
        self.assertEqual((row['burst_time'],row['priority']), (16,8))
        self.assertEqual(self.ns['query_one']("SELECT value FROM radgroupreply WHERE groupname='edited' AND attribute='MikroTik-Rate-Limit'")['value'], '2M/4M')


if __name__ == '__main__':
    unittest.main()

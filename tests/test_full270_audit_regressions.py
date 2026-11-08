"""Local audit regressions: no real router, worker, issuer, or customer database."""
import ast
import copy
import importlib.util
from contextlib import contextmanager
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch
from flask import Flask

ROOT = Path(__file__).resolve().parents[1]


def functions(path, names, ns):
    tree = ast.parse((ROOT / path).read_text(encoding='utf8'))
    nodes = [copy.deepcopy(n) for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    for node in nodes:
        node.decorator_list = []
    exec(compile(ast.Module(body=nodes, type_ignores=[]), path, 'exec'), ns)


class AuditRegressions(unittest.TestCase):
    def test_native_capacity_target_guard_precedes_schema_work(self):
        tree = ast.parse((ROOT / 'tests/test_license_capacity.py').read_text(encoding='utf8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'CapacityTests')
        setup = copy.deepcopy(next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'setUpClass'))
        setup.decorator_list = []
        ns = {'connect': Mock(side_effect=RuntimeError('disposable target required')),
              'heal_database_schema': Mock()}
        exec(compile(ast.Module(body=[setup], type_ignores=[]), 'capacity-fixture-guard', 'exec'), ns)
        with self.assertRaisesRegex(RuntimeError, 'disposable target required'):
            ns['setUpClass'](None)
        ns['heal_database_schema'].assert_not_called()

    def test_sensitive_diagnostic_and_automation_posts_require_csrf(self):
        spec = importlib.util.spec_from_file_location('audit_csrf', ROOT / 'core/license_security.py')
        security = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(security)
        app = Flask('audit-csrf')
        app.secret_key = 'isolated-test-session-key-only'
        security.install_csrf(app, security.protects_admin_account_request)
        paths = ['/api/tools/nas-diagnostics/test-coa-port',
                 '/api/tools/nas-diagnostics/send-coa-disconnect',
                 '/api/automation-rules/run', '/api/automation-rules/toggle']
        calls = []
        for i, path in enumerate(paths):
            app.add_url_rule(path, 'probe' + str(i), lambda: (calls.append(True) or 'ok'), methods=['POST'])
        client = app.test_client()
        with client.session_transaction() as sess:
            sess['_license_csrf'] = 'expected-token'
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(client.post(path).status_code, 403)
                self.assertEqual(client.post(path, headers={'X-CSRF-Token': 'wrong'}).status_code, 403)
                self.assertEqual(client.post(path, headers={'X-CSRF-Token': 'expected-token'}).status_code, 200)
        self.assertEqual(len(calls), 4)

    def test_unsafe_methods_and_unicode_invalid_tokens_fail_closed(self):
        spec = importlib.util.spec_from_file_location('audit_csrf_methods', ROOT / 'core/license_security.py')
        security = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(security)
        app = Flask('audit-unsafe-methods')
        app.secret_key = 'isolated-test-session-key-only'
        security.install_csrf(app, security.protects_admin_account_request)
        app.add_url_rule('/api/future-operation', 'write', lambda: 'ok', methods=['POST', 'PUT', 'PATCH', 'DELETE'])
        client = app.test_client()
        with client.session_transaction() as sess:
            sess['_license_csrf'] = 'expected-token'
        for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            with self.subTest(method=method):
                self.assertEqual(client.open('/api/future-operation', method=method).status_code, 403)
                self.assertEqual(client.open('/api/future-operation', method=method,
                    data={'csrf_token': 'رمز غير صالح'}).status_code, 403)
                self.assertEqual(client.open('/api/future-operation', method=method,
                    headers={'X-CSRF-Token': 'expected-token'}).status_code, 200)

    def test_new_admin_routes_inherit_protection_and_public_contracts_stay_separate(self):
        spec = importlib.util.spec_from_file_location('audit_csrf_defaults', ROOT / 'core/license_security.py')
        security = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(security)
        for path in ('/api/backups/create', '/api/system-services/action', '/api/future-operation', '/settings/update'):
            self.assertTrue(security.protects_admin_account_request(types.SimpleNamespace(path=path)))
        for path in ('/login', '/user/login', '/user/api/change-speed', '/api/whatsapp/webhook'):
            self.assertFalse(security.protects_admin_account_request(types.SimpleNamespace(path=path)))

    def diagnostic(self):
        client = Mock()
        client.disconnect_user.return_value = {'success': True, 'message': 'fixture'}
        ns = {'query_one': Mock(return_value=None), 'RadiusCoaClient': Mock(return_value=client)}
        functions('services/nas_diagnostics_service.py', {'test_coa_port', 'send_test_coa_disconnect'}, ns)
        return ns, client

    def test_legacy_diagnostic_route_uses_reserved_probe_identity(self):
        ns, client = self.diagnostic()
        self.assertTrue(ns['send_test_coa_disconnect']('192.0.2.1', 'fixture-only', '3799')[0])
        client.disconnect_user.assert_called_once_with(username='_radius_probe_test_user_', is_test_probe=True)
        self.assertEqual(ns['RadiusCoaClient'].call_args.kwargs['port'], 3799)

    def test_invalid_diagnostic_input_never_sends_a_packet(self):
        ns, client = self.diagnostic()
        for ip, port in [('', 3799), ('192.0.2.1', 'bad'), ('192.0.2.1', -1), ('192.0.2.1', 65536),
                         ('192.0.2.1', float('inf')), ('192.0.2.1', float('nan'))]:
            with self.subTest(ip=ip, port=port):
                self.assertFalse(ns['send_test_coa_disconnect'](ip, coa_port=port)[0])
        client.disconnect_user.assert_not_called()
        ns['RadiusCoaClient'].assert_not_called()

    def automation(self):
        ns = {'ensure_automation_tables': Mock(), '_get_db': Mock()}
        functions('services/automation_rules_service.py', {'toggle_rule', 'execute_rule_now'}, ns)
        return ns

    def test_invalid_automation_rule_never_opens_database_or_executes(self):
        ns = self.automation()
        for ident in (0, -1, None, 'bad'):
            with self.subTest(ident=ident):
                self.assertFalse(ns['execute_rule_now'](ident)[0])
                self.assertFalse(ns['toggle_rule'](ident, 1)[0])
        ns['ensure_automation_tables'].assert_not_called()
        ns['_get_db'].assert_not_called()

    def test_direct_automation_execution_initializes_schema_without_page_visit(self):
        ns = self.automation()
        factory = types.ModuleType('services.factory_reset_service')
        factory.factory_reset_active = lambda: False
        lifecycle = types.ModuleType('services.account_lifecycle_service')
        @contextmanager
        def lock(*a):
            yield True
        lifecycle.job_lock = lock
        database = types.ModuleType('database.db')
        database.query_one = Mock(return_value=None)
        database.execute_write = Mock()
        with patch.dict(sys.modules, {'services.factory_reset_service': factory,
                                     'services.account_lifecycle_service': lifecycle, 'database.db': database}):
            self.assertFalse(ns['execute_rule_now'](987654321)[0])
        ns['ensure_automation_tables'].assert_called_once_with()
        database.execute_write.assert_not_called()


if __name__ == '__main__':
    unittest.main()

"""Focused transaction contracts; MariaDB/RADIUS acceptance is recorded separately."""
import ast
import datetime
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


SOURCE = Path(__file__).resolve().parents[1] / 'services' / 'license_guard_service.py'


class Cursor:
    lastrowid = 7

    def __init__(self, current, fail_runtime=False):
        self.rows = iter([{'id': 1}, current])
        self.statements = []
        self.fail_runtime = fail_runtime

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, args=()):
        self.statements.append((sql, args))
        if self.fail_runtime and sql.startswith('INSERT INTO wisp_license_runtime_state'):
            raise RuntimeError('injected runtime write failure')

    def fetchone(self):
        return next(self.rows)


class AtomicLicenseTests(unittest.TestCase):
    def setUp(self):
        names = {'_lease_values', '_write_runtime_lease', '_persist_verified_package',
                 'publish_radius_license_state'}
        tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
        selected = ast.Module(body=[n for n in tree.body
                                   if isinstance(n, ast.FunctionDef) and n.name in names], type_ignores=[])
        self.info = dict(license_id='local-atomic-test', license_mode='active_sessions',
                         max_active_sessions=20, is_lifetime=True)
        self.package = dict(payload=dict(license_id='local-atomic-test', is_lifetime=True,
                                        limits={'max_active_sessions': 20}),
                            signature='test-only', enrollment_token='must-not-be-stored')
        self.cursor = Cursor({'id': 7})
        self.connection = Mock()
        self.connection.cursor.return_value = self.cursor
        self.ns = dict(datetime=datetime, json=json, get_connection=lambda: self.connection,
                       get_current_system_counts=lambda: (2, 1, 2),
                       verify_license_package=lambda *a, **k: (True, '', self.info),
                       check_and_update_monotonic_time=lambda: (True, ''),
                       is_license_blacklisted=lambda _: False, clear_license_cache=Mock(),
                       get_active_license_status=lambda **_: dict(valid=True),
                       query_one=lambda _: dict(id=7, raw_package_json=json.dumps(self.package)))
        exec(compile(selected, str(SOURCE), 'exec'), self.ns)
        self.db = patch.dict('sys.modules', {'database.db': Mock(adapt_query=lambda sql, conn: sql)})
        self.db.start()
        self.addCleanup(self.db.stop)

    def test_package_and_lease_share_one_commit(self):
        self.ns['_persist_verified_package'](self.package, 'https://local.invalid')
        statements = self.cursor.statements
        self.assertIn('FOR UPDATE', statements[0][0])
        self.assertTrue(any(sql.startswith('UPDATE wisp_license_info') for sql, _ in statements))
        self.assertTrue(statements[-1][0].startswith('INSERT INTO wisp_license_runtime_state'))
        self.assertNotIn('enrollment_token', json.dumps(statements, default=str))
        self.connection.commit.assert_called_once_with()
        self.connection.rollback.assert_not_called()

    def test_runtime_failure_rolls_back_package(self):
        self.cursor.fail_runtime = True
        with self.assertRaisesRegex(RuntimeError, 'runtime write failure'):
            self.ns['_persist_verified_package'](self.package, 'https://local.invalid')
        self.connection.rollback.assert_called_once_with()
        self.connection.commit.assert_not_called()
        self.ns['clear_license_cache'].assert_not_called()

    def test_invalid_signature_does_not_open_transaction(self):
        self.ns['verify_license_package'] = lambda *a, **k: (False, 'invalid signature', {})
        with self.assertRaisesRegex(ValueError, 'invalid signature'):
            self.ns['_persist_verified_package'](self.package, 'https://local.invalid')
        self.connection.cursor.assert_not_called()

    def test_unknown_capacity_does_not_grant_lease(self):
        self.ns['get_current_system_counts'] = lambda: (2, 1, -1)
        with self.assertRaisesRegex(RuntimeError, 'counts are unavailable'):
            self.ns['_persist_verified_package'](self.package, 'https://local.invalid')
        self.connection.cursor.assert_not_called()

    def test_stale_publisher_cannot_replace_new_lease(self):
        self.cursor.rows = iter([{'id': 1}, dict(id=7, raw_package_json='new package',
                                               status='active', blocked=0)])
        self.assertFalse(self.ns['publish_radius_license_state']())
        self.assertFalse(any(sql.startswith('INSERT INTO wisp_license_runtime_state')
                             for sql, _ in self.cursor.statements))
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_called_once_with()

    def test_revocation_at_publication_is_not_overridden(self):
        raw = json.dumps(self.package)
        self.cursor.rows = iter([{'id': 1}, dict(id=7, raw_package_json=raw,
                                               status='revoked', blocked=1)])
        self.assertTrue(self.ns['publish_radius_license_state']())
        _, arguments = self.cursor.statements[-1]
        self.assertEqual(arguments[2], 0)


class ActivationFailureResponseTests(unittest.TestCase):
    def test_database_failure_returns_review_message(self):
        tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
        selected = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef)
                                   and n.name == 'install_and_activate_license'], type_ignores=[])
        package = {'payload': {'license_id': 'local-response-proof'}}
        persist = Mock(side_effect=RuntimeError('injected storage failure'))
        namespace = dict(DEFAULT_MASTER_SERVER_URL='https://local.invalid',
                         ensure_license_tables=Mock(), clear_license_cache=Mock(),
                         decode_license_string=lambda _: (package, None),
                         get_current_system_counts=lambda: (2, 1, 2),
                         verify_license_package=lambda *a, **k: (True, '', {}),
                         get_machine_id=lambda: 'local', get_instance_uuid=lambda: 'local',
                         APP_VERSION='2.7.1', APP_EDITION='Enterprise', STORAGE_DIR='/unused',
                         get_master_public_key_pem=lambda: 'local-public-key',
                         authenticated_license_exchange=Mock(return_value={'valid': True, 'status': 'active'}),
                         remove_license_revocation=Mock(), logger=Mock(), _persist_verified_package=persist)
        exec(compile(selected, str(SOURCE), 'exec'), namespace)
        accepted, message = namespace['install_and_activate_license']('local-test-token')
        self.assertFalse(accepted)
        self.assertIn('راجع حالته الحالية', message)
        persist.assert_called_once_with(package, 'https://local.invalid')


if __name__ == '__main__':
    unittest.main()

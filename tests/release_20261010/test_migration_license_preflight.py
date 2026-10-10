import ast, sys, types, unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[2] / 'services/database_migration_service.py'
class Cursor:
    def __init__(self, events, error): self.events, self.error = events, error
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, sql): self.events.append(('query', sql))
    def fetchone(self): return {'error': self.error}
class Tests(unittest.TestCase):
    def run_preflight(self, error=None, publish_error=False):
        events = []
        tree = ast.parse(SOURCE.read_text(encoding='utf8'))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'verify_migration_license')
        ns = {}; exec(compile(ast.Module(body=[node], type_ignores=[]), 'preflight', 'exec'), ns)
        module = types.ModuleType('services.license_guard_service')
        def publish():
            events.append(('publish', None))
            if publish_error: raise RuntimeError('Verification unavailable')
        module.publish_radius_license_state = publish
        db = types.SimpleNamespace(cursor=lambda: Cursor(events, error))
        with patch.dict(sys.modules, {'services.license_guard_service': module}):
            ns['verify_migration_license'](db)
        return events
    def test_refresh_then_sql_verification(self):
        events = self.run_preflight(); self.assertEqual(events[0][0], 'publish')
        self.assertIn('fn_check_license_auth', events[1][1]); self.assertEqual(len(events), 2)
    def test_expired_or_revoked_license_rejected(self):
        with self.assertRaises(RuntimeError): self.run_preflight('License invalid')
    def test_publisher_failure_rejected(self):
        with self.assertRaises(RuntimeError): self.run_preflight(publish_error=True)
    def test_preflight_after_schema_before_first_delete(self):
        source = SOURCE.read_text(encoding='utf8')
        body = source[source.index('def execute_database_migration'):]
        self.assertLess(body.index('install_accounting_triggers(db)'), body.index('verify_migration_license(db)'))
        self.assertLess(body.index('verify_migration_license(db)'), body.index('DELETE FROM wisp_vouchers'))
if __name__ == '__main__': unittest.main(verbosity=2)

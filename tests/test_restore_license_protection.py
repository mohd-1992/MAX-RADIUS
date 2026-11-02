"""Actual SQLite transactions around the import boundary, without app startup."""
import ast
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


class Cursor:
    def __init__(self, conn): self.cursor = conn.cursor()
    def __enter__(self): return self
    def __exit__(self, *args): self.cursor.close()
    def __getattr__(self, name): return getattr(self.cursor, name)


class Connection:
    def __init__(self, conn): self.conn = conn
    def cursor(self): return self.conn.cursor()
    def commit(self): self.conn.commit()
    def rollback(self): self.conn.rollback()
    def close(self): pass


class LicenseProtectionTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('CREATE TABLE wisp_license_info(id INT PRIMARY KEY,license_id TEXT,status TEXT,raw_package_json TEXT,max_active_sessions INT)')
        self.target = [(1, 'target-old', 'suspended', '{"old":true}', 150),
                       (2, 'target-current', 'active', '{ "exact": true }', 500)]
        self.conn.executemany('INSERT INTO wisp_license_info VALUES(?,?,?,?,?)', self.target)
        self.conn.commit()
        source = Path(__file__).resolve().parents[1] / 'services' / 'backup_service.py'
        tree = ast.parse(source.read_text(encoding='utf-8-sig'))
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name in ('restore_backup', '_restore_machine_license_rows')]
        for node in nodes: node.decorator_list = []
        self.ns = {'tempfile': tempfile, 'get_connection': lambda: Connection(self.conn),
                   'is_mysql_conn': lambda conn: False,
                   'query_all': lambda q: [dict(x) for x in self.conn.execute(q)]}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), '<license-boundary>', 'exec'), self.ns)
        guard = types.ModuleType('services.license_guard_service')
        self.clears = 0
        def clear(): self.clears += 1
        guard.clear_license_cache = clear
        self.modules = patch.dict(sys.modules, {'services.license_guard_service': guard})
        self.modules.start()

    def tearDown(self):
        self.modules.stop()
        self.conn.close()

    def rows(self): return [tuple(x) for x in self.conn.execute('SELECT * FROM wisp_license_info ORDER BY id')]

    def foreign_import(self, state):
        state['started'] = True
        self.conn.execute('DELETE FROM wisp_license_info')
        self.conn.execute("INSERT INTO wisp_license_info VALUES(9,'foreign','active','{}',999)")
        self.conn.commit()

    def test_failed_import_restores_exact_target_rows(self):
        def failure(filename, actor, directory, state):
            self.foreign_import(state)
            return False, 'deliberate native import failure'
        self.ns['_restore_backup'] = failure
        self.assertFalse(self.ns['restore_backup']('fixture.sql')[0])
        self.assertEqual(self.rows(), self.target)
        self.assertEqual(self.clears, 1)

    def test_success_restores_only_once(self):
        def success(filename, actor, directory, state):
            self.foreign_import(state)
            self.ns['_restore_machine_license_rows'](state['rows'])
            state['restored'] = True
            return True, 'complete'
        self.ns['_restore_backup'] = success
        self.assertTrue(self.ns['restore_backup']('fixture.sql')[0])
        self.assertEqual(self.rows(), self.target)
        self.assertEqual(self.clears, 1)

    def test_preflight_failure_does_not_rewrite_license(self):
        self.ns['_restore_backup'] = lambda *a: (False, 'rejected before import')
        self.assertFalse(self.ns['restore_backup']('bad.sql')[0])
        self.assertEqual(self.rows(), self.target)
        self.assertEqual(self.clears, 0)

    def test_snapshot_read_failure_prevents_import(self):
        def unavailable(query): raise RuntimeError('DB unavailable')
        self.ns['query_all'] = unavailable
        self.ns['_restore_backup'] = lambda *a: self.fail('Import must not start')
        self.assertFalse(self.ns['restore_backup']('fixture.sql')[0])
        self.assertEqual(self.rows(), self.target)

    def test_unexpected_exception_still_restores(self):
        def failure(filename, actor, directory, state):
            self.foreign_import(state)
            raise RuntimeError('unexpected import failure')
        self.ns['_restore_backup'] = failure
        with self.assertRaises(RuntimeError): self.ns['restore_backup']('fixture.sql')
        self.assertEqual(self.rows(), self.target)

    def test_unlicensed_target_never_inherits_foreign_license(self):
        self.conn.execute('DELETE FROM wisp_license_info'); self.conn.commit()
        def failure(filename, actor, directory, state):
            self.foreign_import(state)
            return False, 'failure'
        self.ns['_restore_backup'] = failure
        self.assertFalse(self.ns['restore_backup']('fixture.sql')[0])
        self.assertEqual(self.rows(), [])

    def test_incomplete_license_schema_never_reports_success(self):
        def failure(filename, actor, directory, state):
            state['started'] = True
            self.conn.execute('DROP TABLE wisp_license_info')
            self.conn.execute('CREATE TABLE wisp_license_info(id INT)'); self.conn.commit()
            return True, 'incorrect success'
        self.ns['_restore_backup'] = failure
        self.assertFalse(self.ns['restore_backup']('fixture.sql')[0])

    def test_license_transaction_rolls_back_partial_insert(self):
        before = self.rows()
        duplicate = [dict(zip(('id', 'license_id', 'status', 'raw_package_json', 'max_active_sessions'), x)) for x in self.target]
        duplicate[1]['id'] = duplicate[0]['id']
        with self.assertRaises(sqlite3.IntegrityError): self.ns['_restore_machine_license_rows'](duplicate)
        self.assertEqual(self.rows(), before)


if __name__ == '__main__': unittest.main()

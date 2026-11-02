"""Run the schema helper without importing application configuration or workers."""
import ast
from pathlib import Path
import unittest


source = Path(__file__).resolve().parents[1] / 'database' / 'schema_healer.py'
tree = ast.parse(source.read_text(encoding='utf-8-sig'))
helper = next(node for node in tree.body
              if isinstance(node, ast.FunctionDef) and node.name == '_ensure_cycle_start_default')
namespace = {}
exec(compile(ast.Module(body=[helper], type_ignores=[]), str(source), 'exec'), namespace)
ensure_default = namespace['_ensure_cycle_start_default']


class Cursor:
    def __init__(self, row):
        self.row = row
        self.statements = []

    def execute(self, statement):
        self.statements.append(statement)

    def fetchone(self):
        return self.row


class CycleDefaultTests(unittest.TestCase):
    def test_existing_null_avoids_schema_write_for_both_tables(self):
        for table in ('wisp_vouchers', 'wisp_subscribers'):
            for row in ({'Default': None}, ('last_renewed_at', 'datetime', 'YES', '', None, '')):
                with self.subTest(table=table, row_type=type(row).__name__):
                    cur = Cursor(row)
                    ensure_default(cur, table)
                    self.assertEqual(len(cur.statements), 1)
                    self.assertTrue(cur.statements[0].startswith('SHOW COLUMNS'))

    def test_legacy_default_is_still_migrated(self):
        for table in ('wisp_vouchers', 'wisp_subscribers'):
            cur = Cursor({'Default': 'current_timestamp()'})
            ensure_default(cur, table)
            self.assertEqual(cur.statements[-1],
                             f'ALTER TABLE {table} ALTER COLUMN last_renewed_at SET DEFAULT NULL')

    def test_missing_column_is_not_silently_accepted(self):
        cur = Cursor(None)
        with self.assertRaises(RuntimeError):
            ensure_default(cur, 'wisp_vouchers')
        self.assertEqual(len(cur.statements), 1)

    def test_other_tables_cannot_be_interpolated(self):
        cur = Cursor({'Default': None})
        with self.assertRaises(ValueError):
            ensure_default(cur, 'radacct')
        self.assertEqual(cur.statements, [])

    def test_database_failure_is_propagated(self):
        class BrokenCursor(Cursor):
            def execute(self, statement):
                raise TimeoutError('Fixture metadata lock timeout')
        with self.assertRaises(TimeoutError):
            ensure_default(BrokenCursor(None), 'wisp_vouchers')


if __name__ == '__main__':
    unittest.main()

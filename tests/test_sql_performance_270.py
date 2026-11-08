"""Read-only performance contracts, without MAX app startup or external services."""
import ast
import datetime as dt
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import types
import unittest
from unittest.mock import patch

from services.quota_batch_sql import batch_quota_source
from services.session_spool import SessionSpool
import services.session_history_service as history

ROOT = Path(__file__).resolve().parents[1]


class QuotaSourceTests(unittest.TestCase):
    def test_both_union_branches_are_narrowed_and_values_bound(self):
        sql, params = batch_quota_source(["name' OR 1=1 --", 'Second', 'Second'])
        self.assertNotIn("name' OR", sql)
        self.assertEqual(params, ("name' OR 1=1 --", 'Second') * 2)
        self.assertIn('WHERE a.username IN (?,?)', sql)
        self.assertIn('WHERE v.username IN (?,?)', sql)
        self.assertEqual(sql.count('?'), len(params))

    def test_view_seal_delta_and_summary_identity_preserved(self):
        sql, _ = batch_quota_source(['one'])
        self.assertIn('seal.acctuniqueid=CONVERT(a.acctuniqueid USING ascii)', sql)
        self.assertIn('CAST(a.radacctid AS SIGNED)', sql)
        self.assertIn('-CAST(CONV(SUBSTR(h.summary_key,1,15),16,10) AS SIGNED)', sql)
        for field in ('acctinputoctets', 'acctoutputoctets', 'acctsessiontime'):
            self.assertIn(f'CAST(a.{field} AS DECIMAL(30,0))', sql)
        self.assertIn('h.first_start,h.first_start', sql)

    def test_empty_names_are_rejected(self):
        with self.assertRaises(ValueError):
            batch_quota_source([])


class SpoolTests(unittest.TestCase):
    def test_typed_roundtrip_preserves_unknown_and_large_counters(self):
        rows = [{'username': 'مستخدم', 'counter': 2**63+19, 'unknown': None,
                 'time': dt.datetime(2026, 10, 1, 1, 2, 3, 456789),
                 'day': dt.date(2026, 10, 1), 'value': Decimal('1.2300'),
                 'raw': b'\x00\xff', 'text': '{"_max_session_type":"decimal","value":"1"}'}]
        with SessionSpool() as spool:
            spool.append(rows)
            self.assertEqual(list(spool.batches(250)), [rows])
            self.assertEqual(spool.total, 1)
            self.assertGreater(spool.size_bytes, 0)

    def test_batches_replay_and_empty_export_shape(self):
        with SessionSpool() as spool:
            self.assertEqual(list(spool.batches(2)), [[]])
            spool.append([{'n': n} for n in range(5)])
            expected = [[{'n': 0}, {'n': 1}], [{'n': 2}, {'n': 3}], [{'n': 4}]]
            self.assertEqual(list(spool.batches(2)), expected)
            self.assertEqual(list(spool.batches(2)), expected)

    def test_cleanup_after_consumer_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(InterruptedError):
                with SessionSpool(directory) as spool:
                    stream = spool._stream
                    spool.append([{'n': 1}])
                    raise InterruptedError()
            self.assertTrue(stream.closed)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_invalid_batch_size_rejected(self):
        with SessionSpool() as spool:
            with self.assertRaises(ValueError):
                list(spool.batches(0))

    def test_unsupported_type_fails_closed(self):
        with SessionSpool() as spool:
            with self.assertRaises(TypeError):
                spool.append([{'bad': object()}])


class DashboardCountsTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE wisp_vouchers(status TEXT,first_used_at TEXT,expires_at TEXT)')
        tree = ast.parse((ROOT/'services/stats_service.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.Assign) and
                    any(getattr(t, 'id', '') == 'VOUCHER_COUNTS_SQL' for t in n.targets))
        self.sql = ast.literal_eval(node.value)

    def test_empty_database_has_integer_zeros(self):
        row = dict(self.db.execute(self.sql).fetchone())
        self.assertEqual(set(row.values()), {0})

    def test_predicates_and_overlapping_states_match_previous_contract(self):
        rows = [('active', '2020-01-01', '2099-01-01'), ('active', None, '2000-01-01'),
                ('used', None, None), ('expired', None, '2099-01-01'),
                ('unused', None, '2000-01-01'), ('disabled', None, None),
                ('suspended', None, '2099-01-01'), ('recharged', '2020-01-01', None),
                ('other', None, None), (None, None, '2000-01-01')]
        self.db.executemany('INSERT INTO wisp_vouchers VALUES(?,?,?)', rows)
        old = {
            'activated': "first_used_at IS NOT NULL OR status IN ('active','used','expired')",
            'active': "status IN ('active','used') AND (expires_at IS NULL OR expires_at>CURRENT_TIMESTAMP)",
            'expired': "status='expired' OR (expires_at IS NOT NULL AND expires_at<=CURRENT_TIMESTAMP)",
            'unused': "status='unused'", 'disabled': "status IN ('suspended','disabled')",
            'recharged': "status='recharged'"}
        expected = {key: self.db.execute('SELECT COUNT(*) FROM wisp_vouchers WHERE '+where).fetchone()[0]
                    for key, where in old.items()}
        expected['total'] = len(rows)
        self.assertEqual(dict(self.db.execute(self.sql).fetchone()), expected)


class Cursor:
    def __init__(self, conn, streaming=False):
        self.conn, self.streaming = conn, streaming
        self.rows = []
    def __enter__(self):
        if self.streaming:
            self.conn.source_active = True
        return self
    def __exit__(self, *exc):
        self.conn.source_active = False
    def execute(self, sql, params=()):
        if not self.streaming and self.conn.source_active:
            raise AssertionError('SQL while an unbuffered result is active')
        self.conn.sql.append(sql)
        if sql.startswith('START'):
            return
        if 'FOUND_ROWS()' in sql:
            self.rows = [{'n': self.conn.total}]
        elif 'COUNT(*)' in sql:
            self.rows = [{'n': self.conn.total}]
        else:
            offset = params[-1] if 'OFFSET' in sql else 0
            limit = params[-2] if 'OFFSET' in sql else self.conn.total
            self.rows = [{'n': n} for n in range(offset, min(offset+limit, self.conn.total))]
    def fetchall(self):
        return self.rows
    def fetchmany(self, size):
        rows, self.rows = self.rows[:size], self.rows[size:]
        return rows


class Connection:
    def __init__(self, total=123, maria=True):
        self.total, self.maria, self.rollbacks = total, maria, 0
        self.sql, self.source_active = [], False
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        pass
    def get_server_info(self):
        return '10.11-MariaDB' if self.maria else 'other'
    def cursor(self, cls=None):
        return Cursor(self, bool(cls))
    def rollback(self):
        self.rollbacks += 1


class HistoryContracts(unittest.TestCase):
    def context(self, conn):
        db = types.ModuleType('database.db'); db.get_connection = lambda: conn
        owner = types.ModuleType('core.ownership'); owner.owner_id = lambda: 7
        self.addCleanup(patch.stopall)
        patch.dict('sys.modules', {'database.db': db, 'core.ownership': owner}).start()
        patch.object(history, 'history_sql', lambda username, ident: ('SELECT n FROM fixture', ())).start()
        patch.object(history, 'format_history_rows', lambda rows, query: None).start()

    def test_mariadb_total_clamp_and_no_count_query(self):
        conn = Connection(); self.context(conn)
        result = history.get_session_history('fixture', page=2, page_size=50)
        self.assertEqual((result['total'], result['page'], result['pages']), (123, 2, 3))
        self.assertEqual(result['rows'][0]['n'], 50)
        self.assertFalse(any('COUNT(*)' in sql for sql in conn.sql))
        self.assertEqual(conn.rollbacks, 1)

    def test_page_outside_range_keeps_last_page_behavior(self):
        conn = Connection(); self.context(conn)
        result = history.get_session_history('fixture', page=9999)
        self.assertEqual(result['page'], 3)
        self.assertEqual(result['rows'][0]['n'], 100)

    def test_empty_and_unlimited_pages(self):
        conn = Connection(total=0); self.context(conn)
        self.assertEqual(history.get_session_history('fixture')['rows'], [])
        conn.total = 123
        result = history.get_session_history('fixture', page=9, page_size=0)
        self.assertEqual((result['page'], result['pages'], len(result['rows'])), (1, 1, 123))

    def test_other_backends_keep_count_path(self):
        conn = Connection(maria=False); self.context(conn)
        self.assertEqual(history.get_session_history('fixture')['total'], 123)
        self.assertTrue(any('COUNT(*)' in sql for sql in conn.sql))

    def test_export_reads_source_once_and_replays_bounded_batches(self):
        conn = Connection(); self.context(conn)
        with tempfile.TemporaryDirectory() as directory, patch('services.heavy_operation_service.lock_root', lambda: Path(directory)):
            batches = list(history.iter_session_history('fixture', batch_size=50))
            self.assertEqual([len(x[0]) for x in batches], [50, 50, 23])
            self.assertEqual([x[2] for x in batches], [0, 50, 100])
            self.assertTrue(all(x[1] == 123 for x in batches))
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertEqual(sum(sql.startswith('SELECT') for sql in conn.sql), 1)
        self.assertEqual(conn.rollbacks, 1)

    def test_cancellation_rolls_back_and_closes_spool(self):
        from services.heavy_operation_service import ExportCancelled
        conn = Connection(); self.context(conn)
        with tempfile.TemporaryDirectory() as directory, patch('services.heavy_operation_service.lock_root', lambda: Path(directory)), patch('services.heavy_operation_service.export_cancelled', lambda: True):
            with self.assertRaises(ExportCancelled):
                list(history.iter_session_history('fixture'))
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertEqual(conn.rollbacks, 1)

    def test_consumer_close_and_cancel_during_replay_clean_up(self):
        from services.heavy_operation_service import ExportCancelled
        conn = Connection(); self.context(conn)
        cancelled = [False]
        with tempfile.TemporaryDirectory() as directory, patch('services.heavy_operation_service.lock_root', lambda: Path(directory)), patch('services.heavy_operation_service.export_cancelled', lambda: cancelled[0]):
            iterator = history.iter_session_history('fixture', batch_size=50)
            self.assertEqual(len(next(iterator)[0]), 50)
            iterator.close()
            self.assertEqual(conn.rollbacks, 1)
            iterator = history.iter_session_history('fixture', batch_size=50)
            next(iterator)
            cancelled[0] = True
            with self.assertRaises(ExportCancelled):
                next(iterator)
            self.assertEqual(conn.rollbacks, 2)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_spool_write_failure_rolls_back_without_partial_result(self):
        conn = Connection(); self.context(conn)
        with tempfile.TemporaryDirectory() as directory, patch('services.heavy_operation_service.lock_root', lambda: Path(directory)), patch.object(SessionSpool, 'append', side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError):
                list(history.iter_session_history('fixture'))
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertEqual(conn.rollbacks, 1)


if __name__ == '__main__':
    unittest.main()

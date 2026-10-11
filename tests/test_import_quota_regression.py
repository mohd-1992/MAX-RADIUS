"""Workbook and degraded-accounting regressions without application workers."""
import ast
import datetime
import io
import re
import types
import unittest
from pathlib import Path
from unittest.mock import Mock

import openpyxl

ROOT = Path(__file__).resolve().parents[1]


def load(path, names, namespace):
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    nodes = [n for n in tree.body if
             isinstance(n, ast.FunctionDef) and n.name in names or
             isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in n.targets)]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)


class ExcelTests(unittest.TestCase):
    def setUp(self):
        self.ns = dict(re=re, io=io, openpyxl=openpyxl, xlrd=None)
        load('services/import_service.py', {'COLUMN_ALIASES', 'normalize_header',
             '_excel_header_map', 'parse_excel_file'}, self.ns)

    def workbook(self, headers, row):
        wb = openpyxl.Workbook()
        wb.active.append(headers)
        wb.active.append(row)
        data = io.BytesIO()
        wb.save(data)
        data.seek(0)
        return types.SimpleNamespace(filename='case.xlsx', read=data.read)

    def test_time_and_upload_do_not_overwrite_each_other(self):
        rows = self.ns['parse_excel_file'](self.workbook(
            ['username', 'upload_used_mb', 'uptime_used_mins', 'download_used_mb'],
            ['fixture', 2, 3, 7]))
        self.assertEqual(rows[0]['upload_used_mb'], '2')
        self.assertEqual(rows[0]['uptime_used_mins'], '3')
        self.assertEqual(rows[0]['download_used_mb'], '7')

    def test_all_aliases_and_generated_template_hints(self):
        for name, aliases in self.ns['COLUMN_ALIASES'].items():
            for alias in [name, *aliases]:
                self.assertEqual(self.ns['normalize_header'](alias), name)
            self.assertEqual(self.ns['normalize_header']('عنوان ('+name+')*'), name)
        self.assertEqual(self.ns['normalize_header']('Upload (MB)'), 'upload_used_mb')

    def test_short_aliases_never_match_other_words(self):
        for header in ['backup', 'shipping', 'profile_notes', 'uptime_unknown', 'user_comment']:
            self.assertEqual(self.ns['normalize_header'](header), header)

    def test_duplicate_canonical_field_rejected_before_rows(self):
        for headers in [['upload_used_mb', 'up'], ['username', 'اسم المستخدم'], ['uptime', 'uptime_used_mins']]:
            with self.assertRaisesRegex(ValueError, 'مكرر'):
                self.ns['parse_excel_file'](self.workbook(headers, [2, 3]))

    def test_conflicting_hint_and_blank_headers(self):
        with self.assertRaises(ValueError):
            self.ns['normalize_header']('username (password)')
        with self.assertRaises(ValueError):
            self.ns['_excel_header_map']([(1, None), (2, ' ')])


class QuotaTests(unittest.TestCase):
    def quota(self, kind, usage, expired=False, unlimited=False):
        now = datetime.datetime(2026, 10, 11, 12)
        entity = dict(id=1, username='fixture', status='active', base_quota_mb=0 if unlimited else 100,
                      extra_quota_mb=10, base_uptime_mins=30, created_at=now,
                      expires_at=now-datetime.timedelta(days=1) if expired else now+datetime.timedelta(days=1))
        reader = Mock(side_effect=usage) if isinstance(usage, Exception) else Mock(return_value=usage)
        ns = dict(datetime=datetime, BYTES_PER_MB=1048576, query_one=Mock(return_value=entity),
                  get_accounting_totals=reader, get_db_storage_now=lambda: now)
        load('services/quota_service.py', {'calculate_account_quota'}, ns)
        return ns['calculate_account_quota']('fixture', kind)

    def test_unavailable_is_unknown_not_zero_for_both_types(self):
        for kind in ('voucher', 'subscriber'):
            for value in [RuntimeError('DB unavailable'), None, {'total_bytes': 5}]:
                with self.subTest(kind=kind, value=value):
                    result = self.quota(kind, value)
                    self.assertTrue(result['accounting_error'])
                    self.assertEqual(result['accounting_status'], 'unavailable')
                    for field in ['used_total_bytes', 'used_up_bytes', 'used_down_bytes',
                                  'remaining_mb', 'remaining_uptime_mins', 'sessions_count']:
                        self.assertIsNone(result[field])
                    self.assertFalse(result['is_quota_depleted'])

    def test_time_expiry_still_known_when_accounting_unavailable(self):
        result = self.quota('voucher', RuntimeError('DB'), expired=True)
        self.assertTrue(result['is_time_expired'])
        self.assertTrue(result['is_exhausted'])
        self.assertFalse(result['is_active'])

    def test_available_zero_and_nonzero_are_distinct_from_unavailable(self):
        for mb, count in [(0, 0), (7, 2)]:
            result = self.quota('subscriber', dict(total_bytes=mb*1048576, up_bytes=mb*1048576,
                                                 down_bytes=0, uptime_secs=180, sessions_count=count))
            self.assertFalse(result['accounting_error'])
            self.assertEqual(result['sessions_count'], count)
            self.assertEqual(result['used_mb'], mb)
            self.assertEqual(result['remaining_mb'], 110-mb)


if __name__ == '__main__':
    unittest.main()

"""TXT batch download: real SQLite snapshots and Flask permission boundaries."""
import ast
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
import types
import unittest
from unittest.mock import patch

from flask import Flask, jsonify, request, send_file, session
from services.batch_export_service import export_usernames_text

ROOT = Path(__file__).resolve().parents[1]


class BatchTextExportTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / 'fixture.db'
        self.sql = []
        self.db = sqlite3.connect(self.path)
        self.db.executescript('''
            CREATE TABLE wisp_voucher_batches(id INTEGER PRIMARY KEY,name TEXT,reseller_id INTEGER);
            CREATE TABLE wisp_vouchers(id INTEGER PRIMARY KEY,batch_id INTEGER,username TEXT,
                password TEXT,reseller_id INTEGER);
            INSERT INTO wisp_voucher_batches VALUES(1,'حزمة اختبار',3),(2,'أخرى',4),(3,'فارغة',3);
        ''')
        self.codes = ['000001', '=literal', 'اسم_عربي'] + [f'{i:09d}' for i in range(2498)]
        self.db.executemany('INSERT INTO wisp_vouchers(batch_id,username,password,reseller_id) '
                            'VALUES(1,?,\'SECRET-NOT-FOR-EXPORT\',3)', [(v,) for v in self.codes])
        self.db.execute("INSERT INTO wisp_vouchers(batch_id,username,password,reseller_id) VALUES(2,'FOREIGN','SECRET',4)")
        self.db.commit()
        stub = types.ModuleType('database.db')
        def connect():
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            conn.set_trace_callback(self.sql.append)
            return conn
        stub.get_connection = connect
        stub.is_mysql_conn = lambda conn: False
        stub.adapt_query = lambda query, conn: query
        self.modules = patch.dict('sys.modules', {'database.db': stub})
        self.modules.start()
        self.app = Flask(__name__)
        self.app.secret_key = 'isolated-txt-fixture'
        def query_one(query, args):
            conn = connect()
            try:
                row = conn.execute(query, args).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()
        self.ns = dict(os=os, jsonify=jsonify, request=request, send_file=send_file,
                       query_one=query_one, logger=logging.getLogger('txt-test'),
                       get_current_manager=lambda: session.get('manager'),
                       has_permission=lambda permission, manager: permission in session.get('permissions', []),
                       owner_id=lambda: session['manager']['id'])
        tree = ast.parse((ROOT/'web/routes/vouchers.py').read_text(encoding='utf-8'))
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and
                 n.name in ('export_batch_text', 'guard_voucher_permissions')]
        for node in nodes:
            node.decorator_list = []
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'txt-routes', 'exec'), self.ns)
        self.app.before_request(self.ns['guard_voucher_permissions'])
        self.app.add_url_rule('/batch/<int:batch_id>/text', 'export_batch_text', self.ns['export_batch_text'])
        self.client = self.app.test_client()

    def tearDown(self):
        self.modules.stop()
        self.db.close()
        self.folder.cleanup()

    def sign_in(self, permissions=('vouchers.print',)):
        with self.client.session_transaction() as value:
            value['manager'] = {'id': 3}
            value['permissions'] = list(permissions)

    def test_complete_large_batch_literal_codes_no_password_or_header(self):
        name, payload, count = export_usernames_text(1, 3)
        try:
            self.assertEqual(name, 'حزمة اختبار')
            self.assertEqual(count, 2501)
            self.assertEqual(payload.read(), ('\r\n'.join(self.codes)+'\r\n').encode('utf-8'))
        finally:
            payload.close()
        self.assertGreaterEqual(sum('SELECT id,username,reseller_id' in q for q in self.sql), 3)
        self.assertFalse(any('password' in q.lower() for q in self.sql))
        self.assertFalse(any(q.startswith(('UPDATE', 'DELETE', 'INSERT')) for q in self.sql))

    def test_owned_and_missing_batch_fail_before_any_download(self):
        for batch in (2, 999):
            with self.subTest(batch=batch), self.assertRaises(LookupError):
                export_usernames_text(batch, 3)

    def test_mismatched_card_owner_cannot_leak_a_partial_file(self):
        self.db.execute('UPDATE wisp_vouchers SET reseller_id=4 WHERE id=2000')
        self.db.commit()
        with self.assertRaises(LookupError):
            export_usernames_text(1, 3)

    def test_line_break_in_name_refuses_whole_export(self):
        self.db.execute("UPDATE wisp_vouchers SET username='first'||char(10)||'second' WHERE id=2000")
        self.db.commit()
        with self.assertRaises(ValueError):
            export_usernames_text(1, 3)

    def test_empty_batch_is_an_empty_file(self):
        _, payload, count = export_usernames_text(3, 3)
        try:
            self.assertEqual((count, payload.read()), (0, b''))
        finally:
            payload.close()

    def test_http_authentication_and_print_permission(self):
        self.assertEqual(self.client.get('/batch/1/text').status_code, 401)
        self.sign_in(('vouchers.view',))
        self.assertEqual(self.client.get('/batch/1/text').status_code, 403)
        self.assertFalse(self.sql)
        self.sign_in()
        with self.client.get('/batch/1/text') as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, ('\r\n'.join(self.codes)+'\r\n').encode())
            self.assertTrue(response.content_type.startswith('text/plain'))
            self.assertIn('filename*=', response.headers['Content-Disposition'])
            self.assertIn('.txt', response.headers['Content-Disposition'])
            self.assertEqual(response.headers['Cache-Control'], 'no-store, private')
            self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')

    def test_direct_foreign_batch_is_not_downloadable(self):
        self.sign_in()
        self.assertEqual(self.client.get('/batch/2/text').status_code, 404)


if __name__ == '__main__':
    unittest.main()

"""Isolated loopback TLS uploads and fake DB handles; no app/NAS/customer access."""
import ast
import contextlib
import datetime
import hashlib
import http.server
import io
import ipaddress
import json
import logging
import os
from pathlib import Path
import socket
import ssl
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from services import telegram_document_transport as upload
from services import telegram_backup_parts as parts


ROOT = Path(__file__).resolve().parents[1]
TOKEN = '123456:fixture-only-not-real'


class Fixture:
    def __init__(self, mode='ok', delay=.01):
        self.mode, self.delay = mode, delay
        self.requests, self.bodies = [], []
        self.started, self.release = threading.Event(), threading.Event()

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now-datetime.timedelta(minutes=1))
            .not_valid_after(now+datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([
                x509.DNSName('localhost'), x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), False)
            .sign(key, hashes.SHA256()))
        self.cert, private = root/'cert.pem', root/'key.pem'
        self.cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        private.write_bytes(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def do_POST(self):
                fixture.requests.append(self.path)
                fixture.started.set()
                remaining = int(self.headers['Content-Length'])
                body = bytearray()
                if fixture.mode == 'stall_write':
                    fixture.release.wait(2)
                    self.close_connection = True
                    return
                while remaining:
                    chunk = self.rfile.read(min(8192, remaining))
                    if not chunk:
                        break
                    body.extend(chunk)
                    remaining -= len(chunk)
                    if fixture.mode == 'slow':
                        time.sleep(fixture.delay)
                    if fixture.mode == 'disconnect' and len(body) >= 16384:
                        self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                                  struct.pack('ii', 1, 0))
                        self.close_connection = True
                        return
                fixture.bodies.append(bytes(body))
                if fixture.mode == 'lost_response':
                    self.close_connection = True
                    return
                if fixture.mode == 'stall_read':
                    fixture.release.wait(2)
                    self.close_connection = True
                    return
                if fixture.mode == 'html' and len(fixture.requests) == 1:
                    status = 400
                    result = {'ok': False, 'error_code': 400,
                              'description': "Bad Request: can't parse entities"}
                else:
                    status, result = 200, {'ok': True, 'result': {'message_id': 1}}
                data = json.dumps(result).encode()
                try:
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (OSError, ssl.SSLError):
                    pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert, private)
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'https://127.0.0.1:' + str(self.server.server_port) + '/bot' + TOKEN + '/sendDocument'
        return self

    def transport(self):
        return httpx.HTTPTransport(verify=ssl.create_default_context(cafile=str(self.cert)), retries=0)

    def __exit__(self, *args):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.tmp.cleanup()


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name)/'حزمة اختبار.tar.gz'
        self.file.write_bytes(os.urandom(128*1024))

    def tearDown(self):
        self.tmp.cleanup()

    def send(self, fixture, **kwargs):
        opts = dict(connect_timeout=1, write_timeout=.25, read_timeout=.25,
                    total_timeout=4, _endpoint=fixture.url, _transport=fixture.transport())
        opts.update(kwargs)
        return upload.upload_document(TOKEN, '-1000000000', str(self.file), '<b>fixture</b>', **opts)

    def test_slow_tls_stream_succeeds_beyond_short_old_style_budget(self):
        with Fixture('slow', delay=.025) as f:
            start = time.monotonic()
            result, progress = self.send(f, read_timeout=2)
            self.assertTrue(result['ok'])
            self.assertGreater(time.monotonic()-start, .25)
            self.assertEqual(progress['attempts'], 1)
            self.assertIn(self.file.read_bytes(), f.bodies[0])

    def test_write_stall_is_bounded_without_retry(self):
        self.file.write_bytes(os.urandom(8*1024*1024))
        with Fixture('stall_write') as f:
            start = time.monotonic()
            with self.assertRaises(upload.UploadFailure) as e:
                self.send(f, write_timeout=.15)
            self.assertEqual(e.exception.details['code'], 'write_timeout')
            self.assertLess(time.monotonic()-start, 1.5)
            self.assertEqual(len(f.requests), 1)

    def test_equal_customer_size_slow_upload_old_fails_new_stream_succeeds(self):
        import requests
        self.file.write_bytes(os.urandom(7292261))  # Same byte count, synthetic data only.
        with Fixture('slow', delay=.0032) as f:
            with self.file.open('rb') as file:
                with self.assertRaises(requests.exceptions.RequestException):
                    requests.post(f.url, files={'document': file},
                                  verify=str(f.cert), timeout=.2)
        with Fixture('slow', delay=.0032) as f:
            result, progress = self.send(f, write_timeout=.5, read_timeout=4,
                                         total_timeout=10)
            self.assertTrue(result['ok'])
            self.assertEqual(progress['attempts'], 1)
            self.assertIn(self.file.read_bytes(), f.bodies[0])

    def test_tls_validation_remains_enabled(self):
        with Fixture() as f:
            with self.assertRaises(upload.UploadFailure) as e:
                self.send(f, _transport=httpx.HTTPTransport(retries=0))
            self.assertEqual(e.exception.details['phase'], 'connect')
            self.assertEqual(len(f.requests), 0)

    def test_mid_upload_disconnect_does_not_replay(self):
        self.file.write_bytes(os.urandom(8*1024*1024))
        with Fixture('disconnect') as f:
            with self.assertRaises(upload.UploadFailure) as e:
                self.send(f)
            self.assertTrue(e.exception.details['outcome_uncertain'])
            self.assertEqual(len(f.requests), 1)

    def test_accept_then_response_loss_is_uncertain_not_replayed(self):
        with Fixture('lost_response') as f:
            with self.assertRaises(upload.UploadFailure) as e:
                self.send(f)
            self.assertTrue(e.exception.details['outcome_uncertain'])
            self.assertEqual(len(f.bodies), 1)
            self.assertEqual(len(f.requests), 1)

    def test_read_stall_is_bounded(self):
        with Fixture('stall_read') as f:
            with self.assertRaises(upload.UploadFailure) as e:
                self.send(f, read_timeout=.15)
            self.assertEqual(e.exception.details['code'], 'read_timeout')
            self.assertTrue(e.exception.details['outcome_uncertain'])
            self.assertEqual(len(f.requests), 1)

    def test_html_rejection_rewinds_exact_file_once(self):
        with Fixture('html') as f:
            result, progress = self.send(f)
            self.assertTrue(result['ok'])
            self.assertEqual(progress['attempts'], 2)
            self.assertEqual(len(f.requests), 2)
            self.assertTrue(all(self.file.read_bytes() in b for b in f.bodies))
            self.assertIn(b'name="parse_mode"', f.bodies[0])
            self.assertNotIn(b'name="parse_mode"', f.bodies[1])

    def test_busy_slot_rejects_fast_and_recovers(self):
        self.assertTrue(upload._UPLOAD_SLOT.acquire(False))
        try:
            with self.assertRaises(upload.UploadFailure) as e:
                upload.upload_document(TOKEN, '1', self.file, '')
            self.assertEqual(e.exception.details['code'], 'upload_busy')
        finally:
            upload._UPLOAD_SLOT.release()
        with Fixture() as f:
            self.assertTrue(self.send(f)[0]['ok'])

    def mock(self, callback, **kwargs):
        return upload.upload_document(TOKEN, '1', self.file, '',
            _transport=httpx.MockTransport(callback), **kwargs)

    def test_connect_timeout_redacts_secret_and_is_not_retried(self):
        calls = []
        def fail(request):
            calls.append(1)
            raise httpx.ConnectTimeout(str(request.url))
        with self.assertRaises(upload.UploadFailure) as e:
            self.mock(fail)
        self.assertFalse(e.exception.details['outcome_uncertain'])
        self.assertNotIn(TOKEN, str(e.exception))
        self.assertEqual(len(calls), 1)

    def test_http_error_not_retried(self):
        calls = []
        def deny(request):
            calls.append(1)
            return httpx.Response(403, json={'ok': False, 'error_code': 403})
        self.assertFalse(self.mock(deny)[0]['ok'])
        self.assertEqual(len(calls), 1)

    def test_redirect_does_not_forward_token(self):
        calls = []
        def redirect(request):
            calls.append(str(request.url))
            return httpx.Response(302, headers={'location': 'https://example.invalid/'}, content=b'')
        with self.assertRaises(upload.UploadFailure):
            self.mock(redirect)
        self.assertEqual(len(calls), 1)

    def test_invalid_json_is_uncertain(self):
        with self.assertRaises(upload.UploadFailure) as e:
            self.mock(lambda req: httpx.Response(200, content=b'not json'))
        self.assertTrue(e.exception.details['outcome_uncertain'])

    def test_oversize_reply_bounded(self):
        with self.assertRaises(upload.UploadFailure) as e:
            self.mock(lambda req: httpx.Response(200, content=b'x'*(1024*1024+1)))
        self.assertEqual(e.exception.details['code'], 'reply_too_large')

    def test_info_logs_redact_token(self):
        log = logging.getLogger('httpx');old = log.level
        target = io.StringIO();handler = logging.StreamHandler(target)
        log.addHandler(handler);log.setLevel(logging.INFO)
        try:
            self.mock(lambda req: httpx.Response(200, json={'ok': True}))
            self.assertIn('[REDACTED]', target.getvalue())
            self.assertNotIn(TOKEN, target.getvalue())
        finally:
            log.removeHandler(handler);log.setLevel(old)

    def test_absolute_budget_stops_stream_at_boundary(self):
        now = [0.0]
        class Source(httpx.SyncByteStream):
            def __iter__(self):
                for _ in range(10):
                    now[0] += .4
                    yield b'x'
        progress = {'body_bytes_read': 0}
        stream = upload._DeadlineStream(Source(), 1, lambda: now[0], progress)
        with self.assertRaises(upload.UploadFailure):
            list(stream)
        self.assertEqual(progress['body_bytes_read'], 2)

    def test_negative_timeout_refused(self):
        with self.assertRaises(ValueError):
            upload.upload_document(TOKEN, '1', self.file, '', write_timeout=0)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name)/'backup.tar.gz';self.file.write_bytes(b'fixture')
        self.events=[];self.handles=[];self.logged=[];self.fail_log=False
        outer=self
        class DB:
            def __init__(self):self.closed=False;outer.handles.append(self)
            def cursor(self):return self
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def execute(self,sql,params=None):
                if sql.startswith('INSERT'):
                    if outer.fail_log:raise RuntimeError('fixture log failure')
                    outer.logged.append(params)
            def fetchone(self):return {'bot_token':TOKEN,'chat_id':'main','backup_chat_id':'backup','is_enabled':1}
            def commit(self):outer.events.append('commit')
            def close(self):self.closed=True;outer.events.append('close')
        if os.environ.get('MAX_TELEGRAM_COMPILED') == '1':
            fake=types.ModuleType('database.db');fake.get_connection=lambda:None
            with patch.dict(sys.modules,{'database.db':fake}):
                from services import bot_notifications_service as service
            for name,value in [('ensure_notifications_tables',lambda:None),('_get_db',DB)]:
                patcher=patch.object(service,name,value);patcher.start();self.addCleanup(patcher.stop)
            self.send=service.send_telegram_document
        else:
            tree=ast.parse((ROOT/'services/bot_notifications_service.py').read_text(encoding='utf-8-sig'))
            node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='send_telegram_document')
            ns={'ensure_notifications_tables':lambda:None,'_get_db':DB,'os':os,'json':json}
            exec(compile(ast.Module(body=[node],type_ignores=[]),'notification-service-fixture','exec'),ns)
            self.send=ns[node.name]

    def tearDown(self):self.tmp.cleanup()

    def test_db_read_handle_closed_before_upload_and_log_after(self):
        def transport(token,chat,file,caption):
            self.assertEqual(chat,'backup')
            self.assertTrue(all(d.closed for d in self.handles))
            self.events.append('upload')
            return {'ok':True},{'attempts':1}
        with patch.object(parts,'send_backup_document',transport):ok,msg=self.send(str(self.file))
        self.assertTrue(ok);self.assertTrue(all(d.closed for d in self.handles))
        self.assertLess(self.events.index('close'),self.events.index('upload'))
        self.assertEqual(len(self.logged),1);self.assertEqual(self.logged[0][3],'sent')

    def test_uncertain_failure_logged_once_without_retry(self):
        with patch.object(parts,'send_backup_document',side_effect=upload.UploadFailure('read_timeout','response',{},True)) as call:
            ok,msg=self.send(str(self.file))
        self.assertFalse(ok);self.assertEqual(call.call_count,1)
        self.assertTrue(json.loads(self.logged[0][4])['outcome_uncertain'])
        self.assertTrue(all(d.closed for d in self.handles))

    def test_log_failure_after_accept_keeps_sent_outcome(self):
        self.fail_log=True
        with patch.object(parts,'send_backup_document',return_value=({'ok':True},{})) as call:
            ok,msg=self.send(str(self.file))
        self.assertTrue(ok);self.assertEqual(call.call_count,1)
        self.assertTrue(all(d.closed for d in self.handles))

    def test_explicit_chat_override_preserved(self):
        with patch.object(parts,'send_backup_document',return_value=({'ok':True},{})) as call:
            self.send(str(self.file),override_chat_id='override')
        self.assertEqual(call.call_args.args[1],'override')

    def test_large_archive_reaches_part_sender(self):
        with self.file.open('r+b') as stream: stream.truncate(101*1024*1024)
        with patch.object(parts,'send_backup_document',return_value=({'ok':True},{})) as call:
            self.assertTrue(self.send(str(self.file))[0])
        self.assertEqual(call.call_count,1)

    def test_missing_file_makes_no_network_or_db_read(self):
        with patch.object(parts,'send_backup_document') as call:
            self.assertFalse(self.send(str(self.file)+'missing')[0])
        call.assert_not_called();self.assertEqual(self.handles,[])


if __name__ == '__main__':
    unittest.main()

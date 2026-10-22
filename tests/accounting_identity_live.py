"""Disposable, internally networked FreeRADIUS/MariaDB fixture only.

Invoked by run_accounting_identity_lab.py, never by test discovery.  No app
import, customer dump, customer credentials, external route or published port.
"""
import concurrent.futures
import hashlib
import json
import os
import secrets
import socket
import struct
import sys
import threading
import time
import unittest
from pathlib import Path

import pymysql

OWNER = os.environ.get('MAX_ACCTID_FIXTURE', '')
assert OWNER.startswith('max-acctid-') and len(OWNER) > 20
assert os.environ['DB_HOST'] == OWNER + '-db'
assert os.environ['DB_NAME'] in ('identity_candidate', 'identity_baseline')
SECRET = b'fixture-acctid-only'


def connect():
    conn = pymysql.connect(host=os.environ['DB_HOST'], user='root',
                           password='fixture-acctid-db-only',
                           database=os.environ['DB_NAME'], autocommit=True,
                           charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor)
    with conn.cursor() as q:
        q.execute("SET time_zone='+03:00'")
    return conn


def setup():
    data = json.loads(Path('/fixture/schema.json').read_text())
    c = connect()
    with c.cursor() as q:
        q.execute('SET FOREIGN_KEY_CHECKS=0')
        for ddl in data['tables']:
            q.execute(ddl)
        q.execute('SET FOREIGN_KEY_CHECKS=1')
        q.execute('CREATE TABLE fixture_owner (owner VARCHAR(100) PRIMARY KEY)')
        q.execute('INSERT INTO fixture_owner VALUES (%s)', (OWNER,))
        q.execute(data['GUARD_TABLE_SQL'])
        for name in ['SCHEMA_SQL', 'CHECK_SQL', 'RESERVE_SQL', 'LINK_SQL', 'STOP_SQL']:
            q.execute(data[name])
        # The identity/capacity fixture has no paid account: no artificial
        # contract activation, renewal, wallet or financial settlement.
        q.execute('CREATE FUNCTION fn_freeze_voucher_policy(u VARCHAR(64)) '
                  'RETURNS INT DETERMINISTIC RETURN 1')
        q.execute("INSERT INTO nas(nasname,shortname,type,secret) "
                  "VALUES ('0.0.0.0/0','acctid-fixture','other',%s)", (SECRET.decode(),))
    c.close()


def packet(code, attrs, password=None, expect_response=True):
    authenticator = secrets.token_bytes(16)
    if password is not None:
        padded = password.encode().ljust(16, b'\0')
        mask = hashlib.md5(SECRET + authenticator).digest()
        attrs = attrs + [(2, bytes(a ^ b for a, b in zip(padded, mask)))]
    body = b''.join(bytes((kind, len(value) + 2)) + value for kind, value in attrs)
    header = struct.pack('!BBH', code, secrets.randbelow(256), len(body) + 20)
    if code == 4:
        authenticator = hashlib.md5(header + b'\0' * 16 + body + SECRET).digest()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(3)
        sock.sendto(header + authenticator + body,
                    (os.environ['RADIUS_HOST'], 1812 if code == 1 else 1813))
        try:
            reply = sock.recv(8192)
        except socket.timeout:
            if not expect_response:
                return None
            raise
    assert expect_response, 'Accounting request was acknowledged during failed identity lookup'
    assert reply[1] == header[1]
    assert reply[4:20] == hashlib.md5(reply[:4] + authenticator + reply[20:] + SECRET).digest()
    values = []
    pos = 20
    while pos < len(reply):
        kind, length = reply[pos:pos + 2]
        assert length >= 2
        values.append((kind, reply[pos + 2:pos + length]))
        pos += length
    return reply[0], values


def text(kind, value):
    return kind, str(value).encode()


def number(kind, value):
    return kind, struct.pack('!I', value)


def attrs(user='fixture-a', sid='session-a', port='port-a', nas='10.20.0.1',
          classes=None, status=1, seconds=0, input_bytes=0, output_bytes=0):
    result = [text(1, user), (4, socket.inet_aton(nas)), text(32, 'fixture-nas'),
              text(44, sid), number(5, 42), text(87, port), number(61, 15),
              text(31, '02:00:00:00:00:01'), text(30, 'fixture-hotspot'),
              (8, socket.inet_aton('10.21.0.2')), number(40, status),
              number(46, seconds), number(42, input_bytes & 0xffffffff),
              number(43, output_bytes & 0xffffffff), number(52, input_bytes >> 32),
              number(53, output_bytes >> 32)]
    if status == 2:
        result.append(number(49, 1))
    return result + [text(25, value) for value in classes or []]


TOKEN = 'maxcap:' + 'a' * 32


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.c = connect()
        self.q = self.c.cursor()
        self.q.execute('SELECT owner FROM fixture_owner')
        self.assertEqual(self.q.fetchone()['owner'], OWNER)
        for table in ['radacct', 'radpostauth', 'radcheck', 'radreply',
                      'wisp_session_reservations', 'wisp_license_info',
                      'wisp_revoked_licenses', 'wisp_renewal_guards']:
            self.q.execute('DELETE FROM ' + table)
        self.q.execute("INSERT INTO wisp_license_info(license_id,client_name,hardware_id,"
                       "raw_package_json,signature_b64,status,max_active_sessions) "
                       "VALUES('fixture','fixture','ANY','{}','fixture','active',100)")
        self.q.execute("INSERT INTO wisp_license_runtime_state(id,license_row_id,package_hash,"
                       "is_valid,license_mode,max_active_sessions,valid_until) VALUES"
                       "(1,LAST_INSERT_ID(),SHA2('{}',256),1,'active_sessions',100,"
                       "UTC_TIMESTAMP()+INTERVAL 10 MINUTE) ON DUPLICATE KEY UPDATE "
                       "license_row_id=VALUES(license_row_id),package_hash=VALUES(package_hash),"
                       "is_valid=1,license_mode='active_sessions',max_active_sessions=100,"
                       "valid_until=VALUES(valid_until)")
        for user in ['fixture-a', 'fixture-b', 'fixture-c']:
            self.q.execute("INSERT INTO radcheck(username,attribute,op,value) "
                           "VALUES(%s,'Cleartext-Password',':=','pass')", (user,))

    def tearDown(self):
        self.q.close()
        self.c.close()

    def rows(self):
        self.q.execute('SELECT * FROM radacct ORDER BY radacctid')
        return self.q.fetchall()

    def acct(self, **kw):
        self.assertEqual(packet(4, attrs(**kw))[0], 5)

    def auth(self, user='fixture-a', password='pass', nas='10.20.0.1'):
        return packet(1, [text(1, user), (4, socket.inet_aton(nas)),
                          text(31, '02:00:00:00:00:01'), number(5, 42)], password)

    def count_pending(self):
        self.q.execute('SELECT COUNT(*) n FROM wisp_session_reservations WHERE radacctid IS NULL')
        return self.q.fetchone()['n']

    def seed_old(self, port='port-a', uid=None):
        uid = uid or hashlib.md5(('fixture-a,session-a,10.20.0.1,fixture-nas,' + port + ',42').encode()).hexdigest()
        self.q.execute("INSERT INTO radacct(acctsessionid,acctuniqueid,username,nasipaddress,"
                       "nasportid,callingstationid,acctstarttime,acctupdatetime,acctinputoctets,"
                       "acctoutputoctets,acctsessiontime,class) VALUES('session-a',%s,'fixture-a',"
                       "'10.20.0.1',%s,'02:00:00:00:00:01',NOW(),NOW(),100,200,10,%s)",
                       (uid, port, TOKEN))
        return uid

    def test_changed_port_start_interim_stop(self):
        self.acct(classes=[TOKEN])
        self.acct(classes=[TOKEN], port='port-b', status=3, seconds=60, input_bytes=1000, output_bytes=2000)
        self.acct(classes=[TOKEN], port='port-c', status=2, seconds=90, input_bytes=2000, output_bytes=4000)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['acctinputoctets'], rows[0]['acctoutputoctets'], rows[0]['acctsessiontime']), (2000, 4000, 90))
        self.assertIsNotNone(rows[0]['acctstoptime'])

    def test_same_port(self):
        self.acct(classes=[TOKEN]); self.acct(classes=[TOKEN], status=3, seconds=60, input_bytes=1000)
        self.assertEqual(len(self.rows()), 1)

    def test_different_router_same_class_and_session(self):
        self.acct(classes=[TOKEN]); self.acct(classes=[TOKEN], nas='10.20.0.2')
        self.assertEqual(len(self.rows()), 2)

    def test_different_user_same_class_and_session(self):
        self.acct(classes=[TOKEN]); self.acct(classes=[TOKEN], user='fixture-b')
        self.assertEqual(len(self.rows()), 2)

    def test_different_session_same_class(self):
        self.acct(classes=[TOKEN]); self.acct(classes=[TOKEN], sid='session-b')
        self.assertEqual(len(self.rows()), 2)

    def test_reauthentication_new_class_same_session(self):
        self.acct(classes=[TOKEN]); self.acct(classes=['maxcap:' + 'b' * 32])
        self.assertEqual(len(self.rows()), 2)

    def test_single_legacy_row_preserves_identity(self):
        uid = self.seed_old()
        self.acct(classes=[TOKEN], status=3, port='port-b', seconds=60, input_bytes=1000)
        rows = self.rows(); self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['acctuniqueid'], uid); self.assertEqual(rows[0]['acctinputoctets'], 1000)

    def test_legacy_late_stop_keeps_closed_row(self):
        uid = self.seed_old()
        self.q.execute('UPDATE radacct SET acctstoptime=NOW()')
        self.acct(classes=[TOKEN], port='port-c', status=2, seconds=70, input_bytes=900)
        rows = self.rows(); self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['acctuniqueid'], uid); self.assertEqual(rows[0]['acctinputoctets'], 900)

    def test_ambiguous_legacy_rows_are_not_merged(self):
        a = self.seed_old(); b = self.seed_old('port-b')
        self.acct(classes=[TOKEN], port='port-b', status=3, seconds=60, input_bytes=1000)
        rows = self.rows(); self.assertEqual(len(rows), 2)
        self.assertEqual({r['acctuniqueid'] for r in rows}, {a, b})
        self.assertEqual(rows[0]['acctinputoctets'], 100); self.assertEqual(rows[1]['acctinputoctets'], 1000)

    def test_absent_class_keeps_stock_policy(self):
        self.acct(); self.acct(port='port-b', status=3, input_bytes=100)
        self.assertEqual(len(self.rows()), 2)

    def test_invalid_maxcap_keeps_stock_policy(self):
        for value in ['maxcap:bad', TOKEN + 'x', TOKEN + "'", TOKEN + '\n', 'maxcap:' + 'G' * 32]:
            self.q.execute('DELETE FROM radacct')
            self.acct(classes=[value]); self.acct(classes=[value], port='port-b', status=3, input_bytes=100)
            self.assertEqual(len(self.rows()), 2)

    def test_multiple_classes_keep_stock_policy(self):
        self.acct(classes=[TOKEN, 'other']); self.acct(classes=[TOKEN, 'other'], port='port-b', status=3, input_bytes=100)
        self.assertEqual(len(self.rows()), 2)

    def test_ai_policy_unchanged(self):
        cls = 'ai:' + 'c' * 32
        self.acct(classes=[cls]); self.acct(classes=[cls], port='port-b', status=3, input_bytes=100)
        self.assertEqual(len(self.rows()), 1)

    def test_64bit_reordered_duplicate_interim_and_stop(self):
        self.acct(classes=[TOKEN])
        self.acct(classes=[TOKEN], status=3, port='port-b', seconds=120, input_bytes=2**32 + 100, output_bytes=2**33 + 200)
        self.acct(classes=[TOKEN], status=3, port='port-c', seconds=60, input_bytes=50, output_bytes=100)
        self.acct(classes=[TOKEN], status=2, port='port-d', seconds=180, input_bytes=2**32 + 300, output_bytes=2**33 + 400)
        self.acct(classes=[TOKEN], status=2, port='port-b', seconds=90, input_bytes=1, output_bytes=1)
        r = self.rows(); self.assertEqual(len(r), 1)
        self.assertEqual((r[0]['acctinputoctets'], r[0]['acctoutputoctets'], r[0]['acctsessiontime']), (2**32 + 300, 2**33 + 400, 180))

    def test_interim_without_start(self):
        self.acct(classes=[TOKEN], status=3, port='port-b', seconds=60, input_bytes=100)
        self.acct(classes=[TOKEN], status=2, port='port-c', seconds=90, input_bytes=200)
        self.assertEqual(len(self.rows()), 1); self.assertIsNotNone(self.rows()[0]['acctstoptime'])

    def test_capacity_link_and_stop_release_native(self):
        self.q.execute('UPDATE wisp_license_runtime_state SET max_active_sessions=1')
        code, reply = self.auth(); self.assertEqual(code, 2)
        token = next(v.decode() for k, v in reply if k == 25)
        self.assertTrue(token.startswith('maxcap:')); self.assertEqual(self.count_pending(), 1)
        self.acct(classes=[token]); self.assertEqual(self.count_pending(), 0)
        self.acct(classes=[token], port='port-b', status=3, seconds=60, input_bytes=100)
        self.assertEqual(len(self.rows()), 1); self.assertEqual(self.auth('fixture-b')[0], 3)
        self.acct(classes=[token], port='port-c', status=2, seconds=90, input_bytes=200)
        self.q.execute('SELECT COUNT(*) n FROM wisp_session_reservations'); self.assertEqual(self.q.fetchone()['n'], 0)
        self.assertEqual(self.auth('fixture-b')[0], 2)

    def test_invalid_lease_denies_auth(self):
        self.q.execute('UPDATE wisp_license_runtime_state SET valid_until=UTC_TIMESTAMP()-INTERVAL 1 SECOND')
        self.assertEqual(self.auth()[0], 3); self.assertEqual(self.count_pending(), 0)

    def test_invalid_credentials_do_not_reserve(self):
        self.assertEqual(self.auth(password='wrong')[0], 3); self.assertEqual(self.count_pending(), 0)

    def test_revocation_and_package_mismatch_deny_auth(self):
        self.q.execute("UPDATE wisp_license_info SET status='revoked'")
        self.assertEqual(self.auth()[0], 3)
        self.q.execute("UPDATE wisp_license_info SET status='active',raw_package_json='{\"changed\":true}'")
        self.assertEqual(self.auth()[0], 3); self.assertEqual(self.count_pending(), 0)

    def test_renewal_guard_is_not_bypassed(self):
        self.q.execute("INSERT INTO wisp_renewal_guards VALUES('fixture-a',%s,NOW()+INTERVAL 1 MINUTE)", ('e' * 32,))
        self.assertEqual(self.auth()[0], 3); self.assertEqual(self.count_pending(), 0)

    def test_concurrent_native_capacity_is_bounded(self):
        self.q.execute('UPDATE wisp_license_runtime_state SET max_active_sessions=3')
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            codes = list(pool.map(lambda _: self.auth()[0], range(12)))
        self.assertEqual(codes.count(2), 3); self.assertEqual(codes.count(3), 9)
        self.assertEqual(self.count_pending(), 3)

    def test_concurrent_first_starts_share_one_identity(self):
        def send(i):
            return packet(4, attrs(classes=[TOKEN], port='port-' + str(i)))[0]
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            self.assertEqual(list(pool.map(send, range(12))), [5] * 12)
        self.assertEqual(len(self.rows()), 1)
        self.acct(classes=[TOKEN], status=3, port='last-port', seconds=60, input_bytes=100)
        self.assertEqual(len(self.rows()), 1)

    def test_quote_and_delimiter_attributes(self):
        for i, user in enumerate(["fixture-'quoted", 'fixture-a,b', 'fixture-back\\slash']):
            self.acct(user=user, sid='session,' + str(i), classes=[TOKEN])
            self.acct(user=user, sid='session,' + str(i), classes=[TOKEN], port='port-b', status=3, input_bytes=100)
        self.assertEqual(len(self.rows()), 3)
        self.q.execute('SELECT owner FROM fixture_owner'); self.assertEqual(self.q.fetchone()['owner'], OWNER)

    def test_concurrent_reordered_interims(self):
        self.acct(classes=[TOKEN])
        def send(i):
            return packet(4, attrs(classes=[TOKEN], port='port-' + str(i), status=3,
                                   seconds=i * 10, input_bytes=i * 100, output_bytes=i * 200))[0]
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            self.assertEqual(list(pool.map(send, range(1, 13))), [5] * 12)
        rows = self.rows(); self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['acctinputoctets'], rows[0]['acctoutputoctets']), (1200, 2400))

    def test_failed_lookup_does_not_ack_or_insert(self):
        self.q.execute('RENAME TABLE radacct TO fixture_radacct_unavailable')
        try:
            self.assertIsNone(packet(4, attrs(classes=[TOKEN]), expect_response=False))
        finally:
            self.q.execute('RENAME TABLE fixture_radacct_unavailable TO radacct')
        self.assertEqual(len(self.rows()), 0)
        self.acct(classes=[TOKEN]); self.assertEqual(len(self.rows()), 1)

    def test_invalid_legacy_identity_does_not_ack_or_fork(self):
        self.seed_old(uid='g' * 32)
        self.assertIsNone(packet(4, attrs(classes=[TOKEN], port='port-b', status=3,
                                        seconds=60, input_bytes=1000), expect_response=False))
        rows = self.rows(); self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['acctinputoctets'], 100)


def baseline():
    case = IdentityTests('test_changed_port_start_interim_stop')
    case.setUp()
    try:
        case.acct(classes=[TOKEN]); case.acct(classes=[TOKEN], port='port-b', status=3, seconds=60, input_bytes=1000)
        rows = case.rows()
        assert len(rows) == 2 and rows[0]['acctinputoctets'] == 0 and rows[1]['acctinputoctets'] == 1000
        print(json.dumps({'baseline_bug_reproduced': True, 'rows': len(rows)}))
    finally:
        case.tearDown()


def restart_check(resume=False):
    case = IdentityTests('test_same_port')
    if not resume:
        case.setUp()
        try:
            case.acct(classes=[TOKEN])
            case.acct(classes=[TOKEN], port='port-b', status=3, seconds=60, input_bytes=100)
            row = case.rows()[0]
            Path('/fixture/restart-before.json').write_text(json.dumps({'id': row['radacctid'], 'unique': row['acctuniqueid']}))
        finally:
            case.tearDown()
    else:
        case.c = connect(); case.q = case.c.cursor()
        try:
            before = json.loads(Path('/fixture/restart-before.json').read_text())
            case.acct(classes=[TOKEN], port='port-c', status=3, seconds=120, input_bytes=200)
            case.acct(classes=[TOKEN], port='port-d', status=2, seconds=180, input_bytes=300)
            rows = case.rows(); assert len(rows) == 1
            assert rows[0]['radacctid'] == before['id'] and rows[0]['acctuniqueid'] == before['unique']
            assert rows[0]['acctinputoctets'] == 300 and rows[0]['acctstoptime'] is not None
            print(json.dumps({'restart_continuation_and_stop_passed': True}))
        finally:
            case.tearDown()


def stress():
    """Bounded synthetic NAS identities, no physical-router claim."""
    case = IdentityTests('test_same_port')
    case.setUp()
    samples = []
    lock = threading.Lock()
    count = 128
    started = time.monotonic()
    try:
        case.q.execute('UPDATE wisp_license_runtime_state SET max_active_sessions=%s', (count,))
        for i in range(16):
            case.q.execute("INSERT INTO radcheck(username,attribute,op,value) "
                           "VALUES(%s,'Cleartext-Password',':=','pass')", ('fixture-stress-' + str(i),))
        def timed(call):
            before = time.monotonic()
            value = call()
            with lock:
                samples.append((time.monotonic() - before) * 1000)
            return value
        def authorize(i):
            user = 'fixture-stress-' + str(i % 16)
            nas = '10.20.0.' + str(1 + i % 8)
            code, reply = timed(lambda: case.auth(user=user, nas=nas))
            assert code == 2
            cls = next(v.decode() for k, v in reply if k == 25)
            assert cls.startswith('maxcap:') and len(cls) == 39
            return {'user': user, 'nas': nas, 'sid': 'shared-session-' + str(i // 8), 'classes': [cls]}
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            sessions = list(pool.map(authorize, range(count)))
        assert case.count_pending() == count
        assert timed(lambda: case.auth('fixture-a'))[0] == 3
        def batch(status, seconds, multiplier, offset):
            def send(item):
                i, session = item
                values = dict(session, status=status, seconds=seconds,
                              port='changing-interface-' + str(offset + i % 5),
                              input_bytes=2**32 + multiplier * (i + 1),
                              output_bytes=2**33 + 2 * multiplier * (i + 1))
                assert timed(lambda: packet(4, attrs(**values)))[0] == 5
            with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
                list(pool.map(send, enumerate(sessions)))
        batch(1, 0, 0, 0)
        assert len(case.rows()) == count and case.count_pending() == 0
        assert timed(lambda: case.auth('fixture-a'))[0] == 3
        before_ids = {r['radacctid']: r['acctuniqueid'] for r in case.rows()}
        for step in range(1, 6):
            batch(3, step * 60, step * 1000, step)
        batch(3, 60, 100, 21)  # deliberate old/low counters
        batch(2, 360, 6000, 31)
        batch(2, 120, 10, 41)  # duplicate late Stop must not subtract
        rows = case.rows()
        assert len(rows) == count and all(r['acctstoptime'] is not None for r in rows)
        assert {r['radacctid']: r['acctuniqueid'] for r in rows} == before_ids
        by_class = {r['class']: r for r in rows}
        for i, session in enumerate(sessions):
            row = by_class[session['classes'][0]]
            assert (row['acctinputoctets'], row['acctoutputoctets'], row['acctsessiontime']) == (2**32 + 6000 * (i + 1), 2**33 + 12000 * (i + 1), 360)
        case.q.execute('SELECT COUNT(*) n FROM wisp_session_reservations')
        assert case.q.fetchone()['n'] == 0
        assert timed(lambda: case.auth('fixture-a'))[0] == 2
        latency = sorted(samples)
        summary = {'successful': True, 'simulated_nas_identities': 8,
                   'accounts': 16, 'simultaneous_sessions': count, 'workers': 16,
                   'signed_udp_responses_verified': len(samples),
                   'duration_seconds': round(time.monotonic() - started, 3),
                   'response_ms_p50': round(latency[len(latency) // 2], 3),
                   'response_ms_p95': round(latency[int(len(latency) * .95)], 3),
                   'response_ms_max': round(max(latency), 3),
                   'sql_rows_exact': count, 'genuine_stop_packets_sent': count * 2,
                   'final_stopped_rows': count, 'pending_or_linked_reservations_after_stops': 0,
                   'all_rows_and_unique_ids_preserved': True,
                   'all_64bit_counters_exact': True,
                   'physical_nas_or_roaming_test': False}
        Path('/fixture/stress-result.json').write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary))
    finally:
        case.tearDown()


if __name__ == '__main__':
    if sys.argv[1] == '--setup':
        setup()
        print(json.dumps({'synthetic_fixture_initialized': True}))
    elif sys.argv[1] == '--baseline':
        baseline()
    elif sys.argv[1] == '--open-before-restart':
        restart_check()
    elif sys.argv[1] == '--resume-after-restart':
        restart_check(resume=True)
    elif sys.argv[1] == '--stress':
        stress()
    else:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(IdentityTests)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        Path('/fixture/result.json').write_text(json.dumps({
            'tests': result.testsRun, 'failures': len(result.failures),
            'errors': len(result.errors), 'successful': result.wasSuccessful(),
            'limits': ['Native signed RADIUS packets, not a physical NAS/roaming acceptance.',
                       'Capacity uses actual SQL functions/triggers; lease state is synthetic.',
                       'Paid contract freeze is fixture-neutral, no renewal/financial acceptance.']}, indent=2))
        sys.exit(0 if result.wasSuccessful() else 1)

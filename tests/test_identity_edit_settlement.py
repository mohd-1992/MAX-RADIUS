"""Identity edits with historical uncertainty: SQLite SQL and simulated NAS only."""
import ast
import contextlib
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.datetime(2026, 11, 2, 16)


def load(name, path):
    target = ROOT / path
    if os.environ.get('MAX_TEST_COMPILED_IDENTITY') == '1':
        name = '.'.join(Path(path).with_suffix('').parts)
        target = next(Path('/app').glob(str(Path(path).with_suffix('')) + '*.so'))
    spec = importlib.util.spec_from_file_location(name, target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bind_database(module, database):
    # Extension modules can be cached by CPython across fixture instances.
    for name in ('query_all', 'query_one', 'get_connection', 'db_session',
                 'is_mysql_conn', 'adapt_query', 'execute_write'):
        if hasattr(module, name): setattr(module, name, getattr(database, name))


class Cursor:
    def __init__(self, db):
        self.db, self.cursor, self.synthetic = db, db.cursor(), None

    def __enter__(self): return self
    def __exit__(self, *args): self.cursor.close()

    def execute(self, sql, args=()):
        self.synthetic = None
        if 'GET_LOCK(' in sql:
            self.synthetic = {'acquired': 1}
            return
        if 'RELEASE_LOCK(' in sql:
            self.synthetic = {'released': 1}
            return
        sql = sql.replace('%s', '?').replace(' FOR UPDATE', '')
        sql = sql.replace('NOW()+INTERVAL 5 MINUTE', "'2026-11-02 16:05:00'")
        sql = sql.replace('NOW()-INTERVAL 5 MINUTE', "'2026-11-02 15:55:00'")
        if 'ON DUPLICATE KEY UPDATE token=' in sql:
            sql = sql.split('ON DUPLICATE KEY UPDATE')[0] + 'ON CONFLICT(username) DO UPDATE SET token=excluded.token,expires_at=excluded.expires_at'
        else:
            sql = re.sub(r'ON DUPLICATE KEY UPDATE (\w+)=VALUES\(\1\)',
                         r'ON CONFLICT(username) DO UPDATE SET \1=excluded.\1', sql)
        self.cursor.execute(sql, args)

    def fetchone(self):
        if self.synthetic is not None: return self.synthetic
        row = self.cursor.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self): return [dict(row) for row in self.cursor.fetchall()]


class IdentityEditTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.create_function('NOW', 0, lambda: str(NOW))
        fields = set('username password full_name phone national_id address notes service_type mac_binding static_ip bound_mac pin_code status expires_at last_renewed_at first_used_at created_at'.split())
        fields.update('package_id balance extra_quota_mb loan_balance_mb snap_volume_quota_mb snap_uptime_limit_mins policy_frozen_at snap_validity_value snap_validity_unit snap_rate_download snap_rate_upload snap_rate_limit_str'.split())
        for table in ('wisp_subscribers', 'wisp_vouchers'):
            self.db.execute('CREATE TABLE '+table+'(id INTEGER PRIMARY KEY,'+','.join('"'+f+'"' for f in fields)+')')
        for kind in ('subscriber', 'voucher'):
            self.db.execute(f'CREATE TABLE wisp_{kind}_names(username TEXT PRIMARY KEY,{kind}_id INTEGER)')
            self.db.execute(f'CREATE TABLE wisp_{kind}_edit_receipts(operation_id TEXT PRIMARY KEY,{kind}_id,actor,fingerprint,result)')
        for table in ('radacct', 'radacct_archive', 'wisp_imported_session_history', 'wisp_traffic_history'):
            self.db.execute('CREATE TABLE '+table+'(radacctid INTEGER PRIMARY KEY,username,account_username,account_subscriber_id,account_voucher_id,acctuniqueid,nasipaddress,acctsessionid,framedipaddress,callingstationid,acctstarttime,acctstoptime,acctterminatecause,acctinputoctets,acctoutputoctets,acctsessiontime)')
        self.db.executescript('''
            CREATE VIEW radacct_owned AS SELECT radacctid,COALESCE(account_username,username) AS username,
              nasipaddress,acctsessionid,framedipaddress,callingstationid,acctstarttime,acctstoptime,acctterminatecause FROM radacct;
            CREATE TABLE radpostauth(username,nasipaddress,callingstationid,authdate,reply);
            CREATE TABLE wisp_nas_session_seals(radacctid,nasipaddress,acctsessionid);
            CREATE TABLE wisp_session_reservations(username,radacctid,expires_at);
            CREATE TABLE wisp_username_write_lock(id); INSERT INTO wisp_username_write_lock VALUES(1);
            CREATE TABLE wisp_license_runtime_state(id); INSERT INTO wisp_license_runtime_state VALUES(1);
            CREATE TABLE wisp_renewal_guards(username PRIMARY KEY,token,expires_at);
            CREATE TABLE wisp_online_policy_outbox(username,status,last_error);
            CREATE TABLE wisp_nas_devices(ip_address,api_port,api_username,api_password,secret);
            INSERT INTO wisp_nas_devices VALUES('192.0.2.1',8728,'fixture','fixture','fixture');
            INSERT INTO wisp_nas_devices VALUES('192.0.2.2',8728,'fixture','fixture','fixture');
            CREATE TABLE wisp_loyalty_wallets(id INTEGER PRIMARY KEY,username,points_balance);
            CREATE TABLE user_audit_logs(user_type,user_id,username,admin_name,action,change_details);
            CREATE TABLE wisp_audit_logs(admin_id,username,action,module,details);
        ''')
        for table in ('radcheck', 'radreply', 'radusergroup'):
            self.db.execute('CREATE TABLE '+table+'(id INTEGER PRIMARY KEY,username,attribute,value)')
        self.conn = types.SimpleNamespace(cursor=lambda: Cursor(self.db), commit=self.db.commit,
                                          rollback=self.db.rollback, close=lambda: None)
        @contextlib.contextmanager
        def session():
            try:
                yield self.conn
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        self.dbmod = types.ModuleType('database.db')
        self.dbmod.db_session = session
        self.dbmod.get_connection = lambda: self.conn
        self.dbmod.is_mysql_conn = lambda conn: False
        self.dbmod.adapt_query = lambda sql, conn: sql
        self.dbmod.query_all = self.q
        self.dbmod.query_one = lambda sql, args=(): next(iter(self.q(sql, args)), None)
        self.dbmod.execute_write = lambda sql, args=(): self.q(sql, args)
        self.patchers = [patch.dict(sys.modules, {'database.db': self.dbmod})]
        self.patchers[0].start()
        self.settlement = load('_identity_settlement_test', 'services/renewal_settlement_service.py')
        bind_database(self.settlement, self.dbmod)
        self.settlement.is_mysql_conn = lambda c: True
        self.settlement.current_radius_container = Mock()
        self.settlement.WAIT_SECONDS = 0
        self.settlement._disconnect = Mock()
        self.nas = types.ModuleType('services.nas_session_settlement')
        self.nas.actual_stop = lambda r: bool(r.get('acctstoptime') and r.get('acctterminatecause') not in (None, '', 'NAS-Verified-Closed') + self.settlement.NON_FINAL_CAUSES)
        self.nas.disconnect_for_identity = Mock()
        self.nas.recover_journals = Mock()
        self.visited, self.live, self.unreachable = [], {}, set()
        self.login_ok = True
        test = self
        class NAS:
            def __init__(self, ip, **kw): self.ip = ip
            def connect(self):
                test.visited.append(self.ip)
                if self.ip in test.unreachable: raise TimeoutError('fixture unreachable')
            def login(self, *args): return test.login_ok
            def execute_command(self, path, words=None): return test.live.get((self.ip, path), [])
            def close(self): pass
        api = types.ModuleType('core.mikrotik_api'); api.RouterOSApiProtocol = NAS
        lock = types.ModuleType('services.account_lock_service'); lock.account_lock_name = lambda *a: 'fixture-lock'
        if os.environ.get('MAX_TEST_COMPILED_IDENTITY') == '1':
            self.policy = load('_identity_policy_test', 'services/online_policy_service.py')
            bind_database(self.policy, self.dbmod)
        else:
            self.policy = types.ModuleType('services.online_policy_service')
            ns = self.policy.__dict__
            ns.update(query_all=self.q, query_one=self.dbmod.query_one, local=types.SimpleNamespace(), time=time)
            tree = ast.parse((ROOT/'services/online_policy_service.py').read_text(encoding='utf-8'))
            nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ('fail', 'checkpoint', 'view', 'rows_for', 'seconds')]
            for n in nodes: n.decorator_list = []
            exec(compile(ast.Module(nodes, []), 'online_policy_service.py', 'exec'), ns)
            self.policy.view = contextlib.contextmanager(self.policy.view)
        self.policy.preflight = Mock(); self.policy.intent = Mock(); self.policy.refresh_or_pending = Mock()
        account_mac = types.ModuleType('services.account_mac_service'); account_mac.reject_legacy_mac_change = Mock()
        sub = types.ModuleType('services.subscriber_service')
        def update(ident, data, **kwargs):
            self.q('UPDATE wisp_subscribers SET '+','.join(k+'=?' for k in data)+' WHERE id=?', tuple(data.values())+(ident,))
        sub.update_subscriber = update
        radius = types.ModuleType('core.radius_sync'); radius.sync_voucher_to_radius = Mock(return_value=True)
        mocks = {'services.renewal_settlement_service': self.settlement, 'services.nas_session_settlement': self.nas,
                 'services.online_policy_service': self.policy, 'core.mikrotik_api': api,
                 'services.account_lock_service': lock, 'services.account_mac_service': account_mac,
                 'services.subscriber_service': sub, 'core.radius_sync': radius}
        self.patchers.append(patch.dict(sys.modules, mocks)); self.patchers[-1].start()
        self.sub = load('_identity_sub_test', 'services/subscriber_edit_service.py')
        self.card = load('_identity_card_test', 'services/voucher_edit_service.py')
        bind_database(self.sub, self.dbmod)
        bind_database(self.card, self.dbmod)

    def tearDown(self):
        for p in reversed(self.patchers): p.stop()
        self.db.close()

    def q(self, sql, args=()):
        with Cursor(self.db) as c:
            c.execute(sql, args)
            return c.fetchall() if c.cursor.description else []

    def account(self, kind='subscriber', status='expired'):
        table = 'wisp_subscribers' if kind == 'subscriber' else 'wisp_vouchers'
        self.q('INSERT INTO '+table+'(id,username,password,pin_code,status,service_type,expires_at,created_at,first_used_at,last_renewed_at,balance,package_id) VALUES(1,?,?,?,?,?,?,?,?,?,?,?)',
               ('fixture-old', 'old-password', 'old-password', status, 'hotspot', '2026-11-02 11:00:00', '2026-10-01', '2026-10-01', '2026-10-03', 123.45, 1))
        self.q("INSERT INTO radacct(radacctid,username,nasipaddress,acctsessionid,acctstoptime,acctterminatecause,acctinputoctets,acctoutputoctets) VALUES(1,'fixture-old','192.0.2.1','old-sid','2026-11-01 12:00:00','Watchdog-Autoheal-Timeout',17,31)")
        self.q("INSERT INTO radpostauth VALUES('fixture-old','192.0.2.1','mac','2026-11-01 10:00:00','Access-Accept')")
        self.q("INSERT INTO wisp_loyalty_wallets VALUES(1,'fixture-old',50)")
        self.db.commit()
        return self.q('SELECT * FROM '+table)[0]

    def edit(self, kind, changes):
        module = self.sub if kind == 'subscriber' else self.card
        table = 'wisp_subscribers' if kind == 'subscriber' else 'wisp_vouchers'
        row = self.q('SELECT * FROM '+table)[0]
        request = dict(operation_id='a'*32, expected_revision=module.revision(row))
        request.update(changes)
        fn = module.edit_subscriber if kind == 'subscriber' else module.edit_voucher
        return fn(1, request, actor='fixture-admin')

    def test_old_history_does_not_block_subscriber_identity_fields(self):
        for field, value in [('username', 'fixture-new'), ('mac_binding', '02:00:00:00:00:01'), ('static_ip', '192.0.2.8'), ('service_type', 'pppoe')]:
            with self.subTest(field=field):
                if self.q('SELECT * FROM wisp_subscribers'):
                    self.tearDown(); self.setUp()
                before = self.account()
                raw = self.q('SELECT username,acctterminatecause,acctinputoctets,acctoutputoctets FROM radacct')
                self.assertTrue(self.settlement.has_unaccounted_accept('fixture-old'))
                result = self.edit('subscriber', {field: value, 'password': 'new-password'})
                self.assertTrue(result['success'])
                after = self.q('SELECT * FROM wisp_subscribers')[0]
                self.assertEqual(after[field], value)
                self.assertEqual(after['password'], 'new-password')
                for key in ('status', 'expires_at', 'last_renewed_at', 'balance', 'package_id'):
                    self.assertEqual(before[key], after[key], key)
                self.assertEqual(raw, self.q('SELECT username,acctterminatecause,acctinputoctets,acctoutputoctets FROM radacct'))
                self.assertEqual(set(self.visited), {'192.0.2.1', '192.0.2.2'})

    def test_voucher_name_and_mac_with_old_history(self):
        self.account('voucher')
        self.assertTrue(self.edit('voucher', {'username': 'fixture-new', 'password': 'new-password'})['success'])
        self.assertTrue(self.edit('voucher', {'bound_mac': '02:00:00:00:00:01', 'operation_id': 'b'*32})['success'])

    def test_unreachable_nas_rejects_all_sensitive_subscriber_fields(self):
        before = self.account(); self.unreachable.add('192.0.2.2')
        for changes in ({'username': 'fixture-new'}, {'mac_binding': '02:00:00:00:00:01'}, {'static_ip': '192.0.2.8'}, {'service_type': 'pppoe'}):
            with self.subTest(changes=changes), self.assertRaises(self.settlement.SettlementPending) as error:
                self.edit('subscriber', changes)
            self.assertNotIn('التجديد', str(error.exception))
            self.assertIn('لم تُحفظ', str(error.exception))
            self.assertEqual(before, self.q('SELECT * FROM wisp_subscribers')[0])
            self.assertFalse(self.q('SELECT * FROM wisp_renewal_guards'))

    def test_unknown_live_session_is_not_assumed_offline(self):
        self.account()
        self.live[('192.0.2.2', '/ip/hotspot/active/print')] = [{'user': 'fixture-old', 'address': '192.0.2.7', 'mac-address': '02:00:00:00:00:01'}]
        with self.assertRaises(self.settlement.SettlementPending): self.edit('subscriber', {'username': 'fixture-new'})
        self.nas.disconnect_for_identity.assert_not_called()

    def test_current_open_or_unsealed_session_blocks_scoped_wait(self):
        self.account()
        self.q("INSERT INTO radacct(radacctid,username,nasipaddress,acctsessionid,acctterminatecause) VALUES(2,'fixture-old','192.0.2.1','live-sid','Watchdog-Autoheal-Timeout')")
        with self.assertRaises(self.settlement.SettlementPending): self.settlement.wait_for_final_accounting('fixture-old', session_ids=(2,))
        self.q("UPDATE radacct SET acctstoptime='2026-11-02 16:00:00' WHERE radacctid=2")
        with self.assertRaises(self.settlement.SettlementPending): self.settlement.wait_for_final_accounting('fixture-old', session_ids=(2,))
        self.q("INSERT INTO wisp_nas_session_seals VALUES(2,'192.0.2.1','live-sid')")
        with self.assertRaises(self.settlement.SettlementPending):
            self.settlement.wait_for_final_accounting('fixture-old', session_ids=(2,))
        self.q("UPDATE radacct SET acctterminatecause='User-Request' WHERE radacctid=2")
        self.settlement.wait_for_final_accounting('fixture-old', session_ids=(2,))
        self.settlement._disconnect.assert_not_called()

    def test_current_final_stop_passes_and_missing_generation_rejects(self):
        self.account()
        self.q("UPDATE radacct SET acctterminatecause='User-Request' WHERE radacctid=1")
        self.settlement.wait_for_final_accounting('fixture-old', session_ids=(1,))
        with self.assertRaises(self.settlement.SettlementPending): self.settlement.wait_for_final_accounting('fixture-old', session_ids=(99,))

    def test_multiple_captured_generations_bind_individually(self):
        self.account()
        self.q("UPDATE radacct SET acctterminatecause='User-Request' WHERE radacctid=1")
        for ident in (2, 3):
            self.q("INSERT INTO radacct(radacctid,username,nasipaddress,acctsessionid,acctstoptime,acctterminatecause) VALUES(?,'fixture-old','192.0.2.1',?,'2026-11-02 16:00:00','User-Request')", (ident, 'closed-'+str(ident)))
        self.assertEqual(self.settlement.identity_pending_sessions('fixture-old', (3, 2, 1, 2)), [])
        self.q('UPDATE radacct SET acctstoptime=NULL WHERE radacctid=3')
        self.assertEqual([r['radacctid'] for r in self.settlement.identity_pending_sessions('fixture-old', (1, 2, 3))], [3])
        self.q("INSERT INTO radacct(radacctid,username,nasipaddress,acctsessionid,acctstoptime,acctterminatecause) VALUES(4,'other-owner','192.0.2.1','foreign','2026-11-02 16:00:00','User-Request')")
        with self.assertRaises(self.settlement.SettlementPending):
            self.settlement.identity_pending_sessions('fixture-old', (1, 2, 4))

    def test_pending_reservation_still_blocks_offline_identity(self):
        self.account()
        self.q("INSERT INTO wisp_session_reservations VALUES('fixture-old',NULL,'2026-11-02 17:00:00')")
        with self.assertRaises(self.settlement.SettlementPending): self.edit('subscriber', {'username': 'fixture-new'})

    def test_financial_default_still_rejects_historical_uncertainty(self):
        self.account()
        with self.assertRaises(self.settlement.SettlementPending): self.settlement.wait_for_final_accounting('fixture-old')
        self.settlement._disconnect.assert_called_once()

    def test_password_only_avoids_nas_and_financial_boundary(self):
        self.account(); self.unreachable.update(('192.0.2.1', '192.0.2.2'))
        self.assertTrue(self.edit('subscriber', {'password': 'new-password'})['success'])
        self.assertFalse(self.visited)

    def test_identity_does_not_probe_or_freeze_existing_limits(self):
        self.account()
        with patch.object(self.policy, 'preflight', side_effect=AssertionError('Financial preflight entered')):
            self.assertTrue(self.edit('subscriber', {'service_type':'pppoe'})['success'])
        self.nas.disconnect_for_identity.assert_called_once()

    def test_legacy_service_preserved_for_unrelated_edit(self):
        self.account()
        self.q("UPDATE wisp_subscribers SET service_type='broadband' WHERE id=1")
        self.assertTrue(self.edit('subscriber', {'full_name':'fixture-changed'})['success'])
        self.assertEqual(self.q('SELECT service_type FROM wisp_subscribers')[0]['service_type'],'broadband')
        self.assertFalse(self.visited)

    def test_failed_nas_login_rejects_identity(self):
        self.account(); self.login_ok = False
        with self.assertRaises(self.settlement.SettlementPending): self.edit('subscriber', {'username': 'fixture-new'})
        self.assertEqual(self.q('SELECT username FROM wisp_subscribers')[0]['username'], 'fixture-old')

    def test_conflicting_name_rejected_before_any_nas_action(self):
        self.account()
        self.q("INSERT INTO wisp_vouchers(id,username) VALUES(2,'fixture-new')")
        with self.assertRaises(ValueError): self.edit('subscriber', {'username': 'fixture-new'})
        self.assertFalse(self.visited)

    def test_current_sessions_must_close_with_evidence_before_contract_save(self):
        self.account()
        self.q("INSERT INTO radacct(radacctid,username,nasipaddress,acctsessionid) VALUES(2,'fixture-old','192.0.2.1','live-sid')")
        # Compiled modules are cached across fixtures. Restore this override
        # before another case exercises the real NAS checkpoint.
        self.patchers.append(patch.object(self.policy, 'checkpoint', return_value={2: {'kind': 'hotspot'}}))
        self.patchers[-1].start()
        before = self.q('SELECT * FROM wisp_subscribers')[0]
        # An accepted disconnect alone provides neither Stop nor a counter seal.
        with self.assertRaises(self.settlement.SettlementPending): self.edit('subscriber', {'username': 'fixture-new'})
        self.assertEqual(before, self.q('SELECT * FROM wisp_subscribers')[0])
        def sealed(snapshot, **kw):
            self.q("UPDATE radacct SET acctstoptime='2026-11-02 16:00:00',acctterminatecause='User-Request' WHERE radacctid=2")
            # The real identity path keeps captured IDs until actual Stop is verified.
        self.nas.disconnect_for_identity.side_effect = sealed
        self.policy.checkpoint.return_value = {2: {'kind': 'hotspot'}}
        self.assertTrue(self.edit('subscriber', {'username': 'fixture-new'})['success'])
        self.settlement._disconnect.assert_not_called()

    def test_receipt_retry_does_not_repeat_identity_side_effects(self):
        self.account()
        row = self.q('SELECT * FROM wisp_subscribers')[0]
        data = dict(operation_id='a'*32, expected_revision=self.sub.revision(row), username='fixture-new')
        first = self.sub.edit_subscriber(1, data)
        visits, freezes = len(self.visited), self.nas.disconnect_for_identity.call_count
        self.assertEqual(first, self.sub.edit_subscriber(1, data))
        self.assertEqual(visits, len(self.visited))
        self.assertEqual(freezes, self.nas.disconnect_for_identity.call_count)

    def test_old_accept_alone_after_renewal_does_not_require_new_renewal(self):
        self.account()
        self.q("UPDATE wisp_subscribers SET status='active',last_renewed_at='2026-11-02 15:00:00' WHERE id=1")
        self.assertFalse(self.settlement.pending_sessions('fixture-old'))
        self.assertTrue(self.settlement.has_unaccounted_accept('fixture-old'))
        self.assertTrue(self.edit('subscriber', {'username': 'fixture-new'})['success'])
        self.assertEqual(self.q('SELECT last_renewed_at FROM wisp_subscribers')[0]['last_renewed_at'], '2026-11-02 15:00:00')

    def test_account_status_is_preserved(self):
        for status in ('active', 'expired', 'suspended', 'disabled'):
            with self.subTest(status=status):
                if self.q('SELECT * FROM wisp_subscribers'):
                    self.tearDown(); self.setUp()
                self.account(status=status)
                self.assertTrue(self.edit('subscriber', {'username': 'fixture-new'})['success'])
                self.assertEqual(self.q('SELECT status FROM wisp_subscribers')[0]['status'], status)


if __name__ == '__main__': unittest.main()

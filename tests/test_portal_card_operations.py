"""Isolated SQL/HTTP regression tests; importing the application starts no workers.

Run: python -m unittest discover -s tests -p test_portal_card_operations.py -v
SQLite exercises transaction rollback; live MariaDB/NAS evidence is separate.
"""
import ast
import datetime
import importlib.util
import sqlite3
import hashlib
import socket
import struct
import inspect
from functools import wraps
import sys
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask, jsonify, request, session

ROOT = Path(__file__).resolve().parents[1]


def functions(path, names, namespace):
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    for node in nodes:
        node.decorator_list = []
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        columns = """id INTEGER PRIMARY KEY, username TEXT, password TEXT, pin_code TEXT,
            status TEXT, package_id INTEGER, balance REAL, extra_quota_mb REAL DEFAULT 0,
            expires_at TEXT, last_renewed_at TEXT, created_at TEXT, first_used_at TEXT,
            batch_id INTEGER, serial_number TEXT, reseller_id INTEGER, expire_reason TEXT,
            snap_volume_quota_mb INTEGER, snap_uptime_limit_mins INTEGER,
            snap_validity_value INTEGER, snap_validity_unit TEXT, snap_validity_days INTEGER,
            snap_price REAL, snap_cost REAL, snap_rate_download TEXT, snap_rate_upload TEXT,
            snap_rate_limit_str TEXT, snap_simultaneous_sessions INTEGER, snap_mikrotik_group TEXT,
            loan_balance_mb INTEGER DEFAULT 0, loan_status INTEGER DEFAULT 0"""
        for table in ('wisp_subscribers', 'wisp_vouchers'):
            self.db.execute(f'CREATE TABLE {table} ({columns})')
        self.db.executescript("""
            CREATE TABLE wisp_packages(id INTEGER PRIMARY KEY,name TEXT,is_active INTEGER,
                price REAL,cost REAL,volume_quota_mb INTEGER,uptime_limit_mins INTEGER,
                validity_value INTEGER,validity_unit TEXT,validity_days INTEGER,
                rate_download TEXT,rate_upload TEXT,rate_limit_str TEXT,
                simultaneous_sessions INTEGER,mikrotik_group TEXT,is_rollover_enabled INTEGER,
                is_loyalty_enabled INTEGER DEFAULT 0,loyalty_points INTEGER DEFAULT 0);
            INSERT INTO wisp_packages VALUES(1,'old',1,20,5,100,30,2,'days',2,'2M','1M',NULL,2,'group',1,0,0);
            INSERT INTO wisp_packages SELECT 2,'new',1,30,6,200,40,3,'days',3,'4M','2M',NULL,3,'newgroup',0,0,0;
            CREATE TABLE radcheck(username TEXT,attribute TEXT,op TEXT,value TEXT);
            CREATE TABLE radreply(username TEXT,attribute TEXT,op TEXT,value TEXT);
            CREATE TABLE radusergroup(username TEXT,groupname TEXT,priority INTEGER);
            CREATE TABLE radacct(radacctid INTEGER,username TEXT,acctstoptime TEXT,nasipaddress TEXT,
                acctsessionid TEXT,framedipaddress TEXT,callingstationid TEXT);
            CREATE TABLE nas(id INTEGER,nasname TEXT,secret TEXT);
            CREATE TABLE wisp_system_settings(`key` TEXT,value TEXT);
            CREATE TABLE wisp_voucher_sales(voucher_id INTEGER,batch_id INTEGER,batch_name TEXT,
                username TEXT,serial_number TEXT,package_name TEXT,price REAL,cost REAL,
                reseller_id INTEGER,activated_at TEXT);
            INSERT INTO nas VALUES(1,'10.0.0.1','testsecret');
        """)
        self.db.commit()
        self.db.execute("ALTER TABLE wisp_packages ADD COLUMN service_type TEXT DEFAULT 'pppoe'")
        self.db.execute("ALTER TABLE wisp_subscribers ADD COLUMN service_type TEXT DEFAULT 'pppoe'")
        self.ns = dict(datetime=datetime, db_session=self.transaction,
                       adapt_query=lambda sql, conn: sql, is_mysql_conn=lambda conn: False,
                       query_one=self.one, query_all=self.all, log_user_audit=Mock(),
                       record_session_baselines=Mock(),
                       calculate_cycle_usage_and_rollover=Mock(return_value={
                           'rem_data_mb': 11, 'rem_time_delta': datetime.timedelta(days=1),
                           'rem_days': 1, 'rem_hours': 0}))
        functions('services/user_portal_service.py',
                  {'_lock_portal_account', '_sync_recharge_radius', 'renew_or_change_package',
                   'change_portal_password', 'apply_portal_speed'}, self.ns)
        self.coa = Mock()
        fake = types.ModuleType('core.coa')
        fake.RadiusCoaClient = self.coa
        rate = types.ModuleType('core.rate_limit')
        rate.build_mikrotik_rate_limit = lambda down, up: f'{up}/{down}'
        audit = types.ModuleType('database.db')
        audit.log_user_audit = self.ns['log_user_audit']
        self.modules = patch.dict(sys.modules, {'core.coa': fake, 'core.rate_limit': rate, 'database.db': audit})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.addCleanup(self.db.close)

    @contextmanager
    def transaction(self):
        try:
            yield self.db
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def one(self, sql, args=()):
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def all(self, sql, args=()):
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def account(self, kind='voucher', status='active', balance=100, password='oldpass'):
        table = 'wisp_vouchers' if kind == 'voucher' else 'wisp_subscribers'
        self.db.execute(f"""INSERT INTO {table}(id,username,password,pin_code,status,package_id,
            balance,batch_id,snap_volume_quota_mb,snap_uptime_limit_mins)
            VALUES(1,'card',?,'1234',?,1,?,1,55,12)""", (password,status,balance))
        self.db.commit()
        return table

    def test_renew_active_unused_expired_and_subscriber(self):
        for kind, status in [('voucher','active'),('voucher','unused'),('voucher','expired'),('subscriber','active')]:
            with self.subTest(kind=kind,status=status):
                table = self.account(kind,status)
                self.assertTrue(self.ns['renew_or_change_package']('CARD',1)[0])
                row = self.one(f'SELECT * FROM {table}')
                self.assertEqual((row['balance'],row['status'],row['extra_quota_mb']), (80,'active',11))
                self.assertEqual(row['snap_volume_quota_mb'],55 if kind=='voucher' else 100)
                self.assertEqual(self.one('SELECT value FROM radcheck')['value'],'oldpass')
                self.db.execute(f'DELETE FROM {table}')
                self.db.execute('DELETE FROM radcheck')
                self.db.commit()

    def test_change_package_refreshes_all_snapshots(self):
        table = self.account()
        self.assertTrue(self.ns['renew_or_change_package']('card',2)[0])
        row = self.one(f'SELECT * FROM {table}')
        self.assertEqual((row['package_id'],row['balance'],row['snap_volume_quota_mb'],row['snap_uptime_limit_mins']), (2,70,200,40))
        self.assertEqual((row['snap_rate_limit_str'],row['snap_simultaneous_sessions']),('2M/4M',3))
        self.assertEqual(self.one('SELECT COUNT(*) n FROM wisp_voucher_sales')['n'],1)

    def test_subscriber_change_follows_new_package_quota(self):
        self.account('subscriber')
        self.assertTrue(self.ns['renew_or_change_package']('card', 2)[0])
        row = self.one('SELECT * FROM wisp_subscribers')
        self.assertEqual(row['balance'], 70)
        self.assertEqual(row['package_id'], 2)
        self.assertEqual(row['snap_volume_quota_mb'], 200)
        resolved = self.one('SELECT COALESCE(s.snap_volume_quota_mb,p.volume_quota_mb) quota FROM wisp_subscribers s JOIN wisp_packages p ON p.id=s.package_id')
        self.assertEqual(resolved['quota'], 200)
        self.db.execute('UPDATE wisp_packages SET volume_quota_mb=300 WHERE id=2')
        resolved = self.one('SELECT COALESCE(s.snap_volume_quota_mb,p.volume_quota_mb) quota FROM wisp_subscribers s JOIN wisp_packages p ON p.id=s.package_id')
        self.assertEqual(resolved['quota'], 200)

    def test_rejections_leave_contract_cash_and_sales_unchanged(self):
        for status, cash, pkg in [('suspended',100,1),('disabled',100,1),('recharged',100,1),('active',19,1),('active',100,999)]:
            with self.subTest(status=status,cash=cash,pkg=pkg):
                table = self.account(status=status,balance=cash)
                before = self.one(f'SELECT * FROM {table}')
                self.assertFalse(self.ns['renew_or_change_package']('card',pkg)[0])
                self.assertEqual(before,self.one(f'SELECT * FROM {table}'))
                self.assertEqual(self.one('SELECT COUNT(*) n FROM wisp_voucher_sales')['n'],0)
                self.db.execute(f'DELETE FROM {table}')
                self.db.commit()

    def test_radius_failure_rolls_back_cash_contract_and_sale(self):
        table = self.account()
        before = self.one(f'SELECT * FROM {table}')
        self.ns['_sync_recharge_radius'] = Mock(side_effect=RuntimeError('radius write failed'))
        with self.assertRaises(RuntimeError):
            self.ns['renew_or_change_package']('card',2)
        self.assertEqual(before,self.one(f'SELECT * FROM {table}'))
        self.assertEqual(self.one('SELECT COUNT(*) n FROM wisp_voucher_sales')['n'],0)

    def test_accounting_failure_preserves_balance(self):
        table = self.account()
        before = self.one(f'SELECT * FROM {table}')
        self.ns['calculate_cycle_usage_and_rollover'].side_effect = RuntimeError('no accounting')
        self.assertFalse(self.ns['renew_or_change_package']('card',1)[0])
        self.assertEqual(before,self.one(f'SELECT * FROM {table}'))

    def test_zero_snapshot_is_unlimited_not_package_quota(self):
        table=self.account()
        self.db.execute(f'UPDATE {table} SET snap_volume_quota_mb=0')
        self.db.commit()
        self.assertTrue(self.ns['renew_or_change_package']('card',1)[0])
        self.assertEqual(self.one(f'SELECT snap_volume_quota_mb FROM {table}')['snap_volume_quota_mb'],0)

    def test_consumed_card_rejects_before_nas_settlement(self):
        ns=dict(inspect=inspect,wraps=wraps,query_one=lambda *args: {'username':'card','status':'recharged'},
                settled_account=Mock())
        functions('services/renewal_settlement_service.py',{'settle_cycle_operation'},ns)
        def renew_or_change_package(username,new_pkg_id):
            return False,'consumed'
        self.assertFalse(ns['settle_cycle_operation'](renew_or_change_package)('card',1)[0])
        ns['settled_account'].assert_not_called()

    def test_password_success_and_validation_for_both_types(self):
        for kind in ('voucher','subscriber'):
            with self.subTest(kind=kind):
                table = self.account(kind)
                before = self.one(f'SELECT * FROM {table}')
                for old,new,confirm in [('wrong','newpass','newpass'),('oldpass','abc','abc'),('oldpass','newpass','other')]:
                    self.assertFalse(self.ns['change_portal_password']('card',old,new,confirm)[0])
                    self.assertEqual(before,self.one(f'SELECT * FROM {table}'))
                self.assertTrue(self.ns['change_portal_password']('card','oldpass','newpass','newpass')[0])
                self.assertEqual(self.one(f'SELECT password FROM {table}')['password'],'newpass')
                self.assertEqual(self.one('SELECT value FROM radcheck')['value'],'newpass')
                self.db.execute(f'DELETE FROM {table}')
                self.db.execute('DELETE FROM radcheck')
                self.db.commit()

    def test_password_unused_and_suspended_do_not_unblock(self):
        for status in ('unused','suspended','expired'):
            with self.subTest(status=status):
                table = self.account(status=status,password=None)
                self.db.execute("INSERT INTO radcheck VALUES('card','Auth-Type',':=','Reject')")
                self.db.commit()
                self.assertTrue(self.ns['change_portal_password']('card','1234','newpass','newpass')[0])
                self.assertEqual(self.one(f'SELECT status FROM {table}')['status'],status)
                self.assertEqual(self.one("SELECT value FROM radcheck WHERE attribute='Auth-Type'")['value'],'Reject')
                self.db.execute(f'DELETE FROM {table}')
                self.db.execute('DELETE FROM radcheck')
                self.db.commit()

    def test_password_radius_failure_rolls_back_both_credentials(self):
        table=self.account()
        self.db.execute("INSERT INTO radcheck VALUES('card','Cleartext-Password',':=','oldpass')")
        self.db.execute("CREATE TRIGGER fail_password BEFORE UPDATE ON radcheck BEGIN SELECT RAISE(ABORT,'injected radius failure'); END")
        self.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.ns['change_portal_password']('card','oldpass','newpass','newpass')
        self.assertEqual(self.one(f'SELECT password FROM {table}')['password'],'oldpass')
        self.assertEqual(self.one('SELECT value FROM radcheck')['value'],'oldpass')

    def test_old_pin_does_not_bypass_changed_password(self):
        self.account()
        functions('services/user_portal_service.py',{'authenticate_portal_user'},self.ns)
        self.assertIsNone(self.ns['authenticate_portal_user']('card','1234')[0])
        self.assertIsNone(self.ns['authenticate_portal_user']('card','card')[0])
        self.assertIsNotNone(self.ns['authenticate_portal_user']('card','oldpass')[0])

    def test_all_live_sessions_and_partial_timeout_results(self):
        self.account()
        for i in (1,2):
            self.db.execute("INSERT INTO radacct VALUES(?,'card',NULL,'10.0.0.1',?,'10.1.1.1','AA')", (i,str(i)))
        self.db.execute("INSERT INTO radacct VALUES(3,'card','closed','10.0.0.1','3','10.1.1.1','AA')")
        self.db.commit()
        client = self.coa.return_value
        client.modify_rate_limit.side_effect = [{'success':True,'status':'ack','code':44}, {'success':False,'status':'nak','code':45}]
        result = self.ns['apply_portal_speed']('card','1M/2M')
        self.assertEqual((result['sessions_total'],result['sessions_acknowledged'],result['live_status']),(2,1,'partial'))
        self.assertEqual([c.kwargs['session_id'] for c in client.modify_rate_limit.call_args_list],['1','2'])
        client.modify_rate_limit.side_effect = TimeoutError('NAS')
        self.assertEqual(self.ns['apply_portal_speed']('card','2M/4M')['live_status'],'next_login')
        self.assertEqual(self.one('SELECT COUNT(*) n FROM radreply')['n'],1)

    def test_offline_unlimited_is_saved_and_not_claimed_live(self):
        result = self.ns['apply_portal_speed']('card','0/0')
        self.assertEqual(result['live_status'],'next_login')
        self.assertEqual(self.one('SELECT value FROM radreply')['value'],'0/0')
        self.coa.assert_not_called()

    def test_all_sessions_acknowledged_and_missing_nas(self):
        self.account()
        self.db.execute("INSERT INTO radacct VALUES(1,'card',NULL,'10.0.0.1','1','10.1.1.1','AA')")
        self.db.commit()
        self.coa.return_value.modify_rate_limit.return_value = {'success': True,'status':'ack','code':44}
        self.assertEqual(self.ns['apply_portal_speed']('card','1M/2M')['live_status'],'acknowledged')
        self.db.execute('DELETE FROM nas')
        self.db.commit()
        result = self.ns['apply_portal_speed']('card','1M/2M')
        self.assertEqual(result['results'][0]['status'],'nas_unavailable')
        self.assertEqual(result['live_status'],'next_login')

    def test_hotspot_saves_policy_without_coa_or_session_changes(self):
        for kind in ('voucher', 'subscriber'):
            with self.subTest(kind=kind):
                table = self.account(kind)
                self.db.execute("UPDATE wisp_packages SET service_type='hotspot'")
                if kind == 'subscriber':
                    self.db.execute("UPDATE wisp_subscribers SET service_type='hotspot'")
                self.db.execute("DELETE FROM radacct")
                self.db.execute("INSERT INTO radacct VALUES(1,'card',NULL,'10.0.0.1','1','10.1.1.1','AA')")
                self.db.commit()
                before = self.all('SELECT * FROM radacct')
                result = self.ns['apply_portal_speed']('card','1M/2M')
                self.assertEqual(result['live_status'], 'next_login')
                self.assertEqual(result['sessions_total'], 1)
                self.assertEqual(result['sessions_acknowledged'], 0)
                self.assertEqual(self.one('SELECT value FROM radreply')['value'], '1M/2M')
                self.assertEqual(self.all('SELECT * FROM radacct'), before)
                self.coa.assert_not_called()
                self.db.execute('DELETE FROM '+table)
                self.db.commit()

    def test_speed_endpoint_requires_login_and_reports_pending(self):
        app = Flask(__name__)
        app.secret_key = 'isolated-test'
        ns = dict(request=request,session=session,jsonify=jsonify,
                  query_all=lambda _: [],get_portal_speed_options=lambda _: [],
                  apply_portal_speed=self.ns['apply_portal_speed'])
        functions('web/routes/portal.py',{'api_user_change_speed'},ns)
        app.add_url_rule('/speed',view_func=ns['api_user_change_speed'],methods=['POST'])
        client = app.test_client()
        self.assertEqual(client.post('/speed',json={'username':'victim','speed':'2M'}).status_code,401)
        with client.session_transaction() as s:
            s['portal_user'] = 'card'
        result = client.post('/speed',json={'speed':'2M'}).get_json()
        self.assertTrue(result['success'])
        self.assertEqual(result['coa']['live_status'],'next_login')
        self.assertNotIn('لحظياً',result['message'])


class CoAIntegrityTests(unittest.TestCase):
    def test_signed_ack_nak_and_untrusted_response(self):
        spec = importlib.util.spec_from_file_location('isolated_coa', ROOT / 'core/coa.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for code, corrupt, wrong_id in [(44,False,False),(45,False,False),(44,True,False),(44,False,True)]:
            with self.subTest(code=code,corrupt=corrupt,wrong_id=wrong_id):
                sock = Mock()
                def reply(_):
                    packet = sock.sendto.call_args.args[0]
                    head = struct.pack('!BBH',code,packet[1]+1 if wrong_id and packet[1]<255 else (0 if wrong_id else packet[1]),20)
                    auth = hashlib.md5(head+packet[4:20]+b'testsecret').digest()
                    if corrupt:
                        auth = b'0'*16
                    return head+auth, ('127.0.0.1',3799)
                sock.recvfrom.side_effect = reply
                with patch.object(module.socket,'socket',return_value=sock):
                    result = module.RadiusCoaClient('127.0.0.1','testsecret').modify_rate_limit(username='card',session_id='s1')
                self.assertEqual(result['success'],code==44 and not corrupt and not wrong_id)
                if corrupt or wrong_id:
                    self.assertEqual(result['status'],'invalid_response')


if __name__ == '__main__':
    unittest.main()

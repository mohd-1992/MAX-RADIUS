"""Isolated checkpoint diagnostics: fake SQL/NAS, no application imports."""
import ast
import datetime
import logging
from pathlib import Path
import re
import sys
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class CheckpointDiagnostics(unittest.TestCase):
    def setUp(self):
        self.now = datetime.datetime(2026, 11, 6, 21)
        self.row = dict(radacctid=1, nasipaddress='192.0.2.1', framedipaddress='192.0.2.2',
                        callingstationid='00:11:22:33:44:55', acctstarttime=self.now-datetime.timedelta(seconds=60))
        self.live = {'.id':'*1', 'address':'192.0.2.2', 'mac-address':'00:11:22:33:44:55',
                     'radius':'true', 'uptime':'1m', 'bytes-in':'10', 'bytes-out':'20'}
        self.error = None
        self.journal_error = None
        self.sql = []
        owner = self
        class NAS:
            def __init__(self, *args, **kwargs): pass
            def connect(self):
                if owner.error: raise owner.error
            def login(self, *args): return True
            def execute_command(self, command, words):
                assert command.endswith('/print')
                return [owner.live] if command == '/ip/hotspot/active/print' and owner.live else []
            def close(self): pass
        class Pending(Exception): pass
        self.Pending = Pending
        def query(sql, args=()):
            self.assertTrue(sql.lstrip().startswith('SELECT'))
            self.sql.append(sql)
            if 'FROM radacct_owned' in sql: return [self.row]
            if 'FROM radpostauth' in sql: return []
            if 'FROM wisp_nas_devices' in sql: return [dict(api_username='PRIVATE_USER', api_password='PRIVATE_PASSWORD', secret='PRIVATE_SECRET', api_port=8728)]
            if 'SELECT NOW()' in sql: return [dict(now=self.now)]
            if 'FROM radacct WHERE' in sql: return [dict(acctstoptime=None)]
            raise AssertionError(sql)
        def recover(username):
            if self.journal_error: raise self.journal_error
        mods = {}
        for name in ('core.mikrotik_api', 'services.nas_session_settlement', 'services.renewal_settlement_service'):
            mods[name] = types.ModuleType(name)
        mods['core.mikrotik_api'].RouterOSApiProtocol = NAS
        mods['services.nas_session_settlement'].recover_journals = recover
        mods['services.renewal_settlement_service'].SettlementPending = Pending
        self.modules = patch.dict(sys.modules, mods); self.modules.start(); self.addCleanup(self.modules.stop)
        self.ns = dict(__name__='checkpoint_diagnostic_test', query_all=query,
                       query_one=lambda sql,args=(): next(iter(query(sql,args)),None), time=time, re=re)
        tree = ast.parse((ROOT/'services/online_policy_service.py').read_text(encoding='utf-8'))
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ('fail','checkpoint','rows_for','seconds')]
        exec(compile(ast.Module(nodes, []), 'online_policy_service.py', 'exec'), self.ns)

    def reject(self, reason):
        with self.assertLogs(self.ns['__name__'], level=logging.WARNING) as logs:
            with self.assertRaises(self.Pending) as raised:
                self.ns['checkpoint']('PRIVATE_ACCOUNT')
        self.assertTrue(any('reason='+reason in message for message in logs.output))
        text=' '.join(logs.output)
        for secret in ('PRIVATE_ACCOUNT','PRIVATE_USER','PRIVATE_PASSWORD','PRIVATE_SECRET','192.0.2.'):
            self.assertNotIn(secret,text)
        self.assertIn('لم يتم الخصم', str(raised.exception))

    def test_valid_snapshot_unchanged(self):
        result=self.ns['checkpoint']('PRIVATE_ACCOUNT')
        self.assertEqual((result[1]['input'],result[1]['output']),(10,20))
        self.assertIs(result[1]['row'],self.row)

    def test_missing_counter_records_type_without_payload(self):
        del self.live['bytes-in']; self.reject('nas_read_or_validation')

    def test_uptime_rejection_has_specific_reason(self):
        self.live['uptime']='2m';self.reject('hotspot_uptime')

    def test_identity_rejection_has_specific_reason(self):
        self.live['address']='192.0.2.99';self.reject('hotspot_identity')

    def test_router_error_does_not_leak_exception_text(self):
        self.error=TimeoutError('PRIVATE_PASSWORD PRIVATE_ACCOUNT');self.reject('nas_read_or_validation')

    def test_journal_error_has_specific_reason(self):
        self.journal_error=RuntimeError('PRIVATE_SECRET');self.reject('journal_recovery')

    def test_missing_live_session_is_still_rejected(self):
        self.live=None;self.reject('open_sql_session_absent_on_nas')


if __name__=='__main__': unittest.main()

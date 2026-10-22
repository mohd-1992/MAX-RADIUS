"""Isolated maintenance contracts: no app factory, live DB, NAS or Docker."""
import ast
from contextlib import contextmanager
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def function(path, name, namespace):
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), path, 'exec'), namespace)
    return namespace[name]


class OperationalArchive(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        spec = importlib.util.spec_from_file_location('reset_state_fixture', ROOT / 'services/maintenance_reset_state.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.job = 'a' * 32
        for name in ('userman-jobs', 'nas-session-settlements'):
            (self.root / name).mkdir()
            (self.root / name / 'proof.json').write_text('{"old":true}')

    def test_preserves_files_and_retires_active_bindings(self):
        value = self.module.archive_reset_state(self.job, self.root)
        self.assertEqual(value['file_count'], 2)
        for name in ('userman-jobs', 'nas-session-settlements'):
            self.assertEqual(list((self.root / name).iterdir()), [])
            self.assertEqual((self.root / 'maintenance-archives' / self.job / name / 'proof.json').read_text(), '{"old":true}')
        self.assertEqual(value, self.module.archive_reset_state(self.job, self.root))

    def test_interrupted_rename_can_finish_without_replacing_evidence(self):
        real = self.module.os.replace
        def interrupted(source, target):
            if Path(source).name == 'nas-session-settlements':
                raise OSError('isolated disk failure')
            return real(source, target)
        with patch.object(self.module.os, 'replace', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.module.archive_reset_state(self.job, self.root)
        self.assertEqual(self.module.archive_reset_state(self.job, self.root)['file_count'], 2)

    def test_invalid_job_never_moves_a_file(self):
        with self.assertRaises(ValueError):
            self.module.archive_reset_state('../outside', self.root)
        self.assertTrue((self.root / 'userman-jobs' / 'proof.json').is_file())


class AccountingObservation(unittest.TestCase):
    def setUp(self):
        self.row = dict(radacctid=1,username='fixture',acctuniqueid='unique',acctsessionid='session',
                        nasipaddress='192.0.2.1',framedipaddress='198.51.100.1',callingstationid='mac',
                        acctstarttime='start',acctinputoctets=100,acctoutputoctets=200,acctsessiontime=30,
                        acctstoptime=None,acctterminatecause='')
        self.current = copy.deepcopy(self.row)
        self.live = []
        self.sql_calls = []
        owner = self
        class NAS:
            def __init__(self,*a,**kw):pass
            def connect(self):pass
            def login(self,*a):return True
            def close(self):pass
            def execute_command(self,path,words):
                if not path.endswith('/print'):raise AssertionError('NAS write forbidden')
                return owner.live if '/hotspot/' in path else []
        fake_nas = types.ModuleType('core.mikrotik_api')
        fake_nas.RouterOSApiProtocol = NAS
        settlement = types.ModuleType('services.nas_session_settlement')
        settlement.actual_stop = lambda r: bool(r.get('acctstoptime') and r.get('acctterminatecause')=='User-Request')
        self.patcher = patch.dict('sys.modules', {'core.mikrotik_api':fake_nas,'services.nas_session_settlement':settlement})
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        def read(sql,args):
            self.sql_calls.append(sql)
            if 'wisp_nas_devices' in sql:
                return dict(ip_address='192.0.2.1',api_username='fixture',api_password='fixture',api_port=8728)
            reads=sum('FROM radacct' in s for s in self.sql_calls)
            return copy.deepcopy(self.row if reads==1 else self.current)
        self.observe = function('services/db_maintenance_service.py', '_inspect_stale_session', {'query_one':read})

    def assert_read_only(self):
        self.assertTrue(all(q.startswith('SELECT') for q in self.sql_calls))

    def test_live_radius_session_is_not_closed(self):
        self.live=[dict(user='fixture',address='198.51.100.1',radius='true')]
        result=self.observe('fixture',1)
        self.assertTrue(result['pending'])
        self.assertEqual(result['nas_state'],'active')
        self.assert_read_only()

    def test_nas_absence_without_stop_stays_pending(self):
        result=self.observe('fixture',1)
        self.assertFalse(result['success'])
        self.assertTrue(result['pending'])
        self.assert_read_only()

    def test_only_absence_plus_real_stop_verifies_completion(self):
        self.current.update(acctstoptime='stop',acctterminatecause='User-Request',acctinputoctets=150)
        self.assertTrue(self.observe('fixture',1)['verified'])
        self.assert_read_only()

    def test_changed_identity_rejected(self):
        self.current['acctuniqueid']='new-session'
        self.assertFalse(self.observe('fixture',1)['success'])
        self.assert_read_only()

    def test_decreasing_counter_rejected(self):
        self.current['acctinputoctets']=1
        self.assertFalse(self.observe('fixture',1)['success'])
        self.assert_read_only()


class BatchCounts(unittest.TestCase):
    def test_empty_batch_uses_live_count_despite_stored_column(self):
        queries=[]
        def query(sql, params):
            queries.append(sql)
            return [dict(id=1,card_count=20,actual_card_count=0),
                    dict(id=2,card_count=20,actual_card_count=3)]
        get_batches=function('services/voucher_service.py','get_batches',{'query_all':query})
        rows=get_batches()
        self.assertEqual([r['card_count'] for r in rows],[0,3])
        self.assertTrue(all('actual_card_count' not in r for r in rows))


class RequestProtection(unittest.TestCase):
    def test_maintenance_posts_require_token(self):
        from flask import Flask, session
        spec=importlib.util.spec_from_file_location('maintenance_csrf_fixture',ROOT/'core/license_security.py')
        security=importlib.util.module_from_spec(spec);spec.loader.exec_module(security)
        app=Flask('maintenance_csrf_fixture');app.secret_key='isolated-fixture'
        security.install_csrf(app,security.protects_admin_account_request)
        paths=('fix','fix-all','clean-expired','prune-logs','archive','optimize-tables','factory-reset','factory-reset/cancel')
        for i,path in enumerate(paths):
            app.add_url_rule('/api/tools/database-maintenance/'+path,'case'+str(i),lambda:'ok',methods=['POST'])
        client=app.test_client()
        with client.session_transaction() as state:state['_license_csrf']='fixture-token'
        for path in paths:
            url='/api/tools/database-maintenance/'+path
            with self.subTest(path=path):
                self.assertEqual(client.post(url,json={}).status_code,403)
                self.assertEqual(client.post(url,json={},headers={'X-CSRF-Token':'wrong'}).status_code,403)
                self.assertEqual(client.post(url,json={},headers={'X-CSRF-Token':'fixture-token'}).status_code,200)


if __name__=='__main__':
    unittest.main()

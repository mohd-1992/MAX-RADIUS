"""Isolated cancellation and historical-evidence contracts; no app or live DB."""
import ast
import datetime as dt
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock
from unittest.mock import patch
from contextlib import contextmanager
import types

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('deletion_policy_fixture', ROOT / 'services/deletion_history_policy.py')
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


class HistoricalEvidence(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 11, 3, 12)
        self.account = dict(status='expired', checked_at=self.now)
        self.row = dict(acctstarttime=self.now-dt.timedelta(days=1),
                        acctupdatetime=self.now-dt.timedelta(hours=1), acctstoptime=None)

    def allowed(self, source='wisp_imported_session_history'):
        return policy.historical_observation_allowed(source, self.row, self.account)

    def test_old_imported_observation_does_not_require_fabricated_stop(self):
        self.assertTrue(self.allowed())
        self.assertIsNone(self.row['acctstoptime'])

    def test_physical_open_is_always_protected(self):
        self.assertFalse(self.allowed('radacct'))

    def test_recent_or_future_observation_is_protected(self):
        for delta in (0, -1, 899):
            self.row['acctupdatetime'] = self.now-dt.timedelta(seconds=delta)
            self.assertFalse(self.allowed())

    def test_old_closed_physical_row_can_be_summarized(self):
        self.row['acctstoptime'] = self.now-dt.timedelta(hours=1)
        self.assertTrue(self.allowed('radacct'))

    def test_nonexpired_and_missing_start_are_protected(self):
        self.account['status'] = 'used'
        self.assertFalse(self.allowed())
        self.account['status'] = 'expired'
        self.row['acctstarttime'] = None
        self.assertFalse(self.allowed())

    def test_malformed_or_incompatible_dates_are_protected(self):
        for value in ('unknown', self.now.replace(tzinfo=dt.timezone.utc)):
            self.row['acctupdatetime'] = value
            self.assertFalse(self.allowed())


class CancelRequest(unittest.TestCase):
    def setUp(self):
        self.job = 'a'*32
        self.state = dict(job_id=self.job, state='running')
        self.update = Mock(return_value=1)
        self.audit = Mock()
        ns = dict(get_maintenance_job=lambda kind,job: self.state,
                  execute_update=self.update, log_audit=self.audit)
        tree = ast.parse((ROOT / 'services/maintenance_job_service.py').read_text(encoding='utf-8'))
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ('cancel_cleanup_job','cancel_maintenance_job')]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), '<cancel-fixture>', 'exec'), ns)
        self.cancel = ns['cancel_cleanup_job']

    def test_active_request_sets_durable_flag_and_audits_once(self):
        self.assertEqual(self.cancel(self.job), self.state)
        statement, params = self.update.call_args.args
        self.assertIn('cancel_requested', statement)
        self.assertIn('active_key=?', statement)
        self.assertEqual(params, (self.job,'expired-card-delete'))
        self.audit.assert_called_once()
        self.update.return_value = 0
        self.cancel(self.job)
        self.audit.assert_called_once()

    def test_terminal_or_invalid_request_never_updates(self):
        self.state['state'] = 'completed'
        self.cancel(self.job)
        with self.assertRaises(ValueError):
            self.cancel('../invalid')
        self.update.assert_not_called()
        self.audit.assert_not_called()


class MaintenanceTemplate(unittest.TestCase):
    def test_jinja_parses_complete_maintenance_template(self):
        # CSS adjacent to an ID selector must never become a Jinja {# comment.
        from jinja2 import Environment
        source = (ROOT / 'web/templates/database_maintenance.html').read_text(encoding='utf-8')
        Environment().parse(source)
        self.assertIn('id="cleanupJobCancel"', source)
        self.assertIn('cleanup-cancel[hidden]', source)

    def test_embedding_rejects_broken_css_before_emitting_payload(self):
        import tempfile
        from jinja2 import TemplateSyntaxError
        build_spec = importlib.util.spec_from_file_location('embed_fixture', ROOT / 'packaging/embed_templates.py')
        build = importlib.util.module_from_spec(build_spec)
        build_spec.loader.exec_module(build)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            templates = root / 'web/templates'
            templates.mkdir(parents=True)
            (templates / 'broken.html').write_text('<style>@media(max-width:600px){#id{display:flex}}</style>')
            with self.assertRaises(TemplateSyntaxError):
                build.embed(root)
            self.assertFalse((root / 'web/_embedded_templates.py').exists())


class CleanupProgressPacing(unittest.TestCase):
    def run_cleanup(self, cancelled=False):
        clock = [0.0]
        reports, calls, transactions = [], [], []

        @contextmanager
        def lock(name):
            yield True

        def delete(kind, entity_id, **kwargs):
            self.assertTrue(kwargs['transaction_owned'])
            self.assertFalse(kwargs['queue_pending'])
            calls.append(entity_id)
            clock[0] += .006
            return dict(deleted=True)

        def transaction(operation, connection):
            result = operation(connection)
            transactions.append(len(result))
            return result

        lifecycle = types.SimpleNamespace(delete_account=delete, job_lock=lock,
                                          run_transaction=transaction,
                                          enqueue_deletion_disconnects=Mock())
        ns = dict(time=types.SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0]),
                  query_all=lambda *args: [dict(id=i, last_renewed_at=None, expire_reason=None) for i in range(120)],
                  get_connection=lambda: types.SimpleNamespace(close=lambda: None))
        tree = ast.parse((ROOT / 'services/db_maintenance_service.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'delete_expired_vouchers')
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<progress-fixture>', 'exec'), ns)
        cancel = Mock(return_value=cancelled)
        with patch.dict('sys.modules', {'services.account_lifecycle_service': lifecycle}):
            result = ns['delete_expired_vouchers'](progress_callback=reports.append, cancel_callback=cancel)
        return result, reports, calls, transactions, cancel

    def test_reporting_does_not_change_commit_or_cancel_boundaries(self):
        result, reports, calls, transactions, cancel = self.run_cleanup()
        self.assertTrue(result['deletion_complete'])
        self.assertEqual(len(calls), 120)
        self.assertEqual(transactions, [20]*6)
        self.assertEqual(cancel.call_count, 6)
        self.assertEqual([r['processed'] for r in reports], [0, 100, 120])

    def test_cancel_before_first_chunk_commits_nothing(self):
        result, reports, calls, transactions, cancel = self.run_cleanup(cancelled=True)
        self.assertTrue(result['cancelled'])
        self.assertEqual(result['remaining'], 120)
        self.assertEqual(calls, [])
        self.assertEqual(transactions, [])
        self.assertEqual(cancel.call_count, 1)


if __name__ == '__main__':
    unittest.main()

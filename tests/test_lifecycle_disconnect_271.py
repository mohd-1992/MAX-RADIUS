"""Lifecycle queue routing keeps real accounting barriers without counter seals."""
import ast
import contextlib
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch, call


class LifecycleDisconnectTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[1] / 'services/coa_queue_service.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == '_disconnect_lifecycle_account_sessions')
        self.required = Mock(return_value=True)
        self.checkpoint = Mock(return_value={11: {'row': {'nasipaddress': '192.0.2.1'}},
                                            12: {'row': {'nasipaddress': '192.0.2.2'}}})
        self.disconnect, self.wait = Mock(), Mock()
        self.pending = type('SettlementPending', (RuntimeError,), {})
        modules = {
            'services': types.ModuleType('services'),
            'services.account_lifecycle_service': types.SimpleNamespace(lifecycle_disconnect_still_required=self.required),
            'services.online_policy_service': types.SimpleNamespace(checkpoint=self.checkpoint),
            'services.nas_session_settlement': types.SimpleNamespace(disconnect_for_identity=self.disconnect),
            'services.renewal_settlement_service': types.SimpleNamespace(wait_for_final_accounting=self.wait, SettlementPending=self.pending),
        }
        self.modules = patch.dict('sys.modules', modules); self.modules.start(); self.addCleanup(self.modules.stop)
        ns = {'_disconnect_account_guard': lambda user: contextlib.nullcontext()}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), ns)
        self.run_disconnect = ns[node.name]
        self.task = dict(username='local-fixture', lifecycle_kind='subscriber')

    def test_every_generation_needs_real_final_accounting(self):
        result = self.run_disconnect(self.task)
        self.assertEqual(self.disconnect.call_args_list,
                         [call({rid: row}) for rid, row in self.checkpoint.return_value.items()])
        self.wait.assert_called_once_with('local-fixture', session_ids=[11, 12])
        self.assertTrue(result['accounting_final']); self.assertEqual(result['target_sessions'], 2)

    def test_nas_filter_does_not_target_other_nas(self):
        self.run_disconnect(dict(self.task, nas_ip='192.0.2.2'))
        self.disconnect.assert_called_once_with({12: self.checkpoint.return_value[12]})
        self.wait.assert_called_once_with('local-fixture', session_ids=[12])

    def test_superseded_task_never_reads_or_disconnects(self):
        self.required.return_value = False
        self.assertEqual(self.run_disconnect(self.task)['status'], 'cancelled_after_state_change')
        self.checkpoint.assert_not_called(); self.disconnect.assert_not_called(); self.wait.assert_not_called()

    def test_state_change_during_inventory_cancels_before_disconnect(self):
        self.required.side_effect = [True, False]
        self.assertEqual(self.run_disconnect(self.task)['status'], 'cancelled_after_state_change')
        self.disconnect.assert_not_called(); self.wait.assert_not_called()

    def test_missing_stop_cannot_report_accounting_final(self):
        self.wait.side_effect = RuntimeError('no verified Stop')
        with self.assertRaises(RuntimeError): self.run_disconnect(self.task)

    def test_unverified_disconnect_does_not_cross_final_barrier(self):
        self.disconnect.side_effect = RuntimeError('generation not verified')
        with self.assertRaises(RuntimeError): self.run_disconnect(self.task)
        self.wait.assert_not_called()

    def test_pending_stop_message_does_not_deny_saved_suspension(self):
        self.wait.side_effect = self.pending('لم تحفظ تعديلات الهوية')
        with self.assertRaises(RuntimeError) as raised:
            self.run_disconnect(self.task)
        self.assertNotIn('لم تحفظ تعديلات الهوية', str(raised.exception))
        self.assertIn('المحاسبة النهائية', str(raised.exception))

    def test_partial_failure_still_attempts_other_captured_generation_once(self):
        self.disconnect.side_effect = [self.pending('uncertain first target'), None]
        with self.assertRaises(RuntimeError):
            self.run_disconnect(self.task)
        self.assertEqual(self.disconnect.call_args_list, [
            call({11: self.checkpoint.return_value[11]}),
            call({12: self.checkpoint.return_value[12]})])
        self.wait.assert_not_called()

    def test_last_generation_failure_never_claims_final_accounting(self):
        self.disconnect.side_effect = [None, self.pending('uncertain last target')]
        with self.assertRaises(RuntimeError):
            self.run_disconnect(self.task)
        self.assertEqual(self.disconnect.call_count, 2)
        self.wait.assert_not_called()


if __name__ == '__main__':
    unittest.main()

"""Admission state machine: no application, network, NAS or live database."""
import ast
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load_admission():
    db = types.ModuleType('database.db'); db.get_connection = Mock()
    lifecycle = types.ModuleType('services.account_lifecycle_service')
    lifecycle.job_lock = Mock(); lifecycle.sql = Mock()
    spec = importlib.util.spec_from_file_location('_isolated_admission', ROOT/'services/maintenance_admission.py')
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'database.db': db, 'services.account_lifecycle_service': lifecycle}):
        spec.loader.exec_module(module)
    return module


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.m = load_admission(); self.clock = 0.; self.held = set(); self.attempts = []
        self.blocked = set(); self.events = []; self.release_at = None
        @contextmanager
        def lock(name):
            acquired = name not in self.blocked
            self.attempts.append(name)
            if acquired:self.held.add(name)
            try:yield acquired
            finally:
                if acquired:self.held.remove(name)
        self.m.job_lock = lock; self.m._describe_owners = Mock(return_value=[{'connection': 7}])
        self.monotonic = patch.object(self.m.time, 'monotonic', side_effect=lambda:self.clock);self.monotonic.start();self.addCleanup(self.monotonic.stop)
        sleep_patch = patch.object(self.m.time, 'sleep', side_effect=self.tick);self.sleep=sleep_patch.start();self.addCleanup(sleep_patch.stop)
        self.logging = patch.object(self.m.log, 'warning');self.log=self.logging.start();self.addCleanup(self.logging.stop)
    def tick(self, seconds):
        self.assertEqual(self.held,set(),'No partially acquired locks across a sleep')
        self.clock += seconds
        if self.release_at is not None and self.clock >= self.release_at:self.blocked.clear()
    def progress(self, value):
        self.assertEqual(self.held,set(),'No maintenance locks across heartbeat')
        self.events.append(value)
    def test_immediate_admission_holds_group_until_operation_finishes(self):
        with self.m.maintenance_admission(('a','b')) as r:
            self.assertTrue(r['acquired']);self.assertEqual(self.held,{'a','b'})
        self.assertFalse(self.held);self.sleep.assert_not_called()
    def test_temporary_busy_releases_group_and_executes_once_after_release(self):
        self.blocked={'b'};self.release_at=1;calls=0
        with self.m.maintenance_admission(('a','b'),timeout=3,progress=self.progress) as r:
            self.assertTrue(r['acquired']);calls+=1
        self.assertEqual(calls,1);self.assertEqual(self.clock,1);self.assertTrue(self.events)
        self.m._describe_owners.assert_called_once_with(['b'])
    def test_timeout_is_deferred_and_no_operation_started(self):
        self.blocked={'b'}
        with self.m.maintenance_admission(('a','b'),timeout=1,progress=self.progress) as r:
            self.assertFalse(r['acquired']);self.assertTrue(r['deferred']);self.assertEqual(r['processed'],0)
        self.assertEqual(self.clock,1);self.assertFalse(self.held)
    def test_zero_wait_has_one_attempt_and_preserves_nonautomation_policy(self):
        self.blocked={'b'}
        with self.m.maintenance_admission(('a','b')) as r:self.assertTrue(r['deferred'])
        self.assertEqual(self.attempts,['a','b']);self.sleep.assert_not_called()
    def test_cancel_before_admission_does_not_acquire_any_lock(self):
        with self.m.maintenance_admission(('a',),timeout=2,cancelled=lambda:True) as r:self.assertTrue(r['cancelled'])
        self.assertEqual(self.attempts,[])
    def test_cancel_during_wait_does_not_execute_operation(self):
        self.blocked={'a'}
        with self.m.maintenance_admission(('a',),timeout=2,cancelled=lambda:self.clock>=.5) as r:
            self.assertTrue(r['cancelled']);self.assertFalse(r['acquired'])
        self.assertEqual(self.clock,.5)
    def test_cancel_after_group_acquired_releases_group_without_execution(self):
        calls=iter([False,True])
        with self.m.maintenance_admission(('a','b'),cancelled=lambda:next(calls)) as r:self.assertTrue(r['cancelled'])
        self.assertFalse(self.held)
    def test_failure_after_admission_is_never_replayed(self):
        calls=0
        with self.assertRaisesRegex(RuntimeError,'committed work'):
            with self.m.maintenance_admission(('a','b'),timeout=3):
                calls+=1;raise RuntimeError('committed work; uncertain outcome')
        self.assertEqual(calls,1);self.assertEqual(self.attempts,['a','b']);self.assertFalse(self.held)
    def test_sql_error_acquiring_lock_propagates_without_retry(self):
        self.m.job_lock=Mock(side_effect=RuntimeError('database unavailable'))
        with self.assertRaises(RuntimeError):
            with self.m.maintenance_admission(('a',),timeout=3):self.fail('Must not run')
        self.sleep.assert_not_called()
    def test_diagnostic_failure_does_not_bypass_busy_lock(self):
        self.blocked={'a'};self.m._describe_owners.side_effect=RuntimeError('no permission')
        with self.m.maintenance_admission(('a',),timeout=.5) as r:self.assertTrue(r['deferred'])
        self.assertFalse(self.held)
    def test_heartbeat_failure_stops_before_operation(self):
        self.blocked={'b'}
        with self.assertRaises(RuntimeError):
            with self.m.maintenance_admission(('a','b'),timeout=2,progress=Mock(side_effect=RuntimeError('journal unavailable'))):self.fail('Must not run')
        self.assertFalse(self.held)
    def test_invalid_wait_budget_and_duplicate_locks_rejected(self):
        for names,timeout in [((),1),(('a','a'),1),(('a',),-1),(('a',),301),(('a',),float('nan')),(('a',),float('inf'))]:
            with self.assertRaises(ValueError):
                with self.m.maintenance_admission(names,timeout=timeout):self.fail('Must not run')


if __name__=='__main__':unittest.main()

"""Isolated worker contracts; no app import, network, or live database."""
import ast
import datetime
import hashlib
import json
import logging
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / 'services/license_guard_service.py'


class Finished(BaseException):
    pass


def extracted(name, namespace):
    tree = ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), 'exec'), namespace)
    return namespace[name]


class StartupRetry(unittest.TestCase):
    def worker(self, probes=(True,), heals=(True,), publications=(True,), steps=1):
        sleeps = []
        def sleep(delay):
            sleeps.append(delay)
            if len(sleeps) == steps:
                raise Finished()
        def scripted(values):
            values = iter(values)
            def call(*args, **kwargs):
                value = next(values)
                if isinstance(value, Exception):
                    raise value
                return value
            return Mock(side_effect=call)
        probe, heal, publish = map(scripted, (probes, heals, publications))
        module = types.ModuleType('database.schema_healer')
        module.heal_database_schema = heal
        ns = dict(query_one=probe, publish_radius_license_state=publish,
                  time=types.SimpleNamespace(sleep=sleep), logger=logging.getLogger('startup-test'))
        worker = extracted('_run_radius_license_state_worker', ns)
        with patch.dict(sys.modules, {'database.schema_healer': module}):
            with self.assertRaises(Finished):
                worker()
        return sleeps, probe, heal, publish

    def test_first_attempt_has_no_initial_sleep(self):
        sleeps, probe, heal, publish = self.worker()
        probe.assert_called_once_with('SELECT 1 AS license_database_ready')
        heal.assert_called_once_with(backfill=False)
        publish.assert_called_once_with()
        self.assertEqual(sleeps, [30])

    def test_database_unavailable_does_not_run_schema_or_publish(self):
        sleeps, _, heal, publish = self.worker(probes=[ConnectionError()])
        self.assertEqual(sleeps, [2])
        heal.assert_not_called()
        publish.assert_not_called()

    def test_database_recovery_retries_in_two_seconds(self):
        sleeps, _, heal, publish = self.worker(probes=[ConnectionError(), True], steps=2)
        self.assertEqual(sleeps, [2, 30])
        self.assertEqual(heal.call_count, 1)
        self.assertEqual(publish.call_count, 1)

    def test_schema_failure_never_publishes(self):
        sleeps, _, _, publish = self.worker(heals=[False])
        self.assertEqual(sleeps, [2])
        publish.assert_not_called()

    def test_schema_failure_recovers_without_thirty_second_wait(self):
        sleeps, _, heal, publish = self.worker(probes=[True, True], heals=[False, True], steps=2)
        self.assertEqual(sleeps, [2, 30])
        self.assertEqual(heal.call_count, 2)
        publish.assert_called_once_with()

    def test_publication_failure_retries_without_reinstalling_schema(self):
        sleeps, probe, heal, publish = self.worker(publications=[RuntimeError(), True], steps=2)
        self.assertEqual(sleeps, [2, 30])
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(heal.call_count, 1)
        self.assertEqual(publish.call_count, 2)

    def test_outage_backoff_is_bounded(self):
        sleeps, _, _, _ = self.worker(probes=[ConnectionError()] * 6, steps=6)
        self.assertEqual(sleeps, [2, 4, 8, 10, 10, 10])

    def test_recovery_resets_backoff(self):
        sleeps, _, _, _ = self.worker(publications=[RuntimeError(), RuntimeError(), True, RuntimeError()], steps=4)
        self.assertEqual(sleeps, [2, 4, 30, 2])

    def test_healthy_refresh_does_not_repeat_schema_migration(self):
        sleeps, probe, heal, publish = self.worker(publications=[True]*3, steps=3)
        self.assertEqual(sleeps, [30]*3)
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(heal.call_count, 1)
        self.assertEqual(publish.call_count, 3)


class LeaseBounds(unittest.TestCase):
    def publish(self, valid=True, verified=True, capacity_error=False, expires=None, lifetime=False):
        row = {'id': 1, 'raw_package_json': json.dumps({'payload': {'expires_at': expires}})}
        write = Mock()
        ns = dict(datetime=datetime, json=json, hashlib=hashlib,
                  get_active_license_status=lambda **_: {'valid': valid, 'capacity_read_error': capacity_error},
                  query_one=lambda _: row,
                  verify_license_package=lambda _: (verified, '', {'is_lifetime': lifetime, 'license_mode': 'active_sessions', 'max_active_sessions': 5}),
                  execute_write=write)
        before = datetime.datetime.now(datetime.timezone.utc)
        extracted('publish_radius_license_state', ns)()
        return before, write.call_args.args[1]

    def test_verified_lease_remains_bounded_to_ninety_seconds(self):
        before, params = self.publish()
        self.assertEqual(params[2], 1)
        delta = (params[-1].replace(tzinfo=datetime.timezone.utc) - before).total_seconds()
        self.assertGreaterEqual(delta, 89)
        self.assertLess(delta, 91)

    def test_invalid_signature_is_never_authorized(self):
        self.assertEqual(self.publish(verified=False)[1][2], 0)

    def test_invalid_status_clock_or_revocation_is_never_authorized(self):
        self.assertEqual(self.publish(valid=False)[1][2], 0)

    def test_capacity_read_failure_is_never_authorized(self):
        self.assertEqual(self.publish(capacity_error=True)[1][2], 0)

    def test_package_expiry_caps_lease(self):
        expiry = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=20)
        _, params = self.publish(expires=expiry.isoformat())
        self.assertEqual(params[-1], expiry.replace(tzinfo=None))


if __name__ == '__main__':
    unittest.main()

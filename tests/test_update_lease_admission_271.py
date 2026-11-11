"""Fresh natural lease admission; no Docker, license publication or database."""
import unittest
from unittest.mock import patch

from services import system_update_executor as engine
from services.system_update_safety import Budget, SafetyError


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.plan = dict(previous_version='2.7.0', publisher_key='fixture')

    def proof(self, remaining=88, **extra):
        return dict(verified=True, http=True, license=True, lease=True,
                    lease_remaining=remaining, version='2.7.0', publisher_key='fixture', **extra)

    def sleep(self, seconds):
        self.now += seconds

    def wait(self):
        return engine.await_switch_lease(None, 'retained', self.plan,
                                        clock=lambda: self.now, sleep=self.sleep)

    def test_observed_failure_budget_cannot_admit_a_switch(self):
        with self.assertRaises(SafetyError): Budget(self.proof(60.0355))

    def test_waits_for_natural_renewal_with_original_still_running(self):
        with patch.object(engine, 'execute', side_effect=[self.proof(60.0355), self.proof(88)]) as probe:
            result = self.wait()
        self.assertEqual(result['lease_remaining'], 88)
        self.assertEqual(self.now, 2)
        self.assertEqual(probe.call_count, 2)
        self.assertTrue(all(c.args[2] == engine.SAFE_PROBE for c in probe.call_args_list))

    def test_fresh_proof_needs_no_wait(self):
        with patch.object(engine, 'execute', return_value=self.proof()): self.wait()
        self.assertEqual(self.now, 0)

    def test_below_sixty_is_allowed_to_wait_but_never_to_switch(self):
        with patch.object(engine, 'execute', side_effect=[self.proof(59.4), self.proof(88)]):
            self.assertEqual(self.wait()['lease_remaining'], 88)
        self.assertEqual(self.now, 2)
        with self.assertRaises(SafetyError): Budget(self.proof(59.4))

    def test_no_renewal_times_out_before_any_service_change(self):
        with patch.object(engine, 'execute', return_value=self.proof(70)):
            with self.assertRaises(SafetyError): self.wait()
        self.assertEqual(self.now, 40)

    def test_revoked_unhealthy_or_stale_never_admitted(self):
        for field in ('verified', 'http', 'license', 'lease'):
            proof = self.proof(); proof[field] = False
            with self.subTest(field=field), patch.object(engine, 'execute', return_value=proof):
                with self.assertRaises(SafetyError): self.wait()
        with patch.object(engine, 'execute', return_value=self.proof(0)):
            with self.assertRaises(SafetyError): self.wait()
        self.assertEqual(self.now, 0)

    def test_identity_change_during_wait_is_rejected(self):
        changed = self.proof(); changed['publisher_key'] = 'different'
        with patch.object(engine, 'execute', side_effect=[self.proof(70), changed]):
            with self.assertRaises(engine.UpdateError): self.wait()

    def test_minimum_fresh_lease_preserves_existing_recovery_reserve(self):
        b = Budget(self.proof(85), clock=lambda: 0)
        self.assertEqual(b.forward, 40)
        self.assertEqual(b.recovery, 75)
        with self.assertRaises(SafetyError): Budget(self.proof(84.99))


if __name__ == '__main__': unittest.main()

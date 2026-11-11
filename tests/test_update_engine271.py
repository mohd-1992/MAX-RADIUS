"""Lease-safe actual executor flow using the existing isolated Docker boundary."""
import copy
from unittest.mock import patch
from test_control_update_consumer import EngineTests
from services import system_update_executor as e
from services import system_update_runtime as r


class ProtectedEngineTests(EngineTests):
    def setUp(self):
        super().setUp()
        self.plan.update(version='2.7.1',previous_version='2.7.0')
        core=copy.deepcopy(self.old);core.update(Id='8'*64,Name='/owned_core')
        core['Config']['Labels']['com.docker.compose.service']='core'
        self.api.rows[core['Id']]=core;self.plan['old']['core']=copy.deepcopy(core)
        self.remaining=90
        r.write_json(self.folder/'plan.private.json',self.plan)

    def runjob(self,wait=None):
        proof=dict(verified=True,http=True,license=True,lease=True,lease_remaining=self.remaining,lease_deadline='2026-11-07T16:00:00.000000Z',version='2.7.0',publisher_key='fixture',rows=[])
        with patch.object(e,'execute',return_value=proof):
            return super().runjob(wait)

    def test_short_lease_leaves_all_services_untouched(self):
        self.remaining=20
        clock=[0]
        with patch.object(e.time,'monotonic',side_effect=lambda:clock[0]),patch.object(e.time,'sleep',side_effect=lambda n:clock.__setitem__(0,clock[0]+n)):
            self.assertEqual(self.runjob()['status'],'error')
        self.assertEqual(clock[0],40)
        self.assertFalse(any('/stop?' in c[1] for c in self.api.calls))

    def test_core_routing_change_rejects_candidate_and_restores_web(self):
        def wait(*a,**kw):
            if not kw.get('rollback'):
                self.api.rows['8'*64]['NetworkSettings']['Networks']['owned_net']['IPAddress']='172.31.1.9'
        self.assertEqual(self.runjob(wait)['status'],'recovery_required')
        self.assertTrue(self.api.rows[self.old['Id']]['State']['Running'])

    def test_interrupted_recovery_requires_real_http_even_without_original_budget_object(self):
        clock=[0]
        proof=dict(verified=True,http=False,license=True,lease=True,version='2.7.0',publisher_key='fixture',lease_deadline='2026-11-07 16:00:01')
        with patch.object(e,'execute',return_value=proof):
            with self.assertRaises(r.UpdateError):
                e.wait_ready(self.api,self.old['Id'],'web',self.plan,rollback=True,clock=lambda:clock[0],sleep=lambda n:clock.__setitem__(0,clock[0]+n))
        self.assertEqual(clock[0],35)

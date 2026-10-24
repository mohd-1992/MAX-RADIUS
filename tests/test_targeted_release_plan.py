"""Isolated scope/readiness checks; no application, Docker, DB or network imports."""
import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('targeted_release_plan', Path(__file__).resolve().parents[1] / 'deploy/hosted-max/targeted_release_plan.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class TargetedReleasePlanTests(unittest.TestCase):
    def setUp(self):
        self.rows = [dict(id=str(i)*64, image='sha256:'+str(i)*64, project='4k', service=service,
                          started='2026-10-22T10:00:00Z', restarts=0, health='healthy',
                          running=True, networks={'4k_tenant_net': {}})
                     for i, service in [(1, 'web'), (2, 'radius_core'), (3, 'db')]]
        self.images = {'web': 'sha256:'+'a'*64, 'core': 'sha256:'+'b'*64}
        self.migration = dict(tested=True, accounting_preserved=True, original_sales_preserved=True, evidence_sha256='c'*64)
        self.now = '2026-10-22T13:00:00Z'
        self.plan = m.prepare(m.DOMAIN, self.rows, self.images, self.migration, self.now)

    def test_preparation_has_no_delivery_or_install(self):
        self.assertEqual(m.readiness(self.plan, {})['state'], 'prepared_blocked')
        self.assertFalse(m.public_status(self.plan, {})['published_to_clients'])

    def test_other_domains_denied(self):
        for domain in ('max.max-net.net', 'as.max-net.net', '4k.max-net.net.evil', '', '*'):
            with self.subTest(domain=domain), self.assertRaises(m.Rejected):
                m.prepare(domain, self.rows, self.images, self.migration, self.now)

    def test_shared_components_and_latest_denied(self):
        for images in ({**self.images, 'db':'sha256:'+'d'*64}, {**self.images, 'l2tp':'sha256:'+'e'*64}, {**self.images, 'web':'mohd777/max-radius-web:latest'}):
            with self.assertRaises(m.Rejected):m.prepare(m.DOMAIN, self.rows, images, self.migration, self.now)

    def test_duplicate_live_core_denied(self):
        with self.assertRaises(m.Rejected):m.prepare(m.DOMAIN, self.rows+[copy.deepcopy(self.rows[1])], self.images, self.migration, self.now)

    def test_foreign_network_consumer_denied(self):
        foreign={**self.rows[1], 'id':'f'*64, 'project':'max'}
        with self.assertRaises(m.Rejected):m.prepare(m.DOMAIN, self.rows+[foreign], self.images, self.migration, self.now)

    def test_proxy_display_shared_labels_are_not_authority(self):
        self.rows[1]['shared']=True
        with self.assertRaises(m.Rejected):m.prepare(m.DOMAIN, self.rows, self.images, self.migration, self.now)

    def test_current_identity_and_restart_are_rechecked(self):
        for key,value in [('id','f'*64),('image','sha256:'+'f'*64),('restarts',1),('started','2026-10-22T12:00:00Z'),('health','unhealthy')]:
            rows=copy.deepcopy(self.rows);rows[1][key]=value
            with self.subTest(key=key),self.assertRaises(m.Rejected):m.validate_current(self.plan,m.DOMAIN,rows,self.now)

    def test_tampered_plan_denied(self):
        self.plan['domain']='max.max-net.net'
        with self.assertRaises(m.Rejected):m.validate_current(self.plan,m.DOMAIN,self.rows,self.now)

    def test_network_and_storage_change_without_restart_denied(self):
        for key, value in [('networks', {'shared_net': {}}), ('mounts', [{'Destination':'/app','Source':'/foreign'}]), ('shared', True)]:
            rows=copy.deepcopy(self.rows);rows[1][key]=value
            with self.subTest(key=key),self.assertRaises(m.Rejected):m.validate_current(self.plan,m.DOMAIN,rows,self.now)
        foreign={**self.rows[1], 'id':'f'*64, 'project':'as'}
        with self.assertRaises(m.Rejected):m.validate_current(self.plan,m.DOMAIN,self.rows+[foreign],self.now)

    def test_expired_and_future_plan_denied(self):
        for now in ('2026-10-23T13:00:00Z','2026-10-22T12:59:59Z'):
            with self.assertRaises(m.Rejected):m.validate_current(self.plan,m.DOMAIN,self.rows,now)

    def test_evidence_for_another_plan_does_not_unlock(self):
        certs={k:dict(passed=True,plan_sha256='wrong',evidence_sha256='e'*64) for k in m.CERTIFICATES}
        self.assertFalse(m.readiness(self.plan,certs)['ready'])

    def test_all_gates_required_and_false_is_not_pass(self):
        certs={k:dict(passed=True,plan_sha256=self.plan['plan_sha256'],evidence_sha256='e'*64) for k in m.CERTIFICATES}
        self.assertTrue(m.readiness(self.plan,certs)['ready'])
        for k in certs:
            bad=copy.deepcopy(certs);bad[k]['passed']=False
            self.assertFalse(m.readiness(self.plan,bad)['ready'])

    def test_clone_financial_or_accounting_failure_blocks(self):
        for k in ('tested','accounting_preserved','original_sales_preserved'):
            bad={**self.migration,k:False}
            with self.assertRaises(m.Rejected):m.prepare(m.DOMAIN,self.rows,self.images,bad,self.now)


if __name__ == '__main__':unittest.main()

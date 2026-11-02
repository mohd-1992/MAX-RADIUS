"""The hosted compatibility view must never grant a tenant a host Docker API."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'deploy/hosted-max/tenant_docker_proxy.py'
spec = importlib.util.spec_from_file_location('tenant_proxy', SOURCE)
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.binding = Path(self.tmp.name) / 'binding.json'
        self.row = dict(id='tenant1', slug='m', domain='m.max-r.cc', state='ready', pool='a', instance='INST-TEST',
                        containers={'web': 'mrcc-m-web', 'core': 'mrcc-m-core'},
                        images={'web': 'web@sha256:1', 'core': 'core@sha256:2'})
        self.binding.write_text(json.dumps(self.row))
        self.rows = {}
        for i, role in enumerate(('web', 'core', 'db'), 1):
            name = self.row['containers'].get(role, 'max-db-a')
            self.rows[name] = dict(Id=str(i) * 64, Name='/' + name,
                                  Created='2026-11-01T00:00:00Z', Image='sha256:' + str(i) * 64,
                                  State={'Running': True, 'Status': 'running', 'Health': {'Status': 'healthy', 'Log': ['secret']}},
                                  Config={'Env': ['DB_PASSWORD=secret'],
                                          'Image': self.row['images'].get(role, 'db'),
                                          'Labels': {'max.platform': 'max-r', 'max.tenant': 'tenant1',
                                                     'max.service': role, 'max.pool': 'a'}},
                                  Mounts=[{'Source': '/secret-host'}])
        self.effects = []

        def api(method, path):
            if method == 'POST':
                self.effects.append(path)
                return 204, b''
            ref = path.split('/')[2]
            row = self.rows.get(ref) or next((r for r in self.rows.values() if r['Id'] == ref), None)
            if row is None:
                raise p.Denied('Not owned')
            return 200, json.dumps(row).encode()
        self.api = api
        self.scope = p.Scope({'binding': str(self.binding), 'tenant_id': 'tenant1', 'slug': 'm'}, api)

    def test_view_lists_only_three_owned_services_and_no_secrets(self):
        status, raw = self.scope.handle('GET', '/containers/json?all=1')
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(raw)), 3)
        self.assertNotIn(b'secret', raw)
        self.assertEqual({r['Labels']['com.docker.compose.project'] for r in json.loads(raw)}, {'mrcc-tenant1'})

    def test_inspect_alias_is_scoped_and_redacted(self):
        status, raw = self.scope.handle('GET', '/containers/max_radius_core/json')
        self.assertEqual(json.loads(raw)['Id'], '2' * 64)
        self.assertNotIn(b'secret', raw)
        self.assertNotIn(b'Mounts', raw)

    def test_native_contract_has_real_ownership_and_shared_scope(self):
        _, raw = self.scope.handle('GET', '/max/v1/state')
        data = json.loads(raw)
        self.assertEqual(data['tenant_id'], 'tenant1')
        self.assertEqual(data['instance'], 'INST-TEST')
        self.assertTrue(data['services']['db']['read_only'])
        self.assertNotIn('com.docker.compose.project', data['services']['web']['Config']['Labels'])
        self.assertNotIn(b'secret', raw)

    def test_shared_engine_readable_but_never_mutable(self):
        self.assertEqual(self.scope.handle('GET', '/containers/max_radius_db/json')[0], 200)
        for action in ('restart', 'start', 'stop', 'kill', 'exec'):
            with self.subTest(action=action), self.assertRaises(p.Denied):
                self.scope.handle('POST', '/containers/max_radius_db/' + action)
        self.assertEqual(self.effects, [])

    def test_foreign_container_and_prefix_and_host_api_denied(self):
        for path in ('/containers/foreign/json', '/containers/' + '2' * 12 + '/json',
                     '/containers/json?filters=all', '/containers/create', '/images/json',
                     '/containers/../json', '/containers/%2e%2e/json', 'http://remote/containers/json',
                     '/containers/max_radius_core/exec', '/containers/max_radius_core/logs?tail=all'):
            with self.subTest(path=path), self.assertRaises(p.Denied):
                self.scope.handle('GET', path)
        self.assertEqual(self.effects, [])

    def test_changed_image_or_owner_fails_closed(self):
        for field, value in (('Image', 'unapproved'), ('Labels', {'max.tenant': 'other'})):
            old = copy.deepcopy(self.rows['mrcc-m-core'])
            self.rows['mrcc-m-core']['Config'][field] = value
            with self.assertRaises(p.Denied):
                self.scope.handle('GET', '/containers/json')
            self.rows['mrcc-m-core'] = old

    def test_changed_binding_and_pool_fail_closed(self):
        self.row['id'] = 'replacement'
        self.binding.write_text(json.dumps(self.row))
        with self.assertRaises(p.Denied):
            self.scope.handle('GET', '/containers/json')
        self.row['id'] = 'tenant1'
        self.binding.write_text(json.dumps(self.row))
        self.rows['max-db-a']['Config']['Labels']['max.pool'] = 'b'
        with self.assertRaises(p.Denied):
            self.scope.handle('GET', '/containers/json')

    def test_restart_resolves_full_owned_id(self):
        self.assertEqual(self.scope.handle('POST', '/containers/max_radius_core/restart?t=5')[0], 204)
        self.assertEqual(self.effects, ['/containers/' + '2' * 64 + '/restart?t=5'])

    def test_identity_race_before_action_denied(self):
        calls = [0]
        def racing(method, path):
            calls[0] += 1
            if calls[0] == 4:
                self.rows['mrcc-m-core']['Id'] = '9' * 64
            return self.api(method, path)
        self.scope.api = racing
        with self.assertRaises(p.Denied):
            self.scope.handle('POST', '/containers/max_radius_core/restart')
        self.assertEqual(self.effects, [])

    def test_unavailable_host_has_no_substitute(self):
        def unavailable(*args):
            raise p.Denied('Unavailable')
        self.scope.api = unavailable
        with self.assertRaises(p.Denied):
            self.scope.handle('GET', '/containers/max_radius_core/json')


if __name__ == '__main__':
    unittest.main()

"""Native CPU isolation and sample contract; no application or Docker imports."""
import copy
import importlib.util
from pathlib import Path
import unittest
import tempfile
import socket

SPEC = importlib.util.spec_from_file_location('native_metrics', Path(__file__).resolve().parents[1] / 'deploy/hosted-max/native_tenant_resource_proxy.py')
metrics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(metrics)


class NativeMetricsTests(unittest.TestCase):
    def setUp(self):
        self.row = {'id': 'target-id', 'slug': 'max1', 'domain': 'max1.max-r.cc', 'state': 'ready',
                    'containers': {'web': 'owned-web', 'core': 'owned-core'},
                    'images': {'web': 'web@sha256:1', 'core': 'core@sha256:2'}}
        self.ids = {'web': 'a' * 64, 'core': 'b' * 64}
        self.calls = []
        self.used, self.system, self.previous = 10, 1000, 1000
        self.extra_labels = {}
        self.image_mismatch = False
        self.now = 100.0
        self.value = metrics.Metrics({k: self.row[k] for k in ('id', 'slug', 'domain')}, self.api,
                                     lambda: self.now, lambda: copy.deepcopy(self.row))

    def api(self, path):
        self.calls.append(path)
        if path.endswith('/json'):
            role = 'web' if path == '/containers/owned-web/json' else 'core'
            self.assertIn(path, ['/containers/owned-web/json', '/containers/owned-core/json'])
            return {'Id': self.ids[role], 'State': {'Running': True}, 'Config': {
                'Image': 'wrong' if self.image_mismatch else self.row['images'][role],
                'Labels': {'max.platform': 'max-r', 'max.tenant': 'target-id', 'max.service': role, **self.extra_labels}}}
        self.assertIn(path, ['/containers/' + x + '/stats?stream=false' for x in self.ids.values()])
        return {'cpu_stats': {'system_cpu_usage': self.previous + self.system, 'cpu_usage': {'total_usage': 100 + self.used}},
                'precpu_stats': {'system_cpu_usage': self.previous, 'cpu_usage': {'total_usage': 100}}}

    def get(self):
        return self.value.handle('GET', '/_max_metrics/v1/resources')

    def test_real_sample_only_owned_web_core(self):
        status, value = self.get()
        self.assertEqual(status, 200)
        self.assertEqual(value['cpu_percent'], 2.0)
        self.assertEqual(value['cpu_basis'], 'host_capacity')
        self.assertEqual(set(value['services']), {'web', 'core'})
        self.assertEqual(set(value), {'schema', 'tenant', 'cpu_available', 'cpu_percent', 'cpu_basis', 'sampled_at', 'services'})
        self.assertFalse(any('db' in x for x in self.calls))

    def test_zero_is_real_zero(self):
        self.used = 0
        status, value = self.get()
        self.assertEqual(status, 200)
        self.assertEqual(value['cpu_percent'], 0)
        self.assertIs(value['cpu_available'], True)

    def test_no_external_tenant_or_docker_or_write_routes(self):
        for method, path in [('POST', '/_max_metrics/v1/resources'), ('GET', '/containers/json'),
                             ('GET', '/_max_metrics/v1/resources?tenant=m'), ('GET', '/_max_metrics/v1/resources#x'),
                             ('GET', 'http://remote/_max_metrics/v1/resources'), ('DELETE', '/containers/owned-web')]:
            self.assertEqual(self.value.handle(method, path)[0], 403)
        self.assertEqual(self.calls, [])

    def test_foreign_binding_denied(self):
        self.row['id'] = 'other'
        self.assertEqual(self.get()[0], 503)
        self.assertEqual(self.calls, [])

    def test_foreign_labels_denied(self):
        self.extra_labels['max.tenant'] = 'other'
        self.assertEqual(self.get()[0], 503)
        self.assertFalse(any('/stats' in x for x in self.calls))

    def test_shared_service_denied(self):
        self.extra_labels['max.pool'] = 'b'
        self.assertEqual(self.get()[0], 503)
        self.assertFalse(any('/stats' in x for x in self.calls))

    def test_image_changed_denied(self):
        self.image_mismatch = True
        self.assertEqual(self.get()[0], 503)

    def test_negative_delta_denied(self):
        self.used = -1
        self.assertEqual(self.get()[0], 503)

    def test_unready_system_sample_denied(self):
        self.system = 0
        self.assertEqual(self.get()[0], 503)
        self.system, self.previous = 1000, 0
        self.assertEqual(self.get()[0], 503)

    def test_cache_expires_and_validates_binding(self):
        self.assertEqual(self.get()[0], 200)
        samples = sum('/stats' in x for x in self.calls)
        self.assertEqual(self.get()[0], 200)
        self.assertEqual(sum('/stats' in x for x in self.calls), samples)
        self.row['state'] = 'deleted'
        self.assertEqual(self.get()[0], 503)
        self.assertIsNone(self.value.cache)
        self.row['state'], self.now = 'ready', 104
        self.assertEqual(self.get()[0], 200)
        self.assertGreater(sum('/stats' in x for x in self.calls), samples)

    def test_identity_changed_during_sample_denied(self):
        original = self.value.backend
        def changed(path):
            value = original(path)
            if '/stats' in path:
                self.ids['web'] = 'c' * 64
            return value
        self.value.backend = changed
        self.assertEqual(self.get()[0], 503)

    def test_duplicate_service_identity_denied(self):
        self.ids['core'] = self.ids['web']
        self.assertEqual(self.get()[0], 503)

    def test_existing_regular_file_never_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'resource.sock'
            path.write_text('owned data')
            with self.assertRaises(ValueError):
                metrics.prepare_socket(path)
            self.assertEqual(path.read_text(), 'owned data')

    @unittest.skipUnless(hasattr(__import__('os'), 'getuid') and __import__('os').getuid() == 0, 'Linux root-owned socket fixture')
    def test_stale_socket_recovered_live_socket_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'resource.sock'
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(str(path))
                server.listen(2)
                identity = path.stat().st_ino
                with self.assertRaises(ValueError):
                    metrics.prepare_socket(path)
                self.assertEqual(path.stat().st_ino, identity)
            metrics.prepare_socket(path)
            self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()

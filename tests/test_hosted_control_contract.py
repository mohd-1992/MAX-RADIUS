"""Hosted/local admission, readiness, UI semantics, and host lifecycle isolation."""
import ast
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import socket
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


h = load('hosted_contract', ROOT / 'services/hosted_control_service.py')
lifecycle = load('hosted_lifecycle', ROOT / 'deploy/hosted-max/tenant_control_lifecycle.py')


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.tenant = 'a' * 32
        self.env = {'MAX_CONTROL_MODE': 'hosted', 'MAX_CONTROL_SOCKET': '/private/control.sock',
                    'MAX_CONTROL_TENANT_ID': self.tenant, 'INSTANCE_UUID': 'INST-ABCDEF012345',
                    'CLIENT_SUBDOMAIN': 'm'}
        self.patcher = patch.dict(os.environ, self.env, clear=True)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.rows = {}
        for n, role in enumerate(('web', 'core', 'db'), 1):
            self.rows[role] = {'Id': str(n) * 64, 'Name': '/tenant-' + role,
                               'Config': {'Image': role, 'Labels': {'max.platform': 'max-r',
                                          'max.tenant': self.tenant, 'max.service': role}},
                               'State': {'Running': True, 'Status': 'running', 'Health': {'Status': 'healthy'}},
                               'read_only': role == 'db'}
        self.data = {'schema': 1, 'tenant_id': self.tenant, 'instance': self.env['INSTANCE_UUID'],
                     'domain': 'm.max-r.cc', 'services': self.rows}
        auto = types.ModuleType('services.autoheal_service')
        auto._runtime_container_ids = lambda: {'1' * 64}
        probe = types.ModuleType('services.health_probe_service')
        probe.radius_probe = lambda port: (True, 'verified')
        watchdog = types.ModuleType('core.watchdog')
        watchdog._watchdog_thread = types.SimpleNamespace(is_alive=lambda: True)
        core = types.ModuleType('core')
        core.watchdog = watchdog
        self.modules = patch.dict(sys.modules, {'services.autoheal_service': auto,
                                 'services.health_probe_service': probe, 'core': core})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.request = patch.object(h, 'request', return_value=self.data)
        self.request.start()
        self.addCleanup(self.request.stop)
        h._probe_cache = (0.0, None)

    def test_local_default_and_explicit_local_preserve_legacy_selection(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(h.configured())
        with patch.dict(os.environ, {'MAX_CONTROL_MODE': 'local'}, clear=True):
            self.assertFalse(h.configured())

    def test_partial_hosted_configuration_never_falls_back(self):
        with patch.dict(os.environ, {'MAX_CONTROL_SOCKET': '/x'}, clear=True):
            self.assertTrue(h.configured())

    def test_hosted_core_identity_and_verified_status(self):
        self.assertEqual(h.current_core()['Id'], '2' * 64)

    def test_foreign_tenant_instance_or_domain_denied(self):
        for field in ('tenant_id', 'instance', 'domain'):
            original = self.data[field]
            self.data[field] = 'foreign'
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                h.state()
            self.data[field] = original

    def test_foreign_runtime_or_core_owner_denied(self):
        self.rows['web']['Id'] = '9' * 64
        with self.assertRaises(RuntimeError):
            h.state()
        self.rows['web']['Id'] = '1' * 64
        self.rows['core']['Config']['Labels']['max.tenant'] = 'foreign'
        with self.assertRaises(RuntimeError):
            h.state()

    def test_core_stopped_unhealthy_or_unverified_denied(self):
        self.rows['core']['State']['Running'] = False
        with self.assertRaises(RuntimeError):
            h.current_core()
        self.rows['core']['State']['Running'] = True
        with patch.object(h, 'core_health', return_value=None), self.assertRaises(RuntimeError):
            h.current_core()

    def test_shared_database_cannot_become_writable(self):
        self.rows['db']['read_only'] = False
        with self.assertRaises(RuntimeError):
            h.state()

    def test_fleet_excludes_external_and_internal_from_container_health_count(self):
        rows = h.fleet()
        self.assertEqual(len(rows), 5)
        self.assertEqual(sum(r['monitored'] for r in rows), 3)
        self.assertEqual(sum(r['monitored'] and r['is_healthy'] for r in rows), 3)
        self.assertFalse(next(r for r in rows if r['role'] == 'database')['can_restart'])
        self.assertEqual(next(r for r in rows if r['role'] == 'vpn')['state'], 'external')
        self.assertTrue(next(r for r in rows if r['role'] == 'autoheal')['is_healthy'])

    def test_only_own_core_or_web_can_resolve(self):
        self.assertEqual(h.resolve('2' * 64, True)[0]['Id'], '2' * 64)
        self.assertIsNone(h.resolve('3' * 64, True)[0])
        self.assertIsNone(h.resolve('9' * 64, True)[0])


class LocalGuardTests(unittest.TestCase):
    def test_actual_legacy_compose_guard_still_resolves_only_own_project(self):
        source = ast.parse((ROOT / 'services/renewal_settlement_service.py').read_text(encoding='utf-8'))
        nodes = [n for n in source.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))
                 and n.name in ('SettlementPending', 'current_radius_container')]
        namespace = {}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), '<guard>', 'exec'), namespace)
        web = {'Id': '1' * 64, 'State': 'running', 'Labels': {'com.docker.compose.project': 'local', 'com.docker.compose.service': 'web'}}
        core = {'Id': '2' * 64, 'State': 'running', 'Labels': {'com.docker.compose.project': 'local', 'com.docker.compose.service': 'radius_core'}}
        foreign = {'Id': '3' * 64, 'State': 'running', 'Labels': {'com.docker.compose.project': 'other', 'com.docker.compose.service': 'radius_core'}}
        fleet = [web, core, foreign]
        def api(method, path):
            if path.startswith('/containers/json'):
                return fleet, None
            row = next(x for x in fleet if x['Id'] in path)
            return {'Id': row['Id'], 'Config': {'Labels': row['Labels']}, 'State': {'Running': True, 'Health': {'Status': 'healthy'}}}, None
        fake = types.ModuleType('services.autoheal_service')
        fake.docker_api_request = api
        fake._runtime_container_ids = lambda: {'1' * 64}
        with patch.dict(os.environ, {}, clear=True), patch.dict(sys.modules, {'services.autoheal_service': fake}):
            self.assertEqual(namespace['current_radius_container']()['Id'], '2' * 64)
            core['Labels']['com.docker.compose.project'] = 'other'
            with self.assertRaises(namespace['SettlementPending']):
                namespace['current_radius_container']()


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root, self.system = Path(self.temp.name) / 'host', Path(self.temp.name) / 'etc'
        self.row = {'id': 'a' * 32, 'slug': 'm', 'domain': 'm.max-r.cc'}
        binding = self.root / 'tenants/m/binding.json'
        binding.parent.mkdir(parents=True)
        binding.write_text(json.dumps(self.row))
        self.calls = []
        def run(args, **kwargs):
            self.calls.append(args)
            return types.SimpleNamespace(returncode=1 if 'is-active' in args else 0)
        self.run = run

    def test_create_replace_and_remove_preserve_binding_and_tenant_storage(self):
        a = lifecycle.ensure(self.row, self.root, self.system, self.run)
        b = lifecycle.ensure(self.row, self.root, self.system, self.run)
        self.assertEqual(a, b)
        data = self.root / 'tenants/m/storage/keep'
        data.write_text('customer data')
        lifecycle.remove(self.row, self.root, self.system, self.run)
        self.assertEqual(data.read_text(), 'customer data')
        self.assertTrue((self.root / 'tenants/m/binding.json').exists())
        self.assertTrue(all('a' * 32 in arg[-1] for arg in self.calls if arg[1] != 'daemon-reload'))

    def test_foreign_binding_and_unsafe_slug_rejected_before_effect(self):
        for row in ({**self.row, 'id': 'b' * 32}, {**self.row, 'slug': '../m'}):
            with self.assertRaises(ValueError):
                lifecycle.ensure(row, self.root, self.system, self.run)
        self.assertEqual(self.calls, [])

    @unittest.skipUnless(os.name == 'posix', 'Unix socket archive handling runs on Linux')
    def test_remove_closes_only_control_socket_before_tenant_archive(self):
        lifecycle.ensure(self.row, self.root, self.system, self.run)
        _, path, _, _ = lifecycle.paths(self.row, self.root, self.system)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            lifecycle.remove(self.row, self.root, self.system, self.run)
            self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()

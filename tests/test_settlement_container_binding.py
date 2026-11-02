"""Read-only Docker binding matrix; no app, SQL, router or daemon imports."""
import ast
import copy
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def container(number, role, project='fixture', state='running'):
    return {'Id': '%064x' % number, 'Names': ['/fixture_' + str(number)],
            'State': state, 'Status': 'Up (healthy)',
            'Labels': {'com.docker.compose.project': project,
                       'com.docker.compose.service': role}}


def load_resolver():
    tree = ast.parse((ROOT / 'services/renewal_settlement_service.py').read_text(encoding='utf-8-sig'))
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))
             and n.name in ('current_radius_container', 'SettlementPending')]
    ns = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'isolated-binding', 'exec'), ns)
    return ns


class SettlementBinding(unittest.TestCase):
    def setUp(self):
        self.ns = load_resolver()
        self.web = container(1, 'wisp-web')
        self.core = container(2, 'freeradius')
        self.old_web = container(3, 'wisp-web', state='exited')
        self.old_core = container(4, 'freeradius', state='created')
        self.foreign = container(5, 'radius_core', project='another-tenant')
        self.fleet = [self.old_core, self.old_web, self.foreign, self.web, self.core]
        self.ids = {self.web['Id']}
        self.requests = []
        self.error = None
        self.inspect_change = lambda row: None
        self.module = types.ModuleType('services.autoheal_service')
        self.module._runtime_container_ids = lambda: self.ids
        self.module.docker_api_request = self.api
        self.modules = patch.dict(sys.modules, {'services.autoheal_service': self.module})
        self.modules.start(); self.addCleanup(self.modules.stop)
        self.env = patch.dict(os.environ, {'HOSTNAME': self.old_web['Id'][:12]})
        self.env.start(); self.addCleanup(self.env.stop)

    def api(self, method, path):
        self.assertEqual(method, 'GET', 'The resolver must never control a container')
        self.requests.append(path)
        if path == '/containers/json?all=1':
            return copy.deepcopy(self.fleet), self.error
        ident = path.split('/')[2]
        row = next((r for r in self.fleet if r['Id'] == ident), None)
        if row is None: return None, 'not found'
        result = {'Id': ident, 'Config': {'Labels': copy.deepcopy(row['Labels'])},
                  'State': {'Running': row['State'] == 'running',
                            'Health': {'Status': 'healthy'}}}
        self.inspect_change(result)
        return result, None

    def resolve(self): return self.ns['current_radius_container']()

    def rejected(self):
        with self.assertRaises(self.ns['SettlementPending']): self.resolve()

    def test_local_domain_role_matrix(self):
        for project in ('max-radius', 'tenant-fixture', 'custom_installation'):
            for web in ('web', 'wisp-web'):
                for core in ('radius_core', 'freeradius', 'core'):
                    with self.subTest(project=project, web=web, core=core):
                        self.web['Labels'].update({'com.docker.compose.project': project,
                                                  'com.docker.compose.service': web})
                        self.core['Labels'].update({'com.docker.compose.project': project,
                                                   'com.docker.compose.service': core})
                        self.assertEqual(self.resolve()['Id'], self.core['Id'])

    def test_retained_containers_and_inherited_hostname(self):
        self.assertEqual(self.resolve()['Id'], self.core['Id'])
        self.assertNotIn('/containers/' + self.old_web['Id'] + '/json', self.requests)

    def test_list_order_does_not_change_selection(self):
        self.fleet.reverse(); self.assertEqual(self.resolve()['Id'], self.core['Id'])

    def test_custom_hostname_uses_proven_runtime(self):
        os.environ['HOSTNAME'] = 'custom-web'; self.assertEqual(self.resolve()['Id'], self.core['Id'])

    def test_foreign_hostname_does_not_change_project(self):
        os.environ['HOSTNAME'] = self.foreign['Id'][:12]
        self.assertEqual(self.resolve()['Id'], self.core['Id'])

    def test_default_id_hostname_fallback(self):
        self.ids = set(); os.environ['HOSTNAME'] = self.web['Id']
        self.assertEqual(self.resolve()['Id'], self.core['Id'])

    def test_default_short_hostname_fallback(self):
        self.ids = set(); self.web['Id'] = 'a' * 64
        os.environ['HOSTNAME'] = self.web['Id'][:12]
        self.assertEqual(self.resolve()['Id'], self.core['Id'])

    def test_stopped_hostname_without_runtime_proof_rejected(self):
        self.ids = set(); self.rejected()

    def test_custom_hostname_without_runtime_proof_rejected(self):
        self.ids = set(); os.environ['HOSTNAME'] = 'arbitrary'; self.rejected()

    def test_conflicting_runtime_ids_rejected(self):
        self.ids.add(self.foreign['Id']); self.rejected()

    def test_missing_runtime_id_never_uses_foreign_hint(self):
        self.ids = {'f' * 64}; os.environ['HOSTNAME'] = self.foreign['Id']; self.rejected()

    def test_two_running_cores_rejected(self):
        self.fleet.append(container(9, 'freeradius')); self.rejected()

    def test_no_running_core_rejected(self):
        self.core['State'] = 'exited'; self.rejected()

    def test_only_foreign_core_rejected(self):
        self.fleet.remove(self.core); self.rejected()

    def test_no_project_label_rejected(self):
        self.web['Labels'].pop('com.docker.compose.project'); self.rejected()

    def test_wrong_self_role_rejected(self):
        self.web['Labels']['com.docker.compose.service'] = 'db'; self.rejected()

    def test_oneoff_core_is_not_a_service(self):
        extra = container(9, 'freeradius'); extra['Labels']['com.docker.compose.oneoff'] = 'True'
        self.fleet.append(extra); self.assertEqual(self.resolve()['Id'], self.core['Id'])

    def test_engine_failure_rejected(self):
        self.error = 'socket unavailable'; self.rejected()

    def test_inspect_changed_project_rejected(self):
        self.inspect_change = lambda row: row['Config']['Labels'].update({'com.docker.compose.project': 'foreign'})
        self.rejected()

    def test_inspect_changed_id_rejected(self):
        self.inspect_change = lambda row: row.update(Id='f' * 64); self.rejected()

    def test_inspect_stopped_paused_restarting_unhealthy_rejected(self):
        for state in ({'Running': False}, {'Paused': True}, {'Restarting': True},
                      {'Health': {'Status': 'unhealthy'}}, {'Health': {'Status': 'starting'}}):
            with self.subTest(state=state):
                self.inspect_change = lambda row: row['State'].update(state)
                self.rejected()

    def test_containers_without_healthcheck_supported(self):
        self.inspect_change = lambda row: row['State'].pop('Health', None)
        self.assertEqual(self.resolve()['Id'], self.core['Id'])

    def test_domain_scoped_proxy_metadata_supported(self):
        # The hosted proxy intentionally omits HostConfig, Env and networking.
        def scoped(row):
            row['State'] = {key: row['State'].get(key) for key in ('Running', 'Restarting', 'Health')}
            row['Config']['Labels']['max.panel.tenant'] = 'fixture'
        self.inspect_change = scoped
        self.web['Labels']['com.docker.compose.service'] = 'web'
        self.core['Labels']['com.docker.compose.service'] = 'radius_core'
        self.assertEqual(self.resolve()['Id'], self.core['Id'])


if __name__ == '__main__': unittest.main()

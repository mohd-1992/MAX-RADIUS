"""Control-boundary regressions; actual owned-container checks are separate."""
import ast
import datetime
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
OWN = 'a' * 64


class ServiceControlScopeTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((ROOT / 'services/system_control_service.py').read_text(encoding='utf-8'))
        names = {'format_seconds', 'get_container_info', 'execute_service_action'}
        selected = ast.Module(body=[n for n in tree.body
                                   if isinstance(n, ast.FunctionDef) and n.name in names], type_ignores=[])
        self.hosted = types.ModuleType('services.hosted_control_service')
        self.hosted.configured = lambda: False
        self.autoheal = types.ModuleType('services.autoheal_service')
        self.autoheal.get_container_fleet_status = Mock(return_value=[])
        self.autoheal._resolve_managed_container = Mock(return_value=(None, 'unverified'))
        services = types.ModuleType('services')
        services.hosted_control_service = self.hosted
        self.modules = patch.dict('sys.modules', {'services': services,
                                  'services.hosted_control_service': self.hosted,
                                  'services.autoheal_service': self.autoheal})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.ns = dict(datetime=datetime, subprocess=Mock(), os=Mock(),
                       time=Mock(), threading=Mock(), query_docker_socket=Mock(return_value=True))
        exec(compile(selected, 'system_control_service.py', 'exec'), self.ns)

    def verified(self):
        info = dict(Id=OWN, Name='/owned-core', State=dict(Running=True, Status='running'), Config={})
        self.autoheal.get_container_fleet_status.return_value = [dict(role='radius', container_id=OWN)]
        self.autoheal._resolve_managed_container.return_value = (info, None)

    def test_canonical_name_is_not_a_global_container_lookup(self):
        result = self.ns['get_container_info'](['max_radius_core'])
        self.assertFalse(result['exists'])
        self.ns['query_docker_socket'].assert_not_called()
        self.ns['subprocess'].run.assert_not_called()

    def test_known_role_uses_verified_immutable_id(self):
        self.verified()
        result = self.ns['get_container_info'](['max_radius_core'])
        self.assertEqual(result['container_id'], OWN)
        self.autoheal._resolve_managed_container.assert_called_once_with(OWN, require_id=True)

    def test_ambiguous_role_is_denied(self):
        self.verified()
        self.autoheal.get_container_fleet_status.return_value *= 2
        self.assertFalse(self.ns['get_container_info'](['max_radius_core'])['exists'])
        self.autoheal._resolve_managed_container.assert_not_called()

    def test_unverified_action_has_no_cli_or_systemd_fallback(self):
        self.assertFalse(self.ns['execute_service_action']('freeradius', 'stop')[0])
        self.ns['query_docker_socket'].assert_not_called()
        self.ns['subprocess'].run.assert_not_called()

    def test_verified_action_targets_only_full_id(self):
        self.verified()
        self.assertTrue(self.ns['execute_service_action']('freeradius', 'stop')[0])
        self.ns['query_docker_socket'].assert_called_once_with('/containers/' + OWN + '/stop', method='POST')

    def test_changed_identity_before_action_is_denied(self):
        self.verified()
        good = self.autoheal._resolve_managed_container.return_value
        self.autoheal._resolve_managed_container.side_effect = [good, (None, 'changed project')]
        self.assertFalse(self.ns['execute_service_action']('freeradius', 'restart')[0])
        self.ns['query_docker_socket'].assert_not_called()

    def test_invalid_action_never_resolves_or_writes(self):
        self.assertFalse(self.ns['execute_service_action']('freeradius', 'kill')[0])
        self.autoheal.get_container_fleet_status.assert_not_called()
        self.ns['query_docker_socket'].assert_not_called()

    def test_web_stop_is_denied(self):
        self.ns['get_container_info'] = lambda _: dict(exists=True, container_id=OWN)
        self.assertFalse(self.ns['execute_service_action']('web', 'stop')[0])
        self.ns['query_docker_socket'].assert_not_called()

    def test_failed_web_restart_request_does_not_exit_process(self):
        self.verified()
        self.ns['get_container_info'] = lambda _: dict(exists=True, container_id=OWN)
        self.ns['query_docker_socket'].return_value = None
        self.assertTrue(self.ns['execute_service_action']('web', 'restart')[0])
        callback = self.ns['threading'].Thread.call_args.kwargs['target']
        callback()
        self.ns['os']._exit.assert_not_called()
        self.ns['subprocess'].run.assert_not_called()


class RuntimeIdentityScopeTests(unittest.TestCase):
    def test_inherited_foreign_hostname_cannot_select_project(self):
        tree = ast.parse((ROOT / 'services/autoheal_service.py').read_text(encoding='utf-8'))
        selected = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef)
                                   and n.name == 'get_container_fleet_status'], type_ignores=[])
        foreign = dict(Id='b' * 64, Names=['/max_radius_core'], Labels={
            'com.docker.compose.project': 'foreign', 'com.docker.compose.service': 'core'})
        api = Mock(return_value=([foreign], None))
        namespace = dict(docker_api_request=api, _runtime_container_ids=lambda: set(),
                         _get_fallback_container_fleet=lambda _: [], os=Mock())
        services = types.ModuleType('services')
        services.hosted_control_service = types.SimpleNamespace(configured=lambda: False)
        with patch.dict('sys.modules', {'services': services}):
            exec(compile(selected, 'autoheal_service.py', 'exec'), namespace)
            self.assertEqual(namespace['get_container_fleet_status'](), [])
        api.assert_called_once_with('GET', '/containers/json?all=1')


class ServiceGenerationScopeTests(unittest.TestCase):
    def fleet(self, states):
        tree = ast.parse((ROOT / 'services/autoheal_service.py').read_text(encoding='utf-8'))
        selected = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef)
                                   and n.name == 'get_container_fleet_status'], type_ignores=[])
        web = dict(Id=OWN, Names=['/owned-web'], State='running', Labels={
            'com.docker.compose.project': 'owned', 'com.docker.compose.service': 'web'})
        cores = [dict(Id=str(i) * 64, Names=['/owned-core-' + str(i)], State=state, Labels={
            'com.docker.compose.project': 'owned', 'com.docker.compose.service': 'core'})
                 for i, state in enumerate(states, 1)]
        namespace = dict(datetime=datetime,
                         docker_api_request=lambda *a: ([web] + cores, None),
                         _runtime_container_ids=lambda: {OWN},
                         MONITORED_CONTAINER_NAMES=['max_radius_autoheal', 'max_radius_core',
                                                   'max_radius_web', 'max_radius_db', 'max_radius_l2tp'])
        services = types.ModuleType('services')
        services.hosted_control_service = types.SimpleNamespace(configured=lambda: False)
        with patch.dict('sys.modules', {'services': services}):
            exec(compile(selected, 'autoheal_service.py', 'exec'), namespace)
            return namespace['get_container_fleet_status']()

    def test_unique_stopped_compose_core_remains_addressable(self):
        core = next(v for v in self.fleet(['exited']) if v['role'] == 'radius')
        self.assertEqual(core['container_id'], '1' * 64)
        self.assertEqual(core['state'], 'exited')

    def test_multiple_stopped_generations_do_not_select_rollback(self):
        core = next(v for v in self.fleet(['exited', 'exited']) if v['role'] == 'radius')
        self.assertNotIn('container_id', core)

    def test_running_generation_wins_over_stopped_archive(self):
        core = next(v for v in self.fleet(['exited', 'running']) if v['role'] == 'radius')
        self.assertEqual(core['container_id'], '2' * 64)


if __name__ == '__main__':
    unittest.main()

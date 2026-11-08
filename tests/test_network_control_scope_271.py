"""Native network control contracts, without contacting Docker or real routers."""
import ast
import base64
import os
import re
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


def functions(path, names, namespace):
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    selected = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            node.decorator_list = []
            selected.append(node)
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace


class NetworkControlScopeTests(unittest.TestCase):
    def setUp(self):
        self.services = types.ModuleType('services')
        self.services.__path__ = []
        self.hosted = types.SimpleNamespace(configured=lambda: False)
        self.control = types.ModuleType('services.system_control_service')
        self.control.execute_service_action = Mock(return_value=(True, 'verified'))
        self.tunnels = types.ModuleType('services.l2tp_service')
        self.tunnels.delete_l2tp_tunnel = Mock(return_value=True)
        self.services.hosted_control_service = self.hosted
        self.bridge = types.ModuleType('services.wireguard_service')
        self.bridge._query_wg_bridge = Mock(return_value={'success': True})
        self.modules = patch.dict('sys.modules', {
            'services': self.services, 'services.system_control_service': self.control,
            'services.l2tp_service': self.tunnels, 'services.wireguard_service': self.bridge})
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def test_nas_reload_uses_verified_service_controller(self):
        ns = functions('services/nas_service.py', {'reload_freeradius_clients'}, {'execute_write': Mock()})
        with patch('os.path.exists', return_value=True), patch('socket.socket') as raw_socket:
            self.assertEqual(ns['reload_freeradius_clients'](), (True, 'verified'))
        self.control.execute_service_action.assert_called_once_with('freeradius', 'restart', admin_username='admin')
        raw_socket.assert_not_called()

    def test_hosted_nas_reload_never_uses_native_socket(self):
        self.hosted.configured = lambda: True
        ns = functions('services/nas_service.py', {'reload_freeradius_clients'}, {'execute_write': Mock()})
        with patch('os.path.exists', return_value=False), patch('socket.socket') as raw_socket:
            self.assertTrue(ns['reload_freeradius_clients']()[0])
        self.control.execute_service_action.assert_called_once()
        raw_socket.assert_not_called()

    def test_nas_delete_does_not_match_tunnel_by_display_name(self):
        write = Mock()
        query = Mock(return_value=[])
        ns = functions('services/nas_service.py', {'delete_nas_device'}, dict(
            query_one=lambda *a: dict(ip_address='192.0.2.10', name='same display name'),
            query_all=query, execute_write=write, sync_nas_table_entries=lambda: (True, ''), log_audit=Mock()))
        self.assertTrue(ns['delete_nas_device'](7)[0])
        self.tunnels.delete_l2tp_tunnel.assert_not_called()
        query.assert_called_once_with('SELECT id FROM wisp_l2tp_tunnels WHERE tunnel_ip=?', ('192.0.2.10',))

    def test_failed_linked_tunnel_prevents_nas_credential_deletion(self):
        write = Mock()
        self.tunnels.delete_l2tp_tunnel.return_value = False
        ns = functions('services/nas_service.py', {'delete_nas_device'}, dict(
            query_one=lambda *a: dict(ip_address='192.0.2.10', name='target'),
            query_all=lambda *a: [{'id': 9}], execute_write=write))
        with self.assertRaises(ValueError):
            ns['delete_nas_device'](7)
        write.assert_not_called()

    def kill_namespace(self, outcomes):
        execute = Mock(side_effect=outcomes)
        ns = functions('services/l2tp_service.py', {'kill_l2tp_session'},
                       dict(re=re, time=time, L2TP_CONTAINER_NAME='max_radius_l2tp',
                            _docker_exec_run=execute, query_all=Mock(return_value=[])))
        return ns, execute

    def test_invalid_or_missing_peer_cannot_issue_command(self):
        ns, execute = self.kill_namespace([])
        self.assertFalse(ns['kill_l2tp_session']())
        self.assertFalse(ns['kill_l2tp_session'](tunnel_ip='192.0.2.1; kill 1'))
        execute.assert_not_called()

    def test_ambiguous_peer_does_not_signal_any_process(self):
        text = '2: ppp0 inet 192.0.2.1 peer 192.0.2.10/32\n3: ppp1 inet 192.0.2.1 peer 192.0.2.10/32\n'
        ns, execute = self.kill_namespace([dict(success=True, stdout=text)])
        self.assertFalse(ns['kill_l2tp_session'](tunnel_ip='192.0.2.10'))
        self.assertEqual(execute.call_count, 1)

    def test_only_exact_peer_interface_is_signalled(self):
        first = '2: ppp0 inet 192.0.2.1 peer 192.0.2.10/32\n3: ppp1 inet 192.0.2.1 peer 192.0.2.11/32\n'
        second = '3: ppp1 inet 192.0.2.1 peer 192.0.2.11/32\n'
        ns, execute = self.kill_namespace([dict(success=True, stdout=first), dict(success=True),
                                          dict(success=True, stdout=second)])
        self.assertTrue(ns['kill_l2tp_session'](tunnel_ip='192.0.2.10'))
        command = execute.call_args_list[1].args[1]
        self.assertEqual(command[-2:], ['ppp0', '192.0.2.10'])
        self.assertNotIn('pkill', command[2])
        self.assertNotIn('SIGKILL', command[2])
        self.assertNotIn('for iface', command[2])

    def test_unconfirmed_stop_prevents_tunnel_sql_deletion(self):
        write = Mock()
        ns = functions('services/l2tp_service.py', {'delete_l2tp_tunnel'}, dict(
            query_one=lambda *a: dict(tunnel_ip='192.0.2.10', username='target'),
            kill_l2tp_session=Mock(return_value=False), execute_write=write))
        with self.assertRaises(ValueError):
            ns['delete_l2tp_tunnel'](7)
        write.assert_not_called()

    def test_ppp_fields_escape_quotes_and_reject_control_characters(self):
        ns = functions('services/l2tp_service.py', {'_ppp_secret_field', '_ppp_peer_ip'}, {})
        self.assertEqual(ns['_ppp_secret_field']('a"b\\c'), '"a\\"b\\\\c"')
        with self.assertRaises(ValueError):
            ns['_ppp_secret_field']('a\nEOF\ncommand')
        with self.assertRaises(ValueError):
            ns['_ppp_peer_ip']('0.0.0.0')

    def test_credentials_are_data_arguments_not_shell_program(self):
        with tempfile.TemporaryDirectory(prefix='max-owned-ppp-') as directory:
            root = Path(directory).resolve()
            self.assertTrue(root.is_dir())
            execute = Mock(return_value=dict(success=True))
            ns = functions('services/l2tp_service.py', {'_ppp_secret_field', '_ppp_peer_ip',
                           'sync_all_tunnels_to_vpn'}, dict(os=os, __file__=str(root / 'services/l2tp_service.py'),
                           ensure_l2tp_host_route=Mock(), query_all=lambda *a: [dict(
                               username='literal$(command)', password='quote"\\secret', tunnel_ip='192.0.2.10')],
                           _docker_exec_run=execute, L2TP_CONTAINER_NAME='max_radius_l2tp', logger=Mock()))
            self.assertTrue(ns['sync_all_tunnels_to_vpn']())
            command = execute.call_args.args[1]
            self.assertNotIn('literal$(command)', command[2])
            decoded = base64.b64decode(command[-1]).decode()
            self.assertIn('literal$(command)', decoded)
            self.assertIn('mv -f "$chap" /etc/ppp/chap-secrets', command[2])
            self.assertNotIn('>> /etc/ppp/chap-secrets', command[2])


if __name__ == '__main__':
    unittest.main()

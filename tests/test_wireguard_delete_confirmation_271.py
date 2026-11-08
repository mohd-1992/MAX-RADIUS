"""Delete must retain configuration when host peer removal is unverified."""
import ast
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

SOURCE=Path(__file__).resolve().parents[1]/'services/wireguard_service.py'

class WireGuardDeleteConfirmationTests(unittest.TestCase):
    def setUp(self):
        tree=ast.parse(SOURCE.read_text(encoding='utf-8'))
        node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='delete_wireguard_tunnel')
        self.row=dict(id=71,name='isolated fixture',public_key='LOCAL-PUBLIC',tunnel_ip='192.0.2.71')
        self.env=dict(query_one=Mock(return_value=self.row),remove_peer_from_kernel=Mock(return_value=True),execute_write=Mock(),reload_freeradius_clients=Mock(),log_audit=Mock(),_wg_cache_lock=threading.Lock(),_wg_live_cache={'data':['old']})
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(SOURCE),'exec'),self.env)
        self.delete=self.env['delete_wireguard_tunnel']

    def test_unverified_removal_retains_all_database_records(self):
        self.env['remove_peer_from_kernel'].return_value=False
        with self.assertRaises(RuntimeError):self.delete(71)
        self.env['execute_write'].assert_not_called()
        self.env['reload_freeradius_clients'].assert_not_called()
        self.env['log_audit'].assert_not_called()
        self.assertEqual(self.env['_wg_live_cache']['data'],['old'])

    def test_host_exception_cannot_fall_through_to_deletion(self):
        self.env['remove_peer_from_kernel'].side_effect=OSError('local bridge unavailable')
        with self.assertRaises(OSError):self.delete(71)
        self.env['execute_write'].assert_not_called()
        self.env['reload_freeradius_clients'].assert_not_called()

    def test_verified_removal_deletes_only_target_and_keeps_success_contract(self):
        self.assertIs(self.delete(71),True)
        self.env['remove_peer_from_kernel'].assert_called_once_with('LOCAL-PUBLIC')
        self.assertEqual([c.args[1] for c in self.env['execute_write'].call_args_list],[(71,),('192.0.2.71',),('192.0.2.71',)])
        self.env['reload_freeradius_clients'].assert_called_once()
        self.assertIsNone(self.env['_wg_live_cache']['data'])

    def test_missing_record_does_not_call_host_or_mutate(self):
        self.env['query_one'].return_value=None
        self.assertIs(self.delete(71),False)
        self.env['remove_peer_from_kernel'].assert_not_called()
        self.env['execute_write'].assert_not_called()

if __name__=='__main__':unittest.main()

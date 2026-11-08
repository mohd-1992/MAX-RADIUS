"""Self-service password changes must not omit current-password verification."""
import ast
import unittest
from pathlib import Path
from unittest.mock import Mock


class ProfilePasswordGuardTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / 'services/manager_service.py'
        node = next(n for n in ast.parse(path.read_text(encoding='utf-8')).body
                    if isinstance(n, ast.FunctionDef) and n.name == 'update_manager_profile')
        self.ns = dict(get_manager_by_id=Mock(return_value=dict(id=5, username='local',
                       full_name='Local', password_hash='existing')),
                       verify_manager_password=Mock(return_value=False),
                       hash_manager_password=Mock(return_value='new-hash'),
                       execute_write=Mock(), log_audit=Mock())
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), self.ns)
        self.change = self.ns['update_manager_profile']

    def test_missing_current_password_never_writes(self):
        for value in (None, ''):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.change(5, 'Local', '', '', current_password=value, new_password='new-local')
        self.ns['verify_manager_password'].assert_not_called()
        self.ns['execute_write'].assert_not_called()

    def test_wrong_current_password_never_writes(self):
        with self.assertRaises(ValueError):
            self.change(5, 'Local', '', '', current_password='wrong', new_password='new-local')
        self.ns['execute_write'].assert_not_called()

    def test_valid_current_password_updates_hash(self):
        self.ns['verify_manager_password'].return_value = True
        self.assertTrue(self.change(5, 'Local', '', '', current_password='current', new_password='new-local'))
        self.ns['verify_manager_password'].assert_called_once_with('existing', 'current')
        self.ns['hash_manager_password'].assert_called_once_with('new-local')
        self.assertEqual(self.ns['execute_write'].call_count, 2)

    def test_profile_only_update_needs_no_password(self):
        self.assertTrue(self.change(5, 'Local edited', '', ''))
        self.ns['verify_manager_password'].assert_not_called()
        self.ns['hash_manager_password'].assert_not_called()
        self.assertEqual(self.ns['execute_write'].call_count, 2)


if __name__ == '__main__':
    unittest.main()

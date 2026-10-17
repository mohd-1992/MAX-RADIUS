"""Host manager contracts using temporary roots and fake systemd metadata only."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('host_guard_manager', HERE / 'manage.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'root'
        self.root.mkdir()
        self.journal = Path(self.temp.name) / 'journal'
        self.loaded = ''
        self.reloads = 0

    def reload(self):
        self.reloads += 1
        dropin = self.root / 'etc/systemd/system/docker.service.d/99-max-radius-clock-ready.conf'
        self.loaded = m.active_files.HELPER if dropin.exists() else ''

    def enable(self, check=lambda: None):
        return m.enable(self.journal, self.root, check, self.reload, lambda: self.loaded)

    def test_enable_and_disable_without_service_restart(self):
        self.assertEqual(self.enable()['status'], 'active')
        self.assertEqual(m.disable(self.journal, self.root, self.reload, lambda: self.loaded)['status'], 'active-rolled-back')
        self.assertEqual(self.reloads, 2)

    def test_repeated_enable_is_idempotent(self):
        self.enable()
        self.enable()
        self.assertEqual(self.reloads, 1)

    def test_failed_preflight_does_not_stage_files(self):
        def fail():
            raise RuntimeError('Unconfirmed time')
        with self.assertRaises(RuntimeError):
            self.enable(fail)
        self.assertFalse(self.journal.exists())

    def test_effective_unit_failure_rolls_back(self):
        with self.assertRaises(ValueError):
            m.enable(self.journal, self.root, lambda: None, lambda: None, lambda: '')
        state = json.loads((self.journal / 'journal.json').read_text())
        self.assertEqual(state['status'], 'active-rolled-back')

    def test_modified_active_helper_is_preserved(self):
        self.enable()
        helper = self.root / 'usr/local/libexec/max-radius-clock-ready.py'
        helper.write_text('new administrator content')
        with self.assertRaises(ValueError):
            m.disable(self.journal, self.root, self.reload, lambda: self.loaded)
        self.assertEqual(helper.read_text(), 'new administrator content')

    def test_staged_but_loaded_guard_uses_active_rollback(self):
        m.managed_files.stage(self.root, self.journal)
        self.reload()
        state = m.disable(self.journal, self.root, self.reload, lambda: self.loaded)
        self.assertEqual(state['status'], 'active-rolled-back')

    def test_old_completed_journal_not_replayed(self):
        self.enable()
        m.disable(self.journal, self.root, self.reload, lambda: self.loaded)
        with self.assertRaises(RuntimeError):
            self.enable()


if __name__ == '__main__':
    unittest.main()

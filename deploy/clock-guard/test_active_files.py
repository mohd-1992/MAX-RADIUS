"""Temporary-root tests: no systemd, host time, network or database changes."""
from pathlib import Path
import tempfile
import unittest

import active_files as active
import managed_files as files


class ActiveFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'root'
        self.journal = Path(self.temp.name)/'journal'
        files.stage(self.root, self.journal)
        self.helper = self.root/'usr/local/libexec/max-radius-clock-ready.py'
        self.dropin = self.root/'etc/systemd/system/docker.service.d/99-max-radius-clock-ready.conf'
        self.loaded = ''

    def reload(self):
        self.assertTrue(self.helper.exists(), 'Helper must survive every reload')
        self.loaded = active.HELPER if self.dropin.exists() else ''

    def activate(self):
        return active.activate(self.root, self.journal, self.reload, lambda:self.loaded)

    def rollback(self, reload=None, read=None):
        return active.rollback_loaded(self.root, self.journal, reload or self.reload, read or (lambda:self.loaded))

    def test_01_activate_and_two_phase_rollback_preserves_both_archives(self):
        self.assertEqual(self.activate()['status'],'active')
        self.assertEqual(self.rollback()['status'],'active-rolled-back')
        self.assertFalse(self.helper.exists());self.assertFalse(self.dropin.exists())
        self.assertEqual(len(list((self.journal/'active-rollback-retained').rglob('*.py'))),1)
        self.assertEqual(len(list((self.journal/'active-rollback-retained').rglob('*.conf'))),1)
        self.assertEqual(self.rollback()['status'],'active-rolled-back')

    def test_02_reload_failure_retains_helper_and_resume_succeeds(self):
        self.activate()
        def failure():raise RuntimeError('Injected daemon-reload failure')
        with self.assertRaises(RuntimeError):self.rollback(failure)
        self.assertTrue(self.helper.exists());self.assertFalse(self.dropin.exists())
        self.assertEqual(self.rollback()['status'],'active-rolled-back')

    def test_03_still_loaded_reference_refuses_helper_removal(self):
        self.activate()
        with self.assertRaises(ValueError):self.rollback(read=lambda:active.HELPER)
        self.assertTrue(self.helper.exists())
        self.assertEqual(self.rollback()['status'],'active-rolled-back')

    def test_04_changed_helper_is_preserved(self):
        self.activate();self.helper.write_bytes(b'newer helper')
        with self.assertRaises(ValueError):self.rollback()
        self.assertEqual(self.helper.read_bytes(),b'newer helper')
        self.assertTrue(self.dropin.exists())

    def test_05_activation_not_replayed(self):
        self.activate()
        with self.assertRaises(ValueError):self.activate()

    def test_06_unconfirmed_activation_can_be_rolled_back(self):
        with self.assertRaises(ValueError):active.activate(self.root,self.journal,self.reload,lambda:'')
        self.assertEqual(self.rollback()['status'],'active-rolled-back')

    def test_07_staged_only_round_is_not_active_rollback(self):
        with self.assertRaises(ValueError):self.rollback()
        self.assertTrue(self.helper.exists());self.assertTrue(self.dropin.exists())


if __name__=='__main__':unittest.main(verbosity=2)

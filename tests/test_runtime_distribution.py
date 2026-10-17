"""Installer archive contracts; no installer, application or database execution."""
import io
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASE = ['docker-compose.production.yml', 'database/schema_mysql.sql',
        'docker/l2tp/options.xl2tpd', 'storage/keys/master_public_key.pem',
        'runtime-release.env']
HOST = ['host/clock-guard/' + n for n in ['clock_ready.py', 'managed_files.py',
        'active_files.py', 'manage.py', '99-max-radius-clock-ready.conf']]


class BundleContracts(unittest.TestCase):
    def check_archive(self, names, version='v1', link=None):
        script = re.search(r"<<'BUNDLE_CHECK'\n(.*?)\nBUNDLE_CHECK", (ROOT/'install.sh').read_text(), re.S)[1]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'runtime.tar.gz'
            with tarfile.open(path, 'w:gz') as archive:
                for name in names:
                    entry = tarfile.TarInfo(name)
                    if name == link:
                        entry.type = tarfile.SYMTYPE
                        entry.linkname = '/etc/passwd'
                        archive.addfile(entry)
                    else:
                        entry.size = 1
                        archive.addfile(entry, io.BytesIO(b'x'))
            return subprocess.run([sys.executable, '-', str(path), version],
                                  input=script, capture_output=True, text=True).returncode

    def test_legacy_bundle_supported(self):
        self.assertEqual(self.check_archive(BASE, 'none'), 0)

    def test_r13_bundle_supported(self):
        self.assertEqual(self.check_archive(BASE+HOST), 0)

    def test_missing_host_tool_refused(self):
        self.assertNotEqual(self.check_archive(BASE+HOST[:-1]), 0)

    def test_undeclared_host_tools_refused(self):
        self.assertNotEqual(self.check_archive(BASE+HOST, 'none'), 0)

    def test_symlink_refused(self):
        self.assertNotEqual(self.check_archive(BASE+HOST, link=BASE[0]), 0)

    def test_duplicate_refused(self):
        self.assertNotEqual(self.check_archive(BASE+HOST+[HOST[0]]), 0)

    def test_extra_path_refused(self):
        self.assertNotEqual(self.check_archive(BASE+HOST+['../../etc/passwd']), 0)

    def test_absolute_path_refused(self):
        self.assertNotEqual(self.check_archive(['/'+BASE[0]]+BASE[1:]+HOST), 0)

    def test_unknown_version_refused(self):
        self.assertNotEqual(self.check_archive(BASE+HOST, 'v2'), 0)


if __name__ == '__main__':
    unittest.main()

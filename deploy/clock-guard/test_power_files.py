"""Fault injection in newly created temporary directories; no app or customer access."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import concurrent.futures
from unittest.mock import patch

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('managed_power_fixture',HERE/'managed_files.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


class PowerFiles(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.root=self.base/'root';self.root.mkdir();self.journal=self.base/'journal'
    def tearDown(self):self.temp.cleanup()
    def stage(self):return m.stage(self.root,self.journal,HERE)
    def paths(self):return [self.root/p for p in m.FILES.values()]
    def archives(self):return [self.journal/'rollback-retained'/p for p in m.FILES.values()]

    def test_exclusive_publication_preserves_competing_file(self):
        p=self.root/'original';p.write_bytes(b'other-config')
        with self.assertRaises(FileExistsError):m.atomic_create(p,b'candidate')
        self.assertEqual(p.read_bytes(),b'other-config')

    def test_failed_flush_never_publishes_partial_file(self):
        p=self.root/'destination'
        with patch.object(m.os,'fsync',side_effect=OSError('fixture disk full')):
            with self.assertRaises(OSError):m.atomic_create(p,b'new-bytes')
        self.assertFalse(p.exists())

    def test_existing_complete_archive_allows_interrupted_rollback(self):
        self.stage();source=self.paths()[0];archive=self.archives()[0]
        archive.parent.mkdir(parents=True);archive.write_bytes(source.read_bytes())
        result=m.rollback(self.root,self.journal)
        self.assertEqual(result['status'],'rolled-back')
        self.assertTrue(all(p.exists() for p in self.archives()))

    def test_moved_first_file_allows_rollback_resume(self):
        self.stage();source=self.paths()[0];archive=self.archives()[0]
        archive.parent.mkdir(parents=True);source.replace(archive)
        self.assertEqual(m.rollback(self.root,self.journal)['status'],'rolled-back')
        self.assertFalse(any(p.exists() for p in self.paths()))

    def test_changed_archive_preserves_all_current_files(self):
        self.stage();archive=self.archives()[0];archive.parent.mkdir(parents=True);archive.write_bytes(b'foreign')
        with self.assertRaises(ValueError):m.rollback(self.root,self.journal)
        self.assertTrue(all(p.exists() for p in self.paths()))
        self.assertEqual(archive.read_bytes(),b'foreign')

    def child_exit(self,mode):
        code='''import importlib.util,os,pathlib,sys
source,root,journal,mode=sys.argv[1:]
spec=importlib.util.spec_from_file_location('power_fixture',source+'/managed_files.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
original=m.atomic_create
def cut(path,data,mode_bits=0o644):
 original(path,data,mode_bits)
 if (mode=='stage' and path.name=='max-radius-clock-ready.py') or (mode=='rollback' and 'rollback-retained' in str(path)):os._exit(92)
m.atomic_create=cut
if mode=='stage':m.stage(root,journal,source)
else:m.rollback(root,journal)
'''
        p=subprocess.run([sys.executable,'-c',code,str(HERE),str(self.root),str(self.journal),mode],capture_output=True,timeout=10)
        self.assertEqual(p.returncode,92)

    def test_process_death_after_first_publish_recovers_from_journal(self):
        self.child_exit('stage');self.assertTrue(self.paths()[0].exists());self.assertFalse(self.paths()[1].exists())
        self.assertEqual(m.rollback(self.root,self.journal)['status'],'rolled-back')
        self.assertFalse(any(p.exists() for p in self.paths()))

    def test_process_death_after_archive_before_removal_recovers(self):
        self.stage();self.child_exit('rollback')
        self.assertTrue(self.paths()[1].exists());self.assertTrue(self.archives()[1].exists())
        self.assertEqual(m.rollback(self.root,self.journal)['status'],'rolled-back')
        self.assertFalse(any(p.exists() for p in self.paths()))

    def test_parent_directory_persistence_is_requested(self):
        with patch.object(m,'sync_directory',wraps=m.sync_directory) as sync:
            self.stage();m.rollback(self.root,self.journal)
        self.assertGreater(sync.call_count,10)

    def test_rollback_disables_dropin_before_removing_helper(self):
        self.stage();removed=[];original=Path.unlink
        def traced(path,*a,**kw):
            if path in self.paths():removed.append(path)
            return original(path,*a,**kw)
        with patch.object(Path,'unlink',traced):m.rollback(self.root,self.journal)
        self.assertEqual(removed,list(reversed(self.paths())))

    def test_competing_stage_cannot_replace_round_journal(self):
        def attempt(_):
            try:self.stage();return 'staged'
            except (ValueError,FileExistsError):return 'refused'
        with concurrent.futures.ThreadPoolExecutor(2) as workers:results=list(workers.map(attempt,range(2)))
        self.assertEqual(sorted(results),['refused','staged'])
        self.assertEqual(json.loads((self.journal/'journal.json').read_text())['status'],'staged-not-activated')
        self.assertTrue(all(p.exists() for p in self.paths()))


if __name__=='__main__':unittest.main(verbosity=2)

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('managed_clock_fixture',HERE/'managed_files.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


class ManagedFileTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.base=Path(self.temp.name);self.root=self.base/'host';self.root.mkdir()
        self.journal=self.base/'round'
    def tearDown(self):self.temp.cleanup()
    def stage(self):return m.stage(self.root,self.journal,HERE)
    def destinations(self):return [self.root/x for x in m.FILES.values()]
    def test_stage_exact_files_without_activation(self):
        state=self.stage();self.assertEqual(state['status'],'staged-not-activated')
        for name,rel in m.FILES.items():self.assertEqual((self.root/rel).read_bytes(),(HERE/name).read_bytes())
    def test_existing_configuration_is_preserved(self):
        path=self.destinations()[1];path.parent.mkdir(parents=True);path.write_text('original')
        with self.assertRaises(ValueError):self.stage()
        self.assertEqual(path.read_text(),'original');self.assertFalse(self.destinations()[0].exists())
    def test_rollback_archives_both_files(self):
        self.stage();state=m.rollback(self.root,self.journal)
        self.assertEqual(state['status'],'rolled-back')
        for name,rel in m.FILES.items():
            self.assertFalse((self.root/rel).exists())
            self.assertEqual((self.journal/'rollback-retained'/rel).read_bytes(),(HERE/name).read_bytes())
    def test_repeated_rollback_is_noop(self):
        self.stage();m.rollback(self.root,self.journal)
        self.assertEqual(m.rollback(self.root,self.journal)['status'],'rolled-back')
    def test_replay_is_refused(self):
        self.stage()
        with self.assertRaises(ValueError):self.stage()
    def test_modified_file_refuses_entire_rollback(self):
        self.stage();paths=self.destinations();paths[1].write_text('newer human config')
        with self.assertRaises(ValueError):m.rollback(self.root,self.journal)
        self.assertTrue(paths[0].exists());self.assertEqual(paths[1].read_text(),'newer human config')
    def test_wrong_root_refuses_rollback(self):
        self.stage()
        with self.assertRaises(ValueError):m.rollback(self.base/'other',self.journal)
    def test_symlink_destination_refused(self):
        target=self.base/'external';target.mkdir()
        try:(self.root/'usr').symlink_to(target,target_is_directory=True)
        except OSError:self.skipTest('Symlinks unavailable on this host; Linux fixture verifies this')
        with self.assertRaises(ValueError):self.stage()
        self.assertEqual(list(target.iterdir()),[])
    def test_failure_between_files_rolls_back_first(self):
        original=m.atomic_create
        def fail(path,data,mode=0o644):
            if path.name=='99-max-radius-clock-ready.conf':raise OSError('simulated disk fault')
            return original(path,data,mode)
        with patch.object(m,'atomic_create',side_effect=fail),self.assertRaises(OSError):self.stage()
        self.assertFalse(any(v.exists() for v in self.destinations()))
        self.assertEqual(json.loads((self.journal/'journal.json').read_text())['status'],'rolled-back')
    def test_unrelated_runtime_and_data_remain_unchanged(self):
        files={self.root/'var/lib/mysql/fixture':b'accounting fixture',self.root/'app/license':b'license fixture',self.root/'docker.service':b'original service'}
        for p,data in files.items():p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
        self.stage();m.rollback(self.root,self.journal)
        self.assertEqual({p:p.read_bytes() for p in files},files)
    def test_edited_manifest_cannot_escape_owned_set(self):
        self.stage();p=self.journal/'journal.json';s=json.loads(p.read_text());s['files'][0]['path']='unrelated'
        p.write_text(json.dumps(s))
        with self.assertRaises(ValueError):m.rollback(self.root,self.journal)


if __name__=='__main__':unittest.main(verbosity=2)

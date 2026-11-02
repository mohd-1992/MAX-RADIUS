"""Archive safety only; no application, DB connection or restore of user data."""
import ast
from contextlib import contextmanager
import io
import os
from pathlib import Path
import stat
import sys
import tarfile
import tempfile
import types
import unittest
from unittest.mock import Mock,patch
import zipfile
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from core.backup_archive_policy import validate_archive,upload_targets


class BackupArchiveTests(unittest.TestCase):
    def test_valid_tar_and_zip_preserve_original_database_and_upload_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            for extension in ('.tar.gz','.zip'):
                path=Path(folder)/('snapshot'+extension)
                if extension=='.zip':
                    with zipfile.ZipFile(path,'w') as archive:
                        archive.writestr('database_dump.sql','SELECT 1;');archive.writestr('uploads/logo.txt','synthetic')
                else:
                    with tarfile.open(path,'w:gz') as archive:
                        for name,value in [('database_dump.sql',b'SELECT 1;'),('uploads/logo.txt',b'synthetic')]:
                            item=tarfile.TarInfo(name);item.size=len(value);archive.addfile(item,io.BytesIO(value))
                self.assertEqual(validate_archive(path),{'sql'})
                self.assertEqual(upload_targets('nested/logo.txt',[folder]),[Path(folder).resolve()/'nested/logo.txt'])

    def test_traversal_links_empty_and_noncanonical_payload_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            for unsafe in ('uploads/../../outside','/absolute','uploads/drive:C/file','uploads\\..\\outside','./database_dump.sql'):
                with self.subTest(member=unsafe):
                    path=Path(folder)/'bad.zip'
                    with zipfile.ZipFile(path,'w') as archive:
                        archive.writestr('database_dump.sql','SELECT 1;');archive.writestr(unsafe,'synthetic')
                    with self.assertRaises(ValueError):validate_archive(path)
            path=Path(folder)/'empty.zip'
            with zipfile.ZipFile(path,'w') as archive:archive.writestr('metadata.json','{}')
            with self.assertRaises(ValueError):validate_archive(path)
            path=Path(folder)/'link.zip'
            with zipfile.ZipFile(path,'w') as archive:
                archive.writestr('database_dump.sql','SELECT 1;')
                item=zipfile.ZipInfo('uploads/link');item.create_system=3;item.external_attr=(stat.S_IFLNK|0o777)<<16
                archive.writestr(item,'/outside')
            with self.assertRaises(ValueError):validate_archive(path)

    def test_actual_restore_rejects_archive_before_database_or_upload_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder);path=folder/'bad.zip'
            with zipfile.ZipFile(path,'w') as archive:
                archive.writestr('database_dump.sql','SELECT 1;');archive.writestr('uploads/../../escaped','synthetic')
            tree=ast.parse((ROOT/'services/backup_service.py').read_text(encoding='utf-8-sig'))
            function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_restore_backup')
            database=Mock(side_effect=AssertionError('Database must not be touched'))
            namespace={'ensure_backup_dir':lambda:str(folder),'os':os,'get_connection':database}
            stub=types.ModuleType('database.db');stub.set_import_maintenance_active=lambda value:None
            exec(compile(ast.Module(body=[function],type_ignores=[]),'isolated-original-restore','exec'),namespace)
            with patch.dict(sys.modules,{'database.db':stub}):
                ok,_=namespace['_restore_backup']('bad.zip','synthetic',str(folder))
            self.assertFalse(ok);database.assert_not_called()
            self.assertFalse((folder.parent/'escaped').exists())


if __name__=='__main__':unittest.main()

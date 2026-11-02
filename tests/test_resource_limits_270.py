"""Isolated limits, inter-worker exclusion, catalog safety and bounded SQL."""
import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from services.heavy_operation_service import operation_slot
from services.sql_import_stream import bounded_sql_lines, split_insert_rows, schema_headers
from services.report_catalog_cache import catalog_query, clear, invalidate


class ResourceLimits(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        env=patch.dict(os.environ,STORAGE_DIR=self.directory.name,MAX_REPORT_CATALOG_CACHE='1')
        env.start();self.addCleanup(env.stop);clear()

    def test_another_process_cannot_start_until_slot_released(self):
        script="from services.heavy_operation_service import operation_slot,OperationBusy\ntry:\n with operation_slot(): print('acquired')\nexcept OperationBusy: print('busy')"
        def child():
            return subprocess.check_output([sys.executable,'-B','-c',script],text=True).strip()
        with operation_slot():
            with operation_slot():self.assertEqual(child(),'busy')
        self.assertEqual(child(),'acquired')

    def test_only_owner_can_cancel_active_export_and_markers_are_cleaned(self):
        from flask import Flask,session
        from services.heavy_operation_service import export_job,cancel_export,export_cancelled
        app=Flask('fixture');app.secret_key='fixture-only'
        token='a'*32
        with app.test_request_context(headers={'X-Max-Export':token}):
            session['admin_id']=7
            with export_job():
                self.assertFalse(cancel_export(token,8))
                self.assertFalse(export_cancelled())
                self.assertTrue(cancel_export(token,7))
                self.assertTrue(export_cancelled())
            self.assertFalse(export_cancelled())
            self.assertFalse(cancel_export(token,7))

    def test_exception_releases_slot(self):
        with self.assertRaises(ValueError):
            with operation_slot():raise ValueError('fixture')
        with operation_slot():pass

    def test_busy_async_import_does_not_claim_started_or_replace_active_progress(self):
        import ast,threading
        from pathlib import Path
        from unittest.mock import Mock
        source=(Path(__file__).resolve().parents[1]/'services/database_migration_service.py').read_text(encoding='utf-8-sig')
        node=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='start_async_migration')
        node.decorator_list=[]
        execute=Mock();progress=Mock()
        scope=dict(threading=threading,execute_database_migration=execute,update_migration_progress=progress)
        exec(compile(ast.Module(body=[node],type_ignores=[]),'async-import','exec'),scope)
        with operation_slot():result=scope['start_async_migration']('synthetic-only')
        self.assertFalse(result['success'])
        execute.assert_not_called();progress.assert_not_called()

    def test_catalog_is_copied_scoped_and_invalidated(self):
        calls=[]
        def query(*args):calls.append(args);return [dict(name='fixture')]
        read=catalog_query(query,None)
        read('catalog')[0]['name']='changed'
        self.assertEqual(read('catalog')[0]['name'],'fixture')
        self.assertEqual(len(calls),1)
        invalidate();read('catalog');self.assertEqual(len(calls),2)
        restricted=catalog_query(query,7)
        restricted('catalog');restricted('catalog');self.assertEqual(len(calls),4)
        with patch.dict(os.environ,DB_NAME='other-fixture'):read('catalog')
        self.assertEqual(len(calls),5)

    def test_extended_insert_has_bounded_complete_batches(self):
        source="INSERT INTO `fixture` VALUES "+','.join("(%s,'a),(b; c')"%i for i in range(10000))+";\n"
        result=list(bounded_sql_lines(io.StringIO(source)))
        self.assertEqual(len(result),40)
        self.assertLess(max(map(len,result)),10000)
        parsed=[]
        for line in result:
            raw=line[line.index(' VALUES (')+9:-3]
            parsed.extend(split_insert_rows(raw))
        self.assertEqual(len(parsed),10000)
        self.assertTrue(parsed[0].startswith('0,'))
        self.assertTrue(parsed[-1].startswith('9999,'))

    def test_restore_schema_scan_skips_huge_payload_and_false_headers(self):
        text="CREATE TABLE `a` (id INT);\nINSERT INTO `a` VALUES ('"+'x'*200000+" CREATE TABLE wisp_voucher_names');\nCREATE TABLE `b` (id INT);\n"
        self.assertEqual(list(schema_headers(io.StringIO(text))),["CREATE TABLE `a` (id INT);\n","CREATE TABLE `b` (id INT);\n"])

    def test_wide_sql_records_do_not_accumulate_a_huge_batch(self):
        source="INSERT INTO `fixture` VALUES "+','.join("(%s,'%s')"%(i,'x'*600000) for i in range(8))+";\n"
        result=list(bounded_sql_lines(io.StringIO(source)))
        self.assertLess(max(len(line.encode('utf-8')) for line in result),5*1024**2)
        self.assertEqual(sum(len(split_insert_rows(line[line.index(' VALUES (')+9:-3])) for line in result),8)

    def test_malformed_and_oversize_sql_rejected(self):
        for text,kwargs in [("INSERT INTO `x` VALUES (1,'unfinished",{}),
                            ("INSERT INTO `x` VALUES (1,'long');",dict(max_row=5)),
                            ('x'*65536,{}),('line\n'*10,dict(max_bytes=8))]:
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):
                list(bounded_sql_lines(io.StringIO(text),**kwargs))

    def test_backup_rejects_unsafe_and_oversize_members_before_restore(self):
        import gzip,tarfile
        from pathlib import Path
        from core.backup_archive_policy import validate_archive
        for case in ('link','duplicate','oversize'):
            path=Path(self.directory.name)/(case+'.tar.gz')
            with self.subTest(case=case):
                if case=='oversize':
                    member=tarfile.TarInfo('database_dump.sql');member.size=8*1024**3+1
                    with gzip.open(path,'wb') as archive:archive.write(member.tobuf())
                else:
                    with tarfile.open(path,'w:gz') as archive:
                        member=tarfile.TarInfo('database_dump.sql')
                        if case=='link':member.type=tarfile.SYMTYPE;member.linkname='/outside'
                        archive.addfile(member)
                        if case=='duplicate':archive.addfile(member)
                with self.assertRaises(ValueError):validate_archive(path)


if __name__=='__main__':unittest.main()

"""Exercise real SQL/gzip streams independently of their cache filenames."""
import ast,gzip,io,os,tempfile,unittest
from pathlib import Path
class MigrationStreamSniffTests(unittest.TestCase):
 def setUp(self):
  path=Path('services/database_migration_service.py');node=next(n for n in ast.parse(path.read_text(encoding='utf-8')).body if isinstance(n,ast.FunctionDef) and n.name=='_open_backup_stream');ns=dict(gzip=gzip,io=io,os=os);exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),ns);self.reader=ns[node.name];self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.sql="INSERT INTO `local_fixture` VALUES ('محلي');\n"
 def test_plain_sql_in_gz_cache_name(self):
  path=Path(self.tmp.name)/'uploaded_backup.gz';path.write_text(self.sql,encoding='utf-8')
  with self.reader(path) as stream:self.assertEqual(stream.read(),self.sql)
  self.assertTrue(stream.closed)
 def test_gzip_with_sql_name(self):
  path=Path(self.tmp.name)/'fixture.sql';path.write_bytes(gzip.compress(self.sql.encode()))
  with self.reader(path) as stream:self.assertEqual(stream.read(),self.sql)
  self.assertTrue(stream.closed)
 def test_normal_gzip_cache_stream(self):
  path=Path(self.tmp.name)/'uploaded_backup.gz';path.write_bytes(gzip.compress(self.sql.encode()))
  with self.reader(path) as stream:self.assertEqual(stream.read(),self.sql)
 def test_corrupt_gzip_is_not_treated_as_plain_sql(self):
  path=Path(self.tmp.name)/'fixture.sql';path.write_bytes(b'\x1f\x8bBAD')
  with self.assertRaises((OSError,EOFError)):
   with self.reader(path) as stream:stream.read()
if __name__=='__main__':unittest.main()

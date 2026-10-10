import ast, unittest
from pathlib import Path
class Tests(unittest.TestCase):
 def helper(self):
  p=Path(__file__).resolve().parents[2]/'services/database_migration_service.py'
  tree=ast.parse(p.read_text(encoding='utf8'));f=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='imported_extra_quota_mb');ns={};exec(compile(ast.Module(body=[f],type_ignores=[]),'test','exec'),ns);return ns[f.name]
 def test_additional_credit(self):self.assertEqual(self.helper()({'total_traffic':100*2**30},80*1024),20*1024)
 def test_reduced_ceiling(self):self.assertEqual(self.helper()({'total_traffic':60*2**30},80*1024),-20*1024)
 def test_fractional_credit_not_lost(self):self.assertEqual(self.helper()({'total_traffic':80*2**30+1},80*1024),1)
 def test_missing_ceiling(self):self.assertEqual(self.helper()({},81920),0)
 def test_unlimited_unchanged(self):self.assertEqual(self.helper()({'total_traffic':100*2**30},0),0)
if __name__=='__main__':unittest.main(verbosity=2)

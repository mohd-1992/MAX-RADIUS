import ast,threading,unittest
from pathlib import Path
class MigrationCancelAdmissionTests(unittest.TestCase):
 def function(self):
  source=Path('services/database_migration_service.py');node=next(x for x in ast.parse(source.read_text(encoding='utf-8')).body if isinstance(x,ast.FunctionDef) and x.name=='execute_database_migration');node.decorator_list=[];end=next(i for i,x in enumerate(node.body) if isinstance(x,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='stats' for t in x.targets));node.body=node.body[:end];self.event=threading.Event();ns={'_CANCEL_EVENT':self.event};exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),ns);return ns[node.name]
 def test_admitted_cancel_not_cleared_at_executor_entry(self):
  fn=self.function();self.event.set()
  with self.assertRaises(ValueError):fn('unused',dict(collision_strategy='invalid'),_cancel_prepared=True)
  self.assertTrue(self.event.is_set())
 def test_standalone_new_execution_resets_only_prior_cancel(self):
  fn=self.function();self.event.set()
  with self.assertRaises(ValueError):fn('unused',dict(collision_strategy='invalid'))
  self.assertFalse(self.event.is_set())
if __name__=='__main__':unittest.main()

"""Pure import contract regressions: no app, database writes or router calls."""
import ast
import datetime
import unittest
from pathlib import Path

source=Path(__file__).resolve().parents[1]/'services/import_state_service.py'
tree=ast.parse(source.read_text(encoding='utf-8'))
names={'present_value','parse_import_datetime','validity_duration','imported_state','imported_profile_plan','imported_radius_checks'}
selected=ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names],type_ignores=[])
ns={'datetime':datetime};exec(compile(selected,str(source),'exec'),ns)

class ImportConsistencyTests(unittest.TestCase):
    def test_finite_source_quota_overrides_import_snapshot_without_mutating_open_package(self):
        original={'volume_quota_mb':0,'validity_value':30}
        plan=ns['imported_profile_plan'](original,{'quota_mb':100})
        self.assertEqual(plan['volume_quota_mb'],100);self.assertEqual(original['volume_quota_mb'],0)
    def test_explicit_unlimited_source_remains_zero(self):
        self.assertEqual(ns['imported_profile_plan']({'volume_quota_mb':100},{'quota_mb':0})['volume_quota_mb'],0)
    def test_unknown_source_preserves_matching_plan(self):
        self.assertEqual(ns['imported_profile_plan']({'volume_quota_mb':100},{'quota_mb':None})['volume_quota_mb'],100)
    def test_disabled_with_and_without_history_always_rejects(self):
        now=datetime.datetime(2026,10,13)
        for history in [False,True]:
            row={'disabled':True,'download_used_bytes':10*1048576}
            state=ns['imported_state'](row,{'volume_quota_mb':100,'validity_value':30},now,history)
            self.assertEqual(state['status'],'disabled')
            self.assertEqual(ns['imported_radius_checks']('fixture','fixture',state),[('fixture','Auth-Type',':=','Reject')])
    def test_finite_source_exhaustion_and_remaining_are_distinct(self):
        now=datetime.datetime(2026,10,13)
        plan=ns['imported_profile_plan']({'volume_quota_mb':0,'validity_value':30},{'quota_mb':100})
        for usage,expected in [(10,'active'),(100,'expired')]:
            state=ns['imported_state']({'download_used_bytes':usage*1048576},plan,now,True)
            self.assertEqual(state['status'],expected)
    def test_existing_zero_package_update_is_not_unconditionally_enabled(self):
        text=source.with_name('mikrotik_userman_importer.py').read_text(encoding='utf-8')
        self.assertNotIn('(volume_quota_mb = 0 OR volume_quota_mb IS NULL)',text)
        self.assertIn('imported_profile_plan(plans_by_id[pkg_id], prof_info)',text)

if __name__=='__main__':unittest.main()

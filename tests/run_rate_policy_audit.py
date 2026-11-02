"""Rate-policy fix regressions on disposable SQLite fixtures.

Assertions verify the repaired policy writers and fail-closed historical readers.
No application startup, network, NAS, production DB or financial operations.
Run: python -X utf8 -B tests/run_rate_policy_audit.py
"""
import ast
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_portal_card_operations import PortalTests, functions
from test_batch_exports import BatchExportTests


def real_builder():
    ns = {}
    functions('core/rate_limit.py', {'is_zero_or_empty_rate', 'parse_rate_to_bps',
              'build_mikrotik_rate_limit'}, ns)
    return ns['build_mikrotik_rate_limit']


class RenewalAudit(unittest.TestCase):
    def setUp(self):
        self.f = PortalTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.build = real_builder()
        sys.modules['core.rate_limit'].build_mikrotik_rate_limit = self.build
        sales = types.ModuleType('services.sales_source_service')
        sales.record_sale_source = Mock()
        sales_patch = patch.dict(sys.modules, {'services.sales_source_service': sales})
        sales_patch.start()
        self.addCleanup(sales_patch.stop)

    def test_admin_renewal_writes_complete_limited_and_explicit_unlimited(self):
        f = self.f
        # Fixture's extra package column does not exist in the production schema.
        f.db.execute('ALTER TABLE wisp_packages DROP COLUMN rate_limit_str')
        f.account()
        ns = f.ns
        ns.update(get_target_entity=lambda *args: (f.one('SELECT * FROM wisp_vouchers'), 'voucher'),
                  log_audit=Mock(), format_mb_or_gb=str)
        sales = types.ModuleType('services.sales_source_service')
        sales.record_sale_source = Mock()
        functions('services/quick_action_service.py', {'action_renew_package'}, ns)
        with patch.dict(sys.modules, {'services.sales_source_service': sales}):
            for down, up in [('4M', '2M'), ('0', '0')]:
                f.db.execute('UPDATE wisp_packages SET rate_download=?,rate_upload=?', (down, up))
                f.db.execute("UPDATE wisp_vouchers SET snap_rate_limit_str='2M/4M'")
                f.db.commit()
                ok, message = ns['action_renew_package']('voucher', 1)
                self.assertTrue(ok, message)
                self.assertEqual(f.one('SELECT snap_rate_limit_str rate FROM wisp_vouchers')['rate'], '2M/4M' if down == '4M' else '0/0')
            self.assertEqual(f.one('SELECT COUNT(*) n FROM wisp_voucher_sales')['n'], 2)

    def test_portal_limited_and_unlimited_have_nonempty_rate(self):
        f = self.f
        f.account()
        for down, up, expected in [('4M', '2M', '2M/4M'), ('0', '0', '0/0')]:
            f.db.execute('UPDATE wisp_packages SET rate_download=?,rate_upload=? WHERE id=1', (down, up))
            f.db.commit()
            self.assertTrue(f.ns['renew_or_change_package']('card', 1)[0])
            self.assertEqual(f.one('SELECT snap_rate_limit_str rate FROM wisp_vouchers')['rate'], expected)

    def test_admin_package_change_preserves_full_rate_and_explicit_unlimited(self):
        f = self.f
        f.db.execute('ALTER TABLE wisp_packages DROP COLUMN rate_limit_str')
        f.account()
        f.ns.update(get_target_entity=lambda *args: (f.one('SELECT * FROM wisp_vouchers'), 'voucher'),
                    log_audit=Mock(), format_mb_or_gb=str, build_mikrotik_rate_limit=self.build)
        functions('services/quick_action_service.py', {'action_change_package'}, f.ns)
        for key, value in dict(burst_download='8M', burst_upload='4M', burst_time=12,
                               priority=3, min_download='1M', min_upload='512k').items():
            f.db.execute(f'ALTER TABLE wisp_packages ADD COLUMN {key}')
            f.db.execute(f'UPDATE wisp_packages SET {key}=? WHERE id=2', (value,))
        f.db.commit()
        self.assertTrue(f.ns['action_change_package']('voucher', 1, 2)[0])
        self.assertEqual(f.one('SELECT snap_rate_limit_str rate FROM wisp_vouchers')['rate'],
                         '2M/4M 4M/8M 2M/4M 12/12 3 512k/1M')
        f.db.execute("UPDATE wisp_packages SET rate_download='0',rate_upload='0' WHERE id=2")
        f.db.commit()
        self.assertTrue(f.ns['action_change_package']('voucher', 1, 2)[0])
        self.assertEqual(f.one('SELECT snap_rate_limit_str rate FROM wisp_vouchers')['rate'], '0/0')

    def test_subscriber_admin_renewal_does_not_write_voucher_rate_snapshot(self):
        f = self.f
        f.account('subscriber')
        f.db.execute("UPDATE wisp_subscribers SET snap_rate_limit_str='sentinel'")
        f.db.execute('CREATE TABLE IF NOT EXISTS wisp_invoices(invoice_number,subscriber_id,subscriber_name,amount,status,package_name,notes,paid_at)')
        f.db.commit()
        f.ns.update(get_target_entity=lambda *args: (f.one('SELECT * FROM wisp_subscribers'), 'subscriber'),
                    generate_invoice_number=lambda *args: 'fixture', log_audit=Mock(), format_mb_or_gb=str)
        functions('services/quick_action_service.py', {'action_renew_package'}, f.ns)
        self.assertTrue(f.ns['action_renew_package']('subscriber', 1)[0])
        self.assertEqual(f.one('SELECT snap_rate_limit_str rate FROM wisp_subscribers')['rate'], 'sentinel')

    def test_portal_renewal_preserves_burst_priority_and_minimum(self):
        f = self.f
        f.account()
        params = dict(burst_download='8M', burst_upload='4M', burst_threshold_down='3M',
                      burst_threshold_up='1M', burst_time=12, priority=3,
                      min_download='1M', min_upload='512k')
        for key, value in params.items():
            f.db.execute(f'ALTER TABLE wisp_packages ADD COLUMN {key}')
            f.db.execute(f'UPDATE wisp_packages SET {key}=?', (value,))
        f.db.commit()
        self.assertTrue(f.ns['renew_or_change_package']('card', 1)[0])
        rate = f.one('SELECT snap_rate_limit_str rate FROM wisp_vouchers')['rate']
        self.assertNotEqual(rate, '1M/2M')
        self.assertEqual(rate, self.build('2M', '1M', '8M', '4M', '3M', '1M', 12, 3, '1M', '512k'))

    def test_import_helper_preserves_full_policy_and_normalizes_unlimited(self):
        ns = {}
        functions('services/import_state_service.py', {'imported_policy_rate'}, ns)
        self.assertEqual(ns['imported_policy_rate']({}), '0/0')
        self.assertEqual(ns['imported_policy_rate'](dict(rate_download='4M', rate_upload='2M',
            burst_download='8M', burst_upload='4M', burst_time=12, priority=3,
            min_download='1M', min_upload='512k')), '2M/4M 4M/8M 2M/4M 12/12 3 512k/1M')

    def test_directional_zero_is_refused_instead_of_opening_both_directions(self):
        from services.rate_policy_service import package_rate_limit
        for down, up in [('4M', '0'), ('0', '2M')]:
            with self.assertRaises(ValueError):
                package_rate_limit(dict(rate_download=down, rate_upload=up))

    def test_invalid_admin_rate_leaves_contract_and_sales_unchanged(self):
        f = self.f
        f.account()
        f.db.execute("UPDATE wisp_packages SET rate_download='4M',rate_upload='0'")
        f.db.commit()
        before = f.one('SELECT * FROM wisp_vouchers')
        f.ns.update(get_target_entity=lambda *args: (f.one('SELECT * FROM wisp_vouchers'), 'voucher'))
        functions('services/quick_action_service.py', {'action_renew_package'}, f.ns)
        self.assertFalse(f.ns['action_renew_package']('voucher', 1)[0])
        self.assertEqual(before, f.one('SELECT * FROM wisp_vouchers'))
        self.assertEqual(f.one('SELECT COUNT(*) n FROM wisp_voucher_sales')['n'], 0)
        f.ns['record_session_baselines'].assert_not_called()

    def test_invalid_portal_rate_leaves_money_contract_and_sales_unchanged(self):
        f = self.f
        f.account()
        f.db.execute("UPDATE wisp_packages SET rate_download='4M',rate_upload='0'")
        f.db.commit()
        before = f.one('SELECT * FROM wisp_vouchers')
        self.assertFalse(f.ns['renew_or_change_package']('card', 1)[0])
        self.assertEqual(before, f.one('SELECT * FROM wisp_vouchers'))
        self.assertEqual(f.one('SELECT COUNT(*) n FROM wisp_voucher_sales')['n'], 0)
        f.ns['record_session_baselines'].assert_not_called()

    def test_historical_policy_preserves_burst_and_only_proven_unlimited_fills_gap(self):
        from services.rate_policy_service import snapshot_rate_limit
        full = '2M/4M 4M/8M 2M/4M 12/12 3 512k/1M'
        self.assertEqual(snapshot_rate_limit(full, '4M', '2M'), full)
        self.assertEqual(snapshot_rate_limit('', '0', '0'), '0/0')
        for value in ('', None, '  '):
            with self.assertRaises(ValueError):
                snapshot_rate_limit(value, '4M', '2M')


class ExportRestoreAudit(unittest.TestCase):
    def test_unknown_limited_empty_policy_refused_before_restore(self):
        f = BatchExportTests()
        f.setUp()
        try:
            row = f.exported_row(snap_rate_limit_str='', snap_rate_download='4M', snap_rate_upload='2M')
            from services.batch_restore_service import validate_workbook
            from services.batch_export_service import validate_export_row
            with self.assertRaises(ValueError):
                validate_export_row(row)
            f.db.execute("UPDATE wisp_packages SET name='fixture-package' WHERE id=1")
            f.db.commit()
            ok, errors, cleaned = validate_workbook([row], 'vouchers')
            self.assertFalse(ok)
            self.assertEqual(cleaned, [])
            self.assertEqual(f.db.execute("SELECT COUNT(*) FROM wisp_vouchers WHERE username='0000123'").fetchone()[0], 0)
        finally:
            f.tearDown()


if __name__ == '__main__':
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(c)
                               for c in (RenewalAudit, ExportRestoreAudit))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())

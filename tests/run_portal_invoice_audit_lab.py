"""Explicit disposable MariaDB acceptance; never run against customer schemas."""
import os
import sys


def main():
    if sys.argv[1:] != ['--run'] or not os.environ.get('DB_NAME', '').startswith('subscriber_edit_fixture_'):
        raise SystemExit('Explicit synthetic subscriber-edit database and --run required')
    import datetime
    import json
    import threading
    import unittest
    import uuid
    from pathlib import Path
    from unittest.mock import patch
    real_start = threading.Thread.start
    threading.Thread.start = lambda self: None
    from database.db import init_database, query_one, query_all, execute_write
    init_database()
    from services import user_portal_service as portal
    from services.subscriber_edit_service import edit_subscriber, revision
    from services.subscriber_service import get_user_audit_logs
    from core.radius_sync import sync_subscriber_to_radius
    import core.license_feature_policy as license_policy
    # Positive feature entitlement only inside this synthetic process.
    license_policy.verified_state = lambda: dict(valid=True, features={})
    packages = {}
    for label, quota in [('limited', 10240), ('open', 0)]:
        name = 'invoice_fixture_' + uuid.uuid4().hex[:10]
        execute_write("""INSERT INTO wisp_packages
            (name,service_type,volume_quota_mb,validity_value,validity_unit,validity_days,price,cost,is_active)
            VALUES(%s,'hotspot',%s,30,'days',30,25,7,1)""", (name, quota))
        packages[label] = query_one('SELECT * FROM wisp_packages WHERE name=%s', (name,))

    def account(balance=100):
        name = 'invoice_account_' + uuid.uuid4().hex[:10]
        execute_write("""INSERT INTO wisp_subscribers
            (username,password,full_name,service_type,package_id,status,balance,snap_volume_quota_mb,
             extra_quota_mb,created_at,last_renewed_at,expires_at)
            VALUES(%s,'fixture-only','Synthetic invoice','hotspot',%s,'active',%s,10240,0,
                   NOW()-INTERVAL 2 DAY,NOW()-INTERVAL 1 DAY,NOW()+INTERVAL 10 DAY)""",
            (name, packages['limited']['id'], balance))
        row = query_one('SELECT * FROM wisp_subscribers WHERE username=%s', (name,))
        assert sync_subscriber_to_radius(row['id'])
        return row

    def current(row):
        return query_one('SELECT * FROM wisp_subscribers WHERE id=%s', (row['id'],))

    def invoices(row):
        return query_all('SELECT * FROM wisp_invoices WHERE subscriber_id=%s ORDER BY id', (row['id'],))

    def credentials(row):
        return query_all('SELECT * FROM radcheck WHERE username=%s ORDER BY id', (row['username'],))

    def body(row, **changes):
        return dict(operation_id=uuid.uuid4().hex, expected_revision=revision(row), username=row['username'], **changes)

    class Cases(unittest.TestCase):
        def tearDown(self):
            self.assertEqual(query_one('SELECT COUNT(*) n FROM wisp_renewal_guards')['n'], 0)

        def test_01_purchase_invoice_matches_debit_and_cycle(self):
            row = account()
            ok, message = portal.renew_or_change_package(row['username'], packages['open']['id'])
            self.assertTrue(ok, message)
            new, rows = current(row), invoices(row)
            self.assertEqual(float(new['balance']), 75)
            self.assertEqual(len(rows), 1)
            self.assertEqual(float(rows[0]['amount']), 25)
            self.assertEqual(rows[0]['status'], 'paid')
            self.assertEqual(rows[0]['package_name'], packages['open']['name'])
            self.assertEqual(rows[0]['paid_at'], new['last_renewed_at'])
            self.assertTrue(rows[0]['invoice_number'].startswith('INV-'))

        def test_02_same_package_renewal_also_has_invoice(self):
            row = account()
            self.assertTrue(portal.renew_or_change_package(row['username'], row['package_id'])[0])
            self.assertEqual(float(current(row)['balance']), 75)
            self.assertEqual(len(invoices(row)), 1)

        def test_03_insufficient_balance_has_no_invoice_or_contract_change(self):
            row = account(24)
            self.assertFalse(portal.renew_or_change_package(row['username'], packages['open']['id'])[0])
            self.assertEqual(current(row), row)
            self.assertEqual(invoices(row), [])

        def test_04_invoice_failure_rolls_back_debit_contract_and_radius(self):
            row = account(); before = credentials(row)
            execute_write("""CREATE TRIGGER invoice_fixture_abort BEFORE INSERT ON wisp_invoices
                FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='synthetic invoice failure'""")
            try:
                with self.assertRaisesRegex(Exception, 'synthetic invoice failure'):
                    portal.renew_or_change_package(row['username'], packages['open']['id'])
            finally:
                execute_write('DROP TRIGGER invoice_fixture_abort')
            self.assertEqual(current(row), row)
            self.assertEqual(credentials(row), before)
            self.assertEqual(invoices(row), [])

        def test_05_radius_failure_rolls_back_debit_without_invoice(self):
            row = account(); before = credentials(row); original = portal._sync_recharge_radius
            def fail(*args, **kwargs):
                original(*args, **kwargs)
                raise RuntimeError('synthetic radius failure')
            with patch.object(portal, '_sync_recharge_radius', side_effect=fail):
                with self.assertRaisesRegex(RuntimeError, 'synthetic radius failure'):
                    portal.renew_or_change_package(row['username'], packages['open']['id'])
            self.assertEqual(current(row), row)
            self.assertEqual(credentials(row), before)
            self.assertEqual(invoices(row), [])

        def test_06_concurrent_purchase_one_debit_and_invoice(self):
            from concurrent.futures import ThreadPoolExecutor
            row = account(30)
            with patch.object(threading.Thread, 'start', real_start):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(lambda _: portal.renew_or_change_package(row['username'], packages['open']['id']), [0, 1]))
            self.assertEqual(sum(bool(x[0]) for x in results), 1)
            self.assertEqual(float(current(row)['balance']), 5)
            self.assertEqual(len(invoices(row)), 1)

        def test_07_voucher_purchase_keeps_sale_without_subscriber_invoice(self):
            name = 'invoice_card_' + uuid.uuid4().hex[:10]
            batch = 'invoice_batch_' + uuid.uuid4().hex[:10]
            execute_write("INSERT INTO wisp_voucher_batches(batch_number,name,package_id,card_count) VALUES(%s,%s,%s,1)",
                          (batch, batch, packages['limited']['id']))
            batch_id = query_one('SELECT id FROM wisp_voucher_batches WHERE batch_number=%s', (batch,))['id']
            execute_write("""INSERT INTO wisp_vouchers
                (batch_id,serial_number,username,password,pin_code,package_id,status,balance,snap_volume_quota_mb,last_renewed_at,expires_at)
                VALUES(%s,%s,%s,'fixture-only','fixture-only',%s,'active',100,10240,NOW()-INTERVAL 1 DAY,NOW()+INTERVAL 10 DAY)""",
                (batch_id, name, name, packages['limited']['id']))
            total = query_one('SELECT COUNT(*) n FROM wisp_invoices')['n']
            self.assertTrue(portal.renew_or_change_package(name, packages['open']['id'])[0])
            self.assertEqual(float(query_one('SELECT balance FROM wisp_vouchers WHERE username=%s', (name,))['balance']), 75)
            self.assertEqual(query_one('SELECT COUNT(*) n FROM wisp_voucher_sales WHERE username=%s', (name,))['n'], 1)
            self.assertEqual(query_one('SELECT COUNT(*) n FROM wisp_invoices')['n'], total)

        def test_08_single_audit_contains_all_changes_without_password(self):
            row = account(); expiry = (row['expires_at'] + datetime.timedelta(days=2)).isoformat()
            request = body(row, full_name='Changed fixture', password='new-fixture-password', expires_at=expiry)
            result = edit_subscriber(row['id'], request, actor='fixture-admin', can_change_expiry=True)
            self.assertTrue(result['success'])
            logs = get_user_audit_logs('subscriber', row['id'], row['username'])
            self.assertEqual(len(logs), 1)
            self.assertEqual(logs[0]['action'], 'EDIT_CONTRACT')
            changes = json.loads(logs[0]['change_details'])
            self.assertEqual(set(changes), {'full_name', 'password', 'expires_at'})
            self.assertEqual(changes['password'], {'changed': True})
            self.assertNotIn('fixture-password', logs[0]['change_details'])
            self.assertNotIn('fixture-only', logs[0]['change_details'])
            self.assertEqual(len(logs[0]['detail_lines']), 3)
            self.assertEqual(edit_subscriber(row['id'], request, actor='fixture-admin', can_change_expiry=True), result)
            self.assertEqual(len(get_user_audit_logs('subscriber', row['id'], row['username'])), 1)

        def test_09_noop_has_no_new_audit_or_radius_write(self):
            row = account(); before = credentials(row)
            with patch('services.subscriber_service.sync_subscriber_to_radius') as sync:
                self.assertTrue(edit_subscriber(row['id'], body(row), actor='fixture-admin')['success'])
                sync.assert_not_called()
            self.assertEqual(current(row), row)
            self.assertEqual(credentials(row), before)
            self.assertEqual(get_user_audit_logs('subscriber', row['id'], row['username']), [])

        def test_10_audit_failure_rolls_back_profile_radius_and_receipt(self):
            row = account(); before = credentials(row); request = body(row, password='rollback-password')
            execute_write("""CREATE TRIGGER audit_fixture_abort BEFORE INSERT ON user_audit_logs
                FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='synthetic audit failure'""")
            try:
                with self.assertRaisesRegex(Exception, 'synthetic audit failure'):
                    edit_subscriber(row['id'], request, actor='fixture-admin')
            finally:
                execute_write('DROP TRIGGER audit_fixture_abort')
            self.assertEqual(current(row), row)
            self.assertEqual(credentials(row), before)
            self.assertIsNone(query_one('SELECT operation_id FROM wisp_subscriber_edit_receipts WHERE operation_id=%s', (request['operation_id'],)))
            self.assertEqual(get_user_audit_logs('subscriber', row['id'], row['username']), [])

        def test_11_old_json_readable_without_rewriting_or_hiding_rows(self):
            row = account()
            raw = json.dumps({'expires_at': {'before': '2026-10-24', 'after': '2026-11-24'},
                              'username': {'before': row['username'], 'after': row['username']}})
            for action, details in [('UPDATE_PROFILE', 'تحديث عام لبيانات المشترك'), ('EDIT_CONTRACT', raw)]:
                execute_write("INSERT INTO user_audit_logs(user_type,user_id,username,admin_name,action,change_details) VALUES('subscriber',%s,%s,'fixture-admin',%s,%s)",
                              (row['id'], row['username'], action, details))
            logs = get_user_audit_logs('subscriber', row['id'], row['username'])
            self.assertEqual(len(logs), 2)
            self.assertEqual(logs[0]['change_details'], raw)
            self.assertEqual(len(logs[0]['detail_lines']), 1)
            self.assertIn('تاريخ الانتهاء', logs[0]['detail_lines'][0])
            self.assertNotIn('before', logs[0]['detail_lines'][0])

    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Cases))
    proof = dict(passed=result.wasSuccessful(), tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                 actual_mariadb=True, real_docker_binding=True, core_process_is_fixture=True, nas_tested=False,
                 source_under_test=True, existing_image_not_rebuilt=True)
    Path('/evidence/invoice-audit-proof.private.json').write_text(json.dumps(proof, indent=2))
    print(json.dumps(proof))
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == '__main__':
    main()

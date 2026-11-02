"""Explicit synthetic MariaDB acceptance; no customer or local5090 DB allowed."""
import os
import sys


def main():
    if sys.argv[1:] != ['--run'] or os.environ.get('DB_NAME') != 'mac266_fixture':
        raise SystemExit('Only the isolated mac266_fixture database and --run are accepted')
    import threading
    threading.Thread.start_original = threading.Thread.start
    threading.Thread.start = lambda self: None
    import contextlib
    import hashlib
    import json
    import unittest
    import uuid
    from pathlib import Path
    from unittest.mock import patch
    from database.db import query_one, query_all, execute_write, get_connection
    from database.account_mac_schema import install_account_mac_schema
    from services import account_mac_service as mac
    from core.radius_sync import sync_package_to_radius, sync_subscriber_to_radius, sync_voucher_to_radius
    from services.online_policy_service import local

    def create(kind='subscriber', service='hotspot', legacy=None):
        name = 'mac_fixture_' + uuid.uuid4().hex[:12]
        execute_write("INSERT INTO wisp_packages(name,service_type,simultaneous_sessions,volume_quota_mb,validity_value,validity_unit,is_active) VALUES(?,?,5,0,30,'days',1)", (name, service))
        pkg = query_one('SELECT id FROM wisp_packages WHERE name=?', (name,))['id']
        sync_package_to_radius(pkg)
        if kind == 'subscriber':
            execute_write("INSERT INTO wisp_subscribers(username,password,full_name,service_type,package_id,status,balance,mac_binding) VALUES(?,'fixture-only',?,?,?,'active',123.45,?)", (name, name, service, pkg, legacy or ''))
        else:
            execute_write("INSERT INTO wisp_voucher_batches(batch_number,name,package_id,card_count) VALUES(?,?,?,1)", (name, name, pkg))
            batch = query_one('SELECT id FROM wisp_voucher_batches WHERE batch_number=?', (name,))['id']
            execute_write("INSERT INTO wisp_vouchers(batch_id,serial_number,username,password,pin_code,package_id,status,balance,bound_mac) VALUES(?,?,?,'fixture-only','fixture-only',?,'unused',123.45,?)", (batch, name, name, pkg, legacy))
        row = query_one(f'SELECT * FROM {mac.TABLES[kind]} WHERE username=?', (name,))
        if legacy:
            execute_write("INSERT INTO radcheck(username,attribute,op,value) VALUES(?,'Calling-Station-Id','==',?)", (name, legacy))
        (sync_subscriber_to_radius if kind == 'subscriber' else sync_voucher_to_radius)(row['id'])
        return row

    @contextlib.contextmanager
    def settlement(username):
        local.snapshot = []
        local.operation_succeeded = False
        yield

    def body(kind, row, enabled=True, addresses=None, op=None):
        return {'enabled': enabled, 'addresses': addresses if addresses is not None else [{'mac': f'02:00:00:00:00:{i:02X}', 'note': f'جهاز {i}'} for i in range(7)], 'revision': mac.get_mac_policy(kind, row['id'])['revision'], 'operation_id': op or uuid.uuid4().hex}

    execute_write("UPDATE wisp_license_runtime_state s JOIN wisp_license_info l ON l.id=s.license_row_id SET s.is_valid=1,s.package_hash=SHA2(l.raw_package_json,256),s.valid_until=UTC_TIMESTAMP()+INTERVAL 1 HOUR WHERE s.id=1 AND l.license_id='mac266-synthetic'")

    patches = [patch('services.renewal_settlement_service.settled_account', side_effect=settlement), patch('services.renewal_settlement_service.wait_for_final_accounting'), patch('services.nas_session_settlement.freeze_and_disconnect')]
    for p in patches:
        p.start()

    class Cases(unittest.TestCase):
        def test_01_default_unrestricted_does_not_infer_from_observed_mac(self):
            for kind in mac.TABLES:
                row = create(kind)
                field = 'mac_binding' if kind == 'subscriber' else 'bound_mac'
                execute_write(f'UPDATE {mac.TABLES[kind]} SET {field}=? WHERE id=?', ('02:00:00:00:00:55', row['id']))
                (sync_subscriber_to_radius if kind == 'subscriber' else sync_voucher_to_radius)(row['id'])
                policy = mac.get_mac_policy(kind, row['id'])
                self.assertFalse(policy['enabled'])
                self.assertEqual(policy['addresses'], [])

        def test_02_preserves_legacy_single_restrictions(self):
            for kind in mac.TABLES:
                row = create(kind, legacy='02:00:00:00:00:AA')
                policy = mac.get_mac_policy(kind, row['id'])
                self.assertTrue(policy['enabled'])
                self.assertEqual(policy['addresses'][0]['mac'], '02:00:00:00:00:AA')

        def test_03_seven_addresses_do_not_change_contract_money_or_sessions(self):
            for kind in mac.TABLES:
                row = create(kind)
                payload = body(kind, row)
                self.assertTrue(mac.save_mac_policy(kind, row['id'], payload)['success'])
                after = query_one(f'SELECT * FROM {mac.TABLES[kind]} WHERE id=?', (row['id'],))
                self.assertEqual(after, row)
                self.assertEqual(len(mac.get_mac_policy(kind, row['id'])['addresses']), 7)
                self.assertEqual(query_one("SELECT value FROM radgroupcheck WHERE groupname=? AND attribute='Simultaneous-Use'", (row['username'],))['value'], '5')

        def test_04_receipt_replay_is_one_audit_and_no_second_disconnect(self):
            row = create();payload = body('subscriber', row)
            before = mac.save_mac_policy('subscriber', row['id'], payload)
            freeze = patches[2].target.freeze_and_disconnect
            count = freeze.call_count
            self.assertEqual(mac.save_mac_policy('subscriber', row['id'], payload), before)
            self.assertEqual(freeze.call_count, count)
            self.assertEqual(query_one("SELECT COUNT(*) n FROM user_audit_logs WHERE user_type='subscriber' AND user_id=? AND action='EDIT_MAC_POLICY'", (row['id'],))['n'], 1)

        def test_05_stale_revision_cannot_overwrite_policy(self):
            row = create();a = body('subscriber', row);b = body('subscriber', row)
            mac.save_mac_policy('subscriber', row['id'], a)
            with self.assertRaises(ValueError):
                mac.save_mac_policy('subscriber', row['id'], b)

        def test_06_notes_do_not_disconnect_and_are_literal_text(self):
            row = create();mac.save_mac_policy('subscriber', row['id'], body('subscriber', row))
            payload = body('subscriber', row);payload['addresses'][0]['note'] = '<script>alert(1)</script>'
            freeze = patches[2].target.freeze_and_disconnect;count = freeze.call_count
            mac.save_mac_policy('subscriber', row['id'], payload)
            self.assertEqual(freeze.call_count, count)
            self.assertEqual(mac.get_mac_policy('subscriber', row['id'])['addresses'][0]['note'], '<script>alert(1)</script>')

        def test_07_disable_keeps_list_and_resync_cannot_reenable_legacy_mac(self):
            for kind in mac.TABLES:
                row = create(kind, legacy='02:00:00:00:00:AA')
                mac.save_mac_policy(kind, row['id'], body(kind, row, enabled=False))
                (sync_subscriber_to_radius if kind == 'subscriber' else sync_voucher_to_radius)(row['id'])
                self.assertFalse(mac.get_mac_policy(kind, row['id'])['enabled'])
                self.assertFalse(query_all("SELECT id FROM radcheck WHERE username=? AND attribute='Calling-Station-Id'", (row['username'],)))

        def test_08_repeat_migration_keeps_policy_and_business_rows(self):
            row = create();mac.save_mac_policy('subscriber', row['id'], body('subscriber', row))
            policy = mac.get_mac_policy('subscriber', row['id'])
            with get_connection() as c:
                install_account_mac_schema(c);install_account_mac_schema(c)
            self.assertEqual(mac.get_mac_policy('subscriber', row['id']), policy)
            self.assertEqual(query_one('SELECT * FROM wisp_subscribers WHERE id=?', (row['id'],)), row)

        def test_09_other_owner_cannot_read_or_mutate(self):
            row = create('voucher');payload = body('voucher', row)
            with patch('core.ownership.owner_id', return_value=987654):
                with self.assertRaises(PermissionError):mac.get_mac_policy('voucher', row['id'])
                with self.assertRaises(PermissionError):mac.save_mac_policy('voucher', row['id'], payload)

        def test_10_reused_operation_with_different_payload_rejected(self):
            row = create();payload = body('subscriber', row)
            mac.save_mac_policy('subscriber', row['id'], payload)
            payload['addresses'][0]['note'] = 'different'
            with self.assertRaises(ValueError):mac.save_mac_policy('subscriber', row['id'], payload)

        def test_11_migration_does_not_bind_an_account_from_traffic_metadata(self):
            row = create('voucher')
            execute_write("DELETE FROM wisp_account_mac_policies WHERE account_type='voucher' AND account_id=?", (row['id'],))
            execute_write('UPDATE wisp_vouchers SET bound_mac=? WHERE id=?', ('02:00:00:00:00:55', row['id']))
            with get_connection() as c:install_account_mac_schema(c)
            self.assertFalse(mac.get_mac_policy('voucher', row['id'])['enabled'])

        def test_12_migration_keeps_a_real_legacy_radius_check(self):
            row = create()
            execute_write("DELETE FROM wisp_account_mac_policies WHERE account_type='subscriber' AND account_id=?", (row['id'],))
            execute_write("INSERT INTO radcheck(username,attribute,op,value) VALUES(?,'Calling-Station-Id','==','02:00:00:00:00:AB')", (row['username'],))
            with get_connection() as c:install_account_mac_schema(c)
            policy = mac.get_mac_policy('subscriber', row['id'])
            self.assertTrue(policy['enabled'])
            self.assertEqual(policy['addresses'][0]['mac'], '02:00:00:00:00:AB')

    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Cases))
    for p in reversed(patches):p.stop()
    # Synthetic positive SQL lease is NOT a verified publisher license. It is
    # only for isolated native RADIUS reservation tests, with no license worker.
    execute_write("INSERT INTO wisp_license_info(license_id,client_name,signature_b64,status,raw_package_json) VALUES('mac266-synthetic','Isolated test','fixture-only','active',?) ON DUPLICATE KEY UPDATE status=VALUES(status),raw_package_json=VALUES(raw_package_json)", ('{"payload":{"limits":{"max_active_sessions":100}}}',))
    lic = query_one("SELECT * FROM wisp_license_info WHERE license_id='mac266-synthetic'")
    execute_write("UPDATE wisp_license_runtime_state SET license_row_id=?,package_hash=?,is_valid=1,license_mode='active_sessions',max_active_sessions=100,valid_until=UTC_TIMESTAMP()+INTERVAL 1 HOUR WHERE id=1", (lic['id'], hashlib.sha256(lic['raw_package_json'].encode()).hexdigest()))
    matrix=[]
    for kind, service in [('subscriber','hotspot'),('subscriber','pppoe'),('voucher','hotspot')]:
        # Resource creation after the synthetic lease is permitted by its SQL limits.
        row=create(kind, service)
        for p in patches:p.start()
        mac.save_mac_policy(kind,row['id'],body(kind,row))
        for p in reversed(patches):p.stop()
        matrix.append({'kind':kind,'service':service,'id':row['id'],'username':row['username'],'package_id':row['package_id']})
    proof={'passed':result.wasSuccessful(),'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'mariadb_actual':True,'nas_settlement_mocked':True,'native_cases':matrix}
    Path('/evidence/source-db-proof.private.json').write_text(json.dumps(proof,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in proof.items() if k!='native_cases'}))
    if not result.wasSuccessful():raise SystemExit(1)


if __name__ == '__main__':
    main()

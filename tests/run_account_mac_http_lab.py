"""Explicit HTTP/CSRF checks against a synthetic MariaDB, no production app startup."""
import os
import sys


def main():
    if sys.argv[1:] != ['--run'] or os.environ.get('DB_NAME') != 'mac266_fixture':
        raise SystemExit('Only mac266_fixture with --run is accepted')
    import threading
    threading.Thread.start = lambda self: None
    import json
    import unittest
    import uuid
    from pathlib import Path
    from unittest.mock import patch
    from flask import Flask
    from core.license_security import install_csrf, protects_admin_account_request
    from database.db import query_one
    from web.mac_policy_routes import account_mac_response
    app = Flask('isolated-mac-policy-http')
    app.secret_key = 'synthetic-mac-http-only'
    install_csrf(app, protects_admin_account_request)
    app.add_url_rule('/api/subscribers/<int:account_id>/mac-policy', 'api_subscriber_mac_policy', lambda account_id: account_mac_response('subscriber', account_id), methods=['GET', 'POST'])
    app.add_url_rule('/api/vouchers/<int:account_id>/mac-policy', 'api_voucher_mac_policy', lambda account_id: account_mac_response('voucher', account_id), methods=['GET', 'POST'])
    row = query_one('SELECT id FROM wisp_subscribers ORDER BY id LIMIT 1')

    class Cases(unittest.TestCase):
        def setUp(self):
            self.client = app.test_client()
            self.permissions = {'subscribers.edit', 'vouchers.actions', 'detail.vouchers.card_edit'}
            self.patches = [patch('web.mac_policy_routes.get_current_manager', return_value={'id': 1, 'username': 'fixture-admin'}), patch('web.mac_policy_routes.has_permission', side_effect=lambda p, m=None: p in self.permissions), patch('core.ownership.owner_id', return_value=None)]
            for p in self.patches:p.start()
            with self.client.session_transaction() as s:s['_license_csrf'] = 'fixture-csrf'
            self.url = f'/api/subscribers/{row["id"]}/mac-policy'

        def tearDown(self):
            for p in reversed(self.patches):p.stop()

        def test_get_has_revision_and_list(self):
            response = self.client.get(self.url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.json['policy']['revision']), 64)

        def test_mutation_requires_csrf(self):
            self.assertEqual(self.client.post(self.url, json={}).status_code, 403)

        def test_no_edit_permission_cannot_read_notes_or_write(self):
            self.permissions.clear()
            self.assertEqual(self.client.get(self.url).status_code, 403)
            self.assertEqual(self.client.post(self.url, json={}, headers={'X-CSRF-Token': 'fixture-csrf'}).status_code, 403)

        def test_anonymous_cannot_read(self):
            with patch('web.mac_policy_routes.get_current_manager', return_value=None):
                self.assertEqual(self.client.get(self.url).status_code, 401)

        def test_foreign_reseller_is_not_allowed(self):
            with patch('core.ownership.owner_id', return_value=987654), patch('core.ownership.ownership_manager', return_value={'id':987654,'role_id':3,'allowed_packages':None}):
                self.assertEqual(self.client.get(self.url).status_code, 404)

        def test_invalid_payload_is_not_success(self):
            self.assertEqual(self.client.post(self.url, json=['bad'], headers={'X-CSRF-Token': 'fixture-csrf'}).status_code, 400)

        def test_voucher_detail_does_not_bypass_revoked_parent(self):
            card = query_one('SELECT id FROM wisp_vouchers ORDER BY id LIMIT 1')
            self.permissions.discard('vouchers.actions')
            self.assertEqual(self.client.get(f'/api/vouchers/{card["id"]}/mac-policy').status_code, 403)

        def test_nochange_request_has_idempotent_receipt(self):
            policy = self.client.get(self.url).json['policy']
            body = dict(policy, operation_id=uuid.uuid4().hex)
            a = self.client.post(self.url, json=body, headers={'X-CSRF-Token': 'fixture-csrf'})
            b = self.client.post(self.url, json=body, headers={'X-CSRF-Token': 'fixture-csrf'})
            self.assertEqual(a.status_code, 200)
            self.assertTrue(a.json['success'])
            self.assertEqual(a.json, b.json)

    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Cases))
    proof = {'passed':result.wasSuccessful(), 'tests':result.testsRun, 'errors':len(result.errors), 'failures':len(result.failures), 'csrf_actual':True, 'manager_identity_mocked':True, 'database_actual':True}
    Path('/evidence/http-policy-proof.private.json').write_text(json.dumps(proof,indent=2))
    print(json.dumps(proof))
    if not result.wasSuccessful():raise SystemExit(1)


if __name__ == '__main__':main()

"""Voucher edit HTTP contracts on isolated Flask fixtures."""
import ast
import re
import sys
import types
import unittest
from unittest.mock import Mock,patch
from test_subscriber_admin_safety import fixture
from test_package_page import load_functions,ROOT
from jinja2 import Environment


class VoucherEditTests(unittest.TestCase):
    def setUp(self):
        self.app,self.db,self.ns=fixture();self.client=self.app.test_client()
        self.ns['logger']=Mock()
        load_functions('web/routes/vouchers.py',['edit_active_card_action','edit_active_card_package','guard_voucher_permissions'],self.ns)
        for suffix,endpoint in [('profile','edit_active_card_action'),('package','edit_active_card_package')]:
            self.app.add_url_rule('/vouchers/<int:card_id>/'+suffix,endpoint,self.ns[endpoint],methods=['POST'])
        self.app.before_request(lambda:self.ns['guard_voucher_permissions']() if '/vouchers/' in self.ns['request'].path else None)
        self.card=dict(id=1,username='fixture-card',password='fixture',status='active',package_id=1)
        self.ns['query_one']=lambda q,p=():self.card if 'wisp_vouchers' in q else {'id':2}
        self.ns['action_change_package']=Mock(return_value=(True,'ok'))
        self.service=types.ModuleType('services.voucher_edit_service')
        self.service.edit_voucher=Mock(return_value=dict(success=True,message='ok'))
        self.service.receipt=Mock(return_value=None);self.service.fingerprint=Mock(return_value='digest');self.service.revision=Mock(return_value='revision')
        self.policy=types.ModuleType('web.access_policy');self.policy.can_access_account_action=Mock(return_value=True)
        self.settle=types.ModuleType('services.renewal_settlement_service')
        class SettlementPending(Exception):pass
        self.settle.SettlementPending=SettlementPending
        self.p=patch.dict(sys.modules,{'services.voucher_edit_service':self.service,'web.access_policy':self.policy,'services.renewal_settlement_service':self.settle})
        self.p.start();self.login(['vouchers.actions','vouchers.card_edit','vouchers.extend'])
    def tearDown(self):self.p.stop();self.db.close()
    def login(self,permissions):
        with self.client.session_transaction() as s:
            s['manager']={'id':2,'role_id':2,'username':'fixture-admin'};s['permissions']=permissions;s['_license_csrf']='fixture-token'
    def post(self,kind,data=None,csrf=True):
        return self.client.post('/vouchers/1/'+kind,data=data or {},headers={'X-CSRF-Token':'fixture-token'} if csrf else {})
    def test_csrf_and_base_permission_before_any_service(self):
        for suffix in ('profile','package'):self.assertEqual(self.post(suffix,csrf=False).status_code,403)
        self.login([])
        for suffix in ('profile','package'):self.assertEqual(self.post(suffix).status_code,403)
        self.service.edit_voucher.assert_not_called();self.ns['action_change_package'].assert_not_called()
    def test_expiry_permission_delegated_and_settlement_error(self):
        self.assertEqual(self.post('profile').status_code,200)
        self.assertTrue(self.service.edit_voucher.call_args.kwargs['can_change_expiry'])
        self.login(['vouchers.actions','vouchers.card_edit'])
        self.assertEqual(self.post('profile').status_code,200)
        self.assertFalse(self.service.edit_voucher.call_args.kwargs['can_change_expiry'])
        self.service.edit_voucher.side_effect=self.settle.SettlementPending('pending')
        self.assertEqual(self.post('profile').status_code,409)
    def test_package_revision_receipt_and_explicit_rollover(self):
        body=dict(new_package_id='2',expected_revision='revision',operation_id='a'*32,enable_rollover='1')
        self.assertEqual(self.post('package',body).status_code,200)
        args=self.ns['action_change_package'].call_args
        self.assertTrue(args.kwargs['enable_rollover']);self.assertEqual(args.kwargs['expected_revision'],'revision')
        self.assertEqual(args.kwargs['operation_id'],'a'*32)
        self.service.receipt.return_value=dict(success=True,message='saved')
        self.ns['action_change_package'].reset_mock();self.assertEqual(self.post('package',body).status_code,200)
        self.ns['action_change_package'].assert_not_called()
    def test_stale_or_forbidden_package_does_not_run_operation(self):
        body=dict(new_package_id='2',expected_revision='old',operation_id='a'*32)
        self.assertEqual(self.post('package',body).status_code,400)
        self.policy.can_access_account_action.return_value=False
        self.assertEqual(self.post('package',body).status_code,403)
        self.ns['action_change_package'].assert_not_called()
    def test_card_template_has_separate_receipted_forms_and_second_precision(self):
        text=(ROOT/'web/templates/active_card_user_details.html').read_text(encoding='utf-8')
        Environment().parse(text)
        self.assertIn('data-voucher-edit="profile"',text);self.assertIn('data-voucher-edit="package"',text)
        self.assertEqual(text.count('name="expected_revision"'),2)
        self.assertIn('step="1"',text);self.assertIn('name="enable_rollover"',text)
        self.assertNotIn('name="package_id"',text);self.assertNotIn('name="status"',text)
    def test_owned_views_and_audit_follow_voucher_identity(self):
        source=(ROOT/'core/ownership.py').read_text(encoding='utf-8')
        relations=next(n for n in ast.parse(source).body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='_RELATIONS' for t in n.targets))
        values=ast.literal_eval(relations.value)
        for view in ('radacct_owned','radacct_archive_owned','wisp_imported_session_history_owned'):
            self.assertIn('reseller_id={owner}',values[view])
        self.assertIn('user_id IN',values['user_audit_logs']);self.assertIn("user_type='voucher'",values['user_audit_logs'])


if __name__=='__main__':unittest.main()

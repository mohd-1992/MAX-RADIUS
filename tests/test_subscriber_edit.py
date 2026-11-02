"""New subscriber edit handlers: isolated permission/CSRF/error contracts."""
import sys
import types
import unittest
from unittest.mock import Mock,patch
from flask import current_app,session
from test_subscriber_admin_safety import fixture
from test_package_page import load_functions

class SubscriberEditHttpTests(unittest.TestCase):
    def setUp(self):
        self.app,self.db,self.ns=fixture();self.client=self.app.test_client()
        self.ns['current_app']=current_app
        load_functions('web/routes/subscribers.py',['edit_subscriber_contract','edit_subscriber_package'],self.ns)
        for suffix,endpoint in [('edit-contract','edit_subscriber_contract'),('edit-package','edit_subscriber_package')]:
            self.app.add_url_rule('/subscribers/<int:sub_id>/'+suffix,endpoint,self.ns[endpoint],methods=['POST'])
        self.service=types.ModuleType('services.subscriber_edit_service')
        self.service.edit_subscriber=Mock(return_value={'success':True,'message':'ok'})
        self.service.receipt=Mock(return_value=None);self.service.fingerprint=Mock(return_value='digest')
        self.settlement=types.ModuleType('services.renewal_settlement_service')
        class SettlementPending(Exception):pass
        self.settlement.SettlementPending=SettlementPending
        self.policy=types.ModuleType('web.access_policy');self.policy.can_access_account_action=Mock(return_value=True)
        self.p=patch.dict(sys.modules,{'services.subscriber_edit_service':self.service,'services.renewal_settlement_service':self.settlement,'web.access_policy':self.policy});self.p.start()
        self.login(['subscribers.edit','subscribers.renew'])
    def tearDown(self):self.p.stop();self.db.close()
    def login(self,permissions):
        with self.client.session_transaction() as s:
            s['manager']={'id':2,'role_id':2,'username':'fixture-admin'};s['permissions']=permissions;s['_license_csrf']='fixture-token'
    def post(self,suffix,data=None,csrf=True):
        return self.client.post('/subscribers/1/'+suffix,data=data or {},headers={'X-CSRF-Token':'fixture-token'} if csrf else {})
    def test_csrf_rejects_both_before_service(self):
        for suffix in ['edit-contract','edit-package']:self.assertEqual(self.post(suffix,csrf=False).status_code,403)
        self.service.edit_subscriber.assert_not_called()
    def test_profile_edit_permission_and_expiry_delegation(self):
        self.login([]);self.assertEqual(self.post('edit-contract').status_code,403)
        self.login(['subscribers.edit']);self.assertEqual(self.post('edit-contract').status_code,200)
        self.assertFalse(self.service.edit_subscriber.call_args.kwargs['can_change_expiry'])
        self.login(['subscribers.edit','subscribers.renew']);self.policy.can_access_account_action.return_value=False
        self.assertEqual(self.post('edit-contract').status_code,200)
        self.assertFalse(self.service.edit_subscriber.call_args.kwargs['can_change_expiry'])
    def test_profile_pending_and_invalid_have_explicit_status(self):
        for error,status in [(ValueError('invalid'),400),(self.settlement.SettlementPending('pending'),409)]:
            self.service.edit_subscriber.side_effect=error
            self.assertEqual(self.post('edit-contract').status_code,status)
    def test_package_requires_detailed_grant_and_reuses_operation(self):
        data={'new_package_id':'2','operation_id':'a'*32,'enable_rollover':'on'}
        self.policy.can_access_account_action.return_value=False
        self.assertEqual(self.post('edit-package',data).status_code,403)
        self.policy.can_access_account_action.return_value=True
        action=Mock(return_value=(True,'ok'));self.ns['action_change_package']=action
        self.assertEqual(self.post('edit-package',data).status_code,200)
        action.assert_called_once_with('subscriber',1,2,enable_rollover=True,admin_username='fixture-admin',operation_id='a'*32)

if __name__=='__main__':unittest.main()

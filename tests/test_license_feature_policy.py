"""Offline entitlement boundary checks. Never import web.app or core.config."""
import ast
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch, Mock
from contextlib import nullcontext

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from flask import Flask
from core import license_feature_policy as policy


def source_function(path,name,namespace=None):
    tree=ast.parse((ROOT/path).read_text(encoding='utf-8-sig'))
    function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
    namespace=dict(namespace or {})
    namespace.update(_license_guarded=policy.guarded,_feature_enabled=policy.enabled,
                     _require_feature=policy.require)
    # Compile the actual function only; importing its source module starts other
    # runtime dependencies. Keep its service decorators in their original order.
    exec(compile(ast.Module(body=[function],type_ignores=[]),str(ROOT/path),'exec'),namespace)
    return namespace[name]


class FeaturePolicyTests(unittest.TestCase):
    def test_all_contract_fields_true_false_and_legacy_absence(self):
        self.assertEqual(len(policy.FEATURE_KEYS),23)
        for key in policy.FEATURE_KEYS:
            with self.subTest(feature=key):
                self.assertTrue(policy.enabled(key,{'valid':True,'features':{}}))
                self.assertTrue(policy.enabled(key,{'valid':True,'features':{key:True}}))
                self.assertFalse(policy.enabled(key,{'valid':True,'features':{key:False}}))
                self.assertFalse(policy.enabled(key,{'valid':False,'features':{key:True}}))
        with self.assertRaises(ValueError):policy.enabled('unimplemented_custom_flag',{'valid':True})

    def test_dependency_cannot_grant_child_when_parent_is_off(self):
        for child,parents in policy.DEPENDENCIES.items():
            self.assertFalse(policy.enabled(child,{'valid':True,'features':{child:True,parents[0]:False}}))

    def test_malformed_or_unverified_status_never_grants(self):
        for state in [None,{},False,{'valid':1},{'valid':True,'features':['user_portal']}]:
            with patch.object(policy,'verified_state',return_value=state):
                self.assertFalse(policy.enabled('user_portal'))

    def test_request_cache_does_not_cross_requests_or_worker_calls(self):
        app=Flask(__name__)
        with patch.object(policy,'_verified_state',side_effect=[
            {'valid':True,'features':{}},{'valid':True,'features':{'gis_map':False}},
            {'valid':True,'features':{}},{'valid':True,'features':{'gis_map':False}}]) as verify:
            with app.test_request_context('/'):
                self.assertTrue(policy.enabled('gis_map'))
                self.assertTrue(policy.enabled('gis_map'))
            with app.test_request_context('/'):
                self.assertFalse(policy.enabled('gis_map'))
            self.assertTrue(policy.enabled('gis_map'))
            self.assertFalse(policy.enabled('gis_map'))
            self.assertEqual(verify.call_count,4)

    def test_new_optional_service_calls_denied_before_any_body_effect(self):
        cases=[('services/radius_sim_service.py','simulate_radius_auth',('u','p')),
               ('services/subscriber_portal_service.py','save_subscriber_portal_settings',({},)),
               ('services/wireguard_service.py','add_wireguard_tunnel',({},)),
               ('services/stats_service.py','get_sales_chart_data',())]
        for path,name,args in cases:
            with self.subTest(service=name),patch.object(policy,'verified_state',return_value={'valid':True,'features':dict.fromkeys(policy.FEATURE_KEYS,False)}):
                function=source_function(path,name)
                with self.assertRaises(policy.FeatureUnavailable):function(*args)

    def test_worker_disabled_does_not_touch_database_kernel_or_delivery(self):
        cases=[('services/wireguard_service.py','sync_all_wireguard_peers_to_kernel',()),
               ('services/loyalty_rewards_service.py','award_loyalty_points',('synthetic',10)),
               ('services/whatsapp_service.py','send_whatsapp_message',('000000','synthetic')),
               ('services/bot_notifications_service.py','trigger_daily_sales_summary',())]
        for path,name,args in cases:
            with self.subTest(worker=name),patch.object(policy,'verified_state',return_value={'valid':True,'features':dict.fromkeys(policy.FEATURE_KEYS,False)}):
                result=source_function(path,name)(*args)
                self.assertFalse(result[0] if isinstance(result,tuple) else result)

    def test_completed_wallet_retry_returns_receipt_without_second_transfer(self):
        account={'id':2,'wallet_balance':30,'is_active':1,'is_deleted':0}
        tx=Mock(return_value=account)
        common={'db_session':lambda:nullcontext(object()),'_money':lambda n,**kw:float(n),
                '_manager_tx':tx,'_active_financial_manager':lambda row:None,
                '_operation_invoice':lambda *args:('existing-receipt',{'notes':'تحويل إلى #3: local','balance_after':20})}
        function=source_function('services/manager_service.py','transfer_manager_wallet',common)
        with patch.object(policy,'verified_state',return_value={'valid':True,'features':{'reseller_wallets':False}}):
            self.assertEqual(function(2,3,10,notes='local'),(20,'existing-receipt'))
            self.assertEqual(tx.call_count,2) # only the two account reads
        common['_operation_invoice']=lambda *args:(None,None)
        tx.reset_mock()
        function=source_function('services/manager_service.py','transfer_manager_wallet',common)
        with patch.object(policy,'verified_state',return_value={'valid':True,'features':{'reseller_wallets':False}}):
            with self.assertRaises(policy.FeatureUnavailable):function(2,3,10,notes='local')
            self.assertEqual(tx.call_count,2)

    def test_security_deactivation_and_settlement_are_not_optional_modules(self):
        self.assertEqual(policy.endpoint_features('toggle_manager_status_action',{'_target_role':2,'_target_active':True}),())
        self.assertEqual(policy.endpoint_features('toggle_manager_status_action',{'_target_role':2,'_target_active':False}),('reseller_management',))
        for endpoint in ['pay_invoice_action','settle_manager_debt_action','void_manager_invoice_action','logout','user_logout','api_renew','api_disconnect','license_status']:
            self.assertEqual(policy.endpoint_features(endpoint),())
        self.assertEqual(policy.endpoint_features('edit_manager_action',{'_target_role':1,'role_id':1}),())
        self.assertEqual(policy.endpoint_features('api_subscriber_portal_save'),('hotspot_designer',))

    def test_http_denial_precedes_route_effect_and_preserves_core_endpoint(self):
        # Load the boundary file without executing web/__init__.py.
        spec=importlib.util.spec_from_file_location('isolated_feature_boundary',ROOT/'web/license_features.py')
        boundary=importlib.util.module_from_spec(spec);spec.loader.exec_module(boundary)
        app=Flask(__name__);effects=[]
        app.add_url_rule('/api/optional',endpoint='api_hotspot_generator_save',view_func=lambda:effects.append('write') or {'success':True},methods=['POST'])
        app.add_url_rule('/api/core',endpoint='api_renew',view_func=lambda:effects.append('renew') or {'success':True},methods=['POST'])
        boundary.install_feature_access(app)
        client=app.test_client()
        with patch.object(policy,'verified_state',return_value={'valid':True,'features':{'hotspot_designer':False}}):
            result=client.post('/api/optional',json={})
            self.assertEqual(result.status_code,403)
            self.assertEqual(result.json['error'],'FEATURE_NOT_LICENSED')
            self.assertEqual(effects,[])
            self.assertEqual(client.post('/api/core',json={}).status_code,200)
            self.assertEqual(effects,['renew'])


if __name__=='__main__':unittest.main()

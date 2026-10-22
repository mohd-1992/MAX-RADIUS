"""Verified feature entitlements, independent of roles and account ownership.

Absent fields retain the original client's enabled meaning. Financial history,
settlement, license administration and basic RADIUS are not optional modules.
No database/config import occurs until an actual entitlement decision is made.
"""
from functools import wraps

FEATURE_KEYS=frozenset('user_portal automation_rules gis_map autoheal accounting_archiver radius_simulator traffic_analytics api_access white_label whatsapp_gateway vpn_tunnels loyalty_rewards card_designer quick_cards reseller_management reseller_wallets sales_reports invoice_management userman_import data_import hotspot_designer portal_registration portal_advance'.split())
DEPENDENCIES={'reseller_wallets':('reseller_management',),'portal_registration':('user_portal',),'portal_advance':('user_portal',)}

GROUPS={
 'gis_map':'coverage_map_page api_coverage_map_data api_coverage_map_update api_towers_create api_towers_delete api_access_points_create api_access_points_update api_access_points_delete api_access_points_list',
 'autoheal':'autoheal_page api_autoheal_status api_autoheal_diagnostic api_autoheal_probe_ports api_autoheal_restart api_autoheal_logs api_autoheal_purge_zombies api_autoheal_save_settings api_autoheal_resolve_alert api_autoheal_clear_resolved_alerts api_watchdog_manual_check',
 'accounting_archiver':'accounting_archiver_page api_accounting_archiver_status api_accounting_archiver_run api_accounting_archiver_compact api_maintenance_archive_sessions',
 'radius_simulator':'radius_simulator_page api_radius_simulator_test',
 'traffic_analytics':'traffic_analytics_page api_traffic_analytics_status',
 'automation_rules':'automation_rules_page api_automation_toggle api_automation_run',
 'whatsapp_gateway':'whatsapp_dashboard whatsapp_templates_page whatsapp_broadcast_page whatsapp_logs_page whatsapp_simulator_page api_whatsapp_save_settings api_whatsapp_save_template api_whatsapp_send_test api_whatsapp_send_broadcast api_whatsapp_clear_logs api_whatsapp_simulate_chat api_whatsapp_webhook api_whatsapp_qrcode api_whatsapp_sync_status api_whatsapp_restart_instance',
 'vpn_tunnels':'l2tp_tunnels_page l2tp_save_settings_action api_l2tp_settings l2tp_add_tunnel_action l2tp_edit_tunnel_action api_l2tp_live_status api_l2tp_mikrotik_script download_l2tp_mikrotik_script wireguard_add_tunnel_action wireguard_edit_tunnel_action api_wireguard_live_status api_wireguard_mikrotik_script download_wireguard_mikrotik_script api_wireguard_generate_keys',
 'loyalty_rewards':'loyalty_rewards_page api_loyalty_reward_get api_loyalty_reward_add api_loyalty_reward_edit api_loyalty_reward_delete api_loyalty_reward_toggle api_loyalty_award_points api_loyalty_redeem_reward user_redeem_reward',
 'card_designer':'voucher_designs_list new_voucher_design edit_voucher_design save_card_design_api upload_card_bg_api delete_card_design_api set_default_card_design_api duplicate_card_design_api',
 'quick_cards':'quick_cards_page quick_cards_history quick_cards_issue quick_cards_details quick_cards_print reseller_issue_settings',
 'reseller_management':'resellers add_reseller_action',
 'reseller_wallets':'reseller_wallets_page api_reseller_wallet_transfer api_reseller_wallet_deposit topup_reseller_action deposit_manager_action deduct_manager_action',
 'sales_reports':'sales_reports export_sales',
 'invoice_management':'create_invoice_action save_invoice_design_action',
 'userman_import':'mikrotik_userman_import_page api_userman_job_fetch api_userman_job_import api_mikrotik_userman_analyze_rsc api_mikrotik_userman_test_api api_mikrotik_userman_execute',
 'data_import':'database_migration_studio api_migration_analyze api_migration_execute import_data_page download_sample_template process_import_action',
 'hotspot_designer':'hotspot_generator_page api_hotspot_generator_save api_subscriber_portal_save',
 'user_portal':'user_dashboard user_redeem_reward user_login api_portal_session_status api_portal_login_session api_user_change_speed user_login_action user_register user_register_action user_recharge user_recharge_action user_loan_request_action api_user_loan_request user_packages user_renew_package_action user_change_package_action user_change_password_action user_sessions user_disconnect_session_action',
 'portal_registration':'user_register user_register_action',
 'portal_advance':'user_loan_request_action api_user_loan_request',
}
ENDPOINT_FEATURES={}
for _feature,_names in GROUPS.items():
    for _name in _names.split():ENDPOINT_FEATURES[_name]=ENDPOINT_FEATURES.get(_name,())+(_feature,)

# These services check after their durable receipt lookup, before a new write.
# A disabled module must not turn a completed request into a second purchase.
DEFERRED_ENDPOINTS=frozenset('quick_cards_issue deposit_manager_action deduct_manager_action api_reseller_wallet_transfer api_reseller_wallet_deposit'.split())
BRANDING_FIELDS=frozenset(('network_name','company_name','isp_name','network_logo','logo_url','system_title'))


class FeatureUnavailable(ValueError):
    def __init__(self,key):
        self.feature=key
        super().__init__('هذه الميزة غير متاحة في ترخيص المنظومة.')


def _verified_state():
    from services.license_guard_service import get_active_license_status
    return get_active_license_status(force_refresh=True)


def verified_state():
    from flask import g,has_request_context
    if not has_request_context():return _verified_state()
    if not hasattr(g,'verified_feature_state'):g.verified_feature_state=_verified_state()
    return g.verified_feature_state


def enabled(key,state=None):
    if key not in FEATURE_KEYS:raise ValueError('Unknown client feature contract')
    state=verified_state() if state is None else state
    if not isinstance(state,dict) or state.get('valid') is not True:return False
    features=state.get('features') or {}
    if not isinstance(features,dict):return False
    # Same bool/default behavior as has_license_feature, including old packages.
    return bool(features.get(key,True)) and all(enabled(dep,state) for dep in DEPENDENCIES.get(key,()))


def require(*keys):
    state=verified_state()
    for key in keys:
        if not enabled(key,state):raise FeatureUnavailable(key)


def guarded(*keys):
    def decorate(function):
        @wraps(function)
        def checked(*args,**kwargs):
            require(*keys)
            return function(*args,**kwargs)
        checked.license_features=tuple(keys)
        return checked
    return decorate


def endpoint_features(endpoint,data=None):
    endpoint=(endpoint or '').rsplit('.',1)[-1]
    data=data or {}
    required=ENDPOINT_FEATURES.get(endpoint,())
    if endpoint=='add_manager_action' and data and str(data.get('role_id',2))!='1':
        required+=('reseller_management',)
    if endpoint=='edit_manager_action' and data.get('_target_role') is not None:
        if str(data['_target_role'])!='1' or str(data.get('role_id',data['_target_role']))!='1':required+=('reseller_management',)
    if endpoint=='toggle_manager_status_action' and data.get('_target_role') is not None:
        if str(data['_target_role'])!='1' and not data.get('_target_active'):required+=('reseller_management',)
    if endpoint=='update_settings_action' and BRANDING_FIELDS.intersection(data):required+=('white_label',)
    return tuple(dict.fromkeys(required))


def endpoint_enabled(endpoint,data=None):
    required=endpoint_features(endpoint,data)
    if not required:return True
    state=verified_state()
    return all(enabled(key,state) for key in required)

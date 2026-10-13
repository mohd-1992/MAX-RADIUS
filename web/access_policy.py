"""Shared administrative visibility policy; no DB writes or application startup."""
from flask import request, jsonify, redirect, url_for, render_template
from core.rbac import get_current_manager, has_permission
from core.ownership import owner_id, can_access_owned_endpoint, guard_owned_request, scoped_query_one
from web.endpoint_permissions import ENDPOINT_PERMISSIONS
from core.permission_catalog import required_detail, ACTION_ENDPOINTS

ADMIN_BLUEPRINTS = {'quick_cards_bp', 'dashboard_bp', 'vouchers_bp', 'subscribers_bp', 'packages_bp',
                    'resellers_bp', 'nas_bp', 'accounting_bp', 'system_bp', 'whatsapp_bp'}
TARGET_GUARDED_ENDPOINTS = {'api_delete_entity', 'api_extend_time', 'api_terminate',
    'api_disable_account', 'api_activate_subscriber', 'api_renew', 'api_change_package',
    'api_add_quota', 'api_usage_history', 'api_add_wallet', 'api_deduct_wallet',
    'api_disconnect', 'batches_bulk_action'}


def can_access_endpoint(endpoint, data=None):
    endpoint = (endpoint or '').rsplit('.', 1)[-1]
    if not can_access_owned_endpoint(endpoint):
        return False
    if endpoint in ('dashboard', 'login', 'logout', 'static', 'impersonate_exit', 'profile_update'):
        return get_current_manager() is not None
    permissions = ENDPOINT_PERMISSIONS.get(endpoint)
    detail = required_detail(endpoint, data)
    return bool(permissions) and all(has_permission(code) for code in permissions) and (not detail or has_permission(detail))


def can_access_account_action(action, target_type):
    if owner_id() is not None and (target_type == 'subscriber' or action in ('add-wallet', 'deduct-wallet')):
        return False
    endpoint = next((endpoint for endpoint, value in ACTION_ENDPOINTS.items() if value == action), None)
    if not endpoint:
        return False
    detail = required_detail(endpoint, {'target_type': target_type})
    from core.permission_catalog import permission_rows
    parent = next((row['parent'] for row in permission_rows() if row['code'] == detail), None)
    return bool(parent and has_permission(parent) and has_permission(detail))


def can_view_network_totals():
    # A combined count/traffic total reveals both account populations.
    if owner_id() is not None:
        return has_permission('vouchers.view')
    return has_permission('subscribers.view') and has_permission('vouchers.view')


def filter_dashboard_metrics(metrics):
    if owner_id() is not None:
        metrics = owned_dashboard_metrics()
    allowed = set()
    if has_permission('subscribers.view') and owner_id() is None:
        allowed.update(('total_subscribers', 'online_subscribers', 'online_subscriber_devices'))
    if has_permission('vouchers.view'):
        allowed.update(('vouchers', 'voucher_summary'))
    if can_view_network_totals():
        allowed.update(('active_sessions', 'total_unique_online', 'traffic_in_str', 'traffic_out_str', 'traffic_total_str'))
    if has_permission('nas.view') and owner_id() is None:
        allowed.add('total_nas')
    if has_permission('system.view_services') and owner_id() is None:
        allowed.add('resources')
    return {key: value for key, value in metrics.items() if key in allowed}


def permission_denied():
    if request.is_json or request.path.startswith('/api/') or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify(success=False, message='لا تملك صلاحية هذا الإجراء.'), 403
    return render_template('access_denied.html'), 403


def filter_manager_visibility(managers, kpis):
    if has_permission('managers.billing'):
        return managers, kpis
    financial = {'wallet_balance', 'credit_limit', 'net_outstanding_debt',
                 'total_deposits', 'total_purchases', 'cash_deposits', 'credit_deposits',
                 'debt_payments', 'card_purchases', 'commission_percent', 'invoices_count',
                 'total_cash_deposits', 'total_credit_deposits', 'total_debt_payments'}
    return ([{k: v for k, v in row.items() if k not in financial} for row in managers],
            {k: v for k, v in kpis.items() if k in ('total_managers', 'active_managers', 'roles_count')})


def filter_account_money(account):
    if has_permission('invoices.view') or has_permission('wallets.manage'):
        return account
    return {k: v for k, v in account.items() if k not in ('balance', 'balance_formatted', 'wallet_balance')}


def filter_nas_credentials(device):
    if device is None:
        return device
    hidden = set()
    if not has_permission('nas.manage'):
        hidden.update(('secret', 'api_password', 'api_username'))
    if not can_view_network_totals():
        hidden.update(('active_users', 'total_active_users', 'active_sessions', 'total_download', 'total_upload'))
    return {key: value for key, value in device.items() if key not in hidden}


def install_visibility_policy(app):
    @app.before_request
    def enforce_endpoint_permissions():
        try:
            guard_owned_request()
        except (PermissionError, ValueError):
            return permission_denied()
        endpoint = (request.endpoint or '').rsplit('.', 1)[-1]
        detail = required_detail(endpoint, request.get_json(silent=True) or request.values)
        if detail == '!invalid':
            return jsonify(success=False, message='العملية أو نوع الحساب غير صالح.'), 400
        if detail and not has_permission(detail):
            if not get_current_manager():
                if request.is_json or request.path.startswith('/api/'):
                    return jsonify(success=False, message='يرجى تسجيل الدخول أولًا.'), 401
                return redirect(url_for('login', next=request.url))
            return permission_denied()
        permissions = ENDPOINT_PERMISSIONS.get(endpoint)
        if permissions is None:
            if request.blueprint in ADMIN_BLUEPRINTS and endpoint not in TARGET_GUARDED_ENDPOINTS and endpoint != 'api_whatsapp_webhook':
                return permission_denied()
            return None
        manager = get_current_manager()
        if not manager:
            if request.path.startswith('/api/') or request.is_json:
                return jsonify(success=False, message='يرجى تسجيل الدخول أولًا.'), 401
            return redirect(url_for('login', next=request.url))
        if not all(has_permission(code, manager) for code in permissions):
            return permission_denied()

    @app.context_processor
    def visibility_helpers():
        return dict(can_access_endpoint=can_access_endpoint,
                    can_access_account_action=can_access_account_action,
                    can_view_network_totals=can_view_network_totals,
                    is_reseller_scope=lambda: owner_id() is not None,
                    reseller_owner_id=owner_id())


def owned_dashboard_metrics():
    counts = scoped_query_one("""SELECT COUNT(*) AS total,
        COALESCE(SUM(status='unused'),0) AS unused,
        COALESCE(SUM(status IN ('active','used')),0) AS active,
        COALESCE(SUM(status='expired'),0) AS expired,
        COALESCE(SUM(status IN ('suspended','disabled')),0) AS disabled,
        COALESCE(SUM(status='recharged'),0) AS recharged,
        COALESCE(SUM(first_used_at IS NOT NULL OR status IN ('active','used','expired')),0) AS activated
        FROM wisp_vouchers""") or {}
    sessions = scoped_query_one("""SELECT COUNT(*) AS devices, COUNT(DISTINCT username) AS users
        FROM radacct WHERE acctstoptime IS NULL AND
        (acctupdatetime >= CURRENT_TIMESTAMP - INTERVAL 5 MINUTE OR
         (acctupdatetime IS NULL AND acctstarttime >= CURRENT_TIMESTAMP - INTERVAL 5 MINUTE))""") or {}
    summary = dict(total=counts.get('total',0),available=counts.get('unused',0),
        activated=counts.get('activated',0),total_card_subscribers=counts.get('activated',0),
        active_subscribers=counts.get('active',0),expired_subscribers=counts.get('expired',0),
        disabled_subscribers=counts.get('disabled',0),recharged_subscribers=counts.get('recharged',0),
        online_devices=sessions.get('devices',0),online=sessions.get('devices',0),
        online_cards=sessions.get('users',0),online_subscribers=sessions.get('users',0))
    from core.rate_limit import format_bytes
    traffic = scoped_query_one('SELECT COALESCE(SUM(acctinputoctets),0) AS rx, COALESCE(SUM(acctoutputoctets),0) AS tx FROM radacct') or {}
    return dict(vouchers=counts,voucher_summary=summary,active_sessions=sessions.get('devices',0),
                total_unique_online=sessions.get('users',0), online_subscribers=0, online_subscriber_devices=0,
                traffic_in_str=format_bytes(traffic.get('rx',0)),traffic_out_str=format_bytes(traffic.get('tx',0)),
                traffic_total_str=format_bytes(traffic.get('rx',0)+traffic.get('tx',0)))

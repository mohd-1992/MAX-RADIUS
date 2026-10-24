"""Request-bound reseller ownership; background and subscriber-portal work is unchanged.

Administrative reads use filtered relations, so aggregates, joins and pagination
operate on the same owned rows. Writes must additionally pass the target guard.
No schema migration or inference of ownership from an imported username.
"""
import json
import re
from flask import g, has_request_context, request
from core.rbac import get_current_manager
from database.db import query_all as raw_query_all, query_one as raw_query_one


def ownership_manager():
    if not has_request_context():
        return None
    endpoint = (request.endpoint or '').rsplit('.', 1)[-1]
    # A subscriber portal may coexist with an admin browser cookie.
    if request.blueprint == 'portal_bp' or request.path.startswith('/portal'):
        return None
    if endpoint in ('login', 'logout', 'impersonate_exit', 'impersonate_manager', 'profile_update'):
        return None
    if not hasattr(g, '_ownership_manager'):
        g._ownership_manager = get_current_manager()
    return g._ownership_manager


def is_global_admin(manager):
    return bool(manager and (manager.get('id') == 1 or manager.get('role_id') == 1
                            or manager.get('role_code') == 'superadmin'))


def owner_id():
    manager = ownership_manager()
    network_manager = bool(manager and manager.get('role_id') == 2 and manager.get('role_code') == 'manager')
    return int(manager['id']) if manager and not is_global_admin(manager) and not network_manager else None


def allowed_package_ids(manager):
    value = manager.get('allowed_packages')
    if not value:
        return None  # Existing empty setting means all packages.
    try:
        values = json.loads(value) if isinstance(value, str) and value.startswith('[') else value.split(',') if isinstance(value, str) else value
        return {int(v) for v in values}
    except (ValueError, TypeError):
        raise ValueError('قائمة الباقات المسموحة غير صالحة.')


_RELATIONS = {
    'wisp_quick_card_issues': 'reseller_id={owner}',
    'wisp_vouchers': 'reseller_id={owner}',
    'wisp_voucher_batches': 'reseller_id={owner}',
    'wisp_voucher_sales': 'reseller_id={owner}',
    'wisp_managers': 'id={owner}',
    'wisp_manager_invoices': 'manager_id={owner}',
    'wisp_roles': 'id={role}',
    # Legacy reseller IDs are a different identity namespace.
    'wisp_resellers': '1=0',
    'wisp_reseller_transactions': '1=0',
    # Subscribers/invoices currently have no reliable stored reseller owner.
    'wisp_subscribers': '1=0',
    'wisp_invoices': '1=0',
    'radacct': 'username IN (SELECT username FROM wisp_vouchers WHERE reseller_id={owner})',
    'wisp_session_baselines': 'username IN (SELECT username FROM wisp_vouchers WHERE reseller_id={owner})',
    'user_audit_logs': 'username IN (SELECT username FROM wisp_vouchers WHERE reseller_id={owner})',
}
_TABLE_REF = re.compile(r'\b(FROM|JOIN)\s+`?([a-zA-Z_]\w*)`?', re.I)
_KEYWORDS = {'where', 'join', 'left', 'right', 'inner', 'outer', 'cross', 'on',
             'order', 'group', 'limit', 'having', 'union', 'offset', 'for', 'set', 'using'}


def scope_select(statement):
    ident = owner_id()
    if ident is None:
        return statement
    if not re.match(r'^\s*SELECT\b', statement, re.I):
        raise ValueError('استعلام القراءة غير صالح.')
    manager = ownership_manager()
    conditions = dict(_RELATIONS)
    allowed = allowed_package_ids(manager)
    # Catalog restrictions must not hide an already-owned historical card whose
    # original package was later removed from the purchase catalog.
    account_read = any(name in statement.lower() for name in _RELATIONS)
    if allowed is not None and not account_read:
        conditions['wisp_packages'] = 'id IN (' + ','.join(str(v) for v in sorted(allowed)) + ')' if allowed else '1=0'
    def replace(match):
        name = match[2].lower()
        if name not in conditions:
            return match[0]
        condition = conditions[name].format(owner=ident, role=int(manager.get('role_id') or 0))
        remainder = statement[match.end():]
        alias = re.match(r'\s+(?:AS\s+)?([a-zA-Z_]\w*)', remainder, re.I)
        suffix = '' if alias and alias[1].lower() not in _KEYWORDS else ' AS ' + name
        return match[1] + ' (SELECT * FROM ' + name + ' WHERE ' + condition + ')' + suffix
    return _TABLE_REF.sub(replace, statement)


def scoped_query_all(statement, params=()):
    return raw_query_all(scope_select(statement), params)


def scoped_query_one(statement, params=()):
    return raw_query_one(scope_select(statement), params)


def require_owned(table, ident=None, username=None):
    owner = owner_id()
    if owner is None:
        return
    if table not in ('wisp_vouchers', 'wisp_voucher_batches', 'wisp_managers'):
        raise PermissionError('لا تملك صلاحية الوصول إلى هذا الحساب.')
    owner_column = 'id' if table == 'wisp_managers' else 'reseller_id'
    key = 'username' if username is not None else 'id'
    value = username if username is not None else ident
    if key == 'id':
        text = str(value)
        if isinstance(value, bool) or not text.isascii() or not text.isdigit() or int(text) <= 0:
            raise PermissionError('معرف البيانات غير صالح.')
        value = int(text)
    elif not isinstance(value, str) or not value.strip():
        raise PermissionError('اسم الكرت غير صالح.')
    row = raw_query_one(f'SELECT id FROM {table} WHERE {key}=? AND {owner_column}=?', (value, owner))
    if not row:
        raise PermissionError('لا تملك صلاحية الوصول إلى هذه البيانات.')


def can_access_owned_endpoint(endpoint):
    if endpoint == 'static':
        return True
    if owner_id() is None:
        return True
    endpoint = (endpoint or '').rsplit('.', 1)[-1]
    from web.endpoint_permissions import ENDPOINT_PERMISSIONS
    permissions = ENDPOINT_PERMISSIONS.get(endpoint, [])
    if endpoint in ('profile_update', 'login', 'logout', 'dashboard', 'api_dashboard_live_metrics', 'impersonate_exit'):
        return True
    if endpoint in ('managers_page', 'manager_statement_page', 'manager_invoices_page', 'manager_operation_receipt', 'reseller_wallets_page', 'resellers', 'sales_reports', 'export_sales'):
        return True
    # Shared settings, infrastructure and manager money/role mutations are admin-only.
    return bool(permissions) and all(p.startswith('vouchers.') or p == 'packages.view' for p in permissions) and 'design' not in endpoint and endpoint not in ('upload_card_bg_api',)


def guard_owned_request():
    # Flask serves public UI assets through this exact endpoint. Do not apply
    # account ownership to CSS, JavaScript, fonts or images; file confinement
    # remains Flask's responsibility. Never bypass by URL prefix alone.
    if request.endpoint == 'static':
        return
    owner = owner_id()
    if owner is None:
        return
    endpoint = (request.endpoint or '').rsplit('.', 1)[-1]
    target_actions = {'api_delete_entity', 'api_extend_time', 'api_terminate', 'api_disable_account',
                     'api_activate_subscriber', 'api_renew', 'api_change_package', 'api_add_quota',
                     'api_usage_history', 'api_add_wallet', 'api_deduct_wallet', 'api_disconnect'}
    if endpoint not in target_actions and endpoint != 'batches_bulk_action' and not can_access_owned_endpoint(endpoint):
        raise PermissionError('هذه الصفحة أو العملية متاحة للإدارة فقط.')
    data = request.get_json(silent=True) or request.values
    if not hasattr(data, 'get'):
        raise ValueError('صيغة الطلب غير صالحة.')
    if endpoint in target_actions:
        if endpoint in ('api_add_wallet', 'api_deduct_wallet'):
            raise PermissionError('تعديل الرصيد النقدي متاح للإدارة فقط.')
        if data.get('target_type', 'subscriber') not in ('voucher', 'card'):
            raise PermissionError('لا يوجد إسناد ملكية موثق لهذا الحساب إلى الموزع.')
        ids = data.get('target_ids') or data.get('target_id') or data.get('ids') or data.get('id')
        if not request.is_json:
            ids = request.form.getlist('target_ids[]') or request.form.getlist('target_ids') or ids
        if ids is None:
            raise PermissionError('حدد الكروت المملوكة لك.')
        if not isinstance(ids, (list, tuple)):
            text = str(ids).strip()
            try:
                ids = json.loads(text) if text.startswith('[') else text.split(',')
            except ValueError:
                raise PermissionError('معرفات الكروت غير صالحة.')
        if not isinstance(ids, (list, tuple)):
            raise PermissionError('معرفات الكروت غير صالحة.')
        for ident in ids:
            require_owned('wisp_vouchers', ident=ident)
    if endpoint == 'batches_bulk_action':
        if str(data.get('action') or '').strip() == 'assign_reseller':
            raise PermissionError('تغيير ملكية الحزم متاح للإدارة فقط.')
        ids = data.get('batch_ids', [])
        if isinstance(ids, str):
            try:
                ids = json.loads(ids)
            except ValueError:
                ids = ids.split(',')
        if not isinstance(ids, (list, tuple)) or not ids:
            raise PermissionError('حدد الحزم المملوكة لك.')
        for ident in ids:
            require_owned('wisp_voucher_batches', ident=ident)
    for key, value in (request.view_args or {}).items():
        table = {'batch_id': 'wisp_voucher_batches', 'card_id': 'wisp_vouchers', 'manager_id': 'wisp_managers'}.get(key)
        if table:
            require_owned(table, ident=value)
    if request.method != 'GET':
        if data.get('package_id') or data.get('new_package_id'):
            package = data.get('new_package_id') or data.get('package_id')
            card_id = (request.view_args or {}).get('card_id') or data.get('card_id')
            old = raw_query_one('SELECT package_id FROM wisp_vouchers WHERE id=? AND reseller_id=?',
                                (card_id, owner)) if card_id else None
            if not old or str(old['package_id']) != str(package):
                generation_owner(owner, package)
        for key in ('card_id', 'voucher_id'):
            if data.get(key) is not None:
                require_owned('wisp_vouchers', ident=data[key])
        # In edit forms username is the proposed NEW name; their card ID was
        # validated above. These three APIs use username as a target identifier.
        if data.get('username') and endpoint in (
                'api_reset_voucher_mac', 'api_extend_voucher', 'api_disconnect_voucher'):
            require_owned('wisp_vouchers', username=data['username'])


def generation_owner(requested_owner, package_id):
    owner = owner_id()
    if owner is None:
        return requested_owner
    text = str(package_id)
    if isinstance(package_id, bool) or not text.isascii() or not text.isdigit() or int(text) <= 0:
        raise PermissionError('معرف الباقة غير صالح.')
    manager = ownership_manager()
    allowed = allowed_package_ids(manager)
    if allowed is not None and int(package_id) not in allowed:
        raise PermissionError('الباقة غير مسموحة لهذا الموزع.')
    return owner

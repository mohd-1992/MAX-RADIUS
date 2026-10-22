"""Load visibility functions without importing the application or DB configuration."""
import ast
from pathlib import Path
from flask import request, jsonify, redirect, url_for, render_template
from core.permission_catalog import required_detail, ACTION_ENDPOINTS, permission_rows

ROOT = Path(__file__).resolve().parents[1]


def install_fixture_visibility(app, namespace, enforce=False):
    from license_feature_fixture import fixture_endpoint_enabled
    policy = dict(request=request, jsonify=jsonify, redirect=redirect,
                  url_for=url_for, render_template=render_template,
                  has_permission=namespace['has_permission'],
                  get_current_manager=namespace['get_current_manager'],
                  owner_id=lambda: None, can_access_owned_endpoint=lambda endpoint: True,
                  guard_owned_request=lambda: None,
                  required_detail=required_detail, ACTION_ENDPOINTS=ACTION_ENDPOINTS,
                  endpoint_enabled=fixture_endpoint_enabled)
    parents = {row['code']: row['parent'] for row in permission_rows()}
    legacy_check = namespace['has_permission']
    policy['has_permission'] = lambda code, manager=None: legacy_check(parents.get(code, code), manager)
    namespace['has_permission'] = policy['has_permission']
    exec((ROOT/'web/endpoint_permissions.py').read_text(encoding='utf8'), policy)
    tree = ast.parse((ROOT/'web/access_policy.py').read_text(encoding='utf8'))
    exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.Assign))], type_ignores=[]), 'access_policy.py', 'exec'), policy)
    namespace.setdefault('owner_id', lambda: None)
    namespace.update({name: policy[name] for name in ('filter_manager_visibility', 'filter_account_money', 'filter_dashboard_metrics', 'can_view_network_totals', 'filter_nas_credentials')})
    if enforce:
        policy['install_visibility_policy'](app)
    else:
        app.context_processor(lambda: dict(can_access_endpoint=policy['can_access_endpoint'], can_view_network_totals=policy['can_view_network_totals'],
            can_access_account_action=policy['can_access_account_action'],
            is_reseller_scope=lambda: False, reseller_owner_id=None))
    return policy

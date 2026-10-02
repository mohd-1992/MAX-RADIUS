# -*- coding: utf-8 -*-
"""
Authentication & Role-Based Access Control (RBAC) Decorators for MAX RADIUS Web Application.
"""

from functools import wraps
from flask import request, session, redirect, url_for, jsonify, flash
from core.rbac import (
    get_current_manager,
    has_permission,
    require_permission,
    get_manager_permissions,
    verify_manager_password,
    hash_manager_password,
    login_required
)

def require_role_or_permission(permission_name):
    """
    Decorator to enforce permissions or fallback to admin roles.
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            manager = get_current_manager()
            if not manager:
                if request.is_json or request.path.startswith('/api/'):
                    return jsonify({'success': False, 'message': 'يرجى تسجيل الدخول أولاً للمتابعة.', 'unauthenticated': True}), 401
                return redirect(url_for('login', next=request.url))
            
            if not has_permission(permission_name):
                if request.is_json or request.path.startswith('/api/'):
                    return jsonify({'success': False, 'message': 'ليس لديك الصلاحية الكافية للقيام بهذا الإجراء.'}), 403
                flash('ليس لديك الصلاحية الكافية للوصول إلى هذه الصفحة أو تنفيذ هذا الإجراء.', 'danger')
                return redirect(url_for('dashboard'))
                
            return f(*args, **kwargs)
def require_feature(feature_key):
    """
    Decorator to restrict access based on verified license features.
    If the feature is disabled in the active license, it blocks direct URL visits and API calls.
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            from services.license_guard_service import has_license_feature
            if not has_license_feature(feature_key):
                if request.is_json or request.path.startswith('/api/'):
                    return jsonify({
                        'success': False,
                        'error': 'FEATURE_NOT_LICENSED',
                        'message': f'ميزة ({feature_key}) غير مشمولة في باقة ترخيصك الحالية. يرجى الترقية لتفعيلها.'
                    }), 403
                flash('تنبيه: هذه الميزة غير مفعلة في باقة ترخيص هذا السيرفر. يرجى مراجعة إدارة الدعم الفني لترقية الباقة.', 'warning')
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator

__all__ = [
    'get_current_manager',
    'has_permission',
    'require_permission',
    'get_manager_permissions',
    'verify_manager_password',
    'hash_manager_password',
    'login_required',
    'require_role_or_permission',
    'require_feature'
]

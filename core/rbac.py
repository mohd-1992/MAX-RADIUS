# -*- coding: utf-8 -*-
"""
Advanced Role-Based Access Control (RBAC) Module for MAX RADIUS.
Handles authentication, password hashing/verification, route protection decorators,
and Super Admin (ID 1 / superadmin) bypass.
"""

from functools import wraps
import hashlib
from flask import session, flash, redirect, url_for, request, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from database.db import query_one, query_all

def hash_manager_password(raw_password):
    """Generates a secure pbkdf2:sha256 hash for a manager password."""
    if not raw_password:
        return ''
    return generate_password_hash(raw_password, method='pbkdf2:sha256')

def verify_manager_password(stored_hash, raw_password):
    """
    Verifies a raw password against stored hash with fallback support
    for legacy plaintext seeds or simple md5 hashes.
    """
    if not stored_hash or not raw_password:
        return False
    
    # 1. Standard Werkzeug Hash (pbkdf2, scrypt, sha256)
    if stored_hash.startswith(('pbkdf2:', 'scrypt:', 'argon2:', 'sha256:')):
        try:
            return check_password_hash(stored_hash, raw_password)
        except Exception:
            return False
            
    # 2. Plaintext check (e.g. initial setup seeds like 'admin')
    if stored_hash == raw_password:
        return True
        
    # 3. MD5 hash fallback
    try:
        if hashlib.md5(raw_password.encode('utf-8')).hexdigest().lower() == stored_hash.lower():
            return True
    except Exception:
        pass
        
    return False

def get_current_manager():
    """
    Returns the currently authenticated manager/admin object from database,
    or None if no valid active session exists.
    """
    admin_id = session.get('admin_id')
    if not admin_id:
        return None
        
    try:
        manager = query_one('''
            SELECT m.*, r.name as role_name, r.code as role_code
            FROM wisp_managers m
            JOIN wisp_roles r ON m.role_id = r.id
            WHERE m.id = ? AND m.is_active = 1
        ''', (admin_id,))
        return manager
    except Exception:
        return None

def is_authenticated():
    """Returns True if a manager is currently authenticated."""
    return get_current_manager() is not None

def get_manager_permissions(manager_id=None, role_id=None):
    """Returns a set of permission codes owned by the given manager or role."""
    if manager_id == 1 or role_id == 1:
        # Superadmin has ALL permissions
        all_perms = query_all('SELECT code FROM wisp_permissions')
        return {p['code'] for p in all_perms}
        
    if role_id is None and manager_id:
        mgr = query_one('SELECT role_id FROM wisp_managers WHERE id = ?', (manager_id,))
        if mgr:
            role_id = mgr['role_id']
            
    if not role_id:
        return set()
        
    perms = query_all('''
        SELECT p.code
        FROM wisp_role_permissions rp
        JOIN wisp_permissions p ON rp.permission_id = p.id
        WHERE rp.role_id = ?
    ''', (role_id,))
    return {p['code'] for p in perms}

def has_permission(permission_code, manager=None):
    """
    Checks if a manager has a specific permission code.
    Super Admin (ID 1 or role 'superadmin') ALWAYS returns True.
    """
    if manager is None:
        manager = get_current_manager()
        
    if not manager:
        return False
        
    # 1. Super Admin Bypass (ID 1 or role_code == 'superadmin' or role_id == 1)
    if manager.get('id') == 1 or manager.get('role_code') == 'superadmin' or manager.get('role_id') == 1:
        return True
        
    # 2. Check Role Permissions
    user_perms = get_manager_permissions(role_id=manager.get('role_id'))
    return permission_code in user_perms

def login_required(f):
    """Route decorator requiring authenticated admin/manager session."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        manager = get_current_manager()
        if not manager:
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({
                    'success': False,
                    'message': 'يرجى تسجيل الدخول أولاً للمتابعة.',
                    'unauthenticated': True
                }), 401
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def require_permission(permission_code):
    """
    Flask Route Decorator for protecting endpoints with RBAC permissions.
    Usage:
        @app.route('/subscribers/add')
        @require_permission('subscribers.create')
        def add_subscriber():
            ...
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            manager = get_current_manager()
            if not manager:
                if request.is_json or request.path.startswith('/api/'):
                    return jsonify({
                        'success': False,
                        'message': 'يرجى تسجيل الدخول أولاً.',
                        'unauthenticated': True
                    }), 401
                return redirect(url_for('login', next=request.url))
            
            # Super Admin check
            if manager.get('id') == 1 or manager.get('role_code') == 'superadmin' or manager.get('role_id') == 1:
                return f(*args, **kwargs)
                
            # Permission check
            if not has_permission(permission_code, manager):
                if request.is_json or request.path.startswith('/api/'):
                    return jsonify({
                        'success': False,
                        'message': f'ليس لديك صلاحية لتنفيذ هذا الإجراء ({permission_code}). يرجى مراجعة مدير النظام.'
                    }), 403
                    
                flash(f'عذراً، حسابك لا يملك الصلاحية المطلوبة للوصول إلى هذه الصفحة ({permission_code}).', 'danger')
                return redirect(request.referrer or url_for('dashboard'))
                
            return f(*args, **kwargs)
        return decorated_function
    return decorator

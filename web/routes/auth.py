# -*- coding: utf-8 -*-
"""
MAX RADIUS Web - Authentication, Login, and Session Management Routes Module.
"""

import os
import sys
import json
import time
import datetime
import secrets
import logging

from flask import (
    Blueprint, render_template, request, jsonify, redirect,
    url_for, send_file, flash, session, current_app, Response
)

from database.db import (
    query_all, query_one, execute_write, execute_update, execute_many,
    log_audit, log_user_audit, DB_PATH
)
from core.config import (
    DB_TYPE, APP_NAME, APP_VERSION, APP_EDITION, APP_VERSION_FULL, APP_VERSION_BADGE,
    STORAGE_DIR, BACKUPS_DIR, UPLOADS_DIR, LOGS_DIR
)
from core.rate_limit import format_bytes, format_duration, build_mikrotik_rate_limit
from core.mikrotik_api import get_nas_status
from core.rbac import (
    get_current_manager, has_permission, require_permission,
    verify_manager_password, hash_manager_password, login_required
)
from web.decorators import require_role_or_permission

# Services imports
from services.subscriber_service import *
from services.voucher_service import *
from services.card_design_service import *
from services.reseller_service import *
from services.nas_service import *
from services.l2tp_service import *
from services.stats_service import *
from services.user_portal_service import *
from services.import_service import *
from services.quick_action_service import *
from services.backup_service import *
from services.backup_scheduler_service import *
from services.system_control_service import *
from services.manager_service import *
from services.license_guard_service import *
from services.system_update_service import *
from core.time_service import (
    sync_ntp_time, start_ntp_sync_worker, get_time_sync_status,
    get_configured_timezone_name, get_system_now, get_system_now_str
)

logger = logging.getLogger('auth_bp')

auth_bp = Blueprint('auth_bp', __name__)


@auth_bp.route('/health', endpoint="health_check")

def health_check():
    return jsonify({
        'status': 'healthy',
        'app': 'MAX RADIUS',
        'timestamp': datetime.datetime.now().isoformat()
    }), 200


@auth_bp.route('/login', methods=['GET', 'POST'], endpoint="login")

def login():
    if request.method == 'GET':
        if get_current_manager():
            return redirect(url_for('dashboard'))
        return render_template('login.html')

    username = request.form.get('username', '').strip()
    password = request.form.get('password', '').strip()
    remember = request.form.get('remember') == 'on'

    if not username or not password:
        flash('يرجى إدخال اسم المستخدم وكلمة المرور.', 'danger')
        return render_template('login.html'), 400

    manager = query_one('''
        SELECT m.*, r.name as role_name, r.code as role_code
        FROM wisp_managers m
        JOIN wisp_roles r ON m.role_id = r.id
        WHERE LOWER(m.username) = LOWER(?)
    ''', (username,))

    if not manager:
        log_audit(0, username, 'LOGIN_FAILED', 'auth', f'Failed login attempt for non-existing username [{username}]')
        flash('اسم المستخدم أو كلمة المرور غير صحيحة.', 'danger')
        return render_template('login.html'), 401

    if not manager.get('is_active'):
        log_audit(manager['id'], username, 'LOGIN_BLOCKED', 'auth', f'Deactivated account [{username}] attempted login')
        flash('عذراً، هذا الحساب معطل حالياً. يرجى مراجعة إدارة النظام.', 'danger')
        return render_template('login.html'), 403

    if not verify_manager_password(manager['password_hash'], password):
        log_audit(manager['id'], username, 'LOGIN_FAILED', 'auth', f'Invalid password for [{username}]')
        flash('اسم المستخدم أو كلمة المرور غير صحيحة.', 'danger')
        return render_template('login.html'), 401

    # Successful Login
    session.permanent = True
    session['admin_id'] = manager['id']
    session['admin_username'] = manager['username']
    session['role_code'] = manager['role_code']
    session.pop('original_admin_id', None) # Clear any lingering impersonation

    execute_write('UPDATE wisp_managers SET last_login_at = ? WHERE id = ?',
                  (datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), manager['id']))

    log_audit(manager['id'], username, 'LOGIN_SUCCESS', 'auth', f'Manager [{username}] ({manager["role_name"]}) logged in successfully')

    flash(f'مرحباً بك مجدداً، {manager["full_name"]}', 'success')
    next_page = request.form.get('next') or request.args.get('next')
    if next_page and next_page.startswith('/') and not next_page.startswith('//') and not next_page.startswith('/login'):
        return redirect(next_page)
    return redirect(url_for('dashboard'))


@auth_bp.route('/logout', methods=['GET', 'POST'], endpoint="logout")

def logout():
    manager = get_current_manager()
    if manager:
        log_audit(manager['id'], manager['username'], 'LOGOUT', 'auth', f'Manager [{manager["username"]}] logged out')
    session.clear()
    flash('تم تسجيل الخروج بنجاح.', 'info')
    return redirect(url_for('login'))


@auth_bp.route('/profile/update', methods=['POST'], endpoint="profile_update")

def profile_update():
    manager = get_current_manager()
    if not manager:
        return jsonify({'success': False, 'message': 'يرجى تسجيل الدخول أولاً.'}), 401

    try:
        data = request.form if request.form else (request.get_json(silent=True) or {})
        full_name = data.get('full_name', '').strip()
        email = data.get('email', '').strip()
        phone = data.get('phone', '').strip()
        current_password = data.get('current_password', '').strip() or None
        new_password = data.get('new_password', '').strip() or None
        confirm_password = data.get('confirm_password', '').strip() or None

        if not full_name:
            return jsonify({'success': False, 'message': 'الاسم الكامل مطلوب.'}), 400

        if new_password:
            if new_password != confirm_password:
                return jsonify({'success': False, 'message': 'كلمة المرور الجديدة وتأكيدها غير متطابقين.'}), 400
            if len(new_password) < 4:
                return jsonify({'success': False, 'message': 'كلمة المرور يجب ألا تقل عن 4 أحرف.'}), 400

        update_manager_profile(
            manager_id=manager['id'],
            full_name=full_name,
            email=email,
            phone=phone,
            current_password=current_password,
            new_password=new_password
        )

        return jsonify({
            'success': True,
            'message': 'تم تحديث بيانات الملف الشخصي بنجاح.'
        })
    except ValueError as ve:
        return jsonify({'success': False, 'message': str(ve)}), 400
    except Exception as e:
        return jsonify({'success': False, 'message': f'حدث خطأ أثناء التحديث: {str(e)}'}), 500


@auth_bp.route('/managers/<int:manager_id>/impersonate', methods=['POST'], endpoint="impersonate_manager")

def impersonate_manager(manager_id):
    current = get_current_manager()
    if not current:
        return jsonify({'success': False, 'message': 'يرجى تسجيل الدخول أولاً.'}), 401

    # Super Admin Check: ID 1 or role superadmin or original_admin_id == 1
    is_super = (current.get('id') == 1 or current.get('role_code') == 'superadmin' or session.get('original_admin_id') == 1)
    if not is_super:
        return jsonify({'success': False, 'message': 'عذراً، ميزة انتحال الشخصية متاحة فقط للمدير العام (Super Admin).'}), 403

    target = get_manager_by_id(manager_id)
    if not target:
        return jsonify({'success': False, 'message': 'حساب المدير أو الموزع المطلوب غير موجود.'}), 404

    if not target.get('is_active'):
        return jsonify({'success': False, 'message': 'لا يمكن الدخول كحساب معطل.'}), 400

    # Save original admin id if not already set
    if not session.get('original_admin_id'):
        session['original_admin_id'] = current['id']

    session['admin_id'] = target['id']
    session['admin_username'] = target['username']
    session['role_code'] = target['role_code']

    log_audit(target['id'], current['username'], 'IMPERSONATE_START', 'security',
              f'Super Admin [{current["username"]}] impersonated manager [{target["username"]}] ({target["full_name"]})')

    return jsonify({
        'success': True,
        'message': f'أنت تتصفح الآن كـ: {target["full_name"]} ({target["role_name"]})',
        'redirect_url': url_for('dashboard')
    })


@auth_bp.route('/impersonate/exit', methods=['GET', 'POST'], endpoint="impersonate_exit")

def impersonate_exit():
    original_id = session.pop('original_admin_id', None)
    if original_id:
        orig = get_manager_by_id(original_id)
        if orig:
            session['admin_id'] = orig['id']
            session['admin_username'] = orig['username']
            session['role_code'] = orig['role_code']
            log_audit(orig['id'], orig['username'], 'IMPERSONATE_EXIT', 'security', f'Super Admin [{orig["username"]}] exited impersonation mode')
            flash('تمت العودة إلى حساب المدير العام الأصلي بنجاح.', 'success')
            return redirect(url_for('managers_page'))
    
    session['admin_id'] = 1
    flash('تمت العودة إلى لوحة التحكم الرئيسية.', 'info')
    return redirect(url_for('dashboard'))



# -*- coding: utf-8 -*-
"""
MAX RADIUS Web - Subscriber Self-Service Portal Routes Module.
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

logger = logging.getLogger('portal_bp')

portal_bp = Blueprint('portal_bp', __name__)


@portal_bp.route('/user', endpoint="user_dashboard")

@portal_bp.route('/user/dashboard', endpoint="user_dashboard")

def user_dashboard():
    username = session.get('portal_user')
    if not username:
        return redirect(url_for('user_login'))
    
    user_data = get_portal_user_data(username)
    return render_template('user_portal/dashboard.html', user=user_data)


@portal_bp.route('/user/login', methods=['GET'], endpoint="user_login")

def user_login():
    if session.get('portal_user'):
        return redirect(url_for('user_dashboard'))
    return render_template('user_portal/login.html')


@portal_bp.route('/user/login', methods=['POST'], endpoint="user_login_action")

def user_login_action():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '').strip()
    
    user_obj, err = authenticate_portal_user(username, password)
    if err:
        flash(err, 'danger')
        return redirect(url_for('user_login'))
    
    session['portal_user'] = user_obj['username']
    session['portal_type'] = user_obj['type']
    flash(f'مرحباً بك {user_obj["username"]}! تم تسجيل الدخول بنجاح.', 'success')
    return redirect(url_for('user_dashboard'))


@portal_bp.route('/user/logout', endpoint="user_logout")

def user_logout():
    session.pop('portal_user', None)
    session.pop('portal_type', None)
    flash('تم تسجيل الخروج من حسابك بنجاح.', 'info')
    return redirect(url_for('user_login'))


@portal_bp.route('/user/register', methods=['GET'], endpoint="user_register")

def user_register():
    if session.get('portal_user'):
        return redirect(url_for('user_dashboard'))
        
    settings_rows = query_all('SELECT `key`, `value` FROM wisp_system_settings')
    settings_dict = {r['key']: r['value'] for r in settings_rows} if settings_rows else {}
    if settings_dict.get('portal_allow_registration', '1') == '0':
        flash('عذراً، التسجيل الذاتي لإنشاء حسابات جديدة معطل حالياً من قِبل إدارة الشبكة.', 'warning')
        return redirect(url_for('user_login'))
        
    return render_template('user_portal/register.html', old_form={})


@portal_bp.route('/user/register', methods=['POST'], endpoint="user_register_action")

def user_register_action():
    settings_rows = query_all('SELECT `key`, `value` FROM wisp_system_settings')
    settings_dict = {r['key']: r['value'] for r in settings_rows} if settings_rows else {}
    if settings_dict.get('portal_allow_registration', '1') == '0':
        flash('عذراً، التسجيل الذاتي معطل حالياً من قِبل إدارة الشبكة.', 'danger')
        return redirect(url_for('user_login'))
        
    success, msg = register_portal_subscriber(request.form)
    if success:
        flash(msg, 'success')
        return redirect(url_for('user_login'))
    else:
        flash(msg, 'danger')
        return render_template('user_portal/register.html', old_form=request.form)


@portal_bp.route('/user/recharge', methods=['GET'], endpoint="user_recharge")

def user_recharge():
    username = session.get('portal_user')
    if not username:
        return redirect(url_for('user_login'))
    
    user_data = get_portal_user_data(username)
    return render_template('user_portal/recharge.html', user=user_data)


@portal_bp.route('/user/recharge', methods=['POST'], endpoint="user_recharge_action")

def user_recharge_action():
    username = session.get('portal_user')
    if not username:
        return redirect(url_for('user_login'))
    
    card_code = request.form.get('card_code', '').strip()
    recharge_type = request.form.get('recharge_type', 'package').strip()
    success, msg = recharge_user_wallet_by_card(username, card_code, recharge_type)
    
    if success:
        flash(msg, 'success')
    else:
        flash(msg, 'danger')
        
    return redirect(url_for('user_recharge'))


@portal_bp.route('/user/loan/request', methods=['POST'], endpoint="user_loan_request_action")

def user_loan_request_action():
    username = session.get('portal_user')
    if not username:
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': False, 'message': 'يرجى تسجيل الدخول أولاً'}), 401
        return redirect(url_for('user_login'))
        
    success, msg = request_data_loan(username)
    
    if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': success, 'message': msg})
        
    if success:
        flash(msg, 'success')
    else:
        flash(msg, 'danger')
        
    return redirect(url_for('user_dashboard'))


@portal_bp.route('/user/api/loan/request', methods=['POST'], endpoint="api_user_loan_request")

def api_user_loan_request():
    username = session.get('portal_user')
    if not username:
        data = request.json if request.is_json else request.form.to_dict()
        username = data.get('username', '').strip()
        
    if not username:
        return jsonify({'success': False, 'message': 'اسم المشترك غير محدد'}), 400
        
    success, msg = request_data_loan(username)
    return jsonify({'success': success, 'message': msg})


@portal_bp.route('/user/packages', methods=['GET'], endpoint="user_packages")

def user_packages():
    username = session.get('portal_user')
    if not username:
        return redirect(url_for('user_login'))
        
    settings_rows = query_all('SELECT `key`, `value` FROM wisp_system_settings')
    settings_dict = {r['key']: r['value'] for r in settings_rows} if settings_rows else {}
    if settings_dict.get('portal_allow_package_change', '1') == '0':
        flash('عذراً، استعراض وتغيير الباقات الذاتي معطل حالياً من قِبل إدارة الشبكة.', 'warning')
        return redirect(url_for('user_dashboard'))
    
    user_data = get_portal_user_data(username)
    pkgs = query_all('SELECT * FROM wisp_packages WHERE is_active = 1 AND show_in_portal = 1 ORDER BY price ASC')
    return render_template('user_portal/packages.html', user=user_data, packages=pkgs)


@portal_bp.route('/user/renew-package', methods=['POST'], endpoint="user_renew_package_action")

def user_renew_package_action():
    username = session.get('portal_user')
    if not username:
        return redirect(url_for('user_login'))
        
    settings_rows = query_all('SELECT `key`, `value` FROM wisp_system_settings')
    settings_dict = {r['key']: r['value'] for r in settings_rows} if settings_rows else {}
    if settings_dict.get('portal_allow_package_change', '1') == '0':
        flash('عذراً، تجديد الباقة معطل حالياً من قِبل إدارة الشبكة.', 'danger')
        return redirect(url_for('user_dashboard'))
    
    try:
        pkg_id = int(request.form['package_id'])
        success, msg = renew_or_change_package(username, pkg_id)
        if success:
            flash(msg, 'success')
        else:
            flash(msg, 'danger')
    except Exception as e:
        flash(f'خطأ أثناء التجديد: {str(e)}', 'danger')
        
    return redirect(url_for('user_dashboard'))


@portal_bp.route('/user/change-package', methods=['POST'], endpoint="user_change_package_action")

def user_change_package_action():
    username = session.get('portal_user')
    if not username:
        return redirect(url_for('user_login'))
        
    settings_rows = query_all('SELECT `key`, `value` FROM wisp_system_settings')
    settings_dict = {r['key']: r['value'] for r in settings_rows} if settings_rows else {}
    if settings_dict.get('portal_allow_package_change', '1') == '0':
        flash('عذراً، تغيير الباقة معطل حالياً من قِبل إدارة الشبكة.', 'danger')
        return redirect(url_for('user_dashboard'))
    
    try:
        pkg_id = int(request.form['package_id'])
        success, msg = renew_or_change_package(username, pkg_id)
        if success:
            flash(msg, 'success')
        else:
            flash(msg, 'danger')
    except Exception as e:
        flash(f'خطأ أثناء تغيير الباقة: {str(e)}', 'danger')
        
    return redirect(url_for('user_packages'))


@portal_bp.route('/user/change-password', methods=['POST'], endpoint="user_change_password_action")

def user_change_password_action():
    username = session.get('portal_user')
    if not username:
        return redirect(url_for('user_login'))
        
    settings_rows = query_all('SELECT `key`, `value` FROM wisp_system_settings')
    settings_dict = {r['key']: r['value'] for r in settings_rows} if settings_rows else {}
    if settings_dict.get('portal_allow_password_change', '1') == '0':
        flash('عذراً، تغيير كلمة المرور معطل حالياً من قِبل إدارة الشبكة.', 'danger')
        return redirect(url_for('user_dashboard'))
        
    old_pwd = request.form.get('old_password', '').strip()
    new_pwd = request.form.get('new_password', '').strip()
    confirm_pwd = request.form.get('confirm_password', '').strip()
    
    success, msg = change_portal_password(username, old_pwd, new_pwd, confirm_pwd)
    if success:
        flash(msg, 'success')
    else:
        flash(msg, 'danger')
        
    return redirect(url_for('user_dashboard'))


@portal_bp.route('/user/sessions', methods=['GET'], endpoint="user_sessions")

def user_sessions():
    username = session.get('portal_user')
    if not username:
        return redirect(url_for('user_login'))
    
    user_data = get_portal_user_data(username)
    sessions = get_user_sessions_history(username, limit=50)
    return render_template('user_portal/sessions.html', user=user_data, sessions=sessions)


@portal_bp.route('/user/disconnect-session', methods=['POST'], endpoint="user_disconnect_session_action")

def user_disconnect_session_action():
    username = session.get('portal_user')
    if not username:
        return jsonify({'success': False, 'message': 'غير مصرح'}), 401
    
    from services.quick_action_service import action_disconnect_user
    res = action_disconnect_user(username)
    return jsonify(res)



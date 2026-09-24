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
    
    settings_rows = query_all('SELECT `key`, `value` FROM wisp_system_settings')
    settings_dict = {r['key']: r['value'] for r in settings_rows} if settings_rows else {}
    
    user_data = get_portal_user_data(username)
    if not user_data:
        session.pop('portal_user', None)
        return redirect(url_for('user_login'))
        
    speed_options = get_portal_speed_options(settings_dict)

    # Determine active speed option
    active_speed_id = session.get('portal_selected_speed_id')
    if not active_speed_id:
        if user_data.get('is_open_speed'):
            for sp in speed_options:
                if sp.get('is_open'):
                    active_speed_id = sp['id']
                    break
        else:
            curr_down = str(user_data.get('rate_download') or '').strip().upper()
            for sp in speed_options:
                if not sp.get('is_open') and sp['rate_down'].upper().rstrip('M').rstrip('BPS') == curr_down.rstrip('M').rstrip('BPS'):
                    active_speed_id = sp['id']
                    break
                    
        if not active_speed_id and len(speed_options) > 1:
            active_speed_id = speed_options[1]['id'] # default to balanced
        elif not active_speed_id and len(speed_options) > 0:
            active_speed_id = speed_options[0]['id']

    loan_amount_mb = int(settings_dict.get('loan_amount_mb', '1024') if str(settings_dict.get('loan_amount_mb', '')).isdigit() else 1024)
    loan_amount_str = format_mb_or_gb(loan_amount_mb)

    return render_template(
        'user_portal/dashboard.html',
        user=user_data,
        settings=settings_dict,
        speed_options=speed_options,
        active_speed_id=active_speed_id,
        loan_amount_str=loan_amount_str
    )


@portal_bp.route('/user/login', methods=['GET'], endpoint="user_login")
def user_login():
    if session.get('portal_user') and not request.args.get('login_url') and not request.args.get('error') and not request.args.get('logged_out'):
        return redirect(url_for('user_dashboard'))
        
    settings_rows = query_all('SELECT `key`, `value` FROM wisp_system_settings')
    settings_dict = {r['key']: r['value'] for r in settings_rows} if settings_rows else {}
    
    speed_options = get_portal_speed_options(settings_dict)

    # Fetch active packages for pricing tab
    packages = query_all('SELECT * FROM wisp_packages WHERE is_active = 1 AND show_in_portal = 1 ORDER BY price ASC')
    if not packages:
        packages = query_all('SELECT * FROM wisp_packages WHERE is_active = 1 ORDER BY price ASC LIMIT 6')
        
    # Capture & translate any Hotspot/FreeRADIUS errors
    raw_error = request.args.get('error') or request.args.get('error-orig') or request.args.get('error_orig') or request.args.get('errmsg') or ''
    portal_error = translate_portal_error(raw_error) if raw_error else ''

    if request.args.get('logged_out') == '1':
        portal_error = "✅ تم تسجيل الخروج بنجاح."

    return render_template(
        'user_portal/login.html',
        settings=settings_dict,
        portal_packages=packages,
        speed_options=speed_options,
        portal_error=portal_error,
        prefill_username=request.args.get('username', '')
    )


@portal_bp.route('/user/logout', methods=['GET', 'POST'], endpoint="user_logout")
def user_logout():
    """
    Dual-layer user logout:
    1. Terminates session in RADIUS via CoA Disconnect.
    2. Clears web portal session.
    3. Redirects to MikroTik local logout URL if provided, or back to login page.
    """
    username = session.pop('portal_user', None)
    session.pop('portal_type', None)

    if username:
        try:
            from services.quick_action_service import action_disconnect_user
            action_disconnect_user(username)
        except Exception as e:
            logger.warning("Error disconnecting user on logout: %s", e)

    mikrotik_logout = request.args.get('link_logout') or request.args.get('logoutlink') or request.form.get('link_logout')
    if mikrotik_logout:
        return redirect(mikrotik_logout)

    return redirect(url_for('user_login', logged_out=1))


@portal_bp.route('/user/api/session_status', methods=['GET'], endpoint="api_portal_session_status")
def api_portal_session_status():
    """
    Lightweight heartbeat check for subscriber dashboard.
    Returns whether the current user is active, data quota remaining, and uptime.
    """
    username = session.get('portal_user')
    if not username:
        return jsonify({'active': False, 'reason': 'unauthenticated'}), 401

    user_data = get_portal_user_data(username)
    if not user_data:
        return jsonify({'active': False, 'reason': 'user_not_found'})

    is_expired = user_data.get('is_expired', False)
    is_quota_depleted = user_data.get('is_quota_depleted', False)
    
    return jsonify({
        'active': not (is_expired or is_quota_depleted),
        'username': username,
        'status': user_data.get('status_label', 'نشط'),
        'remaining_mb': user_data.get('traffic_balance_mb', 0),
        'remaining_time': user_data.get('time_left_str', ''),
        'is_expired': is_expired,
        'is_quota_depleted': is_quota_depleted
    })


@portal_bp.route('/user/api/login_session', methods=['POST'], endpoint="api_portal_login_session")
def api_portal_login_session():
    data = request.json if request.is_json else request.form.to_dict()
    username = (data.get('username') or '').strip()
    password = (data.get('password') or username).strip()
    speed_profile = (data.get('speed_profile') or data.get('speed_id') or '').strip()
    
    user_obj, err = authenticate_portal_user(username, password)
    if not err and user_obj:
        session['portal_user'] = user_obj['username']
        session['portal_type'] = user_obj['type']

        settings_rows = query_all('SELECT `key`, `value` FROM wisp_system_settings')
        settings_dict = {r['key']: r['value'] for r in settings_rows} if settings_rows else {}
        speed_options = get_portal_speed_options(settings_dict)

        selected_speed = None
        if speed_profile:
            for sp in speed_options:
                if sp['id'] == speed_profile or sp['rate_down'] == speed_profile or sp['name'] == speed_profile:
                    selected_speed = sp
                    break

        if not selected_speed and speed_profile:
            is_open = speed_profile in ['0', '0M', '0K', 0, 'open', 'unlimited']
            selected_speed = {
                'id': speed_profile if not is_open else 'open',
                'name': 'سرعة مخصصة' if not is_open else 'سرعة مفتوحة',
                'rate_down': speed_profile if not is_open else '0',
                'rate_up': speed_profile if not is_open else '0',
                'is_open': is_open
            }

        if selected_speed:
            session['portal_selected_speed_id'] = selected_speed['id']
            session['portal_selected_rate'] = selected_speed['rate_down']

            is_open = selected_speed.get('is_open') or str(selected_speed['rate_down']).strip() in ['0', '0M', '0K', '']

            execute_write("DELETE FROM radreply WHERE LOWER(username) = LOWER(?) AND attribute = 'MikroTik-Rate-Limit'", (user_obj['username'],))

            if not is_open:
                sp_down = selected_speed['rate_down'].upper()
                if not (sp_down.endswith('M') or sp_down.endswith('K') or '/' in sp_down):
                    sp_down = f"{sp_down}M"
                sp_up = str(selected_speed.get('rate_up', sp_down)).upper()
                if not (sp_up.endswith('M') or sp_up.endswith('K') or '/' in sp_up):
                    sp_up = f"{sp_up}M"
                rate_formatted = f"{sp_up}/{sp_down}" if '/' not in sp_down else sp_down
                execute_write("INSERT INTO radreply (username, attribute, op, value) VALUES (?, 'MikroTik-Rate-Limit', ':=', ?)", (user_obj['username'], rate_formatted))

        return jsonify({
            'success': True,
            'username': user_obj['username'],
            'speed_id': session.get('portal_selected_speed_id')
        })
    return jsonify({'success': False, 'error': err or 'فشل التحقق'})


@portal_bp.route('/user/api/change-speed', methods=['POST'], endpoint="api_user_change_speed")
def api_user_change_speed():
    username = session.get('portal_user')
    data = request.json if request.is_json else request.form.to_dict()
    if not username:
        username = (data.get('username') or '').strip()

    if not username:
        return jsonify({'success': False, 'message': 'يرجى تسجيل الدخول أولاً'}), 401

    speed_id = (data.get('speed_id') or data.get('speed') or '').strip()
    rate_down = str(data.get('rate_down') or speed_id or '').strip()
    rate_up = str(data.get('rate_up') or rate_down).strip()

    settings_rows = query_all('SELECT `key`, `value` FROM wisp_system_settings')
    settings_dict = {r['key']: r['value'] for r in settings_rows} if settings_rows else {}
    speed_options = get_portal_speed_options(settings_dict)

    selected_speed = None
    for sp in speed_options:
        if sp['id'] == speed_id or sp['rate_down'] == rate_down:
            selected_speed = sp
            break

    if not selected_speed:
        is_open = rate_down in ['0', '0M', '0K', 0, 'open', 'unlimited'] or speed_id in ['0', 'open', 'unlimited']
        selected_speed = {
            'id': speed_id or ('open' if is_open else 'custom'),
            'name': 'سرعة مفتوحة' if is_open else 'سرعة مخصصة',
            'rate_down': rate_down if not is_open else '0',
            'rate_up': rate_up if not is_open else '0',
            'is_open': is_open
        }

    session['portal_selected_speed_id'] = selected_speed['id']
    session['portal_selected_rate'] = selected_speed['rate_down']

    is_open = selected_speed.get('is_open') or str(selected_speed['rate_down']).strip() in ['0', '0M', '0K', '']

    # 1. Update FreeRADIUS radreply
    execute_write("DELETE FROM radreply WHERE LOWER(username) = LOWER(?) AND attribute = 'MikroTik-Rate-Limit'", (username,))

    if is_open:
        rate_formatted = "0/0"
        display_speed = "سرعة مفتوحة (أقصى سرعة)"
    else:
        sp_down = selected_speed['rate_down'].upper()
        if not (sp_down.endswith('M') or sp_down.endswith('K') or '/' in sp_down):
            sp_down = f"{sp_down}M"
        sp_up = str(selected_speed.get('rate_up', sp_down)).upper()
        if not (sp_up.endswith('M') or sp_up.endswith('K') or '/' in sp_up):
            sp_up = f"{sp_up}M"
        rate_formatted = f"{sp_up}/{sp_down}" if '/' not in sp_down else sp_down
        display_speed = f"{sp_down}bps"
        execute_write("INSERT INTO radreply (username, attribute, op, value) VALUES (?, 'MikroTik-Rate-Limit', ':=', ?)", (username, rate_formatted))

    # 2. Live CoA update to Router
    active_session = query_one("""
        SELECT * FROM radacct 
        WHERE LOWER(username) = LOWER(?) AND acctstoptime IS NULL
        ORDER BY radacctid DESC LIMIT 1
    """, (username,))

    coa_result = None
    if active_session and active_session.get('nasipaddress'):
        nas_ip = active_session['nasipaddress']
        nas_row = query_one("SELECT secret, ports FROM nas WHERE nasname = ? OR nasname = '0.0.0.0/0' ORDER BY id ASC LIMIT 1", (nas_ip,))
        nas_secret = nas_row['secret'] if nas_row else 'max123'
        
        try:
            from core.coa import RadiusCoaClient
            coa_client = RadiusCoaClient(nas_ip, nas_secret, port=3799, timeout=2.5)
            coa_result = coa_client.modify_rate_limit(
                username=active_session['username'],
                framed_ip=active_session.get('framedipaddress'),
                session_id=active_session.get('acctsessionid'),
                mac_address=active_session.get('callingstationid'),
                rate_limit=rate_formatted if not is_open else "0/0"
            )
        except Exception as e:
            logger.warning(f"CoA speed change exception for {username}: {e}")

    return jsonify({
        'success': True,
        'speed_id': selected_speed['id'],
        'speed': display_speed,
        'rate_formatted': rate_formatted,
        'is_open': is_open,
        'coa': coa_result,
        'message': f'تم تفعيل {selected_speed["name"]} بنجاح وتحديثها لحظياً!'
    })


@portal_bp.route('/user/login', methods=['POST'], endpoint="user_login_action")
def user_login_action():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '').strip()
    speed_profile = request.form.get('speed_profile', '').strip()
    
    user_obj, err = authenticate_portal_user(username, password)
    if err:
        flash(err, 'danger')
        return redirect(url_for('user_login'))
    
    session['portal_user'] = user_obj['username']
    session['portal_type'] = user_obj['type']

    settings_rows = query_all('SELECT `key`, `value` FROM wisp_system_settings')
    settings_dict = {r['key']: r['value'] for r in settings_rows} if settings_rows else {}
    speed_options = get_portal_speed_options(settings_dict)

    selected_speed = None
    if speed_profile:
        for sp in speed_options:
            if sp['id'] == speed_profile or sp['rate_down'] == speed_profile or sp['name'] == speed_profile:
                selected_speed = sp
                break

    if not selected_speed and speed_profile:
        is_open = speed_profile in ['0', '0M', '0K', 0, 'open', 'unlimited']
        selected_speed = {
            'id': speed_profile if not is_open else 'open',
            'name': 'سرعة مخصصة' if not is_open else 'سرعة مفتوحة',
            'rate_down': speed_profile if not is_open else '0',
            'rate_up': speed_profile if not is_open else '0',
            'is_open': is_open
        }

    if selected_speed:
        session['portal_selected_speed_id'] = selected_speed['id']
        session['portal_selected_rate'] = selected_speed['rate_down']

        is_open = selected_speed.get('is_open') or str(selected_speed['rate_down']).strip() in ['0', '0M', '0K', '']
        execute_write("DELETE FROM radreply WHERE LOWER(username) = LOWER(?) AND attribute = 'MikroTik-Rate-Limit'", (user_obj['username'],))

        if not is_open:
            sp_down = selected_speed['rate_down'].upper()
            if not (sp_down.endswith('M') or sp_down.endswith('K') or '/' in sp_down):
                sp_down = f"{sp_down}M"
            sp_up = str(selected_speed.get('rate_up', sp_down)).upper()
            if not (sp_up.endswith('M') or sp_up.endswith('K') or '/' in sp_up):
                sp_up = f"{sp_up}M"
            rate_formatted = f"{sp_up}/{sp_down}" if '/' not in sp_down else sp_down
            execute_write("INSERT INTO radreply (username, attribute, op, value) VALUES (?, 'MikroTik-Rate-Limit', ':=', ?)", (user_obj['username'], rate_formatted))

    flash(f'مرحباً بك {user_obj["username"]}! تم تسجيل الدخول بنجاح.', 'success')
    return redirect(url_for('user_dashboard'))


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



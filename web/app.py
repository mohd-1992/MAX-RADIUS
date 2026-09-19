# -*- coding: utf-8 -*-
"""
Main Flask Web Application & REST API:
Provides a modern, responsive Web GUI for managing FreeRADIUS, MikroTik,
Subscribers, Invoices, Vouchers, Active Card Users, Card Inspector, Sales Reports,
Card Designer, and CoA Disconnects.
"""

import os
import sys
import datetime

import json

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from flask import Flask, render_template, request, jsonify, redirect, url_for, send_file, flash, session, has_request_context
from database.db import init_database, query_all, query_one, execute_write, log_audit, log_user_audit, DB_PATH
from core.config import DB_TYPE, APP_NAME, APP_VERSION, APP_EDITION, APP_VERSION_FULL, APP_VERSION_BADGE
from core.radius_sync import sync_package_to_radius

from core.rate_limit import format_bytes, format_duration, build_mikrotik_rate_limit
from core.mikrotik_api import get_nas_status

from services.subscriber_service import (
    get_subscribers, get_subscriber_status_counts, create_subscriber, update_subscriber, delete_subscriber,
    get_subscriber_sessions, disconnect_subscriber_session,
    get_user_audit_logs, get_user_usage_analytics
)
from services.card_design_service import (
    DEFAULT_CARD_CONFIG,
    get_all_card_designs, get_card_design_by_id, get_default_card_design,
    save_card_design, delete_card_design, set_default_card_design,
    duplicate_card_design, process_and_save_card_background, ensure_card_designs_table
)
from services.voucher_service import (
    generate_voucher_batch, get_batches, get_vouchers, get_batch_cards_for_print, delete_batch,
    update_voucher_batch, update_voucher_card, activate_voucher_card, sync_voucher_sales,
    sync_voucher_activations, get_voucher_summary_counts
)
from services.reseller_service import (
    get_resellers, create_reseller, topup_reseller, get_reseller_transactions
)
from services.nas_service import (
    get_nas_devices, add_nas_device, update_nas_device, delete_nas_device, test_nas_coa
)
from services.l2tp_service import (
    get_l2tp_tunnels, get_l2tp_tunnel, add_l2tp_tunnel, update_l2tp_tunnel,
    delete_l2tp_tunnel, generate_mikrotik_rsc_script, detect_vps_public_ip,
    get_l2tp_network_settings, save_l2tp_network_settings
)
from services.stats_service import (
    get_dashboard_metrics, get_traffic_chart_data, get_sales_chart_data,
    get_recent_live_sessions, get_internet_ping, get_system_uptime_str,
    get_latest_system_operations
)
from services.user_portal_service import (
    authenticate_portal_user, get_portal_user_data, recharge_user_wallet_by_card,
    renew_or_change_package, get_user_sessions_history, register_portal_subscriber,
    change_portal_password, ensure_initial_package, format_mb_or_gb, request_data_loan
)
from services.import_service import (
    generate_sample_template, parse_excel_file, validate_import_data, execute_import
)
from services.quick_action_service import (
    action_extend_time, action_terminate_subscription, action_renew_package,
    action_change_package, action_add_quota, action_get_usage_history,
    action_add_wallet_balance, action_deduct_wallet_balance, action_disconnect_user,
    action_delete_entity, action_bulk_execute
)
from services.backup_service import (
    list_backups, create_backup, upload_backup_file, restore_backup,
    delete_backup, get_backup_filepath, CURRENT_SYSTEM_VERSION,
    get_backup_settings, save_backup_settings
)
from services.backup_scheduler_service import (
    init_backup_scheduler, get_scheduler_status, restart_backup_scheduler,
    stop_backup_scheduler, start_backup_scheduler, reload_backup_schedule,
    healthcheck_backup_scheduler
)
from services.system_control_service import (
    get_all_services_status, execute_service_action
)
from core.time_service import (
    sync_ntp_time, start_ntp_sync_worker, get_time_sync_status,
    get_configured_timezone_name, get_system_now, get_system_now_str
)
from core.rbac import (
    get_current_manager, has_permission, require_permission, get_manager_permissions,
    verify_manager_password, hash_manager_password, login_required
)
from services.manager_service import (
    get_all_roles, get_role_by_id, create_role, update_role, delete_role,
    get_all_permissions_grouped, get_role_permission_ids,
    get_all_managers, get_manager_by_id, create_manager, update_manager,
    toggle_manager_status, delete_manager,
    deposit_manager_wallet, deduct_manager_wallet, settle_manager_debt, void_manager_invoice,
    get_manager_statement, get_all_manager_invoices, get_manager_kpis, update_manager_profile
)
from services.license_guard_service import (
    get_active_license_status, install_and_activate_license,
    sync_license_heartbeat_with_server, ensure_license_tables,
    start_license_heartbeat_daemon
)




app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = os.environ.get('SECRET_KEY', 'wisp-radius-super-secret-key-2026')
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 86400
app.jinja_env.auto_reload = True

@app.after_request
def add_cache_control_headers(response):
    if request.path.startswith('/static/vendor/') or request.path.startswith('/static/fonts/'):
        response.headers['Cache-Control'] = 'public, max-age=604800, immutable'
    elif request.path.startswith('/static/'):
        response.headers['Cache-Control'] = 'public, max-age=86400'
    return response

# Robust Session Isolation & Extended Lifetime
app.config['SESSION_COOKIE_NAME'] = 'max_radius_session'
app.config['PERMANENT_SESSION_LIFETIME'] = datetime.timedelta(days=30)
app.config['SESSION_REFRESH_EACH_REQUEST'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = False

# Start Background 5-minute Real-Time License Heartbeat Sync Daemon
try:
    start_license_heartbeat_daemon(interval_seconds=300)
except Exception:
    pass

@app.before_request
def enforce_license_guard_interceptor():
    """
    Strict License Guard Interceptor:
    If the license is invalid, expired, locked due to sync failure, or revoked,
    blocks access to general admin pages and redirects to /settings/license.
    """
    path = request.path
    if (
        path.startswith('/static') or
        path.startswith('/api/v1/license') or
        path == '/settings/license' or
        path == '/settings/license/activate' or
        path == '/settings/license/sync-heartbeat' or
        path == '/login' or
        path == '/logout' or
        path.startswith('/user/')
    ):
        return None

    try:
        lic = get_active_license_status()
        if not lic or not lic.get('valid'):
            if request.is_json or path.startswith('/api/'):
                return jsonify({
                    "success": False,
                    "error": "LICENSE_REQUIRED",
                    "status": lic.get('status', 'unlicensed') if lic else 'unlicensed',
                    "message": lic.get('message', "النظام مقفل: يجب إدخال وتفعيل ترخيص رسمي صالح للمتابعة.") if lic else "النظام غير مرخص"
                }), 403
            return redirect(url_for('license_status_page'))
    except Exception:
        pass

    return None

@app.template_filter('format_mb')
def jinja_format_mb(val_mb):
    return format_mb_or_gb(val_mb)

@app.template_filter('format_speed')
def jinja_format_speed(speed):
    if speed is None:
        return 'غير محدود'
    s = str(speed).strip().lower()
    if s in ('0', '0m', '0k', '0g', '0mb', '0kb', '0gb', 'unlimited', 'none', '', '-'):
        return 'غير محدود'
    return str(speed)

@app.template_filter('format_speed_pair')
def jinja_format_speed_pair(down, up=None):
    if isinstance(down, (tuple, list)):
        if len(down) >= 2:
            up = down[1]
            down = down[0]
        elif len(down) == 1:
            down = down[0]
            up = None
    elif up is None and isinstance(down, dict):
        up = down.get('rate_upload')
        down = down.get('rate_download')
    
    d_is_unlimited = (down is None) or str(down).strip().lower() in ('0', '0m', '0k', '0g', '0mb', '0kb', '0gb', 'unlimited', 'none', '', '-')
    u_is_unlimited = (up is None) or str(up).strip().lower() in ('0', '0m', '0k', '0g', '0mb', '0kb', '0gb', 'unlimited', 'none', '', '-')
    
    if d_is_unlimited and u_is_unlimited:
        return 'سرعة مفتوحة'
    if d_is_unlimited:
        return f"⬇️ سرعة مفتوحة / ⬆️ {up}"
    if u_is_unlimited:
        return f"⬇️ {down} / ⬆️ سرعة مفتوحة"
    return f"⬇️ {down} / ⬆️ {up}"


@app.context_processor
def inject_global_settings():
    try:
        settings_rows = query_all('SELECT `key`, `value` FROM wisp_system_settings')
        settings = {r['key']: r['value'] for r in settings_rows}
    except Exception:
        settings = {}

    network_name = settings.get('network_name') or settings.get('company_name') or settings.get('isp_name') or 'MAX RADIUS'
    network_logo = settings.get('network_logo', '').strip()
    
    logo_url = None
    if network_logo:
        logo_disk_path = os.path.join(app.static_folder, 'uploads', network_logo)
        if os.path.isfile(logo_disk_path):
            logo_url = url_for('static', filename=f'uploads/{network_logo}')

    currency = settings.get('currency', 'YER')
    currency_symbol = settings.get('currency_symbol', 'ر.ي')
    timezone = settings.get('timezone') or get_configured_timezone_name()
    support_phone = settings.get('support_phone', '')
    support_email = settings.get('support_email', '')
    address = settings.get('address', '')
    hotspot_domain = settings.get('hotspot_domain', 'wifi.maxradius.net')
    system_title = settings.get('system_title', 'MAX RADIUS - نظام إدارة الشبكات والفوترة و FreeRADIUS')
    default_coa_port = settings.get('default_coa_port', '3799')
    try:
        nas_cnt_row = query_one('SELECT COUNT(*) as cnt FROM wisp_nas_devices')
        nas_count = nas_cnt_row['cnt'] if nas_cnt_row else 0
    except Exception:
        nas_count = 0

    current_manager = get_current_manager()
    
    # Calculate live system time and timestamp
    sys_now = get_system_now(timezone)
    server_time_now = sys_now.strftime('%Y-%m-%d %H:%M:%S')
    server_time_only = sys_now.strftime('%H:%M:%S')
    server_date_only = sys_now.strftime('%Y-%m-%d')
    server_timestamp_ms = int(sys_now.timestamp() * 1000)

    # Fast Dynamic RADIUS server IP resolution
    radius_server_ip = settings.get('radius_server_ip') or settings.get('server_ip')
    if not radius_server_ip or str(radius_server_ip).strip() in ('127.0.0.1', 'localhost', '0.0.0.0'):
        if has_request_context() and request.host:
            host_ip = request.host.split(':')[0]
            if host_ip and host_ip not in ('127.0.0.1', 'localhost', '0.0.0.0'):
                radius_server_ip = host_ip
    if not radius_server_ip or str(radius_server_ip).strip() in ('127.0.0.1', 'localhost', '0.0.0.0'):
        radius_server_ip = '192.168.1.100'

    return {
        'settings': settings,
        'network_name': network_name,
        'company_name': network_name,
        'isp_name': network_name,
        'network_logo': network_logo,
        'logo_url': logo_url,
        'currency': currency,
        'currency_symbol': currency_symbol,
        'timezone': timezone,
        'system_timezone': timezone,
        'server_time_now': server_time_now,
        'server_time_only': server_time_only,
        'server_date_only': server_date_only,
        'server_timestamp_ms': server_timestamp_ms,
        'support_phone': support_phone,
        'support_email': support_email,
        'address': address,
        'hotspot_domain': hotspot_domain,
        'system_title': system_title,
        'default_coa_port': default_coa_port,
        'radius_server_ip': radius_server_ip,
        'nas_count': nas_count,
        'current_manager': current_manager,
        'has_permission': has_permission,
        'current_year': sys_now.year,
        'app_name': APP_NAME,
        'app_version': APP_VERSION,
        'app_edition': APP_EDITION,
        'system_version': APP_VERSION_FULL,
        'app_version_badge': APP_VERSION_BADGE
    }

@app.route('/api/coa/status')
def api_coa_status():
    try:
        settings_rows = query_all('SELECT `key`, `value` FROM wisp_system_settings')
        settings = {r['key']: r['value'] for r in settings_rows}
        coa_port = settings.get('default_coa_port', '3799')
        
        from core.mikrotik_api import get_all_nas_live_status
        devices_status = get_all_nas_live_status()
        total_nas = len(devices_status)
        online_nas = sum(1 for d in devices_status if d.get('is_online'))
        
        from services.system_control_service import check_freeradius_probe
        radius_active = check_freeradius_probe()
        
        return jsonify({
            'success': True,
            'radius_active': radius_active,
            'is_active': (radius_active and online_nas > 0),
            'port': coa_port,
            'total_nas': total_nas,
            'online_nas': online_nas,
            'protocol': 'RFC 5176',
            'status_text': f'CoA RFC 5176 (:{coa_port})'
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})



@app.before_request
def check_authentication():
    # Public & bypass endpoints (auth & license activation)
    public_endpoints = {
        'login', 'logout', 'static', 'api_coa_status', 'health_check',
        'license_status_page', 'activate_license_action', 'sync_license_heartbeat_action'
    }
    
    if request.endpoint in public_endpoints:
        return None
    if request.path.startswith('/static') or request.path.startswith('/user') or request.path in [
        '/health', '/api/backup/health', '/favicon.ico',
        '/settings/license', '/settings/license/activate', '/settings/license/sync-heartbeat'
    ]:
        return None
    if request.endpoint and (request.endpoint.startswith('portal_') or request.endpoint.startswith('user_')):
        return None
        
    # 1. Check if admin/manager is logged in
    manager = get_current_manager()
    if not manager:
        if request.is_json or request.path.startswith('/api/'):
            return jsonify({
                'success': False,
                'message': 'انتهت الجلسة أو لم تقم بتسجيل الدخول. يرجى تسجيل الدخول أولاً للمتابعة.',
                'unauthenticated': True
            }), 401
        return redirect(url_for('login', next=request.url))

    # 2. Strict License Guard: Intercept and lock system only if explicitly revoked
    try:
        lic_info = get_active_license_status()
        if lic_info and lic_info.get('status') == 'revoked':
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({
                    'success': False,
                    'locked': True,
                    'message': 'تم حظر وإلغاء ترخيص هذا السيرفر عن بُعد من قبل إدارة المطور.',
                    'license_status': 'revoked'
                }), 403
            flash('تنبيه: تم حظر ترخيص هذا السيرفر. يرجى مراجعة إدارة الدعم الفني.', 'danger')
            return redirect(url_for('license_status_page'))
    except Exception as e:
        pass

@app.route('/health')
def health_check():
    return jsonify({
        'status': 'healthy',
        'app': 'MAX RADIUS',
        'timestamp': datetime.datetime.now().isoformat()
    }), 200

# ----------------- Authentication & Session Management -----------------
@app.route('/login', methods=['GET', 'POST'])
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

@app.route('/logout', methods=['GET', 'POST'])
def logout():
    manager = get_current_manager()
    if manager:
        log_audit(manager['id'], manager['username'], 'LOGOUT', 'auth', f'Manager [{manager["username"]}] logged out')
    session.clear()
    flash('تم تسجيل الخروج بنجاح.', 'info')
    return redirect(url_for('login'))

# ----------------- Profile Management -----------------
@app.route('/profile/update', methods=['POST'])
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

# ----------------- Super Admin Impersonation ("Login As") -----------------
@app.route('/managers/<int:manager_id>/impersonate', methods=['POST'])
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

@app.route('/impersonate/exit', methods=['GET', 'POST'])
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

# ----------------- 1. Dashboard -----------------
@app.route('/')
@app.route('/dashboard')
def dashboard():
    metrics = get_dashboard_metrics()
    traffic_chart = get_traffic_chart_data()
    sales_chart = get_sales_chart_data()
    routers = query_all('SELECT * FROM wisp_nas_devices LIMIT 4')
    internet_status = get_internet_ping()
    system_uptime = get_system_uptime_str()
    server_timezone = get_configured_timezone_name()
    latest_operations = get_latest_system_operations(limit=5)
    system_version = APP_VERSION_FULL
    
    return render_template('dashboard.html',
                           metrics=metrics,
                           traffic_chart=traffic_chart,
                           sales_chart=sales_chart,
                           routers=routers,
                           internet_status=internet_status,
                           system_uptime=system_uptime,
                           server_timezone=server_timezone,
                           latest_operations=latest_operations,
                           system_version=system_version)

@app.route('/api/dashboard/live-metrics')
def api_dashboard_live_metrics():
    metrics = get_dashboard_metrics()
    internet_status = get_internet_ping()
    system_uptime = get_system_uptime_str()
    latest_operations = get_latest_system_operations(limit=5)
    return jsonify({
        'success': True,
        'metrics': metrics,
        'internet_status': internet_status,
        'system_uptime': system_uptime,
        'latest_operations': latest_operations
    })

@app.route('/api/system/ping')
def api_system_ping():
    res = get_internet_ping()
    return jsonify({
        'success': True,
        'ping': res
    })


@app.route('/subscribers')
def subscribers():
    search = request.args.get('q', '').strip()
    stype = request.args.get('type', '').strip()
    package_id = request.args.get('package_id', '').strip()
    # Default to 'active' if not explicitly passed in query params
    status = request.args.get('status')
    if status is None:
        status = 'active'
    subs = get_subscribers(search=search, service_type=stype, status=status, package_id=package_id if package_id else None)
    status_counts = get_subscriber_status_counts(search=search, service_type=stype, package_id=package_id if package_id else None)
    packages = query_all('SELECT id, name, service_type, price FROM wisp_packages WHERE is_active = 1 ORDER BY service_type, price ASC')
    return render_template('subscribers.html', subscribers=subs, packages=packages, q=search, type=stype, status=status, status_counts=status_counts, package_id=package_id)

@app.route('/subscribers/create', methods=['GET', 'POST'])
def subscriber_create_page():
    if request.method == 'POST':
        try:
            sub_id = create_subscriber(request.form, admin_username=session.get('admin_username', 'admin'))
            flash(f'تم إنشاء المشترك [{request.form.get("username")}] ومزامنته مع FreeRADIUS بنجاح.', 'success')
            return redirect(url_for('subscriber_details', sub_id=sub_id))
        except Exception as e:
            flash(f'خطأ أثناء إضافة المشترك: {str(e)}', 'danger')
            return redirect(url_for('subscriber_create_page'))
    
    packages = query_all('''
        SELECT id, name, price, service_type, rate_download, rate_upload,
               volume_quota_mb, validity_value, validity_unit, validity_days
        FROM wisp_packages 
        WHERE is_active = 1
        ORDER BY service_type, price ASC
    ''')
    nas_list = query_all('SELECT id, shortname, nasname, type FROM nas ORDER BY id ASC')
    return render_template('subscriber_create.html', packages=packages, nas_list=nas_list)

@app.route('/subscribers/add', methods=['POST'])
def add_subscriber_action():
    try:
        sub_id = create_subscriber(request.form, admin_username=session.get('admin_username', 'admin'))
        flash('تمت إضافة المشترك ومزامنته مع FreeRADIUS بنجاح.', 'success')
        return redirect(url_for('subscriber_details', sub_id=sub_id))
    except Exception as e:
        flash(f'خطأ أثناء إضافة المشترك: {str(e)}', 'danger')
        return redirect(url_for('subscribers'))

@app.route('/subscribers/<int:sub_id>')
def subscriber_details(sub_id):
    sub = query_one('''
        SELECT s.*, p.name as package_name, p.price as package_price, p.rate_download, p.rate_upload,
               p.volume_quota_mb, p.validity_value, p.validity_unit
        FROM wisp_subscribers s
        JOIN wisp_packages p ON s.package_id = p.id
        WHERE s.id = ?
    ''', (sub_id,))
    if not sub:
        flash('المشترك غير موجود.', 'danger')
        return redirect(url_for('subscribers'))

    # Active session (Online)
    from core.time_service import get_utc_cutoff_str
    cutoff_s = get_utc_cutoff_str(3)
    active_sess = query_one('''
        SELECT radacctid, acctsessionid, nasipaddress, framedipaddress, callingstationid,
               acctstarttime, acctsessiontime, acctinputoctets, acctoutputoctets
        FROM radacct
        WHERE LOWER(username) = LOWER(?) AND acctstoptime IS NULL
          AND (
              (acctupdatetime IS NOT NULL AND acctupdatetime >= ?)
              OR
              (acctupdatetime IS NULL AND acctstarttime >= ?)
          )
        ORDER BY radacctid DESC LIMIT 1
    ''', (sub['username'], cutoff_s, cutoff_s))
    
    # Last session (Offline)
    last_sess = query_one('''
        SELECT acctstoptime, nasipaddress, framedipaddress, callingstationid
        FROM radacct
        WHERE LOWER(username) = LOWER(?)
        ORDER BY radacctid DESC LIMIT 1
    ''', (sub['username'],))
    
    if active_sess:
        sub['is_online'] = True
        sub['ip_address'] = active_sess['framedipaddress']
        sub['last_seen'] = 'متصل الآن (Online)'
        nas_ip = active_sess['nasipaddress']
    else:
        sub['is_online'] = False
        sub['ip_address'] = sub.get('static_ip') or (last_sess['framedipaddress'] if last_sess else None)
        sub['last_seen'] = str(last_sess['acctstoptime']) if last_sess and last_sess.get('acctstoptime') else 'لم يسجل دخول بعد'
        nas_ip = last_sess['nasipaddress'] if last_sess else None

    # Resolve NAS router name
    if nas_ip:
        nas_dev = query_one('SELECT name FROM wisp_nas_devices WHERE ip_address = ?', (nas_ip,))
        sub['nas_name'] = f"{nas_dev['name']} ({nas_ip})" if nas_dev else nas_ip
        sub['nas_ip'] = nas_ip
    else:
        sub['nas_name'] = 'غير محدد'
        sub['nas_ip'] = '-'

    cycle_start = sub.get('last_renewed_at')
    if cycle_start:
        traffic = query_one('''
            SELECT COALESCE(SUM(total_in), 0) as up, COALESCE(SUM(total_out), 0) as down
            FROM (
                SELECT nasipaddress, acctsessionid,
                       MAX(acctinputoctets) as total_in,
                       MAX(acctoutputoctets) as total_out
                FROM radacct
                WHERE LOWER(username) = LOWER(?)
                  AND COALESCE(acctstarttime, acctupdatetime, CURRENT_TIMESTAMP) >= ?
                GROUP BY nasipaddress, acctsessionid
            ) AS sub_traffic
        ''', (sub['username'], str(cycle_start)))
    else:
        traffic = query_one('''
            SELECT COALESCE(SUM(total_in), 0) as up, COALESCE(SUM(total_out), 0) as down
            FROM (
                SELECT nasipaddress, acctsessionid,
                       MAX(acctinputoctets) as total_in,
                       MAX(acctoutputoctets) as total_out
                FROM radacct
                WHERE LOWER(username) = LOWER(?)
                GROUP BY nasipaddress, acctsessionid
            ) AS sub_traffic
        ''', (sub['username'],))
    
    down_bytes = traffic['down'] if traffic else 0
    up_bytes = traffic['up'] if traffic else 0
    total_used_bytes = down_bytes + up_bytes
    total_download = format_bytes(down_bytes)
    total_upload = format_bytes(up_bytes)
    total_used_str = format_bytes(total_used_bytes)

    # Volume quota calculation
    base_quota_mb = float(sub.get('volume_quota_mb') or 0)
    extra_quota_mb = float(sub.get('extra_quota_mb') or 0)
    is_limited_package = (base_quota_mb > 0)
    net_quota_mb = max(0.0, base_quota_mb + extra_quota_mb)

    if is_limited_package or extra_quota_mb > 0:
        quota_bytes = int(net_quota_mb * 1024 * 1024)
        remaining_bytes = max(0, quota_bytes - total_used_bytes)
        quota_str = format_bytes(quota_bytes)
        remaining_str = format_bytes(remaining_bytes)
        usage_percent = 100.0 if quota_bytes == 0 else min(100.0, round((total_used_bytes / quota_bytes) * 100, 1))
    else:
        quota_str = 'غير محدود'
        remaining_str = 'غير محدود'
        usage_percent = 0

    # Expiration & Remaining Days calculation
    exp_val = sub.get('expires_at')
    remaining_days_text = "غير محدد"
    is_expired = False
    if exp_val:
        try:
            if isinstance(exp_val, str):
                exp_dt = datetime.datetime.fromisoformat(exp_val.replace('Z', ''))
            else:
                exp_dt = exp_val
            now = datetime.datetime.now()
            diff = (exp_dt - now).total_seconds()
            if diff > 0:
                d = int(diff // 86400)
                h = int((diff % 86400) // 3600)
                remaining_days_text = f"{d} يوم و {h} ساعة متبقية" if d > 0 else f"{h} ساعة متبقية"
                is_expired = False
            else:
                pd = int(abs(diff) // 86400)
                ph = int((abs(diff) % 86400) // 3600)
                remaining_days_text = f"منتهي منذ {pd} يوم" if pd > 0 else f"منتهي منذ {ph} ساعة"
                is_expired = True
        except Exception:
            pass

    sessions = get_subscriber_sessions(sub['username'], limit=50)
    invoices = query_all('SELECT * FROM wisp_invoices WHERE subscriber_id = ? OR LOWER(subscriber_name) = LOWER(?) ORDER BY id DESC LIMIT 50', (sub_id, sub['username']))
    packages = query_all('SELECT id, name, price, service_type, rate_download, rate_upload FROM wisp_packages WHERE is_active = 1')
    audit_logs = get_user_audit_logs('subscriber', sub_id, sub['username'])
    analytics = get_user_usage_analytics(sub['username'])

    return render_template('subscriber_details.html',
                           subscriber=sub,
                           total_download=total_download,
                           total_upload=total_upload,
                           total_used_str=total_used_str,
                           quota_str=quota_str,
                           remaining_str=remaining_str,
                           usage_percent=usage_percent,
                           remaining_days_text=remaining_days_text,
                           is_expired=is_expired,
                           sessions=sessions,
                           invoices=invoices,
                           audit_logs=audit_logs,
                           analytics=analytics,
                           analytics_json=json.dumps(analytics, default=str),
                           packages=packages)

@app.route('/subscribers/edit/<int:sub_id>', methods=['POST'])
def edit_subscriber_action(sub_id):
    try:
        admin_user = session.get('user', {}).get('username', 'admin')
        update_subscriber(sub_id, request.form, admin_username=admin_user)
        flash('تم تحديث بيانات المشترك ومزامنته مع FreeRADIUS بنجاح.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء التعديل: {str(e)}', 'danger')
    if request.form.get('return_to_details'):
        return redirect(url_for('subscriber_details', sub_id=sub_id))
    return redirect(url_for('subscribers'))

@app.route('/subscribers/delete/<int:sub_id>', methods=['POST'])
def delete_subscriber_action(sub_id):
    try:
        delete_subscriber(sub_id)
        flash('تم حذف المشترك وإلغاء صلاحياته من RADIUS.', 'warning')
    except Exception as e:
        flash(f'خطأ أثناء الحذف: {str(e)}', 'danger')
    return redirect(url_for('subscribers'))

@app.route('/subscribers/sessions/<username>')
def subscriber_sessions(username):
    sessions = get_subscriber_sessions(username)
    return jsonify({'username': username, 'sessions': sessions})

@app.route('/api/subscribers/disconnect', methods=['POST'])
def api_disconnect_subscriber():
    data = request.get_json() or request.form
    username = data.get('username')
    if not username:
        return jsonify({'success': False, 'message': 'اسم المشترك مطلوب.'}), 400
    res = disconnect_subscriber_session(username)
    return jsonify(res)

# ----------------- Invoices -----------------
@app.route('/invoices')
def invoices():
    invs = query_all('SELECT * FROM wisp_invoices ORDER BY id DESC')
    paid_count = sum(1 for i in invs if i.get('status') == 'paid')
    unpaid_count = sum(1 for i in invs if i.get('status') == 'unpaid')
    subs = query_all('''
        SELECT s.id, s.username, s.full_name, p.name as package_name, p.price
        FROM wisp_subscribers s
        JOIN wisp_packages p ON s.package_id = p.id
    ''')
    return render_template('invoices.html', invoices=invs, paid_count=paid_count, unpaid_count=unpaid_count, subscribers=subs)

@app.route('/invoices/create', methods=['POST'])
def create_invoice_action():
    try:
        sub_id = int(request.form['subscriber_id'])
        sub = query_one('''
            SELECT s.full_name, p.name as package_name
            FROM wisp_subscribers s
            JOIN wisp_packages p ON s.package_id = p.id
            WHERE s.id = ?
        ''', (sub_id,))
        sub_name = sub['full_name'] if sub else 'مشترك'
        pkg_name = sub['package_name'] if sub else 'باقة إنترنت'
        amount = float(request.form['amount'])
        status = request.form.get('status', 'unpaid')
        due_date = request.form.get('due_date') or None
        notes = request.form.get('notes', '')
        inv_num = f"INV-{datetime.datetime.now().strftime('%Y%m%d')}-{query_one('SELECT COUNT(*)+1 as cnt FROM wisp_invoices')['cnt']:03d}"

        execute_write('''
            INSERT INTO wisp_invoices (invoice_number, subscriber_id, subscriber_name, package_name, amount, status, due_date, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (inv_num, sub_id, sub_name, pkg_name, amount, status, due_date, notes))
        flash(f'تم إصدار الفاتورة {inv_num} بنجاح.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء إنشاء الفاتورة: {str(e)}', 'danger')
    return redirect(url_for('invoices'))

@app.route('/invoices/pay/<int:inv_id>', methods=['POST'])
def pay_invoice_action(inv_id):
    try:
        execute_write('''
            UPDATE wisp_invoices SET status = 'paid', paid_at = CURRENT_TIMESTAMP WHERE id = ?
        ''', (inv_id,))
        flash('تم تسجيل سداد الفاتورة بنجاح.', 'success')
    except Exception as e:
        flash(f'خطأ: {str(e)}', 'danger')
    return redirect(url_for('invoices'))

@app.route('/invoices/design', methods=['POST'])
def save_invoice_design_action():
    try:
        for k in ['invoice_header', 'tax_number', 'invoice_footer']:
            val = request.form.get(k)
            if val is not None:
                execute_write('''
                    INSERT INTO wisp_system_settings (key, value, description)
                    VALUES (?, ?, 'Invoice Setting')
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                ''', (k, val))
        flash('تم حفظ وتطبيق تصميم الفاتورة بنجاح.', 'success')
    except Exception as e:
        flash(f'خطأ: {str(e)}', 'danger')
    return redirect(url_for('invoices'))

# ----------------- 3. Vouchers & Cards -----------------
@app.route('/vouchers')
def vouchers():
    sync_voucher_activations()
    batch_id = request.args.get('batch_id')
    status = request.args.get('status')
    search = request.args.get('q', '').strip()
    package_id = request.args.get('package_id', '').strip()
    reseller_id = request.args.get('reseller_id', '').strip()
    
    batches = get_batches(search=search, package_id=package_id, reseller_id=reseller_id)
    batch_stats = get_voucher_summary_counts()
    
    selected_batch = None
    batch_cards = []
    if batch_id:
        try:
            bid = int(batch_id)
            selected_batch = query_one('''
                SELECT b.*, p.name as package_name, p.price as package_price
                FROM wisp_voucher_batches b
                JOIN wisp_packages p ON b.package_id = p.id
                WHERE b.id = ?
            ''', (bid,))
            batch_cards = get_vouchers(batch_id=bid, status=status, search=search, limit=500)
        except (ValueError, TypeError):
            selected_batch = None

    cards = batch_cards if selected_batch else get_vouchers(status=status, search=search, limit=50)
    packages = query_all("SELECT id, name, price FROM wisp_packages WHERE service_type IN ('hotspot', 'both')")
    resellers = query_all("""
        SELECT m.id, 
               COALESCE(NULLIF(m.full_name, ''), m.username) AS name, 
               COALESCE(m.wallet_balance, 0.00) AS balance,
               r.name AS role_name
        FROM wisp_managers m
        LEFT JOIN wisp_roles r ON m.role_id = r.id
        WHERE (m.is_deleted = 0 OR m.is_deleted IS NULL) AND (m.is_active = 1 OR m.is_active IS NULL)
        ORDER BY m.id ASC
    """)
    return render_template('vouchers.html',
                           batches=batches,
                           cards=cards,
                           packages=packages,
                           resellers=resellers,
                           selected_batch=selected_batch,
                           batch_cards=batch_cards,
                           batch_stats=batch_stats,
                           q=search,
                           package_id=package_id,
                           reseller_id=reseller_id,
                           status=status)

@app.route('/vouchers/generate', methods=['POST'])
def generate_vouchers_action():
    try:
        package_id = int(request.form['package_id'])
        count = int(request.form['count'])
        format_type = request.form.get('format_type', 'pin_only')
        char_type = request.form.get('char_type', 'numbers')
        code_length = int(request.form.get('code_length', 8))
        pass_length = request.form.get('pass_length')
        pass_length = int(pass_length) if pass_length and pass_length.isdigit() else None
        prefix = request.form.get('prefix', '').strip()
        reseller_id = request.form.get('reseller_id')
        reseller_id = int(reseller_id) if reseller_id and reseller_id.isdigit() else None
        name = request.form.get('name', '').strip()
        same_user_pass = request.form.get('same_user_pass') == '1'
        pin_only = (format_type == 'pin_only')
        
        batch_id, batch_num = generate_voucher_batch(
            name=name,
            package_id=package_id,
            count=count,
            prefix=prefix,
            pin_only=pin_only,
            char_type=char_type,
            code_length=code_length,
            reseller_id=reseller_id,
            created_by=session.get('user', 'admin'),
            same_user_pass=same_user_pass,
            pass_length=pass_length
        )
        flash(f'تم توليد الحزمة بنجاح برقم {batch_num} ومزامنة الكروت مع FreeRADIUS.', 'success')
        return redirect(url_for('view_batch_details', batch_id=batch_id))
    except Exception as e:
        flash(f'خطأ أثناء توليد الحزمة: {str(e)}', 'danger')
        return redirect(url_for('vouchers'))

@app.route('/vouchers/batch/<int:batch_id>')
def view_batch_details(batch_id):
    batch = query_one('''
        SELECT b.*, p.name as package_name, 
               COALESCE(p.price, b.price) as package_price, 
               COALESCE(p.rate_download, b.rate_download) as rate_download, 
               COALESCE(p.rate_upload, b.rate_upload) as rate_upload,
               COALESCE(p.volume_quota_mb, b.volume_quota_mb) as volume_quota_mb,
               COALESCE(p.validity_value, b.validity_value) as validity_value,
               COALESCE(p.validity_unit, b.validity_unit) as validity_unit,
               (SELECT COUNT(*) FROM wisp_vouchers WHERE batch_id = b.id AND status = 'unused') as unused_count,
               (SELECT COUNT(*) FROM wisp_vouchers WHERE batch_id = b.id AND status = 'active') as active_count,
               (SELECT COUNT(*) FROM wisp_vouchers WHERE batch_id = b.id AND status = 'expired') as expired_count
        FROM wisp_voucher_batches b
        JOIN wisp_packages p ON b.package_id = p.id
        WHERE b.id = ?
    ''', (batch_id,))
    if not batch:
        flash('الدفعة المطلوبة غير موجودة.', 'danger')
        return redirect(url_for('vouchers'))
    cards = get_vouchers(batch_id=batch_id, limit=2000)
    packages = query_all("SELECT id, name, price FROM wisp_packages WHERE service_type IN ('hotspot', 'both')")
    return render_template('batch_details.html', batch=batch, cards=cards, packages=packages)

@app.route('/vouchers/batch/edit/<int:batch_id>', methods=['POST'])
def edit_batch_action(batch_id):
    try:
        name = request.form['name'].strip()
        pkg_id = int(request.form['package_id'])
        update_voucher_batch(batch_id, name, pkg_id)
        flash('تم تعديل بيانات الحزمة وتحديث باقة المستخدمين في FreeRADIUS (radusergroup) بنجاح.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء تعديل الحزمة: {str(e)}', 'danger')
    
    if request.form.get('return_to_batch'):
        return redirect(url_for('view_batch_details', batch_id=batch_id))
    return redirect(url_for('vouchers'))

@app.route('/vouchers/card/edit/<int:card_id>', methods=['POST'])
def edit_card_action(card_id):
    batch_id = request.form.get('batch_id')
    try:
        new_username = request.form['username'].strip()
        new_password = request.form['password'].strip()
        update_voucher_card(card_id, new_username, new_password)
        flash(f'تم تعديل الكرت [{new_username}] وتحديث بيانات radcheck و radusergroup في FreeRADIUS بنجاح.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء تعديل الكرت: {str(e)}', 'danger')
    
    if batch_id:
        return redirect(url_for('view_batch_details', batch_id=batch_id))
    return redirect(url_for('vouchers'))

@app.route('/vouchers/active-users')
def active_card_users():
    sync_voucher_activations()
    
    search = request.args.get('q', '').strip()
    batch_filter = request.args.get('batch_id', '').strip()
    pkg_filter = request.args.get('package_id', '').strip()
    raw_status = request.args.get('status')
    
    # Default status to 'active' if not provided
    if raw_status is None or raw_status == '':
        status_filter = 'active'
    else:
        status_filter = raw_status.strip()
    
    try:
        page = max(1, int(request.args.get('page', 1)))
    except (ValueError, TypeError):
        page = 1
    per_page = 50
    offset = (page - 1) * per_page
    
    base_where = ["v.status IN ('active', 'used', 'expired', 'recharged', 'disabled')"]
    base_params = []
    
    if search:
        base_where.append("(v.username LIKE ? OR v.serial_number LIKE ? OR v.pin_code LIKE ?)")
        s_pat = f"%{search}%"
        base_params.extend([s_pat, s_pat, s_pat])
        
    if batch_filter:
        try:
            base_where.append("v.batch_id = ?")
            base_params.append(int(batch_filter))
        except (ValueError, TypeError):
            pass
            
    if pkg_filter:
        try:
            base_where.append("v.package_id = ?")
            base_params.append(int(pkg_filter))
        except (ValueError, TypeError):
            pass

    # Base filters for status counts pills
    base_sql = " AND ".join(base_where)
    from services.subscriber_service import get_heartbeat_cutoff_str
    cutoff_s = get_heartbeat_cutoff_str(5)
    
    count_query = f'''
        SELECT 
            COUNT(*) as total_all,
            COALESCE(SUM(CASE WHEN v.status IN ('active', 'used') AND (v.expires_at IS NULL OR v.expires_at > CURRENT_TIMESTAMP) THEN 1 ELSE 0 END), 0) as total_active,
            COALESCE(SUM(CASE WHEN (v.status = 'expired' OR (v.expires_at IS NOT NULL AND v.expires_at <= CURRENT_TIMESTAMP)) AND v.status NOT IN ('recharged', 'disabled') THEN 1 ELSE 0 END), 0) as total_expired,
            COALESCE(SUM(CASE WHEN v.status IN ('recharged', 'disabled') THEN 1 ELSE 0 END), 0) as total_recharged,
            COALESCE(SUM(CASE WHEN v.username IN (SELECT username FROM radacct WHERE acctstoptime IS NULL AND (acctupdatetime >= ? OR acctstarttime >= ?)) THEN 1 ELSE 0 END), 0) as total_online
        FROM wisp_vouchers v
        WHERE {base_sql}
    '''
    status_row = query_one(count_query, (cutoff_s, cutoff_s, *base_params)) or {}
    status_counts = {
        'all': int(status_row.get('total_all') or 0),
        'active': int(status_row.get('total_active') or 0),
        'online': int(status_row.get('total_online') or 0),
        'expired': int(status_row.get('total_expired') or 0),
        'recharged': int(status_row.get('total_recharged') or 0)
    }

    where_clauses = list(base_where)
    params = list(base_params)
            
    if status_filter == 'active':
        where_clauses.append("v.status IN ('active', 'used') AND (v.expires_at IS NULL OR v.expires_at > CURRENT_TIMESTAMP)")
    elif status_filter == 'online':
        where_clauses.append("v.username IN (SELECT username FROM radacct WHERE acctstoptime IS NULL AND (acctupdatetime >= ? OR acctstarttime >= ?))")
        params.extend([cutoff_s, cutoff_s])
    elif status_filter == 'expired':
        where_clauses.append("(v.status = 'expired' OR (v.expires_at IS NOT NULL AND v.expires_at <= CURRENT_TIMESTAMP)) AND v.status NOT IN ('recharged', 'disabled')")
    elif status_filter == 'recharged':
        where_clauses.append("v.status IN ('recharged', 'disabled')")
    elif status_filter == 'all':
        # All statuses already in base_where
        pass
    elif status_filter:
        where_clauses.append("v.status = ?")
        params.append(status_filter)
        
    where_sql = " AND ".join(where_clauses)
    
    count_row = query_one(f"SELECT COUNT(*) as cnt FROM wisp_vouchers v WHERE {where_sql}", tuple(params))
    total_records = count_row['cnt'] if count_row else 0
    total_pages = max(1, (total_records + per_page - 1) // per_page)
    
    cards_query = f'''
        SELECT v.*,
               p.name as package_name, 
               COALESCE(v.snap_price, p.price) as package_price, 
               COALESCE(v.snap_rate_download, p.rate_download) as rate_download, 
               COALESCE(v.snap_rate_upload, p.rate_upload) as rate_upload,
               COALESCE(v.snap_validity_value, p.validity_value) as validity_value, 
               COALESCE(v.snap_validity_unit, p.validity_unit) as validity_unit, 
               COALESCE(v.snap_volume_quota_mb, p.volume_quota_mb) as volume_quota_mb, 
               COALESCE(v.extra_quota_mb, 0) as extra_quota_mb,
               p.service_type as package_service_type,
               b.name as batch_name, b.batch_number,
               r.name as reseller_name
        FROM (
            SELECT v.id FROM wisp_vouchers v
            WHERE {where_sql}
            ORDER BY v.id DESC
            LIMIT ? OFFSET ?
        ) page
        JOIN wisp_vouchers v ON page.id = v.id
        JOIN wisp_packages p ON v.package_id = p.id
        JOIN wisp_voucher_batches b ON v.batch_id = b.id
        LEFT JOIN wisp_resellers r ON v.reseller_id = r.id
        ORDER BY v.id DESC
    '''
    page_params = list(params) + [per_page, offset]
    cards = query_all(cards_query, tuple(page_params))
    
    for idx, c in enumerate(cards):
        c['seq_id'] = c.get('global_seq_id') or (offset + idx + 1)
        
    usernames = [c['username'] for c in cards if c.get('username')]
    traffic_map = {}
    active_map = {}
    last_map = {}
    
    if usernames:
        placeholders = ','.join(['?'] * len(usernames))
        traffic_rows = query_all(f'''
            SELECT v.id as card_id, LOWER(v.username) as username,
                   COALESCE(SUM(t.max_down), 0) as total_down_bytes,
                   COALESCE(SUM(t.max_up), 0) as total_up_bytes,
                   COALESCE(SUM(t.max_time), 0) as total_session_sec
            FROM wisp_vouchers v
            LEFT JOIN (
                SELECT username, nasipaddress, acctsessionid,
                       MAX(acctoutputoctets) as max_down,
                       MAX(acctinputoctets) as max_up,
                       MAX(acctsessiontime) as max_time,
                       MIN(COALESCE(acctstarttime, acctupdatetime)) as sess_start
                FROM radacct
                WHERE username IN ({placeholders})
                GROUP BY username, nasipaddress, acctsessionid
            ) t ON LOWER(v.username) = LOWER(t.username)
               AND (v.last_renewed_at IS NULL OR t.sess_start >= v.last_renewed_at)
            WHERE v.username IN ({placeholders})
            GROUP BY v.id, v.username
        ''', tuple(usernames) + tuple(usernames))
        for tr in (traffic_rows or []):
            traffic_map[tr['username'].lower()] = tr
            
        from core.time_service import get_utc_cutoff_str
        cutoff_s = get_utc_cutoff_str(3)
        active_params = list(usernames) + [cutoff_s, cutoff_s]
        active_rows = query_all(f'''
            SELECT username, framedipaddress, acctsessiontime, acctinputoctets, acctoutputoctets,
                   calledstationid, nasipaddress
            FROM radacct
            WHERE username IN ({placeholders}) AND acctstoptime IS NULL
              AND (
                  (acctupdatetime IS NOT NULL AND acctupdatetime >= ?)
                  OR
                  (acctupdatetime IS NULL AND acctstarttime >= ?)
              )
        ''', tuple(active_params))
        for ar in (active_rows or []):
            active_map[ar['username'].lower()] = ar

        last_rows = query_all(f'''
            SELECT a.username, a.acctstoptime, a.calledstationid, a.nasipaddress, a.framedipaddress
            FROM radacct a
            INNER JOIN (
                SELECT username, MAX(radacctid) as max_id
                FROM radacct
                WHERE username IN ({placeholders})
                GROUP BY username
            ) m ON a.radacctid = m.max_id
        ''', tuple(usernames))
        for lr in (last_rows or []):
            last_map[lr['username'].lower()] = lr

    now = datetime.datetime.now()

    for c in cards:
        u_key = c['username'].lower() if c.get('username') else ''
        t_data = traffic_map.get(u_key, {})
        a_data = active_map.get(u_key)
        l_data = last_map.get(u_key)
        
        down_b = float(t_data.get('total_down_bytes') or 0)
        up_b = float(t_data.get('total_up_bytes') or 0)
        dur_s = float(t_data.get('total_session_sec') or 0)
        total_used_b = down_b + up_b
        
        c['service_type'] = c.get('service_type') or c.get('package_service_type') or 'hotspot'
        
        if a_data:
            c['is_online'] = True
            c['ip_address'] = a_data.get('framedipaddress') or '-'
            c['access_point'] = a_data.get('calledstationid') or a_data.get('nasipaddress') or 'MikroTik Hotspot'
            c['last_seen_str'] = 'متصل الآن'
        else:
            c['is_online'] = False
            c['ip_address'] = (l_data.get('framedipaddress') if l_data else None) or '-'
            c['access_point'] = (l_data.get('calledstationid') or l_data.get('nasipaddress') if l_data else None) or '-'
            if l_data and l_data.get('acctstoptime'):
                ast = l_data['acctstoptime']
                if isinstance(ast, datetime.datetime):
                    c['last_seen_str'] = ast.strftime('%Y-%m-%d %H:%M')
                else:
                    c['last_seen_str'] = str(ast)[:16]
            elif c.get('first_used_at'):
                c['last_seen_str'] = str(c['first_used_at'])[:16]
            else:
                c['last_seen_str'] = 'لم يتصل بعد'
                
        # Remaining days calculation
        exp = c.get('expires_at')
        if isinstance(exp, str):
            try:
                exp_dt = datetime.datetime.fromisoformat(exp.replace('Z', ''))
            except Exception:
                exp_dt = None
        elif isinstance(exp, datetime.datetime):
            exp_dt = exp
        else:
            exp_dt = None
            
        if c.get('status') == 'expired':
            c['is_expired'] = True
            c['days_remaining_num'] = 0
            c['days_remaining_str'] = '0 يوم (منتهي)'
        elif exp_dt:
            delta = exp_dt - now
            if delta.total_seconds() <= 0:
                c['is_expired'] = True
                c['days_remaining_num'] = 0
                c['days_remaining_str'] = '0 يوم (منتهي)'
            else:
                c['is_expired'] = False
                d_days = delta.days
                d_hours = delta.seconds // 3600
                c['days_remaining_num'] = d_days
                if d_days > 0:
                    c['days_remaining_str'] = f'{d_days} يوم و {d_hours} س'
                else:
                    d_mins = (delta.seconds % 3600) // 60
                    c['days_remaining_str'] = f'{d_hours} س و {d_mins} د'
        elif c.get('validity_value'):
            c['is_expired'] = False
            c['days_remaining_num'] = int(c['validity_value']) if str(c['validity_value']).isdigit() else 30
            unit_ar = {'days': 'يوم', 'hours': 'ساعة', 'months': 'شهر'}.get(c.get('validity_unit', 'days'), c.get('validity_unit', ''))
            c['days_remaining_str'] = f"{c['validity_value']} {unit_ar}"
        else:
            c['is_expired'] = False
            c['days_remaining_num'] = 9999
            c['days_remaining_str'] = 'غير محدد'
            
        # Consumption calculation
        base_quota_mb = float(c.get('volume_quota_mb') or 0)
        extra_quota_mb = float(c.get('extra_quota_mb') or 0)
        is_limited_package = (base_quota_mb > 0)
        vol_quota_mb = max(0.0, base_quota_mb + extra_quota_mb)
        c['has_quota'] = (is_limited_package or extra_quota_mb > 0)
        c['used_bytes_str'] = format_bytes(total_used_b)
        if c['has_quota']:
            quota_b = vol_quota_mb * 1024 * 1024
            c['quota_bytes_str'] = format_bytes(quota_b)
            rem_b = max(0.0, quota_b - total_used_b)
            c['remaining_bytes_str'] = format_bytes(rem_b)
            c['consumption_percent'] = 100.0 if quota_b == 0 else min(100.0, round((total_used_b / quota_b) * 100, 1))
        else:
            c['quota_bytes_str'] = 'غير محدود'
            c['remaining_bytes_str'] = 'غير محدود'
            c['consumption_percent'] = 0
            
        # Format dates & numbers
        if exp_dt:
            c['expires_at_str'] = exp_dt.strftime('%Y-%m-%d %H:%M')
        elif exp:
            c['expires_at_str'] = str(exp)[:16]
        else:
            c['expires_at_str'] = '-'
            
        created = c.get('created_at')
        if isinstance(created, datetime.datetime):
            c['created_at_str'] = created.strftime('%Y-%m-%d %H:%M')
        elif created:
            c['created_at_str'] = str(created)[:16]
        else:
            c['created_at_str'] = '-'
            
        first_used = c.get('first_used_at')
        if isinstance(first_used, datetime.datetime):
            c['first_used_at_str'] = first_used.strftime('%Y-%m-%d %H:%M')
        elif first_used:
            c['first_used_at_str'] = str(first_used)[:16]
        else:
            c['first_used_at_str'] = '-'
            
        c['balance_formatted'] = f"{float(c.get('balance') or 0):,.2f}"
        c['total_down_bytes'] = down_b
        c['total_up_bytes'] = up_b
        c['total_session_sec'] = dur_s
        c['download_str'] = format_bytes(down_b)
        c['upload_str'] = format_bytes(up_b)
        c['uptime_str'] = format_duration(dur_s)

    packages = query_all("SELECT id, name, price, service_type, rate_download, rate_upload FROM wisp_packages WHERE is_active = 1")
    batches = query_all("SELECT id, name, batch_number FROM wisp_voucher_batches ORDER BY id DESC")
    return render_template('active_card_users.html',
                           active_cards=cards,
                           packages=packages,
                           batches=batches,
                           page=page,
                           total_pages=total_pages,
                           total_records=total_records,
                           per_page=per_page,
                           q=search,
                           batch_id=batch_filter,
                           package_id=pkg_filter,
                           status=status_filter,
                           status_counts=status_counts)

# ----------------- Quick Actions AJAX API Endpoints -----------------
def _get_target_params():
    target_type = request.values.get('target_type', 'subscriber')
    ids = request.form.getlist('target_ids[]') or request.form.getlist('target_ids') or request.values.get('target_ids') or request.values.get('target_id')
    if request.is_json:
        data = request.get_json() or {}
        ids = data.get('target_ids') or data.get('target_id') or ids
        target_type = data.get('target_type', target_type)
    return target_type, ids

@app.route('/api/actions/delete', methods=['POST'])
def api_delete_entity():
    target_type, ids = _get_target_params()
    success, msg = action_bulk_execute(action_delete_entity, target_type, ids)
    return jsonify({'success': success, 'message': msg})

@app.route('/api/actions/extend-time', methods=['POST'])
def api_extend_time():
    target_type, ids = _get_target_params()
    days = request.form.get('days', 30)
    success, msg = action_bulk_execute(action_extend_time, target_type, ids, days=days)
    return jsonify({'success': success, 'message': msg})

@app.route('/api/actions/terminate', methods=['POST'])
def api_terminate():
    target_type, ids = _get_target_params()
    success, msg = action_bulk_execute(action_terminate_subscription, target_type, ids)
    return jsonify({'success': success, 'message': msg})

@app.route('/api/actions/renew', methods=['POST'])
def api_renew():
    target_type, ids = _get_target_params()
    success, msg = action_bulk_execute(action_renew_package, target_type, ids)
    return jsonify({'success': success, 'message': msg})

@app.route('/api/actions/change-package', methods=['POST'])
def api_change_package():
    target_type, ids = _get_target_params()
    new_package_id = request.form.get('new_package_id')
    success, msg = action_bulk_execute(action_change_package, target_type, ids, new_package_id=new_package_id)
    return jsonify({'success': success, 'message': msg})

@app.route('/api/actions/add-quota', methods=['POST'])
def api_add_quota():
    target_type, ids = _get_target_params()
    quota_amount = request.form.get('quota_amount', 0)
    quota_unit = request.form.get('quota_unit', 'GB')
    success, msg = action_bulk_execute(action_add_quota, target_type, ids, quota_amount=quota_amount, quota_unit=quota_unit)
    return jsonify({'success': success, 'message': msg})

@app.route('/api/actions/usage-history', methods=['GET', 'POST'])
def api_usage_history():
    target_type = request.values.get('target_type', 'subscriber')
    target_id = request.values.get('target_id')
    success, msg, data = action_get_usage_history(target_type, target_id)
    return jsonify({'success': success, 'message': msg, 'data': data})

@app.route('/api/actions/add-wallet', methods=['POST'])
def api_add_wallet():
    target_type, ids = _get_target_params()
    amount = request.form.get('amount', 0)
    notes = request.form.get('notes', '')
    success, msg = action_bulk_execute(action_add_wallet_balance, target_type, ids, amount=amount, notes=notes)
    return jsonify({'success': success, 'message': msg})

@app.route('/api/actions/deduct-wallet', methods=['POST'])
def api_deduct_wallet():
    target_type, ids = _get_target_params()
    amount = request.form.get('amount', 0)
    notes = request.form.get('notes', '')
    success, msg = action_bulk_execute(action_deduct_wallet_balance, target_type, ids, amount=amount, notes=notes)
    return jsonify({'success': success, 'message': msg})

@app.route('/api/actions/disconnect', methods=['POST'])
def api_disconnect():
    target_type, ids = _get_target_params()
    success, msg = action_bulk_execute(action_disconnect_user, target_type, ids)
    return jsonify({'success': success, 'message': msg})


@app.route('/vouchers/active-users/<int:card_id>')
def active_card_user_details(card_id):
    card = query_one('''
        SELECT v.*, p.name as package_name, 
               COALESCE(v.snap_price, p.price) as price, 
               COALESCE(v.snap_rate_download, p.rate_download) as rate_download, 
               COALESCE(v.snap_rate_upload, p.rate_upload) as rate_upload,
               COALESCE(v.snap_volume_quota_mb, p.volume_quota_mb) as volume_quota_mb, 
               COALESCE(v.snap_validity_value, p.validity_value) as validity_value, 
               COALESCE(v.snap_validity_unit, p.validity_unit) as validity_unit, 
               COALESCE(v.snap_validity_days, p.validity_days) as validity_days,
               r.name as reseller_name, b.name as batch_name, b.batch_number
        FROM wisp_vouchers v
        JOIN wisp_packages p ON v.package_id = p.id
        LEFT JOIN wisp_resellers r ON v.reseller_id = r.id
        LEFT JOIN wisp_voucher_batches b ON v.batch_id = b.id
        WHERE v.id = ?
    ''', (card_id,))
    if not card:
        flash('الكرت المطلوب غير موجود.', 'danger')
        return redirect(url_for('active_card_users'))

    # Active session (Online)
    from core.time_service import get_utc_cutoff_str
    cutoff_s = get_utc_cutoff_str(3)
    active_sess = query_one('''
        SELECT radacctid, acctsessionid, nasipaddress, framedipaddress, callingstationid,
               acctstarttime, acctsessiontime, acctinputoctets, acctoutputoctets
        FROM radacct
        WHERE LOWER(username) = LOWER(?) AND acctstoptime IS NULL
          AND (
              (acctupdatetime IS NOT NULL AND acctupdatetime >= ?)
              OR
              (acctupdatetime IS NULL AND acctstarttime >= ?)
          )
        ORDER BY radacctid DESC LIMIT 1
    ''', (card['username'], cutoff_s, cutoff_s))
    
    # Last session (Offline)
    last_sess = query_one('''
        SELECT acctstoptime, nasipaddress, framedipaddress, callingstationid
        FROM radacct
        WHERE LOWER(username) = LOWER(?)
        ORDER BY radacctid DESC LIMIT 1
    ''', (card['username'],))

    if active_sess:
        card['is_online'] = True
        card['ip_address'] = active_sess['framedipaddress']
        card['last_seen'] = 'متصل الآن (Online)'
        nas_ip = active_sess['nasipaddress']
    else:
        card['is_online'] = False
        card['ip_address'] = last_sess['framedipaddress'] if last_sess else None
        card['last_seen'] = str(last_sess['acctstoptime']) if last_sess and last_sess.get('acctstoptime') else 'لم يسجل دخول بعد'
        nas_ip = last_sess['nasipaddress'] if last_sess else None

    # Resolve NAS router name
    if nas_ip:
        nas_dev = query_one('SELECT name FROM wisp_nas_devices WHERE ip_address = ?', (nas_ip,))
        card['nas_name'] = f"{nas_dev['name']} ({nas_ip})" if nas_dev else nas_ip
        card['nas_ip'] = nas_ip
    else:
        card['nas_name'] = 'غير محدد'
        card['nas_ip'] = '-'

    cycle_start = card.get('last_renewed_at')
    if cycle_start:
        traffic = query_one('''
            SELECT COALESCE(SUM(total_in), 0) as up, COALESCE(SUM(total_out), 0) as down
            FROM (
                SELECT nasipaddress, acctsessionid,
                       MAX(acctinputoctets) as total_in,
                       MAX(acctoutputoctets) as total_out
                FROM radacct
                WHERE LOWER(username) = LOWER(?)
                  AND COALESCE(acctstarttime, acctupdatetime, CURRENT_TIMESTAMP) >= ?
                GROUP BY nasipaddress, acctsessionid
            ) AS card_traffic
        ''', (card['username'], str(cycle_start)))
    else:
        traffic = query_one('''
            SELECT COALESCE(SUM(total_in), 0) as up, COALESCE(SUM(total_out), 0) as down
            FROM (
                SELECT nasipaddress, acctsessionid,
                       MAX(acctinputoctets) as total_in,
                       MAX(acctoutputoctets) as total_out
                FROM radacct
                WHERE LOWER(username) = LOWER(?)
                GROUP BY nasipaddress, acctsessionid
            ) AS card_traffic
        ''', (card['username'],))
    
    down_bytes = traffic['down'] if traffic else 0
    up_bytes = traffic['up'] if traffic else 0
    total_used_bytes = down_bytes + up_bytes
    total_download = format_bytes(down_bytes)
    total_upload = format_bytes(up_bytes)
    total_used_str = format_bytes(total_used_bytes)

    # Volume quota calculation
    base_quota_mb = float(card.get('volume_quota_mb') or 0)
    extra_quota_mb = float(card.get('extra_quota_mb') or 0)
    is_limited_package = (base_quota_mb > 0)
    net_quota_mb = max(0.0, base_quota_mb + extra_quota_mb)

    if is_limited_package or extra_quota_mb > 0:
        quota_bytes = int(net_quota_mb * 1024 * 1024)
        remaining_bytes = max(0, quota_bytes - total_used_bytes)
        quota_str = format_bytes(quota_bytes)
        remaining_str = format_bytes(remaining_bytes)
        usage_percent = 100.0 if quota_bytes == 0 else min(100.0, round((total_used_bytes / quota_bytes) * 100, 1))
    else:
        quota_str = 'غير محدود'
        remaining_str = 'غير محدود'
        usage_percent = 0

    # Expiration & Remaining Days calculation
    exp_val = card.get('expires_at')
    remaining_days_text = "غير محدد"
    is_expired = False
    if exp_val:
        try:
            if isinstance(exp_val, str):
                exp_dt = datetime.datetime.fromisoformat(exp_val.replace('Z', ''))
            else:
                exp_dt = exp_val
            now = datetime.datetime.now()
            diff = (exp_dt - now).total_seconds()
            if diff > 0:
                d = int(diff // 86400)
                h = int((diff % 86400) // 3600)
                remaining_days_text = f"{d} يوم و {h} ساعة متبقية" if d > 0 else f"{h} ساعة متبقية"
                is_expired = False
            else:
                pd = int(abs(diff) // 86400)
                ph = int((abs(diff) % 86400) // 3600)
                remaining_days_text = f"منتهي منذ {pd} يوم" if pd > 0 else f"منتهي منذ {ph} ساعة"
                is_expired = True
        except Exception:
            pass

    sessions = get_subscriber_sessions(card['username'], limit=50)
    invoices = query_all('''
        SELECT id, COALESCE(serial_number, CONCAT('V-SALE-', id)) as invoice_number, 
               activated_at as created_at,
               price as amount, 'شراء وتفعيل كرت' as operation_type, 'paid' as status
        FROM wisp_voucher_sales
        WHERE LOWER(username) = LOWER(?)
        ORDER BY id DESC
    ''', (card['username'],))
    packages = query_all("SELECT id, name, price, service_type, rate_download, rate_upload FROM wisp_packages WHERE service_type IN ('hotspot', 'both')")
    audit_logs = get_user_audit_logs('voucher', card_id, card['username'])
    analytics = get_user_usage_analytics(card['username'])

    return render_template('active_card_user_details.html',
                           card=card,
                           total_download=total_download,
                           total_upload=total_upload,
                           total_used_str=total_used_str,
                           quota_str=quota_str,
                           remaining_str=remaining_str,
                           usage_percent=usage_percent,
                           remaining_days_text=remaining_days_text,
                           is_expired=is_expired,
                           sessions=sessions,
                           invoices=invoices,
                           audit_logs=audit_logs,
                           analytics=analytics,
                           analytics_json=json.dumps(analytics, default=str),
                           packages=packages)

@app.route('/vouchers/active-users/edit/<int:card_id>', methods=['POST'])
def edit_active_card_action(card_id):
    try:
        f = request.form
        new_username = f['username'].strip()
        new_password = f['password'].strip()
        new_pkg_id = int(f.get('package_id', 1))
        bound_mac = f.get('bound_mac', '').strip()
        status = f.get('status', 'active')
        expires_at = f.get('expires_at') or None
        admin_user = session.get('user', {}).get('username', 'admin')

        old_card = query_one('''
            SELECT v.*, p.name as package_name 
            FROM wisp_vouchers v 
            LEFT JOIN wisp_packages p ON v.package_id = p.id 
            WHERE v.id = ?
        ''', (card_id,))
        if not old_card:
            flash('الكرت غير موجود.', 'danger')
            return redirect(url_for('active_card_users'))

        old_username = old_card['username']
        new_pkg = query_one('SELECT name FROM wisp_packages WHERE id = ?', (new_pkg_id,))
        pkg_name = new_pkg['name'] if new_pkg else None

        changes = []
        if old_username != new_username:
            changes.append(f"تعديل اسم المستخدم من '{old_username}' إلى '{new_username}'")
        if old_card.get('password') != new_password:
            changes.append("تعديل كلمة المرور في RADIUS")
        if old_card.get('package_id') != new_pkg_id:
            changes.append(f"تغيير الباقة من '{old_card.get('package_name')}' إلى '{pkg_name}'")
        if (old_card.get('bound_mac') or '') != bound_mac:
            changes.append(f"تعديل تقييد الماك إلى '{bound_mac or 'إلغاء التقييد'}'")
        if (old_card.get('status') or '') != status:
            changes.append(f"تغيير الحالة من '{old_card.get('status')}' إلى '{status}'")
        if str(old_card.get('expires_at') or '') != str(expires_at or ''):
            changes.append(f"تعديل تاريخ الانتهاء إلى '{expires_at or 'تلقائي'}'")

        execute_write('''
            UPDATE wisp_vouchers
            SET username = ?, password = ?, pin_code = ?, package_id = ?, bound_mac = ?, status = ?, expires_at = ?
            WHERE id = ?
        ''', (new_username, new_password, new_password, new_pkg_id, bound_mac or None, status, expires_at, card_id))

        if old_username != new_username:
            execute_write("DELETE FROM radcheck WHERE username = ?", (old_username,))
            execute_write("DELETE FROM radusergroup WHERE username = ?", (old_username,))

        execute_write("DELETE FROM radcheck WHERE username = ? AND attribute = 'Cleartext-Password'", (new_username,))
        execute_write("INSERT INTO radcheck (username, attribute, op, value) VALUES (?, 'Cleartext-Password', ':=', ?)", (new_username, new_password))

        if pkg_name:
            execute_write('DELETE FROM radusergroup WHERE username = ?', (new_username,))
            execute_write('INSERT INTO radusergroup (username, groupname, priority) VALUES (?, ?, 1)', (new_username, pkg_name))

        execute_write("DELETE FROM radcheck WHERE username = ? AND attribute = 'Calling-Station-Id'", (new_username,))
        if bound_mac:
            execute_write("INSERT INTO radcheck (username, attribute, op, value) VALUES (?, 'Calling-Station-Id', '==', ?)", (new_username, bound_mac))

        change_summary = "، ".join(changes) if changes else "تحديث عام لبيانات الكرت"
        log_user_audit('voucher', card_id, new_username, admin_user, 'UPDATE_PROFILE', change_summary)
        log_audit(1, admin_user, 'UPDATE_VOUCHER_CARD', 'vouchers', f'Updated voucher {new_username}: {change_summary}')

        flash('تم حفظ تعديلات الكرت ومزامنة بيانات FreeRADIUS بنجاح.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء تعديل الكرت: {str(e)}', 'danger')

    return redirect(url_for('active_card_user_details', card_id=card_id))

@app.route('/vouchers/update-card', methods=['POST'])
def update_card_action():
    try:
        card_id = int(request.form['card_id'])
        mac = request.form.get('bound_mac', '').strip()
        status = request.form.get('status', 'active')
        execute_write('UPDATE wisp_vouchers SET bound_mac = ?, status = ? WHERE id = ?', (mac, status, card_id))
        flash('تم تعديل بيانات الكرت بنجاح.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء التعديل: {str(e)}', 'danger')
    return redirect(url_for('active_card_users'))

@app.route('/vouchers/inspect')
@app.route('/card-inspect')
@app.route('/card/inspect')
def card_inspect():

    query = request.args.get('q', '').strip()
    card = None
    sessions = []
    total_down = "0 MB"
    total_up = "0 MB"
    is_online = False

    if query:
        card = query_one('''
            SELECT v.*, p.name as package_name, 
                   COALESCE(v.snap_price, p.price) as price, 
                   COALESCE(v.snap_rate_download, p.rate_download) as rate_download, 
                   COALESCE(v.snap_rate_upload, p.rate_upload) as rate_upload,
                   COALESCE(v.snap_volume_quota_mb, p.volume_quota_mb) as volume_quota_mb
            FROM wisp_vouchers v
            JOIN wisp_packages p ON v.package_id = p.id
            WHERE v.username = ? OR v.pin_code = ? OR v.serial_number = ?
            LIMIT 1
        ''', (query, query, query))
        if card:
            sessions = get_subscriber_sessions(card['username'])
            traffic = query_one('''
                SELECT COALESCE(SUM(total_in), 0) as up, COALESCE(SUM(total_out), 0) as down
                FROM (
                    SELECT nasipaddress, acctsessionid,
                           MAX(acctinputoctets) as total_in,
                           MAX(acctoutputoctets) as total_out
                    FROM radacct
                    WHERE LOWER(username) = LOWER(?)
                    GROUP BY nasipaddress, acctsessionid
                ) AS t
            ''', (card['username'],))
            from core.time_service import get_utc_cutoff_str
            cutoff_s = get_utc_cutoff_str(3)
            online_rec = query_one('''
                SELECT 1 FROM radacct 
                WHERE LOWER(username) = LOWER(?) AND acctstoptime IS NULL
                  AND (
                      (acctupdatetime IS NOT NULL AND acctupdatetime >= ?)
                      OR
                      (acctupdatetime IS NULL AND acctstarttime >= ?)
                  )
                LIMIT 1
            ''', (card['username'], cutoff_s, cutoff_s))
            is_online = bool(online_rec)

    return render_template('card_inspect.html', query=query, card=card, sessions=sessions, total_down=total_down, total_up=total_up, is_online=is_online)

@app.route('/sales')
@app.route('/reports/sales')
@app.route('/vouchers/sales')
def sales_reports():
    import math
    sync_voucher_activations()
    sync_voucher_sales()

    today_str = datetime.date.today().strftime('%Y-%m-%d')
    yesterday_str = (datetime.date.today() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
    month_str = datetime.date.today().strftime('%Y-%m')
    year_str = datetime.date.today().strftime('%Y')

    # Get Filter parameters
    period = request.args.get('period', 'all').strip()
    custom_date_from = request.args.get('date_from', '').strip()
    custom_date_to = request.args.get('date_to', '').strip()
    reseller_id = request.args.get('reseller_id', '').strip()
    package_name = request.args.get('package_name', '').strip()
    search_q = request.args.get('q', '').strip()
    try:
        page = max(1, int(request.args.get('page', 1)))
    except (ValueError, TypeError):
        page = 1
    per_page = 50

    # Determine date range boundaries
    start_date = None
    end_date = None

    if period == 'today':
        start_date = f"{today_str} 00:00:00"
        end_date = f"{today_str} 23:59:59"
    elif period == 'yesterday':
        start_date = f"{yesterday_str} 00:00:00"
        end_date = f"{yesterday_str} 23:59:59"
    elif period == 'this_week':
        start_of_week = (datetime.date.today() - datetime.timedelta(days=datetime.date.today().weekday())).strftime('%Y-%m-%d')
        start_date = f"{start_of_week} 00:00:00"
        end_date = f"{today_str} 23:59:59"
    elif period == 'this_month':
        start_date = f"{month_str}-01 00:00:00"
        end_date = f"{today_str} 23:59:59"
    elif period == 'last_month':
        first_day_this_month = datetime.date.today().replace(day=1)
        last_day_prev_month = first_day_this_month - datetime.timedelta(days=1)
        first_day_prev_month = last_day_prev_month.replace(day=1)
        start_date = f"{first_day_prev_month.strftime('%Y-%m-%d')} 00:00:00"
        end_date = f"{last_day_prev_month.strftime('%Y-%m-%d')} 23:59:59"
    elif period == 'this_year':
        start_date = f"{year_str}-01-01 00:00:00"
        end_date = f"{today_str} 23:59:59"
    elif period == 'custom' and (custom_date_from or custom_date_to):
        if custom_date_from:
            start_date = f"{custom_date_from} 00:00:00"
        if custom_date_to:
            end_date = f"{custom_date_to} 23:59:59"

    # Build SQL Where Clauses
    where_clauses = ["1=1"]
    params = []

    if start_date:
        where_clauses.append("s.activated_at >= ?")
        params.append(start_date)
    if end_date:
        where_clauses.append("s.activated_at <= ?")
        params.append(end_date)
    if reseller_id and reseller_id != 'all':
        if reseller_id == 'direct':
            where_clauses.append("s.reseller_id IS NULL")
        else:
            where_clauses.append("s.reseller_id = ?")
            params.append(int(reseller_id))
    if package_name and package_name != 'all':
        where_clauses.append("s.package_name = ?")
        params.append(package_name)
    if search_q:
        where_clauses.append("(s.username LIKE ? OR s.serial_number LIKE ? OR s.batch_name LIKE ?)")
        wildcard = f"%{search_q}%"
        params.extend([wildcard, wildcard, wildcard])

    where_sql = " AND ".join(where_clauses)

    # 1. Period Metrics from filtered wisp_voucher_sales
    # ISP Logic:
    # - gross_sales = SUM(price) (سعر المشترك)
    # - net_isp_revenue = SUM(CASE WHEN reseller_id IS NOT NULL AND cost > 0 THEN cost ELSE price END) (حصة صاحب الشبكة)
    # - total_reseller_commissions = SUM(CASE WHEN reseller_id IS NOT NULL AND cost > 0 THEN (price - cost) ELSE 0 END) (عمولات الموزعين)
    metrics_query = f"""
        SELECT 
            COUNT(*) as total_count,
            COALESCE(SUM(price), 0) as gross_sales,
            COALESCE(SUM(CASE WHEN reseller_id IS NOT NULL AND cost > 0 THEN cost ELSE price END), 0) as net_isp_revenue,
            COALESCE(SUM(CASE WHEN reseller_id IS NOT NULL AND cost > 0 THEN (price - cost) ELSE 0 END), 0) as total_reseller_commissions
        FROM wisp_voucher_sales s
        WHERE {where_sql}
    """
    period_metrics = query_one(metrics_query, tuple(params)) or {}
    gross_sales = round(float(period_metrics.get('gross_sales') or 0), 2)
    net_isp_revenue = round(float(period_metrics.get('net_isp_revenue') or 0), 2)
    total_reseller_commissions = round(float(period_metrics.get('total_reseller_commissions') or 0), 2)
    total_items_count = int(period_metrics.get('total_count') or 0)
    
    isp_share_pct = round((net_isp_revenue / gross_sales * 100), 1) if gross_sales > 0 else 0
    reseller_share_pct = round((total_reseller_commissions / gross_sales * 100), 1) if gross_sales > 0 else 0

    # 2. Lifetime & Today's Net ISP Overview (Optimized Single Combined Query)
    today_start = f"{today_str} 00:00:00"
    today_end = f"{today_str} 23:59:59"
    month_start = f"{month_str}-01 00:00:00"
    month_end = f"{month_str}-31 23:59:59"

    overview_vouchers = query_one("""
        SELECT 
            COUNT(*) as total_sold_cards,
            COALESCE(SUM(CASE WHEN reseller_id IS NOT NULL AND cost > 0 THEN cost ELSE price END), 0) as lifetime_isp_sales,
            COALESCE(SUM(price), 0) as lifetime_gross_sales,
            
            COALESCE(SUM(CASE WHEN activated_at >= ? AND activated_at <= ? AND reseller_id IS NOT NULL AND cost > 0 THEN cost 
                              WHEN activated_at >= ? AND activated_at <= ? THEN price ELSE 0 END), 0) as month_isp_sales,
            
            COALESCE(SUM(CASE WHEN activated_at >= ? AND activated_at <= ? AND reseller_id IS NOT NULL AND cost > 0 THEN cost 
                              WHEN activated_at >= ? AND activated_at <= ? THEN price ELSE 0 END), 0) as today_isp_sales,
            COALESCE(SUM(CASE WHEN activated_at >= ? AND activated_at <= ? THEN price ELSE 0 END), 0) as today_gross_sales,
            COALESCE(SUM(CASE WHEN activated_at >= ? AND activated_at <= ? THEN 1 ELSE 0 END), 0) as today_cards_count
        FROM wisp_voucher_sales
    """, (month_start, month_end, month_start, month_end,
          today_start, today_end, today_start, today_end,
          today_start, today_end,
          today_start, today_end)) or {}

    inv_stats = query_one("""
        SELECT 
            COALESCE(SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END), 0) as subs_total,
            COALESCE(SUM(CASE WHEN status = 'paid' AND paid_at >= ? AND paid_at <= ? THEN amount ELSE 0 END), 0) as month_inv_sales,
            COALESCE(SUM(CASE WHEN status = 'paid' AND paid_at >= ? AND paid_at <= ? THEN amount ELSE 0 END), 0) as today_inv_sales
        FROM wisp_invoices
    """, (month_start, month_end, today_start, today_end)) or {}

    lifetime_isp_sales = round(float(overview_vouchers.get('lifetime_isp_sales') or 0), 2)
    lifetime_gross_sales = round(float(overview_vouchers.get('lifetime_gross_sales') or 0), 2)
    total_sold_cards = int(overview_vouchers.get('total_sold_cards') or 0)

    month_isp_sales = round(float(overview_vouchers.get('month_isp_sales') or 0) + float(inv_stats.get('month_inv_sales') or 0), 2)
    today_isp_sales = round(float(overview_vouchers.get('today_isp_sales') or 0) + float(inv_stats.get('today_inv_sales') or 0), 2)
    today_gross_sales = round(float(overview_vouchers.get('today_gross_sales') or 0) + float(inv_stats.get('today_inv_sales') or 0), 2)
    today_cards_count = int(overview_vouchers.get('today_cards_count') or 0)
    subs_total = round(float(inv_stats.get('subs_total') or 0), 2)

    # 3. Pagination calculation
    total_pages = max(1, math.ceil(total_items_count / per_page))
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page

    # 4. Filtered Sales Records
    list_query = f"""
        SELECT s.*, 
               COALESCE(NULLIF(m.full_name, ''), m.username, 'مبيعات مباشرة (الإدارة)') as reseller_name,
               m.username as reseller_username,
               CASE WHEN s.reseller_id IS NOT NULL AND s.cost > 0 THEN s.cost ELSE s.price END as isp_share,
               CASE WHEN s.reseller_id IS NOT NULL AND s.cost > 0 THEN (s.price - s.cost) ELSE 0 END as reseller_margin
        FROM wisp_voucher_sales s
        LEFT JOIN wisp_managers m ON s.reseller_id = m.id
        WHERE {where_sql}
        ORDER BY s.id DESC
        LIMIT {per_page} OFFSET {offset}
    """
    sales_list = query_all(list_query, tuple(params)) or []

    # 5. Package Breakdown (Optimized GROUP BY queries with zero CPU scan)
    voucher_counts = {r['package_name']: r['c'] for r in query_all("SELECT package_name, COUNT(*) as c FROM wisp_voucher_sales GROUP BY package_name") if r.get('package_name')}
    invoice_counts = {r['package_name']: r['c'] for r in query_all("SELECT package_name, COUNT(*) as c FROM wisp_invoices WHERE status = 'paid' GROUP BY package_name") if r.get('package_name')}

    pkgs = query_all("SELECT name, service_type, price, cost FROM wisp_packages ORDER BY name ASC") or []
    for p in pkgs:
        p_name = p.get('name') or ''
        count = voucher_counts.get(p_name, 0) + invoice_counts.get(p_name, 0)
        p['sold_count'] = count
        p_price = float(p.get('price') or 0)
        p_cost = float(p.get('cost') or 0)
        p['isp_unit_price'] = p_cost if p_cost > 0 else p_price
        p['reseller_unit_commission'] = max(0, p_price - p_cost) if p_cost > 0 else 0
        p['total_gross_revenue'] = round(count * p_price, 2)
        p['total_isp_revenue'] = round(count * p['isp_unit_price'], 2)
        p['total_reseller_profit'] = round(count * p['reseller_unit_commission'], 2)

    # 6. Filter Options (Resellers & Packages)
    resellers_list = query_all("""
        SELECT id, username, full_name,
               COALESCE(NULLIF(full_name, ''), username) as name
        FROM wisp_managers 
        WHERE is_active = 1 
        ORDER BY id ASC
    """) or []
    all_packages_list = query_all("SELECT DISTINCT name FROM wisp_packages ORDER BY name ASC") or []

    sales_chart = get_sales_chart_data()
    traffic_chart = get_traffic_chart_data()

    return render_template('sales_reports.html',
                           period=period,
                           custom_date_from=custom_date_from,
                           custom_date_to=custom_date_to,
                           selected_reseller=reseller_id,
                           selected_package=package_name,
                           search_q=search_q,
                           page=page,
                           total_pages=total_pages,
                           total_items_count=total_items_count,
                           per_page=per_page,
                           gross_sales=gross_sales,
                           net_isp_revenue=net_isp_revenue,
                           total_reseller_commissions=total_reseller_commissions,
                           isp_share_pct=isp_share_pct,
                           reseller_share_pct=reseller_share_pct,
                           today_isp_sales=today_isp_sales,
                           today_gross_sales=today_gross_sales,
                           today_cards_count=today_cards_count,
                           month_isp_sales=month_isp_sales,
                           lifetime_isp_sales=lifetime_isp_sales,
                           lifetime_gross_sales=lifetime_gross_sales,
                           subs_total=subs_total,
                           total_sold_cards=total_sold_cards,
                           package_breakdown=pkgs,
                           recent_sales=sales_list,
                           resellers_list=resellers_list,
                           all_packages_list=all_packages_list,
                           sales_chart=sales_chart,
                           traffic_chart=traffic_chart)


@app.route('/sales/export')
@app.route('/reports/sales/export')
def export_sales():
    import io
    import csv
    from flask import Response

    period = request.args.get('period', 'all').strip()
    custom_date_from = request.args.get('date_from', '').strip()
    custom_date_to = request.args.get('date_to', '').strip()
    reseller_id = request.args.get('reseller_id', '').strip()
    package_name = request.args.get('package_name', '').strip()
    search_q = request.args.get('q', '').strip()
    export_format = request.args.get('format', 'excel').lower().strip()

    today_str = datetime.date.today().strftime('%Y-%m-%d')
    yesterday_str = (datetime.date.today() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
    month_str = datetime.date.today().strftime('%Y-%m')
    year_str = datetime.date.today().strftime('%Y')

    start_date = None
    end_date = None

    if period == 'today':
        start_date = f"{today_str} 00:00:00"
        end_date = f"{today_str} 23:59:59"
    elif period == 'yesterday':
        start_date = f"{yesterday_str} 00:00:00"
        end_date = f"{yesterday_str} 23:59:59"
    elif period == 'this_week':
        start_of_week = (datetime.date.today() - datetime.timedelta(days=datetime.date.today().weekday())).strftime('%Y-%m-%d')
        start_date = f"{start_of_week} 00:00:00"
        end_date = f"{today_str} 23:59:59"
    elif period == 'this_month':
        start_date = f"{month_str}-01 00:00:00"
        end_date = f"{today_str} 23:59:59"
    elif period == 'last_month':
        first_day_this_month = datetime.date.today().replace(day=1)
        last_day_prev_month = first_day_this_month - datetime.timedelta(days=1)
        first_day_prev_month = last_day_prev_month.replace(day=1)
        start_date = f"{first_day_prev_month.strftime('%Y-%m-%d')} 00:00:00"
        end_date = f"{last_day_prev_month.strftime('%Y-%m-%d')} 23:59:59"
    elif period == 'this_year':
        start_date = f"{year_str}-01-01 00:00:00"
        end_date = f"{today_str} 23:59:59"
    elif period == 'custom' and (custom_date_from or custom_date_to):
        if custom_date_from:
            start_date = f"{custom_date_from} 00:00:00"
        if custom_date_to:
            end_date = f"{custom_date_to} 23:59:59"

    where_clauses = ["1=1"]
    params = []

    if start_date:
        where_clauses.append("s.activated_at >= ?")
        params.append(start_date)
    if end_date:
        where_clauses.append("s.activated_at <= ?")
        params.append(end_date)
    if reseller_id and reseller_id != 'all':
        if reseller_id == 'direct':
            where_clauses.append("s.reseller_id IS NULL")
        else:
            where_clauses.append("s.reseller_id = ?")
            params.append(int(reseller_id))
    if package_name and package_name != 'all':
        where_clauses.append("s.package_name = ?")
        params.append(package_name)
    if search_q:
        where_clauses.append("(s.username LIKE ? OR s.serial_number LIKE ? OR s.batch_name LIKE ?)")
        wildcard = f"%{search_q}%"
        params.extend([wildcard, wildcard, wildcard])

    where_sql = " AND ".join(where_clauses)

    records = query_all(f"""
        SELECT s.*, 
               COALESCE(NULLIF(m.full_name, ''), m.username, 'مبيعات مباشرة (الإدارة)') as reseller_name,
               m.username as reseller_username,
               CASE WHEN s.reseller_id IS NOT NULL AND s.cost > 0 THEN s.cost ELSE s.price END as isp_share,
               CASE WHEN s.reseller_id IS NOT NULL AND s.cost > 0 THEN (s.price - s.cost) ELSE 0 END as reseller_margin
        FROM wisp_voucher_sales s
        LEFT JOIN wisp_managers m ON s.reseller_id = m.id
        WHERE {where_sql}
        ORDER BY s.id DESC
        LIMIT 10000
    """, tuple(params)) or []

    file_timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')

    if export_format == 'csv':
        output = io.StringIO()
        output.write('\ufeff')  # UTF-8 BOM
        writer = csv.writer(output)
        writer.writerow(['م', 'اسم المستخدم / الكرت', 'الرقم التسلسلي', 'الحزمة / الدفعة', 'الباقة', 'سعر المشترك', 'حصة الشبكة', 'عمولة الموزع', 'نقطة البيع / الموزع', 'تاريخ ووقت التفعيل'])
        for idx, r in enumerate(records, 1):
            writer.writerow([
                idx,
                r.get('username') or '',
                r.get('serial_number') or '',
                r.get('batch_name') or '',
                r.get('package_name') or '',
                float(r.get('price') or 0),
                float(r.get('isp_share') or 0),
                float(r.get('reseller_margin') or 0),
                r.get('reseller_name') or 'مباشر (الإدارة)',
                str(r.get('activated_at') or '')
            ])
        return Response(output.getvalue(), mimetype='text/csv; charset=utf-8', headers={
            'Content-Disposition': f'attachment; filename=MAX_RADIUS_Sales_{file_timestamp}.csv'
        })

    # Excel export via openpyxl
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "كشف المبيعات والإيرادات"
        ws.views.sheetView[0].rightToLeft = True

        # Header Title
        ws.merge_cells('A1:J1')
        title_cell = ws['A1']
        title_cell.value = "📊 MAX RADIUS 2.0 - كشف مبيعات الكروت وحصة الشبكة وعمولات الموزعين"
        title_cell.font = Font(name='Arial', size=15, bold=True, color='FFFFFF')
        title_cell.fill = PatternFill(start_color='0F172A', end_color='0F172A', fill_type='solid')
        title_cell.alignment = Alignment(horizontal='center', vertical='center')
        ws.row_dimensions[1].height = 40

        # Sub-header with export info
        ws.merge_cells('A2:J2')
        sub_cell = ws['A2']
        sub_cell.value = f"تاريخ التصدير: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} | الفترة: {period} | إجمالي السجلات: {len(records)}"
        sub_cell.font = Font(name='Arial', size=10, italic=True, color='64748B')
        sub_cell.alignment = Alignment(horizontal='center', vertical='center')
        ws.row_dimensions[2].height = 24

        headers = ['#', 'اسم الكرت / المستخدم', 'الرقم التسلسلي', 'الحزمة / الدفعة', 'الباقة', 'سعر المشترك', 'حصة الشبكة', 'عمولة الموزع', 'نقطة البيع / الموزع', 'تاريخ ووقت التفعيل']
        ws.append([])  # Row 3 empty spacer
        ws.append(headers)  # Row 4
        ws.row_dimensions[4].height = 28

        header_fill = PatternFill(start_color='059669', end_color='059669', fill_type='solid')
        header_font = Font(name='Arial', size=11, bold=True, color='FFFFFF')
        thin_border = Border(
            left=Side(style='thin', color='CBD5E1'),
            right=Side(style='thin', color='CBD5E1'),
            top=Side(style='thin', color='CBD5E1'),
            bottom=Side(style='thin', color='CBD5E1')
        )

        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=4, column=col_idx)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal='center', vertical='center')
            cell.border = thin_border

        total_gross = 0
        total_isp = 0
        total_comm = 0

        for idx, r in enumerate(records, 1):
            p = float(r.get('price') or 0)
            isp_s = float(r.get('isp_share') or 0)
            res_m = float(r.get('reseller_margin') or 0)
            total_gross += p
            total_isp += isp_s
            total_comm += res_m

            row_data = [
                idx,
                r.get('username') or '',
                r.get('serial_number') or '',
                r.get('batch_name') or '',
                r.get('package_name') or '',
                p,
                isp_s,
                res_m,
                r.get('reseller_name') or 'مباشر (الإدارة)',
                str(r.get('activated_at') or '')
            ]
            ws.append(row_data)
            curr_row = ws.max_row
            ws.row_dimensions[curr_row].height = 22
            for col_idx in range(1, len(headers) + 1):
                cell = ws.cell(row=curr_row, column=col_idx)
                cell.border = thin_border
                cell.font = Font(name='Arial', size=10)
                if col_idx in [1, 6, 7, 8, 10]:
                    cell.alignment = Alignment(horizontal='center', vertical='center')
                else:
                    cell.alignment = Alignment(horizontal='right', vertical='center')

        # Totals Row
        tot_row_idx = ws.max_row + 1
        ws.row_dimensions[tot_row_idx].height = 28
        ws.merge_cells(f'A{tot_row_idx}:E{tot_row_idx}')
        tot_label = ws[f'A{tot_row_idx}']
        tot_label.value = "الإجمالي العام المحقق"
        tot_label.font = Font(name='Arial', size=11, bold=True, color='0F172A')
        tot_label.alignment = Alignment(horizontal='center', vertical='center')
        tot_label.fill = PatternFill(start_color='E2E8F0', end_color='E2E8F0', fill_type='solid')

        tot_gross_cell = ws.cell(row=tot_row_idx, column=6, value=total_gross)
        tot_isp_cell = ws.cell(row=tot_row_idx, column=7, value=total_isp)
        tot_comm_cell = ws.cell(row=tot_row_idx, column=8, value=total_comm)

        for cell in [tot_gross_cell, tot_isp_cell, tot_comm_cell]:
            cell.font = Font(name='Arial', size=11, bold=True, color='047857')
            cell.alignment = Alignment(horizontal='center', vertical='center')
            cell.fill = PatternFill(start_color='D1FAE5', end_color='D1FAE5', fill_type='solid')

        for col_idx in range(1, len(headers) + 1):
            ws.cell(row=tot_row_idx, column=col_idx).border = thin_border

        # Auto-fit columns
        for col in ws.columns:
            max_len = max(len(str(cell.value or '')) for cell in col)
            col_letter = get_column_letter(col[0].column)
            ws.column_dimensions[col_letter].width = max(max_len + 4, 14)

        out_stream = io.BytesIO()
        wb.save(out_stream)
        out_stream.seek(0)

        return Response(
            out_stream.getvalue(),
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={'Content-Disposition': f'attachment; filename=MAX_RADIUS_Sales_Report_{file_timestamp}.xlsx'}
        )
    except Exception as e:
        logger.error(f"[Export Sales] Excel error: {e}")
        return jsonify({'error': str(e)}), 500


# ==========================================
# Voucher Designs & Card Designer Subsystem
@app.route('/vouchers/designs', endpoint='voucher_designs_list')
@app.route('/vouchers/designer', endpoint='card_designer')
@login_required
def voucher_designs_list():
    """عرض صفحة قائمة تصاميم الكروت المحفوظة"""
    designs = get_all_card_designs()
    return render_template('designs_list.html', designs=designs)

@app.route('/vouchers/designs/new')
@login_required
def new_voucher_design():
    """عرض صفحة مصمم الكروت لإنشاء تصميم جديد"""
    isp_setting = query_one("SELECT `value` FROM wisp_system_settings WHERE `key` = 'isp_name'")
    curr_setting = query_one("SELECT `value` FROM wisp_system_settings WHERE `key` = 'currency_symbol'")
    domain_setting = query_one("SELECT `value` FROM wisp_system_settings WHERE `key` = 'hotspot_domain'")
    
    system_isp_name = isp_setting['value'] if isp_setting else 'شبكة MAX RADIUS'
    system_currency = curr_setting['value'] if curr_setting else 'ر.ي'
    system_domain = domain_setting['value'] if domain_setting else 'wifi.hotspot'

    return render_template(
        'design_editor.html',
        design=None,
        default_config=DEFAULT_CARD_CONFIG,
        is_new=True,
        system_isp_name=system_isp_name,
        system_currency=system_currency,
        system_domain=system_domain
    )

@app.route('/vouchers/designs/edit/<int:design_id>')
@login_required
def edit_voucher_design(design_id):
    """عرض صفحة مصمم الكروت لتعديل تصميم موجود"""
    design = get_card_design_by_id(design_id)
    if not design:
        flash('التصميم المطلوب غير موجود.', 'danger')
        return redirect(url_for('card_designer'))

    isp_setting = query_one("SELECT `value` FROM wisp_system_settings WHERE `key` = 'isp_name'")
    curr_setting = query_one("SELECT `value` FROM wisp_system_settings WHERE `key` = 'currency_symbol'")
    domain_setting = query_one("SELECT `value` FROM wisp_system_settings WHERE `key` = 'hotspot_domain'")
    
    system_isp_name = isp_setting['value'] if isp_setting else 'شبكة MAX RADIUS'
    system_currency = curr_setting['value'] if curr_setting else 'ر.ي'
    system_domain = domain_setting['value'] if domain_setting else 'wifi.hotspot'

    return render_template(
        'design_editor.html',
        design=design,
        default_config=DEFAULT_CARD_CONFIG,
        is_new=False,
        system_isp_name=system_isp_name,
        system_currency=system_currency,
        system_domain=system_domain
    )

@app.route('/api/vouchers/designs/save', methods=['POST'])
@app.route('/api/vouchers/designer/save', methods=['POST'])
@app.route('/vouchers/designer/save', methods=['POST'], endpoint='save_card_designer_action')
@app.route('/vouchers/designer/save', methods=['POST'])
@login_required
def save_card_design_api():
    """حفظ التصميم كـ AJAX أو Form Data"""
    try:
        req_data = request.get_json(silent=True) or request.form.to_dict()
        res = save_card_design(req_data)
        if res.get('success'):
            return jsonify(res)
        else:
            return jsonify(res), 400
    except Exception as e:
        return jsonify({'success': False, 'message': f'خطأ أثناء حفظ التصميم: {str(e)}'}), 500

@app.route('/api/vouchers/designs/upload-bg', methods=['POST'])
@app.route('/api/vouchers/designs/upload-image', methods=['POST'])
@login_required
def upload_card_bg_api():
    """رفع ومعالجة صورة خلفية الكارت عبر Pillow مع القص والتحجيم التلقائي حسب الأبعاد"""
    try:
        if 'image' not in request.files and 'background_image' not in request.files and 'file' not in request.files:
            return jsonify({'success': False, 'message': 'لم يتم اختيار ملف صورة.'}), 400
            
        file = request.files.get('image') or request.files.get('background_image') or request.files.get('file')
        if not file or file.filename == '':
            return jsonify({'success': False, 'message': 'اسم الملف غير صالح.'}), 400

        width_mm = float(request.form.get('width_mm', 85))
        height_mm = float(request.form.get('height_mm', 55))

        res = process_and_save_card_background(file, target_width_mm=width_mm, target_height_mm=height_mm)
        if isinstance(res, dict) and res.get('success'):
            if 'url' not in res and 'file_url' in res:
                res['url'] = res['file_url']
            return jsonify(res)
        elif isinstance(res, tuple):
            ok, data_or_msg = res
            if ok:
                if isinstance(data_or_msg, dict) and 'url' not in data_or_msg and 'file_url' in data_or_msg:
                    data_or_msg['url'] = data_or_msg['file_url']
                return jsonify(data_or_msg if isinstance(data_or_msg, dict) else {'success': True, 'data': data_or_msg})
            return jsonify({'success': False, 'message': str(data_or_msg)}), 400
        else:
            return jsonify(res if isinstance(res, dict) else {'success': False, 'message': 'فشل حفظ ومعالجة الصورة.'}), 400
    except Exception as e:
        return jsonify({'success': False, 'message': f'فشل معالجة الصورة: {str(e)}'}), 500

@app.route('/api/vouchers/designs/delete/<int:design_id>', methods=['POST'])
@app.route('/vouchers/designs/delete/<int:design_id>', methods=['POST'])
@login_required
def delete_card_design_api(design_id):
    """حذف تصميم كارت"""
    res = delete_card_design(design_id)
    if isinstance(res, tuple):
        ok, msg = res
        res = {'success': ok, 'message': msg}
    if request.is_json or request.path.startswith('/api/'):
        return jsonify(res), (200 if res.get('success') else 400)
    if res.get('success'):
        flash(res.get('message', 'تم حذف التصميم بنجاح.'), 'success')
    else:
        flash(res.get('message', 'تعذر حذف التصميم.'), 'danger')
    return redirect(url_for('voucher_designs_list'))

@app.route('/api/vouchers/designs/set-default/<int:design_id>', methods=['POST'])
@app.route('/vouchers/designs/set-default/<int:design_id>', methods=['POST'])
@login_required
def set_default_card_design_api(design_id):
    """تعيين التصميم كافتراضي"""
    res = set_default_card_design(design_id)
    if isinstance(res, tuple):
        ok, msg = res
        res = {'success': ok, 'message': msg}
    if request.is_json or request.path.startswith('/api/'):
        return jsonify(res), (200 if res.get('success') else 400)
    if res.get('success'):
        flash(res.get('message', 'تم تعيين القالب كافتراضي للطباعة.'), 'success')
    else:
        flash(res.get('message', 'تعذر تعيين القالب الافتراضي.'), 'danger')
    return redirect(url_for('voucher_designs_list'))

@app.route('/api/vouchers/designs/duplicate/<int:design_id>', methods=['POST'])
@app.route('/vouchers/designs/duplicate/<int:design_id>', methods=['POST'])
@login_required
def duplicate_card_design_api(design_id):
    """استنساخ تصميم كارت"""
    res = duplicate_card_design(design_id)
    if isinstance(res, tuple):
        new_id, msg = res
        res = {'success': bool(new_id), 'design_id': new_id, 'message': msg}
    if request.is_json or request.path.startswith('/api/'):
        return jsonify(res), (200 if res.get('success') else 400)
    if res.get('success'):
        flash(res.get('message', 'تم استنساخ التصميم بنجاح.'), 'success')
    else:
        flash(res.get('message', 'تعذر استنساخ التصميم.'), 'danger')
    return redirect(url_for('voucher_designs_list'))

@app.route('/vouchers/print/<int:batch_id>')
def print_cards(batch_id):
    batch, cards, template = get_batch_cards_for_print(batch_id)
    if not batch:
        flash('الدفعة المطلوبة غير موجودة.', 'danger')
        return redirect(url_for('vouchers'))
        
    # Load advanced JSON configuration from wisp_system_settings
    setting_row = query_one("SELECT `value` FROM wisp_system_settings WHERE `key` = 'voucher_custom_design_config'")
    saved_config_json = setting_row['value'] if setting_row and setting_row.get('value') else None

    return render_template('print_cards.html', batch=batch, cards=cards, tpl=template, saved_config_json=saved_config_json)

@app.route('/vouchers/delete-batch/<int:batch_id>', methods=['POST'])
def delete_batch_action(batch_id):
    try:
        delete_batch(batch_id)
        flash('تم حذف دفعة الكروت ومسحها من RADIUS.', 'info')
    except Exception as e:
        flash(f'خطأ أثناء حذف الدفعة: {str(e)}', 'danger')
    return redirect(url_for('vouchers'))

# ----------------- Profiles & Packages -----------------
@app.route('/packages')
def packages():
    sync_voucher_activations()
    search = request.args.get('q', '').strip()
    service_type = request.args.get('service_type', '').strip()
    
    query = '''
        SELECT p.*,
               (SELECT COUNT(*) FROM wisp_subscribers WHERE package_id = p.id) as subs_count,
               (SELECT COUNT(*) FROM wisp_vouchers WHERE package_id = p.id) as vouchers_count
        FROM wisp_packages p
        WHERE 1=1
    '''
    params = []
    if search:
        query += ' AND (p.name LIKE ? OR p.description LIKE ?)'
        params.extend([f'%{search}%', f'%{search}%'])
    if service_type:
        if service_type == 'both':
            query += ' AND p.service_type = "both"'
        else:
            query += ' AND (p.service_type = ? OR p.service_type = "both")'
            params.append(service_type)
        
    query += ' ORDER BY p.id ASC'
    pkgs = query_all(query, tuple(params))
    
    all_pkgs = query_all('SELECT id, service_type, is_active FROM wisp_packages')
    total_packages_count = len(all_pkgs)
    hotspot_packages_count = sum(1 for p in all_pkgs if p.get('service_type') in ('hotspot', 'both'))
    pppoe_packages_count = sum(1 for p in all_pkgs if p.get('service_type') in ('pppoe', 'both'))
    
    subs_row = query_one('SELECT COUNT(*) as cnt FROM wisp_subscribers')
    total_subs_count = subs_row['cnt'] if subs_row else 0
    
    vouchers_row = query_one('SELECT COUNT(*) as cnt FROM wisp_vouchers')
    total_vouchers_count = vouchers_row['cnt'] if vouchers_row else 0

    return render_template('packages.html',
                           packages=pkgs,
                           q=search,
                           service_type=service_type,
                           total_packages_count=total_packages_count,
                           hotspot_packages_count=hotspot_packages_count,
                           pppoe_packages_count=pppoe_packages_count,
                           total_subs_count=total_subs_count,
                           total_vouchers_count=total_vouchers_count)

@app.route('/packages/new', methods=['GET'])
@app.route('/packages/add', methods=['GET'])
def new_package_page():
    return render_template('package_add.html')

@app.route('/packages/add', methods=['POST'])
@app.route('/packages/new', methods=['POST'])
def add_package_action():
    try:
        f = request.form
        val = int(f.get('validity_value') or f.get('validity_days') or 0)
        unit = f.get('validity_unit', 'days').strip()
        equiv_days = 0 if val <= 0 else (val * 30 if unit == 'months' else (max(1, round(val / 24)) if unit == 'hours' else (max(1, round(val / 1440)) if unit == 'minutes' else val)))
        mikrotik_group = f.get('mikrotik_group', '').strip()

        pkg_id = execute_write('''
            INSERT INTO wisp_packages (
                name, service_type, price, cost, rate_download, rate_upload,
                burst_download, burst_upload, burst_threshold_down, burst_threshold_up,
                burst_time, priority, min_download, min_upload, volume_quota_mb,
                uptime_limit_mins, validity_days, validity_value, validity_unit,
                mikrotik_group, simultaneous_sessions, is_active, show_in_portal, is_rollover_enabled, description
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            f['name'].strip(), f.get('service_type', 'hotspot'), float(f.get('price', 0)), float(f.get('cost', 0)),
            f.get('rate_download', '2M').strip(), f.get('rate_upload', '1M').strip(),
            f.get('burst_download', '').strip(), f.get('burst_upload', '').strip(),
            f.get('burst_threshold_down', '').strip(), f.get('burst_threshold_up', '').strip(),
            int(f.get('burst_time', 16)), int(f.get('priority', 8)),
            f.get('min_download', '').strip(), f.get('min_upload', '').strip(),
            int(f.get('volume_quota_mb', 0)),
            equiv_days, val, unit,
            mikrotik_group,
            int(f.get('simultaneous_sessions', 1)),
            1 if f.get('is_active') in ('1', 'on', 'true', True, 1) else 0,
            1 if f.get('show_in_portal') in ('1', 'on', 'true', True, 1) else 0,
            1 if f.get('is_rollover_enabled') in ('1', 'on', 'true', True, 1) else 0,
            f.get('description', '')
        ))
        sync_package_to_radius(pkg_id)
        flash('تم حفظ وإنشاء الباقة بنجاح ومزامنة سمات MikroTik و FreeRADIUS.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء إضافة الباقة: {str(e)}', 'danger')
    return redirect(url_for('packages'))


@app.route('/packages/<int:pkg_id>')
def package_details(pkg_id):
    pkg = query_one('SELECT * FROM wisp_packages WHERE id = ?', (pkg_id,))
    if not pkg:
        flash('الباقة المطلوبة غير موجودة.', 'danger')
        return redirect(url_for('packages'))
    
    subs_count = query_one('SELECT COUNT(*) as c FROM wisp_subscribers WHERE package_id = ?', (pkg_id,))['c']
    vouchers_count = query_one('SELECT COUNT(*) as c FROM wisp_vouchers WHERE package_id = ?', (pkg_id,))['c']
    
    preview_rate = build_mikrotik_rate_limit(
        download=pkg['rate_download'],
        upload=pkg['rate_upload'],
        burst_down=pkg.get('burst_download'),
        burst_up=pkg.get('burst_upload'),
        threshold_down=pkg.get('burst_threshold_down'),
        threshold_up=pkg.get('burst_threshold_up'),
        burst_time=pkg.get('burst_time', 16),
        priority=pkg.get('priority', 8),
        min_down=pkg.get('min_download'),
        min_up=pkg.get('min_upload')
    )
    
    radius_replies = query_all('SELECT attribute, op, value FROM radgroupreply WHERE groupname = ?', (pkg['name'],))
    radius_checks = query_all('SELECT attribute, op, value FROM radgroupcheck WHERE groupname = ?', (pkg['name'],))
    
    return render_template('package_details.html',
                           package=pkg,
                           subs_count=subs_count,
                           vouchers_count=vouchers_count,
                           preview_rate=preview_rate or 'سرعة مفتوحة (غير محدود - Unlimited)',
                           radius_replies=radius_replies,
                           radius_checks=radius_checks)

@app.route('/packages/edit/<int:pkg_id>', methods=['POST'])
def edit_package_action(pkg_id):
    try:
        old_pkg = query_one('SELECT * FROM wisp_packages WHERE id = ?', (pkg_id,))
        if not old_pkg:
            flash('الباقة غير موجودة.', 'danger')
            return redirect(url_for('packages'))
        
        f = request.form
        new_name = f['name'].strip()
        old_name = old_pkg['name']
        val = int(f.get('validity_value') or f.get('validity_days') or 0)
        unit = f.get('validity_unit', 'days').strip()
        equiv_days = 0 if val <= 0 else (val * 30 if unit == 'months' else (max(1, round(val / 24)) if unit == 'hours' else (max(1, round(val / 1440)) if unit == 'minutes' else val)))
        mikrotik_group = f.get('mikrotik_group', '').strip()
        
        execute_write('''
            UPDATE wisp_packages SET
                name = ?, service_type = ?, price = ?, cost = ?,
                rate_download = ?, rate_upload = ?, burst_download = ?, burst_upload = ?,
                burst_threshold_down = ?, burst_threshold_up = ?, burst_time = ?,
                priority = ?, min_download = ?, min_upload = ?, volume_quota_mb = ?,
                uptime_limit_mins = 0, validity_days = ?, validity_value = ?, validity_unit = ?,
                mikrotik_group = ?, simultaneous_sessions = ?, is_active = ?, show_in_portal = ?, is_rollover_enabled = ?, description = ?
            WHERE id = ?
        ''', (
            new_name, f.get('service_type', 'hotspot'), float(f.get('price', 0)), float(f.get('cost', 0)),
            f.get('rate_download', '2M').strip(), f.get('rate_upload', '1M').strip(),
            f.get('burst_download', '').strip(), f.get('burst_upload', '').strip(),
            f.get('burst_threshold_down', '').strip(), f.get('burst_threshold_up', '').strip(),
            int(f.get('burst_time', 16)), int(f.get('priority', 8)),
            f.get('min_download', '').strip(), f.get('min_upload', '').strip(),
            int(f.get('volume_quota_mb', 0)),
            equiv_days, val, unit,
            mikrotik_group,
            int(f.get('simultaneous_sessions', 1)),
            1 if f.get('is_active') in ('1', 'on', 'true', True, 1) else 0,
            1 if f.get('show_in_portal') in ('1', 'on', 'true', True, 1) else 0,
            1 if f.get('is_rollover_enabled') in ('1', 'on', 'true', True, 1) else 0,
            f.get('description', ''), pkg_id
        ))
        
        if new_name != old_name:
            execute_write('UPDATE radusergroup SET groupname = ? WHERE groupname = ?', (new_name, old_name))
            execute_write('DELETE FROM radgroupreply WHERE groupname = ?', (old_name,))
            execute_write('DELETE FROM radgroupcheck WHERE groupname = ?', (old_name,))
            
        sync_package_to_radius(pkg_id)
        flash('تم حفظ تعديلات الباقة ومزامنة سمات FreeRADIUS وميكروتيك بنجاح.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء تعديل الباقة: {str(e)}', 'danger')
        
    return redirect(url_for('package_details', pkg_id=pkg_id))

@app.route('/packages/delete/<int:pkg_id>', methods=['POST'])
def delete_package_action(pkg_id):
    try:
        pkg = query_one('SELECT name FROM wisp_packages WHERE id = ?', (pkg_id,))
        if pkg:
            execute_write('DELETE FROM radgroupreply WHERE groupname = ?', (pkg['name'],))
            execute_write('DELETE FROM radgroupcheck WHERE groupname = ?', (pkg['name'],))
            execute_write('DELETE FROM wisp_packages WHERE id = ?', (pkg_id,))
            flash('تم حذف الباقة.', 'info')
    except Exception as e:
        flash(f'خطأ أثناء الحذف: {str(e)}', 'danger')
    return redirect(url_for('packages'))

# ----------------- 4. NAS / MikroTik Routers -----------------
@app.route('/nas')
@login_required
def nas():
    require_permission('nas_view')
    # Fetch devices with live status probe on page load as originally configured
    devices = get_nas_devices(skip_live_probe=False)
    return render_template('nas.html', devices=devices)

@app.route('/nas/add', methods=['GET', 'POST'])
@app.route('/nas/new', methods=['GET', 'POST'])
def add_nas_action():
    if request.method == 'GET':
        return render_template('nas_add.html')
    try:
        add_nas_device(request.form)
        flash('تمت إضافة راوتر MikroTik بنجاح وتوثيقه في قائمة RADIUS NAS.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء إضافة الراوتر: {str(e)}', 'danger')
    return redirect(url_for('nas'))

@app.route('/nas/<int:nas_id>')
def nas_details_page(nas_id):
    device = query_one('SELECT * FROM wisp_nas_devices WHERE id = ?', (nas_id,))
    if not device:
        flash('جهاز الراوتر المطلوب غير موجود.', 'danger')
        return redirect(url_for('nas'))
        
    from core.mikrotik_api import fetch_single_nas_status
    status_info = fetch_single_nas_status(device)
    
    # Active sessions on this NAS router from radacct
    active_sessions = query_all('''
        SELECT * FROM radacct
        WHERE nasipaddress = ? AND acctstoptime IS NULL
        ORDER BY radacctid DESC LIMIT 50
    ''', (device['ip_address'],))
    
    for s in active_sessions:
        s['download_str'] = format_bytes(s.get('acctoutputoctets', 0))
        s['upload_str'] = format_bytes(s.get('acctinputoctets', 0))
        s['duration_str'] = format_duration(s.get('acctsessiontime', 0))
        
    # Total traffic handled by this NAS router
    traffic = query_one('''
        SELECT COALESCE(SUM(total_in), 0) as up, COALESCE(SUM(total_out), 0) as down
        FROM (
            SELECT acctsessionid,
                   MAX(acctinputoctets) as total_in,
                   MAX(acctoutputoctets) as total_out
            FROM radacct
            WHERE nasipaddress = ?
            GROUP BY acctsessionid
        ) AS t
    ''', (device['ip_address'],))

    
    total_down_str = format_bytes(traffic['down'] if traffic else 0)
    total_up_str = format_bytes(traffic['up'] if traffic else 0)
    
    return render_template('nas_details.html',
                           device=device,
                           live_status=status_info.get('status', 'offline'),
                           latency_ms=status_info.get('latency_ms', 0),
                           status_info=status_info,
                           active_sessions=active_sessions,
                           total_download_str=total_down_str,
                           total_upload_str=total_up_str)

@app.route('/nas/edit/<int:nas_id>', methods=['POST'])
def edit_nas_action(nas_id):
    try:
        update_nas_device(nas_id, request.form)
        flash('تم حفظ وتحديث إعدادات الراوتر ومزامنتها في FreeRADIUS بنجاح.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء حفظ تعديلات الراوتر: {str(e)}', 'danger')
    return redirect(url_for('nas_details_page', nas_id=nas_id))

@app.route('/nas/delete/<int:nas_id>', methods=['POST'])
def delete_nas_action(nas_id):
    try:
        delete_nas_device(nas_id)
        flash('تم حذف الراوتر ومسحه من FreeRADIUS بنجاح.', 'warning')
    except Exception as e:
        flash(f'خطأ: {str(e)}', 'danger')
    return redirect(url_for('nas'))

@app.route('/api/nas/test-coa/<int:nas_id>', methods=['POST'])
@app.route('/api/nas/<int:nas_id>/test-coa', methods=['POST'])
def api_test_coa(nas_id):
    res = test_nas_coa(nas_id)
    return jsonify(res)

@app.route('/api/nas-status')
@app.route('/api/nas/live-status')
def api_nas_live_status():
    from core.mikrotik_api import get_all_nas_live_status
    force = request.args.get('force') == '1' or request.args.get('refresh') == 'true'
    devices_status = get_all_nas_live_status(force_refresh=force)
    return jsonify({
        'success': True,
        'timestamp': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'total_routers': len(devices_status),
        'online_routers': sum(1 for d in devices_status if d.get('is_online')),
        'devices': devices_status
    })

@app.route('/api/nas/<int:nas_id>/status')
def api_single_nas_status(nas_id):
    from core.mikrotik_api import fetch_single_nas_status
    device = query_one('SELECT * FROM wisp_nas_devices WHERE id = ?', (nas_id,))
    if not device:
        return jsonify({'success': False, 'message': 'جهاز الراوتر غير موجود.'}), 404
    status_data = fetch_single_nas_status(device)
    return jsonify({'success': True, 'data': status_data})

# ==============================================================================
#  L2TP / IPsec Remote NAS Routers (VPN Tunnels) Management
# ==============================================================================
@app.route('/nas/l2tp')
@login_required
def l2tp_tunnels_page():
    import ipaddress
    tunnels = get_l2tp_tunnels(fast_db_only=False)
    vps_ip = detect_vps_public_ip()
    settings = get_l2tp_network_settings()

    gw_ip = settings.get('l2tp_gateway_ip', '10.10.0.1')
    mask = settings.get('l2tp_mask', '255.255.255.0')
    pool_start = settings.get('l2tp_pool_start', '10.10.0.10')
    pool_end = settings.get('l2tp_pool_end', '10.10.0.250')
    port = settings.get('l2tp_server_port', 1701)
    ipsec_secret = settings.get('l2tp_ipsec_secret', '')

    used_ips = {t.get('tunnel_ip') for t in tunnels if t.get('tunnel_ip')}
    used_ips.add(gw_ip)

    next_ip = ''
    try:
        start_int = int(ipaddress.IPv4Address(pool_start))
        end_int = int(ipaddress.IPv4Address(pool_end))
        for ip_int in range(start_int, end_int + 1):
            cand = str(ipaddress.IPv4Address(ip_int))
            if cand not in used_ips:
                next_ip = cand
                break
    except Exception:
        pass
    if not next_ip:
        prefix = '.'.join(pool_start.split('.')[:3])
        next_ip = f"{prefix}.20"

    return render_template(
        'l2tp_tunnels.html',
        tunnels=tunnels,
        vps_detected_ip=vps_ip,
        l2tp_settings=settings,
        l2tp_gateway_ip=gw_ip,
        l2tp_mask=mask,
        l2tp_pool_start=pool_start,
        l2tp_pool_end=pool_end,
        l2tp_port=port,
        l2tp_ipsec_secret=ipsec_secret,
        next_suggested_ip=next_ip
    )

@app.route('/nas/l2tp/settings', methods=['POST'])
@login_required
def l2tp_save_settings_action():
    try:
        current_mgr = get_current_manager()
        admin_user = current_mgr['username'] if current_mgr else 'admin'
        save_l2tp_network_settings(request.form, admin_username=admin_user)
        flash('تم حفظ وتطبيق إعدادات شبكة L2TP/IPsec VPN وإعادة تشغيل خادم النفق بنجاح.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء حفظ إعدادات شبكة VPN: {str(e)}', 'danger')
    return redirect(url_for('l2tp_tunnels_page'))

@app.route('/api/nas/l2tp/settings', methods=['GET', 'POST'])
@login_required
def api_l2tp_settings():
    if request.method == 'POST':
        try:
            current_mgr = get_current_manager()
            admin_user = current_mgr['username'] if current_mgr else 'admin'
            data = request.get_json() if request.is_json else request.form
            save_l2tp_network_settings(data, admin_username=admin_user)
            return jsonify({'success': True, 'message': 'تم حفظ وتطبيق الإعدادات وإعادة تشغيل خادم النفق بنجاح'})
        except Exception as e:
            return jsonify({'success': False, 'message': str(e)}), 400
    else:
        settings = get_l2tp_network_settings()
        return jsonify({'success': True, 'settings': settings})

@app.route('/nas/l2tp/add', methods=['POST'])
@login_required
def l2tp_add_tunnel_action():
    try:
        current_mgr = get_current_manager()
        admin_user = current_mgr['username'] if current_mgr else 'admin'
        add_l2tp_tunnel(request.form, admin_username=admin_user)
        flash('تمت إضافة نفق راوتر L2TP/IPsec واعتماده في FreeRADIUS وسيرفر النفق بنجاح.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء إضافة راوتر النفق: {str(e)}', 'danger')
    return redirect(url_for('l2tp_tunnels_page'))

@app.route('/nas/l2tp/edit/<int:tunnel_id>', methods=['POST'])
@login_required
def l2tp_edit_tunnel_action(tunnel_id):
    try:
        current_mgr = get_current_manager()
        admin_user = current_mgr['username'] if current_mgr else 'admin'
        update_l2tp_tunnel(tunnel_id, request.form, admin_username=admin_user)
        flash('تم حفظ تعديلات نفق الراوتر بنجاح.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء تعديل النفق: {str(e)}', 'danger')
    return redirect(url_for('l2tp_tunnels_page'))

@app.route('/nas/l2tp/delete/<int:tunnel_id>', methods=['POST'])
@login_required
def l2tp_delete_tunnel_action(tunnel_id):
    try:
        current_mgr = get_current_manager()
        admin_user = current_mgr['username'] if current_mgr else 'admin'
        delete_l2tp_tunnel(tunnel_id, admin_username=admin_user)
        flash('تم حذف نفق الراوتر ومسحه من الراديوس بنجاح.', 'warning')
    except Exception as e:
        flash(f'خطأ أثناء حذف النفق: {str(e)}', 'danger')
    return redirect(url_for('l2tp_tunnels_page'))

@app.route('/api/nas/l2tp/live-status')
@login_required
def api_l2tp_live_status():
    tunnels = get_l2tp_tunnels(fast_db_only=False)
    online_count = len([t for t in tunnels if t.get('is_online')])
    return jsonify({
        'success': True,
        'tunnels': tunnels,
        'total_count': len(tunnels),
        'online_count': online_count,
        'offline_count': len(tunnels) - online_count
    })

@app.route('/api/nas/l2tp/<int:tunnel_id>/script')
@login_required
def api_l2tp_mikrotik_script(tunnel_id):
    vps_host = request.args.get('host')
    script = generate_mikrotik_rsc_script(tunnel_id, vps_host=vps_host)
    if not script:
        return jsonify({'success': False, 'message': 'النفق غير موجود'}), 404
    return jsonify({'success': True, 'script': script})

@app.route('/nas/l2tp/<int:tunnel_id>/download-script')
@login_required
def download_l2tp_mikrotik_script(tunnel_id):
    from flask import Response
    vps_host = request.args.get('host')
    script = generate_mikrotik_rsc_script(tunnel_id, vps_host=vps_host)
    if not script:
        flash('النفق غير موجود.', 'danger')
        return redirect(url_for('l2tp_tunnels_page'))
    return Response(
        script,
        mimetype="text/plain",
        headers={"Content-disposition": f"attachment; filename=mikrotik_l2tp_{tunnel_id}.rsc"}
    )



# ----------------- 5. Managers & Resellers & RBAC -----------------
@app.route('/managers')
@require_permission('managers.view')
def managers_page():
    mgrs = get_all_managers()
    roles = get_all_roles()
    kpis = get_manager_kpis()
    packages = query_all("SELECT id, name, price, service_type FROM wisp_packages ORDER BY id ASC")
    return render_template('managers.html', managers=mgrs, roles=roles, kpis=kpis, packages=packages)

@app.route('/managers/add', methods=['POST'])
@require_permission('managers.manage')
def add_manager_action():
    try:
        data = request.form.to_dict()
        data['allowed_packages'] = request.form.getlist('allowed_packages')
        mgr_id = create_manager(data, admin_user=session.get('user', 'admin'))
        flash('تمت إضافة حساب المدير / الموزع بنجاح وتعيين الصلاحيات.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء إضافة الحساب: {str(e)}', 'danger')
    return redirect(url_for('managers_page'))

@app.route('/managers/edit/<int:manager_id>', methods=['POST'])
@require_permission('managers.manage')
def edit_manager_action(manager_id):
    try:
        data = request.form.to_dict()
        data['allowed_packages'] = request.form.getlist('allowed_packages')
        update_manager(manager_id, data, admin_user=session.get('user', 'admin'))
        flash('تم حفظ وتحديث بيانات الحساب والصلاحيات بنجاح.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء تعديل الحساب: {str(e)}', 'danger')
    return redirect(url_for('managers_page'))

@app.route('/managers/toggle-status/<int:manager_id>', methods=['POST'])
@require_permission('managers.manage')
def toggle_manager_status_action(manager_id):
    try:
        new_status = toggle_manager_status(manager_id, admin_user=session.get('user', 'admin'))
        msg = 'تم تفعيل الحساب بنجاح.' if new_status == 1 else 'تم تعطيل الحساب بنجاح.'
        flash(msg, 'info')
    except Exception as e:
        flash(f'خطأ: {str(e)}', 'danger')
    return redirect(url_for('managers_page'))

@app.route('/managers/delete/<int:manager_id>', methods=['POST'])
@require_permission('managers.manage')
def delete_manager_action(manager_id):
    try:
        delete_manager(manager_id, admin_user=session.get('user', 'admin'))
        flash('تم حذف وتعطيل الحساب بنجاح مع الاحتفاظ بكافة القيود المحاسبية التاريخية.', 'warning')
    except Exception as e:
        flash(f'تعذر حذف الحساب: {str(e)}', 'danger')
    return redirect(url_for('managers_page'))

@app.route('/managers/<int:manager_id>/deposit', methods=['POST'])
@require_permission('managers.billing')
def deposit_manager_action(manager_id):
    try:
        amt = request.form['amount']
        pay_type = request.form.get('payment_type', 'cash')
        notes = request.form.get('notes', '')
        bal_after, inv_num = deposit_manager_wallet(manager_id, amt, payment_type=pay_type, notes=notes, admin_user=session.get('user', 'admin'))
        flash(f'تم إيداع الرصيد بنجاح ({amt}) وإصدار الفاتورة/القيد {inv_num}.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء الإيداع: {str(e)}', 'danger')
    return redirect(request.referrer or url_for('managers_page'))

@app.route('/managers/<int:manager_id>/deduct', methods=['POST'])
@require_permission('managers.billing')
def deduct_manager_action(manager_id):
    try:
        amt = request.form['amount']
        notes = request.form.get('notes', '')
        bal_after, inv_num = deduct_manager_wallet(manager_id, amt, notes=notes, admin_user=session.get('user', 'admin'))
        flash(f'تم خصم الرصيد بنجاح ({amt}) وتسجيل القيد {inv_num}.', 'info')
    except Exception as e:
        flash(f'خطأ أثناء الخصم: {str(e)}', 'danger')
    return redirect(request.referrer or url_for('managers_page'))

@app.route('/managers/<int:manager_id>/settle-debt', methods=['POST'])
@require_permission('managers.billing')
def settle_manager_debt_action(manager_id):
    try:
        amt = request.form['amount']
        pay_type = request.form.get('payment_type', 'cash')
        notes = request.form.get('notes', '')
        rem_debt, inv_num = settle_manager_debt(manager_id, amt, payment_type=pay_type, notes=notes, admin_user=session.get('user', 'admin'))
        flash(f'تم تسجيل سند القبض وسداد الذمة بنجاح بقيمة ({amt}) برقم القيد {inv_num}. الذمة المتبقية: {rem_debt:,.2f}', 'success')
    except Exception as e:
        flash(f'خطأ أثناء سداد الذمة: {str(e)}', 'danger')
    return redirect(request.referrer or url_for('managers_page'))

@app.route('/managers/invoices/<int:invoice_id>/void', methods=['POST'])
@require_permission('managers.billing')
def void_manager_invoice_action(invoice_id):
    try:
        reason = request.form.get('void_reason', '').strip()
        void_manager_invoice(invoice_id, reason, admin_user=session.get('user', 'admin'))
        flash('تم إلغاء الفاتورة/القيد بنجاح وعكس الأثر المالي في المحفظة.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء إلغاء الفاتورة: {str(e)}', 'danger')
    return redirect(request.referrer or url_for('manager_invoices_page'))

@app.route('/managers/<int:manager_id>/statement')
@require_permission('managers.view')
def manager_statement_page(manager_id):
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')
    pay_type = request.args.get('payment_type')
    tx_type = request.args.get('transaction_type')
    res = get_manager_statement(manager_id, date_from=date_from, date_to=date_to, payment_type=pay_type, transaction_type=tx_type)
    if not res or not res[0]:
        flash('الحساب المطلوب غير موجود.', 'danger')
        return redirect(url_for('managers_page'))
    mgr, invoices, stats = res
    return render_template('manager_statement.html', manager=mgr, invoices=invoices, stats=stats)

@app.route('/managers/invoices')
@require_permission('managers.billing')
def manager_invoices_page():
    mgr_id = request.args.get('manager_id')
    tx_type = request.args.get('type')
    pay_type = request.args.get('payment_type')
    invoices = get_all_manager_invoices(limit=150, manager_id=mgr_id, transaction_type=tx_type, payment_type=pay_type)
    managers = get_all_managers()
    kpis = get_manager_kpis()
    return render_template('manager_invoices.html', invoices=invoices, managers=managers, kpis=kpis)

# ----------------- 6. Roles & RBAC Matrix -----------------
@app.route('/roles')
@require_permission('roles.manage')
def roles_page():
    roles = get_all_roles()
    grouped_perms = get_all_permissions_grouped()
    return render_template('roles.html', roles=roles, grouped_permissions=grouped_perms)

@app.route('/roles/add', methods=['POST'])
@require_permission('roles.manage')
def add_role_action():
    try:
        perm_ids = request.form.getlist('permissions')
        create_role(request.form, permission_ids=perm_ids)
        flash('تم إنشاء الدور وتعيين الصلاحيات المحددة بنجاح.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء إنشاء الدور: {str(e)}', 'danger')
    return redirect(url_for('roles_page'))

@app.route('/roles/edit/<int:role_id>', methods=['POST'])
@require_permission('roles.manage')
def edit_role_action(role_id):
    try:
        perm_ids = request.form.getlist('permissions')
        update_role(role_id, request.form, permission_ids=perm_ids)
        flash('تم حفظ وتحديث صلاحيات الدور بنجاح.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء تعديل الدور: {str(e)}', 'danger')
    return redirect(url_for('roles_page'))

@app.route('/roles/delete/<int:role_id>', methods=['POST'])
@require_permission('roles.manage')
def delete_role_action(role_id):
    try:
        delete_role(role_id)
        flash('تم حذف الدور بنجاح.', 'warning')
    except Exception as e:
        flash(f'خطأ: {str(e)}', 'danger')
    return redirect(url_for('roles_page'))

@app.route('/api/roles/<int:role_id>/permissions')
def api_role_permissions(role_id):
    pids = get_role_permission_ids(role_id)
    return jsonify({'success': True, 'permission_ids': pids})

# ----------------- Legacy POS & Resellers Route -----------------
@app.route('/resellers')
def resellers():
    return redirect(url_for('managers_page'))

@app.route('/resellers/add', methods=['POST'])
def add_reseller_action():
    return redirect(url_for('managers_page'))

@app.route('/resellers/topup', methods=['POST'])
def topup_reseller_action():
    return redirect(url_for('managers_page'))

# ----------------- Settings & Audit -----------------
@app.route('/settings')
def settings():
    settings_rows = query_all('SELECT `key`, `value`, `description` FROM wisp_system_settings')
    audit_logs = query_all('SELECT * FROM wisp_audit_logs ORDER BY id DESC LIMIT 30')
    admins = query_all('SELECT id, username, full_name, role, is_active, created_at FROM wisp_admins')
    return render_template('settings.html', settings_list=settings_rows, audit_logs=audit_logs, admins=admins)

@app.route('/settings/update', methods=['POST'])
def update_settings_action():
    try:
        f = request.form

        # 1. Handle Logo Upload
        if 'network_logo' in request.files:
            file = request.files['network_logo']
            if file and file.filename:
                ext = os.path.splitext(file.filename)[1].lower()
                if ext in ['.png', '.jpg', '.jpeg', '.svg', '.webp', '.ico']:
                    upload_dir = os.path.join(app.static_folder, 'uploads')
                    os.makedirs(upload_dir, exist_ok=True)
                    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
                    filename = f"logo_{timestamp}{ext}"
                    filepath = os.path.join(upload_dir, filename)
                    file.save(filepath)

                    existing = query_one('SELECT `key` FROM wisp_system_settings WHERE `key` = ?', ('network_logo',))
                    if existing:
                        execute_write('UPDATE wisp_system_settings SET `value` = ? WHERE `key` = ?', (filename, 'network_logo'))
                    else:
                        execute_write('INSERT INTO wisp_system_settings (`key`, `value`, `description`) VALUES (?, ?, ?)', ('network_logo', filename, 'مسار شعار الشبكة'))

        # 2. Currency Mapping
        selected_currency = f.get('currency', 'SAR').strip()
        custom_symbol = f.get('currency_symbol', '').strip()

        currency_map = {
            'SAR': 'ر.س',
            'YER': 'ر.ي',
            'AED': 'د.إ',
            'EGP': 'ج.م',
            'USD': '$'
        }
        currency_symbol = custom_symbol if custom_symbol else currency_map.get(selected_currency, 'ر.س')

        # 3. Settings to Save
        network_name = f.get('network_name', '').strip() or f.get('company_name', '').strip() or 'MAX RADIUS'
        timezone_val = f.get('timezone', 'Asia/Aden').strip()

        settings_to_save = {
            'network_name': network_name,
            'company_name': network_name,
            'isp_name': network_name,
            'system_title': f.get('system_title', 'MAX RADIUS - نظام إدارة الشبكات والفوترة و FreeRADIUS').strip(),
            'currency': selected_currency,
            'currency_symbol': currency_symbol,
            'timezone': timezone_val,
            'support_phone': f.get('support_phone', '').strip(),
            'support_email': f.get('support_email', '').strip(),
            'address': f.get('address', '').strip(),
            'hotspot_domain': f.get('hotspot_domain', 'wifi.hotspot').strip(),
            'default_coa_port': f.get('default_coa_port', '3799').strip(),
            'sms_gateway_url': f.get('sms_gateway_url', '').strip(),
            'sms_api_key': f.get('sms_api_key', '').strip(),
            'sms_sender_id': f.get('sms_sender_id', 'WISP-NET').strip(),
            'portal_allow_registration': '1' if f.get('portal_allow_registration') in ('1', 'on', 'true', True, 1) else '0',
            'portal_allow_package_change': '1' if f.get('portal_allow_package_change') in ('1', 'on', 'true', True, 1) else '0',
            'portal_allow_password_change': '1' if f.get('portal_allow_password_change') in ('1', 'on', 'true', True, 1) else '0',
            'allow_data_loan': '1' if f.get('allow_data_loan') in ('1', 'on', 'true', True, 1) else '0',
            'loan_amount_mb': str(max(10, int(f.get('loan_amount_mb', '1024').strip()))) if f.get('loan_amount_mb', '').strip().isdigit() else '1024',
            'loan_threshold_mb': str(max(1, int(f.get('loan_threshold_mb', '100').strip()))) if f.get('loan_threshold_mb', '').strip().isdigit() else '100'
        }

        for k, v in settings_to_save.items():
            existing = query_one('SELECT `key` FROM wisp_system_settings WHERE `key` = ?', (k,))
            if existing:
                execute_write('UPDATE wisp_system_settings SET `value` = ? WHERE `key` = ?', (v, k))
            else:
                execute_write('INSERT INTO wisp_system_settings (`key`, `value`, `description`) VALUES (?, ?, ?)', (k, v, 'إعداد نظام'))

        # Set runtime timezone & reload scheduler
        try:
            os.environ['TZ'] = timezone_val
            import time
            if hasattr(time, 'tzset'):
                time.tzset()
            reload_backup_schedule()
        except Exception:
            pass

        log_audit(1, 'admin', 'UPDATE_SETTINGS', 'settings', f'Updated settings. Name: {network_name}, Currency: {selected_currency} ({currency_symbol}), Timezone: {timezone_val}')
        flash(f'تم حفظ وتطبيق كافة إعدادات النظام والهوية والعملة ({currency_symbol}) والمنطقة الزمنية ({timezone_val}) بنجاح.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء حفظ الإعدادات: {str(e)}', 'danger')
    return redirect(url_for('settings'))


# ----------------- Time & NTP Synchronization APIs -----------------
@app.route('/api/time/status', methods=['GET'])
def api_time_status():
    status = get_time_sync_status()
    return jsonify({'success': True, 'data': status})

@app.route('/api/time/sync', methods=['POST'])
def api_time_sync():
    success, offset, server, msg = sync_ntp_time(timeout=3.0)
    if success:
        reload_backup_schedule()
    status = get_time_sync_status()
    return jsonify({'success': success, 'message': msg, 'data': status})


# ----------------- 6. System Services & Engine Control (الأدوات -> خدمات النظام) -----------------
@app.route('/system-services')
@app.route('/tools/system-services')
def system_services_page():
    status_data = get_all_services_status()
    return render_template('system_services.html', status_data=status_data)

@app.route('/api/system-services/status')
def api_system_services_status():
    data = get_all_services_status()
    return jsonify({'success': True, 'data': data})

@app.route('/api/system-services/action', methods=['POST'])
def api_system_services_action():
    service_id = request.values.get('service_id', '').strip()
    action = request.values.get('action', '').strip()
    if not service_id or not action:
        return jsonify({'success': False, 'message': 'يرجى تحديد الخدمة والإجراء المطلوب.'})
    success, msg = execute_service_action(service_id, action, admin_username='admin')
    log_audit(1, 'admin', f'SERVICE_{action.upper()}', 'services', f'Triggered {action} on {service_id}: {msg}')
    return jsonify({'success': success, 'message': msg})

@app.route('/api/system-services/watchdog/check', methods=['POST'])
def api_watchdog_manual_check():
    from core.watchdog import run_watchdog_cycle
    res = run_watchdog_cycle()
    return jsonify({'success': True, 'message': 'تم إجراء فحص المراقبة الذاتية بنجاح.', 'data': res})

@app.route('/api/system-services/alerts/resolve', methods=['POST'])
def api_resolve_system_alert():
    from core.watchdog import resolve_system_alert
    alert_id = request.values.get('alert_id')
    if not alert_id:
        return jsonify({'success': False, 'message': 'يرجى تحديد معرف التنبيه.'})
    success, msg = resolve_system_alert(int(alert_id))
    return jsonify({'success': success, 'message': msg})

@app.route('/api/system-services/alerts/clear', methods=['POST'])
def api_clear_resolved_alerts():
    from core.watchdog import clear_all_resolved_alerts
    success, msg = clear_all_resolved_alerts()
    return jsonify({'success': success, 'message': msg})


# ----------------- Auto-Healing & Watchdog Suite (الأدوات -> التعافي التلقائي والمراقب الآلي) -----------------
@app.route('/tools/autoheal')
def autoheal_page():
    from services.autoheal_service import get_autoheal_dashboard_full
    data = get_autoheal_dashboard_full()
    return render_template('autoheal.html', data=data)

@app.route('/api/tools/autoheal/status')
def api_autoheal_status():
    from services.autoheal_service import get_autoheal_dashboard_full
    data = get_autoheal_dashboard_full()
    return jsonify({'success': True, 'data': data})

@app.route('/api/tools/autoheal/diagnostic', methods=['POST'])
def api_autoheal_diagnostic():
    from services.autoheal_service import run_deep_system_diagnostic
    diagnostic = run_deep_system_diagnostic()
    log_audit(1, 'admin', 'AUTOHEAL_DIAGNOSTIC_RUN', 'tools', f'Deep diagnostic ran. Health score: {diagnostic.get("score")}%')
    return jsonify({'success': True, 'diagnostic': diagnostic})

@app.route('/api/tools/autoheal/probe-ports', methods=['POST'])
def api_autoheal_probe_ports():
    from services.autoheal_service import probe_all_network_ports
    ports = probe_all_network_ports()
    return jsonify({'success': True, 'ports': ports})

@app.route('/api/tools/autoheal/restart', methods=['POST'])
def api_autoheal_restart():
    req_data = request.json if request.is_json else request.form.to_dict()
    container_name = req_data.get('container', '').strip()
    from services.autoheal_service import restart_system_container
    success, msg = restart_system_container(container_name)
    if success:
        log_audit(1, 'admin', 'CONTAINER_RESTART', 'tools', f'Restarted container: {container_name}')
    return jsonify({'success': success, 'message': msg})

@app.route('/api/tools/autoheal/logs')
def api_autoheal_logs():
    container_name = request.args.get('container', 'max_radius_autoheal').strip()
    lines = int(request.args.get('lines', 80))
    from services.autoheal_service import get_live_container_logs
    logs = get_live_container_logs(container_name, lines=lines)
    return jsonify({'success': True, 'logs': logs})

@app.route('/api/tools/autoheal/purge-zombies', methods=['POST'])
def api_autoheal_purge_zombies():
    req_data = request.json if request.is_json else request.form.to_dict()
    timeout = int(req_data.get('timeout_minutes', 15))
    from services.autoheal_service import purge_stale_zombie_sessions
    success, msg = purge_stale_zombie_sessions(timeout_minutes=timeout)
    if success:
        log_audit(1, 'admin', 'ZOMBIE_PURGE', 'tools', f'Purged stale sessions older than {timeout}m')
    return jsonify({'success': success, 'message': msg})

@app.route('/api/tools/autoheal/alerts/resolve', methods=['POST'])
def api_autoheal_resolve_alert():
    req_data = request.json if request.is_json else request.form.to_dict()
    alert_id = int(req_data.get('alert_id', 0))
    from core.watchdog import resolve_system_alert
    success, msg = resolve_system_alert(alert_id)
    return jsonify({'success': success, 'message': msg})

@app.route('/api/tools/autoheal/alerts/clear-resolved', methods=['POST'])
def api_autoheal_clear_resolved_alerts():
    from core.watchdog import clear_all_resolved_alerts
    success, msg = clear_all_resolved_alerts()
    return jsonify({'success': success, 'message': msg})


# ----------------- Database Maintenance & Engineering Audit (الأدوات -> صيانة قاعدة البيانات) -----------------
@app.route('/tools/database-maintenance')
def database_maintenance_page():
    from services.db_maintenance_service import get_maintenance_stats
    stats = get_maintenance_stats()
    return render_template('database_maintenance.html', stats=stats)

@app.route('/api/tools/database-maintenance/stats')
def api_maintenance_stats():
    from services.db_maintenance_service import get_maintenance_stats
    stats = get_maintenance_stats()
    return jsonify({'success': True, 'stats': stats})

@app.route('/api/tools/database-maintenance/audit', methods=['POST'])
def api_run_database_audit():
    from services.db_maintenance_service import run_database_audit
    audit = run_database_audit()
    return jsonify({'success': True, 'audit': audit})

@app.route('/api/tools/database-maintenance/fix', methods=['POST'])
def api_fix_audit_issue():
    data = request.json if request.is_json else request.form.to_dict()
    action_code = data.get('action_code')
    username = data.get('username')
    session_id = data.get('session_id')
    
    from services.db_maintenance_service import fix_audit_issue
    res = fix_audit_issue(action_code, username, {'session_id': session_id})
    if res.get('success'):
        log_audit(1, 'admin', 'DB_AUDIT_FIX', 'tools', f'Fixed {action_code} for user {username}')
    return jsonify(res)

@app.route('/api/tools/database-maintenance/fix-all', methods=['POST'])
def api_fix_all_audit_issues():
    from services.db_maintenance_service import fix_all_audit_issues
    res = fix_all_audit_issues()
    log_audit(1, 'admin', 'DB_AUDIT_FIX_ALL', 'tools', f'Fixed all issues: {res.get("fixed_count")} resolved')
    return jsonify(res)

@app.route('/api/tools/database-maintenance/clean-expired', methods=['POST'])
def api_clean_expired_vouchers():
    data = request.json if request.is_json else request.form.to_dict()
    delete_type = data.get('delete_type', 'all')
    delete_acct = bool(data.get('delete_acct', False))
    
    from services.db_maintenance_service import delete_expired_vouchers
    res = delete_expired_vouchers(delete_type=delete_type, delete_acct=delete_acct)
    if res.get('success'):
        log_audit(1, 'admin', 'DB_CASCADE_CLEAN', 'tools', f'Deleted expired vouchers ({delete_type}): {res.get("deleted_vouchers")} vouchers, {res.get("deleted_radcheck")} radcheck, {res.get("deleted_radusergroup")} radusergroup')
    return jsonify(res)

@app.route('/api/tools/database-maintenance/table-sizes')
def api_table_sizes():
    from services.db_maintenance_service import get_detailed_table_sizes
    res = get_detailed_table_sizes()
    return jsonify(res)

@app.route('/api/tools/database-maintenance/prune-logs', methods=['POST'])
def api_prune_historical_logs():
    data = request.json if request.is_json else request.form.to_dict()
    days = int(data.get('days', 30))
    prune_postauth = bool(data.get('prune_postauth', True))
    prune_audit = bool(data.get('prune_audit', False))
    
    from services.db_maintenance_service import prune_historical_logs
    res = prune_historical_logs(days=days, prune_postauth=prune_postauth, prune_audit=prune_audit)
    if res.get('success'):
        log_audit(1, 'admin', 'DB_LOG_PRUNE', 'tools', f'Pruned historical logs older than {days} days: {res.get("deleted_radacct")} acct, {res.get("deleted_radpostauth")} postauth')
    return jsonify(res)

@app.route('/api/tools/database-maintenance/optimize-tables', methods=['POST'])
def api_optimize_tables():
    data = request.json if request.is_json else request.form.to_dict()
    tables = data.get('tables') if isinstance(data.get('tables'), list) else None
    
    from services.db_maintenance_service import optimize_database_tables
    res = optimize_database_tables(tables=tables)
    if res.get('success'):
        log_audit(1, 'admin', 'DB_OPTIMIZE_TABLES', 'tools', f'Optimized database tables. Freed {res.get("freed_mb", 0)} MB')
    return jsonify(res)

@app.route('/api/tools/database-maintenance/factory-reset', methods=['POST'])
def api_factory_reset_database():
    data = request.json if request.is_json else request.form.to_dict()
    confirm_code = data.get('confirm_code')
    keep_packages = bool(data.get('keep_packages', True))
    keep_resellers = bool(data.get('keep_resellers', False))
    from services.db_maintenance_service import factory_reset_database
    res = factory_reset_database(keep_packages=keep_packages, keep_resellers=keep_resellers)
    if res.get('success'):
        log_audit(1, 'admin', 'DB_FACTORY_RESET', 'tools', f'Factory reset performed: {res.get("message")}')
    return jsonify(res)



# =========================================================================
# 5 Enterprise Tools Suite (الأدوات التخصصية الخمس المتقدمة)
# =========================================================================

# --- 1. MikroTik & NAS Live Diagnostics ---
@app.route('/tools/nas-diagnostics')
def nas_diagnostics_page():
    from services.nas_diagnostics_service import get_nas_diagnostics_overview
    data = get_nas_diagnostics_overview(skip_live_probe=False)
    return render_template('tools/nas_diagnostics.html', data=data)

@app.route('/api/tools/nas-diagnostics/status')
def api_nas_diagnostics_status():
    from services.nas_diagnostics_service import get_nas_diagnostics_overview
    data = get_nas_diagnostics_overview(skip_live_probe=False)
    return jsonify({'success': True, 'data': data})

@app.route('/api/tools/nas-diagnostics/test-coa-port', methods=['POST'])
def api_nas_diagnostics_coa_port():
    ip = request.form.get('ip', '').strip()
    port = int(request.form.get('port', 3799) or 3799)
    secret = request.form.get('secret', '').strip() or None
    from services.nas_diagnostics_service import test_coa_port
    ok, res = test_coa_port(ip, port=port, secret=secret)
    return jsonify({'success': ok, 'data': res, 'message': res})

@app.route('/api/tools/nas-diagnostics/send-coa-disconnect', methods=['POST'])
def api_nas_diagnostics_coa_disconnect():
    ip = request.form.get('ip', '').strip()
    secret = request.form.get('secret', '').strip() or None
    port = int(request.form.get('port', 3799) or 3799)
    from services.nas_diagnostics_service import send_test_coa_disconnect
    ok, msg = send_test_coa_disconnect(ip, nas_secret=secret, coa_port=port)
    return jsonify({'success': ok, 'message': msg})




# --- 3. Live RADIUS Protocol Simulator ---
@app.route('/tools/radius-simulator')
def radius_simulator_page():
    return render_template('tools/radius_simulator.html')

@app.route('/api/tools/radius-simulator/test', methods=['POST'])
def api_radius_simulator_test():
    user = request.form.get('username', '').strip()
    pwd = request.form.get('password', '').strip()
    srv = request.form.get('server', '127.0.0.1').strip()
    port = int(request.form.get('port', 1812))
    secret = request.form.get('secret', 'testing123').strip()
    from services.radius_sim_service import simulate_radius_auth
    res = simulate_radius_auth(user, pwd, radius_server=srv, radius_port=port, nas_secret=secret)
    return jsonify({'success': True, 'data': res})


# --- 4. Network Traffic & Bandwidth Peak Analytics ---
@app.route('/tools/traffic-analytics')
def traffic_analytics_page():
    from services.traffic_analytics_service import get_traffic_analytics_report
    timeframe = request.args.get('timeframe', '30d')
    data = get_traffic_analytics_report(timeframe=timeframe)
    return render_template('tools/traffic_analytics.html', data=data, timeframe=timeframe)

@app.route('/api/tools/traffic-analytics/status')
def api_traffic_analytics_status():
    from services.traffic_analytics_service import get_traffic_analytics_report
    timeframe = request.args.get('timeframe', '30d')
    data = get_traffic_analytics_report(timeframe=timeframe)
    return jsonify({'success': True, 'data': data})


# --- 5. Accounting Archiver & DB Compactor ---
@app.route('/tools/accounting-archiver')
def accounting_archiver_page():
    from services.accounting_archiver_service import get_archiver_status
    data = get_archiver_status()
    return render_template('tools/accounting_archiver.html', data=data)

@app.route('/api/tools/accounting-archiver/status')
def api_accounting_archiver_status():
    from services.accounting_archiver_service import get_archiver_status
    data = get_archiver_status()
    return jsonify({'success': True, 'data': data})

@app.route('/api/tools/accounting-archiver/run', methods=['POST'])
def api_accounting_archiver_run():
    days = int(request.form.get('days', 90))
    from services.accounting_archiver_service import archive_old_sessions
    ok, msg = archive_old_sessions(days_threshold=days)
    return jsonify({'success': ok, 'message': msg})


# =========================================================================
# 5 Next-Gen Enterprise Features (المنظومات الخمس النوعية الكبرى)
# =========================================================================

# --- 1. Smart Notifications & Bot Integration ---
@app.route('/notifications')
def notifications_center_page():
    from services.bot_notifications_service import get_notification_settings
    data = get_notification_settings()
    return render_template('notifications_center.html', data=data)

@app.route('/api/notifications/settings/save', methods=['POST'])
def api_notifications_settings_save():
    token = request.form.get('bot_token', '').strip()
    chat_id = request.form.get('chat_id', '').strip()
    enabled = request.form.get('is_enabled', '0')
    low_quota = request.form.get('low_quota', '0')
    recharge = request.form.get('recharge', '0')
    nas_down = request.form.get('nas_down', '0')
    daily = request.form.get('daily_summary', '0')
    from services.bot_notifications_service import update_notification_settings
    ok, msg = update_notification_settings(token, chat_id, enabled, low_quota, recharge, nas_down, daily)
    return jsonify({'success': ok, 'message': msg})

@app.route('/api/notifications/test-send', methods=['POST'])
def api_notifications_test_send():
    msg = request.form.get('message', '').strip()
    from services.bot_notifications_service import send_telegram_alert
    ok, res = send_telegram_alert(msg or "🚀 Test Message from MAX RADIUS", message_type='TEST_ALERT')
    return jsonify({'success': ok, 'message': res})


# --- 2. Loyalty Points & Rewards Engine ---
@app.route('/loyalty-rewards')
def loyalty_rewards_page():
    from services.loyalty_rewards_service import get_loyalty_overview
    data = get_loyalty_overview()
    return render_template('loyalty_rewards.html', data=data)

@app.route('/api/loyalty-rewards/award', methods=['POST'])
def api_loyalty_award_points():
    user = request.form.get('username', '').strip()
    pts = int(request.form.get('points', 0))
    rsn = request.form.get('reason', 'Admin Award')
    from services.loyalty_rewards_service import award_loyalty_points
    ok, msg = award_loyalty_points(user, pts, reason=rsn)
    return jsonify({'success': ok, 'message': msg})

@app.route('/api/loyalty-rewards/redeem', methods=['POST'])
def api_loyalty_redeem_reward():
    user = request.form.get('username', '').strip()
    reward_id = int(request.form.get('reward_id', 0))
    from services.loyalty_rewards_service import redeem_reward
    ok, msg = redeem_reward(user, reward_id)
    return jsonify({'success': ok, 'message': msg})


# --- 3. Smart Automation & Rules Engine ---
@app.route('/automation-rules')
def automation_rules_page():
    from services.automation_rules_service import get_automation_overview
    data = get_automation_overview()
    return render_template('automation_rules.html', data=data)

@app.route('/api/automation-rules/toggle', methods=['POST'])
def api_automation_toggle():
    rule_id = int(request.form.get('rule_id', 0))
    is_active = int(request.form.get('is_active', 1))
    from services.automation_rules_service import toggle_rule
    ok, msg = toggle_rule(rule_id, is_active)
    return jsonify({'success': ok, 'message': msg})

@app.route('/api/automation-rules/run', methods=['POST'])
def api_automation_run():
    rule_id = int(request.form.get('rule_id', 0))
    from services.automation_rules_service import execute_rule_now
    ok, msg = execute_rule_now(rule_id)
    return jsonify({'success': ok, 'message': msg})


# --- Database Migration Studio ---
@app.route('/tools/database-migration')
def database_migration_studio():
    return render_template('tools/database_migration.html')

@app.route('/api/tools/database-migration/analyze', methods=['POST'])
def api_migration_analyze():
    from services.database_migration_service import analyze_backup_file
    try:
        if 'backup_file' in request.files and request.files['backup_file'].filename:
            f = request.files['backup_file']
            temp_dir = os.path.join(os.getcwd(), 'data', 'migration_cache')
            os.makedirs(temp_dir, exist_ok=True)
            temp_path = os.path.join(temp_dir, 'uploaded_backup.gz')
            f.save(temp_path)
            res = analyze_backup_file(temp_path)
            res['temp_server_path'] = temp_path
            return jsonify(res)
        
        server_path = request.form.get('server_path')
        if not server_path and request.is_json:
            server_path = request.json.get('server_path')
        
        if server_path and os.path.exists(server_path):
            res = analyze_backup_file(server_path)
            res['temp_server_path'] = server_path
            return jsonify(res)
        
        return jsonify({'success': False, 'error': 'لم يتم تزويد ملف أو مسار صالح على السيرفر.'}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/tools/database-migration/execute', methods=['POST'])
def api_migration_execute():
    from services.database_migration_service import execute_database_migration
    try:
        data = request.get_json(silent=True) or {}
        server_path = data.get('server_path') or data.get('temp_server_path')
        
        if not server_path or not os.path.exists(server_path):
            default_cached = os.path.join(os.getcwd(), 'data', 'migration_cache', 'uploaded_backup.gz')
            if os.path.exists(default_cached):
                server_path = default_cached

        if not server_path or not os.path.exists(server_path):
            return jsonify({'success': False, 'error': 'مسار ملف النسخة الاحتياطية غير موجود أو غير صالح على السيرفر.'}), 400

        res = execute_database_migration(server_path, options=data)
        return jsonify(res)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/tools/database-migration/progress')
def api_migration_progress():
    from services.database_migration_service import get_migration_progress
    return jsonify(get_migration_progress())

@app.route('/api/tools/database-migration/cancel', methods=['POST'])
def api_migration_cancel():
    from services.database_migration_service import cancel_migration
    res = cancel_migration()
    return jsonify(res)


@app.route('/tools/mikrotik-import')
@login_required
def mikrotik_userman_import_page():
    try:
        from services.nas_service import get_nas_devices
        nas_devices = get_nas_devices(skip_live_probe=False)
    except Exception:
        nas_devices = []
    return render_template('tools/mikrotik_userman_import.html', nas_devices=nas_devices)

@app.route('/api/tools/mikrotik-import/analyze-rsc', methods=['POST'])
@login_required
def api_mikrotik_userman_analyze_rsc():
    from services.mikrotik_userman_importer import parse_rsc_content
    if 'rsc_file' not in request.files or not request.files['rsc_file'].filename:
        return jsonify({'success': False, 'error': 'لم يتم تحديد ملف السكربت'}), 400
    try:
        f = request.files['rsc_file']
        raw_text = f.read().decode('utf-8', errors='ignore')
        parsed = parse_rsc_content(raw_text)
        return jsonify({'success': True, 'data': parsed})
    except Exception as e:
        return jsonify({'success': False, 'error': f'فشل قراءة الملف: {str(e)}'}), 500

@app.route('/api/tools/mikrotik-import/test-api', methods=['POST'])
@login_required
def api_mikrotik_userman_test_api():
    from services.mikrotik_userman_importer import fetch_userman_via_api
    data = request.json or {}
    host = data.get('host', '').strip()
    port = int(data.get('port', 8728) or 8728)
    user = data.get('username', '').strip()
    pwd = data.get('password', '')
    use_ssl = bool(data.get('use_ssl', False))

    if not host or not user:
        return jsonify({'success': False, 'error': 'يرجى تزويد عنوان IP واسم المستخدم'}), 400

    try:
        parsed = fetch_userman_via_api(host=host, username=user, password=pwd, port=port, use_ssl=use_ssl)
        return jsonify({'success': True, 'data': parsed})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/tools/mikrotik-import/execute', methods=['POST'])
@login_required
def api_mikrotik_userman_execute():
    from services.mikrotik_userman_importer import execute_userman_import
    req_data = request.json or {}
    parsed_data = req_data.get('data') or {}
    target_type = req_data.get('target_type', 'subscribers')
    fallback_package = req_data.get('fallback_package')
    duplicate_action = req_data.get('duplicate_action', 'skip')
    admin_user = session.get('username', 'admin')

    res = execute_userman_import(
        parsed_data=parsed_data,
        target_type=target_type,
        fallback_package=fallback_package,
        duplicate_action=duplicate_action,
        admin_user=admin_user
    )
    return jsonify(res)





# --- 4. Reseller Digital Wallets ---
@app.route('/reseller-wallets')
def reseller_wallets_page():
    from services.reseller_wallet_service import get_wallets_overview
    data = get_wallets_overview()
    return render_template('reseller_wallet.html', data=data)

@app.route('/api/reseller-wallets/transfer', methods=['POST'])
def api_reseller_wallet_transfer():
    from_id = int(request.form.get('from_id', 0))
    to_id = int(request.form.get('to_id', 0))
    amount = float(request.form.get('amount', 0))
    notes = request.form.get('notes', '')
    from services.reseller_wallet_service import transfer_reseller_balance
    ok, msg = transfer_reseller_balance(from_id, to_id, amount, notes=notes)
    return jsonify({'success': ok, 'message': msg})

@app.route('/api/reseller-wallets/deposit', methods=['POST'])
def api_reseller_wallet_deposit():
    reseller_id = int(request.form.get('reseller_id', 0))
    amount = float(request.form.get('amount', 0))
    notes = request.form.get('notes', 'Direct Deposit')
    from services.reseller_wallet_service import deposit_reseller_balance
    ok, msg = deposit_reseller_balance(reseller_id, amount, notes=notes)
    return jsonify({'success': ok, 'message': msg})


# --- 5. Live Coverage & Towers GIS Map ---
@app.route('/coverage-map')
@login_required
def coverage_map_page():
    from services.coverage_map_service import get_coverage_map_data
    data = get_coverage_map_data()
    nas_list = query_all('SELECT id, name, ip_address, nas_type FROM wisp_nas_devices ORDER BY id ASC')
    return render_template('coverage_map.html', data=data, nas_list=nas_list)

@app.route('/api/coverage-map/data')
@login_required
def api_coverage_map_data():
    from services.coverage_map_service import get_coverage_map_data
    data = get_coverage_map_data()
    return jsonify({'success': True, 'data': data})

@app.route('/api/coverage-map/update', methods=['POST'])
@login_required
def api_coverage_map_update():
    tower_id = int(request.form.get('tower_id', 0))
    name = request.form.get('tower_name', '')
    lat = request.form.get('latitude', 0)
    lng = request.form.get('longitude', 0)
    radius = request.form.get('radius', 500)
    tower_type = request.form.get('tower_type', 'hotspot')
    nas_id = request.form.get('nas_id')
    nas_ip = request.form.get('nas_ip')
    notes = request.form.get('notes', '')
    from services.coverage_map_service import update_tower_location
    ok, msg = update_tower_location(tower_id, lat, lng, radius, name, tower_type=tower_type, nas_id=nas_id, nas_ip=nas_ip, notes=notes)
    return jsonify({'success': ok, 'message': msg})

# --- Towers API ---
@app.route('/api/towers/create', methods=['POST'])
@login_required
def api_towers_create():
    name = request.form.get('tower_name', '').strip()
    lat = request.form.get('latitude', 15.369445)
    lng = request.form.get('longitude', 44.191006)
    radius = request.form.get('radius', 500)
    tower_type = request.form.get('tower_type', 'hotspot')
    nas_id = request.form.get('nas_id') or None
    nas_ip = request.form.get('nas_ip') or None
    notes = request.form.get('notes', '')

    if not name:
        return jsonify({'success': False, 'message': 'يرجى إدخال اسم البرج / الموقع.'}), 400

    from services.coverage_map_service import create_tower
    ok, msg = create_tower(name, lat, lng, coverage_radius_meters=radius, tower_type=tower_type, nas_id=nas_id, nas_ip=nas_ip, notes=notes)
    return jsonify({'success': ok, 'message': msg})

@app.route('/api/towers/delete', methods=['POST'])
@login_required
def api_towers_delete():
    tower_id = int(request.form.get('tower_id', 0))
    if not tower_id:
        return jsonify({'success': False, 'message': 'رقم البرج غير صحيح.'}), 400
    from services.coverage_map_service import delete_tower
    ok, msg = delete_tower(tower_id)
    return jsonify({'success': ok, 'message': msg})

# --- Access Points API ---
@app.route('/api/access-points/create', methods=['POST'])
@login_required
def api_access_points_create():
    tower_id = int(request.form.get('tower_id', 0))
    name = request.form.get('name', '').strip()
    device_model = request.form.get('device_model', 'Ubiquiti Rocket')
    ip_address = request.form.get('ip_address', '').strip()
    snmp_community = request.form.get('snmp_community', 'public').strip()
    interface_name = request.form.get('interface_name', '').strip()
    frequency_mhz = int(request.form.get('frequency_mhz', 5800) or 5800)
    azimuth_deg = int(request.form.get('azimuth_deg', 0) or 0)
    beamwidth_deg = int(request.form.get('beamwidth_deg', 90) or 90)
    ssid = request.form.get('ssid', '').strip()
    radius = int(request.form.get('coverage_radius_meters', 400) or 400)
    status = request.form.get('status', 'active')
    notes = request.form.get('notes', '').strip()

    if not tower_id or not name:
        return jsonify({'success': False, 'message': 'يرجى اختيار البرج وإدخال اسم جهاز البث.'}), 400

    from services.coverage_map_service import create_access_point
    ok, msg = create_access_point(
        tower_id=tower_id, name=name, device_model=device_model, ip_address=ip_address,
        snmp_community=snmp_community, interface_name=interface_name, frequency_mhz=frequency_mhz,
        azimuth_deg=azimuth_deg, beamwidth_deg=beamwidth_deg, ssid=ssid, coverage_radius_meters=radius,
        status=status, notes=notes
    )
    return jsonify({'success': ok, 'message': msg})

@app.route('/api/access-points/update', methods=['POST'])
@login_required
def api_access_points_update():
    ap_id = int(request.form.get('ap_id', 0))
    name = request.form.get('name', '').strip()
    device_model = request.form.get('device_model', 'Ubiquiti Rocket')
    ip_address = request.form.get('ip_address', '').strip()
    snmp_community = request.form.get('snmp_community', 'public').strip()
    interface_name = request.form.get('interface_name', '').strip()
    frequency_mhz = int(request.form.get('frequency_mhz', 5800) or 5800)
    azimuth_deg = int(request.form.get('azimuth_deg', 0) or 0)
    beamwidth_deg = int(request.form.get('beamwidth_deg', 90) or 90)
    ssid = request.form.get('ssid', '').strip()
    radius = int(request.form.get('coverage_radius_meters', 400) or 400)
    status = request.form.get('status', 'active')
    notes = request.form.get('notes', '').strip()

    if not ap_id or not name:
        return jsonify({'success': False, 'message': 'بيانات جهاز البث غير صحيحة.'}), 400

    from services.coverage_map_service import update_access_point
    ok, msg = update_access_point(
        ap_id=ap_id, name=name, device_model=device_model, ip_address=ip_address,
        snmp_community=snmp_community, interface_name=interface_name, frequency_mhz=frequency_mhz,
        azimuth_deg=azimuth_deg, beamwidth_deg=beamwidth_deg, ssid=ssid, coverage_radius_meters=radius,
        status=status, notes=notes
    )
    return jsonify({'success': ok, 'message': msg})

@app.route('/api/access-points/delete', methods=['POST'])
@login_required
def api_access_points_delete():
    ap_id = int(request.form.get('ap_id', 0))
    if not ap_id:
        return jsonify({'success': False, 'message': 'رقم جهاز البث غير صحيح.'}), 400
    from services.coverage_map_service import delete_access_point
    ok, msg = delete_access_point(ap_id)
    return jsonify({'success': ok, 'message': msg})

@app.route('/api/access-points/list')
@login_required
def api_access_points_list():
    tower_id = int(request.args.get('tower_id', 0))
    from services.coverage_map_service import get_tower_access_points
    aps = get_tower_access_points(tower_id)
    return jsonify({'success': True, 'data': aps})


# ----------------- 7. Backup Management (الأدوات -> النسخ الاحتياطي) -----------------
@app.route('/backups')
def backups_page():
    backups = list_backups()
    db_engine = 'MariaDB 10.11' if DB_TYPE == 'mysql' else 'SQLite 3'
    backup_settings = get_backup_settings()
    scheduler_status = get_scheduler_status()
    return render_template(
        'backups.html',
        backups=backups,
        current_version=CURRENT_SYSTEM_VERSION,
        db_engine=db_engine,
        backup_settings=backup_settings,
        scheduler_status=scheduler_status
    )

@app.route('/api/backup/settings', methods=['GET', 'POST'])
def api_backup_settings():
    if request.method == 'GET':
        settings = get_backup_settings()
        status = get_scheduler_status()
        return jsonify({'success': True, 'settings': settings, 'status': status})
    
    # POST - Save settings
    data = request.json if request.is_json else request.form.to_dict()
    auto_enabled = data.get('auto_enabled')
    if isinstance(auto_enabled, str):
        auto_enabled = auto_enabled.lower() in ['true', '1', 'on', 'yes']
    else:
        auto_enabled = bool(auto_enabled)

    interval_days = int(data.get('interval_days', 1))
    
    # Parse times (can be list or JSON string or comma-separated)
    raw_times = data.get('times', [])
    times = []
    if isinstance(raw_times, str):
        try:
            times = json.loads(raw_times)
        except Exception:
            times = [t.strip() for t in raw_times.split(',') if t.strip()]
    elif isinstance(raw_times, list):
        times = [str(t).strip() for t in raw_times if str(t).strip()]
        
    storage_path = str(data.get('storage_path', '')).strip()

    success, msg = save_backup_settings(
        auto_enabled=auto_enabled,
        interval_days=interval_days,
        times=times,
        storage_path=storage_path
    )

    if success:
        reload_backup_schedule()
        
    return jsonify({
        'success': success,
        'message': msg,
        'settings': get_backup_settings(),
        'status': get_scheduler_status()
    })

@app.route('/api/backup/health', methods=['GET'])
def api_backup_health():
    probe = healthcheck_backup_scheduler()
    status_code = 200 if probe.get('healthy') else 503
    return jsonify(probe), status_code

@app.route('/api/backup/scheduler/action', methods=['POST'])
def api_backup_scheduler_action():
    data = request.json if request.is_json else request.form.to_dict()
    action = data.get('action', '').strip().lower()
    
    if action == 'start':
        success, msg = start_backup_scheduler()
    elif action == 'stop':
        success, msg = stop_backup_scheduler()
    elif action == 'restart':
        success, msg = restart_backup_scheduler()
    elif action == 'reload':
        success, msg = reload_backup_schedule()
    else:
        return jsonify({'success': False, 'message': f'إجراء غير صالح: {action}'})
        
    return jsonify({'success': success, 'message': msg, 'status': get_scheduler_status()})

@app.route('/api/backups/create', methods=['POST'])
def api_create_backup():
    try:
        admin_user = session.get('admin_username') or 'admin'
        success, msg, data = create_backup(admin_username=admin_user, notes='نسخة احتياطية يدوية')
        return jsonify({'success': success, 'message': msg, 'data': data})
    except Exception as e:
        return jsonify({'success': False, 'message': f'خطأ أثناء إنشاء النسخة الاحتياطية: {str(e)}'}), 500

@app.route('/api/backups/upload', methods=['POST'])
def api_upload_backup():
    try:
        if 'backup_file' not in request.files:
            return jsonify({'success': False, 'message': 'لم يتم العثور على ملف مرفوع في الطلب.'})
        file_storage = request.files['backup_file']
        admin_user = session.get('admin_username') or 'admin'
        success, msg = upload_backup_file(file_storage, admin_username=admin_user)
        return jsonify({'success': success, 'message': msg})
    except Exception as e:
        return jsonify({'success': False, 'message': f'خطأ أثناء رفع النسخة الاحتياطية: {str(e)}'}), 500

@app.route('/api/backups/restore', methods=['POST'])
def api_restore_backup():
    try:
        filename = request.values.get('filename', '').strip()
        if not filename and request.is_json:
            filename = (request.json or {}).get('filename', '').strip()
        if not filename:
            return jsonify({'success': False, 'message': 'يرجى تحديد اسم ملف النسخة الاحتياطية.'})
        admin_user = session.get('admin_username') or 'admin'
        success, msg = restore_backup(filename, admin_username=admin_user)
        return jsonify({'success': success, 'message': msg})
    except Exception as e:
        return jsonify({'success': False, 'message': f'خطأ أثناء استعادة النسخة الاحتياطية: {str(e)}'}), 500

@app.route('/api/backups/delete', methods=['POST'])
def api_delete_backup():
    try:
        filename = request.values.get('filename', '').strip()
        if not filename and request.is_json:
            filename = (request.json or {}).get('filename', '').strip()
        if not filename:
            return jsonify({'success': False, 'message': 'يرجى تحديد اسم ملف النسخة الاحتياطية.'})
        admin_user = session.get('admin_username') or 'admin'
        success, msg = delete_backup(filename, admin_username=admin_user)
        return jsonify({'success': success, 'message': msg})
    except Exception as e:
        return jsonify({'success': False, 'message': f'خطأ أثناء حذف النسخة الاحتياطية: {str(e)}'}), 500


@app.route('/backups/download/<path:filename>')
def download_backup_file_route(filename):
    filepath = get_backup_filepath(filename)
    if filepath and os.path.isfile(filepath):
        return send_file(filepath, as_attachment=True, download_name=os.path.basename(filepath))
    flash('ملف النسخة الاحتياطية المطلوب غير موجود على السيرفر.', 'danger')
    return redirect(url_for('backups_page'))

@app.route('/settings/backup')
def download_backup():
    return redirect(url_for('backups_page'))


# ----------------- 8. Excel Import Tool (الأدوات -> استيراد الكروت والمشتركين) -----------------
@app.route('/tools/import')
@app.route('/tools/import-data')
def import_data_page():
    resellers = query_all("SELECT id, name, phone FROM wisp_resellers WHERE status = 'active' ORDER BY name ASC")
    packages = query_all("SELECT id, name, price, service_type FROM wisp_packages WHERE is_active = 1 ORDER BY name ASC")
    return render_template('import_data.html', resellers=resellers, packages=packages)

@app.route('/tools/import/sample')
def download_sample_template():
    import_type = request.args.get('type', 'vouchers').strip().lower()
    if import_type not in ['vouchers', 'subscribers']:
        import_type = 'vouchers'
    try:
        sample_io = generate_sample_template(import_type)
        download_name = f"sample_{import_type}_{datetime.date.today().strftime('%Y%m%d')}.xlsx"
        return send_file(
            sample_io,
            as_attachment=True,
            download_name=download_name,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
    except Exception as e:
        flash(f'خطأ أثناء توليد نموذج الإكسل: {str(e)}', 'danger')
        return redirect(url_for('import_data_page'))

@app.route('/tools/import/process', methods=['POST'])
def process_import_action():
    try:
        import_type = request.form.get('import_type', 'vouchers').strip().lower()
        if import_type not in ['vouchers', 'subscribers']:
            import_type = 'vouchers'

        raw_reseller = request.form.get('default_reseller_id', '0')
        default_reseller_id = int(raw_reseller) if (raw_reseller and raw_reseller != '0') else None
        batch_name = request.form.get('batch_name', '').strip()

        if 'excel_file' not in request.files:
            return jsonify({'success': False, 'message': 'يرجى اختيار ملف Excel للرفع.'}), 400

        file = request.files['excel_file']
        if not file or not file.filename:
            return jsonify({'success': False, 'message': 'يرجى اختيار ملف صالح.'}), 400

        fname = file.filename.lower()
        if not (fname.endswith('.xlsx') or fname.endswith('.xls')):
            return jsonify({'success': False, 'message': 'صيغة الملف غير مدعومة. يرجى رفع ملفات .xlsx أو .xls فقط.'}), 400

        # 1. Parse Excel
        try:
            raw_rows = parse_excel_file(file)
        except Exception as e:
            return jsonify({'success': False, 'message': f'فشل قراءة ملف الإكسل: {str(e)}'}), 400

        if not raw_rows:
            return jsonify({'success': False, 'message': 'الملف المرفوع لا يحتوي على أي صفوف بيانات.'}), 400

        # 2. Strict Zero-Tolerance Validation
        is_valid, errors, cleaned_rows = validate_import_data(raw_rows, import_type=import_type, default_reseller_id=default_reseller_id)
        if not is_valid:
            return jsonify({
                'success': False,
                'message': f'تم العثور على {len(errors)} خطأ في ملف الإكسل يمنع الاستيراد (تم إلغاء العملية بالكامل لضمان سلامة البيانات).',
                'errors': errors
            }), 400

        # 3. Atomic Database Insertion
        admin_user = session.get('admin_user') or 'admin'
        success, msg, data = execute_import(import_type=import_type, cleaned_rows=cleaned_rows, batch_name=batch_name, admin_username=admin_user)

        if not success:
            return jsonify({'success': False, 'message': msg}), 500

        redirect_url = url_for('view_batch_details', batch_id=data['batch_id']) if (import_type == 'vouchers' and data.get('batch_id')) else url_for('subscribers')

        return jsonify({
            'success': True,
            'message': msg,
            'count': data.get('count', len(cleaned_rows)),
            'batch_name': data.get('batch_name'),
            'redirect_url': redirect_url
        })

    except Exception as e:
        return jsonify({'success': False, 'message': f'حدث استثناء غير متوقع أثناء معالجة الاستيراد: {str(e)}'}), 500



# ==========================================================
# ----------------- User Portal (Client Area) -------------
# ==========================================================
@app.route('/user')
@app.route('/user/dashboard')
def user_dashboard():
    username = session.get('portal_user')
    if not username:
        return redirect(url_for('user_login'))
    
    user_data = get_portal_user_data(username)
    return render_template('user_portal/dashboard.html', user=user_data)

@app.route('/user/login', methods=['GET'])
def user_login():
    if session.get('portal_user'):
        return redirect(url_for('user_dashboard'))
    return render_template('user_portal/login.html')

@app.route('/user/login', methods=['POST'])
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

@app.route('/user/logout')
def user_logout():
    session.pop('portal_user', None)
    session.pop('portal_type', None)
    flash('تم تسجيل الخروج من حسابك بنجاح.', 'info')
    return redirect(url_for('user_login'))

@app.route('/user/register', methods=['GET'])
def user_register():
    if session.get('portal_user'):
        return redirect(url_for('user_dashboard'))
        
    settings_rows = query_all('SELECT `key`, `value` FROM wisp_system_settings')
    settings_dict = {r['key']: r['value'] for r in settings_rows} if settings_rows else {}
    if settings_dict.get('portal_allow_registration', '1') == '0':
        flash('عذراً، التسجيل الذاتي لإنشاء حسابات جديدة معطل حالياً من قِبل إدارة الشبكة.', 'warning')
        return redirect(url_for('user_login'))
        
    return render_template('user_portal/register.html', old_form={})

@app.route('/user/register', methods=['POST'])
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

@app.route('/user/recharge', methods=['GET'])
def user_recharge():
    username = session.get('portal_user')
    if not username:
        return redirect(url_for('user_login'))
    
    user_data = get_portal_user_data(username)
    return render_template('user_portal/recharge.html', user=user_data)

@app.route('/user/recharge', methods=['POST'])
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

@app.route('/user/loan/request', methods=['POST'])
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

@app.route('/user/api/loan/request', methods=['POST'])
def api_user_loan_request():
    username = session.get('portal_user')
    if not username:
        data = request.json if request.is_json else request.form.to_dict()
        username = data.get('username', '').strip()
        
    if not username:
        return jsonify({'success': False, 'message': 'اسم المشترك غير محدد'}), 400
        
    success, msg = request_data_loan(username)
    return jsonify({'success': success, 'message': msg})


@app.route('/user/packages', methods=['GET'])
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

@app.route('/user/renew-package', methods=['POST'])
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

@app.route('/user/change-package', methods=['POST'])
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

@app.route('/user/change-password', methods=['POST'])
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

@app.route('/user/sessions', methods=['GET'])
def user_sessions():
    username = session.get('portal_user')
    if not username:
        return redirect(url_for('user_login'))
    
    user_data = get_portal_user_data(username)
    sessions = get_user_sessions_history(username, limit=50)
    return render_template('user_portal/sessions.html', user=user_data, sessions=sessions)

@app.route('/user/disconnect-session', methods=['POST'])
def user_disconnect_session_action():
    username = session.get('portal_user')
    if not username:
        return jsonify({'success': False, 'message': 'غير مصرح'}), 401
    
    from services.quick_action_service import action_disconnect_user
    res = action_disconnect_user(username)
    return jsonify(res)


# ==========================================================
# Client Licensing & System Security Routes
# ==========================================================

@app.route('/settings/license', methods=['GET'])
def license_status_page():
    lic_info = get_active_license_status()
    manager = get_current_manager()
    if not lic_info.get('valid') or not manager:
        return render_template('license_standalone.html', license=lic_info)
    return render_template('license_status.html', license=lic_info)

@app.route('/settings/license/activate', methods=['POST'])
def activate_license_action():
    lic_info = get_active_license_status()
    manager = get_current_manager()
    if lic_info.get('valid') and manager:
        require_permission('settings_manage')
        
    master_url = request.form.get('master_server_url', 'http://136.244.95.245:3030').strip()
    
    lic_file = request.files.get('license_file')
    lic_token = request.form.get('license_token', '').strip()
    
    content = ""
    if lic_file and lic_file.filename:
        try:
            content = lic_file.read().decode('utf-8')
        except Exception as e:
            flash(f"فشل قراءة ملف الترخيص المرفوع: {str(e)}", "danger")
            return redirect(url_for('license_status_page'))
    elif lic_token:
        content = lic_token
    else:
        flash("يرجى اختيار ملف الترخيص (.lic) أو لصق المفتاح المشفر", "danger")
        return redirect(url_for('license_status_page'))
        
    success, msg = install_and_activate_license(content, master_url)
    flash(msg, "success" if success else "danger")
    if success:
        if get_current_manager():
            return redirect(url_for('dashboard'))
        return redirect(url_for('login'))
    return redirect(url_for('license_status_page'))

@app.route('/settings/license/sync-heartbeat', methods=['POST'])
@login_required
def sync_license_heartbeat_action():
    success, msg = sync_license_heartbeat_with_server()
    return jsonify({"success": success, "message": msg})

if __name__ == '__main__':
    init_database()
    try:
        from services.l2tp_service import sync_all_tunnels_to_vpn
        sync_all_tunnels_to_vpn()
    except Exception as e:
        print(f"[Startup] L2TP Sync Notice: {e}")
    port = int(os.environ.get('PORT', 5090))
    app.run(host='0.0.0.0', port=port, debug=False)

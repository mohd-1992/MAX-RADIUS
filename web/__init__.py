# -*- coding: utf-8 -*-
"""
MAX RADIUS Application Factory & Blueprint Initializer.
"""

import os
import datetime
from flask import Flask, request, jsonify, redirect, url_for, session, render_template, flash

from core.config import (
    DB_TYPE, APP_NAME, APP_VERSION, APP_EDITION, APP_VERSION_FULL, APP_VERSION_BADGE
)
from database.db import query_all, query_one
from core.time_service import (
    get_configured_timezone_name, get_system_now
)
from core.rbac import get_current_manager, has_permission
from services.license_guard_service import (
    get_active_license_status, start_license_heartbeat_daemon
)
from web.utils import register_template_filters

def create_app(config=None):
    """
    Application Factory: Creates, configures and returns the Flask application instance.
    """
    app = Flask(
        __name__,
        template_folder=os.path.join(os.path.dirname(__file__), 'templates'),
        static_folder=os.path.join(os.path.dirname(__file__), 'static')
    )
    
    app.secret_key = os.environ.get('SECRET_KEY', 'wisp-radius-super-secret-key-2026')
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 86400
    app.jinja_env.auto_reload = True

    # Robust Session Isolation & Extended Lifetime
    app.config['SESSION_COOKIE_NAME'] = 'max_radius_session'
    app.config['PERMANENT_SESSION_LIFETIME'] = datetime.timedelta(days=30)
    app.config['SESSION_REFRESH_EACH_REQUEST'] = True
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['SESSION_COOKIE_SECURE'] = False

    if config:
        app.config.update(config)

    # Register Template Filters
    register_template_filters(app)

    # Start Background 5-minute Real-Time License Heartbeat Sync Daemon
    try:
        start_license_heartbeat_daemon(interval_seconds=300)
    except Exception:
        pass

    # 1. Cache Control Headers
    @app.after_request
    def add_cache_control_headers(response):
        if request.path.startswith('/static/vendor/') or request.path.startswith('/static/fonts/'):
            response.headers['Cache-Control'] = 'public, max-age=604800, immutable'
        elif request.path.startswith('/static/'):
            response.headers['Cache-Control'] = 'public, max-age=86400'
        return response

    # 2. License Guard Interceptor
    @app.before_request
    def enforce_license_guard_interceptor():
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

    # 3. Authentication & Access Interceptor
    @app.before_request
    def check_authentication():
        public_endpoints = {
            'login', 'logout', 'static', 'api_coa_status', 'health_check',
            'license_status_page', 'activate_license_action', 'sync_license_heartbeat_action'
        }
        
        ep = request.endpoint.split('.')[-1] if request.endpoint else ''
        if request.endpoint in public_endpoints or ep in public_endpoints:
            return None
        if request.path.startswith('/static') or request.path.startswith('/user') or request.path in [
            '/login', '/logout', '/health', '/api/backup/health', '/favicon.ico',
            '/settings/license', '/settings/license/activate', '/settings/license/sync-heartbeat'
        ]:
            return None
        if ep and (ep.startswith('portal_') or ep.startswith('user_')):
            return None
            
        # Check if admin/manager is logged in
        manager = get_current_manager()
        if not manager:
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({
                    'success': False,
                    'message': 'انتهت الجلسة أو لم تقم بتسجيل الدخول. يرجى تسجيل الدخول أولاً للمتابعة.',
                    'unauthenticated': True
                }), 401
            return redirect(url_for('login', next=request.url))

        # Strict License Guard
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
        except Exception:
            pass
        return None

    # 4. Context Processor
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
        sys_now = get_system_now(timezone)
        server_time_now = sys_now.strftime('%Y-%m-%d %H:%M:%S')
        server_time_only = sys_now.strftime('%H:%M:%S')
        server_date_only = sys_now.strftime('%Y-%m-%d')
        server_timestamp_ms = int(sys_now.timestamp() * 1000)
        # Fast Dynamic RADIUS server IP resolution
        radius_server_ip = settings.get('radius_server_ip') or settings.get('server_ip')
        if not radius_server_ip or str(radius_server_ip).strip() in ('127.0.0.1', 'localhost', '0.0.0.0'):
            if request.host:
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

    # 6. Global url_for Blueprint Endpoint Resolver
    def handle_blueprint_url_build_error(error, endpoint, values):
        """
        Transparently resolves endpoint names without blueprint prefix (e.g. url_for('login')
        instead of url_for('auth_bp.login')) to guarantee 100% template compatibility.
        """
        for rule in app.url_map.iter_rules():
            if rule.endpoint.endswith('.' + endpoint):
                return url_for(rule.endpoint, **values)
        raise error

    app.url_build_error_handlers.append(handle_blueprint_url_build_error)

    # 7. Register all 10 Blueprints
    from web.routes import (
        auth_bp, dashboard_bp, vouchers_bp, subscribers_bp,
        packages_bp, resellers_bp, nas_bp, accounting_bp,
        system_bp, portal_bp
    )

    for bp in [
        auth_bp, dashboard_bp, vouchers_bp, subscribers_bp,
        packages_bp, resellers_bp, nas_bp, accounting_bp,
        system_bp, portal_bp
    ]:
        app.register_blueprint(bp)

    return app

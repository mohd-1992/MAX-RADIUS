# -*- coding: utf-8 -*-
"""
MAX RADIUS Application Factory & Blueprint Initializer.
"""

import os
import datetime
import logging
from flask import Flask, g, request, jsonify, redirect, url_for, session, render_template, flash

logger = logging.getLogger('web')

from core.config import (
    DB_TYPE, APP_NAME, APP_VERSION, APP_EDITION, APP_VERSION_FULL, APP_VERSION_BADGE, SECRET_KEY
)
from database.db import query_all, query_one
from core.time_service import (
    get_configured_timezone_name, get_system_now
)
from core.rbac import get_current_manager, has_permission
from services.license_guard_service import (
    get_active_license_status, has_license_feature, start_license_heartbeat_daemon
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
    
    app.secret_key = SECRET_KEY
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
    from core.license_security import install_csrf
    install_csrf(app, lambda req: req.path in (
        '/settings/license/activate', '/settings/license/sync-heartbeat'))

    # Start Background 5-minute Real-Time License Heartbeat Sync Daemon
    try:
        start_license_heartbeat_daemon(interval_seconds=300)
        from services.license_guard_service import start_radius_license_state_daemon
        start_radius_license_state_daemon()
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

    @app.before_request
    def enforce_maintenance_mode():
        import os
        if request.method in ('POST','PUT','PATCH','DELETE') and request.path not in (
            '/api/tools/database-maintenance/factory-reset', '/api/tools/database-maintenance/factory-reset/cancel', '/login', '/logout'):
            try:
                from services.factory_reset_service import begin_operational_request
                if not begin_operational_request():
                    return jsonify(success=False,message='إعادة المصنع قيد التنفيذ؛ انتظر اكتمالها قبل إجراء عمليات أخرى.'),409
                g.factory_operational_request = True
            except Exception:
                return jsonify(success=False,message='تعذر التأكد من حالة إعادة المصنع.'),503
        if os.environ.get('MAX_MAINTENANCE_MODE') != '1':
            return None
        if request.path.startswith(('/static/', '/api/tools/database-maintenance', '/tools/database-maintenance', '/backups', '/api/backups', '/login', '/logout', '/settings/license')):
            if request.method in ('POST','PUT','PATCH','DELETE'):
                from services.account_lifecycle_service import job_lock
                guard=job_lock('maintenance-request')
                if not guard.__enter__():
                    guard.__exit__(None,None,None)
                    return jsonify(success=False,message='توجد عملية صيانة قيد التنفيذ.'),409
                g.maintenance_guard=guard
            return None
        return jsonify(success=False,message='النظام في وضع الصيانة؛ عمليات الحسابات متوقفة مؤقتًا.'), 503

    @app.teardown_request
    def release_maintenance_request(error=None):
        if g.pop('factory_operational_request',False):
            from services.factory_reset_service import end_operational_request
            end_operational_request()
        guard=g.pop('maintenance_guard',None)
        if guard is not None:
            guard.__exit__(None,None,None)

    # 2. License Guard Interceptor
    @app.before_request
    def enforce_license_guard_interceptor():
        path = request.path
        if (
            path.startswith('/static') or
            path.startswith('/api/v1/license') or
            path.startswith('/api/hotspot') or
            path in ('/settings/license', '/license', '/settings/license/activate', '/settings/license/sync-heartbeat') or
            path in ('/login', '/logout') or
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
                # Force redirect all browser tab navigation back to license page
                return redirect('/settings/license')
        except Exception as e:
            logger.error(f"[License Interceptor Error] {e}")
            if request.is_json or path.startswith('/api/'):
                return jsonify({
                    "success": False,
                    "error": "LICENSE_CHECK_FAILED",
                    "message": "حدث خطأ أثناء التحقق من الترخيص. تم إيقاف الطلب أمنياً."
                }), 403
            return redirect('/settings/license')
        return None

    # 3. Authentication & Access Interceptor
    @app.before_request
    def check_authentication():
        public_endpoints = {
            'login', 'logout', 'static', 'api_coa_status', 'health_check',
            
            'api_download_hotspot_package_zip'
        }
        
        ep = request.endpoint.split('.')[-1] if request.endpoint else ''
        if request.endpoint in public_endpoints or ep in public_endpoints:
            return None
        if request.path.startswith('/static') or request.path.startswith('/user') or request.path.startswith('/api/hotspot') or request.path in [
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

        # Strict License Guard (Second Enforcement Layer)
        try:
            lic_info = get_active_license_status()
            if lic_info and not lic_info.get('valid'):
                if request.is_json or request.path.startswith('/api/'):
                    return jsonify({
                        'success': False,
                        'locked': True,
                        'message': lic_info.get('message', 'النظام مقفل: يتطلب تفعيل الترخيص الرسمي.'),
                        'license_status': lic_info.get('status', 'unlicensed')
                    }), 403
                return redirect('/settings/license')
        except Exception:
            pass
        return None

    # 4. Context Processor
    @app.context_processor
    def inject_global_settings():
        try:
            try:
                settings_rows = query_all('SELECT `key`, `value` FROM wisp_system_settings')
                settings = {r['key']: r['value'] for r in settings_rows}
            except Exception:
                settings = {}

            network_name = settings.get('network_name') or settings.get('company_name') or settings.get('isp_name') or 'MAX RADIUS'
            network_logo = settings.get('network_logo', '').strip()
            
            logo_url = None
            if network_logo:
                try:
                    logo_disk_path = os.path.join(app.static_folder, 'uploads', network_logo)
                    if os.path.isfile(logo_disk_path):
                        logo_url = url_for('static', filename=f'uploads/{network_logo}')
                except Exception:
                    pass

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

            try:
                current_manager = get_current_manager()
            except Exception:
                current_manager = None

            try:
                sys_now = get_system_now(timezone)
            except Exception:
                sys_now = datetime.datetime.now()

            server_time_now = sys_now.strftime('%Y-%m-%d %H:%M:%S')
            server_time_only = sys_now.strftime('%H:%M:%S')
            server_date_only = sys_now.strftime('%Y-%m-%d')
            server_timestamp_ms = int(sys_now.timestamp() * 1000)
            
            # Fast Dynamic RADIUS server IP resolution
            radius_server_ip = settings.get('radius_server_ip') or settings.get('server_ip')
            if not radius_server_ip or str(radius_server_ip).strip() in ('127.0.0.1', 'localhost', '0.0.0.0'):
                try:
                    if request.host:
                        host_ip = request.host.split(':')[0]
                        if host_ip and host_ip not in ('127.0.0.1', 'localhost', '0.0.0.0'):
                            radius_server_ip = host_ip
                except Exception:
                    pass
            if not radius_server_ip or str(radius_server_ip).strip() in ('127.0.0.1', 'localhost', '0.0.0.0'):
                radius_server_ip = '192.168.1.100'

            try:
                lic_stat = get_active_license_status()
            except Exception:
                lic_stat = {}

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
                'has_feature': has_license_feature,
                'license_info': lic_stat,
                'is_over_quota': lic_stat.get('is_over_quota', False),
                'quota_warning': lic_stat.get('quota_warning', ''),
                'current_active_sessions': lic_stat.get('current_active_sessions'),
                'max_active_sessions': lic_stat.get('max_active_sessions', 0),
                'available_capacity': lic_stat.get('available_capacity', 0),
                'pending_reservations': lic_stat.get('pending_reservations', 0),
                'capacity_usage_pct': lic_stat.get('capacity_usage_pct', 0.0),
                'active_sessions_read_error': lic_stat.get('active_sessions_read_error', False),
                'is_legacy_license': lic_stat.get('is_legacy_license', False),
                'current_year': sys_now.year,
                'app_name': APP_NAME,
                'app_version': APP_VERSION,
                'app_edition': (lic_stat.get('plan_tier') if lic_stat and lic_stat.get('plan_tier') else APP_EDITION),
                'system_version': APP_VERSION_FULL,
                'app_version_badge': f"v{APP_VERSION} {lic_stat.get('plan_tier') if lic_stat and lic_stat.get('plan_tier') else APP_EDITION}"
            }
        except Exception as _ctx_err:
            logger.error(f"[Global Settings Context Fallback Triggered]: {_ctx_err}")
            return {
                'settings': {},
                'network_name': 'MAX RADIUS',
                'company_name': 'MAX RADIUS',
                'isp_name': 'MAX RADIUS',
                'currency_symbol': 'ر.ي',
                'is_over_quota': False,
                'quota_warning': '',
                'current_year': 2026,
                'app_name': APP_NAME,
                'app_version': APP_VERSION,
                'system_version': APP_VERSION_FULL,
                'has_permission': lambda *args, **kwargs: True,
                'has_feature': lambda *args, **kwargs: True
            }

    # 5. Fault-Tolerant Error Handlers
    from werkzeug.exceptions import HTTPException
    import traceback

    @app.errorhandler(500)
    @app.errorhandler(Exception)
    def handle_exception(err):
        # Let standard non-500 HTTP exceptions (like 404, 403, 302) pass through untouched
        try:
            if isinstance(err, HTTPException) and err.code != 500:
                return err
        except Exception:
            pass

        try:
            logger.error(f"[Server Unhandled Exception Handled]: {err}\n{traceback.format_exc()}")
        except Exception:
            try:
                print(f"[ERROR] [Server Unhandled Exception]: {err}")
            except Exception:
                pass

        if request.is_json or request.path.startswith('/api/'):
            return jsonify({
                'success': False,
                'error': 'INTERNAL_SERVER_ERROR',
                'message': 'حدث خطأ مؤقت في السيرفر أو تعارض أقفال. تم استرجاع المعاملة تلقائياً.'
            }), 500
        return f"""
        <div style="font-family: Cairo, Tahoma, sans-serif; text-align: center; padding: 50px; direction: rtl; background: #070b14; color: #fff; min-height: 100vh;">
            <div style="max-width: 600px; margin: 0 auto; background: #0b0f19; padding: 30px; border-radius: 20px; border: 1px solid #1c2840; box-shadow: 0 10px 30px rgba(0,0,0,0.5);">
                <div style="font-size: 40px; margin-bottom: 15px;">⚠️</div>
                <h2 style="color: #f87171; font-size: 20px; font-weight: bold; margin-bottom: 12px;">حدث خطأ مؤقت أثناء معالجة الطلب</h2>
                <p style="color: #94a3b8; font-size: 14px; line-height: 1.6;">الخادم قيد العمل وتم استرجاع الاتصال بأمان. يمكنك إعادة تحميل الصفحة أو العودة للرئيسية.</p>
                <div style="margin-top: 15px; padding: 12px; background: #05080e; border-radius: 10px; border: 1px solid #162136; text-align: left; font-family: monospace; font-size: 11px; color: #ef4444; overflow-x: auto; max-height: 150px;">
                    {str(err)}
                </div>
                <div style="margin-top: 25px; display: flex; gap: 10px; justify-content: center;">
                    <a href="/dashboard" style="display: inline-block; padding: 10px 20px; background: #2563eb; color: white; border-radius: 10px; text-decoration: none; font-weight: bold; font-size: 13px;">العودة للوحة التحكم</a>
                    <a href="javascript:location.reload()" style="display: inline-block; padding: 10px 20px; background: #1e293b; color: #cbd5e1; border-radius: 10px; text-decoration: none; font-weight: bold; font-size: 13px;">إعادة تحميل الصفحة</a>
                </div>
            </div>
        </div>
        """, 500

    # 6. Global url_for Blueprint Endpoint Resolver
    def handle_blueprint_url_build_error(error, endpoint, values):
        """
        Transparently resolves endpoint names without blueprint prefix (e.g. url_for('login')
        instead of url_for('auth_bp.login')) to guarantee 100% template compatibility.
        If endpoint still cannot be resolved, logs a warning and returns safe fallback URL instead of crashing.
        """
        try:
            for rule in app.url_map.iter_rules():
                if rule.endpoint.endswith('.' + endpoint) or rule.endpoint == endpoint:
                    return url_for(rule.endpoint, **values)
        except Exception:
            pass

        if 'license' in str(endpoint).lower():
            return '/settings/license'
        if 'login' in str(endpoint).lower():
            return '/login'
        if 'dashboard' in str(endpoint).lower():
            return '/dashboard'
        try:
            logger.warning(f"[URL Build Fallback] Unknown endpoint: {endpoint}")
        except Exception:
            pass
        return '#'

    app.url_build_error_handlers.append(handle_blueprint_url_build_error)

    # 7. Register all 11 Blueprints
    from web.routes import (
        auth_bp, dashboard_bp, vouchers_bp, subscribers_bp,
        packages_bp, resellers_bp, nas_bp, accounting_bp,
        system_bp, portal_bp, whatsapp_bp
    )

    for bp in [
        auth_bp, dashboard_bp, vouchers_bp, subscribers_bp,
        packages_bp, resellers_bp, nas_bp, accounting_bp,
        system_bp, portal_bp, whatsapp_bp
    ]:
        app.register_blueprint(bp)

    # Recover only the service state of an interrupted reset, never repeat its wipe.
    from services.factory_reset_service import recover_factory_reset
    import threading
    def recover_reset_service():
        try:
            # A newly queued job gets a short grace period, including after a process crash.
            from services.factory_reset_service import get_factory_reset
            import time
            for attempt in range(4):
                recover_factory_reset()
                job=get_factory_reset()
                if not job or job['state']!='queued':break
                if attempt<3:time.sleep(20)
        except Exception:logger.exception('Unable to recover interrupted factory reset')
    threading.Thread(target=recover_reset_service,name='FactoryResetRecovery',daemon=True).start()
    return app

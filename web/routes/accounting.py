# -*- coding: utf-8 -*-
"""
MAX RADIUS Web - Radius Accounting, Live Sessions, and CoA Disconnects Routes Module.
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

logger = logging.getLogger('accounting_bp')

accounting_bp = Blueprint('accounting_bp', __name__)


@accounting_bp.route('/api/coa/status', endpoint="api_coa_status")

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


@accounting_bp.route('/tools/radius-simulator', endpoint="radius_simulator_page")

def radius_simulator_page():
    return render_template('tools/radius_simulator.html')


@accounting_bp.route('/api/tools/radius-simulator/test', methods=['POST'], endpoint="api_radius_simulator_test")

def api_radius_simulator_test():
    user = request.form.get('username', '').strip()
    pwd = request.form.get('password', '').strip()
    srv = request.form.get('server', '127.0.0.1').strip()
    port = int(request.form.get('port', 1812))
    secret = request.form.get('secret', 'testing123').strip()
    from services.radius_sim_service import simulate_radius_auth
    res = simulate_radius_auth(user, pwd, radius_server=srv, radius_port=port, nas_secret=secret)
    return jsonify({'success': True, 'data': res})


@accounting_bp.route('/tools/traffic-analytics', endpoint="traffic_analytics_page")

def traffic_analytics_page():
    from services.traffic_analytics_service import get_traffic_analytics_report
    timeframe = request.args.get('timeframe', '30d')
    data = get_traffic_analytics_report(timeframe=timeframe)
    return render_template('tools/traffic_analytics.html', data=data, timeframe=timeframe)


@accounting_bp.route('/api/tools/traffic-analytics/status', endpoint="api_traffic_analytics_status")

def api_traffic_analytics_status():
    from services.traffic_analytics_service import get_traffic_analytics_report
    timeframe = request.args.get('timeframe', '30d')
    data = get_traffic_analytics_report(timeframe=timeframe)
    return jsonify({'success': True, 'data': data})


@accounting_bp.route('/tools/accounting-archiver', endpoint="accounting_archiver_page")

def accounting_archiver_page():
    from services.accounting_archiver_service import get_archiver_status
    data = get_archiver_status()
    return render_template('tools/accounting_archiver.html', data=data)


@accounting_bp.route('/api/tools/accounting-archiver/status', endpoint="api_accounting_archiver_status")

def api_accounting_archiver_status():
    from services.accounting_archiver_service import get_archiver_status
    data = get_archiver_status()
    return jsonify({'success': True, 'data': data})


@accounting_bp.route('/api/tools/accounting-archiver/run', methods=['POST'], endpoint="api_accounting_archiver_run")

def api_accounting_archiver_run():
    days = int(request.form.get('days', 90))
    from services.accounting_archiver_service import archive_old_sessions
    ok, msg = archive_old_sessions(days_threshold=days)
    return jsonify({'success': ok, 'message': msg})



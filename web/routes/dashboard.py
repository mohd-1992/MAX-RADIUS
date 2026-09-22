# -*- coding: utf-8 -*-
"""
MAX RADIUS Web - Live Metrics, Dashboard, and Coverage Map Routes Module.
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

logger = logging.getLogger('dashboard_bp')

dashboard_bp = Blueprint('dashboard_bp', __name__)


@dashboard_bp.route('/', endpoint="dashboard")

@dashboard_bp.route('/dashboard', endpoint="dashboard")

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


@dashboard_bp.route('/api/dashboard/live-metrics', endpoint="api_dashboard_live_metrics")

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


@dashboard_bp.route('/api/system/ping', endpoint="api_system_ping")

def api_system_ping():
    res = get_internet_ping()
    return jsonify({
        'success': True,
        'ping': res
    })


@dashboard_bp.route('/coverage-map', endpoint="coverage_map_page")

@login_required
def coverage_map_page():
    from services.coverage_map_service import get_coverage_map_data
    data = get_coverage_map_data()
    nas_list = query_all('SELECT id, name, ip_address, nas_type FROM wisp_nas_devices ORDER BY id ASC')
    return render_template('coverage_map.html', data=data, nas_list=nas_list)


@dashboard_bp.route('/api/coverage-map/data', endpoint="api_coverage_map_data")

@login_required
def api_coverage_map_data():
    from services.coverage_map_service import get_coverage_map_data
    data = get_coverage_map_data()
    return jsonify({'success': True, 'data': data})


@dashboard_bp.route('/api/coverage-map/update', methods=['POST'], endpoint="api_coverage_map_update")

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


@dashboard_bp.route('/api/towers/create', methods=['POST'], endpoint="api_towers_create")

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


@dashboard_bp.route('/api/towers/delete', methods=['POST'], endpoint="api_towers_delete")

@login_required
def api_towers_delete():
    tower_id = int(request.form.get('tower_id', 0))
    if not tower_id:
        return jsonify({'success': False, 'message': 'رقم البرج غير صحيح.'}), 400
    from services.coverage_map_service import delete_tower
    ok, msg = delete_tower(tower_id)
    return jsonify({'success': ok, 'message': msg})


@dashboard_bp.route('/api/access-points/create', methods=['POST'], endpoint="api_access_points_create")

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


@dashboard_bp.route('/api/access-points/update', methods=['POST'], endpoint="api_access_points_update")

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


@dashboard_bp.route('/api/access-points/delete', methods=['POST'], endpoint="api_access_points_delete")

@login_required
def api_access_points_delete():
    ap_id = int(request.form.get('ap_id', 0))
    if not ap_id:
        return jsonify({'success': False, 'message': 'رقم جهاز البث غير صحيح.'}), 400
    from services.coverage_map_service import delete_access_point
    ok, msg = delete_access_point(ap_id)
    return jsonify({'success': ok, 'message': msg})


@dashboard_bp.route('/api/access-points/list', endpoint="api_access_points_list")

@login_required
def api_access_points_list():
    tower_id = int(request.args.get('tower_id', 0))
    from services.coverage_map_service import get_tower_access_points
    aps = get_tower_access_points(tower_id)
    return jsonify({'success': True, 'data': aps})



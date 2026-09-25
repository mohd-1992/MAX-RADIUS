# -*- coding: utf-8 -*-
"""
MAX RADIUS Web - NAS Routers, VPN Tunnels, and MikroTik Integration Routes Module.
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
from services.wireguard_service import (
    get_all_wireguard_tunnels, get_wireguard_tunnel, get_available_wireguard_ip,
    get_wireguard_server_keys, generate_wg_keypair, add_wireguard_tunnel,
    update_wireguard_tunnel, delete_wireguard_tunnel, generate_mikrotik_wireguard_script
)
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

logger = logging.getLogger('nas_bp')

nas_bp = Blueprint('nas_bp', __name__)


@nas_bp.route('/nas', endpoint="nas")
@login_required
def nas():
    require_permission('nas_view')
    # Fetch devices with live status probe on page load as originally configured
    devices = get_nas_devices(skip_live_probe=False)
    return render_template('nas.html', devices=devices)


@nas_bp.route('/nas/add', methods=['GET', 'POST'], endpoint="add_nas_action")
@nas_bp.route('/nas/new', methods=['GET', 'POST'], endpoint="add_nas_action")
def add_nas_action():
    if request.method == 'GET':
        return render_template('nas_add.html')
    try:
        add_nas_device(request.form)
        flash('تمت إضافة راوتر MikroTik بنجاح وتوليد إعدادات RADIUS NAS.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء إضافة الراوتر: {str(e)}', 'danger')
    return redirect(url_for('nas'))


@nas_bp.route('/nas/<int:nas_id>', endpoint="nas_details_page")
def nas_details_page(nas_id):
    device = query_one('SELECT * FROM wisp_nas_devices WHERE id = ?', (nas_id,))
    if not device:
        flash('الراوتر غير موجود.', 'danger')
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
    
    total_up = traffic['up'] if traffic else 0
    total_down = traffic['down'] if traffic else 0
    
    return render_template(
        'nas_details.html',
        device=device,
        status_info=status_info,
        active_sessions=active_sessions,
        total_upload_str=format_bytes(total_up),
        total_download_str=format_bytes(total_down)
    )


@nas_bp.route('/nas/<int:nas_id>/edit', methods=['GET', 'POST'], endpoint="edit_nas_action")
def edit_nas_action(nas_id):
    device = query_one('SELECT * FROM wisp_nas_devices WHERE id = ?', (nas_id,))
    if not device:
        flash('الراوتر غير موجود.', 'danger')
        return redirect(url_for('nas'))
        
    if request.method == 'GET':
        flash('تعديل الراوتر متاح مباشرة عبر نافذة التعديل السريع في صفحة الراوترات.', 'info')
        return redirect(url_for('nas_details_page', nas_id=nas_id))
        
    try:
        update_nas_device(nas_id, request.form)
        flash('تم تحديث بيانات الراوتر بنجاح.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء التحديث: {str(e)}', 'danger')
    return redirect(url_for('nas_details_page', nas_id=nas_id))


@nas_bp.route('/nas/<int:nas_id>/delete', methods=['POST'], endpoint="delete_nas_action")
def delete_nas_action(nas_id):
    try:
        delete_nas_device(nas_id)
        flash('تم حذف الراوتر وجميع ارتباطاته في الراديوس بنجاح.', 'warning')
    except Exception as e:
        flash(f'خطأ أثناء الحذف: {str(e)}', 'danger')
    return redirect(url_for('nas'))


@nas_bp.route('/nas/<int:nas_id>/script', endpoint="nas_setup_script_page")
def nas_setup_script_page(nas_id):
    device = query_one('SELECT * FROM wisp_nas_devices WHERE id = ?', (nas_id,))
    if not device:
        flash('الراوتر غير موجود.', 'danger')
        return redirect(url_for('nas'))
        
    vps_ip = detect_vps_public_ip()
    script = generate_nas_script(device, vps_ip)
    
    return Response(script, mimetype='text/plain')


@nas_bp.route('/nas/<int:nas_id>/download-script', endpoint="download_nas_script_file")
def download_nas_script_file(nas_id):
    device = query_one('SELECT * FROM wisp_nas_devices WHERE id = ?', (nas_id,))
    if not device:
        flash('الراوتر غير موجود.', 'danger')
        return redirect(url_for('nas'))
        
    vps_ip = detect_vps_public_ip()
    script = generate_nas_script(device, vps_ip)
    
    return Response(
        script,
        mimetype="text/plain",
        headers={"Content-disposition": f"attachment; filename=setup_nas_{device['name']}.rsc"}
    )


@nas_bp.route('/api/nas/<int:nas_id>/live-status', endpoint="api_nas_live_status")
def api_nas_live_status(nas_id):
    device = query_one('SELECT * FROM wisp_nas_devices WHERE id = ?', (nas_id,))
    if not device:
        return jsonify({'success': False, 'message': 'الراوتر غير موجود.'}), 404
    from core.mikrotik_api import fetch_single_nas_status
    status_data = fetch_single_nas_status(device)
    return jsonify({'success': True, 'data': status_data})

@nas_bp.route('/api/nas/test-coa/<int:nas_id>', methods=['POST'], endpoint="api_nas_test_coa")
@login_required
def api_nas_test_coa(nas_id):
    from services.nas_service import test_nas_coa
    res = test_nas_coa(nas_id)
    return jsonify(res)



# ==============================================================================
# DUAL-PROTOCOL VPN MANAGEMENT: L2TP/IPSEC & WIREGUARD
# ==============================================================================

@nas_bp.route('/nas/l2tp', endpoint="l2tp_tunnels_page")
@login_required
def l2tp_tunnels_page():
    import ipaddress
    tunnels = get_l2tp_tunnels(fast_db_only=False)
    wg_tunnels = get_all_wireguard_tunnels(fast_db_only=False)
    vps_ip = detect_vps_public_ip()
    settings = get_l2tp_network_settings()
    wg_server = get_wireguard_server_keys()

    gw_ip = settings.get('l2tp_gateway_ip', '192.168.44.1')
    mask = settings.get('l2tp_mask', '255.255.255.0')
    pool_start = settings.get('l2tp_pool_start', '192.168.44.10')
    pool_end = settings.get('l2tp_pool_end', '192.168.44.250')
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

    wg_suggested_ip = get_available_wireguard_ip()
    active_tab = request.args.get('tab', 'l2tp').strip().lower()

    return render_template(
        'l2tp_tunnels.html',
        tunnels=tunnels,
        wg_tunnels=wg_tunnels,
        vps_detected_ip=vps_ip,
        l2tp_settings=settings,
        l2tp_gateway_ip=gw_ip,
        l2tp_mask=mask,
        l2tp_pool_start=pool_start,
        l2tp_pool_end=pool_end,
        l2tp_port=port,
        l2tp_ipsec_secret=ipsec_secret,
        next_suggested_ip=next_ip,
        wg_server=wg_server,
        wg_suggested_ip=wg_suggested_ip,
        active_tab=active_tab
    )


@nas_bp.route('/nas/l2tp/settings', methods=['POST'], endpoint="l2tp_save_settings_action")
@login_required
def l2tp_save_settings_action():
    try:
        current_mgr = get_current_manager()
        admin_user = current_mgr['username'] if current_mgr else 'admin'
        save_l2tp_network_settings(request.form, admin_username=admin_user)
        flash('تم حفظ وتطبيق إعدادات شبكة L2TP/IPsec VPN وإعادة تشغيل خادم النفق بنجاح.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء حفظ إعدادات شبكة VPN: {str(e)}', 'danger')
    return redirect(url_for('l2tp_tunnels_page', tab='l2tp'))


@nas_bp.route('/api/nas/l2tp/settings', methods=['GET', 'POST'], endpoint="api_l2tp_settings")
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


@nas_bp.route('/nas/l2tp/add', methods=['POST'], endpoint="l2tp_add_tunnel_action")
@login_required
def l2tp_add_tunnel_action():
    try:
        current_mgr = get_current_manager()
        admin_user = current_mgr['username'] if current_mgr else 'admin'
        add_l2tp_tunnel(request.form, admin_username=admin_user)
        flash('تمت إضافة نفق راوتر L2TP/IPsec واعتماده في FreeRADIUS وسيرفر النفق بنجاح.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء إضافة راوتر النفق: {str(e)}', 'danger')
    return redirect(url_for('l2tp_tunnels_page', tab='l2tp'))


@nas_bp.route('/nas/l2tp/edit/<int:tunnel_id>', methods=['POST'], endpoint="l2tp_edit_tunnel_action")
@login_required
def l2tp_edit_tunnel_action(tunnel_id):
    try:
        current_mgr = get_current_manager()
        admin_user = current_mgr['username'] if current_mgr else 'admin'
        update_l2tp_tunnel(tunnel_id, request.form, admin_username=admin_user)
        flash('تم حفظ تعديلات نفق الراوتر بنجاح.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء تعديل النفق: {str(e)}', 'danger')
    return redirect(url_for('l2tp_tunnels_page', tab='l2tp'))


@nas_bp.route('/nas/l2tp/delete/<int:tunnel_id>', methods=['POST'], endpoint="l2tp_delete_tunnel_action")
@login_required
def l2tp_delete_tunnel_action(tunnel_id):
    try:
        current_mgr = get_current_manager()
        admin_user = current_mgr['username'] if current_mgr else 'admin'
        delete_l2tp_tunnel(tunnel_id, admin_username=admin_user)
        flash('تم حذف نفق الراوتر ومسحه من الراديوس بنجاح.', 'warning')
    except Exception as e:
        flash(f'خطأ أثناء حذف النفق: {str(e)}', 'danger')
    return redirect(url_for('l2tp_tunnels_page', tab='l2tp'))


@nas_bp.route('/api/nas/l2tp/live-status', endpoint="api_l2tp_live_status")
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


@nas_bp.route('/api/nas/l2tp/<int:tunnel_id>/script', endpoint="api_l2tp_mikrotik_script")
@login_required
def api_l2tp_mikrotik_script(tunnel_id):
    vps_host = request.args.get('host')
    script = generate_mikrotik_rsc_script(tunnel_id, vps_host=vps_host)
    if not script:
        return jsonify({'success': False, 'message': 'النفق غير موجود'}), 404
    return jsonify({'success': True, 'script': script})


@nas_bp.route('/nas/l2tp/<int:tunnel_id>/download-script', endpoint="download_l2tp_mikrotik_script")
@login_required
def download_l2tp_mikrotik_script(tunnel_id):
    vps_host = request.args.get('host')
    script = generate_mikrotik_rsc_script(tunnel_id, vps_host=vps_host)
    if not script:
        flash('النفق غير موجود.', 'danger')
        return redirect(url_for('l2tp_tunnels_page', tab='l2tp'))
    return Response(
        script,
        mimetype="text/plain",
        headers={"Content-disposition": f"attachment; filename=mikrotik_l2tp_{tunnel_id}.rsc"}
    )


# ==============================================================================
# WIREGUARD VPN ROUTES
# ==============================================================================

@nas_bp.route('/nas/wireguard/add', methods=['POST'], endpoint="wireguard_add_tunnel_action")
@login_required
def wireguard_add_tunnel_action():
    try:
        current_mgr = get_current_manager()
        admin_user = current_mgr['username'] if current_mgr else 'admin'
        add_wireguard_tunnel(request.form, admin_username=admin_user)
        flash('تمت إضافة نفق WireGuard وتسجيله في النواة وFreeRADIUS بنجاح.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء إضافة راوتر WireGuard: {str(e)}', 'danger')
    return redirect(url_for('l2tp_tunnels_page', tab='wireguard'))


@nas_bp.route('/nas/wireguard/edit/<int:tunnel_id>', methods=['POST'], endpoint="wireguard_edit_tunnel_action")
@login_required
def wireguard_edit_tunnel_action(tunnel_id):
    try:
        current_mgr = get_current_manager()
        admin_user = current_mgr['username'] if current_mgr else 'admin'
        update_wireguard_tunnel(tunnel_id, request.form, admin_username=admin_user)
        flash('تم حفظ تعديلات نفق WireGuard وتحديث النواة بنجاح.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء تعديل نفق WireGuard: {str(e)}', 'danger')
    return redirect(url_for('l2tp_tunnels_page', tab='wireguard'))


@nas_bp.route('/nas/wireguard/delete/<int:tunnel_id>', methods=['POST'], endpoint="wireguard_delete_tunnel_action")
@login_required
def wireguard_delete_tunnel_action(tunnel_id):
    try:
        current_mgr = get_current_manager()
        admin_user = current_mgr['username'] if current_mgr else 'admin'
        delete_wireguard_tunnel(tunnel_id, admin_username=admin_user)
        flash('تم حذف نفق WireGuard ومسحه من النواة والراديوس بنجاح.', 'warning')
    except Exception as e:
        flash(f'خطأ أثناء حذف نفق WireGuard: {str(e)}', 'danger')
    return redirect(url_for('l2tp_tunnels_page', tab='wireguard'))


@nas_bp.route('/api/nas/wireguard/live-status', endpoint="api_wireguard_live_status")
@login_required
def api_wireguard_live_status():
    wg_tunnels = get_all_wireguard_tunnels(fast_db_only=False)
    online_count = len([t for t in wg_tunnels if t.get('is_online')])
    return jsonify({
        'success': True,
        'tunnels': wg_tunnels,
        'total_count': len(wg_tunnels),
        'online_count': online_count,
        'offline_count': len(wg_tunnels) - online_count
    })


@nas_bp.route('/api/nas/wireguard/<int:tunnel_id>/script', endpoint="api_wireguard_mikrotik_script")
@login_required
def api_wireguard_mikrotik_script(tunnel_id):
    vps_host = request.args.get('host')
    script = generate_mikrotik_wireguard_script(tunnel_id, vps_host=vps_host)
    if not script:
        return jsonify({'success': False, 'message': 'النفق غير موجود'}), 404
    return jsonify({'success': True, 'script': script})


@nas_bp.route('/nas/wireguard/<int:tunnel_id>/download-script', endpoint="download_wireguard_mikrotik_script")
@login_required
def download_wireguard_mikrotik_script(tunnel_id):
    vps_host = request.args.get('host')
    script = generate_mikrotik_wireguard_script(tunnel_id, vps_host=vps_host)
    if not script:
        flash('النفق غير موجود.', 'danger')
        return redirect(url_for('l2tp_tunnels_page', tab='wireguard'))
    return Response(
        script,
        mimetype="text/plain",
        headers={"Content-disposition": f"attachment; filename=mikrotik_wireguard_{tunnel_id}.rsc"}
    )


@nas_bp.route('/api/nas/wireguard/generate-keys', endpoint="api_wireguard_generate_keys")
@login_required
def api_wireguard_generate_keys():
    priv, pub = generate_wg_keypair()
    return jsonify({
        'success': bool(priv and pub),
        'private_key': priv or '',
        'public_key': pub or ''
    })


# ==============================================================================
# NAS DIAGNOSTICS & MIKROTIK IMPORT TOOLS
# ==============================================================================

@nas_bp.route('/tools/nas-diagnostics', endpoint="nas_diagnostics_page")
def nas_diagnostics_page():
    from services.nas_diagnostics_service import get_nas_diagnostics_overview
    data = get_nas_diagnostics_overview(skip_live_probe=False)
    return render_template('tools/nas_diagnostics.html', data=data)


@nas_bp.route('/api/tools/nas-diagnostics/status', endpoint="api_nas_diagnostics_status")
def api_nas_diagnostics_status():
    from services.nas_diagnostics_service import get_nas_diagnostics_overview
    data = get_nas_diagnostics_overview(skip_live_probe=False)
    return jsonify({'success': True, 'data': data})


@nas_bp.route('/api/tools/nas-diagnostics/test-coa-port', methods=['POST'], endpoint="api_nas_diagnostics_coa_port")
def api_nas_diagnostics_coa_port():
    payload = request.get_json(silent=True) or request.form
    ip = (payload.get('ip') or '').strip()
    port = int(payload.get('port', 3799) or 3799)
    secret = (payload.get('secret') or '').strip() or None
    from services.nas_diagnostics_service import test_coa_port
    ok, res = test_coa_port(ip, port=port, secret=secret)
    return jsonify({'success': ok, 'data': res, 'message': res})


@nas_bp.route('/api/tools/nas-diagnostics/send-coa-disconnect', methods=['POST'], endpoint="api_nas_diagnostics_coa_disconnect")
def api_nas_diagnostics_coa_disconnect():
    ip = request.form.get('ip', '').strip()
    secret = request.form.get('secret', '').strip() or None
    port = int(request.form.get('port', 3799) or 3799)
    from services.nas_diagnostics_service import send_test_coa_disconnect
    ok, msg = send_test_coa_disconnect(ip, nas_secret=secret, coa_port=port)
    return jsonify({'success': ok, 'message': msg})


@nas_bp.route('/tools/mikrotik-import', endpoint="mikrotik_userman_import_page")
@login_required
def mikrotik_userman_import_page():
    return redirect(url_for('import_data_page', tab='mikrotik'))


@nas_bp.route('/api/tools/mikrotik-import/analyze-rsc', methods=['POST'], endpoint="api_mikrotik_userman_analyze_rsc")
@login_required
def api_mikrotik_userman_analyze_rsc():
    from services.mikrotik_userman_importer import parse_rsc_content
    if 'rsc_file' not in request.files or not request.files['rsc_file'].filename:
        return jsonify({'success': False, 'error': 'لم يتم اختيار ملف'}), 400
    try:
        f = request.files['rsc_file']
        raw_text = f.read().decode('utf-8', errors='ignore')
        parsed = parse_rsc_content(raw_text)
        return jsonify({'success': True, 'data': parsed})
    except Exception as e:
        return jsonify({'success': False, 'error': f'خطأ أثناء تحليل الملف: {str(e)}'}), 500


@nas_bp.route('/api/tools/mikrotik-import/test-api', methods=['POST'], endpoint="api_mikrotik_userman_test_api")
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
        return jsonify({'success': False, 'error': 'يرجى إدخال عنوان IP واسم المستخدم'}), 400

    try:
        parsed = fetch_userman_via_api(host=host, username=user, password=pwd, port=port, use_ssl=use_ssl)
        return jsonify({'success': True, 'data': parsed})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@nas_bp.route('/api/tools/mikrotik-import/execute', methods=['POST'], endpoint="api_mikrotik_userman_execute")
@login_required
def api_mikrotik_userman_execute():
    from services.mikrotik_userman_importer import execute_userman_import
    req_data = request.json or {}
    parsed_data = req_data.get('data') or {}
    target_type = req_data.get('target_type', 'vouchers')
    fallback_package = req_data.get('fallback_package')
    duplicate_action = req_data.get('duplicate_action', 'skip')
    ignore_expired = bool(req_data.get('ignore_expired', True))
    import_consumption = bool(req_data.get('import_consumption', False))
    admin_user = session.get('username', 'admin')

    res = execute_userman_import(
        parsed_data=parsed_data,
        target_type=target_type,
        fallback_package=fallback_package,
        duplicate_action=duplicate_action,
        ignore_expired=ignore_expired,
        import_consumption=import_consumption,
        admin_user=admin_user
    )
    return jsonify(res)


# ==============================================================================
# MIKROTIK HOTSPOT & DYNAMIC PCQ QUEUE GENERATOR TOOL
# ==============================================================================
@nas_bp.route('/tools/hotspot-generator', endpoint="hotspot_generator_page")
@login_required
def hotspot_generator_page():
    import json
    from services.hotspot_generator_service import (
        get_hotspot_generator_settings,
        generate_mikrotik_hotspot_script
    )
    runtime_host = request.host
    settings = get_hotspot_generator_settings(default_host=runtime_host)
    script_text = generate_mikrotik_hotspot_script(settings)
    
    speed_options = []
    if settings.get('portal_speed_options'):
        try:
            speed_options = json.loads(settings.get('portal_speed_options'))
        except Exception:
            speed_options = []

    return render_template(
        'tools/hotspot_generator.html',
        settings=settings,
        speed_options=speed_options,
        script_text=script_text,
        runtime_host=runtime_host
    )


@nas_bp.route('/api/tools/hotspot-generator/save', methods=['POST'], endpoint="api_hotspot_generator_save")
@login_required
def api_hotspot_generator_save():
    from services.hotspot_generator_service import (
        save_hotspot_generator_settings,
        get_hotspot_generator_settings,
        generate_mikrotik_hotspot_script
    )
    req_data = request.json or {}
    save_hotspot_generator_settings(req_data)
    updated_settings = get_hotspot_generator_settings(default_host=request.host)
    updated_script = generate_mikrotik_hotspot_script(updated_settings)
    return jsonify({
        'success': True,
        'settings': updated_settings,
        'script': updated_script
    })


@nas_bp.route('/tools/hotspot-generator/download-zip', endpoint="download_hotspot_generator_zip")
@nas_bp.route('/api/hotspot/package.zip', endpoint="api_download_hotspot_package_zip")
def download_hotspot_generator_zip():
    from services.hotspot_generator_service import (
        get_hotspot_generator_settings,
        generate_hotspot_zip_bytes
    )
    runtime_host = request.host
    settings = get_hotspot_generator_settings(default_host=runtime_host)
    zip_bytes = generate_hotspot_zip_bytes(settings)
    folder_name = settings.get('folder_name', 'max-radius') or 'max-radius'
    
    return Response(
        zip_bytes,
        mimetype='application/zip',
        headers={
            'Content-Disposition': f'attachment; filename={folder_name}.zip'
        }
    )


@nas_bp.route('/tools/hotspot-generator/download-script', endpoint="download_hotspot_generator_script")
@login_required
def download_hotspot_generator_script():
    from services.hotspot_generator_service import (
        get_hotspot_generator_settings,
        generate_mikrotik_hotspot_script
    )
    runtime_host = request.host
    settings = get_hotspot_generator_settings(default_host=runtime_host)
    script_text = generate_mikrotik_hotspot_script(settings)
    folder_name = settings.get('folder_name', 'max-radius') or 'max-radius'
    
    return Response(
        script_text,
        mimetype='text/plain; charset=utf-8',
        headers={
            'Content-Disposition': f'attachment; filename=setup_hotspot_{folder_name}.rsc'
        }
    )


@nas_bp.route('/api/nas-status', endpoint="api_nas_status_compat")
@login_required
def api_nas_status_compat():
    from services.nas_service import get_nas_devices
    devices = get_nas_devices(skip_live_probe=False)
    online_count = sum(1 for d in devices if d.get('is_online'))
    total_users = sum(d.get('active_users', 0) for d in devices)
    return jsonify({
        'success': True,
        'devices': devices,
        'total_routers': len(devices),
        'online_routers': online_count,
        'offline_routers': len(devices) - online_count,
        'total_active_users': total_users
    })


@nas_bp.route('/api/nas/<int:nas_id>/status', endpoint="api_nas_single_status_compat")
@login_required
def api_nas_single_status_compat(nas_id):
    device = query_one('SELECT * FROM wisp_nas_devices WHERE id = ?', (nas_id,))
    if not device:
        return jsonify({'success': False, 'message': 'الراوتر غير موجود.'}), 404
    from core.mikrotik_api import fetch_single_nas_status
    status_data = fetch_single_nas_status(device)
    return jsonify({'success': True, 'data': status_data, 'status': status_data})

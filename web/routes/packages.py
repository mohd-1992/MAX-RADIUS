# -*- coding: utf-8 -*-
"""
MAX RADIUS Web - Package Configuration and Rate Limit Profiles Routes Module.
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
from core.radius_sync import sync_package_to_radius
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

logger = logging.getLogger('packages_bp')

packages_bp = Blueprint('packages_bp', __name__)


@packages_bp.route('/packages', endpoint="packages")

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


@packages_bp.route('/packages/new', methods=['GET'], endpoint="new_package_page")

@packages_bp.route('/packages/add', methods=['GET'], endpoint="new_package_page")

def new_package_page():
    return render_template('package_add.html')


@packages_bp.route('/packages/add', methods=['POST'], endpoint="add_package_action")

@packages_bp.route('/packages/new', methods=['POST'], endpoint="add_package_action")

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


@packages_bp.route('/packages/<int:pkg_id>', endpoint="package_details")

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


@packages_bp.route('/packages/edit/<int:pkg_id>', methods=['POST'], endpoint="edit_package_action")

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


@packages_bp.route('/packages/delete/<int:pkg_id>', methods=['POST'], endpoint="delete_package_action")

def delete_package_action(pkg_id):
    try:
        pkg = query_one('SELECT name FROM wisp_packages WHERE id = ?', (pkg_id,))
        if pkg:
            execute_write('DELETE FROM radgroupreply WHERE groupname = ?', (pkg['name'],))
            execute_write('DELETE FROM radgroupcheck WHERE groupname = ?', (pkg['name'],))
            execute_write('DELETE FROM wisp_packages WHERE id = ?', (pkg_id,))
            flash('تم حذف الباقة بنجاح.', 'info')
    except Exception as e:
        flash(f'خطأ أثناء الحذف: {str(e)}', 'danger')
    return redirect(url_for('packages'))


@packages_bp.route('/packages/clone/<int:pkg_id>', methods=['POST'], endpoint="clone_package_action")

def clone_package_action(pkg_id):
    try:
        pkg = query_one('SELECT * FROM wisp_packages WHERE id = ?', (pkg_id,))
        if not pkg:
            return jsonify({'success': False, 'message': 'الباقة غير موجودة.'}), 404
        
        new_name = f"{pkg['name']}_نسخة"
        counter = 1
        while query_one('SELECT id FROM wisp_packages WHERE name = ?', (new_name,)):
            counter += 1
            new_name = f"{pkg['name']}_نسخة_{counter}"

        new_id = execute_write('''
            INSERT INTO wisp_packages (
                name, service_type, price, cost, rate_download, rate_upload,
                burst_download, burst_upload, burst_threshold_down, burst_threshold_up,
                burst_time, priority, min_download, min_upload, volume_quota_mb,
                uptime_limit_mins, validity_days, validity_value, validity_unit,
                mikrotik_group, simultaneous_sessions, is_active, show_in_portal, is_rollover_enabled, description
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            new_name, pkg.get('service_type', 'hotspot'), pkg.get('price', 0), pkg.get('cost', 0),
            pkg.get('rate_download', '2M'), pkg.get('rate_upload', '1M'),
            pkg.get('burst_download', ''), pkg.get('burst_upload', ''),
            pkg.get('burst_threshold_down', ''), pkg.get('burst_threshold_up', ''),
            pkg.get('burst_time', 16), pkg.get('priority', 8),
            pkg.get('min_download', ''), pkg.get('min_upload', ''),
            pkg.get('volume_quota_mb', 0),
            pkg.get('uptime_limit_mins', 0), pkg.get('validity_days', 30), pkg.get('validity_value', 30), pkg.get('validity_unit', 'days'),
            pkg.get('mikrotik_group', ''), pkg.get('simultaneous_sessions', 1),
            pkg.get('is_active', 1), pkg.get('show_in_portal', 1), pkg.get('is_rollover_enabled', 0),
            f"نسخة مكررة من {pkg['name']}"
        ))
        sync_package_to_radius(new_id)
        if request.is_json or 'application/json' in request.headers.get('Accept', ''):
            return jsonify({'success': True, 'message': f'تم تكرار الباقة باسم [{new_name}] بنجاح.', 'new_id': new_id})
        flash(f'تم تكرار الباقة باسم [{new_name}] بنجاح.', 'success')
        return redirect(url_for('packages'))
    except Exception as e:
        if request.is_json or 'application/json' in request.headers.get('Accept', ''):
            return jsonify({'success': False, 'message': str(e)}), 500
        flash(f'خطأ أثناء تكرار الباقة: {str(e)}', 'danger')
        return redirect(url_for('packages'))


@packages_bp.route('/packages/bulk-action', methods=['POST'], endpoint="packages_bulk_action")

def packages_bulk_action():
    try:
        data = request.get_json(silent=True) or request.form
        action = data.get('action', '').strip()
        raw_ids = data.get('package_ids', [])
        if isinstance(raw_ids, str):
            import json as _json
            try:
                raw_ids = _json.loads(raw_ids)
            except Exception:
                raw_ids = [int(x.strip()) for x in raw_ids.split(',') if x.strip().isdigit()]
        
        pkg_ids = [int(i) for i in raw_ids if str(i).isdigit()]
        if not pkg_ids:
            return jsonify({'success': False, 'message': 'يرجى تحديد باقة واحدة على الأقل.'}), 400

        count = len(pkg_ids)
        placeholders = ','.join(['?'] * count)

        if action == 'delete':
            # 1. Fetch names to clean RADIUS groups
            pkgs = query_all(f"SELECT name FROM wisp_packages WHERE id IN ({placeholders})", tuple(pkg_ids))
            for p in pkgs:
                execute_write('DELETE FROM radgroupreply WHERE groupname = ?', (p['name'],))
                execute_write('DELETE FROM radgroupcheck WHERE groupname = ?', (p['name'],))
            execute_write(f"DELETE FROM wisp_packages WHERE id IN ({placeholders})", tuple(pkg_ids))
            msg = f'تم حذف {count} باقة محددة بنجاح.'

        elif action == 'activate':
            execute_write(f"UPDATE wisp_packages SET is_active = 1 WHERE id IN ({placeholders})", tuple(pkg_ids))
            msg = f'تم تفعيل {count} باقة محددة بنجاح.'

        elif action == 'deactivate':
            execute_write(f"UPDATE wisp_packages SET is_active = 0 WHERE id IN ({placeholders})", tuple(pkg_ids))
            msg = f'تم تعطيل {count} باقة محددة بنجاح.'

        elif action == 'update_price':
            price_mode = str(data.get('price_mode') or data.get('price_type') or 'fixed').strip().lower()
            if price_mode == 'percentage_increase':
                price_mode = 'increase_percent'
            elif price_mode == 'percentage_discount':
                price_mode = 'discount_percent'
            elif price_mode == 'amount_increase':
                price_mode = 'increase_amount'
            elif price_mode == 'amount_discount':
                price_mode = 'discount_amount'

            val = float(data.get('price_value', 0))
            if price_mode == 'fixed':
                execute_write(f"UPDATE wisp_packages SET price = ? WHERE id IN ({placeholders})", (val, *pkg_ids))
                msg = f'تم تعيين السعر {val} لـ {count} باقة محددة.'
            elif price_mode == 'increase_percent':
                execute_write(f"UPDATE wisp_packages SET price = ROUND(price * (1 + ? / 100), 2) WHERE id IN ({placeholders})", (val, *pkg_ids))
                msg = f'تمت زيادة أسعار {count} باقة بنسبة {val}%.'
            elif price_mode == 'discount_percent':
                execute_write(f"UPDATE wisp_packages SET price = ROUND(GREATEST(0, price * (1 - ? / 100)), 2) WHERE id IN ({placeholders})", (val, *pkg_ids))
                msg = f'تم خصم {val}% من أسعار {count} باقة محددة.'
            elif price_mode == 'increase_amount':
                execute_write(f"UPDATE wisp_packages SET price = ROUND(price + ?, 2) WHERE id IN ({placeholders})", (val, *pkg_ids))
                msg = f'تمت إضافة {val} إلى أسعار {count} باقة محددة.'
            elif price_mode == 'discount_amount':
                execute_write(f"UPDATE wisp_packages SET price = ROUND(GREATEST(0, price - ?), 2) WHERE id IN ({placeholders})", (val, *pkg_ids))
                msg = f'تم خصم {val} من أسعار {count} باقة محددة.'

        elif action == 'update_validity':
            val = int(data.get('validity_value', 30))
            unit = str(data.get('validity_unit', 'days')).strip()
            equiv_days = 0 if val <= 0 else (val * 30 if unit == 'months' else (max(1, round(val / 24)) if unit == 'hours' else (max(1, round(val / 1440)) if unit == 'minutes' else val)))
            execute_write(f"UPDATE wisp_packages SET validity_value = ?, validity_unit = ?, validity_days = ? WHERE id IN ({placeholders})", (val, unit, equiv_days, *pkg_ids))
            msg = f'تم تعديل الصلاحية إلى ({val} {unit}) لـ {count} باقة محددة.'

        elif action == 'update_service_type':
            stype = str(data.get('service_type') or data.get('target_service_type') or 'hotspot').strip().lower()
            if stype not in ('hotspot', 'pppoe', 'both'):
                stype = 'hotspot'
            execute_write(f"UPDATE wisp_packages SET service_type = ? WHERE id IN ({placeholders})", (stype, *pkg_ids))
            msg = f'تم تعديل نوع الخدمة إلى [{stype}] لـ {count} باقة محددة.'

        else:
            return jsonify({'success': False, 'message': 'إجراء غير معروف.'}), 400

        # Sync all updated packages to radius
        for pid in pkg_ids:
            try:
                sync_package_to_radius(pid)
            except Exception:
                pass

        if request.is_json:
            return jsonify({'success': True, 'message': msg, 'count': count})
        flash(msg, 'success')
        return redirect(url_for('packages'))

    except Exception as e:
        if request.is_json:
            return jsonify({'success': False, 'message': str(e)}), 500
        flash(f'خطأ أثناء تنفيذ الإجراء الجماعي: {str(e)}', 'danger')
        return redirect(url_for('packages'))



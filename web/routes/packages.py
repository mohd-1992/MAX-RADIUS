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
from collections.abc import Mapping

from flask import (
    Blueprint, render_template, request, jsonify, redirect,
    url_for, send_file, flash, session, current_app, Response
)

from database.db import (
    query_all, query_one, execute_write, execute_update, execute_many,
    log_audit, log_user_audit, DB_PATH, db_session, adapt_query
)
from core.config import (
    DB_TYPE, APP_NAME, APP_VERSION, APP_EDITION, APP_VERSION_FULL, APP_VERSION_BADGE,
    STORAGE_DIR, BACKUPS_DIR, UPLOADS_DIR, LOGS_DIR
)
from core.rate_limit import format_bytes, format_duration, build_mikrotik_rate_limit
from core.radius_sync import sync_package_to_radius
from services.package_validation import validate_package_form, nonnegative_number, integer
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

from core.ownership import scoped_query_all as query_all, scoped_query_one as query_one

packages_bp = Blueprint('packages_bp', __name__)


def package_write(conn, sql, params=()):
    cursor = conn.cursor()
    try:
        cursor.execute(adapt_query(sql, conn), params)
        return cursor.lastrowid
    finally:
        cursor.close()


@packages_bp.before_request
def guard_package_permissions():
    permission = 'packages.manage' if request.method == 'POST' or request.endpoint.endswith('new_package_page') else 'packages.view'
    return require_permission(permission)(lambda: None)()


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
        f = validate_package_form(request.form)
        val = f['validity_value']
        unit = f.get('validity_unit', 'days').strip()
        equiv_days = 0 if val <= 0 else (val * 30 if unit == 'months' else (max(1, round(val / 24)) if unit == 'hours' else (max(1, round(val / 1440)) if unit == 'minutes' else val)))
        mikrotik_group = f.get('mikrotik_group', '').strip()

        is_loyalty = 1 if f.get('is_loyalty_enabled') in ('1', 'on', 'true', True, 1) else 0
        loyalty_pts = int(f.get('loyalty_points') or 0) if is_loyalty else 0

        with db_session() as conn:
            pkg_id = package_write(conn, '''
                INSERT INTO wisp_packages (
                    name, service_type, price, cost, rate_download, rate_upload,
                    burst_download, burst_upload, burst_threshold_down, burst_threshold_up,
                    burst_time, priority, min_download, min_upload, volume_quota_mb,
                    uptime_limit_mins, validity_days, validity_value, validity_unit,
                    mikrotik_group, simultaneous_sessions, is_active, show_in_portal, is_rollover_enabled,
                    is_loyalty_enabled, loyalty_points, description
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                f['name'].strip(), f.get('service_type', 'hotspot'), float(f.get('price', 0)), float(f.get('cost', 0)),
                f.get('rate_download', '2M').strip(), f.get('rate_upload', '1M').strip(),
                f.get('burst_download', '').strip(), f.get('burst_upload', '').strip(),
                f.get('burst_threshold_down', '').strip(), f.get('burst_threshold_up', '').strip(),
                int(f.get('burst_time', 16)), int(f.get('priority', 8)),
                f.get('min_download', '').strip(), f.get('min_upload', '').strip(),
                int(f.get('volume_quota_mb', 0)),
                f['uptime_limit_mins'],
                equiv_days, val, unit,
                mikrotik_group,
                int(f.get('simultaneous_sessions', 1)),
                1 if f.get('is_active') in ('1', 'on', 'true', True, 1) else 0,
                1 if f.get('show_in_portal') in ('1', 'on', 'true', True, 1) else 0,
                1 if f.get('is_rollover_enabled') in ('1', 'on', 'true', True, 1) else 0,
                is_loyalty, loyalty_pts,
                f.get('description', '')
            ))
            sync_package_to_radius(pkg_id, conn=conn)
        flash('تم حفظ وإنشاء الباقة بنجاح ومزامنة سمات MikroTik و FreeRADIUS.', 'success')
    except ValueError as e:
        flash(str(e), 'danger')
    except Exception:
        logger.exception('Package creation failed')
        flash('تعذر حفظ الباقة؛ لم يتم اعتماد التغييرات.', 'danger')
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
        
        form = request.form.to_dict()
        for field in ('min_download', 'min_upload', 'uptime_limit_mins'):
            form.setdefault(field, old_pkg.get(field) or '')
        f = validate_package_form(form)
        new_name = f['name'].strip()
        old_name = old_pkg['name']
        val = f['validity_value']
        unit = f.get('validity_unit', 'days').strip()
        equiv_days = 0 if val <= 0 else (val * 30 if unit == 'months' else (max(1, round(val / 24)) if unit == 'hours' else (max(1, round(val / 1440)) if unit == 'minutes' else val)))
        mikrotik_group = f.get('mikrotik_group', '').strip()
        
        is_loyalty = 1 if f.get('is_loyalty_enabled') in ('1', 'on', 'true', True, 1) else 0
        loyalty_pts = int(f.get('loyalty_points') or 0) if is_loyalty else 0

        with db_session() as conn:
            package_write(conn, '''
                UPDATE wisp_packages SET
                    name = ?, service_type = ?, price = ?, cost = ?,
                    rate_download = ?, rate_upload = ?, burst_download = ?, burst_upload = ?,
                    burst_threshold_down = ?, burst_threshold_up = ?, burst_time = ?,
                    priority = ?, min_download = ?, min_upload = ?, volume_quota_mb = ?,
                    uptime_limit_mins = ?, validity_days = ?, validity_value = ?, validity_unit = ?,
                    mikrotik_group = ?, simultaneous_sessions = ?, is_active = ?, show_in_portal = ?, is_rollover_enabled = ?,
                    is_loyalty_enabled = ?, loyalty_points = ?, description = ?
                WHERE id = ?
            ''', (
                new_name, f.get('service_type', 'hotspot'), float(f.get('price', 0)), float(f.get('cost', 0)),
                f.get('rate_download', '2M').strip(), f.get('rate_upload', '1M').strip(),
                f.get('burst_download', '').strip(), f.get('burst_upload', '').strip(),
                f.get('burst_threshold_down', '').strip(), f.get('burst_threshold_up', '').strip(),
                int(f.get('burst_time', 16)), int(f.get('priority', 8)),
                f.get('min_download', '').strip(), f.get('min_upload', '').strip(),
                int(f.get('volume_quota_mb', 0)),
                f['uptime_limit_mins'],
                equiv_days, val, unit,
                mikrotik_group,
                int(f.get('simultaneous_sessions', 1)),
                1 if f.get('is_active') in ('1', 'on', 'true', True, 1) else 0,
                1 if f.get('show_in_portal') in ('1', 'on', 'true', True, 1) else 0,
                1 if f.get('is_rollover_enabled') in ('1', 'on', 'true', True, 1) else 0,
                is_loyalty, loyalty_pts,
                f.get('description', ''), pkg_id
            ))
        
            if new_name != old_name:
                package_write(conn, 'UPDATE radusergroup SET groupname = ? WHERE groupname = ?', (new_name, old_name))
                package_write(conn, 'DELETE FROM radgroupreply WHERE groupname = ?', (old_name,))
                package_write(conn, 'DELETE FROM radgroupcheck WHERE groupname = ?', (old_name,))
            
            sync_package_to_radius(pkg_id, conn=conn)
        flash('تم حفظ تعديلات الباقة ومزامنة سمات FreeRADIUS وميكروتيك بنجاح.', 'success')
    except ValueError as e:
        flash(str(e), 'danger')
    except Exception:
        logger.exception('Package update failed')
        flash('تعذر حفظ الباقة؛ لم يتم اعتماد التغييرات.', 'danger')
        
    return redirect(url_for('package_details', pkg_id=pkg_id))


@packages_bp.route('/packages/delete/<int:pkg_id>', methods=['POST'], endpoint="delete_package_action")

def delete_package_action(pkg_id):
    try:
        from services.account_lifecycle_service import delete_packages
        count = delete_packages([pkg_id])
        flash('تم حذف الباقة.' if count else 'الباقة غير موجودة.', 'info')
    except Exception as exc:
        flash(str(exc), 'danger')
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

        with db_session() as conn:
            new_id = package_write(conn, '''
                INSERT INTO wisp_packages (
                    name, service_type, price, cost, rate_download, rate_upload,
                    burst_download, burst_upload, burst_threshold_down, burst_threshold_up,
                    burst_time, priority, min_download, min_upload, volume_quota_mb,
                    uptime_limit_mins, validity_days, validity_value, validity_unit,
                    mikrotik_group, simultaneous_sessions, is_active, show_in_portal, is_rollover_enabled,
                    is_loyalty_enabled, loyalty_points, description
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                pkg.get('is_loyalty_enabled', 0), pkg.get('loyalty_points', 0),
                pkg.get('description', '')
            ))
            sync_package_to_radius(new_id, conn=conn)
        if request.is_json or 'application/json' in request.headers.get('Accept', ''):
            return jsonify({'success': True, 'message': f'تم تكرار الباقة باسم [{new_name}] بنجاح.', 'new_id': new_id})
        flash(f'تم تكرار الباقة باسم [{new_name}] بنجاح.', 'success')
        return redirect(url_for('packages'))
    except Exception:
        logger.exception('Package clone failed')
        if request.is_json or 'application/json' in request.headers.get('Accept', ''):
            return jsonify({'success': False, 'message': 'تعذر نسخ الباقة؛ لم يتم إنشاء نسخة جزئية.'}), 500
        flash('تعذر نسخ الباقة؛ لم يتم إنشاء نسخة جزئية.', 'danger')
        return redirect(url_for('packages'))


@packages_bp.route('/packages/bulk-action', methods=['POST'], endpoint="packages_bulk_action")

def packages_bulk_action():
    try:
        data = request.get_json(silent=True) or request.form
        if not isinstance(data, Mapping):
            raise ValueError('بيانات الطلب غير صالحة.')
        action = str(data.get('action', '')).strip()
        raw_ids = data.get('package_ids', [])
        if isinstance(raw_ids, str):
            import json as _json
            try:
                raw_ids = _json.loads(raw_ids)
            except Exception:
                raw_ids = [int(x.strip()) for x in raw_ids.split(',') if x.strip().isdigit()]
        
        if not isinstance(raw_ids, list):
            raise ValueError('قائمة الباقات غير صالحة.')
        pkg_ids = list(dict.fromkeys(integer(i, 1) for i in raw_ids))
        if not pkg_ids:
            return jsonify({'success': False, 'message': 'يرجى تحديد باقة واحدة على الأقل.'}), 400

        count = len(pkg_ids)
        placeholders = ','.join(['?'] * count)
        existing = query_all(f'SELECT id FROM wisp_packages WHERE id IN ({placeholders})', tuple(pkg_ids))
        if len(existing) != count:
            return jsonify(success=False, message='بعض الباقات لم تعد موجودة؛ حدّث الصفحة وأعد التحديد.'), 409

        if action == 'delete':
            from services.account_lifecycle_service import delete_packages
            deleted = delete_packages(pkg_ids)
            count = deleted
            msg = f'تم حذف {deleted} باقة غير مرتبطة بنجاح.'

        elif action == 'activate':
            execute_write(f"UPDATE wisp_packages SET is_active = 1 WHERE id IN ({placeholders})", tuple(pkg_ids))
            msg = f'تم تفعيل {count} باقة محددة بنجاح.'

        elif action == 'deactivate':
            execute_write(f"UPDATE wisp_packages SET is_active = 0 WHERE id IN ({placeholders})", tuple(pkg_ids))
            msg = f'تم تعطيل {count} باقة محددة بنجاح.'

        elif action in ('update_rollover', 'update_loyalty', 'update_portal_visibility'):
            enabled = integer(data.get('enabled'), 0, 1)
            columns = {'update_rollover': 'is_rollover_enabled', 'update_loyalty': 'is_loyalty_enabled', 'update_portal_visibility': 'show_in_portal'}
            if action == 'update_loyalty':
                points = integer(data.get('loyalty_points', 0)) if enabled else 0
                execute_write(f'UPDATE wisp_packages SET is_loyalty_enabled = ?, loyalty_points = ? WHERE id IN ({placeholders})', (enabled, points, *pkg_ids))
            else:
                execute_write(f'UPDATE wisp_packages SET {columns[action]} = ? WHERE id IN ({placeholders})', (enabled, *pkg_ids))
            msg = f'تم تحديث إعدادات {count} باقة محددة.'

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

            if price_mode not in ('fixed', 'increase_percent', 'discount_percent', 'increase_amount', 'discount_amount'):
                raise ValueError('طريقة تعديل السعر غير صالحة.')
            val = nonnegative_number(data.get('price_value', 0))
            if price_mode == 'discount_percent' and val > 100:
                raise ValueError('نسبة الخصم يجب ألا تتجاوز 100%.')
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
            val = integer(data.get('validity_value', 30), maximum=71582788)
            unit = str(data.get('validity_unit', 'days')).strip()
            if unit not in ('days', 'hours', 'minutes', 'months'):
                raise ValueError('وحدة الصلاحية غير صالحة.')
            equiv_days = 0 if val <= 0 else (val * 30 if unit == 'months' else (max(1, round(val / 24)) if unit == 'hours' else (max(1, round(val / 1440)) if unit == 'minutes' else val)))
            execute_write(f"UPDATE wisp_packages SET validity_value = ?, validity_unit = ?, validity_days = ? WHERE id IN ({placeholders})", (val, unit, equiv_days, *pkg_ids))
            msg = f'تم تعديل الصلاحية إلى ({val} {unit}) لـ {count} باقة محددة.'

        elif action == 'update_service_type':
            stype = str(data.get('service_type') or data.get('target_service_type') or 'hotspot').strip().lower()
            if stype not in ('hotspot', 'pppoe', 'both'):
                raise ValueError('نوع الخدمة غير صالح.')
            execute_write(f"UPDATE wisp_packages SET service_type = ? WHERE id IN ({placeholders})", (stype, *pkg_ids))
            msg = f'تم تعديل نوع الخدمة إلى [{stype}] لـ {count} باقة محددة.'

        else:
            return jsonify({'success': False, 'message': 'إجراء غير معروف.'}), 400

        # These settings do not change RADIUS attributes or existing contracts.

        if request.is_json:
            return jsonify({'success': True, 'message': msg, 'count': count})
        flash(msg, 'success')
        return redirect(url_for('packages'))

    except (ValueError, TypeError) as e:
        if request.is_json:
            return jsonify(success=False, message=str(e)), 400
        flash(str(e), 'danger')
        return redirect(url_for('packages'))
    except Exception:
        logger.exception('Package bulk action failed')
        if request.is_json:
            return jsonify({'success': False, 'message': 'تعذر حفظ إعدادات الباقات؛ تحقق من الخادم.'}), 500
        flash('تعذر حفظ إعدادات الباقات؛ تحقق من الخادم.', 'danger')
        return redirect(url_for('packages'))



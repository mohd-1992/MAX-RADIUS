# -*- coding: utf-8 -*-
"""
MAX RADIUS Web - Subscriber Management, Invoices, and Profiles Routes Module.
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

logger = logging.getLogger('subscribers_bp')

subscribers_bp = Blueprint('subscribers_bp', __name__)


@subscribers_bp.route('/subscribers', endpoint="subscribers")

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


@subscribers_bp.route('/subscribers/create', methods=['GET', 'POST'], endpoint="subscriber_create_page")

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


@subscribers_bp.route('/subscribers/add', methods=['POST'], endpoint="add_subscriber_action")

def add_subscriber_action():
    try:
        sub_id = create_subscriber(request.form, admin_username=session.get('admin_username', 'admin'))
        flash('تمت إضافة المشترك ومزامنته مع FreeRADIUS بنجاح.', 'success')
        return redirect(url_for('subscriber_details', sub_id=sub_id))
    except Exception as e:
        flash(f'خطأ أثناء إضافة المشترك: {str(e)}', 'danger')
        return redirect(url_for('subscribers'))


@subscribers_bp.route('/subscribers/<int:sub_id>', endpoint="subscriber_details")

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


@subscribers_bp.route('/subscribers/edit/<int:sub_id>', methods=['POST'], endpoint="edit_subscriber_action")

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


@subscribers_bp.route('/subscribers/delete/<int:sub_id>', methods=['POST'], endpoint="delete_subscriber_action")

def delete_subscriber_action(sub_id):
    try:
        delete_subscriber(sub_id)
        flash('تم حذف المشترك وإلغاء صلاحياته من RADIUS.', 'warning')
    except Exception as e:
        flash(f'خطأ أثناء الحذف: {str(e)}', 'danger')
    return redirect(url_for('subscribers'))


@subscribers_bp.route('/subscribers/sessions/<username>', endpoint="subscriber_sessions")

def subscriber_sessions(username):
    sessions = get_subscriber_sessions(username)
    return jsonify({'username': username, 'sessions': sessions})


@subscribers_bp.route('/api/subscribers/disconnect', methods=['POST'], endpoint="api_disconnect_subscriber")

def api_disconnect_subscriber():
    data = request.get_json() or request.form
    username = data.get('username')
    if not username:
        return jsonify({'success': False, 'message': 'اسم المشترك مطلوب.'}), 400
    res = disconnect_subscriber_session(username)
    return jsonify(res)


@subscribers_bp.route('/invoices', endpoint="invoices")

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


@subscribers_bp.route('/invoices/create', methods=['POST'], endpoint="create_invoice_action")

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


@subscribers_bp.route('/invoices/pay/<int:inv_id>', methods=['POST'], endpoint="pay_invoice_action")

def pay_invoice_action(inv_id):
    try:
        execute_write('''
            UPDATE wisp_invoices SET status = 'paid', paid_at = CURRENT_TIMESTAMP WHERE id = ?
        ''', (inv_id,))
        flash('تم تسجيل سداد الفاتورة بنجاح.', 'success')
    except Exception as e:
        flash(f'خطأ: {str(e)}', 'danger')
    return redirect(url_for('invoices'))


@subscribers_bp.route('/invoices/design', methods=['POST'], endpoint="save_invoice_design_action")

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



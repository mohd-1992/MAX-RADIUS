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
from decimal import Decimal, InvalidOperation

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

from web.access_policy import filter_account_money

from core.ownership import scoped_query_all as query_all, scoped_query_one as query_one, owner_id, generation_owner

subscribers_bp = Blueprint('subscribers_bp', __name__)

@subscribers_bp.route('/api/subscribers/<int:account_id>/mac-policy', methods=['GET', 'POST'], endpoint='api_subscriber_mac_policy')
@login_required
def api_subscriber_mac_policy(account_id):
    from web.mac_policy_routes import account_mac_response
    return account_mac_response('subscriber', account_id)

@subscribers_bp.before_request
def guard_subscriber_permissions():
    endpoint = (request.endpoint or '').split('.')[-1]
    permission = {
        'subscribers': 'subscribers.view', 'subscriber_details': 'subscribers.view',
        'subscriber_sessions': 'subscribers.view', 'subscriber_sessions_pdf': 'subscribers.view',
        'subscriber_create_page': 'subscribers.create', 'add_subscriber_action': 'subscribers.create',
        'edit_subscriber_action': 'subscribers.edit', 'delete_subscriber_action': 'subscribers.delete',
        'api_disconnect_subscriber': 'subscribers.disconnect',
        'invoices': 'invoices.view', 'create_invoice_action': 'invoices.manage',
        'pay_invoice_action': 'invoices.manage', 'save_invoice_design_action': 'invoices.manage',
    }.get(endpoint)
    manager = get_current_manager()
    if permission and not manager:
        return jsonify(success=False, message='يرجى تسجيل الدخول أولًا.'), 401
    if permission and not has_permission(permission, manager):
        return jsonify(success=False, message='لا تملك صلاحية هذا الإجراء.'), 403


@subscribers_bp.route('/subscribers', endpoint="subscribers")

def subscribers():
    from services.list_sort_service import sort_state, sort_rows
    list_sort = sort_state(request.args, 'subscribers')
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
    return render_template('subscribers.html', list_sort=list_sort, subscribers=sort_rows([filter_account_money(row) for row in subs], list_sort), packages=packages, q=search, type=stype, status=status, status_counts=status_counts, package_id=package_id)


@subscribers_bp.route('/subscribers/create', methods=['GET', 'POST'], endpoint="subscriber_create_page")

def subscriber_create_page():
    if request.method == 'POST':
        try:
            sub_id = create_subscriber(request.form, admin_username=get_current_manager().get('username') or 'admin')
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
        sub_id = create_subscriber(request.form, admin_username=get_current_manager().get('username') or 'admin')
        flash('تمت إضافة المشترك ومزامنته مع FreeRADIUS بنجاح.', 'success')
        return redirect(url_for('subscriber_details', sub_id=sub_id))
    except Exception as e:
        flash(f'خطأ أثناء إضافة المشترك: {str(e)}', 'danger')
        return redirect(url_for('subscribers'))


@subscribers_bp.route('/subscribers/<int:sub_id>', endpoint="subscriber_details")

def subscriber_details(sub_id):
    sub = query_one('''
        SELECT s.*, p.name as package_name, p.price as package_price, p.rate_download, p.rate_upload,
               p.volume_quota_mb, p.validity_value, p.validity_unit, p.validity_days
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
        FROM radacct_owned
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
        FROM radacct_owned
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

    from services.quota_service import calculate_account_quota
    q_info = calculate_account_quota(sub['id'], entity_type='subscriber')
    if q_info:
        down_bytes = q_info['used_down_bytes']
        up_bytes = q_info['used_up_bytes']
        total_used_bytes = q_info['used_total_bytes']
        total_download = format_bytes(down_bytes)
        total_upload = format_bytes(up_bytes)
        total_used_str = format_bytes(total_used_bytes)
        if not q_info['is_quota_unlimited']:
            quota_bytes = int(q_info['total_quota_mb'] * 1024 * 1024)
            remaining_bytes = q_info['remaining_bytes']
            quota_str = format_bytes(quota_bytes)
            remaining_str = format_bytes(remaining_bytes)
            usage_percent = q_info['quota_used_pct']
        else:
            quota_str = 'غير محدود'
            remaining_str = 'غير محدود'
            usage_percent = 0
    else:
        down_bytes = 0
        up_bytes = 0
        total_used_bytes = 0
        total_download = '0 B'
        total_upload = '0 B'
        total_used_str = '0 B'
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

    from services.session_history_service import get_session_history
    session_history = get_session_history(sub['username'], page=max(1, request.args.get('session_page', 1, type=int)))
    sessions = session_history['rows']
    invoices = query_all('SELECT * FROM wisp_invoices WHERE subscriber_id = ? OR LOWER(subscriber_name) = LOWER(?) ORDER BY id DESC LIMIT 50', (sub_id, sub['username'])) if has_permission('invoices.view') else []
    packages = query_all('SELECT id,name,price,service_type,rate_download,rate_upload,volume_quota_mb,validity_value,validity_unit,validity_days FROM wisp_packages WHERE is_active = 1')
    audit_logs = get_user_audit_logs('subscriber', sub_id, sub['username'])
    analytics = get_user_usage_analytics(sub['username'])

    from services.subscriber_edit_service import revision
    from services.account_mac_service import get_mac_policy
    return render_template('subscriber_details.html',
                           mac_policy=get_mac_policy('subscriber', sub['id']),
                           edit_revision=revision(sub), edit_operation_id=secrets.token_hex(16), package_operation_id=secrets.token_hex(16),
                           subscriber=filter_account_money(sub),
                           total_download=total_download,
                           total_upload=total_upload,
                           total_used_str=total_used_str,
                           quota_str=quota_str,
                           remaining_str=remaining_str,
                           usage_percent=usage_percent,
                           remaining_days_text=remaining_days_text,
                           is_expired=is_expired,
                           sessions=sessions, session_history=session_history,
                           invoices=invoices,
                           audit_logs=audit_logs if has_permission('system.audit_logs') else [],
                           analytics=analytics,
                           analytics_json=json.dumps(analytics, default=str),
                           packages=packages)


@subscribers_bp.route('/subscribers/edit/<int:sub_id>', methods=['POST'], endpoint="edit_subscriber_action")

def edit_subscriber_action(sub_id):
    try:
        admin_user = get_current_manager().get('username') or 'admin'
        update_subscriber(sub_id, request.form, admin_username=admin_user)
        flash('تم تحديث بيانات المشترك ومزامنته مع FreeRADIUS بنجاح.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء التعديل: {str(e)}', 'danger')
    if request.form.get('return_to_details'):
        return redirect(url_for('subscriber_details', sub_id=sub_id))
    return redirect(url_for('subscribers'))


@subscribers_bp.route('/subscribers/<int:sub_id>/edit-contract', methods=['POST'], endpoint='edit_subscriber_contract')
@login_required
def edit_subscriber_contract(sub_id):
    from web.access_policy import can_access_account_action
    if not has_permission('subscribers.edit'):
        return jsonify(success=False,message='لا تملك صلاحية تعديل المشتركين.'),403
    from services.subscriber_edit_service import edit_subscriber
    from services.renewal_settlement_service import SettlementPending
    try:
        result=edit_subscriber(sub_id,request.form,actor=get_current_manager().get('username') or 'admin',
                               can_change_expiry=has_permission('subscribers.renew') and can_access_account_action('extend-time','subscriber'))
        return jsonify(result)
    except ValueError as exc:
        return jsonify(success=False,message=str(exc)),400
    except SettlementPending as exc:
        return jsonify(success=False,message=str(exc)),409
    except Exception:
        current_app.logger.exception('Subscriber edit failed: subscriber_id=%s',sub_id)
        return jsonify(success=False,message='تعذر تأكيد حفظ التعديل. حدّث صفحة المشترك للتحقق قبل إعادة العملية.'),500


@subscribers_bp.route('/subscribers/<int:sub_id>/edit-package', methods=['POST'], endpoint='edit_subscriber_package')
@login_required
def edit_subscriber_package(sub_id):
    from web.access_policy import can_access_account_action
    if not has_permission('subscribers.edit') or not has_permission('subscribers.renew') or not can_access_account_action('change-package','subscriber'):
        return jsonify(success=False,message='تغيير الباقة يحتاج صلاحية تعديل وتجديد المشتركين.'),403
    try:
        package_id=int(request.form.get('new_package_id',''))
        if package_id<=0:raise ValueError()
        from services.subscriber_edit_service import receipt,fingerprint
        actor=get_current_manager().get('username') or 'admin'
        rollover=request.form.get('enable_rollover') in ('1','true','on')
        operation_id=request.form.get('operation_id','')
        # Reject missing/invalid operation IDs before entering the existing service.
        receipt(operation_id,sub_id,actor,fingerprint('package',sub_id,actor,{'package_id':package_id,'rollover':rollover}))
        success,message=action_change_package('subscriber',sub_id,package_id,enable_rollover=rollover,admin_username=actor,operation_id=operation_id)
        return jsonify(success=success,message=message),(200 if success else 409)
    except ValueError:
        return jsonify(success=False,message='اختر باقة صحيحة وأعد فتح الصفحة إذا كان طلب التعديل منتهيًا.'),400
    except Exception:
        current_app.logger.exception('Subscriber package edit failed: subscriber_id=%s',sub_id)
        return jsonify(success=False,message='تعذر تأكيد نتيجة تغيير الباقة. راجع الحالة قبل تكرار الطلب.'),500


@subscribers_bp.route('/subscribers/delete/<int:sub_id>', methods=['POST'], endpoint="delete_subscriber_action")

def delete_subscriber_action(sub_id):
    try:
        if not delete_subscriber(sub_id, admin_username=get_current_manager().get('username') or 'admin'):
            raise ValueError("المشترك غير موجود أو لم يكتمل الحذف")
        flash('تم حذف المشترك وإلغاء صلاحياته من RADIUS.', 'warning')
    except Exception as e:
        flash(f'خطأ أثناء الحذف: {str(e)}', 'danger')
    return redirect(url_for('subscribers'))


@subscribers_bp.route('/subscribers/sessions/<username>', endpoint="subscriber_sessions")

def subscriber_sessions(username):
    if not query_one('SELECT id FROM wisp_subscribers WHERE LOWER(username) = LOWER(?)', (username,)):
        return jsonify(success=False, message='المشترك غير موجود.'), 404
    sessions = get_subscriber_sessions(username)
    return jsonify({'username': username, 'sessions': sessions})


@subscribers_bp.route('/api/subscribers/disconnect', methods=['POST'], endpoint="api_disconnect_subscriber")
def api_disconnect_subscriber():
    data = request.get_json(silent=True) or request.form
    username = data.get('username')
    if not username:
        return jsonify({'success': False, 'message': 'اسم المشترك مطلوب.'}), 400
    row=query_one('SELECT id,username FROM wisp_subscribers WHERE LOWER(username)=LOWER(?)',(username,))
    if not row:
        return jsonify(success=False, message='المشترك غير موجود.'), 404
    from services.session_control_service import submit_manual
    try:
        result=submit_manual('subscriber',row['id'],row['username'],get_current_manager().get('username') or 'admin',data.get('operation_id'))
    except (ValueError,RuntimeError,OSError):
        return jsonify(success=False,message='تعذر قبول طلب الفصل؛ لم يُؤكد أي فصل.'),409
    return jsonify(success=True,**result)


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
            SELECT s.full_name, s.username, p.name as package_name
            FROM wisp_subscribers s
            JOIN wisp_packages p ON s.package_id = p.id
            WHERE s.id = ?
        ''', (sub_id,))
        if not sub:
            raise ValueError('المشترك غير موجود.')
        sub_name = sub['full_name']
        pkg_name = sub['package_name'] if sub else 'باقة إنترنت'
        try:
            amount = Decimal(request.form['amount'])
            if not amount.is_finite() or amount < 0 or amount > Decimal('99999999.99') or amount != amount.quantize(Decimal('0.01')):
                raise ValueError()
        except (InvalidOperation, ValueError, TypeError):
            raise ValueError('مبلغ الفاتورة غير صالح.')
        status = request.form.get('status', 'unpaid')
        if status not in ('paid', 'unpaid'):
            raise ValueError('حالة الفاتورة غير صالحة.')
        due_date = request.form.get('due_date') or None
        notes = request.form.get('notes', '')
        inv_num = f"INV-{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(8)}"

        from database.db import db_session, adapt_query
        from services.sales_source_service import record_sale_source
        with db_session() as conn:
            with conn.cursor() as cur:
                cur.execute(adapt_query("""INSERT INTO wisp_invoices
                    (invoice_number,subscriber_id,subscriber_name,package_name,amount,status,due_date,notes,paid_at)
                    VALUES (?,?,?,?,?,?,?,?,CASE WHEN ?='paid' THEN CURRENT_TIMESTAMP ELSE NULL END)""", conn),
                    (inv_num,sub_id,sub_name,pkg_name,amount,status,due_date,notes,status))
                ident = cur.lastrowid
            if status == 'paid' and sub:
                record_sale_source(conn,'invoice',ident,sub['username'])
        flash(f'تم إصدار الفاتورة {inv_num} بنجاح.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء إنشاء الفاتورة: {str(e)}', 'danger')
    return redirect(url_for('invoices'))


@subscribers_bp.route('/invoices/pay/<int:inv_id>', methods=['POST'], endpoint="pay_invoice_action")

def pay_invoice_action(inv_id):
    try:
        from database.db import db_session, adapt_query
        from services.sales_source_service import record_sale_source
        with db_session() as conn:
            with conn.cursor() as cur:
                cur.execute(adapt_query("SELECT i.status,s.username FROM wisp_invoices i LEFT JOIN wisp_subscribers s ON s.id=i.subscriber_id WHERE i.id=? FOR UPDATE",conn),(inv_id,))
                invoice = cur.fetchone()
                if not invoice:
                    raise ValueError('الفاتورة غير موجودة.')
                if invoice['status'] != 'paid':
                    cur.execute(adapt_query("UPDATE wisp_invoices SET status='paid',paid_at=CURRENT_TIMESTAMP WHERE id=?",conn),(inv_id,))
                    if invoice.get('username'):
                        record_sale_source(conn,'invoice',inv_id,invoice['username'])
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




@subscribers_bp.route('/subscribers/<int:sub_id>/sessions.pdf', endpoint='subscriber_sessions_pdf')
def subscriber_sessions_pdf(sub_id):
    account = query_one('SELECT id,username FROM wisp_subscribers WHERE id=?', (sub_id,))
    if not account:
        return jsonify(success=False, message='الحساب غير موجود.'), 404
    from web.session_export import session_pdf_response
    return session_pdf_response(account, 'subscriber')

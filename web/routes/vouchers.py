# -*- coding: utf-8 -*-
"""
MAX RADIUS Web - Voucher Batch Generation, Printing, and Card Inspection Routes Module.
"""

from services.heavy_operation_service import heavy_operation
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
    log_audit, log_user_audit, DB_PATH, is_mysql_conn
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
    get_configured_timezone_name, get_system_now, get_system_now_str, get_db_storage_now
)

logger = logging.getLogger('vouchers_bp')

from core.ownership import scoped_query_all as query_all, scoped_query_one as query_one, owner_id, generation_owner

from services.quick_card_service import get_issue_policy

vouchers_bp = Blueprint('vouchers_bp', __name__)

@vouchers_bp.before_request
def guard_voucher_permissions():
    endpoint = (request.endpoint or '').split('.')[-1]
    if 'design' in endpoint or endpoint in ('print_cards','export_batch_excel','export_batch_text','upload_card_bg_api'):
        permission = 'vouchers.print'
    elif endpoint=='generate_vouchers_action':
        permission = 'vouchers.generate'
    elif endpoint=='delete_batch_action':
        permission = 'vouchers.delete'
    elif endpoint=='batches_bulk_action':
        data = request.get_json(silent=True) or request.form
        permission = {'delete':'vouchers.delete','export':'vouchers.print'}.get(data.get('action'),'vouchers.actions')
    elif request.method=='POST':
        permission = 'vouchers.actions'
    else:
        permission = 'vouchers.view'
    manager = get_current_manager()
    if not manager:
        return jsonify(success=False,message='يرجى تسجيل الدخول أولًا.'),401
    if not has_permission(permission,manager):
        return jsonify(success=False,message='لا تملك صلاحية هذا الإجراء.'),403




@vouchers_bp.route('/api/vouchers/<int:account_id>/mac-policy', methods=['GET', 'POST'], endpoint='api_voucher_mac_policy')
@login_required
def api_voucher_mac_policy(account_id):
    from web.mac_policy_routes import account_mac_response
    return account_mac_response('voucher', account_id)


@vouchers_bp.route('/vouchers', endpoint="vouchers")

def vouchers():
    from services.list_sort_service import sort_state, sort_rows
    list_sort = sort_state(request.args, 'batches')
    sync_voucher_activations()
    batch_id = request.args.get('batch_id')
    status = request.args.get('status')
    search = request.args.get('q', '').strip()
    package_id = request.args.get('package_id', '').strip()
    reseller_id = request.args.get('reseller_id', '').strip()
    
    batches = get_batches(search=search, package_id=package_id, reseller_id=reseller_id)
    batches = sort_rows(batches, list_sort)
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
                           list_sort=list_sort,
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
                           status=status,
                           card_designs=get_all_card_designs(),
                           issue_policy=get_issue_policy() if owner_id() else None)



@vouchers_bp.route('/vouchers/generate', methods=['POST'], endpoint="generate_vouchers_action")

def generate_vouchers_action():
    try:
        package_id = int(request.form['package_id'])
        count = int(request.form['count'])
        
        # 1. License validity verification
        from services.license_guard_service import get_active_license_status
        lic_st = get_active_license_status()
        if not lic_st.get('valid'):
            flash(lic_st.get('message', "النظام مقفل أو غير مرخص. لا يمكن توليد كروت جديدة."), 'danger')
            return redirect(url_for('vouchers'))
        if lic_st.get('status') == 'revoked':
            flash("الترخيص محظور من قِبل المطور. لا يمكن توليد كروت جديدة.", 'danger')
            return redirect(url_for('vouchers'))
        format_type = request.form.get('format_type', 'pin_only')
        char_type = request.form.get('char_type', 'numbers')
        code_length = int(request.form.get('code_length', 8))
        pass_length = request.form.get('pass_length')
        pass_length = int(pass_length) if pass_length and pass_length.isdigit() else None
        prefix = request.form.get('prefix', '').strip()
        reseller_id = request.form.get('reseller_id')
        reseller_id = int(reseller_id) if reseller_id and reseller_id.isdigit() else None
        name = '' if request.form.get('auto_name') == '1' else request.form.get('name', '').strip()
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
            created_by=get_current_manager().get('username') or 'admin',
            same_user_pass=same_user_pass,
            pass_length=pass_length
        )
        flash(f'تم توليد الحزمة بنجاح برقم {batch_num} ومزامنة الكروت مع FreeRADIUS.', 'success')
        return redirect(url_for('view_batch_details', batch_id=batch_id))
    except Exception as e:
        flash(f'خطأ أثناء توليد الحزمة: {str(e)}', 'danger')
        return redirect(url_for('vouchers'))


@vouchers_bp.route('/vouchers/batch/<int:batch_id>', endpoint="view_batch_details")
def view_batch_details(batch_id):
    search = request.args.get('q', '').strip()
    status = request.args.get('status', '').strip()
    batch = query_one('''
        SELECT b.*, p.name as package_name, 
               COALESCE(p.price, b.price) as package_price, 
               COALESCE(p.rate_download, b.rate_download) as rate_download, 
               COALESCE(p.rate_upload, b.rate_upload) as rate_upload,
               COALESCE(p.volume_quota_mb, b.volume_quota_mb) as volume_quota_mb,
               COALESCE(p.validity_value, b.validity_value) as validity_value,
               COALESCE(p.validity_unit, b.validity_unit) as validity_unit,
               (SELECT COUNT(*) FROM wisp_vouchers WHERE batch_id = b.id) as total_cards,
               (SELECT COUNT(*) FROM wisp_vouchers WHERE batch_id = b.id AND status = 'unused') as unused_count,
               (SELECT COUNT(*) FROM wisp_vouchers WHERE batch_id = b.id AND status = 'active') as active_count,
               (SELECT COUNT(*) FROM wisp_vouchers WHERE batch_id = b.id AND status = 'recharged') as recharged_count,
               (SELECT COUNT(*) FROM wisp_vouchers WHERE batch_id = b.id AND status = 'expired') as expired_count
        FROM wisp_voucher_batches b
        LEFT JOIN wisp_packages p ON b.package_id = p.id
        WHERE b.id = ?
    ''', (batch_id,))
    if not batch:
        flash('الدفعة المطلوبة غير موجودة.', 'danger')
        return redirect(url_for('vouchers'))
    cards = get_vouchers(batch_id=batch_id, limit=5000)
    packages = query_all("SELECT id, name, price FROM wisp_packages WHERE service_type IN ('hotspot', 'both')")
    return render_template('batch_details.html', batch=batch, cards=cards, packages=packages, q=search, status=status)


@vouchers_bp.route('/vouchers/batch/edit/<int:batch_id>', methods=['POST'], endpoint="edit_batch_action")

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


@vouchers_bp.route('/vouchers/batches/bulk-action', methods=['POST'], endpoint="batches_bulk_action")
def batches_bulk_action():
    try:
        data = request.get_json(silent=True) or request.form
        action = data.get('action', '').strip()
        raw_ids = data.get('batch_ids', [])
        if isinstance(raw_ids, str):
            try:
                raw_ids = json.loads(raw_ids)
            except Exception:
                raw_ids = [x.strip() for x in raw_ids.split(',') if x.strip()]

        if not isinstance(raw_ids,(list,tuple)) or any(not str(i).isdigit() or int(i)<=0 for i in raw_ids):
            return jsonify(success=False,message='معرفات الحزم غير صالحة.'),400
        batch_ids = list(dict.fromkeys(int(i) for i in raw_ids))
        if not batch_ids:
            return jsonify({'success': False, 'message': 'يرجى تحديد حزمة واحدة على الأقل.'}), 400

        count = len(batch_ids)
        admin_username = session.get('admin_username', 'admin')

        if action == 'delete':
            deleted_count = 0
            results = []
            for bid in batch_ids:
                try:
                    if delete_batch(bid, admin_username=admin_username):
                        deleted_count += 1
                        results.append({'batch_id': bid, 'status': 'deleted', 'message': 'تم حذف الحزمة.'})
                    else:
                        results.append({'batch_id': bid, 'status': 'not_found', 'message': 'الحزمة غير موجودة أو حُذفت سابقًا.'})
                except ValueError as e:
                    results.append({'batch_id': bid, 'status': 'pending' if getattr(e, 'code', None) == 'sessions_pending' else 'failed', 'message': str(e)})
                except Exception as e:
                    logger.error(f"Error deleting batch {bid}: {e}")
                    results.append({'batch_id': bid, 'status': 'failed', 'message': 'تعذر إكمال الحذف؛ تحقق من حالة الحزمة قبل إعادة الطلب.'})
            log_audit(session.get('admin_id', 1), admin_username, 'BULK_DELETE_BATCHES', 'vouchers', f'Bulk deleted {deleted_count} batches')
            return jsonify({'success': deleted_count > 0, 'partial': 0 < deleted_count < count,
                            'successful': deleted_count, 'failed': count-deleted_count,
                            'results': results,
                            'message': f'حُذفت {deleted_count} حزمة؛ لم تُحذف {count-deleted_count} حزمة. تحقق من حالتها قبل إعادة الطلب.'})

        elif action == 'change_package':
            new_pkg_id = data.get('package_id')
            if not new_pkg_id:
                return jsonify({'success': False, 'message': 'يرجى اختيار الباقة الجديدة.'}), 400
            new_pkg = query_one('SELECT id, name FROM wisp_packages WHERE id = ?', (int(new_pkg_id),))
            if not new_pkg:
                return jsonify({'success': False, 'message': 'الباقة المختارة غير صالحة.'}), 400

            updated_count = 0
            for bid in batch_ids:
                try:
                    update_voucher_batch(bid, name=None, package_id=int(new_pkg_id), admin_username=admin_username)
                    updated_count += 1
                except Exception as e:
                    logger.error(f"Error updating package for batch {bid}: {e}")
            log_audit(session.get('admin_id', 1), admin_username, 'BULK_UPDATE_BATCH_PKG', 'vouchers', f'Updated package to [{new_pkg["name"]}] for {updated_count} batches')
            return jsonify({'success': updated_count > 0, 'partial': 0 < updated_count < count,
                            'successful': updated_count, 'failed': count-updated_count,
                            'message': f'تغيرت باقة {updated_count} حزمة إلى [{new_pkg["name"]}]؛ لم تتغير {count-updated_count} حزمة.'})

        elif action == 'assign_reseller':
            new_reseller_id = data.get('reseller_id')
            if new_reseller_id not in (None, '') and (not str(new_reseller_id).isdigit() or int(new_reseller_id)<=0):
                return jsonify(success=False,message='معرف الموزع غير صالح.'),400
            rid = int(new_reseller_id) if new_reseller_id not in (None,'') else None
            from database.db import db_session
            from services.voucher_service import _VoucherWriter
            placeholders=','.join(['?']*count)
            with db_session() as conn:
                tx=_VoucherWriter(conn)
                for bid in batch_ids:
                    if not tx.one('SELECT id FROM wisp_voucher_batches WHERE id=?'+(' FOR UPDATE' if is_mysql_conn(conn) else ''),(bid,)):
                        raise ValueError('إحدى الحزم غير موجودة؛ لم تُحفظ التغييرات.')
                if rid and not tx.one('SELECT id FROM wisp_managers WHERE id=? AND is_active=1 AND COALESCE(is_deleted,0)=0',(rid,)):
                    raise ValueError('الموزع غير صالح.')
                tx.write(f'UPDATE wisp_voucher_batches SET reseller_id=? WHERE id IN ({placeholders})',(rid,*batch_ids))
                tx.write(f'UPDATE wisp_vouchers SET reseller_id=? WHERE batch_id IN ({placeholders})',(rid,*batch_ids))
            reseller_label = 'إدارة النظام'
            if rid:
                r = query_one('SELECT COALESCE(NULLIF(full_name, ""), username) as name FROM wisp_managers WHERE id = ?', (rid,))
                if r:
                    reseller_label = r['name']
            log_audit(session.get('admin_id', 1), admin_username, 'BULK_ASSIGN_RESELLER', 'vouchers', f'Assigned {count} batches to reseller [{reseller_label}]')
            return jsonify({'success': True, 'message': f'تم تعيين {count} حزمة محددة للموزع [{reseller_label}] بنجاح.'})

        elif action == 'export':
            placeholders = ','.join(['?'] * count)
            cards = query_all(f'''
                SELECT v.serial_number, v.username, v.password, v.pin_code, v.status, 
                       b.name as batch_name, p.name as package_name
                FROM wisp_vouchers v
                JOIN wisp_voucher_batches b ON v.batch_id = b.id
                LEFT JOIN wisp_packages p ON v.package_id = p.id
                WHERE v.batch_id IN ({placeholders})
                ORDER BY v.batch_id DESC, v.id ASC
            ''', tuple(batch_ids))
            import csv
            import io
            si = io.StringIO()
            cw = csv.writer(si)
            cw.writerow(['الحزمة (Batch)', 'الباقة (Package)', 'الرقم التسلسلي (Serial)', 'اسم المستخدم (Username)', 'كلمة المرور (Password)', 'الحالة (Status)'])
            for c in cards:
                cw.writerow([c.get('batch_name'), c.get('package_name'), c.get('serial_number'), c.get('username'), c.get('password'), c.get('status')])
            output = si.getvalue()
            return Response(
                "\ufeff" + output,
                mimetype="text/csv; charset=utf-8",
                headers={"Content-Disposition": "attachment;filename=selected_batches_vouchers.csv"}
            )

        return jsonify({'success': False, 'message': 'إجراء غير معروف.'}), 400

    except ValueError as exc:
        return jsonify(success=False,message=str(exc)),400
    except Exception:
        logger.exception('Batch operation failed')
        return jsonify(success=False,message='تعذرت العملية؛ تحقق من حالة الحزم قبل إعادة الطلب.'),500


@vouchers_bp.route('/vouchers/card/edit/<int:card_id>', methods=['POST'], endpoint="edit_card_action")

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


@vouchers_bp.route('/vouchers/active-users', endpoint="active_card_users")

def active_card_users():
    from services.list_sort_service import sort_state, sort_rows, card_order
    list_sort = sort_state(request.args, 'cards')
    sql_order = card_order(list_sort)
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
    if status_filter == 'disabled':
        status_filter = 'suspended'
    
    try:
        page = max(1, int(request.args.get('page', 1)))
    except (ValueError, TypeError):
        page = 1
    per_page = 50
    offset = (page - 1) * per_page
    
    base_where = ["v.status IN ('active', 'used', 'expired', 'recharged', 'suspended', 'disabled')"]
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
            COALESCE(SUM(CASE WHEN (v.status = 'expired' OR (v.expires_at IS NOT NULL AND v.expires_at <= CURRENT_TIMESTAMP)) AND v.status NOT IN ('recharged', 'disabled', 'suspended') THEN 1 ELSE 0 END), 0) as total_expired,
            COALESCE(SUM(CASE WHEN v.status = 'recharged' THEN 1 ELSE 0 END), 0) as total_recharged,
            COALESCE(SUM(CASE WHEN v.status IN ('suspended','disabled') THEN 1 ELSE 0 END), 0) as total_suspended,
            COALESCE(SUM(CASE WHEN v.username IN (SELECT username FROM radacct WHERE acctstoptime IS NULL AND (acctupdatetime >= ? OR (acctupdatetime IS NULL AND acctstarttime >= ?))) THEN 1 ELSE 0 END), 0) as total_online
        FROM wisp_vouchers v
        WHERE {base_sql}
    '''
    status_row = query_one(count_query, (cutoff_s, cutoff_s, *base_params)) or {}
    status_counts = {
        'all': int(status_row.get('total_all') or 0),
        'active': int(status_row.get('total_active') or 0),
        'online': int(status_row.get('total_online') or 0),
        'expired': int(status_row.get('total_expired') or 0),
        'recharged': int(status_row.get('total_recharged') or 0),
        'suspended': int(status_row.get('total_suspended') or 0)
    }

    where_clauses = list(base_where)
    params = list(base_params)
            
    if status_filter == 'active':
        where_clauses.append("v.status IN ('active', 'used') AND (v.expires_at IS NULL OR v.expires_at > CURRENT_TIMESTAMP)")
    elif status_filter == 'online':
        where_clauses.append("v.username IN (SELECT username FROM radacct WHERE acctstoptime IS NULL AND (acctupdatetime >= ? OR (acctupdatetime IS NULL AND acctstarttime >= ?)))")
        params.extend([cutoff_s, cutoff_s])
    elif status_filter == 'expired':
        where_clauses.append("(v.status = 'expired' OR (v.expires_at IS NOT NULL AND v.expires_at <= CURRENT_TIMESTAMP)) AND v.status NOT IN ('recharged', 'disabled', 'suspended')")
    elif status_filter == 'recharged':
        where_clauses.append("v.status = 'recharged'")
    elif status_filter == 'suspended':
        where_clauses.append("v.status IN ('suspended','disabled')")
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
    page = min(page, total_pages)
    offset = (page - 1) * per_page
    # Small derived sorts retain their single-chunk path. Large sets select
    # compact global keys first, then hydrate only the selected page.
    load_limit = per_page if sql_order else total_records
    load_offset = offset if sql_order else 0
    query_order = sql_order or 'v.id DESC'
    list_now=get_db_storage_now().replace(tzinfo=None)
    compact_usage=None
    # Small sets already fit one quota chunk; avoid extra selection queries.
    compact_page = not sql_order and total_records > 400
    if compact_page:
        from services.card_list_page_service import page_ids
        from services.quota_service import get_voucher_accounting_totals_batch, AccountingReadError
        selected_ids,compact_usage=page_ids(query_all,where_sql,params,list_sort,offset,per_page,
            list_now,cutoff_s,
            get_voucher_accounting_totals_batch,AccountingReadError)
        where_sql += ' AND v.id IN ('+','.join('?' for _ in selected_ids)+')' if selected_ids else ' AND 1=0'
        params.extend(selected_ids)
        load_limit,load_offset=per_page,0
    
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
            JOIN wisp_packages p ON v.package_id=p.id
            JOIN wisp_voucher_batches b ON v.batch_id=b.id
            LEFT JOIN wisp_resellers r ON v.reseller_id=r.id
            WHERE {where_sql}
            ORDER BY {query_order}
            LIMIT ? OFFSET ?
        ) page
        JOIN wisp_vouchers v ON page.id = v.id
        JOIN wisp_packages p ON v.package_id = p.id
        JOIN wisp_voucher_batches b ON v.batch_id = b.id
        LEFT JOIN wisp_resellers r ON v.reseller_id = r.id
        ORDER BY {query_order}
    '''
    page_params = list(params) + [load_limit, load_offset]
    cards = query_all(cards_query, tuple(page_params))
    
    for idx, c in enumerate(cards):
        c['seq_id'] = c.get('global_seq_id') or c['id']
        
    usernames = [c['username'] for c in cards if c.get('username')]
    traffic_map = {}
    active_map = {}
    active_session_counts = {}
    last_map = {}
    
    if usernames:
        placeholders = ','.join(['?'] * len(usernames))
        from services.quota_service import get_voucher_accounting_totals_batch, AccountingReadError
        try:
            traffic_map = compact_usage if compact_usage is not None else get_voucher_accounting_totals_batch([c['id'] for c in cards])
        except AccountingReadError:
            traffic_map = {}

        from services.subscriber_service import get_heartbeat_cutoff_str
        cutoff_s = get_heartbeat_cutoff_str(5)
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
        from services.session_display_service import index_live_sessions
        active_map, active_session_counts = index_live_sessions(active_rows)

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

    now = list_now

    for c in cards:
        u_key = c['username'].lower() if c.get('username') else ''
        t_data = traffic_map.get(u_key)
        a_data = active_map.get(u_key)
        c['active_session_count'] = active_session_counts.get(u_key, 0)
        l_data = last_map.get(u_key)
        
        c['accounting_unavailable'] = t_data is None
        down_b = float(t_data['down_bytes']) if t_data else 0
        up_b = float(t_data['up_bytes']) if t_data else 0
        dur_s = float(t_data['uptime_secs']) if t_data else 0
        total_used_b = down_b + up_b
        c['sort_used_bytes'] = total_used_b if t_data is not None else None
        
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
            
        c['sort_remaining_seconds'] = (0 if c.get('is_expired') else
            max(0, (exp_dt - now).total_seconds()) if exp_dt else
            float(c['validity_value']) * {'minutes':60,'hours':3600,'days':86400,'months':2592000}.get(c.get('validity_unit'),86400)
            if str(c.get('validity_value') or '').isdigit() else None)
        # Consumption calculation
        base_quota_mb = float(c.get('volume_quota_mb') or 0)
        extra_quota_mb = float(c.get('extra_quota_mb') or 0)
        is_limited_package = (base_quota_mb > 0)
        vol_quota_mb = max(0.0, base_quota_mb + extra_quota_mb)
        c['has_quota'] = is_limited_package
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
            
        if c['accounting_unavailable']:
            c['used_bytes_str'] = c['remaining_bytes_str'] = 'غير متاح'
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

    if not sql_order:
        cards = sort_rows(cards, list_sort)
        if not compact_page:
            cards = cards[offset:offset + per_page]
    packages = query_all("SELECT id, name, price, service_type, rate_download, rate_upload FROM wisp_packages WHERE is_active = 1")
    batches = query_all("SELECT id, name, batch_number FROM wisp_voucher_batches ORDER BY id DESC")
    return render_template('active_card_users.html',
                           list_sort=list_sort,
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


@vouchers_bp.route('/vouchers/active-users/<int:card_id>', endpoint="active_card_user_details")

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
    from services.subscriber_service import get_heartbeat_cutoff_str
    cutoff_s = get_heartbeat_cutoff_str(5)
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

    from services.quota_service import calculate_account_quota
    q_info = calculate_account_quota(card['id'], entity_type='voucher')
    if q_info and not q_info.get('accounting_error',False):
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
        total_download = total_upload = total_used_str = quota_str = remaining_str = 'غير متاح'
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
            now = get_db_storage_now().replace(tzinfo=None)
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
    session_history = get_session_history(card['username'], page=max(1, request.args.get('session_page', 1, type=int)))
    sessions = session_history['rows']
    invoices = query_all('''
        SELECT id, COALESCE(serial_number, CONCAT('V-SALE-', id)) as invoice_number, 
               activated_at as created_at,
               price as amount, 'شراء وتفعيل كرت' as operation_type, 'paid' as status
        FROM wisp_voucher_sales
        WHERE voucher_id = ?
        ORDER BY id DESC
    ''', (card_id,))
    packages = query_all("SELECT id,name,price,service_type,rate_download,rate_upload,volume_quota_mb,validity_value,validity_unit,validity_days FROM wisp_packages WHERE is_active=1 AND service_type IN ('hotspot','both')")
    audit_logs = get_user_audit_logs('voucher', card_id, card['username'])
    analytics = get_user_usage_analytics(card['username'])

    from services.voucher_edit_service import revision
    import secrets
    from services.account_mac_service import get_mac_policy
    return render_template('active_card_user_details.html',
                           mac_policy=get_mac_policy('voucher', card['id']),
                           edit_revision=revision(card),edit_operation_id=secrets.token_hex(16),package_operation_id=secrets.token_hex(16),
                           card=card,
                           total_download=total_download,
                           total_upload=total_upload,
                           total_used_str=total_used_str,
                           quota_str=quota_str,
                           remaining_str=remaining_str,
                           usage_percent=usage_percent,
                           remaining_days_text=remaining_days_text,
                           is_expired=is_expired,
                           sessions=sessions, session_history=session_history,
                           invoices=invoices if has_permission('invoices.view') else [],
                           audit_logs=audit_logs if has_permission('system.audit_logs') else [],
                           analytics=analytics,
                           analytics_json=json.dumps(analytics, default=str),
                           packages=packages)


@vouchers_bp.route('/vouchers/active-users/edit/<int:card_id>', methods=['POST'], endpoint="edit_active_card_action")

def edit_active_card_action(card_id):
    from services.voucher_edit_service import edit_voucher
    from services.renewal_settlement_service import SettlementPending
    from web.access_policy import can_access_account_action
    if not query_one('SELECT id FROM wisp_vouchers WHERE id=?',(card_id,)):
        return jsonify(success=False,message='الكرت غير موجود.'),404
    try:
        return jsonify(edit_voucher(card_id,request.form,actor=get_current_manager().get('username') or 'admin',
            can_change_expiry=has_permission('vouchers.extend') and can_access_account_action('extend-time','voucher')))
    except ValueError as exc:
        return jsonify(success=False,message=str(exc)),400
    except SettlementPending as exc:
        return jsonify(success=False,message=str(exc)),409
    except Exception:
        logger.exception('Voucher edit failed: card_id=%s',card_id)
        return jsonify(success=False,message='تعذر تأكيد حفظ التعديل. راجع حالة الكرت قبل إعادة العملية.'),500


@vouchers_bp.route('/vouchers/active-users/<int:card_id>/edit-package',methods=['POST'],endpoint='edit_active_card_package')
def edit_active_card_package(card_id):
    from web.access_policy import can_access_account_action
    from services.voucher_edit_service import revision,receipt,fingerprint
    if not has_permission('detail.vouchers.card_edit') or not can_access_account_action('change-package','voucher'):
        return jsonify(success=False,message='لا تملك صلاحية تغيير باقة الكرت.'),403
    row=query_one('SELECT * FROM wisp_vouchers WHERE id=?',(card_id,))
    if not row:return jsonify(success=False,message='الكرت غير موجود.'),404
    try:
        package_id=int(request.form.get('new_package_id',''))
        if package_id<=0:raise ValueError('اختر باقة صحيحة.')
        if not query_one("SELECT id FROM wisp_packages WHERE id=? AND is_active=1 AND service_type IN ('hotspot','both')",(package_id,)):
            raise ValueError('الباقة غير متاحة لهذا الكرت.')
        actor=get_current_manager().get('username') or 'admin'
        rollover=request.form.get('enable_rollover') in ('1','true','on')
        if row['status']=='unused':rollover=False
        op=request.form.get('operation_id','')
        digest=fingerprint('package',card_id,actor,{'package_id':package_id,'rollover':rollover})
        old=receipt(op,card_id,actor,digest)
        if old:return jsonify(old)
        expected=request.form.get('expected_revision')
        if expected!=revision(row):raise ValueError('تغيرت بيانات الكرت؛ أعد فتح الصفحة.')
        unused_profile=dict(username=row['username'],password=row['password'],bound_mac=row.get('bound_mac'),status='unused') if row['status']=='unused' else None
        ok,msg=action_change_package('voucher',card_id,package_id,enable_rollover=rollover,
            admin_username=actor,operation_id=op,expected_revision=expected,voucher_profile=unused_profile)
        return jsonify(success=ok,message=msg),(200 if ok else 409)
    except ValueError as exc:
        return jsonify(success=False,message=str(exc)),400
    except Exception:
        logger.exception('Voucher package edit failed: card_id=%s',card_id)
        return jsonify(success=False,message='تعذر تأكيد نتيجة تغيير الباقة؛ راجع الحالة قبل إعادة الطلب.'),500


@vouchers_bp.route('/vouchers/update-card', methods=['POST'], endpoint="update_card_action")

def update_card_action():
    try:
        card_id=int(request.form['card_id'])
        card=query_one('SELECT * FROM wisp_vouchers WHERE id=?',(card_id,))
        if not card:
            raise ValueError('الكرت غير موجود.')
        if request.form.get('status',card['status']) != card['status']:
            raise ValueError('استخدم عملية الإيقاف أو التنشيط المخصصة.')
        update_voucher_card(card_id,card['username'],card['password'],bound_mac=request.form.get('bound_mac',''),admin_username=session.get('admin_username') or 'admin')
        flash('تم حفظ الماك ومزامنته مع الراديوس.','success')
    except Exception as exc:
        flash(str(exc),'danger')
    return redirect(url_for('active_card_users'))


@vouchers_bp.route('/vouchers/inspect', endpoint="card_inspect")

@vouchers_bp.route('/card-inspect', endpoint="card_inspect")

@vouchers_bp.route('/card/inspect', endpoint="card_inspect")

def card_inspect():
    query = (request.args.get('q') or request.args.get('code') or '').strip()
    card = None
    sessions = []
    diag = {
        'total_down_str': "0 MB",
        'total_up_str': "0 MB",
        'total_traffic_str': "0 MB",
        'total_down_bytes': 0,
        'total_up_bytes': 0,
        'total_traffic_bytes': 0,
        'total_quota_str': "0 MB",
        'rem_quota_str': "0 MB",
        'pct_used': 0.0,
        'pct_remaining': 100.0,
        'time_left_str': "غير محدد",
        'is_expired': False,
        'is_online': False,
        'active_session': None,
        'online_nas': None,
        'online_ip': None,
        'online_mac': None,
        'online_duration': None
    }

    if query:
        card = query_one('''
            SELECT v.*, 
                   p.name as package_name, 
                   COALESCE(v.snap_price, p.price) as price, 
                   COALESCE(v.snap_rate_download, p.rate_download) as rate_download, 
                   COALESCE(v.snap_rate_upload, p.rate_upload) as rate_upload,
                   COALESCE(v.snap_volume_quota_mb, p.volume_quota_mb) as volume_quota_mb,
                   COALESCE(v.snap_uptime_limit_mins, p.uptime_limit_mins) as uptime_limit_mins,
                   COALESCE(v.snap_validity_days, p.validity_days) as validity_days,
                   b.name as batch_name,
                   b.prefix as batch_prefix,
                   r.name as reseller_name
            FROM wisp_vouchers v
            LEFT JOIN wisp_packages p ON v.package_id = p.id
            LEFT JOIN wisp_voucher_batches b ON v.batch_id = b.id
            LEFT JOIN wisp_resellers r ON v.reseller_id = r.id
            WHERE v.username = ? OR v.pin_code = ? OR v.serial_number = ?
            LIMIT 1
        ''', (query, query, query))

        if card:
            # 1. Fetch sessions
            sessions = get_subscriber_sessions(card['username'])

            from services.quota_service import calculate_account_quota
            q_info = calculate_account_quota(card['id'],entity_type='voucher')
            diag['accounting_unavailable'] = not q_info or q_info.get('accounting_error',False)
            if diag['accounting_unavailable']:
                for field in ('total_down_str','total_up_str','total_traffic_str','rem_quota_str'):
                    diag[field]='غير متاح'
                diag['total_down_bytes']=diag['total_up_bytes']=diag['total_traffic_bytes']=None
                diag['total_quota_str']='غير متاح'
                diag['pct_used']=diag['pct_remaining']=0
            else:
                diag.update(total_down_bytes=q_info['used_down_bytes'],total_up_bytes=q_info['used_up_bytes'],total_traffic_bytes=q_info['used_total_bytes'],
                            total_down_str=format_bytes(q_info['used_down_bytes']),total_up_str=format_bytes(q_info['used_up_bytes']),total_traffic_str=format_bytes(q_info['used_total_bytes']))
                diag['total_quota_str']='غير محدود' if q_info['is_quota_unlimited'] else format_bytes(q_info['total_quota_mb']*1048576)
                diag['rem_quota_str']='غير محدود' if q_info['is_quota_unlimited'] else format_bytes(q_info['remaining_bytes'])
                diag['pct_used']=q_info.get('quota_used_pct') or 0
                diag['pct_remaining']=100-diag['pct_used']

            # 4. Validity & Expiration Time left
            if card.get('expires_at'):
                try:
                    exp_dt = datetime.datetime.strptime(str(card['expires_at']), '%Y-%m-%d %H:%M:%S')
                    now_dt = get_db_storage_now().replace(tzinfo=None)
                    if exp_dt > now_dt:
                        diff = exp_dt - now_dt
                        days = diff.days
                        hours = diff.seconds // 3600
                        mins = (diff.seconds % 3600) // 60
                        if days > 0:
                            diag['time_left_str'] = f"{days} يوم و {hours} س متبقية"
                        elif hours > 0:
                            diag['time_left_str'] = f"{hours} ساعة و {mins} د متبقية"
                        else:
                            diag['time_left_str'] = f"{mins} دقيقة متبقية"
                        diag['is_expired'] = False
                    else:
                        diff = now_dt - exp_dt
                        days = diff.days
                        diag['time_left_str'] = f"منتهي الصلاحية منذ {days} يوم" if days > 0 else "منتهي الصلاحية اليوم"
                        diag['is_expired'] = True
                except Exception:
                    diag['time_left_str'] = "غير محدد"
            elif card.get('status') == 'unused':
                value=card.get('snap_validity_value')
                if value is None:
                    value=card.get('validity_days')
                value=30 if value is None else value
                unit={'days':'يوم','hours':'ساعة','minutes':'دقيقة','months':'شهر'}.get(card.get('snap_validity_unit') or 'days','يوم')
                diag['time_left_str']='لم يبدأ بعد (صلاحية غير محدودة)' if int(value)==0 else f'لم يبدأ بعد ({value} {unit} عند التفعيل)'

            # 5. Live Online Check & Active Session Details
            from services.subscriber_service import get_heartbeat_cutoff_str
            cutoff_s = get_heartbeat_cutoff_str(5)
            active_sess = query_one('''
                SELECT r.*, n.name as nas_name
                FROM radacct r
                LEFT JOIN wisp_nas_devices n ON (r.nasipaddress = n.ip_address)
                WHERE LOWER(r.username) = LOWER(?) AND r.acctstoptime IS NULL
                  AND (
                      (r.acctupdatetime IS NOT NULL AND r.acctupdatetime >= ?)
                      OR
                      (r.acctupdatetime IS NULL AND r.acctstarttime >= ?)
                  )
                ORDER BY r.radacctid DESC
                LIMIT 1
            ''', (card['username'], cutoff_s, cutoff_s))

            if active_sess:
                diag['is_online'] = True
                diag['active_session'] = active_sess
                diag['online_nas'] = active_sess.get('nas_name') or active_sess.get('nasipaddress') or '-'
                diag['online_ip'] = active_sess.get('framedipaddress') or '-'
                diag['online_mac'] = active_sess.get('callingstationid') or '-'
                duration_sec = int(active_sess.get('acctsessiontime') or 0)
                diag['online_duration'] = format_duration(duration_sec) if duration_sec > 0 else "متصل الآن"

    currency_setting = query_one("SELECT `value` FROM wisp_system_settings WHERE `key` = 'currency_symbol'")
    curr_sym = currency_setting['value'] if currency_setting and currency_setting.get('value') else 'ريال'

    return render_template(
        'card_inspect.html',
        query=query,
        card=card,
        sessions=sessions,
        diag=diag,
        currency_symbol=curr_sym,
        total_down=diag['total_down_str'],
        total_up=diag['total_up_str'],
        is_online=diag['is_online']
    )


@vouchers_bp.route('/api/vouchers/reset-mac', methods=['POST'], endpoint="api_reset_voucher_mac")
def api_reset_voucher_mac():
    try:
        data=request.get_json(silent=True) or request.form
        card=query_one('SELECT * FROM wisp_vouchers WHERE username=?',(str(data.get('username') or '').strip(),))
        if not card:
            return jsonify(success=False,message='الكرت غير موجود.'),404
        from services.account_mac_service import get_mac_policy,save_mac_policy
        policy=get_mac_policy('voucher',card['id'])
        save_mac_policy('voucher',card['id'],dict(policy,enabled=False,operation_id=secrets.token_hex(16)),actor=session.get('admin_username') or 'admin')
        return jsonify(success=True,message='تم فك قيد الماك ومزامنة الراديوس.')
    except ValueError as exc:
        return jsonify(success=False,message=str(exc)),400
    except Exception:
        logger.exception('Voucher MAC reset failed')
        return jsonify(success=False,message='تعذر تحديث الكرت؛ لم تُحفظ التغييرات.'),500


@vouchers_bp.route('/api/vouchers/extend-quota-time', methods=['POST'], endpoint="api_extend_voucher")
def api_extend_voucher():
    try:
        data=request.get_json(silent=True) or request.form
        card=query_one('SELECT id FROM wisp_vouchers WHERE username=?',(str(data.get('username') or '').strip(),))
        if not card:
            return jsonify(success=False,message='الكرت غير موجود.'),404
        ok,message=action_adjust_entitlement('voucher',card['id'],days=data.get('add_days',0),quota_amount=data.get('add_mb',0),quota_unit='MB',admin_username=session.get('admin_username') or 'admin')
        return jsonify(success=ok,message=message),200 if ok else 400
    except Exception:
        logger.exception('Voucher topup failed')
        return jsonify(success=False,message='تعذر تمديد الكرت؛ لم تُحفظ التغييرات.'),500


@vouchers_bp.route('/api/vouchers/disconnect',methods=['POST'])
def api_disconnect_voucher():
    data=request.get_json(silent=True) or request.form
    card=query_one('SELECT id FROM wisp_vouchers WHERE username=?',(str(data.get('username') or '').strip(),))
    if not card:
        return jsonify(success=False,message='الكرت غير موجود.'),404
    success,message=action_disconnect_user('voucher',card['id'],admin_username=session.get('admin_username') or 'admin',operation_id=data.get('operation_id'))
    return jsonify(dict(success=success, **message) if isinstance(message,dict) else dict(success=success,message=message))


@vouchers_bp.route('/api/vouchers/activate',methods=['POST'])
def api_activate_voucher():
    try:
        data=request.get_json(silent=True) or request.form
        success,message,status=action_activate_subscriber(int(data.get('card_id')),admin_username=session.get('admin_username') or 'admin',entity_type='voucher')
        return jsonify(success=success,message=message,status=status)
    except (ValueError,TypeError) as exc:
        return jsonify(success=False,message='بيانات الكرت غير صالحة.' if isinstance(exc,TypeError) else str(exc)),400
    except Exception:
        logger.exception('Voucher activation failed')
        return jsonify(success=False,message='تعذر تنشيط الكرت؛ لم تُحفظ التغييرات.'),500


@vouchers_bp.route('/vouchers/designs', endpoint='voucher_designs_list')
@vouchers_bp.route('/vouchers/designer', endpoint='card_designer')
@vouchers_bp.route('/card-designer', endpoint='card_designer_short')
@login_required
def voucher_designs_list():
    """عرض صفحة قائمة تصاميم الكروت المحفوظة"""
    designs = get_all_card_designs()
    return render_template('designs_list.html', designs=designs)


@vouchers_bp.route('/vouchers/designs/new', endpoint="new_voucher_design")

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


@vouchers_bp.route('/vouchers/designs/<int:design_id>', endpoint="edit_voucher_design_direct")
@vouchers_bp.route('/vouchers/designs/edit/<int:design_id>', endpoint="edit_voucher_design")
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


@vouchers_bp.route('/api/vouchers/designs/save', methods=['POST'], endpoint="save_card_design_api")

@vouchers_bp.route('/api/vouchers/designer/save', methods=['POST'], endpoint="save_card_design_api")

@vouchers_bp.route('/vouchers/designer/save', methods=['POST'], endpoint='save_card_designer_action')
@vouchers_bp.route('/vouchers/designer/save', methods=['POST'], endpoint="save_card_design_api")

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


@vouchers_bp.route('/api/vouchers/designs/upload-bg', methods=['POST'], endpoint="upload_card_bg_api")

@vouchers_bp.route('/api/vouchers/designs/upload-image', methods=['POST'], endpoint="upload_card_bg_api")

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


@vouchers_bp.route('/api/vouchers/designs/delete/<int:design_id>', methods=['POST'], endpoint="delete_card_design_api")

@vouchers_bp.route('/vouchers/designs/delete/<int:design_id>', methods=['POST'], endpoint="delete_card_design_api")

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


@vouchers_bp.route('/api/vouchers/designs/set-default/<int:design_id>', methods=['POST'], endpoint="set_default_card_design_api")

@vouchers_bp.route('/vouchers/designs/set-default/<int:design_id>', methods=['POST'], endpoint="set_default_card_design_api")

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


@vouchers_bp.route('/api/vouchers/designs/duplicate/<int:design_id>', methods=['POST'], endpoint="duplicate_card_design_api")

@vouchers_bp.route('/vouchers/designs/duplicate/<int:design_id>', methods=['POST'], endpoint="duplicate_card_design_api")

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


@vouchers_bp.route('/vouchers/print/<int:batch_id>', endpoint="print_cards")

@heavy_operation
def print_cards(batch_id):
    # Check ownership before the print service performs its unscoped reads.
    if not query_one('SELECT id FROM wisp_voucher_batches WHERE id=?', (batch_id,)):
        return jsonify(success=False, message='الحزمة غير موجودة.'), 404
    pdf_export = request.args.get('export') == 'pdf'
    if pdf_export:
        size=query_one('SELECT COUNT(*) AS n FROM wisp_vouchers WHERE batch_id=?',(batch_id,))['n']
        if not 1 <= size <= 2000:
            return jsonify(success=False,message='تصدير PDF متاح للحزم من 1 إلى 2000 كرت.'),400
    batch, cards, template = get_batch_cards_for_print(batch_id)
    if not batch:
        flash('الدفعة المطلوبة غير موجودة.', 'danger')
        return redirect(url_for('vouchers'))

    design_id = request.values.get('design_id')
    selected_design = None
    if design_id:
        try:
            selected_design = get_card_design_by_id(int(design_id))
        except Exception:
            selected_design = None
        if not selected_design:
            return jsonify(success=False, message='التصميم المحدد غير موجود.'), 400

    designs = get_all_card_designs()

    # Calculate pagination pages if custom design is selected
    card_pages = []
    if selected_design:
        cols = int(selected_design.get('cards_per_row') or 2)
        rows_cnt = int(selected_design.get('cards_per_col') or 5)
        cards_per_page = max(1, cols * rows_cnt)
        card_pages = [cards[i:i + cards_per_page] for i in range(0, len(cards), cards_per_page)]

        # Auto-containment: calculate exact card dimensions to fill A4 without gaps
        try:
            margin_top = float(selected_design.get('margin_top_mm') if selected_design.get('margin_top_mm') is not None else 10.0)
            margin_page = float(selected_design.get('margin_page_mm') if selected_design.get('margin_page_mm') is not None else 10.0)
            spacing = float(selected_design.get('card_spacing_mm') if selected_design.get('card_spacing_mm') is not None else 0.0)

            avail_w = max(20.0, 210.0 - (2.0 * margin_page) - ((cols - 1) * spacing))
            avail_h = max(20.0, 297.0 - (2.0 * margin_top) - ((rows_cnt - 1) * spacing))

            card_w = round(avail_w / cols, 2)
            card_h = round(avail_h / rows_cnt, 2)
            selected_design['width_mm'] = card_w
            selected_design['height_mm'] = card_h
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Error calculating auto-containment in print_cards: %s", e)
    else:
        card_pages = [cards]

    # Load advanced JSON configuration from wisp_system_settings
    setting_row = query_one("SELECT `value` FROM wisp_system_settings WHERE `key` = 'voucher_custom_design_config'")
    saved_config_json = setting_row['value'] if setting_row and setting_row.get('value') else None

    if not pdf_export:
        html = render_template('print_cards.html',
                               batch=batch,
                               cards=cards,
                               card_pages=card_pages,
                               tpl=template,
                               saved_config_json=saved_config_json,
                               designs=designs,
                               selected_design=selected_design,
                               selected_design_id=int(design_id) if (design_id and str(design_id).isdigit()) else None)
        return html
    if not cards or len(cards) > 2000:
        return jsonify(success=False, message='تصدير PDF متاح للحزم من 1 إلى 2000 كرت.'), 400
    try:
        import io
        from services.batch_export_service import batch_download_name, iter_card_pdf_groups
        from services.pdf_export_service import render_parts
        from services.heavy_operation_service import ExportCancelled
        def parts():
            # Render several existing A4 sheets together; the visible grid and
            # individual sheet boundaries remain controlled by the template.
            for subset, pages in iter_card_pdf_groups(
                    cards, cards_per_page if selected_design else None):
                yield render_template('print_cards.html',batch=batch,cards=subset,
                    card_pages=pages,tpl=template,saved_config_json=saved_config_json,
                    designs=designs,selected_design=selected_design,
                    selected_design_id=int(design_id) if design_id and str(design_id).isdigit() else None)
        payload=render_parts(parts(),current_app.static_folder,timeout=600,parts_per_worker=8)
        response = send_file(payload, mimetype='application/pdf',
                             as_attachment=True, download_name=batch_download_name(batch['name'], 'pdf'), max_age=0)
        response.call_on_close(payload.close)
        response.headers['Cache-Control'] = 'no-store, private'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response
    except ExportCancelled:
        return jsonify(success=False,message='أُلغي التصدير ولم يُصدر ملف جزئي.'),409
    except Exception:
        logger.exception('Batch PDF export failed')
        return jsonify(success=False, message='تعذر تجهيز PDF. تحقق من صور التصميم أو أعد المحاولة بعد قليل.'), 503


@vouchers_bp.route('/vouchers/batch/<int:batch_id>/excel', endpoint='export_batch_excel')
@heavy_operation
def export_batch_excel(batch_id):
    batch = query_one('SELECT * FROM wisp_voucher_batches WHERE id=?', (batch_id,))
    if not batch:
        return jsonify(success=False, message='الحزمة غير موجودة.'), 404
    from services.batch_export_service import export_snapshot, make_workbook, batch_download_name
    try:
        batch, cards, totals = export_snapshot(batch_id, owner_id())
        response = send_file(make_workbook(batch, cards, totals),
                             mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                             as_attachment=True, download_name=batch_download_name(batch['name'], 'xlsx'), max_age=0)
        response.headers['Cache-Control'] = 'no-store, private'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response
    except Exception:
        logger.exception('Batch Excel export failed')
        return jsonify(success=False, message='تعذر تصدير الملف بسبب عدم اكتمال بيانات العقد أو محاسبة الدورة.'), 409


@vouchers_bp.route('/vouchers/batch/<int:batch_id>/text', endpoint='export_batch_text')
def export_batch_text(batch_id):
    if not query_one('SELECT id FROM wisp_voucher_batches WHERE id=?', (batch_id,)):
        return jsonify(success=False, message='الحزمة غير موجودة.'), 404
    from services.batch_export_service import export_usernames_text, batch_download_name
    payload = None
    try:
        name, payload, _ = export_usernames_text(batch_id, owner_id())
        response = send_file(payload, mimetype='text/plain', as_attachment=True,
                             download_name=batch_download_name(name, 'txt'), max_age=0)
        response.content_length = os.fstat(payload.fileno()).st_size
        response.call_on_close(payload.close)
        response.headers['Cache-Control'] = 'no-store, private'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response
    except LookupError:
        if payload is not None:
            payload.close()
        return jsonify(success=False, message='الحزمة غير موجودة.'), 404
    except ValueError:
        if payload is not None:
            payload.close()
        return jsonify(success=False, message='تعذر التصدير لوجود اسم مستخدم غير صالح داخل الحزمة.'), 409
    except Exception:
        if payload is not None:
            payload.close()
        logger.exception('Batch text export failed')
        return jsonify(success=False, message='تعذر تجهيز الملف النصي. أعد المحاولة بعد قليل.'), 503


@vouchers_bp.route('/vouchers/delete-batch/<int:batch_id>', methods=['POST'], endpoint="delete_batch_action")

def delete_batch_action(batch_id):
    try:
        deleted = delete_batch(batch_id, admin_username=session.get('admin_username', 'admin'))
        flash('تم حذف الحزمة ومعالجة الاسترداد وفق خصم الإصدار المثبت.' if deleted else 'الدفعة غير موجودة.', 'info')
    except Exception as e:
        flash(f'خطأ أثناء حذف الدفعة: {str(e)}', 'danger')
    return redirect(url_for('vouchers'))




@vouchers_bp.route('/vouchers/active-users/<int:card_id>/sessions.pdf', endpoint='voucher_sessions_pdf')
def voucher_sessions_pdf(card_id):
    account = query_one('SELECT id,username FROM wisp_vouchers WHERE id=?', (card_id,))
    if not account:
        return jsonify(success=False, message='الحساب غير موجود.'), 404
    from web.session_export import session_pdf_response
    return session_pdf_response(account, 'voucher')

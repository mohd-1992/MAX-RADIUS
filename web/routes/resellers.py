# -*- coding: utf-8 -*-
"""
MAX RADIUS Web - Reseller Networks, Wallets, Roles, and Staff Accounts Routes Module.
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

logger = logging.getLogger('resellers_bp')

from web.access_policy import filter_manager_visibility

from core.ownership import scoped_query_all as query_all, scoped_query_one as query_one, owner_id, generation_owner

resellers_bp = Blueprint('resellers_bp', __name__)


def _financial_request_id():
    reference = request.form.get('request_id', '')
    if not reference:
        raise ValueError('مرجع العملية مفقود؛ حدّث الصفحة ثم أعد المحاولة.')
    return reference


@resellers_bp.route('/managers', endpoint="managers_page")

@require_permission('managers.view')
def managers_page():
    mgrs = get_all_managers()
    roles = get_all_roles()
    kpis = get_manager_kpis()
    mgrs, kpis = filter_manager_visibility(mgrs, kpis)
    packages = query_all("SELECT id, name, price, service_type FROM wisp_packages ORDER BY id ASC")
    return render_template('managers.html', managers=mgrs, roles=roles, kpis=kpis, packages=packages)


@resellers_bp.route('/managers/add', methods=['POST'], endpoint="add_manager_action")

@require_permission('managers.manage')
def add_manager_action():
    try:
        data = request.form.to_dict()
        if not has_permission('managers.billing'):
            for key in ('wallet_balance', 'credit_limit', 'commission_percent', 'payment_type'):
                data.pop(key, None)
        data['allowed_packages'] = request.form.getlist('allowed_packages')
        mgr_id = create_manager(data, admin_user=session.get('user', 'admin'))
        flash('تمت إضافة حساب المدير / الموزع بنجاح وتعيين الصلاحيات.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء إضافة الحساب: {str(e)}', 'danger')
    return redirect(url_for('managers_page'))


@resellers_bp.route('/managers/edit/<int:manager_id>', methods=['POST'], endpoint="edit_manager_action")

@require_permission('managers.manage')
def edit_manager_action(manager_id):
    try:
        data = request.form.to_dict()
        if not has_permission('managers.billing'):
            for key in ('wallet_balance', 'credit_limit', 'commission_percent', 'payment_type'):
                data.pop(key, None)
        data['allowed_packages'] = request.form.getlist('allowed_packages')
        update_manager(manager_id, data, admin_user=session.get('user', 'admin'))
        flash('تم حفظ وتحديث بيانات الحساب والصلاحيات بنجاح.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء تعديل الحساب: {str(e)}', 'danger')
    return redirect(url_for('managers_page'))


@resellers_bp.route('/managers/toggle-status/<int:manager_id>', methods=['POST'], endpoint="toggle_manager_status_action")

@require_permission('managers.manage')
def toggle_manager_status_action(manager_id):
    try:
        new_status = toggle_manager_status(manager_id, admin_user=session.get('user', 'admin'))
        msg = 'تم تفعيل الحساب بنجاح.' if new_status == 1 else 'تم تعطيل الحساب بنجاح.'
        flash(msg, 'info')
    except Exception as e:
        flash(f'خطأ: {str(e)}', 'danger')
    return redirect(url_for('managers_page'))


@resellers_bp.route('/managers/delete/<int:manager_id>', methods=['POST'], endpoint="delete_manager_action")

@require_permission('managers.manage')
def delete_manager_action(manager_id):
    try:
        delete_manager(manager_id, admin_user=session.get('user', 'admin'))
        flash('تم حذف وتعطيل الحساب بنجاح مع الاحتفاظ بكافة القيود المحاسبية التاريخية.', 'warning')
    except Exception as e:
        flash(f'تعذر حذف الحساب: {str(e)}', 'danger')
    return redirect(url_for('managers_page'))


@resellers_bp.route('/managers/<int:manager_id>/deposit', methods=['POST'], endpoint="deposit_manager_action")

@require_permission('managers.billing')
def deposit_manager_action(manager_id):
    try:
        amt = request.form['amount']
        pay_type = request.form.get('payment_type', 'cash')
        notes = request.form.get('notes', '')
        bal_after, inv_num = deposit_manager_wallet(manager_id, amt, payment_type=pay_type, notes=notes, admin_user=session.get('admin_username', 'admin'), request_id=_financial_request_id())
        flash(f'تم إيداع الرصيد بنجاح ({amt}) وإصدار الفاتورة/القيد {inv_num}.', 'success')
    except Exception as e:
        from core.license_feature_policy import FeatureUnavailable
        if isinstance(e,FeatureUnavailable):raise
        flash(f'خطأ أثناء الإيداع: {str(e)}', 'danger')
    return redirect(request.referrer or url_for('managers_page'))


@resellers_bp.route('/managers/<int:manager_id>/deduct', methods=['POST'], endpoint="deduct_manager_action")

@require_permission('managers.billing')
def deduct_manager_action(manager_id):
    try:
        reason = request.form.get('reason', '').strip()
        if reason not in ('سحب نقدي', 'تصحيح رصيد', 'أخرى'):
            raise ValueError('اختر سبب الخصم.')
        if reason == 'أخرى' and not request.form.get('notes', '').strip():
            raise ValueError('اكتب تفاصيل سبب الخصم.')
        amt = request.form['amount']
        notes = reason + (': ' + request.form.get('notes', '').strip() if request.form.get('notes', '').strip() else '')
        bal_after, inv_num = deduct_manager_wallet(manager_id, amt, notes=notes, admin_user=session.get('admin_username', 'admin'), request_id=_financial_request_id())
        flash(f'تم خصم الرصيد بنجاح ({amt}) وتسجيل القيد {inv_num}.', 'info')
    except Exception as e:
        from core.license_feature_policy import FeatureUnavailable
        if isinstance(e,FeatureUnavailable):raise
        flash(f'خطأ أثناء الخصم: {str(e)}', 'danger')
    return redirect(request.referrer or url_for('managers_page'))


@resellers_bp.route('/managers/<int:manager_id>/settle-debt', methods=['POST'], endpoint="settle_manager_debt_action")

@require_permission('managers.billing')
def settle_manager_debt_action(manager_id):
    try:
        amt = request.form['amount']
        pay_type = request.form.get('payment_type', 'cash')
        notes = request.form.get('notes', '')
        rem_debt, inv_num = settle_manager_debt(manager_id, amt, payment_type=pay_type, notes=notes, admin_user=session.get('admin_username', 'admin'), request_id=_financial_request_id())
        flash(f'تم تسجيل سند القبض وسداد الذمة بنجاح بقيمة ({amt}) برقم القيد {inv_num}. الذمة المتبقية: {rem_debt:,.2f}', 'success')
    except Exception as e:
        flash(f'خطأ أثناء سداد الذمة: {str(e)}', 'danger')
    return redirect(request.referrer or url_for('managers_page'))


@resellers_bp.route('/managers/invoices/<int:invoice_id>/void', methods=['POST'], endpoint="void_manager_invoice_action")

@require_permission('managers.billing')
def void_manager_invoice_action(invoice_id):
    try:
        reason = request.form.get('void_reason', '').strip()
        void_manager_invoice(invoice_id, reason, admin_user=session.get('user', 'admin'))
        flash('تم إلغاء الفاتورة/القيد بنجاح وعكس الأثر المالي في المحفظة.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء إلغاء الفاتورة: {str(e)}', 'danger')
    return redirect(request.referrer or url_for('manager_invoices_page'))


@resellers_bp.route('/managers/<int:manager_id>/statement', endpoint="manager_statement_page")

@require_permission('managers.billing')
def manager_statement_page(manager_id):
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')
    pay_type = request.args.get('payment_type')
    tx_type = request.args.get('transaction_type')
    res = get_manager_statement(manager_id, date_from=date_from, date_to=date_to, payment_type=pay_type, transaction_type=tx_type)
    if not res or not res[0]:
        flash('الحساب المطلوب غير موجود.', 'danger')
        return redirect(url_for('managers_page'))
    mgr, invoices, stats = res
    mgr.pop('password_hash', None)
    return render_template('manager_statement.html', manager=mgr, invoices=invoices, stats=stats)


@resellers_bp.route('/managers/invoices', endpoint="manager_invoices_page")

@require_permission('managers.billing')
def manager_invoices_page():
    mgr_id = request.args.get('manager_id')
    tx_type = request.args.get('type')
    pay_type = request.args.get('payment_type')
    page = max(1, request.args.get('page', 1, type=int) or 1)
    invoices = get_all_manager_invoices(limit=101, manager_id=mgr_id, transaction_type=tx_type, payment_type=pay_type, offset=(page-1)*100)
    has_next = len(invoices) > 100
    managers = get_all_managers()
    kpis = get_manager_kpis()
    return render_template('manager_invoices.html', invoices=invoices[:100], managers=managers, kpis=kpis, page=page, has_next=has_next)


@resellers_bp.route('/roles', endpoint="roles_page")

@require_permission('roles.manage')
def roles_page():
    roles = get_all_roles()
    grouped_perms = get_all_permissions_grouped()
    return render_template('roles.html', roles=roles, grouped_permissions=grouped_perms)


@resellers_bp.route('/roles/add', methods=['POST'], endpoint="add_role_action")

@require_permission('roles.manage')
def add_role_action():
    try:
        perm_ids = request.form.getlist('permissions')
        create_role(request.form, permission_ids=perm_ids)
        flash('تم إنشاء الدور وتعيين الصلاحيات المحددة بنجاح.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء إنشاء الدور: {str(e)}', 'danger')
    return redirect(url_for('roles_page'))


@resellers_bp.route('/roles/edit/<int:role_id>', methods=['POST'], endpoint="edit_role_action")

@require_permission('roles.manage')
def edit_role_action(role_id):
    try:
        perm_ids = request.form.getlist('permissions')
        update_role(role_id, request.form, permission_ids=perm_ids)
        flash('تم حفظ وتحديث صلاحيات الدور بنجاح.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء تعديل الدور: {str(e)}', 'danger')
    return redirect(url_for('roles_page'))


@resellers_bp.route('/roles/delete/<int:role_id>', methods=['POST'], endpoint="delete_role_action")

@require_permission('roles.manage')
def delete_role_action(role_id):
    try:
        delete_role(role_id)
        flash('تم حذف الدور بنجاح.', 'warning')
    except Exception as e:
        flash(f'خطأ: {str(e)}', 'danger')
    return redirect(url_for('roles_page'))


@resellers_bp.route('/api/roles/<int:role_id>/permissions', endpoint="api_role_permissions")

@require_permission('roles.manage')
def api_role_permissions(role_id):
    pids = get_role_permission_ids(role_id)
    return jsonify({'success': True, 'permission_ids': pids})


@resellers_bp.route('/resellers', endpoint="resellers")

def resellers():
    return redirect(url_for('managers_page'))


@resellers_bp.route('/resellers/add', methods=['POST'], endpoint="add_reseller_action")

def add_reseller_action():
    return redirect(url_for('managers_page'))


@resellers_bp.route('/resellers/topup', methods=['POST'], endpoint="topup_reseller_action")

def topup_reseller_action():
    return redirect(url_for('managers_page'))


@resellers_bp.route('/reseller-wallets', endpoint="reseller_wallets_page")

@require_permission('managers.billing')
def reseller_wallets_page():
    from services.reseller_wallet_service import get_wallets_overview
    data = get_wallets_overview()
    return render_template('reseller_wallet.html', data=data)


@resellers_bp.route('/api/reseller-wallets/transfer', methods=['POST'], endpoint="api_reseller_wallet_transfer")

@require_permission('managers.billing')
def api_reseller_wallet_transfer():
    try:
        transfer_manager_wallet(request.form.get('from_id'), request.form.get('to_id'), request.form.get('amount'),
                                notes=request.form.get('notes', ''), admin_user=session.get('admin_username', 'admin'),
                                request_id=_financial_request_id())
        return jsonify(success=True, message='تم التحويل وتسجيل القيدين في محافظ المدراء.')
    except (ValueError, TypeError) as exc:
        from core.license_feature_policy import FeatureUnavailable
        if isinstance(exc,FeatureUnavailable):raise
        return jsonify(success=False, message=str(exc)), 400


@resellers_bp.route('/api/reseller-wallets/deposit', methods=['POST'], endpoint="api_reseller_wallet_deposit")

@require_permission('managers.billing')
def api_reseller_wallet_deposit():
    try:
        deposit_manager_wallet(int(request.form.get('reseller_id', 0)), request.form.get('amount'),
                               notes=request.form.get('notes', ''), admin_user=session.get('admin_username', 'admin'),
                               request_id=_financial_request_id())
        return jsonify(success=True, message='تم الإيداع في محفظة المدير وتسجيل فاتورته.')
    except (ValueError, TypeError) as exc:
        from core.license_feature_policy import FeatureUnavailable
        if isinstance(exc,FeatureUnavailable):raise
        return jsonify(success=False, message=str(exc)), 400




@resellers_bp.route('/managers/invoices/<int:invoice_id>/receipt', endpoint='manager_operation_receipt')
@require_permission('managers.billing')
def manager_operation_receipt(invoice_id):
    receipt = get_manager_operation_receipt(invoice_id)
    if not receipt:
        from flask import abort
        abort(404)
    return render_template('manager_operation_receipt.html', receipt=receipt)

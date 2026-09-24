# -*- coding: utf-8 -*-
"""
MAX RADIUS Web - System Settings, Backups, Updates, Diagnostics, and Maintenance Routes Module.
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

logger = logging.getLogger('system_bp')

system_bp = Blueprint('system_bp', __name__)


def _get_target_params():
    """Extracts target_type and target_ids from form-data, query string, or JSON payload."""
    target_type = request.values.get('target_type', 'subscriber')
    ids = (
        request.form.getlist('target_ids[]') or 
        request.form.getlist('target_ids') or 
        request.values.get('target_ids') or 
        request.values.get('target_id') or 
        request.values.get('ids') or 
        request.values.get('id')
    )
    if request.is_json:
        data = request.get_json() or {}
        ids = data.get('target_ids') or data.get('target_id') or data.get('ids') or data.get('id') or ids
        target_type = data.get('target_type', target_type)
    return target_type, ids


@system_bp.route('/api/actions/delete', methods=['POST'], endpoint="api_delete_entity")

def api_delete_entity():
    target_type, ids = _get_target_params()
    success, msg = action_bulk_execute(action_delete_entity, target_type, ids)
    return jsonify({'success': success, 'message': msg})


@system_bp.route('/api/actions/extend-time', methods=['POST'], endpoint="api_extend_time")

def api_extend_time():
    target_type, ids = _get_target_params()
    days = request.form.get('days', 30)
    success, msg = action_bulk_execute(action_extend_time, target_type, ids, days=days)
    return jsonify({'success': success, 'message': msg})


@system_bp.route('/api/actions/terminate', methods=['POST'], endpoint="api_terminate")

def api_terminate():
    target_type, ids = _get_target_params()
    success, msg = action_bulk_execute(action_terminate_subscription, target_type, ids)
    return jsonify({'success': success, 'message': msg})


@system_bp.route('/api/actions/renew', methods=['POST'], endpoint="api_renew")

def api_renew():
    target_type, ids = _get_target_params()
    success, msg = action_bulk_execute(action_renew_package, target_type, ids)
    return jsonify({'success': success, 'message': msg})


@system_bp.route('/api/actions/change-package', methods=['POST'], endpoint="api_change_package")

def api_change_package():
    target_type, ids = _get_target_params()
    new_package_id = request.form.get('new_package_id')
    success, msg = action_bulk_execute(action_change_package, target_type, ids, new_package_id=new_package_id)
    return jsonify({'success': success, 'message': msg})


@system_bp.route('/api/actions/add-quota', methods=['POST'], endpoint="api_add_quota")

def api_add_quota():
    target_type, ids = _get_target_params()
    quota_amount = request.form.get('quota_amount', 0)
    quota_unit = request.form.get('quota_unit', 'GB')
    success, msg = action_bulk_execute(action_add_quota, target_type, ids, quota_amount=quota_amount, quota_unit=quota_unit)
    return jsonify({'success': success, 'message': msg})


@system_bp.route('/api/actions/usage-history', methods=['GET', 'POST'], endpoint="api_usage_history")

def api_usage_history():
    target_type = request.values.get('target_type', 'subscriber')
    target_id = request.values.get('target_id')
    success, msg, data = action_get_usage_history(target_type, target_id)
    return jsonify({'success': success, 'message': msg, 'data': data})


@system_bp.route('/api/actions/add-wallet', methods=['POST'], endpoint="api_add_wallet")

def api_add_wallet():
    target_type, ids = _get_target_params()
    amount = request.form.get('amount', 0)
    notes = request.form.get('notes', '')
    success, msg = action_bulk_execute(action_add_wallet_balance, target_type, ids, amount=amount, notes=notes)
    return jsonify({'success': success, 'message': msg})


@system_bp.route('/api/actions/deduct-wallet', methods=['POST'], endpoint="api_deduct_wallet")

def api_deduct_wallet():
    target_type, ids = _get_target_params()
    amount = request.form.get('amount', 0)
    notes = request.form.get('notes', '')
    success, msg = action_bulk_execute(action_deduct_wallet_balance, target_type, ids, amount=amount, notes=notes)
    return jsonify({'success': success, 'message': msg})


@system_bp.route('/api/actions/disconnect', methods=['POST'], endpoint="api_disconnect")

def api_disconnect():
    target_type, ids = _get_target_params()
    success, msg = action_bulk_execute(action_disconnect_user, target_type, ids)
    return jsonify({'success': success, 'message': msg})


@system_bp.route('/sales', endpoint="sales_reports")

@system_bp.route('/reports/sales', endpoint="sales_reports")

@system_bp.route('/vouchers/sales', endpoint="sales_reports")

def sales_reports():
    import math
    sync_voucher_activations()
    sync_voucher_sales()

    today_str = datetime.date.today().strftime('%Y-%m-%d')
    yesterday_str = (datetime.date.today() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
    month_str = datetime.date.today().strftime('%Y-%m')
    year_str = datetime.date.today().strftime('%Y')

    # Get Filter parameters
    period = request.args.get('period', 'all').strip()
    custom_date_from = request.args.get('date_from', '').strip()
    custom_date_to = request.args.get('date_to', '').strip()
    reseller_id = request.args.get('reseller_id', '').strip()
    package_name = request.args.get('package_name', '').strip()
    search_q = request.args.get('q', '').strip()
    try:
        page = max(1, int(request.args.get('page', 1)))
    except (ValueError, TypeError):
        page = 1
    per_page = 50

    # Determine date range boundaries
    start_date = None
    end_date = None

    if period == 'today':
        start_date = f"{today_str} 00:00:00"
        end_date = f"{today_str} 23:59:59"
    elif period == 'yesterday':
        start_date = f"{yesterday_str} 00:00:00"
        end_date = f"{yesterday_str} 23:59:59"
    elif period == 'this_week':
        start_of_week = (datetime.date.today() - datetime.timedelta(days=datetime.date.today().weekday())).strftime('%Y-%m-%d')
        start_date = f"{start_of_week} 00:00:00"
        end_date = f"{today_str} 23:59:59"
    elif period == 'this_month':
        start_date = f"{month_str}-01 00:00:00"
        end_date = f"{today_str} 23:59:59"
    elif period == 'last_month':
        first_day_this_month = datetime.date.today().replace(day=1)
        last_day_prev_month = first_day_this_month - datetime.timedelta(days=1)
        first_day_prev_month = last_day_prev_month.replace(day=1)
        start_date = f"{first_day_prev_month.strftime('%Y-%m-%d')} 00:00:00"
        end_date = f"{last_day_prev_month.strftime('%Y-%m-%d')} 23:59:59"
    elif period == 'this_year':
        start_date = f"{year_str}-01-01 00:00:00"
        end_date = f"{today_str} 23:59:59"
    elif period == 'custom' and (custom_date_from or custom_date_to):
        if custom_date_from:
            start_date = f"{custom_date_from} 00:00:00"
        if custom_date_to:
            end_date = f"{custom_date_to} 23:59:59"

    # Build SQL Where Clauses
    where_clauses = ["1=1"]
    params = []

    if start_date:
        where_clauses.append("s.activated_at >= ?")
        params.append(start_date)
    if end_date:
        where_clauses.append("s.activated_at <= ?")
        params.append(end_date)
    if reseller_id and reseller_id != 'all':
        if reseller_id == 'direct':
            where_clauses.append("s.reseller_id IS NULL")
        else:
            where_clauses.append("s.reseller_id = ?")
            params.append(int(reseller_id))
    if package_name and package_name != 'all':
        where_clauses.append("s.package_name = ?")
        params.append(package_name)
    if search_q:
        where_clauses.append("(s.username LIKE ? OR s.serial_number LIKE ? OR s.batch_name LIKE ?)")
        wildcard = f"%{search_q}%"
        params.extend([wildcard, wildcard, wildcard])

    where_sql = " AND ".join(where_clauses)

    # 1. Period Metrics from filtered wisp_voucher_sales
    # ISP Logic:
    # - gross_sales = SUM(price) (سعر المشترك)
    # - net_isp_revenue = SUM(CASE WHEN reseller_id IS NOT NULL AND cost > 0 THEN cost ELSE price END) (حصة صاحب الشبكة)
    # - total_reseller_commissions = SUM(CASE WHEN reseller_id IS NOT NULL AND cost > 0 THEN (price - cost) ELSE 0 END) (عمولات الموزعين)
    metrics_query = f"""
        SELECT 
            COUNT(*) as total_count,
            COALESCE(SUM(price), 0) as gross_sales,
            COALESCE(SUM(CASE WHEN reseller_id IS NOT NULL AND cost > 0 THEN cost ELSE price END), 0) as net_isp_revenue,
            COALESCE(SUM(CASE WHEN reseller_id IS NOT NULL AND cost > 0 THEN (price - cost) ELSE 0 END), 0) as total_reseller_commissions
        FROM wisp_voucher_sales s
        WHERE {where_sql}
    """
    period_metrics = query_one(metrics_query, tuple(params)) or {}
    gross_sales = round(float(period_metrics.get('gross_sales') or 0), 2)
    net_isp_revenue = round(float(period_metrics.get('net_isp_revenue') or 0), 2)
    total_reseller_commissions = round(float(period_metrics.get('total_reseller_commissions') or 0), 2)
    total_items_count = int(period_metrics.get('total_count') or 0)
    
    isp_share_pct = round((net_isp_revenue / gross_sales * 100), 1) if gross_sales > 0 else 0
    reseller_share_pct = round((total_reseller_commissions / gross_sales * 100), 1) if gross_sales > 0 else 0

    # 2. Lifetime & Today's Net ISP Overview (Optimized Single Combined Query)
    today_start = f"{today_str} 00:00:00"
    today_end = f"{today_str} 23:59:59"
    month_start = f"{month_str}-01 00:00:00"
    month_end = f"{month_str}-31 23:59:59"

    overview_vouchers = query_one("""
        SELECT 
            COUNT(*) as total_sold_cards,
            COALESCE(SUM(CASE WHEN reseller_id IS NOT NULL AND cost > 0 THEN cost ELSE price END), 0) as lifetime_isp_sales,
            COALESCE(SUM(price), 0) as lifetime_gross_sales,
            
            COALESCE(SUM(CASE WHEN activated_at >= ? AND activated_at <= ? AND reseller_id IS NOT NULL AND cost > 0 THEN cost 
                              WHEN activated_at >= ? AND activated_at <= ? THEN price ELSE 0 END), 0) as month_isp_sales,
            
            COALESCE(SUM(CASE WHEN activated_at >= ? AND activated_at <= ? AND reseller_id IS NOT NULL AND cost > 0 THEN cost 
                              WHEN activated_at >= ? AND activated_at <= ? THEN price ELSE 0 END), 0) as today_isp_sales,
            COALESCE(SUM(CASE WHEN activated_at >= ? AND activated_at <= ? THEN price ELSE 0 END), 0) as today_gross_sales,
            COALESCE(SUM(CASE WHEN activated_at >= ? AND activated_at <= ? THEN 1 ELSE 0 END), 0) as today_cards_count
        FROM wisp_voucher_sales
    """, (month_start, month_end, month_start, month_end,
          today_start, today_end, today_start, today_end,
          today_start, today_end,
          today_start, today_end)) or {}

    inv_stats = query_one("""
        SELECT 
            COALESCE(SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END), 0) as subs_total,
            COALESCE(SUM(CASE WHEN status = 'paid' AND paid_at >= ? AND paid_at <= ? THEN amount ELSE 0 END), 0) as month_inv_sales,
            COALESCE(SUM(CASE WHEN status = 'paid' AND paid_at >= ? AND paid_at <= ? THEN amount ELSE 0 END), 0) as today_inv_sales
        FROM wisp_invoices
    """, (month_start, month_end, today_start, today_end)) or {}

    lifetime_isp_sales = round(float(overview_vouchers.get('lifetime_isp_sales') or 0), 2)
    lifetime_gross_sales = round(float(overview_vouchers.get('lifetime_gross_sales') or 0), 2)
    total_sold_cards = int(overview_vouchers.get('total_sold_cards') or 0)

    month_isp_sales = round(float(overview_vouchers.get('month_isp_sales') or 0) + float(inv_stats.get('month_inv_sales') or 0), 2)
    today_isp_sales = round(float(overview_vouchers.get('today_isp_sales') or 0) + float(inv_stats.get('today_inv_sales') or 0), 2)
    today_gross_sales = round(float(overview_vouchers.get('today_gross_sales') or 0) + float(inv_stats.get('today_inv_sales') or 0), 2)
    today_cards_count = int(overview_vouchers.get('today_cards_count') or 0)
    subs_total = round(float(inv_stats.get('subs_total') or 0), 2)

    # 3. Pagination calculation
    total_pages = max(1, math.ceil(total_items_count / per_page))
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page

    # 4. Filtered Sales Records
    list_query = f"""
        SELECT s.*, 
               COALESCE(NULLIF(m.full_name, ''), m.username, 'مبيعات مباشرة (الإدارة)') as reseller_name,
               m.username as reseller_username,
               CASE WHEN s.reseller_id IS NOT NULL AND s.cost > 0 THEN s.cost ELSE s.price END as isp_share,
               CASE WHEN s.reseller_id IS NOT NULL AND s.cost > 0 THEN (s.price - s.cost) ELSE 0 END as reseller_margin
        FROM wisp_voucher_sales s
        LEFT JOIN wisp_managers m ON s.reseller_id = m.id
        WHERE {where_sql}
        ORDER BY s.id DESC
        LIMIT {per_page} OFFSET {offset}
    """
    sales_list = query_all(list_query, tuple(params)) or []

    # 5. Package Breakdown (Optimized GROUP BY queries with zero CPU scan)
    voucher_counts = {r['package_name']: r['c'] for r in query_all("SELECT package_name, COUNT(*) as c FROM wisp_voucher_sales GROUP BY package_name") if r.get('package_name')}
    invoice_counts = {r['package_name']: r['c'] for r in query_all("SELECT package_name, COUNT(*) as c FROM wisp_invoices WHERE status = 'paid' GROUP BY package_name") if r.get('package_name')}

    pkgs = query_all("SELECT name, service_type, price, cost FROM wisp_packages ORDER BY name ASC") or []
    for p in pkgs:
        p_name = p.get('name') or ''
        count = voucher_counts.get(p_name, 0) + invoice_counts.get(p_name, 0)
        p['sold_count'] = count
        p_price = float(p.get('price') or 0)
        p_cost = float(p.get('cost') or 0)
        p['isp_unit_price'] = p_cost if p_cost > 0 else p_price
        p['reseller_unit_commission'] = max(0, p_price - p_cost) if p_cost > 0 else 0
        p['total_gross_revenue'] = round(count * p_price, 2)
        p['total_isp_revenue'] = round(count * p['isp_unit_price'], 2)
        p['total_reseller_profit'] = round(count * p['reseller_unit_commission'], 2)

    # 6. Filter Options (Resellers & Packages)
    resellers_list = query_all("""
        SELECT id, username, full_name,
               COALESCE(NULLIF(full_name, ''), username) as name
        FROM wisp_managers 
        WHERE is_active = 1 
        ORDER BY id ASC
    """) or []
    all_packages_list = query_all("SELECT DISTINCT name FROM wisp_packages ORDER BY name ASC") or []

    sales_chart = get_sales_chart_data()
    traffic_chart = get_traffic_chart_data()

    return render_template('sales_reports.html',
                           period=period,
                           custom_date_from=custom_date_from,
                           custom_date_to=custom_date_to,
                           selected_reseller=reseller_id,
                           selected_package=package_name,
                           search_q=search_q,
                           page=page,
                           total_pages=total_pages,
                           total_items_count=total_items_count,
                           per_page=per_page,
                           gross_sales=gross_sales,
                           net_isp_revenue=net_isp_revenue,
                           total_reseller_commissions=total_reseller_commissions,
                           isp_share_pct=isp_share_pct,
                           reseller_share_pct=reseller_share_pct,
                           today_isp_sales=today_isp_sales,
                           today_gross_sales=today_gross_sales,
                           today_cards_count=today_cards_count,
                           month_isp_sales=month_isp_sales,
                           lifetime_isp_sales=lifetime_isp_sales,
                           lifetime_gross_sales=lifetime_gross_sales,
                           subs_total=subs_total,
                           total_sold_cards=total_sold_cards,
                           package_breakdown=pkgs,
                           recent_sales=sales_list,
                           resellers_list=resellers_list,
                           all_packages_list=all_packages_list,
                           sales_chart=sales_chart,
                           traffic_chart=traffic_chart)


@system_bp.route('/sales/export', endpoint="export_sales")

@system_bp.route('/reports/sales/export', endpoint="export_sales")

def export_sales():
    import io
    import csv
    from flask import Response

    period = request.args.get('period', 'all').strip()
    custom_date_from = request.args.get('date_from', '').strip()
    custom_date_to = request.args.get('date_to', '').strip()
    reseller_id = request.args.get('reseller_id', '').strip()
    package_name = request.args.get('package_name', '').strip()
    search_q = request.args.get('q', '').strip()
    export_format = request.args.get('format', 'excel').lower().strip()

    today_str = datetime.date.today().strftime('%Y-%m-%d')
    yesterday_str = (datetime.date.today() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
    month_str = datetime.date.today().strftime('%Y-%m')
    year_str = datetime.date.today().strftime('%Y')

    start_date = None
    end_date = None

    if period == 'today':
        start_date = f"{today_str} 00:00:00"
        end_date = f"{today_str} 23:59:59"
    elif period == 'yesterday':
        start_date = f"{yesterday_str} 00:00:00"
        end_date = f"{yesterday_str} 23:59:59"
    elif period == 'this_week':
        start_of_week = (datetime.date.today() - datetime.timedelta(days=datetime.date.today().weekday())).strftime('%Y-%m-%d')
        start_date = f"{start_of_week} 00:00:00"
        end_date = f"{today_str} 23:59:59"
    elif period == 'this_month':
        start_date = f"{month_str}-01 00:00:00"
        end_date = f"{today_str} 23:59:59"
    elif period == 'last_month':
        first_day_this_month = datetime.date.today().replace(day=1)
        last_day_prev_month = first_day_this_month - datetime.timedelta(days=1)
        first_day_prev_month = last_day_prev_month.replace(day=1)
        start_date = f"{first_day_prev_month.strftime('%Y-%m-%d')} 00:00:00"
        end_date = f"{last_day_prev_month.strftime('%Y-%m-%d')} 23:59:59"
    elif period == 'this_year':
        start_date = f"{year_str}-01-01 00:00:00"
        end_date = f"{today_str} 23:59:59"
    elif period == 'custom' and (custom_date_from or custom_date_to):
        if custom_date_from:
            start_date = f"{custom_date_from} 00:00:00"
        if custom_date_to:
            end_date = f"{custom_date_to} 23:59:59"

    where_clauses = ["1=1"]
    params = []

    if start_date:
        where_clauses.append("s.activated_at >= ?")
        params.append(start_date)
    if end_date:
        where_clauses.append("s.activated_at <= ?")
        params.append(end_date)
    if reseller_id and reseller_id != 'all':
        if reseller_id == 'direct':
            where_clauses.append("s.reseller_id IS NULL")
        else:
            where_clauses.append("s.reseller_id = ?")
            params.append(int(reseller_id))
    if package_name and package_name != 'all':
        where_clauses.append("s.package_name = ?")
        params.append(package_name)
    if search_q:
        where_clauses.append("(s.username LIKE ? OR s.serial_number LIKE ? OR s.batch_name LIKE ?)")
        wildcard = f"%{search_q}%"
        params.extend([wildcard, wildcard, wildcard])

    where_sql = " AND ".join(where_clauses)

    records = query_all(f"""
        SELECT s.*, 
               COALESCE(NULLIF(m.full_name, ''), m.username, 'مبيعات مباشرة (الإدارة)') as reseller_name,
               m.username as reseller_username,
               CASE WHEN s.reseller_id IS NOT NULL AND s.cost > 0 THEN s.cost ELSE s.price END as isp_share,
               CASE WHEN s.reseller_id IS NOT NULL AND s.cost > 0 THEN (s.price - s.cost) ELSE 0 END as reseller_margin
        FROM wisp_voucher_sales s
        LEFT JOIN wisp_managers m ON s.reseller_id = m.id
        WHERE {where_sql}
        ORDER BY s.id DESC
        LIMIT 10000
    """, tuple(params)) or []

    file_timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')

    if export_format == 'csv':
        output = io.StringIO()
        output.write('\ufeff')  # UTF-8 BOM
        writer = csv.writer(output)
        writer.writerow(['م', 'اسم المستخدم / الكرت', 'الرقم التسلسلي', 'الحزمة / الدفعة', 'الباقة', 'سعر المشترك', 'حصة الشبكة', 'عمولة الموزع', 'نقطة البيع / الموزع', 'تاريخ ووقت التفعيل'])
        for idx, r in enumerate(records, 1):
            writer.writerow([
                idx,
                r.get('username') or '',
                r.get('serial_number') or '',
                r.get('batch_name') or '',
                r.get('package_name') or '',
                float(r.get('price') or 0),
                float(r.get('isp_share') or 0),
                float(r.get('reseller_margin') or 0),
                r.get('reseller_name') or 'مباشر (الإدارة)',
                str(r.get('activated_at') or '')
            ])
        return Response(output.getvalue(), mimetype='text/csv; charset=utf-8', headers={
            'Content-Disposition': f'attachment; filename=MAX_RADIUS_Sales_{file_timestamp}.csv'
        })

    # Excel export via openpyxl
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "كشف المبيعات والإيرادات"
        ws.views.sheetView[0].rightToLeft = True

        # Header Title
        ws.merge_cells('A1:J1')
        title_cell = ws['A1']
        title_cell.value = "📊 MAX RADIUS 2.0 - كشف مبيعات الكروت وحصة الشبكة وعمولات الموزعين"
        title_cell.font = Font(name='Arial', size=15, bold=True, color='FFFFFF')
        title_cell.fill = PatternFill(start_color='0F172A', end_color='0F172A', fill_type='solid')
        title_cell.alignment = Alignment(horizontal='center', vertical='center')
        ws.row_dimensions[1].height = 40

        # Sub-header with export info
        ws.merge_cells('A2:J2')
        sub_cell = ws['A2']
        sub_cell.value = f"تاريخ التصدير: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} | الفترة: {period} | إجمالي السجلات: {len(records)}"
        sub_cell.font = Font(name='Arial', size=10, italic=True, color='64748B')
        sub_cell.alignment = Alignment(horizontal='center', vertical='center')
        ws.row_dimensions[2].height = 24

        headers = ['#', 'اسم الكرت / المستخدم', 'الرقم التسلسلي', 'الحزمة / الدفعة', 'الباقة', 'سعر المشترك', 'حصة الشبكة', 'عمولة الموزع', 'نقطة البيع / الموزع', 'تاريخ ووقت التفعيل']
        ws.append([])  # Row 3 empty spacer
        ws.append(headers)  # Row 4
        ws.row_dimensions[4].height = 28

        header_fill = PatternFill(start_color='059669', end_color='059669', fill_type='solid')
        header_font = Font(name='Arial', size=11, bold=True, color='FFFFFF')
        thin_border = Border(
            left=Side(style='thin', color='CBD5E1'),
            right=Side(style='thin', color='CBD5E1'),
            top=Side(style='thin', color='CBD5E1'),
            bottom=Side(style='thin', color='CBD5E1')
        )

        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=4, column=col_idx)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal='center', vertical='center')
            cell.border = thin_border

        total_gross = 0
        total_isp = 0
        total_comm = 0

        for idx, r in enumerate(records, 1):
            p = float(r.get('price') or 0)
            isp_s = float(r.get('isp_share') or 0)
            res_m = float(r.get('reseller_margin') or 0)
            total_gross += p
            total_isp += isp_s
            total_comm += res_m

            row_data = [
                idx,
                r.get('username') or '',
                r.get('serial_number') or '',
                r.get('batch_name') or '',
                r.get('package_name') or '',
                p,
                isp_s,
                res_m,
                r.get('reseller_name') or 'مباشر (الإدارة)',
                str(r.get('activated_at') or '')
            ]
            ws.append(row_data)
            curr_row = ws.max_row
            ws.row_dimensions[curr_row].height = 22
            for col_idx in range(1, len(headers) + 1):
                cell = ws.cell(row=curr_row, column=col_idx)
                cell.border = thin_border
                cell.font = Font(name='Arial', size=10)
                if col_idx in [1, 6, 7, 8, 10]:
                    cell.alignment = Alignment(horizontal='center', vertical='center')
                else:
                    cell.alignment = Alignment(horizontal='right', vertical='center')

        # Totals Row
        tot_row_idx = ws.max_row + 1
        ws.row_dimensions[tot_row_idx].height = 28
        ws.merge_cells(f'A{tot_row_idx}:E{tot_row_idx}')
        tot_label = ws[f'A{tot_row_idx}']
        tot_label.value = "الإجمالي العام المحقق"
        tot_label.font = Font(name='Arial', size=11, bold=True, color='0F172A')
        tot_label.alignment = Alignment(horizontal='center', vertical='center')
        tot_label.fill = PatternFill(start_color='E2E8F0', end_color='E2E8F0', fill_type='solid')

        tot_gross_cell = ws.cell(row=tot_row_idx, column=6, value=total_gross)
        tot_isp_cell = ws.cell(row=tot_row_idx, column=7, value=total_isp)
        tot_comm_cell = ws.cell(row=tot_row_idx, column=8, value=total_comm)

        for cell in [tot_gross_cell, tot_isp_cell, tot_comm_cell]:
            cell.font = Font(name='Arial', size=11, bold=True, color='047857')
            cell.alignment = Alignment(horizontal='center', vertical='center')
            cell.fill = PatternFill(start_color='D1FAE5', end_color='D1FAE5', fill_type='solid')

        for col_idx in range(1, len(headers) + 1):
            ws.cell(row=tot_row_idx, column=col_idx).border = thin_border

        # Auto-fit columns
        for col in ws.columns:
            max_len = max(len(str(cell.value or '')) for cell in col)
            col_letter = get_column_letter(col[0].column)
            ws.column_dimensions[col_letter].width = max(max_len + 4, 14)

        out_stream = io.BytesIO()
        wb.save(out_stream)
        out_stream.seek(0)

        return Response(
            out_stream.getvalue(),
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={'Content-Disposition': f'attachment; filename=MAX_RADIUS_Sales_Report_{file_timestamp}.xlsx'}
        )
    except Exception as e:
        logger.error(f"[Export Sales] Excel error: {e}")
        return jsonify({'error': str(e)}), 500


@system_bp.route('/settings', endpoint="settings")

def settings():
    settings_rows = query_all('SELECT `key`, `value`, `description` FROM wisp_system_settings')
    audit_logs = query_all('SELECT * FROM wisp_audit_logs ORDER BY id DESC LIMIT 30')
    admins = query_all('SELECT id, username, full_name, role, is_active, created_at FROM wisp_admins')
    return render_template('settings.html', settings_list=settings_rows, audit_logs=audit_logs, admins=admins)


@system_bp.route('/settings/update', methods=['POST'], endpoint="update_settings_action")

def update_settings_action():
    try:
        f = request.form

        # 1. Handle Logo Upload
        if 'network_logo' in request.files:
            file = request.files['network_logo']
            if file and file.filename:
                ext = os.path.splitext(file.filename)[1].lower()
                if ext in ['.png', '.jpg', '.jpeg', '.svg', '.webp', '.ico']:
                    upload_dir = os.path.join(current_app.static_folder, 'uploads')
                    os.makedirs(upload_dir, exist_ok=True)
                    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
                    filename = f"logo_{timestamp}{ext}"
                    filepath = os.path.join(upload_dir, filename)
                    file.save(filepath)

                    existing = query_one('SELECT `key` FROM wisp_system_settings WHERE `key` = ?', ('network_logo',))
                    if existing:
                        execute_write('UPDATE wisp_system_settings SET `value` = ? WHERE `key` = ?', (filename, 'network_logo'))
                    else:
                        execute_write('INSERT INTO wisp_system_settings (`key`, `value`, `description`) VALUES (?, ?, ?)', ('network_logo', filename, 'مسار شعار الشبكة'))

        # 2. Currency Mapping
        selected_currency = f.get('currency', 'SAR').strip()
        custom_symbol = f.get('currency_symbol', '').strip()

        currency_map = {
            'SAR': 'ر.س',
            'YER': 'ر.ي',
            'AED': 'د.إ',
            'EGP': 'ج.م',
            'USD': '$'
        }
        currency_symbol = custom_symbol if custom_symbol else currency_map.get(selected_currency, 'ر.س')

        # 3. Settings to Save
        network_name = f.get('network_name', '').strip() or f.get('company_name', '').strip() or 'MAX RADIUS'
        timezone_val = f.get('timezone', 'Asia/Aden').strip()

        settings_to_save = {
            'network_name': network_name,
            'company_name': network_name,
            'isp_name': network_name,
            'system_title': f.get('system_title', 'MAX RADIUS - نظام إدارة الشبكات والفوترة و FreeRADIUS').strip(),
            'currency': selected_currency,
            'currency_symbol': currency_symbol,
            'timezone': timezone_val,
            'support_phone': f.get('support_phone', '').strip(),
            'support_email': f.get('support_email', '').strip(),
            'address': f.get('address', '').strip(),
            'hotspot_domain': f.get('hotspot_domain', 'wifi.hotspot').strip(),
            'default_coa_port': f.get('default_coa_port', '3799').strip(),
            'sms_gateway_url': f.get('sms_gateway_url', '').strip(),
            'sms_api_key': f.get('sms_api_key', '').strip(),
            'sms_sender_id': f.get('sms_sender_id', 'WISP-NET').strip(),
            'portal_allow_registration': '1' if f.get('portal_allow_registration') in ('1', 'on', 'true', True, 1) else '0',
            'portal_allow_package_change': '1' if f.get('portal_allow_package_change') in ('1', 'on', 'true', True, 1) else '0',
            'portal_allow_password_change': '1' if f.get('portal_allow_password_change') in ('1', 'on', 'true', True, 1) else '0',
            'allow_data_loan': '1' if f.get('allow_data_loan') in ('1', 'on', 'true', True, 1) else '0',
            'loan_amount_mb': str(max(10, int(f.get('loan_amount_mb', '1024').strip()))) if f.get('loan_amount_mb', '').strip().isdigit() else '1024',
            'loan_threshold_mb': str(max(1, int(f.get('loan_threshold_mb', '100').strip()))) if f.get('loan_threshold_mb', '').strip().isdigit() else '100'
        }

        for k, v in settings_to_save.items():
            existing = query_one('SELECT `key` FROM wisp_system_settings WHERE `key` = ?', (k,))
            if existing:
                execute_write('UPDATE wisp_system_settings SET `value` = ? WHERE `key` = ?', (v, k))
            else:
                execute_write('INSERT INTO wisp_system_settings (`key`, `value`, `description`) VALUES (?, ?, ?)', (k, v, 'إعداد نظام'))

        # Set runtime timezone & reload scheduler
        try:
            from core.time_service import update_system_timezone
            update_system_timezone(timezone_val)
            reload_backup_schedule()
        except Exception:
            pass

        log_audit(1, 'admin', 'UPDATE_SETTINGS', 'settings', f'Updated settings. Name: {network_name}, Currency: {selected_currency} ({currency_symbol}), Timezone: {timezone_val}')
        flash(f'تم حفظ وتطبيق كافة إعدادات النظام والهوية والعملة ({currency_symbol}) والمنطقة الزمنية ({timezone_val}) بنجاح.', 'success')
    except Exception as e:
        flash(f'خطأ أثناء حفظ الإعدادات: {str(e)}', 'danger')
    return redirect(url_for('settings'))


@system_bp.route('/api/time/status', methods=['GET'], endpoint="api_time_status")

def api_time_status():
    status = get_time_sync_status()
    return jsonify({'success': True, 'data': status})


@system_bp.route('/api/time/sync', methods=['POST'], endpoint="api_time_sync")

def api_time_sync():
    success, offset, server, msg = sync_ntp_time(timeout=3.0)
    if success:
        reload_backup_schedule()
    status = get_time_sync_status()
    return jsonify({'success': success, 'message': msg, 'data': status})


@system_bp.route('/system-services', endpoint="system_services_page")

@system_bp.route('/tools/system-services', endpoint="system_services_page")

def system_services_page():
    status_data = get_all_services_status()
    return render_template('system_services.html', status_data=status_data)


@system_bp.route('/api/system-services/status', endpoint="api_system_services_status")

def api_system_services_status():
    data = get_all_services_status()
    return jsonify({'success': True, 'data': data})


@system_bp.route('/api/system-services/action', methods=['POST'], endpoint="api_system_services_action")

def api_system_services_action():
    service_id = request.values.get('service_id', '').strip()
    action = request.values.get('action', '').strip()
    if not service_id or not action:
        return jsonify({'success': False, 'message': 'يرجى تحديد الخدمة والإجراء المطلوب.'})
    success, msg = execute_service_action(service_id, action, admin_username='admin')
    log_audit(1, 'admin', f'SERVICE_{action.upper()}', 'services', f'Triggered {action} on {service_id}: {msg}')
    return jsonify({'success': success, 'message': msg})


@system_bp.route('/api/system-services/watchdog/check', methods=['POST'], endpoint="api_watchdog_manual_check")

def api_watchdog_manual_check():
    from core.watchdog import run_watchdog_cycle
    res = run_watchdog_cycle()
    return jsonify({'success': True, 'message': 'تم إجراء فحص المراقبة الذاتية بنجاح.', 'data': res})


@system_bp.route('/api/system-services/alerts/resolve', methods=['POST'], endpoint="api_resolve_system_alert")

def api_resolve_system_alert():
    from core.watchdog import resolve_system_alert
    alert_id = request.values.get('alert_id')
    if not alert_id:
        return jsonify({'success': False, 'message': 'يرجى تحديد معرف التنبيه.'})
    success, msg = resolve_system_alert(int(alert_id))
    return jsonify({'success': success, 'message': msg})


@system_bp.route('/api/system-services/alerts/clear', methods=['POST'], endpoint="api_clear_resolved_alerts")

def api_clear_resolved_alerts():
    from core.watchdog import clear_all_resolved_alerts
    success, msg = clear_all_resolved_alerts()
    return jsonify({'success': success, 'message': msg})


@system_bp.route('/tools/autoheal', endpoint="autoheal_page")

def autoheal_page():
    from services.autoheal_service import get_autoheal_dashboard_full
    data = get_autoheal_dashboard_full()
    return render_template('autoheal.html', data=data)


@system_bp.route('/api/tools/autoheal/status', endpoint="api_autoheal_status")

def api_autoheal_status():
    from services.autoheal_service import get_autoheal_dashboard_full
    data = get_autoheal_dashboard_full()
    return jsonify({'success': True, 'data': data})


@system_bp.route('/api/tools/autoheal/diagnostic', methods=['POST'], endpoint="api_autoheal_diagnostic")

def api_autoheal_diagnostic():
    from services.autoheal_service import run_deep_system_diagnostic
    diagnostic = run_deep_system_diagnostic()
    log_audit(1, 'admin', 'AUTOHEAL_DIAGNOSTIC_RUN', 'tools', f'Deep diagnostic ran. Health score: {diagnostic.get("score")}%')
    return jsonify({'success': True, 'diagnostic': diagnostic})


@system_bp.route('/api/tools/autoheal/probe-ports', methods=['POST'], endpoint="api_autoheal_probe_ports")

def api_autoheal_probe_ports():
    from services.autoheal_service import probe_all_network_ports
    ports = probe_all_network_ports()
    return jsonify({'success': True, 'ports': ports})


@system_bp.route('/api/tools/autoheal/restart', methods=['POST'], endpoint="api_autoheal_restart")

def api_autoheal_restart():
    req_data = request.json if request.is_json else request.form.to_dict()
    container_name = req_data.get('container', '').strip()
    from services.autoheal_service import restart_system_container
    success, msg = restart_system_container(container_name)
    if success:
        log_audit(1, 'admin', 'CONTAINER_RESTART', 'tools', f'Restarted container: {container_name}')
    return jsonify({'success': success, 'message': msg})


@system_bp.route('/api/tools/autoheal/logs', endpoint="api_autoheal_logs")

def api_autoheal_logs():
    container_name = request.args.get('container', 'max_radius_autoheal').strip()
    lines = int(request.args.get('lines', 80))
    from services.autoheal_service import get_live_container_logs
    logs = get_live_container_logs(container_name, lines=lines)
    return jsonify({'success': True, 'logs': logs})


@system_bp.route('/api/tools/autoheal/purge-zombies', methods=['POST'], endpoint="api_autoheal_purge_zombies")
def api_autoheal_purge_zombies():
    req_data = request.json if request.is_json else request.form.to_dict()
    timeout = int(req_data.get('timeout_minutes', 15))
    from services.autoheal_service import purge_stale_zombie_sessions
    success, msg = purge_stale_zombie_sessions(timeout_minutes=timeout)
    if success:
        log_audit(1, 'admin', 'ZOMBIE_PURGE', 'tools', f'Purged stale sessions older than {timeout}m')
    return jsonify({'success': success, 'message': msg})


@system_bp.route('/api/tools/autoheal/save-settings', methods=['POST'], endpoint="api_autoheal_save_settings")
def api_autoheal_save_settings():
    req_data = request.json if request.is_json else request.form.to_dict()
    timeout = int(req_data.get('timeout_minutes', 15))
    from services.autoheal_service import set_zombie_session_timeout
    success, msg = set_zombie_session_timeout(timeout)
    if success:
        log_audit(1, 'admin', 'AUTOHEAL_SETTINGS_UPDATE', 'tools', f'Updated zombie timeout to {timeout}m')
    return jsonify({'success': success, 'message': msg})


@system_bp.route('/api/tools/autoheal/alerts/resolve', methods=['POST'], endpoint="api_autoheal_resolve_alert")

def api_autoheal_resolve_alert():
    req_data = request.json if request.is_json else request.form.to_dict()
    alert_id = int(req_data.get('alert_id', 0))
    from core.watchdog import resolve_system_alert
    success, msg = resolve_system_alert(alert_id)
    return jsonify({'success': success, 'message': msg})


@system_bp.route('/api/tools/autoheal/alerts/clear-resolved', methods=['POST'], endpoint="api_autoheal_clear_resolved_alerts")

def api_autoheal_clear_resolved_alerts():
    from core.watchdog import clear_all_resolved_alerts
    success, msg = clear_all_resolved_alerts()
    return jsonify({'success': success, 'message': msg})


@system_bp.route('/tools/database-maintenance', endpoint="database_maintenance_page")

def database_maintenance_page():
    from services.db_maintenance_service import get_maintenance_stats
    stats = get_maintenance_stats()
    return render_template('database_maintenance.html', stats=stats)


@system_bp.route('/api/tools/database-maintenance/stats', endpoint="api_maintenance_stats")

def api_maintenance_stats():
    from services.db_maintenance_service import get_maintenance_stats
    stats = get_maintenance_stats()
    return jsonify({'success': True, 'stats': stats})


@system_bp.route('/api/tools/database-maintenance/audit', methods=['POST'], endpoint="api_run_database_audit")

def api_run_database_audit():
    from services.db_maintenance_service import run_database_audit
    audit = run_database_audit()
    return jsonify({'success': True, 'audit': audit})


@system_bp.route('/api/tools/database-maintenance/fix', methods=['POST'], endpoint="api_fix_audit_issue")

def api_fix_audit_issue():
    data = request.json if request.is_json else request.form.to_dict()
    action_code = data.get('action_code')
    username = data.get('username')
    session_id = data.get('session_id')
    
    from services.db_maintenance_service import fix_audit_issue
    res = fix_audit_issue(action_code, username, {'session_id': session_id})
    if res.get('success'):
        log_audit(1, 'admin', 'DB_AUDIT_FIX', 'tools', f'Fixed {action_code} for user {username}')
    return jsonify(res)


@system_bp.route('/api/tools/database-maintenance/fix-all', methods=['POST'], endpoint="api_fix_all_audit_issues")

def api_fix_all_audit_issues():
    from services.db_maintenance_service import fix_all_audit_issues
    res = fix_all_audit_issues()
    log_audit(1, 'admin', 'DB_AUDIT_FIX_ALL', 'tools', f'Fixed all issues: {res.get("fixed_count")} resolved')
    return jsonify(res)


@system_bp.route('/api/tools/database-maintenance/clean-expired', methods=['POST'], endpoint="api_clean_expired_vouchers")

def api_clean_expired_vouchers():
    data = request.json if request.is_json else request.form.to_dict()
    delete_type = data.get('delete_type', 'all')
    delete_acct = bool(data.get('delete_acct', False))
    
    from services.db_maintenance_service import delete_expired_vouchers
    res = delete_expired_vouchers(delete_type=delete_type, delete_acct=delete_acct)
    if res.get('success'):
        log_audit(1, 'admin', 'DB_CASCADE_CLEAN', 'tools', f'Deleted expired vouchers ({delete_type}): {res.get("deleted_vouchers")} vouchers, {res.get("deleted_radcheck")} radcheck, {res.get("deleted_radusergroup")} radusergroup')
    return jsonify(res)


@system_bp.route('/api/tools/database-maintenance/table-sizes', endpoint="api_table_sizes")

def api_table_sizes():
    from services.db_maintenance_service import get_detailed_table_sizes
    res = get_detailed_table_sizes()
    return jsonify(res)


@system_bp.route('/api/tools/database-maintenance/prune-logs', methods=['POST'], endpoint="api_prune_historical_logs")

def api_prune_historical_logs():
    data = request.json if request.is_json else request.form.to_dict()
    days = int(data.get('days', 30))
    prune_postauth = bool(data.get('prune_postauth', True))
    prune_audit = bool(data.get('prune_audit', False))
    
    from services.db_maintenance_service import prune_historical_logs
    res = prune_historical_logs(days=days, prune_postauth=prune_postauth, prune_audit=prune_audit)
    if res.get('success'):
        log_audit(1, 'admin', 'DB_LOG_PRUNE', 'tools', f'Pruned historical logs older than {days} days: {res.get("deleted_radacct")} acct, {res.get("deleted_radpostauth")} postauth')
    return jsonify(res)


@system_bp.route('/api/tools/database-maintenance/optimize-tables', methods=['POST'], endpoint="api_optimize_tables")

def api_optimize_tables():
    data = request.json if request.is_json else request.form.to_dict()
    tables = data.get('tables') if isinstance(data.get('tables'), list) else None
    
    from services.db_maintenance_service import optimize_database_tables
    res = optimize_database_tables(tables=tables)
    if res.get('success'):
        log_audit(1, 'admin', 'DB_OPTIMIZE_TABLES', 'tools', f'Optimized database tables. Freed {res.get("freed_mb", 0)} MB')
    return jsonify(res)


@system_bp.route('/api/tools/database-maintenance/factory-reset', methods=['POST'], endpoint="api_factory_reset_database")

def api_factory_reset_database():
    data = request.json if request.is_json else request.form.to_dict()
    confirm_code = data.get('confirm_code')
    keep_packages = bool(data.get('keep_packages', True))
    keep_resellers = bool(data.get('keep_resellers', False))
    from services.db_maintenance_service import factory_reset_database
    res = factory_reset_database(keep_packages=keep_packages, keep_resellers=keep_resellers)
    if res.get('success'):
        log_audit(1, 'admin', 'DB_FACTORY_RESET', 'tools', f'Factory reset performed: {res.get("message")}')
    return jsonify(res)


@system_bp.route('/notifications', endpoint="notifications_center_page")

def notifications_center_page():
    from services.bot_notifications_service import get_notification_settings
    data = get_notification_settings()
    return render_template('notifications_center.html', data=data)


@system_bp.route('/api/notifications/settings/save', methods=['POST'], endpoint="api_notifications_settings_save")

def api_notifications_settings_save():
    token = request.form.get('bot_token', '').strip()
    chat_id = request.form.get('chat_id', '').strip()
    enabled = request.form.get('is_enabled', '0')
    low_quota = request.form.get('low_quota', '0')
    recharge = request.form.get('recharge', '0')
    nas_down = request.form.get('nas_down', '0')
    daily = request.form.get('daily_summary', '0')
    from services.bot_notifications_service import update_notification_settings
    ok, msg = update_notification_settings(token, chat_id, enabled, low_quota, recharge, nas_down, daily)
    return jsonify({'success': ok, 'message': msg})


@system_bp.route('/api/notifications/test-send', methods=['POST'], endpoint="api_notifications_test_send")

def api_notifications_test_send():
    msg = request.form.get('message', '').strip()
    from services.bot_notifications_service import send_telegram_alert
    ok, res = send_telegram_alert(msg or "🚀 Test Message from MAX RADIUS", message_type='TEST_ALERT')
    return jsonify({'success': ok, 'message': res})


@system_bp.route('/loyalty-rewards', endpoint="loyalty_rewards_page")

def loyalty_rewards_page():
    from services.loyalty_rewards_service import get_loyalty_overview
    data = get_loyalty_overview()
    return render_template('loyalty_rewards.html', data=data)


@system_bp.route('/api/loyalty-rewards/award', methods=['POST'], endpoint="api_loyalty_award_points")

def api_loyalty_award_points():
    user = request.form.get('username', '').strip()
    pts = int(request.form.get('points', 0))
    rsn = request.form.get('reason', 'Admin Award')
    from services.loyalty_rewards_service import award_loyalty_points
    ok, msg = award_loyalty_points(user, pts, reason=rsn)
    return jsonify({'success': ok, 'message': msg})


@system_bp.route('/api/loyalty-rewards/redeem', methods=['POST'], endpoint="api_loyalty_redeem_reward")

def api_loyalty_redeem_reward():
    user = request.form.get('username', '').strip()
    reward_id = int(request.form.get('reward_id', 0))
    from services.loyalty_rewards_service import redeem_reward
    ok, msg = redeem_reward(user, reward_id)
    return jsonify({'success': ok, 'message': msg})


@system_bp.route('/automation-rules', endpoint="automation_rules_page")

def automation_rules_page():
    from services.automation_rules_service import get_automation_overview
    data = get_automation_overview()
    return render_template('automation_rules.html', data=data)


@system_bp.route('/api/automation-rules/toggle', methods=['POST'], endpoint="api_automation_toggle")

def api_automation_toggle():
    rule_id = int(request.form.get('rule_id', 0))
    is_active = int(request.form.get('is_active', 1))
    from services.automation_rules_service import toggle_rule
    ok, msg = toggle_rule(rule_id, is_active)
    return jsonify({'success': ok, 'message': msg})


@system_bp.route('/api/automation-rules/run', methods=['POST'], endpoint="api_automation_run")

def api_automation_run():
    rule_id = int(request.form.get('rule_id', 0))
    from services.automation_rules_service import execute_rule_now
    ok, msg = execute_rule_now(rule_id)
    return jsonify({'success': ok, 'message': msg})


@system_bp.route('/tools/database-migration', endpoint="database_migration_studio")

@login_required
def database_migration_studio():
    return redirect(url_for('import_data_page', tab='database'))


@system_bp.route('/api/tools/database-migration/analyze', methods=['POST'], endpoint="api_migration_analyze")

def api_migration_analyze():
    from services.database_migration_service import analyze_backup_file
    try:
        if 'backup_file' in request.files and request.files['backup_file'].filename:
            f = request.files['backup_file']
            temp_dir = os.path.join(os.getcwd(), 'data', 'migration_cache')
            os.makedirs(temp_dir, exist_ok=True)
            temp_path = os.path.join(temp_dir, 'uploaded_backup.gz')
            f.save(temp_path)
            res = analyze_backup_file(temp_path)
            res['temp_server_path'] = temp_path
            return jsonify(res)
        
        server_path = request.form.get('server_path')
        if not server_path and request.is_json:
            server_path = request.json.get('server_path')
        
        if server_path and os.path.exists(server_path):
            res = analyze_backup_file(server_path)
            res['temp_server_path'] = server_path
            return jsonify(res)
        
        return jsonify({'success': False, 'error': 'لم يتم تزويد ملف أو مسار صالح على السيرفر.'}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@system_bp.route('/api/tools/database-migration/execute', methods=['POST'], endpoint="api_migration_execute")

def api_migration_execute():
    from services.database_migration_service import execute_database_migration
    try:
        data = request.get_json(silent=True) or {}
        server_path = data.get('server_path') or data.get('temp_server_path')
        
        if not server_path or not os.path.exists(server_path):
            default_cached = os.path.join(os.getcwd(), 'data', 'migration_cache', 'uploaded_backup.gz')
            if os.path.exists(default_cached):
                server_path = default_cached

        if not server_path or not os.path.exists(server_path):
            return jsonify({'success': False, 'error': 'مسار ملف النسخة الاحتياطية غير موجود أو غير صالح على السيرفر.'}), 400

        res = execute_database_migration(server_path, options=data)
        return jsonify(res)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@system_bp.route('/api/tools/database-migration/progress', endpoint="api_migration_progress")

def api_migration_progress():
    from services.database_migration_service import get_migration_progress
    return jsonify(get_migration_progress())


@system_bp.route('/api/tools/database-migration/cancel', methods=['POST'], endpoint="api_migration_cancel")

def api_migration_cancel():
    from services.database_migration_service import cancel_migration
    res = cancel_migration()
    return jsonify(res)


@system_bp.route('/backups', endpoint="backups_page")

def backups_page():
    backups = list_backups()
    db_engine = 'MariaDB 10.11' if DB_TYPE == 'mysql' else 'SQLite 3'
    backup_settings = get_backup_settings()
    scheduler_status = get_scheduler_status()
    return render_template(
        'backups.html',
        backups=backups,
        current_version=CURRENT_SYSTEM_VERSION,
        db_engine=db_engine,
        backup_settings=backup_settings,
        scheduler_status=scheduler_status
    )


@system_bp.route('/api/backup/settings', methods=['GET', 'POST'], endpoint="api_backup_settings")

def api_backup_settings():
    if request.method == 'GET':
        settings = get_backup_settings()
        status = get_scheduler_status()
        return jsonify({'success': True, 'settings': settings, 'status': status})
    
    # POST - Save settings
    data = request.json if request.is_json else request.form.to_dict()
    auto_enabled = data.get('auto_enabled')
    if isinstance(auto_enabled, str):
        auto_enabled = auto_enabled.lower() in ['true', '1', 'on', 'yes']
    else:
        auto_enabled = bool(auto_enabled)

    interval_days = int(data.get('interval_days', 1))
    
    # Parse times (can be list or JSON string or comma-separated)
    raw_times = data.get('times', [])
    times = []
    if isinstance(raw_times, str):
        try:
            times = json.loads(raw_times)
        except Exception:
            times = [t.strip() for t in raw_times.split(',') if t.strip()]
    elif isinstance(raw_times, list):
        times = [str(t).strip() for t in raw_times if str(t).strip()]
        
    storage_path = str(data.get('storage_path', '')).strip()

    success, msg = save_backup_settings(
        auto_enabled=auto_enabled,
        interval_days=interval_days,
        times=times,
        storage_path=storage_path
    )

    if success:
        reload_backup_schedule()
        
    return jsonify({
        'success': success,
        'message': msg,
        'settings': get_backup_settings(),
        'status': get_scheduler_status()
    })


@system_bp.route('/api/backup/health', methods=['GET'], endpoint="api_backup_health")

def api_backup_health():
    probe = healthcheck_backup_scheduler()
    status_code = 200 if probe.get('healthy') else 503
    return jsonify(probe), status_code


@system_bp.route('/api/backup/scheduler/action', methods=['POST'], endpoint="api_backup_scheduler_action")

def api_backup_scheduler_action():
    data = request.json if request.is_json else request.form.to_dict()
    action = data.get('action', '').strip().lower()
    
    if action == 'start':
        success, msg = start_backup_scheduler()
    elif action == 'stop':
        success, msg = stop_backup_scheduler()
    elif action == 'restart':
        success, msg = restart_backup_scheduler()
    elif action == 'reload':
        success, msg = reload_backup_schedule()
    else:
        return jsonify({'success': False, 'message': f'إجراء غير صالح: {action}'})
        
    return jsonify({'success': success, 'message': msg, 'status': get_scheduler_status()})


@system_bp.route('/api/backups/create', methods=['POST'], endpoint="api_create_backup")

def api_create_backup():
    try:
        admin_user = session.get('admin_username') or 'admin'
        success, msg, data = create_backup(admin_username=admin_user, notes='نسخة احتياطية يدوية')
        return jsonify({'success': success, 'message': msg, 'data': data})
    except Exception as e:
        return jsonify({'success': False, 'message': f'خطأ أثناء إنشاء النسخة الاحتياطية: {str(e)}'}), 500


@system_bp.route('/api/backups/upload', methods=['POST'], endpoint="api_upload_backup")

def api_upload_backup():
    try:
        if 'backup_file' not in request.files:
            return jsonify({'success': False, 'message': 'لم يتم العثور على ملف مرفوع في الطلب.'})
        file_storage = request.files['backup_file']
        admin_user = session.get('admin_username') or 'admin'
        success, msg = upload_backup_file(file_storage, admin_username=admin_user)
        return jsonify({'success': success, 'message': msg})
    except Exception as e:
        return jsonify({'success': False, 'message': f'خطأ أثناء رفع النسخة الاحتياطية: {str(e)}'}), 500


@system_bp.route('/api/backups/restore', methods=['POST'], endpoint="api_restore_backup")

def api_restore_backup():
    try:
        filename = request.values.get('filename', '').strip()
        if not filename and request.is_json:
            filename = (request.json or {}).get('filename', '').strip()
        if not filename:
            return jsonify({'success': False, 'message': 'يرجى تحديد اسم ملف النسخة الاحتياطية.'})
        admin_user = session.get('admin_username') or 'admin'
        success, msg = restore_backup(filename, admin_username=admin_user)
        return jsonify({'success': success, 'message': msg})
    except Exception as e:
        return jsonify({'success': False, 'message': f'خطأ أثناء استعادة النسخة الاحتياطية: {str(e)}'}), 500


@system_bp.route('/api/backups/delete', methods=['POST'], endpoint="api_delete_backup")

def api_delete_backup():
    try:
        filename = request.values.get('filename', '').strip()
        if not filename and request.is_json:
            filename = (request.json or {}).get('filename', '').strip()
        if not filename:
            return jsonify({'success': False, 'message': 'يرجى تحديد اسم ملف النسخة الاحتياطية.'})
        admin_user = session.get('admin_username') or 'admin'
        success, msg = delete_backup(filename, admin_username=admin_user)
        return jsonify({'success': success, 'message': msg})
    except Exception as e:
        return jsonify({'success': False, 'message': f'خطأ أثناء حذف النسخة الاحتياطية: {str(e)}'}), 500


@system_bp.route('/backups/download/<path:filename>', endpoint="download_backup_file_route")

def download_backup_file_route(filename):
    filepath = get_backup_filepath(filename)
    if filepath and os.path.isfile(filepath):
        return send_file(filepath, as_attachment=True, download_name=os.path.basename(filepath))
    flash('ملف النسخة الاحتياطية المطلوب غير موجود على السيرفر.', 'danger')
    return redirect(url_for('backups_page'))


@system_bp.route('/settings/backup', endpoint="download_backup")

def download_backup():
    return redirect(url_for('backups_page'))


@system_bp.route('/tools/import', endpoint="import_data_page")

@system_bp.route('/tools/import-data', endpoint="import_data_page")

@system_bp.route('/import-data', endpoint="import_data_page")

@login_required
def import_data_page():
    tab = request.args.get('tab', 'database').strip().lower()
    if tab not in ['database', 'mikrotik', 'excel']:
        tab = 'database'
    resellers = query_all("SELECT id, name, phone FROM wisp_resellers WHERE status = 'active' ORDER BY name ASC")
    packages = query_all("SELECT id, name, price, service_type FROM wisp_packages WHERE is_active = 1 ORDER BY name ASC")
    try:
        from services.nas_service import get_nas_devices
        nas_devices = get_nas_devices(skip_live_probe=False)
    except Exception:
        nas_devices = []
    return render_template('tools/unified_import.html', resellers=resellers, packages=packages, nas_devices=nas_devices, active_tab=tab)


@system_bp.route('/tools/import/sample', endpoint="download_sample_template")

def download_sample_template():
    import_type = request.args.get('type', 'vouchers').strip().lower()
    if import_type not in ['vouchers', 'subscribers']:
        import_type = 'vouchers'
    try:
        sample_io = generate_sample_template(import_type)
        download_name = f"sample_{import_type}_{datetime.date.today().strftime('%Y%m%d')}.xlsx"
        return send_file(
            sample_io,
            as_attachment=True,
            download_name=download_name,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
    except Exception as e:
        flash(f'خطأ أثناء توليد نموذج الإكسل: {str(e)}', 'danger')
        return redirect(url_for('import_data_page'))


@system_bp.route('/tools/import/process', methods=['POST'], endpoint="process_import_action")

def process_import_action():
    try:
        import_type = request.form.get('import_type', 'vouchers').strip().lower()
        if import_type not in ['vouchers', 'subscribers']:
            import_type = 'vouchers'

        raw_reseller = request.form.get('default_reseller_id', '0')
        default_reseller_id = int(raw_reseller) if (raw_reseller and raw_reseller != '0') else None
        batch_name = request.form.get('batch_name', '').strip()

        if 'excel_file' not in request.files:
            return jsonify({'success': False, 'message': 'يرجى اختيار ملف Excel للرفع.'}), 400

        file = request.files['excel_file']
        if not file or not file.filename:
            return jsonify({'success': False, 'message': 'يرجى اختيار ملف صالح.'}), 400

        fname = file.filename.lower()
        if not (fname.endswith('.xlsx') or fname.endswith('.xls')):
            return jsonify({'success': False, 'message': 'صيغة الملف غير مدعومة. يرجى رفع ملفات .xlsx أو .xls فقط.'}), 400

        # 1. Parse Excel
        try:
            raw_rows = parse_excel_file(file)
        except Exception as e:
            return jsonify({'success': False, 'message': f'فشل قراءة ملف الإكسل: {str(e)}'}), 400

        if not raw_rows:
            return jsonify({'success': False, 'message': 'الملف المرفوع لا يحتوي على أي صفوف بيانات.'}), 400

        # 2. Strict Zero-Tolerance Validation
        is_valid, errors, cleaned_rows = validate_import_data(raw_rows, import_type=import_type, default_reseller_id=default_reseller_id)
        if not is_valid:
            return jsonify({
                'success': False,
                'message': f'تم العثور على {len(errors)} خطأ في ملف الإكسل يمنع الاستيراد (تم إلغاء العملية بالكامل لضمان سلامة البيانات).',
                'errors': errors
            }), 400

        # 3. Atomic Database Insertion
        admin_user = session.get('admin_user') or 'admin'
        success, msg, data = execute_import(import_type=import_type, cleaned_rows=cleaned_rows, batch_name=batch_name, admin_username=admin_user)

        if not success:
            return jsonify({'success': False, 'message': msg}), 500

        redirect_url = url_for('view_batch_details', batch_id=data['batch_id']) if (import_type == 'vouchers' and data.get('batch_id')) else url_for('subscribers')

        return jsonify({
            'success': True,
            'message': msg,
            'count': data.get('count', len(cleaned_rows)),
            'batch_name': data.get('batch_name'),
            'redirect_url': redirect_url
        })

    except Exception as e:
        return jsonify({'success': False, 'message': f'حدث استثناء غير متوقع أثناء معالجة الاستيراد: {str(e)}'}), 500


@system_bp.route('/settings/license', methods=['GET'], endpoint="license_status_page")

def license_status_page():
    lic_info = get_active_license_status()
    manager = get_current_manager()
    if not lic_info.get('valid') or not manager:
        return render_template('license_standalone.html', license=lic_info)
    return render_template('license_status.html', license=lic_info)


@system_bp.route('/settings/license/activate', methods=['POST'], endpoint="activate_license_action")

def activate_license_action():
    lic_info = get_active_license_status()
    manager = get_current_manager()
    if lic_info.get('valid') and manager:
        require_permission('settings_manage')
        
    master_url = request.form.get('master_server_url', 'http://136.244.95.245:3030').strip()
    
    lic_file = request.files.get('license_file')
    lic_token = request.form.get('license_token', '').strip()
    
    content = ""
    if lic_file and lic_file.filename:
        try:
            content = lic_file.read().decode('utf-8')
        except Exception as e:
            flash(f"فشل قراءة ملف الترخيص المرفوع: {str(e)}", "danger")
            return redirect(url_for('license_status_page'))
    elif lic_token:
        content = lic_token
    else:
        flash("يرجى اختيار ملف الترخيص (.lic) أو لصق المفتاح المشفر", "danger")
        return redirect(url_for('license_status_page'))
        
    success, msg = install_and_activate_license(content, master_url)
    flash(msg, "success" if success else "danger")
    if success:
        if get_current_manager():
            return redirect(url_for('dashboard'))
        return redirect(url_for('login'))
    return redirect(url_for('license_status_page'))


@system_bp.route('/settings/license/sync-heartbeat', methods=['POST'], endpoint="sync_license_heartbeat_action")

@login_required
def sync_license_heartbeat_action():
    success, msg = sync_license_heartbeat_with_server()
    return jsonify({"success": success, "message": msg})


@system_bp.route('/tools/system-update', methods=['GET'], endpoint="system_update_page")

@login_required
def system_update_page():
    current_version = get_current_system_version()
    update_info = check_for_updates(force_refresh=False)
    progress_info = get_update_progress()
    return render_template(
        'tools/system_update.html',
        current_version=current_version,
        update_info=update_info,
        progress_info=progress_info
    )


@system_bp.route('/api/tools/system-update/check', methods=['GET'], endpoint="api_system_update_check")

@login_required
def api_system_update_check():
    force = request.args.get('force', 'false').lower() in ['true', '1']
    res = check_for_updates(force_refresh=force)
    return jsonify(res)


@system_bp.route('/api/tools/system-update/apply', methods=['POST'], endpoint="api_system_update_apply")

@login_required
def api_system_update_apply():
    manager = get_current_manager()
    operator_name = manager.get('username') if manager else 'Admin'
    
    data = request.get_json(silent=True) or {}
    backup_first = data.get('backup_first', True)
    target_version = data.get('version')
    
    res = trigger_system_update(target_version=target_version, backup_first=backup_first, triggered_by=operator_name)
    if res.get('success'):
        try:
            log_audit(
                operator=operator_name,
                action_type='system_update_triggered',
                target_type='system',
                target_id=res.get('target_version', 'latest'),
                details=f"بدء ترقية النظام إلى الإصدار {res.get('target_version')}"
            )
        except Exception:
            pass
    return jsonify(res)


@system_bp.route('/api/tools/system-update/progress', methods=['GET'], endpoint="api_system_update_progress")

@login_required
def api_system_update_progress():
    res = get_update_progress()
    return jsonify(res)



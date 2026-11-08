# -*- coding: utf-8 -*-
"""
MAX RADIUS Web - System Settings, Backups, Updates, Diagnostics, and Maintenance Routes Module.
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
from web.decorators import require_role_or_permission, require_feature

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

from core.ownership import scoped_query_all as query_all, scoped_query_one as query_one, owner_id, generation_owner

system_bp = Blueprint('system_bp', __name__)


@system_bp.app_context_processor
def verified_branding_context():
    from core.license_feature_policy import enabled
    if enabled('white_label'):
        return {}
    return {'network_name': APP_NAME, 'company_name': APP_NAME, 'isp_name': APP_NAME,
            'logo_url': None, 'system_title': 'نظام إدارة الشبكات والفوترة و FreeRADIUS'}


@system_bp.before_request
def guard_account_action_permissions():
    # The status route authorizes the saved job's current account and action.
    # Its final URL segment is a job ID, not an account-action name.
    if (request.endpoint or '').rsplit('.', 1)[-1] == 'api_session_operation':
        return None
    if not request.path.startswith('/api/actions/'):
        return None
    action = request.path.rsplit('/', 1)[-1]
    manager = get_current_manager()
    if not manager:
        return jsonify(success=False, message='يرجى تسجيل الدخول أولًا.'), 401
    data = (request.get_json(silent=True) or {}) if request.is_json else request.values
    kind = data.get('target_type', 'subscriber')
    if kind not in ('subscriber', 'voucher', 'card'):
        return jsonify(success=False, message='نوع الحساب غير صالح.'), 400
    if action in ('add-wallet', 'deduct-wallet'):
        permission = 'wallets.manage'
    elif action == 'usage-history':
        permission = 'subscribers.view' if kind == 'subscriber' else 'vouchers.view'
    elif kind != 'subscriber':
        permission = 'vouchers.delete' if action == 'delete' else 'vouchers.actions'
    else:
        permission = {'delete': 'subscribers.delete', 'extend-time': 'subscribers.renew',
            'renew': 'subscribers.renew', 'disconnect': 'subscribers.disconnect',
            'terminate': 'subscribers.edit', 'disable': 'subscribers.edit',
            'activate-subscriber': 'subscribers.edit', 'change-package': 'subscribers.edit',
            'add-quota': 'subscribers.edit'}.get(action)
    if not permission or not has_permission(permission, manager):
        return jsonify(success=False, message='لا تملك صلاحية هذا الإجراء.'), 403


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
    success, msg = action_bulk_execute(action_delete_entity, target_type, ids, admin_username=get_current_manager().get('username') or 'admin')
    return jsonify({'success': success, 'message': msg})


@system_bp.route('/api/actions/extend-time', methods=['POST'], endpoint="api_extend_time")

def api_extend_time():
    target_type, ids = _get_target_params()
    days = request.form.get('days', 30)
    success, msg = action_bulk_execute(action_extend_time, target_type, ids, days=days, admin_username=get_current_manager().get('username') or 'admin')
    return jsonify({'success': success, 'message': msg})


@system_bp.route('/api/actions/terminate', methods=['POST'], endpoint="api_terminate")

def api_terminate():
    target_type, ids = _get_target_params()
    success, msg = action_bulk_execute(action_terminate_subscription, target_type, ids, admin_username=get_current_manager().get('username') or 'admin')
    return jsonify({'success': success, 'message': msg})


@system_bp.route('/api/actions/disable', methods=['POST'])
@login_required
def api_disable_account():
    data = request.get_json(silent=True) if request.is_json else request.form
    data = data or {}
    kind = {'subscriber': 'subscriber', 'card': 'voucher', 'voucher': 'voucher'}.get(data.get('target_type'))
    target_id = str(data.get('target_id') or '')
    if not kind or not target_id.isascii() or not target_id.isdigit() or int(target_id) <= 0:
        return jsonify(success=False, message='حدد نوع الحساب ورقمه الصحيح'), 400
    permission = 'subscribers.edit' if kind == 'subscriber' else 'vouchers.actions'
    manager = get_current_manager()
    if not has_permission(permission, manager):
        return jsonify(success=False, message='لا تملك صلاحية تعطيل هذا الحساب'), 403
    success, message = action_disable_account(kind, int(target_id), admin_username=manager.get('username') or 'admin')
    return jsonify(success=success, message=message)


@system_bp.route('/api/actions/activate-subscriber', methods=['POST'])
@login_required
def api_activate_subscriber():
    data = request.get_json(silent=True) if request.is_json else request.form
    data = data or {}
    target_id = str(data.get('target_id') or '')
    if data.get('target_type') != 'subscriber' or not target_id.isascii() or not target_id.isdigit() or int(target_id) <= 0:
        return jsonify(success=False, message='حدد رقم المشترك الصحيح'), 400
    manager = get_current_manager()
    if not has_permission('subscribers.edit', manager):
        return jsonify(success=False, message='لا تملك صلاحية تنشيط هذا المشترك'), 403
    try:
        success, message, status = action_activate_subscriber(int(target_id), admin_username=manager.get('username') or 'admin')
        return jsonify(success=success, message=message, status=status)
    except ValueError as error:
        return jsonify(success=False, message=str(error)), 400


@system_bp.route('/api/actions/renew', methods=['POST'], endpoint="api_renew")

def api_renew():
    target_type, ids = _get_target_params()
    success, msg = action_bulk_execute(action_renew_package, target_type, ids, admin_username=get_current_manager().get('username') or 'admin')
    return jsonify({'success': success, 'message': msg})


@system_bp.route('/api/actions/change-package', methods=['POST'], endpoint="api_change_package")
def api_change_package():
    target_type, ids = _get_target_params()
    new_package_id = request.form.get('new_package_id')
    raw_ro = request.form.get('enable_rollover')
    enable_rollover = raw_ro in ('1', 'true', 'on', True, 1)
    operation_id = request.form.get('operation_id', '')
    action = action_change_package
    if operation_id:
        import hashlib
        import re
        if not re.fullmatch('[a-f0-9]{32}', operation_id):
            return jsonify(success=False, message='مرجع تغيير الباقة غير صالح؛ أعد فتح العملية.'), 400

        def change_one(kind, ident, **options):
            # One stable receipt per normalized target, independent of list order.
            child_id = hashlib.sha256(f'{operation_id}:{kind}:{int(ident)}'.encode()).hexdigest()[:32]
            return action_change_package(kind, ident, operation_id=child_id, **options)

        action = change_one
    success, msg = action_bulk_execute(action, target_type, ids, new_package_id=new_package_id, enable_rollover=enable_rollover, admin_username=get_current_manager().get('username') or 'admin')
    return jsonify({'success': success, 'message': msg})


@system_bp.route('/api/actions/add-quota', methods=['POST'], endpoint="api_add_quota")

def api_add_quota():
    target_type, ids = _get_target_params()
    quota_amount = request.form.get('quota_amount', 0)
    quota_unit = request.form.get('quota_unit', 'GB')
    success, msg = action_bulk_execute(action_add_quota, target_type, ids, quota_amount=quota_amount, quota_unit=quota_unit, admin_username=get_current_manager().get('username') or 'admin')
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
    success, msg = action_bulk_execute(action_add_wallet_balance, target_type, ids, amount=amount, notes=notes, admin_username=get_current_manager().get('username') or 'admin')
    return jsonify({'success': success, 'message': msg})


@system_bp.route('/api/actions/deduct-wallet', methods=['POST'], endpoint="api_deduct_wallet")

def api_deduct_wallet():
    target_type, ids = _get_target_params()
    amount = request.form.get('amount', 0)
    notes = request.form.get('notes', '')
    success, msg = action_bulk_execute(action_deduct_wallet_balance, target_type, ids, amount=amount, notes=notes, admin_username=get_current_manager().get('username') or 'admin')
    return jsonify({'success': success, 'message': msg})


@system_bp.route('/api/actions/disconnect', methods=['POST'], endpoint="api_disconnect")

def api_disconnect():
    target_type, ids = _get_target_params()
    success, msg = action_bulk_execute(action_disconnect_user, target_type, ids, admin_username=get_current_manager().get('username') or 'admin', operation_id=(request.get_json(silent=True) or request.form).get('operation_id'))
    return jsonify(dict(success=success, **msg) if isinstance(msg,dict) else dict(success=success,message=msg))


@system_bp.route('/api/actions/session-operation/<job_id>', methods=['GET'])
def api_session_operation(job_id):
    from services.session_control_service import find, public, start_worker
    from core.ownership import scoped_query_one
    manager=get_current_manager()
    if not manager: return jsonify(success=False,message='يرجى تسجيل الدخول.'),401
    try: job=find(job_id)
    except ValueError: job=None
    if not job: return jsonify(success=False,message='العملية غير موجودة.'),404
    from database.db import query_one as identity_query
    if (job['schema']!=identity_query('SELECT DATABASE() AS tenant_schema')['tenant_schema']
            or job['db_host']!=os.environ.get('DB_HOST','')):
        return jsonify(success=False,message='العملية غير متاحة.'),404
    kind=job['kind']; table='wisp_vouchers' if kind=='voucher' else 'wisp_subscribers'
    if not scoped_query_one(f'SELECT id FROM {table} WHERE id=?',(job['account_id'],)):
        return jsonify(success=False,message='العملية غير متاحة.'),404
    if job['policy'] is not None:
        allowed=has_permission('subscribers.edit' if kind=='subscriber' else 'detail.vouchers.card_edit',manager)
        if kind=='voucher': allowed=allowed and has_permission('vouchers.actions',manager)
    else:
        allowed=has_permission('subscribers.disconnect' if kind=='subscriber' else 'vouchers.actions',manager)
    if not allowed: return jsonify(success=False,message='لا تملك صلاحية عرض العملية.'),403
    start_worker()
    response=jsonify(success=True,**public(job))
    response.headers['Cache-Control']='no-store, private'
    return response


@system_bp.route('/sales', endpoint="sales_reports")

@system_bp.route('/reports/sales', endpoint="sales_reports")

@system_bp.route('/vouchers/sales', endpoint="sales_reports")

def sales_reports():
    if request.args.get('report_view') == 'traffic':
        from services.network_traffic_service import report as traffic_report, format_bytes
        try:
            data = traffic_report(request.args)
        except ValueError as exc:
            data = traffic_report({})
            data['filter_error'] = str(exc)
            return render_template('network_traffic.html', traffic_bytes=format_bytes, **data), 400
        except Exception:
            current_app.logger.exception('Network traffic report unavailable')
            return render_template('network_traffic.html', traffic_bytes=format_bytes, traffic_unavailable=True), 503
        return render_template('network_traffic.html', traffic_bytes=format_bytes, **data)
    from services.sales_reporting_service import report
    try:
        data = report(request.args)
    except ValueError as exc:
        data = report({})
        data['filter_error'] = str(exc)
        return render_template('sales_reports.html', **data), 400
    return render_template('sales_reports.html', **data)


@system_bp.route('/sales/export', endpoint="export_sales")

@system_bp.route('/reports/sales/export', endpoint="export_sales")

@heavy_operation
def export_sales():
    import csv
    import io
    from services.sales_reporting_service import report, export_value
    try:
        data = report(request.args, export=True)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    headers = ['م','نوع العملية','اسم المستخدم / المشترك','الرقم التسلسلي / الفاتورة','الحزمة / الدفعة',
               'الباقة','المبلغ المسجل','حصة الشبكة','عمولة الموزع','الموزع / نقطة البيع','جهاز الميكروتيك',
               'سيرفر Hotspot / PPPoE','تاريخ العملية','نوع الخدمة','حالة التحديد','ملاحظة التحديد']
    def records():
        for index,row in enumerate(data['recent_sales'],1):
            yield [index,row['operation'],row['username'],row['serial_number'],row['batch_name'],row['package_name'],
                row['price'],row['isp_share'],row['reseller_margin'],row['reseller_name'],row['nas_label'] or 'غير محدد',
                row['server_label'],str(row['activated_at'] or 'تاريخ غير محدد'),row['service_type'] or 'غير محدد',
                row['source_status_label'],row['source_reason_label']]
    totals = ['الإجمالي','','','','','',data['gross_sales'],data['net_isp_revenue'],data['total_reseller_commissions'],'','','','']
    totals.extend(['']*(len(headers)-len(totals)))
    format_name = request.args.get('format','excel').strip().lower()
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d_%H%M%S')
    from services.heavy_operation_service import temporary_output
    if format_name not in ('csv','excel'):
        return jsonify({'error':'صيغة التصدير غير مدعومة.'}),400
    output=temporary_output()
    try:
        if format_name=='csv':
            stream=io.TextIOWrapper(output,encoding='utf-8-sig',newline='',write_through=True)
            try:
                writer=csv.writer(stream);writer.writerow(headers)
                for row in records():writer.writerow([export_value(v) for v in row])
                writer.writerow(totals);stream.flush()
            finally:stream.detach()
            mime='text/csv; charset=utf-8';extension='csv'
        else:
            from openpyxl import Workbook
            from openpyxl.cell import WriteOnlyCell
            from openpyxl.styles import Alignment,Font,PatternFill
            from openpyxl.utils import get_column_letter
            workbook=Workbook(write_only=True);sheet=workbook.create_sheet('Sales Report')
            sheet.sheet_view.rightToLeft=True;sheet.freeze_panes='A2'
            sheet.auto_filter.ref=f"A1:{get_column_letter(len(headers))}{len(data['recent_sales'])+1}"
            for col in range(1,len(headers)+1):
                sheet.column_dimensions[get_column_letter(col)].width=26 if col not in (1,7,8,9) else 16
            def append(values,header=False):
                cells=[]
                for col,value in enumerate(values,1):
                    cell=WriteOnlyCell(sheet,value=export_value(value))
                    if header:
                        cell.fill=PatternFill('solid',fgColor='172554');cell.font=Font(color='FFFFFF',bold=True)
                    else:
                        cell.alignment=Alignment(vertical='center',horizontal='right',wrap_text=True)
                        if col in (7,8,9):cell.number_format='#,##0.00'
                    cells.append(cell)
                sheet.append(cells)
            append(headers,True)
            for row in records():append(row)
            append(totals);workbook.save(output)
            mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';extension='xlsx'
        output.seek(0)
        response=send_file(output,mimetype=mime,as_attachment=True,
            download_name=f'MAX_RADIUS_Sales_{stamp}.{extension}',max_age=0)
        response.call_on_close(output.close)
        response.headers['Cache-Control']='no-store, private'
        return response
    except BaseException:
        output.close();raise


@system_bp.route('/settings', endpoint="settings")
def settings():
    settings_rows = query_all('SELECT `key`, `value`, `description` FROM wisp_system_settings')
    settings_dict = {r['key']: r['value'] for r in settings_rows} if settings_rows else {}
    audit_logs = query_all('SELECT * FROM wisp_audit_logs ORDER BY id DESC LIMIT 30')
    admins = query_all('SELECT id, username, full_name, role, is_active, created_at FROM wisp_admins')
    return render_template('settings.html', settings_list=settings_rows, audit_logs=audit_logs, admins=admins)


@system_bp.route('/settings/update', methods=['POST'], endpoint="update_settings_action")
def update_settings_action():
    try:
        from services.system_settings_service import save_system_settings
        saved = save_system_settings(request.form, request.files.get('network_logo'), current_app.static_folder)
        currency_symbol = saved['currency_symbol']
        timezone_val = saved['timezone']
        try:
            from core.time_service import update_system_timezone
            update_system_timezone(timezone_val)
            reload_backup_schedule()
        except Exception:
            pass
        flash(f'تم حفظ وتطبيق إعدادات النظام المسموحة والعملة ({currency_symbol}) والمنطقة الزمنية ({timezone_val}) بنجاح.', 'success')
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
    # Diagnostic only: no clock, database or scheduler changes.
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


def _autoheal_input_guard(func):
    from functools import wraps
    @wraps(func)
    def wrapped(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except (ValueError, TypeError):
            return jsonify({'success': False, 'message': 'قيم الطلب غير صحيحة'}), 400
    return wrapped

@system_bp.route('/tools/autoheal', endpoint="autoheal_page")
@login_required
@require_feature('autoheal')
@require_permission('settings_manage')
def autoheal_page():
    from services.autoheal_service import get_autoheal_dashboard_full
    data = get_autoheal_dashboard_full(alert_filter=request.args.get('alert_state'))
    return render_template('autoheal.html', data=data)


@system_bp.route('/api/tools/autoheal/status', endpoint="api_autoheal_status")
@login_required
@require_feature('autoheal')
@require_permission('settings_manage')
@_autoheal_input_guard
def api_autoheal_status():
    from services.autoheal_service import get_autoheal_dashboard_full
    data = get_autoheal_dashboard_full(alert_filter=request.args.get('alert_state'))
    return jsonify({'success': True, 'data': data})


@system_bp.route('/api/tools/autoheal/diagnostic', methods=['POST'], endpoint="api_autoheal_diagnostic")
@login_required
@require_feature('autoheal')
@require_permission('settings_manage')
@_autoheal_input_guard
def api_autoheal_diagnostic():
    from services.autoheal_service import run_deep_system_diagnostic
    diagnostic = run_deep_system_diagnostic()
    log_audit((get_current_manager() or {}).get('id'), (get_current_manager() or {}).get('username'), 'AUTOHEAL_DIAGNOSTIC_RUN', 'tools', f'Deep diagnostic ran. Health score: {diagnostic.get("score")}%')
    return jsonify({'success': True, 'diagnostic': diagnostic})


@system_bp.route('/api/tools/autoheal/probe-ports', methods=['POST'], endpoint="api_autoheal_probe_ports")
@login_required
@require_feature('autoheal')
@require_permission('settings_manage')
@_autoheal_input_guard
def api_autoheal_probe_ports():
    from services.autoheal_service import probe_all_network_ports
    ports = probe_all_network_ports()
    return jsonify({'success': True, 'ports': ports})


@system_bp.route('/api/tools/autoheal/restart', methods=['POST'], endpoint="api_autoheal_restart")
@login_required
@require_feature('autoheal')
@require_permission('settings_manage')
@_autoheal_input_guard
def api_autoheal_restart():
    req_data = request.json if request.is_json else request.form.to_dict()
    container_name = req_data.get('container', '').strip()
    from services.autoheal_service import restart_system_container
    success, msg = restart_system_container(container_name)
    if success:
        log_audit((get_current_manager() or {}).get('id'), (get_current_manager() or {}).get('username'), 'CONTAINER_RESTART', 'tools', f'Restarted container: {container_name}')
    return jsonify({'success': success, 'message': msg})


@system_bp.route('/api/tools/autoheal/logs', endpoint="api_autoheal_logs")
@login_required
@require_feature('autoheal')
@require_permission('settings_manage')
@_autoheal_input_guard
def api_autoheal_logs():
    container_name = request.args.get('container', 'max_radius_autoheal').strip()
    lines = int(request.args.get('lines', 80))
    from services.autoheal_service import get_live_container_logs
    logs = get_live_container_logs(container_name, lines=lines)
    return jsonify({'success': not logs.startswith('تعذر استخراج السجلات:'), 'logs': logs, 'message': logs if logs.startswith('تعذر استخراج السجلات:') else ''})


@system_bp.route('/api/tools/autoheal/purge-zombies', methods=['POST'], endpoint="api_autoheal_purge_zombies")
@login_required
@require_feature('autoheal')
@require_permission('settings_manage')
@_autoheal_input_guard
def api_autoheal_purge_zombies():
    req_data = request.json if request.is_json else request.form.to_dict()
    timeout = int(req_data.get('timeout_minutes', 15))
    from services.autoheal_service import purge_stale_zombie_sessions
    success, msg = purge_stale_zombie_sessions(timeout_minutes=timeout)
    if success:
        log_audit((get_current_manager() or {}).get('id'), (get_current_manager() or {}).get('username'), 'ZOMBIE_PURGE', 'tools', f'Purged stale sessions older than {timeout}m')
    return jsonify({'success': success, 'message': msg})


@system_bp.route('/api/tools/autoheal/save-settings', methods=['POST'], endpoint="api_autoheal_save_settings")
@login_required
@require_feature('autoheal')
@require_permission('settings_manage')
@_autoheal_input_guard
def api_autoheal_save_settings():
    req_data = request.json if request.is_json else request.form.to_dict()
    timeout = int(req_data.get('timeout_minutes', 15))
    from services.autoheal_service import set_zombie_session_timeout
    success, msg = set_zombie_session_timeout(timeout)
    if success:
        log_audit((get_current_manager() or {}).get('id'), (get_current_manager() or {}).get('username'), 'AUTOHEAL_SETTINGS_UPDATE', 'tools', f'Updated zombie timeout to {timeout}m')
    return jsonify({'success': success, 'message': msg})


@system_bp.route('/api/tools/autoheal/alerts/resolve', methods=['POST'], endpoint="api_autoheal_resolve_alert")
@login_required
@require_feature('autoheal')
@require_permission('settings_manage')
@_autoheal_input_guard
def api_autoheal_resolve_alert():
    req_data = request.json if request.is_json else request.form.to_dict()
    alert_id = int(req_data.get('alert_id', 0))
    from core.watchdog import resolve_system_alert
    success, msg = resolve_system_alert(alert_id)
    return jsonify({'success': success, 'message': msg})


@system_bp.route('/api/tools/autoheal/alerts/clear-resolved', methods=['POST'], endpoint="api_autoheal_clear_resolved_alerts")
@login_required
@require_feature('autoheal')
@require_permission('settings_manage')
@_autoheal_input_guard
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
    return jsonify({'success': audit.get('success',False), 'audit': audit, 'message': audit.get('error','')})


@system_bp.route('/api/tools/database-maintenance/fix', methods=['POST'], endpoint="api_fix_audit_issue")

def api_fix_audit_issue():
    data = request.json if request.is_json else request.form.to_dict()
    action_code = data.get('action_code')
    username = data.get('username')
    session_id = data.get('session_id')
    
    from services.db_maintenance_service import fix_audit_issue
    res = fix_audit_issue(action_code, username, {key:data.get(key) for key in ('session_id','record_id','attribute_name')})
    if res.get('success'):
        log_audit(1, 'admin', 'DB_AUDIT_FIX', 'tools', f'Fixed {action_code} for user {username}')
    return jsonify(res)


@system_bp.route('/api/tools/database-maintenance/fix-all', methods=['POST'], endpoint="api_fix_all_audit_issues")

def api_fix_all_audit_issues():
    from services.db_maintenance_service import fix_all_audit_issues
    res = fix_all_audit_issues()
    log_audit(1, 'admin', 'DB_AUDIT_FIX_ALL', 'tools', f'Audit repair: {res.get("fixed_count")} fixed; {res.get("errors_count")} failed; complete={res.get("success")}')
    return jsonify(res)


@system_bp.route('/api/tools/database-maintenance/clean-expired', methods=['POST'], endpoint="api_clean_expired_vouchers")

def api_clean_expired_vouchers():
    data = request.get_json(silent=True) if request.is_json else request.form.to_dict()
    data = data or {}
    from services.maintenance_job_service import start_cleanup_job
    try:
        job, created = start_cleanup_job(data.get('delete_type','all'),session.get('admin_username','admin'))
        return jsonify(success=True,job=job,already_running=not created),202
    except ValueError as exc:
        return jsonify(success=False,message=str(exc)),409
    except Exception as exc:
        logger.exception('Unable to start expired-card cleanup')
        return jsonify(success=False,message='تعذر بدء مهمة الحذف: '+str(exc)),500


@system_bp.route('/api/tools/database-maintenance/clean-expired/jobs/latest', methods=['GET'], endpoint='api_latest_cleanup_job')
@system_bp.route('/api/tools/database-maintenance/clean-expired/jobs/<job_id>', methods=['GET'], endpoint='api_cleanup_job_status')
def api_cleanup_job_status(job_id=None):
    from services.maintenance_job_service import get_cleanup_job
    try:
        job = get_cleanup_job(job_id)
        if job_id and job is None:return jsonify(success=False,message='المهمة غير موجودة'),404
        return jsonify(success=True,job=job)
    except Exception as exc:
        return jsonify(success=False,message='تعذر قراءة تقدم المهمة: '+str(exc)),503


@system_bp.route('/api/tools/database-maintenance/clean-expired/jobs/<job_id>/cancel', methods=['POST'], endpoint='api_cancel_cleanup_job')
def api_cancel_cleanup_job(job_id):
    from services.maintenance_job_service import cancel_cleanup_job
    try:
        job=cancel_cleanup_job(job_id,session.get('admin_username','admin'))
        if job is None:return jsonify(success=False,message='المهمة غير موجودة'),404
        return jsonify(success=True,job=job)
    except ValueError as exc:
        return jsonify(success=False,message=str(exc)),400


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


@system_bp.route('/api/tools/database-maintenance/archive', methods=['POST'], endpoint="api_maintenance_archive_sessions")
def api_maintenance_archive_sessions():
    data = (request.get_json(silent=True) if request.is_json else request.form.to_dict()) or {}
    from services.maintenance_job_service import start_archive_job
    try:
        job,created=start_archive_job(data.get('days',90),data.get('mode','archive'),data.get('chunk_size',500),session.get('admin_username','admin'))
        return jsonify(success=True,job=job,already_running=not created),202
    except (ValueError,TypeError) as exc:return jsonify(success=False,message=str(exc)),409
    except Exception:
        logger.exception('Unable to submit history maintenance')
        return jsonify(success=False,message='تعذر تأكيد بدء معالجة التاريخ؛ راجع حالة المهمة قبل إعادة المحاولة.'),503


@system_bp.route('/api/tools/database-maintenance/archive/plan', methods=['GET'], endpoint='api_archive_plan')
def api_archive_plan():
    from services.accounting_archiver_service import get_archive_plan
    try:
        plan=get_archive_plan(request.args.get('days',90),request.args.get('mode','archive'))
        return jsonify(success=True,plan=plan)
    except (ValueError,TypeError) as exc:return jsonify(success=False,message=str(exc)),400
    except Exception:
        logger.exception('Unable to calculate archive preview')
        return jsonify(success=False,message='تعذر حساب التاريخ المؤهل حاليًا.'),503


@system_bp.route('/api/tools/database-maintenance/archive/jobs/latest', methods=['GET'], endpoint='api_latest_archive_job')
@system_bp.route('/api/tools/database-maintenance/archive/jobs/<job_id>', methods=['GET'], endpoint='api_archive_job_status')
def api_archive_job_status(job_id=None):
    from services.maintenance_job_service import get_maintenance_job
    try:
        job=get_maintenance_job('accounting-archive',job_id)
        if job_id and job is None:return jsonify(success=False,message='المهمة غير موجودة'),404
        return jsonify(success=True,job=job)
    except Exception:return jsonify(success=False,message='تعذرت قراءة حالة المعالجة.'),503


@system_bp.route('/api/tools/database-maintenance/archive/jobs/<job_id>/cancel', methods=['POST'], endpoint='api_cancel_archive_job')
def api_cancel_archive_job(job_id):
    from services.maintenance_job_service import cancel_maintenance_job
    try:
        job=cancel_maintenance_job(job_id,'accounting-archive',session.get('admin_username','admin'))
        if job is None:return jsonify(success=False,message='المهمة غير موجودة'),404
        return jsonify(success=True,job=job)
    except ValueError as exc:return jsonify(success=False,message=str(exc)),400
    except Exception:return jsonify(success=False,message='تعذر تأكيد الإلغاء؛ راجع حالة المهمة.'),503


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

@require_permission('settings_manage')
def api_factory_reset_database():
    data = (request.get_json(silent=True) or {}) if request.is_json else request.form.to_dict()
    if str(data.get('confirm_code','')).strip().upper() not in ('RESET','تصفير','CONFIRM'):
        return jsonify(success=False,message='رمز تأكيد إعادة المصنع غير صحيح.'),400
    def flag(name, default):
        value=data.get(name,default)
        if type(value) is bool:return value
        if value in ('true','1'):return True
        if value in ('false','0'):return False
        raise ValueError('خيارات إعادة المصنع غير صالحة.')
    try:
        from services.factory_reset_service import start_factory_reset
        job,created=start_factory_reset(flag('keep_packages',True),flag('keep_resellers',False),session.get('username','admin'))
        return jsonify(success=True,job=job,created=created),202
    except Exception as exc:
        return jsonify(success=False,message=str(exc)),409


@system_bp.route('/api/tools/database-maintenance/factory-reset/cancel', methods=['POST'], endpoint='api_factory_reset_cancel')
@require_permission('settings_manage')
def api_factory_reset_cancel():
    data=request.get_json(silent=True) or {}
    if not isinstance(data,dict):return jsonify(success=False,message='طلب غير صالح.'),400
    try:
        from services.factory_reset_service import cancel_factory_reset
        job=cancel_factory_reset(str(data.get('job_id','')))
        return jsonify(success=True,job=job),202
    except ValueError as exc:
        return jsonify(success=False,message=str(exc)),409


@system_bp.route('/api/tools/database-maintenance/factory-reset/status', methods=['GET'], endpoint='api_factory_reset_status')
@require_permission('settings_manage')
def api_factory_reset_status():
    from services.factory_reset_service import get_factory_reset
    job=get_factory_reset(request.args.get('job_id'))
    if request.args.get('job_id') and not job:
        return jsonify(success=False,message='سجل المهمة غير موجود؛ أعد فتح الصفحة للتحقق من الحالة.'),404
    return jsonify(success=True,job=job)


@system_bp.route('/notifications', endpoint="notifications_center_page")

def notifications_center_page():
    from services.bot_notifications_service import get_notification_settings
    data = get_notification_settings()
    return render_template('notifications_center.html', data=data)


@system_bp.route('/api/notifications/settings/save', methods=['POST'], endpoint="api_notifications_settings_save")
def api_notifications_settings_save():
    token = request.form.get('bot_token', '').strip()
    chat_id = request.form.get('chat_id', '').strip()
    backup_chat_id = request.form.get('backup_chat_id', '').strip()
    enabled = request.form.get('is_enabled', '0')
    auto_send_backups = request.form.get('auto_send_backups', '0')
    low_quota = request.form.get('low_quota', '0')
    recharge = request.form.get('recharge', '0')
    nas_down = request.form.get('nas_down', '0')
    daily = request.form.get('daily_summary', '0')
    from services.bot_notifications_service import update_notification_settings
    ok, msg = update_notification_settings(
        token, chat_id, enabled, low_quota, recharge, nas_down, daily,
        backup_chat_id=backup_chat_id,
        auto_send_backups=auto_send_backups
    )
    return jsonify({'success': ok, 'message': msg})


@system_bp.route('/api/notifications/verify-bot', methods=['POST'], endpoint="api_notifications_verify_bot")
def api_notifications_verify_bot():
    token = request.form.get('bot_token', '').strip()
    from services.bot_notifications_service import verify_telegram_bot
    ok, res = verify_telegram_bot(token)
    if ok:
        return jsonify({
            'success': True,
            'bot': res,
            'message': f"تم التحقق بنجاح! متصل بالبوت: @{res.get('username')} ({res.get('first_name')})"
        })
    else:
        return jsonify({'success': False, 'message': str(res)})


@system_bp.route('/api/notifications/test-send', methods=['POST'], endpoint="api_notifications_test_send")
def api_notifications_test_send():
    msg = request.form.get('message', '').strip()
    target_chat = request.form.get('target_chat', '').strip() or None
    from services.bot_notifications_service import send_telegram_alert
    ok, res = send_telegram_alert(msg or "🚀 Test Message from MAX RADIUS", message_type='TEST_ALERT', override_chat_id=target_chat)
    return jsonify({'success': ok, 'message': res})



@system_bp.route('/loyalty-rewards', endpoint="loyalty_rewards_page")
@system_bp.route('/loyalty', endpoint="loyalty_rewards_short")
def loyalty_rewards_page():
    from services.loyalty_rewards_service import get_loyalty_overview
    data = get_loyalty_overview()
    return render_template('loyalty_rewards.html', data=data)


@system_bp.route('/api/loyalty-rewards/reward/<int:reward_id>', methods=['GET'], endpoint="api_loyalty_reward_get")
def api_loyalty_reward_get(reward_id):
    from services.loyalty_rewards_service import get_reward_by_id
    reward = get_reward_by_id(reward_id)
    if not reward:
        return jsonify({'success': False, 'message': 'المكافأة غير موجودة'}), 404
    return jsonify({'success': True, 'reward': dict(reward)})


@system_bp.route('/api/loyalty-rewards/reward/add', methods=['POST'], endpoint="api_loyalty_reward_add")
def api_loyalty_reward_add():
    reward_name = request.form.get('reward_name', '').strip()
    points_cost = int(request.form.get('points_cost', 0))
    reward_type = request.form.get('reward_type', 'data_bonus_mb')
    reward_value = int(request.form.get('reward_value', 0))
    description = request.form.get('description', '').strip()
    icon = request.form.get('icon', 'fa-gift').strip()
    is_active = 1 if request.form.get('is_active') in ['1', 'true', 'on'] else 0

    from services.loyalty_rewards_service import create_reward
    ok, msg = create_reward(reward_name, points_cost, reward_type, reward_value, description, icon, is_active)
    return jsonify({'success': ok, 'message': msg})


@system_bp.route('/api/loyalty-rewards/reward/edit', methods=['POST'], endpoint="api_loyalty_reward_edit")
def api_loyalty_reward_edit():
    reward_id = int(request.form.get('reward_id', 0))
    reward_name = request.form.get('reward_name', '').strip()
    points_cost = int(request.form.get('points_cost', 0))
    reward_type = request.form.get('reward_type', 'data_bonus_mb')
    reward_value = int(request.form.get('reward_value', 0))
    description = request.form.get('description', '').strip()
    icon = request.form.get('icon', 'fa-gift').strip()
    is_active = 1 if request.form.get('is_active') in ['1', 'true', 'on'] else 0

    from services.loyalty_rewards_service import update_reward
    ok, msg = update_reward(reward_id, reward_name, points_cost, reward_type, reward_value, description, icon, is_active)
    return jsonify({'success': ok, 'message': msg})


@system_bp.route('/api/loyalty-rewards/reward/delete', methods=['POST'], endpoint="api_loyalty_reward_delete")
def api_loyalty_reward_delete():
    reward_id = int(request.form.get('reward_id', 0))
    from services.loyalty_rewards_service import delete_reward
    ok, msg = delete_reward(reward_id)
    return jsonify({'success': ok, 'message': msg})


@system_bp.route('/api/loyalty-rewards/reward/toggle', methods=['POST'], endpoint="api_loyalty_reward_toggle")
def api_loyalty_reward_toggle():
    reward_id = int(request.form.get('reward_id', 0))
    is_active = 1 if request.form.get('is_active') in ['1', 'true', 'on'] else 0
    from services.loyalty_rewards_service import toggle_reward_status
    ok, msg = toggle_reward_status(reward_id, is_active)
    return jsonify({'success': ok, 'message': msg})


@system_bp.route('/api/loyalty-rewards/award', methods=['POST'], endpoint="api_loyalty_award_points")
def api_loyalty_award_points():
    user = request.form.get('username', '').strip()
    pts = int(request.form.get('points', 0))
    rsn = request.form.get('reason', 'Admin Award').strip()
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
@login_required
@require_feature('automation_rules')
def automation_rules_page():
    from services.automation_rules_service import get_automation_overview
    data = get_automation_overview()
    return render_template('automation_rules.html', data=data)


@system_bp.route('/api/automation-rules/toggle', methods=['POST'], endpoint="api_automation_toggle")
@login_required
@require_feature('automation_rules')
def api_automation_toggle():
    rule_id = int(request.form.get('rule_id', 0))
    is_active = int(request.form.get('is_active', 1))
    from services.automation_rules_service import toggle_rule
    ok, msg = toggle_rule(rule_id, is_active)
    return jsonify({'success': ok, 'message': msg})


@system_bp.route('/api/automation-rules/run', methods=['POST'], endpoint="api_automation_run")
@login_required
@require_feature('automation_rules')
def api_automation_run():
    rule_id = int(request.form.get('rule_id', 0))
    from services.automation_rules_service import execute_rule_now
    ok, msg = execute_rule_now(rule_id)
    return jsonify({'success': ok, 'message': msg})


@system_bp.route('/api/automation-tasks', methods=['GET', 'POST'], endpoint='api_automation_tasks')
@system_bp.route('/api/automation-tasks/<action>', methods=['POST'], endpoint='api_automation_task_action')
@login_required
@require_feature('automation_rules')
def api_automation_tasks(action=None):
    # These are system-wide maintenance tasks and financial reports, not reseller jobs.
    from core.ownership import is_global_admin
    if not is_global_admin(get_current_manager()):
        return jsonify(success=False, message='إدارة المهام العامة للمدير الرئيسي فقط'), 403
    from services import automation_task_service as tasks
    try:
        if request.method == 'GET':
            return jsonify(success=True, **tasks.overview())
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError('بيانات الطلب غير صالحة')
        if action is None:
            return jsonify(success=True, task=tasks.save(data))
        if action == 'delete':
            tasks.remove(data.get('id'), data.get('revision'))
        elif action == 'run':
            return jsonify(success=True, run_id=tasks.submit(data.get('id'), data.get('request_id')))
        elif action == 'cancel':
            tasks.cancel(data.get('run_id'))
        elif action == 'schedule':
            from services.automation_schedule_service import next_times
            return jsonify(success=True, dates=[d.isoformat() for d in next_times(data)])
        elif action == 'policy':
            return jsonify(success=True, policy=tasks.save_policy(data))
        else:
            raise ValueError('إجراء غير صالح')
        return jsonify(success=True)
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        return jsonify(success=False, message=str(exc)), 400


@system_bp.route('/api/automation-notices', endpoint='api_automation_notices')
@login_required
@require_feature('automation_rules')
def api_automation_notices():
    from core.ownership import is_global_admin
    if not is_global_admin(get_current_manager()):
        return jsonify(success=False), 403
    from services.automation_task_service import ensure_tables
    ensure_tables()
    notices = query_all("""SELECT n.task_id,n.state,n.message,n.updated_at FROM wisp_automation_notices n
        JOIN wisp_automation_tasks t ON t.id=n.task_id
        WHERE t.deleted_at IS NULL AND t.enabled=1 AND (n.state='low' OR n.updated_at>UTC_TIMESTAMP()-INTERVAL 1 DAY)
        ORDER BY n.updated_at DESC LIMIT 20""")
    return jsonify(success=True, notices=notices)


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
            from services.migration_upload_service import save_backup_upload
            temp_path = save_backup_upload(f)
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
    from services.database_migration_service import start_async_migration
    try:
        data = request.get_json(silent=True) or {}
        server_path = data.get('server_path') or data.get('temp_server_path')
        
        if not server_path or not os.path.exists(server_path):
            from services.migration_upload_service import cached_backup
            default_cached = cached_backup()
            if default_cached:
                server_path = default_cached

        if not server_path or not os.path.exists(server_path):
            return jsonify({'success': False, 'error': 'مسار ملف النسخة الاحتياطية غير موجود أو غير صالح على السيرفر.'}), 400

        res = start_async_migration(server_path, options=data)
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
@system_bp.route('/tools/backups', endpoint="tools_backups_short")
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


@system_bp.route('/api/backups/send-telegram', methods=['POST'], endpoint="api_send_backup_telegram")
def api_send_backup_telegram():
    try:
        filename = request.values.get('filename', '').strip()
        if not filename and request.is_json:
            filename = (request.json or {}).get('filename', '').strip()
        if not filename:
            return jsonify({'success': False, 'message': 'يرجى تحديد اسم ملف النسخة الاحتياطية.'})
        admin_user = session.get('admin_username') or 'admin'
        from services.backup_service import send_backup_to_telegram
        success, msg = send_backup_to_telegram(filename, admin_username=admin_user)
        return jsonify({'success': success, 'message': msg})
    except Exception as e:
        return jsonify({'success': False, 'message': f'خطأ أثناء إرسال النسخة عبر تيليجرام: {str(e)}'}), 500


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
        import_consumption = (request.form.get('import_consumption', 'no').strip().lower() in ('yes', 'true', '1'))

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
        success, msg, data = execute_import(
            import_type=import_type,
            cleaned_rows=cleaned_rows,
            batch_name=batch_name,
            admin_username=admin_user,
            import_consumption=import_consumption
        )

        if not success:
            return jsonify({'success': False, 'message': msg}), 500

        redirect_url = url_for('view_batch_details', batch_id=data['batch_id']) if (import_type == 'vouchers' and data.get('batch_id')) else url_for('subscribers')

        return jsonify({
            'success': True,
            'message': msg,
            'count': data.get('count', len(cleaned_rows)),
            'batch_name': data.get('batch_name'),
            'imported_consumption_count': data.get('imported_consumption_count', 0),
            'redirect_url': redirect_url
        })

    except Exception as e:
        return jsonify({'success': False, 'message': f'حدث استثناء غير متوقع أثناء معالجة الاستيراد: {str(e)}'}), 500


@system_bp.route('/settings/license', methods=['GET'], endpoint="license_status_page")
@system_bp.route('/license', methods=['GET'], endpoint="license_short")
@system_bp.route('/settings/license-management', methods=['GET'], endpoint="license_management")
@system_bp.route('/settings/license/manage', methods=['GET'], endpoint="license_status")
def license_status_page():
    manager = get_current_manager()
    if not manager:
        return redirect(url_for('login', next=url_for('license_status_page')))
    lic_info = get_active_license_status()
    from core.license_protocol import instance_public_key
    lic_info = dict(lic_info, instance_public_key=instance_public_key(STORAGE_DIR),
                    current_instance_uuid=get_instance_uuid())
    return render_template('license_status.html', license=lic_info)


@system_bp.route('/settings/license/activate', methods=['POST'], endpoint="activate_license_action")
@login_required
@require_permission('settings_manage')
def activate_license_action():
    master_url = request.form.get('master_server_url', DEFAULT_MASTER_SERVER_URL).strip()
    
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
        flash("يرجى اختيار ملف الترخيص (.lic) أو إدخال كود مفتاح الترخيص المعتمد", "danger")
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
@require_permission('settings_manage')
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
    if not manager or manager.get('role_code') != 'superadmin':
        return jsonify({'success': False, 'message': 'تحديث النظام متاح لمدير النظام الرئيسي فقط.'}), 403
    operator_name = manager.get('username') or 'Admin'
    
    data = request.get_json(silent=True) or {}
    backup_first = data.get('backup_first', True)
    target_version = data.get('version')
    
    res = trigger_system_update(target_version=target_version, backup_first=backup_first, triggered_by=operator_name,
                                release_id=data.get('release_id'), manifest_sha256=data.get('manifest_sha256'))
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



@system_bp.route('/api/exports/<token>/cancel',methods=['POST'],endpoint='cancel_export')
def cancel_export_request(token):
    from services.heavy_operation_service import cancel_export
    manager=get_current_manager()
    if not manager:
        return jsonify(success=False,message='يرجى تسجيل الدخول أولًا.'),401
    if not cancel_export(token,manager['id']):
        return jsonify(success=False,message='لا توجد عملية تصدير نشطة تخصك بهذا المعرف.'),404
    return jsonify(success=True,message='جارٍ إلغاء التصدير'),202

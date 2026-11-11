"""Adapters to existing maintenance engines; never implement accounting again."""
import datetime as dt
from decimal import Decimal
from pathlib import Path
import re
import shutil
import tempfile
import time
import hashlib
from zoneinfo import ZoneInfo

from database.db import query_all, query_one, execute_write, execute_update, get_connection


def sales_window(period, now):
    today = now.date()
    if period == 'day':
        return today - dt.timedelta(days=1), today - dt.timedelta(days=1)
    if period == 'week':
        end = today - dt.timedelta(days=today.weekday() + 1)
        return end - dt.timedelta(days=6), end
    end = today.replace(day=1) - dt.timedelta(days=1)
    return end.replace(day=1), end


def sales_summary(parameters, timezone, reference=None):
    from services.sales_reporting_service import report_source, MONEY_SQL, filters
    from core.license_feature_policy import enabled
    if not enabled('sales_reports'):
        raise ValueError('تقارير المبيعات غير متاحة في الترخيص')
    now = dt.datetime.fromisoformat(reference).astimezone(ZoneInfo(timezone)) if reference else dt.datetime.now(ZoneInfo(timezone))
    start, end = sales_window(parameters.get('period', 'week'), now)
    conn = get_connection()
    totals = {}
    activity = {}
    try:
        with conn.cursor() as cur:
            cur.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
            for kind in ('voucher', 'invoice'):
                _, where, values = filters(dict(period='custom', date_from=str(start), date_to=str(end), record_kind=kind), owner=None, timezone=timezone)
                cur.execute('SELECT ' + MONEY_SQL + ' FROM ' + report_source(kind) + ' WHERE ' + where, values)
                row = cur.fetchone()
                totals[kind] = {k: str(v) if isinstance(v, Decimal) else v for k, v in row.items()}
            # Same current activity fields as the notification-center report.
            # Read them in the existing read-only snapshot, not during delivery.
            cur.execute("SELECT COUNT(*) AS cnt FROM wisp_subscribers WHERE status='active'")
            activity['active_subscribers'] = int(cur.fetchone()['cnt'])
            cur.execute('SELECT COUNT(DISTINCT username) AS cnt FROM radacct WHERE acctstoptime IS NULL')
            activity['online_sessions'] = int(cur.fetchone()['cnt'])
    finally:
        conn.rollback()
        conn.close()
    return dict(success=True, message='اكتمل ملخص المبيعات', period_start=str(start), period_end=str(end),
                timezone=timezone, metrics=totals, activity=activity, telegram='not_configured')


def clean_temporary(age, progress, cancelled):
    # Existing PDF renderer owns this exact prefix and uses the same heavy lock.
    # Do not scan storage, uploads, backups, arbitrary paths or Chromium profiles.
    root = Path(tempfile.gettempdir()).resolve()
    removed = size = skipped = 0
    for folder in root.glob('max-pdf-*'):
        if cancelled():
            return dict(success=False, cancelled=True, processed=removed, bytes_freed=size)
        if not re.fullmatch(r'max-pdf-[a-z0-9_]{8}', folder.name) or folder.is_symlink() or not folder.is_dir():
            continue
        files = list(folder.iterdir())
        if (len(files) > 5000 or time.time() - folder.stat().st_mtime < age * 86400 or
                any(f.is_symlink() or not f.is_file() or not re.fullmatch(r'\d+\.(html|pdf)', f.name) for f in files) or
                any(time.time() - f.stat().st_mtime < age * 86400 for f in files)):
            skipped += 1
            continue
        for f in files:
            # No recursive delete, and recheck immediately before unlinking.
            if f.is_symlink() or f.resolve().parent != root / folder.name:
                raise ValueError('تغير مسار ملف مؤقت؛ أوقف التنظيف')
            size += f.stat().st_size
            f.unlink()
            removed += 1
        folder.rmdir()
        progress(dict(processed=removed, bytes_freed=size, skipped=skipped))
    return dict(success=True, processed=removed, bytes_freed=size, skipped=skipped, message='اكتمل تنظيف ملفات PDF المؤقتة القديمة المسموحة')


def clean_logs(age, progress, cancelled):
    # Only terminal automation results and sent/failed notification delivery logs.
    removed = 0
    for table, date_column, condition in (
            ('wisp_automation_runs', 'created_at', "state IN ('success','failed','partial','cancelled','skipped','interrupted')"),
            ('wisp_automation_logs', 'executed_at', '1=1'),
            ('wisp_notification_logs', 'created_at', "status IN ('sent','failed')")):
        if not query_one('SELECT 1 n FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=?', (table,)):
            continue
        while not cancelled():
            if table == 'wisp_automation_runs':
                changed = execute_update(f"UPDATE {table} SET result=JSON_OBJECT('message','انتهت مدة حفظ تفاصيل النتيجة') WHERE {condition} AND {date_column}<UTC_TIMESTAMP()-INTERVAL ? DAY AND JSON_LENGTH(result)>1 LIMIT 250", (age,))
            else:
                changed = execute_update(f'DELETE FROM {table} WHERE {condition} AND {date_column}<UTC_TIMESTAMP()-INTERVAL ? DAY LIMIT 250', (age,))
            removed += changed
            progress(dict(processed=removed))
            if changed < 250:
                break
    return dict(success=not cancelled(), cancelled=cancelled(), processed=removed, message='انتهى تنظيف سجلات التشغيل المحددة')


def perform_action(value, progress, cancelled, task_id, run_id):
    p, kind = value['params'], value['kind']
    if cancelled():
        return dict(success=False, cancelled=True, message='ألغيت المهمة قبل التنفيذ')
    if kind == 'summary':
        from services.accounting_archiver_service import run_archive_maintenance
        result = run_archive_maintenance(days=p['age'], mode='summary', chunk_size=min(p['batch'], 1000), progress_callback=progress, cancel_callback=cancelled, account_scope=p, account_limit=p['batch'])
        result['partial'] = (not result.get('complete') or bool(result.get('protected_cards'))) and not result.get('cancelled')
        return result
    if kind == 'delete':
        from services.account_lifecycle_service import job_lock
        from services.db_maintenance_service import delete_expired_vouchers
        with job_lock('factory-reset') as acquired:
            if not acquired:
                raise ValueError('توجد عملية صيانة متعارضة')
            result = delete_expired_vouchers(delete_type=p.get('reason', 'all'), delete_acct=False,
                batch_limit=p['batch'], min_age_days=p['age'], progress_callback=progress, cancel_callback=cancelled, account_scope=p)
        result['partial'] = not result.get('deletion_complete') and not result.get('cancelled')
        return result
    if kind == 'audit':
        from services.db_maintenance_service import run_database_audit
        result = run_database_audit()
        # Account-level details stay in the protected maintenance audit, not notifications.
        result={k: v for k, v in result.items() if k != 'issues'}
        result['message']=f"اكتمل الفحص؛ {result.get('total_issues',0)} ملاحظة، دون أي إصلاح تلقائي"
        result['partial']=bool(result.get('total_issues'))
        return result
    if kind == 'temp':
        return clean_temporary(p['age'], progress, cancelled)
    if kind == 'logs':
        return clean_logs(p['age'], progress, cancelled)
    if kind == 'alert':
        from core.config import STORAGE_DIR
        usage = shutil.disk_usage(STORAGE_DIR)
        free_percent = usage.free * 100 / usage.total
        state = 'low' if free_percent < p['threshold'] else 'recovered'
        message = f'المساحة المتاحة {free_percent:.1f}%؛ الحد المحدد {p["threshold"]}%'
        previous = query_one('SELECT state FROM wisp_automation_notices WHERE task_id=?', (task_id,))
        changed = state == 'low' and not previous or bool(previous and previous['state'] != state and previous['state'] in ('low', 'recovered'))
        if state == 'low' or (previous and previous['state'] == 'low'):
            execute_write('INSERT INTO wisp_automation_notices(task_id,state,message,updated_at) VALUES(?,?,?,UTC_TIMESTAMP()) ON DUPLICATE KEY UPDATE state=VALUES(state),message=VALUES(message),updated_at=VALUES(updated_at)', (task_id, state, message))
        return dict(success=True, free_bytes=usage.free, free_percent=free_percent, message=message, changed=changed)
    if kind == 'report':
        reference = {'reference': value['scheduled_for']} if value.get('scheduled_for') else {}
        result = sales_summary(p, value['schedule']['timezone'], **reference)
        # Commit the report before attempting delivery. An uncertain delivery is never replayed.
        progress(result)
        if cancelled():
            return dict(result, cancelled=True, telegram='not_sent')
        from services.bot_notifications_service import get_notification_settings, send_telegram_alert
        settings = (get_notification_settings().get('settings') or {})
        if settings.get('is_enabled') and settings.get('bot_token') and settings.get('chat_id'):
            period_key=hashlib.sha256((result['period_start']+':'+result['period_end']).encode()).hexdigest()
            claimed=execute_update("INSERT IGNORE INTO wisp_automation_deliveries(task_id,period_key,status,updated_at) VALUES(?,?,'sending_uncertain',UTC_TIMESTAMP())",(task_id,period_key))
            if not claimed:
                prior=query_one('SELECT status FROM wisp_automation_deliveries WHERE task_id=? AND period_key=?',(task_id,period_key))
                result['telegram']='already_sent' if prior and prior['status']=='sent' else 'needs_review'
                result['partial']=result['telegram']=='needs_review'
                return result
            result['telegram'] = 'sending_uncertain'
            progress(result)
            from services.sales_summary_message import format_sales_summary
            voucher, invoice = result['metrics']['voucher'], result['metrics']['invoice']
            text = format_sales_summary(p.get('period', 'week'), result['period_start'], result['period_end'],
                voucher_count=voucher['sold_count'], voucher_revenue=voucher['total_gross_revenue'],
                invoice_count=invoice['sold_count'], invoice_revenue=invoice['total_gross_revenue'],
                **result.get('activity', {}))
            ok, _ = send_telegram_alert(text, message_type='AUTOMATION_SALES_SUMMARY')
            result['telegram'] = 'sent' if ok else 'failed_or_uncertain'
            result['partial'] = not ok
            execute_write('UPDATE wisp_automation_deliveries SET status=?,updated_at=UTC_TIMESTAMP() WHERE task_id=? AND period_key=?',(result['telegram'],task_id,period_key))
        return result
    if kind == 'optimize':
        from core.config import STORAGE_DIR
        from services.db_maintenance_service import optimize_database_tables, get_detailed_table_sizes
        sizes = get_detailed_table_sizes()
        if not sizes.get('success'):
            raise ValueError('تعذر التحقق من حجم الجداول قبل التحسين')
        if shutil.disk_usage(STORAGE_DIR).free < max(512 * 1024 ** 2, 2 * float(sizes['total_db_mb']) * 1024 ** 2):
            raise ValueError('المساحة المتاحة لا تكفي لهامش التحسين')
        progress(dict(message='بدأ تحسين الجداول؛ الأمر الجاري لا يقبل الإلغاء الآمن'))
        result = optimize_database_tables(p['tables'])
        result['cancel_not_supported_after_start'] = True
        return result
    raise ValueError('نوع مهمة غير مدعوم')

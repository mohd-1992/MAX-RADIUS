# -*- coding: utf-8 -*-
"""
Automated Backup Scheduler & Daemon Service for MAX RADIUS.
Powered by APScheduler (with background thread fallback) to execute scheduled comprehensive backups,
monitor health, handle watchdog probes, and support live control actions (start/stop/restart/reload).
"""

import os
import sys
import time
import datetime
import threading
import logging
from services.backup_service import create_backup, get_backup_settings
from core.time_service import get_system_timezone, get_system_now, get_system_now_str

logger = logging.getLogger('backup_scheduler')

# Global Scheduler Instance
_SCHEDULER = None
_SCHEDULER_LOCK = threading.Lock()
_SCHEDULER_STATE = {
    'is_running': False,
    'started_at': None,
    'last_run_time': None,
    'last_run_status': None,
    'last_error': None,
    'jobs': [],
    'uptime_sec': 0
}

def _scheduled_backup_job(scheduled_time_str):
    """Execution wrapper for scheduled backup trigger."""
    from services.factory_reset_service import factory_reset_active
    if factory_reset_active():
        return
    now_str = get_system_now_str()
    logger.info(f"Triggering automated backup scheduled at {scheduled_time_str} (System Time: {now_str})...")
    
    _SCHEDULER_STATE['last_run_time'] = now_str
    try:
        success, msg, data = create_backup(
            admin_username='system_scheduler',
            notes=f'نسخة احتياطية تلقائية مجدولة ({scheduled_time_str})'
        )
        if success:
            _SCHEDULER_STATE['last_run_status'] = 'success'
            _SCHEDULER_STATE['last_error'] = None
            logger.info(f"Automated backup succeeded: {msg}")
        else:
            _SCHEDULER_STATE['last_run_status'] = 'error'
            _SCHEDULER_STATE['last_error'] = msg
            logger.error(f"Automated backup failed: {msg}")
    except Exception as err:
        _SCHEDULER_STATE['last_run_status'] = 'error'
        _SCHEDULER_STATE['last_error'] = str(err)
def _scheduled_expiry_check_job():
    """Periodic task to scan active accounts and mark expired vouchers and subscribers."""
    from services.factory_reset_service import factory_reset_active
    if factory_reset_active():
        return
    try:
        from services.voucher_service import check_and_update_expired_vouchers
        check_and_update_expired_vouchers()
    except Exception as e:
        logger.error(f"Error in automated expiry check job: {e}")

def _scheduled_daily_sales_summary_job():
    """Trigger daily sales summary dispatch to Telegram at 23:59."""
    from services.factory_reset_service import factory_reset_active
    if factory_reset_active():
        return
    try:
        # The user rule replaces this legacy timer once explicitly enabled.
        from database.db import query_one
        rule=query_one("SELECT COUNT(*) n FROM wisp_automation_rules WHERE action_type='SEND_DAILY_SALES_REPORT' AND is_active=1 AND schedule_enabled=1")
        if rule and rule['n']:
            return
        from services.bot_notifications_service import trigger_daily_sales_summary
        ok, res = trigger_daily_sales_summary()
        logger.info(f"Daily sales summary job completed: ok={ok}, res={res}")
    except Exception as e:
        logger.error(f"Error in daily sales summary job: {e}")

def _scheduled_nas_watchdog_job():
    """Probe all NAS routers and alert via Telegram if any router goes down/recovers."""
    from services.factory_reset_service import factory_reset_active
    if factory_reset_active():
        return
    try:
        from core.mikrotik_api import get_all_nas_live_status
        from services.bot_notifications_service import trigger_nas_down_notification, trigger_nas_recovered_notification
        
        statuses = get_all_nas_live_status(force_refresh=True)
        for dev in (statuses or []):
            nas_id = dev.get('id')
            nas_name = dev.get('name') or f"NAS-{nas_id}"
            nas_ip = dev.get('ip_address') or '-'
            is_online = dev.get('is_online', False)
            status_text = dev.get('status_text') or 'Offline'
            
            if not is_online:
                trigger_nas_down_notification(nas_id, nas_name, nas_ip, status_text)
            else:
                trigger_nas_recovered_notification(nas_id, nas_name, nas_ip)
    except Exception as e:
        logger.error(f"Error in NAS watchdog job: {e}")

def _scheduled_low_quota_check_job():
    """Scan active subscribers for low quota thresholds and dispatch warnings."""
    from services.factory_reset_service import factory_reset_active
    if factory_reset_active():
        return
    try:
        from database.db import query_all
        from services.bot_notifications_service import trigger_low_quota_notification
        
        rows = query_all("""
            SELECT s.username, 
                   COALESCE(p.volume_quota_mb, 0) as total_quota_mb,
                   COALESCE(SUM(r.acctinputoctets + r.acctoutputoctets), 0) as used_bytes
            FROM wisp_subscribers s
            JOIN wisp_packages p ON s.package_id = p.id
            LEFT JOIN radacct r ON r.username = s.username
            WHERE s.status = 'active' AND p.volume_quota_mb > 0
            GROUP BY s.username, p.volume_quota_mb
        """)
        for r in (rows or []):
            total_mb = float(r.get('total_quota_mb') or 0.0)
            used_mb = float(r.get('used_bytes') or 0.0) / (1024 * 1024)
            rem_mb = max(0.0, total_mb - used_mb)
            
            if total_mb > 0 and (rem_mb <= (total_mb * 0.10) or rem_mb <= 100.0) and rem_mb > 0:
                trigger_low_quota_notification(r['username'], rem_mb, total_mb)
    except Exception as e:
        logger.error(f"Error in low quota check job: {e}")

def _register_background_jobs(scheduler, system_tz):
    """Register all persistent background monitoring & notification jobs in APScheduler."""
    from apscheduler.triggers.interval import IntervalTrigger
    from apscheduler.triggers.cron import CronTrigger

    # Lifecycle expiry is owned exclusively by core.watchdog.
    scheduler.add_job(_scheduled_automation_rules_job, trigger=IntervalTrigger(seconds=60),
                      id='automation_rules', name='Automation rules', replace_existing=True, max_instances=1, coalesce=True)

    # 2. Daily sales summary at 23:59
    try:
        scheduler.add_job(
            _scheduled_daily_sales_summary_job,
            trigger=CronTrigger(hour=23, minute=59, timezone=system_tz),
            id="daily_sales_summary_telegram",
            name="Daily Sales Summary Telegram Alert",
            replace_existing=True
        )
    except Exception as e:
        logger.error(f"Failed to add daily sales summary job: {e}")

    # 3. NAS Router Watchdog every 2 minutes
    try:
        scheduler.add_job(
            _scheduled_nas_watchdog_job,
            trigger=IntervalTrigger(seconds=120),
            id="nas_health_watchdog_telegram",
            name="NAS Router Health Watchdog and Telegram Alert",
            replace_existing=True
        )
    except Exception as e:
        logger.error(f"Failed to add NAS watchdog job: {e}")

    # 4. Subscriber Low Quota Warning Check every 5 minutes
    try:
        scheduler.add_job(
            _scheduled_low_quota_check_job,
            trigger=IntervalTrigger(seconds=300),
            id="subscriber_low_quota_check",
            name="Subscriber Low Quota Telegram Alert",
            replace_existing=True
        )
    except Exception as e:
        logger.error(f"Failed to add low quota check job: {e}")

def init_backup_scheduler():
    """Initialize and start the backup scheduler with the database configured Timezone."""
    import os
    if os.environ.get('MAX_MAINTENANCE_MODE') == '1':
        return None
    global _SCHEDULER
    with _SCHEDULER_LOCK:
        if _SCHEDULER is not None and _SCHEDULER_STATE['is_running']:
            return _SCHEDULER

        from services.automation_rules_service import ensure_automation_tables
        ensure_automation_tables()
        settings = get_backup_settings()
        system_tz = get_system_timezone()
        
        # Try APScheduler BackgroundScheduler
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger
            
            scheduler = BackgroundScheduler(daemon=True, timezone=system_tz)
            _SCHEDULER = scheduler
            
            if settings.get('auto_enabled'):
                times = settings.get('times', ['03:00'])
                for t in times:
                    try:
                        parts = t.strip().split(':')
                        hour = int(parts[0])
                        minute = int(parts[1]) if len(parts) > 1 else 0
                        job_id = f"auto_backup_{hour:02d}_{minute:02d}"
                        scheduler.add_job(
                            _scheduled_backup_job,
                            trigger=CronTrigger(hour=hour, minute=minute, timezone=system_tz),
                            id=job_id,
                            name=f"Backup at {hour:02d}:{minute:02d}",
                            args=[f"{hour:02d}:{minute:02d}"],
                            replace_existing=True
                        )
                    except Exception as ex:
                        logger.error(f"Failed to add schedule job for time {t}: {ex}")
                        
            # Register background monitoring & telegram notification jobs
            _register_background_jobs(scheduler, system_tz)

            scheduler.start()
            _SCHEDULER_STATE['is_running'] = True
            _SCHEDULER_STATE['started_at'] = time.time()
            logger.info(f"APScheduler started successfully with Timezone [{system_tz}].")
            
        except ImportError:
            # Fallback to Thread-based scheduler daemon
            logger.warning("APScheduler not installed, using built-in Thread timer scheduler daemon.")
            _start_thread_scheduler(settings)
            
        return _SCHEDULER

def _start_thread_scheduler(settings):
    """Fallback thread loop that checks for scheduled backup times in the system timezone."""
    global _SCHEDULER_STATE
    _SCHEDULER_STATE['is_running'] = True
    _SCHEDULER_STATE['started_at'] = time.time()
    
    def loop():
        last_executed_minute = None
        last_expiry_check = 0
        last_nas_check = 0
        last_quota_check = 0
        last_daily_summary_minute = None
        while _SCHEDULER_STATE['is_running']:
            try:
                now_ts = time.time()
                # 1. Expiry check (every 60s)
                if now_ts - last_expiry_check >= 60:
                    last_expiry_check = now_ts
                    _scheduled_automation_rules_job()

                # 2. NAS Watchdog (every 120s)
                if now_ts - last_nas_check >= 120:
                    last_nas_check = now_ts
                    _scheduled_nas_watchdog_job()

                # 3. Low Quota Check (every 300s)
                if now_ts - last_quota_check >= 300:
                    last_quota_check = now_ts
                    _scheduled_low_quota_check_job()

                # 4. Scheduled Daily Summary at 23:59
                now = get_system_now()
                curr_hm = now.strftime('%H:%M')
                if curr_hm == '23:59' and last_daily_summary_minute != curr_hm:
                    last_daily_summary_minute = curr_hm
                    _scheduled_daily_sales_summary_job()

                # 5. Scheduled Backups
                curr_settings = get_backup_settings()
                if curr_settings.get('auto_enabled'):
                    if curr_hm in curr_settings.get('times', []) and curr_hm != last_executed_minute:
                        last_executed_minute = curr_hm
                        _scheduled_backup_job(curr_hm)
            except Exception as e:
                logger.error(f"Thread scheduler loop error: {e}")
            time.sleep(25)
            
    t = threading.Thread(target=loop, daemon=True, name="BackupThreadScheduler")
    t.start()

def reload_backup_schedule():
    """Reloads the scheduled jobs dynamically with current timezone and backup settings."""
    global _SCHEDULER
    settings = get_backup_settings()
    system_tz = get_system_timezone()
    
    with _SCHEDULER_LOCK:
        if _SCHEDULER and hasattr(_SCHEDULER, 'remove_all_jobs'):
            try:
                _SCHEDULER.remove_all_jobs()
                if hasattr(_SCHEDULER, 'timezone'):
                    _SCHEDULER.timezone = system_tz
                    
                if settings.get('auto_enabled'):
                    from apscheduler.triggers.cron import CronTrigger
                    times = settings.get('times', ['03:00'])
                    for t in times:
                        try:
                            parts = t.strip().split(':')
                            hour = int(parts[0])
                            minute = int(parts[1]) if len(parts) > 1 else 0
                            job_id = f"auto_backup_{hour:02d}_{minute:02d}"
                            _SCHEDULER.add_job(
                                _scheduled_backup_job,
                                trigger=CronTrigger(hour=hour, minute=minute, timezone=system_tz),
                                id=job_id,
                                name=f"Backup at {hour:02d}:{minute:02d}",
                                args=[f"{hour:02d}:{minute:02d}"],
                                replace_existing=True
                            )
                        except Exception as ex:
                            logger.error(f"Error adding job for {t}: {ex}")

                # Re-register background monitoring & telegram notification jobs
                _register_background_jobs(_SCHEDULER, system_tz)

                logger.info(f"Backup schedule reloaded with Timezone [{system_tz}] and settings.")
                return True, "تم تحديث جدول النسخ الاحتياطي بنجاح."
            except Exception as err:
                logger.error(f"Failed to reload schedule: {err}")
                return False, str(err)
                
    return True, "تم تطبيق الإعدادات الجديدة."

def stop_backup_scheduler():
    """Stops the scheduler daemon."""
    global _SCHEDULER
    with _SCHEDULER_LOCK:
        _SCHEDULER_STATE['is_running'] = False
        if _SCHEDULER and hasattr(_SCHEDULER, 'shutdown'):
            try:
                _SCHEDULER.shutdown(wait=False)
                _SCHEDULER = None
            except Exception:
                pass
        logger.info("Backup scheduler stopped.")
        return True, "تم إيقاف خدمة المجدول بنجاح."

def start_backup_scheduler():
    """Starts or restarts the scheduler daemon."""
    stop_backup_scheduler()
    init_backup_scheduler()
    return True, "تم تشغيل خدمة المجدول بنجاح."

def restart_backup_scheduler():
    """Restarts the scheduler daemon."""
    stop_backup_scheduler()
    time.sleep(0.5)
    init_backup_scheduler()
    return True, "تمت إعادة تشغيل خدمة المجدول بنجاح."

def get_scheduler_status():
    """Retrieve live status, uptime, and next run times for the backup scheduler."""
    settings = get_backup_settings()
    is_running = _SCHEDULER_STATE['is_running']
    
    uptime_sec = 0
    if is_running and _SCHEDULER_STATE['started_at']:
        uptime_sec = int(time.time() - _SCHEDULER_STATE['started_at'])
        
    # Get upcoming jobs / next run times
    jobs_info = []
    next_run_str = 'غير مجدول'
    
    if _SCHEDULER and hasattr(_SCHEDULER, 'get_jobs'):
        try:
            for job in _SCHEDULER.get_jobs():
                next_fire = job.next_run_time
                next_fire_str = next_fire.strftime('%Y-%m-%d %H:%M') if next_fire else 'غير محدد'
                jobs_info.append({
                    'id': job.id,
                    'name': job.name,
                    'next_run': next_fire_str
                })
            if jobs_info and jobs_info[0].get('next_run'):
                next_run_str = jobs_info[0]['next_run']
        except Exception:
            pass
            
    if not jobs_info and settings.get('auto_enabled'):
        times = settings.get('times', [])
        if times:
            next_run_str = f"يومياً في: {', '.join(times)}"
            
    # Format human readable uptime
    from services.system_control_service import format_seconds
    uptime_str = format_seconds(uptime_sec) if is_running else 'متوقف'
    
    status_code = 'running' if (is_running and settings.get('auto_enabled')) else ('idle' if is_running else 'stopped')
    status_text = 'يعمل ومجدول بنشاط' if (is_running and settings.get('auto_enabled')) else ('المجدول يعمل (النسخ معطل)' if is_running else 'متوقف')
    
    return {
        'id': 'backup_scheduler',
        'name': 'مجدول النسخ الاحتياطي والأتمتة',
        'subname': 'APScheduler Automated Backup Engine',
        'icon': 'fa-solid fa-clock-rotate-left',
        'color': 'amber',
        'status': 'running' if is_running else 'stopped',
        'is_running': is_running,
        'auto_enabled': settings.get('auto_enabled', False),
        'status_text': status_text,
        'uptime': uptime_str,
        'uptime_sec': uptime_sec,
        'times': settings.get('times', []),
        'interval_days': settings.get('interval_days', 1),
        'storage_path': settings.get('storage_path', ''),
        'next_run': next_run_str,
        'last_run': settings.get('last_run') or _SCHEDULER_STATE.get('last_run_time') or 'لم ينفذ بعد',
        'last_status': settings.get('last_status') or _SCHEDULER_STATE.get('last_run_status') or 'normal',
        'last_message': settings.get('last_message') or '',
        'type': 'الأتمتة والنسخ الاحتياطي',
        'details': f"الجدولة: {', '.join(settings.get('times', []))} | مسار التخزين: {settings.get('storage_path', '')}"
    }

def healthcheck_backup_scheduler():
    """Watchdog healthcheck probe for the backup scheduler daemon."""
    status = get_scheduler_status()
    is_healthy = status['is_running']
    
    # If auto backup is enabled in settings, but scheduler is stopped, attempt self-healing restart
    if not is_healthy and status.get('auto_enabled'):
        try:
            logger.warning("Autoheal Watchdog: Backup scheduler found stopped while enabled. Restarting...")
            start_backup_scheduler()
            status = get_scheduler_status()
            is_healthy = status['is_running']
        except Exception as e:
            logger.error(f"Watchdog auto-recovery failed for backup scheduler: {e}")
            
    return {
        'healthy': is_healthy,
        'status': status['status'],
        'auto_enabled': status['auto_enabled'],
        'next_run': status['next_run'],
        'last_run': status['last_run'],
        'timestamp': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }


def _scheduled_automation_rules_job():
    from services.factory_reset_service import factory_reset_active
    if factory_reset_active():
        return
    from services.automation_rules_service import run_due_rules
    run_due_rules()

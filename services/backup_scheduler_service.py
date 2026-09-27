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
    try:
        from services.voucher_service import check_and_update_expired_vouchers
        check_and_update_expired_vouchers()
    except Exception as e:
        logger.error(f"Error in automated expiry check job: {e}")

def init_backup_scheduler():
    """Initialize and start the backup scheduler with the database configured Timezone."""
    global _SCHEDULER
    with _SCHEDULER_LOCK:
        if _SCHEDULER is not None and _SCHEDULER_STATE['is_running']:
            return _SCHEDULER

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
                        
            # Add lightweight real-time periodic check & disconnect for expired vouchers and subscribers (every 1 minute)
            try:
                from apscheduler.triggers.interval import IntervalTrigger
                scheduler.add_job(
                    _scheduled_expiry_check_job,
                    trigger=IntervalTrigger(seconds=60),
                    id="auto_voucher_expiry_check",
                    name="Automated Voucher and Subscriber Expiry Check & Disconnect",
                    replace_existing=True
                )
            except Exception as e_job:
                logger.error(f"Failed to add expiry check job: {e_job}")

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
        while _SCHEDULER_STATE['is_running']:
            try:
                now_ts = time.time()
                if now_ts - last_expiry_check >= 60:  # 1 minute real-time watchdog
                    last_expiry_check = now_ts
                    _scheduled_expiry_check_job()

                curr_settings = get_backup_settings()
                if curr_settings.get('auto_enabled'):
                    now = get_system_now()
                    curr_hm = now.strftime('%H:%M')
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

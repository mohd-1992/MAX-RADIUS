"""
services/automation_rules_service.py
------------------------------------
Smart Automation & Rules Engine for MAX RADIUS.
Provides trigger-based actions (auto-purge expired, auto-generate invoices, auto-alert on low quota).
"""

import time
from datetime import datetime
from database.db import get_connection

_get_db = get_connection
_READY = False

def ensure_automation_tables():
    """Ensure automation rules and execution logs tables exist."""
    global _READY
    if _READY:
        return
    db = _get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS wisp_automation_rules (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    rule_name VARCHAR(128) NOT NULL,
                    trigger_event VARCHAR(64) NOT NULL,
                    action_type VARCHAR(64) NOT NULL,
                    parameters JSON NULL,
                    is_active TINYINT(1) DEFAULT 1,
                    last_run_at DATETIME NULL,
                    execution_count INT DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS wisp_automation_logs (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    rule_id INT NOT NULL,
                    rule_name VARCHAR(128) NOT NULL,
                    status ENUM('success', 'failed', 'skipped') DEFAULT 'success',
                    affected_count INT DEFAULT 0,
                    details TEXT NULL,
                    executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_rule (rule_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)
            cur.execute("SHOW COLUMNS FROM wisp_automation_rules LIKE 'schedule_enabled'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE wisp_automation_rules ADD COLUMN IF NOT EXISTS schedule_enabled TINYINT NOT NULL DEFAULT 0")
            cur.execute("UPDATE wisp_automation_rules SET rule_name='تنظيف الكروت المنتهية وصلاحيتها قبل 60 يوماً' WHERE action_type='CLEAN_EXPIRED_CARDS' AND rule_name='تنظيف كروت الشحن المنتهية منذ أكثر من 60 يوماً'")
            # Populate default automation rules if empty
            cur.execute("SELECT COUNT(*) as cnt FROM wisp_automation_rules")
            if (cur.fetchone() or {}).get('cnt', 0) == 0:
                cur.execute("""
                    INSERT INTO wisp_automation_rules (rule_name, trigger_event, action_type, is_active)
                    VALUES 
                    ('أرشفة الجلسات القديمة تلقائياً (> 90 يوم)', 'DAILY_MIDNIGHT', 'ARCHIVE_OLD_SESSIONS', 1),
                    ('تنظيف الكروت المنتهية وصلاحيتها قبل 60 يوماً', 'WEEKLY_SCHEDULE', 'CLEAN_EXPIRED_CARDS', 1),
                    ('فحص ومزامنة جلسات الميكروتيك العالقة', 'EVERY_15_MINUTES', 'PURGE_ZOMBIE_SESSIONS', 1),
                    ('توليد ملخص تقارير المبيعات اليومي', 'DAILY_23_59', 'SEND_DAILY_SALES_REPORT', 1)
                """)
            db.commit()
            _READY = True
    finally:
        db.close()

def get_automation_overview():
    """Get active rules, recent execution logs and status."""
    ensure_automation_tables()
    db = _get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT * FROM wisp_automation_rules ORDER BY id ASC")
            rules = cur.fetchall()
            for rule in rules:
                rule["is_active"] = bool(rule["is_active"] and rule.get("schedule_enabled"))

            cur.execute("SELECT * FROM wisp_automation_logs ORDER BY id DESC LIMIT 20")
            logs = cur.fetchall()

            cur.execute("SELECT COUNT(*) as active_cnt FROM wisp_automation_rules WHERE is_active = 1 AND schedule_enabled=1")
            active_cnt = (cur.fetchone() or {}).get('active_cnt', 0)

            cur.execute("SELECT COUNT(*) as total_runs FROM wisp_automation_logs")
            total_runs = (cur.fetchone() or {}).get('total_runs', 0)

        return {
            'rules': rules,
            'logs': logs,
            'active_rules_count': active_cnt,
            'total_runs_count': total_runs
        }
    finally:
        db.close()

def toggle_rule(rule_id, is_active):
    """Enable or disable an automation rule."""
    db = _get_db()
    try:
        with db.cursor() as cur:
            cur.execute("UPDATE wisp_automation_rules SET is_active = %s, schedule_enabled=1 WHERE id = %s", (int(is_active), rule_id))
            db.commit()
            return True, "تم تعديل حالة القاعدة بنجاح."
    except Exception as e:
        return False, str(e)
    finally:
        db.close()

def execute_rule_now(rule_id, scheduled=False, expected_last=None):
    from services.factory_reset_service import factory_reset_active
    if factory_reset_active():
        return False, 'إعادة المصنع قيد التنفيذ.'
    import json
    from services.account_lifecycle_service import job_lock
    from database.db import query_one, execute_write
    with job_lock(f'automation-rule-{int(rule_id)}') as acquired:
        if not acquired:
            return False, 'القاعدة قيد التنفيذ بالفعل.'
        rule=query_one('SELECT * FROM wisp_automation_rules WHERE id=?',(rule_id,))
        if not rule:return False, 'القاعدة غير موجودة.'
        if scheduled and (not rule['is_active'] or not rule.get('schedule_enabled') or rule.get('last_run_at')!=expected_last):
            return False, 'تغيرت حالة الجدولة؛ ألغيت المهمة.'
        try:
            params=json.loads(rule.get('parameters') or '{}')
            if not isinstance(params,dict):raise ValueError('معلمات قاعدة غير صالحة')
            action=rule['action_type'];affected=0
            if action=='PURGE_ZOMBIE_SESSIONS':
                from services.autoheal_service import purge_stale_zombie_sessions
                ok,msg=purge_stale_zombie_sessions()
            elif action=='ARCHIVE_OLD_SESSIONS':
                from services.accounting_archiver_service import archive_old_sessions
                ok,msg=archive_old_sessions(days_threshold=max(1,int(params.get('days',90))))
            elif action=='CLEAN_EXPIRED_CARDS':
                from services.db_maintenance_service import delete_expired_vouchers
                res=delete_expired_vouchers(min_age_days=max(60,int(params.get('days',60))))
                affected=res['deleted_vouchers'];ok=res['success'];msg=res['message']
            elif action=='SEND_DAILY_SALES_REPORT':
                from services.bot_notifications_service import trigger_daily_sales_summary
                ok,msg=trigger_daily_sales_summary()
            else:
                ok=False;msg='نوع قاعدة غير مدعوم؛ لم تنفذ العملية.'
        except Exception as exc:
            ok=False;msg=str(exc);affected=0
        execute_write('UPDATE wisp_automation_rules SET last_run_at=NOW(),execution_count=execution_count+1 WHERE id=?',(rule_id,))
        execute_write('INSERT INTO wisp_automation_logs (rule_id,rule_name,status,affected_count,details) VALUES (?,?,?,?,?)',(rule_id,rule['rule_name'],'success' if ok else 'failed',affected,str(msg)))
        return bool(ok),str(msg)


def run_due_rules():
    from services.license_guard_service import has_license_feature
    if not has_license_feature('automation_rules'):
        return
    from database.db import query_all
    from core.time_service import get_system_now, get_db_storage_now
    ensure_automation_tables()
    now=get_system_now()
    for rule in query_all('SELECT * FROM wisp_automation_rules WHERE is_active=1 AND schedule_enabled=1 ORDER BY id'):
        event=rule['trigger_event'];last=rule.get('last_run_at')
        timed_last=last.replace(tzinfo=get_db_storage_now().tzinfo).astimezone(now.tzinfo) if last else None
        due=False
        if event=='EVERY_15_MINUTES':due=last is None or (now-timed_last).total_seconds()>=900
        elif event=='WEEKLY_SCHEDULE':due=last is None or (now-timed_last).total_seconds()>=604800
        elif event in ('DAILY_MIDNIGHT','DAILY_23_59'):
            due=(last is None or timed_last.date()<now.date()) and (event=='DAILY_MIDNIGHT' or (now.hour==23 and now.minute>=59))
        if due:
            execute_rule_now(rule['id'],scheduled=True,expected_last=last)

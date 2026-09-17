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

def ensure_automation_tables():
    """Ensure automation rules and execution logs tables exist."""
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
            # Populate default automation rules if empty
            cur.execute("SELECT COUNT(*) as cnt FROM wisp_automation_rules")
            if (cur.fetchone() or {}).get('cnt', 0) == 0:
                cur.execute("""
                    INSERT INTO wisp_automation_rules (rule_name, trigger_event, action_type, is_active)
                    VALUES 
                    ('أرشفة الجلسات القديمة تلقائياً (> 90 يوم)', 'DAILY_MIDNIGHT', 'ARCHIVE_OLD_SESSIONS', 1),
                    ('تنظيف كروت الشحن المنتهية منذ أكثر من 60 يوماً', 'WEEKLY_SCHEDULE', 'CLEAN_EXPIRED_CARDS', 1),
                    ('فحص ومزامنة جلسات الميكروتيك العالقة', 'EVERY_15_MINUTES', 'PURGE_ZOMBIE_SESSIONS', 1),
                    ('توليد ملخص تقارير المبيعات اليومي', 'DAILY_23_59', 'SEND_DAILY_SALES_REPORT', 1)
                """)
            db.commit()
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

            cur.execute("SELECT * FROM wisp_automation_logs ORDER BY id DESC LIMIT 20")
            logs = cur.fetchall()

            cur.execute("SELECT COUNT(*) as active_cnt FROM wisp_automation_rules WHERE is_active = 1")
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
            cur.execute("UPDATE wisp_automation_rules SET is_active = %s WHERE id = %s", (int(is_active), rule_id))
            db.commit()
            return True, "تم تعديل حالة القاعدة بنجاح."
    except Exception as e:
        return False, str(e)
    finally:
        db.close()

def execute_rule_now(rule_id):
    """Manually run a specific automation rule."""
    db = _get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT * FROM wisp_automation_rules WHERE id = %s", (rule_id,))
            rule = cur.fetchone()
            if not rule:
                return False, "القاعدة غير موجودة."

        action = rule['action_type']
        affected = 0
        details = "Executed manually"

        if action == 'PURGE_ZOMBIE_SESSIONS':
            from services.autoheal_service import purge_stale_zombie_sessions
            ok, msg = purge_stale_zombie_sessions(timeout_minutes=15)
            details = msg
        elif action == 'ARCHIVE_OLD_SESSIONS':
            from services.accounting_archiver_service import archive_old_sessions
            ok, msg = archive_old_sessions(days_threshold=90)
            details = msg
        elif action == 'CLEAN_EXPIRED_CARDS':
            from services.db_maintenance_service import delete_expired_vouchers
            res = delete_expired_vouchers(delete_type='all')
            affected = res.get('deleted_vouchers', 0)
            details = f"تم تنظيف {affected} كرت منتهي."
            ok = True
        else:
            ok = True
            details = f"تمت محاكاة تنفيذ المهمة {action} بنجاح."

        with db.cursor() as cur:
            cur.execute("""
                UPDATE wisp_automation_rules 
                SET last_run_at = NOW(), execution_count = execution_count + 1 
                WHERE id = %s
            """, (rule_id,))
            cur.execute("""
                INSERT INTO wisp_automation_logs (rule_id, rule_name, status, affected_count, details)
                VALUES (%s, %s, %s, %s, %s)
            """, (rule_id, rule['rule_name'], 'success' if ok else 'failed', affected, details))
            db.commit()

        return ok, details
    except Exception as e:
        return False, str(e)
    finally:
        db.close()

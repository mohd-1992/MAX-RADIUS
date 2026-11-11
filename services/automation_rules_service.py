"""
services/automation_rules_service.py
------------------------------------
Smart Automation & Rules Engine for MAX RADIUS.
Provides trigger-based actions (auto-purge expired, auto-generate invoices, auto-alert on low quota).
"""

from core.license_feature_policy import guarded as _license_guarded
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
            # Legacy definitions are retired by the versioned task installer. Never reseed.
            db.commit()
            from services.automation_task_service import ensure_tables
            ensure_tables()
            _READY = True
    finally:
        db.close()

@_license_guarded('automation_rules')
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

@_license_guarded('automation_rules')
def _toggle_rule_legacy(rule_id, is_active):
    """Enable or disable an automation rule."""
    if not str(rule_id).isdigit() or int(rule_id) < 1:
        return False, 'القاعدة غير موجودة.'
    ensure_automation_tables()
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

@_license_guarded('automation_rules')
def toggle_rule(rule_id, is_active):
    return False, 'القواعد السابقة أُلغيت؛ استخدم المهام الحالية.'


@_license_guarded('automation_rules')
def execute_rule_now(rule_id, scheduled=False, expected_last=None):
    if not str(rule_id).isdigit() or int(rule_id) < 1:
        return False, 'القاعدة غير موجودة.'
    return False, 'القواعد السابقة أُلغيت؛ استخدم المهام الحالية.'


def run_due_rules():
    from core.license_feature_policy import enabled as _feature_enabled
    if not _feature_enabled('automation_rules'):
        return
    from database.db import query_all
    from core.time_service import get_system_now, get_db_storage_now
    ensure_automation_tables()
    from services.automation_task_service import tick
    tick()

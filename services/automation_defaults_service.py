"""One-time, transactional release defaults; never launch or replay a task."""
import datetime as dt
import json
import uuid
from zoneinfo import ZoneInfo


VERSION = 2
NAMESPACE = uuid.UUID('5377f6fc-15a2-46a2-ac65-66b831c61bf9')
STOCK_RULES = {
    'ARCHIVE_OLD_SESSIONS': {'أرشفة الجلسات القديمة تلقائياً (> 90 يوم)'},
    'CLEAN_EXPIRED_CARDS': {'تنظيف الكروت المنتهية وصلاحيتها قبل 60 يوماً',
                          'تنظيف كروت الشحن المنتهية منذ أكثر من 60 يوماً'},
    'SEND_DAILY_SALES_REPORT': {'توليد ملخص تقارير المبيعات اليومي'},
}


def equivalent_action(left, right):
    """Default installation only: different ages/scopes are not duplicates.

    Reuse an enabled equivalent custom action instead of adding another schedule.
    This deliberately does not change validation of manually added tasks.
    """
    if left['kind'] != right['kind']:
        return False
    kind = left['kind']
    fields = {'delete': ('age', 'batch', 'reason'), 'summary': ('age', 'batch'),
              'report': ('period',), 'temp': ('age',), 'logs': ('age',)}.get(kind)
    if fields is None:
        return False
    from services.automation_scope_service import normalize_scope
    lp, rp = left['params'], right.get('params', {})
    defaults = dict(age=90 if kind in ('summary', 'logs') else 7 if kind == 'temp' else 60,
                    batch=5000 if kind == 'delete' else 500, reason='all', period='week')
    return (normalize_scope(lp) == normalize_scope(rp) and
            all(lp.get(k, defaults[k]) == rp.get(k, defaults[k]) for k in fields))


def default_definitions(now, timezone='Asia/Aden'):
    local = now.astimezone(ZoneInfo(timezone))
    start = local.date().isoformat()
    rows = [
        ('delete-expired', 'حذف الكروت المنتهية منذ 14 يومًا', 'delete',
         dict(mode='interval', interval=3, unit='days', startTime='06:00', times=['06:00']),
         dict(age=14, batch=5000, reason='all')),
        ('summary', 'تلخيص الجلسات القديمة', 'summary',
         dict(mode='daily', times=['04:00']), dict(age=90, batch=500)),
        ('temporary', 'تنظيف الملفات المؤقتة', 'temp',
         dict(mode='weekly', days=[1], times=['01:00']), dict(age=7)),
        ('logs', 'تنظيف السجلات التشغيلية', 'logs',
         dict(mode='weekly', days=[1], times=['02:00']), dict(age=90)),
        ('weekly-report', 'تقرير المبيعات الأسبوعي', 'report',
         dict(mode='weekly', days=[1], times=['00:00']), dict(period='week')),
        ('monthly-report', 'تقرير المبيعات الشهري', 'report',
         dict(mode='monthly', monthDay=1, times=['00:30']), dict(period='month')),
    ]
    return [dict(key=key, id=uuid.uuid5(NAMESPACE, key).hex,
                 value=dict(name=name, kind=kind, enabled=True, consent=True,
                            params=dict(scope='all', **params),
                            options=dict(notify=True, resourcePause=True),
                            schedule=dict(start=start, timezone=timezone,
                                          endMode='never', **schedule)))
            for key, name, kind, schedule, params in rows]


def install_defaults():
    from database.db import get_connection
    from services.automation_task_service import task_lock, validate, dump
    from services.automation_schedule_service import next_times
    from core.time_service import get_configured_timezone_name
    now = dt.datetime.now(dt.timezone.utc)
    timezone = get_configured_timezone_name()
    definitions = default_definitions(now, timezone)
    with task_lock('config', timeout=5) as acquired:
        if not acquired:
            return False
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT definition FROM wisp_automation_policy WHERE id=1 FOR UPDATE')
                settings = json.loads(cur.fetchone()['definition'])
                # A small receipt prevents a permanently removed default returning.
                removed = set(settings.get('removed_default_ids', []))
                cur.execute('SELECT id FROM wisp_automation_tasks WHERE deleted_at IS NOT NULL')
                removed.update(row['id'] for row in cur.fetchall() if row['id'] in {d['id'] for d in definitions})
                cur.execute('''DELETE n FROM wisp_automation_notices n JOIN wisp_automation_tasks t
                    ON t.id=n.task_id WHERE t.deleted_at IS NOT NULL''')
                cur.execute("DELETE FROM wisp_automation_tasks WHERE deleted_at IS NOT NULL AND NOT EXISTS (SELECT 1 FROM wisp_automation_runs r WHERE r.task_id=wisp_automation_tasks.id AND r.state IN ('queued','running'))")
                settings['removed_default_ids'] = sorted(removed)
                # Reduce old idle definitions to the new server-side ceiling.
                # An already recorded run is capped separately before execution.
                cur.execute('''UPDATE wisp_automation_tasks t SET
                    definition=JSON_SET(definition,'$.params.batch',5000),revision=revision+1
                    WHERE kind='delete' AND deleted_at IS NULL
                    AND CAST(JSON_UNQUOTE(JSON_EXTRACT(definition,'$.params.batch')) AS UNSIGNED)>5000
                    AND NOT EXISTS(SELECT 1 FROM wisp_automation_runs r
                        WHERE r.task_id=t.id AND r.state IN ('queued','running'))''')
                cur.execute("SELECT id,kind,enabled,definition FROM wisp_automation_tasks WHERE deleted_at IS NULL")
                existing = cur.fetchall()
                default_ids = {item['id'] for item in definitions}
                cur.execute("SELECT 1 present FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='wisp_automation_rules'")
                legacy_exists = bool(cur.fetchone())
                legacy = []
                if legacy_exists:
                    cur.execute("SELECT 1 present FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='wisp_automation_rules' AND COLUMN_NAME='schedule_enabled'")
                    schedule_column = 'schedule_enabled' if cur.fetchone() else '0 AS schedule_enabled'
                    cur.execute('SELECT id,rule_name,action_type,is_active,' + schedule_column + ' FROM wisp_automation_rules FOR UPDATE')
                    legacy = cur.fetchall()
                # Legacy definitions are retired; their independent execution log remains.
                # Refuse retirement while an old worker owns a rule, rather than racing it.
                from contextlib import ExitStack
                from services.account_lifecycle_service import job_lock
                locks = ExitStack()
                paused, conflicts = [], []
                for rule in legacy:
                    if not locks.enter_context(job_lock('automation-rule-' + str(rule['id']))):
                        raise ValueError('انتظر انتهاء القاعدة القديمة قبل ترقية الأتمتة')
                if legacy:
                    cur.execute('DELETE FROM wisp_automation_rules')
                if settings.get('restore_review_required') or settings.get('release_defaults', {}).get('version', 0) >= VERSION:
                    cur.execute('UPDATE wisp_automation_policy SET definition=%s WHERE id=1', (dump(settings),))
                    conn.commit()
                    return True
                deferred = []
                adopted = {}
                prior = settings.get('release_defaults', {})
                for item in definitions:
                    if item['id'] in removed or any(row['id'] == item['id'] for row in existing):
                        continue
                    if item['id'] in prior.get('ids', []):
                        # An earlier install already handled it; absence means removal.
                        removed.add(item['id'])
                        continue
                    value = validate(item['value'])
                    duplicate = next((row for row in existing if row['enabled'] and row['id'] not in default_ids
                        and equivalent_action(value, json.loads(row['definition']))), None)
                    if duplicate:
                        adopted[item['key']] = duplicate['id']
                        continue
                    dates = next_times(value['schedule'], after=now, count=1)
                    next_at = dates[0].replace(tzinfo=None) if dates and value['enabled'] else None
                    cur.execute('''INSERT IGNORE INTO wisp_automation_tasks
                        (id,name,kind,enabled,definition,next_at,created_at)
                        VALUES(%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP())''',
                        (item['id'], value['name'], value['kind'], value['enabled'], dump(value), next_at))
                    if not value['enabled']:
                        cur.execute('''INSERT IGNORE INTO wisp_automation_notices
                            (task_id,state,message,updated_at) VALUES(%s,'review',%s,UTC_TIMESTAMP())''',
                            (item['id'], 'المهمة الافتراضية تنتظر مراجعة وجود مهمة مخصصة أو قاعدة قديمة مماثلة؛ لم تنشأ جدولة مكررة'))
                settings['release_defaults'] = dict(version=VERSION, installed_at=now.isoformat(),
                    ids=[item['id'] for item in definitions], legacy_paused=paused,
                    custom_legacy_conflicts=deferred, adopted=adopted, legacy_retired=len(legacy))
                settings['removed_default_ids'] = sorted(removed)
                cur.execute('UPDATE wisp_automation_policy SET definition=%s WHERE id=1', (dump(settings),))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
            if 'locks' in locals():
                locks.close()

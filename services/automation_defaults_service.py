"""One-time, transactional release defaults; never launch or replay a task."""
import datetime as dt
import json
import uuid
from zoneinfo import ZoneInfo


VERSION = 1
NAMESPACE = uuid.UUID('5377f6fc-15a2-46a2-ac65-66b831c61bf9')
STOCK_RULES = {
    'ARCHIVE_OLD_SESSIONS': {'أرشفة الجلسات القديمة تلقائياً (> 90 يوم)'},
    'CLEAN_EXPIRED_CARDS': {'تنظيف الكروت المنتهية وصلاحيتها قبل 60 يوماً',
                          'تنظيف كروت الشحن المنتهية منذ أكثر من 60 يوماً'},
    'SEND_DAILY_SALES_REPORT': {'توليد ملخص تقارير المبيعات اليومي'},
}


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
    with task_lock('defaults-v271', timeout=5) as acquired:
        if not acquired:
            return False
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT definition FROM wisp_automation_policy WHERE id=1 FOR UPDATE')
                settings = json.loads(cur.fetchone()['definition'])
                # Reduce old idle definitions to the new server-side ceiling.
                # An already recorded run is capped separately before execution.
                cur.execute('''UPDATE wisp_automation_tasks t SET
                    definition=JSON_SET(definition,'$.params.batch',5000),revision=revision+1
                    WHERE kind='delete' AND deleted_at IS NULL
                    AND CAST(JSON_UNQUOTE(JSON_EXTRACT(definition,'$.params.batch')) AS UNSIGNED)>5000
                    AND NOT EXISTS(SELECT 1 FROM wisp_automation_runs r
                        WHERE r.task_id=t.id AND r.state IN ('queued','running'))''')
                if settings.get('restore_review_required') or settings.get('release_defaults', {}).get('version', 0) >= VERSION:
                    conn.commit()
                    return True
                cur.execute("SELECT id,kind,definition FROM wisp_automation_tasks WHERE enabled=1 AND deleted_at IS NULL")
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
                paused, conflicts = [], []
                for rule in legacy:
                    if not (rule['is_active'] and rule['schedule_enabled']):
                        continue
                    if rule['action_type'] not in STOCK_RULES:
                        continue
                    if rule['rule_name'] in STOCK_RULES[rule['action_type']]:
                        cur.execute('UPDATE wisp_automation_rules SET schedule_enabled=0 WHERE id=%s', (rule['id'],))
                        paused.append(dict(id=rule['id'], schedule_enabled=1))
                    else:
                        conflicts.append(rule['action_type'])
                kind_action = {'summary': 'ARCHIVE_OLD_SESSIONS', 'delete': 'CLEAN_EXPIRED_CARDS',
                               'report': 'SEND_DAILY_SALES_REPORT'}
                deferred = []
                for item in definitions:
                    value = validate(item['value'])
                    duplicate = any(row['id'] not in default_ids and row['kind'] == value['kind']
                        and (value['kind'] != 'report' or json.loads(row['definition']).get('params', {}).get('period', 'week') == value['params'].get('period', 'week'))
                        for row in existing)
                    if kind_action.get(value['kind']) in conflicts or duplicate:
                        value['enabled'] = False
                        deferred.append(item['key'])
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
                    custom_legacy_conflicts=deferred)
                cur.execute('UPDATE wisp_automation_policy SET definition=%s WHERE id=1', (dump(settings),))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

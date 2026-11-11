"""Durable Web automation. A recorded attempt is never replayed after interruption."""
import contextlib
import datetime as dt
import json
import logging
import threading
import time
import uuid

from database.db import get_connection, query_all, query_one, execute_write, execute_update
from services.automation_schedule_service import normalize, next_times

KINDS = {'summary', 'delete', 'audit', 'temp', 'logs', 'alert', 'report', 'optimize'}
TERMINAL = {'success', 'partial', 'failed', 'cancelled', 'skipped', 'interrupted'}
HEAVY = KINDS - {'alert'}
OPTIMIZE_TABLES = {'radacct', 'radacct_archive', 'wisp_imported_session_history',
                   'radpostauth', 'radcheck', 'radusergroup', 'wisp_vouchers',
                   'wisp_voucher_sales', 'wisp_subscribers', 'wisp_audit_logs'}
_ready = False
_schema_lock = threading.Lock()
log = logging.getLogger(__name__)


@contextlib.contextmanager
def task_lock(name, timeout=0):
    """64-byte, database-scoped names also work for long tenant schema names."""
    conn=get_connection();acquired=False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT GET_LOCK(SHA2(CONCAT(DATABASE(),':automation-',%s),256),%s) AS acquired",(name,timeout))
            acquired=cur.fetchone()['acquired']==1
        conn.commit()
        yield acquired
    finally:
        if acquired:
            with conn.cursor() as cur:
                cur.execute("SELECT RELEASE_LOCK(SHA2(CONCAT(DATABASE(),':automation-',%s),256))",(name,))
            conn.commit()
        conn.close()


def dump(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def resource_pressure():
    """Best-effort admission, respecting Web's cgroup memory limit as well as host RAM."""
    import psutil
    from pathlib import Path
    available=psutil.virtual_memory().available
    try:
        root=Path('/sys/fs/cgroup')
        maximum=(root/'memory.max').read_text().strip()
        if maximum!='max':
            available=min(available,max(0,int(maximum)-int((root/'memory.current').read_text())))
    except (OSError,ValueError):
        pass
    return available<256*1024*1024 or psutil.cpu_percent(interval=.1)>85


def ensure_tables(seed_defaults=True):
    global _ready
    with _schema_lock:
        if _ready:
            return
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                for ddl in (
                    '''CREATE TABLE IF NOT EXISTS wisp_automation_tasks (
                        id CHAR(32) PRIMARY KEY, revision INT NOT NULL DEFAULT 1,
                        name VARCHAR(90) NOT NULL, kind VARCHAR(16) NOT NULL,
                        enabled TINYINT NOT NULL DEFAULT 0, definition JSON NOT NULL,
                        next_at DATETIME NULL, scheduled_count INT NOT NULL DEFAULT 0,
                        deleted_at DATETIME NULL, created_at DATETIME NOT NULL,
                        INDEX due(enabled,next_at)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''',
                    '''CREATE TABLE IF NOT EXISTS wisp_automation_runs (
                        id CHAR(32) PRIMARY KEY, task_id CHAR(32) NOT NULL,
                        slot_key VARCHAR(96) NOT NULL UNIQUE, state VARCHAR(16) NOT NULL,
                        definition JSON NOT NULL, result JSON NOT NULL,
                        cancel_requested TINYINT NOT NULL DEFAULT 0,
                        created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
                        INDEX task_history(task_id,created_at), INDEX active(state,updated_at)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''',
                    '''CREATE TABLE IF NOT EXISTS wisp_automation_notices (
                        task_id CHAR(32) PRIMARY KEY, state VARCHAR(16) NOT NULL,
                        message VARCHAR(500) NOT NULL, updated_at DATETIME NOT NULL
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''',
                    '''CREATE TABLE IF NOT EXISTS wisp_automation_policy (
                        id INT PRIMARY KEY, definition JSON NOT NULL
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''',
                    '''CREATE TABLE IF NOT EXISTS wisp_automation_deliveries (
                        task_id CHAR(32) NOT NULL, period_key CHAR(64) NOT NULL,
                        status VARCHAR(32) NOT NULL, updated_at DATETIME NOT NULL,
                        PRIMARY KEY(task_id,period_key)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'''):
                    cur.execute(ddl)
                cur.execute('INSERT IGNORE INTO wisp_automation_policy(id,definition) VALUES(1,%s)',
                            (dump(dict(missed='skip', retention=90, resourcePause=True, notify=True)),))
            conn.commit()
            _ready = True
        finally:
            conn.close()
        if seed_defaults:
            from services.automation_defaults_service import install_defaults
            try:
                _ready = install_defaults()
            except Exception:
                _ready = False
                raise


def validate(data):
    if not isinstance(data, dict) or data.get('kind') not in KINDS:
        raise ValueError('نوع المهمة غير صالح')
    name = str(data.get('name', '')).strip()
    if not 1 <= len(name) <= 90:
        raise ValueError('اسم المهمة مطلوب وبحد أقصى 90 حرفًا')
    kind = data['kind']
    p = dict(data.get('params') or {})
    # Never silently expand a selected scope into all accounts.
    if p.get('scope', 'all') != 'all' and kind not in ('delete','summary'):
        raise ValueError('هذه المهمة تدعم النطاق الشامل فقط حاليًا؛ لم تُحفظ مهمة بنطاق مختلف')
    from services.automation_scope_service import normalize_scope
    p.update(normalize_scope(p))
    for field, default, maximum in (('age', 90 if kind == 'summary' else 60, 3650),
                                   ('batch', 5000 if kind == 'delete' else 500, 5000 if kind == 'delete' else 50000), ('threshold', 15, 99)):
        p[field] = int(p.get(field, default))
        minimum=0 if field=='age' and kind=='delete' else 2 if field=='age' and kind=='summary' else 1
        if not minimum <= p[field] <= maximum:
            raise ValueError('حدود المهمة غير صالحة')
    if p.get('reason', 'all') not in ('all', 'time', 'quota'):
        raise ValueError('سبب الانتهاء غير صالح')
    if p.get('period', 'week') not in ('day', 'week', 'month'):
        raise ValueError('فترة التقرير غير صالحة')
    if kind == 'optimize':
        tables = p.get('tables')
        if not isinstance(tables, list) or not tables or any(t not in OPTIMIZE_TABLES for t in tables):
            raise ValueError('اختر الجداول المسموحة للتحسين')
        p['tables'] = sorted(set(tables))
    if kind in ('delete', 'logs', 'optimize') and data.get('consent') is not True:
        raise ValueError('تحتاج المهمة موافقة صريحة على نطاق الحذف أو الصيانة')
    options = data.get('options') or {}
    return dict(name=name, kind=kind, params=p, schedule=normalize(data['schedule']),
                options=dict(notify=bool(options.get('notify', True)),
                             resourcePause=bool(options.get('resourcePause', True))),
                enabled=data.get('enabled') is True, consent=data.get('consent') is True)


def utc_stamp(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).isoformat().replace('+00:00', 'Z')


def _public(row):
    value = json.loads(row['definition'])
    value.update(id=row['id'], revision=row['revision'], enabled=bool(row['enabled']),
                 scheduled_count=row['scheduled_count'], next_at=utc_stamp(row['next_at']))
    value['next_dates']=[d.isoformat() for d in next_times(value['schedule'], scheduled_count=row['scheduled_count'])] if value['enabled'] else []
    return value


def overview():
    ensure_tables()
    recover_interrupted()
    task_rows = query_all('''SELECT t.*,(SELECT COUNT(*) FROM wisp_automation_runs r WHERE r.task_id=t.id) AS run_count
        FROM wisp_automation_tasks t WHERE deleted_at IS NULL ORDER BY created_at,id''')
    tasks = [_public(r) for r in task_rows]
    for value, row in zip(tasks, task_rows):
        value['run_count'] = row['run_count']
    runs = query_all('SELECT * FROM wisp_automation_runs WHERE created_at>=UTC_TIMESTAMP()-INTERVAL ? DAY ORDER BY created_at DESC,id DESC LIMIT 100', (policy()['retention'],))
    for r in runs:
        r['definition'] = json.loads(r['definition'])
        r['result'] = json.loads(r['result'])
        r['created_at'] = utc_stamp(r['created_at'])
        r['updated_at'] = utc_stamp(r['updated_at'])
    return dict(tasks=tasks, runs=runs, policy=policy(), notices=task_notices(), packages=query_all('SELECT id,name FROM wisp_packages ORDER BY id'), batches=query_all('SELECT id,name FROM wisp_voucher_batches ORDER BY id DESC LIMIT 1000'))


def task_notices(disk=False):
    """Reconcile old review notices without editing any task or running work."""
    execute_write('''DELETE n FROM wisp_automation_notices n LEFT JOIN wisp_automation_tasks t ON t.id=n.task_id
        WHERE t.id IS NULL OR t.deleted_at IS NOT NULL OR (n.state='review' AND t.enabled=1)''')
    rows = query_all('''SELECT n.*,t.name,t.kind,t.enabled,t.definition FROM wisp_automation_notices n
        JOIN wisp_automation_tasks t ON t.id=n.task_id WHERE t.deleted_at IS NULL''')
    result = []
    for row in rows:
        if row['kind'] == 'alert' and row['state'] not in ('low', 'recovered'):
            continue
        if row['state'] == 'review':
            from services.automation_defaults_service import equivalent_action
            value = json.loads(row['definition'])
            peers = query_all('SELECT definition FROM wisp_automation_tasks WHERE id<>? AND enabled=1 AND deleted_at IS NULL', (row['task_id'],))
            if not any(equivalent_action(value, json.loads(p['definition'])) for p in peers):
                execute_write("DELETE FROM wisp_automation_notices WHERE task_id=? AND state='review'", (row['task_id'],))
                continue
        is_disk = row['kind'] == 'alert' and row['state'] in ('low', 'recovered')
        if disk != is_disk or (is_disk and not row['enabled']):
            continue
        result.append(dict(task_id=row['task_id'], name=row['name'], kind=row['kind'],
                           state=row['state'], message=row['message'], updated_at=utc_stamp(row['updated_at'])))
    return result


def policy():
    row=query_one('SELECT definition FROM wisp_automation_policy WHERE id=1')
    if not row:
        execute_write('INSERT IGNORE INTO wisp_automation_policy(id,definition) VALUES(1,?)',
                      (dump(dict(missed='skip',retention=90,resourcePause=True,notify=True)),))
        row=query_one('SELECT definition FROM wisp_automation_policy WHERE id=1')
    stored = json.loads(row['definition'])
    return {k: stored[k] for k in ('missed', 'retention', 'resourcePause', 'notify')}


def save_policy(value):
    if value.get('missed') not in ('skip', 'once') or int(value.get('retention', 0)) not in (30, 90, 180):
        raise ValueError('سياسة غير صالحة')
    result = dict(missed=value['missed'], retention=int(value['retention']), resourcePause=bool(value.get('resourcePause')), notify=bool(value.get('notify')))
    # Keep operator-owned default/restore receipts out of editable policy fields.
    stored = json.loads(query_one('SELECT definition FROM wisp_automation_policy WHERE id=1')['definition'])
    execute_write('UPDATE wisp_automation_policy SET definition=? WHERE id=1', (dump(dict(stored, **result)),))
    return result


def _legacy_conflict(kind):
    actions = {'summary': 'ARCHIVE_OLD_SESSIONS', 'delete': 'CLEAN_EXPIRED_CARDS', 'report': 'SEND_DAILY_SALES_REPORT'}
    action = actions.get(kind)
    return bool(action and query_one('SELECT id FROM wisp_automation_rules WHERE action_type=? AND is_active=1 AND schedule_enabled=1 LIMIT 1', (action,)))


def save(data):
    ensure_tables()
    value = validate(data)
    with task_lock('config') as acquired:
        if not acquired:
            raise ValueError('توجد عملية تعديل مهام جارية')
        if value['enabled'] and _legacy_conflict(value['kind']):
            raise ValueError('جدولة قديمة من النوع نفسه مفعلة؛ أوقفها من القواعد السابقة قبل تفعيل البديل لتجنب التكرار')
        task_id = data.get('id') or uuid.uuid4().hex
        old = query_one('SELECT * FROM wisp_automation_tasks WHERE id=? AND deleted_at IS NULL', (task_id,))
        if data.get('id') and (not old or int(data.get('revision', 0)) != old['revision']):
            raise ValueError('تغيرت المهمة؛ حدّث الصفحة')
        if query_one("SELECT id FROM wisp_automation_runs WHERE task_id=? AND state IN ('queued','running') LIMIT 1", (task_id,)):
            raise ValueError('انتظر انتهاء التشغيل أو إلغاءه قبل تعديل المهمة')
        count = old['scheduled_count'] if old else 0
        dates = next_times(value['schedule'], scheduled_count=count)
        next_at = dates[0].replace(tzinfo=None) if dates and value['enabled'] else None
        if old:
            execute_write('UPDATE wisp_automation_tasks SET name=?,kind=?,enabled=?,definition=?,next_at=?,revision=revision+1 WHERE id=?',
                          (value['name'], value['kind'], value['enabled'], dump(value), next_at, task_id))
        else:
            execute_write('INSERT INTO wisp_automation_tasks(id,name,kind,enabled,definition,next_at,created_at) VALUES(?,?,?,?,?,?,UTC_TIMESTAMP())',
                          (task_id, value['name'], value['kind'], value['enabled'], dump(value), next_at))
        execute_write("DELETE FROM wisp_automation_notices WHERE task_id=? AND state='review'", (task_id,))
        return _public(query_one('SELECT * FROM wisp_automation_tasks WHERE id=?', (task_id,)))


def remove(task_id, revision):
    ensure_tables()
    with task_lock('config') as acquired:
        if not acquired:
            raise ValueError('توجد عملية تعديل مهام جارية')
        if query_one("SELECT id FROM wisp_automation_runs WHERE task_id=? AND state IN ('queued','running')", (task_id,)):
            raise ValueError('ألغِ التشغيل قبل حذف المهمة')
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT definition FROM wisp_automation_policy WHERE id=1 FOR UPDATE')
                stored = json.loads(cur.fetchone()['definition'])
                cur.execute('DELETE FROM wisp_automation_tasks WHERE id=%s AND revision=%s AND deleted_at IS NULL', (task_id, int(revision)))
                if cur.rowcount != 1:
                    raise ValueError('تغيرت المهمة؛ حدّث الصفحة')
                from services.automation_defaults_service import default_definitions
                default_ids = {d['id'] for d in default_definitions(dt.datetime.now(dt.timezone.utc))}
                if task_id in default_ids:
                    stored['removed_default_ids'] = sorted(set(stored.get('removed_default_ids', [])) | {task_id})
                    cur.execute('UPDATE wisp_automation_policy SET definition=%s WHERE id=1', (dump(stored),))
                cur.execute('DELETE FROM wisp_automation_notices WHERE task_id=%s', (task_id,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def submit(task_id, request_id=None, scheduled=False):
    ensure_tables()
    if not scheduled:
        import re
        if not re.fullmatch('[a-f0-9]{32}', str(request_id)):
            raise ValueError('معرف طلب التشغيل غير صالح')
    with task_lock('config',timeout=2) as acquired:
        if not acquired:
            raise ValueError('يجري تجهيز مهمة')
        task = query_one('SELECT * FROM wisp_automation_tasks WHERE id=? AND deleted_at IS NULL', (task_id,))
        if not task:
            raise ValueError('المهمة غير موجودة')
        if _legacy_conflict(task['kind']):
            raise ValueError('أوقف القاعدة السابقة من النوع نفسه قبل تشغيل البديل لتجنب التكرار')
        if scheduled and (not task['enabled'] or not task['next_at'] or task['next_at'] > dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)):
            return None
        key = task_id + ':' + (str(task['next_at']) if scheduled else request_id)
        prior = query_one('SELECT id FROM wisp_automation_runs WHERE slot_key=?', (key,))
        if prior:
            return prior['id']
        if query_one("SELECT id FROM wisp_automation_runs WHERE task_id=? AND state IN ('queued','running')", (task_id,)):
            raise ValueError('المهمة قيد التشغيل')
        run_id = uuid.uuid4().hex
        # Atomically record intent and advance the schedule. A crash cannot replay work.
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO wisp_automation_runs(id,task_id,slot_key,state,definition,result,created_at,updated_at) VALUES(%s,%s,%s,'queued',%s,'{}',UTC_TIMESTAMP(),UTC_TIMESTAMP())", (run_id, task_id, key, task['definition']))
                if scheduled:
                    count = task['scheduled_count'] + 1
                    dates = next_times(json.loads(task['definition'])['schedule'], scheduled_count=count)
                    cur.execute('UPDATE wisp_automation_tasks SET scheduled_count=%s,next_at=%s WHERE id=%s', (count, dates[0].replace(tzinfo=None) if dates else None, task_id))
            conn.commit()
        finally:
            conn.close()
        try:
            launch_worker(run_id)
        except Exception:
            finish(run_id, 'failed', {'message': 'تعذر بدء العامل؛ لم تنفذ المهمة'})
            raise
        return run_id


def launch_worker(run_id):
    threading.Thread(target=worker, args=(run_id,), name='AutomationTask', daemon=True).start()


def cancel(run_id):
    row=query_one('SELECT state,definition FROM wisp_automation_runs WHERE id=?',(run_id,))
    if row and row['state']=='running' and json.loads(row['definition'])['kind']=='optimize':
        raise ValueError('بدأ أمر تحسين الجدول؛ انتظر اكتماله، ولا يمكن إلغاؤه بأمان')
    if row and json.loads(row['definition'])['kind']=='optimize':
        changed=execute_update("UPDATE wisp_automation_runs SET cancel_requested=1 WHERE id=? AND state='queued'",(run_id,))
        if not changed and query_one("SELECT id FROM wisp_automation_runs WHERE id=? AND state='running'",(run_id,)):
            raise ValueError('بدأ أمر تحسين الجدول؛ انتظر اكتماله')
        return
    execute_write("UPDATE wisp_automation_runs SET cancel_requested=1 WHERE id=? AND state IN ('queued','running')", (run_id,))


def finish(run_id, state, result):
    execute_write("UPDATE wisp_automation_runs SET state=?,result=?,updated_at=UTC_TIMESTAMP() WHERE id=? AND state IN ('queued','running')", (state, dump(result), run_id))


def recover_interrupted():
    # A named lock stays held throughout the worker, even during a slow SQL call.
    execute_write("""UPDATE wisp_automation_runs SET state='interrupted',
        result=JSON_SET(result,'$.message','توقف العامل؛ راجع النتيجة قبل إعادة التشغيل'),updated_at=UTC_TIMESTAMP()
        WHERE state IN ('queued','running') AND updated_at<UTC_TIMESTAMP()-INTERVAL 120 SECOND
        AND IS_USED_LOCK(SHA2(CONCAT(DATABASE(),':automation-run-',id),256)) IS NULL""")


def tick():
    ensure_tables()
    recover_interrupted()
    for row in query_all('SELECT id FROM wisp_automation_tasks WHERE enabled=1 AND deleted_at IS NULL AND next_at<=UTC_TIMESTAMP() ORDER BY next_at LIMIT 10'):
        try:
            submit(row['id'], scheduled=True)
        except ValueError:
            pass


def worker(run_id):
    from services.heavy_operation_service import operation_slot, OperationBusy
    try:
        with task_lock('run-' + run_id) as acquired:
            if not acquired:
                return
            row = query_one('SELECT * FROM wisp_automation_runs WHERE id=?', (run_id,))
            if not row or row['state'] != 'queued':
                return
            definition = json.loads(row['definition'])
            if definition['kind'] == 'delete' and int(definition.get('params', {}).get('batch', 5000)) > 5000:
                definition['params']['batch'] = 5000
                execute_write("UPDATE wisp_automation_runs SET definition=?,result=JSON_SET(result,'$.limit_applied',5000) WHERE id=? AND state='queued'", (dump(definition), run_id))
            value = validate(definition)
            if ' ' in row['slot_key'].split(':',1)[1]:
                # Scheduled slots contain the stored UTC datetime; manual IDs are hex.
                due = dt.datetime.fromisoformat(row['slot_key'].split(':',1)[1])
                created = row.get('created_at') or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
                if isinstance(created, str):
                    created = dt.datetime.fromisoformat(created.replace('Z', '+00:00')).replace(tzinfo=None)
                if policy()['missed'] == 'skip' and (created-due).total_seconds() > 90:
                    finish(run_id, 'skipped', {'message': 'تخطي موعد فائت وفق السياسة؛ لم يبدأ التنفيذ'})
                    return
                value['scheduled_for'] = due.replace(tzinfo=dt.timezone.utc).isoformat()
            def cancelled():
                r = query_one('SELECT cancel_requested FROM wisp_automation_runs WHERE id=?', (run_id,))
                return not r or bool(r['cancel_requested'])
            def progress(result):
                execute_write("UPDATE wisp_automation_runs SET result=?,updated_at=UTC_TIMESTAMP() WHERE id=? AND state='running'", (dump(result), run_id))
            from services.factory_reset_service import factory_reset_active
            from core.license_feature_policy import enabled
            while True:
                if cancelled():
                    finish(run_id, 'cancelled', {'message': 'ألغي التشغيل قبل البدء'})
                    return
                if factory_reset_active():
                    finish(run_id, 'skipped', {'message': 'إعادة المصنع قيد التنفيذ'})
                    return
                if not enabled('automation_rules'):
                    finish(run_id, 'failed', {'message': 'الأتمتة غير متاحة في الترخيص'})
                    return
                try:
                    with contextlib.ExitStack() as stack:
                        serial = stack.enter_context(task_lock('execution'))
                        first = query_one("SELECT id FROM wisp_automation_runs WHERE state IN ('queued','running') ORDER BY created_at,id LIMIT 1") if serial else None
                        if serial and first and first['id'] == run_id:
                            if value['kind'] in HEAVY:
                                stack.enter_context(operation_slot())
                                if (policy()['resourcePause'] or value['options']['resourcePause']) and resource_pressure():
                                    finish(run_id, 'skipped', {'message': 'أُجل التنفيذ بسبب ضغط الموارد؛ لم يبدأ أي إجراء'})
                                    return
                            if execute_update("UPDATE wisp_automation_runs SET state='running',updated_at=UTC_TIMESTAMP() WHERE id=? AND state='queued'", (run_id,)) != 1:
                                return
                            result = perform(value, progress, cancelled, row['task_id'], run_id)
                            state = 'cancelled' if result.get('cancelled') else 'success' if result.get('success') else 'partial' if result.get('processed') else 'failed'
                            if result.get('success') and (result.get('partial') or result.get('pending')):
                                state = 'partial'
                            finish(run_id, state, result)
                            return
                except OperationBusy:
                    pass
                execute_write("UPDATE wisp_automation_runs SET result=JSON_SET(result,'$.message','بانتظار دور التنفيذ؛ لم تبدأ المهمة'),updated_at=UTC_TIMESTAMP() WHERE id=? AND state='queued'", (run_id,))
                time.sleep(2)
    except OperationBusy:
        finish(run_id, 'skipped', {'message': 'توجد عملية ثقيلة أخرى؛ لم يبدأ التنفيذ'})
    except Exception:
        log.exception('Automation task failed: %s', run_id)
        previous=query_one('SELECT result FROM wisp_automation_runs WHERE id=?',(run_id,))
        result=json.loads(previous['result']) if previous else {}
        result.update(success=False,message='تعذر إكمال المهمة؛ راجع السجل التشغيلي المحمي قبل المحاولة مجددًا')
        finish(run_id, 'partial' if result.get('processed') or result.get('metrics') else 'failed', result)


def perform(value, progress, cancelled, task_id, run_id):
    from services.automation_task_actions import perform_action
    return perform_action(value, progress, cancelled, task_id, run_id)


def sanitize_restored_tasks():
    global _ready
    _ready=False
    ensure_tables(seed_defaults=False)
    stored = json.loads(query_one('SELECT definition FROM wisp_automation_policy WHERE id=1')['definition'])
    execute_write('UPDATE wisp_automation_policy SET definition=? WHERE id=1', (dump(dict(stored, restore_review_required=True)),))
    execute_write('UPDATE wisp_automation_tasks SET enabled=0,next_at=NULL,revision=revision+1')
    execute_write("UPDATE wisp_automation_runs SET state='interrupted',result=JSON_SET(result,'$.message','استعيد السجل؛ لم يستأنف التنفيذ أو الإرسال') WHERE state IN ('queued','running')")
    execute_write('DELETE FROM wisp_automation_notices')

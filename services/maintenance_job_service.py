"""Durable maintenance journals; workers are never replayed after interruption."""
import json
import threading
import uuid
from database.db import query_one, execute_write, execute_update, log_audit
from services.account_lifecycle_service import job_lock

_READY = False
_SCHEMA_LOCK = threading.Lock()

def ensure_job_table():
    global _READY
    with _SCHEMA_LOCK:
        if not _READY:
            row=query_one("SELECT COUNT(*) n FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='wisp_maintenance_jobs'")
            if not row or not row['n']:
                raise RuntimeError('سجل الصيانة غير مهيأ؛ يلزم تهيئة الإصدار قبل تنفيذ الصيانة.')
            _READY = True

def _public(row):
    if not row:return None
    return dict(job_id=row['job_id'],state=row['state'],parameters=json.loads(row['parameters']),result=json.loads(row['progress']),created_at=str(row['created_at']),updated_at=str(row['updated_at']))

def _recover_interrupted(kind='expired-card-delete'):
    # A missing worker must never trigger automatic deletion after restart/restore.
    execute_write("""UPDATE wisp_maintenance_jobs SET state='interrupted',active_key=NULL
        WHERE active_key=? AND state IN ('queued','running')
        AND updated_at<NOW()-INTERVAL 60 SECOND
        AND IS_USED_LOCK(CONCAT(DATABASE(), ':',?)) IS NULL""",(kind,kind))

def get_cleanup_job(job_id=None):
    return get_maintenance_job('expired-card-delete',job_id)


def get_maintenance_job(kind,job_id=None):
    if kind not in ('expired-card-delete','accounting-archive'):raise ValueError('نوع مهمة غير صالح')
    ensure_job_table()
    condition = "COALESCE(JSON_UNQUOTE(JSON_EXTRACT(parameters, '$.job_kind')), 'expired-card-delete')=?"
    if job_id:
        row=query_one('SELECT * FROM wisp_maintenance_jobs WHERE job_id=? AND '+condition,(job_id,kind))
    else:
        row=query_one('SELECT * FROM wisp_maintenance_jobs WHERE '+condition+' ORDER BY (active_key IS NOT NULL) DESC,created_at DESC,job_id DESC LIMIT 1',(kind,))
    # GET reports interrupted work without writing a journal or creating schema.
    # Explicit submission/recovery may persist the observation later.
    if row and row['state'] in ('queued','running'):
        stale=query_one("""SELECT updated_at<NOW()-INTERVAL 60 SECOND
            AND IS_USED_LOCK(CONCAT(DATABASE(), ':',?)) IS NULL AS stale
            FROM wisp_maintenance_jobs WHERE job_id=?""",(kind,row['job_id']))
        if stale and stale['stale']:
            row=dict(row,state='interrupted',active_key=None)
    return _public(row)

def _update(job_id, state, result, terminal=False, kind='expired-card-delete'):
    payload=json.dumps(result,ensure_ascii=False)
    execute_write("""UPDATE wisp_maintenance_jobs SET state=?,progress=?,updated_at=NOW(6),active_key=?
        WHERE job_id=? AND state IN ('queued','running')""",
        (state,payload,None if terminal else kind,job_id))

def _worker(job_id, parameters, created_by):
    result=dict(success=False,processed=0,total=0,deleted_vouchers=0,pending=0,skipped=0)
    try:
        changed=execute_update("UPDATE wisp_maintenance_jobs SET state='running',updated_at=NOW(6) WHERE job_id=? AND state='queued'",(job_id,))
        if changed != 1:return
        from services.db_maintenance_service import delete_expired_vouchers
        def progress(current):
            nonlocal result
            result=current
            _update(job_id,'running',current)
        def cancelled():
            row=query_one('SELECT parameters FROM wisp_maintenance_jobs WHERE job_id=?',(job_id,))
            return not row or bool(json.loads(row['parameters']).get('cancel_requested'))
        result=delete_expired_vouchers(**parameters,progress_callback=progress,cancel_callback=cancelled)
    except Exception as exc:
        result=dict(result,success=False,error=str(exc),message='لم تكتمل مهمة الحذف: '+str(exc))
    state='cancelled' if result.get('cancelled') else ('completed' if result.get('success') and result.get('deletion_complete') else ('partial' if result.get('processed') else 'failed'))
    try:
        _update(job_id,state,result,terminal=True)
        log_audit(1,created_by,'DB_CASCADE_CLEAN' if state=='completed' else 'DB_CASCADE_CLEAN_PARTIAL','tools',f"Job {job_id}: {result.get('message','')}")
    except Exception:
        # Progress already saved remains readable; stale work is marked interrupted later.
        import logging
        logging.getLogger(__name__).exception('Unable to persist cleanup outcome for %s',job_id)


def cancel_cleanup_job(job_id, requested_by='admin'):
    return cancel_maintenance_job(job_id,'expired-card-delete',requested_by)


def cancel_maintenance_job(job_id,kind,requested_by='admin'):
    import re
    if not re.fullmatch('[a-f0-9]{32}',str(job_id)):
        raise ValueError('معرف المهمة غير صالح.')
    row=get_maintenance_job(kind,job_id)
    if not row:return None
    if row['state'] in ('queued','running'):
        changed=execute_update("""UPDATE wisp_maintenance_jobs
            SET parameters=JSON_SET(parameters,'$.cancel_requested',1),updated_at=NOW(6)
            WHERE job_id=? AND active_key=? AND state IN ('queued','running')
            AND COALESCE(JSON_EXTRACT(parameters,'$.cancel_requested'),0)<>1""",(job_id,kind))
        if changed:
            log_audit(1,requested_by,'DB_CLEANUP_CANCEL_REQUEST' if kind=='expired-card-delete' else 'DB_ARCHIVE_CANCEL_REQUEST','tools','Cancellation requested for '+job_id)
    return get_maintenance_job(kind,job_id)

def start_cleanup_job(delete_type='all',created_by='admin'):
    if delete_type not in ('all','time','quota'):raise ValueError('نوع حذف غير صالح')
    ensure_job_table()
    with job_lock('cleanup-job-submit') as acquired:
        if not acquired:
            row=query_one("SELECT * FROM wisp_maintenance_jobs WHERE active_key='expired-card-delete'")
            if row:return _public(row),False
            raise ValueError('يجري تجهيز مهمة حذف؛ أعد المحاولة لاحقًا.')
        _recover_interrupted()
        row=query_one("SELECT * FROM wisp_maintenance_jobs WHERE active_key='expired-card-delete'")
        if row:return _public(row),False
        job_id=uuid.uuid4().hex
        parameters=dict(delete_type=delete_type,delete_acct=False)
        progress=dict(success=True,processed=0,total=0,deleted_vouchers=0,pending=0,skipped=0,message='تم قبول المهمة؛ يجري تحديد الكروت المؤهلة.')
        execute_write("""INSERT INTO wisp_maintenance_jobs (job_id,active_key,state,parameters,progress,created_by)
            VALUES (?,'expired-card-delete','queued',?,?,?)""",(job_id,json.dumps(parameters),json.dumps(progress,ensure_ascii=False),created_by))
        try:
            threading.Thread(target=_worker,args=(job_id,parameters,created_by),name='ExpiredCardCleanup',daemon=True).start()
        except Exception as exc:
            _update(job_id,'failed',dict(progress,success=False,message=str(exc)),terminal=True)
            raise
        return _public(query_one('SELECT * FROM wisp_maintenance_jobs WHERE job_id=?',(job_id,))),True


def _archive_worker(job_id,parameters,created_by):
    result=dict(success=False,processed=0,total=0,archived_sessions=0,summarized_sessions=0)
    try:
        if execute_update("UPDATE wisp_maintenance_jobs SET state='running',updated_at=NOW(6) WHERE job_id=? AND state='queued'",(job_id,))!=1:return
        from services.accounting_archiver_service import run_archive_maintenance
        import time
        last=[0.0]
        def progress(current):
            nonlocal result
            result=current
            now=time.monotonic()
            if now-last[0]>=.5:
                _update(job_id,'running',current,kind='accounting-archive');last[0]=now
        def cancelled():
            row=query_one('SELECT parameters FROM wisp_maintenance_jobs WHERE job_id=?',(job_id,))
            return not row or bool(json.loads(row['parameters']).get('cancel_requested'))
        options={k:parameters[k] for k in ('days','mode','chunk_size')}
        result=run_archive_maintenance(**options,progress_callback=progress,cancel_callback=cancelled)
    except Exception as exc:
        result=dict(result,success=False,message='لم تكتمل معالجة التاريخ: '+str(exc))
    state='cancelled' if result.get('cancelled') else 'completed' if result.get('success') and result.get('complete') else 'partial' if result.get('processed') else 'failed'
    try:
        _update(job_id,state,result,terminal=True,kind='accounting-archive')
        log_audit(1,created_by,'DB_ARCHIVE_SESSIONS' if state=='completed' else 'DB_ARCHIVE_PARTIAL','tools',f"Job {job_id}: {result.get('message','')}")
    except Exception:
        import logging
        logging.getLogger(__name__).exception('Unable to persist archive outcome')


def start_archive_job(days=90,mode='archive',chunk_size=500,created_by='admin'):
    from services.accounting_archiver_service import _validate_archive_options
    days=_validate_archive_options(days,mode);chunk_size=max(1,min(int(chunk_size),1000))
    ensure_job_table()
    with job_lock('archive-job-submit') as acquired:
        if not acquired:raise ValueError('يجري تجهيز مهمة أرشفة؛ أعد المحاولة لاحقًا.')
        _recover_interrupted('accounting-archive')
        row=query_one("SELECT * FROM wisp_maintenance_jobs WHERE active_key='accounting-archive'")
        if row:return _public(row),False
        job_id=uuid.uuid4().hex
        parameters=dict(job_kind='accounting-archive',days=days,mode=mode,chunk_size=chunk_size)
        progress=dict(success=True,processed=0,total=0,archived_sessions=0,summarized_sessions=0,message='تم قبول المهمة؛ يجري فحص التاريخ المؤهل.')
        execute_write("INSERT INTO wisp_maintenance_jobs(job_id,active_key,state,parameters,progress,created_by) VALUES (?,'accounting-archive','queued',?,?,?)",(job_id,json.dumps(parameters),json.dumps(progress,ensure_ascii=False),created_by))
        try:
            threading.Thread(target=_archive_worker,args=(job_id,parameters,created_by),name='ArchiveMaintenance',daemon=True).start()
        except Exception as exc:
            _update(job_id,'failed',dict(progress,success=False,message=str(exc)),terminal=True,kind='accounting-archive');raise
        return _public(query_one('SELECT * FROM wisp_maintenance_jobs WHERE job_id=?',(job_id,))),True

"""One background expired-card deletion with persistent, resumable UI progress."""
import json
import threading
import uuid
from database.db import query_one, execute_write, execute_update, log_audit
from database.maintenance_jobs import MAINTENANCE_JOBS_DDL
from services.account_lifecycle_service import job_lock

_READY = False
_SCHEMA_LOCK = threading.Lock()

def ensure_job_table():
    global _READY
    with _SCHEMA_LOCK:
        if not _READY:
            execute_write(MAINTENANCE_JOBS_DDL)
            _READY = True

def _public(row):
    if not row:return None
    return dict(job_id=row['job_id'],state=row['state'],parameters=json.loads(row['parameters']),result=json.loads(row['progress']),created_at=str(row['created_at']),updated_at=str(row['updated_at']))

def _recover_interrupted():
    # A missing worker must never trigger automatic deletion after restart/restore.
    execute_write("""UPDATE wisp_maintenance_jobs SET state='interrupted',active_key=NULL
        WHERE active_key='expired-card-delete' AND state IN ('queued','running')
        AND updated_at<NOW()-INTERVAL 60 SECOND
        AND IS_USED_LOCK(CONCAT(DATABASE(), ':expired-card-delete')) IS NULL""")

def get_cleanup_job(job_id=None):
    ensure_job_table()
    _recover_interrupted()
    condition = "COALESCE(JSON_UNQUOTE(JSON_EXTRACT(parameters, '$.job_kind')), 'expired-card-delete')='expired-card-delete'"
    if job_id:
        return _public(query_one('SELECT * FROM wisp_maintenance_jobs WHERE job_id=? AND '+condition,(job_id,)))
    return _public(query_one('SELECT * FROM wisp_maintenance_jobs WHERE '+condition+' ORDER BY (active_key IS NOT NULL) DESC,created_at DESC,job_id DESC LIMIT 1'))

def _update(job_id, state, result, terminal=False):
    payload=json.dumps(result,ensure_ascii=False)
    execute_write("""UPDATE wisp_maintenance_jobs SET state=?,progress=?,updated_at=NOW(6),active_key=?
        WHERE job_id=? AND state IN ('queued','running')""",
        (state,payload,None if terminal else 'expired-card-delete',job_id))

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
        result=delete_expired_vouchers(**parameters,progress_callback=progress)
    except Exception as exc:
        result=dict(result,success=False,error=str(exc),message='لم تكتمل مهمة الحذف: '+str(exc))
    state='completed' if result.get('success') else ('partial' if result.get('processed') else 'failed')
    try:
        _update(job_id,state,result,terminal=True)
        log_audit(1,created_by,'DB_CASCADE_CLEAN' if result.get('success') else 'DB_CASCADE_CLEAN_PARTIAL','tools',f"Job {job_id}: {result.get('message','')}")
    except Exception:
        # Progress already saved remains readable; stale work is marked interrupted later.
        import logging
        logging.getLogger(__name__).exception('Unable to persist cleanup outcome for %s',job_id)

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

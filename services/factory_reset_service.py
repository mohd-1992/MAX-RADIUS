"""Durable, fail-safe preparation and execution of a local factory reset."""
import json
import logging
import threading
import time
import uuid
from database.db import query_one, query_all, execute_write, execute_update, log_audit
from services.account_lifecycle_service import job_lock
from services.maintenance_job_service import ensure_job_table, _public

log = logging.getLogger(__name__)
_REQUESTS = threading.Condition()
_IN_FLIGHT = 0


def begin_operational_request():
    global _IN_FLIGHT
    with _REQUESTS:
        if factory_reset_active():return False
        _IN_FLIGHT += 1
        return True


def end_operational_request():
    global _IN_FLIGHT
    with _REQUESTS:
        _IN_FLIGHT -= 1
        _REQUESTS.notify_all()


def _drain_operational_requests(timeout=120):
    deadline=time.monotonic()+timeout
    with _REQUESTS:
        while _IN_FLIGHT:
            remaining=deadline-time.monotonic()
            if remaining<=0:raise RuntimeError('لم تكتمل العمليات السابقة؛ لم ينفذ المسح.')
            _REQUESTS.wait(min(remaining,1))



def factory_reset_active():
    # The table is created by the schema healer. Do not silently allow writes on DB failure.
    return bool(query_one("SELECT 1 n FROM wisp_maintenance_jobs WHERE active_key='factory-reset' LIMIT 1"))


def _save(job_id, result, state='running', terminal=False):
    execute_update("""UPDATE wisp_maintenance_jobs SET state=?,progress=?,updated_at=NOW(6),active_key=?
        WHERE job_id=?""", (state,json.dumps(result,ensure_ascii=False),None if terminal else 'factory-reset',job_id))


def _docker(method, path):
    from services.autoheal_service import docker_api_request
    data, error = docker_api_request(method,path,timeout=30)
    if error:raise RuntimeError(error)
    return data


def _owns_core(info):
    """Verify ownership even when the tenant Docker proxy redacts Env."""
    import os
    from core.config import DB_NAME
    config=info.get('Config',{})
    env=dict(v.split('=',1) for v in config.get('Env',[]) if '=' in v)
    labels=config.get('Labels',{})
    if 'DB_NAME' in env:
        if env['DB_NAME']!=DB_NAME:return False
        # Legacy local installs expose Env and may not use Compose labels.
        if not labels.get('com.docker.compose.project'):return True
    if labels.get('com.docker.compose.service') not in ('radius_core','core','freeradius'):return False
    own=_docker('GET','/containers/'+os.environ.get('HOSTNAME','unknown')+'/json')
    own_labels=own.get('Config',{}).get('Labels',{})
    project=own_labels.get('com.docker.compose.project')
    return bool(project and own_labels.get('com.docker.compose.service')=='web'
                and labels.get('com.docker.compose.project')==project)


def _inspect(container_id):
    info=_docker('GET',f'/containers/{container_id}/json')
    if not _owns_core(info):raise RuntimeError('حاوية الراديوس لا تخص هذه المنظومة.')
    return info


def _cores():
    matches=[]
    for item in _docker('GET','/containers/json?all=1'):
        if not any('core' in n or 'freeradius' in n for n in item.get('Names',[])):continue
        info=_docker('GET',f"/containers/{item['Id']}/json")
        if _owns_core(info):
            matches.append(dict(id=item['Id'],was_running=bool(info['State']['Running'])))
    if not matches:raise RuntimeError('تعذر تحديد حاوية الراديوس لهذه القاعدة؛ لم ينفذ المسح.')
    return matches


def _restore(cores):
    errors=[]
    for core in cores:
        if not core['was_running']:continue
        try:
            if not _inspect(core['id'])['State']['Running']:
                _docker('POST',f"/containers/{core['id']}/start")
            deadline=time.monotonic()+45
            while True:
                state=_inspect(core['id'])['State']
                if not state['Running']:raise RuntimeError('الراديوس لم يعد للعمل.')
                health=state.get('Health',{}).get('Status')
                if health in (None,'healthy'):break
                if time.monotonic()>=deadline:raise RuntimeError('الراديوس يعمل لكن فحص صحته لم ينجح.')
                time.sleep(1)
        except Exception as exc:errors.append(str(exc))
    return errors


def _close_sessions(progress, timeout=600):
    from services.coa_queue_service import enqueue_disconnect, _COA_TASK_QUEUE
    rows=query_all('SELECT radacctid,username,nasipaddress,acctsessionid,framedipaddress,callingstationid FROM radacct WHERE acctstoptime IS NULL')
    for row in rows:
        ok,msg=enqueue_disconnect(row['username'],nas_ip=row['nasipaddress'],session_id=row['acctsessionid'],
            framed_ip=row['framedipaddress'],mac_address=row['callingstationid'],radacctid=row['radacctid'],reason='Factory reset')
        if not ok and 'already' not in str(msg).lower():
            # Duplicate work may still finish; final SQL/queue checks are authoritative.
            log.warning('Factory reset disconnect enqueue: %s',msg)
    deadline=time.monotonic()+timeout
    while True:
        remaining=query_one('SELECT COUNT(*) n FROM radacct WHERE acctstoptime IS NULL')['n']
        progress(remaining,len(rows))
        if remaining==0 and _COA_TASK_QUEUE.unfinished_tasks==0:return
        if time.monotonic()>=deadline:
            raise RuntimeError(f'تعذر تأكيد فصل {remaining} جلسة أو إكمال مهام الفصل؛ لم ينفذ المسح.')
        time.sleep(1)


def _worker(job_id, parameters, created_by):
    result=dict(success=False,message='جارٍ التحضير',stage='preparing',percent=5,cores=[],data_reset=False)
    try:
        with job_lock('factory-reset') as acquired, job_lock('expired-card-delete') as idle, job_lock('expiry-sweep', timeout=60) as expiry_idle:
            if not acquired or not idle:raise RuntimeError('توجد عملية حذف أو إعادة مصنع قيد التنفيذ؛ أعد المحاولة بعد اكتمالها.')
            if not expiry_idle:raise RuntimeError('لم تتوقف دورة فحص الانتهاء خلال المهلة المحددة؛ لم يُنفذ المسح.')
            if execute_update("UPDATE wisp_maintenance_jobs SET state='running' WHERE job_id=? AND state='queued'",(job_id,))!=1:return
            try:
                _drain_operational_requests()
                cores=_cores()
                # Journal original state BEFORE any stop; crash recovery only restores these containers.
                result.update(cores=cores,stage='backup',percent=10,message='إنشاء نسخة احتياطية استرجاعية')
                _save(job_id,result)
                from services.backup_service import create_backup
                ok,msg,backup=create_backup(admin_username=created_by,notes='قبل إعادة ضبط المصنع',dispatch_notifications=False)
                if not ok:raise RuntimeError(msg)
                result.update(backup=backup,stage='stopping',percent=30,message='إيقاف استقبال المصادقات الجديدة مؤقتًا')
                _save(job_id,result)
                for core in cores:
                    if core['was_running']:_docker('POST',f"/containers/{core['id']}/stop?t=10")
                    if _inspect(core['id'])['State']['Running']:raise RuntimeError('تعذر إيقاف الراديوس؛ لم ينفذ المسح.')
                def progress(remaining,total):
                    result.update(stage='disconnecting',percent=45,message=f'فصل الجلسات والتحقق من إغلاقها: متبقي {remaining}',remaining_sessions=remaining,total_sessions=total)
                    _save(job_id,result)
                _close_sessions(progress)
                result.update(stage='wiping',percent=65,message='مسح البيانات التشغيلية في معاملة واحدة')
                _save(job_id,result)
                from services.db_maintenance_service import _wipe_factory_database
                result['tables_reset']=_wipe_factory_database(parameters['keep_packages'],parameters['keep_resellers'])
                result.update(data_reset=True,percent=75,message='تم المسح؛ جارٍ تحسين الجداول واستعادة المساحة',stage='reclaiming')
                _save(job_id,result)
                def reclaim_progress(done,total,table):
                    result.update(percent=75+int(15*done/max(total,1)),message=f'استعادة المساحة: جدول {table} ({done+1}/{total})')
                    _save(job_id,result)
                try:
                    from services.db_maintenance_service import _reclaim_factory_space
                    reclaimed=_reclaim_factory_space(parameters['keep_packages'],parameters['keep_resellers'],reclaim_progress)
                    result['space_reclamation']=reclaimed
                    if not reclaimed.get('success'):
                        result['warnings']=['نجح المسح لكن تحسين بعض الجداول واستعادة المساحة لم يكتمل؛ راجع نتائج التحسين.']
                except Exception as reclaim_error:
                    result['space_reclamation']=dict(success=False,message=str(reclaim_error))
                    result['warnings']=['نجح المسح لكن استعادة المساحة لم تكتمل: '+str(reclaim_error)]
                result.update(percent=95,message='جارٍ إعادة الراديوس إلى حالته السابقة',stage='restarting')
                _save(job_id,result)
                result['success']=True
            except Exception as exc:
                result.update(success=False,message=str(exc))
            finally:
                errors=_restore(result['cores'])
                result['restart_errors']=errors
                if errors:
                    result.update(success=False,message=result['message']+'؛ تعذر إعادة الراديوس: '+'؛ '.join(errors))
            if result['success']:
                result.update(percent=100,stage='completed',message='اكتملت إعادة المصنع وحُفظت الإعدادات والترخيص والنسخة الاحتياطية، وأُعيد الراديوس إلى حالته السابقة.')
            if result.get('warnings'):
                result['message']+=' '+ '؛ '.join(result['warnings'])
            _save(job_id,result,'completed' if result['success'] else 'failed',terminal=not result['restart_errors'])
            try:log_audit(1,created_by,'DB_FACTORY_RESET','tools',result['message'])
            except Exception:log.exception('Unable to write factory reset audit; persisted job outcome is unchanged')
    except Exception as exc:
        log.exception('Factory reset job failed')
        # Never clear a restoration journal if stopping/wiping may already have happened.
        result.update(success=False,message=str(exc))
        try:_save(job_id,result,'failed',terminal=not result.get('cores'))
        except Exception:log.exception('Unable to persist factory reset outcome')


def recover_factory_reset():
    """Restore service after an interrupted worker; never resume the destructive wipe."""
    ensure_job_table()
    with job_lock('factory-reset-submit') as submit, job_lock('factory-reset') as idle:
        if not submit or not idle:return
        row=query_one("SELECT * FROM wisp_maintenance_jobs WHERE active_key='factory-reset'")
        if not row:return
        if row['state']=='queued' and query_one('SELECT updated_at>=NOW()-INTERVAL 60 SECOND AS fresh FROM wisp_maintenance_jobs WHERE job_id=?',(row['job_id'],))['fresh']:
            return
        result=json.loads(row['progress'])
        errors=_restore(result.get('cores',[]))
        result.update(success=False,restart_errors=errors,stage='interrupted',message='انقطعت مهمة إعادة المصنع؛ لم يُستأنف المسح تلقائيًا. '+('تعذر إعادة الراديوس: '+'؛ '.join(errors) if errors else 'أُعيد الراديوس إلى حالته السابقة.'))
        _save(row['job_id'],result,'interrupted',terminal=not errors)


def sanitize_restored_jobs():
    """Imported worker journals belong to another runtime and cannot be resumed."""
    ensure_job_table()
    rows=query_all("SELECT job_id,progress FROM wisp_maintenance_jobs WHERE active_key IS NOT NULL")
    for row in rows:
        result=json.loads(row['progress'])
        result.update(success=False,cores=[],restart_errors=[],stage='interrupted',
            message='تمت استعادة سجل المهمة من نسخة احتياطية؛ لم تُستأنف العملية تلقائيًا.')
        _save(row['job_id'],result,'interrupted',terminal=True)


def get_factory_reset(job_id=None):
    ensure_job_table()
    condition="JSON_UNQUOTE(JSON_EXTRACT(parameters,'$.job_kind'))='factory-reset'"
    row=query_one('SELECT * FROM wisp_maintenance_jobs WHERE '+condition+(' AND job_id=?' if job_id else ' ORDER BY (active_key IS NOT NULL) DESC,created_at DESC LIMIT 1'),(job_id,) if job_id else ())
    return _public(row)


def start_factory_reset(keep_packages=True, keep_resellers=False, created_by='admin'):
    if type(keep_packages) is not bool or type(keep_resellers) is not bool:raise ValueError('خيارات إعادة المصنع غير صالحة.')
    ensure_job_table()
    with job_lock('factory-reset-submit') as submit:
        if not submit:raise ValueError('يجري تجهيز إعادة المصنع؛ أعد المحاولة.')
        row=query_one("SELECT * FROM wisp_maintenance_jobs WHERE active_key='factory-reset'")
        if row:return _public(row),False
        job_id=uuid.uuid4().hex
        parameters=dict(job_kind='factory-reset',keep_packages=keep_packages,keep_resellers=keep_resellers)
        result=dict(success=False,stage='queued',percent=0,message='تم قبول إعادة المصنع؛ جارٍ التحضير في الخلفية.',cores=[],data_reset=False)
        execute_write("INSERT INTO wisp_maintenance_jobs(job_id,active_key,state,parameters,progress,created_by) VALUES (?,'factory-reset','queued',?,?,?)",(job_id,json.dumps(parameters),json.dumps(result,ensure_ascii=False),created_by))
        try:threading.Thread(target=_worker,args=(job_id,parameters,created_by),name='FactoryReset',daemon=True).start()
        except Exception:
            _save(job_id,dict(result,message='تعذر تشغيل المهمة'),'failed',terminal=True)
            raise
        return get_factory_reset(job_id),True

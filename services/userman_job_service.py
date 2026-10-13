"""Private durable User Manager progress. Never replay an interrupted SQL batch."""
import copy
import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from contextlib import contextmanager

class JobCancelled(Exception): pass
class JobNotFound(Exception): pass

def process_start(pid):
    try: return Path('/proc/'+str(pid)+'/stat').read_text().rsplit(')',1)[1].split()[19]
    except (OSError,IndexError): return None

@contextmanager
def import_gate():
    from services.account_lifecycle_service import job_lock
    from services.factory_reset_service import factory_reset_active
    with job_lock('userman-import') as acquired:
        if not acquired or factory_reset_active():
            raise ValueError('توجد إعادة مصنع أو عملية استيراد قيد التنفيذ؛ لم يبدأ الاستيراد.')
        yield

def root():
    from core.config import STORAGE_DIR
    folder=Path(STORAGE_DIR)/'userman-jobs';folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    return folder

def write(path,value):
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as stream: json.dump(value,stream,ensure_ascii=False)
    for attempt in range(10):
        try:
            os.replace(temporary,path)
            break
        except PermissionError:
            if os.name!='nt' or attempt==9: raise
            time.sleep(.01)

def folder(job_id,owner):
    if not re.fullmatch('[a-f0-9]{32}',str(job_id)): raise JobNotFound()
    path=root()/job_id
    try: state=json.loads((path/'state.json').read_text(encoding='utf-8'))
    except (FileNotFoundError,ValueError): raise JobNotFound()
    if state['owner']!=int(owner): raise JobNotFound()
    return path,state

def public(path,state):
    out={k:v for k,v in state.items() if k not in ('owner','pid','process_start')}
    out['cancel_requested']=(path/'cancel').exists()
    return out

def status(job_id,owner):
    path,state=folder(job_id,owner)
    if state['state']=='running' and os.name!='nt':
        try:
            os.kill(state['pid'],0)
            if state.get('process_start')!=process_start(state['pid']): raise ProcessLookupError()
        except (OSError,ProcessLookupError):
            state.update(state='interrupted',message='توقفت المهمة؛ لا تعِد الدفعة قبل مراجعة أثرها.',updated_at=time.time())
            write(path/'state.json',state)
    return public(path,state)

def cancel(job_id,owner):
    path,state=folder(job_id,owner)
    if state['state']=='running': (path/'cancel').touch(mode=0o600,exist_ok=True)
    return status(job_id,owner)

def result(job_id,owner):
    path,state=folder(job_id,owner)
    if state['state']!='completed': raise ValueError('لم تكتمل المهمة؛ النتيجة غير جاهزة.')
    value=json.loads((path/'result.json').read_text(encoding='utf-8'))
    if state['kind']=='fetch':
        value['users']=value['users'][:100]
        value['source_job_id']=job_id
    return value

@contextmanager
def operation_lock():
    stream=(root()/'.operation.lock').open('a+b')
    try:
        if os.name=='nt':
            import msvcrt
            stream.write(b'0');stream.flush();stream.seek(0)
            try: msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            except OSError: raise ValueError('توجد مهمة سحب أو استيراد قيد التنفيذ؛ انتظر اكتمالها أو ألغها.')
        else:
            import fcntl
            try: fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: raise ValueError('توجد مهمة سحب أو استيراد قيد التنفيذ؛ انتظر اكتمالها أو ألغها.')
        yield
    finally: stream.close()

def launch(kind,owner,worker,binding=None,initial_options=None):
    # The worker owns the lock until its final journal update.
    lock=operation_lock();lock.__enter__()
    try:
        if binding and binding.exists():
            old=json.loads(binding.read_text());lock.__exit__(None,None,None)
            return status(old['job_id'],owner)
        job_id=uuid.uuid4().hex;path=root()/job_id;path.mkdir(mode=0o700)
        state=dict(id=job_id,owner=int(owner),pid=os.getpid(),process_start=process_start(os.getpid()),kind=kind,state='running',stage='connecting' if kind=='fetch' else 'preparing',percent=0,processed=0,total=None,message='',stats={},created_at=time.time(),updated_at=time.time())
        write(path/'state.json',state)
        if initial_options is not None: write(path/'options.json',initial_options)
        if binding: write(binding,{'job_id':job_id})
        def run():
            last=[0.0]
            def check_cancel():
                if (path/'cancel').exists(): raise JobCancelled()
            def update(force=False,**values):
                state.update(values)
                if force or time.monotonic()-last[0]>.25:
                    state['updated_at']=time.time();write(path/'state.json',state);last[0]=time.monotonic()
            try:
                value=worker(path,update,check_cancel)
                # A completed import is truthful even if a late cancel arrives after commit.
                write(path/'result.json',value)
                update(force=True,state='completed',stage='completed',percent=100)
            except JobCancelled:
                update(force=True,state='cancelled',stage='cancelled',message='أُلغيت المهمة. الدفعات المكتملة قبل الإلغاء محفوظة؛ لم تُعد أو تُحذف.')
            except Exception:
                update(force=True,state='failed',stage='failed',message='تعذرت متابعة المهمة؛ راجع إعدادات الاتصال وسجل الخادم. لا تُعد الدفعة تلقائيًا.')
                import logging
                logging.getLogger(__name__).exception('User Manager %s job failed',kind)
            finally: lock.__exit__(None,None,None)
        threading.Thread(target=run,daemon=True,name='Userman-'+job_id[:8]).start()
        return public(path,state)
    except Exception:
        lock.__exit__(None,None,None);raise

def start_fetch(owner,credentials):
    host=str(credentials.get('host','')).strip();username=str(credentials.get('username','')).strip()
    port=int(credentials.get('port') or 8728)
    if not host or not username or not 1<=port<=65535: raise ValueError('تحقق من عنوان الراوتر واسم المستخدم والمنفذ.')
    def worker(path,update,check):
        from services.mikrotik_userman_importer import fetch_userman_via_api
        value=fetch_userman_via_api(host,username,str(credentials.get('password','')),port=port,
            use_ssl=bool(credentials.get('use_ssl')),timeout=120,progress=update,check_cancel=check)
        check();write(path/'source.json',value)
        update(force=True,processed=value['total_users'],total=value['total_users'])
        return value
    return launch('fetch',owner,worker)

def start_import(owner,request,admin_user):
    source_id=request.get('source_job_id')
    if source_id:
        source,state=folder(source_id,owner)
        if state['kind']!='fetch' or state['state']!='completed': raise ValueError('لم يكتمل سحب المصدر.')
        data=json.loads((source/'source.json').read_text(encoding='utf-8'))
        binding=source/'import-job.json'
    else:
        data=request.get('data') or {}
        key=request.get('request_key','')
        if not re.fullmatch('[a-f0-9]{32}',key): raise ValueError('مرجع الاستيراد غير صالح.')
        binding=root()/('import-'+str(int(owner))+'-'+key+'.json')
    if not isinstance(data.get('users'),list) or not data['users']: raise ValueError('لا توجد كروت للاستيراد.')
    options={key:request.get(key,default) for key,default in [('target_type','vouchers'),('fallback_package',None),('duplicate_action','skip'),('ignore_expired',True),('import_consumption',False),('profile_costs',{})]}
    if options['target_type'] not in ('vouchers','subscribers') or options['duplicate_action'] not in ('skip','overwrite'): raise ValueError('خيارات الاستيراد غير صالحة.')
    if any(not isinstance(options[k],bool) for k in ('ignore_expired','import_consumption')): raise ValueError('خيارات الاستيراد غير صالحة.')
    fingerprint=hashlib.sha256(json.dumps({'source':source_id or data,'options':options},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    if binding.exists():
        old=json.loads(binding.read_text());old_path,_=folder(old['job_id'],owner)
        saved=json.loads((old_path/'options.json').read_text())
        if saved.get('fingerprint')!=fingerprint: raise ValueError('المصدر مرتبط بمحاولة سابقة بخيارات مختلفة؛ راجع نتيجتها أولًا.')
        return status(old['job_id'],owner)
    def worker(path,update,check):
        from services.mikrotik_userman_importer import execute_userman_import
        total=len(data['users']);size=200;count=(total+size-1)//size;cache=None;processed=0
        update(force=True,total=total,stage='importing')
        for index in range(count):
            check();chunk=data['users'][index*size:(index+1)*size]
            # The marker remains if the process dies during/after commit: never auto-replay.
            write(path/'batch-in-flight.json',{'batch':index,'processed_before':processed})
            value=execute_userman_import({'profiles':data.get('profiles',[]),'users':chunk},admin_user=admin_user,
                chunk_index=index,total_chunks=count,state_cache=copy.deepcopy(cache),check_cancel=check,**options)
            if not value.get('success'):
                check();raise RuntimeError('User Manager batch failed; no automatic replay')
            cache=value['state_cache'];processed+=len(chunk)
            update(force=True,processed=processed,percent=int(100*processed/total),stats=value.get('stats',{}),batch=index+1,total_batches=count)
            (path/'batch-in-flight.json').unlink()
        value.pop('state_cache',None)
        value['target_type']=options['target_type']
        return value
    def guarded_worker(path,update,check):
        with import_gate(): return worker(path,update,check)
    return launch('import',owner,guarded_worker,binding,{'source_job_id':source_id,'options':options,'fingerprint':fingerprint})

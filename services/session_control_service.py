"""Durable tenant-local session operations, independent of financial settlement.

All replicas must share STORAGE_DIR, as for export admission. Journals are not
uploads or backups. An interrupted attempt is verified without resending it.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

IDENTITY = ('radacctid','acctuniqueid','username','nasipaddress','acctsessionid',
            'framedipaddress','callingstationid','acctstarttime')
_thread = None
_start_lock = threading.Lock()


class ControlBusy(RuntimeError):
    pass


def root():
    base = Path(os.environ.get('STORAGE_DIR') or Path(__file__).resolve().parents[1]/'storage')
    folder = base/'session-operations'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return folder


def write(path, value):
    temp = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    with temp.open('x', encoding='utf8') as handle:
        os.chmod(temp, 0o600)
        json.dump(value, handle, ensure_ascii=False, default=str)
        handle.flush(); os.fsync(handle.fileno())
    os.replace(temp, path)
    if os.name != 'nt':
        fd = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)


@contextlib.contextmanager
def locked(key):
    # A fixed number of lock files; receipt retention must not grow lock inodes.
    path = root()/('maintenance.lock' if key=='maintenance' else 'lock-'+hashlib.sha256(key.encode()).hexdigest()[:2])
    with path.open('a+b') as handle:
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0); handle.write(b'0'); handle.flush(); handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ControlBusy('توجد عملية متعارضة قيد التنفيذ؛ لم يكتمل هذا الطلب بعد.') from None
        try: yield
        finally:
            if os.name == 'nt':
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def epoch():
    path = root()/'generation'
    try:
        with path.open('x') as handle: handle.write(uuid.uuid4().hex)
    except FileExistsError: pass
    return path.read_text()


def invalidate():
    """Restore/reset must never replay control operations for restored IDs."""
    write(root()/'generation', uuid.uuid4().hex)


@contextlib.contextmanager
def maintenance():
    try:
        with locked('maintenance'):
            invalidate()
            try: yield
            finally: invalidate()
    except ControlBusy as exc:
        from services.heavy_operation_service import OperationBusy
        raise OperationBusy(str(exc)) from None


def policy_key(policy):
    return hashlib.sha256(json.dumps([bool(policy['enabled']), sorted(
        a['mac'] for a in policy['addresses'])], sort_keys=True).encode()).hexdigest()


def snapshot(username):
    from database.db import query_all
    rows = query_all('SELECT * FROM radacct_owned WHERE username=%s AND acctstoptime IS NULL ORDER BY radacctid LIMIT 1001', (username,))
    if len(rows) > 1000:
        raise ValueError('عدد الجلسات يتجاوز ميزانية العملية؛ لم يُرسل فصل جزئي صامت.')
    return [{k: str(row[k]) if row.get(k) is not None else None for k in IDENTITY} for row in rows]


def prepare(kind, ident, username, actor, request_id=None, policy=None):
    from database.db import query_one
    if kind not in ('voucher','subscriber'): raise ValueError('نوع الحساب غير صالح.')
    if request_id and not re.fullmatch('[a-f0-9]{32}',request_id): raise ValueError('معرف العملية غير صالح.')
    targets = snapshot(username)
    key = hashlib.sha256(json.dumps([kind,int(ident),username,actor,request_id or targets,epoch()],sort_keys=True).encode()).hexdigest()[:32]
    with locked(key):
        existing = find(key)
        if existing: return public(existing)
        import itertools
        if sum(1 for _ in itertools.islice(root().glob('pending-*.json'),200))>=200:
            raise ValueError('طابور العمليات ممتلئ؛ تعذر تأكيد الجدولة.')
        schema=query_one('SELECT DATABASE() AS tenant_schema')['tenant_schema']
        if not schema: raise RuntimeError('Tenant schema unavailable')
        job = dict(id=key, kind=kind, account_id=int(ident), username=username, schema=schema,
                   db_host=os.environ.get('DB_HOST',''),
                   actor=actor, generation=epoch(), created=time.time(), deadline=time.time()+180,
                   state='queued', targets=targets, outcomes={}, attempted=[],
                   policy=policy, request_id=request_id)
        write(root()/('pending-'+key+'.json'), job)
    return public(job)


def find(key):
    if not re.fullmatch('[a-f0-9]{32}',str(key)): raise ValueError('معرف العملية غير صالح.')
    for prefix in ('done-','pending-'):
        try: return json.loads((root()/(prefix+key+'.json')).read_text(encoding='utf8'))
        except FileNotFoundError: pass
    return None


def public(job):
    rows = list(job['outcomes'].values())
    confirmed = sum(bool(r.get('success')) for r in rows if r.get('status')!='allowed')
    preserved = sum(r.get('status')=='allowed' for r in rows)
    pending = job['state'] in ('queued','running')
    message = 'تم حفظ الطلب؛ جارٍ التحقق من الجلسات المستهدفة.' if pending else {
        'completed':'تأكد اختفاء الجلسات المستهدفة من الراوتر؛ المحاسبة مستقلة وتنتظر سجلاتها الفعلية.',
        'no_targets':'لا توجد جلسات محاسبة مفتوحة مستهدفة؛ لم يُرسل طلب فصل.',
        'allowed_only':'الجلسات المحفوظة مطابقة لقائمة الماكات؛ لم يُطلب فصلها.',
        'partial':'اكتمل الفصل جزئيًا؛ توجد جلسات لم يتأكد فصلها.',
        'failed':'لم يتأكد فصل الجلسات المستهدفة؛ راجع النتيجة قبل إعادة الطلب.',
        'cancelled':'ألغيت العملية بعد تغير الحساب أو السياسة أو استعادة البيانات.',
    }.get(job['state'],'تعذر إكمال التحقق.')
    targeted=[r for r in rows if r.get('status')!='allowed']
    return dict(job_id=job['id'], state=job['state'], pending=pending, message=message,
                target_sessions=len(job['targets']), confirmed=confirmed, preserved=preserved,
                unknown=max(0,len(job['targets'])-confirmed-preserved),
                accounting_final=bool(targeted) and all(r.get('accounting_final') for r in targeted))


def process(key):
    from services.account_lifecycle_service import job_lock
    with locked('maintenance'), job_lock('factory-reset') as idle:
        if idle: return _process(key)


def _process(key):
    from database.db import query_one, is_import_maintenance_active
    from services.coa_queue_service import _disconnect_account_guard, _disconnect_single_session
    if is_import_maintenance_active(): return
    with locked(key):
        path = root()/('pending-'+key+'.json')
        if not path.exists(): return
        job = json.loads(path.read_text(encoding='utf8'))
        if (job['generation'] != epoch() or job['schema']!=query_one('SELECT DATABASE() AS tenant_schema')['tenant_schema']
                or job['db_host']!=os.environ.get('DB_HOST','')): job['state']='cancelled'
        elif time.time()>job['deadline']: job['state']='partial' if any(r.get('success') for r in job['outcomes'].values()) else 'failed'
        else:
            with _disconnect_account_guard(job['username']):
                table = 'wisp_vouchers' if job['kind']=='voucher' else 'wisp_subscribers'
                account=query_one(f'SELECT username FROM {table} WHERE id=%s',(job['account_id'],))
                if not account or account['username']!=job['username']:
                    job['state']='cancelled'
                else:
                    allowed = None
                    if job['policy'] is not None:
                        # File is staged before commit. No router action without its SQL receipt.
                        receipt=query_one('SELECT operation_id FROM wisp_account_mac_receipts WHERE operation_id=%s',(job['request_id'],))
                        if not receipt: return
                        from services.account_mac_service import get_mac_policy
                        policy=get_mac_policy(job['kind'],job['account_id'])
                        if policy_key(policy)!=job['policy']:
                            job['state']='cancelled'
                        else: allowed={a['mac'] for a in policy['addresses']} if policy['enabled'] else set()
                    if job['state']!='cancelled':
                        job['state']='running';write(path,job)
                        for target in job['targets']:
                            rid=target['radacctid']
                            if rid in job['outcomes']: continue
                            if time.time()>job['deadline']: break
                            row=query_one('SELECT * FROM radacct_owned WHERE radacctid=%s AND username=%s',(rid,job['username']))
                            same=bool(row) and all((str(row[k]) if row.get(k) is not None else None)==target[k] for k in IDENTITY)
                            result={'success':False,'status':'generation_changed'}
                            if same and row.get('acctterminatecause')=='Backup-Restored-Closed':
                                result={'success':False,'status':'restored_session'}
                            elif same:
                                if allowed is not None:
                                    from core.mac_policy import normalize_mac
                                    try: mac=normalize_mac(row.get('callingstationid'))
                                    except ValueError: mac=None
                                    if not mac: result={'success':False,'status':'mac_unknown'}
                                    elif mac in allowed: result={'success':False,'status':'allowed'}
                                    else: result=None
                                else: result=None
                                if result is None:
                                    already=rid in job['attempted'] or bool(row.get('acctstoptime'))
                                    if not already:
                                        job['attempted'].append(rid);write(path,job)
                                    result=_disconnect_single_session(job['username'],row['nasipaddress'],row['acctsessionid'],
                                        row.get('framedipaddress'),row.get('callingstationid'),int(rid),lifecycle_stop=True,
                                        expected_start=row.get('acctstarttime'),verify_only=already)
                            job['outcomes'][rid]=result;write(path,job)
                        rows=list(job['outcomes'].values())
                        good=sum(bool(r.get('success')) or r.get('status')=='allowed' for r in rows)
                        job['state']='no_targets' if not job['targets'] else 'allowed_only' if len(rows)==len(job['targets']) and all(r.get('status')=='allowed' for r in rows) else 'completed' if good==len(job['targets']) else 'partial' if good else 'failed'
        write(root()/('done-'+key+'.json'),job)
        path.unlink(missing_ok=True)


def start_worker():
    global _thread
    with _start_lock:
        if _thread and _thread.is_alive(): return
        def run():
            wait=threading.Event()
            while not wait.wait(2):
                try:
                    for path in list(root().glob('pending-*.json'))[:100]:
                        try: process(path.stem[8:])
                        except (OSError,RuntimeError): continue
                    # Retain bounded receipts for seven days; never remove an active journal.
                    for path in root().glob('done-*.json'):
                        if time.time()-path.stat().st_mtime>7*86400:
                            with locked(path.stem[5:]): path.unlink(missing_ok=True)
                except Exception:
                    import logging
                    logging.getLogger(__name__).warning('Session operation deferred; no accounting closure invented.')
        _thread=threading.Thread(target=run,name='session-operations',daemon=True);_thread.start()


def submit_manual(kind, ident, username, actor='admin', request_id=None):
    from services.coa_queue_service import _disconnect_account_guard
    with _disconnect_account_guard(username):
        result=prepare(kind,ident,username,actor,request_id)
    start_worker()
    return result

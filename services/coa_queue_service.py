# -*- coding: utf-8 -*-
"""
services/coa_queue_service.py
-----------------------------
Asynchronous CoA / PoD Disconnect & Speed Change Queue Engine (Core Architecture Pillar 3).

Provides:
- Non-blocking, thread-safe background task queue for Disconnect-Request (RFC 5176) & CoA.
- Immediate UI return (< 1ms) so web endpoints never hang on unreachable NAS routers.
- Automatic NAS discovery from live radacct sessions or wisp_nas_devices / nas tables.
- Dual-tier failover: RFC 5176 UDP CoA -> MikroTik RouterOS API -> Database session cleanup.
- Circular audit ring-buffer recording the last 100 CoA executions with latency telemetry.
"""

import time
import queue
import threading
import datetime
from collections import deque

from core.coa import RadiusCoaClient
from database.db import query_one, execute_write, log_audit

_COA_TASK_QUEUE = queue.Queue(maxsize=10000)
_RECENT_COA_LOG = deque(maxlen=100)
_LOG_LOCK = threading.Lock()

_worker_thread = None
_WORKER_START_LOCK = threading.Lock()
_PENDING_LOCK = threading.Lock()
_PENDING_KEYS = set()
_WORKERS = []
_USER_LOCKS = [threading.Lock() for _ in range(64)]
_stop_event = threading.Event()
_stats = {
    'total_enqueued': 0,
    'total_processed': 0,
    'total_success': 0,
    'total_failed': 0
}

def _resolve_nas_credentials(nas_ip=None):
    """Retrieves secret, coa_port, and API credentials for target NAS."""
    secret = 'max123'
    coa_port = 3799
    api_port = None
    api_user = None
    api_pass = None

    if nas_ip:
        nas_rec = query_one(
            "SELECT secret, coa_port, api_port, api_username, api_password FROM wisp_nas_devices WHERE ip_address = %s LIMIT 1",
            (nas_ip,)
        )
        if not nas_rec:
            nas_rec = query_one("SELECT secret, ports FROM nas WHERE nasname = %s LIMIT 1", (nas_ip,))

        if nas_rec:
            secret = nas_rec.get('secret') or secret
            coa_port = int(nas_rec.get('coa_port') or coa_port)
            api_port = nas_rec.get('api_port')
            api_user = nas_rec.get('api_username')
            api_pass = nas_rec.get('api_password') or ''
    else:
        # Fallback to default NAS
        nas_rec = query_one("SELECT ip_address, secret, coa_port FROM wisp_nas_devices ORDER BY id ASC LIMIT 1")
        if nas_rec:
            nas_ip = nas_rec.get('ip_address')
            secret = nas_rec.get('secret') or secret
            coa_port = int(nas_rec.get('coa_port') or coa_port)

    return nas_ip, secret, coa_port, api_port, api_user, api_pass

def _ppp_session_id(value):
    """Router API uses 0x/uppercase; RADIUS retains its own wire format."""
    import re
    value = str(value or '').strip().lower()
    if value.startswith('0x'):
        value = value[2:]
    return value if re.fullmatch(r'[0-9a-f]+', value) else None


def _mac(value):
    return str(value or '').replace('-', ':').upper()


def _nas_inventory(ros, username):
    inventory = []
    for path, key in (('/ip/hotspot/active', 'user'), ('/ppp/active', 'name')):
        replies = ros.talk_raw([path + '/print'])
        kinds = [kind for kind, _ in replies]
        if '!done' not in kinds or any(k in kinds for k in ('!trap', '!fatal')):
            raise RuntimeError('NAS inventory unavailable')
        for kind, row in replies:
            if kind == '!re' and str(row.get(key, '')).casefold() == username.casefold():
                if not row.get('.id'):
                    raise RuntimeError('NAS inventory lacks identity')
                inventory.append((path, row))
    return inventory


def _select_nas_session(inventory, session_id, framed_ip, mac_address):
    matches = []
    for path, row in inventory:
        if framed_ip and str(row.get('address', '')) != str(framed_ip):
            continue
        if mac_address and _mac(row.get('mac-address') or row.get('caller-id')) != _mac(mac_address):
            continue
        if path == '/ppp/active' and session_id:
            wanted, actual = _ppp_session_id(session_id), _ppp_session_id(row.get('session-id'))
            if wanted is None or actual is None:
                raise RuntimeError('Unverifiable PPP session identity')
            if wanted != actual:
                # Same IP/MAC with a different generation is not proof of absence.
                if framed_ip or mac_address:
                    raise RuntimeError('PPP identity conflicts with accounting')
                continue
        elif path == '/ip/hotspot/active':
            actual = row.get('acct-session-id') or row.get('session-id')
            if actual and session_id and str(actual) != str(session_id):
                raise RuntimeError('Hotspot identity conflicts with accounting')
            if not framed_ip or not mac_address:
                raise RuntimeError('Hotspot requires IP and MAC identity')
        elif not framed_ip or not mac_address:
            raise RuntimeError('NAS session identity is incomplete')
        matches.append((path, row))
    if len(matches) > 1:
        raise RuntimeError('NAS identity is ambiguous')
    return matches


def _counter_verified_disconnect(username, radacctid, nas_ip, session_id, framed_ip, mac_address):
    """Serialize with renewal, block fresh logins, then seal only the target."""
    import hashlib,uuid
    from database.db import get_connection
    from services.online_policy_service import checkpoint
    from services.nas_session_settlement import freeze_and_disconnect
    conn=get_connection();token=uuid.uuid4().hex
    lock='max-cycle:'+hashlib.sha256(username.casefold().encode()).hexdigest()[:48]
    locked=False;guarded=False
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT GET_LOCK(%s,0) AS acquired',(lock,))
            locked=cur.fetchone()['acquired']==1
            if not locked:raise RuntimeError('Account operation already running')
            cur.execute('UPDATE wisp_license_runtime_state SET id=id WHERE id=1')
            cur.execute('INSERT INTO wisp_renewal_guards(username,token,expires_at) VALUES(%s,%s,NOW()+INTERVAL 5 MINUTE) ON DUPLICATE KEY UPDATE token=VALUES(token),expires_at=VALUES(expires_at)',(username,token))
        conn.commit();guarded=True
        if query_one('SELECT 1 AS pending FROM wisp_session_reservations WHERE username=%s AND radacctid IS NULL AND expires_at>NOW() LIMIT 1',(username,)):
            raise RuntimeError('Accounting-Start still pending')
        snapshot=checkpoint(username)
        target={rid:row for rid,row in snapshot.items() if (radacctid is None or int(rid)==int(radacctid))
            and row['row']['nasipaddress']==nas_ip
            and (not framed_ip or row['row']['framedipaddress']==framed_ip)
            and (not mac_address or _mac(row['row']['callingstationid'])==_mac(mac_address))
            and (not session_id or (row['kind']=='pppoe' and _ppp_session_id(row['row']['acctsessionid'])==_ppp_session_id(session_id)) or (row['kind']=='hotspot' and row['row']['acctsessionid']==session_id))}
        if len(target)!=1:raise RuntimeError('No unique accounting target for final counters')
        freeze_and_disconnect(target,include_ppp=True)
        if target:raise RuntimeError('Target settlement incomplete')
    finally:
        try:
            with conn.cursor() as cur:
                if guarded:cur.execute('DELETE FROM wisp_renewal_guards WHERE username=%s AND token=%s',(username,token))
                if locked:cur.execute('SELECT RELEASE_LOCK(%s)',(lock,))
            conn.commit()
        finally:conn.close()


def _disconnect_single_session(username, nas_ip, session_id, framed_ip, mac_address, radacctid=None, require_accounting_stop=False):
    """ACK requests removal; verified NAS absence confirms it. Never invent Stop."""
    resolved_ip, secret, coa_port, api_port, api_user, api_pass = _resolve_nas_credentials(nas_ip)
    target_nas_ip = resolved_ip or nas_ip or '127.0.0.1'
    res = {'success': False, 'status': 'disconnect_unverified',
           'message': 'لم يتم تأكيد اختفاء الجلسة من الراوتر.'}
    ros = None
    try:
        if not api_port or not api_user:
            raise RuntimeError('NAS verification unavailable')
        from core.mikrotik_api import RouterOSApiProtocol
        ros = RouterOSApiProtocol(target_nas_ip, port=int(api_port), timeout=2.5)
        ros.connect()
        if not ros.login(api_user, api_pass):
            raise RuntimeError('NAS verification login failed')
        before = _nas_inventory(ros, username)
        matches = _select_nas_session(before, session_id, framed_ip, mac_address)
        if not matches:
            # Inventory with other account sessions cannot prove a missing target
            # when its identity was not supplied or understood.
            if before and not (session_id and framed_ip and mac_address):
                raise RuntimeError('Target absence cannot be verified')
            res.update(success=True, status='nas_confirmed_absent')
        else:
            path, original = matches[0]
            _counter_verified_disconnect(username, radacctid, target_nas_ip, session_id, framed_ip, mac_address)
            for attempt in range(3):
                after = _nas_inventory(ros, username)
                remaining = _select_nas_session(after, session_id, framed_ip, mac_address)
                if not remaining:
                    res.update(success=True, status='nas_confirmed_disconnected')
                    break
                time.sleep(.1)
            if not res['success']:
                raise RuntimeError('NAS still has the target session')
        res['message'] = 'تأكد اختفاء الجلسة المطلوبة من الراوتر.'
        # Final usage belongs to Accounting-Stop (or a separate verified-counter
        # settlement). ACK/API done never closes radacct or fabricates counters.
        stopped = query_one('SELECT acctstoptime,acctterminatecause FROM radacct WHERE radacctid=%s AND username=%s',
                            (radacctid, username)) if radacctid else None
        synthetic = ('Admin-Reset-CoA','Stale-Session-Timeout','Watchdog-Autoheal-Timeout','Backup-Restored-Closed')
        final = bool(stopped and stopped.get('acctstoptime') and stopped.get('acctterminatecause') not in synthetic)
        res['accounting_final'] = final
        if not final:
            res['message'] += ' المحاسبة النهائية قيد الاستلام؛ لم يُغلق سجلها افتراضياً.'
    except Exception as exc:
        res.update(success=False, status='disconnect_unverified', error=type(exc).__name__,
                   message='تعذر تأكيد فصل الجلسة؛ لم يُغلق سجل المحاسبة افتراضياً.')
    finally:
        if ros is not None:
            ros.close()
    return res


def _process_coa_task(task):
    """Executes a single CoA or Disconnect action with failover and audit logging."""
    from services.account_lifecycle_service import lifecycle_disconnect_still_required
    if not lifecycle_disconnect_still_required(task):
        return {'success': True, 'status': 'cancelled_after_state_change', 'message': 'ألغيت المهمة بعد تغير حالة الحساب أو دورته'}
    action = task.get('action', 'disconnect')
    username = task.get('username')
    admin_user = task.get('admin_username', 'system')
    nas_ip = task.get('nas_ip')
    framed_ip = task.get('framed_ip')
    session_id = task.get('session_id')
    mac_address = task.get('mac_address')
    radacctid = task.get('radacctid')
    rate_limit = task.get('rate_limit')
    reason = task.get('reason', 'Scheduled Lifecycle / Expiry Disconnect')

    t_start = time.time()
    res = {'success': False, 'status': 'unknown', 'message': '', 'latency_ms': 0.0}

    try:
        if action == 'disconnect':
            if radacctid:
                sess = query_one("SELECT nasipaddress, acctsessionid, framedipaddress, callingstationid FROM radacct WHERE radacctid = %s AND username = %s AND (acctstoptime IS NULL OR acctterminatecause IN ('Stale-Session-Timeout','Watchdog-Autoheal-Timeout','Backup-Restored-Closed'))", (radacctid, username))
                if not sess:
                    raise ValueError('الجلسة المطلوبة غير موجودة أو مغلقة')
                nas_ip, session_id = sess['nasipaddress'], sess['acctsessionid']
                framed_ip, mac_address = sess.get('framedipaddress'), sess.get('callingstationid')
            if radacctid or session_id:
                # Disconnecting a specific identified session
                res = _disconnect_single_session(username, nas_ip, session_id, framed_ip, mac_address, radacctid,
                    require_accounting_stop=bool(task.get('require_accounting_stop')))
            else:
                # Disconnecting the user entirely: process each active session independently
                from database.db import query_all
                session_sql = """
                    SELECT radacctid, nasipaddress, acctsessionid, framedipaddress, callingstationid
                    FROM radacct
                    WHERE username = %s AND acctstoptime IS NULL
                """
                params = (username,)
                if nas_ip:
                    session_sql += " AND nasipaddress = %s"
                    params += (nas_ip,)
                active_sessions = query_all(session_sql, params)

                if active_sessions:
                    last_res = None
                    all_success = True
                    for sess in active_sessions:
                        s_res = _disconnect_single_session(
                            username=username,
                            nas_ip=sess.get('nasipaddress'),
                            session_id=sess.get('acctsessionid'),
                            framed_ip=sess.get('framedipaddress'),
                            mac_address=sess.get('callingstationid'),
                            radacctid=sess.get('radacctid'),
                            require_accounting_stop=bool(task.get('require_accounting_stop'))
                        )
                        all_success = all_success and bool(s_res.get('success'))
                        last_res = s_res
                    res = last_res or {'success': False, 'status': 'no_sessions', 'message': 'لا توجد جلسات مفتوحة'}
                    res['success'] = all_success
                    if not all_success:
                        res['status'] = 'incomplete'
                        res['message'] = 'لم ينجح فصل جميع الجلسات المطلوبة'
                else:
                    # No active sessions in radacct; send probe to default NAS
                    res = _disconnect_single_session(username, nas_ip, None, framed_ip, mac_address, None,
                        require_accounting_stop=bool(task.get('require_accounting_stop')))

        elif action == 'speed_change' and rate_limit:
            resolved_ip, secret, coa_port, api_port, api_user, api_pass = _resolve_nas_credentials(nas_ip)
            target_nas_ip = resolved_ip or nas_ip or '127.0.0.1'
            client = RadiusCoaClient(nas_ip=target_nas_ip, secret=secret, port=coa_port, timeout=1.8)
            res = client.modify_rate_limit(
                rate_limit_str=rate_limit,
                username=username,
                framed_ip=framed_ip,
                session_id=session_id,
                mac_address=mac_address
            )

        latency = round((time.time() - t_start) * 1000, 1)
        res['latency_ms'] = latency

    except Exception as e:
        res = {
            'success': False,
            'status': 'error',
            'message': str(e),
            'latency_ms': round((time.time() - t_start) * 1000, 1)
        }

    # Record in circular audit log
    entry = {
        'timestamp': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'action': action,
        'username': username,
        'nas_ip': nas_ip,
        'success': bool(res.get('success')),
        'status': res.get('status', 'unknown'),
        'message': res.get('message', ''),
        'latency_ms': res.get('latency_ms', 0.0),
        'reason': reason,
        'admin_username': admin_user
    }
    with _LOG_LOCK:
        _RECENT_COA_LOG.appendleft(entry)
        _stats['total_processed'] += 1
        if entry['success']:
            _stats['total_success'] += 1
        else:
            _stats['total_failed'] += 1

    return res

def _task_key(task):
    return tuple(str(task.get(k) or '') for k in ('action','username','nas_ip','session_id','radacctid','reason','lifecycle_cycle','rate_limit'))


def _enqueue(task):
    key = _task_key(task)
    with _PENDING_LOCK:
        if key in _PENDING_KEYS:
            return True, 'المهمة موجودة في الطابور بالفعل.'
        _PENDING_KEYS.add(key)
        try:
            _COA_TASK_QUEUE.put_nowait(task)
        except queue.Full:
            _PENDING_KEYS.discard(key)
            return False, 'طابور الفصل ممتلئ؛ لم يتم قبول المهمة. أعد المحاولة.'
        _stats['total_enqueued'] += 1
    return True, 'تم قبول المهمة في الطابور؛ لم يكتمل التنفيذ بعد.'


def _coa_worker_loop():
    while not _stop_event.is_set():
        try:
            task = _COA_TASK_QUEUE.get(timeout=1)
        except queue.Empty:
            continue
        try:
            if task is None:
                return
            with _USER_LOCKS[hash(task.get('username')) % len(_USER_LOCKS)]:
                _process_coa_task(task)
        except Exception as exc:
            print(f'CoA task failed: {exc}')
        finally:
            if task:
                with _PENDING_LOCK:
                    _PENDING_KEYS.discard(_task_key(task))
            _COA_TASK_QUEUE.task_done()


def start_coa_worker():
    global _worker_thread
    with _WORKER_START_LOCK:
        alive = [worker for worker in _WORKERS if worker.is_alive()]
        _WORKERS[:] = alive
        _stop_event.clear()
        while len(_WORKERS) < 3:
            worker = threading.Thread(target=_coa_worker_loop, name='CoAQueueWorker', daemon=True)
            _WORKERS.append(worker)
            worker.start()
        _worker_thread = _WORKERS[0]


def enqueue_disconnect(username, nas_ip=None, framed_ip=None, session_id=None, mac_address=None, radacctid=None, reason=None, admin_username='admin', lifecycle_kind=None, lifecycle_id=None, lifecycle_cycle=None, require_accounting_stop=False):
    """
    Non-blocking enqueue of a Disconnect-Request (RFC 5176).
    Returns immediately (< 1ms).
    """
    username = str(username or '').strip()
    if not username:
        return False, "اسم المستخدم غير محدد"

    start_coa_worker()
    task = {
        'action': 'disconnect',
        'require_accounting_stop': bool(require_accounting_stop),
        'username': username,
        'nas_ip': nas_ip,
        'framed_ip': framed_ip,
        'session_id': session_id,
        'mac_address': mac_address,
        'radacctid': radacctid,
        'reason': reason or 'Manual / Automated Disconnect',
        'admin_username': admin_username,
        'enqueued_at': time.time(),
        'lifecycle_kind': lifecycle_kind, 'lifecycle_id': lifecycle_id, 'lifecycle_cycle': lifecycle_cycle
    }
    return _enqueue(task)


def enqueue_speed_change(username, rate_limit_str, nas_ip=None, admin_username='admin'):
    """
    Non-blocking enqueue of a Dynamic Speed Limit change (CoA).
    Returns immediately (< 1ms).
    """
    username = str(username or '').strip()
    rate_limit_str = str(rate_limit_str or '').strip()
    if not username or not rate_limit_str:
        return False, "البيانات غير مكتملة لتعديل السرعة"

    start_coa_worker()
    task = {
        'action': 'speed_change',
        'username': username,
        'rate_limit': rate_limit_str,
        'nas_ip': nas_ip,
        'reason': f"Dynamic Speed Update: {rate_limit_str}",
        'admin_username': admin_username,
        'enqueued_at': time.time()
    }
    return _enqueue(task)


def enqueue_bulk_disconnect(usernames, reason=None, admin_username='admin'):
    """
    Asynchronously enqueues disconnect requests for multiple usernames.
    """
    if not usernames:
        return 0

    count = 0
    start_coa_worker()
    for u in usernames:
        clean_u = str(u or '').strip()
        if clean_u:
            ok, _ = enqueue_disconnect(clean_u, reason=reason, admin_username=admin_username)
            count += int(ok)
    return count

def get_coa_queue_status():
    """Returns telemetry of queue health, pending tasks, and recent execution logs."""
    start_coa_worker()
    with _LOG_LOCK:
        recent = list(_RECENT_COA_LOG)

    with _COA_TASK_QUEUE.mutex:
        queued = list(_COA_TASK_QUEUE.queue)
    return {
        'queue_size': _COA_TASK_QUEUE.qsize(),
        'worker_count': sum(worker.is_alive() for worker in _WORKERS),
        'oldest_pending_seconds': max([time.time()-task.get('enqueued_at',time.time()) for task in queued if task] or [0]),
        'worker_alive': bool(_worker_thread and _worker_thread.is_alive()),
        'total_enqueued': _stats['total_enqueued'],
        'total_processed': _stats['total_processed'],
        'total_success': _stats['total_success'],
        'total_failed': _stats['total_failed'],
        'recent_tasks': recent
    }

# Workers start lazily when a task is submitted.

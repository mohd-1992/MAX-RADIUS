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

def _disconnect_single_session(username, nas_ip, session_id, framed_ip, mac_address, radacctid=None):
    """Executes disconnect on a single session and updates radacct ONLY for that session if successful."""
    resolved_ip, secret, coa_port, api_port, api_user, api_pass = _resolve_nas_credentials(nas_ip)
    target_nas_ip = resolved_ip or nas_ip or '127.0.0.1'
    client = RadiusCoaClient(nas_ip=target_nas_ip, secret=secret, port=coa_port, timeout=1.8)

    res = client.disconnect_user(
        username=username,
        framed_ip=framed_ip,
        session_id=session_id,
        mac_address=mac_address
    )

    # MikroTik API fallback if UDP CoA failed
    if not res.get('success') and api_port and api_user and (session_id or framed_ip or mac_address):
        ros = None
        try:
            from core.mikrotik_api import RouterOSApiProtocol
            ros = RouterOSApiProtocol(target_nas_ip, port=int(api_port), timeout=2.5)
            ros.connect()
            if ros.login(api_user, api_pass):
                u_lower = username.lower()
                candidates = []
                for path, user_key in (('/ip/hotspot/active', 'user'), ('/ppp/active', 'name')):
                    replies = ros.talk_raw([path + '/print'])
                    kinds = [kind for kind, _ in replies]
                    if '!done' not in kinds or any(kind in kinds for kind in ('!trap', '!fatal')):
                        raise RuntimeError('Router session inventory could not be verified')
                    for kind, item in replies:
                        if kind != '!re':
                            continue
                        if str(item.get(user_key, '')).lower() != u_lower or not item.get('.id'):
                            continue
                        api_sid = item.get('session-id') or item.get('acct-session-id')
                        if session_id and api_sid:
                            matches = str(api_sid) == str(session_id)
                        elif framed_ip:
                            matches = str(item.get('address', '')) == str(framed_ip)
                        elif mac_address:
                            actual_mac = item.get('mac-address') or item.get('caller-id') or ''
                            matches = str(actual_mac).replace('-', ':').lower() == str(mac_address).replace('-', ':').lower()
                        else:
                            matches = False
                        if matches:
                            candidates.append((path, item['.id']))
                if not candidates:
                    res.update(success=True,status='api_confirmed_absent',message='أكدت قراءة قوائم الراوتر أن الجلسة المطلوبة غير موجودة.')
                # Ambiguous identities must not disconnect an unrelated session.
                if len(candidates) == 1:
                    path, item_id = candidates[0]
                    replies = ros.talk_raw([path + '/remove', f'=.id={item_id}'])
                    reply_types = [kind for kind, _ in replies]
                    if '!done' in reply_types and not any(kind in reply_types for kind in ('!trap', '!fatal')):
                        res.update(success=True, status='api_ack', message=f'تم فصل الجلسة عبر MikroTik API ({target_nas_ip}).')
        except Exception:
            pass
        finally:
            if ros is not None:
                ros.close()

    # Cleanly update radacct session ONLY if CoA / API disconnect succeeded for THIS session
    if res.get('success'):
        try:
            if radacctid:
                execute_write("""
                    UPDATE radacct 
                    SET acctstoptime = COALESCE(acctstoptime, CURRENT_TIMESTAMP),
                        acctterminatecause = 'Admin-Reset-CoA'
                    WHERE radacctid = %s AND (acctstoptime IS NULL OR acctterminatecause IN ('Stale-Session-Timeout','Watchdog-Autoheal-Timeout','Backup-Restored-Closed'))
                """, (radacctid,))
            elif target_nas_ip and session_id:
                execute_write("""
                    UPDATE radacct 
                    SET acctstoptime = COALESCE(acctstoptime, CURRENT_TIMESTAMP),
                        acctterminatecause = 'Admin-Reset-CoA'
                    WHERE username = %s AND nasipaddress = %s AND acctsessionid = %s AND acctstoptime IS NULL
                """, (username, target_nas_ip, session_id))
        except Exception:
            pass

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
                res = _disconnect_single_session(username, nas_ip, session_id, framed_ip, mac_address, radacctid)
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
                            radacctid=sess.get('radacctid')
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
                    res = _disconnect_single_session(username, nas_ip, None, framed_ip, mac_address, None)

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


def enqueue_disconnect(username, nas_ip=None, framed_ip=None, session_id=None, mac_address=None, radacctid=None, reason=None, admin_username='admin', lifecycle_kind=None, lifecycle_id=None, lifecycle_cycle=None):
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

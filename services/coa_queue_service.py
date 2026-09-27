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

def _process_coa_task(task):
    """Executes a single CoA or Disconnect action with failover and audit logging."""
    action = task.get('action', 'disconnect')
    username = task.get('username')
    admin_user = task.get('admin_username', 'system')
    nas_ip = task.get('nas_ip')
    framed_ip = task.get('framed_ip')
    session_id = task.get('session_id')
    mac_address = task.get('mac_address')
    rate_limit = task.get('rate_limit')
    reason = task.get('reason', 'Scheduled Lifecycle / Expiry Disconnect')

    t_start = time.time()
    res = {'success': False, 'status': 'unknown', 'message': '', 'latency_ms': 0.0}

    try:
        # 1. Inspect active session from radacct if fields missing
        if not nas_ip or not session_id or not framed_ip:
            sess = query_one("""
                SELECT nasipaddress, acctsessionid, framedipaddress, callingstationid
                FROM radacct
                WHERE username = %s AND acctstoptime IS NULL
                ORDER BY radacctid DESC LIMIT 1
            """, (username,))
            if sess:
                nas_ip = nas_ip or sess.get('nasipaddress')
                session_id = session_id or sess.get('acctsessionid')
                framed_ip = framed_ip or sess.get('framedipaddress')
                mac_address = mac_address or sess.get('callingstationid')

        # 2. Resolve NAS network parameters
        resolved_ip, secret, coa_port, api_port, api_user, api_pass = _resolve_nas_credentials(nas_ip)
        nas_ip = resolved_ip or nas_ip or '127.0.0.1'

        # 3. Execute Primary RFC 5176 CoA/PoD
        client = RadiusCoaClient(nas_ip=nas_ip, secret=secret, port=coa_port, timeout=1.8)

        if action == 'disconnect':
            res = client.disconnect_user(
                username=username,
                framed_ip=framed_ip,
                session_id=session_id,
                mac_address=mac_address
            )
            # MikroTik API fallback if UDP CoA failed
            if not res.get('success') and api_port and api_user:
                try:
                    from core.mikrotik_api import RouterOSApiProtocol
                    ros = RouterOSApiProtocol(nas_ip, port=int(api_port), timeout=2.5)
                    ros.connect()
                    if ros.login(api_user, api_pass):
                        u_lower = username.lower()
                        # Drop from hotspot active
                        try:
                            items = ros.talk(['/ip/hotspot/active/print'])
                            for it in (items or []):
                                if str(it.get('user', '')).lower() == u_lower and it.get('.id'):
                                    ros.talk(['/ip/hotspot/active/remove', f"=.id={it.get('.id')}"])
                                    res['success'] = True
                                    res['status'] = 'api_ack'
                                    res['message'] = f'تم فصل الجلسة عبر MikroTik API ({nas_ip}).'
                        except Exception:
                            pass
                        # Drop from ppp active
                        try:
                            ppp_items = ros.talk(['/ppp/active/print'])
                            for it in (ppp_items or []):
                                if str(it.get('name', '')).lower() == u_lower and it.get('.id'):
                                    ros.talk(['/ppp/active/remove', f"=.id={it.get('.id')}"])
                                    res['success'] = True
                                    res['status'] = 'api_ack'
                                    res['message'] = f'تم فصل جلسة PPPoE عبر MikroTik API ({nas_ip}).'
                        except Exception:
                            pass
                        ros.close()
                except Exception as api_err:
                    pass

            # Cleanly update radacct session
            try:
                execute_write("""
                    UPDATE radacct 
                    SET acctstoptime = CURRENT_TIMESTAMP,
                        acctterminatecause = 'Admin-Reset-CoA'
                    WHERE username = %s AND acctstoptime IS NULL
                """, (username,))
            except Exception:
                pass

        elif action == 'speed_change' and rate_limit:
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

def _coa_worker_loop():
    """Background worker daemon processing queued CoA tasks."""
    while not _stop_event.is_set():
        try:
            task = _COA_TASK_QUEUE.get(timeout=1.0)
            if task is None:
                break
            _process_coa_task(task)
            _COA_TASK_QUEUE.task_done()
        except queue.Empty:
            continue
        except Exception as e:
            print(f"Error in CoA worker daemon: {e}")

def start_coa_worker():
    """Starts the background worker thread if not already running."""
    global _worker_thread
    if _worker_thread is None or not _worker_thread.is_alive():
        _stop_event.clear()
        _worker_thread = threading.Thread(target=_coa_worker_loop, name="CoAQueueWorker", daemon=True)
        _worker_thread.start()

def enqueue_disconnect(username, nas_ip=None, framed_ip=None, session_id=None, mac_address=None, reason=None, admin_username='admin'):
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
        'reason': reason or 'Manual / Automated Disconnect',
        'admin_username': admin_username,
        'enqueued_at': time.time()
    }
    _COA_TASK_QUEUE.put(task)
    _stats['total_enqueued'] += 1
    return True, f"تمت جدولة أمر قطع الاتصال للمشترك [{username}] في طابور المعالجة اللحظية بنجاح."

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
    _COA_TASK_QUEUE.put(task)
    _stats['total_enqueued'] += 1
    return True, f"تمت جدولة أمر تعديل السرعة إلى ({rate_limit_str}) في طابور المعالجة اللحظية."

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
            enqueue_disconnect(clean_u, reason=reason, admin_username=admin_username)
            count += 1
    return count

def get_coa_queue_status():
    """Returns telemetry of queue health, pending tasks, and recent execution logs."""
    start_coa_worker()
    with _LOG_LOCK:
        recent = list(_RECENT_COA_LOG)

    return {
        'queue_size': _COA_TASK_QUEUE.qsize(),
        'worker_alive': bool(_worker_thread and _worker_thread.is_alive()),
        'total_enqueued': _stats['total_enqueued'],
        'total_processed': _stats['total_processed'],
        'total_success': _stats['total_success'],
        'total_failed': _stats['total_failed'],
        'recent_tasks': recent
    }

# Ensure worker starts on module load
start_coa_worker()

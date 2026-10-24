"""Web bridge to an installation-owned Unix update controller; no Docker writes."""
import datetime as dt
import hmac
import http.client
import json
import os
import socket

PREFIX = '/_max_updates/v1/'


class Connection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(os.environ.get('MAX_UPDATE_SOCKET', '/var/run/max-updates.sock'))


def request(method, action, data=None):
    connection = Connection('localhost', timeout=18)
    try:
        connection.request(method, PREFIX+action, body=json.dumps(data).encode() if data is not None else None,
                           headers={'Content-Type':'application/json'})
        response = connection.getresponse(); raw = response.read(262145)
        if response.status != 200 or len(raw)>262144:
            raise RuntimeError('Installation controller unavailable')
        value = json.loads(raw)
        if not isinstance(value, dict): raise ValueError('Invalid controller response')
        return value
    finally:
        connection.close()


def authorized():
    from flask import has_request_context, request as req, session
    if not has_request_context(): return False
    from core.rbac import get_current_manager
    manager = get_current_manager()
    token, expected = req.headers.get('X-CSRF-Token',''), session.get('_license_csrf','')
    from urllib.parse import urlsplit
    origin = req.headers.get('Origin')
    return bool(manager and manager.get('role_code')=='superadmin' and token and expected
                and hmac.compare_digest(token,expected)
                and (not origin or urlsplit(origin).netloc == req.host))


def check(current):
    try:
        result = request('GET','check')
        if result.get('current_version') != current: raise ValueError('Runtime version mismatch')
        return result
    except Exception:
        return {'success':False,'has_update':False,'current_version':current,'latest_version':None,
                'checked_at':dt.datetime.now(dt.timezone.utc).isoformat(),
                'message':'قناة التحديثات غير مهيأة أو تعذر التحقق منها؛ لم يتم إثبات أن النظام محدث.'}


def progress():
    try: return request('GET','progress')
    except Exception:
        return {'status':'unavailable','percent':0,'stage':'تعذر قراءة حالة التحديث؛ لا تعِد البدء قبل التحقق.'}


def apply(target_version, backup_first, release_id, manifest_sha256):
    if not authorized():
        return {'success':False,'message':'يتطلب التحديث جلسة المدير الرئيسي وحماية الطلب.'}
    if backup_first is not True or not target_version or not release_id or not manifest_sha256:
        return {'success':False,'message':'يجب مراجعة إصدار موثق وأخذ نسخة احتياطية إلزامية.'}
    try:
        return request('POST','apply',{'version':target_version,'backup_first':True,
                       'release_id':release_id,'manifest_sha256':manifest_sha256})
    except Exception:
        return {'success':False,'message':'لم يتأكد استلام طلب التحديث؛ راجع الحالة قبل تكراره.'}

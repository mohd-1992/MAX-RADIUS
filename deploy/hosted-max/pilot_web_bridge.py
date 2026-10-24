"""4k bootstrap bridge. Existing MAX routes retain login/superadmin/CSRF checks.

No registry, shared update feed, subprocess or Docker mutation is available here.
The host controller holds the immutable release and owns durable execution.
"""
import datetime
import http.client
import json
import socket
from core.config import APP_VERSION


class Connection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect('/var/run/docker.sock')


def _request(method, path, body=None):
    connection = Connection('localhost', timeout=12)
    try:
        connection.request(method, '/_max_pilot/4k/' + path,
                           body=json.dumps(body).encode() if body is not None else None,
                           headers={'Content-Type': 'application/json'})
        response = connection.getresponse()
        raw = response.read(65537)
        if response.status != 200 or len(raw) > 65536:
            raise RuntimeError('Scoped release controller unavailable')
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError('Invalid response')
        return result
    finally:
        connection.close()


def get_current_system_version():
    return APP_VERSION


def check_for_updates(force_refresh=False, force=False):
    try:
        result = _request('GET', 'check')
        if result.get('target_domain') != '4k.max-net.net' or result.get('components') != ['web', 'core']:
            raise ValueError('Audience mismatch')
        result.update(current_version=APP_VERSION, has_update=bool(result.get('has_update') and APP_VERSION != result.get('latest_version')))
        return result
    except Exception:
        return {'success': False, 'has_update': False, 'current_version': APP_VERSION,
                'latest_version': None, 'message': 'تعذر التحقق من قناة 4k الخاصة؛ التثبيت محجوب.',
                'checked_at': datetime.datetime.now().isoformat(timespec='seconds')}


def get_update_progress():
    try:
        return _request('GET', 'progress')
    except Exception:
        return {'status': 'unavailable', 'percent': 0, 'stage': 'تعذر قراءة الحالة؛ لا تعِد بدء التحديث.'}


def _authorized():
    import hmac
    from flask import has_request_context, request, session
    if not has_request_context() or request.host.split(':')[0]!='4k.max-net.net':
        return False
    from core.rbac import get_current_manager
    manager=get_current_manager()
    origin=request.headers.get('Origin')
    token=request.headers.get('X-CSRF-Token','')
    expected=session.get('_license_csrf','')
    return bool(manager and manager.get('role_code')=='superadmin' and
                (not origin or origin=='https://4k.max-net.net') and token and expected and hmac.compare_digest(token,expected))


def trigger_system_update(target_version=None, backup_first=True, triggered_by='Admin'):
    if backup_first is not True or target_version != '2.6.3':
        return {'success': False, 'message': 'الإصدار والنسخة الإلزامية لا يقبلان التجاوز.'}
    if not _authorized():
        return {'success': False, 'message': 'يتطلب تحديث 4k جلسة المدير الرئيسي وتأكيد حماية الطلب.'}
    try:
        return _request('POST', 'apply', {'version': target_version, 'backup_first': True})
    except Exception:
        return {'success': False, 'message': 'لم يتأكد بدء المهمة؛ راجع التقدم قبل تكرار الطلب.'}

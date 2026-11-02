"""Explicit hosted control contract. Missing authority never falls back to Docker."""
import http.client
import json
import os
from pathlib import Path
import re
import socket
import threading
import time


def configured():
    return (os.environ.get('MAX_CONTROL_MODE', '') not in ('', 'local')
            or bool(os.environ.get('MAX_CONTROL_SOCKET') or os.environ.get('MAX_CONTROL_TENANT_ID')))


def request(method, path):
    if os.environ.get('MAX_CONTROL_MODE') != 'hosted':
        raise RuntimeError('إعداد وصلة المضيف غير مكتمل؛ لم يستخدم Docker بديلًا.')
    endpoint = os.environ.get('MAX_CONTROL_SOCKET', '')
    if not endpoint.startswith('/') or '..' in Path(endpoint).parts:
        raise RuntimeError('مسار وصلة المضيف غير صالح.')

    class Connection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(6)
            self.sock.connect(endpoint)

    conn = Connection('localhost', timeout=6)
    try:
        conn.request(method, path, headers={'Connection': 'close'})
        response = conn.getresponse()
        raw = response.read(1024 * 1024 + 1)
        if response.status >= 400 or len(raw) > 1024 * 1024:
            raise RuntimeError('تعذر التحقق من خدمة المضيف أو أن العملية غير مسموحة.')
        return json.loads(raw) if raw else True
    finally:
        conn.close()


def state():
    data = request('GET', '/max/v1/state')
    tenant = os.environ.get('MAX_CONTROL_TENANT_ID', '')
    if (not isinstance(data, dict) or data.get('schema') != 1
            or not re.fullmatch('[a-f0-9]{32}', tenant)
            or data.get('tenant_id') != tenant
            or data.get('instance') != os.environ.get('INSTANCE_UUID')
            or data.get('domain') != os.environ.get('CLIENT_SUBDOMAIN', '') + '.max-r.cc'):
        raise RuntimeError('هوية وصلة المضيف لا تطابق هذه المنظومة.')
    from services.autoheal_service import _runtime_container_ids
    identities = _runtime_container_ids()
    services = data.get('services')
    if not isinstance(services, dict) or set(services) != {'web', 'core', 'db'}:
        raise RuntimeError('عقد خدمات المضيف غير مكتمل.')
    web = services['web']
    if len(identities) != 1 or web.get('Id') not in identities:
        raise RuntimeError('لم تثبت ملكية حاوية الويب لهذه الوصلة.')
    for role, row in services.items():
        if not isinstance(row, dict) or not re.fullmatch('[a-f0-9]{64}', row.get('Id', '')):
            raise RuntimeError('هوية الخدمة غير صالحة.')
        labels = row.get('Config', {}).get('Labels', {})
        if role != 'db' and (labels.get('max.tenant') != tenant
                            or labels.get('max.service') != role
                            or labels.get('max.platform') != 'max-r'):
            raise RuntimeError('ملكية الخدمة لا تطابق هذه المنظومة.')
        if role == 'db' and row.get('read_only') is not True:
            raise RuntimeError('محرك الاستضافة المشترك يجب أن يبقى للقراءة فقط.')
    return data


def current_core():
    data = state()
    for role in ('web', 'core'):
        status = data['services'][role].get('State', {})
        if (not status.get('Running') or status.get('Paused') or status.get('Restarting')
                or status.get('Health', {}).get('Status') in ('unhealthy', 'starting')):
            raise RuntimeError('إحدى حاويتي Web أو RADIUS لم تجتز الجاهزية.')
    if core_health() is not True:
        raise RuntimeError('لم تتأكد استجابة RADIUS الموثقة للمصادقة والمحاسبة.')
    return data['services']['core']


_probe_lock = threading.Lock()
_probe_cache = (0.0, None)


def core_health():
    global _probe_cache
    with _probe_lock:
        now = time.monotonic()
        if now - _probe_cache[0] > 15 or not _probe_cache[0]:
            from services.health_probe_service import radius_probe
            auth = radius_probe(int(os.environ.get('RADIUS_AUTH_PORT', '1812')))[0]
            acct = radius_probe(int(os.environ.get('RADIUS_ACCT_PORT', '1813')))[0]
            _probe_cache = (now, True if auth is True and acct is True else False if auth is False or acct is False else None)
        return _probe_cache[1]


def fleet():
    data = state()
    titles = {'web': ('لوحة التحكم وبوابة المشتركين', 'fa-solid fa-globe', 'blue', 'web'),
              'core': ('محرك المصادقة والمحاسبة FreeRADIUS', 'fa-solid fa-satellite-dish', 'indigo', 'radius'),
              'db': ('محرك قاعدة البيانات المشترك', 'fa-solid fa-database', 'cyan', 'database')}
    rows = []
    for role, raw in data['services'].items():
        title, icon, color, display_role = titles[role]
        running = raw['State'].get('Running') is True
        health = raw['State'].get('Health', {}).get('Status')
        verified = core_health() if role == 'core' and running else health == 'healthy'
        label = ('استجابة RADIUS موثقة' if role == 'core' else 'سليم وصحي') if running and verified else 'يعمل؛ الصحة غير متحققة' if running and verified is not False else 'تعذر فحص الاستجابة' if running else 'متوقف'
        rows.append(dict(name=raw['Name'].lstrip('/'), id=raw['Id'][:12], container_id=raw['Id'],
                         role=display_role, title=title, icon=icon, color=color, kind='container',
                         desc='خدمة مشتركة؛ التحكم من لوحة المنظومات' if role == 'db' else 'خدمة خاصة بهذه المنظومة',
                         state='running' if running else raw['State'].get('Status', 'unknown'),
                         status='تعمل من المضيف — قراءة فقط' if role == 'db' and running else raw['State'].get('Status', 'unknown'),
                         image=raw['Config']['Image'], created=raw.get('Created', '-'),
                         is_healthy=running and verified is True, health_label=label,
                         monitored=True, can_restart=role != 'db', can_logs=role != 'db', read_only=role == 'db'))
    from core import watchdog
    active = bool(watchdog._watchdog_thread and watchdog._watchdog_thread.is_alive())
    rows.insert(0, dict(name='internal-watchdog', id='internal', role='autoheal', title='المراقب الداخلي',
                        desc='خدمة داخل عملية النظام؛ لا تحتاج حاوية مستقلة', icon='fa-solid fa-heart-pulse',
                        color='emerald', kind='internal', state='running' if active else 'stopped',
                        status='داخل النظام', image='—', created='—', monitored=False,
                        is_healthy=active, health_label='يعمل داخل النظام' if active else 'المراقب غير نشط',
                        can_restart=False, can_logs=False, read_only=True))
    rows.append(dict(name='host-tunnels', id='external', role='vpn', title='الأنفاق المشتركة',
                     desc='تدار من لوحة المنظومات؛ ليست حاوية خاصة بهذه المنظومة',
                     icon='fa-solid fa-network-wired', color='amber', kind='external', state='external',
                     status='تدار من المضيف', image='—', created='—', monitored=False,
                     is_healthy=None, health_label='خدمة خارجية', can_restart=False, can_logs=False, read_only=True))
    return rows


def service_action(role, action):
    data = state()
    if role not in ('web', 'core') or action not in ('start', 'restart', 'stop'):
        raise RuntimeError('الخدمات المشتركة والداخلية تدار من المضيف.')
    ident = data['services'][role]['Id']
    return request('POST', '/containers/' + ident + '/' + action)


def resolve(reference, require_id=False):
    if not isinstance(reference, str) or (require_id and not re.fullmatch('[a-f0-9]{64}', reference)):
        return None, 'مرجع الخدمة غير صالح؛ حدّث الصفحة.'
    try:
        rows = state()['services']
        for role in ('web', 'core'):
            row = rows[role]
            if reference in (row['Id'], row['Name'].lstrip('/')):
                return row, None
        return None, 'التحكم متاح لخدمات هذه المنظومة فقط؛ الخدمات المشتركة تدار من المضيف.'
    except Exception:
        return None, 'تعذر التحقق من وصلة المضيف؛ لم تستخدم خدمة بديلة.'


def container_info(candidates):
    aliases = {'max_radius_web': 'web', 'wisp_web_v2': 'web', 'max-radius-wisp-web': 'web',
               'max_radius_core': 'core', 'wisp_freeradius_v2': 'core', 'max-radius-freeradius': 'core',
               'max_radius_db': 'db', 'wisp_mariadb_v2': 'db'}
    candidates = [candidates] if isinstance(candidates, str) else list(candidates)
    rows = state()['services']
    for name in candidates:
        role = aliases.get(name)
        if role:
            row = rows[role]
            return {'exists': True, 'name': row['Name'].lstrip('/'),
                    'status': row['State'].get('Status', 'unknown'),
                    'is_running': row['State'].get('Running') is True,
                    'uptime': row['State'].get('StartedAt', 'غير متوفر'),
                    'image': row['Config']['Image'], 'read_only': role == 'db'}
    return {'exists': False, 'name': candidates[0], 'status': 'external',
            'is_running': False, 'uptime': 'تدار من المضيف', 'read_only': True}

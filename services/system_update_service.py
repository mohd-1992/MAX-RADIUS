"""Authenticated catalogue lookup and durable, Web-only update dispatch."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import threading
import urllib.parse
import urllib.request
import uuid

from core.config import APP_VERSION, STORAGE_DIR
from services.system_update_runtime import Docker, PIN, TERMINAL, UpdateError, scope, storage_mount, endpoints_for, write_json

DEFAULT_UPDATE_SERVER_URL = os.environ.get('MASTER_UPDATE_SERVER_URL', 'https://raw.githubusercontent.com/mohd-1992/MAX-RADIUS/main/release.json')
_UPDATE_CACHE = {'data': None, 'timestamp': 0}
_UPDATE_LOCK = threading.Lock()

def get_current_system_version():
    return APP_VERSION

def parse_version_tuple(value):
    import re
    if not re.fullmatch(r'v?\d+(?:\.\d+){1,3}', str(value)):
        raise ValueError('Invalid release version')
    parts = tuple(int(v) for v in str(value).lstrip('v').split('.'))
    return parts + (0,) * (4-len(parts))

def is_newer_version(current_v, latest_v):
    return parse_version_tuple(latest_v) > parse_version_tuple(current_v)

def check_for_updates(force_refresh=False, force=False):
    import time
    now = time.monotonic()
    if not (force or force_refresh) and _UPDATE_CACHE['data'] and now - _UPDATE_CACHE['timestamp'] < 300:
        return dict(_UPDATE_CACHE['data'])
    result = {'success': False, 'has_update': False, 'current_version': APP_VERSION,
              'latest_version': None, 'message': 'تعذر التحقق من إصدار موثوق؛ لم يتم إثبات أن النظام محدث.',
              'checked_at': datetime.datetime.now().isoformat(timespec='seconds')}
    try:
        parsed = urllib.parse.urlsplit(DEFAULT_UPDATE_SERVER_URL)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise UpdateError('Update catalogue requires HTTPS')
        req = urllib.request.Request(DEFAULT_UPDATE_SERVER_URL, headers={'User-Agent': 'MAX-RADIUS-Client/' + APP_VERSION})
        with urllib.request.urlopen(req, timeout=8) as response:
            if response.status != 200 or urllib.parse.urlsplit(response.geturl()).scheme != 'https':
                raise UpdateError('Invalid catalogue response')
            raw = response.read(262145)
            if len(raw) > 262144:
                raise UpdateError('Catalogue too large')
            release = json.loads(raw)
        version = release['version']
        newer = is_newer_version(APP_VERSION, version)
        image = release.get('docker_image') or release.get('images', {}).get('web', '')
        if newer and (not PIN.fullmatch(image) or release.get('update_scope') != 'web_only'):
            raise UpdateError('Release lacks a pinned compatible Web image')
        result.update(success=True, has_update=newer, latest_version=version,
                      docker_image=image if newer else None, title=str(release.get('title', 'تحديث النظام')),
                      changelog=[str(v) for v in release.get('changelog', [])][:50],
                      release_date=str(release.get('release_date', '')), is_critical=bool(release.get('is_critical', False)),
                      message='تم التحقق من بيان الإصدار.')
    except Exception:
        # Never expose remote responses, URLs with credentials or Docker environments.
        pass
    _UPDATE_CACHE.update(data=result, timestamp=now)
    return dict(result)

def _root():
    root = Path(STORAGE_DIR) / 'system_updates'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root

def get_update_progress():
    try:
        pointer = json.loads((_root()/'current.json').read_text())
        job = pointer['job']
        if not __import__('re').fullmatch('[0-9a-f]{32}', job):
            raise ValueError('Invalid job')
        state = json.loads((_root()/job/'state.json').read_text())
        return {k: v for k, v in state.items() if k in ('status','stage','percent','message','error','started_at','target_version','completed_at')}
    except FileNotFoundError:
        return {'status': 'idle', 'percent': 0, 'stage': ''}
    except Exception:
        return {'status': 'error', 'percent': 0, 'error': 'تعذرت قراءة حالة التحديث المحفوظة.'}

def _verified_backup(operator, folder):
    import tarfile
    from services.backup_service import create_backup, get_backup_filepath
    response = create_backup(admin_username=operator, notes='Pre-update snapshot', dispatch_notifications=False)
    if not isinstance(response, tuple) or len(response) != 3 or not response[0]:
        raise UpdateError('Mandatory pre-update backup failed')
    path = Path(get_backup_filepath(response[2]['filename']))
    if not path.is_file() or path.stat().st_size == 0:
        raise UpdateError('Backup file unavailable')
    # Reading every member verifies gzip/tar integrity, including the SQL payload.
    sql_found = False
    with tarfile.open(path, 'r:gz') as archive:
        for member in archive:
            if not member.isfile():
                continue
            stream = archive.extractfile(member)
            count = 0
            while True:
                block = stream.read(1048576)
                if not block:
                    break
                count += len(block)
            sql_found |= member.name.endswith('.sql') and count > 0
    if not sql_found:
        raise UpdateError('Backup does not contain a database dump')
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    write_json(folder/'backup.private.json', {'filename': path.name, 'bytes': path.stat().st_size, 'sha256': digest})

def _execute_update_worker(target_version, release, folder, operator):
    from services.system_update_runtime import inspect
    state_path = folder/'state.json'
    try:
        api = Docker()
        old = scope(api, os.environ.get('HOSTNAME', ''))
        mount = storage_mount(old, str(Path(STORAGE_DIR)))
        endpoints = endpoints_for(old)
        _verified_backup(operator, folder)
        state = json.loads(state_path.read_text())
        state.update(percent=40, stage='تحميل الصورة المثبتة والتحقق من هويتها')
        write_json(state_path, state)
        image = release['docker_image']
        if not PIN.fullmatch(image):
            raise UpdateError('Unpinned image')
        api('POST', '/images/create?fromImage=' + urllib.parse.quote(image, safe=''), timeout=240)
        target = api('GET', '/images/' + urllib.parse.quote(image, safe='') + '/json')
        if not target or image not in target.get('RepoDigests', []) or not str(target.get('Id','')).startswith('sha256:'):
            raise UpdateError('Pulled digest did not verify')
        if target['Id'] == old['Image']:
            raise UpdateError('Target image is already running')
        key_hash = hashlib.sha256(Path('/opt/max-protection/runtime-public.pem').read_bytes()).hexdigest()
        plan = {'job': folder.name, 'old': old, 'target_id': target['Id'], 'target_digest': image,
                'endpoints': endpoints, 'key_hash': key_hash, 'version': target_version}
        write_json(folder/'plan.private.json', plan)
        # Start the helper from the CURRENT trusted image, with no network, app/DB environment or public tag.
        helper = {'Image': old['Image'], 'Entrypoint': ['python','-c'],
                  'Cmd': ['from services.system_update_runtime import run_job;run_job(' + repr(str(folder)) + ')'],
                  'Env': ['PYTHONDONTWRITEBYTECODE=1','PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin'],
                  'Labels': {'max.update.helper': folder.name},
                  'HostConfig': {'NetworkMode': 'none', 'ReadonlyRootfs': True,
                                 'RestartPolicy': {'Name': 'on-failure', 'MaximumRetryCount': 3},
                                 'Mounts': [mount, {'Type':'bind','Source':'/var/run/docker.sock','Target':'/var/run/docker.sock','ReadOnly':False}],
                                 'Tmpfs': {'/tmp':'rw,nosuid,size=33554432'},
                                 'LogConfig': {'Type':'json-file','Config':{'max-size':'5m','max-file':'1'}}}}
        created = api('POST', '/containers/create?name=max_web_update_helper_' + folder.name, helper)
        write_json(folder/'helper.private.json', {'id': created['Id']})
        state = json.loads(state_path.read_text())
        state['helper_start_attempted'] = True
        write_json(state_path, state)
        api('POST', '/containers/' + created['Id'] + '/start')
        # Only the independent helper can mark completed, after replacement verification.
    except Exception as exc:
        write_json(folder/'failure.private.json', {'exception': type(exc).__name__})
        state = json.loads(state_path.read_text())
        state.update(status='error', percent=0, stage='توقف التحديث قبل التبديل', error='تعذر تجهيز التحديث؛ بقيت الواجهة الحالية دون تبديل.')
        write_json(state_path, state)

def trigger_system_update(target_version=None, backup_first=True, triggered_by='Admin'):
    if os.name != 'posix':
        return {'success':False, 'message':'التحديث المباشر متاح في بيئة Docker Linux فقط.'}
    import fcntl
    with _UPDATE_LOCK, (_root()/'dispatch.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'success':False, 'message':'توجد عملية تحديث أخرى.'}
        if get_update_progress()['status'] not in {'completed','rolled_back','error','idle'}:
            return {'success':False, 'message':'عملية التحديث السابقة لم تنته بعد.'}
        if backup_first is not True:
            return {'success':False, 'message':'النسخة الاحتياطية شرط إلزامي للتحديث.'}
        release = check_for_updates(force_refresh=True)
        if not release.get('success') or not release.get('has_update') or target_version != release.get('latest_version'):
            return {'success':False, 'message':'لا يوجد إصدار أحدث موثوق ومثبت مطابق للطلب.'}
        job = uuid.uuid4().hex
        folder = _root()/job
        folder.mkdir(mode=0o700)
        write_json(folder/'state.json', {'status':'running','percent':10,'stage':'التحقق من النطاق وأخذ النسخة الاحتياطية',
                                       'target_version':target_version,'started_at':datetime.datetime.now().isoformat()})
        write_json(_root()/'current.json', {'job':job})
        threading.Thread(target=_execute_update_worker, args=(target_version,release,folder,triggered_by), daemon=True).start()
        return {'success':True,'target_version':target_version,'message':'بدأ تجهيز التحديث؛ النجاح ينتظر التحقق من التشغيل.'}

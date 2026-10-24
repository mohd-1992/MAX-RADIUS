"""Pinned Web-only updates. The helper survives the Web process it replaces."""
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import time
import urllib.parse

PIN = re.compile(r'^mohd777/max-radius-web@sha256:[0-9a-f]{64}$')
TERMINAL = {'completed', 'error', 'rolled_back', 'recovery_required'}

class UpdateError(RuntimeError):
    pass

class Docker:
    def __call__(self, method, path, body=None, timeout=30):
        conn = http.client.HTTPConnection('localhost', timeout=timeout)
        conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.sock.settimeout(timeout)
        conn.sock.connect('/var/run/docker.sock')
        try:
            conn.request(method, '/v1.45' + path, body=json.dumps(body).encode() if body is not None else None,
                         headers={'Content-Type': 'application/json'})
            response = conn.getresponse()
            data = response.read()
            if not 200 <= response.status < 300:
                raise UpdateError('Docker API rejected operation (%s)' % response.status)
            if not data:
                return None
            try:
                value = json.loads(data)
            except ValueError:
                # Pull streams are JSON lines. HTTP 200 does not imply a successful pull.
                value = [json.loads(line) for line in data.splitlines() if line.strip()]
            if isinstance(value, list) and any(isinstance(v, dict) and (v.get('error') or v.get('errorDetail')) for v in value):
                raise UpdateError('Image pull failed')
            return value
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise UpdateError('Docker API unavailable or invalid response') from exc
        finally:
            conn.close()

def write_json(path, value):
    path = Path(path)
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8') as out:
        os.chmod(tmp, 0o600)
        json.dump(value, out, ensure_ascii=False)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)

def inspect(api, ident):
    value = api('GET', '/containers/' + ident + '/json')
    if not isinstance(value, dict) or not value.get('Id'):
        raise UpdateError('Container identity unavailable')
    return value

def scope(api, hostname):
    fleet = api('GET', '/containers/json?all=1')
    own = [v for v in (fleet or []) if v.get('Id', '').startswith(hostname) and hostname]
    if len(own) != 1:
        raise UpdateError('Cannot establish current Web identity')
    old = inspect(api, own[0]['Id'])
    labels = old['Config'].get('Labels') or {}
    if not labels.get('com.docker.compose.project') or labels.get('com.docker.compose.service') not in ('wisp-web', 'wisp_web', 'web'):
        raise UpdateError('Current container is not a scoped Web service')
    if not old['State']['Running'] or old['State'].get('Health', {}).get('Status') != 'healthy':
        raise UpdateError('Current Web is not healthy')
    for mount in old.get('Mounts', []):
        dest = mount['Destination']
        if dest in ('/app', '/app/web') or any(dest == '/app/' + p or dest.startswith('/app/' + p + '/') for p in ('core', 'services', 'web/routes', 'database', 'web/templates')):
            raise UpdateError('Runtime code override requires a separately reviewed update')
    return old

def storage_mount(old, storage):
    matches = [v for v in old.get('Mounts', []) if v['Destination'] == storage and v.get('RW')]
    if len(matches) != 1 or matches[0]['Type'] not in ('bind', 'volume'):
        raise UpdateError('Persistent shared storage mount unavailable')
    m = matches[0]
    return {'Type': m['Type'], 'Source': m.get('Name') if m['Type'] == 'volume' else m['Source'],
            'Target': storage, 'ReadOnly': False}

def candidate_spec(old, image, endpoints):
    config = copy.deepcopy(old['Config'])
    # Inspect-only fields are not accepted by create. Preserve credentials privately.
    allowed = {'User','AttachStdin','AttachStdout','AttachStderr','ExposedPorts','Tty','OpenStdin','StdinOnce',
               'Env','Cmd','Healthcheck','ArgsEscaped','Volumes','WorkingDir','Entrypoint','NetworkDisabled',
               'MacAddress','OnBuild','Labels','StopSignal','StopTimeout','Shell'}
    config = {k: v for k, v in config.items() if k in allowed}
    config['Image'] = image
    config['Env'] = [v for v in config.get('Env', []) if not v.startswith('HOSTNAME=')]
    config['HostConfig'] = copy.deepcopy(old['HostConfig'])
    config['HostConfig'].pop('Binds', None)
    config['HostConfig']['Mounts'] = [
        {'Type': m['Type'], 'Source': m.get('Name') if m['Type'] == 'volume' else m['Source'],
         'Target': m['Destination'], 'ReadOnly': not m['RW']}
        for m in old.get('Mounts', []) if m['Type'] in ('bind', 'volume')]
    if any(m['Type'] not in ('bind','volume','tmpfs') for m in old.get('Mounts', [])):
        raise UpdateError('Unsupported mount type')
    config['HostConfig']['RestartPolicy'] = {'Name': 'no', 'MaximumRetryCount': 0}
    config['NetworkingConfig'] = {'EndpointsConfig': endpoints}
    return config

def endpoints_for(old):
    result = {}
    for name, v in old['NetworkSettings']['Networks'].items():
        if not v.get('IPAddress'):
            raise UpdateError('Explicit bridge network identity required')
        aliases = [a for a in v.get('Aliases') or [] if a not in (old['Id'], old['Id'][:12], old['Config'].get('Hostname'))]
        result[name] = {'IPAMConfig': {'IPv4Address': v['IPAddress']}, 'Aliases': aliases}
    if not result:
        raise UpdateError('No owned network endpoints')
    return result

def restore(api, plan, state):
    old = inspect(api, plan['old']['Id'])
    if old['Image'] != plan['old']['Image']:
        raise UpdateError('Rollback identity changed')
    new_id = state.get('new_id')
    if new_id:
        new = inspect(api, new_id)
        if new['Image'] != plan['target_id'] or new['Config'].get('Labels', {}).get('max.update.job') != plan['job']:
            raise UpdateError('Candidate identity changed')
        if new['State']['Running']:
            api('POST', '/containers/' + new_id + '/stop?t=20')
        api('POST', '/containers/' + new_id + '/update', {'RestartPolicy': {'Name': 'no'}})
        for net in new['NetworkSettings']['Networks']:
            api('POST', '/networks/' + urllib.parse.quote(net, safe='') + '/disconnect', {'Container': new_id, 'Force': True})
        role = plan.get('role', 'web')
        if role not in ('web', 'core'):
            raise UpdateError('Unsupported rollback role')
        api('POST', '/containers/' + new_id + '/rename?name=max_' + role + '_failed_update_' + plan['job'])
    # Never take a name from a container outside this round.
    fleet = api('GET', '/containers/json?all=1')
    occupants = [v['Id'] for v in fleet if plan['old']['Name'] in v.get('Names', [])]
    if any(i != old['Id'] for i in occupants):
        raise UpdateError('Another deployment owns the Web name')
    if old['Name'] != plan['old']['Name']:
        api('POST', '/containers/' + old['Id'] + '/rename?name=' + plan['old']['Name'].lstrip('/'))
    for net, endpoint in plan['endpoints'].items():
        if net not in inspect(api, old['Id'])['NetworkSettings']['Networks']:
            api('POST', '/networks/' + urllib.parse.quote(net, safe='') + '/connect', {'Container': old['Id'], 'EndpointConfig': endpoint})
    api('POST', '/containers/' + old['Id'] + '/update', {'RestartPolicy': plan['old']['HostConfig']['RestartPolicy']})
    if not inspect(api, old['Id'])['State']['Running']:
        api('POST', '/containers/' + old['Id'] + '/start')

# Executed inside the replacement Web: no app import, writes, fake traffic or lease overrides.
VERIFY = """import hashlib,json,pathlib,subprocess
from services.license_guard_service import get_active_license_status
from core.config import APP_VERSION
from database.db import query_one
p=subprocess.run(['python','/opt/max-protection/verify.py'],capture_output=True)
x=get_active_license_status(force_refresh=True)
y=query_one('SELECT is_valid,valid_until>UTC_TIMESTAMP() fresh FROM wisp_license_runtime_state WHERE id=1')
print(json.dumps({'verified':p.returncode==0,'key':hashlib.sha256(pathlib.Path('/opt/max-protection/runtime-public.pem').read_bytes()).hexdigest(),'version':APP_VERSION,'license':bool(x['valid']),'lease':bool(y and y['is_valid'] and y['fresh'])}))
"""

def exec_verify(api, ident, expected_key, expected_version):
    created = api('POST', '/containers/' + ident + '/exec', {'AttachStdout': True, 'AttachStderr': True, 'Tty': True, 'Cmd': ['python', '-c', VERIFY]})
    # TTY avoids Docker multiplex framing. The API transport returns JSON from stdout.
    value = api('POST', '/exec/' + created['Id'] + '/start', {'Detach': False, 'Tty': True}, timeout=35)
    exited = api('GET', '/exec/' + created['Id'] + '/json')
    return exited.get('ExitCode') == 0 and isinstance(value, dict) and all(value.get(k) for k in ('verified','license','lease')) and value.get('key') == expected_key and value.get('version') == expected_version

def run_job(folder, api=None, verify=None, sleep=time.sleep, clock=time.monotonic):
    import fcntl
    api = api or Docker()
    folder = Path(folder)
    with (folder / 'helper.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = json.loads((folder / 'plan.private.json').read_text())
        state_path = folder / 'state.json'
        state = json.loads(state_path.read_text())
        if state['status'] in TERMINAL:
            return state
        try:
            if state.get('switch_attempted'):
                raise UpdateError('Interrupted update requires rollback')
            current = inspect(api, plan['old']['Id'])
            if any(current.get(k) != plan['old'].get(k) for k in ('Config','HostConfig','Mounts')) or current['Name'] != plan['old']['Name'] or current['Image'] != plan['old']['Image'] or current['State']['StartedAt'] != plan['old']['State']['StartedAt']:
                raise UpdateError('Web identity changed since backup')
            state.update(status='running', stage='تبديل واجهة النظام والتحقق من جاهزيتها', percent=70, switch_attempted=True)
            write_json(state_path, state)
            old_id = current['Id']
            api('POST', '/containers/' + old_id + '/update', {'RestartPolicy': {'Name': 'no'}})
            api('POST', '/containers/' + old_id + '/stop?t=25')
            for net in plan['endpoints']:
                api('POST', '/networks/' + urllib.parse.quote(net, safe='') + '/disconnect', {'Container': old_id, 'Force': True})
            api('POST', '/containers/' + old_id + '/rename?name=max_web_prev_update_' + plan['job'])
            # Record create intent before issuing it: resume discovers a lost create response by exact name.
            state['create_attempted'] = True
            write_json(state_path, state)
            config = candidate_spec(current, plan['target_id'], plan['endpoints'])
            config.setdefault('Labels', {})['max.update.job'] = plan['job']
            new = api('POST', '/containers/create?name=' + plan['old']['Name'].lstrip('/'), config)
            state['new_id'] = new['Id']
            write_json(state_path, state)
            api('POST', '/containers/' + new['Id'] + '/start')
            deadline = clock() + 120
            good = False
            while clock() < deadline:
                candidate = inspect(api, new['Id'])
                if not candidate['State']['Running'] or candidate['State'].get('OOMKilled'):
                    break
                if candidate['State'].get('Health', {}).get('Status') == 'healthy':
                    good = (verify or exec_verify)(api, new['Id'], plan['key_hash'], plan['version'])
                    if good:
                        break
                sleep(2)
            if not good:
                raise UpdateError('Candidate health, integrity or license lease did not verify')
            api('POST', '/containers/' + new['Id'] + '/update', {'RestartPolicy': plan['old']['HostConfig']['RestartPolicy']})
            state.update(status='completed', percent=100, stage='اكتمل التحديث بعد التحقق من الواجهة والترخيص', message='تم حفظ النسخة السابقة للرجوع.', completed_at=time.time())
        except Exception as exc:
            state.update(status='error', percent=0, error='تعذر إكمال التحديث؛ راجع التقرير الخاص.', stage='فشل التحديث')
            write_json(folder / 'failure.private.json', {'exception': type(exc).__name__})
            if state.get('switch_attempted'):
                try:
                    if state.get('create_attempted') and not state.get('new_id'):
                        fleet = api('GET', '/containers/json?all=1')
                        ids = [v['Id'] for v in fleet if plan['old']['Name'] in v.get('Names', []) and v['Id'] != plan['old']['Id']]
                        if len(ids) > 1:
                            raise UpdateError('Ambiguous candidate after lost response')
                        if ids:
                            state['new_id'] = ids[0]
                    restore(api, plan, state)
                    state.update(status='rolled_back', stage='فشل التحديث وأعيدت الواجهة السابقة دون استعادة البيانات')
                except Exception:
                    state.update(status='recovery_required', stage='تعذر تأكيد الرجوع؛ يلزم فحص هوية التشغيل', error='لم يؤكد النظام استعادة الواجهة السابقة.')
        write_json(state_path, state)
        return state

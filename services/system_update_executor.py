"""Durable Web/Core switch for one explicitly enrolled Compose installation.

Runs in a helper from the old trusted image. No arbitrary programs, SQL migrations,
DB image updates, shared services, automatic retries or database restores.
"""
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.parse

from services.system_update_runtime import (Docker, UpdateError, candidate_spec, endpoints_for,
                                           inspect, restore, write_json)

PROBE = r'''import json,hashlib,pathlib,subprocess
from core.config import APP_VERSION
from services.license_guard_service import get_active_license_status
from database.db import query_one,query_all
x=get_active_license_status(force_refresh=True)
y=query_one('SELECT is_valid,valid_until>UTC_TIMESTAMP() fresh FROM wisp_license_runtime_state WHERE id=1')
rows=query_all('SELECT radacctid,acctinputoctets,acctoutputoctets,acctsessiontime,acctuniqueid FROM radacct ORDER BY radacctid DESC LIMIT 300')
v=subprocess.run(['python','/opt/max-protection/verify.py'],capture_output=True)
print(json.dumps({'version':APP_VERSION,'verified':v.returncode==0,'license':bool(x['valid']),
'lease':bool(y and y['is_valid'] and y['fresh']),
'publisher_key':hashlib.sha256(pathlib.Path('/opt/max-protection/runtime-public.pem').read_bytes()).hexdigest(),
'rows':rows},default=str))
'''
COMPARE = r'''import json,sys
from database.db import query_one
rows=json.loads(sys.argv[1]);good=True
for r in rows:
 x=query_one('SELECT radacctid,acctinputoctets,acctoutputoctets,acctsessiontime,acctuniqueid FROM radacct WHERE radacctid=%s',(r['radacctid'],))
 if not x or x['acctuniqueid']!=r['acctuniqueid'] or any(int(x[k] or 0)<int(r[k] or 0) for k in ('acctinputoctets','acctoutputoctets','acctsessiontime')):good=False;break
print(json.dumps({'preserved':good,'sample':len(rows)}))
'''
BACKUP = r'''import hashlib,json,tarfile,pathlib
from services.backup_service import create_backup,get_backup_filepath
r=create_backup(admin_username='SystemUpdate',notes='Mandatory signed update backup',dispatch_notifications=False)
assert isinstance(r,tuple) and len(r)==3 and r[0]
p=pathlib.Path(get_backup_filepath(r[2]['filename']));assert p.is_file() and p.stat().st_size
sql=False
with tarfile.open(p,'r:gz') as a:
 for m in a:
  if m.isfile():
   f=a.extractfile(m);n=0
   while True:
    b=f.read(1048576)
    if not b:break
    n+=len(b)
   if m.name.endswith('.sql') and n:sql=True
assert sql
with p.open('rb') as f:h=hashlib.file_digest(f,'sha256').hexdigest()
print(json.dumps({'verified':True,'filename':p.name,'bytes':p.stat().st_size,'sha256':h}))
'''


def execute(api, ident, code, args=(), timeout=120):
    value = api('POST', '/containers/'+ident+'/exec', {'AttachStdout': True, 'AttachStderr': True,
                'Tty': True, 'Cmd': ['python', '-c', code, *args]})
    result = api('POST', '/exec/'+value['Id']+'/start', {'Detach': False, 'Tty': True}, timeout=timeout)
    if api('GET', '/exec/'+value['Id']+'/json').get('ExitCode') != 0 or not isinstance(result, dict):
        raise UpdateError('Scoped runtime verification failed')
    return result


def owned_snapshot(api, config):
    project, names = config['project'], config['services']
    if not project or set(names) != {'web', 'core', 'db'} or len(set(names.values())) != 3:
        raise UpdateError('Installation ownership is not configured')
    fleet = api('GET', '/containers/json?all=1')
    items = {}
    for role, service in names.items():
        matches = [v['Id'] for v in fleet if (v.get('Labels') or {}).get('com.docker.compose.project') == project
                   and (v.get('Labels') or {}).get('com.docker.compose.service') == service
                   and v.get('State') == 'running']
        if len(matches) != 1:
            raise UpdateError('Ambiguous installation service')
        item = inspect(api, matches[0])
        if item['State'].get('Health', {}).get('Status') not in (None, 'healthy'):
            raise UpdateError('Installation service is not ready')
        if role != 'db':
            for m in item['Mounts']:
                d = m['Destination']
                if d == '/app' or any(d == '/app/'+p or d.startswith('/app/'+p+'/') for p in ('core','services','database','web/routes','web/templates')):
                    raise UpdateError('Application source overrides are not supported')
        items[role] = item
    return items


def prepare(api, config, response, current, folder):
    from services.system_update_channel import verify_response
    manifest = verify_response(response, config, current)
    if not manifest:
        raise UpdateError('No approved release')
    old = owned_snapshot(api, config)
    before = execute(api, old['web']['Id'], PROBE)
    if before['version'] != current or not all(before.get(k) for k in ('verified','license','lease')):
        raise UpdateError('Current runtime failed preflight')
    folder = Path(folder)
    backup = execute(api, old['web']['Id'], BACKUP, timeout=300)
    if backup.get('verified') is not True:
        raise UpdateError('Mandatory backup did not verify')
    write_json(folder/'backup.private.json', backup)
    images = {}
    for role, reference in manifest['images'].items():
        api('POST', '/images/create?fromImage='+urllib.parse.quote(reference, safe=''), timeout=600)
        image = api('GET', '/images/'+urllib.parse.quote(reference, safe='')+'/json')
        if reference not in image.get('RepoDigests', []):
            raise UpdateError('Registry digest did not verify')
        images[role] = image['Id']
    # Bind the plan to the immutable release and the exact pre-backup identities.
    again = owned_snapshot(api, config)
    if again != old:
        # Docker inspection contains dynamic health logs; compare stable fields only.
        for role in old:
            for key in ('Id','Image','Config','HostConfig','Mounts'):
                if again[role][key] != old[role][key]:
                    raise UpdateError('Installation changed during preparation')
            if again[role]['State']['StartedAt'] != old[role]['State']['StartedAt']:
                raise UpdateError('Installation restarted during preparation')
    plan = {'job': folder.name, 'old': old, 'images': images, 'references': manifest['images'],
            'version': manifest['version'], 'previous_version': current, 'publisher_key': before['publisher_key'],
            'accounting': before['rows'], 'release': manifest['release'], 'project': config['project']}
    if config.get('compose_path'):
        path = Path(config['compose_path'])
        if path.is_symlink() or not path.is_file():
            raise UpdateError('Compose definition is unavailable')
        import yaml
        document = yaml.safe_load(path.read_text(encoding='utf-8'))
        for role, service in config['services'].items():
            if service not in document['services']:
                raise UpdateError('Compose service is missing')
        plan.update(compose_path=str(path), compose_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    compose_services=config['services'])
        (folder/'compose-before.private').write_bytes(path.read_bytes())
        bridge = config.get('enrollment_socket')
        if bridge:
            if not Path(bridge).is_absolute() or Path(bridge).is_symlink() or not Path(bridge).is_dir():
                raise UpdateError('Invalid host enrollment socket directory')
            from services.system_update_channel import version
            if version(manifest['version']) < (2,6,4):
                raise UpdateError('Enrolling an older installation requires a control-capable release')
            existing = [m for m in old['web']['Mounts'] if m['Destination']=='/max-update-socket']
            if existing:
                if (len(existing)!=1 or existing[0]['Source']!=bridge or existing[0]['RW']
                        or 'MAX_UPDATE_SOCKET=/max-update-socket/controller.sock' not in old['web']['Config'].get('Env',[])):
                    raise UpdateError('Existing enrollment socket does not match trusted configuration')
            else:
                plan['enrollment_socket'] = bridge
    else:
        raise UpdateError('Persistent Compose definition is required')
    write_json(folder/'plan.private.json', plan)
    return plan


def wait_ready(api, ident, role, plan, rollback=False, sleep=time.sleep, clock=time.monotonic):
    end = clock()+150
    while clock() < end:
        value = inspect(api, ident)
        if not value['State']['Running'] or value['State'].get('OOMKilled'):
            raise UpdateError('Replacement stopped')
        if value['State'].get('Health', {}).get('Status') in (None, 'healthy'):
            try:
                if role == 'web':
                    result = execute(api, ident, PROBE)
                    expected = plan['previous_version'] if rollback else plan['version']
                    if (all(result.get(k) for k in ('verified','license','lease')) and result['version'] == expected
                            and result['publisher_key'] == plan['publisher_key']):
                        compare = execute(api, ident, COMPARE, [json.dumps(plan['accounting'])])
                        if compare.get('preserved') is not True:
                            raise UpdateError('Accounting checkpoint did not verify')
                        return
                else:
                    # Inspect the RADIUS listener as well as container/process health.
                    result = api('POST', '/containers/'+ident+'/exec', {'AttachStdout': True, 'AttachStderr': True,
                        'Tty': True, 'Cmd': ['sh','-c', 'grep -qi ":0714 " /proc/net/udp /proc/net/udp6 && grep -qi ":0715 " /proc/net/udp /proc/net/udp6']})
                    api('POST','/exec/'+result['Id']+'/start',{'Detach':False,'Tty':True})
                    if api('GET','/exec/'+result['Id']+'/json').get('ExitCode') == 0:
                        return
            except (UpdateError, OSError, KeyError, ValueError):
                pass
        sleep(2)
    raise UpdateError('Runtime health, trust, license or accounting did not verify')


def persist_compose(plan, folder, rollback=False):
    import yaml
    path = Path(plan['compose_path'])
    before = (folder/'compose-before.private').read_bytes()
    previous = hashlib.sha256(before).hexdigest()
    document = yaml.safe_load(before.decode('utf-8'))
    for role, reference in plan['references'].items():
        document['services'][plan['compose_services'][role]]['image'] = reference
    if plan.get('enrollment_socket'):
        web = document['services'][plan['compose_services']['web']]
        env = web.setdefault('environment', {})
        if isinstance(env, list): env = dict(v.split('=',1) for v in env)
        env['MAX_UPDATE_SOCKET'] = '/max-update-socket/controller.sock'
        web['environment'] = env
        volumes = web.setdefault('volumes', [])
        if any((v.split(':')[1] if isinstance(v,str) else v.get('target')) == '/max-update-socket' for v in volumes):
            raise UpdateError('Existing update socket requires explicit enrollment review')
        volumes.append(plan['enrollment_socket']+':/max-update-socket:ro')
    updated = yaml.safe_dump(document, sort_keys=False, allow_unicode=True).encode('utf-8')
    current = hashlib.sha256(path.read_bytes()).hexdigest()
    if current not in (previous, hashlib.sha256(updated).hexdigest()):
        raise UpdateError('Compose was changed by another operator')
    data = before if rollback else updated
    temp = path.with_name('.'+path.name+'.update-'+plan['job'])
    with temp.open('wb') as out:
        os.chmod(temp, 0o600); out.write(data); out.flush(); os.fsync(out.fileno())
    os.replace(temp, path)


def run_job(folder, api=None, wait=None):
    import fcntl
    api, folder = api or Docker(), Path(folder)
    wait = wait or wait_ready
    with (folder/'execution.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = json.loads((folder/'plan.private.json').read_text(encoding='utf-8'))
        state_path = folder/'state.json'
        state = json.loads(state_path.read_text(encoding='utf-8'))
        if state['status'] in ('completed','rolled_back','error','recovery_required'):
            return state
        state.setdefault('services', {})
        try:
            if state.get('switch_attempted'):
                raise UpdateError('Interrupted update must recover before any new install')
            if set(plan['images'])-{'web','core'} or 'web' not in plan['images']:
                raise UpdateError('Only scoped Web/Core updates are supported')
            for role, frozen in plan['old'].items():
                actual = inspect(api, frozen['Id'])
                if any(actual[k] != frozen[k] for k in ('Id','Image','Name','Config','HostConfig','Mounts')) or actual['State']['StartedAt'] != frozen['State']['StartedAt']:
                    raise UpdateError('Service ownership changed before switch')
            # Resolve networks and reject unknown storage before touching any service.
            specs = {r: candidate_spec(plan['old'][r], image, endpoints_for(plan['old'][r])) for r,image in plan['images'].items()}
            if plan.get('enrollment_socket'):
                spec = specs['web']
                if any(m['Target']=='/max-update-socket' for m in spec['HostConfig']['Mounts']):
                    raise UpdateError('Refusing to override an existing update socket')
                spec['HostConfig']['Mounts'].append({'Type':'bind','Source':plan['enrollment_socket'],
                                                    'Target':'/max-update-socket','ReadOnly':True})
                spec['Env']=[v for v in spec.get('Env',[]) if not v.startswith('MAX_UPDATE_SOCKET=')]
                spec['Env'].append('MAX_UPDATE_SOCKET=/max-update-socket/controller.sock')
            for role in specs:
                specs[role].setdefault('Labels', {})['max.update.job'] = plan['job']
            state.update(switch_attempted=True, status='running', percent=60, stage='تبديل المكونات المعتمدة')
            write_json(state_path, state)
            for role in ('web','core'):
                if role not in specs: continue
                old = plan['old'][role]; record = state['services'].setdefault(role, {})
                record['touched'] = True; write_json(state_path, state)
                api('POST','/containers/'+old['Id']+'/update',{'RestartPolicy':{'Name':'no'}})
                api('POST','/containers/'+old['Id']+'/stop?t=25')
                for net in old['NetworkSettings']['Networks']:
                    api('POST','/networks/'+urllib.parse.quote(net,safe='')+'/disconnect',{'Container':old['Id'],'Force':True})
                api('POST','/containers/'+old['Id']+'/rename?name='+old['Name'].lstrip('/')+'_previous_'+plan['job'])
            for role in ('core','web'):
                if role not in specs: continue
                old = plan['old'][role]; record = state['services'][role]
                record['create_attempted'] = True; write_json(state_path,state)
                new = api('POST','/containers/create?name='+old['Name'].lstrip('/'),specs[role])
                record['new_id'] = new['Id']; write_json(state_path,state)
                api('POST','/containers/'+new['Id']+'/start')
                wait(api,new['Id'],role,plan)
                api('POST','/containers/'+new['Id']+'/update',{'RestartPolicy':old['HostConfig']['RestartPolicy']})
            wait(api,state['services']['web']['new_id'],'web',plan)
            database = inspect(api,plan['old']['db']['Id'])
            if database['Image'] != plan['old']['db']['Image'] or database['State']['StartedAt'] != plan['old']['db']['State']['StartedAt']:
                raise UpdateError('Database identity changed')
            persist_compose(plan,folder)
            state.update(status='completed',percent=100,stage='اكتمل التحديث بعد التحقق من التشغيل والمحاسبة',completed_at=time.time())
        except Exception as exc:
            write_json(folder/'failure.private.json',{'type':type(exc).__name__})
            if not state.get('switch_attempted'):
                state.update(status='error',percent=0,stage='توقف التحديث قبل التبديل')
            else:
                try:
                    for role in ('core','web'):
                        record = state['services'].get(role,{})
                        if not record.get('touched'): continue
                        old = plan['old'][role]
                        if record.get('create_attempted') and not record.get('new_id'):
                            fleet = api('GET','/containers/json?all=1')
                            found = [v['Id'] for v in fleet if old['Name'] in v.get('Names',[]) and v['Id'] != old['Id']]
                            if len(found)>1: raise UpdateError('Ambiguous create response')
                            if found: record['new_id']=found[0]
                        restore(api,{'job':plan['job'],'role':role,'old':old,'target_id':plan['images'][role],
                                     'endpoints':endpoints_for(old)},record)
                        wait(api,old['Id'],role,plan,rollback=True)
                    persist_compose(plan,folder,rollback=True)
                    state.update(status='rolled_back',percent=0,stage='أعيد الإصدار السابق وتحقق تشغيله مع حفظ أحدث المحاسبة')
                except Exception:
                    state.update(status='recovery_required',percent=0,stage='يلزم تحقق الاسترداد؛ لا تعِد التحديث')
        write_json(state_path,state)
        return state

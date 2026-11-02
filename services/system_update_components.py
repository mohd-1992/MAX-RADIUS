"""Opt-in signed component executor. No application imports or implicit enrollment.

Registry and journal must be host-owned, outside tenant storage. This candidate is
not advertised by the v1 tenant updater until bootstrap/distribution acceptance.
No shell/SQL from a release, DB restore, major DB downgrade or arbitrary mounts.
"""
import base64
from contextlib import ExitStack, contextmanager
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import time
import urllib.parse

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from services.system_update_runtime import Docker, UpdateError, candidate_spec, endpoints_for, inspect, write_json, storage_mount

PROTOCOL = 4
ROLES = {'web', 'core', 'db', 'l2tp', 'autoheal'}
KEY = re.compile(r'[a-z][a-z0-9_-]{0,47}\Z')
REFERENCE = re.compile(r'[a-z0-9][a-z0-9./:_-]*@sha256:[a-f0-9]{64}\Z')
DB_OPTIONS = {'aria-pagecache-buffer-size', 'key-buffer-size', 'innodb-buffer-pool-size'}
TERMINAL = {'completed', 'rolled_back', 'error', 'recovery_required'}
IDENTITY = '''import hashlib,json
from core.hardware_fingerprint import get_machine_id,get_instance_uuid
from core.license_protocol import instance_public_key
from core.config import STORAGE_DIR,SECRET_KEY
values={'machine':get_machine_id(),'instance':get_instance_uuid(),'license_key':instance_public_key(STORAGE_DIR),'web_secret':str(SECRET_KEY)}
print(json.dumps({k:hashlib.sha256(v.encode()).hexdigest() for k,v in values.items()}))
'''


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def require(condition, message):
    if not condition:
        raise UpdateError(message)


def registry_revision(registry):
    return hashlib.sha256(canonical(registry)).hexdigest()


def validate_registry(registry):
    require(set(registry) == {'schema', 'authority', 'identity', 'components', 'tenants'}, 'Invalid component registry')
    require(registry['schema'] == 1 and registry['authority'] in ('installation', 'host'), 'Invalid registry authority')
    require(isinstance(registry['identity'], str) and 0 < len(registry['identity']) <= 128, 'Invalid registry identity')
    components, tenants = registry['components'], registry['tenants']
    require(isinstance(components, dict) and 0 < len(components) <= 100, 'Invalid component count')
    require(isinstance(tenants, dict) and 0 < len(tenants) <= 50, 'Invalid tenant registry')
    labels = set()
    for key, item in components.items():
        require(KEY.fullmatch(key) and set(item) == {'role', 'project', 'service', 'compose_path', 'images'}, 'Invalid registered component')
        require(item['role'] in ROLES and KEY.fullmatch(item['project']) and KEY.fullmatch(item['service']), 'Invalid registered ownership')
        pair = (item['project'], item['service'])
        require(pair not in labels, 'A component is registered twice')
        labels.add(pair)
        require(Path(item['compose_path']).is_absolute(), 'Compose path must be host provisioned')
        require(isinstance(item['images'], dict) and item['images'], 'Missing trusted image compatibility')
        for ref, compatibility in item['images'].items():
            require(REFERENCE.fullmatch(ref) and isinstance(compatibility, str) and KEY.fullmatch(compatibility), 'Invalid pinned image contract')
    for tenant, item in tenants.items():
        require(KEY.fullmatch(tenant) and set(item) == {'web', 'core', 'db', 'dependencies', 'controller_root'}, 'Invalid tenant dependency registry')
        require(Path(item['controller_root']).is_absolute(), 'Trusted tenant controller root is required')
        require(isinstance(item['dependencies'], list) and len(set(item['dependencies'])) == len(item['dependencies']), 'Invalid dependencies')
        for role in ('web', 'core', 'db'):
            require(item[role] in components and components[item[role]]['role'] == role, 'Tenant role mismatch')
        require(set(item['dependencies']) <= set(components), 'Unknown tenant dependency')
        require({item['web'], item['core'], item['db']} <= set(item['dependencies']), 'Incomplete tenant dependencies')
    require(set(components) == {key for tenant in tenants.values() for key in tenant['dependencies']}, 'Orphan registered component')
    if registry['authority'] == 'installation':
        require(len(tenants) == 1, 'Installation cannot coordinate other tenants')
    return registry


def affected_components(registry, changes):
    affected = set(changes)
    participants = set()
    while True:
        previous = (set(affected), set(participants))
        participants.update(t for t, cfg in registry['tenants'].items() if set(cfg['dependencies']) & affected)
        for tenant in participants:
            cfg = registry['tenants'][tenant]
            # Quiesce its monitor even for a Web-only replacement.
            affected.update(k for k in cfg['dependencies'] if registry['components'][k]['role'] == 'autoheal')
            if any(registry['components'][k]['role'] in ('db', 'l2tp', 'core') for k in set(cfg['dependencies']) & set(changes)):
                affected.update((cfg['web'], cfg['core']))
        if previous == (affected, participants):
            return affected, sorted(participants)


@contextmanager
def tenant_barriers(registry, participants):
    """Interoperate with v1 dispatch locks; never overlap an existing helper."""
    import fcntl
    with ExitStack() as stack:
        roots = sorted({registry['tenants'][t]['controller_root'] for t in participants})
        for name in roots:
            root = Path(name)
            require(root.is_dir() and not root.is_symlink(), 'Tenant update root unavailable')
            stream = stack.enter_context((root/'dispatch.lock').open('a'))
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            pointer = root/'current.private.json'
            if pointer.exists():
                require(not pointer.is_symlink(), 'Invalid legacy job pointer')
                job = json.loads(pointer.read_text(encoding='utf-8'))['job']
                require(re.fullmatch('[0-9a-f]{32}', job), 'Invalid legacy job identity')
                state = root/'jobs'/job/'state.json'
                require(state.is_file() and not state.is_symlink(), 'Legacy job state unavailable')
                require(json.loads(state.read_text(encoding='utf-8'))['status'] in ('completed', 'rolled_back'), 'Legacy helper still active or unverified')
        yield


def verify_bundle(envelope, trust, registry, now=None):
    validate_registry(registry)
    require(set(envelope) == {'bundle', 'signature'}, 'Invalid signed component envelope')
    key = base64.b64decode(trust['public_key'], validate=True)
    require(hashlib.sha256(key).hexdigest() == trust['key_fingerprint'], 'Pinned component trust mismatch')
    value = envelope['bundle']
    Ed25519PublicKey.from_public_bytes(key).verify(base64.b64decode(envelope['signature'], validate=True), canonical(value))
    require(set(value) == {'schema', 'protocol', 'release', 'audience', 'registry_revision', 'issued_at', 'valid_until', 'participants', 'changes', 'data_policy', 'application_versions'}, 'Invalid component contract')
    require(value['schema'] == 1 and value['protocol'] == PROTOCOL, 'Unsupported component protocol')
    require(isinstance(value['release'], str) and KEY.fullmatch(value['release']), 'Invalid release identity')
    require(value['audience'] == {'authority': registry['authority'], 'identity': registry['identity']}, 'Component audience mismatch')
    require(value['registry_revision'] == registry_revision(registry), 'Dependency registry changed')
    issued, expires = (dt.datetime.fromisoformat(value[k]) for k in ('issued_at', 'valid_until'))
    now = now or dt.datetime.now(dt.timezone.utc)
    require(issued.tzinfo is not None and expires.tzinfo is not None and issued <= now < expires and 0 < (expires-issued).total_seconds() <= 300, 'Expired component authorization')
    require(value['data_policy'] == 'preserve-compatible-no-restore', 'Unsupported data recovery policy')
    changes = value['changes']
    require(isinstance(changes, dict) and changes and set(changes) <= set(registry['components']), 'Unknown component change')
    _, affected = affected_components(registry, changes)
    require(value['participants'] == affected, 'All affected tenants must participate')
    versions = value['application_versions']
    require(isinstance(versions, dict) and set(versions) == {k for k, c in changes.items() if registry['components'][k]['role'] == 'web' and 'image' in c}, 'Web image changes need explicit application versions')
    require(all(isinstance(v, str) and re.fullmatch(r'\d+\.\d+\.\d+', v) for v in versions.values()), 'Invalid expected application version')
    for component, change in changes.items():
        item = registry['components'][component]
        require(isinstance(change, dict) and change and set(change) <= {'image', 'environment', 'mariadb_options'}, 'Unsupported component operation')
        if 'image' in change:
            require(change['image'] in item['images'], 'Image has no host-approved compatibility contract')
        if 'environment' in change:
            require(item['role'] == 'db' and change['environment'] == {'MALLOC_ARENA_MAX': '2'}, 'Unapproved environment change')
        if 'mariadb_options' in change:
            options = change['mariadb_options']
            require(item['role'] == 'db' and isinstance(options, dict) and options and set(options) <= DB_OPTIONS, 'Unapproved MariaDB option')
            require(all(type(v) is int and 8*1048576 <= v <= 1024*1048576 for v in options.values()), 'MariaDB option outside bounds')
    return value


def snapshot(api, registry):
    fleet, result = api('GET', '/containers/json?all=1'), {}
    for key, item in registry['components'].items():
        matches = [r for r in fleet if r.get('State') == 'running' and (r.get('Labels') or {}).get('com.docker.compose.project') == item['project'] and (r.get('Labels') or {}).get('com.docker.compose.service') == item['service']]
        require(len(matches) == 1, 'Ambiguous registered component')
        row = inspect(api, matches[0]['Id'])
        require(row['State']['Running'] and not row['State'].get('OOMKilled'), 'Registered component is not running')
        require(row['State'].get('Health', {}).get('Status') in (None, 'healthy'), 'Registered component is not healthy')
        if item['role'] in ('web', 'core'):
            for mount in row['Mounts']:
                dest = mount['Destination']
                require(not (dest in ('/app', '/app/web') or any(dest == '/app/'+p or dest.startswith('/app/'+p+'/') for p in ('core', 'services', 'database', 'web/routes', 'web/templates'))), 'Runtime application overrides are not supported')
        if item['role'] == 'web':
            env = dict(v.split('=', 1) for v in row['Config'].get('Env', []) if '=' in v)
            storage = env.get('STORAGE_DIR', '/app/storage')
            storage_mount(row, storage)
            # hardware_fingerprint uses /app/storage unless INSTANCE_UUID is
            # explicitly provisioned, independently of configurable key storage.
            if storage != '/app/storage' and not env.get('INSTANCE_UUID'):
                storage_mount(row, '/app/storage')
        candidate_spec(row, row['Image'], endpoints_for(row))
        result[key] = row
    return result


def stable(row):
    return {k: row[k] for k in ('Id', 'Image', 'Name', 'Config', 'HostConfig', 'Mounts', 'NetworkSettings')} | {'StartedAt': row['State']['StartedAt']}


def change_spec(old, image, change):
    spec = candidate_spec(old, image, endpoints_for(old))
    if 'environment' in change:
        values = change['environment']
        spec['Env'] = [v for v in spec.get('Env', []) if v.split('=', 1)[0] not in values] + [k+'='+v for k, v in values.items()]
    if 'mariadb_options' in change:
        options = change['mariadb_options']
        command = spec.get('Cmd') or []
        require(isinstance(command, list) and all(isinstance(v, str) for v in command), 'MariaDB command is not resolved')
        # Refuse a conflicting short/underscore option rather than guessing.
        for value in command:
            if value.startswith('--') and value[2:].split('=', 1)[0].replace('_', '-') in options:
                require('=' in value, 'Split MariaDB arguments require enrollment normalization')
        command = [v for v in command if not (v.startswith('--') and v[2:].split('=', 1)[0].replace('_', '-') in options)]
        spec['Cmd'] = command + ['--'+k+'='+str(v) for k, v in sorted(options.items())]
    return spec


def default_probe(api, ident, role, context):
    """Fixed probes only; never execute a command from a manifest."""
    from services.system_update_executor import execute, PROBE, COMPARE
    if role == 'web':
        result = execute(api, ident, PROBE)
        require(all(result.get(k) for k in ('verified', 'license', 'lease')), 'Web trust or license not verified')
        result['identity'] = execute(api, ident, IDENTITY)
        if context.get('baseline'):
            before = context['baseline']
            require(result['publisher_key'] == before['publisher_key'], 'Publisher changed')
            require(result['identity'] == before.get('identity'), 'Installation identity or license key changed')
            require(result['version'] == context.get('expected_version', before['version']), 'Unexpected application version')
            require(execute(api, ident, COMPARE, [json.dumps(before['rows'])]).get('preserved') is True, 'Accounting checkpoint failed')
        return result
    if role == 'db':
        # ping can report an alive server even when credentials are rejected.
        # Require an authenticated SQL round trip, with no password in argv.
        command = ['sh', '-ec', 'secret="${MARIADB_ROOT_PASSWORD:-${MYSQL_ROOT_PASSWORD:-}}"; '
                   'file="${MARIADB_ROOT_PASSWORD_FILE:-${MYSQL_ROOT_PASSWORD_FILE:-}}"; '
                   'if [ -n "$file" ]; then secret="$(cat "$file")"; fi; '
                   'MYSQL_PWD="$secret" exec mariadb --protocol=socket --connect-timeout=3 '
                   '-uroot --batch --skip-column-names -e "SELECT 1" >/dev/null 2>&1']
    elif role == 'core':
        command = ['sh', '-ec', 'grep -qi ":0714 " /proc/net/udp /proc/net/udp6 && grep -qi ":0715 " /proc/net/udp /proc/net/udp6']
    else:
        # Transport/watchdog need an installed image health contract, not PID1 alone.
        row = inspect(api, ident)
        require(bool(row['Config'].get('Healthcheck')) and row['State'].get('Health', {}).get('Status') == 'healthy', 'Component health contract unavailable')
        return {'verified': True}
    task = api('POST', '/containers/'+ident+'/exec', {'AttachStdout': True, 'AttachStderr': True, 'Tty': True, 'Cmd': command})
    api('POST', '/exec/'+task['Id']+'/start', {'Detach': False, 'Tty': True})
    require(api('GET', '/exec/'+task['Id']+'/json').get('ExitCode') == 0, 'Component native probe failed')
    return {'verified': True}


def wait_probe(api, ident, role, context, probe):
    deadline = time.monotonic()+120
    while True:
        row = inspect(api, ident)
        require(row['State']['Running'] and not row['State'].get('OOMKilled'), 'Replacement stopped')
        try:
            if row['State'].get('Health', {}).get('Status') in (None, 'healthy'):
                return probe(api, ident, role, context)
        except (UpdateError, OSError, ValueError, KeyError):
            pass
        require(time.monotonic() < deadline, 'Native component verification timed out')
        time.sleep(1)


def compose_documents(registry, old, specs):
    import yaml
    documents = {}
    for key, spec in specs.items():
        item = registry['components'][key]
        path = Path(item['compose_path'])
        require(path.is_file() and not path.is_symlink(), 'Persistent Compose unavailable')
        if str(path) not in documents:
            raw = path.read_bytes()
            documents[str(path)] = {'before': raw.decode('utf-8'), 'document': yaml.safe_load(raw)}
        service = documents[str(path)]['document']['services'][item['service']]
        change = spec['change']
        if 'image' in change:
            service['image'] = change['image']
        if 'environment' in change:
            env = service.get('environment') or {}
            if isinstance(env, list):
                require(all('=' in v for v in env), 'Compose environment must be resolved')
                env = dict(v.split('=', 1) for v in env)
            service['environment'] = dict(env, **change['environment'])
        if 'mariadb_options' in change:
            service['command'] = spec['spec']['Cmd']
    for data in documents.values():
        data['after'] = yaml.safe_dump(data.pop('document'), sort_keys=False, allow_unicode=True)
    return documents


def persist_documents(documents, rollback=False):
    for name, data in documents.items():
        path = Path(name)
        require(not path.is_symlink() and path.read_bytes().decode('utf-8') in (data['before'], data['after']), 'Compose modified by another operator')
        temp = path.with_name('.'+path.name+'.components.tmp')
        with temp.open('w', encoding='utf-8', newline='') as stream:
            os.chmod(temp, 0o600)
            stream.write(data['before'] if rollback else data['after'])
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, path)


def prepare(api, registry, bundle, job, probe=default_probe):
    from services.system_update_executor import execute, BACKUP
    old = snapshot(api, registry)
    changes, participants = bundle['changes'], bundle['participants']
    for key in changes:
        if registry['components'][key]['role'] == 'db':
            scope = (old[key]['Config'].get('Labels') or {}).get('max.db.scope')
            require(scope in ('dedicated', 'shared'), 'DB management scope requires explicit host enrollment')
            require(scope != 'shared' or registry['authority'] == 'host', 'Tenant cannot mutate a shared engine')
    # Restart clients of a DB/transport/core so stale connection pools cannot hide failure.
    affected, _ = affected_components(registry, changes)
    # The retained deployment must be verifiable BEFORE stopping anything.
    # Otherwise an installed transport lacking a health contract can fail both
    # candidate verification and rollback only after services are interrupted.
    for component in sorted(affected):
        role = registry['components'][component]['role']
        if role != 'web':
            probe(api, old[component]['Id'], role, {})
    baseline, backups = {}, {}
    for tenant in participants:
        web = registry['tenants'][tenant]['web']
        baseline[tenant] = probe(api, old[web]['Id'], 'web', {})
        backups[tenant] = execute(api, old[web]['Id'], BACKUP, timeout=300)
        require(backups[tenant].get('verified') is True and backups[tenant].get('sha256'), 'Every affected tenant requires verified backup')
    specs = {}
    for key in sorted(affected):
        change = changes.get(key, {})
        item = registry['components'][key]
        target = old[key]['Image']
        if 'image' in change:
            reference = change['image']
            current_image = api('GET', '/images/'+urllib.parse.quote(target, safe='')+'/json')
            contracts = {item['images'][r] for r in current_image.get('RepoDigests', []) if r in item['images']}
            require(len(contracts) == 1 and item['images'][reference] in contracts, 'No compatible rollback against current data')
            api('POST', '/images/create?fromImage='+urllib.parse.quote(reference, safe=''), timeout=600)
            image = api('GET', '/images/'+urllib.parse.quote(reference, safe='')+'/json')
            require(reference in image.get('RepoDigests', []), 'Pulled digest mismatch')
            target = image['Id']
        if item['role'] == 'db':
            mounts = [m for m in old[key]['Mounts'] if m['Destination'] == '/var/lib/mysql' and m.get('RW')]
            require(len(mounts) == 1 and mounts[0]['Type'] in ('bind', 'volume'), 'Database persistent data mount unavailable')
        spec = change_spec(old[key], target, change)
        spec.setdefault('Labels', {})['max.update.job'] = job
        spec['Labels']['max.update.component'] = key
        specs[key] = {'spec': spec, 'image': target, 'change': change}
    again = snapshot(api, registry)
    require(all(stable(old[k]) == stable(again[k]) for k in old), 'Deployment changed during backup or preparation')
    rank = {'db': 0, 'l2tp': 1, 'core': 2, 'web': 3, 'autoheal': 4}
    order = sorted(affected, key=lambda k: (rank[registry['components'][k]['role']], k))
    return {'job': job, 'registry': registry, 'registry_revision': registry_revision(registry), 'bundle': bundle,
            'old': old, 'specs': specs, 'order': order, 'baseline': baseline, 'backups': backups,
            'documents': compose_documents(registry, old, specs)}


def verify_running_components(plan, state, api, probe, rollback=False):
    old, registry = plan['old'], plan['registry']
    for key in plan['order']:
        ident = old[key]['Id'] if rollback else state['records'][key].get('new_id', old[key]['Id'])
        role = registry['components'][key]['role']
        if role != 'web':
            wait_probe(api, ident, role, {}, probe)
    for tenant in plan['bundle']['participants']:
        key = registry['tenants'][tenant]['web']
        ident = old[key]['Id'] if rollback else state['records'].get(key, {}).get('new_id', old[key]['Id'])
        context = {'baseline': plan['baseline'][tenant]}
        if not rollback and key in plan['bundle']['application_versions']:
            context['expected_version'] = plan['bundle']['application_versions'][key]
        wait_probe(api, ident, 'web', context, probe)


def rollback_components(plan, state, api, probe, save):
    registry, order, old = plan['registry'], plan['order'], plan['old']
    # Quiesce ALL candidates before bringing old DB/clients back.
    for key in reversed(order):
        record = state['records'].get(key, {})
        if record.get('create_attempted') and not record.get('new_id'):
            matches = [r for r in api('GET', '/containers/json?all=1') if old[key]['Name'] in r.get('Names', []) and (r.get('Labels') or {}).get('max.update.job') == plan['job'] and (r.get('Labels') or {}).get('max.update.component') == key]
            require(len(matches) <= 1, 'Ambiguous component create')
            if matches:
                record['new_id'] = matches[0]['Id']; save()
        if record.get('new_id'):
            row = inspect(api, record['new_id'])
            require(row['Image'] == plan['specs'][key]['image'] and row['Config']['Labels'].get('max.update.job') == plan['job'] and row['Config']['Labels'].get('max.update.component') == key, 'Candidate ownership changed')
            if row['State']['Running']:
                api('POST', '/containers/'+row['Id']+'/stop?t=30', timeout=45)
            api('POST', '/containers/'+row['Id']+'/update', {'RestartPolicy': {'Name': 'no'}})
            # A lost create can leave a never-started container:
            # Docker then has a network name but no allocated IP.
            # It needs no endpoint validation/reconnect contract.
            if row['HostConfig'].get('NetworkMode') not in ('none', 'host'):
                for net, endpoint in row['NetworkSettings']['Networks'].items():
                    if endpoint.get('EndpointID') or endpoint.get('IPAddress'):
                        api('POST', '/networks/'+urllib.parse.quote(net, safe='')+'/disconnect', {'Container': row['Id'], 'Force': True})
            api('POST', '/containers/'+row['Id']+'/rename?name=max_failed_'+key+'_'+plan['job'])
    for key in order:
        if not state['records'].get(key, {}).get('touched'):
            continue
        row = inspect(api, old[key]['Id'])
        require(row['Image'] == old[key]['Image'] and row['Mounts'] == old[key]['Mounts'] and row['Config'] == old[key]['Config'], 'Original component changed')
        occupants = [r for r in api('GET', '/containers/json?all=1') if old[key]['Name'] in r.get('Names', [])]
        require(all(r['Id'] == row['Id'] for r in occupants), 'Component name has a foreign owner')
        if row['Name'] != old[key]['Name']:
            api('POST', '/containers/'+row['Id']+'/rename?name='+old[key]['Name'].lstrip('/'))
        for net, endpoint in endpoints_for(old[key]).items():
            if net not in row['NetworkSettings']['Networks']:
                api('POST', '/networks/'+urllib.parse.quote(net, safe='')+'/connect', {'Container': row['Id'], 'EndpointConfig': endpoint})
        api('POST', '/containers/'+row['Id']+'/update', {'RestartPolicy': old[key]['HostConfig']['RestartPolicy']})
        if not row['State']['Running']:
            api('POST', '/containers/'+row['Id']+'/start')
        if registry['components'][key]['role'] != 'web':
            wait_probe(api, row['Id'], registry['components'][key]['role'], {}, probe)
    persist_documents(plan['documents'], rollback=True)
    verify_running_components(plan, state, api, probe, rollback=True)
    state.update(status='rolled_back', message='Previous configuration verified; latest data retained without restore')


def run(folder, api=None, probe=default_probe):
    """One durable attempt. Unknown interruption is not silently replayed."""
    import fcntl
    api, folder = api or Docker(), Path(folder)
    with (folder/'execution.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = json.loads((folder/'plan.private.json').read_text(encoding='utf-8'))
        state_path = folder/'state.json'
        state = json.loads(state_path.read_text(encoding='utf-8'))
        if state['status'] in TERMINAL:
            return state
        if state.get('switch_attempted'):
            state.update(status='recovery_required', message='Interrupted component job; do not replay')
            write_json(state_path, state); return state
        state['records'] = {}
        registry, order, old = plan['registry'], plan['order'], plan['old']
        def save():
            write_json(state_path, state)
        def check_all(rollback=False):
            return verify_running_components(plan, state, api, probe, rollback)
        try:
            actual = snapshot(api, registry)
            require(all(stable(actual[k]) == stable(old[k]) for k in old), 'Identity changed before component switch')
            persist_documents(plan['documents'], rollback=True)  # CAS before any stop.
            state.update(status='running', switch_attempted=True)
            save()
            for key in reversed(order):
                row = old[key]
                state['records'][key] = {'touched': True}; save()
                api('POST', '/containers/'+row['Id']+'/update', {'RestartPolicy': {'Name': 'no'}})
                # Docker may use the entire grace period before returning 204.
                # The HTTP deadline must exceed it, including in rollback.
                api('POST', '/containers/'+row['Id']+'/stop?t=30', timeout=45)
                for net in endpoints_for(row):
                    api('POST', '/networks/'+urllib.parse.quote(net, safe='')+'/disconnect', {'Container': row['Id'], 'Force': True})
                api('POST', '/containers/'+row['Id']+'/rename?name='+row['Name'].lstrip('/')+'_previous_'+plan['job'])
            for key in order:
                record = state['records'][key]
                record['create_attempted'] = True; save()
                spec = plan['specs'][key]['spec']
                ident = api('POST', '/containers/create?name='+old[key]['Name'].lstrip('/'), spec)['Id']
                record['new_id'] = ident; save()
                api('POST', '/containers/'+ident+'/start')
                # Dependencies are started first; Web is checked after all clients start.
                if registry['components'][key]['role'] != 'web':
                    wait_probe(api, ident, registry['components'][key]['role'], {}, probe)
            check_all()
            for key in order:
                api('POST', '/containers/'+state['records'][key]['new_id']+'/update', {'RestartPolicy': old[key]['HostConfig']['RestartPolicy']})
            persist_documents(plan['documents'])
            state.update(status='completed', message='All affected components and tenant checkpoints verified')
        except Exception as exc:
            write_json(folder/'failure.private.json', {'type': type(exc).__name__,
                'reason': str(exc) if isinstance(exc, UpdateError) else 'Scoped operation failed'})
            if not state.get('switch_attempted'):
                state.update(status='error', message='Preflight failed; no component switched')
            else:
                try:
                    rollback_components(plan, state, api, probe, save)
                except Exception as recovery_error:
                    write_json(folder/'recovery-failure.private.json', {'type': type(recovery_error).__name__,
                        'reason': str(recovery_error) if isinstance(recovery_error, UpdateError) else 'Scoped recovery failed'})
                    state.update(status='recovery_required', message='Recovery unverified; no automatic replay or database restore')
        save()
        return state


def recover(root, job, api=None, probe=default_probe):
    """One verified rollback of the current journal; never replay update or restore SQL.

    Only a host-owned controller may call this. Locks reject a still-live worker.
    Expiry authorizes a new switch, not recovery of an already signed attempt.
    """
    import fcntl
    root, api = Path(root), api or Docker()
    require(root.is_absolute() and root.is_dir() and not root.is_symlink(), 'Invalid recovery root')
    require(isinstance(job, str) and re.fullmatch('[a-f0-9]{32}', job), 'Invalid recovery job')
    def read(path):
        require(path.is_file() and not path.is_symlink(), 'Invalid recovery journal')
        return json.loads(path.read_text(encoding='utf-8'))
    with (root/'components.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require(read(root/'component-current.private.json') == {'job': job}, 'Recovery is not current')
        folder = root/'component-jobs'/job
        require(folder.is_dir() and not folder.is_symlink(), 'Invalid recovery folder')
        with (folder/'execution.lock').open('a') as worker_lock:
            fcntl.flock(worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = read(folder/'state.json')
            require(state.get('job') == job, 'Recovery state mismatch')
            if state['status'] in ('completed', 'rolled_back', 'error') or state.get('recovery_attempted'):
                return state
            if not (folder/'plan.private.json').exists():
                require(not state.get('switch_attempted'), 'Missing switched plan')
                state.update(status='error', message='Interrupted preparation; no replay and no component switch')
                write_json(folder/'state.json', state)
                return state
            plan = read(folder/'plan.private.json')
            registry = validate_registry(read(root/'component-registry.private.json'))
            require(plan['job'] == job and plan['registry'] == registry and
                    plan['registry_revision'] == registry_revision(registry), 'Recovery enrollment changed')
            envelope = read(folder/'request.private.json')
            trust = read(root/'component-trust.private.json')
            issued = dt.datetime.fromisoformat(envelope['bundle']['issued_at'])
            bundle = verify_bundle(envelope, trust, registry, now=issued)
            require(bundle == plan['bundle'], 'Recovery signed request changed')
            require(set(state.get('records', {})) <= set(plan['order']), 'Unexpected recovery records')
            state.setdefault('records', {})
            with tenant_barriers(registry, bundle['participants']):
                def save():
                    write_json(folder/'state.json', state)
                state['recovery_attempted'] = True
                save()
                try:
                    rollback_components(plan, state, api, probe, save)
                except Exception as exc:
                    write_json(folder/'recovery-failure.private.json', {'type': type(exc).__name__})
                    state.update(status='recovery_required', message='Recovery unverified; no repeated attempt or database restore')
                save()
                return state


def submit(root, envelope, api=None, probe=default_probe):
    """Host CLI entry point; HTTP must never accept registry/paths/trust from body.

    Root is provisioned once for its entire ownership boundary. All jobs under
    this root share a lock. Shared DB is rejected from an installation boundary.
    """
    import fcntl
    import uuid
    root, api = Path(root), api or Docker()
    require(root.is_absolute() and root.is_dir() and not root.is_symlink(), 'Invalid controller root')
    with (root/'components.lock').open('a') as lock, ExitStack() as barriers:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def read(name):
            path = root/name
            require(path.is_file() and not path.is_symlink(), 'Missing trusted component enrollment')
            return json.loads(path.read_text(encoding='utf-8'))
        registry, trust = read('component-registry.private.json'), read('component-trust.private.json')
        bundle = verify_bundle(envelope, trust, registry)
        barriers.enter_context(tenant_barriers(registry, bundle['participants']))
        pointer = root/'component-current.private.json'
        if pointer.exists():
            previous = read('component-current.private.json')
            require(re.fullmatch('[a-f0-9]{32}', previous['job']), 'Invalid component journal pointer')
            prior = root/'component-jobs'/previous['job']/'state.json'
            require(not prior.is_symlink(), 'Invalid component state')
            state = json.loads(prior.read_text(encoding='utf-8'))
            require(state['status'] in ('completed', 'rolled_back', 'error'), 'Prior component job requires review')
        receipt = root/('component-release-'+hashlib.sha256(bundle['release'].encode()).hexdigest()+'.private.json')
        require(not receipt.exists(), 'Release already attempted; inspect original journal')
        job = uuid.uuid4().hex
        folder = root/'component-jobs'/job
        folder.mkdir(mode=0o700, parents=True)
        write_json(folder/'state.json', {'job': job, 'status': 'preparing'})
        write_json(folder/'request.private.json', envelope)
        write_json(receipt, {'job': job, 'release': bundle['release']})
        write_json(pointer, {'job': job})
        try:
            plan = prepare(api, registry, bundle, job, probe)
            # Fresh expiry, registry, signature and CAS after slow pulls/backups.
            require(read('component-registry.private.json') == registry, 'Registry changed during preparation')
            verify_bundle(envelope, trust, registry)
            write_json(folder/'plan.private.json', plan)
            write_json(folder/'state.json', {'job': job, 'status': 'queued'})
        except Exception as exc:
            write_json(folder/'failure.private.json', {'type': type(exc).__name__})
            write_json(folder/'state.json', {'job': job, 'status': 'error', 'message': 'Preparation failed; no switch'})
            raise
        return run(folder, api, probe)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Explicit host-owned component maintenance')
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--signed-request', required=True, type=Path)
    args = parser.parse_args()
    result = submit(args.root, json.loads(args.signed_request.read_text(encoding='utf-8')))
    print(json.dumps({k: result[k] for k in ('job', 'status', 'message') if k in result}))

"""Explicit disposable Docker/MariaDB component tests; no customer or real NAS.

Run in Linux with /var/run/docker.sock and a fresh /lab report mount. Web backup
is the pinned product implementation. Runtime trust/license/NAS probes are
synthetic and explicitly injected; these tests do not authorize publication.
"""
import base64
import copy
import datetime as dt
import hashlib
import json
import os
import signal
from pathlib import Path
import sys
import time
import uuid
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from services import system_update_components as engine
from services.system_update_executor import execute
from services.system_update_runtime import Docker, UpdateError, inspect, write_json

WEB = 'sha256:c5ab9fe42359c867c6dedabaf607ff492b2652e8160fa5df987e34e645db098b'
DB = 'sha256:0d0945c57f75b3a3b9ebe71aedaaad005fa5305054e8fca3d2e42a8c92b43b5e'
CORE = 'sha256:bc4d4644108018c7f67188e639bfeea9a4d12deedc01602bc15e219406ac4932'
L2TP = 'mohd777/max-radius-l2tp@sha256:a7fa96b5a7a485ce47755e0379c5553d1a0490855fcfda950602d6c1d9affee7'
AUTOHEAL = 'willfarrell/autoheal@sha256:658d4db0d5d570d07f1977e6cd344d5d9789f4453a050abf784caa330458ab26'
ORACLE = """import json
from database.db import query_one
print(json.dumps(query_one('SELECT COUNT(*) n,SUM(input_bytes) upload,SUM(output_bytes) download,SUM(price) gross FROM diag_component_oracle'),default=str))
"""


def main():
    if sys.argv[1:] != ['--run']:
        raise SystemExit('Explicit --run required')
    root = Path('/lab')
    marker = root/'ATTEMPTED.private.json'
    if marker.exists():
        raise SystemExit('Do not replay a completed or uncertain experiment')
    transport = Docker()
    transport_failures = []
    web_target = os.environ.get('MAX_COMPONENT_WEB_TARGET')
    if web_target:
        import re
        assert re.fullmatch('max-radius-web@sha256:[a-f0-9]{64}', web_target)
    def api(method, path, body=None, timeout=30):
        started = time.monotonic()
        if method == 'POST' and path.startswith('/images/create?') and os.environ.get('MAX_COMPONENT_NATIVE_AUX') == '1':
            # Exercise exact digest admission against locally cached originals.
            # Registry download is deliberately not part of this offline lab.
            import urllib.parse
            reference = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)['fromImage'][0]
            assert reference in (L2TP, AUTOHEAL, web_target)
            image = transport('GET', '/images/'+urllib.parse.quote(reference, safe='')+'/json')
            assert reference in image.get('RepoDigests', [])
            return []
        try:
            return transport(method, path, body, timeout)
        except UpdateError as exc:
            transport_failures.append({'method': method, 'route': path.split('?')[0],
                'timeout': timeout, 'elapsed_seconds': round(time.monotonic()-started, 3),
                'type': type(exc.__cause__).__name__ if exc.__cause__ else type(exc).__name__})
            write_json(root/'TRANSPORT-FAILURES.private.json', transport_failures)
            raise
    owner = 'components4-'+uuid.uuid4().hex[:10]
    write_json(marker, {'owner': owner, 'web_image': WEB, 'db_image': DB})
    before = {r['Id']: engine.stable(inspect(api, r['Id'])) for r in api('GET', '/containers/json?all=1')}
    write_json(root/'PREEXISTING-BEFORE.private.json',before)
    owned, networks, volumes, results = set(), [], [], []
    complete = False
    native = os.environ.get('MAX_COMPONENT_NATIVE_RADIUS') == '1'
    native_aux = os.environ.get('MAX_COMPONENT_NATIVE_AUX') == '1'
    load_rounds = int(os.environ.get('MAX_COMPONENT_LOAD_ROUNDS', '0'))
    assert 0 <= load_rounds <= 100
    environments = os.environ.get('MAX_COMPONENT_ENVIRONMENTS', 'local,domain,shared').split(',')
    profiles = os.environ.get('MAX_COMPONENT_PROFILES', 'success,rollback,lost_create').split(',')
    assert environments and len(set(environments)) == len(environments) and set(environments) <= {'local', 'domain', 'shared'}
    extra_profiles = {'stop_reply_lost', 'rename_reply_lost', 'start_reply_lost',
                      'create_rejected', 'crash_before_stop', 'crash_after_db_start'}
    assert profiles and len(set(profiles)) == len(profiles) and set(profiles) <= {'success', 'rollback', 'lost_create'} | extra_profiles
    def created(name, spec, start=True):
        spec.setdefault('Labels', {})['max.audit'] = owner
        result = api('POST', '/containers/create?name='+name, spec)
        owned.add(result['Id'])
        if start:
            api('POST', '/containers/'+result['Id']+'/start')
        return result['Id']
    def wait_db(ident):
        deadline = time.monotonic()+90
        while time.monotonic() < deadline:
            try:
                engine.default_probe(api, ident, 'db', {}); return
            except Exception:
                time.sleep(1)
        raise UpdateError('Fixture DB did not become ready')
    def clean_case():
        # Discover candidates even after an intentionally lost create response.
        for row in api('GET', '/containers/json?all=1'):
            if (row.get('Labels') or {}).get('max.audit') == owner:
                owned.add(row['Id'])
        for ident in list(owned):
            row = inspect(api, ident)
            if row['Config'].get('Labels', {}).get('max.audit') != owner:
                raise AssertionError('Cleanup ownership mismatch')
            api('DELETE', '/containers/'+ident+'?force=1&v=0'); owned.remove(ident)
        for network in list(networks):
            api('DELETE', '/networks/'+network); networks.remove(network)
        for volume in list(volumes):
            api('DELETE', '/volumes/'+volume); volumes.remove(volume)
    try:
        for environment in environments:
            for profile in (['success'] if os.environ.get('MAX_COMPONENT_FINAL_MATRIX') == '1' and environment != 'shared' else profiles):
                case = environment+'-'+profile
                directory = root/case; directory.mkdir(mode=0o700)
                project = owner+'-'+case
                if len(project) > 48:
                    project = owner+'-'+hashlib.sha256(case.encode()).hexdigest()[:12]
                network = api('POST', '/networks/create', {'Name': project, 'Internal': True,
                    'Labels': {'max.audit': owner}, 'IPAM': {'Config': [{'Subnet': '10.237.'+str(int(uuid.uuid4().hex[:2], 16))+'.0/28'}]}})['Id']
                networks.append(network)
                registry = {'schema': 1, 'authority': 'host' if environment == 'shared' else 'installation',
                    'identity': case+'.invalid', 'components': {}, 'tenants': {}}
                ids = {}
                tenants = ('a', 'b') if environment == 'shared' else ('a',)
                compose = directory/'compose.json'
                document = {'services': {}}
                dbkey = 'engine-db'
                volume = project+'-db-data'
                api('POST', '/volumes/create', {'Name': volume, 'Labels': {'max.audit': owner}}); volumes.append(volume)
                dbspec = {'Image': DB, 'Env': ['MARIADB_ROOT_PASSWORD=isolated-fixture-only'],
                    'Labels': {'com.docker.compose.project': project, 'com.docker.compose.service': dbkey,
                               'max.db.scope': 'shared' if environment == 'shared' else 'dedicated'},
                    'HostConfig': {'Memory': 805306368, 'MemorySwap': 805306368, 'NanoCpus': 1000000000,
                        'RestartPolicy': {'Name': 'no'}, 'NetworkMode': project,
                        'Mounts': [{'Type': 'volume', 'Source': volume, 'Target': '/var/lib/mysql'}]},
                    'NetworkingConfig': {'EndpointsConfig': {project: {'Aliases': [dbkey]}}}}
                ids[dbkey] = created(project+'-'+dbkey, dbspec); wait_db(ids[dbkey])
                for name in (dbkey, *[t+'-'+r for t in tenants for r in ('web', 'core')], 'tunnel', 'watchdog'):
                    role = 'db' if name == dbkey else 'l2tp' if name == 'tunnel' else 'autoheal' if name == 'watchdog' else name.split('-')[1]
                    reference = ('fixture/db' if role == 'db' else 'fixture/'+role)+'@sha256:'+'a'*64
                    if native_aux and role in ('l2tp', 'autoheal'):
                        reference = L2TP if role == 'l2tp' else AUTOHEAL
                    registry['components'][name] = {'role': role, 'project': project, 'service': name, 'compose_path': str(compose), 'images': {reference: 'fixture-compatible'}}
                    if web_target and role == 'web':
                        registry['components'][name]['images'] = {'max-radius-web@'+WEB: 'fixture-compatible', web_target: 'fixture-compatible'}
                    document['services'][name] = {'image': DB if role == 'db' else CORE if native and role == 'core' else WEB}
                    if role == 'db':
                        continue
                    tenant = name.split('-')[0] if role in ('web', 'core') else 'a'
                    spec = {'Image': WEB, 'Entrypoint': ['python', '-c'], 'Cmd': ['import time;time.sleep(1800)'],
                        'Healthcheck': {'Test': ['CMD', 'python', '-c', 'pass'], 'Interval': 1000000000},
                        'StopSignal': 'SIGKILL', 'Env': ['DB_TYPE=mysql', 'DB_HOST='+dbkey, 'DB_USER=root',
                            'DB_PASSWORD=isolated-fixture-only', 'DB_NAME=fixture_'+tenant,
                            'STORAGE_DIR=/app/storage', 'BACKUPS_DIR=/app/storage/backups', 'PYTHONDONTWRITEBYTECODE=1'],
                        'Labels': {'com.docker.compose.project': project, 'com.docker.compose.service': name},
                        'HostConfig': {'Memory': 268435456, 'MemorySwap': 268435456, 'NanoCpus': 1000000000,
                            'NetworkMode': project, 'RestartPolicy': {'Name': 'no'}},
                        'NetworkingConfig': {'EndpointsConfig': {project: {'Aliases': [name]}}}}
                    if native and role == 'core':
                        spec['Image'] = CORE
                        for field in ('Entrypoint', 'Cmd', 'Healthcheck', 'StopSignal'):
                            spec.pop(field, None)
                        spec['Env'].append('RADIUS_SECRET_DEFAULT=fixture-only')
                    if native_aux and role in ('l2tp', 'autoheal'):
                        spec['Image'] = L2TP if role == 'l2tp' else AUTOHEAL
                        for field in ('Entrypoint', 'Cmd', 'Healthcheck', 'StopSignal'):
                            spec.pop(field, None)
                        spec['Env'] = []
                        if role == 'l2tp':
                            # A trusted installed deployment contract, since the
                            # original L2TP image does not ship its own healthcheck.
                            spec['Healthcheck'] = {'Test': ['CMD-SHELL', 'pidof xl2tpd >/dev/null && grep -qi ":06A5 " /proc/net/udp'], 'Interval': 1000000000}
                            spec['HostConfig']['CapAdd'] = ['NET_ADMIN']
                        else:
                            # Random per-case label: the native watchdog can only
                            # act on explicitly labeled disposable lab containers.
                            spec['Env'] = ['AUTOHEAL_CONTAINER_LABEL=max.component.lab.'+project,
                                'AUTOHEAL_INTERVAL=2', 'AUTOHEAL_START_PERIOD=0', 'AUTOHEAL_DEFAULT_STOP_TIMEOUT=2']
                            spec['HostConfig']['Binds'] = ['/var/run/docker.sock:/var/run/docker.sock']
                        document['services'][name]['image'] = spec['Image']
                    if role == 'web':
                        web_volume = project+'-'+name+'-storage'
                        api('POST', '/volumes/create', {'Name': web_volume, 'Labels': {'max.audit': owner}}); volumes.append(web_volume)
                        spec['HostConfig']['Mounts'] = [{'Type': 'volume', 'Source': web_volume, 'Target': '/app/storage'}]
                    ids[name] = created(project+'-'+name, spec, start=not (native and role == 'core'))
                compose.write_text(json.dumps(document))
                for tenant in tenants:
                    controller_root = directory/(tenant+'-controller'); controller_root.mkdir()
                    registry['tenants'][tenant] = {'web': tenant+'-web', 'core': tenant+'-core', 'db': dbkey,
                        'dependencies': [tenant+'-web', tenant+'-core', dbkey, 'tunnel', 'watchdog'], 'controller_root': str(controller_root)}
                    # Database creation and schema initialization are fixture-only.
                    code = """import os,pymysql,contextlib,io,json,time
result={}
try:
 with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
  deadline=time.monotonic()+60
  while True:
   try:
    c=pymysql.connect(host=os.environ['DB_HOST'],user='root',password=os.environ['DB_PASSWORD']);break
   except pymysql.OperationalError:
    if time.monotonic()>=deadline:raise
    time.sleep(1)
  with c.cursor() as q:q.execute('CREATE DATABASE '+os.environ['DB_NAME'])
  c.close()
  from database.db import init_database,get_connection
  init_database()
  with get_connection() as c:
   with c.cursor() as q:
    q.execute('CREATE TABLE diag_component_oracle(id INT PRIMARY KEY,input_bytes BIGINT,output_bytes BIGINT,price DECIMAL(20,2))')
    q.execute('INSERT INTO diag_component_oracle VALUES(1,4294967400,8589934600,123.45)')
   c.commit()
 result={'initialized':True}
except Exception as exc:result={'initialized':False,'type':type(exc).__name__,'error':str(exc)}
print(json.dumps(result))
"""
                    initialized = execute(api, ids[tenant+'-web'], code, timeout=180)
                    write_json(directory/(tenant+'-initialization.private.json'), initialized)
                    assert initialized['initialized'], 'Fixture initialization failed; inspect private receipt'
                    if native:
                        seed = """import json,contextlib,io
with contextlib.redirect_stdout(io.StringIO()):
 from database.db import execute_write,query_one
 execute_write("INSERT INTO wisp_license_info(license_id,client_name,raw_package_json,signature_b64,status) VALUES('component-fixture','Synthetic','{}','fixture-only','active')")
 row=query_one("SELECT id FROM wisp_license_info WHERE license_id='component-fixture'")
 execute_write("INSERT INTO wisp_license_runtime_state(id,license_row_id,package_hash,is_valid,license_mode,max_active_sessions,valid_until) VALUES(1,%s,SHA2('{}',256),1,'active_sessions',1000000,UTC_TIMESTAMP()+INTERVAL 3 HOUR) ON DUPLICATE KEY UPDATE license_row_id=VALUES(license_row_id),package_hash=VALUES(package_hash),is_valid=1,license_mode='active_sessions',max_active_sessions=1000000,valid_until=VALUES(valid_until)",(row['id'],))
 execute_write("INSERT INTO wisp_packages(id,name,service_type,volume_quota_mb,simultaneous_sessions) VALUES(900,'component-fixture','hotspot',0,100)")
 execute_write("INSERT INTO wisp_subscribers(username,password,full_name,status,balance,package_id,first_used_at,last_renewed_at,expires_at,snap_volume_quota_mb) VALUES('component_radius','fixture-only','Synthetic','active',17.25,900,NOW()-INTERVAL 1 DAY,NOW()-INTERVAL 1 DAY,NOW()+INTERVAL 30 DAY,0)")
 from core.radius_sync import sync_package_to_radius,sync_subscriber_to_radius
 sync_package_to_radius(900);sync_subscriber_to_radius(query_one("SELECT id FROM wisp_subscribers WHERE username='component_radius'")['id'])
print(json.dumps({'seeded':True}))
"""
                        import textwrap
                        seed = ('import contextlib,io,json\nresult={}\ntry:\n with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):\n'
                            +textwrap.indent(seed, '  ')+"\n result={'seeded':True}\nexcept Exception as exc:result={'seeded':False,'type':type(exc).__name__,'error':str(exc)}\nprint(json.dumps(result))")
                        seeded = execute(api, ids[tenant+'-web'], seed)
                        write_json(directory/(tenant+'-radius-seed.private.json'), seeded)
                        assert seeded['seeded'], 'Native fixture seed failed; inspect private receipt'
                        api('POST', '/containers/'+ids[tenant+'-core']+'/start')
                        engine.wait_probe(api, ids[tenant+'-core'], 'core', {}, engine.default_probe)
                baselines = {t: execute(api, ids[t+'-web'], ORACLE) for t in tenants}
                identities = {t: execute(api, ids[t+'-web'], engine.IDENTITY) for t in tenants}
                if native_aux:
                    for component, role in (('tunnel', 'l2tp'), ('watchdog', 'autoheal')):
                        engine.wait_probe(api, ids[component], role, {}, engine.default_probe)
                    sentinel = created(project+'-watchdog-sentinel', {'Image': WEB,
                        'Entrypoint': ['python', '-c'], 'Cmd': ['import time;time.sleep(600)'],
                        'Labels': {'max.component.lab.'+project: 'true'},
                        'Healthcheck': {'Test': ['CMD', 'test', '-f', '/tmp/recovered'], 'Interval': 1000000000, 'Retries': 1},
                        'HostConfig': {'NetworkMode': 'none', 'Memory': 67108864, 'MemorySwap': 67108864, 'RestartPolicy': {'Name': 'no'}}})
                    sentinel_start = inspect(api, sentinel)['State']['StartedAt']
                    deadline = time.monotonic()+45
                    while inspect(api, sentinel)['State']['StartedAt'] == sentinel_start:
                        assert time.monotonic() < deadline, 'Native watchdog did not restart its owned unhealthy sentinel'
                        time.sleep(1)
                    task = api('POST', '/containers/'+sentinel+'/exec', {'AttachStdout': True, 'AttachStderr': True, 'Tty': True,
                        'Cmd': ['sh', '-ec', 'touch /tmp/recovered; printf "{}"']})
                    api('POST', '/exec/'+task['Id']+'/start', {'Detach': False, 'Tty': True})
                    assert api('GET', '/exec/'+task['Id']+'/json')['ExitCode'] == 0
                    assert inspect(api, sentinel)['Config']['Labels']['max.audit'] == owner
                    api('DELETE', '/containers/'+sentinel+'?force=1&v=0'); owned.remove(sentinel)
                initial_oracles = copy.deepcopy(baselines)
                progressed = set()
                original_ids = set(ids.values())
                radius_started, radius_index, radius_packets = set(), {t: 0 for t in tenants}, []
                def packet(adapter, coreid, tenant, status=None):
                    text = 'User-Name = "component_radius"\nNAS-IP-Address = 127.0.0.1\nCalled-Station-Id = "hs-component"\nService-Type = Login-User\nCalling-Station-Id = "02:00:00:00:00:61"\nNAS-Port = 1\n'
                    if status:
                        index = radius_index[tenant]
                        incoming = 0 if status == 'Start' else 4294967596+index
                        outgoing = 0 if status == 'Start' else 8589935192+index
                        text += 'Acct-Session-Id = "component-'+tenant+'"\nAcct-Status-Type = '+status+'\nAcct-Session-Time = '+str(index*60)+'\nAcct-Input-Octets = '+str(incoming%4294967296)+'\nAcct-Input-Gigawords = '+str(incoming//4294967296)+'\nAcct-Output-Octets = '+str(outgoing%4294967296)+'\nAcct-Output-Gigawords = '+str(outgoing//4294967296)+'\n'
                    else:
                        text += 'User-Password = "fixture-only"\n'
                    expected = 'Accounting-Response' if status else 'Access-Accept'
                    script = 'printf "%s" "$1" | radclient -r 1 -t 3 127.0.0.1 "$2" fixture-only >/tmp/component-packet.out 2>&1; grep -q "Received '+expected+'" /tmp/component-packet.out; printf "{}"'
                    task = adapter('POST', '/containers/'+coreid+'/exec', {'AttachStdout': True, 'AttachStderr': True, 'Tty': True,
                        'Cmd': ['sh', '-ec', script, 'fixture', text, 'acct' if status else 'auth']})
                    adapter('POST', '/exec/'+task['Id']+'/start', {'Detach': False, 'Tty': True})
                    good = adapter('GET', '/exec/'+task['Id']+'/json')['ExitCode'] == 0
                    radius_packets.append({'tenant': tenant, 'kind': status or 'auth', 'passed': good})
                    write_json(directory/'RADIUS-PACKETS.private.json', radius_packets)
                    assert good, 'Native RADIUS packet failed; inspect owned fixture'
                def radius_flow(adapter, webid, tenant):
                    service = registry['components'][tenant+'-core']['service']
                    matches = [r['Id'] for r in adapter('GET', '/containers/json?all=1') if r.get('State') == 'running' and (r.get('Labels') or {}).get('com.docker.compose.project') == project and (r.get('Labels') or {}).get('com.docker.compose.service') == service]
                    assert len(matches) == 1
                    packet(adapter, matches[0], tenant)
                    if tenant not in radius_started:
                        packet(adapter, matches[0], tenant, 'Start'); radius_started.add(tenant)
                    else:
                        radius_index[tenant] += 1
                        packet(adapter, matches[0], tenant, 'Interim-Update')
                def probe(adapter, ident, role, context):
                    if role == 'db':
                        return engine.default_probe(adapter, ident, role, context)
                    if role == 'web':
                        tenant = next(t for t in tenants if registry['components'][t+'-web']['service'] == inspect(adapter, ident)['Config']['Labels']['com.docker.compose.service'])
                        if ident not in original_ids and tenant not in progressed:
                            # Commit after the backup, then fail the candidate:
                            # rollback must preserve this newer data, not restore SQL.
                            code = """import json
from database.db import execute_write
execute_write('UPDATE diag_component_oracle SET input_bytes=input_bytes+100,output_bytes=output_bytes+200,price=price+1 WHERE id=1')
print(json.dumps({'advanced':True}))
"""
                            assert execute(adapter, ident, code)['advanced']
                            expected = initial_oracles[tenant]
                            baselines[tenant] = dict(expected, upload=str(int(expected['upload'])+100), download=str(int(expected['download'])+200), gross=format(__import__('decimal').Decimal(expected['gross'])+1, '.2f'))
                            progressed.add(tenant)
                        counts = execute(adapter, ident, ORACLE)
                        assert counts == baselines[tenant], 'Latest data changed across DB switch'
                        identity = execute(adapter, ident, engine.IDENTITY)
                        assert identity == identities[tenant], 'Installation identity or key changed'
                        if native:
                            radius_flow(adapter, ident, tenant)
                            for _ in range(load_rounds):
                                radius_flow(adapter, ident, tenant)
                        if profile == 'rollback' and ident not in original_ids:
                            raise RuntimeError('Injected Web rejection after newer write committed')
                        return {'verified': True, 'version': '2.6.9', 'publisher_key': 'synthetic-license-probe', 'rows': [], 'oracle': counts, 'identity': identity}
                    if native and role == 'core':
                        return engine.default_probe(adapter, ident, role, context)
                    if native_aux and role in ('l2tp', 'autoheal'):
                        return engine.default_probe(adapter, ident, role, context)
                    assert inspect(adapter, ident)['State']['Running']
                    return {'verified': True, 'synthetic_non_db_process_probe': True}
                now = dt.datetime.now(dt.timezone.utc)
                bundle = {'schema': 1, 'protocol': 4, 'release': case,
                    'audience': {'authority': registry['authority'], 'identity': registry['identity']},
                    'registry_revision': engine.registry_revision(registry), 'issued_at': now.isoformat(),
                    'valid_until': (now+dt.timedelta(minutes=5)).isoformat(), 'participants': list(tenants),
                    'changes': {dbkey: {'environment': {'MALLOC_ARENA_MAX': '2'},
                        'mariadb_options': {'aria-pagecache-buffer-size': 16777216, 'key-buffer-size': 8388608}},
                        'tunnel': {'image': next(iter(registry['components']['tunnel']['images']))}},
                    'data_policy': 'preserve-compatible-no-restore', 'application_versions': {}}
                # Tunnel uses same image config only: image-update digest behavior is
                # tested separately with signed mocks; no invented registry pull here.
                del bundle['changes']['tunnel']
                if native_aux:
                    bundle['changes']['tunnel'] = {'image': L2TP}
                    bundle['changes']['watchdog'] = {'image': AUTOHEAL}
                if web_target:
                    for tenant in tenants:
                        bundle['changes'][tenant+'-web'] = {'image': web_target}
                        bundle['application_versions'][tenant+'-web'] = '2.6.9'
                key = Ed25519PrivateKey.generate(); public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
                trust = {'public_key': base64.b64encode(public).decode(), 'key_fingerprint': hashlib.sha256(public).hexdigest()}
                envelope = {'bundle': bundle, 'signature': base64.b64encode(key.sign(engine.canonical(bundle))).decode()}
                engine.verify_bundle(envelope, trust, registry)
                if os.environ.get('MAX_COMPONENT_ENROLL') == '1':
                    from services.system_component_control import enroll
                    enrollment = enroll(directory, registry, trust, api)
                    write_json(directory/'ENROLLMENT.private.json', enrollment)
                else:
                    write_json(directory/'component-registry.private.json', registry)
                    write_json(directory/'component-trust.private.json', trust)
                lost = [False]
                def adapter(method, path, body=None, timeout=30):
                    if profile == 'create_rejected' and method == 'POST' and path.startswith('/containers/create?') and not lost[0]:
                        lost[0] = True
                        raise UpdateError('Injected create rejection before Docker mutation')
                    if profile == 'crash_before_stop' and method == 'POST' and '/stop?' in path:
                        os.kill(os.getpid(), signal.SIGKILL)
                    result = api(method, path, body, timeout)
                    if method == 'POST' and path.startswith('/containers/create?'):
                        owned.add(result['Id'])
                        if profile == 'lost_create' and not lost[0]:
                            lost[0] = True
                            raise UpdateError('Injected lost create reply')
                    fault_route = {'stop_reply_lost': '/stop?', 'rename_reply_lost': '/rename?', 'start_reply_lost': '/start'}
                    if profile in fault_route and method == 'POST' and fault_route[profile] in path and path.startswith('/containers/') and not lost[0]:
                        lost[0] = True
                        raise UpdateError('Injected lost mutation reply')
                    if profile == 'crash_after_db_start' and method == 'POST' and path.startswith('/containers/') and path.endswith('/start'):
                        ident = path.split('/')[2]
                        if inspect(api, ident)['Config']['Labels']['com.docker.compose.service'] == dbkey:
                            os.kill(os.getpid(), signal.SIGKILL)
                    return result
                crashed = profile.startswith('crash_')
                if crashed:
                    # Kill a real forked worker; no exception handler or finally runs
                    # in it. The parent retains ownership of disposable lab cleanup.
                    pid = os.fork()
                    if pid == 0:
                        try:
                            engine.submit(directory, envelope, adapter, probe)
                        except BaseException:
                            os._exit(99)
                        os._exit(98)
                    _, status = os.waitpid(pid, 0)
                    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
                    pointer = json.loads((directory/'component-current.private.json').read_text())
                    folder = directory/'component-jobs'/pointer['job']
                    interrupted = json.loads((folder/'state.json').read_text())
                    assert interrupted['switch_attempted']
                    result = engine.run(folder, api, probe)
                    assert result['status'] == 'recovery_required'
                    frozen = {r['Id']: engine.stable(inspect(api, r['Id'])) for r in api('GET', '/containers/json?all=1') if (r.get('Labels') or {}).get('max.audit') == owner}
                    assert engine.run(folder, api, probe) == result
                    try:
                        engine.submit(directory, envelope, api, probe)
                    except UpdateError:
                        pass
                    else:
                        raise AssertionError('Interrupted maintenance was replayed')
                    assert frozen == {r['Id']: engine.stable(inspect(api, r['Id'])) for r in api('GET', '/containers/json?all=1') if (r.get('Labels') or {}).get('max.audit') == owner}
                    plan = json.loads((folder/'plan.private.json').read_text())
                    assert len(plan['backups']) == len(tenants)
                    for key, original in plan['old'].items():
                        row = inspect(api, original['Id'])
                        assert row['Image'] == original['Image'] and row['Mounts'] == original['Mounts']
                    if profile == 'crash_after_db_start':
                        dbid = result['records'][dbkey]['new_id']
                        assert inspect(api, dbid)['State']['Running']
                        wait_db(dbid)
                        # Read data directly through the native socket; Web/Core are
                        # intentionally unavailable pending explicit recovery.
                        query = "SELECT JSON_OBJECT('n',COUNT(*),'upload',SUM(input_bytes),'download',SUM(output_bytes),'gross',SUM(price)) FROM diag_component_oracle"
                        for tenant in tenants:
                            task = api('POST', '/containers/'+dbid+'/exec', {'AttachStdout': True, 'AttachStderr': True, 'Tty': True,
                                'Cmd': ['sh', '-ec', 'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" exec mariadb -uroot -N -B "$1" -e "$2"', 'fixture', 'fixture_'+tenant, query]})
                            value = api('POST', '/exec/'+task['Id']+'/start', {'Detach': False, 'Tty': True})
                            assert api('GET', '/exec/'+task['Id']+'/json')['ExitCode'] == 0
                            assert value['n'] == 1 and int(value['upload']) == 4294967400 and int(value['download']) == 8589934600 and str(value['gross']) == '123.45'
                    packet_receipt = directory/'RADIUS-PACKETS.private.json'
                    observed_packets = json.loads(packet_receipt.read_text()) if packet_receipt.exists() else []
                    record = {'case': case, 'status': 'recovery_required', 'expected_safety_rejection': True,
                        'actual_worker_sigkill': True, 'automatic_replay_blocked': True,
                        'original_images_and_volumes_preserved': True, 'mandatory_product_backups': len(plan['backups']),
                        'sql_data_preserved_after_crash': profile == 'crash_after_db_start',
                        'automatic_service_recovery': False, 'no_database_restore': True,
                        'participants': len(tenants), 'radius_packets': len(observed_packets),
                        'radius_receipt_scope': 'pre-interruption only; no final Stop acceptance'}
                    if os.environ.get('MAX_COMPONENT_RECOVER') == '1':
                        # New acceptance round: reconcile the original killed journal,
                        # never rerun its prepare/backup/switch and never restore SQL.
                        if native:
                            radius_packets[:] = observed_packets
                            radius_started.update(tenants)
                            for tenant in tenants:
                                radius_index[tenant] = sum(p['tenant']==tenant and p['kind']=='Interim-Update' for p in observed_packets)
                        daemon_recovery = os.environ.get('MAX_COMPONENT_DAEMON_RECOVER') == '1'
                        if daemon_recovery:
                            # Start the actual UNIX server under its singleton lock.
                            # Its startup reconciles the killed worker before serving.
                            import importlib.util, multiprocessing, socket, http.client
                            from services.system_component_control import ComponentController
                            def boot_server():
                                spec=importlib.util.spec_from_file_location('native_component_server',
                                    Path('/src/deploy/hosted-max/update_controller.py'))
                                module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
                                module.serve(ComponentController(directory,api,probe),directory/'acceptance.sock')
                            server=multiprocessing.get_context('fork').Process(target=boot_server)
                            server.start()
                            try:
                                deadline=time.monotonic()+120
                                while not (directory/'acceptance.sock').exists():
                                    assert server.is_alive() and time.monotonic()<deadline, 'Controller startup recovery failed'
                                    time.sleep(.2)
                                class UnixHTTP(http.client.HTTPConnection):
                                    def connect(self):
                                        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
                                        self.sock.settimeout(10);self.sock.connect(str(directory/'acceptance.sock'))
                                connection=UnixHTTP('localhost')
                                connection.request('GET','/_max_updates/v4/components/progress')
                                response=connection.getresponse();recovered=json.loads(response.read())
                                assert response.status==200
                                connection.close()
                            finally:
                                server.terminate();server.join(10)
                                assert not server.is_alive()
                        else:
                            recovered = engine.recover(directory, pointer['job'], api, probe)
                        assert recovered['status'] == 'rolled_back', recovered
                        if native and daemon_recovery:
                            # The daemon runs in its own process. Import its packet
                            # progress before generating a strictly newer Stop.
                            radius_packets[:] = json.loads(packet_receipt.read_text())
                            for tenant in tenants:
                                radius_index[tenant] = sum(p['tenant']==tenant and p['kind']=='Interim-Update' for p in radius_packets)
                        for tenant in tenants:
                            assert execute(api, ids[tenant+'-web'], ORACLE) == baselines[tenant]
                            assert execute(api, ids[tenant+'-web'], engine.IDENTITY) == identities[tenant]
                            assert inspect(api, ids[tenant+'-core'])['State']['Running']
                            if native:
                                radius_index[tenant] += 1
                                packet(api, ids[tenant+'-core'], tenant, 'Stop')
                                value=execute(api, ids[tenant+'-web'], "import json;from database.db import query_all;print(json.dumps({'rows':query_all(\"SELECT acctstoptime,acctinputoctets,acctoutputoctets FROM radacct WHERE username='component_radius'\")},default=str))")
                                assert len(value['rows'])==1 and value['rows'][0]['acctstoptime'] is not None
                                assert int(value['rows'][0]['acctinputoctets']) > 4294967296
                        frozen_after={r['Id']:engine.stable(inspect(api,r['Id'])) for r in api('GET','/containers/json?all=1') if (r.get('Labels') or {}).get('max.audit')==owner}
                        assert engine.recover(directory,pointer['job'],api,probe)['status']=='rolled_back'
                        assert frozen_after=={r['Id']:engine.stable(inspect(api,r['Id'])) for r in api('GET','/containers/json?all=1') if (r.get('Labels') or {}).get('max.audit')==owner}
                        record.update(status='rolled_back',verified_recovery_after_sigkill=True,
                                      automatic_service_recovery=daemon_recovery,service_recovery_verified=True,
                                      recovery_trigger='controller_startup_current_job' if daemon_recovery else 'explicit_current_job_recovery',final_radius_stop_verified=native,
                                      radius_packets=len(json.loads(packet_receipt.read_text())) if packet_receipt.exists() else 0,
                                      radius_receipt_scope='pre-interruption and post-recovery with final Stop' if native else 'synthetic')
                    results.append(record); write_json(root/'RESULTS.private.json', results)
                    print(json.dumps(record), flush=True)
                    clean_case()
                    continue
                result = engine.submit(directory, envelope, adapter, probe)
                pointer = json.loads((directory/'component-current.private.json').read_text())
                folder = directory/'component-jobs'/pointer['job']
                plan = json.loads((folder/'plan.private.json').read_text())
                expected = 'completed' if profile == 'success' else 'rolled_back'
                assert result['status'] == expected, (case, result['status'])
                assert engine.run(folder, adapter, probe)['status'] == expected
                for tenant in tenants:
                    webid = result['records'][tenant+'-web']['new_id'] if expected == 'completed' else ids[tenant+'-web']
                    assert execute(api, webid, ORACLE) == baselines[tenant]
                    assert execute(api, webid, engine.IDENTITY) == identities[tenant]
                    if native:
                        coreid = result['records'][tenant+'-core']['new_id'] if expected == 'completed' else ids[tenant+'-core']
                        radius_index[tenant] += 1
                        packet(api, coreid, tenant, 'Stop')
                        verify_sql = """import json
from database.db import query_all,query_one
rows=query_all("SELECT acctinputoctets,acctoutputoctets,acctstoptime FROM radacct WHERE username='component_radius'")
auth=query_one("SELECT COUNT(*) n FROM radpostauth WHERE username='component_radius' AND reply='Access-Accept'")
balance=query_one("SELECT balance FROM wisp_subscribers WHERE username='component_radius'")
print(json.dumps({'rows':rows,'auth':auth['n'],'balance':balance['balance']},default=str))
"""
                        value = execute(api, webid, verify_sql)
                        write_json(directory/(tenant+'-RADIUS-SQL.private.json'), value)
                        assert len(value['rows']) == 1 and value['rows'][0]['acctstoptime'] is not None
                        assert int(value['rows'][0]['acctinputoctets']) == 4294967596+radius_index[tenant]
                        assert int(value['rows'][0]['acctoutputoctets']) == 8589935192+radius_index[tenant]
                        assert value['auth'] >= 2 and str(value['balance']) == '17.25'
                dbid = result['records'][dbkey]['new_id'] if expected == 'completed' else ids[dbkey]
                row = inspect(api, dbid)
                assert any(m.get('Name') == volume and m['Destination'] == '/var/lib/mysql' for m in row['Mounts'])
                assert ('MALLOC_ARENA_MAX=2' in row['Config'].get('Env', [])) == (expected == 'completed')
                check = api('POST', '/containers/'+dbid+'/exec', {'AttachStdout': True, 'AttachStderr': True, 'Tty': True,
                    'Cmd': ['mariadb', '-uroot', '-pisolated-fixture-only', '-N', '-B', '-e', "SELECT JSON_OBJECT('aria',@@global.aria_pagecache_buffer_size,'key_buffer',@@global.key_buffer_size)"]})
                settings = api('POST', '/exec/'+check['Id']+'/start', {'Detach': False, 'Tty': True})
                assert api('GET', '/exec/'+check['Id']+'/json')['ExitCode'] == 0
                if expected == 'completed':
                    assert settings == {'aria': 16777216, 'key_buffer': 8388608}
                else:
                    assert settings == {'aria': 134217728, 'key_buffer': 134217728}
                if native:
                    # An alive MariaDB is insufficient: reject wrong credentials.
                    def wrong_secret(method, path, body=None, timeout=30):
                        if method == 'POST' and path.endswith('/exec'):
                            body = copy.deepcopy(body)
                            body['Env'] = ['MARIADB_ROOT_PASSWORD=wrong-fixture-password', 'MARIADB_ROOT_PASSWORD_FILE=', 'MYSQL_ROOT_PASSWORD_FILE=']
                        return api(method, path, body, timeout)
                    rejected = False
                    try:
                        engine.default_probe(wrong_secret, dbid, 'db', {})
                    except UpdateError:
                        rejected = True
                    assert rejected, 'Alive DB must not pass a bad-credential probe'
                    assert engine.default_probe(api, dbid, 'db', {})['verified']
                real_web_negative = os.environ.get('MAX_COMPONENT_REAL_WEB_NEGATIVE') == '1'
                if real_web_negative:
                    # Do this AFTER the packet/accounting checks: the true guard
                    # must reject our deliberately unsigned synthetic license.
                    # Never patch the immutable embedded licensing trust anchor.
                    webid = result['records'][tenants[0]+'-web']['new_id'] if expected == 'completed' else ids[tenants[0]+'-web']
                    from services.system_update_executor import PROBE
                    proof = execute(api, webid, PROBE)
                    assert proof['verified'] is True and proof['license'] is False
                    try:
                        engine.default_probe(api, webid, 'web', {})
                    except UpdateError:
                        pass
                    else:
                        raise AssertionError('Native Web accepted an unsigned fixture license')
                    write_json(directory/'WEB-NEGATIVE.private.json', {'publisher_integrity_verified': True,
                        'unsigned_fixture_license_rejected': True, 'licensing_trust_anchor_unchanged': True})
                unaffected = engine.stable(inspect(api, ids['tunnel'])) == engine.stable(plan['old']['tunnel'])
                watchdog_id = result['records']['watchdog']['new_id'] if expected == 'completed' else ids['watchdog']
                assert inspect(api, watchdog_id)['State']['Running']
                if native_aux:
                    tunnel_id = result['records']['tunnel']['new_id'] if expected == 'completed' else ids['tunnel']
                    assert engine.default_probe(api, tunnel_id, 'l2tp', {})['verified']
                    assert inspect(api, tunnel_id)['Image'] == api('GET', '/images/'+L2TP+'/json')['Id']
                    assert engine.default_probe(api, watchdog_id, 'autoheal', {})['verified']
                else:
                    assert unaffected
                record = {'case': case, 'status': result['status'], 'participants': len(tenants),
                    'mandatory_product_backups': len(plan['backups']), 'same_data_volume': True, 'accounting_and_sales_oracle_preserved': True,
                    'untargeted_transport_unchanged': None if native_aux else unaffected, 'watchdog_quiesced_and_restored': True, 'no_database_restore': True,
                    'effective_db_settings_verified': True, 'newer_commits_preserved': len(progressed),
                    'radius_packets': len(radius_packets), 'native_radius_sql_verified': native,
                    'wrong_db_credentials_rejected': native,
                    'native_aux_daemons_and_installed_health_verified': native_aux,
                    'native_watchdog_restarted_only_owned_sentinel': native_aux,
                    'actual_web_rejected_unsigned_license': real_web_negative,
                    'signed_same_version_web_image_switch': bool(web_target),
                    'aux_image_operation': 'same pinned original digest recreation; cached images, no registry download' if native_aux else 'fixture processes',
                    'instance_machine_license_key_and_web_secret_preserved': True,
                    'license_and_radius_probe': 'RADIUS/SQL real; unsigned synthetic fixture lease, no real NAS' if native else 'synthetic; not production acceptance'}
                results.append(record); write_json(root/'RESULTS.private.json', results)
                print(json.dumps(record), flush=True)
                clean_case()
        complete = True
    finally:
        clean_case()
        after = {r['Id']: engine.stable(inspect(api, r['Id'])) for r in api('GET', '/containers/json?all=1')}
        write_json(root/'PREEXISTING-AFTER.private.json',after)
        # Ignore changing runtime endpoint stats; stable Config/identity are compared.
        unchanged = all(k in after and all(after[k][f] == v[f] for f in ('Id', 'Image', 'Name', 'Config', 'HostConfig', 'Mounts', 'StartedAt')) for k, v in before.items())
        write_json(root/'FINAL.private.json', {'complete': complete, 'cases': len(results), 'owner': owner,
            'preexisting_identity_state_unchanged': unchanged, 'owned_containers_remaining': len(owned),
            'owned_volumes_remaining': len(volumes), 'owned_networks_remaining': len(networks),
            'customer_access': False, 'published': False, 'native_radius_sql': native, 'real_license_or_NAS_acceptance': False})
        assert unchanged


if __name__ == '__main__':
    main()

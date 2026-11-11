"""Lease-aware update deadlines. No license writes, grace extension or app import."""
import copy
import time
import json
import re


PROBE = r'''import json,hashlib,pathlib,subprocess,urllib.request,os
from core.config import APP_VERSION
from services.license_guard_service import get_active_license_status
from database.db import query_one,query_all
x=get_active_license_status(force_refresh=True)
y=query_one("""SELECT r.is_valid,
 TIMESTAMPDIFF(MICROSECOND,UTC_TIMESTAMP(6),r.valid_until)/1000000 remaining,
 CAST(r.valid_until AS CHAR) lease_deadline,
 r.package_hash=SHA2(i.raw_package_json,256) AND r.license_row_id=i.id bound
 FROM wisp_license_runtime_state r JOIN wisp_license_info i ON i.id=r.license_row_id
 WHERE r.id=1 AND i.id=(SELECT MAX(id) FROM wisp_license_info)""")
v=subprocess.run(['python','/opt/max-protection/verify.py'],capture_output=True,timeout=8)
try:
 with urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('APP_PORT','5090')+'/login',timeout=2) as response:http=response.status==200
except Exception:http=False
rows=query_all('SELECT radacctid,acctinputoctets,acctoutputoctets,acctsessiontime,acctuniqueid FROM radacct ORDER BY radacctid DESC LIMIT 300')
print(json.dumps(dict(version=APP_VERSION,http=http,verified=v.returncode==0,license=bool(x.get('valid')),
 lease=bool(y and y['is_valid'] and y['bound'] and float(y['remaining'])>0),
 lease_remaining=float(y['remaining']) if y else 0,
 lease_deadline=y['lease_deadline'] if y else None,
 publisher_key=hashlib.sha256(pathlib.Path('/opt/max-protection/runtime-public.pem').read_bytes()).hexdigest(),rows=rows),default=str))
'''


class SafetyError(RuntimeError):
    pass


# Leave at least 40 seconds for the forward transition, in addition to the
# existing 35-second recovery reserve and 10-second expiry margin.
MIN_SWITCH_LEASE = 85


def contract_program(old, contracts, native=False):
    custom = any(m.get('Destination')=='/run/max-rollout-kit' for m in old.get('Mounts',[]))
    required_paths={'/run/max-rollout-kit/web-gate.sh','/run/max-rollout-kit/normal_launch.py'}
    if custom and set(contracts or {})!=required_paths:
        raise SafetyError('Pinned native startup contract is required')
    if not contracts:return None
    if set(contracts)!=required_paths or any(not re.fullmatch('[a-f0-9]{64}',v) for v in contracts.values()):
        raise SafetyError('Invalid native startup contract')
    code='import json,pathlib,hashlib\nvalues=json.loads('+repr(json.dumps(contracts))+')\ngood=all(pathlib.Path(p).is_file() and not pathlib.Path(p).is_symlink() and hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()==h for p,h in values.items())\n'
    return code+("print('MAX_CONTROL_RESULT='+json.dumps({'verified':good}))" if native else "print(json.dumps({'verified':good}))")


def required(version):
    try:
        return tuple(int(v) for v in version.split('.')) >= (2, 7, 1)
    except (ValueError, AttributeError):
        raise SafetyError('Invalid update version')


class Budget:
    """Reserve 35s for rollback and 10s margin inside the observed signed lease.

    Callers must preflight BEFORE stopping Web. No serialized monotonic deadline
    is trusted across processes; recovery after interruption is always attempted.
    """
    def __init__(self, proof, clock=None):
        self.clock = clock or time.monotonic
        remaining = float(proof.get('lease_remaining', 0))
        if not all(proof.get(k) for k in ('verified', 'http', 'license', 'lease')) or not MIN_SWITCH_LEASE <= remaining <= 91:
            raise SafetyError('Insufficient verified lease budget; no switch allowed')
        self.forward = self.clock() + remaining - 45
        self.recovery = self.clock() + remaining - 10
        self.recovering = False

    @classmethod
    def for_recovery(cls, state, clock=None):
        value=cls.__new__(cls);value.clock=clock or time.monotonic
        now=value.clock();value.forward=now
        value.recovery=min(float(state.get('recovery_deadline',now+35)),now+35)
        value.recovering=True
        return value

    def timeout(self, requested=10):
        remaining = (self.recovery if self.recovering else self.forward) - self.clock()
        if remaining <= 0:
            if self.recovering:
                # Never refuse to start the retained Web because the deadline
                # was missed. Report unverified recovery instead of success.
                return min(float(requested), 3)
            raise SafetyError('Update lease deadline reached')
        return max(.05, min(float(requested), 10, remaining))

    def api(self, api):
        def bounded(method, path, body=None, timeout=30):
            # The stop allowance is part of the same global budget.
            for grace in (25,20,10):
                path = path.replace('/stop?t='+str(grace), '/stop?t=3')
            return api(method, path, body, timeout=self.timeout(timeout))
        bounded.update_budget = self
        return bounded


def replacement_spec(old, image, version, job):
    """Keep the exact frozen runtime; only image/version and transaction change."""
    allowed = {'User','AttachStdin','AttachStdout','AttachStderr','ExposedPorts','Tty',
               'OpenStdin','StdinOnce','Env','Cmd','Healthcheck','ArgsEscaped','Volumes',
               'WorkingDir','Entrypoint','NetworkDisabled','MacAddress','OnBuild',
               'Labels','StopSignal','StopTimeout','Shell'}
    value = {k:copy.deepcopy(v) for k,v in old['Config'].items() if k in allowed}
    value['Image'] = image
    value['Env'] = [v for v in value.get('Env', []) if not v.startswith('HOSTNAME=')]
    value.setdefault('Labels', {}).update({'max.version':version,'max.update.job':job})
    value['HostConfig'] = copy.deepcopy(old['HostConfig'])
    value['HostConfig'].pop('Binds', None)
    value['HostConfig']['Mounts'] = []
    for mount in old.get('Mounts', []):
        dest = mount['Destination']
        if dest in ('/app','/app/web') or any(dest == '/app/'+p or dest.startswith('/app/'+p+'/') for p in ('core','services','database','web/routes','web/templates')):
            raise SafetyError('Application source mount requires separate acceptance')
        if mount['Type'] in ('bind','volume'):
            value['HostConfig']['Mounts'].append(dict(Type=mount['Type'],Source=mount.get('Name') if mount['Type']=='volume' else mount['Source'],Target=dest,ReadOnly=not mount['RW']))
        elif mount['Type'] != 'tmpfs':
            raise SafetyError('Unknown storage mount')
    endpoints = {}
    for name, endpoint in old['NetworkSettings']['Networks'].items():
        endpoints[name] = dict(IPAMConfig=copy.deepcopy(endpoint.get('IPAMConfig') or {'IPv4Address':endpoint['IPAddress']}),Aliases=copy.deepcopy(endpoint.get('Aliases') or []))
    if not endpoints:
        raise SafetyError('Missing frozen network')
    value['NetworkingConfig'] = {'EndpointsConfig':endpoints}
    return value

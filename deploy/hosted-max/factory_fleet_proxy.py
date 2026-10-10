"""Tenant-scoped, read-only Docker metadata for hosted MAX dashboards."""
import http.client
import json
import os
import re
import socket
import socketserver
import sys
import subprocess
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import unquote, urlsplit, parse_qs

ALIASES = {'web': 'max_radius_web', 'radius_core': 'max_radius_core',
           'db': 'max_radius_db', 'autoheal': 'max_radius_autoheal',
           'l2tp': 'max_radius_l2tp'}
SHARED_SERVICES = {'autoheal': 'max_radius_autoheal', 'l2tp': 'max_panel_l2tp'}


def listener_health(item):
    """Read the central container's network namespace without executing inside it."""
    state = item.get('State', {})
    if not state.get('Running'):
        return 'unhealthy'
    pid = state.get('Pid')
    if not isinstance(pid, int) or pid <= 0:
        return 'unknown'
    readable = False
    for protocol in ('udp', 'udp6'):
        try:
            lines = Path(f'/proc/{pid}/net/{protocol}').read_text().splitlines()[1:]
            readable = True
            if any(len(parts := line.split()) > 1 and
                   parts[1].rsplit(':', 1)[-1].upper() == '06A5' for line in lines):
                return 'healthy'
        except (OSError, UnicodeError):
            pass
    return 'unhealthy' if readable else 'unknown'


def display_labels(tenant, service, shared=False, owner=None):
    labels = {'com.docker.compose.project': tenant,
              'com.docker.compose.service': service}
    if shared:
        # The project is a display context, not permission to control this service.
        labels.update({'max.panel.shared': 'true', 'max.panel.owner': owner or 'host'})
    return labels


def shared_view(tenant, service, listing, details, health_probe):
    state = details.get('State', {})
    health = state.get('Health', {}).get('Status')
    if service == 'l2tp' and not health:
        health = health_probe(details)
    status = listing.get('Status', state.get('Status', 'unknown'))
    if health in ('healthy', 'unhealthy', 'starting'):
        status = re.sub(r'\s*\((?:healthy|unhealthy|health: starting)\)', '', status)
        status += ' (health: starting)' if health == 'starting' else f' ({health})'
    status += ' | خدمة مشتركة؛ التحكم من لوحة المنظومات'
    if service == 'l2tp':
        status += ' | فحص استماع UDP 1701' if health != 'unknown' else ' | تعذر التحقق من الاستماع'
    owner = details.get('Config', {}).get('Labels', {}).get('com.docker.compose.project')
    return {key: listing.get(key) for key in ('Id', 'Image', 'Created')} | {
        'State': state.get('Status', listing.get('State', 'unknown')),
        'Status': status,
        'Labels': display_labels(tenant, service, shared=True, owner=owner),
        'Names': ['/' + SHARED_SERVICES[service], '/' + ALIASES[service]],
        '_shared_service': service, '_health': health}


class DockerConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect('/var/run/docker.sock')


def docker_read(path):
    connection = DockerConnection('localhost', timeout=5)
    try:
        connection.request('GET', path)
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError('Docker metadata unavailable')
        return json.loads(response.read())
    finally:
        connection.close()


def metadata_response(tenant, method, path, backend=docker_read, health_probe=listener_health):
    if method != 'GET':
        return 403, {'message': 'This endpoint only permits metadata reads'}
    route = unquote(urlsplit(path).path)
    route = re.sub(r'^/v\d+\.\d+(?=/)', '', route)
    if route == '/_ping':
        return 200, 'OK'
    if route == '/version':
        return 200, {'ApiVersion': '1.41'}
    # No environment, secrets, logs, filesystem or cross-tenant inspection.
    if route != '/containers/json' and not re.fullmatch(r'/containers/[A-Za-z0-9_.-]+/json', route):
        return 403, {'message': 'Unsupported metadata operation'}
    all_items = backend('/containers/json?all=1')
    owned = [item for item in all_items
             if item.get('Labels', {}).get('com.docker.compose.project') == tenant
             and item.get('Labels', {}).get('com.docker.compose.service') in ALIASES]
    visible = list(owned)
    own_services = {item['Labels']['com.docker.compose.service'] for item in owned}
    for service, name in SHARED_SERVICES.items():
        if service in own_services:
            continue
        matches = [item for item in all_items if '/' + name in item.get('Names', [])]
        if len(matches) != 1:
            continue
        listing = matches[0]
        details = backend('/containers/' + listing['Id'] + '/json')
        if details.get('Id') != listing['Id'] or details.get('Name') != '/' + name:
            continue
        visible.append(shared_view(tenant, service, listing, details, health_probe))
    if route == '/containers/json':
        result = []
        for item in visible:
            service = item['Labels']['com.docker.compose.service']
            result.append({**{key: item.get(key) for key in
                             ('Id', 'Image', 'Created', 'State', 'Status')},
                           'Labels': item['Labels'] if item.get('_shared_service') else display_labels(tenant, service),
                           'Names': item.get('Names', []) + ['/' + ALIASES[service]]})
        return 200, result
    target = route.split('/')[2]
    matches = [item for item in visible if
               item['Id'] == target or
               (len(target) >= 12 and item['Id'].startswith(target)) or
               target in [x.lstrip('/') for x in item.get('Names', [])] or
               target == ALIASES[item['Labels']['com.docker.compose.service']]]
    if len(matches) != 1:
        return 404, {'message': 'Container not available in this tenant'}
    item = backend('/containers/' + matches[0]['Id'] + '/json')
    # Recheck ownership after listing to avoid a stale/replaced container.
    labels = item.get('Config', {}).get('Labels', {})
    shared = matches[0].get('_shared_service')
    if shared:
        if item.get('Id') != matches[0]['Id'] or item.get('Name') != '/' + SHARED_SERVICES[shared]:
            return 404, {'message': 'Shared service ownership changed'}
        view = shared_view(tenant, shared, matches[0], item, health_probe)
        labels = view['Labels']
    elif labels.get('com.docker.compose.project') != tenant:
        return 404, {'message': 'Container ownership changed'}
    else:
        labels = display_labels(tenant, labels.get('com.docker.compose.service'))
    state = item.get('State', {})
    safe_state = {key: state.get(key) for key in
                  ('Status', 'Running', 'Restarting', 'OOMKilled', 'ExitCode')}
    if 'Health' in state:
        safe_state['Health'] = {'Status': state['Health'].get('Status')}
    elif shared and view.get('_health') in ('healthy', 'unhealthy', 'starting'):
        safe_state['Health'] = {'Status': view['_health']}
    return 200, {key: item.get(key) for key in ('Id', 'Name', 'Created')} | {
        'State': safe_state,
        'Config': {'Labels': labels, 'Image': item.get('Config', {}).get('Image')}}


def reset_authorized(web_id, core_id, action):
    # Read only the durable reset journal from the owning web container.
    code="""import json,sys
from database.db import get_connection
c=get_connection()
try:
 with c.cursor() as x:
  x.execute("SELECT progress FROM wisp_maintenance_jobs WHERE active_key='factory-reset' AND state IN ('running','failed','interrupted')")
  row=x.fetchone()
 print(json.dumps(json.loads(row['progress']) if row else None))
finally:c.close()
"""
    r=subprocess.run(['docker','exec',web_id,'python','-c',code],capture_output=True,text=True,timeout=10)
    if r.returncode:return False
    data=json.loads(r.stdout)
    if not isinstance(data,dict):return False
    core=next((v for v in data.get('cores',[]) if v.get('id')==core_id),None)
    if not core or core.get('was_running') is not True:return False
    if action=='stop':return data.get('stage')=='stopping' and bool(data.get('backup'))
    return action=='start'


def docker_reset_action(core_id, action):
    connection=DockerConnection('localhost',timeout=20)
    try:
        connection.request('POST','/containers/'+core_id+'/'+action+('?t=10' if action=='stop' else ''))
        response=connection.getresponse();response.read()
        if response.status not in (204,304):raise RuntimeError('Docker reset action failed')
        return {}
    finally:connection.close()


def respond(tenant, method, path, backend=docker_read, health_probe=listener_health,
            authorize=reset_authorized, control=docker_reset_action):
    if method=='GET':return metadata_response(tenant,method,path,backend,health_probe)
    url=urlsplit(path)
    route=re.sub(r'^/v\d+\.\d+(?=/)','',unquote(url.path))
    match=re.fullmatch(r'/containers/([a-f0-9]{64})/(stop|start)',route)
    if tenant!='max' or method!='POST' or not match:
        return 403,{'message':'Operation is not permitted by the reset controller'}
    core_id,action=match.groups()
    if parse_qs(url.query,keep_blank_values=True) not in ({},{'t':['10']}):
        return 403,{'message':'Unsupported reset action parameters'}
    owned=[r for r in backend('/containers/json?all=1') if r.get('Labels',{}).get('com.docker.compose.project')==tenant]
    cores=[r for r in owned if r.get('Id')==core_id and r.get('Labels',{}).get('com.docker.compose.service')=='radius_core']
    webs=[r for r in owned if r.get('Labels',{}).get('com.docker.compose.service')=='web']
    if len(cores)!=1 or len(webs)!=1:return 404,{'message':'Reset container ownership could not be verified'}
    info=backend('/containers/'+core_id+'/json');labels=info.get('Config',{}).get('Labels',{})
    if labels.get('com.docker.compose.project')!=tenant or labels.get('com.docker.compose.service')!='radius_core':
        return 404,{'message':'Reset container ownership changed'}
    if not authorize(webs[0]['Id'],core_id,action):
        return 403,{'message':'No authorized factory reset journal for this action'}
    return 200,control(core_id,action)


class Handler(BaseHTTPRequestHandler):
    def handle_request(self):
        try:
            status, data = respond(self.server.tenant, self.command, self.path)
        except Exception:
            status, data = 503, {'message': 'Docker metadata unavailable'}
        body = data.encode() if isinstance(data, str) else json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'text/plain' if isinstance(data, str) else 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = handle_request

    def log_message(self, *args):
        pass


class Server(socketserver.ThreadingMixIn,
             getattr(socketserver, 'UnixStreamServer', socketserver.TCPServer)):
    daemon_threads = True


def main():
    tenant = sys.argv[1]
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', tenant):
        raise SystemExit('Invalid tenant')
    if int(os.environ.get('LISTEN_PID', '0')) != os.getpid() or os.environ.get('LISTEN_FDS') != '1':
        raise SystemExit('Requires one systemd socket')
    endpoint = '/run/max-panel-fleet/' + tenant + '/docker.sock'
    server = Server(endpoint, Handler, bind_and_activate=False)
    server.socket.close()
    server.socket = socket.fromfd(3, socket.AF_UNIX, socket.SOCK_STREAM)
    server.tenant = tenant
    server.serve_forever()


if __name__ == '__main__':
    main()

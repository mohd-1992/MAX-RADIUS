"""Host-owned Docker compatibility view for one MAX tenant, never a daemon proxy.

The Compose labels below describe the scoped API view, not host Docker labels.
No environment, mounts, arbitrary exec, image mutation, or shared-service writes
are exposed. One diagnostic capability runs fixed radtest arguments in own Core.
"""
import datetime as dt
import http.client
import json
import os
from pathlib import Path
import re
import socket
import socketserver
import subprocess
import threading
import select
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit


class Denied(Exception):
    pass


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout=8):
        super().__init__('localhost', timeout=timeout)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def docker(method, path):
    conn = UnixHTTP('/var/run/docker.sock', timeout=12)
    try:
        conn.request(method, path, headers={'Connection': 'close'})
        response = conn.getresponse()
        data = response.read(4 * 1024 * 1024 + 1)
        if len(data) > 4 * 1024 * 1024 or response.status >= 400:
            raise Denied('Host service unavailable; no fallback target selected')
        return response.status, data
    finally:
        conn.close()


def validate_radius_probe(payload):
    required = {'username', 'password', 'secret', 'expected_core_id'}
    if not isinstance(payload, dict) or set(payload) != required:
        raise Denied('Invalid diagnostic request')
    limits = {'username': 253, 'password': 128, 'secret': 128, 'expected_core_id': 64}
    for field, limit in limits.items():
        value = payload[field]
        if (not isinstance(value, str) or len(value.encode('utf-8')) > limit
                or any(c in value for c in ('\x00', '\r', '\n'))):
            raise Denied('Invalid diagnostic argument')
    if (not payload['username'] or payload['username'].startswith('-')
            or not payload['secret'] or not re.fullmatch('[a-f0-9]{64}', payload['expected_core_id'])):
        raise Denied('Invalid diagnostic identity')
    return dict(payload)


def run_radius_probe(core_id, payload):
    """Fixed loopback radtest only; bounded output/time and no shell or fallback."""
    payload = validate_radius_probe(payload)
    if core_id != payload['expected_core_id']:
        raise Denied('Diagnostic target differs from the verified Core')
    command = ['docker', 'exec', core_id, 'radtest', payload['username'],
               payload['password'] or 'none', '127.0.0.1', '0', payload['secret']]
    process = None
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   bufsize=0)
        deadline = time.monotonic() + 4
        output = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                return dict(exit_code=-1, output='RADIUS probe timeout; no response verified')
            part = os.read(process.stdout.fileno(), min(4096, 65537-len(output)))
            if not part:
                break
            output.extend(part)
            if len(output) > 65536:
                raise Denied('Diagnostic output limit exceeded')
        code = process.wait(timeout=max(.01, deadline-time.monotonic()))
        lines = []
        for line in output.decode('utf-8', errors='replace').splitlines():
            if re.search(r'(?:User|Cleartext|CHAP|MS-CHAP)-Password\s*[=:]', line, re.I):
                lines.append('[credential redacted]')
            else:
                lines.append(line)
        cleaned = '\n'.join(lines)
        if len(cleaned.encode('utf-8')) > 65536:
            raise Denied('Diagnostic output limit exceeded')
        return dict(exit_code=int(code), output=cleaned)
    except subprocess.TimeoutExpired:
        return dict(exit_code=-1, output='RADIUS probe timeout; no response verified')
    except OSError:
        return dict(exit_code=-1, output='Scoped RADIUS probe unavailable; no response verified')
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()  # Only the child Docker CLI, never a tenant service.
                process.wait()
            if process.stdout is not None:
                process.stdout.close()


class Scope:
    def __init__(self, config, api=docker, probe_runner=None):
        self.config, self.api = config, api
        self.probe_runner = probe_runner or run_radius_probe
        self.probe_lock = threading.Lock()

    def state(self):
        p = Path(self.config['binding'])
        if p.is_symlink():
            raise Denied('Binding must be host owned')
        b = json.loads(p.read_text())
        if (b['id'] != self.config['tenant_id'] or b['slug'] != self.config['slug']
                or b['domain'] != b['slug'] + '.max-r.cc' or b['state'] != 'ready'):
            raise Denied('Tenant binding changed')
        rows = {}
        for role in ('web', 'core', 'db'):
            name = b['containers'][role] if role != 'db' else 'max-db-' + b['pool']
            if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', name):
                raise Denied('Invalid owned service name')
            _, raw = self.api('GET', '/containers/' + name + '/json')
            c = json.loads(raw)
            labels = c['Config'].get('Labels') or {}
            if not re.fullmatch('[a-f0-9]{64}', c['Id']):
                raise Denied('Invalid service identity')
            if role == 'db':
                if labels.get('max.pool') != b['pool']:
                    raise Denied('Shared engine ownership differs')
            elif (labels.get('max.platform') != 'max-r'
                  or labels.get('max.tenant') != b['id']
                  or labels.get('max.service') != role
                  or c['Config']['Image'] != b['images'][role]):
                raise Denied('Tenant service ownership or image differs')
            rows[role] = c
        return b, rows

    @staticmethod
    def project(b, role, c):
        labels = {k: v for k, v in (c['Config'].get('Labels') or {}).items()
                  if k in ('max.platform', 'max.tenant', 'max.service', 'max.pool')}
        labels.update({'com.docker.compose.project': 'mrcc-' + b['id'],
                       'com.docker.compose.service': {'web': 'web', 'core': 'radius_core', 'db': 'mariadb'}[role],
                       'com.docker.compose.oneoff': 'False',
                       'max.control.view': 'tenant-scoped-v1',
                       'max.control.read_only': 'true' if role == 'db' else 'false'})
        state = {k: v for k, v in c['State'].items()
                 if k in ('Status', 'Running', 'Paused', 'Restarting', 'OOMKilled', 'ExitCode', 'StartedAt', 'FinishedAt')}
        if 'Health' in c['State']:
            state['Health'] = {'Status': c['State']['Health']['Status']}
        return {'Id': c['Id'], 'Name': c['Name'], 'Created': c['Created'],
                'Image': c['Image'], 'Config': {'Image': c['Config']['Image'], 'Labels': labels},
                'State': state, 'RestartCount': c.get('RestartCount', 0)}

    def handle(self, method, target, payload=None):
        if '%' in target or len(target) > 1024:
            raise Denied('Noncanonical request')
        url = urlsplit(target)
        if url.scheme or url.netloc or url.fragment:
            raise Denied('Local requests only')
        b, rows = self.state()
        probe_request = method == 'POST' and url.path == '/max/v1/radius/probe' and not url.query
        if payload is not None and not probe_request:
            raise Denied('Request bodies are not accepted for this operation')
        if probe_request:
            arguments = validate_radius_probe(payload)
            core = rows['core']
            if arguments['expected_core_id'] != core['Id']:
                raise Denied('The requested Core generation is no longer current')
            if not self.probe_lock.acquire(blocking=False):
                raise Denied('A tenant diagnostic probe is already running')
            try:
                b2, fresh = self.state()
                state = fresh['core']['State']
                if (b2['id'] != b['id'] or fresh['core']['Id'] != core['Id']
                        or not state.get('Running') or state.get('Paused') or state.get('Restarting')):
                    raise Denied('Core changed or is unavailable before the probe')
                result = self.probe_runner(core['Id'], arguments)
                return 200, json.dumps(dict(schema=1, tenant_id=b['id'], core_id=core['Id'],
                                            exit_code=result['exit_code'], output=result['output'])).encode()
            finally:
                self.probe_lock.release()
        if method == 'GET' and url.path == '/max/v1/state' and not url.query:
            services = {}
            for role, c in rows.items():
                row = self.project(b, role, c)
                row['Config']['Labels'] = {k: v for k, v in (c['Config'].get('Labels') or {}).items()
                                           if k in ('max.platform', 'max.tenant', 'max.service', 'max.pool')}
                row['read_only'] = role == 'db'
                services[role] = row
            return 200, json.dumps({'schema': 1, 'tenant_id': b['id'], 'instance': b['instance'],
                                    'domain': b['domain'], 'services': services,
                                    'capabilities': ['radius_probe_v1']}).encode()
        if method == 'GET' and url.path == '/containers/json':
            if set(parse_qs(url.query)) - {'all'}:
                raise Denied('Unsupported query')
            result = []
            for role, c in rows.items():
                p = self.project(b, role, c)
                health = p['State'].get('Health', {}).get('Status')
                result.append({'Id': p['Id'], 'Names': [p['Name']],
                               'Image': p['Config']['Image'], 'ImageID': p['Image'],
                               'Labels': p['Config']['Labels'],
                               'State': p['State']['Status'],
                               'Status': p['State']['Status'] + (' (' + health + ')' if health else ''),
                               'Created': int(dt.datetime.fromisoformat(p['Created'].replace('Z', '+00:00')).timestamp())})
            return 200, json.dumps(result).encode()
        match = re.fullmatch(r'/containers/([a-zA-Z0-9_-]{1,100})/(json|logs|restart|start|stop)', url.path)
        if not match:
            raise Denied('Operation outside tenant control scope')
        reference, operation = match.groups()
        aliases = {'max_radius_web': 'web', 'wisp_web_v2': 'web', 'max-radius-wisp-web': 'web',
                   'max_radius_core': 'core', 'wisp_freeradius_v2': 'core', 'max-radius-freeradius': 'core',
                   'max_radius_db': 'db', 'wisp_mariadb_v2': 'db'}
        role = aliases.get(reference)
        if role is None:
            found = [k for k, c in rows.items() if reference in (c['Id'], c['Name'].lstrip('/'))]
            if len(found) != 1:
                raise Denied('Service does not belong to this tenant')
            role = found[0]
        c = rows[role]
        if method == 'GET' and operation == 'json' and not url.query:
            return 200, json.dumps(self.project(b, role, c)).encode()
        if method == 'GET' and operation == 'logs' and role != 'db':
            query = parse_qs(url.query)
            if set(query) - {'stdout', 'stderr', 'tail', 'timestamps'}:
                raise Denied('Unsupported log query')
            try:
                tail = int(query.get('tail', ['100'])[0])
            except ValueError:
                raise Denied('Invalid log limit') from None
            if not 1 <= tail <= 200:
                raise Denied('Log limit exceeded')
            return self.api('GET', '/containers/' + c['Id'] + '/logs?stdout=1&stderr=1&timestamps=1&tail=' + str(tail))
        if method == 'POST' and operation in ('restart', 'start', 'stop'):
            if role == 'db':
                raise Denied('Shared database is read-only; use the host control panel')
            if role == 'web' and operation == 'stop':
                raise Denied('Stopping Web from inside the tenant is not allowed')
            if parse_qs(url.query) not in ({}, {'t': ['5']}):
                raise Denied('Unsupported control timeout')
            # Re-read the host binding and full service identity immediately before effects.
            b2, fresh = self.state()
            if b2['id'] != b['id'] or fresh[role]['Id'] != c['Id']:
                raise Denied('Service changed before operation')
            return self.api('POST', '/containers/' + c['Id'] + '/' + operation + '?t=5')
        raise Denied('Operation not allowed')


def serve(config):
    scope = Scope(config)
    path = Path(config['socket'])
    if path.parent.is_symlink():
        raise Denied('Invalid socket directory')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        if not path.is_socket():
            raise Denied('Refusing to replace a non-socket')
        path.unlink()

    class Handler(BaseHTTPRequestHandler):
        def dispatch(self):
            try:
                if self.headers.get('Transfer-Encoding'):
                    raise Denied('Transfer encoding is not accepted')
                lengths = self.headers.get_all('Content-Length', [])
                if len(lengths) > 1:
                    raise Denied('Ambiguous request length')
                try:
                    length = int(lengths[0]) if lengths else 0
                except ValueError:
                    raise Denied('Invalid request length') from None
                payload = None
                if length:
                    if (self.command != 'POST' or self.path != '/max/v1/radius/probe'
                            or not 0 < length <= 8192
                            or self.headers.get_content_type() != 'application/json'):
                        raise Denied('Request body outside diagnostic scope')
                    try:
                        payload = json.loads(self.rfile.read(length).decode('utf-8'))
                    except (ValueError, UnicodeDecodeError):
                        raise Denied('Invalid diagnostic JSON') from None
                status, raw = scope.handle(self.command, self.path, payload)
            except Denied as error:
                status, raw = 403, json.dumps({'message': str(error)}).encode()
            except Exception:
                status, raw = 503, b'{"message":"Host verification unavailable"}'
            self.send_response(status)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(raw)

        do_GET = dispatch
        do_POST = dispatch
        do_DELETE = dispatch
        do_PUT = dispatch

        def log_message(self, *_):
            pass

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(10)
            return connection, address

    with Server(str(path), Handler) as server:
        os.chmod(path, 0o600)
        server.serve_forever()


if __name__ == '__main__':
    import sys
    serve(json.loads(Path(sys.argv[1]).read_text()))

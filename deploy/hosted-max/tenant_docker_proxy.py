"""Host-owned Docker compatibility view for one MAX tenant, never a daemon proxy.

The Compose labels below describe the scoped API view, not host Docker labels.
No environment, mounts, exec, image mutation, or shared-service writes are exposed.
"""
import datetime as dt
import http.client
import json
import os
from pathlib import Path
import re
import socket
import socketserver
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


class Scope:
    def __init__(self, config, api=docker):
        self.config, self.api = config, api

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

    def handle(self, method, target):
        if '%' in target or len(target) > 1024:
            raise Denied('Noncanonical request')
        url = urlsplit(target)
        if url.scheme or url.netloc or url.fragment:
            raise Denied('Local requests only')
        b, rows = self.state()
        if method == 'GET' and url.path == '/max/v1/state' and not url.query:
            services = {}
            for role, c in rows.items():
                row = self.project(b, role, c)
                row['Config']['Labels'] = {k: v for k, v in (c['Config'].get('Labels') or {}).items()
                                           if k in ('max.platform', 'max.tenant', 'max.service', 'max.pool')}
                row['read_only'] = role == 'db'
                services[role] = row
            return 200, json.dumps({'schema': 1, 'tenant_id': b['id'], 'instance': b['instance'],
                                    'domain': b['domain'], 'services': services}).encode()
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
                if int(self.headers.get('Content-Length', '0')) != 0 or self.headers.get('Transfer-Encoding'):
                    raise Denied('Request bodies are not accepted')
                status, raw = scope.handle(self.command, self.path)
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

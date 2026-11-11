"""Read-only native tenant CPU endpoint; shared database engines are excluded.

Installed as a separate host service for an explicitly pinned binding. This is
not a Docker API proxy and never starts, stops or executes a container.
"""
import http.client
from http.server import BaseHTTPRequestHandler
import json
import math
import os
from pathlib import Path
import re
import socket
import socketserver
import stat
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor


def docker(path):
    connection = http.client.HTTPConnection('localhost', timeout=4)
    connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.sock.settimeout(4)
    connection.sock.connect('/var/run/docker.sock')
    try:
        connection.request('GET', '/v1.45' + path)
        response = connection.getresponse()
        raw = response.read(4 * 1024 * 1024 + 1)
        if response.status != 200 or len(raw) > 4 * 1024 * 1024:
            raise ValueError('Owned service metric unavailable')
        return json.loads(raw)
    finally:
        connection.close()


class Metrics:
    def __init__(self, config, backend=docker, clock=time.time, loader=None):
        self.config, self.backend, self.clock = config, backend, clock
        self.loader = loader or self.binding
        self.lock, self.cache = threading.Lock(), None

    def binding(self):
        path = Path(self.config['binding'])
        stat = path.lstat()
        if path.is_symlink() or not path.is_file() or stat.st_uid != 0 or stat.st_mode & 0o022:
            raise ValueError('Host-owned binding required')
        return json.loads(path.read_text())

    def owned(self):
        row = self.loader()
        if (any(row.get(k) != self.config[k] for k in ('id', 'slug', 'domain'))
                or row.get('state') != 'ready' or row['domain'] != row['slug'] + '.max-r.cc'):
            raise ValueError('Tenant binding changed')
        services = {}
        for role in ('web', 'core'):
            name = row['containers'][role]
            if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', name):
                raise ValueError('Invalid service reference')
            value = self.backend('/containers/' + name + '/json')
            labels = value.get('Config', {}).get('Labels') or {}
            if (not re.fullmatch('[a-f0-9]{64}', value.get('Id', ''))
                    or not value.get('State', {}).get('Running')
                    or any(labels.get(k) != v for k, v in {
                        'max.platform': 'max-r', 'max.tenant': row['id'], 'max.service': role}.items())
                    or labels.get('max.pool') or labels.get('max.panel.shared') == 'true'
                    or value['Config'].get('Image') != row['images'][role]):
                raise ValueError('Service ownership or image changed')
            services[role] = value['Id']
        if len(set(services.values())) != 2:
            raise ValueError('Duplicate service identity')
        return services

    def collect(self, services):
        def sample(pair):
            role, identity = pair
            value = self.backend('/containers/' + identity + '/stats?stream=false')
            current, previous = value['cpu_stats'], value['precpu_stats']
            system = current['system_cpu_usage'] - previous['system_cpu_usage']
            used = current['cpu_usage']['total_usage'] - previous['cpu_usage']['total_usage']
            if previous['system_cpu_usage'] <= 0 or system <= 0 or used < 0:
                raise ValueError('CPU sample not ready')
            percent = used / system * 100.0
            if not math.isfinite(percent) or not 0 <= percent <= 100:
                raise ValueError('CPU sample out of bounds')
            return role, percent
        with ThreadPoolExecutor(max_workers=2) as executor:
            values = dict(executor.map(sample, services.items()))
        if self.owned() != services:
            raise ValueError('Owned service changed during sampling')
        return {'schema': 1, 'tenant': self.config['slug'], 'cpu_available': True,
                'cpu_percent': round(min(100.0, sum(values.values())), 2),
                'cpu_basis': 'host_capacity', 'sampled_at': self.clock(),
                'services': {k: {'cpu_percent': round(v, 2)} for k, v in values.items()}}

    def handle(self, method, target):
        if method != 'GET' or target != '/_max_metrics/v1/resources':
            return 403, {'message': 'Scoped read-only metric required'}
        try:
            with self.lock:
                services = self.owned()  # Validate even a cached response.
                if self.cache:
                    identities, value = self.cache
                    if identities == services and 0 <= self.clock() - value['sampled_at'] < 3:
                        return 200, value
                value = self.collect(services)
                self.cache = services, value
                return 200, value
        except Exception:
            self.cache = None
            return 503, {'cpu_available': False, 'cpu_percent': None}


def prepare_socket(path):
    """Reject a live listener; remove only a stale, root-owned socket inode."""
    if path.is_symlink():
        raise ValueError('Unexpected socket link')
    if not path.exists():
        return
    previous = path.lstat()
    if previous.st_uid != 0 or not stat.S_ISSOCK(previous.st_mode):
        raise ValueError('Host-owned socket required')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        try:
            probe.connect(str(path))
        except FileNotFoundError:
            return
        except ConnectionRefusedError:
            current = path.lstat()
            if (current.st_dev, current.st_ino) != (previous.st_dev, previous.st_ino):
                raise ValueError('Socket changed during recovery')
            path.unlink()
        else:
            raise ValueError('Metric endpoint is already served')


def serve(config):
    path = Path(config['socket'])
    expected = Path('/var/lib/max-host-r/tenants') / config['slug'] / 'storage/.host-control/resources.sock'
    if (not re.fullmatch('[a-z][a-z0-9-]{0,30}', config['slug']) or path != expected
            or path.parent.is_symlink() or path.is_symlink()):
        raise ValueError('Unexpected tenant socket')
    if not path.parent.is_dir():
        raise ValueError('Existing host-control directory required')
    metrics = Metrics(config)
    metrics.owned()
    prepare_socket(path)

    class Handler(BaseHTTPRequestHandler):
        def dispatch(self):
            try:
                _, uid, _ = struct.unpack('3i', self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != 0 or self.headers.get('Transfer-Encoding') or int(self.headers.get('Content-Length', '0')) != 0:
                    status, value = 403, {'message': 'Read-only request required'}
                else:
                    status, value = metrics.handle(self.command, self.path)
            except Exception:
                status, value = 503, {'cpu_available': False, 'cpu_percent': None}
            raw = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = dispatch
        do_POST = dispatch
        do_PUT = dispatch
        do_DELETE = dispatch

        def log_message(self, *_):
            pass

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(6)
            return connection, address

    with Server(str(path), Handler) as server:
        os.chmod(path, 0o600)
        server.serve_forever()


if __name__ == '__main__':
    import sys
    if len(sys.argv) != 2:
        raise SystemExit('Host-owned tenant metrics configuration required')
    serve(json.loads(Path(sys.argv[1]).read_text()))

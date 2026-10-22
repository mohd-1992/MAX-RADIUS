"""Bounded, read-only integrity observation, independent of RADIUS/licensing.

Startup verification remains owned by the release entrypoint. This observer never
exits the process, writes SQL, sends messages, repairs files, or changes a lease.
"""
import base64
import hashlib
import json
import logging
import threading
from pathlib import Path

_lock = threading.Lock()
_started = False
_status = {'state': 'not_checked'}


def check_runtime(root=Path('/app'), control=Path('/opt/max-protection')):
    from cryptography.hazmat.primitives import serialization
    root, control = Path(root).resolve(), Path(control)
    try:
        payload = (control / 'runtime-manifest.json').read_bytes()
        public = serialization.load_pem_public_key((control / 'runtime-public.pem').read_bytes())
        public.verify(base64.b64decode((control / 'runtime-manifest.sig').read_bytes(), validate=True), payload)
        expected = json.loads(payload)['files']
        for relative, digest in expected.items():
            path = (root / relative).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                return {'state': 'modified'}
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                return {'state': 'modified'}
        for path in root.rglob('*'):
            rel = path.relative_to(root)
            if not path.is_file() or rel.parts[0] == 'storage' or rel.parts[:3] == ('web', 'static', 'uploads'):
                continue
            if rel.as_posix() not in expected:
                return {'state': 'modified'}
        return {'state': 'verified'}
    except Exception:
        return {'state': 'unavailable'}


def integrity_status():
    with _lock:
        return dict(_status)


def start_integrity_observer(interval=300):
    global _started
    with _lock:
        if _started:
            return
        _started = True

    def observe():
        global _status
        previous = None
        wait = threading.Event()
        while True:
            result = check_runtime()
            with _lock:
                _status = result
            if result['state'] != previous and result['state'] != 'verified':
                logging.getLogger(__name__).warning('Publisher runtime integrity observation: %s', result['state'])
            previous = result['state']
            wait.wait(max(60, interval))

    threading.Thread(target=observe, name='runtime-integrity-observer', daemon=True).start()

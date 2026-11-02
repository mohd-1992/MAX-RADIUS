"""Explicit host-owned protocol4 enrollment and controller adapter; no app imports.

Installation sockets must never own a shared DB. A host authority uses a separate
socket kept outside tenant mounts; its signed bundle covers every participant.
Legacy protocol3 channels are deliberately not upgraded by merely importing this.
"""
import base64
import hashlib
import json
from pathlib import Path

from services import system_update_components as engine
from services.system_update_runtime import Docker, write_json


def read(root, name):
    path = Path(root)/name
    engine.require(not Path(name).is_absolute() and '..' not in Path(name).parts, 'Invalid protected path')
    for parent in path.parents:
        if parent == Path(root): break
        engine.require(not parent.is_symlink(), 'Invalid journal directory')
    engine.require(path.is_file() and not path.is_symlink(), 'Missing protected enrollment')
    return json.loads(path.read_text(encoding='utf-8'))


def enroll(root, registry, trust, api=None):
    import fcntl
    root=Path(root)
    engine.require(root.is_absolute() and root.is_dir() and not root.is_symlink(), 'Invalid enrollment root')
    with (root/'components.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _enroll(root,registry,trust,api)


def _enroll(root, registry, trust, api=None):
    """Operator-only, one-time admission. Does not stop/start/create a service."""
    import os
    root = Path(root)
    engine.require(root.is_absolute() and root.is_dir() and not root.is_symlink(), 'Invalid enrollment root')
    engine.require(root.stat().st_mode & 0o077 == 0, 'Enrollment root must be private')
    engine.validate_registry(registry)
    key = base64.b64decode(trust['public_key'], validate=True)
    engine.require(len(key) == 32 and hashlib.sha256(key).hexdigest() == trust['key_fingerprint'], 'Invalid pinned trust')
    old_trust = root/'update-channel.private.json'
    if old_trust.exists():
        old = read(root, old_trust.name)
        engine.require(all(old.get(k) == trust[k] for k in ('public_key','key_fingerprint')), 'Enrollment cannot change installed trust')
    names = ('component-registry.private.json','component-trust.private.json','component-controller.private.json')
    engine.require(not any((root/name).exists() for name in names), 'Enrollment already attempted; do not replay')
    snapshots = engine.snapshot(api or Docker(), registry)
    for key, row in snapshots.items():
        for mount in row['Mounts']:
            source = mount.get('Source')
            if source:
                engine.require(not root.is_relative_to(Path(source)), 'Controller state cannot be tenant-mounted')
        if registry['components'][key]['role'] == 'db':
            scope = (row['Config'].get('Labels') or {}).get('max.db.scope')
            engine.require(scope in ('dedicated','shared'), 'Explicit DB scope required')
            engine.require(scope != 'shared' or registry['authority'] == 'host', 'Shared engine requires host controller')
    write_json(root/names[0], registry)
    write_json(root/names[1], trust)
    # Admission marker is written last; a partial enrollment is never enabled.
    write_json(root/names[2], {'schema':1, 'enabled':True, 'registry_revision':engine.registry_revision(registry)})
    for name in names:
        os.chmod(root/name, 0o600)
    return {'enrolled':True,'protocol':4,'authority':registry['authority'],
            'participants':len(registry['tenants']),'services_changed':False}


class ComponentController:
    def __init__(self, root, api=None, probe=engine.default_probe):
        self.root, self.api, self.probe = Path(root), api or Docker(), probe

    def config(self):
        marker = read(self.root,'component-controller.private.json')
        registry = engine.validate_registry(read(self.root,'component-registry.private.json'))
        engine.require(marker == {'schema':1,'enabled':True,'registry_revision':engine.registry_revision(registry)}, 'Component admission changed')
        return registry

    def engine(self):
        registry = self.config()
        return {'success':True,'engine_protocol':4,'authority':registry['authority'],
                'capabilities':['signed_components','scoped_registry','verified_rollback','preserve_latest_data'],
                'participants':len(registry['tenants'])}

    def progress(self):
        self.config()
        pointer = self.root/'component-current.private.json'
        if not pointer.exists(): return {'status':'idle'}
        job = read(self.root,pointer.name)['job']
        import re
        engine.require(re.fullmatch('[a-f0-9]{32}',job), 'Invalid component pointer')
        state = read(self.root,'component-jobs/'+job+'/state.json')
        return {k:v for k,v in state.items() if k in ('job','status','message')}

    def submit(self, body):
        self.config()
        state = engine.submit(self.root, body, self.api, self.probe)
        return {k:v for k,v in state.items() if k in ('job','status','message')}

    def recover(self, body):
        self.config()
        engine.require(isinstance(body,dict) and set(body)=={'job'}, 'Recovery requires current job only')
        state = engine.recover(self.root, body['job'], self.api, self.probe)
        return {k:v for k,v in state.items() if k in ('job','status','message')}

    def resume_interrupted(self):
        """Called under the singleton server lock; rollback only, never re-apply."""
        state = self.progress()
        if state['status'] in ('preparing','queued','running','recovery_required'):
            return self.recover({'job':state['job']})
        return state


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Local operator-only component enrollment')
    parser.add_argument('--root',required=True,type=Path)
    parser.add_argument('--registry',required=True,type=Path)
    parser.add_argument('--trust',required=True,type=Path)
    args = parser.parse_args()
    print(json.dumps(enroll(args.root,json.loads(args.registry.read_text()),json.loads(args.trust.read_text()))))

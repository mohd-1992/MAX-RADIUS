"""Two-phase activation/rollback with explicit service-control callbacks.

Importing this module never controls a service. Caller supplies the authorized
host's daemon-reload and effective ExecStartPre readers. Keep the helper until
systemd has demonstrably forgotten its invocation; preserve all archived bytes.
"""
import json
from pathlib import Path
import stat

import managed_files as files

HELPER = '/usr/local/libexec/max-radius-clock-ready.py'


def load(root, journal):
    root, journal = Path(root).resolve(), Path(journal).resolve()
    state = json.loads((journal/'journal.json').read_text())
    if Path(state['root']).resolve() != root:
        raise ValueError('Journal root mismatch')
    allowed = set(files.FILES.values())
    if len(state['files']) != 2 or {v['path'] for v in state['files']} != allowed:
        raise ValueError('Unexpected managed files')
    for item in state['files']:
        path = files.owned_path(root, item['path'])
        if path.exists() and files.digest(path.read_bytes()) != item['sha256']:
            raise ValueError('Changed managed file; preserve it')
        archive = files.owned_path(journal, 'active-rollback-retained/'+item['path'])
        if archive.exists() and files.digest(archive.read_bytes()) != item['sha256']:
            raise ValueError('Changed retained archive; preserve it')
    return root, journal, state


def save(journal, state, status):
    state['status'] = status
    files.atomic_write(journal/'journal.json', json.dumps(state, indent=2).encode(), 0o600)


def activate(root, journal, reload_unit, read_exec_start_pre):
    root, journal, state = load(root, journal)
    if state['status'] != 'staged-not-activated':
        raise ValueError('Activation already attempted or outside staged state')
    if not all(files.owned_path(root, v['path']).is_file() for v in state['files']):
        raise ValueError('Incomplete staged files')
    save(journal, state, 'activating')
    reload_unit()
    if HELPER not in read_exec_start_pre():
        raise ValueError('Effective unit did not load the helper')
    save(journal, state, 'active')
    return state


def archive_remove(root, journal, item):
    path = files.owned_path(root, item['path'])
    archive = files.owned_path(journal, 'active-rollback-retained/'+item['path'])
    if path.exists():
        data = path.read_bytes()
        if files.digest(data) != item['sha256']:
            raise ValueError('File changed before rollback')
        if not archive.exists():
            files.atomic_create(archive, data, stat.S_IMODE(path.stat().st_mode))
        if files.digest(archive.read_bytes()) != item['sha256'] or files.digest(path.read_bytes()) != item['sha256']:
            raise ValueError('Archive or source changed; preserve both')
        path.unlink()
        files.sync_directory(path.parent)
    elif not archive.exists():
        raise ValueError('Missing source and archive')


def rollback_loaded(root, journal, reload_unit, read_exec_start_pre):
    root, journal, state = load(root, journal)
    if state['status'] == 'active-rolled-back':
        return state
    if state['status'] not in ('active', 'activating', 'deactivating', 'inactive-helper-retained'):
        raise ValueError('Not an activated-service round')
    helper = next(v for v in state['files'] if v['path'].startswith('usr/'))
    dropin = next(v for v in state['files'] if v['path'].startswith('etc/'))
    save(journal, state, 'deactivating')
    archive_remove(root, journal, dropin)
    # A failure or interruption here retains the helper for cached systemd state.
    reload_unit()
    if HELPER in read_exec_start_pre():
        raise ValueError('Loaded unit still references helper; keep it')
    if files.owned_path(root, dropin['path']).exists():
        raise ValueError('Drop-in appeared during rollback; keep helper')
    save(journal, state, 'inactive-helper-retained')
    archive_remove(root, journal, helper)
    save(journal, state, 'active-rolled-back')
    return state

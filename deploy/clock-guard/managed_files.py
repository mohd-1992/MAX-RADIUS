"""Stage/rollback only two owned files; never starts/reloads/stops any service.

Activation is deliberately separate and must use the explicitly authorized host.
Original/unrecognized paths are refused. Rollback archives, never deletes.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import stat

FILES = {
    'clock_ready.py': 'usr/local/libexec/max-radius-clock-ready.py',
    '99-max-radius-clock-ready.conf': 'etc/systemd/system/docker.service.d/99-max-radius-clock-ready.conf',
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sync_directory(path):
    if os.name!='posix':return
    fd=os.open(path,os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def durable_mkdir(path):
    missing=[];parent=Path(path)
    while not parent.exists():missing.append(parent);parent=parent.parent
    Path(path).mkdir(parents=True,exist_ok=True)
    for directory in reversed(missing):
        sync_directory(directory.parent);sync_directory(directory)


def owned_path(root, relative):
    base = Path(root).resolve()
    path = base
    for component in Path(relative).parts:
        path = path / component
        if path.is_symlink():
            raise ValueError('Refusing symlink in managed destination')
    if not path.resolve().is_relative_to(base):
        raise ValueError('Managed destination escapes root')
    return path


def atomic_write(path, data, mode=0o644):
    durable_mkdir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix='.max-clock-stage-', dir=path.parent)
    try:
        os.chmod(temporary,mode)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


def atomic_create(path,data,mode=0o644):
    """Publish complete bytes exclusively; never truncate a competing file."""
    durable_mkdir(path.parent)
    fd,temporary=tempfile.mkstemp(prefix='.max-clock-stage-',dir=path.parent)
    try:
        os.chmod(temporary,mode)
        with os.fdopen(fd,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.link(temporary,path)
        sync_directory(path.parent)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink();sync_directory(path.parent)


def stage(root, journal, source=None):
    root = Path(root).resolve()
    journal = Path(journal).resolve()
    source = Path(source or __file__).resolve().parent if source is None else Path(source).resolve()
    if journal.exists():
        raise ValueError('Round journal already exists; inspect before replay')
    planned = []
    for name, relative in FILES.items():
        data = (source / name).read_bytes()
        path = owned_path(root, relative)
        if path.exists():
            raise ValueError('Existing target is preserved; no overwrite allowed')
        planned.append((relative, path, data))
    durable_mkdir(journal.parent)
    journal.mkdir(mode=0o700)
    sync_directory(journal.parent)
    state = {'root': str(root), 'status': 'staging', 'files': []}
    # Prepared manifest precedes any target write, enabling a rollback after interruption.
    state['files'] = [{'path': relative, 'sha256': digest(data)} for relative, _, data in planned]
    atomic_write(journal / 'journal.json', json.dumps(state, indent=2).encode(), 0o600)
    try:
        for relative, path, data in planned:
            atomic_create(path, data)
        if any(digest(path.read_bytes()) != digest(data) for _,path,data in planned):
            raise ValueError('Staged file verification failed')
        state['status'] = 'staged-not-activated'
        atomic_write(journal / 'journal.json', json.dumps(state, indent=2).encode(), 0o600)
    except Exception:
        rollback(root, journal)
        raise
    return state


def rollback(root, journal):
    """Undo staged, NOT activated files. A loaded systemd unit needs a separate
    disable/reload phase while its helper remains present; never use this as an
    activated-service rollback. This module deliberately cannot reload services.
    """
    root, journal = Path(root).resolve(), Path(journal).resolve()
    state = json.loads((journal / 'journal.json').read_text())
    if Path(state['root']).resolve() != root:
        raise ValueError('Rollback root differs from staged root')
    allowed = set(FILES.values())
    if (len(state['files']) != len(allowed)
            or {v['path'] for v in state['files']} != allowed):
        raise ValueError('Rollback manifest is outside the owned file set')
    if state['status'] == 'rolled-back':
        return state
    if state['status'] not in ('staging','staged-not-activated','rolling-back'):
        raise ValueError('Unknown rollback state')
    checked = []
    for item in state['files']:
        path = owned_path(root, item['path'])
        if path.exists() and digest(path.read_bytes()) != item['sha256']:
            raise ValueError('File changed after staging; rollback refuses it')
        archive = owned_path(journal,'rollback-retained/'+item['path'])
        if archive.exists() and digest(archive.read_bytes())!=item['sha256']:
            raise ValueError('Rollback archive conflict; manual review required')
        checked.append((item,path,archive))
    state['status']='rolling-back'
    atomic_write(journal/'journal.json',json.dumps(state,indent=2).encode(),0o600)
    # Remove the not-yet-loaded drop-in before removing its helper.
    for item,path,archive in reversed(checked):
        if path.exists():
            data=path.read_bytes()
            if digest(data)!=item['sha256']:raise ValueError('File changed during rollback')
            if not archive.exists():atomic_create(archive,data,stat.S_IMODE(path.stat().st_mode))
            if digest(archive.read_bytes())!=item['sha256']:raise ValueError('Archive verification failed')
            if digest(path.read_bytes())!=item['sha256']:raise ValueError('File changed before archival removal')
            path.unlink();sync_directory(path.parent)
        item['archived']=archive.exists()
        atomic_write(journal/'journal.json',json.dumps(state,indent=2).encode(),0o600)
    state['status'] = 'rolled-back'
    atomic_write(journal / 'journal.json', json.dumps(state, indent=2).encode(), 0o600)
    return state

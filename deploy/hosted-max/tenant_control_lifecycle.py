"""Host provisioning hook: scoped control survives tenant Web replacement."""
import json
import os
from pathlib import Path
import re
import subprocess


def retired_binding(root, name, identity):
    if not re.fullmatch('[a-z][a-z0-9-]{0,30}', name) or not re.fullmatch('[a-f0-9]{32}', identity):
        raise ValueError('Invalid retired binding lookup')
    candidates = list((Path(root) / 'released-reservations').glob(name + '-' + identity + '-*/tenant/binding.json'))
    direct = Path(root) / 'tenants' / name / 'binding.json'
    if direct.exists():
        candidates.append(direct)
    for path in candidates:
        if path.is_symlink():
            continue
        row = json.loads(path.read_text())
        if (row.get('state') == 'deleted' and row.get('id') == identity
                and row.get('slug') == name and row.get('domain') == name + '.max-r.cc'):
            return {'deleted': True, 'tenant_id': identity, 'domain': row['domain']}
    return {'deleted': False}


def paths(row, root=Path('/var/lib/max-host-r'), system=Path('/etc')):
    if (not re.fullmatch('[a-f0-9]{32}', row.get('id', ''))
            or not re.fullmatch('[a-z][a-z0-9-]{0,30}', row.get('slug', ''))
            or row.get('domain') != row['slug'] + '.max-r.cc'):
        raise ValueError('Invalid tenant control identity')
    binding = root / 'tenants' / row['slug'] / 'binding.json'
    if binding.is_symlink() or json.loads(binding.read_text())['id'] != row['id']:
        raise ValueError('Host binding differs')
    name = 'max-tenant-control-' + row['id'] + '.service'
    return (binding, binding.parent / 'storage/.host-control/docker.sock',
            system / 'max-host-r/tenant-control' / (row['id'] + '.json'),
            system / 'systemd/system' / name)


def write(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError('Refusing control symlink')
    if path.exists() and path.read_text() == content:
        return False
    temporary = path.with_name(path.name + '.new')
    with temporary.open('x') as stream:
        os.chmod(temporary, mode)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return True


def ensure(row, root=Path('/var/lib/max-host-r'), system=Path('/etc'), run=subprocess.run):
    binding, sock, config, unit = paths(row, root, system)
    if sock.parent.is_symlink():
        raise ValueError('Invalid tenant socket directory')
    sock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_changed = write(config, json.dumps({'binding': str(binding), 'socket': str(sock),
                                             'tenant_id': row['id'], 'slug': row['slug']}))
    content = ('[Unit]\nDescription=MAX tenant scoped control\nAfter=docker.service max-host-r.service\nRequires=docker.service\n'
               '[Service]\nType=simple\nExecStart=/usr/bin/python3 -B /opt/max-host-r/current/backend/tenant_docker_proxy.py ' + str(config) +
               '\nRestart=on-failure\nRestartSec=5\nMemoryMax=64M\nTasksMax=32\nCPUQuota=30%\nNoNewPrivileges=true\nProtectSystem=strict\nProtectHome=true\nPrivateTmp=true\nRestrictAddressFamilies=AF_UNIX\nReadWritePaths=' + str(sock.parent) +
               '\nUMask=0077\n[Install]\nWantedBy=multi-user.target\n')
    unit_changed = write(unit, content, 0o644)
    if unit_changed:
        run(['systemctl', 'daemon-reload'], check=True, timeout=20)
    active = run(['systemctl', 'is-active', '--quiet', unit.name], check=False,
                 timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if not active:
        run(['systemctl', 'enable', '--now', unit.name], check=True, timeout=25)
    elif config_changed:
        run(['systemctl', 'restart', unit.name], check=True, timeout=25)
    return {'socket': '/app/storage/.host-control/docker.sock', 'tenant_id': row['id'], 'mode': 'hosted'}


def remove(row, root=Path('/var/lib/max-host-r'), system=Path('/etc'), run=subprocess.run):
    _, sock, config, unit = paths(row, root, system)
    if unit.exists():
        run(['systemctl', 'disable', '--now', unit.name], check=True, timeout=25)
        unit.unlink()
        run(['systemctl', 'daemon-reload'], check=True, timeout=20)
    if sock.exists():
        if not sock.is_socket():
            raise ValueError('Refusing to remove non-socket tenant data')
        sock.unlink()
    if config.exists():
        config.unlink()

"""Build a deterministic, runtime-only installer archive; never run the installer."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile

ROOT = Path(__file__).resolve().parents[1]
BASE = {
    'docker-compose.production.yml': 'docker-compose.production.yml',
    'database/schema_mysql.sql': 'database/schema_mysql.sql',
    'docker/l2tp/options.xl2tpd': 'docker/l2tp/options.xl2tpd',
    'storage/keys/master_public_key.pem': 'storage/keys/master_public_key.pem',
}
HOST = ('clock_ready.py', 'managed_files.py', 'active_files.py', 'manage.py',
        '99-max-radius-clock-ready.conf')
IMAGES = {'WEB': 'web', 'CORE': 'core', 'L2TP': 'l2tp',
          'DB': 'database', 'AUTOHEAL': 'autoheal'}


def build(root, manifest):
    if manifest.get('format') != 1 or manifest.get('platform') != 'linux/amd64':
        raise ValueError('Unsupported installer format or platform')
    if not re.fullmatch(r'[A-Za-z0-9._-]+', manifest['release']):
        raise ValueError('Invalid release name')
    members = {name: (root / source).read_bytes().replace(b'\r\n', b'\n')
               for name, source in BASE.items()}
    tools = manifest.get('host_tools', {}).get('clock_guard', 'none')
    if tools == 'v1':
        members.update({'host/clock-guard/' + name:
                        (root / 'deploy/clock-guard' / name).read_bytes().replace(b'\r\n', b'\n')
                        for name in HOST})
    elif tools != 'none':
        raise ValueError('Unsupported clock guard version')
    lines = []
    for key, role in IMAGES.items():
        reference = manifest['images'][role]
        if not re.fullmatch(r'[a-zA-Z0-9./_-]+@sha256:[a-f0-9]{64}', reference):
            raise ValueError('Every image must be pinned by digest')
        lines.append('MAX_' + key + '_IMAGE=' + reference)
    members['runtime-release.env'] = ('\n'.join(lines) + '\n').encode()
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode='wb', filename='', mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w', format=tarfile.USTAR_FORMAT) as archive:
            for name, content in sorted(members.items()):
                info = tarfile.TarInfo(name)
                info.size = len(content)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue(), members


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=ROOT / 'release.json')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--update-manifest', action='store_true')
    args = parser.parse_args()
    record = json.loads(args.manifest.read_text(encoding='utf-8-sig'))
    data, members = build(ROOT, record)
    output = args.output or ROOT / 'releases' / ('runtime-' + record['release'] + '.tar.gz')
    if output.exists() and output.read_bytes() != data:
        raise ValueError('Refusing to overwrite a different existing runtime archive')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    if args.update_manifest:
        record['bundle']['sha256'] = digest
        args.manifest.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
        frozen = {'release': record['release'], 'archive_sha256': digest,
                  'members_lf_sha256': {name: hashlib.sha256(value).hexdigest()
                                       for name, value in members.items()},
                  'scope': 'Immutable installer bundle; independent of future development source.'}
        (ROOT / 'releases' / ('installer-bundle-' + record['release'] + '.json')).write_text(
            json.dumps(frozen, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'bundle': str(output), 'sha256': digest,
                      'members': len(members), 'bytes': len(data)}))


if __name__ == '__main__':
    main()

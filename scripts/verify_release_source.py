"""Verify the published source snapshot without importing application modules."""
import argparse
import hashlib
import json
from pathlib import Path


def verify(root, manifest):
    record = json.loads(manifest.read_text(encoding='utf-8-sig'))
    differences = []
    for group in ('files_lf_sha256', 'build_files_lf_sha256'):
        for name, expected in record[group].items():
            path = (root / name).resolve()
            if not path.is_relative_to(root.resolve()):
                raise ValueError('Source manifest path escapes repository')
            if not path.is_file():
                differences.append({'file': name, 'reason': 'missing'})
            elif hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest() != expected:
                differences.append({'file': name, 'reason': 'changed'})
    return {'passed': not differences, 'version': record['version'],
            'source_files': len(record['files_lf_sha256']),
            'web_image': record['web_image'], 'differences': differences}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--manifest', default='releases/source-20261024-v2.6.5-batches-r1.json')
    args = parser.parse_args()
    result = verify(args.root, args.root / args.manifest)
    print(json.dumps(result, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

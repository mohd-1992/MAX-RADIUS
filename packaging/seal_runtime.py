"""Seal public runtime hashes using a BuildKit-only private signing secret."""
import base64
import hashlib
import json
from pathlib import Path
from cryptography.hazmat.primitives import serialization

root = Path('/app')
control = Path('/opt/max-protection')
control.mkdir(parents=True, exist_ok=True)
files = {}
for path in sorted(root.rglob('*')):
    relative = path.relative_to(root)
    if not path.is_file() or relative.parts[0] == 'storage' or relative.parts[:3] == ('web', 'static', 'uploads'):
        continue
    files[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
encoded = json.dumps({'format': 1, 'files': files}, sort_keys=True, separators=(',', ':')).encode()
key = serialization.load_pem_private_key(Path('/run/secrets/runtime_signing_key').read_bytes(), password=None)
(control / 'runtime-manifest.json').write_bytes(encoded)
(control / 'runtime-manifest.sig').write_bytes(base64.b64encode(key.sign(encoded)))
(control / 'runtime-public.pem').write_bytes(key.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))

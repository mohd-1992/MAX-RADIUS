"""Public startup integrity verifier; contains no developer private key."""
import base64,hashlib,json,sys
from pathlib import Path
from cryptography.hazmat.primitives import serialization

ROOT=Path('/app')
CONTROL=Path('/opt/max-protection')
def fail():
    print('MAX RUNTIME INTEGRITY CHECK FAILED',file=sys.stderr)
    raise SystemExit(78)

try:
    encoded=(CONTROL/'runtime-manifest.json').read_bytes()
    signature=base64.b64decode((CONTROL/'runtime-manifest.sig').read_bytes(),validate=True)
    key=serialization.load_pem_public_key((CONTROL/'runtime-public.pem').read_bytes())
    key.verify(signature,encoded)
    manifest=json.loads(encoded)
    expected=manifest['files']
    for relative,digest in expected.items():
        path=(ROOT/relative).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:fail()
    # An added package initializer or source file can change import behaviour even
    # when all original binaries retain their hashes. Reject unexpected app files.
    for path in ROOT.rglob('*'):
        relative=path.relative_to(ROOT)
        if not path.is_file() or relative.parts[0]=='storage' or relative.parts[:3]==('web','static','uploads'):continue
        if str(relative) not in expected:fail()
except SystemExit:raise
except Exception:fail()
print('MAX RUNTIME INTEGRITY OK')

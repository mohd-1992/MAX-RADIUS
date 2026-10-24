"""Explicitly executed inside the selected Web, never imported for indexing."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from core.config import APP_VERSION
from database.db import query_one,query_all
from services.license_guard_service import get_active_license_status

mode=sys.argv[1]
if mode not in ('before','candidate','final','rollback'):raise ValueError('Unknown probe mode')
license_state=get_active_license_status(force_refresh=True)
lease=query_one('SELECT is_valid,valid_until>UTC_TIMESTAMP() fresh FROM wisp_license_runtime_state WHERE id=1')
version_ok=APP_VERSION==('2.6.3' if mode in ('candidate','final') else '2.6.2')
payload=json.load(sys.stdin)
checkpoint=payload.get('checkpoint',[])
accounting_ok=True
if checkpoint:
    ids=[int(v['radacctid']) for v in checkpoint]
    rows=query_all('SELECT radacctid,acctuniqueid,acctinputoctets,acctoutputoctets,acctsessiontime FROM radacct WHERE radacctid IN ('+','.join('?' for _ in ids)+')',tuple(ids))
    actual={int(v['radacctid']):v for v in rows}
    accounting_ok=len(actual)==len(checkpoint) and all(int(v['radacctid']) in actual and actual[int(v['radacctid'])]['acctuniqueid']==v['acctuniqueid'] and all(int(actual[int(v['radacctid'])].get(k) or 0)>=int(v.get(k) or 0) for k in ('acctinputoctets','acctoutputoctets','acctsessiontime')) for v in checkpoint)
integrity=None
if mode in ('candidate','final'):
    integrity=subprocess.run(['python','/opt/max-protection/verify.py'],capture_output=True).returncode==0
else:
    # Before/rollback use the exact retained legacy Web, which predates the
    # signed runtime. Its frozen source hashes are checked by the host separately.
    integrity=Path('/app/services/system_update_service.py').is_file()
print(json.dumps({'passed':bool(version_ok and integrity and accounting_ok and license_state.get('valid') and lease and lease['is_valid'] and lease['fresh']),'version':APP_VERSION,'integrity':integrity,'accounting_checkpoint_rows':len(checkpoint),'accounting_preserved':accounting_ok,'license_valid':bool(license_state.get('valid')),'lease_valid':bool(lease and lease['is_valid'] and lease['fresh']),'publisher_key':hashlib.sha256(Path('/opt/max-protection/runtime-public.pem').read_bytes()).hexdigest() if Path('/opt/max-protection/runtime-public.pem').exists() else None}))

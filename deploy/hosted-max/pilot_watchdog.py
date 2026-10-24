"""One independent host recovery timer; accepted or resolved jobs are never replayed."""
import json
from pathlib import Path
import re
import subprocess
import sys

root=Path(sys.argv[1]);unit=sys.argv[2]
if not re.fullmatch('max-4k-pilot-[0-9a-f]{32}',unit):raise ValueError('Invalid worker unit')
state=json.loads((root/'state.json').read_text())
if (root/'ACCEPTED').exists() or state.get('status') in ('completed','rolled_back','recovery_required','error','unknown'):raise SystemExit(0)
subprocess.run(['systemctl','kill','--kill-whom=all','--signal=SIGKILL',unit],capture_output=True)
subprocess.run(['/usr/bin/python3',str(root.parent.parent/'pilot_host_engine.py'),'rollback',str(root)],check=True,timeout=300)

#!/usr/bin/python3
"""Linux fixture executable, never installed on the customer."""
import json
from pathlib import Path
import sys
import time

path=Path('/tmp/max-clock-fixture-probe.json')
state=json.loads(path.read_text())
state['calls']=state.get('calls',0)+1
path.write_text(json.dumps(state))
mode=state['mode']
if mode=='timeout':time.sleep(2)
if mode=='error':
    print('private-fixture-secret',file=sys.stderr)
    sys.exit(1)
print('yes' if mode=='yes' or mode=='delayed' and state['calls']>=2 else 'no')

"""Run only in the explicitly owned, network-none Linux clock fixture."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

if (os.environ.get('MAX_CLOCK_GUARD_FIXTURE')!='clock-boot-stability-20261017'
        or not Path('/.dockerenv').exists() or Path(__file__).resolve().parent!=Path('/fixture')):
    raise SystemExit('Refusing non-fixture environment')

HERE=Path('/fixture')
marker=Path('/run/systemd/timesync/synchronized')
state_path=Path('/tmp/max-clock-fixture-probe.json')
checks=[]

def scenario(name,mode,marker_present,expected,seconds=.5):
    if marker.exists():marker.unlink()
    if marker_present:marker.write_bytes(b'');marker.chmod(0o644)
    state_path.write_text(json.dumps({'mode':mode,'calls':0}))
    sentinel=Path('/tmp/max-clock-fixture-service-started')
    if sentinel.exists():sentinel.unlink()
    start=time.monotonic()
    p=subprocess.run(['/usr/bin/python3',str(HERE/'clock_ready.py'),'--wait-seconds',str(seconds),
                     '--poll-seconds','.03','--probe-timeout','.06'],capture_output=True,text=True,timeout=3)
    elapsed=time.monotonic()-start
    if p.returncode==0:sentinel.write_text('fixture service only')
    assert p.returncode==expected,(name,p.returncode,p.stderr)
    assert sentinel.exists()==(expected==0)
    assert elapsed<2,(name,elapsed)
    assert 'private-fixture-secret' not in p.stdout+p.stderr
    checks.append({'name':name,'passed':True,'exit':p.returncode,'elapsed_seconds':round(elapsed,3)})

scenario('confirmed time permits fixture start','yes',True,0)
scenario('kernel yes without boot marker refuses start','yes',False,75)
scenario('marker alone without synchronization refuses start','no',True,75)
scenario('delayed synchronization permits start','delayed',True,0)
scenario('DBus/probe failure refuses start without leaking stderr','error',True,75)
scenario('probe timeout stays bounded','timeout',True,75)

# Validate actual systemd parsing with representative Docker service dependencies.
units=Path('/tmp/max-clock-fixture-units');units.mkdir()
(units/'docker.service.d').mkdir()
shutil.copyfile(HERE/'99-max-radius-clock-ready.conf',units/'docker.service.d/99-max-radius-clock-ready.conf')
(units/'docker.service').write_text('[Unit]\nRequires=docker.socket\nAfter=docker.socket containerd.service\n[Service]\nType=notify\nExecStart=/bin/true\nRestart=always\nTimeoutStartSec=0\n')
(units/'docker.socket').write_text('[Socket]\nListenStream=/tmp/max-clock-fixture-docker.sock\n')
for name in ('containerd.service','systemd-timesyncd.service'):
    (units/name).write_text('[Service]\nType=oneshot\nExecStart=/bin/true\nRemainAfterExit=yes\n')
environment=dict(os.environ,SYSTEMD_UNIT_PATH=str(units)+':/usr/lib/systemd/system:/lib/systemd/system')
p=subprocess.run(['systemd-analyze','verify','--man=no',str(units/'docker.service')],capture_output=True,text=True,env=environment,timeout=15)
assert p.returncode==0,p.stderr
checks.append({'name':'systemd validates merged service and drop-in ordering','passed':True})

# Full public CLI with an absent real DBus must fail, never set the host clock.
marker.unlink(missing_ok=True)
start=time.monotonic()
p=subprocess.run(['/usr/bin/python3',str(HERE/'clock_ready.py'),'--wait-seconds','.2','--poll-seconds','.02'],capture_output=True,text=True,timeout=2)
assert p.returncode==75 and time.monotonic()-start<1
checks.append({'name':'standalone CLI with no readiness returns temporary failure','passed':True})

source_hashes={name:hashlib.sha256((HERE/name).read_bytes()).hexdigest() for name in ('clock_ready.py','99-max-radius-clock-ready.conf','managed_files.py')}
print(json.dumps({'fixture':'clock-boot-stability-20261017','checks':checks,'passed':len(checks),'source_hashes':source_hashes,
                  'network':'none','customer_database_used':False,'real_boot_tested':False,'systemd_is_pid1':False,'privileged':False}))
